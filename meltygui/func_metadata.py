"""Runtime metadata about view-function scopes, captured from live evaluation.

Keyed by the function object itself, ``FuncsMetadata.metadata[func]`` maps each
in-scope name (the function's parameters / eval locals) to a :class:`VarMeta`
carrying the observed runtime type and member names. The studio's eval REPL holds
the real call-time locals, so it records EXACT types here -- closing the gap left
by un-annotated signatures (a ``draw_state`` param with no hint is still known to
be a ``DrawState``). The eval tab's autocomplete reads this back for scope-aware,
type-accurate suggestions; the code editors can tap the same cache later for
better hints without re-deriving types statically.

The cache key is the *wrapper* function (``draw_state._view_func``) so callers can
look it up straight off a draw_state. Globals are resolved against the unwrapped
body's ``__globals__`` (the wrapper lives in core_render, with the wrong globals),
so :func:`scope_names` unwraps before reading them.
"""

import builtins as _builtins
import inspect
import keyword as _keyword

# Numbers carry only object/number dunders that nobody worth completing, and a
# full dir() per kwarg adds up, so we skip the member snapshot for them.
_PRIMITIVE_NO_MEMBERS = (int, float, bool, complex)


def _safe_dir(obj):
    """``dir(obj)`` as a list, swallowing anything that misbehaves."""
    try:
        return list(dir(obj))
    except Exception:
        return []


def _safe_members(value):
    """dir() snapshot of a live value as a tuple (empty for bare numbers/None)."""
    if value is None or isinstance(value, _PRIMITIVE_NO_MEMBERS):
        return ()
    return tuple(_safe_dir(value))


def _type_name(t):
    """Short display name for a type (``DrawState``), or '' when unknown."""
    if t is None:
        return ""
    return getattr(t, "__name__", None) or str(t)


class VarMeta:
    """What runtime observation told us about one in-scope name.

    ``type`` is the observed class (exact, even when the signature is unhinted).
    ``members`` is dir() of the observed VALUE at capture time -- so it includes
    instance attributes set in ``__init__``, which ``dir(type)`` alone would miss.
    Both are cheap, GC-safe snapshots (a class ref + a tuple of strings); the live
    object itself is never retained."""

    __slots__ = ("type", "members")

    def __init__(self, type=None, members=()):
        self.type = type
        self.members = members

    def __repr__(self):
        return f"VarMeta(type={_type_name(self.type)!r}, {len(self.members)} members)"


class FuncsMetadata:
    """Process-wide cache of observed scope metadata, keyed by function object."""

    metadata = {}  # func -> {name: VarMeta}

    @classmethod
    def record(cls, func, scope):
        """Merge observed runtime types/members for ``func`` from a ``{name:
        value}`` scope. Later observations refine earlier ones (the eval tab's
        approximate pre-capture is overwritten by run_scoped_eval's exact ns).
        Individual names are skipped silently when introspection raises."""
        if func is None or not scope:
            return None
        slot = cls.metadata.get(func)
        if slot is None:
            slot = cls.metadata[func] = {}
        for name, value in scope.items():
            if not isinstance(name, str):
                continue
            try:
                slot[name] = VarMeta(type=type(value), members=_safe_members(value))
            except Exception:
                continue
        return slot

    @classmethod
    def get(cls, func):
        """``{name: VarMeta}`` observed for ``func``, or ``{}`` if never recorded."""
        return cls.metadata.get(func, {})

    @classmethod
    def clear(cls, func=None):
        """Drop one function's metadata, or the whole cache when ``func`` is None."""
        if func is None:
            cls.metadata.clear()
        else:
            cls.metadata.pop(func, None)


# ── Completion providers (read side) ─────────────────────────────────────────
# Both return ordered ``[(name, kind)]`` lists, best-first. ``kind`` is the dim
# tag the editor shows on the right of each row. The eval tab's payoff is that a
# scope var's tag is its OBSERVED TYPE NAME -- so the popup reads
# ``draw_state  DrawState`` even though the signature had no annotation.

_MISSING = object()


def _unwrap(func):
    try:
        return inspect.unwrap(func)
    except Exception:
        return func


def _lookup_live(func, name):
    """The live object a free ``name`` resolves to in ``func``'s scope -- a module
    global or a builtin -- or ``_MISSING``. Lets ``imgui.``/``len.`` complete
    against the real object (exact, chainable) without holding anything: the
    module/builtin already lives forever."""
    g = getattr(_unwrap(func), "__globals__", None) or {}
    if name in g:
        return g[name]
    if hasattr(_builtins, name):
        return getattr(_builtins, name)
    return _MISSING


