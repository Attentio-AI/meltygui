"""Custom application titlebar (Toggles.Melty.enhanced_titlebar).

Removes the server-side decoration so the UI extends to the top of the
display, draws min/max/close as overlay-drawlist widgets in the top-right
corner, and hands drag / edge-resize back to the window manager via
_NET_WM_MOVERESIZE — so snapping, tiling and drag smoothness stay native.

Backend support. X11 (and XWayland): everything above. Native Wayland:
GLFW has no public route to xdg_toplevel.move()/resize(), so the OS window
KEEPS a frame there — GLFW's own fallback frame (a caption strip + borders,
compositor-driven move/resize) once Toggles.Melty.wayland_native_frame has
switched libdecor off at the first glfw.init (glfw_utils
.apply_wayland_frame_hint) — or, with Toggles.Melty.wayland_show_frame off
(the default), no frame at all. This module then contributes the buttons
(always on Wayland; Toggles.Melty.enhanced_titlebar is an X11 knob), the
right-drag resize (app-driven set_window_size, bottom-right corner only —
the top-left is pinned) and, through gl_gui/wayland_move.py, the SAME
strip / drag-anywhere move and edge resize as X11: xdg_toplevel.move /
.resize sent straight to the compositor (what Super+drag and the caption
strip do), the grab then driven by GNOME. With libdecor still on, its own
title bar carries the buttons and nothing here draws (backend_supported
is False).

Everything here runs on the visualization thread inside the imgui frame
(called from LSDStudio.render). The decoration attribute is synced live
each frame, so flipping the toggle takes effect without a restart.
"""

import ctypes

import glfw
import imgui
import OpenGL.GL as gl

from src.lsd.gl_gui import mouse_cursor
from src.lsd.gl_gui import wayland_move
from src.lsd.gl_gui.gl_state import GLState, is_gl_thread
from src.lsd.gl_gui.shader_func import shader_func

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

_BTN_H = 30.0   # top_inset: chrome height to keep clear (buttons ≈ this)
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


def _on_wayland():
    try:
        return glfw.get_platform() == glfw.PLATFORM_WAYLAND
    except AttributeError:
        return False  # pre-3.4 glfw on linux = X11


def backend_supported():
    """X11 always; native Wayland only once libdecor is out of the picture
    (the fallback frame has no buttons — ours fill in), never beside
    libdecor's own title bar."""
    if _on_wayland():
        from src.lsd.gl_gui.utils.glfw_utils import wayland_native_frame_active
        return wayland_native_frame_active()
    return True


def titlebar_enabled():
    """X11: Toggles.Melty.enhanced_titlebar (it replaces the WM frame, an
    opt-in). Wayland: whenever the native frame is up — GLFW's fallback frame
    carries no buttons, so ours are the only min/max/close the window gets,
    and the toggle is not consulted."""
    if _on_wayland():
        return backend_supported()
    from src.lsd.gl_gui.toggles import Toggles
    return Toggles.Melty.enhanced_titlebar and backend_supported()


def wants_os_decoration():
    """DECORATED for the OS window. Wayland: with libdecor still up, always
    (its frame is the window's chrome); on the native frame,
    Toggles.Melty.wayland_show_frame — off = frameless, resize by right-drag,
    move by the compositor. X11: only while the enhanced titlebar is off (it
    replaces the WM's frame with _NET_WM_MOVERESIZE gestures). Boot hint and
    per-frame sync both read this."""
    if _on_wayland():
        if not backend_supported():
            return True
        from src.lsd.gl_gui.toggles import Toggles
        return bool(Toggles.Melty.wayland_show_frame)
    return not titlebar_enabled()


def sync_decoration(window):
    """Apply the toggle live: called every frame on the viz thread."""
    want_bar = titlebar_enabled()
    decorated = bool(glfw.get_window_attrib(window, glfw.DECORATED))
    want_decorated = wants_os_decoration()
    if decorated != want_decorated:
        glfw.set_window_attrib(window, glfw.DECORATED,
                               glfw.TRUE if want_decorated else glfw.FALSE)
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


