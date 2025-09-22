import dataclasses
import collections.abc
import hashlib
import re
import libcst as cst

from src.lsd.gl_gui.model.core_model.new_core_model import exclude

# ==============================
# Formatting & whitespace constants
# ==============================

# no space '=' for keyword args
TIGHT_EQ = cst.AssignEqual(
    whitespace_before=cst.SimpleWhitespace(""),
    whitespace_after=cst.SimpleWhitespace("")
)

# ==============================
# Trivia-insensitive subtree hashing
# ==============================

_TRIVIA_FIELD_NAMES = {
    "whitespace", "leading_lines", "trailing_whitespace",
    "lpar", "rpar", "star", "comma", "semicolon",
    "header", "footer", "default_newline", "default_indent",
    "encoding", "has_trailing_newline", "trailing_comma",
}

_TRIVIA_NODE_TYPES = (
    cst.EmptyLine,
    cst.TrailingWhitespace,
    cst.SimpleWhitespace,
    cst.Newline,
    cst.ParenthesizedWhitespace,
)


def _is_trivia_node(n: cst.CSTNode) -> bool:
    return isinstance(n, _TRIVIA_NODE_TYPES)


def _fp_primitive(x):
    if x is None:       return ("none",)
    if x is True:       return ("bool", 1)
    if x is False:      return ("bool", 0)
    if isinstance(x, (int, float)): return ("num", repr(x))
    if isinstance(x, str):          return ("str", x)
    return ("repr", repr(x))


def _last_stmt(mapping) -> cst.BaseStatement | None:
    last = None
    for v in mapping.values():
        n = v.node if hasattr(v, "node") else v
        if isinstance(n, cst.BaseStatement):
            last = n
    return last



def _expr_placeholder_like_last_stmt(stmt: cst.BaseStatement | None) -> cst.BaseExpression:
    """
    If the last statement is a simple constant expression, mirror its *type*.
    Otherwise fall back to NAME('PLACEHOLDER').
    """
    if isinstance(stmt, cst.SimpleStatementLine) and stmt.body:
        first = stmt.body[0]
        if isinstance(first, cst.Expr):
            v = first.value
            if isinstance(v, (cst.Integer, cst.Float, cst.Imaginary, cst.SimpleString,
                              cst.Name, cst.List, cst.Tuple, cst.Set, cst.Dict,
                              cst.ConcatenatedString, cst.FormattedString)):
                # reuse your existing shape logic
                like = _placeholder_like(v)
                # _placeholder_like returns a CSTNode; check BaseExpression
                if isinstance(like, cst.BaseExpression):
                    return like
    # default
    return cst.Name("PLACEHOLDER")


def _make_const_line_like(last_stmt: cst.BaseStatement | None) -> cst.SimpleStatementLine:
    """
    Produce a one-line statement with a constant-like expression, inferred from last_stmt.
    This renders with its own newline automatically.
    """
    expr = _expr_placeholder_like_last_stmt(last_stmt)
    return cst.SimpleStatementLine([cst.Expr(value=expr)])


def _make_pass_line() -> cst.SimpleStatementLine:
    return cst.SimpleStatementLine([cst.Pass()])

def _fingerprint_struct(obj):
    # unwrap proxy
    if isinstance(obj, CSTProxy):
        obj = obj.node

    if isinstance(obj, cst.CSTNode) and dataclasses.is_dataclass(obj):
        typ = type(obj).__name__
        parts = []
        for f in dataclasses.fields(obj):
            name = f.name
            if name in _TRIVIA_FIELD_NAMES:
                continue
            val = getattr(obj, name)

            if isinstance(val, cst.CSTNode) and _is_trivia_node(val):
                continue

            if isinstance(val, collections.abc.Sequence) and not isinstance(val, str):
                seq_elems = []
                for e in val:
                    if isinstance(e, cst.CSTNode) and _is_trivia_node(e):
                        continue
                    seq_elems.append(_fingerprint_struct(e))
                parts.append((name, ("seq", tuple(seq_elems))))
            else:
                parts.append((name, _fingerprint_struct(val)))
        return ("cst", typ, tuple(parts))

    if isinstance(obj, collections.abc.Sequence) and not isinstance(obj, str):
        return ("seq", tuple(_fingerprint_struct(e) for e in obj))

    return _fp_primitive(obj)


