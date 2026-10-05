"""Melty's shared application lifecycle driven by a UIKit display callback.

UIKit owns the view, input delivery, suspension and presentation. This owner
runs the same view, conversion, persistence and tile-cache lifecycles as a
desktop surface without creating an OpenGL context or entering an event loop.
"""
from __future__ import annotations

import threading
import time
import math
from types import SimpleNamespace


class NativeApplication:
    def __init__(self, config, host, renderer_factory):
        self.config = dict(config)
        self.host = host
        self._renderer_factory = renderer_factory
        self.renderer = None
        self.window = self.ctx = self.input = self.cache = None
        self.window_backend = None
        self.root_windows = {}
        self.closed = self.started = self.suspended = False
        self.frames = 0
        self.name = self.title = config.get('app_id', config.get('entry_module', 'Melty'))
        self.settings = None
        self.tint = None
        self._thread = threading.get_ident()
        self._safe_zone = None

    def _check_thread(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError('The native application belongs to its render thread')

    def boot(self):
        """Minimal setup before importing an app's decorated views and models."""
        self._check_thread()
        if self.ctx is not None:
            return
        import meltygui_imgui as imgui
        from meltygui.core.windowing import window_api, glfw_utils
        from meltygui.core.melty import Melty

        from meltygui.core.runtime import app
        if app._state.get('app_id') is not None:
            self.config['app_id'] = app._state['app_id']
        self.window_backend = window_api.select_ios_backend(self.host)
        self.window = self.window_backend.window
        self.ctx = imgui.create_context()
        imgui.set_current_context(self.ctx)
        io = imgui.get_io()
        io.ini_file_name = None
        io.display_size = (1.0, 1.0)
        self.renderer = self._renderer_factory()
        glfw_utils._render_thread_id = self._thread
        Melty.glfw_window = self.window
        Melty.graphics_backend = self.renderer
        Melty.native_surface = self
        Melty.is_touch = True

    def start(self):
        """Complete setup after the app has registered its root and model types."""
        self._check_thread()
        if self.started:
            return
        from meltygui.core.runtime import app
        if len(app._ROOTS) != 1:
            raise RuntimeError('An iOS app must register exactly one root window')
        self.boot()
        import meltygui_imgui as imgui
        from meltygui.core.melty import Melty
        from meltygui.core.input.input_handler import InputHandler
        from meltygui.core.input.ios_input import IOSInput
        from meltygui.core.cache.tile_cache import TileCacheMasked
        from meltygui.core.windowing import glfw_utils

        io = imgui.get_io()
        app._initialize_fonts(io)
        self.renderer.refresh_font_texture()
        session = app._initialize_session()
        Melty.vis = SimpleNamespace(root=session, window=self.window, tracked_keys=[],
                                    first_frame_keys=set(), fa_font=None)
        Melty.event_handler = InputHandler()
        self.input = IOSInput(Melty.event_handler, self.window_backend, io, Melty)
        Melty.backend = self.input
        self.cache = Melty.cache = self.renderer.create_tile_cache()
        TileCacheMasked.window_caches[self.cache] = self.request_frame
        # Match desktop warm-up: collect two frames of geometry, then cache.
        self.cache.enabled = False
        fn, config = app._ROOTS[0]
        self.name = self.title = config['name']
        self.root_config = config
        self.settings = config.get('settings')
        self.body = app._searchable_body(app._root_body(fn, self.name, config=config), config)
        glfw_utils.register_surface(self)
        self.started = True
        self._sync_mobile_settings()
        app.mark('native Melty runtime configured')
        self.request_frame()

    def _sync_mobile_settings(self):
        from meltygui.core.melty import Melty
        from meltygui.core.runtime.toggles import Toggles

        inset = float(Toggles.Mobile.Safezone) if Melty.is_touch else 0.0
        if not math.isfinite(inset):
            raise ValueError('Toggles.Mobile.Safezone must be a finite number of pixels')
        inset = max(0.0, inset)
        if inset != self._safe_zone:
            self.host.set_safe_zone(inset)
            self._safe_zone = inset

    def request_frame(self):
        self.host.request_frame()

    def content_size(self):
        return self.window_backend.get_framebuffer_size(self.window)

    def frame(self, info, events):
        self._check_thread()
        if self.closed or self.suspended:
            return False
        if not self.started:
            raise RuntimeError('The native application has not started')
        import meltygui_imgui as imgui
        from meltygui.core.runtime import app
        from meltygui.core.melty import Melty
        from meltygui.core.windowing import glfw_utils

        imgui.set_current_context(self.ctx)
        previous_scope, glfw_utils.render_scope = glfw_utils.render_scope, self
        glfw_utils._needs_render.clear()
        _, generation = glfw_utils.take_surface_request(self, -1)
        Melty.app_tick += 1
        previous_frame = Melty._frame_draw_start
        Melty._frame_draw_start = time.monotonic()
        try:
            # Workers publish changes before any view reads this frame's state.
            Melty._drain_render_tasks()
            self._sync_mobile_settings()
            previous_size = self.window_backend.get_window_size(self.window)
            self.input.process_inputs(info, events)
            size = self.window_backend.get_window_size(self.window)
            if (min(*previous_size, *size) > 0
                    and any(new < old for new, old in zip(size, previous_size))):
                # UIKit shrinks the surface for the keyboard or rotation.
                # Reveal the stationary caret once using the text view's
                # normal scroll owner; subsequent manual pans stay untouched.
                focused = Melty.text_focused_ds
                if focused is not None:
                    focused.misc['reveal_text_cursor'] = True
                    focused.invalidate()
            fb_size = self.window_backend.get_framebuffer_size(self.window)
            if min(fb_size) <= 0:
                return False
            Melty.framebuffer_size = fb_size
            Melty.frame_inset, Melty.frame_origin = 0, (0, 0)
            self.renderer.begin_scene(*fb_size, (0.0, 0.0, 0.0, 1.0))
            Melty.apply_ui_scale(self.renderer)
            self._draw_frame()
            self.input.update_keyboard()
            self.frames += 1
            if self.frames == 2:
                self.cache.set_enabled(True)
            requested, _ = glfw_utils.take_surface_request(self, generation)
            return (self.frames < 3 or requested or glfw_utils._needs_render.is_set()
                    or self.input.has_pending_events or glfw_utils.frames_left > 0)
        except Exception:
            app._state['failed'] = True
            raise
        finally:
            Melty._frame_draw_start = previous_frame
            glfw_utils.render_scope = previous_scope

    def _draw_frame(self):
        from meltygui.core.melty import Melty
        from meltygui.core.windowing.surface_frame import draw_surface_frame
        self.tint = (self.root_config.get('view_kwargs') or {}).get('tint')
        draw_surface_frame(self)
        Melty.post_frame(self.renderer, self.window)

    def presented(self):
        """The native host has successfully submitted the first drawable."""
        self._check_thread()
        from meltygui.core.runtime import app
        if not app._state.get('presented'):
            app._first_frame_presented([self])

    def suspend(self):
        self._check_thread()
        if self.closed or self.suspended:
            return
        from meltygui.core.runtime import app
        self.suspended = True
        if self.input is not None:
            self.input.suspend()
        if not app.checkpoint():
            raise RuntimeError('The application could not save its state before suspension')

    def resume(self):
        self._check_thread()
        if self.closed:
            return
        self.suspended = False
        if self.input is not None:
            self.input.resume()
        self.request_frame()

    def close(self):
        self._check_thread()
        if self.closed:
            return
        if self.window_backend is None and self.ctx is None:
            self.closed = True
            return
        from meltygui.core.runtime import app
        from meltygui.core.melty import Melty, FileWatch
        from meltygui.core.windowing import glfw_utils, window_api
        from meltygui.core.cache.tile_cache import TileCacheMasked
        import meltygui_imgui as imgui
        errors = []

        def release(action):
            try:
                action()
            except Exception as error:
                errors.append(error)

        def save():
            if not app.checkpoint():
                raise RuntimeError('The application could not save its state before closing')

        if self.started:
            release(FileWatch.stop)
            release(save)
        self.closed = True
        if self.input is not None:
            release(self.input.close)
        if self.cache is not None:
            TileCacheMasked.window_caches.pop(self.cache, None)
            release(self.cache.cleanup)
        if self.renderer is not None:
            release(self.renderer.shutdown)
        glfw_utils.forget_surface(self)
        if self.ctx is not None:
            release(lambda: imgui.destroy_context(self.ctx))
            self.ctx = None
        Melty.native_surface = Melty.graphics_backend = Melty.glfw_window = None
        Melty.is_touch = False
        if self.window_backend is not None:
            release(window_api.terminate)
            self.window_backend = None
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup('The native application could not close cleanly', errors)


def start_native_application(config, host, renderer_factory=None):
    """Launch an ordinary app module after installing its native host."""
    import importlib
    from meltygui.core.runtime import app
    if renderer_factory is None:
        import _melty_metal
        from meltygui.core.graphics.metal_renderer import MetalRenderer
        renderer_factory = lambda: MetalRenderer(_melty_metal)
    entry = config['entry_module']
    if not isinstance(entry, str) or not all(part.isidentifier() for part in entry.split('.')):
        raise ValueError('entry_module must be a Python module name')
    application = NativeApplication(config, host, renderer_factory)
    app.install_native_host(application)
    importlib.import_module(entry)
    app.run()
    return application
