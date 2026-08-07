"""Custom application titlebar (Toggles.Melty.enhanced_titlebar).

Removes the server-side decoration so the UI extends to the top of the
display, draws min/max/close as overlay-drawlist widgets in the top-right
corner, and hands drag / edge-resize back to the window manager via
_NET_WM_MOVERESIZE — so snapping, tiling and drag smoothness stay native.

Backend support: X11 only (which includes every XWayland session — the
bundled GLFW is built without a Wayland backend, so that is every session
today). Native Wayland has no public GLFW route to xdg_toplevel.move();
when a Wayland-capable GLFW lands, a second backend slots in behind
begin_move()/begin_resize() and the widget code above is unchanged.

Everything here runs on the visualization thread inside the imgui frame
(called from LSDStudio.render). The decoration attribute is synced live
each frame, so flipping the toggle takes effect without a restart.
"""

import ctypes

import glfw
import imgui

# ---------------------------------------------------------------------------
# X11 backend: _NET_WM_MOVERESIZE via ctypes → libX11
# ---------------------------------------------------------------------------

# direction values from the EWMH spec
_SIZE_TOPLEFT = 0
_SIZE_TOP = 1
_SIZE_TOPRIGHT = 2
_SIZE_RIGHT = 3
_SIZE_BOTTOMRIGHT = 4
_SIZE_BOTTOM = 5
_SIZE_BOTTOMLEFT = 6
_SIZE_LEFT = 7
_MOVE = 8

_CLIENT_MESSAGE = 33
_BUTTON_RELEASE = 5
_BUTTON_RELEASE_MASK = 1 << 3
_SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)  # SubstructureNotify | SubstructureRedirect


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
        ("_pad", ctypes.c_char * 96),  # pad out to XEvent union size
    ]


class _XButtonEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("button", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
        ("_pad", ctypes.c_char * 96),
    ]


_x11 = None
_moveresize_atom = None


def _lib():
    global _x11
    if _x11 is None:
        _x11 = ctypes.CDLL("libX11.so.6")
        _x11.XInternAtom.restype = ctypes.c_ulong
        _x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        _x11.XUngrabPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        _x11.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                    ctypes.c_long, ctypes.c_void_p]
        _x11.XFlush.argtypes = [ctypes.c_void_p]
        _x11.XDefaultRootWindow.restype = ctypes.c_ulong
        _x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        _x11.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + \
            [ctypes.c_void_p] * 7
    return _x11


def _handles(window):
    return glfw.get_x11_display(), glfw.get_x11_window(window)


