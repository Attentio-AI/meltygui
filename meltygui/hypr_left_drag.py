"""The desktop's window-gesture toggle, mirrored in melty's own title bar.

Lukas's patched Hyprland (~/.local/opt/hyprland-hdr, `general:left_drag_move`)
moves a floating window by a plain left drag on its empty space, and its
hyprbars title bars carry a stateful button (`state = "left_drag_move"`)
that shows whether that applies to the window's app and toggles it — the
app's class goes in / out of `general:left_drag_move_exclude`, live and
persisted through the desktop's Settings overrides, by
`desktop/left-drag-toggle`. melty's frameless windows draw their own
controls (titlebar.py), so on that desktop they get the same button:

    available()   the compositor has the option (a stock Hyprland answers
                  "no such option"; GNOME / KDE have no socket at all)
    enabled()     left-drag move applies to THIS window's class right now
    toggle()      runs the desktop's own script for the class — one source
                  of truth, one notification, the hyprbars refresh included

The button owns BOTH compositor gestures for this app's class (09-13):
plain left-drag MOVE (general:left_drag_move) and right-drag RESIZE
(general:right_drag_resize). Lit = the compositor drives them; faded = the
class is excluded from both and the melty app does them itself — its
drag-anywhere xdg_toplevel.move (titlebar.drag_anywhere_enabled) and its
right-drag resize through the edge physics (os_frame), which is also what
lets nested melty windows keep their own right-drag (the compositor's
resize swallows the press otherwise). The studio is excluded statically in
hyprland.lua; apps flip through this button. `toggle()` runs the script
with `--gestures`, which moves the class through both exclude lists; a
probe that finds the right list out of step with the left one (a hyprbars
button or the Settings app edited only the left) runs `--gestures --sync`
once to align it (`resize_synced`).

Both reads are `getoption` requests on Hyprland's socket
(geometry_feed.hypr_request, ~0.05 ms) made on a daemon thread, the same
shape as titlebar_buttons: `state()` answers "unknown" until the first
probe lands, a change requests a frame, `refresh_if_stale` re-probes at
Toggles.Melty.titlebar_button_refresh_s (the desktop's Settings app can
flip the option too), `refresh()` after a focus gain. The toggle script
is spawned through posix_spawn (absolute path, close_fds=False, no cwd —
the studio never forks, CLAUDE.md) and a re-probe follows it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

OPTION = "general:left_drag_move"
EXCLUDE_OPTION = "general:left_drag_move_exclude"
RESIZE_OPTION = "general:right_drag_resize"
RESIZE_EXCLUDE_OPTION = "general:right_drag_resize_exclude"

# The desktop's toggle script: on PATH if the session exports it, else at
# the desktop's install root (config/hyprland.lua's `root`).
_DESKTOP_ROOT = os.path.expanduser("~/.local/opt/hyprland-hdr")
TOGGLE_SCRIPT = shutil.which("left-drag-toggle") \
    or os.path.join(_DESKTOP_ROOT, "desktop", "left-drag-toggle")

# Survives hotswap re-exec (module globals are reused).
_state = globals().get("_state") or {
    "available": None,      # None = not probed yet
    "enabled": False,
    "wm_class": "",
    "probed_at": 0.0,
    "thread": None,
    "toggling": False,
    "resize_synced": True,  # right_drag_resize_exclude list matches the move list
    "synced_for": None,     # (wm_class, enabled) the last --sync ran for: no loop on a failing script
}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def window_class():
    """The class Hyprland files this process's window under: what the feed
    saw (`j/clients` → wm_class), else the BOOTED melty app's id
    (app.boot's app_id — the GLFW app_id / X11 class hint; known before
    any Surface exists, which is when the first probe runs), else the
    app's declared Surface.app_id, else the studio's own
    (geometry_feed.WM_CLASS). The studio fallback is LAST and only for a
    process that never booted as an app: a probe that read an app as
    "lsd-studio" toggled and synced the wrong class (09-13)."""
    from src.lsd.gl_gui import geometry_feed
    frame = geometry_feed._STATE.get("frame") if isinstance(geometry_feed._STATE, dict) else None
    if frame and frame.get("wm_class"):
        return frame["wm_class"]
    try:
        from src.lsd.gl_gui import app
        if app._state.get("booted") and app._state.get("app_id"):
            return app._state["app_id"]
    except Exception:
        pass
    try:
        from src.lsd.gl_gui.surface import Surface
        if Surface.all:
            return Surface.app_id
    except Exception:
        pass
    return geometry_feed.WM_CLASS


def class_is_certain():
    """True when window_class() comes from something that names THIS
    process's window: the feed's frame or the booted app's id. The
    Surface.app_id default and the studio fallback are guesses — good
    enough to paint the button, never to WRITE the compositor's lists
    (an app probed before its Surface existed read itself as the studio
    and synced the studio's entry away, 09-13)."""
    from src.lsd.gl_gui import geometry_feed
    frame = geometry_feed._STATE.get("frame") if isinstance(geometry_feed._STATE, dict) else None
    if frame and frame.get("wm_class"):
        return True
    try:
        from src.lsd.gl_gui import app
        return bool(app._state.get("booted") and app._state.get("app_id"))
    except Exception:
        return False


def _getoption(name, request=None):
    """(kind, value) of a Hyprland option through the socket, or
    (None, None) when the option does not exist / no socket."""
    from src.lsd.gl_gui import geometry_feed
    request = request or geometry_feed.hypr_request
    try:
        reply = request(f"j/getoption {name}")
    except Exception:
        return None, None
    reply = reply.strip()
    if not reply.startswith("{"):
        return None, None
    try:
        data = json.loads(reply)
    except ValueError:
        # hyprctl-style replies don't escape quotes inside str values.
        match = re.search(r'"str":\s*"(.*)",\s*"set"', reply)
        return ("str", match.group(1)) if match else (None, None)
    for kind in ("bool", "int", "float", "str"):
        if kind in data:
            return kind, data[kind]
    return None, None


def class_excluded(exclude_text, wm_class):
    """True when `wm_class` fully matches one of the comma-separated
    regexes in the exclude option — the compositor's own rule, and
    desktop/left-drag-toggle's."""
    for entry in (exclude_text or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if re.fullmatch(entry, wm_class):
                return True
        except re.error:
            continue
    return False


def detect(request=None):
    """(available, enabled, wm_class) read from the compositor now."""
    return detect_gestures(request)[:3]


def detect_gestures(request=None):
    """(available, enabled, wm_class, resize_synced): the left-drag read plus
    whether the class's membership in right_drag_resize_exclude matches
    its left-list membership (True when the compositor has no
    right_drag_resize, or the feature is off — nothing to align)."""
    from src.lsd.gl_gui import geometry_feed
    if geometry_feed.backend() != "hyprland":
        return False, False, "", True
    kind, value = _getoption(OPTION, request)
    if kind is None:
        return False, False, "", True
    wm_class = window_class()
    if not value:
        return True, False, wm_class, True    # the feature itself is off
    _kind, exclude = _getoption(EXCLUDE_OPTION, request)
    enabled = not class_excluded(str(exclude or ""), wm_class)
    resize_kind, resize_on = _getoption(RESIZE_OPTION, request)
    if resize_kind is None or not resize_on:
        return True, enabled, wm_class, True
    _kind, resize_exclude = _getoption(RESIZE_EXCLUDE_OPTION, request)
    resize_excluded = class_excluded(str(resize_exclude or ""), wm_class)
    return True, enabled, wm_class, resize_excluded == (not enabled)


# ---------------------------------------------------------------------------
# Probe thread
# ---------------------------------------------------------------------------

def _probe(run=None):
    try:
        available, enabled, wm_class, resize_synced = detect_gestures()
    except Exception:
        available, enabled, wm_class, resize_synced = False, False, "", True
    changed = (available, enabled) != (_state["available"], _state["enabled"])
    _state.update(available=available, enabled=enabled, wm_class=wm_class,
                  resize_synced=resize_synced, probed_at=time.monotonic())
    if changed:
        try:
            from src.lsd.gl_gui.utils.glfw_utils import request_render
            request_render()
        except Exception:
            pass
    if available and not resize_synced and class_is_certain():
        _sync_resize(wm_class, enabled, run)


def _sync_resize(wm_class, enabled, run=None):
    """Align right_drag_resize_exclude with the left list for this class
    (the script's `--gestures --sync`), once per (class, state) — a script
    that fails must not be re-run every probe. Re-probes after."""
    if _state["synced_for"] == (wm_class, enabled) or not os.path.isfile(TOGGLE_SCRIPT):
        return
    _state["synced_for"] = (wm_class, enabled)
    run = run or subprocess.run
    try:
        run([TOGGLE_SCRIPT, "--gestures", "--sync", wm_class], close_fds=False,
            capture_output=True, timeout=10.0)
    except Exception as ex:
        print(f"[hypr_left_drag] {TOGGLE_SCRIPT} --gestures --sync {wm_class!r}: {ex}", flush=True)
        return
    try:
        _, _, _, resize_synced = detect_gestures()
    except Exception:
        return
    _state["resize_synced"] = resize_synced


def start_probe(force=False):
    """Read on a daemon thread (idempotent while one runs; a known answer
    is kept unless ``force``). Returns the thread, or None."""
    thread = _state["thread"]
    if thread is not None and thread.is_alive():
        return thread
    if _state["available"] is not None and not force:
        return None
    thread = threading.Thread(target=_probe, name="hypr-left-drag", daemon=True)
    thread.start()
    _state["thread"] = thread
    return thread


def available():
    """True on the patched desktop; False until the first probe lands
    (kicked here if nobody started it)."""
    if _state["available"] is None:
        start_probe()
        return False
    return bool(_state["available"])


def enabled():
    """The compositor's gestures (left-drag move, right-drag resize) apply
    to this window's class (as of the last probe). False = the melty app
    moves and resizes itself."""
    return bool(_state["enabled"])


def state():
    """"unknown" before the first probe, "off" without the feature,
    "on" / "excluded" with it — for the info tab / tests."""
    if _state["available"] is None:
        return "unknown"
    if not _state["available"]:
        return "off"
    return "on" if _state["enabled"] else "excluded"


def refresh(min_interval=2.0):
    """Re-read (a focus gain), at most once per ``min_interval`` seconds."""
    if time.monotonic() - _state["probed_at"] >= min_interval:
        start_probe(force=True)


def refresh_if_stale(interval):
    """Per-frame: re-read once the last probe is ``interval`` seconds old."""
    if interval and interval > 0:
        refresh(min_interval=float(interval))


# ---------------------------------------------------------------------------
# The toggle
# ---------------------------------------------------------------------------

def _run_toggle(wm_class, run=None):
    run = run or subprocess.run
    try:
        # posix_spawn: absolute executable, close_fds=False, no env / preexec_fn.
        run([TOGGLE_SCRIPT, "--gestures", wm_class], close_fds=False, capture_output=True, timeout=10.0)
    except Exception as ex:
        print(f"[hypr_left_drag] {TOGGLE_SCRIPT} --gestures {wm_class!r}: {ex}", flush=True)
    finally:
        _state["toggling"] = False
    _probe(run)


def toggle(run=None):
    """Flip the compositor's window gestures (left-drag move AND right-drag
    resize) for this window's class through the desktop's own script
    (`--gestures`: it edits both exclude lists, persists them, refreshes
    hyprbars and notifies). The button flips at once (the probe after the script
    confirms); a second click while one runs is ignored. Returns the
    worker thread, or None when nothing was started."""
    if not available() or _state["toggling"]:
        return None
    if not os.path.isfile(TOGGLE_SCRIPT):
        print(f"[hypr_left_drag] toggle script missing: {TOGGLE_SCRIPT}", flush=True)
        return None
    wm_class = _state["wm_class"] or window_class()
    _state["toggling"] = True
    _state["enabled"] = not _state["enabled"]
    thread = threading.Thread(target=_run_toggle, args=(wm_class, run),
                              name="hypr-left-drag-toggle", daemon=True)
    thread.start()
    return thread
