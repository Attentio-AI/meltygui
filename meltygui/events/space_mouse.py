"""
3D mouse (3Dconnexion SpaceMouse) input for the InputHandler.

The device is read through spacenavd's Unix socket, not the evdev node:
spacenavd grabs /dev/input/eventN exclusively (so the puck can't double as
a pointer), needs no group membership on our side, and applies the user's
/etc/spnavrc sensitivity / dead zone / axis inversion before we see anything.
The wire format is spacenavd's protocol v0 — the one every client gets
without a handshake: a stream of 32-byte frames, eight native int32s each:

    [0, x, y, z, rx, ry, rz, period_ms]   motion (raw units, ±FULL_DEFLECTION)
    [1, button, 0, ...]                   button press
    [2, button, 0, ...]                   button release

Three pieces, deliberately separate:

  - `_Reader`: one daemon thread per PROCESS (parked on sys._lsd_space_mouse
    so a studio restart or a hotswap never spawns a second one) that keeps the
    socket open, reconnects while spacenavd is away, and holds the LEVEL
    state — the latest deflection of the six axes, stamped with its arrival
    time — plus a queue of button edges. It never touches the handler.
  - `pump(handler)`: called once per frame beside the GLFW backend's pump. It
    samples the level state like the cursor position is sampled (level
    state, per frame: nothing is lost by not queueing every report) and
    feeds ONE axes event holding the deflection integrated over the frame —
    full-deflection-seconds, so a view's sensitivity is "per second at full
    push" and frame rate never changes how fast the camera moves.
  - The mapping helpers (`normalize`) — dead zone, axis swap / inversion,
    unit scale — pure functions over the raw tuple, tested offline.

Every knob is in Toggles.SpaceMouse (device side); what a view DOES with the
axes is that view's business (draw_voxels: the navigation knobs in the same Toggles.SpaceMouse).
"""

from __future__ import annotations
import socket
import struct
import sys
import threading
import time
import weakref
from collections import deque

from src.lsd.gl_gui.toggles import Toggles

# The input_id a view subscribes to: `space_mouse_changed(event` on a
# render_func signature. Buttons are `space_mouse_button_0_down` etc.
INPUT_ID = "space_mouse"
BUTTON_ID = "space_mouse_button_{}"
# Axis order on the wire and in event.axes.
AXES = ("tx", "ty", "tz", "rx", "ry", "rz")
# Blender's hand, measured against it on 09-04 (Lukas, spacenavd's stream on
# a SpaceMouse Wireless): translation passes straight through - push right,
# the volume shifts right; push forward, it comes closer - and every ROTATION
# is negated relative to the raw stream. Applied in normalize() so the axes
# reach views in OBJECT terms (voxel_camera.apply_space_mouse).
BLENDER_SIGNS = (1.0, 1.0, 1.0, -1.0, -1.0, -1.0)
_FRAME = struct.Struct("@8i")
FRAME_SIZE = _FRAME.size
_MOTION, _PRESS, _RELEASE = 0, 1, 2


def parse_frame(data: bytes):
    """One protocol-v0 frame → ("motion", (x, y, z, rx, ry, rz)) or
    ("button", index, pressed) or None for a frame type we don't know
    (skipped, the stream stays aligned since every frame is FRAME_SIZE)."""
    kind, a, b, c, d, e, f, _period = _FRAME.unpack(data)
    if kind == _MOTION:
        return "motion", (a, b, c, d, e, f)
    if kind == _PRESS or kind == _RELEASE:
        return "button", a, kind == _PRESS
    return None


def axis_sensitivities():
    """Toggles.SpaceMouse's six per-axis floats, in AXES order."""
    return (float(Toggles.SpaceMouse.tx_sensitivity), float(Toggles.SpaceMouse.ty_sensitivity),
            float(Toggles.SpaceMouse.tz_sensitivity), float(Toggles.SpaceMouse.rx_sensitivity),
            float(Toggles.SpaceMouse.ry_sensitivity), float(Toggles.SpaceMouse.rz_sensitivity))


