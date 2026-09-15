"""Static "will it run" checks — undefined names + call-signature mismatches.

`check_source(text, path=None)` -> [(line, message)], 1-based lines aligned with
the buffer. Runs on chain_in's background thread AFTER libcst and compile()
both passed (see _run_chain_in), so the source is known syntactically valid and
this pass only adds the NameError / TypeError / AttributeError class of
mistakes those let through:

  * a Name load that no enclosing scope, builtin, or live-module global binds
    ("name 'myvarr' is not defined"). When the name is satisfiable by an
    import — an importable top-level module, or a name other live modules got
    from an import (`np`, `Path`, ...) — the message carries the exact fixing
    statement after a "missing import:" marker, e.g.
    "name 'np' is not defined — missing import: import numpy as np", so the
    editor can both style it distinctly and offer the auto-import fix.
  * a call to a function/class DEFINED IN THIS BUFFER whose arguments can't
    bind (unknown kwarg, too many positionals, missing required args)
  * the same signature check against LIVE objects — a bare imported name
    (`request_render(1, 2, 3)`), a builtin (`isinstance(x)`), or a dotted
    module attribute (`imgui.dummy()`), resolved through the running process's
    module for `path`. C/Cython callables that hide their signature from
    inspect fall back to the embedsignature doc line ("dummy(width, height)").
    When the callee's DEFINING file has unsaved edits, the expected signature
    comes from that file's pending text instead of the live object (which
    reflects the last compile) — see the pending-truth signature section.
    Span buffers (only_missing_imports) run the same call check through the
    enclosing module's pending text (_check_call_span).
  * a missing attribute on a live MODULE (`imgui.dummyy`) — chains only ever
    walk through module objects, never arbitrary instances

Every rule errs on SILENCE — a missed problem beats a false alarm in an editor
that flags while you type:

  * `from x import *` anywhere disables the name pass for the whole buffer
  * signature checks skip decorated defs (a decorator can change the signature
    arbitrarily — and @render_func always does), rebound/duplicated names, and
    calls through anything we can't pin to a def in the buffer or a live object
  * live objects carrying `__wrapped__` are skipped — inspect.signature follows
    the wrap, but the wrapper may inject arguments (render_func's draw_state)
  * a call using *args / **kwargs expansion skips the counts it makes unknowable
  * annotations are never name-checked (string/forward refs, future-import)
  * `'name' in globals()` guards bind the tested name; a try body whose except
    catches NameError suppresses name checks, TypeError/AttributeError ones
    suppress signature/attribute checks (probing is the handled case)
  * missing-attr checks skip modules with a PEP 562 `__getattr__`, dotted paths
    the buffer itself imports (`import a.b` ⇒ `a.b` will exist), and attrs the
    buffer assigns (`mod.flag = True` earlier in the file)
  * scope analysis is flow-insensitive: module-level code may legally use names
    defined later (function bodies run later), so order is ignored

Name resolution follows Python's actual rule — local scope, enclosing FUNCTION
scopes (class scopes are invisible to nested functions), module, builtins —
plus one editor-specific fallback: the LIVE module's namespace when `path` maps
to an entry in sys.modules, so names a hotswap/exec injected at runtime don't
flag even though no static binding exists.
"""

import ast
import builtins
import inspect
import os
import sys
import time
import types

from meltygui.notifications import lag_traced

# Names every module/frame sees without a visible binding.
_BUILTIN_NAMES = frozenset(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__path__", "__class__",
    "__annotations__", "__dict__", "__module__", "__qualname__",
}

_MISS = object()


class _Scope:

    def __init__(self, kind, parent):
        self.kind = kind            # 'module' | 'function' | 'class' | 'comp'
        self.parent = parent
        self.binds = set()          # every name bound somewhere in this scope
        self.import_binds = set()   # names bound by an import statement
        self.other_binds = set()    # names bound by anything else
        self.ambiguous = set()      # rebound names - excluded from signature checks
        self.defs = {}              # name -> (FunctionDef, flavor) - undecorated, bound once
        self.classes = {}           # name -> (ClassDef, class _Scope) - undecorated, bound once
        self.loads = []             # (name, lineno) Name loads made in this scope
        self.global_names = set()   # names declared `global` here
        self.self_name = None       # method scopes: the first arg ('self'/'cls')

    def bind(self, name, is_import=False):
        if name in self.binds:
            # A second binding makes "which def is this name" unknowable for the
            # signature pass; the name pass only needs set membership.
            self.defs.pop(name, None)
            self.classes.pop(name, None)
            self.ambiguous.add(name)
        else:
            self.binds.add(name)
        (self.import_binds if is_import else self.other_binds).add(name)


# Project decorator conventions, matched BY NAME (bare or called). This is a
# project-specific lint, so the names are trusted without resolving them:
#   * transparent - leave the function UNCHANGED (window_decoration.window,
#     core_decoration.defaults and app.glfw_window only register), so the
#     def's own signature covers the calling code, in any order relative
#     to @render_func.
#   * wrapper — @render_func replaces the def with core_render's
#     `wrapper(input_value=None, **kwargs)`: at most ONE positional, any
#     kwarg accepted (modes/defaults/comment-args may fill required params,
#     so only the positional shape is checkable).
_TRANSPARENT_DECORATORS = frozenset({"window", "defaults", "glfw_window"})
_WRAPPER_DECORATORS = frozenset({"render_func"})


def _decorator_name(dec):
    """The bare name a decorator is spelled with (`@window` / `@window(...)`),
    or None for anything dotted/complex."""
    f = dec.func if isinstance(dec, ast.Call) else dec
    return f.id if isinstance(f, ast.Name) else None


def _decorator_flavor(decorator_list):
    """'plain' / 'static' / 'class' when the signature is still trustworthy,
    None when a decorator could have changed it. Besides the two builtins,
    transparent project decorators (@window) keep the def's real signature."""
    if not decorator_list:
        return "plain"
    if len(decorator_list) == 1 and isinstance(decorator_list[0], ast.Name):
        if decorator_list[0].id == "staticmethod":
            return "static"
        if decorator_list[0].id == "classmethod":
            return "class"
    if all(_decorator_name(d) in _TRANSPARENT_DECORATORS
           for d in decorator_list):
        return "plain"
    return None


def _unwind_chain(node):
    """node and its .value ancestry as (base Name, [(attr, node)…]) when the
    whole chain is Name.attr.attr…, else (None, None)."""
    attrs = []
    cur = node
    while isinstance(cur, ast.Attribute):
        attrs.append((cur.attr, cur))
        cur = cur.value
    if isinstance(cur, ast.Name) and isinstance(cur.ctx, ast.Load):
        attrs.reverse()
        return cur, attrs
    return None, None


def _handler_catches(handler_type, names):
    if handler_type is None:
        return True                                  # bare except
    if isinstance(handler_type, ast.Tuple):
        return any(_handler_catches(e, names) for e in handler_type.elts)
    return isinstance(handler_type, ast.Name) and handler_type.id in names


def _analysis_checkpoint():
    """Let input/rendering run between bounded pieces of background analysis."""
    import threading
    if threading.current_thread() is threading.main_thread():
        return
    from meltygui.code.libcst_conversion import _yield_to_ui
    _yield_to_ui()  # also guards a non-main GL thread


def _analysis_matches(pattern, text):
    import re
    _analysis_checkpoint()
    for index, match in enumerate(re.finditer(pattern, text)):
        if index and index % 128 == 0:
            _analysis_checkpoint()
        yield match.group(1)


def _parse_for_analysis(text):
    _analysis_checkpoint()
    result = ast.parse(text)
    _analysis_checkpoint()
    return result


