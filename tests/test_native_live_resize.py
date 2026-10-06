"""Cocoa refresh callbacks must present before its modal resize loop returns."""
import threading
from types import SimpleNamespace

import pytest
import meltygui_imgui as imgui

from meltygui.core.melty import Melty
from meltygui.core.runtime import app
from meltygui.core.windowing import glfw_utils, window_api as glfw, melty_windows
from meltygui.core.windowing.surface import Surface


@pytest.mark.parametrize('cooperative', [False, True])
def test_cooperative_resize_has_no_release_settling_delay(monkeypatch, cooperative):
    from meltygui.core.windowing import titlebar
    window = object()
    monkeypatch.setattr(titlebar, '_last_stamped_size', None)
    monkeypatch.setattr(titlebar, '_on_wayland', lambda: False)
    monkeypatch.setattr(melty_windows, 'defer_refresh', lambda w: cooperative)
    monkeypatch.setattr(titlebar.time, 'monotonic', lambda: 100.)
    monkeypatch.setattr(glfw_utils, 'request_render', lambda: None)
    monkeypatch.setattr(Melty, 'os_resize_time', -1000.)
    titlebar.on_surface_resized(window, 800, 600)
    assert Melty.os_resize_live() is (not cooperative)


def test_native_resize_settling_belongs_to_its_surface(monkeypatch):
    from meltygui.core.windowing.surface import MELTY_ATTRS, MODULE_GLOBALS
    from meltygui.core.windowing import titlebar
    first, second = Surface.__new__(Surface), Surface.__new__(Surface)
    first._melty, first._mods = {'os_resize_time': 100.}, {(titlebar, '_last_stamped_size'): (800, 600)}
    second._melty, second._mods = {}, {}  # Also exercises a live pre-hotswap surface.
    monkeypatch.setattr(Melty, 'os_resize_time', -1000.)
    monkeypatch.setattr(titlebar, '_last_stamped_size', None)
    assert 'os_resize_time' in MELTY_ATTRS
    assert '_last_stamped_size' in MODULE_GLOBALS[titlebar]
    first._restore()
    assert Melty.os_resize_time == 100.
    assert titlebar._last_stamped_size == (800, 600)
    second._restore()
    assert Melty.os_resize_time == -1000.
    assert titlebar._last_stamped_size is None
    first._restore()
    assert Melty.os_resize_time == 100.


@pytest.fixture
def refresh_windows(monkeypatch):
    monkeypatch.setattr(melty_windows, 'defer_refresh', lambda window: False)
    monkeypatch.setattr(app.sys, 'platform', 'darwin')
    monkeypatch.setattr(app, '_state', {'failed': False})
    monkeypatch.setattr(Melty, 'app_tick', 10)
    monkeypatch.setattr(Melty, 'surface_windows', {})
    monkeypatch.setattr(glfw_utils, '_render_thread_id', threading.get_ident())
    monkeypatch.setattr(glfw_utils, '_requested_surfaces', set())
    monkeypatch.setattr(glfw_utils, '_all_surfaces_generation', 0)
    monkeypatch.setattr(glfw_utils, 'render_scope', None)
    monkeypatch.setitem(glfw.__dict__, 'post_empty_event', lambda: None)
    monkeypatch.setitem(glfw.__dict__, 'get_window_attrib', lambda *args: True)
    context = {}
    monkeypatch.setitem(glfw.__dict__, 'get_current_context', lambda: context['gl'])
    monkeypatch.setitem(glfw.__dict__, 'make_context_current', lambda value: context.update(gl=value))
    monkeypatch.setattr(imgui, 'get_current_context', lambda: context['imgui'])
    monkeypatch.setattr(imgui, 'set_current_context', lambda value: context.update(imgui=value))
    callbacks = {}
    for name in ('key', 'char', 'mouse_button', 'scroll', 'cursor_pos', 'cursor_enter',
                 'window_size', 'window_focus', 'framebuffer_size', 'window_close', 'window_refresh'):
        def setter(window, callback, name=name):
            previous = callbacks.get((window, name))
            callbacks[window, name] = callback
            return previous
        monkeypatch.setitem(glfw.__dict__, f'set_{name}_callback', setter)
    windows = []
    for name in ('first', 'second'):
        surface = object.__new__(Surface)
        surface.window, surface.ctx = object(), object()
        surface.closed, surface.frames = False, 3
        surface.drawn_tick = 10
        surface.drawn_generation, surface.drawn_visible = 0, True
        surface._stash = lambda: None
        surface._restore = lambda s=surface: setattr(Melty, 'glfw_window', s.window)
        surface._hook_callbacks()
        windows.append(surface)
    first, second = windows
    monkeypatch.setattr(Surface, 'active', first)
    monkeypatch.setattr(Melty, 'glfw_window', first.window)
    context.update(gl=first.window, imgui=first.ctx)
    presented = []
    def draw():
        second.activate()
        presented.append((Melty.app_tick, Melty.glfw_window, dict(context)))
    second._frame = draw
    yield first, second, context, presented, callbacks[second.window, 'window_refresh']
    glfw_utils._needs_render.clear()