def normalize(raw, *, full_deflection=None, dead_zone=None, swap_yz=None,
              invert_axes=None, sensitivities=None):
    """Raw device units → (tx, ty, tz, rx, ry, rz) in full-deflection units
    (±1 at full push before the per-axis sensitivity), with
    Toggles.SpaceMouse's dead zone, y/z swap, Blender's hand (BLENDER_SIGNS),
    per-axis inversion and per-axis sensitivity applied (keyword overrides
    exist for the tests). The result is in OBJECT terms — the motion the
    view's content should make — so no consumer needs to know the hand."""
    full = float(Toggles.SpaceMouse.full_deflection if full_deflection is None else full_deflection)
    dead = float(Toggles.SpaceMouse.dead_zone if dead_zone is None else dead_zone)
    swap = Toggles.SpaceMouse.swap_yz if swap_yz is None else swap_yz
    invert = Toggles.SpaceMouse.invert_axes if invert_axes is None else invert_axes
    sens = axis_sensitivities() if sensitivities is None else sensitivities
    v = [float(r) / full for r in raw]
    if swap:
        v[1], v[2], v[4], v[5] = v[2], v[1], v[5], v[4]
    out = []
    for name, x, hand, gain in zip(AXES, v, BLENDER_SIGNS, sens):
        # Dead zone re-scaled so the live range still starts at 0 (no jump
        # across the threshold) and still reaches 1.0 at full push.
        mag = abs(x)
        if mag <= dead:
            x = 0.0
        elif dead > 0.0:
            x = (mag - dead) / (1.0 - dead) * (1.0 if x > 0 else -1.0)
        x = max(-1.0, min(1.0, x * hand)) * gain
        if name in invert:
            x = -x
        out.append(x)
    return tuple(out)


