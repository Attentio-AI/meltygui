"""The single UIKit-owned view, exposed through Melty's window vocabulary.

There is no GLFW library, GL context or Python event loop here. The native host
pushes geometry before each frame and owns display pacing. Window movement,
extra OS windows and context operations deliberately remain unsupported.
"""
import math
import time

from meltygui.core.windowing import window_constants as codes


class NativeWindow:
    native_ios = True

    def __init__(self, backend):
        self.backend = backend
        self.width = self.height = 0.0
        self.scale = 1.0
        self.cursor_pos = (0.0, 0.0)
        self.cursor_motion_generation = 0
        self.focused = self.visible = True
        self.hovered = self.closed = self.destroyed = False
        self.keys = {}
        self.buttons = {}
        self.callbacks = {}
        self.touches = {}
        self.primary_touch = None

    def emit(self, event, *args):
        callback = self.callbacks.get(event)
        if callback is not None:
            callback(self, *args)


class Backend:
    name = 'ios'

    def __init__(self, host):
        if not callable(getattr(host, 'request_frame', None)):
            raise TypeError('the iOS host must implement request_frame()')
        self.host = host
        self.window = NativeWindow(self)

    def _check(self, window):
        if window is not self.window or window.destroyed:
            raise ValueError('window does not belong to the active UIKit surface')

    def update_frame(self, info):
        """Width/height are UIKit points; scale converts them to Metal pixels."""
        width, height, scale = (float(info[key]) for key in ('width', 'height', 'scale'))
        if not all(math.isfinite(value) for value in (width, height, scale)) or min(width, height) < 0 or scale <= 0:
            raise ValueError('iOS frame geometry must be finite, nonnegative, and have positive scale')
        window = self.window
        self._check(window)
        old_size, old_scale = self.get_window_size(window), window.scale
        old_fb = self.get_framebuffer_size(window)
        window.width, window.height, window.scale = width, height, scale
        if old_size != self.get_window_size(window):
            window.emit('window_size', width, height)
        if old_scale != scale:
            window.emit('window_content_scale', scale, scale)
        if old_fb != self.get_framebuffer_size(window):
            window.emit('framebuffer_size', *self.get_framebuffer_size(window))

    def set_active(self, active):
        window = self.window
        changed = window.focused != bool(active)
        window.focused = window.visible = bool(active)
        if changed:
            window.emit('window_focus', int(active))

    def get_window_size(self, window):
        self._check(window)
        return window.width, window.height

    def get_framebuffer_size(self, window):
        self._check(window)
        return round(window.width * window.scale), round(window.height * window.scale)

    def get_window_content_scale(self, window):
        self._check(window)
        return window.scale, window.scale

    def get_window_pos(self, window):
        self._check(window)
        return 0, 0  # coordinates are local to the one UIKit view

    def get_window_frame_size(self, window):
        self._check(window)
        return 0, 0, 0, 0

    def get_window_attrib(self, window, attribute):
        self._check(window)
        attributes = {codes.FOCUSED: window.focused, codes.VISIBLE: window.visible,
                      codes.HOVERED: window.hovered, codes.ICONIFIED: False,
                      codes.MAXIMIZED: False, codes.DECORATED: False,
                      codes.RESIZABLE: False, codes.TRANSPARENT_FRAMEBUFFER: False,
                      codes.CLIENT_API: codes.NO_API}
        if attribute not in attributes:
            raise NotImplementedError(f'UIKit window attribute {attribute} is not available')
        return attributes[attribute]

    def get_cursor_pos(self, window):
        self._check(window)
        return window.cursor_pos

    def get_mouse_button(self, window, button):
        self._check(window)
        return window.buttons.get(button, codes.RELEASE)

    def get_key(self, window, key):
        self._check(window)
        return window.keys.get(key, codes.RELEASE)

    def get_input_mode(self, window, mode):
        self._check(window)
        if mode == codes.CURSOR:
            return codes.CURSOR_NORMAL
        raise NotImplementedError(f'UIKit input mode {mode} is not available')

    def window_should_close(self, window):
        self._check(window)
        return window.closed

    def set_window_should_close(self, window, closed):
        self._check(window)
        window.closed = bool(closed)
        self.post_empty_event()

    def _service(self, name, *args):
        service = getattr(self.host, name, None)
        if not callable(service):
            raise NotImplementedError(f'the iOS host does not supply {name}()')
        return service(*args)

    def get_clipboard_string(self, window):
        self._check(window)
        return self._service('get_clipboard_text')

    def set_clipboard_string(self, window, text):
        self._check(window)
        self._service('set_clipboard_text', text.decode('utf-8') if isinstance(text, bytes) else str(text))

    def set_keyboard_visible(self, visible):
        self._service('set_keyboard_visible', bool(visible))

    def post_empty_event(self):
        self.host.request_frame()

    def get_time(self):
        return time.perf_counter()

    def get_platform(self):
        return 'ios'  # toolkit extension; never claim to be GLFW's Cocoa backend

    def terminate(self):
        self.window.keys.clear()
        self.window.buttons.clear()
        self.window.callbacks.clear()
        self.window.touches.clear()
        self.window.destroyed = True

    def create_window(self, *args, **kwargs):
        raise NotImplementedError('UIKit owns the single iOS view; additional OS windows are unsupported')

    def set_window_size(self, *args):
        raise NotImplementedError('UIKit owns the iOS view size')

    def set_window_pos(self, *args):
        raise NotImplementedError('UIKit owns the iOS view position')

    def make_context_current(self, *args):
        raise NotImplementedError('the iOS Metal surface has no OpenGL context')

    def swap_buffers(self, *args):
        raise NotImplementedError('the native Metal host owns presentation')

    def poll_events(self):
        raise NotImplementedError('UIKit delivers input to the frame callback')

    def wait_events(self):
        raise NotImplementedError('the native display link owns frame scheduling')

    def __getattr__(self, name):
        callbacks = {'key', 'char', 'mouse_button', 'cursor_pos', 'cursor_enter',
                     'scroll', 'window_size', 'framebuffer_size', 'window_content_scale',
                     'window_focus', 'window_close', 'window_refresh'}
        if name.startswith('set_') and name.endswith('_callback') and name[4:-9] in callbacks:
            event = name[4:-9]
            def install(window, callback):
                self._check(window)
                previous = window.callbacks.get(event)
                window.callbacks[event] = callback
                return previous
            return install
        raise AttributeError(name)
