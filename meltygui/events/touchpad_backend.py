"""
OS-level three-finger click/drag for Linux touchpads (macOS-style).

Architecture — permanent grab + clone forwarding:

    real touchpad ──(EVIOCGRAB, ours exclusively)──► this daemon
        │  not latched: every event forwarded verbatim ──► virtual clone
        │              (libinput drives the cursor from the clone as normal)
        └─ latched (3-finger): events consumed; finger motion becomes
           REL_X/REL_Y + BTN_MIDDLE on a separate virtual pointer

libinput only ever sees the clone, whose state we keep consistent: when the
latch begins we synthesize a clean "all fingers lifted" frame on the clone,
so there is never a mid-gesture grab/ungrab that strands libinput with
phantom fingers (which froze pointer input in the grab-on-demand version).

Safety — the system must NEVER lose the mouse:
  - The kernel releases EVIOCGRAB automatically when our fd closes, for any
    exit including SIGKILL. A crash always returns the real touchpad.
  - Finger count comes from the hardware's own BTN_TOOL_* / BTN_TOUCH keys
    (authoritative), not our MT-slot bookkeeping, so the latch cannot stick
    on a miscount; BTN_TOUCH==0 force-releases regardless of other state.
  - A physical clickpad button press while latched is an emergency release
    (and the click passes through to the virtual pointer, so it still works).
  - A watchdog force-releases the latch (and, on repeated trouble, shuts the
    whole daemon down, closing fds → grab released) if the device goes
    silent mid-latch for WATCHDOG_S seconds.
  - Any exception path closes all fds in `finally`.

Gesture semantics (macOS "three finger drag"): middle press immediately on
3-finger contact (a quick tap is a middle click); lifting down to one finger
keeps the latch; releasing all fingers releases the button.

The virtual pointer declares BTN_LEFT/BTN_RIGHT alongside BTN_MIDDLE —
libinput refuses to treat a device without BTN_LEFT as a pointer, which is
why the first version's middle clicks never reached the compositor.
"""

from __future__ import annotations
import threading
import time

try:
    import evdev
    from evdev import InputDevice, UInput, ecodes as e
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

# Touchpad units → cursor pixels (pad ~20 units/mm).
SENSITIVITY = 0.55
ACCEL_MAX = 2.5
ACCEL_REF = 30.0   # units/s where acceleration saturates
WATCHDOG_S = 5.0   # latched + zero events for this long → force release
# Fingers land staggered (1→2→3 over a few frames). Events from touch-begin
# are held back: reach 3 fingers → latch and the clone never sees the touch;
# otherwise the buffer flushes and the touch proceeds normally. Before this
# the clone saw a brief 2-finger touch + our synthesized lift, which libinput
# read as a 2-finger tap → spurious RIGHT CLICKS.
# The flush trigger is MOTION, not time: a natural 3-finger touch often
# starts from 1–2 resting fingers with the last finger landing 200ms+ later,
# so any fixed window either turns 3-finger drags into 2-finger SCROLLS
# (flushed too early) or makes every scroll start sluggish (flushed too
# late). Stationary fingers carry no intent - hold them back indefinitely;
# actual travel (or a physical button press, or the touch ending as a tap)
# reveals the intent and flushes immediately.
MOVE_FLUSH = 15  # device units (~0.75mm) of travel that flushes the hold
DEBUG = False

_TOOL_COUNT = {}
if HAS_EVDEV:
    _TOOL_COUNT = {
        e.BTN_TOOL_FINGER: 1, e.BTN_TOOL_DOUBLETAP: 2,
        e.BTN_TOOL_TRIPLETAP: 3, e.BTN_TOOL_QUADTAP: 4,
        e.BTN_TOOL_QUINTTAP: 5,
    }


CLONE_TAG = "(melty)"


def find_touchpad():
    for path in evdev.list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        # NEVER pick one of our own virtual devices: a stale clone from
        # another still-running daemon (e.g. the model server's) matches the
        # MT capability check and only ever carries the events that daemon
        # forwards - grabbing it permanently breaks gesture detection.
        if CLONE_TAG in dev.name or "latent-descent" in dev.name:
            dev.close()
            continue
        caps = dev.capabilities()
        abs_codes = {c for c, _ in caps.get(e.EV_ABS, [])}
        if e.ABS_MT_SLOT in abs_codes and e.BTN_TOOL_FINGER in caps.get(e.EV_KEY, []):
            return dev
        dev.close()
    return None


def existing_clone_present():
    """A live clone means another process' daemon already owns the touchpad."""
    for path in evdev.list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        name = dev.name
        dev.close()
        if CLONE_TAG in name:
            return True
    return False


