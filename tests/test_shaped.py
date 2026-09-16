"""Shape-refined default routing (`src/lsd/gl_gui/shaped.py`).

- the pattern grammar: ints / None / `...` / one-of sets
- value signatures: tuples are 1-D arrays, tensors report .shape, dtype KIND
  with torch-style promotion for sequences
- best_match specificity: score, then MRO distance, then registration order
- end to end through Melty.get_default_view_function: shape beats the
  type-name tier, the attribute-name tier beats shape
"""
import os
import sys


import numpy as np
import pytest
torch = pytest.importorskip("torch")

from meltygui.core.shaped import Shaped
from meltygui.core.shaped import best_match
from meltygui.core.shaped import mro_distance
from meltygui.core.shaped import shape_matches
from meltygui.core.shaped import value_dtype
from meltygui.core.shaped import value_shape
from meltygui.core.shaped import SEQ_DTYPE_SCAN_CAP


# ---------------------------------------------------------------- grammar --

def test_pattern_normalisation_and_repr():
    assert Shaped(tuple, 3).shape == (3,)
    assert Shaped(tuple, None).shape == (None,)
    assert Shaped(tuple, [3, None]).shape == (3, None)
    s = Shaped(tuple, ({3, 4},), float)
    assert s.shape == (frozenset({3, 4}),)
    assert hash(s) == hash(Shaped(tuple, ({4, 3},), float))  # hashable, order-free
    assert repr(s) == "Shaped(tuple, ({3, 4},), float)"
    assert repr(Shaped("Tensor", (None, None, None, ...))) == 'Shaped(\'Tensor\', (None, None, None, ...))'
    assert repr(Shaped(tuple, (3,), (float, int))) == "Shaped(tuple, (3,), (float, int))"


def test_pattern_validation():
    with pytest.raises(ValueError):
        Shaped(tuple, (..., None))            # ... must be last
    with pytest.raises(TypeError):
        Shaped(tuple, (True,))                # bool is not an extent
    with pytest.raises(TypeError):
        Shaped(tuple, ("3",))
    with pytest.raises(TypeError):
        Shaped(tuple, (3,), str)              # kinds only
    with pytest.raises(TypeError):
        Shaped(3, (3,))                       # base = type or name


@pytest.mark.parametrize("pattern, shape, ok", [
    ((3,), (3,), True),
    ((3,), (4,), False),
    ((None,), (99,), True),
    ((None,), (1, 2), False),
    ((None, None), (64, 512), True),
    ((None, None), (64,), False),
    ((None, None), (2, 3, 4), False),
    ((None, None, None, ...), (2, 3, 4), True),
    ((None, None, None, ...), (2, 3, 4, 5, 6), True),
    ((None, None, None, ...), (2, 3), False),
    ((3, None, None), (3, 10, 10), True),
    ((3, None, None), (4, 10, 10), False),
    ((...,), (), True),
    ((...,), (7, 7), True),
    ((), (), True),
    ((), (1,), False),
])
def test_shape_matches(pattern, shape, ok):
    assert shape_matches(Shaped(tuple, pattern).shape, shape) is ok


def test_one_of_axis():
    s = Shaped(tuple, ({3, 4},), float)
    assert s.matches((0.1, 0.2, 0.3))
    assert s.matches((0.1, 0.2, 0.3, 1.0))
    assert not s.matches((0.1, 0.2))


# ------------------------------------------------------------- signatures --

def test_value_shape_sequences_and_arrays():
    assert value_shape((1, 2, 3)) == (3,)
    assert value_shape([1, 2]) == (2,)
    assert value_shape("abc") is None
    assert value_shape({"a": 1}) is None
    assert value_shape(3.0) is None
    assert value_shape(None) is None
    assert value_shape(torch.zeros(2, 3, 4)) == (2, 3, 4)
    assert value_shape(torch.tensor(1.0)) == ()
    assert value_shape(np.zeros((5, 6))) == (5, 6)