# One directional shape per edge/corner (right edge |>, left edge <|, each
# corner its own), not the shared directional arrows. Pushed via
# mouse_cursor.request - these shapes are past imgui's control.
_EDGE_CURSOR = {
    _SIZE_TOP: mouse_cursor.RESIZE_N, _SIZE_BOTTOM: mouse_cursor.RESIZE_S,
    _SIZE_LEFT: mouse_cursor.RESIZE_W, _SIZE_RIGHT: mouse_cursor.RESIZE_E,
    _SIZE_TOPLEFT: mouse_cursor.RESIZE_NW, _SIZE_BOTTOMRIGHT: mouse_cursor.RESIZE_SE,
    _SIZE_TOPRIGHT: mouse_cursor.RESIZE_NE, _SIZE_BOTTOMLEFT: mouse_cursor.RESIZE_SW,
}


# EWMH direction → xdg_toplevel.resize_edge, for the Wayland edge zones.
_XDG_EDGE = {
    _SIZE_TOP: wayland_move.EDGE_TOP, _SIZE_BOTTOM: wayland_move.EDGE_BOTTOM,
    _SIZE_LEFT: wayland_move.EDGE_LEFT, _SIZE_RIGHT: wayland_move.EDGE_RIGHT,
    _SIZE_TOPLEFT: wayland_move.EDGE_TOP_LEFT, _SIZE_TOPRIGHT: wayland_move.EDGE_TOP_RIGHT,
    _SIZE_BOTTOMLEFT: wayland_move.EDGE_BOTTOM_LEFT, _SIZE_BOTTOMRIGHT: wayland_move.EDGE_BOTTOM_RIGHT,
}


def _wm_gestures_available():
    """Can the strip / edge zones hand a gesture to the window manager?
    X11: always (_NET_WM_MOVERESIZE). Wayland: once wayland_move found the
    window's xdg_toplevel (LSDStudio attaches it after the input backend)."""
    return wayland_move.available() if _on_wayland() else True


def _release_after_wayland_grab(window):
    """The compositor's grab swallows the button release. X11 gets a real
    synthesized X event; on Wayland GLFW's own state can't be poked, so the
    input handler is fed the release here and the polls read the button as
    up through wayland_move.button_masked until GLFW's next real event."""
    from src.lsd.gl_gui.melty import Melty
    backend = getattr(Melty, "backend", None)
    if backend is None or not hasattr(backend, "_on_button"):
        return
    for button in wayland_move.masked_buttons():
        backend._on_button(window, button, glfw.RELEASE, 0)


def _begin_wm_move(window):
    if _on_wayland():
        if wayland_move.begin_move(window):
            _release_after_wayland_grab(window)
            return True
        return False
    return _begin_moveresize(window, _MOVE)


def _begin_wm_resize(window, direction):
    if _on_wayland():
        if wayland_move.begin_resize(window, _XDG_EDGE[direction]):
            _release_after_wayland_grab(window)
            return True
        return False
    return _begin_moveresize(window, direction)


def _toggle_maximize(window):
    if glfw.get_window_attrib(window, glfw.MAXIMIZED):
        glfw.restore_window(window)
    else:
        glfw.maximize_window(window)


def _workarea_for(window):
    """(left, top, right, bottom) of the workarea of the monitor holding the
    window's center — the resize bounds. Falls back to the primary monitor
    when the center sits off every monitor (mid-drag between screens)."""
    if _on_wayland():
        # No window positions on Wayland: the bounds are the primary
        # monitor's workarea SIZE anchored at the window's own top-left
        # (which the compositor pins) - a cap on how far a resize may grow.
        _ax, _ay, aw, ah = glfw.get_monitor_workarea(glfw.get_primary_monitor())
        return 0.0, 0.0, float(aw), float(ah)
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

    if not _on_wayland():        # Wayland: no client positioning, size only
        glfw.set_window_pos(window, int(round(L)), int(round(T)))
    glfw.set_window_size(window, int(round(R - L)), int(round(B - T)))


# FontAwesome glyphs (merged in the default UI font - see fonts._fa_merge),
# same icon set the rest of the app draws with.
_ICON_MINIMIZE = "\uf2d1"   # fa window-minimize
_ICON_MAXIMIZE = "\uf2d0"   # fa window-maximize
_ICON_RESTORE = "\uf2d2"    # fa window-restore
_ICON_CLOSE = "\uf00d"      # fa times


def _button_icons(maximized):
    return (_ICON_MINIMIZE, _ICON_RESTORE if maximized else _ICON_MAXIMIZE, _ICON_CLOSE)


