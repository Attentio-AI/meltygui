"""Tensor model functions and supporting definitions."""



class TensorDim(int):
    """A tensor dim index that is still an int everywhere it matters
    (indexing, comparisons, arithmetic, `int()`, pickling) but carries its own
    TYPE, so meltygui routes it to its own renderer instead of the plain int one
    — a dim picker rather than a number field.

    Values only stay TensorDim if whatever writes them keeps the type: a
    renderer registered `@render_func(is_default_for=TensorDim)` should return
    TensorDim(...), otherwise the first edit stores a plain int and the row
    falls back to the int renderer."""

    __slots__ = ()

    def __repr__(self):
        return f"TensorDim({int(self)})"


class TensorDims(tuple):
    """A SET of tensor dim indices (`mean_dims`) — tuple everywhere it
    matters, but typed so it routes to the same dim picker as TensorDim
    (multi-select tabs). A tuple needs SOME type to route by; this is the
    minimal one, and the renderer is shared."""

    __slots__ = ()

    def __repr__(self):
        return f"TensorDims({tuple(int(v) for v in self)})"


class Lut(str):
    """A LUT NAME that is still a str everywhere it matters (dict keys,
    comparisons, GLSL host lookups) but carries its own TYPE, so meltygui routes
    it to its own renderer — a dropdown of the available LUTs rather than a
    text field. Same contract as TensorDim: the renderer must return
    Lut(...) or the first edit stores a plain str and the row falls back to
    the generic str renderer."""

    __slots__ = ()

    def __repr__(self):
        return f"Lut({str(self)!r})"