class _Collector:
    """One pass over the tree building the scope graph + the check worklists."""

    def __init__(self):
        self._visited = 0
        self.module = _Scope("module", None)
        self.scopes = [self.module]
        self.calls = []             # (Call node, scope, type_guarded)
        self.attr_chains = []       # (base Name node, [(attr, node)...], scope, guarded)
        self.declared_attrs = set() # dotted paths the buffer itself makes visible:
                                    # `import a.b` / `from a.b import c` / `a.b = ...`
        self.star_import = False
        self.star_modules = []      # `from X import *` source module names
        self._name_guard = 0        # >0 inside try guarded by except NameError
        self._type_guard = 0        # >0 inside try guarded by except TypeError/AttributeError

    def run(self, tree):
        self._body(tree.body, self.module)

    def __del__(self):
        # The scope graph is cyclic: child.parent points up while
        # parent.classes[name] = (ClassDef, child) points down - and defs /
        # classes / calls hold ast nodes, so every pass left the ENTIRE parsed
        # tree as cyclic garbage that only a full gc pass could reclaim (the
        # gc profile of this session's boot collect: 680k objects, ~all ast.*
        # nodes + their __dict__/body lists). Collectors are function-local
        # everywhere, so cut the up-edges here and the tree frees by refcount
        # the moment the pass returns.
        for s in self.scopes:
            s.parent = None

    # ── plumbing ────────────────────────────────────────────────────────────
    def _new_scope(self, kind, parent):
        s = _Scope(kind, parent)
        self.scopes.append(s)
        return s

    def _body(self, stmts, scope):
        for stmt in stmts:
            self._visit(stmt, scope)

    def _visit(self, node, scope):
        self._visited += 1
        if self._visited % 128 == 0:
            _analysis_checkpoint()
        meth = getattr(self, "_v_" + type(node).__name__, None)
        if meth is not None:
            meth(node, scope)
            return
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope)

    # ── scope makers ────────────────────────────────────────────────────────
    def _function(self, node, scope, name=None):
        flavor = _decorator_flavor(getattr(node, "decorator_list", []))
        if name is not None:
            scope.bind(name)
            if flavor is not None and name not in scope.ambiguous:
                scope.defs[name] = (node, flavor)
        for dec in getattr(node, "decorator_list", []):
            self._visit(dec, scope)
        a = node.args
        for default in list(a.defaults) + [d for d in a.kw_defaults if d is not None]:
            self._visit(default, scope)
        # node.returns / arg annotations deliberately not handled (see above).
        child = self._new_scope("function", scope)
        all_args = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
        for arg in all_args:
            child.bind(arg.arg)
        for var in (a.vararg, a.kwarg):
            if var is not None:
                child.bind(var.arg)
        if scope.kind == "class" and flavor == "plain" and all_args:
            child.self_name = all_args[0].arg
        body = node.body if isinstance(node.body, list) else [node.body]
        self._body(body, child)

    def _v_FunctionDef(self, node, scope):
        self._function(node, scope, name=node.name)

    def _v_AsyncFunctionDef(self, node, scope):
        self._function(node, scope, name=node.name)

    def _v_Lambda(self, node, scope):
        self._function(node, scope)

    def _v_ClassDef(self, node, scope):
        scope.bind(node.name)
        for dec in node.decorator_list:
            self._visit(dec, scope)
        for base in list(node.bases) + list(node.keywords):
            self._visit(base, scope)
        child = self._new_scope("class", scope)
        if not node.decorator_list and node.name not in scope.ambiguous:
            scope.classes[node.name] = (node, child)
        self._body(node.body, child)

    def _comp(self, node, scope):
        child = self._new_scope("comp", scope)
        first = True
        for gen in node.generators:
            self._visit(gen.iter, scope if first else child)
            first = False
            self._visit(gen.target, child)
            for cond in gen.ifs:
                self._visit(cond, child)
        for field in ("elt", "key", "value"):
            sub = getattr(node, field, None)
            if sub is not None:
                self._visit(sub, child)

    _v_ListComp = _v_SetComp = _v_GeneratorExp = _v_DictComp = _comp

    def _try(self, node, scope):
        # Code in a guarded try body is PROBING - that's the handled case, not
        # a bug. except NameError guards the name pass; except TypeError /
        # AttributeError guard the signature / missing-attr pass. The broad
        # handlers guard everything.
        broad = ("Exception", "BaseException")
        name_guarded = any(_handler_catches(h.type, ("NameError",) + broad)
                           for h in node.handlers)
        type_guarded = any(_handler_catches(h.type, ("TypeError", "AttributeError") + broad)
                           for h in node.handlers)
        self._name_guard += name_guarded
        self._type_guard += type_guarded
        self._body(node.body, scope)
        self._name_guard -= name_guarded
        self._type_guard -= type_guarded
        for h in node.handlers:
            self._visit(h, scope)
        self._body(node.orelse, scope)
        self._body(node.finalbody, scope)

    _v_Try = _v_TryStar = _try

    # ── binders and loads ─────────────────────────────────────────────────────
    def _v_Name(self, node, scope):
        if isinstance(node.ctx, ast.Load):
            if not self._name_guard:
                scope.loads.append((node.id, node.lineno))
        else:
            scope.bind(node.id)
            if node.id in scope.global_names:
                self.module.bind(node.id)

    def _v_Global(self, node, scope):
        scope.global_names.update(node.names)
        for name in node.names:
            # Permissive both ways: resolvable here, and at module level (this
            # function may be the module-level name's only definer).
            scope.bind(name)
            self.module.bind(name)

    def _v_Nonlocal(self, node, scope):
        # compile() already rejected a nonlocal with no enclosing binding.
        for name in node.names:
            scope.bind(name)

    def _v_Import(self, node, scope):
        for alias in node.names:
            scope.bind(alias.asname or alias.name.partition(".")[0],
                       is_import=True)
            # `import a.b.c` guarantees a.b and a.b.c exist as attributes.
            parts = alias.name.split(".")
            for i in range(1, len(parts) + 1):
                self.declared_attrs.add(".".join(parts[:i]))

    def _v_ImportFrom(self, node, scope):
        if node.module and not node.level:
            parts = node.module.split(".")
            for i in range(1, len(parts) + 1):
                self.declared_attrs.add(".".join(parts[:i]))
        for alias in node.names:
            if alias.name == "*":
                self.star_import = True
                # Which module the * came from (None for relative imports) -
                # _module_text_binds resolves the export list through the
                # LIVE module so a star import doesn't force the whole
                # binds answer to "unknowable".
                self.star_modules.append(node.module if not node.level else None)
            else:
                scope.bind(alias.asname or alias.name, is_import=True)
                if node.module and not node.level:
                    # `from a.b import c` makes c an attribute of a.b too.
                    self.declared_attrs.add(f"{node.module}.{alias.name}")

    def _v_ExceptHandler(self, node, scope):
        if node.name:
            scope.bind(node.name)
        if node.type is not None:
            self._visit(node.type, scope)
        self._body(node.body, scope)

    def _v_NamedExpr(self, node, scope):
        # A walrus binds in the nearest enclosing non-comprehension scope; bind
        # in the comp scope too so later use inside the same comp resolves.
        target = scope
        while target.kind == "comp":
            target = target.parent
        target.bind(node.target.id)
        if scope is not target:
            scope.bind(node.target.id)
        self._visit(node.value, scope)

    def _v_AnnAssign(self, node, scope):
        self._visit(node.target, scope)   # binds (Store ctx)
        if node.value is not None:
            self._visit(node.value, scope)
        # node.annotation deliberately ignored.

    def _v_MatchAs(self, node, scope):
        if node.name:
            scope.bind(node.name)
        if node.pattern is not None:
            self._visit(node.pattern, scope)

    def _v_MatchStar(self, node, scope):
        if node.name:
            scope.bind(node.name)

    def _v_MatchMapping(self, node, scope):
        if node.rest:
            scope.bind(node.rest)
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope)

    def _v_TypeAlias(self, node, scope):  # py3.12 `type X = ...` - annotation-like
        if isinstance(node.name, ast.Name):
            scope.bind(node.name.id)

    def _v_Call(self, node, scope):
        self.calls.append((node, scope, self._type_guard > 0))
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope)

    def _v_Attribute(self, node, scope):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            # `obj.flag = True` makes the attr exist at runtime - declare the
            # whole dotted path so a later LOAD of it doesn't flag as missing.
            base, attrs = _unwind_chain(node.value)
            if base is not None:
                self.declared_attrs.add(
                    ".".join([base.id] + [a for a, _ in attrs] + [node.attr]))
            self._visit(node.value, scope)
            return
        base, attrs = _unwind_chain(node)
        if base is not None:
            self.attr_chains.append(
                (base, attrs, scope, self._name_guard > 0 or self._type_guard > 0))
            # We don't recurse into the chain, so record the base node here.
            if not self._name_guard:
                scope.loads.append((base.id, base.lineno))
            return
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope)

    def _v_Compare(self, node, scope):
        # `'name' in globals()` is a deliberate is-it-re-bound-yet guard
        # (latent_descent's server hooks). Treat the tested name as module-bound
        # so the guarded load it gates doesn't flag.
        if (len(node.ops) == 1 and isinstance(node.ops[0], ast.In)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
                and node.left.value.isidentifier()
                and isinstance(node.comparators[0], ast.Call)
                and isinstance(node.comparators[0].func, ast.Name)
                and node.comparators[0].func.id in ("globals", "locals", "vars", "dir")):
            self.module.bind(node.left.value)
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope)


# ── name resolution ──────────────────────────────────────────────────────────

def _resolves(scope, name):
    """Python's lookup rule: own scope, then enclosing scopes SKIPPING class
    scopes (a class body is invisible to the functions nested inside it)."""
    own = True
    s = scope
    while s is not None:
        if (own or s.kind != "class") and name in s.binds:
            return True
        own = False
        s = s.parent
    return False


def _binding_scope(scope, name):
    """The scope whose binding a load of `name` would see, or None."""
    own = True
    s = scope
    while s is not None:
        if (own or s.kind != "class") and name in s.binds:
            return s
        own = False
        s = s.parent
    return None


def _lookup_def(scope, name):
    """The def/class a bare-name call statically pins to, or None. The FIRST
    scope binding the name decides (a nearer non-def binding shadows an outer
    def → unknowable → None). Class-scope defs are skipped except from the
    class body itself — a bare-name method call is an unbound call whose
    `self` convention we can't assume."""
    s = _binding_scope(scope, name)
    if s is None or name in s.ambiguous:
        return None
    if name in s.defs and s.kind != "class":
        return ("func", *s.defs[name])
    if name in s.classes:
        return ("cls", *s.classes[name])
    return None


def _enclosing_method_class(scope, base_name):
    """For a `self.m(...)` call: the class scope owning the method we're inside,
    if `base_name` is that method's first arg. None otherwise."""
    s = scope
    while s is not None:
        if s.kind == "function" and s.self_name == base_name:
            parent = s.parent
            return parent if parent is not None and parent.kind == "class" else None
        if s.kind == "function" and s.self_name is None:
            return None     # an inner plain def shadows the method's self
        s = s.parent
    return None


# ── call-signature matching ──────────────────────────────────────────────

class _Spec:
    """One callable's parameters, however we learned them (buffer ast,
    inspect.signature, or a Cython embedsignature doc line)."""

    def __init__(self):
        self.named = []             # positional(-or-keyword) names, in order
        self.n_posonly = 0          # leading slice of `named` not passable by kw
        self.required = set()       # names with no default (positional + kwonly)
        self.kwonly = []
        self.kw_required = set()
        self.has_var_pos = False
        self.has_var_kw = False
        self.types = {}             # name -> 'float'|'int'|'str'|'bool', only
                                    # where known (doc C type / annotation)


def _spec_from_arguments(a, skip_first=0):
    spec = _Spec()
    named = [x.arg for x in a.posonlyargs] + [x.arg for x in a.args]
    n_posonly = len(a.posonlyargs)
    if skip_first:
        if not named:
            return None             # *args-only def used as a method - fine
        named = named[1:]
        n_posonly = max(0, n_posonly - 1)
    spec.named = named
    spec.n_posonly = n_posonly
    spec.required = set(named[:len(named) - len(a.defaults)])
    spec.kwonly = [x.arg for x in a.kwonlyargs]
    spec.kw_required = {x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is None}
    spec.required |= spec.kw_required
    spec.has_var_pos = a.vararg is not None
    spec.has_var_kw = a.kwarg is not None
    for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
        ann = arg.annotation
        if isinstance(ann, ast.Name) and ann.id in _CHECKED_TYPES:
            spec.types[arg.arg] = ann.id
    return spec


