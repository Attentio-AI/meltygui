"""View identity helpers give the wrapper's historical values, so persisted
draw_states keep resolving and wrapper-free views share the same identity."""
import zlib

import pytest

from meltygui.core.melty import Melty
from meltygui.core.rendering import view_identity
from meltygui.core.rendering.view_identity import get_draw_state
from meltygui.core.rendering.view_identity import link_parent
from meltygui.core.rendering.view_identity import place_in_parent_window
from meltygui.core.rendering.view_identity import view_tile_id
from meltygui.core.rendering.view_identity import view_unique
from meltygui.state.new_core_model import DrawState


def _crc(text):
    return zlib.crc32(text.encode("utf-8")) & 0xffffffff


def _legacy_ui_id(suffix, idx=0):
    """The pre-extraction core_render.ui_id, verbatim in effect."""
    h = ((0 * 16777619) ^ _crc("")) & 0xffffffff
    return _crc(str(((h * 16777619) ^ _crc(str(suffix))) + (idx + 1)))


@pytest.fixture
def stacks(monkeypatch):
    monkeypatch.setattr(Melty, "depth", 0)
    monkeypatch.setattr(Melty, "unique_stack", [])
    monkeypatch.setattr(Melty, "melty_window_stack", [])
    monkeypatch.setattr(Melty, "draw_state_stack", [])
    view_identity._UI_ID_MEMO.clear()


def test_root_unique_matches_legacy_formula(stacks):
    unique, suffix = view_unique("Settings", "draw_collection", key="")
    assert unique == _legacy_ui_id("Settings" + "Settings" + "" + "draw_collection")
    assert suffix == f"Settings_draw_collection_{unique}_"


@pytest.mark.parametrize("key, index", [("row_gap", 0), (3, 3)])
def test_nested_unique_matches_legacy_formula(stacks, monkeypatch, key, index):
    window = DrawState()
    window.name = "Toggles"
    monkeypatch.setattr(Melty, "depth", 2)
    monkeypatch.setattr(Melty, "unique_stack", [1234])
    monkeypatch.setattr(Melty, "melty_window_stack", [window])
    unique, suffix = view_unique("row_gap", "as_float", key=key, old_suffix="outer")
    assert suffix == f"outer_1234_row_gap_{key}"
    assert unique == _legacy_ui_id(suffix + "row_gap" + "row_gap" + "Toggles" + str(key) + "as_float",
                                   idx=index)


def test_nested_unique_without_window_hashes_root(stacks, monkeypatch):
    monkeypatch.setattr(Melty, "depth", 1)
    unique, suffix = view_unique("a", "f", key="k", unique_name="u")
    assert suffix == "None_a_u_k"
    assert unique == _legacy_ui_id(suffix + "u" + "a" + "Root" + "k" + "f")


def test_draw_state_is_stable_per_unique_and_tile_id_follows_it(stacks, monkeypatch):
    monkeypatch.setattr(Melty, "vis", None)
    monkeypatch.setattr(Melty, "draw_state_registry", {})
    first = get_draw_state(77)
    assert get_draw_state(77) is first and first.unique == 77
    assert get_draw_state(78) is not first
    assert view_tile_id("name", 77, first) == f"name##{_crc('77' + str(first.id))}"


def test_link_parent_registers_in_enclosing_view(stacks, monkeypatch):
    parent, child = DrawState(), DrawState()
    monkeypatch.setattr(Melty, "draw_state_stack", [parent])
    link_parent(child)
    assert child._parent is parent
    assert parent._view_children[id(child)] is child


def test_place_in_parent_window_is_a_noop_at_the_true_root(stacks):
    view = DrawState()
    view.left_offset, view.top_offset = 5, 6
    place_in_parent_window(view)
    assert view.parent_window is None
    assert (view.left_offset, view.top_offset) == (5, 6)


def test_place_in_parent_window_without_view_offset(stacks):
    view, parent = DrawState(), DrawState()
    place_in_parent_window(view, parent_window=parent, left=12, view_offset=False)
    assert view.parent_window is parent
    assert (view.left_offset, view.top_offset) == (12, 0)
