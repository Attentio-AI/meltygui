"""
Bubbling proxies — make a nested mutation notify a root object.

A RenderHost only intercepts writes to its OWN keys. A mutation to a nested
container — `host['cfg']['rank'] = 16`, or editing a `GeneralParse` you grabbed a
reference to — runs through THAT child's methods, which the host never sees. So a
deep edit silently fails to mark the host dirty, and its file-IO / state handling
doesn't react.

`install_bubbling(value, root)` walks a value tree and upgrades every mutable
container so a mutation at ANY depth calls `root._mark_changed()`. With it, the
host reflects (and an editor like code_file_io saves) deep edits with no manual
signal — the stateful handling becomes invisible.

Two type-preserving upgrade paths:

  • A dict / list SUBCLASS that carries a __dict__ (GeneralParse, Conditional, Loop,
    CallParse, …) is upgraded IN PLACE via `__class__` reassignment to a generated
    `(mixin, base)` subclass. Same object, same identity, same attributes, and
    `isinstance(x, GeneralParse)` still holds — so a reference grabbed BEFORE
    adoption bubbles too, and the converters / renderers (all isinstance/MRO based)
    keep working.

  • A PLAIN dict / list (no __dict__, so `__class__` can't be reassigned) is replaced
    by a constructed bubbling copy, written back into its parent.

The framework resolves `@defaults` kwargs by EXACT type (not MRO — see
core_render.py's `default_kwargs_by_type[type(input_value)]`), so a generated
subclass would otherwise lose its base's tint / included / disable_scroll defaults.
Each generated subclass therefore mirrors the base's per-class registry entries.

Cycle- and idempotency-safe: re-installing an already-bubbling tree just re-points
the root and recurses into any fresh subtrees.
"""

from collections import deque

from meltygui.runtime import Melty


# ── Lazy deep attribute traversal ──────────────────────────────────────────────
#
# A converted value tree is full of redundant wrapper rungs:
#
#     render_func_dict["value"]["draw_collection"]["decorators"]["render_func"]
#
# Only "decorators" and "render_func" carry meaning - "value" and "draw_collection"
# are bookkeeping the converter had to spell out and guard (`if "value" in d: ...`).
# The `.deep` accessor lets you name ONLY the rungs you care about and finds the rest:
#
#     func_dict.deep.decorators.render_func()   # == the line above, without guards
#
# It is reached via an EXPLICIT `.deep` property - NOT a blanket __getattr__ on
# the container. A blanket __getattr__ would answer EVERY missing-attribute probe the
# framework makes (`hasattr(d, 'children')`, `getattr(d, 'x', default)`, copy/pickle
# dunders) with a stand-in object instead of the AttributeError those call sites
# expect - so MISSING/proxy values leak everywhere. `.deep` is opt-in: normal
# attribute access on the container is still completely unaffected.
#
# `.deep.a.b` accumulates the path lazily; calling it (`()`) resolves the WHOLE path
# at once with backtracking, so a name that matches a dead-end branch (one lacking the
# next rung) doesn't terminate the lookup - it tries the next candidate. Resolution
# returns the real value (ready to hand to draw_collection), or MISSING.


class _Missing:
    """The result of a deep lookup that found nothing.

    A null object so a resolved chain is easy to test: it's falsy, iterates empty,
    has len 0, and any further attr/index access returns itself. Underscore names
    still raise AttributeError so it can't masquerade as having dunder/protocol
    methods (copy/pickle/etc.)."""

    __slots__ = ()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self

    def __getitem__(self, key):
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __contains__(self, item):
        return False

    def __repr__(self):
        return "<missing>"


MISSING = _Missing()


def _iter_matches(node, name):
    """Yield every value stored under key `name` at or below `node`, breadth-first
    (shallowest first), descending through nested dicts/lists. Internal `__…` keys
    are neither matched nor descended."""
    queue = deque([node])
    seen = set()
    while queue:
        cur = queue.popleft()
        cid = id(cur)
        if cid in seen:
            continue
        seen.add(cid)
        if isinstance(cur, dict):
            if name in cur and not _is_internal_key(name):
                yield dict.__getitem__(cur, name)
            for k, v in cur.items():
                if not _is_internal_key(k) and isinstance(v, (dict, list)):
                    queue.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    queue.append(v)


