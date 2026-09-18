"""fast_draw_collection hosts draw_collection's body without the wrapper."""
import time

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
    # The header builds real colours from the style manager (the shared
    # harness stubs it with a MagicMock).
    stub, stub_attr = meltygui.style_manager, meltygui.global_attrs.get("style_manager")
    meltygui.style_manager = meltygui.global_attrs["style_manager"] = ImGuiStyleManager()
    previous = Toggles.Collection.fast_draw_collection
    Toggles.Collection.fast_draw_collection = True
    yield meltygui
    Toggles.Collection.fast_draw_collection = previous
    meltygui.style_manager, meltygui.global_attrs["style_manager"] = stub, stub_attr


def _nested(depth, width=3):
    if depth == 0:
        return {"value": 1.5, "flag": True, "label": "text"}
    return {f"level_{depth}_{index}": _nested(depth - 1, width) for index in range(width)}


def _frames(meltygui, data, count=3, **kwargs):
    from meltygui.core.rendering.render_dispatch import draw_any
    result = None
    for _ in range(count):
        _tick_frame(meltygui)
        meltygui.channels_split = False     # a new frame's draw lists start unsplit
        begin_frame()
        imgui.begin("fast collection test")
        result = draw_any(data, name="settings", return_extras=True, **kwargs)
        imgui.end()
        end_frame()
    return result


def test_draw_any_forwards_collections_to_the_fast_host(melty):
    from meltygui.view.collection_view import draw_collection, fast_draw_collection
    for collection_type in (dict, list, tuple):
        assert melty.get_default_view_function(real_type=collection_type) is fast_draw_collection
    changed, value, outermost = _frames(melty, _nested(2))
    assert not changed and outermost._wrapper is draw_collection    # the tree's tile boundary
    draw_state = outermost._children[0]
    assert draw_state._wrapper is fast_draw_collection and draw_state._parent is outermost
    assert draw_state._view_func is draw_collection.__wrapped__
    assert draw_state.header_height > 0 and draw_state.height > draw_state.header_height
    nested = draw_state._children[0]
    assert nested._wrapper is fast_draw_collection and nested._parent is draw_state
    assert nested._collection_draw_state is None      # rows are immediate-mode drag handles
    assert melty.depth == 0 and melty.draw_state_stack == [] and melty.bg_stack == []


def test_both_hosts_share_identity_and_layout(melty):
    from meltygui.core.runtime.toggles import Toggles
    data = _nested(2)
    _, _, fast_state = _frames(melty, data)
    fast_box = (fast_state.width, fast_state.height, fast_state.header_height)
    fast_child = fast_state._children[1]
    Toggles.Collection.fast_draw_collection = False
    _, _, wrapper_state = _frames(melty, data)
    assert wrapper_state is fast_state                  # same unique -> saved state carries over
    assert wrapper_state._children[1] is fast_child
    assert (wrapper_state.width, wrapper_state.height, wrapper_state.header_height) == fast_box


def test_collapsed_collection_skips_its_body_and_keeps_a_drop_slot(melty):
    data = {"folder": _nested(1)}
    draw_state = _frames(melty, data)[2]._children[0]
    data = data["folder"]
    expanded_height = draw_state.height
    draw_state.expanded = False
    _frames(melty, {"folder": data})
    assert draw_state.height < expanded_height
    assert [slot[0] for slot in draw_state._dnd_extra_slots] == [len(data)]


def test_a_landed_drop_reorders_in_place_and_reports_changed(melty):
    from meltygui.core.input.drag_drop_core import DragDrop, DropEvent
    data = {"a": {"x": 1}, "b": {"x": 2}, "c": {"x": 3}}
    root = {"folder": data}
    draw_state = _frames(melty, root)[2]._children[0]
    DragDrop._pending_drops[draw_state] = DropEvent("reorder", "c", data["c"], 2, 0)
    changed, value, _ = _frames(melty, root, count=1)
    assert changed and value is root and root["folder"] is data and list(data) == ["c", "a", "b"]


def test_wrapper_only_requests_go_to_the_wrapper(melty):
    from meltygui.view.collection_view import draw_collection
    _, _, draw_state = _frames(melty, _nested(1), use_cache=True)
    assert draw_state._wrapper is draw_collection


def test_fast_host_is_cheaper_than_the_wrapper(melty):
    from meltygui.core.runtime.toggles import Toggles
    data = _nested(3)

    def frame_time():
        _frames(melty, data, count=3)
        start = time.perf_counter()
        _frames(melty, data, count=5)
        return (time.perf_counter() - start) / 5

    fast = frame_time()
    Toggles.Collection.fast_draw_collection = False
    wrapped = frame_time()
    print(f"\nfast {fast * 1000:.2f} ms/frame, wrapper {wrapped * 1000:.2f} ms/frame")
    assert fast < wrapped
