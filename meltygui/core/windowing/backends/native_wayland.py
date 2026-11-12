"""Native Wayland/EGL window backend for meltygui's Surface share group.

GLFW-compatible constants/callbacks let existing input/chrome code share this
backend without loading GLFW. All protocol dispatch and graphics calls run on
the render thread.

Latency: the frame loop calls `pace` before it reads input (app.py). It waits
there for the compositor's frame callback, so a frame is drawn from input read
after the previous one was taken, and `eglSwapBuffers` no longer blocks with a
finished frame in hand: input reaches the screen one refresh sooner. It then
waits on, so the frame is handed over shortly before the refresh it is for
instead of right after the callback (`LATE`, `GUARD`; the guard tunes itself, `tune`). Every swap also asks for
presentation feedback (`presented`): when each frame was on screen. That gives
the refresh times `pace` aims at, and `get_presentation_latency_ms` (input read
to on screen), which the frame-rate overlay shows. Levers: the constants below.
"""
import ctypes
import errno
import math
import mmap
import os
import select
import socket
import threading
import time
from types import SimpleNamespace

import meltygui.core.windowing.window_constants as codes

from meltygui.core.windowing.backends.wayland_protocol import Native
from meltygui.core.windowing.backends.wayland_protocol import function
from meltygui.core.windowing.backends.wayland_protocol import P
from meltygui.core.windowing.backends.wayland_protocol import U
from meltygui.core.windowing.backends.wayland_protocol import I


