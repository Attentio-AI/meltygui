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

Hyprland backend (`backend() == "hyprland"`, picked when
HYPRLAND_INSTANCE_SIGNATURE names a live socket): no extension at all —
Hyprland's request socket ($XDG_RUNTIME_DIR/hypr/<sig>/.socket.sock, the
one `hyprctl` speaks) answers `j/clients` / `j/monitors` in ~0.03 ms with
every window's pid, class, position and size, so the same thread POLLS it
at Toggles.Melty.hyprland_feed_poll_hz (its event socket has no per-pixel
move / resize event for floating windows: `movewindow` there is a
workspace move). Two semantic differences from GNOME, both handled here:
Hyprland renders the WHOLE surface at `at` and ignores the xdg window
geometry (Renderer.cpp offsets popups only), so `at`/`size` are the
SURFACE rect and `frame_rect(inset=…)` shrinks it by the shadow margin to
get the content; and it ignores a toplevel's buffer offset, so a
client-side move goes through `hypr_set_box` / `hypr_move_window` over
the same socket (titlebar.apply_pending_surface_size) instead of
wayland_move.set_surface_offset. The 0.56 Lua config manager parses
`dispatch` as Lua (`hl.dsp.window.move{…}`; the classic text is a syntax
error) — `hypr_config_is_lua` probes which, and `hypr_set_box` folds the
resize + move into one `eval` anchored at the top-left, since Hyprland's
floating resize is centred.
"""
import ctypes
import json
import os
import re
import socket
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
    "updates": 0, "loop": None, "backend": None, "gen": 0, "lua": None,
    "geometry": None,
}

_G_BUS_TYPE_SESSION = 2

# Hyprland: the request socket hyprctl speaks is named by the instance
# signature every client of the session inherits.
HYPR_SIGNATURE_ENV = "HYPRLAND_INSTANCE_SIGNATURE"
HYPR_REQUEST_TIMEOUT_S = 0.25
HYPR_MONITORS_EVERY_S = 1.0


# The resolved socket is remembered for HYPR_SOCKET_RECHECK_S, so the
# per-frame backend() reads cost one dict lookup, not a connect.
HYPR_SOCKET_RECHECK_S = 2.0
_socket_resolution = globals().get("_socket_resolution")   # ((signature, hypr dir), path, checked_at)


def _hypr_runtime_dir():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "hypr")


def _socket_accepts(path, timeout=0.25):
    """Is a Hyprland listening on this unix socket? A stale socket FILE
    stays behind when an instance dies without cleaning up. One cheap
    `version` request (a bare connect-and-close makes the peer's reply
    fail on a broken pipe)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(b"version")
        sock.recv(4096)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def hyprland_socket_path():
    """The Hyprland request socket of the LIVE session, or None outside one.
    HYPRLAND_INSTANCE_SIGNATURE is the first candidate, but a process
    started from a shell that outlived a relogin (the launcher's tmux
    pane, 09-10) inherits the DEAD instance's signature — and that
    instance's socket file can still be there (a crash leaves it behind), so
    existence proves nothing: the env's socket wins if it accepts a
    connection, else the newest instance directory whose socket does, else
    the env's path as it is (unavailable, retried by the feed thread — a
    Hyprland mid-restart). Re-resolved every HYPR_SOCKET_RECHECK_S and
    whenever a request fails (`forget_socket`)."""
    global _socket_resolution
    signature = os.environ.get(HYPR_SIGNATURE_ENV)
    if not signature:
        return None
    now = time.monotonic()
    hypr_dir = _hypr_runtime_dir()
    cached = _socket_resolution
    if cached and cached[0] == (signature, hypr_dir) and now - cached[2] < HYPR_SOCKET_RECHECK_S:
        return cached[1]
    env_path = os.path.join(hypr_dir, signature, ".socket.sock")
    path = None
    if os.path.exists(env_path) and _socket_accepts(env_path):
        path = env_path
    else:
        try:
            others = [d for d in os.listdir(hypr_dir) if d != signature
                      and os.path.exists(os.path.join(hypr_dir, d, ".socket.sock"))]
        except OSError:
            others = []
        others.sort(key=lambda d: os.path.getmtime(os.path.join(hypr_dir, d)), reverse=True)
        for other in others:
            candidate = os.path.join(hypr_dir, other, ".socket.sock")
            if _socket_accepts(candidate):
                path = candidate
                break
        if path is None and os.path.exists(env_path):
            path = env_path
    _socket_resolution = ((signature, hypr_dir), path, now)
    return path