def _spec_from_signature(sig):
    P = inspect.Parameter
    spec = _Spec()
    for p in sig.parameters.values():
        ann = p.annotation
        if isinstance(ann, type) and ann.__name__ in _CHECKED_TYPES:
            spec.types[p.name] = ann.__name__
        elif isinstance(ann, str) and ann in _CHECKED_TYPES:
            spec.types[p.name] = ann
        if p.kind in (P.POSITIONAL_ONLY, P.POSITIONAL_OR_KEYWORD):
            spec.named.append(p.name)
            if p.kind == P.POSITIONAL_ONLY:
                spec.n_posonly = len(spec.named)
            if p.default is P.empty:
                spec.required.add(p.name)
        elif p.kind == P.KEYWORD_ONLY:
            spec.kwonly.append(p.name)
            if p.default is P.empty:
                spec.kw_required.add(p.name)
                spec.required.add(p.name)
        elif p.kind == P.VAR_POSITIONAL:
            spec.has_var_pos = True
        elif p.kind == P.VAR_KEYWORD:
            spec.has_var_kw = True
    return spec


def _split_top_level(text):
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _spec_from_doc(fname, doc):
    """Cython embedsignature fallback: pyimgui-style C functions raise on
    inspect.signature but lead their docstring with `dummy(width, height)` /
    `arrow_button(str label, ImGuiDir direction=DIRECTION_NONE)`. Parse that
    line into a spec; param tokens may carry a C type prefix (drop it). Anything
    that doesn't look exactly like a signature line → None (no check)."""
    if not doc:
        return None
    lines = doc.strip().split("\n")
    line = lines[0].strip()
    # The doc line may sign itself with an ALIAS TARGET's name - pyimgui's
    # set_cursor_position shares set_cursor_pos's doc ("set_cursor_pos(
    # local_position)") - so accept any identifier-headed signature line and use
    # ITS name for the overload scan; `fname` stays the alias's spelling for
    # messages. Prose first lines fail the identifier/paren test here or the
    # strict per-piece validation below.
    docname = line.partition("(")[0].strip()
    if not docname.isidentifier() or not line.startswith(docname + "("):
        return None
    # Polymorphic callables document each overload on its own line - bare
    # ("slice(stop)" / "slice(start, stop[, step])") or directive-marked the way
    # torch does (".. function:: mean(input, dim, ...)"). The first line alone
    # is a lie, so any bare overload line disqualifies the whole fallback.
    for more in lines[1:]:
        more = more.strip()
        for marker in (".. function::", ".. method::"):
            if more.startswith(marker):
                more = more[len(marker):].strip()
        if more.startswith(docname + "("):
            return None
    # Take exactly the BALANCED paren group after the name - torch-style lines
    # carry a return annotation after it ("sort(input, ...) -> (Tensor,
    # LongTensor)") that must not leak into the spec.
    head = line[len(docname):]
    inner = tail = None
    depth = 0
    for i, ch in enumerate(head):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                inner = head[1:i].strip()
                tail = head[i + 1:].strip()
                break
    if inner is None or (tail and not tail.startswith("->")):
        return None
    spec = _Spec()
    if not inner:
        return spec
    kwonly = False
    for piece in _split_top_level(inner):
        piece = piece.strip()
        if not piece or piece == "...":
            return None             # can't check a partial signature
        if piece == "/":
            spec.n_posonly = len(spec.named)
            continue
        if piece == "*":
            kwonly = True
            continue
        if piece.startswith("**"):
            spec.has_var_kw = True
            continue
        if piece.startswith("*"):
            spec.has_var_pos = True
            kwonly = True
            continue
        name_part = piece.split("=", 1)[0].strip()
        tokens = name_part.split()
        pname = tokens[-1] if tokens else ""
        if not pname.isidentifier():
            return None
        if len(tokens) >= 2:
            # C type prefix ("float position") - convert the ones the literal
            # type check knows; unknown types simply aren't checked.
            t = _DOC_TYPE_MAP.get(tokens[-2].lstrip("*"))
            if t is not None:
                spec.types[pname] = t
        has_default = "=" in piece
        if kwonly:
            spec.kwonly.append(pname)
            if not has_default:
                spec.kw_required.add(pname)
                spec.required.add(pname)
        else:
            # A required positional AFTER a default one is illegal in real
            # Python - the doc line is describing overload shorthand (torch's
            # "arange(start=0, end, step=1)"), not a signature. Trust nothing.
            if not has_default and any(n not in spec.required for n in spec.named):
                return None
            spec.named.append(pname)
            if not has_default:
                spec.required.add(pname)
    return spec


def _match_spec(fname, spec, call):
    """One mismatch message, or None. Mirrors CPython's binding rules but
    checks only what the call makes knowable: *args in the call hides
    positional counts, ** hides keyword coverage."""
    # A *splat normally makes the positional count unknowable - EXCEPT a
    # literal tuple/list (`f(*(0, 0))`), whose count is right there.
    star_args = False
    npos = 0
    for x in call.args:
        if isinstance(x, ast.Starred):
            v = x.value
            if (isinstance(v, (ast.Tuple, ast.List))
                    and not any(isinstance(e, ast.Starred) for e in v.elts)):
                npos += len(v.elts)
            else:
                star_args = True
        else:
            npos += 1
    kw_expand = any(k.arg is None for k in call.keywords)
    kw_names = [k.arg for k in call.keywords if k.arg is not None]

    if not spec.has_var_kw:
        allowed = set(spec.named[spec.n_posonly:]) | set(spec.kwonly)
        for k in kw_names:
            if k not in allowed:
                return f"{fname}() got an unexpected keyword argument '{k}'"

    if star_args:
        return None                 # positional count unknowable from here on

    if npos > len(spec.named) and not spec.has_var_pos:
        plural = "s" if len(spec.named) != 1 else ""
        return (f"{fname}() takes {len(spec.named)} positional argument{plural} "
                f"but {npos} were given")

    consumed = set(spec.named[:npos])
    for k in kw_names:
        if k in consumed:
            return f"{fname}() got multiple values for argument '{k}'"

    if not kw_expand:
        kw_set = set(kw_names)
        missing = [p for i, p in enumerate(spec.named)
                   if i >= npos and p in spec.required and p not in kw_set]
        missing += [k for k in spec.kwonly if k in spec.kw_required and k not in kw_set]
        if missing:
            listed = ", ".join(f"'{m}'" for m in missing)
            plural = "s" if len(missing) != 1 else ""
            return f"{fname}() missing required argument{plural}: {listed}"
    return _literal_type_mismatch(fname, spec, call)


# Declared param types the literal check knows, and the literal types each
# accepts. Deliberately narrower than Python's coercion rules: bool→float is
# LEGAL at runtime (bool is an int) but `same_line(False)` is a bug every
# time it's written, so numeric params reject bool literals. None literals
# are never checked (Optional params are indistinguishable statically).
_CHECKED_TYPES = frozenset({"float", "int", "str", "bool"})
_TYPE_ACCEPTS = {
    "float": ("float", "int"),
    "int": ("int",),
    "str": ("str",),
    "bool": ("bool",),
}
# Doc C-type spellings → the checked type they mean; anything else unchecked.
_DOC_TYPE_MAP = {
    "float": "float", "double": "float",
    "int": "int", "long": "int", "short": "int", "unsigned": "int",
    "size_t": "int", "Py_ssize_t": "int",
    "bool": "bool", "bint": "bool",
    "str": "str", "string": "str",
}


def _literal_arg_type(node):
    """'bool'/'int'/'float'/'str' when the argument is that LITERAL (unary
    +/- kept for numbers), else None — expressions, names, calls and None
    literals are never type-checked."""
    if isinstance(node, ast.Constant):
        v = node.value
        if v is True or v is False:
            return "bool"           # before int - bool subclasses int
        if isinstance(v, float):
            return "float"
        if isinstance(v, int):
            return "int"
        if isinstance(v, str):
            return "str"
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        t = _literal_arg_type(node.operand)
        return t if t in ("int", "float") else None
    return None


def _literal_type_mismatch(fname, spec, call):
    """A wrong-TYPE message for a LITERAL argument against a DECLARED param
    type (doc C type or float/int/str/bool annotation), or None. Runs last in
    _match_spec, so the call already binds; only (declared, literal) pairs
    the tables above know are judged — everything else is silence."""
    if not spec.types:
        return None
    try:
        from meltygui.toggles import Toggles
        if not Toggles.TextEditor.lint_literal_types:
            return None
    except Exception:
        pass
    flat = []                       # positional args incl. any splats
    for x in call.args:
        if isinstance(x, ast.Starred):
            flat.extend(x.value.elts)   # unknowable splats bailed earlier
        else:
            flat.append(x)
    pairs = list(zip(spec.named, flat))
    pairs += [(k.arg, k.value) for k in call.keywords
              if k.arg is not None and k.arg in spec.types]
    for pname, node in pairs:
        want = spec.types.get(pname)
        if want is None:
            continue
        got = _literal_arg_type(node)
        if got is None or got in _TYPE_ACCEPTS[want]:
            continue
        if want == "int" and got == "float":
            # An INTEGRAL float literal (drag_int(min_value=0.0)) coerces
            # losslessly and is common working code - only a fractional
            # literal (2.5) is provably wrong.
            try:
                v = ast.literal_eval(node)
            except Exception:
                continue
            if isinstance(v, float) and v.is_integer():
                continue
        return f"{fname}() expected {want} for '{pname}', got {got}"
    return None


def _check_call_static(call, scope):
    """Signature check against defs/classes in the BUFFER. Returns the message
    or None; sets nothing aside — a None just means 'nothing provably wrong'."""
    func = call.func
    if isinstance(func, ast.Name):
        spec_info = _lookup_def(scope, func.id)
        if spec_info is None:
            return None
        if spec_info[0] == "func":
            _, node, flavor = spec_info
            spec = _spec_from_arguments(node.args,
                                        skip_first=1 if flavor == "class" else 0)
            return _match_spec(func.id, spec, call) if spec else None
        _, node, cls_scope = spec_info
        init = cls_scope.defs.get("__init__")
        if init is not None and init[1] == "plain":
            spec = _spec_from_arguments(init[0].args, skip_first=1)
            return _match_spec(func.id, spec, call) if spec else None
        if (init is None and not node.bases and not node.keywords
                and "__init__" not in cls_scope.binds
                and "__new__" not in cls_scope.binds):
            if call.args or call.keywords:
                return f"{func.id}() takes no arguments"
        return None
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        cls_scope = _enclosing_method_class(scope, func.value.id)
        if cls_scope is None or func.attr in cls_scope.ambiguous:
            return None
        method = cls_scope.defs.get(func.attr)
        if method is None:
            return None             # not defined directly - could be inherited
        node, flavor = method
        spec = _spec_from_arguments(node.args,
                                    skip_first=0 if flavor == "static" else 1)
        return _match_spec(func.attr, spec, call) if spec else None
    return None


