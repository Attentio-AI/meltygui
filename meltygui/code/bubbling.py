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

from src.lsd.gl_gui.melty import Melty


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
class _BubblingDictMixin:
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


class _BubblingListMixin:
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
