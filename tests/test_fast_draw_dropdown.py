"""fast_draw_dropdown hosts draw_dropdown's body without the wrapper; the
breadcrumb strip is a plain function drawing one fast dropdown per crumb."""
import meltygui_imgui as imgui
import pytest

from conftest import begin_frame, end_frame
from test_render_func_integration import _init_melty, _tick_frame


@pytest.fixture
def melty():
    imgui.get_io().ini_file_name = None     # a headless imgui.begin must not write imgui.ini into the checkout
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.core.styling.style_core import ImGuiStyleManager
    meltygui = _init_melty()
    meltygui.vis.root.draw_state_registry.clear()
    stub, stub_attr = meltygui.style_manager, meltygui.global_attrs.get("style_manager")
    meltygui.style_manager = meltygui.global_attrs["style_manager"] = ImGuiStyleManager()
    previous = Toggles.Collection.fast_draw_collection
    Toggles.Collection.fast_draw_collection = True
    yield meltygui
    Toggles.Collection.fast_draw_collection = previous
    meltygui.popover_focused_ds = None
    meltygui.style_manager, meltygui.global_attrs["style_manager"] = stub, stub_attr


def _frames(meltygui, draw, count=3):
    result = None
    for _ in range(count):
        _tick_frame(meltygui)
        meltygui.channels_split = False     # a new frame's draw lists start unsplit
        begin_frame()
        imgui.begin("fast dropdown test")
        result = draw()
        imgui.end()
        end_frame()
    return result


def test_fast_host_binds_the_body_and_its_state(melty):
    from meltygui.state.new_core_model import DropDownState
    from meltygui.view.dropdown_view import draw_dropdown, fast_draw_dropdown
    options = {"red": 1, "green": 2}
    changed, value, draw_state = _frames(melty, lambda: fast_draw_dropdown(
        1, collection=options, name="colour", width=120, show_header=False, return_extras=True))
    assert not changed and value == 1
    assert draw_state._wrapper is fast_draw_dropdown
    assert draw_state._view_func is draw_dropdown.__wrapped__
    state = draw_state.misc["drop_down_state"]
    assert isinstance(state, DropDownState) and state.selected_label == "red"
    assert draw_state.width == 120 and draw_state.height >= 25
    assert melty.depth == 0 and melty.draw_state_stack == [] and melty.unique_stack == []


def test_both_hosts_share_the_draw_state(melty):
    from meltygui.view.dropdown_view import draw_dropdown, fast_draw_dropdown
    options = {"red": 1, "green": 2}
    fast_state = _frames(melty, lambda: fast_draw_dropdown(
        1, collection=options, name="colour", show_header=False, return_extras=True))[2]
    wrapper_state = _frames(melty, lambda: draw_dropdown(
        1, collection=options, name="colour", show_header=False, return_extras=True))[2]
    assert wrapper_state is fast_state
    assert wrapper_state.misc["drop_down_state"] is fast_state.misc["drop_down_state"]


def test_wrapper_features_go_to_the_wrapper(melty):
    from meltygui.view.dropdown_view import draw_dropdown, fast_draw_dropdown
    draw_state = _frames(melty, lambda: fast_draw_dropdown(
        1, collection={"red": 1}, name="colour", show_header=False, use_cache=True, return_extras=True))[2]
    assert draw_state._wrapper is draw_dropdown


def test_breadcrumbs_draw_a_fast_dropdown_per_crumb(melty, tmp_path):
    from meltygui.core.core_render import render_func
    from meltygui.view import file_view
    from meltygui.view.dropdown_view import fast_draw_dropdown
    target = tmp_path / "pkg" / "module.py"
    target.parent.mkdir()
    target.write_text("x = 1\n")
    (target.parent / "other.py").write_text("y = 2\n")
    assert not hasattr(file_view.draw_breadcrumbs, "__wrapped__")     # a plain function
    seen = {}

    @render_func(use_cache=False, show_header=False)
    def host(input_value, draw_state, **kwargs):
        seen["result"] = file_view.draw_breadcrumbs(input_value, draw_state, width=4000, file_metadata={})
        seen["host"] = draw_state
        return False, input_value

    _frames(melty, lambda: host(str(target), name="crumb host"))
    assert seen["result"] == (False, str(target))
    strip = file_view._crumb_strips[(seen["host"].unique, "breadcrumbs")]
    parts = target.parts
    assert sorted(strip["states"]) == list(range(len(parts)))
    for crumb_state, _drop_down_state in strip["states"].values():
        assert crumb_state._wrapper is fast_draw_dropdown and crumb_state._parent is seen["host"]
    assert "_crumb_states" not in seen["host"].misc      # the host's persisted state stays clean

    # An open crumb reads its directory; the row on the path is highlighted.
    last_state, last_drop_down = strip["states"][len(parts) - 1]
    melty.popover_focused_ds = last_state
    _frames(melty, lambda: host(str(target), name="crumb host"), count=1)
    rows = strip["memos"][len(parts) - 1]["rows"]
    assert sorted(rows.values()) == sorted(str(path) for path in target.parent.iterdir())
    assert rows[last_drop_down.selected_path[0]] == str(target)
