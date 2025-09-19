import dataclasses
import collections.abc
import libcst as cst

from src.lsd.gl_gui.model.core_model.new_core_model import exclude


# ---------- tiny converters (one place) ----------

def _lexeme(py):
    if isinstance(py, bool):  return "True" if py else "False"
    if py is None:            return "None"
    if isinstance(py, float): return repr(py)
    if isinstance(py, int):   return str(py)
    if isinstance(py, str):   return py
    return str(py)


def _autobox_expr(py, hint: cst.CSTNode | None):
    """Turn Python primitives into LibCST expression nodes, using 'hint' to keep kind stable."""
    if isinstance(py, cst.CSTNode): return py
    if isinstance(py, CSTProxy):    return py.node
    if isinstance(py, bool):        return cst.Name("True" if py else "False")
    if py is None:                  return cst.Name("None")
    if isinstance(py, int):         return cst.Integer(_lexeme(py))
    if isinstance(py, float):       return cst.Float(_lexeme(py))
    if isinstance(py, str):
        if isinstance(hint, cst.Name):         return cst.Name(py)
        if isinstance(hint, cst.SimpleString): return cst.SimpleString(repr(py))
        return cst.SimpleString(repr(py))
    return py  # last resort


def _to_libcst(value, orig_field):
    """Convert proxy/UI value back to what LibCST expects, using original field as the kind hint."""
    # Already CST?
    if isinstance(value, CSTProxy):    return value.node
    if isinstance(value, cst.CSTNode): return value

    # Sequence field (e.g., body, args, params)
    if isinstance(orig_field, collections.abc.Sequence) and not isinstance(orig_field, str):
        # Support dict-backed sequences: use insertion order of .values()
        elems = value.values() if isinstance(value, collections.abc.Mapping) else value

        # Use first element as a loose hint for element kind
        hint_elem = None
        for e in orig_field:
            if isinstance(e, cst.CSTNode):
                hint_elem = e
                break

        out = []
        for x in elems:
            if isinstance(x, CSTProxy):
                out.append(x.node)
            elif isinstance(x, cst.CSTNode):
                out.append(x)
            else:
                out.append(_autobox_expr(x, hint_elem))
        return out

    # Leaf string field (like Integer.value, Name.value, SimpleString.value)
    if isinstance(orig_field, str):
        return _lexeme(value)

    # Node-typed field (e.g., Assign.value, Arg.value, etc.)
    if isinstance(orig_field, cst.CSTNode):
        return _autobox_expr(value, orig_field)

    # Everything else (sentinels, enums, None, bool flags, etc.) - pass through
    return value


# ---------- dict proxy (ordered, auto-bubble dirty) ----------
class CSTDictProxy(dict):
    """
    Ordered, dict-like proxy for sequence fields. Keys are stable strings so you can
    track items across transforms without index churn. Values are wrapped, and any
    mutator bubbles dirty up to the parent CSTProxy.
    """

    def __init__(self, items_kv, parent: "CSTProxy", field_name: str):
        super().__init__()
        self._parent = parent
        self._field = field_name
        for k, v in items_kv:
            w = wrap(v)
            if isinstance(w, CSTProxy):
                w._set_parent(parent, field_name)
            super().__setitem__(k, w)

    # ---- utils ----
    def _touch(self):
        self._parent._mark_dirty_up()

    # ---- mutators ----
    def __setitem__(self, k, v):
        w = wrap(v)
        if isinstance(w, CSTProxy):
            w._set_parent(self._parent, self._field)
        super().__setitem__(k, w)
        self._touch()

    def __delitem__(self, k):
        super().__delitem__(k)
        self._touch()

    def clear(self):
        super().clear()
        self._touch()

    def pop(self, k, *default):
        val = super().pop(k, *default) if default else super().pop(k)
        self._touch()
        return val

    def popitem(self):
        kv = super().popitem()
        self._touch()
        return kv

    def setdefault(self, k, default=None):
        if k in self:
            return super().get(k)
        w = wrap(default)
        if isinstance(w, CSTProxy):
            w._set_parent(self._parent, self._field)
        super().__setitem__(k, w)
        self._touch()
        return w

    def update(self, other=None, /, **kw):
        if other is None:
            other = {}
        if hasattr(other, "items"):
            it = other.items()
        else:
            it = other  # iterable of (k, v)
        changed = False
        for k, v in it:
            w = wrap(v)
            if isinstance(w, CSTProxy):
                w._set_parent(self._parent, self._field)
            super().__setitem__(k, w)
            changed = True
        for k, v in kw.items():
            w = wrap(v)
            if isinstance(w, CSTProxy):
                w._set_parent(self._parent, self._field)
            super().__setitem__(k, w)
            changed = True
        if changed:
            self._touch()

    def __ior__(self, other):
        self.update(other)
        return self

    # Convenience, stable reordering APIs (optional but handy)
    def insert_before(self, key_to_insert, before_key):
        if key_to_insert not in self or before_key not in self:
            return
        val = super().pop(key_to_insert)
        new = {}
        for k, v in self.items():
            if k == before_key:
                new[key_to_insert] = val
            new[k] = v
        super().clear()
        super().update(new)
        self._touch()

    def insert_after(self, key_to_insert, after_key):
        if key_to_insert not in self or after_key not in self:
            return
        val = super().pop(key_to_insert)
        new = {}
        for k, v in self.items():
            new[k] = v
            if k == after_key:
                new[key_to_insert] = val
        super().clear()
        super().update(new)
        self._touch()


