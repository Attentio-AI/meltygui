import dataclasses
import collections.abc
import libcst as cst


# ---------- tiny converters (one place) ----------

def _lexeme(py):
    if isinstance(py, bool):  return "True" if py else "False"
    if py is None:            return "None"
    if isinstance(py, float): return repr(py)
    if isinstance(py, int):   return str(py)
    if isinstance(py, str):   return py
    return str(py)


def _autobox_expr(py, hint: cst.CSTNode | None):
    if isinstance(py, cst.CSTNode): return py
    if isinstance(py, CSTProxy):    return py.node
    if isinstance(py, bool):        return cst.Name("True" if py else "False")
    if py is None:                  return cst.Name("None")
    if isinstance(py, int):         return cst.Integer(_lexeme(py))
    if isinstance(py, float):       return cst.Float(_lexeme(py))
    if isinstance(py, str):
        # Use hint to decide identifier vs string literal, otherwise default literal
        if isinstance(hint, cst.Name):         return cst.Name(py)
        if isinstance(hint, cst.SimpleString): return cst.SimpleString(repr(py))
        return cst.SimpleString(repr(py))
    return py  # last resort


def _to_libcst(value, orig_field):
    """Convert proxy/UI value back to what LibCST expects, using original field as kind hint."""
    # Already CST?
    if isinstance(value, CSTProxy):    return value.node
    if isinstance(value, cst.CSTNode): return value

    # Sequence field (e.g., body, args, params)
    if isinstance(orig_field, collections.abc.Sequence) and not isinstance(orig_field, str):
        # Use first element as a loose hint for element kind
        hint_elem = None
        for e in orig_field:
            if isinstance(e, cst.CSTNode):
                hint_elem = e
                break
        out = []
        for x in value:
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

    # Node-typed field (e.g., Arg.value, Assign.value, etc.)
    if isinstance(orig_field, cst.CSTNode):
        return _autobox_expr(value, orig_field)

    # Everything else (booleans sentinels, None, enums) - pass through
    return value


# ---------- list proxy (tiny) ----------

class CSTListProxy(list):
    def __init__(self, items, parent):
        super().__init__(wrap(x) for x in items)
        self._parent = parent

    # mark dirty & rebuild lazily
    def _touch(self):
        self._parent._dirty = True

    # mutators
    def __setitem__(self, i, v): super().__setitem__(i, wrap(v)); self._touch()

    def __delitem__(self, i):    super().__delitem__(i);          self._touch()

    def append(self, v):         super().append(wrap(v));         self._touch()

    def insert(self, i, v):      super().insert(i, wrap(v));      self._touch()

    def extend(self, it):        super().extend(wrap(x) for x in it); self._touch()

    def pop(self, i=-1):         val = super().pop(i); self._touch(); return val

    def clear(self):             super().clear();                  self._touch()

    def remove(self, v):         super().remove(v);                self._touch()

    def sort(self, *a, **k):     super().sort(*a, **k);            self._touch()

    def reverse(self):           super().reverse();                self._touch()


# ---------- object proxy (tiny) ----------

class CSTProxy:
    __slots__ = ("_node", "_field_names", "_dirty", "__dict__")

    def __init__(self, node: cst.CSTNode):
        object.__setattr__(self, "_node", node)
        object.__setattr__(self, "_dirty", False)
        self._refresh_fields_from(node)

    def _refresh_fields_from(self, node):
        # node is guaranteed dataclass by wrap(), but keep a check just in case
        if not dataclasses.is_dataclass(node):
            object.__setattr__(self, "_field_names", ())
            return
        fns = tuple(f.name for f in dataclasses.fields(node))
        object.__setattr__(self, "_field_names", fns)

        # Rebuild just the CST fields in __dict__, preserve non-CST attrs
        keep = {k: v for k, v in self.__dict__.items() if k not in fns}
        self.__dict__.clear()
        self.__dict__.update(keep)

        for name in fns:
            val = getattr(node, name)
            if isinstance(val, collections.abc.Sequence) and not isinstance(val, str):
                self.__dict__[name] = CSTListProxy(val, self)
            else:
                self.__dict__[name] = wrap(val)

    @property
    def node(self):
        if self._dirty:
            self._rebuild()
        return object.__getattribute__(self, "_node")

    def _rebuild(self):
        node = object.__getattribute__(self, "_node")
        kwargs = {}
        for name in self._field_names:
            current = self.__dict__[name]
            original = getattr(node, name)
            kwargs[name] = _to_libcst(current, original)  # your small, central converter
        new_node = node.with_changes(**kwargs)
        object.__setattr__(self, "_node", new_node)
        object.__setattr__(self, "_dirty", False)
        self._refresh_fields_from(new_node)



    # class spoofing so type-based routing works
    @property
    def __class__(self):
        return type(self._node)

    def __setattr__(self, name, value):
        # If it's a CST field, store & mark dirty; else just set as normal Python attr.
        if name in self._field_names:
            self.__dict__[name] = wrap(value)
            object.__setattr__(self, "_dirty", True)
        else:
            self.__dict__[name] = value

    def __repr__(self):
        return f"<{type(self._node).__name__}Proxy>"


def wrap(obj):
    if isinstance(obj, CSTProxy):  # don't rewrap
        return obj
    if isinstance(obj, cst.CSTNode) and dataclasses.is_dataclass(obj):
        return CSTProxy(obj)  # only dataclass-based CST nodes
    if isinstance(obj, collections.abc.Sequence) and not isinstance(obj, str):
        return [wrap(x) for x in obj]  # recurse sequences
    return obj  # pass through (sentinels, None, strings, etc.)