def _resolve_chain(node, names):
    """Resolve `names` from `node` with backtracking. For each rung, try every place
    the name matches (shallowest first); recurse for the rest, and only accept a
    candidate whose subtree can satisfy the REMAINING rungs. So a repeated key on a
    branch that lacks the next rung is skipped rather than dead-ending the lookup.
    Returns the matched value, or MISSING when no full path exists."""
    if not names:
        return node
    first, rest = names[0], names[1:]
    for cand in _iter_matches(node, first):
        result = _resolve_chain(cand, rest)
        if result is not MISSING:
            return result
    return MISSING


def deep_get(container, *names):
    """Resolve a path of `names` through `container`, skipping redundant wrapper rungs
    and backtracking past dead-end branches (see `_resolve_chain`). The function form
    of the `.deep` accessor: `deep_get(host, "decorators", "render_func")`."""
    return _resolve_chain(container, names)


def _resolve_all(node, names, out, seen):
    """Collect EVERY value reachable by `names` from `node` (all branches, not just the
    first backtracked hit), deduped by identity. Empty `names` → `node` itself."""
    if not names:
        if id(node) not in seen:
            seen.add(id(node))
            out.append(node)
        return
    first, rest = names[0], names[1:]
    for cand in _iter_matches(node, first):
        _resolve_all(cand, rest, out, seen)


def _iter_leaves(node, prefix, seen):
    """Yield `(path_tuple, value)` for every LEAF (non-dict/list) at or below `node`,
    descending dicts/lists. Internal `__…` keys are skipped. Cycle-safe."""
    nid = id(node)
    if nid in seen:
        return
    if isinstance(node, dict):
        seen.add(nid)
        for k, v in node.items():
            if _is_internal_key(k):
                continue
            if isinstance(v, (dict, list)):
                yield from _iter_leaves(v, prefix + (k,), seen)
            else:
                yield prefix + (k,), v
    elif isinstance(node, list):
        seen.add(nid)
        for i, v in enumerate(node):
            if isinstance(v, (dict, list)):
                yield from _iter_leaves(v, prefix + (i,), seen)
            else:
                yield prefix + (i,), v
    else:
        yield prefix, node


def deep_all(container, *names):
    """Every value matching `names` across ALL branches (the multi-hit form of
    `deep_get`). With no names → every leaf value in the whole tree. Function form of
    `host.deep.all()` / `host.deep.<names>.all()`."""
    if not names:
        return [v for _, v in _iter_leaves(container, (), set())]
    out = []
    _resolve_all(container, names, out, set())
    return out


