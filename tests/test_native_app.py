"""UIKit-paced Python lifecycle contracts with real ImGui and no GPU renderer.

The recording renderer/cache verify ownership and scheduling only. These tests
do not exercise UIKit, Metal command submission, or the full Melty draw pass.
"""
import sys
import threading
from types import SimpleNamespace

import meltygui_imgui as imgui
import pytest

from meltygui.core.cache.tile_cache import TileCacheMasked
from meltygui.core.input import input_handler
from meltygui.core.melty import FileWatch, Melty
from meltygui.core.runtime import app, launch_override, paths
from meltygui.core.runtime.native_app import NativeApplication
from meltygui.core.styling import fonts, warm_start
from meltygui.core.windowing import glfw_utils, window_api, window_constants as codes


class Host:
    def __init__(self):
        self.wakes = []
        self.keyboard = []
        self.clipboard = ''
        self.safe_zones = []

    def request_frame(self):
        self.wakes.append(threading.get_ident())

    def set_keyboard_visible(self, visible):
        self.keyboard.append(visible)

    def set_safe_zone(self, top, bottom):
        self.safe_zones.append((top, bottom))

    def get_clipboard_text(self):
        return self.clipboard

    def set_clipboard_text(self, text):
        self.clipboard = text


class RecordingCache:
    def __init__(self, calls):
        self.calls = calls
        self.enabled = True

    def set_enabled(self, value):
        self.calls.append(('cache-enabled', value))
        self.enabled = value

    def cleanup(self):
        self.calls.append('cache-cleanup')


class RecordingRenderer:
    def __init__(self, calls):
        self.calls = calls
        self.cache = RecordingCache(calls)

    def refresh_font_texture(self):
        self.calls.append('font-texture')
        # Build the actual CPU atlas so the headless ImGui frame is valid.
        imgui.get_io().fonts.get_tex_data_as_rgba32()

    def create_tile_cache(self):
        self.calls.append('create-cache')
        return self.cache

    def begin_scene(self, width, height, color):
        self.calls.append(('begin-scene', width, height, color))

    def shutdown(self):
        self.calls.append('renderer-shutdown')


def frame_info(index=0):
    return dict(width=400, height=300, scale=3, now=10 + index / 60,
                presentation_time=10 + (index + 1) / 60)