# ── live-module resolution ───────────────────────────────────────────────────

class _LiveCtx:

    def __init__(self, module, declared):
        try:
            self.ns = dict(vars(module)) if module is not None else None
        except TypeError:
            self.ns = None
        self.declared = declared


def _module_for(path):
    """The live module object whose __file__ is `path`, or None — covers
    hotswap/exec-injected globals no static binding accounts for. The same
    file can appear in sys.modules under SEVERAL names (the studio holds both
    `src.lsd...` and `lsd...` aliases, one of them a barely-initialized stub) —
    take the match with the richest namespace, that's the one that actually
    ran."""
    if not path:
        return None
    target = str(path)
    try:
        target_real = os.path.realpath(target)
    except OSError:
        target_real = target
    best = None
    best_size = -1
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f is not None and (f == target or f == target_real):
            try:
                size = len(vars(mod))
            except TypeError:
                continue
            if size > best_size:
                best, best_size = mod, size
    return best


def _import_only(scope, name):
    """True when the binding a load would see comes ONLY from import statements
    — the one case where the live module's value for the name is trustworthy
    (an assignment/def in the buffer may not match the running process yet)."""
    s = _binding_scope(scope, name)
    return (s is not None and name in s.import_binds
            and name not in s.other_binds)


def _resolve_live_base(ctx, scope, name):
    if ctx.ns is None or not _import_only(scope, name):
        return _MISS
    return ctx.ns.get(name, _MISS)


def _walk_chain(ctx, scope, base, attrs):
    """Resolve base.attr.attr… through LIVE objects. Returns (object, report):
    object is _MISS when unresolvable, report is a (line, msg) missing-attr
    finding. Only ever steps through MODULE objects — instances can hide
    anything behind descriptors/__getattr__, so we stop (silently) at them
    unless they're the final resolved value."""
    obj = _resolve_live_base(ctx, scope, base.id)
    if obj is _MISS:
        return _MISS, None
    dotted = base.id
    for attr, node in attrs:
        if not isinstance(obj, types.ModuleType):
            return _MISS, None
        dotted += "." + attr
        nxt = inspect.getattr_static(obj, attr, _MISS)
        if nxt is _MISS:
            if dotted in ctx.declared or "__getattr__" in vars(obj):
                return _MISS, None  # buffer imports/assigns it, or lazy module
            return _MISS, (node.lineno,
                           f"module '{obj.__name__}' has no attribute '{attr}'")
        obj = nxt
    return obj, None


def _live_spec(obj, fname):
    if hasattr(obj, "__wrapped__"):
        return None                 # the wrapper may inject args (@render_func)
    try:
        return _spec_from_signature(inspect.signature(obj))
    except (ValueError, TypeError):
        # C/Cython callable hiding its signature - pyimgui embeds it in the
        # docstring's first line instead. Real function objects only: a
        # callable INSTANCE hiding its signature (PyOpenGL's glDrawBuffers
        # wrapper) documents C args that parse into the wrong arity.
        if not isinstance(obj, (types.BuiltinFunctionType, types.MethodType)):
            return None
        return _spec_from_doc(fname, getattr(obj, "__doc__", None))


def _check_call_live(ctx, call, scope):
    """Signature check against the LIVE object a call resolves to: a bare
    import-bound name, a builtin, or a dotted module-attribute chain."""
    func = call.func
    obj, fname = _MISS, None
    if isinstance(func, ast.Name):
        fname = func.id
        obj = _resolve_live_base(ctx, scope, fname)
        if obj is _MISS and not _resolves(scope, fname):
            obj = getattr(builtins, fname, _MISS)   # len(), isinstance(x), ...
    elif isinstance(func, ast.Attribute):
        base, attrs = _unwind_chain(func)
        if base is not None:
            obj, _ = _walk_chain(ctx, scope, base, attrs)
            fname = attrs[-1][0]
    if obj is _MISS or not callable(obj):
        return None
    spec = _object_spec(obj, fname)
    return _match_spec(fname, spec, call) if spec is not None else None


# ── import suggestions - a SEPARATE channel from errors ─────────────────────
#
# These are what the editor's quick-fix reads ({line: [statements]},
# built by collect_import_suggestions below), never encoded into error
# messages: an error describes what's wrong ("expected NAME", "name 'json' is
# not defined") whereas suggestions propose a fix, and the two travel side by
# side through the chain payload (`lint` vs `imports`).

# How many candidate statements a symbol gets (the editor shows them in a
# dropdown; past a handful they're meaningless, their choice).
_MAX_IMPORT_CANDIDATES = 4

_import_suggestion_cache = {}   # name -> [import statements] (possibly empty)


def _suggest_import(name):
    """The import statements that would bind `name`, best-ranked first — []
    when nothing importable answers to it (then it's just a typo/unassigned
    variable).

    Three probes, cheapest first, results cached per name:
      * `name` is a top-level module already loaded in this process
      * other LIVE modules bind `name` — to a module (`np` → numpy ⇒
        `import numpy as np`) or to an object its defining module really
        exports (`Path` ⇒ `from pathlib import Path`); candidates rank by how
        many live modules vote for them
      * `name` is an importable-but-not-yet-loaded module (find_spec — path
        search only, nothing executes)
    """
    if name in _import_suggestion_cache:
        return _import_suggestion_cache[name]
    stmts = []
    if name in sys.modules and "." not in name:
        stmts.append(f"import {name}")
    votes = {}
    for mod in list(sys.modules.values()):
        try:
            obj = vars(mod).get(name, _MISS)
        except TypeError:
            continue
        if obj is _MISS:
            continue
        if isinstance(obj, types.ModuleType):
            top = obj.__name__
            if top.endswith("." + name) and (stmts
                                             or top == f"{getattr(mod, '__name__', '')}.{name}"):
                # A nested `*.json`-style module, either the binder's own
                # submodule attribute (datasets.utils.json - not an alias
                # vote), or trumped by the exact top-level module when one
                # exists (`import json` beats any wrapper of it).
                continue
            cand = (f"import {top}" if top == name
                    else f"import {top} as {name}")
        else:
            owner = getattr(obj, "__module__", None)
            owner_mod = sys.modules.get(owner) if owner else None
            if (owner_mod is None
                    or getattr(owner_mod, name, _MISS) is not obj):
                continue            # not really importable as `name` from there
            cand = f"from {owner} import {name}"
        votes[cand] = votes.get(cand, 0) + 1
    for cand, _n in sorted(votes.items(), key=lambda kv: -kv[1]):
        if cand not in stmts:
            stmts.append(cand)
    if not stmts:
        import importlib.util
        try:
            if "." not in name and importlib.util.find_spec(name) is not None:
                stmts.append(f"import {name}")
        except Exception:
            pass
    stmts = stmts[:_MAX_IMPORT_CANDIDATES]
    _import_suggestion_cache[name] = stmts
    return stmts


_project_import_suggestion_cache = {}


def invalidate_import_bindings(path=None):
    """An explicit import edit affects uses in every block, not just its own line."""
    keys = {None, str(path) if path else None}
    if path is not None:
        from pathlib import Path
        keys.add(str(Path(path).resolve()))
    for key in keys:
        _file_binds_cache.pop(key, None)
        _inc_scan_state.pop(key, None)
        for span in (False, True):
            _inc_lint_state.pop((key, span), None)


def _suggest_import_for_path(name, path, cached_only=False):
    if path is None:
        return _import_suggestion_cache.get(name) if cached_only else _suggest_import(name)
    key = (str(path), name)
    if cached_only:
        return _project_import_suggestion_cache.get(key)
    from meltygui.extensions import get
    provider = get('source_imports')
    statements = provider(name, path) if provider else _suggest_import(name)
    _project_import_suggestion_cache[key] = statements[:_MAX_IMPORT_CANDIDATES]
    return _project_import_suggestion_cache[key]


_project_importables_cache = None   # (src_module_count, rows, stmts)


def project_importables():
    """(rows, stmts) for the completion popup's import shortcuts: `rows` is a
    sorted [(name, "auto_import")] list of the main package's top-level classes
    and modules, `stmts` maps each name to the import statement that binds it
    (`DrawState` → `from src.…draw_state import DrawState`, `draw_state` →
    `from src.…core_model import draw_state`). Only the canonical `src.`
    module identities are scanned (the bare `lsd.` aliases of the same files
    would spell imports the project doesn't use). Classes win a name collision
    with a module. Cached; rebuilt when the number of loaded src. modules
    changes (imports only ever add modules mid-session)."""
    global _project_importables_cache
    from meltygui.code.address import is_editable_source
    src_mods = {n: m for n, m in list(sys.modules.items())
                if m is not None and getattr(m, "__file__", None)
                and is_editable_source(m.__file__)}
    stamp = len(src_mods)
    if (_project_importables_cache is not None
            and _project_importables_cache[0] == stamp):
        return _project_importables_cache[1], _project_importables_cache[2]
    stmts = {}
    for mod_name in sorted(src_mods):
        mod = src_mods[mod_name]
        try:
            ns = vars(mod)
        except TypeError:
            continue
        for attr, obj in list(ns.items()):
            if (not attr.startswith("_") and isinstance(obj, type)
                    and getattr(obj, "__module__", None) == mod_name):
                stmts.setdefault(attr, f"from {mod_name} import {attr}")
    for mod_name in sorted(src_mods):
        parent, _, base = mod_name.rpartition(".")
        if parent and not base.startswith("_"):
            stmts.setdefault(base, f"from {parent} import {base}")
    rows = [(n, "auto_import") for n in sorted(stmts)]
    _project_importables_cache = (stamp, rows, stmts)
    return rows, stmts


_file_binds_cache = {}   # str(path) -> (mtime_ns, pending_gen, binds, mono_ts)

# Freshness floor for the binds cache: within this window a cached answer is
# served even when (mtime, pending_gen) moved on. pending_gen bumps on EVERY
# queued keystroke save (and redundantly during chain_out echo bursts), so
# keying on it alone would re-run the whole-file ast parse near-continuously
# while typing. Import-block changes are rare and human-paced - a second of
# staleness is invisible, the saved parses are not.
_FILE_BINDS_MIN_INTERVAL_S = 1.0