def forget_socket():
    """Drop the memoized socket so the next lookup rescans the instances."""
    global _socket_resolution
    _socket_resolution = None


def backend():
    """"hyprland" when this process runs under a Hyprland session whose
    request socket exists, else "gnome" (the extension's D-Bus feed)."""
    path = hyprland_socket_path()
    if path and os.path.exists(path):
        return "hyprland"
    return "gnome"

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
# Hyprland: poll the request socket
# ---------------------------------------------------------------------------

def hypr_request(command, path=None, timeout=HYPR_REQUEST_TIMEOUT_S):
    """One request on Hyprland's socket (a fresh connection per request,
    exactly like hyprctl): the reply text. `j/…` replies are JSON."""
    path = path or hyprland_socket_path()
    if not path:
        raise RuntimeError("not a Hyprland session")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(command.encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    return b"".join(chunks).decode("utf-8", "replace")


def hyprland_window_info(client):
    """A `j/clients` entry as the feed's window dict (the extension's keys,
    so os_frame / pick_window read both backends alike). x/y/width/height
    are the SURFACE rect — Hyprland places the surface, not the xdg
    geometry — in logical pixels; `address` is Hyprland's window handle
    (the dispatchers' `address:0x…` selector)."""
    at = client.get("at") or (0, 0)
    size = client.get("size") or (0, 0)
    address = str(client.get("address") or "0x0")
    fullscreen = client.get("fullscreen") or 0          # 0 none, 1 maximized, 2 fullscreen
    return {
        "id": int(address, 16),
        "address": address,
        "pid": int(client.get("pid") or 0),
        "wm_class": client.get("class") or "",
        "title": client.get("title") or "",
        "x": int(at[0]), "y": int(at[1]),
        "width": int(size[0]), "height": int(size[1]),
        "monitor": int(client.get("monitor") if client.get("monitor") is not None else 0),
        "maximized": fullscreen == 1,
        "fullscreen": fullscreen == 2,
        "focused": client.get("focusHistoryID") == 0,
        "floating": bool(client.get("floating")),
        "mapped": bool(client.get("mapped", True)),
    }


def hyprland_monitor_info(monitor):
    """A `j/monitors` entry as the feed's monitor dict. Hyprland reports
    the mode in PHYSICAL pixels; the layout (window `at`/`size`) is
    logical, so the size is divided by the scale and swapped for a 90°
    transform. `reserved` = [left, top, right, bottom] px kept for
    bars (waybar's dock) — the work area is the geometry less those."""
    scale = float(monitor.get("scale") or 1.0)
    width = float(monitor.get("width") or 0) / scale
    height = float(monitor.get("height") or 0) / scale
    if int(monitor.get("transform") or 0) % 2:
        width, height = height, width
    left, top, right, bottom = (list(monitor.get("reserved") or [0, 0, 0, 0]) + [0, 0, 0, 0])[:4]
    x, y = int(monitor.get("x") or 0), int(monitor.get("y") or 0)
    return {
        "index": int(monitor.get("id") or 0),
        "name": monitor.get("name") or "",
        "x": x, "y": y,
        "width": int(round(width)), "height": int(round(height)),
        "work_x": x + int(left), "work_y": y + int(top),
        "work_width": int(round(width)) - int(left) - int(right),
        "work_height": int(round(height)) - int(top) - int(bottom),
        "scale": scale,
        "primary": bool(monitor.get("focused")),
    }


def _hypr_poll_windows(pid, path):
    """One `j/clients` poll: our window into _STATE. `updates` moves only
    when the rect (or the window) changed — the count is the consumers'
    change signal, a poll that saw nothing new must not bump it."""
    clients = json.loads(hypr_request("j/clients", path))
    infos = [hyprland_window_info(c) for c in clients]
    # Every window of ours (app.py surfaces: several per process), for the
    # by-title lookups (surface_rect / place_window / _current_frame).
    _STATE["windows"] = [w for w in infos if w.get("pid") == pid]
    win = pick_window(infos, pid)
    cur = _STATE["frame"]
    if win is None:
        if cur is not None:
            _STATE["frame"] = None
            _STATE["updates"] += 1
        return None
    if cur is None or any(win[k] != cur.get(k) for k in ("id", "x", "y", "width", "height", "monitor",
                                                          "maximized", "fullscreen")):
        _STATE["frame"] = win
        _STATE["updates"] += 1
        _refresh_workarea()
    return win


def _hypr_poll_monitors(path):
    _STATE["monitors"] = [hyprland_monitor_info(m) for m in json.loads(hypr_request("j/monitors", path))]
    _refresh_workarea()


def _hypr_poll_interval():
    try:
        from src.lsd.gl_gui.toggles import Toggles
        hz = float(Toggles.Melty.hyprland_feed_poll_hz)
    except Exception:
        hz = 120.0
    return 1.0 / max(hz, 1.0)


def _alive(gen):
    """Is the thread of generation ``gen`` still the wanted one? start()
    bumps the generation, so a superseded thread (a hotswap that changed
    the backend, a stop) winds down on its own — the old D-Bus loop kept
    running through the first Hyprland hotswap and start() saw a live
    thread and did nothing (09-06)."""
    return _STATE["running"] and _STATE.get("gen") == gen


def _hyprland_thread_main(pid, gen):
    """Poll loop: clients every tick, monitors every HYPR_MONITORS_EVERY_S.
    A failed request (Hyprland restarting, socket gone) marks the feed
    unavailable and retries after RETRY_SECONDS."""
    path = hyprland_socket_path()
    monitors_at = 0.0
    while _alive(gen):
        try:
            now = time.monotonic()
            if now - monitors_at >= HYPR_MONITORS_EVERY_S:
                _hypr_poll_monitors(path)
                monitors_at = now
            _hypr_poll_windows(pid, path)
            _STATE["available"] = True
            _STATE["error"] = None
            time.sleep(_hypr_poll_interval())
        except Exception as ex:
            _STATE["available"] = False
            _STATE["error"] = str(ex)
            deadline = time.monotonic() + RETRY_SECONDS
            while _alive(gen) and time.monotonic() < deadline:
                time.sleep(0.25)
            forget_socket()
            path = hyprland_socket_path()       # the instance may have restarted under us
    if _STATE.get("gen") == gen:
        _STATE["available"] = False


def _current_frame():
    """The feed's window dict for the CURRENT window: the studio's picked
    one, or — once app.py surfaces are in use — the active surface's,
    matched by title (several windows share our pid and class)."""
    title = _active_surface_title()
    if title is not None:
        return _window_by_title(title)
    return _STATE["frame"]


def _active_surface_title():
    try:
        from src.lsd.gl_gui.surface import Surface
    except Exception:
        return None
    active = Surface.active
    return active.title if active is not None else None


def _window_by_title(title):
    for w in _STATE.get("windows") or ():
        if w.get("title") == title:
            return w
    return None


def surface_rect(title):
    """(x, y, width, height) of OUR window titled ``title`` (its surface
    rect, logical px), or None while the feed has not seen it."""
    if not _STATE["available"]:
        return None
    w = _window_by_title(title)
    return (w["x"], w["y"], w["width"], w["height"]) if w else None


def place_window(title, rect, *, resize=True):
    """Move AND resize our window titled ``title`` to ``rect`` (absolute
    logical px, top-left anchored) in one request — app.py's child
    surfaces following their parent. With resize=False only move: the
    surface's edge solver owns its size. Hyprland only; True on "ok"."""
    if backend() != "hyprland":
        return False
    w = _window_by_title(title)
    if w is None:
        return False
    selector = f"address:{w['address']}"
    x, y, width, height = (int(v) for v in rect)
    if hypr_config_is_lua():
        size_request = (f'hl.dispatch(hl.dsp.window.resize({{x = {width}, y = {height}, window = "{selector}"}})); '
                        if resize else '')
        script = (f'local w = hl.get_window("{selector}"); '
                  f'if not w then error("no window {selector}") end; '
                  f'{size_request}'
                  f'hl.dispatch(hl.dsp.window.move({{x = {x}, y = {y}, window = "{selector}"}}))')
        return _hypr_eval(script, "place_window")
    ok = not resize or _hypr_run(f"dispatch resizewindowpixel exact {width} {height},{selector}", "resizewindowpixel")
    return _hypr_run(f"dispatch movewindowpixel exact {x} {y},{selector}", "movewindowpixel") and ok


def _hypr_selector():
    """Hyprland's window selector for the current window (_current_frame),
    or None while the feed has not seen it."""
    frame = _current_frame()
    if backend() != "hyprland" or frame is None:
        return None
    return f"address:{frame['address']}"


def hypr_honors_geometry():
    """Does this Hyprland honour xdg_surface.set_window_geometry on
    toplevels (the 09-09 compositor patch, `render:xdg_window_geometry`)?
    Then the feed's `at` / `size` are the CONTENT box — the border, the
    shadow and the hit test hug it — and the surface overhangs it by the
    shadow margin, exactly as on GNOME; `frame_rect` needs no inset and
    `titlebar.sync_window_geometry` sends the content rect. Probed ONCE
    per feed generation with `getoption`: "bool: true" = yes; a stock or
    older binary answers "no such option" (= no) and keeps the
    box-is-the-surface handling."""
    known = _STATE.get("geometry")
    if known is not None:
        return known
    if backend() != "hyprland":
        return False
    try:
        reply = hypr_request("getoption render:xdg_window_geometry")
    except Exception as ex:
        _STATE["error"] = str(ex)
        return False                            # unknown: try again next time
    first = reply.strip().split("\n", 1)[0].strip()
    _STATE["geometry"] = first.startswith("bool:") and first.split(":", 1)[1].strip() in ("true", "1")
    return _STATE["geometry"]


def hypr_config_is_lua():
    """Does this Hyprland parse socket `dispatch` requests as LUA (the
    0.56 Lua config manager: `dispatch X` is `eval return hl.dispatch(X)`,
    so the classic `movewindowpixel dx dy,address:…` text is a Lua syntax
    error — every move and resize the studio sent under it was refused
    with "')' expected", which is what kept the window fixed while the UI
    moved, 09-08)? Probed ONCE per feed generation with `eval return true`:
    "ok" = Lua; the hyprlang build answers "eval is only supported with
    the lua config manager"."""
    known = _STATE.get("lua")
    if known is not None:
        return known
    try:
        reply = hypr_request("eval return true").strip()
    except Exception as ex:
        _STATE["error"] = str(ex)
        return False                            # unknown: try again next time
    _STATE["lua"] = reply == "ok"
    return _STATE["lua"]


def _hypr_run(request, what):
    """One request expected to answer "ok"; the error text lands in
    `last_error()` under ``what`` otherwise."""
    try:
        reply = hypr_request(request)
    except Exception as ex:
        _STATE["error"] = str(ex)
        if os.environ.get("MELTY_DEBUG"):
            print(f"[geometry_feed] {what}: {request!r} -> EXC {ex}", flush=True)
        return False
    ok = reply.strip() == "ok"
    if os.environ.get("MELTY_DEBUG"):
        print(f"[geometry_feed] {what}: {request[:160]!r} -> {reply.strip()[:80]!r}", flush=True)
    if not ok:
        _STATE["error"] = f"{what}: {reply.strip()}"
    return ok


def _hypr_dispatch(command):
    """Legacy (hyprlang) `dispatch <command>,address:…` on the studio's
    window; True on "ok"."""
    selector = _hypr_selector()
    if selector is None:
        return False
    return _hypr_run(f"dispatch {command},{selector}", command.split()[0])


def _hypr_eval(script, what):
    """`eval <script>` on the Lua config manager; True on "ok"."""
    return _hypr_run(f"eval {script}", what)


def hypr_set_box(width, height, dx=0, dy=0):
    """Resize the studio's window box to (width, height) logical px AND
    move it by (dx, dy), anchored at its top-left, as ONE request —
    Hyprland's floating resize is CENTRED (DefaultFloatingAlgorithm
    ::resizeTarget translates by −Δ/2 whatever the corner), so a bare
    exact resize slid the window by half the growth. On the Lua build
    one `eval` reads the window's goal position, resizes, then moves it
    ABSOLUTELY to goal + (dx, dy): atomic (nothing observes the centred
    intermediate), integral (no half-pixel goals), and never dependent on
    the feed's possibly stale position. Hyprland never adopts a size the
    client commits by itself (CWindow::clampWindowSize only clamps its
    OWN size — a bare glfw.set_window_size changed the buffer and left
    the box where it was, 09-06), so this is the way the box follows the
    surface; the configure it sends back names the size GLFW already
    applied. Legacy hyprlang build (unverified here): resizewindowpixel
    exact + movewindowpixel, the centred half-step uncompensated."""
    width, height, dx, dy = int(width), int(height), int(dx), int(dy)
    selector = _hypr_selector()
    if selector is None:
        return False
    if hypr_config_is_lua():
        script = (f'local w = hl.get_window("{selector}"); '
                  f'if not w then error("no window {selector}") end; '
                  f'local p = w.at; '
                  f'hl.dispatch(hl.dsp.window.resize({{x = {width}, y = {height}, window = "{selector}"}})); '
                  f'hl.dispatch(hl.dsp.window.move({{x = p.x + ({dx}), y = p.y + ({dy}), window = "{selector}"}}))')
        return _hypr_eval(script, "set_box")
    ok = _hypr_dispatch(f"resizewindowpixel exact {width} {height}")
    if dx or dy:
        ok = _hypr_dispatch(f"movewindowpixel {dx} {dy}") and ok
    return ok


def hypr_resize_window(width, height):
    """Resize the studio's window box to (width, height) logical px, its
    top-left held (hypr_set_box)."""
    return hypr_set_box(width, height)


def hypr_move_window(dx, dy):
    """Move the studio's window by (dx, dy) logical px — the Hyprland
    stand-in for the buffer-offset move (which Hyprland ignores on
    toplevels). Synchronous, ~0.05 ms; True when Hyprland answered "ok".
    The feed shows the move on its next poll, which is what os_frame's
    in-flight bookkeeping waits for."""
    dx, dy = int(dx), int(dy)
    if not (dx or dy):
        return False
    selector = _hypr_selector()
    if selector is None:
        return False
    if hypr_config_is_lua():
        return _hypr_eval(f'hl.dispatch(hl.dsp.window.move({{x = {dx}, y = {dy}, relative = true, '
                          f'window = "{selector}"}}))', "move")
    return _hypr_dispatch(f"movewindowpixel {dx} {dy}")


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


def _thread_main(pid, gen):
    gio, glib = _libs()
    ctx = glib.g_main_context_new()
    glib.g_main_context_push_thread_default(ctx)
    try:
        while _alive(gen):
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
                while _alive(gen) and time.monotonic() < deadline:
                    time.sleep(0.25)
    finally:
        glib.g_main_context_pop_thread_default(ctx)
        glib.g_main_context_unref(ctx)
        if _STATE.get("gen") == gen:
            _STATE["available"] = False


def start(pid=None):
    """Start the reader thread for the session's backend (idempotent while
    that thread runs; a live thread of the OTHER backend — a hotswap after
    a desktop switch — is superseded by a new generation)."""
    wanted = backend()
    thread = _STATE["thread"]
    if thread is not None and thread.is_alive() and _STATE.get("backend") == wanted:
        return thread
    stop()
    if pid is not None:
        _STATE["pid"] = int(pid)
    elif not _STATE.get("pid"):
        _STATE["pid"] = os.getpid()             # a restart keeps the pid it was started with
    _STATE["gen"] = _STATE.get("gen", 0) + 1
    _STATE["running"] = True
    _STATE["backend"] = wanted
    _STATE["frame"] = None
    _STATE["lua"] = None                    # re-probe the dispatching (hypr_config_is_lua)
    _STATE["geometry"] = None               # re-probe the geometry support (hypr_honors_geometry)
    _STATE["available"] = False
    target = _hyprland_thread_main if wanted == "hyprland" else _thread_main
    thread = threading.Thread(target=target, args=(_STATE["pid"], _STATE["gen"]),
                              name="window-geometry-feed", daemon=True)
    _STATE["thread"] = thread
    thread.start()
    return thread


def ensure_started():
    """Per-frame cheap check (os_frame._observe): the feed thread of the
    CURRENT backend is running — starts / restarts it otherwise."""
    thread = _STATE["thread"]
    if thread is None or not thread.is_alive() or _STATE.get("backend") != backend():
        start()


def stop():
    _STATE["running"] = False
    _STATE["gen"] = _STATE.get("gen", 0) + 1       # the old thread notices this after a restart
    loop = _STATE.get("loop")
    if loop:
        _libs()[1].g_main_loop_quit(loop)


def available():
    return bool(_STATE["available"] and _STATE["frame"])


def last_error():
    return _STATE["error"]


def frame_rect(inset=0):
    """(x, y, width, height) of the studio's content rect on the screen, or
    None while the feed is unavailable. ``inset`` = the transparent shadow
    margin (titlebar.window_inset) — applied only on a Hyprland that does
    NOT honour the window geometry (its rect is then the SURFACE); the
    GNOME feed's frame rect and a geometry-honouring Hyprland's box are
    the xdg geometry, already the content (hypr_honors_geometry)."""
    frame = _current_frame()
    if not _STATE["available"] or frame is None:
        return None
    x, y, w, h = frame["x"], frame["y"], frame["width"], frame["height"]
    if inset and _STATE.get("backend") == "hyprland" and not hypr_honors_geometry():
        inset = int(inset)
        x, y, w, h = x + inset, y + inset, max(w - 2 * inset, 0), max(h - 2 * inset, 0)
    return (x, y, w, h)


def workarea():
    """(x, y, width, height) of the work area of the monitor holding the
    studio, or None."""
    if not _STATE["available"]:
        return None
    return _STATE["workarea"]


def updates():
    """Monotone count of frame-rect updates seen — cheap change detection."""
    return _STATE["updates"]
