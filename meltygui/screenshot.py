"""Per-window screenshots of the live Melty studio.

The launcher MCP server runs in the same process as the studio (the studio
renders on a worker thread), but the GL context is only current on that render
thread. So capture is a hand-off:

  * request_capture(name)  — called from the MCP thread; enqueues a request,
    wakes the render loop, and blocks until it's fulfilled.
  * process_captures(window) — called once per frame from Melty.post_frame on
    the render thread (just before swap, so GL_BACK holds the finished frame);
    reads the named window's sub-rectangle and writes a PNG.

Capturing one window keeps the image small and legible (the real display can be
an ultra-wide spanning many windows).
"""

import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
SHOT_DIR = _ROOT / ".melty" / "screenshots"

_pending = []
_lock = threading.Lock()


def list_window_names():
    """Names of currently-registered top-level windows (for error messages)."""
    from src.lsd.gl_gui.melty import Melty
    return [mw.name for mw in Melty.registered_windows.values()
            if getattr(mw, "name", None) and getattr(mw, "draw_state", None) is not None]


# Frames to wait after fronting a window before reading pixels. apply_move_to_front
# runs in end_frame, and the bumped layer only affects the *next* frame's draw, so
# a couple of frames must render before the window is actually composited on top.
_SETTLE_FRAMES = 3


def request_capture(name, timeout=8.0):
    """Request a capture of window `name`. Returns (path, error); one is None.

    Safe to call off the render thread — blocks until the render thread fronts
    the window, lets it composite, captures it, or `timeout` elapses.
    """
    req = {"name": name, "stage": "front", "frames_left": 0,
           "path": None, "error": None, "event": threading.Event()}
    with _lock:
        _pending.append(req)
    _nudge()
    if not req["event"].wait(timeout):
        with _lock:
            if req in _pending:
                _pending.remove(req)
        return None, (f"timed out after {timeout:.0f}s — is a studio session "
                      f"running and rendering? windows: {list_window_names()}")
    return req["path"], req["error"]


def _nudge():
    try:
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()  # force the loop to render the next frame
    except Exception:
        pass


def process_captures(window):
    """Advance pending captures one frame. Call from the render thread
    (post_frame), before swap_buffers, with the GL context current.

    Each request walks: front (raise the window) -> settle (let it composite)
    -> capture. We pump one frame per step via request_render.
    """
    with _lock:
        if not _pending:
            return
        reqs = _pending[:]
        _pending.clear()

    from src.lsd.gl_gui.melty import Melty
    unfinished = []
    for req in reqs:
        try:
            if req["stage"] == "front":
                mw = _find_window(req["name"])
                if mw is None:
                    req["error"] = (f"no window named {req['name']!r}; "
                                    f"available: {list_window_names()}")
                    req["event"].set()
                    continue
                # A closed window early-returns from its render (draw None),
                # so open it. Clear both the draw_state flag and the stored
                # window_args, since the renderer re-applies kwargs["closed"]
                # each frame.
                mw.draw_state.closed = False
                if isinstance(mw.window_args, dict):
                    mw.window_args["closed"] = False
                Melty.move_window_to_front(mw.draw_state)
                req["stage"] = "settle"
                req["frames_left"] = _SETTLE_FRAMES
                unfinished.append(req)
            elif req["stage"] == "settle":
                req["frames_left"] -= 1
                if req["frames_left"] <= 0:
                    req["stage"] = "capture"
                unfinished.append(req)
            elif req["stage"] == "capture":
                req["path"] = _capture(window, req["name"])
                req["event"].set()
        except Exception as e:
            req["error"] = str(e)
            req["event"].set()

    if unfinished:
        with _lock:
            _pending.extend(unfinished)
        _nudge()  # keep frames coming while staged requests advance


def _find_window(name):
    from src.lsd.gl_gui.melty import Melty
    wins = [mw for mw in Melty.registered_windows.values()
            if getattr(mw, "name", None) and getattr(mw, "draw_state", None) is not None]
    # Prefer an exact name; fall back to case-insensitive substring. In both
    # cases the last match is the most-recently-fronted window.
    exact = [mw for mw in wins if mw.name == name]
    if exact:
        return exact[-1]
    sub = [mw for mw in wins if name.lower() in mw.name.lower()]
    if sub:
        return sub[-1]
    return None


def _capture(window, name):
    import glfw
    import numpy as np
    import OpenGL.GL as gl
    from PIL import Image

    mw = _find_window(name)
    if mw is None:
        raise ValueError(f"no window named {name!r}; available: {list_window_names()}")

    ds = mw.draw_state
    left, top, w, h = ds.left, ds.top, ds.width, ds.height
    if not w or not h:
        raise ValueError(f"window {mw.name!r} has no size ({w}x{h}); is it open?")

    fb_w, fb_h = glfw.get_framebuffer_size(window)
    win_w, win_h = glfw.get_window_size(window)
    scale = (fb_w / win_w) if win_w else 1.0

    # Window coordinates in framebuffer pixels, clamped to the framebuffer.
    x0 = max(0, min(int(round(left * scale)), fb_w))
    y_top = max(0, min(int(round(top * scale)), fb_h))
    pw = max(1, min(int(round(w * scale)), fb_w - x0))
    ph = max(1, min(int(round(h * scale)), fb_h - y_top))
    y_bottom = fb_h - (y_top + ph)  # GL origin is bottom-left

    gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
    gl.glReadBuffer(gl.GL_BACK)
    data = gl.glReadPixels(x0, y_bottom, pw, ph, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)

    if isinstance(data, bytes):
        arr = np.frombuffer(data, dtype=np.uint8)
    else:
        arr = np.asarray(data, dtype=np.uint8).ravel()
    arr = arr.reshape(ph, pw, 4)
    arr = np.flipud(arr)  # bottom-left -> top-left origin

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in mw.name)[:40] or "window"
    path = SHOT_DIR / f"{safe}_{int(time.time())}.png"
    Image.fromarray(arr, "RGBA").save(path)
    return str(path)