@lag_traced("module-binds parse", 30)
def _module_text_binds(path):
    """Module-scope names the file's CURRENT text binds — pending-save
    inclusive, so an import removed (or added) in an unsaved edit changes the
    answer immediately. This, not the live namespace, is the truth for "does
    the module still import X": the live module keeps a binding forever once
    an import RAN, so lint suppression keyed on it could never re-flag a
    removed import.

    None when the text is unreadable/unparseable or holds a star import —
    callers fall back to the live namespace (err on silence, exactly the old
    behavior). Cached on (mtime_ns, pending_gen) per CLAUDE.md's no-content-
    hash rule, with a time floor (_FILE_BINDS_MIN_INTERVAL_S) so gen churn
    can't re-parse the file continuously; runs on the lint's worker."""
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    now = time.monotonic()

    def _fresh(hit):
        return hit is not None and (
            (hit[0] == st.st_mtime_ns and hit[1] == gen)
            or now - hit[3] < _FILE_BINDS_MIN_INTERVAL_S)

    def _reusable(hit, new_text):
        # Diff-based shortcut (an incremental import-scan fix): a stale-by-gen
        # hit whose cached TEXT differs from the new pending text only
        # inside def/class bodies can't change its MODULE-scope binds -
        # skip the O(file) ast parse (~60ms on a 340KB file, observed on the
        # render thread) and keep the set. Text rides in the cache entry
        # (index 4; older 4-tuples predate this and never reuse).
        return (hit is not None and len(hit) > 4 and hit[2] is not None
                and hit[4] is not None and new_text is not None
                and _binds_unchanged(hit[4], new_text))

    gen = 0
    text = None
    key = str(path)
    try:
        from meltygui.editor.pending_save import PendingSave
        from pathlib import Path as _P
        rp = _P(path).resolve()
        key = str(rp)
        gen = PendingSave.pending_gen_for(rp)
        hit = _file_binds_cache.get(key)
        if _fresh(hit):
            return hit[2]
        text = PendingSave.current_file_text(rp)
        if _reusable(hit, text):
            _file_binds_cache[key] = (st.st_mtime_ns, gen, hit[2], now, text)
            return hit[2]
    except Exception:
        hit = _file_binds_cache.get(key)
        if _fresh(hit):
            return hit[2]
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = None
        if _reusable(hit, text):
            _file_binds_cache[key] = (st.st_mtime_ns, gen, hit[2], now, text)
            return hit[2]
    binds = None
    if text is not None:
        try:
            file_col = _Collector()
            file_col.run(_parse_for_analysis(text))
            binds = set(file_col.module.binds)
            # Star imports don't make the answer unknowable: resolve each
            # source to its LIVE module's export list (__all__, else
            # public names). Only an unloaded/relative star source degrades
            # to None (fall back to live-ns suppression).
            for sm in file_col.star_modules:
                mod = sys.modules.get(sm) if sm else None
                if mod is None:
                    binds = None
                    break
                names = getattr(mod, "__all__", None)
                if names is None:
                    names = [n for n in vars(mod) if not n.startswith("_")]
                binds.update(names)
            if binds is not None:
                binds = frozenset(binds)
        except (SyntaxError, ValueError, RecursionError, TypeError):
            binds = None
    _file_binds_cache[key] = (st.st_mtime_ns, gen, binds, now, text)
    return binds


def _binds_unchanged(old_text, new_text):
    """True when the old→new edit provably can't change MODULE-scope binds:
    every changed line (both sides of the diff) is blank/comment or indented,
    contains no import/global statement, and the enclosing column-0 block is
    a def/class/decorator — whose interior binds function or class scope, not
    module scope. An edit inside a module-level `if:`/`try:` block (indented
    yet module-scope) fails the header check and re-parses. Conservative by
    construction: any doubt → False → the full parse runs."""
    if old_text is new_text or old_text == new_text:
        return True
    a = old_text.split("\n")
    b = new_text.split("\n")
    na, nb = len(a), len(b)
    pre = 0
    m = min(na, nb)
    while pre < m and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < (na - pre) and suf < (nb - pre) and a[na - 1 - suf] == b[nb - 1 - suf]:
        suf += 1
    lo, hi = pre, nb - suf
    for ln in b[lo:hi] + a[lo:na - suf]:
        s = ln.lstrip()
        if not s or s.startswith("#"):
            continue
        if ln[0] not in " \t":
            return False                    # a top-level line changed
        if s.startswith(("global ", "import ", "from ")):
            return False
    start = min(lo, nb - 1)
    while start > 0 and (not b[start] or b[start][0] in " \t"):
        start -= 1
    head = b[start].lstrip() if 0 <= start < nb else ""
    return head.startswith(("def ", "async def ", "class ", "@"))


# ── pending-truth signature source ───────────────────────────────────────────
#
# The live object's inspect.signature reflects the last COMPILE, and disk only
# updates at shutdown (PendingSave defers all writes) - so between an edit and
# its recompile both lie about a function's parameters. The file's CURRENT
# text (disk + every queued unsaved edit, via PendingSave.current_file_text)
# is truth reliable, exactly the way Ctrl+B's find usages and the jedi passes
# already read it. _signature_table parses that text for call specs; callers
# prefer it over live introspection whenever the defining file has pending
# edits.

_file_sig_cache = {}   # str(realpath) -> (mtime_ns, pending_gen, table, mono_ts)

# Sentinel spec: the name IS bound at module scope but its signature is
# unknowable (decorated def, rebound name, inherited __init__) - suppresses
# both the check and any live-object fallback (err on silence).
_SIG_UNKNOWN = object()


def _class_call_spec(node, cls_scope):
    """The spec calling class `node` checks against — mirrors the buffer-static
    rule in _check_call_static: a plain __init__ decides; a bare class with no
    bases/keywords and no __init__/__new__ takes no args; anything else is
    unknowable."""
    init = cls_scope.defs.get("__init__")
    if init is not None and init[1] == "plain":
        spec = _spec_from_arguments(init[0].args, skip_first=1)
        return spec if spec is not None else _SIG_UNKNOWN
    if (init is None and not node.bases and not node.keywords
            and "__init__" not in cls_scope.binds
            and "__new__" not in cls_scope.binds):
        return _Spec()              # no-arg constructor
    return _SIG_UNKNOWN


def _wrapper_spec_for(decorator_list):
    """The calling-convention spec for a render-wrapper-decorated def, or None
    (unknowable). Applies when every decorator is a known project convention
    and at least one is a wrapper (@render_func): the live callable is
    core_render's `wrapper(input_value=None, **kwargs)`, so the ONLY checkable
    claims are the positional shape (at most one) and an input_value
    positional/keyword collision — required params may be filled by modes,
    decorator defaults, comment args or annotation maps, and wrapper-level
    kwargs (mode, name, ...) are always legal."""
    names = [_decorator_name(d) for d in decorator_list]
    known = _TRANSPARENT_DECORATORS | _WRAPPER_DECORATORS
    if (not names or not all(n in known for n in names)
            or not any(n in _WRAPPER_DECORATORS for n in names)):
        return None
    spec = _Spec()
    spec.named = ["input_value"]
    spec.has_var_kw = True
    return spec


def _module_scope_defs(tree):
    """{name: FunctionDef | None} for every function def the module's own
    scope executes — descending module-level if/try/with/for blocks but never
    def/class bodies (mirrors _module_scope_imports). A name defined twice
    maps to None (which def wins is unknowable)."""
    defs = {}

    def walk(stmts):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs[st.name] = None if st.name in defs else st
                continue
            if isinstance(st, ast.ClassDef):
                continue
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(st, field, None)
                if sub:
                    walk(sub)
            for h in getattr(st, "handlers", None) or ():
                walk(h.body)

    walk(tree.body)
    return defs


def _module_scope_imports(tree):
    """(from_imports, module_aliases) the module's own scope executes —
    descending module-level if/try/with/for blocks but never def/class bodies
    (those bind other scopes). from_imports is {alias: (module, name)} for
    absolute `from m import x`; module_aliases is {alias: module_name} for
    `import m` / `import m.n as p` (dotted-call bases: `imgui.dummy(...)`).
    Relative imports are skipped (not hop-resolvable by name)."""
    imports = {}
    modules = {}

    def walk(stmts):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
                continue
            if isinstance(st, ast.ImportFrom):
                if st.module and not st.level:
                    for al in st.names:
                        if al.name != "*":
                            imports[al.asname or al.name] = (st.module, al.name)
                continue
            if isinstance(st, ast.Import):
                for al in st.names:
                    if al.asname:
                        modules[al.asname] = al.name
                    else:
                        # `import a.b` binds `a`; the chain walk steps to `b`.
                        top = al.name.partition(".")[0]
                        modules[top] = top
                continue
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(st, field, None)
                if sub:
                    walk(sub)
            for h in getattr(st, "handlers", None) or ():
                walk(h.body)

    walk(tree.body)
    return imports, modules


