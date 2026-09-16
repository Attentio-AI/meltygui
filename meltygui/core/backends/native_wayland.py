"""Native Wayland/EGL window backend for meltygui's Surface share group.

GLFW-compatible constants/callbacks let existing input/chrome code share this
backend without loading GLFW. All protocol dispatch and graphics calls run on
the render thread.
"""
import ctypes
import errno
import mmap
import os
import select
import socket
import threading
import time
from types import SimpleNamespace

import meltygui.core.window_constants as codes

from meltygui.core.backends.wayland_protocol import Native
from meltygui.core.backends.wayland_protocol import function
from meltygui.core.backends.wayland_protocol import P
from meltygui.core.backends.wayland_protocol import U
from meltygui.core.backends.wayland_protocol import I


class NativeWindow:
    native_wayland = True
    cursor_motion_generation = 0

    def __init__(self, backend, width, height, title, hints):
        self.backend = backend
        self.width, self.height, self.title = width, height, title
        self.hints = hints.copy()
        self.surface = self.xdg_surface = self.toplevel = None
        self.decoration = None
        self.decoration_mode = 0
        self.egl_window = self.egl_surface = self.context = None
        self.configured = self.closed = self.destroyed = False
        self.visible = bool(hints.get(codes.VISIBLE, True))
        self.focused = self.hovered = self.maximized = self.iconified = False
        self.scale = 1
        self.outputs = set()
        self.cursor_pos = (0., 0.)
        self.keys, self.buttons, self.callbacks = {}, {}, {}
        self.saved_size = (width, height)

    @property
    def _as_parameter_(self):
        # Existing GLState keys use ctypes.cast(window, c_void_p).
        return ctypes.c_void_p(self.context)

    def emit(self, event, *args):
        callback = self.callbacks.get(event)
        if callback is not None:
            callback(self, *args)


