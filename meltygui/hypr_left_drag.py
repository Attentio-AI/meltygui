"""The desktop's left-drag-move toggle, mirrored in melty's own title bar.

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
}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def window_class():
    """The class Hyprland files this process's window under: what the feed
    saw (`j/clients` → wm_class), else the app's declared id (a melty app's
    Surface.app_id), else the studio's own (geometry_feed.WM_CLASS)."""
    from src.lsd.gl_gui import geometry_feed
    frame = geometry_feed._STATE.get("frame") if isinstance(geometry_feed._STATE, dict) else None
    if frame and frame.get("wm_class"):
        return frame["wm_class"]
    try:
        from src.lsd.gl_gui.surface import Surface
        if Surface.all:
            return Surface.app_id
    except Exception:
        pass
    return geometry_feed.WM_CLASS


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
    from src.lsd.gl_gui import geometry_feed
    if geometry_feed.backend() != "hyprland":
        return False, False, ""
    kind, value = _getoption(OPTION, request)
    if kind is None:
        return False, False, ""
    wm_class = window_class()
    if not value:
        return True, False, wm_class          # the feature itself is off
    _kind, exclude = _getoption(EXCLUDE_OPTION, request)
    return True, not class_excluded(str(exclude or ""), wm_class), wm_class


# ---------------------------------------------------------------------------
# Probe thread
# ---------------------------------------------------------------------------

def _probe():
    try:
        available, enabled, wm_class = detect()
    except Exception:
        available, enabled, wm_class = False, False, ""
    changed = (available, enabled) != (_state["available"], _state["enabled"])
    _state.update(available=available, enabled=enabled, wm_class=wm_class,
                  probed_at=time.monotonic())
    if changed:
        try:
            from src.lsd.gl_gui.utils.glfw_utils import request_render
            request_render()
        except Exception:
            pass


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
    """Left-drag move applies to this window's class (as of the last probe)."""
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
        run([TOGGLE_SCRIPT, wm_class], close_fds=False, capture_output=True, timeout=10.0)
    except Exception as ex:
        print(f"[hypr_left_drag] {TOGGLE_SCRIPT} {wm_class!r}: {ex}", flush=True)
    finally:
        _state["toggling"] = False
    _probe()


def toggle(run=None):
    """Flip left-drag move for this window's class through the desktop's
    own script (it edits the exclude list, persists it, refreshes hyprbars
    and notifies). The button flips at once (the probe after the script
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