def test_value_dtype_promotion_like_torch():
    assert value_dtype((0.1, 0.2, 0.3)) is float
    assert value_dtype((0, 0, 0, 0.1)) is float          # mixed promotes to float
    assert value_dtype((1, 2, 3)) is int
    assert value_dtype((True, False)) is bool
    assert value_dtype((True, 2)) is int
    assert value_dtype((1, 2j)) is complex
    assert value_dtype((1, "a", 2)) is None
    assert value_dtype(((1, 2), (3, 4))) is None          # nested → not a kind
    assert value_dtype(()) is None
    assert value_dtype((np.float32(0.5), 1)) is float     # numpy scalars
    assert value_dtype((np.int64(1), 2)) is int


def test_value_dtype_tensors():
    assert value_dtype(torch.zeros(2, dtype=torch.bfloat16)) is float
    assert value_dtype(torch.zeros(2, dtype=torch.float64)) is float
    assert value_dtype(torch.zeros(2, dtype=torch.int8)) is int
    assert value_dtype(torch.zeros(2, dtype=torch.uint8)) is int
    assert value_dtype(torch.zeros(2, dtype=torch.bool)) is bool
    assert value_dtype(torch.zeros(2, dtype=torch.complex64)) is complex
    assert value_dtype(np.zeros(2, dtype=np.float16)) is float
    assert value_dtype(np.zeros(2, dtype=np.uint32)) is int
    assert value_dtype(np.zeros(2, dtype=bool)) is bool
    assert value_dtype(np.zeros(2, dtype=object)) is None


def test_sequence_dtype_scan_is_capped():
    big = tuple(range(SEQ_DTYPE_SCAN_CAP + 1))
    assert value_dtype(big) is None
    assert value_dtype(tuple(range(SEQ_DTYPE_SCAN_CAP))) is int
    # An unbounded 1-D pattern with a dtype therefore never matches a huge list.
    assert not Shaped(tuple, (None,), int).matches(big)
    assert Shaped(tuple, (None,)).matches(big)


def test_mro_distance_by_type_and_name():
    class Base: ...
    class Sub(Base): ...
    assert mro_distance(Sub, Sub) == 0
    assert mro_distance(Base, Sub) == 1
    assert mro_distance("Base", Sub) == 1
    assert mro_distance(Base, int) == -1
    assert mro_distance("Tensor", torch.nn.Parameter) == 1
    assert mro_distance("Tensor", torch.Tensor) == 0
    assert mro_distance("ndarray", np.ndarray) == 0


# -------------------------------------------------------- the two use cases --

COLOR3 = Shaped(tuple, (3,), float)
COLOR4 = Shaped(tuple, (4,), float)
LINES1 = Shaped("Tensor", (None,))
LINES2 = Shaped("Tensor", (None, None))
VOXELS = Shaped("Tensor", (None, None, None, ...))


def test_colour_tuples():
    assert COLOR3.matches((0.2, 0.5, 1.0))
    assert COLOR4.matches((0, 0, 0, 0.1))           # real tint with int zeros
    assert not COLOR3.matches((1, 2, 3))             # int tuple (version / pos)
    assert not COLOR3.matches((0.2, 0.5))
    assert not COLOR3.matches((0.2, 0.5, 1.0, 0.5))
    assert not COLOR3.matches(("a", "b", "c"))
    assert not COLOR3.matches([0.2, 0.5, 1.0])       # list is not a tuple
    assert not COLOR3.matches(torch.zeros(3))        # tensor of 3 is not a tuple