def _button_layout(disp_w, maximized):
    """Rects of the three controls, right-aligned at the top: the header's
    close button sizing (flat_button: glyph + px(15) wide, + px(8) tall, one
    width for all three so they line up) inset by button_margin from the
    top-right corner, button_gap apart."""
    from src.lsd.gl_gui.melty import Melty
    # [tint=(1.0, 0.55, 0.2)]
    button_margin = Melty.px(4.0)
    # [tint=(1.0, 0.55, 0.2)]
    button_gap = Melty.px(3.0)
    sizes = [imgui.calc_text_size(icon) for icon in _button_icons(maximized)]
    width = max(s.x for s in sizes) + Melty.px(15.0)
    height = max(s.y for s in sizes) + Melty.px(8.0)
    n = len(sizes)
    left = disp_w - button_margin - n * width - (n - 1) * button_gap
    rects = []
    for i in range(n):
        x0 = left + i * (width + button_gap)
        rects.append((x0, button_margin, x0 + width, button_margin + height))
    return rects


def _paint_buttons(dl, rects, over_button, maximized):
    """The three controls, each painted EXACTLY like a window header's
    close button (draw_header_end): the same flat_button call — colour
    (9, 1, 1), glyph + px(15) by glyph + px(8), rounded, theme-mixed glyph,
    hover brightening, a depth mark for the shadow pass (drop shadow + lit
    rim) — run under Toggles.Melty.melty_window_tint set in the style
    manager, the way a header runs under its window's tint (that tint is
    what make_color_rgb mixes the colour against). The previous tint is
    restored afterwards."""
    from src.lsd.gl_gui.view.core_views.headers import flat_button
    from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.toggles import Toggles
    # The header close button's colour (draw_header_end).
    # [tint=(0.9, 0.15, 0.15)]
    close_color = (9, 1, 1)
    style_manager = Melty.style_manager
    previous_tint = style_manager.get_tint() if style_manager is not None else None
    chrome_tint = Toggles.Melty.melty_window_tint
    if style_manager is not None and chrome_tint and len(chrome_tint) >= 3:
        style_manager.set_imgui_tint(*chrome_tint[:4])
    try:
        for i, (icon, (x0, y0, x1, y1)) in enumerate(zip(_button_icons(maximized), rects)):
            # Ownerless raised mark at the current paint rank - the frame
            # has painted everything by now, so it lands on top; no clip
            # (the window clip stack is gone at this point of the frame).
            add_shadow((x0, y0, x1 - x0, y1 - y0), corner_radius=Melty.px(6.0), clip=False)
            flat_button(icon, None, view_id=f"titlebar_button_{i}",
                        width=x1 - x0, height=y1 - y0, pos=(x0, y0),
                        hovered=(over_button == i), layout=False, draw_list=dl,
                        color=close_color)
    finally:
        if style_manager is not None and previous_tint is not None:
            style_manager.set_imgui_tint(*previous_tint)


def _close_blocked_by_merge():
    """True when quitting would lose pending state: some file has BOTH pending
    edits and unmerged external drift (PendingSave.needs_merge). Instead of
    closing, surface the merge window — the manual merge is the only way that
    state resolves — with as much as possible already staged (its Auto-merge
    run over every drifted file) and the conflicts that are left flashed;
    Ctrl+M then applies. Errors never block the close."""
    try:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        if not PendingSave.needs_merge():
            return False
        from src.lsd.gl_gui.view.core_views.merge_files import MergeFiles
        from src.lsd.gl_gui.notifications import notify
        MergeFiles.open(auto_merge=True)
        notify("Unmerged external changes — merge before closing "
               "(Ctrl+M applies the staged merge)", tint=(1.0, 0.7, 0.2))
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()
        return True
    except Exception:
        return False


