"""Static "will it run" checks — undefined names + call-signature mismatches.

`check_source(text, path=None)` -> [(line, message)], 1-based lines aligned with
the buffer. Runs on chain_in's background thread AFTER libcst and compile()
both passed (see _run_chain_in), so the source is known syntactically valid and
this pass only adds the NameError / TypeError / AttributeError class of
mistakes those let through:

  * a Name load that no enclosing scope, builtin, or live-module global binds
    ("name 'myvarr' is not defined")
  * a call to a function/class DEFINED IN THIS BUFFER whose arguments can't
    bind (unknown kwarg, too many positionals, missing required args)
  * the same signature check against LIVE objects — a bare imported name
    (`request_render(1, 2, 3)`), a builtin (`isinstance(x)`), or a dotted
    module attribute (`imgui.dummy()`), resolved through the running process's
    module for `path`. C/Cython callables that hide their signature from
    inspect fall back to the embedsignature doc line ("dummy(width, height)").
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
import types

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


def _decorator_flavor(decorator_list):
    """'plain' / 'static' / 'class' when the signature is still trustworthy,
    None when a decorator could have changed it (anything but the two builtins)."""
    if not decorator_list:
        return "plain"
    if len(decorator_list) == 1 and isinstance(decorator_list[0], ast.Name):
        if decorator_list[0].id == "staticmethod":
            return "static"
        if decorator_list[0].id == "classmethod":
            return "class"
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


class _Collector:
    """One pass over the tree building the scope graph + the check worklists."""

    def __init__(self):
        self.module = _Scope("module", None)
        self.scopes = [self.module]
        self.calls = []             # (Call node, scope, type_guarded)
        self.attr_chains = []       # (base Name node, [(attr, node)...], scope, guarded)
        self.declared_attrs = set() # dotted paths the buffer itself makes visible:
                                    # `import a.b` / `from a.b import c` / `a.b = ...`
        self.star_import = False
        self._name_guard = 0        # >0 inside try guarded by except NameError
        self._type_guard = 0        # >0 inside try guarded by except TypeError/AttributeError

    def run(self, tree):
        self._body(tree.body, self.module)

    # ── plumbing ────────────────────────────────────────────────────────────
    def _new_scope(self, kind, parent):
        s = _Scope(kind, parent)
        self.scopes.append(s)
        return s

    def _body(self, stmts, scope):
        for stmt in stmts:
            self._visit(stmt, scope)

    def _visit(self, node, scope):
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
    return spec


def _spec_from_signature(sig):
    P = inspect.Parameter
    spec = _Spec()
    for p in sig.parameters.values():
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
    if not line.startswith(fname + "("):
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
        if more.startswith(fname + "("):
            return None
    # Take exactly the BALANCED paren group after the name - torch-style lines
    # carry a return annotation after it ("sort(input, ...) -> (Tensor,
    # LongTensor)") that must not leak into the spec.
    head = line[len(fname):]
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
    star_args = any(isinstance(x, ast.Starred) for x in call.args)
    kw_expand = any(k.arg is None for k in call.keywords)
    kw_names = [k.arg for k in call.keywords if k.arg is not None]
    npos = len(call.args)

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
        # docstring's first line instead.
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
    spec = _live_spec(obj, fname)
    return _match_spec(fname, spec, call) if spec is not None else None


# ── entry point ──────────────────────────────────────────────────────────────

def check_source(text, path=None, max_reports=40):
    """[(line, message)] for problems that would survive compile() but blow up
    at run time. Empty list when clean — or when the buffer isn't checkable
    (syntax error here means the parse/compile pass already reported it)."""
    try:
        tree = ast.parse(text)
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

    if not col.star_import:
        for scope in col.scopes:
            for name, lineno in scope.loads:
                if name in _BUILTIN_NAMES or name in live_names:
                    continue
                if _resolves(scope, name):
                    continue
                report(lineno, f"name '{name}' is not defined")

    for call, scope, guarded in col.calls:
        msg = _check_call_static(call, scope)
        if msg is None and not guarded:
            try:
                msg = _check_call_live(ctx, call, scope)
            except Exception:
                msg = None          # live introspection should never break the lint
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