def _root_pointer(x11, dpy, root):
    r = ctypes.c_ulong()
    child = ctypes.c_ulong()
    rx, ry, wx, wy = (ctypes.c_int() for _ in range(4))
    mask = ctypes.c_uint()
    x11.XQueryPointer(dpy, root, ctypes.byref(r), ctypes.byref(child),
                      ctypes.byref(rx), ctypes.byref(ry),
                      ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
    return rx.value, ry.value


def _synthesize_button_release(x11, dpy, win, root, button=1):
    # The WM's grab swallows the real ButtonRelease, so GLFW (and imgui's
    # process_inputs) would think the button stayed down until the next real
    # click. A release sent only to our own window fixes the client-side
    # state without touching the server's physical button state the WM is
    # tracking for the drag.
    ev = _XButtonEvent()
    ev.type = _BUTTON_RELEASE
    ev.display = dpy
    ev.window = win
    ev.root = root
    ev.time = 0  # CurrentTime
    ev.x_root, ev.y_root = _root_pointer(x11, dpy, root)
    ev.state = 1 << (7 + button)  # Button<N>Mask (Button1Mask = 1<<8)
    ev.button = button
    ev.same_screen = 1
    x11.XSendEvent(dpy, win, True, _BUTTON_RELEASE_MASK, ctypes.byref(ev))


def _begin_moveresize(window, direction, button=1):
    """Hand the in-progress button press to the WM as a move/resize."""
    global _moveresize_atom
    try:
        x11 = _lib()
        dpy, win = _handles(window)
        if not dpy or not win:
            return False
        root = x11.XDefaultRootWindow(dpy)
        if _moveresize_atom is None:
            _moveresize_atom = x11.XInternAtom(dpy, b"_NET_WM_MOVERESIZE", 0)
        rx, ry = _root_pointer(x11, dpy, root)

        x11.XUngrabPointer(dpy, 0)  # release the implicit press grab so the WM can take it
        ev = _XClientMessageEvent()
        ev.type = _CLIENT_MESSAGE
        ev.display = dpy
        ev.window = win
        ev.message_type = _moveresize_atom
        ev.format = 32
        ev.data[0] = rx
        ev.data[1] = ry
        ev.data[2] = direction
        ev.data[3] = button
        ev.data[4] = 1  # source = normal application
        x11.XSendEvent(dpy, root, False, _SUBSTRUCTURE_MASK, ctypes.byref(ev))
        _synthesize_button_release(x11, dpy, win, root, button)
        x11.XFlush(dpy)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Widget: window controls + drag strip + edge resize
# ---------------------------------------------------------------------------

_BTN_W = 40.0
_BTN_H = 30.0
_pressed_button = None  # index armed by a press, fires on release inside

# The drag strip participates in the normal input-handler pipeline as the
# WORST-priority drag subscriber: on mouse-down the handler gives the drag
# to the best-priority hovered subscriber, so any view that actually uses
# click-and-drag (window headers, sliders, dnd, ...) captures it and the
# strip only receives the drag when nothing else under the cursor wants one.
_STRIP_ID = "enhanced_titlebar_strip"
_RESIZE_ID = "enhanced_titlebar_rdrag_resize"
_STRIP_PRIORITY = 10 ** 9
_wm_move_started = False  # latch: dragged is per-frame, the WM move starts once

# Right-drag resize gesture state, latched on the first dragged event:
# (L0, T0, R0, B0, px0, py0, (grab_right, grab_bottom), workarea). App-side
# (glfw set_window_pos/size per frame) rather than a pointer grab, so the melty
# display-edge behavior works: an edge dragged past the workarea edge pins
# there but moves the OPPOSITE edge instead (slide + resize), capping the
# window at the workarea.
_rdrag = None
_MIN_W, _MIN_H = 320.0, 200.0
# Corner-priority band: the top/left edges only grab within this many px of
# their edge; everything past it belongs to the bottom-right corner, which
# is the overwhelmingly common resize.
_GRAB_BAND = 50.0


def top_inset():
    """px of top-edge chrome the app should keep clear (the window-control
    row) — 0 when the custom titlebar is off. For overlays that would
    otherwise collide with the buttons (e.g. the GPU readout)."""
    return _BTN_H if titlebar_enabled() else 0.0


def backend_supported():
    try:
        return glfw.get_platform() == glfw.PLATFORM_X11
    except AttributeError:
        return True  # pre-3.4 glfw on linux = X11


def titlebar_enabled():
    from src.lsd.gl_gui.toggles import Toggles
    melty_toggles = getattr(Toggles, "Melty", None)
    return (melty_toggles is not None
            and getattr(melty_toggles, "enhanced_titlebar", False)
            and backend_supported())


def sync_decoration(window):
    """Apply the toggle live: called every frame on the viz thread."""
    want_bar = titlebar_enabled()
    decorated = glfw.get_window_attrib(window, glfw.DECORATED)
    if want_bar and decorated:
        glfw.set_window_attrib(window, glfw.DECORATED, glfw.FALSE)
    elif not want_bar and not decorated:
        glfw.set_window_attrib(window, glfw.DECORATED, glfw.TRUE)
    return want_bar


def _edge_at(mx, my, w, h, border, corner):
    """EWMH resize direction for a pointer at (mx, my), or None."""
    on_l, on_r = mx <= border, mx >= w - border
    on_t, on_b = my <= border, my >= h - border
    near_l, near_r = mx <= corner, mx >= w - corner
    near_t, near_b = my <= corner, my >= h - corner
    if (on_t and near_l) or (on_l and near_t):
        return _SIZE_TOPLEFT
    if (on_t and near_r) or (on_r and near_t):
        return _SIZE_TOPRIGHT
    if (on_b and near_l) or (on_l and near_b):
        return _SIZE_BOTTOMLEFT
    if (on_b and near_r) or (on_r and near_b):
        return _SIZE_BOTTOMRIGHT
    if on_t:
        return _SIZE_TOP
    if on_b:
        return _SIZE_BOTTOM
    if on_l:
        return _SIZE_LEFT
    if on_r:
        return _SIZE_RIGHT
    return None


_EDGE_CURSOR = {
    _SIZE_TOP: "MOUSE_CURSOR_RESIZE_NS", _SIZE_BOTTOM: "MOUSE_CURSOR_RESIZE_NS",
    _SIZE_LEFT: "MOUSE_CURSOR_RESIZE_EW", _SIZE_RIGHT: "MOUSE_CURSOR_RESIZE_EW",
    _SIZE_TOPLEFT: "MOUSE_CURSOR_RESIZE_NWSE", _SIZE_BOTTOMRIGHT: "MOUSE_CURSOR_RESIZE_NWSE",
    _SIZE_TOPRIGHT: "MOUSE_CURSOR_RESIZE_NESW", _SIZE_BOTTOMLEFT: "MOUSE_CURSOR_RESIZE_NESW",
}


def _toggle_maximize(window):
    if glfw.get_window_attrib(window, glfw.MAXIMIZED):
        glfw.restore_window(window)
    else:
        glfw.maximize_window(window)


def _workarea_for(window):
    """(left, top, right, bottom) of the workarea of the monitor holding the
    window's center — the resize bounds. Falls back to the primary monitor
    when the center sits off every monitor (mid-drag between screens)."""
    wx, wy = glfw.get_window_pos(window)
    ww, wh = glfw.get_window_size(window)
    cx, cy = wx + ww / 2, wy + wh / 2
    target = None
    for m in glfw.get_monitors():
        mx, my = glfw.get_monitor_pos(m)
        mode = glfw.get_video_mode(m)
        if (mx <= cx < mx + mode.size.width
                and my <= cy < my + mode.size.height):
            target = m
            break
    if target is None:
        target = glfw.get_primary_monitor()
    ax, ay, aw, ah = glfw.get_monitor_workarea(target)
    return float(ax), float(ay), float(ax + aw), float(ay + ah)


def _apply_rdrag_resize(window, px, py):
    """One frame of the right-drag resize: move the grabbed corner's two
    edges by the pointer delta since the gesture latched, with the melty
    window clamp — an edge dragged past the workarea edge pins there and the
    remaining growth pushes the OPPOSITE edge (slide + resize), so the
    window maxes out filling the workarea instead of running off-screen."""
    L0, T0, R0, B0, px0, py0, (grab_right, grab_bottom), wa = _rdrag
    wa_l, wa_t, wa_r, wa_b = wa
    dx, dy = px - px0, py - py0

    L, T, R, B = L0, T0, R0, B0
    if grab_right:
        R = max(R0 + dx, L + _MIN_W)
        if R > wa_r:
            L = max(L - (R - wa_r), wa_l)
            R = wa_r
    else:
        L = min(L0 + dx, R - _MIN_W)
        if L < wa_l:
            R = min(R + (wa_l - L), wa_r)
            L = wa_l
    if grab_bottom:
        B = max(B0 + dy, T + _MIN_H)
        if B > wa_b:
            T = max(T - (B - wa_b), wa_t)
            B = wa_b
    else:
        T = min(T0 + dy, B - _MIN_H)
        if T < wa_t:
            B = min(B + (wa_t - T), wa_b)
            T = wa_t

    glfw.set_window_pos(window, int(round(L)), int(round(T)))
    glfw.set_window_size(window, int(round(R - L)), int(round(B - T)))


# FontAwesome glyphs (merged in the default UI font - see fonts._fa_merge),
# same icon set the rest of the app draws with.
_ICON_MINIMIZE = "\uf2d1"   # fa window-minimize
_ICON_MAXIMIZE = "\uf2d0"   # fa window-maximize
_ICON_RESTORE = "\uf2d2"    # fa window-restore
_ICON_CLOSE = "\uf00d"      # fa times


def _draw_glyph(dl, idx, cx, cy, color, maximized):
    """Minimize / maximize-restore / close, centered FontAwesome text glyphs.
    Uses the CURRENT font (draw_titlebar runs inside the frame before any
    push_font), which is the default UI font with FontAwesome merged in."""
    icon = (_ICON_MINIMIZE, _ICON_RESTORE if maximized else _ICON_MAXIMIZE,
            _ICON_CLOSE)[idx]
    ts = imgui.calc_text_size(icon)
    dl.add_text(cx - ts.x / 2.0, cy - ts.y / 2.0, color, icon)


def _close_blocked_by_merge():
    """True when quitting would lose pending state: some file has BOTH pending
    edits and unmerged external drift (PendingSave.needs_merge). Instead of
    closing, surface the merge window — the manual merge is the only way that
    state resolves. Errors never block the close."""
    try:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        if not PendingSave.needs_merge():
            return False
        from src.lsd.gl_gui.view.core_views.new_core_view import Core
        from src.lsd.gl_gui.notifications import notify
        Core.melty.open_window("merge_files")
        notify("Unmerged external changes — merge before closing",
               tint=(1.0, 0.7, 0.2))
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()
        return True
    except Exception:
        return False


def draw_titlebar(window):
    """Per-frame entry point — call inside the imgui frame on the viz thread.

    Handles decoration sync, the top-right window controls, the top drag
    strip (double-click = maximize) and edge/corner resize.
    """
    global _pressed_button, _wm_move_started, _rdrag
    if not sync_decoration(window):
        _pressed_button = None
        _wm_move_started = False
        _rdrag = None
        return

    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.toggles import Toggles
    melty_toggles = Toggles.Melty

    io = imgui.get_io()
    dl = imgui.get_overlay_draw_list()
    disp_w, disp_h = io.display_size.x, io.display_size.y
    mx, my = io.mouse_pos.x, io.mouse_pos.y
    maximized = bool(glfw.get_window_attrib(window, glfw.MAXIMIZED))

    # --- window control buttons, right-aligned at the very top -------------
    n_buttons = 3
    bar_left = disp_w - n_buttons * _BTN_W
    button_rects = []
    for i in range(n_buttons):
        x0 = bar_left + i * _BTN_W
        button_rects.append((x0, 0.0, x0 + _BTN_W, _BTN_H))

    over_button = None
    for i, (x0, y0, x1, y1) in enumerate(button_rects):
        if x0 <= mx <= x1 and y0 <= my <= y1:
            over_button = i
            break

    # --- edge/corner resize (skip while maximized) -------------------------
    border = float(getattr(melty_toggles, "resize_border", 6))
    corner = float(getattr(melty_toggles, "resize_corner", 18))
    edge = None
    if not maximized and over_button is None:
        edge = _edge_at(mx, my, disp_w, disp_h, border, corner)
    if edge is not None:
        cursor = getattr(imgui, _EDGE_CURSOR[edge], None)
        if cursor is not None:
            imgui.set_mouse_cursor(cursor)
        if imgui.is_mouse_clicked(0):
            _begin_moveresize(window, edge)
            return

    # --- buttons: arm on press, fire on release inside ---------------------
    if over_button is not None and imgui.is_mouse_clicked(0):
        _pressed_button = over_button
    if _pressed_button is not None and imgui.is_mouse_released(0):
        if over_button == _pressed_button:
            if _pressed_button == 0:
                glfw.iconify_window(window)
            elif _pressed_button == 1:
                _toggle_maximize(window)
            elif _close_blocked_by_merge():
                pass        # merge window opened instead; app stays up
            else:
                glfw.set_window_should_close(window, True)
        _pressed_button = None

    # --- drag strip along the top edge -------------------------------------
    # Register as the lowest-priority drag subscriber while the cursor is in
    # the strip; the handler's down-capture means a view that uses
    # click-and-drag always wins, and the strip only sees the drag when
    # nothing else claimed it. Clean clicks are untouched (dragged fires
    # only past the handler's drag threshold). Double-click = maximize,
    # same lowest-priority rule.
    strip_h = float(getattr(melty_toggles, "drag_strip_height", 50))
    if my <= strip_h and mx < bar_left and edge is None:
        Melty.event_handler.register_hovered(
            _STRIP_ID, ["left_mouse_dragged", "left_mouse_double_clicked"],
            priority=_STRIP_PRIORITY)
    strip_events = (getattr(Melty, "events", None) or {}).get(_STRIP_ID, {})
    if _wm_move_started and not Melty.event_handler.is_down("left_mouse"):
        _wm_move_started = False  # synthetic release landed - re-arm
    if "left_mouse_double_clicked" in strip_events:
        _toggle_maximize(window)
    elif "left_mouse_dragged" in strip_events and not _wm_move_started:
        _wm_move_started = True
        _begin_moveresize(window, _MOVE)
        return

    # --- right-drag resize, anywhere in the window -------------------------
    # Same worst-priority pattern as the strip, but over the WHOLE window:
    # any view that actually uses a right drag (camera orbits, melty window
    # resize) captures it first, and plain right-CLICKS are untouched
    # (dragged only fires past the handler's drag threshold, so context
    # menus keep working). The grabbed corner is the one nearest the pointer
    # at gesture start. App-side geometry rather than a WM grab so the melty
    # display-edge fix applies (see _apply_rdrag_resize) - the tradeoff is
    # a client-driven resize, which the per-drag-event render keeps smooth.
    if not maximized and over_button is None and edge is None:
        Melty.event_handler.register_hovered(
            _RESIZE_ID, ["right_mouse_dragged"], priority=_STRIP_PRIORITY)
    resize_events = (getattr(Melty, "events", None) or {}).get(_RESIZE_ID, {})
    if _rdrag is not None and not Melty.event_handler.is_down("right_mouse"):
        _rdrag = None  # gesture ended - re-latch on the next drag
    if "right_mouse_dragged" in resize_events or _rdrag is not None:
        try:
            x11 = _lib()
            dpy, _win = _handles(window)
            px, py = _root_pointer(x11, dpy, x11.XDefaultRootWindow(dpy))
        except Exception:
            px = py = None
        if px is not None:
            if _rdrag is None:
                wx, wy = glfw.get_window_pos(window)
                ww, wh = glfw.get_window_size(window)
                # Corner pick: bottom-right gets most of the window - the
                # top/left grabs only apply within the first _GRAB_BAND px of
                # their edge (halved on windows too small for two full bands,
                # so tiny windows still split sensibly).
                band_x = min(_GRAB_BAND, ww / 2)
                band_y = min(_GRAB_BAND, wh / 2)
                _rdrag = (float(wx), float(wy), float(wx + ww), float(wy + wh),
                          px, py, (mx >= band_x, my >= band_y),
                          _workarea_for(window))
            grab_right, grab_bottom = _rdrag[6]
            direction = ((_SIZE_BOTTOMRIGHT if grab_right else _SIZE_BOTTOMLEFT)
                         if grab_bottom else
                         (_SIZE_TOPRIGHT if grab_right else _SIZE_TOPLEFT))
            cursor = getattr(imgui, _EDGE_CURSOR[direction], None)
            if cursor is not None:
                imgui.set_mouse_cursor(cursor)
            _apply_rdrag_resize(window, px, py)

    # --- paint the buttons (topmost, after all the logic) ------------------
    for i, (x0, y0, x1, y1) in enumerate(button_rects):
        hovered = over_button == i
        if hovered:
            if i == 2:
                bg = imgui.get_color_u32_rgba(0.78, 0.16, 0.16, 0.9)
            else:
                bg = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.10)
            dl.add_rect_filled(x0, y0, x1, y1, bg)
        alpha = 1.0 if hovered else 0.55
        color = imgui.get_color_u32_rgba(0.9, 0.9, 0.9, alpha)
        _draw_glyph(dl, i, (x0 + x1) / 2.0, (y0 + y1) / 2.0, color, maximized)