class _Reader:
    """The socket thread + level state. One per process."""

    def __init__(self):
        self.lock = threading.Lock()
        self.raw = (0, 0, 0, 0, 0, 0)     # latest motion frame, raw units
        self.stamp = 0.0                  # perf_counter of latest frame
        self.buttons = []                 # button (index, pressed) edges
        self.connected = False
        self.error = None
        self.frames = 0
        self._sock = None
        self._thread = threading.Thread(target=self._run, name="space-mouse", daemon=True)
        self._thread.start()

    # ── the thread ──────────────────────────────────────────────────────────
    def _run(self):
        while True:
            if not Toggles.SpaceMouse.enabled:
                self._drop()
                time.sleep(0.5)
                continue
            if self._sock is None and not self._connect():
                time.sleep(float(Toggles.SpaceMouse.retry_s))
                continue
            try:
                data = self._sock.recv(FRAME_SIZE * 64)
            except socket.timeout:
                continue
            except OSError as e:
                self.error = str(e)
                self._drop()
                continue
            if not data:
                self.error = "spacenavd closed the socket"
                self._drop()
                continue
            self._feed(data)

    def _connect(self):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(str(Toggles.SpaceMouse.socket_path))
        except OSError as e:
            self.error = str(e)
            return False
        self._sock = s
        self._buffer = b""
        self.connected = True
        self.error = None
        return True

    def _drop(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.connected = False
        with self.lock:
            self.raw = (0, 0, 0, 0, 0, 0)

    def _feed(self, data: bytes):
        buf = self._buffer + data
        pos = 0
        now = time.perf_counter()
        moved = False
        while len(buf) - pos >= FRAME_SIZE:
            frame = parse_frame(buf[pos:pos + FRAME_SIZE])
            pos += FRAME_SIZE
            self.frames += 1
            if frame is None:
                continue
            if frame[0] == "motion":
                with self.lock:
                    self.raw = frame[1]
                    self.stamp = now
                moved = True
            else:
                with self.lock:
                    self.buttons.append((frame[1], frame[2]))
                moved = True
        self._buffer = buf[pos:]
        if moved:
            # Wake the render loop (glfw.wait_events otherwise sleeps until
            # a pointer/keyboard event): the next frame's pump samples us.
            _wake()


def _wake():
    try:
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()
    except Exception:
        pass


def reader() -> _Reader:
    """The process-lifetime reader, created on first use."""
    r = getattr(sys, "_lsd_space_mouse", None)
    if r is None:
        r = _Reader()
        sys._lsd_space_mouse = r
    return r


def start():
    """Start reading (idempotent). Called from Melty.init_input_backend."""
    reader()


# Per-frame sampling state: the last pump time and the axes last fed, so a
# release still delivers one final zero event (the view sees a gesture end).
_PUMP = globals().get("_PUMP") or {"last": 0.0, "active": False, "buttons_down": set(),
                                   "status": None, "frame_ms": 0.0}
# (pump time, reader.frames) samples for the report-rate estimate in stats().
# Counted on the PUMP side, not in the reader thread: the reader is a
# process-lifetime object bound to whatever class it was born with, so a
# reload swap of this file never changes the code its thread runs: a stamp field
# filled by _feed caused 0 Hz after a swap (Lukas 09-04). The frames counter is
# the one thing every reader version has always kept.
_RATE = globals().get("_RATE") or deque(maxlen=128)
# Views that show the reading location (draw_space_mouse) without being the
# hovered event target: their draw_states, invalidated in the pump on every
# active frame, once more on release, and when the connection status flips.
_WATCHERS: "weakref.WeakSet" = globals().get("_WATCHERS") or weakref.WeakSet()


def watch(draw_state):
    """Register a draw_state to be invalidated whenever the reading moves."""
    _WATCHERS.add(draw_state)


def _invalidate_watchers():
    for draw_state in list(_WATCHERS):
        try:
            draw_state.invalidate()
        except Exception:
            pass


def pump(handler, now: float = None):
    """Per frame: integrate the level state over the frame and feed the
    handler. `now` is injectable for the tests."""
    r = getattr(sys, "_lsd_space_mouse", None)
    if r is None:
        return
    now = time.perf_counter() if now is None else now
    dt = now - _PUMP["last"]
    _PUMP["last"] = now
    _PUMP["frame_ms"] = dt * 1000.0
    _RATE.append((now, int(getattr(r, "frames", 0))))
    dt = max(0.0, min(dt, float(Toggles.SpaceMouse.max_frame_dt)))
    with r.lock:
        raw, stamp = r.raw, r.stamp
        buttons, r.buttons = r.buttons, []
    # A stuck level (spacedavd died mid-push, a dropped release frame) should
    # never keep the camera moving: past stale_s the reading is zero.
    if now - stamp > float(Toggles.SpaceMouse.stale_s):
        raw = (0, 0, 0, 0, 0, 0)
    axes = normalize(raw)
    active = any(a != 0.0 for a in axes)
    if active or _PUMP["active"]:
        handler.feed_axes(INPUT_ID, tuple(a * dt for a in axes), now)
        _invalidate_watchers()
    _PUMP["active"] = active
    current_status = (r.connected, r.error)
    if current_status != _PUMP["status"]:
        _PUMP["status"] = current_status
        _invalidate_watchers()
    if active:
        _wake()   # keep frames coming while the puck is pressed
    for index, pressed in buttons:
        input_id = BUTTON_ID.format(index)
        if pressed:
            _PUMP["buttons_down"].add(input_id)
            handler.feed_down(input_id, t=now)
        elif input_id in _PUMP["buttons_down"]:
            _PUMP["buttons_down"].discard(input_id)
            handler.feed_up(input_id, t=now)


def active() -> bool:
    """True while the cap is deflected (as of the last pump) — the studio's
    gesture flag: Melty folds it into on_drag so the churn suppression a
    mouse drag gets (hover invalidation, occluder diffs, content-height
    commits, RenderHost draws) holds for a 3D-mouse flight too."""
    return bool(_PUMP["active"])


def stats(now: float = None) -> dict:
    """Latency diagnostics: `rate_hz` = the device's motion-report rate over
    the last reports, `age_ms` = how old the newest report is at this call,
    `frame_ms` = the interval between the last two pumps (the studio's frame
    time while the cap is held). Latency = age + the frame + presentation."""
    r = getattr(sys, "_lsd_space_mouse", None)
    now = time.perf_counter() if now is None else now
    if r is None:
        return {"rate_hz": 0.0, "age_ms": 0.0, "frame_ms": _PUMP["frame_ms"]}
    with r.lock:
        stamp = r.stamp
    # Reports per second in the pump samples of the last second: the
    # counter delta over the time delta, so reports between the frames all
    # count (a per-frame stamp compare would fold them into one).
    rate = 0.0
    recent = [(t, n) for t, n in _RATE if now - t <= 1.0]
    if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
        rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
    age = (now - stamp) * 1000.0 if stamp else 0.0
    return {"rate_hz": rate, "age_ms": age, "frame_ms": _PUMP["frame_ms"]}


def status() -> str:
    """One line for a status row / the console."""
    r = getattr(sys, "_lsd_space_mouse", None)
    if r is None:
        return "not started"
    if r.connected:
        return f"connected ({r.frames} frames)"
    return f"disconnected: {r.error or 'connecting'}"
