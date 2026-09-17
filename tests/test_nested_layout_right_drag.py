"""Caller-sized nested windows retain right-drag access to their own layouts."""
from types import SimpleNamespace

import pytest
from conftest import begin_frame, end_frame
from test_render_func_integration import _init_melty, _tick_frame
from meltygui.core.core_render import render_func
from meltygui.core.layout import column_core
from meltygui.state.new_core_model import DrawState


@pytest.mark.parametrize('fixed_size', [False, True])
@pytest.mark.parametrize('top_left', [False, True])
def test_nested_window_routes_right_drag_to_local_columns_and_rows(
        gl_context, monkeypatch, fixed_size, top_left):
    runtime = _init_melty()
    states, queued = {}, []
    edges_x = [{'x': x} for x in (0., 180., 360., 540.)]
    edges_y = [{'y': y} for y in (0., 120., 240., 360.)]
    event = None
    original_action = DrawState.on_action

    def action(self, name, *args, **kwargs):
        original_action(self, name, *args, **kwargs)
        expected = 'double_right_mouse_drag' if top_left else 'right_mouse_drag'
        if self.name == 'nested workspace' and name == expected:
            return event
        return None

    def edge_pass(state):
        column_core._ensure_window_state(state)
        queued.extend((state.name, axis, edge, target, cursor)
                      for axis, pending in [('x', state._pending_drags),
                                            ('y', state._pending_row_drags)]
                      for edge, target, cursor in pending)
        state._pending_drags.clear()
        state._pending_row_drags.clear()

    monkeypatch.setattr(DrawState, 'on_action', action)
    monkeypatch.setattr(column_core, 'window_edge_pass', edge_pass)

    @render_func(tint=(.3, .5, .7), use_cache=False, show_header=False,
                 auto_resize=False, closable=True)
    def workspace(input_value: str, draw_state=None):
        states['workspace'] = draw_state
        # Layout registration is window-local even below ordinary view wrappers.
        draw_state._edge_views['columns'] = (draw_state, edges_x)
        draw_state._row_views['rows'] = (draw_state, edges_y)
        draw_state._edge_bands['columns'] = (edges_y[0], edges_y[-1])
        draw_state._row_bands['rows'] = (edges_x[0], edges_x[-1])
        return False, input_value

    @render_func(tint=(.3, .5, .7), use_cache=False, show_header=False)
    def ordinary(input_value: str, draw_state=None):
        size = dict(width=540, height=360) if fixed_size else {}
        workspace(input_value, name='nested workspace', window_pos=(25, 35), **size)
        return False, input_value

    @render_func(tint=(.3, .5, .7), use_cache=False, show_header=False,
                 auto_resize=False, closable=True)
    def root(input_value: str, draw_state=None):
        ordinary(input_value, name='ordinary wrapper')
        return False, input_value

    for frame in range(2):
        _tick_frame(runtime)
        begin_frame()
        try:
            if frame:
                state = states['workspace']
                event = SimpleNamespace(x=state.abs_left + 250 + 20,
                                        y=state.abs_top + 180 + 15,
                                        total_dx=20., total_dy=15.)
            root('', name='native body', window_pos=(10, 30), width=780,
                 height=550, draggable=False, frame_pinned=True)
        finally:
            end_frame()
    index = 1 if top_left else 2
    assert len(queued) == 2
    for name, axis, edge, target, cursor in queued:
        assert name == 'nested workspace'
        expected = edges_x[index] if axis == 'x' else edges_y[index]
        assert edge is expected
        assert target == expected[axis] + (20 if axis == 'x' else 15)
        assert cursor is True
