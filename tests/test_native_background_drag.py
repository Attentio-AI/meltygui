"""Pinned workspace backgrounds pass left drags to native chrome; popovers do not."""
import pytest
from conftest import begin_frame, end_frame
from test_render_func_integration import _init_melty, _tick_frame
from meltygui.core.core_render import render_func
from meltygui.core.input.input_handler import InputHandler
from meltygui.core.windowing.titlebar import _STRIP_ID, _STRIP_PRIORITY
from meltygui.state.new_core_model import DrawState


@pytest.mark.parametrize('pinned', [False, True])
@pytest.mark.parametrize('control', [False, True])
def test_nested_background_move_capture(gl_context, monkeypatch, pinned, control):
    runtime = _init_melty()
    handler = InputHandler()
    handlers = []

    def action(self, names, view_id=None, **kwargs):
        # Capture the wrapper's background subscriptions, excluding border
        # handles. The gesture is in empty body space, away from those rects.
        if view_id == 'window_hold' and names == 'left_mouse_drag':
            handlers.append(self.name)
        return None
    monkeypatch.setattr(DrawState, 'on_action', action)

    @render_func(tint=(.3, .5, .7), use_cache=False, show_header=False,
                 auto_resize=False, closable=True)
    def body(input_value: str, draw_state=None):
        return False, input_value

    @render_func(tint=(.3, .5, .7), use_cache=False, show_header=False,
                 auto_resize=False, closable=True)
    def root(input_value: str, draw_state=None):
        body(input_value, name='workspace', window_pos=(0, 0), width=700,
             height=480, frame_pinned=pinned)
        return False, input_value

    _tick_frame(runtime)
    begin_frame()
    try:
        root('', name='native root', window_pos=(0, 30), width=780,
             height=550, draggable=False, frame_pinned=True)
    finally:
        end_frame()
    handler.register_hovered('workspace-blocker', [], priority=0,
                             tile_id='workspace', blocker=True)
    for name in handlers:
        handler.register_hovered(name, ['left_mouse_drag'], priority=-1,
                                 tile_id='workspace')
    handler.register_hovered(_STRIP_ID, ['non_blocking_left_mouse_dragged'],
                             priority=_STRIP_PRIORITY)
    if control:
        handler.register_hovered('interactive child', ['left_mouse_drag'], priority=-2)
    handler.feed_down('left_mouse', x=200, y=200)
    handler.process_frame()
    expected = 'interactive child' if control else (_STRIP_ID if pinned else 'workspace')
    assert handler._drag_capture['left_mouse'][0] == expected