def test_tensor_ranks():
    for t in (torch.zeros(5), torch.zeros(5, dtype=torch.int32)):
        assert LINES1.matches(t) and not LINES2.matches(t) and not VOXELS.matches(t)
    t2 = torch.zeros(64, 512)
    assert LINES2.matches(t2) and not LINES1.matches(t2) and not VOXELS.matches(t2)
    for t in (torch.zeros(2, 3, 4), torch.zeros(2, 3, 4, 5), torch.zeros(1, 2, 3, 4, 5, 6)):
        assert VOXELS.matches(t) and not LINES2.matches(t)
    assert not VOXELS.matches(torch.tensor(1.0))
    assert not LINES1.matches(torch.tensor(1.0))
    # subclasses route through the MRO name match
    assert LINES2.matches(torch.nn.Parameter(torch.zeros(3, 3)))
    # an ndarray is NOT a Tensor
    assert not LINES2.matches(np.zeros((3, 3)))


# ------------------------------------------------------------- best_match --

def test_best_match_specificity_and_ties():
    generic = object()
    color = object()
    reg = {Shaped(tuple, (None,)): generic, COLOR3: color}
    assert best_match(reg.items(), (0.1, 0.2, 0.3)) is color        # score 3 > 1
    assert best_match(reg.items(), (1, 2, 3)) is generic             # dtype fails → falls back
    assert best_match(reg.items(), (1, 2)) is generic
    assert best_match(reg.items(), "abc") is None
    assert best_match(reg.items(), None) is None
    assert best_match({}.items(), (1, 2, 3)) is None

    # Equal score: closer MRO wins.
    class Vec(tuple): ...
    base = object(); sub = object()
    reg2 = {Shaped(tuple, (3,)): base, Shaped(Vec, (3,)): sub}
    assert best_match(reg2.items(), Vec((1, 2, 3))) is sub
    assert best_match(reg2.items(), (1, 2, 3)) is base

    # Equal shape + equal distance: first registered wins.
    a = object(); b = object()
    assert best_match({Shaped(tuple, (3,)): a, Shaped(tuple, (3,)): b}.items(), (1, 2, 3)) is b  # same key → overwritten
    assert best_match([(Shaped(tuple, (3,)), a), (Shaped(tuple, (3, )), b)], (1, 2, 3)) is a


def test_best_match_dtype_loser_does_not_block_next():
    # A higher-scoring entry that fails ONLY on dtype must not shadow a
    # lower-scoring entry that fits.
    strict = object(); loose = object()
    reg = [(Shaped(tuple, (3,), float), strict), (Shaped(tuple, (3,)), loose)]
    assert best_match(reg, (1, 2, 3)) is loose
    assert best_match(reg, (1.0, 2, 3)) is strict


def test_best_match_real_type_override():
    class Fancy(tuple): ...
    f = object()
    reg = {Shaped(Fancy, (3,)): f}
    assert best_match(reg.items(), (1, 2, 3)) is None
    assert best_match(reg.items(), (1, 2, 3), real_type=Fancy) is f


# ------------------------------------------------------ through Melty --

@pytest.fixture
def meltygui():
    from meltygui.core.melty import Melty
    saved = (dict(Melty.default_funcs_by_shape), dict(Melty.default_lenses_by_shape),
             dict(Melty.default_funcs_by_name), dict(Melty.default_funcs_by_type))
    yield Melty
    Melty.default_funcs_by_shape.clear(); Melty.default_funcs_by_shape.update(saved[0])
    Melty.default_lenses_by_shape.clear(); Melty.default_lenses_by_shape.update(saved[1])
    Melty.default_funcs_by_name.clear(); Melty.default_funcs_by_name.update(saved[2])
    Melty.default_funcs_by_type.clear(); Melty.default_funcs_by_type.update(saved[3])