class ThreeFingerDrag:
    def __init__(self):
        self.dev: InputDevice | None = None
        self.clone: UInput | None = None
        self.pointer: UInput | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._latched = False
        self._last_event_t = 0.0

    # ------------------------------------------------------------- lifecycle

    def start(self) -> bool:
        if not HAS_EVDEV:
            print("touchpad_backend: evdev not installed, 3-finger drag disabled")
            return False
        if existing_clone_present():
            print("touchpad_backend: another 3-finger-drag daemon is already "
                  "running (clone device present) — not starting a second one")
            return False
        self.dev = find_touchpad()
        if self.dev is None:
            print("touchpad_backend: no multitouch touchpad found")
            return False
        try:
            # BTN_LEFT is mandatory for libinput to classify this as a pointer.
            self.pointer = UInput(
                {e.EV_REL: [e.REL_X, e.REL_Y],
                 e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]},
                name="latent-descent virtual pointer")
            # Clone of the touchpad - libinput's view of the pad from now on.
            self.clone = UInput.from_device(self.dev,
                                            name=f"{self.dev.name} (melty)")
            # Give udev/libinput a moment to pick the clone up BEFORE we grab
            # the real device, so there is no window with no working touchpad.
            time.sleep(0.5)
            self.dev.grab()
        except Exception as ex:
            print(f"touchpad_backend: setup failed ({ex}), 3-finger drag disabled")
            self._close_all()
            return False
        self._thread = threading.Thread(target=self._run, name="touchpad-3fd",
                                        daemon=True)
        self._thread.start()
        threading.Thread(target=self._watchdog, name="touchpad-3fd-watchdog",
                         daemon=True).start()
        print(f"touchpad_backend: 3-finger drag active on {self.dev.name}")
        return True

    def stop(self):
        self._stop.set()
        self._close_all()  # closing dev releases the grab and unblocks reading

    def _close_all(self):
        for attr in ("dev", "clone", "pointer"):
            obj = getattr(self, attr)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _watchdog(self):
        """Absolute backstop: a latch with a silent device is force-released;
        if the release itself can't be delivered, kill the daemon entirely —
        closing the fds drops the grab and hands the real pad back to libinput."""
        while not self._stop.is_set():
            time.sleep(1.0)
            if self._latched and time.monotonic() - self._last_event_t > WATCHDOG_S:
                print("touchpad_backend: watchdog releasing stale 3-finger latch")
                try:
                    self._release_latch()
                except Exception:
                    print("touchpad_backend: watchdog release failed — "
                          "shutting down, touchpad returns to system")
                    self.stop()
                    return

    # ------------------------------------------------------------- latch ops

    def _release_latch(self):
        if self._latched:
            self.pointer.write(e.EV_KEY, e.BTN_MIDDLE, 0)
            self.pointer.syn()
            self._latched = False

    def _lift_fingers_on_clone(self, touches, active_tool):
        """Tell libinput (via the clone) that all fingers left the pad, so the
        clone's state stays consistent while we consume the real events."""
        c = self.clone
        for slot in list(touches):
            c.write(e.EV_ABS, e.ABS_MT_SLOT, slot)
            c.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        if active_tool is not None:
            c.write(e.EV_KEY, active_tool, 0)
        c.write(e.EV_KEY, e.BTN_TOUCH, 0)
        c.syn()

    # ------------------------------------------------------------- main loop

    def _run(self):
        dev, clone, pointer = self.dev, self.clone, self.pointer
        touches: dict[int, list] = {}     # slot -> [x, y]
        slot = 0
        fingers = 0                       # from BTN_TOOL_* (authoritative)
        touching = False                  # BTN_TOUCH level
        active_tool = None
        frame: list = []                  # raw events of the current frame
        primary: tuple | None = None      # (slot, x, y) the drag follows
        rx = ry = 0.0
        # Touch-begin hold: frames withheld from the clone while we wait to
        # see if this touch becomes 3-finger (see MOVE_FLUSH).
        holding = False
        hold_buf: list = []               # withheld raw events (frames + SYNs)
        hold_origin: dict[int, tuple] = {}  # slot -> first (x, y) seen in hold
        hold_flush_now = False            # physical button seen mid-hold
        clone_touching = False            # does the clone believe fingers are down?

        def flush_hold():
            nonlocal holding, clone_touching
            for hev in hold_buf:
                clone.write_event(hev)
            if hold_buf:
                clone.syn()
                clone_touching = touching
            hold_buf.clear()
            holding = False

        try:
            for ev in dev.read_loop():
                if self._stop.is_set():
                    break
                self._last_event_t = time.monotonic()

                if ev.type == e.EV_ABS:
                    if ev.code == e.ABS_MT_SLOT:
                        slot = ev.value
                    elif ev.code == e.ABS_MT_TRACKING_ID:
                        if ev.value == -1:
                            touches.pop(slot, None)
                        else:
                            touches[slot] = [None, None]
                    elif ev.code == e.ABS_MT_POSITION_X and slot in touches:
                        touches[slot][0] = ev.value
                    elif ev.code == e.ABS_MT_POSITION_Y and slot in touches:
                        touches[slot][1] = ev.value
                elif ev.type == e.EV_KEY:
                    if ev.code in _TOOL_COUNT:
                        if ev.value:
                            fingers = _TOOL_COUNT[ev.code]
                            active_tool = ev.code
                            if DEBUG:
                                print(f"3fd: fingers={fingers}")
                        elif ev.code == active_tool:
                            active_tool = None
                            fingers = 0
                    elif ev.code == e.BTN_TOUCH:
                        if ev.value and not touching:
                            # Touch begin → start withholding from the clone.
                            holding = True
                            hold_buf.clear()
                            hold_origin.clear()
                            hold_flush_now = False
                        touching = bool(ev.value)
                    elif ev.code in (e.BTN_LEFT, e.BTN_RIGHT):
                        if self._latched:
                            # Physical clickpad press mid-latch: emergency
                            # release; the clickpad works via the pointer.
                            self._release_latch()
                            pointer.write(e.EV_KEY, ev.code, ev.value)
                            pointer.syn()
                        elif holding:
                            # A physical click must never be missed - flush
                            # the held touch so libinput sees it immediately.
                            hold_flush_now = True

                if not (ev.type == e.EV_SYN and ev.code == e.SYN_REPORT):
                    frame.append(ev)
                    continue

                # ---- end of frame: handle latch / hold / forward / consume ----
                if not self._latched and fingers >= 3 and touching:
                    if clone_touching:
                        # Touch was already flushed to the clone (3rd finger
                        # arrived after we began) - retract it cleanly.
                        self._lift_fingers_on_clone(touches, active_tool)
                        clone_touching = False
                    holding = False
                    hold_buf.clear()      # clone never sees the held prefix
                    pointer.write(e.EV_KEY, e.BTN_MIDDLE, 1)
                    pointer.syn()
                    self._latched = True
                    primary = None
                    rx = ry = 0.0
                    if DEBUG:
                        print("3fd: LATCH")
                elif self._latched and (not touching or not touches):
                    self._release_latch()
                    if DEBUG:
                        print("3fd: RELEASE")
                    frame = []   # clone already thinks fingers are up
                    continue

                if self._latched:
                    primary, rx, ry = self._track(pointer, touches, primary, rx, ry)
                elif holding:
                    hold_buf.extend(frame)
                    hold_buf.append(ev)   # keep the SYN so frames stay framed
                    # Flush when the touch reveals a non-3-finger intent: it
                    # ended (a tap), a physical button was pressed, or the
                    # fingers have traveled (cursor move / scroll).
                    # Stationary fingers stay held - the 3rd finger may still
                    # be on the way, however late.
                    moved = 0
                    for s, pos in touches.items():
                        if pos[0] is None or pos[1] is None:
                            continue
                        if s not in hold_origin:
                            hold_origin[s] = (pos[0], pos[1])
                        ox, oy = hold_origin[s]
                        moved = max(moved, abs(pos[0] - ox) + abs(pos[1] - oy))
                    if not touching or hold_flush_now or moved > MOVE_FLUSH:
                        flush_hold()
                else:
                    for fev in frame:
                        clone.write_event(fev)
                    clone.syn()
                    clone_touching = touching
                frame = []
        except OSError:
            pass  # device closed / unplugged / suspend - exit cleanly
        finally:
            try:
                self._release_latch()
            except Exception:
                pass
            self._close_all()  # closing dev releases the kernel grab

    @staticmethod
    def _track(pointer, touches, primary, rx, ry):
        """Follow one finger; re-anchor without a jump when it changes."""
        live = {s: p for s, p in touches.items()
                if p[0] is not None and p[1] is not None}
        if not live:
            return primary, rx, ry
        cur = min(live)
        x, y = live[cur]
        if primary is None or primary[0] != cur:
            return (cur, x, y), 0.0, 0.0
        dx, dy = x - primary[1], y - primary[2]
        if dx or dy:
            speed = max(abs(dx), abs(dy))
            scale = SENSITIVITY * min(ACCEL_MAX, 1.0 + speed / ACCEL_REF)
            rx += dx * scale
            ry += dy * scale
            ix, iy = int(rx), int(ry)
            rx -= ix
            ry -= iy
            if ix or iy:
                if ix:
                    pointer.write(e.EV_REL, e.REL_X, ix)
                if iy:
                    pointer.write(e.EV_REL, e.REL_Y, iy)
                pointer.syn()
        return (cur, x, y), rx, ry


def start_three_finger_drag():
    """Idempotent module-level starter (safe across hotswap re-exec)."""
    inst = globals().get("_instance")
    if inst is not None and inst._thread is not None and inst._thread.is_alive():
        return inst
    inst = ThreeFingerDrag()
    inst.start()
    globals()["_instance"] = inst
    return inst