@lag_traced("signature-table parse", 30)
def _signature_table(path):
    """Call specs the file's CURRENT text (pending-save inclusive) defines at
    module scope, or None (unreadable/unparseable — callers err on silence).

    {"specs": {name: _Spec | _SIG_UNKNOWN} for module-level defs/classes,
     "imports": {alias: (module, name)} for its `from m import x` bindings}

    Trust rules match the buffer-static pass: decorated defs, rebound names
    and non-trivial classes map to _SIG_UNKNOWN (bound, but never checked).
    Cached on (mtime_ns, pending_gen) with the same freshness floor as
    _file_binds_cache — an O(file) parse, throttled, worker-side only."""
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    now = time.monotonic()

    def _fresh(hit):
        return hit is not None and (
            (hit[0] == st.st_mtime_ns and hit[1] == gen)
            or now - hit[3] < _FILE_BINDS_MIN_INTERVAL_S)

    gen = 0
    text = None
    key = str(path)
    try:
        from meltygui.editor.pending_save import PendingSave
        from pathlib import Path as _P
        rp = _P(path).resolve()
        key = str(rp)
        gen = PendingSave.pending_gen_for(rp)
        hit = _file_sig_cache.get(key)
        if _fresh(hit):
            return hit[2]
        text = PendingSave.current_file_text(rp)
    except Exception:
        hit = _file_sig_cache.get(key)
        if _fresh(hit):
            return hit[2]
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = None
    table = None
    if text is not None:
        try:
            tree = _parse_for_analysis(text)
            col = _Collector()
            col.run(tree)
            specs = {}
            # Walked directly (not col.module.defs - the collector drops every
            # decorated def) so @command/@render_func defs get their convention
            # specs; rebound names still fall to unknowable via `ambiguous`.
            for name, node in _module_scope_defs(tree).items():
                if node is None or name in col.module.ambiguous:
                    specs[name] = _SIG_UNKNOWN
                    continue
                flavor = _decorator_flavor(node.decorator_list)
                spec = (_spec_from_arguments(node.args)
                        if flavor in ("plain", "static")
                        else _wrapper_spec_for(node.decorator_list))
                specs[name] = spec if spec is not None else _SIG_UNKNOWN
            for name, (node, cls_scope) in col.module.classes.items():
                specs[name] = _class_call_spec(node, cls_scope)
            imports, modules = _module_scope_imports(tree)
            for d in (imports, modules):
                for alias in list(d):
                    # A name the module also defs/rebinds isn't the import target.
                    if alias in specs or alias in col.module.ambiguous:
                        del d[alias]
            table = {"specs": specs, "imports": imports, "modules": modules}
        except (SyntaxError, ValueError, RecursionError, TypeError):
            table = None
    if table is None and hit is not None and hit[2] is not None:
        # Known-good: the pending text is unparseable exactly while the cursor is
        # MID-KEYSTROKE in some span of this file - a None here would blank
        # every signature marker built from the previous good parse (the
        # flash-then-vanish bug) so the stale table serves until a clean
        # parse replaces it. Signatures rarely change in the broken window.
        table = hit[2]
    _file_sig_cache[key] = (st.st_mtime_ns, gen, table, now)
    return table


def _pending_spec_for(obj):
    """(handled, spec) — the spec for `obj` from its defining file's PENDING
    text. handled=True means the pending source answered authoritatively
    (spec may be None = deliberately unknowable → silence); handled=False
    means no pending edits / not locatable → use live introspection."""
    if isinstance(obj, type):
        mod = sys.modules.get(getattr(obj, "__module__", None) or "")
        file = getattr(mod, "__file__", None)
        qual = getattr(obj, "__qualname__", None)
    else:
        fn = obj
        for _ in range(8):          # unwrap decorators to the def's real code
            inner = getattr(fn, "__wrapped__", None)
            if inner is None:
                break
            fn = inner
        code = getattr(fn, "__code__", None)
        if code is None:
            return False, None
        file = code.co_filename
        qual = getattr(fn, "__qualname__", None)
    if (not file or not qual or "." in qual):
        return False, None          # only module-level names are in the table
    try:
        from meltygui.editor.pending_save import PendingSave
        from pathlib import Path as _P
        rp = _P(file).resolve()
        if PendingSave.pending_gen_for(rp) <= 0:
            return False, None      # no unsaved edits - live/disk agree
    except Exception:
        return False, None
    table = _signature_table(str(rp))
    if table is None:
        return False, None
    spec = table["specs"].get(qual, _MISS)
    if spec is _MISS:
        return False, None          # def moved/renamed - fall back to live
    return True, (None if spec is _SIG_UNKNOWN else spec)


def _object_spec(obj, fname):
    """The spec a call to `obj` checks against: the pending text of its
    defining file when that file has unsaved edits (the live signature goes
    stale between an edit and its recompile), else live introspection."""
    try:
        handled, spec = _pending_spec_for(obj)
    except Exception:
        handled, spec = False, None
    if handled:
        return spec
    return _live_spec(obj, fname)


def _span_spec_for_live(obj, fname):
    """Spec for a LIVE object reached from span mode, or None. Stricter than
    the whole-file live pass: only real function objects are trusted.
    Classes lie (ast.Constant's signature claims required params its
    constructor doesn't enforce) and callable INSTANCES lie (PyOpenGL's
    glDrawBuffers wrapper hides its signature and its doc line parses into
    the wrong arity) — both produce false alarms, so they stay silent."""
    if isinstance(obj, type) or not isinstance(
            obj, (types.FunctionType, types.BuiltinFunctionType)):
        return None
    return _object_spec(obj, fname)

def _check_dotted_call_span(call, func, scope, path):
    """Span-mode signature check for a dotted call (`imgui.dummy(...)`,
    `mod.helper(...)`): the base name resolves through the module file's
    import bindings (its own text, so a base the buffer shadows never
    matches), then the chain walks LIVE modules only — the same modules-only
    rule as _walk_chain, minus the missing-attr report (out of scope for the
    span pass). Anything unresolvable → silence."""
    base, attrs = _unwind_chain(func)
    if base is None or _resolves(scope, base.id):
        return None
    table = _signature_table(path) if path else None
    if table is None:
        return None
    obj = _MISS
    modname = table["modules"].get(base.id)
    if modname is not None:
        mod = sys.modules.get(modname)
        obj = mod if mod is not None else _MISS
    else:
        imp = table["imports"].get(base.id)
        if imp is not None:
            mod = sys.modules.get(imp[0])
            try:
                obj = vars(mod).get(imp[1], _MISS) if mod is not None else _MISS
            except TypeError:
                obj = _MISS
    if obj is _MISS:
        return None
    for attr, _node in attrs:
        if not isinstance(obj, types.ModuleType):
            return None             # never walk through functions/classes
        obj = inspect.getattr_static(obj, attr, _MISS)
        if obj is _MISS:
            return None
    if not callable(obj):
        return None
    fname = attrs[-1][0]
    spec = _span_spec_for_live(obj, fname)
    return _match_spec(fname, spec, call) if spec is not None else None