@pytest.fixture
def app_environment(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(app, '_state', dict(
        booted=False, ran=False, app_id=None, app_name=None, cache=None,
        failed=False, imports=None, import_error=None, switch_interval=None))
    monkeypatch.setattr(app, '_ROOTS', [])
    monkeypatch.setattr(app, '_AFTER_FIRST_FRAME', [])
    monkeypatch.setattr(app, '_MARKS', [])
    monkeypatch.setattr(app, '_utf8_output', lambda: None)
    monkeypatch.setattr(app, '_register_editable', lambda path: None)
    monkeypatch.setattr(app, '_unhook_main_return', lambda: None)
    monkeypatch.setattr(paths, 'cache_root', lambda app_id: tmp_path / app_id)
    monkeypatch.setattr(launch_override, 'install', lambda app_id: calls.append(('overrides', app_id)))
    return calls


@pytest.fixture
def native(monkeypatch, app_environment):
    calls = app_environment
    previous_context = imgui.get_current_context()
    host = Host()
    renderer = RecordingRenderer(calls)
    monkeypatch.setitem(sys.modules, '_melty_ios', host)
    monkeypatch.setattr(window_api, '_state', dict(backend=None, selected=False))
    monkeypatch.setattr(glfw_utils, '_render_thread_id', threading.get_ident())
    monkeypatch.setattr(glfw_utils, '_needs_render', threading.Event())
    monkeypatch.setattr(glfw_utils, 'render_scope', None)
    monkeypatch.setattr(glfw_utils, '_open_surfaces', set())
    monkeypatch.setattr(glfw_utils, '_requested_surfaces', set())
    monkeypatch.setattr(glfw_utils, '_all_surfaces_generation', 0)
    monkeypatch.setattr(glfw_utils, 'frames_left', 0)
    monkeypatch.setattr(TileCacheMasked, 'window_caches', {})
    monkeypatch.setattr(input_handler, '_BUTTON_PROBE', {'fn': None})
    # Production owns these globals for an app's lifetime; isolate each test.
    reset = dict(glfw_window=None, native_surface=None, graphics_backend=None, is_touch=False,
                 font_mgr=None, style_manager=None, global_attrs={}, vis=None,
                 draw_state_registry={}, registered_windows={}, event_handler=None,
                 backend=None, cache=None, annotation_mode=False, app_tick=0,
                 framebuffer_size=(0, 0), frame_inset=0, frame_origin=(0, 0),
                 frame_key_events=[], frame_text_events=[], _keys_down=set(),
                 text_focused_ds=None, _render_tasks=[], _pointer_inside=False)
    for name, value in reset.items():
        monkeypatch.setattr(Melty, name, value)
    for name in ('_last_input_time', '_last_presence_time', '_last_key_time'):
        monkeypatch.setattr(Melty, name, 0)

    class DefaultFonts:
        def __init__(self, io, scale, *, pixel_scale=1.0):
            self.io = io

        def prewarm(self):
            calls.append('fonts-prewarmed')
            self.io.fonts.add_font_default()

    session = SimpleNamespace(draw_state_registry={}, registered_windows={})
    root_config = dict(name='Editor', settings=object(), view_kwargs={})
    monkeypatch.setattr(fonts, 'FontManager', DefaultFonts)
    monkeypatch.setattr(Melty, 'resolve_ui_scale', lambda: 1.0)
    monkeypatch.setattr(Melty, 'apply_ui_scale', lambda renderer: calls.append('ui-scale'))
    monkeypatch.setattr(app, '_load_session', lambda: session)
    monkeypatch.setattr(app, '_register_projects', lambda: calls.append('projects'))
    monkeypatch.setattr(app, '_ROOTS', [(lambda: None, root_config)])
    monkeypatch.setattr(app, '_root_body', lambda fn, name, config: lambda surface: None)
    monkeypatch.setattr(app, '_searchable_body', lambda body, config: body)
    monkeypatch.setattr(FileWatch, 'stop', lambda: calls.append('watcher-stop'))
    monkeypatch.setattr(app, '_flush_pending_saves', lambda: calls.append('save-source'))
    monkeypatch.setattr(app, '_save_session', lambda: calls.append('save-session') or True)
    monkeypatch.setattr(app, '_save_settings', lambda: calls.append('save-settings') or True)
    monkeypatch.setattr(launch_override, 'flush', lambda: calls.append('save-overrides') or True)
    surface = NativeApplication({'app_id': 'native-test'}, host, lambda: renderer)
    snapshots = []

    def draw():
        calls.append('draw')
        snapshots.append((list(Melty.frame_text_events), list(Melty.frame_key_events)))
        imgui.new_frame()
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(300, 200)
        imgui.begin('Native lifecycle test')
        imgui.text('Headless CPU draw data')
        imgui.end()
        imgui.render()

    monkeypatch.setattr(surface, '_draw_frame', draw)
    yield SimpleNamespace(surface=surface, host=host, renderer=renderer, calls=calls,
                          session=session, root_config=root_config, snapshots=snapshots)
    # Tear down only the real resources this harness allocated. Tests below
    # exercise production close explicitly, including its exceptional paths.
    if surface.ctx is not None:
        imgui.set_current_context(surface.ctx)
        if surface.input is not None:
            surface.input.close()
        imgui.destroy_context(surface.ctx)
    imgui.set_current_context(previous_context)


def test_install_boot_and_run_delegate_once_without_desktop_loop(app_environment, monkeypatch):
    calls = app_environment
    host = SimpleNamespace(boot=lambda: calls.append('boot'), start=lambda: calls.append('start'))

    def desktop_only(*args):
        raise AssertionError('A native app entered desktop setup')

    monkeypatch.setattr(window_api, 'select_backend', desktop_only)
    monkeypatch.setattr(app, '_wait_imports', desktop_only)
    monkeypatch.setattr(app, '_init_melty', desktop_only)
    monkeypatch.setattr(app, '_hook_main_return', desktop_only)
    app.install_native_host(host)
    app.install_native_host(host)
    app.boot('ios-editor')
    app.boot('ios-editor')

    @app.glfw_window(name='Editor', app_id='ios-editor')
    def editor():
        pass

    app.run()
    app.run()
    assert calls == [('overrides', 'ios-editor'), 'boot', 'start']
    assert app._ROOTS[0][0] is editor
    assert app._state['booted'] and app._state['ran']
    assert not app._state.get('hooked')


def test_run_boots_a_preinstalled_host(app_environment, monkeypatch):
    monkeypatch.setattr(app, '_default_app_id', lambda: 'native-default')
    app.install_native_host(SimpleNamespace(
        boot=lambda: app_environment.append('boot'), start=lambda: app_environment.append('start')))
    app.run()
    assert app_environment == [('overrides', 'native-default'), 'boot', 'start']


def test_install_rejects_replacement_or_late_hosts(app_environment):
    first = object()
    app.install_native_host(first)
    with pytest.raises(RuntimeError, match='already owns'):
        app.install_native_host(object())
    for phase in ('booted', 'ran'):
        app._state[phase] = True
        with pytest.raises(RuntimeError, match='before meltygui.boot'):
            app.install_native_host(first)
        app._state[phase] = False


def test_native_start_owns_real_context_and_shared_runtime(native):
    surface = native.surface
    surface.boot()
    context = surface.ctx
    surface.boot()
    assert surface.ctx is context
    assert imgui.get_current_context() is context
    assert not imgui.get_io().ini_file_name  # the binding returns '' for a null C string
    assert window_api.backend_name() == 'ios'
    assert Melty.graphics_backend is native.renderer
    assert Melty.native_surface is surface
    assert Melty.is_touch
    surface.start()
    surface.start()
    assert surface.started
    assert surface.settings is native.root_config['settings']
    assert Melty.draw_state_registry is native.session.draw_state_registry
    assert Melty.registered_windows is native.session.registered_windows
    assert Melty.backend is surface.input
    assert Melty.event_handler is surface.input.handler
    assert Melty.cache is native.renderer.cache
    assert not surface.cache.enabled
    assert surface in glfw_utils._open_surfaces
    assert TileCacheMasked.window_caches[surface.cache] == surface.request_frame
    assert native.calls.count('fonts-prewarmed') == native.calls.count('create-cache') == 1
    assert len(native.host.wakes) == 1


@pytest.mark.parametrize('count', [0, 2])
def test_start_requires_one_root_before_allocating_resources(native, monkeypatch, count):
    monkeypatch.setattr(app, '_ROOTS', [(lambda: None, {})] * count)
    with pytest.raises(RuntimeError, match='exactly one'):
        native.surface.start()
    assert native.surface.ctx is None
    assert not native.surface.started
    assert not window_api._state['selected']


def test_frame_requires_start(native):
    with pytest.raises(RuntimeError, match='has not started'):
        native.surface.frame(frame_info(), [])


def test_frame_applies_worker_results_before_input_and_draw(native, monkeypatch):
    surface = native.surface
    surface.start()
    order = []
    worker = threading.Thread(target=lambda: Melty.post_to_render(
        lambda: order.append(('worker-result', threading.get_ident()))))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive() and not order
    process_inputs = surface.input.process_inputs
    draw = surface._draw_frame

    def inputs(info, events):
        order.append(('input', threading.get_ident()))
        process_inputs(info, events)

    def drawing():
        order.append(('draw', threading.get_ident()))
        assert glfw_utils.render_scope is surface
        draw()

    monkeypatch.setattr(surface.input, 'process_inputs', inputs)
    monkeypatch.setattr(surface, '_draw_frame', drawing)
    previous_scope = object()
    monkeypatch.setattr(glfw_utils, 'render_scope', previous_scope)
    assert surface.frame(frame_info(), []) is True
    assert order == [(name, threading.get_ident()) for name in ('worker-result', 'input', 'draw')]
    assert not Melty._render_tasks
    assert glfw_utils.render_scope is previous_scope
    assert Melty.framebuffer_size == (1200, 900)
    assert surface.content_size() == (1200, 900)
    assert ('begin-scene', 1200, 900, (0.0, 0.0, 0.0, 1.0)) in native.calls
    assert imgui.get_draw_data().total_vtx_count > 0


def test_safe_zone_updates_native_layout_only_when_setting_changes(native, monkeypatch):
    from meltygui.core.runtime.toggles import Toggles

    monkeypatch.setattr(Toggles.Mobile, 'Safezone', 64)
    monkeypatch.setattr(Toggles.Mobile, 'bottom_safezone', 24)
    native.surface.start()
    assert native.host.safe_zones == [(64.0, 24.0)]
    for index in range(3):
        native.surface.frame(frame_info(index), [])
    assert native.host.safe_zones == [(64.0, 24.0)]
    Toggles.Mobile.Safezone = 92
    native.surface.frame(frame_info(3), [])
    assert native.host.safe_zones == [(64.0, 24.0), (92.0, 24.0)]
    Toggles.Mobile.Safezone = -10
    native.surface.frame(frame_info(4), [])
    assert native.host.safe_zones == [(64.0, 24.0), (92.0, 24.0), (0.0, 24.0)]
    Toggles.Mobile.bottom_safezone = 16
    native.surface.frame(frame_info(5), [])
    assert native.host.safe_zones[-1] == (0.0, 16.0)
    Toggles.Mobile.bottom_safezone = -5
    native.surface.frame(frame_info(6), [])
    assert native.host.safe_zones[-1] == (0.0, 0.0)
    native.surface.close()
    assert not Melty.is_touch


@pytest.mark.parametrize("setting", ["Safezone", "bottom_safezone"])
def test_safe_zone_rejects_nonfinite_layout(native, monkeypatch, setting):
    from meltygui.core.runtime.toggles import Toggles

    native.surface.start()
    previous = list(native.host.safe_zones)
    monkeypatch.setattr(Toggles.Mobile, setting, float('nan'))
    with pytest.raises(ValueError, match='finite'):
        native.surface.frame(frame_info(), [])
    assert native.host.safe_zones == previous


def test_keyboard_shrink_requests_one_focused_caret_reveal_before_draw(native, monkeypatch):
    surface = native.surface
    surface.start()
    invalidations, reveals = [], []
    preserved = object()
    focused = SimpleNamespace(_kwargs={}, misc={'existing-state': preserved}, invalidate=lambda: invalidations.append(
        surface.window_backend.get_window_size(surface.window)))
    monkeypatch.setattr(Melty, 'text_focused_ds', focused)
    draw = surface._draw_frame

    def consume_reveal():
        # Stand in for the text view consuming its one-shot request; real
        # caret scrolling is exercised separately by the text-view tests.
        reveals.append(focused.misc.pop('reveal_text_cursor', False))
        draw()

    monkeypatch.setattr(surface, '_draw_frame', consume_reveal)
    sizes = [(400, 300), (400, 300), (400, 180), (400, 180), (400, 300), (400, 180)]
    for index, (width, height) in enumerate(sizes):
        surface.frame(frame_info(index) | dict(width=width, height=height), [])

    # First layout, steady frames and keyboard dismissal must not continually
    # drag a manually panned editor back to its stationary caret.
    assert reveals == [False, False, True, False, False, True]
    assert invalidations == [(400, 180), (400, 180)]
    assert focused.misc == {'existing-state': preserved}
    assert native.host.keyboard == [True]


def test_rotation_reveals_focused_caret_when_either_axis_shrinks(native, monkeypatch):
    surface = native.surface
    surface.start()
    invalidations = []
    focused = SimpleNamespace(_kwargs={}, misc={}, invalidate=lambda: invalidations.append(True))
    monkeypatch.setattr(Melty, 'text_focused_ds', focused)

    surface.frame(frame_info() | dict(width=300, height=600), [])
    assert focused.misc == {}  # Startup has no previous positive viewport.
    for index, (width, height) in enumerate(((600, 300), (300, 600)), 1):
        surface.frame(frame_info(index) | dict(width=width, height=height), [])
        assert focused.misc.pop('reveal_text_cursor') is True
    assert len(invalidations) == 2


def test_viewport_shrink_without_focus_leaves_other_text_state_untouched(native, monkeypatch):
    surface = native.surface
    surface.start()
    unfocused = SimpleNamespace(misc={}, invalidate=lambda: pytest.fail('Unfocused text was invalidated'))
    monkeypatch.setattr(Melty, 'draw_state_registry', {'unfocused': unfocused})
    for index, (width, height) in enumerate(((400, 300), (400, 180), (300, 400))):
        surface.frame(frame_info(index) | dict(width=width, height=height), [])

    assert Melty.text_focused_ds is None
    assert unfocused.misc == {}
    assert native.host.keyboard == [False]
    assert not app._state['failed']


@pytest.mark.parametrize('zero_size', [(0, 300), (400, 0)])
def test_zero_size_layout_and_recovery_do_not_request_caret_reveal(native, monkeypatch, zero_size):
    surface = native.surface
    surface.start()
    invalidations = []
    focused = SimpleNamespace(_kwargs={}, misc={}, invalidate=lambda: invalidations.append(True))
    monkeypatch.setattr(Melty, 'text_focused_ds', focused)
    surface.frame(frame_info(), [])
    width, height = zero_size
    assert surface.frame(frame_info(1) | dict(width=width, height=height), []) is False
    assert surface.frames == 1  # A zero-size layout does not draw.
    surface.frame(frame_info(2) | dict(height=150), [])
    assert focused.misc == {}
    assert invalidations == []

    surface.frame(frame_info(3) | dict(height=100), [])
    assert focused.misc == {'reveal_text_cursor': True}
    assert invalidations == [True]
    assert not app._state['failed']


def test_warmup_enables_cache_and_idle_frames_stop(native):
    surface = native.surface
    surface.start()
    assert surface.frame(frame_info(0), []) is True
    assert not surface.cache.enabled
    assert surface.frame(frame_info(1), []) is True
    assert surface.cache.enabled
    assert surface.frame(frame_info(2), []) is False
    assert native.calls.count(('cache-enabled', True)) == 1
    assert surface.frames == 3


def test_queued_input_keeps_frames_until_ordered_edits_are_consumed(native):
    surface = native.surface
    surface.start()
    for index in range(3):
        surface.frame(frame_info(index), [])
    native.snapshots.clear()
    events = [dict(kind='text', text='abc'), dict(kind='backspace'), dict(kind='text', text='d')]
    assert surface.frame(frame_info(3), events) is True
    assert surface.input.has_pending_events
    assert surface.frame(frame_info(4), []) is True
    assert surface.frame(frame_info(5), []) is False
    assert not surface.input.has_pending_events
    assert native.snapshots == [(['abc'], []), ([], [(codes.KEY_BACKSPACE, 0)]), (['d'], [])]
    assert surface.frame(frame_info(6), []) is False
    assert native.snapshots[-1] == ([], [])


def test_request_during_draw_is_not_lost(native, monkeypatch):
    surface = native.surface
    surface.start()
    for index in range(3):
        surface.frame(frame_info(index), [])
    draw = surface._draw_frame

    def request_from_draw():
        draw()
        glfw_utils.request_render()

    monkeypatch.setattr(surface, '_draw_frame', request_from_draw)
    assert surface.frame(frame_info(3), []) is True
    monkeypatch.setattr(surface, '_draw_frame', draw)
    assert surface.frame(frame_info(4), []) is False


def test_suspend_cancels_input_checkpoints_once_and_resume_wakes(native):
    surface = native.surface
    surface.start()
    surface.frame(frame_info(), [dict(kind='touch_begin', touch_id=1, x=10, y=10),
                                dict(kind='key', key=codes.KEY_S, action=codes.PRESS,
                                     modifiers=codes.MOD_SUPER)])
    # This harness replaces drawing/dispatch. Native contact edges are queued
    # until the shared dispatcher can choose between a tap and a scroll.
    assert surface.input.handler._touch_input.events
    assert Melty._keys_down
    surface.input.update_keyboard(True)
    native.calls.clear()
    surface.suspend()
    surface.suspend()
    assert surface.suspended and not surface.window.focused
    assert native.host.keyboard[-1] is False
    assert not surface.input.handler.is_down('left_mouse')
    assert not surface.input.handler._touch_input.events
    assert not surface.input.handler._touch_input.active
    assert not surface.window.touches and not Melty._keys_down
    assert native.calls == ['save-source', 'save-session', 'save-settings', 'save-overrides']
    assert surface.frame(frame_info(1), []) is False
    assert surface.frames == 1
    wakes = len(native.host.wakes)
    surface.resume()
    assert not surface.suspended and surface.window.focused
    assert len(native.host.wakes) > wakes
    assert surface.frame(frame_info(120), []) is True
    assert imgui.get_io().delta_time < 0.1


def test_suspend_reports_checkpoint_failure(native, monkeypatch):
    native.surface.start()
    monkeypatch.setattr(app, '_save_session', lambda: False)
    with pytest.raises(RuntimeError, match='save.*suspension'):
        native.surface.suspend()
    assert native.surface.suspended
    assert not native.surface.window.focused


def test_close_saves_before_releasing_resources_and_is_idempotent(native):
    surface = native.surface
    surface.start()
    window = surface.window
    native.calls.clear()
    surface.close()
    assert native.calls == ['watcher-stop', 'save-source', 'save-session', 'save-settings',
                            'save-overrides', 'cache-cleanup', 'renderer-shutdown']
    assert surface.closed and surface.ctx is None
    assert surface not in glfw_utils._open_surfaces
    assert surface.cache not in TileCacheMasked.window_caches
    assert Melty.native_surface is Melty.graphics_backend is Melty.glfw_window is None
    assert window.destroyed
    assert not window_api._state['selected']
    calls, wakes = list(native.calls), len(native.host.wakes)
    surface.close()
    surface.resume()
    assert surface.frame(frame_info(), []) is False
    assert native.calls == calls and len(native.host.wakes) == wakes


def test_failed_frame_does_not_save_broken_state_and_close_still_releases(native, monkeypatch):
    surface = native.surface
    surface.start()

    def broken_draw():
        raise ValueError('view failed')

    monkeypatch.setattr(surface, '_draw_frame', broken_draw)
    previous_scope = object()
    monkeypatch.setattr(glfw_utils, 'render_scope', previous_scope)
    with pytest.raises(ValueError, match='view failed'):
        surface.frame(frame_info(), [])
    assert app._state['failed']
    assert glfw_utils.render_scope is previous_scope
    assert surface.frames == 0
    native.calls.clear()
    with pytest.raises(RuntimeError, match='save.*closing'):
        surface.close()
    assert native.calls == ['watcher-stop', 'cache-cleanup', 'renderer-shutdown']
    assert surface.closed and surface.ctx is None
    assert not window_api._state['selected']


def test_close_before_boot_has_no_resources_or_checkpoint(native):
    native.surface.close()
    assert native.surface.closed
    assert not native.calls


def test_failed_renderer_construction_can_release_partial_boot(native, monkeypatch):
    def unavailable_renderer():
        raise RuntimeError('Metal device unavailable')

    monkeypatch.setattr(native.surface, '_renderer_factory', unavailable_renderer)
    with pytest.raises(RuntimeError, match='Metal device unavailable'):
        native.surface.boot()
    assert native.surface.ctx is not None
    assert window_api._state['selected']
    window = native.surface.window
    native.surface.close()
    assert native.surface.ctx is None and native.surface.closed
    assert window.destroyed
    assert not window_api._state['selected']
    assert not native.calls


def test_close_attempts_all_cleanup_after_independent_failures(native, monkeypatch):
    surface = native.surface
    surface.start()
    window = surface.window

    def fail(name):
        def release():
            native.calls.append(name)
            raise RuntimeError(name)
        return release

    monkeypatch.setattr(surface.input, 'close', fail('input-close-failed'))
    monkeypatch.setattr(surface.cache, 'cleanup', fail('cache-cleanup-failed'))
    monkeypatch.setattr(surface.renderer, 'shutdown', fail('renderer-shutdown-failed'))
    with pytest.raises(ExceptionGroup) as errors:
        surface.close()
    assert [str(error) for error in errors.value.exceptions] == [
        'input-close-failed', 'cache-cleanup-failed', 'renderer-shutdown-failed']
    assert surface.closed and surface.ctx is None and window.destroyed
    assert surface not in glfw_utils._open_surfaces
    assert surface.cache not in TileCacheMasked.window_caches
    assert Melty.native_surface is None
    assert not window_api._state['selected']


def test_lifecycle_methods_require_owner_thread_but_wakes_do_not(native):
    surface = native.surface
    errors = []

    def from_worker():
        for action in (surface.boot, surface.start, lambda: surface.frame(frame_info(), []),
                       surface.presented, surface.suspend, surface.resume, surface.close):
            try:
                action()
            except RuntimeError as error:
                errors.append(str(error))
        surface.request_frame()

    worker = threading.Thread(target=from_worker)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 7 and all('render thread' in error for error in errors)
    assert native.host.wakes == [worker.ident]
    assert not surface.started and surface.ctx is None and not surface.closed


def test_first_presented_callback_waits_for_host_submission_and_runs_once(native, monkeypatch):
    monkeypatch.setattr(app, '_write_startup_log', lambda *args: None)
    monkeypatch.setattr(warm_start, 'remember_code_stack', lambda *args: None)
    submitted = []
    app.after_first_frame(lambda: submitted.append('presented'))
    native.surface.start()
    native.surface.frame(frame_info(), [])
    assert submitted == []
    native.surface.presented()
    native.surface.presented()
    assert submitted == ['presented'] and app._state['presented']
    app.after_first_frame(lambda: submitted.append('late callback'))
    assert submitted == ['presented', 'late callback']