def test_melty_precedence_name_over_shape_over_type(meltygui):
    by_name = object(); by_shape = object(); by_type_name = object(); by_type = object()
    meltygui.default_funcs_by_name["pos"] = by_name
    meltygui.default_funcs_by_shape[Shaped(tuple, (3,), float)] = by_shape
    meltygui.default_funcs_by_name["tuple"] = by_type_name
    meltygui.default_funcs_by_type[tuple] = by_type
    look = meltygui.get_default_view_function
    v = (0.1, 0.2, 0.3)
    assert look(real_type=tuple, attrib_key="pos", value=v) is by_name          # name wins
    assert look(real_type=tuple, attrib_key="vec##x", value=v) is by_shape     # shape beats type tiers
    assert look(real_type=tuple, attrib_key="vec##x", value=(1, 2, 3)) is by_type_name  # no shape match → type name
    del meltygui.default_funcs_by_name["tuple"]
    assert look(real_type=tuple, attrib_key="vec##x", value=(1, 2, 3)) is by_type
    assert look(real_type=tuple, attrib_key="vec##x", value=None) is by_type   # None has no shape


def test_melty_tensor_rank_routing(meltygui):
    lines = object(); voxels = object(); fallback = object()
    meltygui.default_funcs_by_shape[LINES1] = lines
    meltygui.default_funcs_by_shape[LINES2] = lines
    meltygui.default_funcs_by_shape[VOXELS] = voxels
    meltygui.default_funcs_by_name["Tensor"] = fallback
    look = lambda t: meltygui.get_default_view_function(real_type=type(t), attrib_key="t", value=t)
    assert look(torch.zeros(8)) is lines
    assert look(torch.zeros(8, 8)) is lines
    assert look(torch.zeros(8, 8, 8)) is voxels
    assert look(torch.zeros(2, 8, 8, 8)) is voxels
    assert look(torch.tensor(0.5)) is fallback


def test_melty_lens_resolution(meltygui):
    by_type = object(); by_shape = object()
    meltygui.default_lenses_by_type[tuple] = by_type
    try:
        meltygui.default_lenses_by_shape[Shaped(tuple, (3,), float)] = by_shape
        assert meltygui.get_default_lens_function((0.1, 0.2, 0.3)) is by_shape
        assert meltygui.get_default_lens_function((1, 2)) is by_type
        assert meltygui.get_default_lens_function("x") is None
    finally:
        meltygui.default_lenses_by_type.pop(tuple, None)


def test_registered_app_defaults_route_the_two_use_cases():
    """The real registrations: draw_tuple for float 3/4-tuples, draw_line_graph
    for 1-D/2-D tensors, draw_voxels for 3-D+ (importing the views registers
    them)."""
    from meltygui.core.melty import Melty
    import meltygui.core.render_dispatch as new_core_view
    import meltygui.core.graph_core as line_graph_playground
    import meltygui.tensor.voxel_playground as voxel_playground

    look = lambda v, key="value": Melty.get_default_view_function(real_type=type(v), attrib_key=key, value=v)
    assert look((0.2, 0.5, 1.0)) is new_core_view.draw_tuple
    assert look((0, 0, 0, 0.1)) is new_core_view.draw_tuple
    assert look((1, 2, 3)) is not new_core_view.draw_tuple            # int triple → collection
    assert look((0, 0, 0), key="tint") is new_core_view.draw_tuple    # name still wins
    assert look(torch.zeros(16)) is line_graph_playground.draw_line_graph
    assert look(torch.zeros(16, 4)) is line_graph_playground.draw_line_graph
    assert look(torch.zeros(4, 4, 4)) is voxel_playground.draw_voxels
    assert look(torch.zeros(2, 4, 4, 4)) is voxel_playground.draw_voxels
    assert look(torch.tensor(1.0)) is voxel_playground.draw_voxels      # "Tensor" fallback
    # Project-process captures arrive as NumPy arrays, including 3-D volumes.
    import numpy as np
    assert look(np.zeros(16)) is line_graph_playground.draw_line_graph
    assert look(np.zeros((16, 4))) is line_graph_playground.draw_line_graph
    assert look(np.zeros((4, 4, 4))) is voxel_playground.draw_voxels
    assert look(np.zeros((2, 4, 4, 4))) is voxel_playground.draw_voxels
