"""Caller change notifications belong to the wrapper, not the view signature."""
from conftest import begin_frame, end_frame
from test_render_func_integration import _init_melty, _tick_frame
from meltygui.core.core_render import render_func


def test_changed_reaches_wrapper_without_view_parameter(gl_context):
    runtime = _init_melty()
    notifications = []

    @render_func(tint=(.3, .5, .7), use_cache=False, show_bg=False)
    def view(input_value: list, draw_state=None):
        notifications.append(draw_state._external_change)
        return False, input_value

    value = [1]
    for changed in (False, True, False):
        _tick_frame(runtime)
        begin_frame()
        try:
            view(value, name='caller change notification', changed=changed)
        finally:
            end_frame()
    assert notifications == [False, True, False]


def test_notification_refreshes_cached_body_then_allows_cache_hits(gl_context, monkeypatch):
    """Exercise the wrapper's real cache boundary without offscreen GL capture."""
    runtime = _init_melty()
    monkeypatch.setattr(runtime.cache, 'enabled', True)
    bodies, decisions = [], []

    def cache_gate(draw_state):
        redraw = not bodies or draw_state._external_change
        decisions.append(redraw)
        return redraw

    monkeypatch.setattr(runtime.cache, 'mark_start_offscreen', cache_gate)
    monkeypatch.setattr(runtime.cache, 'mark_end_offscreen', lambda: None)

    @render_func(tint=(.3, .5, .7), use_cache=True, show_bg=False)
    def view(input_value: list, draw_state=None):
        bodies.append(input_value[0])
        return False, input_value

    value = [1]
    for number, changed in ((1, False), (1, False), (2, True), (2, False)):
        value[0] = number
        _tick_frame(runtime)
        begin_frame()
        try:
            view(value, name='cached mutation notification', changed=changed)
        finally:
            end_frame()
    assert decisions == [True, False, True, False]
    assert bodies == [1, 2]