def _hash_key_for_elem(elem, digest_size=12) -> str:
    """
    Stable short hex id for an element based on its trivia-insensitive structure.
    digest_size=12 -> 24 hex chars.
    """
    struct = _fingerprint_struct(elem)
    payload = repr(struct).encode("utf-8")
    h = hashlib.blake2b(payload, digest_size=digest_size)
    return h.hexdigest()


def _dedupe_key(base: str, mapping: collections.abc.Mapping) -> str:
    key = base
    i = 1
    while key in mapping:
        i += 1
        key = f"{base}~{i}"
    return key


_CONST_NAME_RE = re.compile(r"^CONSTANT_(\d+)$")


def _next_constant_index_in_body(mapping) -> int:
    """
    Scan a CSTDictProxy representing a .body field and find the next CONSTANT_N index.
    """
    max_idx = -1
    for v in mapping.values():
        n = v.node if hasattr(v, "node") else v
        if isinstance(n, cst.SimpleStatementLine):
            for small in n.body:
                if isinstance(small, cst.Assign):
                    # consider only the `Name = ...` targets
                    for tgt in small.targets:
                        t = tgt.target
                        if isinstance(t, cst.Name):
                            m = _CONST_NAME_RE.match(t.value)
                            if m:
                                try:
                                    idx = int(m.group(1))
                                    if idx > max_idx:
                                        max_idx = idx
                                except ValueError:
                                    pass
    return max_idx + 1


def _make_constant_assignment_line(name: str,
                                   value_expr: cst.BaseExpression | None = None,
                                   tight_equals: bool = True) -> cst.SimpleStatementLine:
    if value_expr is None:
        # default requested by you
        value_expr = cst.SimpleString('"default_val"')
    assign = cst.Assign(
        targets=[cst.AssignTarget(target=cst.Name(name))],
        value=value_expr
    )
    return cst.SimpleStatementLine([assign])

# ==============================
# Converters (primitive <-> LibCST)
# ==============================

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
        if isinstance(hint, cst.Name):         return cst.Name(py)
        if isinstance(hint, cst.SimpleString): return cst.SimpleString(repr(py))
        return cst.SimpleString(repr(py))
    return py


def _to_libcst(value, orig_field):
    if isinstance(value, CSTProxy):    return value.node
    if isinstance(value, cst.CSTNode): return value

    if isinstance(orig_field, collections.abc.Sequence) and not isinstance(orig_field, str):
        elems = value.values() if isinstance(value, collections.abc.Mapping) else value
        hint_elem = next((e for e in orig_field if isinstance(e, cst.CSTNode)), None)
        out = []
        for x in elems:
            if isinstance(x, CSTProxy):
                out.append(x.node)
            elif isinstance(x, cst.CSTNode):
                out.append(x)
            else:
                out.append(_autobox_expr(x, hint_elem))
        return out

    if isinstance(orig_field, str):
        return _lexeme(value)

    if isinstance(orig_field, cst.CSTNode):
        return _autobox_expr(value, orig_field)

    return value


# ==============================
# Naming helpers & placeholders
# ==============================

def _first_hint_elem(seq):
    for e in seq:
        if isinstance(e, cst.CSTNode):
            return e
    return None


def _default_placeholder_for_hint(hint: cst.CSTNode | None) -> cst.CSTNode:
    if isinstance(hint, cst.Arg) or hint is None:
        return cst.Arg(value=cst.Name("PLACEHOLDER"))
    if isinstance(hint, cst.BaseExpression):
        return cst.Name("PLACEHOLDER")
    if isinstance(hint, cst.Param):
        return cst.Param(name=cst.Name("placeholder"))
    if isinstance(hint, cst.Element):
        return cst.Element(value=cst.Name("PLACEHOLDER"))
    if isinstance(hint, cst.DictElement):
        return cst.DictElement(key=cst.SimpleString("'key'"), value=cst.Name("PLACEHOLDER"))
    if isinstance(hint, cst.BaseStatement):
        return cst.Pass()
    return cst.Name("PLACEHOLDER")