def draw_titlebar(window):
    """Per-frame entry point — call inside the imgui frame on the viz thread.

    Handles decoration sync, the top-right window controls, the top drag
    strip (double-click = maximize; with Toggles.Melty.move_drag_anywhere an
    unclaimed left-drag anywhere moves too), edge/corner resize and the
    right-drag resize. The WM gestures go through _NET_WM_MOVERESIZE on X11
    and wayland_move (xdg_toplevel.move/resize) on Wayland.
    """
    global _pressed_button, _wm_move_started, _rdrag
    if not sync_decoration(window):
        _pressed_button = None
        _wm_move_started = False
        _rdrag = None
        return

    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.toggles import Toggles

    io = imgui.get_io()
    dl = imgui.get_overlay_draw_list()
    disp_w, disp_h = io.display_size.x, io.display_size.y
    mx, my = io.mouse_pos.x, io.mouse_pos.y
    maximized = bool(glfw.get_window_attrib(window, glfw.MAXIMIZED))
    # The strip and edge gestures hand the drag to the window manager
    # (_begin_wm_move / _begin_wm_resize) - on Wayland only once wayland_move
    # is ready. The right-drag resize is app-driven (set_window_size) and
    # runs on both; on Wayland it reads the surface relative pointer and
    # always grabs the bottom-right corner (the top-left is pinned).
    wayland = _on_wayland()
    gestures = _wm_gestures_available()

    # --- window control buttons, right-aligned at the very top -------------
    button_rects = _button_layout(disp_w, maximized)
    bar_left = button_rects[0][0]

    over_button = None
    for i, (x0, y0, x1, y1) in enumerate(button_rects):
        if x0 <= mx <= x1 and y0 <= my <= y1:
            over_button = i
            break

    # --- edge/corner resize (skip while maximized) -------------------------
    border = float(Toggles.Melty.resize_border)
    corner = float(Toggles.Melty.resize_corner)
    edge = None
    if gestures and not maximized and over_button is None:
        edge = _edge_at(mx, my, disp_w, disp_h, border, corner)
    if edge is not None:
        mouse_cursor.request(_EDGE_CURSOR[edge])
        if imgui.is_mouse_clicked(0) and _begin_wm_resize(window, edge):
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
    # Toggles.Melty.move_drag_anywhere widens the drag (not the double-click)
    # to the whole window at the same worst priority: only a drag nothing
    # else claimed - including background - moves the OS window.
    strip_h = float(Toggles.Melty.drag_strip_height)
    in_strip = my <= strip_h and mx < bar_left
    anywhere = bool(Toggles.Melty.move_drag_anywhere) and over_button is None
    if gestures and edge is None and (in_strip or anywhere):
        Melty.event_handler.register_hovered(
            _STRIP_ID,
            ["left_mouse_dragged", "left_mouse_double_clicked"] if in_strip
            else ["left_mouse_dragged"],
            priority=_STRIP_PRIORITY)
    strip_events = (getattr(Melty, "events", None) or {}).get(_STRIP_ID, {})
    if _wm_move_started and not Melty.event_handler.is_down("left_mouse"):
        _wm_move_started = False  # synthetic release landed - re-arm
    if "left_mouse_double_clicked" in strip_events:
        _toggle_maximize(window)
    elif "left_mouse_dragged" in strip_events and not _wm_move_started:
        _wm_move_started = True
        if _begin_wm_move(window):
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
        # Left held too = the TOP-LEFT corner (the melty windows' own
        # left+right priority rule): on Wayland that requires needs a move
        # by the compositor - xdg_toplevel.resize(top_left) - so hand
        # the whole gesture over; on X11 the app-side path slides the window.
        both = _rdrag is None and Melty.event_handler.is_down("left_mouse")
        if both and wayland and wayland_move.begin_resize(window, wayland_move.EDGE_TOP_LEFT):
            _release_after_wayland_grab(window)
            _rdrag = None
            return
        if wayland:
            # Surface-relative pointer: only deltas matter, and the window's
            # top-left never moves here, so the frame of reference holds.
            px, py = mx, my
        else:
            try:
                x11 = _lib()
                dpy, _win = _handles(window)
                px, py = _root_pointer(x11, dpy, x11.XDefaultRootWindow(dpy))
            except Exception:
                px = py = None
        if px is not None:
            if _rdrag is None:
                ww, wh = glfw.get_window_size(window)
                wx, wy = (0, 0) if wayland else glfw.get_window_pos(window)
                # Corner pick: bottom-right gets most of the window - the
                # top/left grabs only apply within the first _GRAB_BAND px of
                # their edge (halved on windows too small for two full bands,
                # so tiny windows still resize sensibly). Wayland: always the
                # bottom-right - a left/top grab would need the window placed
                # it, and clients can't position themselves there.
                band_x = min(_GRAB_BAND, ww / 2)
                band_y = min(_GRAB_BAND, wh / 2)
                if both:
                    grab = (False, False)
                else:
                    grab = (True, True) if wayland else (mx >= band_x, my >= band_y)
                _rdrag = (float(wx), float(wy), float(wx + ww), float(wy + wh),
                          px, py, grab, _workarea_for(window))
            grab_right, grab_bottom = _rdrag[6]
            direction = ((_SIZE_BOTTOMRIGHT if grab_right else _SIZE_BOTTOMLEFT)
                         if grab_bottom else
                         (_SIZE_TOPRIGHT if grab_right else _SIZE_TOPLEFT))
            mouse_cursor.request(_EDGE_CURSOR[direction])
            _apply_rdrag_resize(window, px, py)

    # --- paint the buttons (topmost, after all the logic) ------------------
    _paint_buttons(dl, button_rects, over_button, maximized)


