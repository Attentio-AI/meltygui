"""Where is the studio's window on the screen? — the GNOME extension's feed.

A Wayland client never learns its own screen position. The GNOME Shell
extension shipped in gl_gui/gnome_extension (installed by
installation_helper.py) publishes every window's frame rect and the
monitors' work areas on the session bus, org.latentdescent.WindowGeometry.
This module is the studio's reader: one background thread owns a
GDBusConnection through libgio (ctypes — the venv has no D-Bus library),
subscribes to the extension's signals for OUR pid (Watch), and keeps the
latest frame rect + work area in _STATE for the render thread to read
(`frame_rect()`, `workarea()`). The OS-edge physics (gl_gui/os_frame.py)
is the consumer: with the position known, the screen edges are honest
collision walls for the studio's own window edges.

Coordinates are the compositor's LOGICAL pixels. The frame rect is the xdg
window geometry — the studio sets that to its content rect (titlebar
.sync_window_geometry), so `frame_rect()` is the content's screen rect.

Unavailable (no extension, X11, no bus): `available()` is False and every
reader returns None; the thread retries the subscription every
RETRY_SECONDS. Nothing here ever blocks the render thread.
"""
import ctypes
import os
import re
import threading
import time

BUS_NAME = "org.latentdescent.WindowGeometry"
OBJECT_PATH = "/org/latentdescent/WindowGeometry"
INTERFACE = "org.latentdescent.WindowGeometry"
# The studio's app id (glfw WAYLAND_APP_ID / X11 class in lsd_studio.py);
# the launcher shares our pid, so the class picks the studio's window.
WM_CLASS = "lsd-studio"
RETRY_SECONDS = 5.0
CALL_TIMEOUT_MS = 2000

# Survives hotswap (module re-exec reuses the existing dict).
_STATE = globals().get("_STATE") or {
    "thread": None, "running": False, "available": False, "error": None,
    "pid": None, "frame": None, "monitors": None, "workarea": None,
    "updates": 0, "loop": None,
}

_G_BUS_TYPE_SESSION = 2

_lib = None


class _GError(ctypes.Structure):
    _fields_ = [("domain", ctypes.c_uint32), ("code", ctypes.c_int), ("message", ctypes.c_char_p)]


_SIGNAL_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                              ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)


def _libs():
    """(gio, glib) with the handful of signatures this module calls."""
    global _lib
    if _lib is not None:
        return _lib
    gio = ctypes.CDLL("libgio-2.0.so.0")
    glib = ctypes.CDLL("libglib-2.0.so.0")
    P = ctypes.c_void_p
    gio.g_bus_get_sync.argtypes = [ctypes.c_int, P, ctypes.POINTER(ctypes.POINTER(_GError))]
    gio.g_bus_get_sync.restype = P
    gio.g_dbus_connection_call_sync.argtypes = [P, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                                                ctypes.c_char_p, P, P, ctypes.c_int, ctypes.c_int, P,
                                                ctypes.POINTER(ctypes.POINTER(_GError))]
    gio.g_dbus_connection_call_sync.restype = P
    gio.g_dbus_connection_signal_subscribe.argtypes = [P, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                                                       ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
                                                       _SIGNAL_CB, P, P]
    gio.g_dbus_connection_signal_subscribe.restype = ctypes.c_uint
    gio.g_dbus_connection_signal_unsubscribe.argtypes = [P, ctypes.c_uint]
    gio.g_dbus_connection_signal_unsubscribe.restype = None
    glib.g_variant_parse.argtypes = [P, ctypes.c_char_p, ctypes.c_char_p, P,
                                     ctypes.POINTER(ctypes.POINTER(_GError))]
    glib.g_variant_parse.restype = P
    glib.g_variant_type_new.argtypes = [ctypes.c_char_p]
    glib.g_variant_type_new.restype = P
    glib.g_variant_type_free.argtypes = [P]
    glib.g_variant_print.argtypes = [P, ctypes.c_int]
    glib.g_variant_print.restype = P            # char* - must g_free
    glib.g_variant_unref.argtypes = [P]
    glib.g_free.argtypes = [P]
    glib.g_error_free.argtypes = [P]
    glib.g_main_context_new.restype = P
    glib.g_main_context_push_thread_default.argtypes = [P]
    glib.g_main_context_pop_thread_default.argtypes = [P]
    glib.g_main_context_unref.argtypes = [P]
    glib.g_main_loop_new.argtypes = [P, ctypes.c_int]
    glib.g_main_loop_new.restype = P
    glib.g_main_loop_run.argtypes = [P]
    glib.g_main_loop_quit.argtypes = [P]
    glib.g_main_loop_unref.argtypes = [P]
    _lib = (gio, glib)
    return _lib