def _placeholder_like(expr: cst.CSTNode) -> cst.CSTNode:
    if isinstance(expr, cst.Integer):       return cst.Integer("0")
    if isinstance(expr, cst.Float):         return cst.Float("0.0")
    if isinstance(expr, cst.Imaginary):     return cst.Imaginary("0j")
    if isinstance(expr, cst.SimpleString):  return cst.SimpleString("''")
    if isinstance(expr, cst.Name):
        if expr.value in ("True", "False"): return cst.Name("False")
        if expr.value == "None":            return cst.Name("None")
        return cst.Name("PLACEHOLDER")
    if isinstance(expr, cst.List):          return cst.List([])
    if isinstance(expr, cst.Tuple):         return cst.Tuple([])
    if isinstance(expr, cst.Set):           return cst.Set([])
    if isinstance(expr, cst.Dict):          return cst.Dict([])
    if isinstance(expr, (cst.ConcatenatedString, cst.FormattedString)):
        return cst.SimpleString("''")
    return cst.Name("PLACEHOLDER")


def _next_indexed_name(prefix: str, taken: set[int]) -> str:
    n = 0
    while n in taken:
        n += 1
    return f"{prefix}_{n}"


def _collect_taken_indices_from_names(names: list[str], prefix: str) -> set[int]:
    pat = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    out = set()
    for s in names:
        m = pat.match(s)
        if m:
            out.add(int(m.group(1)))
    return out


def _last_arg_value(args_mapping) -> cst.CSTNode | None:
    last_val = None
    for v in args_mapping.values():
        n = v.node if hasattr(v, "node") else v
        if isinstance(n, cst.Arg):
            last_val = n.value
    return last_val


# ==============================
# Error state
# ==============================

@dataclasses.dataclass
class ErrorState:
    message: str
    field: str | None = None
    detail: str | None = None


# ==============================
# DictProxy for sequence fields
# ==============================

