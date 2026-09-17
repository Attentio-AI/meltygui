"""Native body placement stays below chrome before descendants capture offsets."""
import pytest
from conftest import begin_frame, end_frame
from test_render_func_integration import _init_melty, _tick_frame
from meltygui import imgui
from meltygui.core.core_render import render_func
from meltygui.core.layout import column_core


@pytest.mark.parametrize('solved_origin', [(0, 30), (12, 42)])
def test_native_body_uses_solved_origin_on_its_first_frames(gl_context, monkeypatch, solved_origin):
    runtime = _init_melty()
    observed, incoming = [], []

    def solve(state):
        incoming.append(state.window_pos)
        state.window_pos = solved_origin

    monkeypatch.setattr(column_core, 'window_edge_pass', solve)

    @render_func(tint=(.3, .5, .7), use_cache=False, show_bg=False,
                 show_header=False, indent=0)
    def body(input_value: str, draw_state=None):
        observed.append((draw_state.abs_left, draw_state.abs_top,
                         tuple(imgui.get_cursor_screen_pos()),
                         imgui.get_style().item_spacing[1]))
        return False, input_value

    for _ in range(2):
        _tick_frame(runtime)
        begin_frame()
        try:
            width, height = imgui.get_io().display_size
            imgui.set_cursor_screen_pos((0, 30))
            body('', name='native body cursor', closable=True, draggable=False,
                 frame_pinned=True, window_pos=(0, 30), width=width,
                 height=height-30, auto_resize=False)
        finally:
            end_frame()
    assert incoming == [(0, 30), (0, 30)]
    assert len(observed) == 2
    for x, y, cursor, spacing_y in observed:
        assert (x, y) == solved_origin
        assert cursor == pytest.approx((x, y + spacing_y))
