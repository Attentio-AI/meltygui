"""Shape-refined default routing: `Shaped(of, shape, dtype)`.

`is_default_for` / `is_lens_for` route a value to a view by its TYPE or type
NAME. A `Shaped` entry refines that by the value's SHAPE (and optionally its
dtype), so "tuples of 3 floats" and "tensors with 3+ dims" are both one
registration:

    @render_func(is_default_for=('tint', Shaped(tuple, (3,), float),
                                         Shaped(tuple, (4,), float)))
    @render_func(is_default_for=(Shaped("Tensor", (None, None)),))          # 2-D
    @render_func(is_default_for=("GLTexture", Shaped("Tensor", (None, None, None, ...))))

The unifying idea: every value has a signature `(type, shape, dtype)` and a
tuple is a tiny 1-D tensor — `(0.2, 0.5, 1.0)` is `(tuple, (3,), float)`,
`torch.randn(8, 64, 64)` is `(Tensor, (8, 64, 64), float)`. One pattern
grammar covers both.

Shape PATTERN grammar — plain tuples, Python's own shape idiom:

    3          this axis has extent exactly 3
    None       this axis has any extent                  (TensorShape([None, 3]))
    ...        any number of further axes; must be LAST  (tuple[int, ...])
    {3, 4}     this axis has one of these extents

    (3,)                      len-3 tuple / 1-D tensor of 3
    (None, None)              exactly 2-D
    (None, None, None, ...)   3-D or more
    ({3, 4},)                 len 3 or 4

`dtype` is a Python KIND — `float`, `int`, `bool`, `complex` — or a tuple of
kinds (any-of), or None (don't care). Tensor/ndarray dtypes map to their kind
(bf16/f16/f32/f64 → float, every int width → int). A SEQUENCE's dtype is its
PROMOTED kind, like torch.result_type: any float present → float, all ints →
int, all bools → bool, anything else (str, nested, mixed) → None. So
`Shaped(tuple, (3,), float)` takes `(0, 0, 0, 0.1)` (a real tint) but not
`(1, 2, 3)` (a version / int triple), which keeps int positions and extents
off the colour picker.

Cost contract: shape extraction is O(1) — `.shape` on tensors, `len()` on
sequences. The only O(n) step is the sequence dtype promotion and it runs ONLY
after the shape pattern already matched (so the pattern bounds n) and never
past `SEQ_DTYPE_SCAN_CAP` elements (dtype = None beyond it). No value-RANGE
constraints on purpose: they'd be an O(content) scan on tensors.

Precedence among Shaped entries is by `score()`: the more constrained pattern
wins (fixed axis 2, `None` axis 1, `...` 0, dtype +1), so a generic
`(None,)` never steals a `(3,), float` colour. Ties → closer MRO match →
registration order. This module is dependency-free so it can be imported from
anywhere (registrations live in view modules; resolution lives in melty).
"""
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Sequences longer than this never get a dtype (pattern-matched first, so only
# an unbounded `(None,)`/`(...)` pattern can reach it).
SEQ_DTYPE_SCAN_CAP = 64

_KINDS = (float, int, bool, complex)


def _freeze_axis(axis):
    if axis is None or axis is Ellipsis:
        return axis
    if isinstance(axis, bool):
        raise TypeError(f"shape axis must be int/None/.../set, got bool {axis!r}")
    if isinstance(axis, int):
        if axis < 0:
            raise ValueError(f"shape axis extent must be >= 0, got {axis}")
        return axis
    if isinstance(axis, (set, frozenset, list, tuple)):
        members = frozenset(axis)
        if not members or any(not isinstance(m, int) or isinstance(m, bool) or m < 0
                              for m in members):
            raise TypeError(f"one-of axis must hold non-negative ints, got {axis!r}")
        return members
    raise TypeError(f"unsupported shape axis {axis!r}")


@dataclass(frozen=True)
class Shaped:
    """A (type, shape-pattern, dtype) matcher. Hashable → usable as a registry
    key alongside plain types / type names in `is_default_for`."""
    of: Any                        # type or type NAME ("Tensor", "ndarray")
    shape: tuple                   # pattern, see module doc
    dtype: Any = None              # kind, tuple of kinds, or None

    def __post_init__(self):
        if not isinstance(self.of, (type, str)):
            raise TypeError(f"Shaped.of must be a type or a type name, got {self.of!r}")
        if isinstance(self.shape, (int, type(None))):
            shape = (self.shape,)
        else:
            shape = tuple(self.shape)
        shape = tuple(_freeze_axis(a) for a in shape)
        if Ellipsis in shape[:-1]:
            raise ValueError("`...` may only appear as the LAST axis of a shape pattern")
        object.__setattr__(self, "shape", shape)
        dtype = self.dtype
        if dtype is not None:
            kinds = tuple(dtype) if isinstance(dtype, (tuple, list, set, frozenset)) else (dtype,)
            for k in kinds:
                if k not in _KINDS:
                    raise TypeError(f"Shaped.dtype must be one of {_KINDS}, got {k!r}")
            object.__setattr__(self, "dtype", kinds if len(kinds) > 1 else kinds[0])

    # -- display ---------------------------------------------------------
    def __repr__(self):
        of = self.of.__name__ if isinstance(self.of, type) else repr(self.of)
        axes = []
        for a in self.shape:
            if a is Ellipsis:
                axes.append("...")
            elif isinstance(a, frozenset):
                axes.append("{" + ", ".join(str(m) for m in sorted(a)) + "}")
            else:
                axes.append(str(a))
        shape = "(" + ", ".join(axes) + ("," if len(axes) == 1 else "") + ")"
        if self.dtype is None:
            return f"Shaped({of}, {shape})"
        dt = self.dtype
        dt = ("(" + ", ".join(k.__name__ for k in dt) + ")") if isinstance(dt, tuple) else dt.__name__
        return f"Shaped({of}, {shape}, {dt})"

    # -- matching --------------------------------------------------------
    @property
    def score(self) -> int:
        """Constraint strength; higher = more specific. Fixed / one-of axis 2,
        `None` axis 1 (still pins ndim), `...` 0, dtype +1."""
        s = 0
        for a in self.shape:
            if a is Ellipsis:
                continue
            s += 1 if a is None else 2
        if self.dtype is not None:
            s += 1
        return s

    def matches_type(self, real_type: type) -> int:
        """MRO distance of the closest class matching `of` (0 = exact), or -1."""
        return mro_distance(self.of, real_type)

    def matches_shape(self, shape) -> bool:
        return shape is not None and shape_matches(self.shape, shape)

    def matches_dtype(self, value) -> bool:
        if self.dtype is None:
            return True
        kind = value_dtype(value)
        if kind is None:
            return False
        return kind in self.dtype if isinstance(self.dtype, tuple) else kind is self.dtype

    def matches(self, value, real_type: Optional[type] = None) -> bool:
        """Full match: type (by MRO or name), then shape, then dtype — in that
        order so the O(n) sequence dtype scan only ever runs on a value whose
        shape already fit the pattern."""
        if self.matches_type(real_type if real_type is not None else type(value)) < 0:
            return False
        if not self.matches_shape(value_shape(value)):
            return False
        return self.matches_dtype(value)


# ---------------------------------------------------------------------------
# value → (shape, dtype)
# ---------------------------------------------------------------------------

def mro_distance(of, real_type: type) -> int:
    """Index in `real_type.__mro__` of the class `of` names (a type, or a
    type's __name__), or -1 when no class in the MRO matches."""
    mro = getattr(real_type, "__mro__", None)
    if mro is None:
        return -1
    if isinstance(of, str):
        for i, t in enumerate(mro):
            if t.__name__ == of:
                return i
        return -1
    for i, t in enumerate(mro):
        if t is of:
            return i
    return -1


def value_shape(value) -> Optional[tuple]:
    """The value's shape tuple, or None when it has none.

    Array-likes (anything with a tuple-able `.shape`: torch tensors, ndarrays,
    GLTexture, CudaVolumeView …) report it directly; tuples/lists report
    `(len,)` — a sequence is a 1-D array. str/bytes/dicts are not shaped."""
    shape = getattr(value, "shape", None)
    if shape is not None:
        try:
            return tuple(int(d) for d in shape)
        except (TypeError, ValueError):
            return None
    if isinstance(value, (tuple, list)):
        return (len(value),)
    return None


def _kind_of_dtype(dtype):
    """Map a torch / numpy dtype object to a Python kind, duck-typed so this
    module never imports either library."""
    # numpy: dtype.kind in 'biufcmMOSUV'
    kind_char = getattr(dtype, "kind", None)
    if isinstance(kind_char, str):
        return {"b": bool, "i": int, "u": int, "f": float, "c": complex}.get(kind_char)
    # torch: is_floating_point / is_complex properties; bool by name.
    if getattr(dtype, "is_floating_point", False):
        return float
    if getattr(dtype, "is_complex", False):
        return complex
    name = str(dtype)
    if name.endswith("bool"):
        return bool
    if "int" in name:
        return int
    return None


def value_dtype(value):
    """The value's KIND (float / int / bool / complex) or None.

    Array-likes map their dtype. Sequences promote like torch.result_type:
    any float → float, else all int → int, else all bool → bool; any other
    element kind → None. Sequences past SEQ_DTYPE_SCAN_CAP are None."""
    dtype = getattr(value, "dtype", None)
    if dtype is not None:
        return _kind_of_dtype(dtype)
    if isinstance(value, (tuple, list)):
        if len(value) > SEQ_DTYPE_SCAN_CAP:
            return None
        rank = 0  # 0 empty, 1 bool, 2 int, 3 float, 4 complex - promotion order
        for c in value:
            # bool first: it's an int subclass. duck-type checks are the fast
            # path; the isinstance fallbacks catch numpy scalars & subclasses.
            t = type(c)
            if t is bool or isinstance(c, bool):
                r = 1
            elif t is int or isinstance(c, int):
                r = 2
            elif t is float or isinstance(c, float):
                r = 3
            elif isinstance(c, complex):
                r = 4
            else:
                # numpy / torch 0-d scalars carry a dtype of their kind.
                k = _kind_of_dtype(getattr(c, "dtype", None))
                if k is None:
                    return None
                r = {bool: 1, int: 2, float: 3, complex: 4}[k]
            if r > rank:
                rank = r
        return (None, bool, int, float, complex)[rank]
    return None


def shape_matches(pattern: tuple, shape: tuple) -> bool:
    """Does the concrete `shape` fit the `pattern`? See the module doc."""
    if pattern and pattern[-1] is Ellipsis:
        fixed = pattern[:-1]
        if len(shape) < len(fixed):
            return False
    else:
        fixed = pattern
        if len(shape) != len(fixed):
            return False
    for want, got in zip(fixed, shape):
        if want is None:
            continue
        if isinstance(want, frozenset):
            if got not in want:
                return False
        elif got != want:
            return False
    return True


# ---------------------------------------------------------------------------
# Searching over a registry
# ---------------------------------------------------------------------------

def best_match(entries: Iterable, value, real_type: Optional[type] = None):
    """Pick the registered function whose `Shaped` key best fits `value`.

    `entries` iterates `(Shaped, func)` pairs (a dict's `.items()`). Returns
    the func or None. Most constrained pattern (score) wins; ties go to the
    closest MRO match, then to the earliest registered. The value's shape is
    extracted at most once, and only when some entry's type matched."""
    if real_type is None:
        real_type = type(value)
    best = None
    best_key = None
    shape = _UNSET = object()
    for shaped, func in entries:
        if func is None or not isinstance(shaped, Shaped):
            continue
        dist = shaped.matches_type(real_type)
        if dist < 0:
            continue
        if shape is _UNSET:
            shape = value_shape(value)
        if shape is None or not shaped.matches_shape(shape):
            continue
        # (score desc, mro distance asc) - compared as a tuple, first wins on ties.
        key = (shaped.score, -dist)
        if best_key is None or key > best_key:
            if not shaped.matches_dtype(value):
                continue
            best, best_key = func, key
    return best