# ---------------------------------------------------------------------------
# GVariant text ↔ Python - the a{sv} dicts the extension speaks
# ---------------------------------------------------------------------------

def parse_variant_dicts(text):
    """The a{sv} dicts of a GVariant text form as Python dicts (ints,
    floats, bools and strings — everything GetWindows / GetMonitors carry)."""
    dicts = []
    for chunk in re.findall(r"\{([^{}]*)\}", text or ""):
        entry = {}
        for key, raw in re.findall(r"'(\w+)':\s*<([^>]*)>", chunk):
            raw = raw.strip()
            # a typed literal: <uint64 42>, <int32 -3>, <double 1.5>
            match = re.fullmatch(r"(?:u?int(?:16|32|64)|byte|double)\s+(\S+)", raw)
            if match:
                raw = match.group(1)
            if raw in ("true", "false"):
                entry[key] = raw == "true"
            elif re.fullmatch(r"-?\d+", raw):
                entry[key] = int(raw)
            elif re.fullmatch(r"-?\d+\.\d*(?:e[-+]?\d+)?", raw):
                entry[key] = float(raw)
            else:
                entry[key] = raw.strip("'\"")
        if entry:
            dicts.append(entry)
    return dicts


def pick_window(windows, pid, wm_class=WM_CLASS):
    """Our window among the feed's: the pid's window of our class, else the
    pid's first window."""
    mine = [w for w in windows or () if w.get("pid") == pid]
    for w in mine:
        if w.get("wm_class") == wm_class:
            return w
    return mine[0] if mine else None


# ---------------------------------------------------------------------------
# Our bus
# ---------------------------------------------------------------------------

class _Bus:
    """One GDBusConnection on the feed thread."""

    def __init__(self):
        self.gio, self.glib = _libs()
        self.conn = None

    def connect(self):
        err = ctypes.POINTER(_GError)()
        self.conn = self.gio.g_bus_get_sync(_G_BUS_TYPE_SESSION, None, ctypes.byref(err))
        if not self.conn:
            raise RuntimeError(self._take(err))
        return self.conn

    def _take(self, err):
        message = "unknown GError"
        if err:
            message = (err.contents.message or b"").decode("utf-8", "replace")
            self.glib.g_error_free(err)
        return message

    def call(self, method, params_text=None, params_type=None):
        """Call ``method`` on the extension; returns the reply's text form."""
        params = None
        if params_text is not None:
            err = ctypes.POINTER(_GError)()
            vtype = self.glib.g_variant_type_new(params_type.encode()) if params_type else None
            params = self.glib.g_variant_parse(vtype, params_text.encode(), None, None, ctypes.byref(err))
            if vtype:
                self.glib.g_variant_type_free(vtype)
            if not params:
                raise RuntimeError(f"bad params {params_text!r}: {self._take(err)}")
        err = ctypes.POINTER(_GError)()
        reply = self.gio.g_dbus_connection_call_sync(
            self.conn, BUS_NAME.encode(), OBJECT_PATH.encode(), INTERFACE.encode(), method.encode(),
            params, None, 0, CALL_TIMEOUT_MS, None, ctypes.byref(err))
        if not reply:
            raise RuntimeError(self._take(err))
        try:
            return self.text_of(reply)
        finally:
            self.glib.g_variant_unref(reply)

    def text_of(self, variant):
        raw = self.glib.g_variant_print(variant, 0)
        try:
            return ctypes.string_at(raw).decode("utf-8", "replace")
        finally:
            self.glib.g_free(raw)


def _apply_windows(text, pid):
    win = pick_window(parse_variant_dicts(text), pid)
    if win is not None:
        _STATE["frame"] = win
        _STATE["updates"] += 1
    return win


def _apply_monitors(text):
    monitors = parse_variant_dicts(text)
    _STATE["monitors"] = monitors
    _refresh_workarea()


def _refresh_workarea():
    frame, monitors = _STATE["frame"], _STATE["monitors"]
    if not monitors:
        return
    index = frame.get("monitor", 0) if frame else 0
    mon = next((m for m in monitors if m.get("index") == index), monitors[0])
    _STATE["workarea"] = (mon["work_x"], mon["work_y"], mon["work_width"], mon["work_height"])