# ---------------------------------------------------------------------------
# Rounded window corners - a transparent framebuffer + a final alpha pass
# ---------------------------------------------------------------------------

def wants_transparent_framebuffer():
    """Boot hint (GLFW TRANSPARENT_FRAMEBUFFER): only a frameless window
    with a corner radius needs per-pixel alpha at the compositor."""
    from src.lsd.gl_gui.toggles import Toggles
    return titlebar_enabled() and Toggles.Melty.window_corner_radius > 0


_CORNER_FRAG = """
#version 330 core
out vec4 FragColor;
void main() {
    // Signed distance to the rounded rect covering the whole framebuffer
    // (gl_FragCoord is pixel-centred, origin bottom-left), 0 on the edge.
    vec2 half_size = size * 0.5;
    vec2 d = abs(gl_FragCoord.xy - half_size) - (half_size - vec2(radius));
    float dist = length(max(d, vec2(0.0))) + min(max(d.x, d.y), 0.0) - radius;
    // Anti-aliased: one pixel of ramp across the edge.
    FragColor = vec4(0.0, 0.0, 0.0, 1.0 - smoothstep(-0.5, 0.5, dist));
}
"""


@shader_func(fragment=_CORNER_FRAG)
def _corner_alpha_pass(gl_state: GLState = None, size=(1.0, 1.0), radius=0.0, **kwargs):
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)


# One GLState for the pass, module-owned (hotswap keeps it).
_corner_gl = globals().get("_corner_gl")


def punch_rounded_corners(fb_w, fb_h):
    """Last GL work of the frame (Melty.post_frame, after the overlay): write
    the framebuffer's ALPHA only — 1 inside the rounded rect, 0 outside —
    so the compositor clips the corners and every pixel imgui's blending
    left translucent (dst_a = a² + dst_a·(1-a) < 1 over an opaque clear)
    reads opaque again. No-op unless the window was created transparent
    (wants_transparent_framebuffer at boot) and the radius is > 0."""
    global _corner_gl
    from src.lsd.gl_gui.toggles import Toggles
    radius = float(Toggles.Melty.window_corner_radius)
    if radius <= 0 or fb_w <= 0 or fb_h <= 0 or not is_gl_thread():
        return False
    if _corner_gl is None:
        _corner_gl = GLState()
    saved_mask = gl.glGetBooleanv(gl.GL_COLOR_WRITEMASK)
    blend = gl.glIsEnabled(gl.GL_BLEND)
    scissor = gl.glIsEnabled(gl.GL_SCISSOR_TEST)
    depth = gl.glIsEnabled(gl.GL_DEPTH_TEST)
    stencil = gl.glIsEnabled(gl.GL_STENCIL_TEST)
    try:
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glViewport(0, 0, int(fb_w), int(fb_h))
        gl.glDisable(gl.GL_BLEND)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_TRUE)
        _corner_alpha_pass(_corner_gl, size=(float(fb_w), float(fb_h)), radius=radius)
        return True
    finally:
        gl.glColorMask(*[gl.GL_TRUE if bool(m) else gl.GL_FALSE for m in saved_mask])
        (gl.glEnable if blend else gl.glDisable)(gl.GL_BLEND)
        (gl.glEnable if scissor else gl.glDisable)(gl.GL_SCISSOR_TEST)
        (gl.glEnable if depth else gl.glDisable)(gl.GL_DEPTH_TEST)
        (gl.glEnable if stencil else gl.glDisable)(gl.GL_STENCIL_TEST)
        gl.glBindVertexArray(0)