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


def _shot_dir():
    """Output directory for screenshots — `Toggles.screenshots` (expanded),
    falling back to the in-repo `.melty/screenshots` if it can't be read."""
    try:
        from src.lsd.gl_gui.toggles import Toggles
        configured = Toggles.screenshots
        if configured:
            return Path(configured).expanduser()
    except Exception:
        pass
    return SHOT_DIR


def list_window_names():
    """Names of currently-registered top-level windows (for error messages)."""
    from src.lsd.gl_gui.melty import Melty
    return [mw.name for mw in Melty.registered_windows.values()
            if getattr(mw, "name", None) and getattr(mw, "draw_state", None) is not None]


# Frames to wait after fronting a window before reading pixels. apply_move_to_front
# runs in end_frame, and the bumped layer only affects the *next* frame's draw, so
# a couple of frames must render before the window is actually composited on top.
_SETTLE_FRAMES = 3


def _submit(req, timeout):
    """Enqueue `req`, wake the render loop, and block until it's fulfilled or
    `timeout` elapses. Returns (path, error); one is None."""
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


def request_capture(name, timeout=8.0):
    """Request a capture of window `name`. Returns (path, error); one is None.

    Safe to call off the render thread — blocks until the render thread fronts
    the window, lets it composite, captures it, or `timeout` elapses.
    """
    req = {"name": name, "stage": "front", "frames_left": 0,
           "path": None, "error": None, "event": threading.Event()}
    return _submit(req, timeout)


def request_tile_capture(name, timeout=8.0):
    """Like `request_capture`, but reads the window's cached offscreen blit tile
    straight from its FBO instead of the on-screen framebuffer.

    The tile holds the window's own composited pixels, so the capture is
    immune to whatever else is stacked on top of the window on screen (no
    fronting/settling needed). Falls back to `request_capture`'s
    front -> settle -> framebuffer-read flow if the window has no usable tile.

    Safe to call off the render thread.
    """
    req = {"name": name, "stage": "tile", "frames_left": 0,
           "path": None, "error": None, "event": threading.Event()}
    return _submit(req, timeout)


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
            if req["stage"] == "tile":
                path = _capture_tile(req["name"])
                if path is not None:
                    req["path"] = path
                    req["event"].set()
                else:
                    # No cached tile for this window -> fall back to the on-screen
                    # front -> settle -> framebuffer-read flow.
                    req["stage"] = "front"
                    unfinished.append(req)
                continue
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


_view_pending = []


def request_view_capture(draw_state, requested_frame, reopen_menu_ds=None,
                         on_captured=None):
    """Queue a deferred framebuffer capture of a single VIEW (any draw_state's
    on-screen rect), not a whole window. Serviced by process_take_screenshot_flags
    after `_SETTLE_FRAMES` -- enough frames for the window front-move to composite
    and the closed context menu to clear from the framebuffer.

    `requested_frame` is Melty.frame_count at request time. `reopen_menu_ds`, if
    given, is the context menu's draw_state to reopen once the shot lands.
    `on_captured`, if given, is called with the saved PNG path on the render
    thread once the shot lands (not called if the capture fails).
    Call on the render thread (from the menu's render).
    """
    _view_pending.append({
        "draw_state": draw_state,
        "requested_frame": requested_frame,
        "reopen_menu_ds": reopen_menu_ds,
        "on_captured": on_captured,
    })
    _nudge()


def process_take_screenshot_flags(window):
    """Service deferred per-view screenshots queued from the context menu. Call
    from the render thread (post_frame), with the GL context current.

    The context-menu screenshot button fronts the window, queues the view via
    request_view_capture, and closes the menu -- all during one frame's draw. We
    then wait `_SETTLE_FRAMES` so the front-move composites the window on top and
    the closed menu has cleared, then grab the view's rect and reopen the menu.
    Pumps frames while waiting.
    """
    if not _view_pending:
        return
    from src.lsd.gl_gui.melty import Melty
    still = []
    for req in _view_pending:
        if Melty.frame_count - int(req["requested_frame"]) < _SETTLE_FRAMES:
            still.append(req)  # not settled yet
            continue
        ds = req["draw_state"]
        path = None
        try:
            path = _capture_view(window, ds)
        except Exception as e:
            name = getattr(ds, "name", None) or "view"
            print(f"Screenshot (view) failed for {name!r}: {e}")
        if path is not None and req.get("on_captured") is not None:
            try:
                req["on_captured"](path)
            except Exception as e:
                print(f"Screenshot on_captured callback failed: {e}")
        _reopen_context_menu_ds(req.get("reopen_menu_ds"))
    _view_pending[:] = still
    if still:
        _nudge()  # keep frames coming until the window is fronted & menu gone