class _DeepPath:
    """Lazy, backtracking path builder returned by `.deep`. Each `.name` / `[name]`
    appends a rung WITHOUT resolving; calling it (`path()`) resolves the whole chain
    at once against the root, so backtracking can see the full path. Underscore names
    raise AttributeError so it stays invisible to attribute/copy/pickle probes."""

    __slots__ = ("_root", "_names")

    def __init__(self, root, names=()):
        object.__setattr__(self, "_root", root)
        object.__setattr__(self, "_names", names)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _DeepPath(self._root, self._names + (name,))

    def __getitem__(self, key):
        return _DeepPath(self._root, self._names + (key,))

    def __call__(self):
        return _resolve_chain(self._root, self._names)

    def all(self):
        """Every value matching the accumulated path, across ALL branches — the
        multi-hit counterpart to `()` (which returns just the first). With NO path
        (`host.deep.all()`) → every leaf value in the whole tree: "I don't know the
        path, give me everything". `host.deep.params.all()` → every `params` anywhere."""
        return deep_all(self._root, *self._names)

    def unwrap(self):
        """The structure-preserving "everything": descend past redundant single-entry
        wrapper rungs (`value`, the fn-name level, …) and return the first MEANINGFUL
        container — the dict/list that actually holds the content — with its KEYS
        intact, ready for draw_collection. Use this (not `.all()`) to render a whole
        subtree when you don't know the path: `.all()` flattens to a list of leaf
        values (so a labelled `tint=(...)` becomes a positional list entry); `.unwrap()`
        keeps it a dict. Stops at the first branch (>1 child) or leaf-bearing level."""
        node = self.__call__()
        seen = set()
        while isinstance(node, dict) and id(node) not in seen:
            seen.add(id(node))
            keys = [k for k in node.keys() if not _is_internal_key(k)]
            if len(keys) != 1:
                break                                  # branch or empty → this is the content
            child = dict.__getitem__(node, keys[0])
            if not isinstance(child, (dict, list)):
                break                                  # single child is a leaf → keep the dict
            node = child
        return node

    def items(self):
        """Every leaf as `(path_tuple, value)` so you can see WHERE each came from.
        Spans all branches the path resolves to (the whole tree for an empty path)."""
        roots = self.all() if self._names else [self._root]
        out = []
        for node in roots:
            if node is MISSING:
                continue
            out.extend(_iter_leaves(node, (), set()))
        return out

    def __bool__(self):
        # `if host.a.b:` resolves and tests the result - no explicit call needed. Also
        # the safety net for a bad probe that leaks here: an unresolved/empty chain
        # reads as falsy, so framework code treats a leaked proxy like None.
        result = self.__call__()
        return result is not MISSING and bool(result)

    def __iter__(self):
        result = self.__call__()
        return iter(result) if result is not MISSING else iter(())

    def __len__(self):
        result = self.__call__()
        try:
            return len(result) if result is not MISSING else 0
        except TypeError:
            return 0

    def __contains__(self, item):
        result = self.__call__()
        try:
            return result is not MISSING and item in result
        except TypeError:
            return False

    def __repr__(self):
        return f"<deep {'.'.join(map(str, self._names))} -> {self.__call__()!r}>"


class _DeepAttrMixin:
    """Gives a dict/list container a `.deep` entry point for safe path traversal.

    `.deep` is a real attribute (a property), so it never interferes with normal
    attribute lookup, `getattr(x, name, default)`, `hasattr`, or copy/pickle — those
    all behave exactly as before. Only `host.deep.<names>()` runs the deep search."""

    @property
    def deep(self):
        return _DeepPath(self)


# base type -> generated bubbling subclass (one per concrete type, cached)
_BUBBLING_DICT_CLASSES = {}
_BUBBLING_LIST_CLASSES = {}

# Melty registries looked up by EXACT class (no MRO lookup), so a bubbling subclass
# must inherit the base's entries or it loses the base's @defaults / annotations.
_CLASS_KEYED_REGISTRIES = (
    "default_kwargs_by_type",
    "default_kwargs_by_attrib_type",
    "default_funcs_by_name_type",
)


def _mirror_registries(base, bubbling):
    """Point a generated bubbling subclass at the SAME per-class registry entries as
    its base, so exact-type lookups (@defaults kwargs, per-attr overrides) still
    resolve. Share the references — base entries are import-time and don't change."""
    for reg_name in _CLASS_KEYED_REGISTRIES:
        reg = getattr(Melty, reg_name, None)
        if reg is not None and base in reg:        # `in` on a registry: no autocreate
            reg[bubbling] = reg[base]


def base_of_bubbling(t):
    """A generated bubbling subclass (`Bubbling_<Base>`, made by `type(...)` at runtime)
    has NO source of its own, so `inspect.getfile`/`getsourcelines` on it raises "could
    not find class definition". It's an implementation detail that should behave like its
    base everywhere — the registry mirroring already does this for `@defaults`; this does
    it for SOURCE resolution. Return the real base (the non-mixin entry of `__bases__`,
    since the subclass is `(mixin, base)`); pass non-bubbling types through unchanged."""
    if isinstance(t, type) and issubclass(t, (_BubblingDictMixin, _BubblingListMixin)):
        for b in t.__bases__:
            if not (isinstance(b, type) and issubclass(b, (_BubblingDictMixin, _BubblingListMixin))):
                return b
    return t


def _notify(node):
    root = getattr(node, "_bubble_root", None)
    if root is not None:
        root._mark_changed()