# Levers of frame pacing (`Backend.pace`).
# True: start each frame late (step 2 of `pace`), so that it is handed to the compositor `GUARD`
# seconds before the refresh it is meant for. Input is then that much fresher on screen.
LATE = True
# True: a frame that started late waits for the GPU to finish drawing it before it is handed over,
# and that wait counts as drawing time (`cost`). Handed over with the GPU still on it, the frame
# reaches the compositor as it starts its own draw on the same GPU: its draw measures slow, its
# deadline moves ahead of ours, and every frame shows a refresh late (60 fps, 16 ms; measured
# 2026-11-12 on the panel, set off by a window resize). Costs the GPU time in latency (about 1 ms
# for a 4000 px wide editor). False hands over at once.
FINISH = True
# Seconds before a refresh by which the frame for it is handed over: what the compositor needs to
# take a frame and put it up. Smaller is less latency and closer to a missed refresh (the frame
# shows a whole period late: a stutter). The refreshes are the times the compositor reports frames
# presented at (`presented`), not when its frame callbacks arrive: those shift with its own load.
# The guard is a base plus a lift (`Backend.tune`, levers below). `GUARD` is where the base starts
# and what it returns to (at most half a period); base and lift together never exceed `LONG` (a
# share of the period: 5.5 ms at 120 Hz. Longer bought nothing when measured, 0.4 % misses at 6 ms
# against 0.9 % at 5.5, and from about 6 ms on frames start aiming at every second refresh). Measured 2026-11-12 on a Hyprland desktop shown through a 120 Hz panel
# with tools/pace_calibrate.py (small window): 4 ms before missed none of 480 frames, 3.5 ms 0.2 %
# (the compositor's own), 3 ms 1.1 %, 2.5 ms 2.8 %. A 2130x1763 window there needs about 4.5 ms.
# `ADAPT` False keeps the guard at `GUARD` (the calibration tool steps it by hand).
GUARD = 0.0035
LONG = 0.66
ADAPT = True
# Frames whose drawing time is remembered, for how many seconds, and which of them (sorted, as a
# share) a frame is given: 0.9 leaves the slowest tenth to the guard. `FRESH` is what lets a few
# slow frames go: without it they would count until `COSTS` more were drawn, long when frames are sparse.
COSTS = 24
FRESH = 0.5
COST = 0.9
# How the guard finds its length. A frame missed when it was handed over in time (no more than
# `SLIP` after its guard began) and still came on screen later than the refresh it was for. Not
# judged: a frame that took longer to draw than the ones before it (no guard helps it), and the
# first frame after a pause or a slow frame (the compositor is not in step then).
# - Lift, the answer to what happens now: `MISSES` misses within `RECENT` seconds make the guard
#   `MISS` longer at once. The lift fades to nothing over `FADE` seconds, so a disturbance costs
#   latency for about that long and no longer. Misses in the `SETTLE` seconds after a lift do not
#   count (frames already on their way).
# - Base, what the display needs: lifts that keep following each other (each within `AGAIN`
#   seconds of the one before) for `LEARN` seconds say it is no disturbance, and the base gets
#   `MISS` longer. After `REST` seconds without that it eases back towards `GUARD`, `STEP` every
#   `CALM` seconds, never below `GUARD`: it does not search for the edge. Misses while easing
#   put it back `MISS` (no higher than where the easing began); when that happens again at
#   about the same length the rest doubles, up to `REST_MAX`.
# Less latency for more stutters: raise `MISSES`, lower `GUARD`. Quicker return of a learned
# base: lower `REST` / `REST_MAX` (each return that fails costs `MISSES` late frames).
# A new refresh rate starts over; misses in the first `CALM` seconds after that (a window
# opening) do not count.
MISSES = 3
RECENT = 0.5
MISS = 0.0005
FADE = 0.5
AGAIN = 2.
LEARN = 3.
SLIP = 0.0003
SETTLE = 0.25
STEP = 0.0001
CALM = 1.
REST = 10.
REST_MAX = 120.
# Longest wait for the compositor to ask for a frame: a hidden or covered window is never asked.
WAIT = 0.05


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
        # Pacing (Backend.pace): the frame callback of the last swap still to come, when it was asked for,
        # when the compositor last sent one, and the time the frames took to draw lately.
        self.frame_due, self.frame_asked, self.frame_at = None, 0., 0.
        self.costs = []
        self.latency_ms = None     # input to screen of the last frame the compositor reported presented
        self.refresh = 0.          # seconds between two refreshes of its output, as the compositor reports them
        self.shown = 0.            # when its last frame was on screen: a refresh, on the compositor's clock
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
        self.presentation = None
        self.clock = time.CLOCK_MONOTONIC
        self.feedbacks = {}        # presentation feedback proxy -> (window, when its frame's input was read)
        self.callbacks_due = {}    # frame callback proxy -> window
        self.sampled = 0.          # when input was last read for a frame (pace)
        self.late = False          # that frame started late: it is judged when it is presented
        self.aim = None            # the refresh the frame being drawn started late for
        self.due = 0.              # when that frame is to be handed over: the guard before `aim`
        # The guard as it tunes itself (`tune`): base plus lift, the period it was found for, until
        # when misses do not count, the lift as it was at `hit` (it fades from then), since when
        # lifts follow each other, when frames lately missed, when the base may next ease, its rest
        # after misses, the length easing last failed at and where the easing under way began
        # (None: not easing).
        self.guard, self.base, self.paced, self.deaf = GUARD, GUARD, 0., 0.
        self.lift, self.hit, self.since, self.missed = 0., 0., 0., []
        self.settled, self.rest, self.failed, self.held = 0., REST, 0., None

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
        # A frame callback and a presentation feedback are made per frame: one listener table for each kind.
        self.frame_table = native.shared([([U], self.frame_done)], self.fail)
        self.feedback_table = native.shared([([P], lambda proxy, output: None), ([U] * 7, self.presented),
                                             ([], self.discarded)], self.fail)
        if 'wp_presentation' in self.globals:
            self.presentation = self.bind('wp_presentation')
            self.listen(self.presentation, [([U], self.clock_id)])
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
                                     scale=1, refresh=60000, subpixel=0)
            self.outputs[output] = record
            def geometry(x, y, physical_width, physical_height, subpixel, make, model, transform):
                record.x, record.y = x, y
                record.subpixel = subpixel
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
        self.finish = function(ctypes.CDLL('libOpenGL.so.0'), 'glFinish', None)
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

    def subpixel(self, window):
        """Stripe order of the outputs `window` is on, for window_api.text_subpixel:
        'rgb' / 'bgr', 'off' for vertical stripes or outputs that disagree, None
        when no output says. wl_output.subpixel 0 (unknown) and 1 (none) both
        count as silence: Hyprland sends 1 for every output, LCD or not."""
        known = {self.outputs[o].subpixel for o in getattr(window, 'outputs', ()) if o in self.outputs} - {0, 1}
        if not known:
            return None
        return {2: 'rgb', 3: 'bgr'}.get(known.pop(), 'off') if len(known) == 1 else 'off'

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
        if window.surface:
            native = self.native
            if FINISH and self.late:
                self.finish()
            if self.presentation is not None:
                # When this frame reaches the screen (`presented`): what `get_presentation_latency_ms` reports.
                proxy = native.request(self.presentation, 1, P(window.surface), P(), interface='wp_presentation_feedback')
                native.listen(proxy, self.feedback_table, None)
                # A frame handed over after its guard began is not one to judge the guard by.
                judged = self.aim if self.aim is None or time.clock_gettime(self.clock) <= self.due + SLIP else None
                self.feedbacks[proxy] = window, self.sampled or time.clock_gettime(self.clock), judged
            if window.frame_due is None:
                # The compositor says when it wants the next frame: `pace` waits for that.
                window.frame_due = native.request(window.surface, 3, P(), interface='wl_callback')
                native.listen(window.frame_due, self.frame_table, None)
                self.callbacks_due[window.frame_due] = window
                window.frame_asked = time.monotonic()
            if self.sampled:
                now = time.clock_gettime(self.clock)
                window.costs = (window.costs + [(now, now - self.sampled)])[-COSTS:]
        self.egl_check(self.egl.eglSwapBuffers(self.egl_display, window.egl_surface), 'eglSwapBuffers')

    def pace(self, drawing=lambda: True):
        """Call before the input of a frame is read. The frame is then drawn from input that is as
        fresh as it can be when the compositor takes it:

        1. wait until the compositor has asked for the next frame of every window swapped since
           (its frame callback), handling events meanwhile. This replaces the wait `eglSwapBuffers`
           would do after the frame is drawn, with input already read;
        2. when `drawing()` says a frame is wanted, and `LATE`, wait on so that the frame, drawn in
           the time the last ones took, is handed over the guard before the first refresh it can
           make. The refreshes are counted on from the last one a frame was presented at.

        The price of 2 is a frame that takes much longer to draw than the ones before it: it
        misses its refresh and shows one later. The guard tunes itself to how often frames miss (`tune`).
        Levers: the constants at the top of this file."""
        waiting = [window for window in self.windows if window.frame_due is not None]
        if waiting:
            until = min(window.frame_asked for window in waiting) + WAIT
            while any(window.frame_due is not None for window in waiting):
                left = until - time.monotonic()
                if left <= 0:
                    break
                self.wait_events_timeout(left)
        self.late, self.aim = False, None
        if LATE and drawing():
            now = time.clock_gettime(self.clock)
            # Windows with a refresh known from lately: after a pause the compositor draws at once.
            live = [window for window in self.windows if window.costs and 0 <= now - window.shown < 4 * self.period(window)]
            if live:
                period = max(self.period(window) for window in live)
                cost = sum(self.cost(window, now) for window in live)
                guard = self.tune(period, now) if ADAPT else GUARD
                shown = max(window.shown for window in live)
                # The first refresh the frame can make, counted on from the last one a frame
                # was on screen at: drawn in `cost` and handed over `guard` before it. That
                # may be the one after next (guard and cost together longer than a period, or
                # a compositor that asks for a frame only as it draws for the next refresh):
                # the frame then starts before the refresh in between, and still one frame
                # goes out per refresh as long as drawing one takes less than a period.
                # A frame slower than that starts at once, as without a late start.
                if cost < period:
                    aim = shown + math.ceil((now + guard + cost - shown) / period) * period
                    start = aim - guard - cost
                    self.late, self.aim, self.due = True, aim, aim - guard
                    while True:
                        left = start - time.clock_gettime(self.clock)
                        if left <= 0:
                            break
                        self.wait_events_timeout(left)
        self.sampled = time.clock_gettime(self.clock)

    def cost(self, window, now):
        """Seconds to allow for drawing the window's next frame: the `COST` share of its frames of
        the last `FRESH` seconds (the newest one when none is that fresh)."""
        costs = sorted(cost for at, cost in window.costs if now - at < FRESH) or [window.costs[-1][1]]
        return costs[int((len(costs) - 1) * COST)]

    def tune(self, period, now, missed=None):
        """The guard for `period` (base plus what is left of the lift), moved when due (levers and
        the rule: top of this file). `missed` is how a frame handed over in time did when it was
        presented; None only reads the guard and eases the base when due."""
        floor, longest = min(GUARD, period / 2), period * LONG
        if abs(period - self.paced) > .05 * period:
            self.base, self.paced, self.deaf = floor, period, now + CALM
            self.lift, self.hit, self.since, self.missed = 0., 0., 0., []
            self.settled, self.rest, self.failed, self.held = now + CALM, REST, 0., None
        lift = self.lift * max(0., 1. - (now - self.hit) / FADE)
        if missed and now >= self.deaf:
            self.missed = [at for at in self.missed if now - at < RECENT] + [now]
            if len(self.missed) >= MISSES:
                if self.held is not None:      # easing found a length that misses: back, and rest longer when it is the same again
                    self.rest = min(self.rest * 2, REST_MAX) if abs(self.base - self.failed) < MISS else REST
                    self.failed, self.base, self.held = self.base, min(self.base + MISS, self.held), None
                    self.settled, self.since = now + self.rest, now
                elif now - self.hit >= AGAIN:
                    self.since = now           # a first lift: a disturbance until it keeps coming back
                elif now - self.since >= LEARN:
                    self.base, self.since, self.settled = min(self.base + MISS, longest), now, now + self.rest
                lift += MISS
                self.lift, self.hit, self.missed, self.deaf = lift, now, [], now + SETTLE
        elif now >= self.settled and self.base > floor:
            if self.held is None:
                self.held = self.base
            self.base, self.settled = max(self.base - STEP, floor), now + CALM
        self.guard = min(self.base + lift, longest)
        return self.guard

    def period(self, window):
        """Seconds between two refreshes of the window's output: as the compositor reports it with
        a presented frame, else from the mode of the output the window is on. Never measured from
        the frames themselves: frames that miss a refresh, or aim at every second one, would
        stretch it, and a period taken too long spaces the frames out further."""
        if window.refresh:
            return window.refresh
        rates = [self.outputs[output].refresh for output in window.outputs if output in self.outputs]
        return 1000 / max(rates) if rates and max(rates) > 0 else 1 / 120

    def frame_done(self, proxy, stamp):
        window = self.callbacks_due.pop(proxy, None)
        self.native.destroy(proxy)
        if window is not None and window.frame_due == proxy:
            window.frame_due, window.frame_at = None, time.monotonic()

    def clock_id(self, clock):
        self.clock = clock

    def presented(self, proxy, high, low, nanoseconds, refresh, sequence_high, sequence_low, flags):
        window, sampled, aim = self.feedbacks.pop(proxy, (None, 0., None))
        self.native.destroy(proxy)
        if window is not None:
            shown = (high << 32 | low) + nanoseconds * 1e-9
            window.latency_ms = (shown - sampled) * 1000.
            window.refresh = refresh * 1e-9
            before, window.shown = window.shown, shown
            period = self.period(window)
            if aim is not None and ADAPT and aim - before < 1.5 * period:
                # Handed over in time for `aim`, the refresh after the frame before it, and on
                # screen a refresh or two later: a miss. (Later still: the compositor paused.)
                self.tune(period, shown, .5 * period < shown - aim < 2.5 * period)

    def discarded(self, proxy):
        self.feedbacks.pop(proxy, None)
        self.native.destroy(proxy)

    def get_presentation_latency_ms(self):
        """Input read to on screen, for the last presented frame of the window being drawn."""
        window = self.get_current_context()
        return window.latency_ms if window is not None else None

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
        for proxies in (self.callbacks_due, self.feedbacks):
            for proxy in [proxy for proxy, held in proxies.items() if (held[0] if isinstance(held, tuple) else held) is window]:
                del proxies[proxy]
                self.native.destroy(proxy)
        window.frame_due = None
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

    def poke(self):
        """`post_empty_event` for a thread without the interpreter: a byte
        written to this descriptor (`window_api.poke`)."""
        return 'fd', self.wake_writer.fileno()

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
        import meltygui.core.windowing.geometry_feed as geometry_feed
        rect = geometry_feed.surface_rect(window.title)
        return (int(rect[0]), int(rect[1])) if rect else (0, 0)

    def set_window_pos(self, window, x, y):
        import meltygui.core.windowing.geometry_feed as geometry_feed
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


# TEMPORARY (pacing debug, remove): trace this process's pacing to /tmp/pacedbg/live-<pid>.log while the flag file exists.
if os.path.exists('/tmp/pacedbg/on'):
    exec(open('/tmp/pacedbg/hooks.py').read(), {'__name__': 'pacedbg'})