class CSTDictProxy(dict):
    """
    Ordered, dict-like proxy for sequence fields. Keys are stable subtree-hash IDs.
    Any mutation bubbles dirty to the parent CSTProxy.
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

    # --- helpers ---
    def _touch(self):
        self._parent._mark_dirty_up()

    # --- mutators ---
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
        it = other.items() if hasattr(other, "items") else other
        changed = False
        for k, v in it:
            w = wrap(v)
            if isinstance(w, CSTProxy):
                w._set_parent(self._parent, self._field)
            super().__setitem__(k, w);
            changed = True
        for k, v in kw.items():
            w = wrap(v)
            if isinstance(w, CSTProxy):
                w._set_parent(self._parent, self._field)
            super().__setitem__(k, w);
            changed = True
        if changed:
            self._touch()

    def __ior__(self, other):
        self.update(other)
        return self

    # --- ordering helpers ---
    def insert_before(self, key_to_insert, before_key):
        if key_to_insert not in self or before_key not in self:
            return
        val = super().pop(key_to_insert)
        new = {}
        for k, v in self.items():
            if k == before_key:
                new[key_to_insert] = val
            new[k] = v
        super().clear();
        super().update(new);
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
        super().clear();
        super().update(new);
        self._touch()

    # --- auto append that infers kind/value from context ---
    def append_to(self, value=None, *, factory=None, key: str | None = None) -> str:
        """
        Append an element to the end of this dict-backed sequence.

        Context-aware defaults:
          - Call.args:
              * infer positional/keyword legality from existing args
              * infer default value "shape" from last arg's value (e.g., int->0, str->'')
              * keyword names: arg_0, arg_1, ...
              * keyword '=' tight formatting
          - Parameters.{params,posonly_params,kwonly_params}: param_0, param_1, ...
          - Dict.elements: 'key_0', 'key_1', ...
          - List/Tuple.elements: Element(Name('arg_N'))
          - Module/IndentedBlock.body: Pass()
          - Fallback: type-shaped placeholder from hint
        """
        parent_px = self._parent
        parent_node = object.__getattribute__(parent_px, "_node")
        orig_seq = getattr(parent_node, self._field)
        hint_elem = _first_hint_elem(orig_seq)

        if value is None and not callable(factory):
            # ----------- Call.args: ----------
            if isinstance(parent_node, cst.Call) and self._field == "args":
                any_keyword = False
                keyword_names = []
                last_arg = None
                last_is_positional = False
                for v in self.values():
                    n = v.node if hasattr(v, "node") else v
                    if isinstance(n, cst.Arg):
                        last_arg = n
                        if n.keyword:
                            any_keyword = True
                            if isinstance(n.keyword, cst.Name):
                                keyword_names.append(n.keyword.value)
                if isinstance(last_arg, cst.Arg):
                    last_is_positional = (last_arg.keyword is None)

                exemplar_val = _last_arg_value(self)  # last existing arg value
                inferred_default = (
                    _placeholder_like(exemplar_val)
                    if exemplar_val is not None
                    else cst.Name("PLACEHOLDER")
                )

                if any_keyword or not last_is_positional:
                    taken = _collect_taken_indices_from_names(keyword_names, "arg")
                    name = _next_indexed_name("arg", taken)
                    value = cst.Arg(keyword=cst.Name(name), equal=TIGHT_EQ, value=inferred_default)
                else:
                    value = cst.Arg(value=inferred_default)

            # ----------- Parameters -----------
            elif isinstance(parent_node, cst.Parameters) and self._field in {"params", "posonly_params",
                                                                             "kwonly_params"}:
                existing = []
                for v in self.values():
                    n = v.node if hasattr(v, "node") else v
                    if isinstance(n, cst.Param) and isinstance(n.name, cst.Name):
                        existing.append(n.name.value)
                prefix = "param"  # you can split: po/kw/param if desired
                taken = _collect_taken_indices_from_names(existing, prefix)
                name = _next_indexed_name(prefix, taken)
                value = cst.Param(name=cst.Name(name))

            # ----------- Dict elements -----------
            elif isinstance(parent_node, cst.Dict) and self._field == "elements":
                existing = []
                for v in self.values():
                    n = v.node if hasattr(v, "node") else v
                    if isinstance(n, cst.DictElement) and isinstance(n.key, cst.SimpleString):
                        try:
                            existing.append(eval(n.key.value))
                        except Exception:
                            pass
                taken = _collect_taken_indices_from_names(existing, "key")
                key_name = _next_indexed_name("key", taken)
                value = cst.DictElement(key=cst.SimpleString(repr(key_name)),
                                        value=cst.Name("PLACEHOLDER"))

            # ----------- List/Tuple elements -----------
            elif isinstance(parent_node, (cst.List, cst.Tuple)) and self._field == "elements":
                existing = []
                for v in self.values():
                    n = v.node if hasattr(v, "node") else v
                    if isinstance(n, cst.Element) and isinstance(n.value, cst.Name):
                        existing.append(n.value.value)
                taken = _collect_taken_indices_from_names(existing, "arg")
                name = _next_indexed_name("arg", taken)
                value = cst.Element(value=cst.Name(name))

            # ----------- Bodies & fallback -----------
            elif isinstance(parent_node, (cst.Module, cst.IndentedBlock)) and self._field == "body":
                # Build CONSTANT_N="default_val" as a full statement line (renders with newline)
                next_idx = _next_constant_index_in_body(self)
                const_name = f"CONSTANT_{next_idx}"
                value = _make_constant_assignment_line(const_name)

            else:
                value = _default_placeholder_for_hint(hint_elem)

        if value is None and callable(factory):
            value = factory(hint_elem)

        # wrap & wire
        w = wrap(value)
        if isinstance(w, CSTProxy):
            w._set_parent(parent_px, self._field)

        # key: use hash (dedupe)
        if key is None:
            base = _hash_key_for_elem(w)
            key = _dedupe_key(base, self)

        self[key] = w  # go through override so _touch() runs  # __setitem__ bubbles dirty
        return key


# ==============================
# CSTProxy with error handling
# ==============================

@exclude(["star", "header", "footer", "comma", "default_newline", "default_indent", "encoding", "has_trailing_newline"])
class CSTProxy:
    __slots__ = ("_node", "_field_names", "_dirty", "_parent", "_parent_field", "_error", "__dict__")

    def __init__(self, node: cst.CSTNode):
        object.__setattr__(self, "_node", node)
        object.__setattr__(self, "_dirty", False)
        object.__setattr__(self, "_parent", None)
        object.__setattr__(self, "_parent_field", None)
        object.__setattr__(self, "_error", None)  # ErrorState | None
        self._refresh_fields_from(node)

    # class spoofing so type-based routing works
    @property
    def __class__(self):
        return type(self._node)

    # ---- error handling ----
    @property
    def has_error(self) -> bool:
        return object.__getattribute__(self, "_error") is not None

    @property
    def error(self) -> ErrorState | None:
        return object.__getattribute__(self, "_error")

    def clear_error(self):
        object.__setattr__(self, "_error", None)

    # materialize node safely
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
    def _refresh_fields_from(self, node):
        if not dataclasses.is_dataclass(node):
            object.__setattr__(self, "_field_names", ())
            return
        fns = tuple(f.name for f in dataclasses.fields(node))
        object.__setattr__(self, "_field_names", fns)

        keep = {k: v for k, v in self.__dict__.items() if k not in fns}
        self.__dict__.clear()
        self.__dict__.update(keep)

        for name in fns:
            val = getattr(node, name)
            if isinstance(val, collections.abc.Sequence) and not isinstance(val, str):
                self.__dict__[name] = _make_sequence_proxy(self, name, list(val))
            else:
                child = wrap(val)
                if isinstance(child, CSTProxy):
                    child._set_parent(self, name)
                self.__dict__[name] = child

    def _rebuild(self):
        node = object.__getattribute__(self, "_node")
        try:
            kwargs = {}
            for name in self._field_names:
                current = self.__dict__[name]
                original = getattr(node, name)
                kwargs[name] = _to_libcst(current, original)
            new_node = node.with_changes(**kwargs)
        except Exception as e:
            object.__setattr__(self, "_error", ErrorState(
                message=f"rebuild failed on {type(node).__name__}",
                field=None,
                detail=repr(e),
            ))
            # keep old node; leave _ = True so caller/UI can decide to auto-resolve
            return

        object.__setattr__(self, "_node", new_node)
        object.__setattr__(self, "_dirty", False)
        object.__setattr__(self, "_error", None)
        self._refresh_fields_from(new_node)

    def auto_resolve_error(self) -> bool:
        """
        Attempt to repair bad field shapes in-place.
        Returns True if a subsequent rebuild succeeds.
        """
        node = object.__getattribute__(self, "_node")
        repaired_any = False

        for name in self._field_names:
            original = getattr(node, name)
            current = self.__dict__[name]

            # 1) Sequence field as mapping (CSTDictProxy)
            if isinstance(original, collections.abc.Sequence) and not isinstance(original, str):
                hint = _first_hint_elem(original)

                # If not a mapping, coerce into mapping
                if not isinstance(current, collections.abc.Mapping):
                    seq = current if isinstance(current, collections.abc.Sequence) else [current]
                    fixed = []
                    for elem in seq:
                        if isinstance(elem, CSTProxy):
                            fixed.append(elem.node)
                        elif isinstance(elem, cst.CSTNode):
                            fixed.append(elem)
                        else:
                            fixed.append(_autobox_expr(elem, hint) if hint else _default_placeholder_for_hint(hint))
                    self.__dict__[name] = _make_sequence_proxy(self, name, fixed)
                    repaired_any = True
                else:
                    # validate each mapping value
                    mapping = current
                    new_values = []
                    for _, v in mapping.items():
                        n = v.node if isinstance(v, CSTProxy) else v
                        if isinstance(n, cst.CSTNode):
                            new_values.append(n)
                        else:
                            try:
                                new_values.append(
                                    _autobox_expr(n, hint) if hint else _default_placeholder_for_hint(hint))
                            except Exception:
                                new_values.append(_default_placeholder_for_hint(hint))
                                repaired_any = True

                    # Special legality for Call.args: no positional after keyword
                    if isinstance(node, cst.Call) and name == "args":
                        vals = new_values
                        # normalize order legality
                        seen_kw = False
                        kw_names = [a.keyword.value for a in vals if
                                    isinstance(a, cst.Arg) and isinstance(a.keyword, cst.Name)]
                        taken = _collect_taken_indices_from_names(kw_names, "arg")

                        coerced = []
                        for a in vals:
                            if isinstance(a, cst.Arg):
                                if a.keyword is not None:
                                    seen_kw = True
                                    coerced.append(a)
                                else:
                                    if seen_kw:
                                        idx_name = _next_indexed_name("arg", taken)
                                        taken.add(int(idx_name.split("_")[-1]))
                                        coerced.append(
                                            cst.Arg(keyword=cst.Name(idx_name), equal=TIGHT_EQ, value=a.value))
                                    else:
                                        coerced.append(a)
                            else:
                                coerced.append(cst.Arg(value=cst.Name("PLACEHOLDER")))
                        new_values = coerced

                    self.__dict__[name] = _make_sequence_proxy(self, name, new_values)
                    repaired_any = True

            # 2) Leaf string field
            elif isinstance(original, str):
                try:
                    self.__dict__[name] = _lexeme(current)
                except Exception:
                    self.__dict__[name] = _lexeme("")
                repaired_any = True

            # 3) Node-typed field
            elif isinstance(original, cst.CSTNode):
                v = current
                n = v.node if isinstance(v, CSTProxy) else v
                if not isinstance(n, cst.CSTNode):
                    try:
                        self.__dict__[name] = wrap(_autobox_expr(n, original))
                    except Exception:
                        self.__dict__[name] = wrap(_placeholder_like(original))
                    repaired_any = True

            # 4) Other scalars: rely on _to_libcst during rebuild

        pre = self._error
        self._rebuild()
        return (not self.has_error) and (repaired_any or pre is not None)

    # generic append API working on any sequence proxy this proxy owns
    def append_to(self,
                  field_name: str,
                  value=None,
                  *,
                  factory=None,
                  key: str | None = None) -> str:
        seq = self.__dict__.get(field_name, None)
        if not isinstance(seq, CSTDictProxy):
            raise TypeError(f"Field '{field_name}' is not a sequence (CSTDictProxy); got {type(seq).__name__}")

        # Decide placeholder if value=None
        orig_seq = getattr(object.__getattribute__(self, "_node"), field_name)
        hint_elem = _first_hint_elem(orig_seq)
        if value is None:
            if callable(factory):
                value = factory(hint_elem)
            else:
                # Defer to the dict proxy's context-aware append_to (smarts live there)
                return seq.append_to(value=None, factory=None, key=key)

        # Wrap & ded parent if needed, then append via seq
        w = wrap(value)
        if isinstance(w, CSTProxy):
            w._set_parent(self, field_name)
        if key is None:
            base = _hash_key_for_elem(w)
            key = _dedupe_key(base, seq)
        seq[key] = w  # bubbles dirty
        return key

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


# ==============================
# Sequence proxy factory
# ==============================

def _make_sequence_proxy(parent_px: CSTProxy, field_name: str, seq_values: list) -> CSTDictProxy:
    used = {}
    kv = []
    for elem in seq_values:
        base = _hash_key_for_elem(elem)
        key = _dedupe_key(base, used)
        used[key] = True
        kv.append((key, elem))
    return CSTDictProxy(kv, parent_px, field_name)


# ==============================
# Public wrapper
# ==============================

def wrap(obj):
    if isinstance(obj, CSTProxy):  return obj
    if isinstance(obj, cst.CSTNode) and dataclasses.is_dataclass(obj):
        return CSTProxy(obj)
    if isinstance(obj, collections.abc.Sequence) and not isinstance(obj, str):
        # bare sequences outside of fields become simple wrapped lists
        return [wrap(x) for x in obj]
    return obj