# Immutable SCALAR leaf types. imgui widgets hand back a FRESH object each frame for
# these (a slider returns a new int/float; ints outside CPython's -5..256 cache aren't
# interned), so an identity (`is`) check sees a spurious "change" and re-fires notify -
# which, through the proxy round-trip, becomes a Tree↔String save feedback loop. An `==`
# on a scalar leaf is O(1) and BOUNDED (its own size, the rendering cost paid), so
# it's safe here - unlike a deep `==` over a whole container/GeneralParse tree, which is
# what doesn't scale. Containers fall through to identity (they're edited in place).
_SCALAR_TYPES = frozenset({int, float, complex, bool, str, bytes, type(None)})


def _unchanged_leaf(cur, value):
    """True if writing `value` over `cur` is a no-op: the SAME object, or an equal
    immutable scalar (a reconstructed-but-equal leaf). Containers → identity only."""
    return cur is value or (type(value) in _SCALAR_TYPES and cur == value)


def _is_internal_key(key):
    """`__…` keys are framework bookkeeping, NOT user data — the editor already
    excludes them (`excluded=["__cst__"]`, draw skips `__`-prefixed). Writing them
    must NOT mark the host dirty: dict_to_cst_module's `leave_ClassDef` caches the
    rebuilt node as `edit_dict["__cst__"] = node` while SERIALIZING the tree, and if
    that re-dirties the proxy, every save triggers a serialize that triggers another
    save — an infinite loop. So bubbling sets these raw and never notifies."""
    return isinstance(key, str) and key.startswith("__")


# ── Mixins (front of the MRO) - bubble every mutation, keep new children bubbling ──
class _BubblingDictMixin(_DeepAttrMixin):
    _bubble_root = None

    def __setitem__(self, key, value):
        if _is_internal_key(key):
            super().__setitem__(key, value)      # bookkeeping write, no bubble
            return
        # Re-assigning an unchanged child is not an edit (draw_collection writes the
        # rendered child back every frame) - store it, but don't notify / loop. Identity
        # for containers (edited in place → same object), O(1) `==` for scalar leaves
        # (imgui reconstructs them, so `is` would spuriously fire). See _unchanged_leaf.
        unchanged = key in self and _unchanged_leaf(dict.__getitem__(self, key), value)
        root = self._bubble_root
        if root is not None:
            value = install_bubbling(value, root)
        super().__setitem__(key, value)
        if not unchanged:
            _notify(self)

    def __delitem__(self, key):
        print("DEL", key)
        internal = _is_internal_key(key)
        super().__delitem__(key)
        if not internal:
            _notify(self)

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        root = self._bubble_root
        if root is not None:
            _reinstall_children(self, root)
        _notify(self)

    def setdefault(self, key, default=None):
        if key in self:
            return super().__getitem__(key)
        root = self._bubble_root
        if root is not None:
            default = install_bubbling(default, root)
        super().__setitem__(key, default)
        _notify(self)
        return default

    def pop(self, *args):
        had = (not args) or args[0] in self
        result = super().pop(*args)
        if had:
            _notify(self)
        return result

    def popitem(self):
        result = super().popitem()
        _notify(self)
        return result

    def clear(self):
        had = bool(self)
        super().clear()
        if had:
            _notify(self)


class _BubblingListMixin(_DeepAttrMixin):
    _bubble_root = None

    def __setitem__(self, idx, value):
        # Unchanged element is not an edit → store but don't notify. Identity for containers,
        # O(1) `==` for scalar leaves (imgui hands back a new int/float each frame, so a
        # `is` check spuriously fires → the list-element feedback loop). See _unchanged_leaf.
        unchanged = not isinstance(idx, slice) and _unchanged_leaf(list.__getitem__(self, idx), value)
        root = self._bubble_root
        if root is not None:
            value = ([install_bubbling(v, root) for v in value]
                     if isinstance(idx, slice) else install_bubbling(value, root))
        super().__setitem__(idx, value)
        if not unchanged:
            _notify(self)

    def __delitem__(self, idx):
        super().__delitem__(idx)
        _notify(self)

    def append(self, value):
        root = self._bubble_root
        if root is not None:
            value = install_bubbling(value, root)
        super().append(value)
        _notify(self)

    def extend(self, iterable):
        root = self._bubble_root
        if root is not None:
            iterable = [install_bubbling(v, root) for v in iterable]
        super().extend(iterable)
        _notify(self)

    def insert(self, idx, value):
        root = self._bubble_root
        if root is not None:
            value = install_bubbling(value, root)
        super().insert(idx, value)
        _notify(self)

    def pop(self, *args):
        result = super().pop(*args)
        _notify(self)
        return result

    def remove(self, value):
        super().remove(value)
        _notify(self)

    def clear(self):
        had = bool(self)
        super().clear()
        if had:
            _notify(self)

    def __iadd__(self, other):
        self.extend(other)
        return self


