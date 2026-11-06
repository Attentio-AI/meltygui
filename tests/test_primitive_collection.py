"""Primitive dictionaries must render children through the public collection view."""
import meltygui
import meltygui_imgui as imgui
import pytest

from conftest import begin_frame, end_frame
from test_fast_draw_collection import melty
from test_render_func_integration import _tick_frame


@pytest.mark.parametrize("renderer", [meltygui.draw_collection, meltygui.draw_any])
@pytest.mark.parametrize("clip", [None, (20, 20, 500, 500)])
def test_primitive_collection_children(melty, renderer, clip):
    data = {"string": "Hello, world!", "integer": 42,
            "float": 3.14, "boolean": True}
    for _ in range(3):
        _tick_frame(melty)
        melty.channels_split = False
        begin_frame()
        imgui.begin("primitive collection")
        if clip is not None:
            melty.clip_stack.append(clip)
            imgui.set_cursor_screen_pos((30, 30))
        _, result, state = renderer(
            data, name="Primitive data", return_extras=True)
        assert state._stack_trace is None, str(state._stack_trace)
        assert result is data
        assert state.width > 0
        assert state.content_width > 0
        if clip is not None:
            assert state.abs_left + state.width <= clip[2]
        assert len(state._children) == len(data)
        for child in state._children.values():
            assert child._stack_trace is None, str(child._stack_trace)
            assert child.height > 0
        # A one-line string must not allocate a full-window text editor.
        assert state._children[0].height < 100
        meltygui.draw_bool(True, name="hello", width=100)
        if clip is not None:
            melty.clip_stack.pop()
        imgui.end()
        end_frame()