class Backend:
    def __init__(self):
        self.native = None
        self.decoration_manager = None
        self.display = self.egl_display = None
        self.windows = []
        self.by_surface = {}
        self.globals, self.outputs = {}, {}
        self.hints, self.string_hints = {}, {}
        self.current = threading.local()
        self.error = None
        self.pointer_window = self.keyboard_window = None
        self.pointer = self.keyboard = None
        self.serial = self.enter_serial = 0
        self.modifiers = 0
        self.repeat_key = None
        self.repeat_rate, self.repeat_delay = 25, 0.5
        self.next_repeat = 0.
        self.xkb_context = self.xkb_keymap = self.xkb_state = None
        self.clipboard_text = None
        self.selection = self.data_source = None
        self.offers = {}
        self.cursor_theme = self.cursor_surface = None
        self.custom_cursor_buffers = []
        self.cursor_scale = 1
        self.wake_reader, self.wake_writer = socket.socketpair()
        self.wake_reader.setblocking(False)
        self.wake_writer.setblocking(False)

    def init(self):
        if self.display:
            return True
        self.native = Native()
        native = self.native
        inherited = os.environ.pop('WAYLAND_SOCKET', None)
        self.display = native.connect_fd(int(inherited)) if inherited else native.connect(None)
        if not self.display:
            raise RuntimeError('Native Wayland: cannot connect to compositor')
        self.registry = native.request(self.display, 1, P(), interface='wl_registry', version=1)
        self.listen(self.registry, [([U, ctypes.c_char_p, U], self.global_added), ([U], self.global_removed)])
        self.check(native.roundtrip(self.display))
        self.compositor = self.bind('wl_compositor', 4)
        self.shm = self.bind('wl_shm')
        self.wm = self.bind('xdg_wm_base')
        if 'zxdg_decoration_manager_v1' in self.globals:
            self.decoration_manager = self.bind('zxdg_decoration_manager_v1')
        self.listen(self.wm, [([U], lambda serial: native.request(self.wm, 3, U(serial)))])
        self.seat = self.bind('wl_seat', 5)
        self.listen(self.seat, [([U], self.capabilities), ([ctypes.c_char_p], lambda name: None)])
        if 'wl_data_device_manager' in self.globals:
            self.data_manager = self.bind('wl_data_device_manager')
            self.data_device = native.request(self.data_manager, 1, P(), P(self.seat), interface='wl_data_device')
            self.listen(self.data_device, [([P], self.data_offer),
                ([U, P, I, I, P], lambda *args: None), ([], lambda: None),
                ([U, I, I], lambda *args: None), ([], lambda: None), ([P], self.select_offer)])
        self.check(native.roundtrip(self.display))
        self.init_egl()
        return True

    def check(self, result):
        if self.error:
            error, self.error = self.error, None
            raise error
        if result < 0:
            raise RuntimeError('Native Wayland connection failed')

    def listen(self, proxy, callbacks):
        self.native.listener(proxy, callbacks, self.fail)

    def fail(self, error):
        self.error = error

    def global_added(self, name, interface, version):
        interface = interface.decode()
        self.globals.setdefault(interface, (name, version))
        if interface == 'wl_output':
            output = self.native.request(self.registry, 0, U(name), ctypes.c_char_p(b'wl_output'),
                                         U(min(version, 2)), P(), interface='wl_output', version=min(version, 2))
            record = SimpleNamespace(proxy=output, name=name, x=0, y=0, width=1280, height=800,
                                     scale=1, refresh=60000)
            self.outputs[output] = record
            def geometry(x, y, physical_width, physical_height, subpixel, make, model, transform):
                record.x, record.y = x, y
            def mode(flags, width, height, refresh):
                if flags & 1:
                    record.width, record.height, record.refresh = width, height, refresh
            def scale(value):
                record.scale = max(1, value)
                for window in self.windows:
                    self.update_scale(window)
            self.listen(output, [([I, I, I, I, I, ctypes.c_char_p, ctypes.c_char_p, I], geometry),
                                 ([U, I, I, I], mode), ([], lambda: None), ([I], scale)])

    def global_removed(self, name):
        for proxy, output in list(self.outputs.items()):
            if output.name == name:
                del self.outputs[proxy]
                self.native.destroy(proxy)
        for window in self.windows:
            self.update_scale(window)

    def bind(self, interface, version=1):
        if interface not in self.globals:
            raise RuntimeError(f'Native Wayland requires {interface}')
        name, advertised = self.globals[interface]
        version = min(version, advertised)
        return self.native.request(self.registry, 0, U(name), ctypes.c_char_p(interface.encode()), U(version),
                                   P(), interface=interface, version=version)

    def init_egl(self):
        library = ctypes.CDLL('libEGL.so.1')
        signatures = {
            'eglGetError': (U,), 'eglGetPlatformDisplay': (P, U, P, P),
            'eglInitialize': (U, P, ctypes.POINTER(I), ctypes.POINTER(I)), 'eglBindAPI': (U, U),
            'eglChooseConfig': (U, P, ctypes.POINTER(I), ctypes.POINTER(P), I, ctypes.POINTER(I)),
            'eglCreateContext': (P, P, P, P, ctypes.POINTER(I)),
            'eglCreateWindowSurface': (P, P, P, P, P),
            'eglCreatePbufferSurface': (P, P, P, ctypes.POINTER(I)),
            'eglMakeCurrent': (U, P, P, P, P), 'eglSwapBuffers': (U, P, P), 'eglSwapInterval': (U, P, I),
            'eglDestroySurface': (U, P, P), 'eglDestroyContext': (U, P, P), 'eglTerminate': (U, P)}
        self.egl = SimpleNamespace(**{name: function(library, name, *signature)
                                      for name, signature in signatures.items()})
        self.egl_display = self.egl.eglGetPlatformDisplay(0x31D8, self.display, None)
        self.egl_check(self.egl_display, 'eglGetPlatformDisplay')
        major, minor = I(), I()
        self.egl_check(self.egl.eglInitialize(self.egl_display, ctypes.byref(major), ctypes.byref(minor)), 'eglInitialize')
        self.egl_check(self.egl.eglBindAPI(0x30A2), 'eglBindAPI')
        # Window and pbuffer config: the hidden atlas owner needs a framebuffer.
        attributes = (I * 15)(0x3033, 5, 0x3040, 8, 0x3024, 8, 0x3023, 8,
                              0x3022, 8, 0x3021, 8, 0x3026, 8, 0x3038)
        self.config, count = P(), I()
        self.egl_check(self.egl.eglChooseConfig(self.egl_display, attributes, ctypes.byref(self.config),
                                               1, ctypes.byref(count)), 'eglChooseConfig')
        if not count.value:
            raise RuntimeError('No native EGL OpenGL RGBA8/stencil8 config')
        library = ctypes.CDLL('libwayland-egl.so.1')
        self.egl_window_create = function(library, 'wl_egl_window_create', P, P, I, I)
        self.egl_window_resize = function(library, 'wl_egl_window_resize', None, P, I, I, I, I)
        self.egl_window_destroy = function(library, 'wl_egl_window_destroy', None, P)

    def egl_check(self, result, operation):
        if not result:
            raise RuntimeError(f'{operation} failed: EGL 0x{self.egl.eglGetError():04x}')

    def create_window(self, width, height, title, monitor=None, share=None):
        window = NativeWindow(self, int(width), int(height), title, self.hints)
        self.windows.append(window)
        try:
            if window.visible:
                native = self.native
                window.surface = native.request(self.compositor, 0, P(), interface='wl_surface')
                self.by_surface[window.surface] = window
                self.listen(window.surface, [([P], lambda output: self.surface_output(window, output, True)),
                                              ([P], lambda output: self.surface_output(window, output, False))])
                window.xdg_surface = native.request(self.wm, 2, P(), P(window.surface), interface='xdg_surface')
                self.listen(window.xdg_surface, [([U], lambda serial: self.configure(window, serial))])
                window.toplevel = native.request(window.xdg_surface, 1, P(), interface='xdg_toplevel')
                self.listen(window.toplevel, [([I, I, P], lambda w, h, states: self.toplevel_size(window, w, h, states)),
                                              ([], lambda: self.request_close(window))])
                native.request(window.toplevel, 2, ctypes.c_char_p(title.encode()))
                app_id = self.string_hints.get(codes.WAYLAND_APP_ID, 'meltygui')
                native.request(window.toplevel, 3, ctypes.c_char_p(app_id.encode()))
                self.sync_decoration(window)
                native.request(window.surface, 6)
                while not window.configured:
                    self.check(native.dispatch(self.display))
                window.egl_window = self.egl_window_create(window.surface, *self.get_framebuffer_size(window))
                if not window.egl_window:
                    raise RuntimeError('wl_egl_window_create failed')
                window.egl_surface = self.egl.eglCreateWindowSurface(self.egl_display, self.config, window.egl_window, None)
            else:
                attributes = (I * 5)(0x3057, width, 0x3056, height, 0x3038)
                window.egl_surface = self.egl.eglCreatePbufferSurface(self.egl_display, self.config, attributes)
            self.egl_check(window.egl_surface, 'create EGL surface')
            attributes = (I * 7)(0x3098, 4, 0x30FB, 3, 0x30FD, 1, 0x3038)
            window.context = self.egl.eglCreateContext(self.egl_display, self.config,
                                                      share.context if share else None, attributes)
            self.egl_check(window.context, 'eglCreateContext')
            return window
        except BaseException:
            self.destroy_window(window)
            raise

    def toplevel_size(self, window, width, height, states):
        class Array(ctypes.Structure):
            _fields_ = [('size', ctypes.c_size_t), ('alloc', ctypes.c_size_t), ('data', P)]
        values = ctypes.cast(states, ctypes.POINTER(Array)).contents
        states = list((U * (values.size // 4)).from_address(values.data)) if values.size else []
        window.maximized = 1 in states
        window.width, window.height = width or window.width, height or window.height

    def configure(self, window, serial):
        self.native.request(window.xdg_surface, 4, U(serial))
        window.configured = True
        if window.egl_window:
            self.egl_window_resize(window.egl_window, *self.get_framebuffer_size(window), 0, 0)
            window.emit('window_size', window.width, window.height)
            window.emit('framebuffer_size', *self.get_framebuffer_size(window))

    def surface_output(self, window, output, entered):
        if entered:
            window.outputs.add(output)
        else:
            window.outputs.discard(output)
        self.update_scale(window)

    def update_scale(self, window):
        scale = max((self.outputs[o].scale for o in window.outputs if o in self.outputs), default=1)
        if scale != window.scale:
            window.scale = scale
            self.native.request(window.surface, 8, I(scale))
            if window.egl_window:
                self.egl_window_resize(window.egl_window, *self.get_framebuffer_size(window), 0, 0)
                window.emit('framebuffer_size', *self.get_framebuffer_size(window))

    def make_context_current(self, window):
        if self.get_current_context() is window:
            return
        surface, context = (window.egl_surface, window.context) if window else (None, None)
        self.egl_check(self.egl.eglMakeCurrent(self.egl_display, surface, surface, context), 'eglMakeCurrent')
        self.current.window = window

    def get_current_context(self):
        return getattr(self.current, 'window', None)

    def swap_buffers(self, window):
        self.egl_check(self.egl.eglSwapBuffers(self.egl_display, window.egl_surface), 'eglSwapBuffers')

    def swap_interval(self, interval):
        self.egl_check(self.egl.eglSwapInterval(self.egl_display, interval), 'eglSwapInterval')

    def destroy_window(self, window):
        if window.destroyed:
            return
        window.destroyed = True
        if self.get_current_context() is window:
            self.make_context_current(None)
        if window.egl_surface:
            self.egl.eglDestroySurface(self.egl_display, window.egl_surface)
        if window.context:
            self.egl.eglDestroyContext(self.egl_display, window.context)
        if window.egl_window:
            self.egl_window_destroy(window.egl_window)
        for proxy in (window.decoration, window.toplevel, window.xdg_surface, window.surface):
            if proxy:
                self.native.request(proxy, 0, destroy=True)
        self.by_surface.pop(window.surface, None)
        if window in self.windows:
            self.windows.remove(window)
        if self.keyboard_window is window:
            self.keyboard_window, self.repeat_key = None, None
        if self.pointer_window is window:
            self.pointer_window = None
        window.callbacks.clear()

    def terminate(self):
        for window in list(reversed(self.windows)):
            self.destroy_window(window)
        if self.egl_display:
            self.egl.eglTerminate(self.egl_display)
        if self.cursor_theme:
            self.cursor_theme_destroy(self.cursor_theme)
        for attr, release in [('xkb_state', 'xkb_state_unref'), ('xkb_keymap', 'xkb_keymap_unref'),
                               ('xkb_context', 'xkb_context_unref')]:
            value = getattr(self, attr)
            if value:
                getattr(self.xkb, release)(value)
        if self.display:
            self.native.disconnect(self.display)
        self.display = self.egl_display = None
        self.wake_reader.close()
        self.wake_writer.close()

    def capabilities(self, capabilities):
        native = self.native
        if capabilities & 2 and not self.keyboard:
            self.keyboard = native.request(self.seat, 1, P(), interface='wl_keyboard')
            self.listen(self.keyboard, [([U, I, U], self.keymap), ([U, P, P], self.keyboard_enter),
                ([U, P], self.keyboard_leave), ([U, U, U, U], self.key),
                ([U, U, U, U, U], self.keyboard_modifiers), ([I, I], self.repeat_info)])
        if capabilities & 1 and not self.pointer:
            self.pointer = native.request(self.seat, 0, P(), interface='wl_pointer')
            self.listen(self.pointer, [([U, P, I, I], self.pointer_enter), ([U, P], self.pointer_leave),
                ([U, I, I], self.pointer_motion), ([U, U, U, U], self.pointer_button),
                ([U, U, I], self.pointer_axis), ([], lambda: None), ([U], lambda source: None),
                ([U, U], lambda timestamp, axis: None), ([U, I], lambda axis, steps: None)])

    def keymap(self, format, descriptor, size):
        try:
            if format != 1:
                return
            with mmap.mmap(descriptor, size, access=mmap.ACCESS_READ) as source:
                text = source[:]
        finally:
            os.close(descriptor)
        if not self.xkb_context:
            library = ctypes.CDLL('libxkbcommon.so.0')
            signatures = {'xkb_context_new': (P, U), 'xkb_context_unref': (None, P),
                'xkb_keymap_new_from_string': (P, P, ctypes.c_char_p, U, U), 'xkb_keymap_unref': (None, P),
                'xkb_state_new': (P, P), 'xkb_state_unref': (None, P),
                'xkb_state_update_mask': (U, P, U, U, U, U, U, U),
                'xkb_state_key_get_utf32': (U, P, U), 'xkb_state_key_get_one_sym': (U, P, U),
                'xkb_state_mod_name_is_active': (I, P, ctypes.c_char_p, U),
                'xkb_keymap_key_repeats': (I, P, U)}
            self.xkb = SimpleNamespace(**{name: function(library, name, *signature)
                                         for name, signature in signatures.items()})
            self.xkb_context = self.xkb.xkb_context_new(0)
        if self.xkb_state:
            self.xkb.xkb_state_unref(self.xkb_state)
            self.xkb.xkb_keymap_unref(self.xkb_keymap)
        self.xkb_keymap = self.xkb.xkb_keymap_new_from_string(self.xkb_context, text, 1, 0)
        if not self.xkb_keymap:
            raise RuntimeError('Cannot compile Wayland keyboard keymap')
        self.xkb_state = self.xkb.xkb_state_new(self.xkb_keymap)

    def keyboard_modifiers(self, serial, depressed, latched, locked, group):
        self.serial = serial
        if not self.xkb_state:
            return
        self.xkb.xkb_state_update_mask(self.xkb_state, depressed, latched, locked, 0, 0, group)
        self.modifiers = 0
        for name, mask in [('Shift', codes.MOD_SHIFT), ('Control', codes.MOD_CONTROL),
                           ('Mod1', codes.MOD_ALT), ('Mod4', codes.MOD_SUPER)]:
            if self.xkb.xkb_state_mod_name_is_active(self.xkb_state, name.encode(), 1 << 3):
                self.modifiers |= mask

    def keyboard_enter(self, serial, surface, keys):
        self.serial = serial
        window = self.by_surface.get(surface)
        self.keyboard_window = window
        if window:
            window.focused, window.iconified = True, False
            if keys:
                class Array(ctypes.Structure):
                    _fields_ = [('size', ctypes.c_size_t), ('alloc', ctypes.c_size_t), ('data', P)]
                held = ctypes.cast(keys, ctypes.POINTER(Array)).contents
                scans = (U * (held.size // ctypes.sizeof(U))).from_address(held.data) if held.size else ()
                for scan in scans:
                    key = self.key_code(scan)
                    window.keys[key] = codes.PRESS
                    window.emit('key', key, scan, codes.PRESS, self.modifiers)
            window.emit('window_focus', True)

    def keyboard_leave(self, serial, surface):
        self.serial = serial
        window = self.by_surface.get(surface)
        if window:
            for key in list(window.keys):
                if window.keys[key]:
                    window.keys[key] = codes.RELEASE
                    window.emit('key', key, 0, codes.RELEASE, self.modifiers)
            window.focused = False
            window.emit('window_focus', False)
        self.keyboard_window, self.repeat_key = None, None

    def key_code(self, scan):
        # GLFW key tokens use keyboard physical positions, independent of text layout.
        rows = [(2, '1234567890'), (16, 'QWERTYUIOP'), (30, 'ASDFGHJKL'), (44, 'ZXCVBNM')]
        for start, letters in rows:
            if start <= scan < start + len(letters):
                return ord(letters[scan - start])
        special = {1: 'ESCAPE', 12: 'MINUS', 13: 'EQUAL', 14: 'BACKSPACE', 15: 'TAB',
            26: 'LEFT_BRACKET', 27: 'RIGHT_BRACKET', 28: 'ENTER', 29: 'LEFT_CONTROL',
            39: 'SEMICOLON', 40: 'APOSTROPHE', 41: 'GRAVE_ACCENT', 42: 'LEFT_SHIFT',
            43: 'BACKSLASH', 51: 'COMMA', 52: 'PERIOD', 53: 'SLASH', 54: 'RIGHT_SHIFT',
            55: 'KP_MULTIPLY', 56: 'LEFT_ALT', 57: 'SPACE', 58: 'CAPS_LOCK', 69: 'NUM_LOCK',
            70: 'SCROLL_LOCK', 71: 'KP_7', 72: 'KP_8', 73: 'KP_9', 74: 'KP_SUBTRACT',
            75: 'KP_4', 76: 'KP_5', 77: 'KP_6', 78: 'KP_ADD', 79: 'KP_1', 80: 'KP_2',
            81: 'KP_3', 82: 'KP_0', 83: 'KP_DECIMAL', 87: 'F11', 88: 'F12', 96: 'KP_ENTER',
            97: 'RIGHT_CONTROL', 98: 'KP_DIVIDE', 100: 'RIGHT_ALT', 102: 'HOME', 103: 'UP',
            104: 'PAGE_UP', 105: 'LEFT', 106: 'RIGHT', 107: 'END', 108: 'DOWN',
            109: 'PAGE_DOWN', 110: 'INSERT', 111: 'DELETE', 125: 'LEFT_SUPER', 126: 'RIGHT_SUPER', 127: 'MENU'}
        if 59 <= scan <= 68:
            return codes.KEY_F1 + scan - 59
        name = special.get(scan)
        return getattr(codes, 'KEY_' + name) if name else codes.KEY_UNKNOWN

    def key(self, serial, timestamp, scan, state):
        self.serial = serial
        window = self.keyboard_window
        if window is None:
            return
        key = self.key_code(scan)
        action = codes.PRESS if state else codes.RELEASE
        window.keys[key] = action
        window.emit('key', key, scan, action, self.modifiers)
        if state:
            self.character(window, scan)
            if self.xkb_keymap and self.xkb.xkb_keymap_key_repeats(self.xkb_keymap, scan + 8):
                self.repeat_key, self.next_repeat = (window, scan, key), time.monotonic() + self.repeat_delay
        elif self.repeat_key and self.repeat_key[1] == scan:
            self.repeat_key = None

    def character(self, window, scan):
        if self.xkb_state and not self.modifiers & (codes.MOD_CONTROL | codes.MOD_SUPER):
            character = self.xkb.xkb_state_key_get_utf32(self.xkb_state, scan + 8)
            if character >= 32 and character != 127:
                window.emit('char', character)

    def repeat_info(self, rate, delay):
        self.repeat_rate, self.repeat_delay = max(0, rate), max(0, delay) / 1000

    def repeat(self):
        if self.repeat_key and self.repeat_rate and time.monotonic() >= self.next_repeat:
            window, scan, key = self.repeat_key
            window.emit('key', key, scan, codes.REPEAT, self.modifiers)
            self.character(window, scan)
            self.next_repeat = time.monotonic() + 1 / self.repeat_rate

    def pointer_enter(self, serial, surface, x, y):
        self.enter_serial = self.serial = serial
        self.pointer_window = window = self.by_surface.get(surface)
        if window:
            window.hovered = True
            window.cursor_pos = (x / 256, y / 256)
            window.cursor_motion_generation += 1
            window.emit('cursor_enter', True)
            window.emit('cursor_pos', *window.cursor_pos)
            self.set_cursor(window, getattr(window, 'cursor', None))

    def pointer_leave(self, serial, surface):
        window = self.by_surface.get(surface)
        if window:
            window.hovered = False
            window.emit('cursor_enter', False)
        self.pointer_window = None

    def pointer_motion(self, timestamp, x, y):
        if self.pointer_window:
            self.pointer_window.cursor_pos = (x / 256, y / 256)
            self.pointer_window.cursor_motion_generation += 1
            self.pointer_window.emit('cursor_pos', *self.pointer_window.cursor_pos)

    def pointer_button(self, serial, timestamp, button, state):
        self.serial = serial
        if self.pointer_window:
            button = {272: 0, 273: 1, 274: 2}.get(button, button - 272)
            self.pointer_window.buttons[button] = int(state)
            self.pointer_window.emit('mouse_button', button, int(state), self.modifiers)

    def pointer_axis(self, timestamp, axis, value):
        if self.pointer_window:
            offset = -value / 2560
            self.pointer_window.emit('scroll', offset if axis else 0., 0. if axis else offset)

    def poll_events(self):
        self.wait_events_timeout(0)

    def wait_events(self):
        self.wait_events_timeout(None)

    def wait_events_timeout(self, timeout):
        native = self.native
        self.check(native.pending(self.display))
        if self.repeat_key and self.repeat_rate:
            delay = max(0., self.next_repeat - time.monotonic())
            timeout = min(timeout, delay) if timeout is not None else delay
        # EGL might also read this connection. Reserve the read before selecting
        # so another reader cannot consume the readiness and leave us blocked.
        while native.prepare_read(self.display) != 0:
            self.check(native.pending(self.display))
        prepared = True
        descriptor = native.get_fd(self.display)
        try:
            flushed = native.flush(self.display)
            blocked = flushed < 0 and ctypes.get_errno() == errno.EAGAIN
            if flushed < 0 and not blocked:
                self.check(flushed)
            ready, writable, _ = select.select([descriptor, self.wake_reader],
                                               [descriptor] if blocked else [], [], timeout)
            if descriptor in ready:
                prepared = False
                self.check(native.read_events(self.display))
            if writable:
                native.flush(self.display)
        finally:
            if prepared:
                native.cancel_read(self.display)
        self.check(native.pending(self.display))
        if self.wake_reader in ready:
            try:
                while self.wake_reader.recv(4096):
                    pass
            except BlockingIOError:
                pass
        self.repeat()

    def post_empty_event(self):
        try:
            self.wake_writer.send(b'1')
        except (BlockingIOError, OSError):
            pass

    def data_offer(self, offer):
        self.offers[offer] = []
        self.listen(offer, [([ctypes.c_char_p], lambda mime: self.offers[offer].append(mime))])

    def select_offer(self, offer):
        previous = self.selection
        self.selection = offer
        if previous and previous != offer:
            self.native.request(previous, 2, destroy=True)
            self.offers.pop(previous, None)

    def set_clipboard_string(self, window, text):
        if not hasattr(self, 'data_device'):
            raise RuntimeError('Compositor has no clipboard protocol')
        self.clipboard_text = text.encode('utf-8') if isinstance(text, str) else bytes(text)
        if self.data_source:
            self.native.request(self.data_source, 1, destroy=True)
        self.data_source = source = self.native.request(self.data_manager, 0, P(), interface='wl_data_source')
        payload = self.clipboard_text
        def send(mime, descriptor):
            def write():
                try:
                    os.set_blocking(descriptor, True)
                    remaining = memoryview(payload)
                    while remaining:
                        remaining = remaining[os.write(descriptor, remaining):]
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    os.close(descriptor)
            threading.Thread(target=write, daemon=True, name='wayland-clipboard').start()
        def cancelled():
            if self.data_source == source:
                self.data_source = None
                self.clipboard_text = None
            self.native.request(source, 1, destroy=True)
        self.listen(source, [([ctypes.c_char_p], lambda mime: None), ([ctypes.c_char_p, I], send), ([], cancelled)])
        for mime in (b'text/plain;charset=utf-8', b'text/plain', b'UTF8_STRING'):
            self.native.request(source, 0, ctypes.c_char_p(mime))
        self.native.request(self.data_device, 1, P(source), U(self.serial))
        self.native.flush(self.display)

    def get_clipboard_string(self, window):
        if self.data_source and self.clipboard_text is not None:
            return self.clipboard_text
        if not self.selection:
            return b''
        offered = self.offers.get(self.selection, [])
        mime = next((m for m in (b'text/plain;charset=utf-8', b'UTF8_STRING', b'text/plain') if m in offered), None)
        if mime is None:
            return b''
        reader, writer = os.pipe2(os.O_CLOEXEC)
        os.set_blocking(reader, False)
        data = bytearray()
        try:
            self.native.request(self.selection, 1, ctypes.c_char_p(mime), I(writer))
            self.native.flush(self.display)
            os.close(writer)
            writer = -1
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if select.select([reader], [], [], 0.01)[0]:
                    chunk = os.read(reader, 65536)
                    if not chunk:
                        return bytes(data)
                    data.extend(chunk)
                self.poll_events()
            raise TimeoutError('Wayland clipboard transfer timed out')
        finally:
            os.close(reader)
            if writer >= 0:
                os.close(writer)

    def create_standard_cursor(self, shape):
        return shape

    def create_cursor(self, image, xhot, yhot):
        # Custom cursors are represented without GL/Wayland work until selected.
        return SimpleNamespace(image=image, xhot=int(xhot), yhot=int(yhot), buffer=None)

    def set_cursor(self, window, cursor):
        window.cursor = cursor
        if window is not self.pointer_window or not self.enter_serial:
            return
        self.install_cursor(cursor)

    def install_cursor(self, cursor):
        if cursor is not None and not isinstance(cursor, int):
            self.install_image_cursor(cursor)
            return
        if not self.cursor_theme:
            library = ctypes.CDLL('libwayland-cursor.so.0')
            load = function(library, 'wl_cursor_theme_load', P, ctypes.c_char_p, I, P)
            self.cursor_theme_destroy = function(library, 'wl_cursor_theme_destroy', None, P)
            self.cursor_get = function(library, 'wl_cursor_theme_get_cursor', P, P, ctypes.c_char_p)
            self.cursor_buffer = function(library, 'wl_cursor_image_get_buffer', P, P)
            self.cursor_theme = load(os.environ.get('XCURSOR_THEME', 'default').encode(),
                                     int(os.environ.get('XCURSOR_SIZE', '24')), self.shm)
            self.cursor_surface = self.native.request(self.compositor, 0, P(), interface='wl_surface')
        names = {codes.IBEAM_CURSOR: b'text', codes.CROSSHAIR_CURSOR: b'crosshair',
                 codes.POINTING_HAND_CURSOR: b'pointer', codes.RESIZE_EW_CURSOR: b'ew-resize',
                 codes.RESIZE_NS_CURSOR: b'ns-resize', codes.RESIZE_NWSE_CURSOR: b'nwse-resize',
                 codes.RESIZE_NESW_CURSOR: b'nesw-resize', codes.NOT_ALLOWED_CURSOR: b'not-allowed'}
        name = names.get(cursor, b'left_ptr') if isinstance(cursor, int) else b'left_ptr'
        pointer = self.cursor_get(self.cursor_theme, name) if self.cursor_theme else None
        if not pointer:
            return
        class Cursor(ctypes.Structure):
            _fields_ = [('count', U), ('images', ctypes.POINTER(P)), ('name', ctypes.c_char_p)]
        class Image(ctypes.Structure):
            _fields_ = [('width', U), ('height', U), ('hotspot_x', U), ('hotspot_y', U), ('delay', U)]
        image_pointer = ctypes.cast(pointer, ctypes.POINTER(Cursor)).contents.images[0]
        image = ctypes.cast(image_pointer, ctypes.POINTER(Image)).contents
        buffer = self.cursor_buffer(image_pointer)
        self.native.request(self.pointer, 0, U(self.enter_serial), P(self.cursor_surface), I(image.hotspot_x), I(image.hotspot_y))
        self.native.request(self.cursor_surface, 1, P(buffer), I(0), I(0))
        self.native.request(self.cursor_surface, 2, I(0), I(0), I(image.width), I(image.height))
        self.native.request(self.cursor_surface, 6)

    def install_image_cursor(self, cursor):
        if not self.cursor_surface:
            self.cursor_surface = self.native.request(self.compositor, 0, P(), interface='wl_surface')
        if cursor.buffer is None:
            import numpy
            pixels = numpy.asarray(cursor.image.convert('RGBA'), dtype=numpy.uint8)
            # wl_shm ARGB8888 stores premultiplied BGRA bytes on little-endian Linux.
            pixels = pixels[:, :, [2, 1, 0, 3]].copy()
            pixels[:, :, :3] = ((pixels[:, :, :3].astype(numpy.uint16) *
                                pixels[:, :, 3:4].astype(numpy.uint16) + 127) // 255).astype(numpy.uint8)
            payload = pixels.tobytes()
            descriptor = os.memfd_create('meltygui-cursor', os.MFD_CLOEXEC)
            try:
                os.ftruncate(descriptor, len(payload))
                with mmap.mmap(descriptor, len(payload)) as memory:
                    memory[:] = payload
                pool = self.native.request(self.shm, 0, P(), I(descriptor), I(len(payload)), interface='wl_shm_pool')
                width, height = cursor.image.size
                cursor.buffer = self.native.request(pool, 0, P(), I(0), I(width), I(height), I(width * 4), U(0),
                                                     interface='wl_buffer')
                self.listen(cursor.buffer, [([], lambda: None)])
                self.native.request(pool, 1, destroy=True)
                self.custom_cursor_buffers.append(cursor.buffer)
            finally:
                os.close(descriptor)
        width, height = cursor.image.size
        self.native.request(self.pointer, 0, U(self.enter_serial), P(self.cursor_surface), I(cursor.xhot), I(cursor.yhot))
        self.native.request(self.cursor_surface, 1, P(cursor.buffer), I(0), I(0))
        self.native.request(self.cursor_surface, 2, I(0), I(0), I(width), I(height))
        self.native.request(self.cursor_surface, 6)

    def request_close(self, window):
        window.closed = True
        window.emit('window_close')

    def window_should_close(self, window):
        return window.closed

    def set_window_should_close(self, window, value):
        window.closed = bool(value)
        self.post_empty_event()

    def get_window_size(self, window):
        return window.width, window.height

    def get_framebuffer_size(self, window):
        return window.width * window.scale, window.height * window.scale

    def set_window_size(self, window, width, height):
        window.width, window.height = max(1, int(width)), max(1, int(height))
        if window.egl_window:
            self.egl_window_resize(window.egl_window, *self.get_framebuffer_size(window), 0, 0)
        window.emit('window_size', window.width, window.height)
        window.emit('framebuffer_size', *self.get_framebuffer_size(window))

    def get_window_attrib(self, window, attribute):
        values = {codes.FOCUSED: window.focused, codes.HOVERED: window.hovered,
                  codes.VISIBLE: window.visible, codes.MAXIMIZED: window.maximized, codes.ICONIFIED: window.iconified,
                  codes.TRANSPARENT_FRAMEBUFFER: window.hints.get(codes.TRANSPARENT_FRAMEBUFFER, False)}
        return int(values.get(attribute, window.hints.get(attribute, False)))

    def sync_decoration(self, window):
        # Undecorated means the app supplies its own chrome. Without this
        # request Hyprbars cannot distinguish it from an unframed client.
        if not self.decoration_manager or not window.toplevel:
            return
        if window.decoration is None:
            window.decoration = self.native.request(
                self.decoration_manager, 1, P(), P(window.toplevel),
                interface='zxdg_toplevel_decoration_v1')
            self.listen(window.decoration, [([U], lambda mode: setattr(window, 'decoration_mode', mode))])
        mode = 2 if window.hints.get(codes.DECORATED, True) else 1
        self.native.request(window.decoration, 1, U(mode))

    def set_window_attrib(self, window, attribute, value):
        window.hints[attribute] = value
        if attribute == codes.DECORATED:
            self.sync_decoration(window)

    def get_cursor_pos(self, window):
        return window.cursor_pos

    def get_mouse_button(self, window, button):
        return window.buttons.get(button, codes.RELEASE)

    def get_key(self, window, key):
        return window.keys.get(key, codes.RELEASE)

    def get_time(self):
        return time.monotonic()

    def get_platform(self):
        return codes.PLATFORM_WAYLAND

    def get_wayland_display(self):
        return self.display

    def get_wayland_window(self, window):
        return window.surface

    def init_hint(self, *args):
        pass

    def default_window_hints(self):
        self.hints.clear()
        self.string_hints.clear()

    def window_hint(self, hint, value):
        self.hints[hint] = value

    def window_hint_string(self, hint, value):
        self.string_hints[hint] = value

    def set_window_title(self, window, title):
        window.title = title
        if window.toplevel:
            self.native.request(window.toplevel, 2, ctypes.c_char_p(title.encode()))

    def maximize_window(self, window):
        self.native.request(window.toplevel, 9)

    def restore_window(self, window):
        self.native.request(window.toplevel, 10)
        window.iconified = False

    def iconify_window(self, window):
        self.native.request(window.toplevel, 13)
        window.iconified = True

    def hide_window(self, window):
        if window.surface:
            self.native.request(window.surface, 1, P(), I(0), I(0))
            self.native.request(window.surface, 6)
        window.visible, window.configured = False, False

    def show_window(self, window):
        if window.surface and not window.visible:
            self.native.request(window.surface, 6)
            while not window.configured:
                self.check(self.native.dispatch(self.display))
        window.visible = True
        self.post_empty_event()

    def get_window_pos(self, window):
        import meltygui.core.geometry_feed as geometry_feed
        rect = geometry_feed.surface_rect(window.title)
        return (int(rect[0]), int(rect[1])) if rect else (0, 0)

    def set_window_pos(self, window, x, y):
        import meltygui.core.geometry_feed as geometry_feed
        geometry_feed.place_window(window.title, (x, y, window.width, window.height), resize=False)

    def get_monitors(self):
        return list(self.outputs.values())

    def get_primary_monitor(self):
        return next(iter(self.outputs.values()), None)

    def get_window_monitor(self, window):
        return None

    def get_monitor_pos(self, monitor):
        return monitor.x, monitor.y

    def get_monitor_workarea(self, monitor):
        return monitor.x, monitor.y, monitor.width // monitor.scale, monitor.height // monitor.scale

    def get_video_mode(self, monitor):
        return SimpleNamespace(size=SimpleNamespace(width=monitor.width // monitor.scale,
                                                    height=monitor.height // monitor.scale),
                               refresh_rate=monitor.refresh // 1000, bits=(8, 8, 8))

    def __getattr__(self, name):
        if name.startswith('set_') and name.endswith('_callback'):
            event = name[4:-9]
            def setter(window, callback):
                previous = window.callbacks.get(event)
                window.callbacks[event] = callback
                return previous
            return setter
        raise AttributeError(f'Native Wayland has no window operation {name}')