def _check_call_span(call, scope, path, file_binds):
    """Signature check for SPAN buffers (only_missing_imports mode), where the
    live-ctx pass can't run (the buffer binds none of its module's names). A
    bare-name call the buffer itself doesn't bind resolves through the
    enclosing module's CURRENT text (_signature_table — pending-save
    inclusive, so a signature edited in another view flags wrong call sites
    before any recompile): a module-level def/class directly, a
    `from m import name` via the live module (upgraded to m's pending text
    when m's file has unsaved edits), and builtins only when the module's
    text provably doesn't shadow the name. Anything else → silence."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return _check_dotted_call_span(call, func, scope, path)
    if not isinstance(func, ast.Name):
        return None
    name = func.id
    if _resolves(scope, name):
        return None                 # the buffer's own binding, static pass owns it
    table = _signature_table(path) if path else None
    if table is None:
        return None
    spec = table["specs"].get(name, _MISS)
    if spec is _SIG_UNKNOWN:
        return None
    if spec is not _MISS:
        return _match_spec(name, spec, call)
    imp = table["imports"].get(name)
    if imp is not None:
        mod = sys.modules.get(imp[0])

        try:
            obj = vars(mod).get(imp[1], _MISS) if mod is not None else _MISS
        except TypeError:
            obj = _MISS
        if obj is _MISS or not callable(obj):
            return None
        spec = _span_spec_for_live(obj, name)
        return _match_spec(name, spec, call) if spec is not None else None
    if file_binds is not None and name not in file_binds:
        obj = getattr(builtins, name, _MISS)
        if obj is not _MISS and callable(obj):
            spec = _live_spec(obj, name)
            return _match_spec(name, spec, call) if spec is not None else None
    return None


def _buffer_bound_names(text):
    """Every name the buffer text plausibly BINDS — local vars, params,
    def/class names, loop/with targets, import statements — in a handful of
    regex passes (findall, C-speed), never per-name. Approximate on purpose,
    erring toward "bound" (a bound name is merely never suggested — silence
    beats noise). Used where the buffer's parse can't be trusted (mid-edit
    syntax errors)."""
    import re
    bound = set()
    bound.update(_analysis_matches(r"(?m)^\s*(?:def|class)\s+(\w+)", text))
    bound.update(_analysis_matches(r"(?m)^\s*(\w+)\s*(?:=[^=]|,|\)|=$)", text))
    bound.update(_analysis_matches(r"\b(?:as|for)\s+(\w+)", text))
    # Tuple targets: every name between `for` and `in` is a binding
    # (`for chev, step, vid in ...` - the pass above only got `chev`), and
    # likewise every name on the left of an unpack assignment (`a, b = ...`).
    # `(` is excluded from the assignment class so a call's kwargs
    # (`foo(bar, baz=1)`) are never read as targets.
    for targets in _analysis_matches(r"\bfor\s+([\w\s,()\[\]*]+?)\s+in\b", text):
        bound.update(re.findall(r"\w+", targets))
    for targets in _analysis_matches(r"(?m)^\s*([\w\s,\[\]*]+?)\s*=[^=]", text):
        bound.update(re.findall(r"\w+", targets))
    for params in _analysis_matches(r"(?m)^\s*(?:def\s+\w+|lambda)\s*\(([^)]*)", text):
        bound.update(re.findall(r"\w+", params))
    # Import bindings at ANY indent - a function-local `from m import name`
    # covers `name` for the whole buffer's scope, so it must never be
    # re-suggested. The parenthesized form is captured ACROSS lines ([^)]
    # matches newlines), so EVERY name of a multi-line
    # `from m import (a,\n b, c)` binds, not just the first per line.
    for names in _analysis_matches(
            r"(?m)^\s*from\s+[.\w]+\s+import\s+(\([^)]*\)?|[^#\n]*)", text):
        names = names.strip("()")
        for part in names.split(","):
            toks = part.split()
            if toks and toks[0] != "*":
                bound.add(toks[0])
    for names in _analysis_matches(r"(?m)^\s*import\s+([^#\n]+)", text):
        for part in names.split(","):
            toks = part.split()
            if toks:
                bound.add(toks[0].split(".")[0])
    return bound


def _tokenize_lenient(slice_text):
    """[(type, string, rel_line)] NAME/OP tokens for a slice. Whole-slice
    tokenize first; where it BREAKS (a mid-edit dedent mismatch — e.g. an
    appended line shallower than the line above — or an unterminated string)
    the remaining lines are tokenized INDIVIDUALLY, stripped so indentation
    can't fault. The scan's filters only use prev/next context within a
    line, so per-line context is enough; without the salvage every line
    after the break silently vanished from the scan."""
    import io
    import tokenize as _tokenize
    out = []
    broke = False
    try:
        for index, tok in enumerate(_tokenize.generate_tokens(io.StringIO(slice_text).readline)):
            if index % 128 == 0:
                _analysis_checkpoint()
            if tok.type in (_tokenize.NAME, _tokenize.OP):
                out.append((tok.type, tok.string, tok.start[0]))
    except (_tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        broke = True
    lines = slice_text.split("\n")
    # Salvage ONLY small slices (the incremental region case, where the break
    # is the edit). A whole-buffer scan that breaks mid-file must NOT be
    # salvaged: per-line mode has no string context, so thousands of
    # docstring lines below the break would tokenize as code and flood the
    # UI with prose "suggestions". Losing the below-break findings for
    # one mid-edit scan is the old, silent behavior - the incremental state
    # keeps the previous findings for those lines anyway.
    if broke and len(lines) <= 200:
        resume = max((ln for _t, _s, ln in out), default=0) + 1
        for idx in range(resume - 1, len(lines)):
            stripped = lines[idx].strip()
            if not stripped:
                continue
            try:
                for tok in _tokenize.generate_tokens(
                        io.StringIO(stripped + "\n").readline):
                    if tok.type in (_tokenize.NAME, _tokenize.OP):
                        out.append((tok.type, tok.string, idx + 1))
            except (_tokenize.TokenError, IndentationError, SyntaxError,
                    ValueError):
                continue
    return out


def _scan_slice(slice_text, line_offset, bound, path, cached_only=False):
    """{absolute 1-based line: [import stmts]} for one text slice — the
    tokenize-based candidate pass shared by the full and incremental scans.
    Base identifiers only (not attributes after a dot, not assignment
    targets, not keywords / import / decorator lines); a name survives when
    neither the module's current text (_module_text_binds) nor `bound` (the
    buffer's own bindings) accounts for it AND an import statement would
    bind it (_suggest_import, cached per name)."""
    import keyword
    import tokenize as _tokenize
    toks = _tokenize_lenient(slice_text)
    if not toks:
        return {}
    file_binds = None
    file_binds_ready = False
    lines = slice_text.split("\n")
    verdict = {}                    # name -> [stmts] or None (checked once)
    out = {}
    for i, (kind, s, rel_line) in enumerate(toks):
        if kind != _tokenize.NAME or keyword.iskeyword(s):
            continue
        stripped = (lines[rel_line - 1].strip()
                    if 1 <= rel_line <= len(lines) else "")
        if stripped.startswith(("import ", "from ")):
            continue                # import block line
        prev = toks[i - 1][1] if i > 0 else None
        nxt = toks[i + 1][1] if i + 1 < len(toks) else None
        # Attribute / binding position. Decorator lines scan like any other
        # code - the name after `@` AND its arguments are real usages an
        # import could fix (the old line-level `@` skip hid both).
        if prev in (".", "def", "class", "as", "import", "from"):
            continue
        if nxt == "=":              # plain assignment target (== is one token)
            continue
        if s not in verdict:
            stmts = None
            if s not in _BUILTIN_NAMES and s not in bound:
                if not file_binds_ready:
                    # Lazy: most slices resolve every name via builtins/bound
                    # and never need the (cached/throttled) file parse.
                    if cached_only:
                        # The render-thread incremental path must not parse the
                        # module or discover imports. A miss belongs to relint.
                        hit = _file_binds_cache.get(str(path)) if path else None
                        if path and hit is None:
                            return None
                        file_binds = hit[2] if hit else None
                    else:
                        file_binds = _module_text_binds(path) if path else None
                    file_binds_ready = True
                if not (file_binds is not None and s in file_binds):
                    try:
                        stmts = _suggest_import_for_path(s, path, cached_only=cached_only)
                        if cached_only and stmts is None:
                            return None
                        stmts = stmts or None
                    except Exception:
                        stmts = None
            verdict[s] = stmts
        stmts = verdict[s]
        if stmts:
            row = out.setdefault(line_offset + rel_line, [])
            for st in stmts:
                if st not in row:
                    row.append(st)
    return out


# Incremental scan state, one entry per lint_path: the last scan text, its
# result, and the buffer's bound-name set. Two buffers sharing a path (two
# span editors of one file) are back to full rescans - never wrong.
_inc_scan_state = {}

# An edit region larger than this re-runs the full scan instead (the diff
# bookkeeping stops being cheaper than one pass).
_INC_MAX_REGION_CHARS = 4096


def has_scan_state(path):
    """True when a background pass already warmed `path`'s incremental scan
    state — the editor's per-keystroke fast path (text_editor.py) probes this
    before calling collect_import_suggestions on the RENDER thread: a warm
    incremental step is O(changed region) (~0.4ms), but a path's FIRST scan is
    O(buffer tokenize + module-binds parse) and belongs on a worker."""
    return path is not None and str(path) in _inc_scan_state


@lag_traced("import scan", 30)
def collect_import_suggestions(text, path=None, full=False,
                               incremental_only=False):
    """{1-based line: [import statements]} for every symbol the buffer USES
    but nothing binds — the editor's Alt+Enter quick-fix data, a SEPARATE
    channel from the error lint. Tokenize-based, so it works mid-edit (a
    dangling `json.` breaks the parse, not the tokenizer).

    INCREMENTAL per keystroke: the previous text/result are kept per path,
    the edit is located by common prefix/suffix (C-speed string ops), only
    the changed LINES are re-tokenized, and every unchanged line's findings
    are shifted, not recomputed — so a keystroke costs O(changed region),
    never O(buffer). `full=True` (the relint path — the file's import block
    may have changed) and structural cases (first scan, big paste, edits
    inside triple-quoted strings, tokenizer trouble) run the whole pass.

    `incremental_only=True` (the editor's render-thread fast path on large
    buffers) returns None instead of running that O(buffer) full pass — the
    caller falls back to the debounced background channel, whose next run
    re-warms the state here."""
    key = str(path) if path else None
    st = _inc_scan_state.get(key) if key else None
    if not full and st is not None:
        old = st["text"]
        if old is text or old == text:
            return st["result"]
        inc = _incremental_scan(st, old, text, path, cached_only=incremental_only)
        if inc is not None:
            if key:
                _inc_scan_state[key] = inc
            return inc["result"]
    if incremental_only:
        return None
    bound = _buffer_bound_names(text)
    result = _scan_slice(text, 0, bound, path)
    if key:
        _inc_scan_state[key] = {"text": text, "result": result, "bound": bound}
    return result


def _incremental_scan(st, old, text, path, cached_only=False):
    """The O(changed region) path: new state dict, or None → run a full scan.

    The changed region is the line span between the common prefix and common
    suffix. Findings on lines before it are kept as-is, lines after it shift
    by the line-count delta, and the region itself is re-tokenized in
    isolation. The bound-name set only GROWS here (bindings added in the
    region); a binding DELETED elsewhere keeps its name suppressed until the
    next full scan — the relint kick that follows every queued save runs one
    within ~a second, so the miss is transient. An edit inside a triple-
    quoted string would tokenize prose as code, so an odd quote count before
    the region skips its rescan (pure line-shift instead)."""
    # Common prefix/suffix by CHUNKED slice compares (C-speed memcmp) - a
    # per-char Python loop here costs ~25ms on a 300k buffer, which is
    # the exact per-keystroke stall this incremental path aims to kill.
    max_p = min(len(old), len(text))
    p = 0
    for step in (1 << 16, 1 << 12, 1 << 8, 1 << 4, 1):
        while p + step <= max_p and old[p:p + step] == text[p:p + step]:
            p += step
    max_s = max_p - p
    s = 0
    for step in (1 << 16, 1 << 12, 1 << 8, 1 << 4, 1):
        while (s + step <= max_s
               and old[len(old) - s - step:len(old) - s]
               == text[len(text) - s - step:len(text) - s]):
            s += step
    if len(text) - s - p > _INC_MAX_REGION_CHARS:
        return None                 # big paste/rewrite - full scan is cheaper
    pre_lines = text.count("\n", 0, p)
    old_total = old.count("\n") + 1
    new_total = text.count("\n") + 1
    suf_lines = text.count("\n", len(text) - s) if s else 0
    delta = new_total - old_total
    result = {}
    for ln, stmts in st["result"].items():
        if ln <= pre_lines:
            result[ln] = stmts
        elif ln > old_total - suf_lines:
            result[ln + delta] = stmts
    # Region slice, rounded to whole lines.
    start_idx = text.rfind("\n", 0, p) + 1
    end_idx = len(text) - s
    nl = text.find("\n", end_idx)
    slice_end = len(text) if nl == -1 else nl
    slice_text = text[start_idx:slice_end]
    bound = st["bound"]
    in_string = (text.count('"""', 0, start_idx)
                 + text.count("'''", 0, start_idx)) % 2 == 1
    if slice_text and not in_string:
        region_bound = _buffer_bound_names(slice_text)
        if region_bound - bound:
            bound = bound | region_bound
        findings = _scan_slice(slice_text, pre_lines, bound, path, cached_only=cached_only)
        if findings is None:
            return None
        for ln, stmts in findings.items():
            result[ln] = stmts
    return {"text": text, "result": result, "bound": bound}


# ── entry point ──────────────────────────────────────────────────────────────

# Incremental lint state, one entry per (path, mode): the last linted text and
# its findings. Two buffers sharing a path (a file class + a mid-file view)
# degrade to full re-lints on each swap - never wrong, just slower.
_inc_lint_state = {}

# An edited block larger than this skips its region re-lint (findings inside it
# go stale until the next full pass) - the point of the incremental path is
# bounding worker GIL-hold, so a monster block must not sneak an O(n)-
# scale pass back in.
_INC_LINT_REGION_CHARS = 64 * 1024


def _changed_block_bounds(a, b):
    """The edited region of line-lists `a` → `b`, expanded to enclosing
    top-level block(s) (nearest column-0 lines — the same expansion the
    editor's region compile uses). Returns (start, end_new, end_old, delta),
    all 0-based with exclusive ends, or None when the texts are line-identical."""
    na, nb = len(a), len(b)
    pre = 0
    m = min(na, nb)
    while pre < m and a[pre] == b[pre]:
        pre += 1
    if pre == na and pre == nb:
        return None
    suf = 0
    while suf < (na - pre) and suf < (nb - pre) and a[na - 1 - suf] == b[nb - 1 - suf]:
        suf += 1
    lo, hi = pre, nb - suf
    start = min(lo, nb - 1)
    while start > 0 and (not b[start] or b[start][0] in " \t"):
        start -= 1
    end = hi
    while end < nb and (not b[end] or b[end][0] in " \t"):
        end += 1
    return start, end, end + (na - nb), nb - na