def _reopen_context_menu_ds(menu_ds):
    """Reopen a context menu after its screenshot lands. The menu set `_reopen`
    on its own draw_state. Reopening means flipping the owning view's
    `context_menu_open` back on (its source of truth) and clearing the menu's
    `closed`, then re-rendering the owner."""
    if menu_ds is None or not getattr(menu_ds, "_reopen", False):
        return
    menu_ds._reopen = False
    menu_ds.closed = False
    owner = getattr(menu_ds, "_raw_input_value", None)  # the view that owns the menu
    if owner is not None:
        owner.context_menu_open = True
        try:
            owner.invalidate_up(max_depth=5)
        except Exception:
            pass
    _nudge()


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
    """Grab a whole window's on-screen rect to a PNG (the MCP/window path)."""
    mw = _find_window(name)
    if mw is None:
        raise ValueError(f"no window named {name!r}; available: {list_window_names()}")
    return _capture_rect(window, mw.draw_state, mw.name)


def _capture_view(window, draw_state):
    """Grab a single VIEW's on-screen rect to a PNG. Same framebuffer read as
    `_capture`, but bounded to the given draw_state's rect rather than a whole
    top-level window -- so the context menu can shoot just the view it's for."""
    name = getattr(draw_state, "name", None) or "view"
    return _capture_rect(window, draw_state, name)


def _capture_rect(window, ds, name):
    """Read draw_state `ds`'s on-screen rect from GL_BACK and save it as a PNG
    named after `name`. The draw_state supplies the rect (left/top/width/height
    in window points); shared by the window and per-view capture paths."""
    import glfw
    import numpy as np
    import OpenGL.GL as gl
    from PIL import Image

    left, top, w, h = ds.left, ds.top, ds.width, ds.height
    if not w or not h:
        raise ValueError(f"{name!r} has no size ({w}x{h}); is it open?")

    fb_w, fb_h = glfw.get_framebuffer_size(window)
    win_w, win_h = glfw.get_window_size(window)
    scale = (fb_w / win_w) if win_w else 1.0

    # Rect in framebuffer pixels, clamped to the framebuffer.
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

    path = _shot_path(name)
    Image.fromarray(arr, "RGBA").save(path)
    return str(path)


def _shot_path(window_name):
    """A fresh timestamped PNG path under the configured shot dir (created)."""
    shot_dir = _shot_dir()
    shot_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in window_name)[:40] or "window"
    return shot_dir / f"{safe}_{int(time.time())}.png"


def _capture_tile(name):
    """Read window `name`'s cached offscreen blit tile straight from its FBO and
    save a PNG. Returns the path, or None if the window has no usable tile (the
    caller then falls back to the framebuffer capture).

    Reading the tile sidesteps window-overlay artifacts: the tile holds the
    window's own composited pixels regardless of what's stacked on top of it on
    screen. Must run on the render thread with the GL context current.
    """
    import numpy as np
    import OpenGL.GL as gl
    from PIL import Image
    from src.lsd.gl_gui.melty import Melty

    mw = _find_window(name)
    if mw is None:
        return None
    ds = getattr(mw, "draw_state", None)
    cache = getattr(Melty, "cache", None)
    tile_id = getattr(ds, "_tile_id", None) if ds is not None else None
    tiles = getattr(cache, "_tiles", None) if cache is not None else None
    if tile_id is None or tiles is None:
        return None
    tile = tiles.get(tile_id)
    if tile is None:
        return None
    w, h = tile.size
    if not w or not h or tile.fbo in (None, -1):
        return None

    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, tile.fbo)
    gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
    gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
    # Content is top-anchored in a possibly bottom-padded texture: the logical
    # w x h pixels live in the texture rows [alloc_h - h, alloc_h), not at y=0.
    alloc_h = (getattr(tile, "alloc_size", None) or tile.size)[1]
    data = gl.glReadPixels(0, int(alloc_h) - int(h), int(w), int(h), gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)  # restore the default framebuffer

    if isinstance(data, bytes):
        arr = np.frombuffer(data, dtype=np.uint8)
    else:
        arr = np.asarray(data, dtype=np.uint8).ravel()
    arr = arr.reshape(int(h), int(w), 4)
    arr = np.flipud(arr)  # GL origin is bottom-left -> top-left

    path = _shot_path(mw.name)
    Image.fromarray(arr, "RGBA").save(path)
    return str(path)