def _live_member_kind(obj, name):
    try:
        return "method" if callable(getattr(obj, name)) else "attr"
    except Exception:
        return "attr"


def _type_member_kind(t, name):
    """method/attr tag for ``name`` on class ``t`` without an instance."""
    try:
        attr = inspect.getattr_static(t, name)
    except Exception:
        try:
            attr = getattr(t, name, None)
        except Exception:
            attr = None
    if attr is None:
        return "attr"
    if isinstance(attr, (staticmethod, classmethod)):
        return "method"
    if inspect.isfunction(attr) or inspect.ismethod(attr) or inspect.isbuiltin(attr):
        return "method"
    return "attr"


def _attr_type(t, name):
    """Best-effort TYPE of attribute ``name`` on class ``t`` (annotation, else the
    class-level value's type). ``None`` when undeterminable -- the chain stops."""
    try:
        import typing
        hints = typing.get_type_hints(t)
        h = hints.get(name)
        if isinstance(h, type):
            return h
    except Exception:
        pass
    try:
        attr = inspect.getattr_static(t, name)
    except Exception:
        attr = getattr(t, name, None)
    if isinstance(attr, type):
        return attr
    if attr is not None and not callable(attr):
        return type(attr)
    return None


def member_completions(func, receiver):
    """``[(name, kind)]`` for ``receiver.``<caret>. The receiver resolves either
    to a LIVE object (module global / builtin -> walked with getattr, exact and
    chainable) or to a recorded scope var (-> its captured member snapshot for the
    first hop, a static type-chain walk for deeper hops). ``[]`` when unresolved
    (the popup simply stays closed)."""
    if not receiver:
        return []
    segs = receiver.split(".")
    head, rest = segs[0], segs[1:]

    live = _lookup_live(func, head)
    if live is not _MISSING:
        obj = live
        for seg in rest:
            try:
                obj = getattr(obj, seg)
            except Exception:
                return []
        return [(n, _live_member_kind(obj, n)) for n in _safe_dir(obj)]

    vm = FuncsMetadata.get(func).get(head)
    if vm is None:
        return []
    if not rest:
        names = vm.members or _safe_dir(vm.type)
        return [(n, _type_member_kind(vm.type, n)) for n in names]
    cur = vm.type
    for seg in rest:
        cur = _attr_type(cur, seg)
        if cur is None:
            return []
    return [(n, _type_member_kind(cur, n)) for n in _safe_dir(cur)]


def scope_names(func):
    """``[(name, kind)]`` for bare-identifier completion in ``func``'s scope: the
    recorded scope vars first (kind = observed type name), then the function's
    module globals (kind ``global``), then builtins and keywords. De-duplicated,
    first occurrence wins -- so a scope var shadows a same-named global."""
    out, seen = [], set()
    for name, vm in FuncsMetadata.get(func).items():
        if name not in seen:
            seen.add(name)
            out.append((name, _type_name(vm.type) or "local"))
    g = getattr(_unwrap(func), "__globals__", None) or {}
    for name in g:
        if isinstance(name, str) and name not in seen and not name.startswith("__"):
            seen.add(name)
            out.append((name, "global"))
    for name in dir(_builtins):
        if not name.startswith("_") and name not in seen:
            seen.add(name)
            out.append((name, "builtin"))
    for kw in _keyword.kwlist:
        if kw not in seen:
            seen.add(kw)
            out.append((kw, "kw"))
    return out


def _receiver_before(text, anchor):
    """The dotted receiver expression ending at the '.' just left of ``anchor``
    (the index where the half-typed member begins). ``'draw_state.foo.|'`` ->
    ``'draw_state.foo'``; ``''`` when no identifier precedes the dot."""
    dot = anchor - 1  # the '.' that triggered member access
    i = dot - 1
    while i >= 0 and (text[i].isalnum() or text[i] in "_."):
        i -= 1
    return text[i + 1:dot]


_eval_sources = {}


def eval_completion_source(func):
    """The callable draw_text's autocomplete invokes for the eval box. Memoized
    per ``func`` so the SAME object is handed in every frame (a fresh closure each
    frame would look like a changed kwarg and defeat the box's render cache). It
    reads the cache live, so it always reflects the latest recorded scope.

    Contract (matches draw_text): ``(text, anchor, prefix, dot_trigger) ->
    [(name, kind)]``. The prefix filtering is draw_text's job, not ours."""
    src = _eval_sources.get(func)
    if src is None:
        def src(text, anchor, prefix, dot_trigger, _f=func):
            if dot_trigger:
                return member_completions(_f, _receiver_before(text, anchor))
            return scope_names(_f)
        _eval_sources[func] = src
    return src