def _on_signal(_conn, _sender, _path, _iface, name, params, _data):
    """Feed-thread callback for the extension's signals."""
    try:
        bus = _STATE.get("bus")
        if bus is None:
            return
        name = (name or b"").decode()
        text = bus.text_of(params) if params else ""
        if name == "Geometry":
            dicts = parse_variant_dicts(text)
            if dicts and dicts[0].get("pid") == _STATE["pid"]:
                win = dicts[0]
                cur = _STATE["frame"]
                # a second window of ours (the launcher) never displaces
                # the studio's
                if cur is None or win.get("id") == cur.get("id") or win.get("wm_class") == WM_CLASS:
                    _STATE["frame"] = win
                    _STATE["updates"] += 1
                    _refresh_workarea()
        elif name == "MonitorsChanged":
            _apply_monitors(bus.call("GetMonitors"))
        elif name == "Removed":
            cur = _STATE["frame"]
            match = re.match(r"\((?:uint64 )?(\d+),", text)
            if cur is not None and match and int(match.group(1)) == cur.get("id"):
                _STATE["frame"] = None
    except Exception as ex:          # never let a callback escape into C
        _STATE["error"] = str(ex)


_signal_cb = _SIGNAL_CB(_on_signal)


def _thread_main(pid):
    gio, glib = _libs()
    ctx = glib.g_main_context_new()
    glib.g_main_context_push_thread_default(ctx)
    try:
        while _STATE["running"]:
            try:
                bus = _Bus()
                bus.connect()
                _STATE["bus"] = bus
                sub = gio.g_dbus_connection_signal_subscribe(
                    bus.conn, BUS_NAME.encode(), INTERFACE.encode(), None, OBJECT_PATH.encode(),
                    None, 0, _signal_cb, None, None)
                # Watch first (the subscription is live from here), then the
                # snapshot: a move between the two is caught by the signal.
                bus.call("Watch", f"({int(pid)},)", "(i)")
                _apply_monitors(bus.call("GetMonitors"))
                _apply_windows(bus.call("GetWindows", f"({int(pid)},)", "(i)"), pid)
                _STATE["available"] = True
                _STATE["error"] = None
                loop = glib.g_main_loop_new(ctx, 0)
                _STATE["loop"] = loop
                glib.g_main_loop_run(loop)
                _STATE["loop"] = None
                glib.g_main_loop_unref(loop)
                gio.g_dbus_connection_signal_unsubscribe(bus.conn, sub)
                try:
                    bus.call("Unwatch", f"({int(pid)},)", "(i)")
                except Exception:
                    pass
                return
            except Exception as ex:
                _STATE["available"] = False
                _STATE["error"] = str(ex)
                _STATE["bus"] = None
                deadline = time.monotonic() + RETRY_SECONDS
                while _STATE["running"] and time.monotonic() < deadline:
                    time.sleep(0.25)
    finally:
        glib.g_main_context_pop_thread_default(ctx)
        glib.g_main_context_unref(ctx)
        _STATE["available"] = False


def start(pid=None):
    """Start the reader thread (idempotent)."""
    if _STATE["thread"] is not None and _STATE["thread"].is_alive():
        return _STATE["thread"]
    _STATE["pid"] = os.getpid() if pid is None else int(pid)
    _STATE["running"] = True
    thread = threading.Thread(target=_thread_main, args=(_STATE["pid"],),
                              name="window-geometry-feed", daemon=True)
    _STATE["thread"] = thread
    thread.start()
    return thread


def stop():
    _STATE["running"] = False
    loop = _STATE.get("loop")
    if loop:
        _libs()[1].g_main_loop_quit(loop)


def available():
    return bool(_STATE["available"] and _STATE["frame"])


def last_error():
    return _STATE["error"]


def frame_rect():
    """(x, y, width, height) of the studio's content rect on the screen, or
    None while the feed is unavailable."""
    frame = _STATE["frame"]
    if not _STATE["available"] or frame is None:
        return None
    return (frame["x"], frame["y"], frame["width"], frame["height"])


def workarea():
    """(x, y, width, height) of the work area of the monitor holding the
    studio, or None."""
    if not _STATE["available"]:
        return None
    return _STATE["workarea"]


def updates():
    """Monotone count of frame-rect updates seen — cheap change detection."""
    return _STATE["updates"]