@pytest.mark.parametrize('timeout', [None, 1.0])
def test_refresh_presents_inside_poll_and_wait_then_restores_contexts(refresh_windows, monkeypatch, timeout):
    first, second, context, presented, callback = refresh_windows
    def dispatch(*args):
        callback(second.window)
        assert len(presented) == 1  # before the OS event call has returned
    monkeypatch.setitem(glfw.__dict__, 'poll_events', dispatch)
    monkeypatch.setitem(glfw.__dict__, 'wait_events_timeout', dispatch)
    app._process_events(timeout)
    assert presented == [(11, second.window, {'gl': second.window, 'imgui': second.ctx})]
    assert Surface.active is first and Melty.glfw_window is first.window
    assert context == {'gl': first.window, 'imgui': first.ctx}
    assert glfw_utils.render_scope is None
    assert not app._state['processing_events']


def test_refresh_during_render_queues_another_frame_without_recursing(refresh_windows, monkeypatch):
    first, second, context, presented, callback = refresh_windows
    draw = second._frame
    def drawing():
        draw()
        callback(second.window)
    second._frame = drawing
    monkeypatch.setitem(glfw.__dict__, 'poll_events', lambda: callback(second.window))
    app._process_events()
    assert len(presented) == 1
    assert second in glfw_utils._requested_surfaces
    assert context == {'gl': first.window, 'imgui': first.ctx}


def test_refresh_reconciles_child_lifetimes_before_the_next_tick(refresh_windows, monkeypatch):
    first, second, _, _, callback = refresh_windows
    live, stale, unrelated = [SimpleNamespace(closed=False, stale=False) for _ in range(3)]
    requests = [SimpleNamespace(surface=child, parent_surface=parent, tick=10)
                for child, parent in ((live, second), (stale, second), (unrelated, first))]
    Melty.surface_windows.update(enumerate(requests))
    draw = second._frame
    def drawing():
        draw()
        requests[0].tick = Melty.app_tick
    second._frame = drawing
    monkeypatch.setitem(glfw.__dict__, 'poll_events', lambda: callback(second.window))
    app._process_events()
    assert not live.closed and not unrelated.closed
    assert stale.closed and stale.stale


@pytest.mark.parametrize('blocked', ['between_dispatches', 'linux', 'uninitialized', 'closed', 'worker'])
def test_refresh_defers_when_a_frame_is_not_safe(refresh_windows, monkeypatch, blocked):
    _, second, _, presented, callback = refresh_windows
    app._state['processing_events'] = blocked != 'between_dispatches'
    if blocked == 'linux':
        monkeypatch.setattr(app.sys, 'platform', 'linux')
    elif blocked == 'uninitialized':
        second.frames = 0
    elif blocked == 'closed':
        second.closed = True
    elif blocked == 'worker':
        monkeypatch.setattr(glfw_utils, '_render_thread_id', -1)
    callback(second.window)
    assert not presented
    assert glfw_utils._needs_render.is_set()


def test_refresh_error_reaches_app_loop_instead_of_being_lost_in_ctypes(refresh_windows, monkeypatch):
    first, second, context, _, callback = refresh_windows
    error = RuntimeError('render failed')
    def broken_frame():
        second.activate()
        raise error
    second._frame = broken_frame
    def dispatch():
        callback(second.window)  # callback must not raise into native code
        assert app._state['failed']
    monkeypatch.setitem(glfw.__dict__, 'poll_events', dispatch)
    with pytest.raises(RuntimeError) as caught:
        app._process_events()
    assert caught.value is error
    assert not app._state['processing_events'] and not app._state['refreshing']
    assert context == {'gl': first.window, 'imgui': first.ctx}
    assert Surface.active is first and Melty.glfw_window is first.window


def test_cooperative_resize_queues_refresh_until_event_dispatch_returns(refresh_windows, monkeypatch):
    _, second, _, presented, callback = refresh_windows
    monkeypatch.setattr(melty_windows, 'defer_refresh', lambda window: True)
    def dispatch():
        for _ in range(100):
            callback(second.window)
        assert not presented
    monkeypatch.setitem(glfw.__dict__, 'poll_events', dispatch)
    app._process_events()
    assert second in glfw_utils._requested_surfaces
    assert second.wants_frame()
    second.frame()
    assert len(presented) == 1


@pytest.mark.parametrize('failure', [False, True])
def test_native_presentation_group_always_ends_after_render(refresh_windows, monkeypatch, failure):
    _, second, _, _, _ = refresh_windows
    events = []
    monkeypatch.setattr(melty_windows, 'begin_frame', lambda window: events.append('begin') or 'token')
    monkeypatch.setattr(melty_windows, 'end_frame', lambda token: events.append(('end', token)))
    def render():
        events.append('render')
        if failure:
            raise RuntimeError('draw failed')
        events.append('swap')
    second._frame = render
    if failure:
        with pytest.raises(RuntimeError, match='draw failed'):
            second.frame()
    else:
        second.frame()
    assert events == ['begin', 'render'] + ([] if failure else ['swap']) + [('end', 'token')]
    assert glfw_utils.render_scope is None