@lag_traced("incremental lint", 30)
def check_source_incremental(text, path=None, only_missing_imports=False):
    """check_source, O(edited block) per call: diff against the last linted
    text, keep findings outside the edited top-level block (shifted by the
    line delta), and re-lint only the block itself.

    Region lint is sound here for the same reason SPAN lint is: check_source
    with `path` resolves names through the live module namespace and the
    pending-file binds (_module_text_binds), so a lone block from mid-file
    sees its module's imports and sibling definitions instead of flagging
    them. A block that doesn't parse (mid-edit) reports [] for the region —
    errs silent, the next clean edit re-lints it.

    Trade-offs vs the full pass: a binding added/removed OUTSIDE the edited
    block doesn't re-verify findings elsewhere, and an over-sized block
    (>_INC_LINT_REGION_CHARS) keeps its stale findings. The first call per
    (path, mode) pays one full pass to seed the state."""
    key = (str(path) if path else None, bool(only_missing_imports))
    st = _inc_lint_state.get(key)
    if st is None:
        findings = check_source(text, path=path,
                                only_missing_imports=only_missing_imports)
        # The buffer-wide bound-name set suppresses region findings about
        # names DEFINED IN OTHER BLOCKS of this buffer: a lone block can't
        # see them itself, and the live-module fallback only covers files
        # actually loaded in this process. _buffer_bound_names deliberately
        # over-approximates (a wrongly-suppressed finding beats a false
        # alarm). Grow it, using the import scanner's bound set.
        _inc_lint_state[key] = {"text": text, "findings": findings,
                                "binds": _buffer_bound_names(text)}
        return findings
    old = st["text"]
    if old is text or old == text:
        return st["findings"]
    bounds = _changed_block_bounds(old.split("\n"), text.split("\n"))
    if bounds is None:
        st["text"] = text
        return st["findings"]
    start, end, end_old, delta = bounds
    # Bindings have file-wide effects. Recheck all uses when a declaration is
    # added/removed instead of retaining a grow-only set of old imports.
    previous_region = '\n'.join(old.split('\n')[start:end_old])
    current_region = '\n'.join(text.split('\n')[start:end])
    if _buffer_bound_names(previous_region) != _buffer_bound_names(current_region):
        _file_binds_cache.pop(str(path), None)
        findings = check_source(text, path=path, only_missing_imports=only_missing_imports)
        _inc_lint_state[key] = {'text': text, 'findings': findings,
                                'binds': _buffer_bound_names(text)}
        return findings
    # Old-text region rows are start+1 .. end_old (1 based): findings above
    # keep their line, findings below shift by the edit line delta, findings
    # inside are re-derived from the fresh region lint.
    kept = [(ln, msg) if ln <= start else (ln + delta, msg)
            for ln, msg in st["findings"]
            if ln <= start or ln > end_old]
    region = "\n".join(text.split("\n")[start:end])
    if len(region) <= _INC_LINT_REGION_CHARS:
        binds = st.get("binds")
        if binds is None:                     # state predates the binds field
            binds = st["binds"] = _buffer_bound_names(old)
        binds |= _buffer_bound_names(region)
        # `from x import *` binds names _buffer_bound_names can't see (it
        # skips `*` on purpose) - a region using a star-imported name
        # (TrainingStatus in lsd_train.py) would report "not defined" even
        # though the full-buffer pass stays silent (star_import kills its
        # name pass). _module_text_binds expands star sources through their
        # LIVE module's exports; None (unreadable / unresolvable) degrades
        # to the plain binds check.
        try:
            _mod_binds = _module_text_binds(path) if path else None
        except Exception:
            _mod_binds = None
        try:
            for ln, msg in check_source(region, path=path,
                                        only_missing_imports=only_missing_imports):
                # Name-shape findings about a name some OTHER block in this
                # buffer binds are cross-block artifacts - drop them. Other
                # finding shapes (signature/attr) pass through untouched.
                if msg.startswith("name '"):
                    _nm = msg[6:msg.find("'", 6)]
                    if _nm in binds or (_mod_binds is not None
                                        and _nm in _mod_binds):
                        continue
                kept.append((ln + start, msg))
        except Exception:
            pass
        kept.sort(key=lambda f: f[0])
    _inc_lint_state[key] = {"text": text, "findings": kept,
                            "binds": st.get("binds")}
    return kept


@lag_traced("check_source (lint)", 50)
def check_source(text, path=None, max_reports=40, only_missing_imports=False):
    """[(line, message)] for problems that would survive compile() but blow up
    at run time. Empty list when clean — or when the buffer isn't checkable
    (syntax error here means the parse/compile pass already reported it).

    only_missing_imports=True is the SPAN-buffer mode (a function/class source
    edited on its own): the buffer legitimately uses names its module's import
    block binds, so a generic undefined-name report would flag every one. With
    `path` = the enclosing module's file, the live-module namespace suppresses
    everything the module actually binds; what's left is only reported when an
    import statement would fix it (the missing-import classification below) —
    a bare typo stays silent. Call-signature checks DO run in span mode
    (Toggles.TextEditor.lint_span_calls), resolved through the module file's
    pending text (_check_call_span) rather than the live ctx; the attr pass
    stays off (its module-walk needs import-bound names the span never
    sees)."""
    try:
        tree = _parse_for_analysis(text)
    except IndentationError:
        # A method/nested span arrives at its class-body indent - dedent and
        # retry (line numbers survive; textwrap.dedent strips only the common
        # prefix). string_to_cst_module does its own dedent, but this is the
        # lint's mirror of the same normalization.
        import textwrap
        try:
            tree = _parse_for_analysis(textwrap.dedent(text))
        except (SyntaxError, ValueError):
            return []
    except (SyntaxError, ValueError):
        return []
    col = _Collector()
    try:
        col.run(tree)
    except RecursionError:
        return []

    live_mod = _module_for(path)
    ctx = _LiveCtx(live_mod, col.declared_attrs)
    live_names = ctx.ns.keys() if ctx.ns is not None else ()

    reports = []
    seen = set()

    def report(line, msg):
        if (line, msg) not in seen:
            seen.add((line, msg))
            reports.append((line, msg))

    if path:
        from meltygui.extensions import call
        for line, message in call('source_diagnostics', text, path) or ():
            report(line, message)

    # Span mode checks "does the module bind this" against the file's
    # CURRENT text (pending/inclusive), not its live namespace: a module keeps
    # a live binding forever once an import happens, so live-ns suppression would
    # never re-flag an import the user removed. None (unreadable / mid-edit /
    # star import) falls back to the live namespace as usual.
    file_binds = (_module_text_binds(path)
                  if only_missing_imports and path else None)

    if not col.star_import:
        for scope in col.scopes:
            _analysis_checkpoint()
            for index, (name, lineno) in enumerate(scope.loads):
                if index % 128 == 0:
                    _analysis_checkpoint()
                if name in _BUILTIN_NAMES:
                    continue
                if _resolves(scope, name):
                    continue
                if file_binds is not None and name in file_binds:
                    continue        # the module's current text binds it
                if file_binds is None and only_missing_imports and name in live_names:
                    continue        # no readable file text - old suppression
                try:
                    fixable = bool(_suggest_import_for_path(name, path))
                except Exception:
                    fixable = False  # classification must never break the lint
                # A fixable name reports even when the live namespace still
                # carries it (a removed import, or an exec-injected binding
                # the SOURCE never declares): the file wouldn't run from
                # scratch. The fix itself rides the SEPARATE suggestions
                # channel (collect_import_suggestions), not the message.
                if not fixable and (name in live_names or only_missing_imports):
                    continue        # injected-at-runtime / a module global we
                                    # can't see - unfixable, stay silent
                report(lineno, f"name '{name}' is not defined")

    if only_missing_imports:
        # Span buffers get the call-signature pass too, resolved through the
        # module file's PENDING text instead of the live namespace (which needs
        # import-bound names the span never sees) - see _check_call_span.
        try:
            from meltygui.toggles import Toggles
            _span_calls = Toggles.TextEditor.lint_span_calls
        except Exception:
            _span_calls = True
        if _span_calls and not col.star_import:
            for call, scope, guarded in col.calls:
                if guarded:
                    continue
                msg = _check_call_static(call, scope)
                if msg is None:
                    try:
                        msg = _check_call_span(call, scope, path, file_binds)
                    except Exception:
                        msg = None  # resolution must never break the lint
                if msg is not None:
                    report(call.lineno, msg)
        reports.sort()
        return reports[:max_reports]

    # Same toggle as the span pass - the pending-table fallback below is the
    # same feature surfaced in whole-file/region mode.
    try:
        from meltygui.toggles import Toggles
        _table_calls = Toggles.TextEditor.lint_span_calls
    except Exception:
        _table_calls = True
    for index, (call, scope, guarded) in enumerate(col.calls):
        if index % 128 == 0:
            _analysis_checkpoint()
        msg = _check_call_static(call, scope)
        if msg is None and not guarded:
            try:
                msg = _check_call_live(ctx, call, scope)
            except Exception:
                msg = None          # live introspection should never break the lint
        if (msg is None and not guarded and _table_calls
                and path and not col.star_import):
            # Pending-table fallback: the INCREMENTAL whole-file path lints
            # one edited top-level block in isolation, where a sibling def
            # (flat_button) is neither in the buffer's scopes nor live-
            # resolvable; the module file's pending text alone knows it.
            # file_binds=None: full mode's live pass already covered builtins.
            try:
                msg = _check_call_span(call, scope, path, None)
            except Exception:
                msg = None
        if msg is not None:
            report(call.lineno, msg)

    for base, attrs, scope, guarded in col.attr_chains:
        if guarded:
            continue
        try:
            _, rep = _walk_chain(ctx, scope, base, attrs)
        except Exception:
            rep = None
        if rep is not None:
            report(*rep)

    reports.sort()
    return reports[:max_reports]