class _BubblingDict(_BubblingDictMixin, dict):
    """Bubbling replacement for a PLAIN dict (which can't be upgraded in place)."""


class _BubblingList(_BubblingListMixin, list):
    """Bubbling replacement for a PLAIN list."""


def _bubbling_dict_class_for(base):
    cls = _BUBBLING_DICT_CLASSES.get(base)
    if cls is None:
        cls = type(f"Bubbling_{base.__name__}", (_BubblingDictMixin, base), {})
        _BUBBLING_DICT_CLASSES[base] = cls
        _mirror_registries(base, cls)
    return cls


def _bubbling_list_class_for(base):
    cls = _BUBBLING_LIST_CLASSES.get(base)
    if cls is None:
        cls = type(f"Bubbling_{base.__name__}", (_BubblingListMixin, base), {})
        _BUBBLING_LIST_CLASSES[base] = cls
        _mirror_registries(base, cls)
    return cls


def _reinstall_children(node, root, _seen=None):
    """Upgrade every child of an already-bubbling container, writing replacements
    back through the BASE methods (raw dict/list ops) so we don't re-fire notify."""
    if _seen is None:
        _seen = set()
    if isinstance(node, dict):
        for k in list(dict.keys(node)):
            child = dict.__getitem__(node, k)
            new = install_bubbling(child, root, _seen)
            if new is not child:
                dict.__setitem__(node, k, new)
    elif isinstance(node, list):
        for i in range(len(node)):
            child = list.__getitem__(node, i)
            new = install_bubbling(child, root, _seen)
            if new is not child:
                list.__setitem__(node, i, new)


def install_bubbling(value, root, _seen=None):
    """Upgrade `value` and everything nested under it to bubble mutations to `root`.

    Returns the bubbling value: the SAME object when it can be upgraded in place (a
    __dict__-bearing dict/list subclass), or a constructed copy for a plain
    dict/list (whose parent then stores the replacement). Leaves immutable / opaque
    leaves (str, int, tuple, libcst nodes, …) untouched. Idempotent and cycle-safe."""
    if _seen is None:
        _seen = set()
    vid = id(value)

    # Already bubbling - just (re)point the root and recurse for fresh subtrees.
    if isinstance(value, (_BubblingDictMixin, _BubblingListMixin)):
        value._bubble_root = root
        if vid not in _seen:
            _seen.add(vid)
            _reinstall_children(value, root, _seen)
        return value

    if isinstance(value, dict):
        if vid in _seen:
            return value
        _seen.add(vid)
        if type(value) is dict:                    # plain dict: can't reclass, copy
            target = _BubblingDict()
            dict.update(target, value)
        else:                                       # subclass with __dict__: in-place
            try:
                value.__class__ = _bubbling_dict_class_for(type(value))
                target = value
            except TypeError:                       # exotic dict that refuses reclass
                target = _BubblingDict()
                dict.update(target, value)
        target._bubble_root = root
        _reinstall_children(target, root, _seen)
        return target

    if isinstance(value, list):
        if vid in _seen:
            return value
        _seen.add(vid)
        if type(value) is list:
            target = _BubblingList()
            list.extend(target, value)
        else:
            try:
                value.__class__ = _bubbling_list_class_for(type(value))
                target = value
            except TypeError:
                target = _BubblingList()
                list.extend(target, value)
        target._bubble_root = root
        _reinstall_children(target, root, _seen)
        return target

    return value