# ---------- object proxy (class spoofing, parent bubbling, lazy rebuild) ----------
@exclude(["star", "header", "footer", "comma", "default_newline", "default_indent", "encoding", "has_trailing_newline"])
class CSTProxy:
    __slots__ = ("_node", "_field_names", "_dirty", "_parent", "_parent_field", "__dict__")

    def __init__(self, node: cst.CSTNode):
        object.__setattr__(self, "_node", node)
        object.__setattr__(self, "_dirty", False)
        object.__setattr__(self, "_parent", None)  # parent proxy or None
        object.__setattr__(self, "_parent_field", None)  # field name in parent
        self._refresh_fields_from(node)

    # class spoofing so type-based routing works
    @property
    def __class__(self):
        return type(self._node)

    @property
    def node(self) -> cst.CSTNode:
        if self._dirty:
            self._rebuild()
        return object.__getattribute__(self, "_node")

    def flush(self):
        if self._dirty:
            self._rebuild()

    # --- parent plumbing & dirtiness bubbling ---
    def _set_parent(self, parent: "CSTProxy|None", field_name: str | None):
        object.__setattr__(self, "_parent", parent)
        object.__setattr__(self, "_parent_field", field_name)

    def _mark_dirty_up(self):
        object.__setattr__(self, "_dirty", True)
        p = object.__getattribute__(self, "_parent")
        if p is not None:
            p._mark_dirty_up()

    # --- field reflection & rebuild ---

    @staticmethod
    def _gen_key(i: int) -> str:
        # zero-padded index makes debugging/natural sort easy; keys are stable unless you rekey
        return f"{i:06d}"

    def _refresh_fields_from(self, node):
        if not dataclasses.is_dataclass(node):
            object.__setattr__(self, "_field_names", ())
            return
        fns = tuple(f.name for f in dataclasses.fields(node))
        exclude_names = {"keyword"}  # keep your original value
        object.__setattr__(self, "_field_names", fns)

        # keep non-CST attrs, refresh CST fields
        keep = {k: v for k, v in self.__dict__.items() if k not in fns and k not in exclude_names}
        self.__dict__.clear()
        self.__dict__.update(keep)

        for name in fns:
            val = getattr(node, name)

            # If this is a loosely-typed sequence (LibCST lists/tuples), present it as an ordered dict
            if isinstance(val, collections.abc.Sequence) and not isinstance(val, str):
                # If we already had a dict proxy for this field, try to preserve keys (track items by position)
                prev = keep.get(name)
                if isinstance(prev, CSTDictProxy):
                    # Map existing keys to new elements; if lengths differ, generate keys for extras
                    old_keys = list(prev.keys())
                    new_items = list(val)
                    kv = []
                    for i, elem in enumerate(new_items):
                        k = old_keys[i] if i < len(old_keys) else self._gen_key(i)
                        kv.append((k, elem))
                else:
                    kv = [(self._gen_key(i), elem) for i, elem in enumerate(val)]

                self.__dict__[name] = CSTDictProxy(kv, self, name)

            else:
                child = wrap(val)
                if isinstance(child, CSTProxy):
                    child._set_parent(self, name)
                self.__dict__[name] = child

    def _rebuild(self):
        node = object.__getattribute__(self, "_node")
        kwargs = {}
        for name in self._field_names:
            current = self.__dict__[name]
            original = getattr(node, name)
            kwargs[name] = _to_libcst(current, original)
        new_node = node.with_changes(**kwargs)
        object.__setattr__(self, "_node", new_node)
        object.__setattr__(self, "_dirty", False)
        self._refresh_fields_from(new_node)

    def __setattr__(self, name, value):
        if name in getattr(self, "_field_names", ()):
            w = wrap(value)
            if isinstance(w, CSTProxy):
                w._set_parent(self, name)
            self.__dict__[name] = w
            self._mark_dirty_up()
        else:
            self.__dict__[name] = value

    def __repr__(self):
        return f"<{type(self._node).__name__}Proxy>"


# ---------- wrapper functions ----------

def wrap(obj):
    if isinstance(obj, CSTProxy):  # don't rewrap
        return obj
    if isinstance(obj, cst.CSTNode) and dataclasses.is_dataclass(obj):
        return CSTProxy(obj)
    if isinstance(obj, collections.abc.Sequence) and not isinstance(obj, str):
        # bare sequences outside CST nodes become simple wrapped lists (unchanged behavior)
        return [wrap(x) for x in obj]
    return obj
