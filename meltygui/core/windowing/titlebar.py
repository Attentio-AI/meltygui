"""Custom application titlebar (Toggles.Melty.enhanced_titlebar).

Removes the server-side decoration so the UI extends to the top of the
display, draws the window controls as overlay-drawlist widgets in the top
corners — WHICH of minimize / maximize / close, and on which side, follows
the desktop's own title-bar button setting (titlebar_buttons.py, or
Toggles.Melty.titlebar_button_layout; an OS window whose root meltygui window
has a header hosts them inside that header, draw_header_controls) — and
hands drag / edge-resize back to the window manager via
_NET_WM_MOVERESIZE — so snapping, tiling and drag smoothness stay native.

Backend support. X11 (and XWayland): everything above. Native Wayland:
GLFW has no public route to xdg_toplevel.move()/resize(), so the OS window
KEEPS a frame there — GLFW's own fallback frame (a caption strip + borders,
compositor-driven move/resize) once Toggles.Melty.wayland_native_frame has
switched libdecor off at the first glfw.init (glfw_utils
.apply_wayland_frame_hint) — or, with Toggles.Melty.wayland_show_frame off
(the default), no frame at all. This module then contributes the buttons
(always on Wayland; Toggles.Melty.enhanced_titlebar is an X11 knob), the
right-drag resize (a cursor-driven drag of the OS window's own frame edges
through the edge physics, gl_gui/os_frame.py — bottom-right corner, or the
top-left on a DOUBLE right-drag, the meltygui windows' rule) and, through
gl_gui/wayland_move.py, the SAME strip / drag-anywhere move and edge resize
as X11: xdg_toplevel.move / .resize sent straight to the compositor (what
Super+drag and the caption strip do), the grab then driven by GNOME. With libdecor still on, its own
title bar carries the buttons and nothing here draws (backend_supported
is False).

Everything here runs on the visualization thread inside the imgui frame
(called from LSDStudio.render). The decoration attribute is synced live
each frame, so flipping the toggle takes effect without a restart.
"""

import ctypes
import time

import meltygui.core.windowing.window_api as glfw
import meltygui_imgui as imgui
import OpenGL.GL as gl

import meltygui.core.input.mouse_cursor as mouse_cursor
import meltygui.core.windowing.titlebar_buttons as titlebar_buttons
import meltygui.core.windowing.wayland_move as wayland_move
import meltygui.core.input.hypr_left_drag as hypr_left_drag
from meltygui.core.graphics.gl_state import GLState
from meltygui.core.graphics.gl_state import is_gl_thread
from meltygui.core.graphics.shader_func import shader_func

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

# Right-drag resize gesture: {"top_left": bool, "x": last total_dx, "y":
# last total_dy}, latched on the first dragged frame and dropped on release.
_rdrag = None


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
        from meltygui.core.windowing.glfw_utils import wayland_native_frame_active
        return wayland_native_frame_active()
    return True


def titlebar_enabled():
    """X11: Toggles.Melty.enhanced_titlebar (it replaces the WM frame, an
    opt-in). Wayland: whenever the native frame is up — GLFW's fallback frame
    carries no buttons, so ours are the only min/max/close the window gets,
    and the toggle is not consulted."""
    if _on_wayland():
        return backend_supported()
    from meltygui.core.runtime.toggles import Toggles
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
        from meltygui.core.runtime.toggles import Toggles
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
    """EWMH resize direction for a pointer at (mx, my), or None. An
    off-window pointer — the backend's (-1, -1) / -FLT_MAX sentinel — is
    never on an edge (it used to read as the top-left corner)."""
    if mx < 0 or my < 0:
        return None
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
    from meltygui.core.melty import Melty
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
    if _maximized(window):
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
        # The SURFACE may overhang the workarea by the shadow margin on
        # every side (the window client is the content), so the cap is
        # the workarea plus twice the margin.
        _ax, _ay, aw, ah = glfw.get_monitor_workarea(glfw.get_primary_monitor())
        ox, oy = content_origin()
        inset = window_inset()
        return 0.0, 0.0, float(aw + ox + inset), float(ah + oy + inset)
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


# FontAwesome glyphs (merged in the default UI font - see fonts._fa_merge),
# same icon set the rest of the app draws with.
_ICON_MINIMIZE = "\uf2d1"   # fa window-minimize
_ICON_MAXIMIZE = "\uf2d0"   # fa window-maximize
_ICON_RESTORE = "\uf2d2"    # fa window-restore
_ICON_CLOSE = "\uf00d"      # fa times
_ICON_MOVE = "\uf0b2"       # fa arrows-alt - the desktop's left-drag move toggle (hypr_left_drag)
_ICONS = (_ICON_MINIMIZE, _ICON_MAXIMIZE, _ICON_RESTORE, _ICON_CLOSE, _ICON_MOVE)


def _button_icon(kind, maximized):
    if kind == "minimize":
        return _ICON_MINIMIZE
    if kind == "maximize":
        return _ICON_RESTORE if maximized else _ICON_MAXIMIZE
    if kind == "move":
        return _ICON_MOVE
    return _ICON_CLOSE


def button_layout():
    """(left kinds, right kinds) the chrome shows — each a tuple over
    "minimize" / "maximize" / "close", in on-screen order: Toggles.Melty
    .titlebar_button_layout (GNOME syntax) when set, else the desktop's own
    title-bar button setting (titlebar_buttons.system_layout: gsettings,
    kwinrc, xfwm4, the settings portal)."""
    from meltygui.core.runtime.toggles import Toggles
    pinned = Toggles.Melty.titlebar_button_layout
    if pinned:
        return titlebar_buttons.parse_gnome(pinned)
    return titlebar_buttons.system_layout()


def drag_anywhere_enabled():
    """Whether an unclaimed left drag on bare background moves the OS window
    through meltygui's OWN xdg_toplevel.move this frame:
    Toggles.Melty.move_drag_anywhere, and — on the patched Hyprland that
    carries the window-gesture toggle (hypr_left_drag) — only while the
    compositor's own left-drag move does NOT apply to this app's class
    (the chrome's "move" button faded). Lit, the compositor drives the
    move itself and meltygui stands down; faded (the studio always, an app
    after a click), meltygui moves and resizes the window itself. The drag
    strip is the app's caption and keeps moving either way."""
    from meltygui.core.runtime.toggles import Toggles
    if not Toggles.Melty.move_drag_anywhere:
        return False
    if hypr_left_drag.available():
        return not hypr_left_drag.enabled()
    return True


def control_kinds():
    """button_layout() plus the desktop's own extra: on Lukas's patched
    Hyprland (hypr_left_drag.available — the compositor has
    general:left_drag_move) a "move" toggle, the hyprbars
    `state = "left_drag_move"` button mirrored, unless
    Toggles.Melty.titlebar_move_toggle hides it. It sits INNERMOST of the
    right group like the hyprbars one (before minimize / maximize / close),
    or ends the left group when everything is on the left."""
    from meltygui.core.runtime.toggles import Toggles
    left_kinds, right_kinds = button_layout()
    if not (Toggles.Melty.titlebar_move_toggle and hypr_left_drag.available()):
        return left_kinds, right_kinds
    if right_kinds or not left_kinds:
        return tuple(left_kinds), ("move",) + tuple(right_kinds)
    return tuple(left_kinds) + ("move",), tuple(right_kinds)


def _button_metrics():
    """(margin, gap, height, {kind: width}) shared by both groups: the
    header's close button sizing (flat_button: glyph + px(15) wide, + px(8)
    tall — every kind its own glyph, like the header) inset by
    button_margin from the corner, button_gap apart."""
    from meltygui.core.melty import Melty
    # [tint=(1.0, 0.55, 0.2)]
    button_margin = Melty.px(4.0)
    # [tint=(1.0, 0.55, 0.2)]
    button_gap = Melty.px(3.0)
    sizes = {icon: imgui.calc_text_size(icon) for icon in _ICONS}
    height = max(s.y for s in sizes.values()) + Melty.px(8.0)
    return button_margin, button_gap, height, {icon: s.x + Melty.px(15.0) for icon, s in sizes.items()}


def _button_layout(disp_w, maximized):
    """[(kind, icon, rect)] of every control the layout asks for, in
    on-screen order: the left group from the top-left corner, the right
    group right-aligned at the top-right, each inset button_margin from
    its corner and button_gap apart."""
    left_kinds, right_kinds = control_kinds()
    button_margin, button_gap, height, widths = _button_metrics()
    buttons = []
    x0 = button_margin
    for kind in left_kinds:
        icon = _button_icon(kind, maximized)
        width = widths[icon]
        buttons.append((kind, icon, (x0, button_margin, x0 + width, button_margin + height)))
        x0 += width + button_gap
    right = []
    x1 = disp_w - button_margin
    for kind in reversed(right_kinds):
        icon = _button_icon(kind, maximized)
        width = widths[icon]
        right.append((kind, icon, (x1 - width, button_margin, x1, button_margin + height)))
        x1 -= width + button_gap
    right.reverse()
    return buttons + right


def _button_bands(buttons, disp_w):
    """(left_edge, right_edge): the x range between the two control groups
    — the drag strip's span (a corner group's whole column is the
    buttons', not the strip's). The window's edges when a side is empty."""
    left_edge = max((r[2] for kind, icon, r in buttons if r[0] < disp_w / 2), default=0.0)
    right_edge = min((r[0] for kind, icon, r in buttons if r[0] >= disp_w / 2), default=disp_w)
    return left_edge, right_edge


def chrome_insets():
    """(left_px, right_px) the window controls take from the window's top
    corners this frame — what a meltygui header drawn in the chrome row keeps
    clear on each side (surface.root_view_kwargs: `header_indent` on the
    left, a reserving `with_header_end` on the right). 0 for an empty side
    or with the chrome off."""
    if not titlebar_enabled():
        return 0.0, 0.0
    left_kinds, right_kinds = control_kinds()
    button_margin, button_gap, height, widths = _button_metrics()
    maximized = _maximized(_studio_window())

    def group(kinds):
        if not kinds:
            return 0.0
        return button_margin + sum(widths[_button_icon(k, maximized)] for k in kinds) \
            + button_gap * len(kinds)

    return group(left_kinds), group(right_kinds)


# Melty.frame_count of the frame whose root view hosted the controls
# (draw_header_controls): paint_window_controls and draw_titlebar's button
# hits stand down for it - the header painted and clicked them.
_hosted_frame = -1


def draw_header_controls(draw_state=None, **kwargs):
    """The `with_header_end` of an OS window's root view (surface
    .root_view_kwargs): the window controls painted INSIDE the root's meltygui
    header, where a header's close button sits — the same flat_button paint
    as _paint_buttons, in the root's own tile (an overlay paint under a
    cached root tile was blitted over — the glyphs vanished, 09-12), hover
    through the wrapper's repaint of a bounding-hovered view, clicks through
    draw_state.on_action. In the flow it claims the RIGHT group's width, so
    the wrapper right-aligns it under those buttons and clips the header
    before them (draw_state.header_end_width); both groups are painted at
    their corner rects (the root fills the surface, so window and surface
    coordinates agree). The overlay path stands down for the frame
    (_hosted_frame)."""
    global _hosted_frame
    from meltygui.core.melty import Melty
    imgui.dummy(chrome_insets()[1], 0)
    if draw_state is None or not titlebar_enabled():
        return
    window = _studio_window()
    io = imgui.get_io()
    buttons = _button_layout(io.display_size.x, _maximized(window))
    mx, my = io.mouse_pos.x, io.mouse_pos.y
    over_button = next((i for i, (kind, icon, (x0, y0, x1, y1)) in enumerate(buttons)
                        if x0 <= mx <= x1 and y0 <= my <= y1), None)
    if not draw_state._bounding_hovered or Melty.on_drag:
        over_button = None
    _hosted_frame = Melty.frame_count
    _paint_buttons(imgui.get_window_draw_list(), buttons, over_button)
    for i, (kind, icon, (x0, y0, x1, y1)) in enumerate(buttons):
        # Same delivery as flat_button's own click (priority_delta=4 over
        # the body's registrations, the plain pointer over the button).
        if draw_state.on_action("left_mouse_clicked", view_id=f"titlebar_button_{i}",
                                rect=(x0, y0, x1, y1), priority_delta=4,
                                cursor=mouse_cursor.ARROW) is not None:
            _activate_button(kind, window)


def _activate_button(kind, window):
    """What a control does: minimize / maximize / close (the close deferred
    to the merge window while pending state would be lost) / the desktop's
    left-drag-move toggle for this app (hypr_left_drag.toggle)."""
    if kind == "minimize":
        glfw.iconify_window(window)
    elif kind == "maximize":
        _toggle_maximize(window)
    elif kind == "move":
        hypr_left_drag.toggle()
    elif _close_blocked_by_merge():
        pass        # merge window opened instead; app stays up
    else:
        _close_window(window)


def _studio_window():
    """The active OS window, accepting both native and GLFW handles.

    Keep mocked vis handles out: ctypes can coerce MagicMock to a garbage
    pointer. Native windows are Python objects with a class-level marker.
    """
    import ctypes
    from meltygui.core.melty import Melty
    window = Melty.glfw_window or getattr(Melty.vis, "window", None)
    return window if glfw.is_native_window(window) or isinstance(window, ctypes._Pointer) else None


def _main_window_ds():
    """The Main Window's draw_state (draw_main) — the owner the controls'
    flat-mask marks ride under. In an app (surface.py, no draw_main) the
    surface's root meltygui window: with its header in the chrome row its
    cached tile covers the corners, and an OWNERLESS mark never keeps a
    blit from copying over the buttons. None before the root's first
    frame."""
    from meltygui.core.melty import Melty
    registry = getattr(Melty, "draw_state_registry", None) or {}
    main = next((d for d in registry.values() if getattr(d, "name", None) == "Main Window"), None)
    if main is not None:
        return main
    roots = getattr(Melty, "root_draw_states", None) or {}
    return next((d for entries in roots.values() for d in entries), None)


def paint_window_controls(draw_list):
    """Paint the min/max/close controls — from draw_melty_windows, on the
    MAIN WINDOW draw list's top channel just before Melty.end_frame, i.e.
    BEFORE the capture/shadow passes (draw_titlebar keeps the hit logic).
    That placement is the whole point: the shadow composite paints each
    depth mark's drop shadow and lit rim INTO the framebuffer and the
    overlay list renders after it, so buttons drawn there covered their own
    rim with their background (the "rect in front of it" look). Painted
    here they get composited exactly like a header's close button.

    Each button also marks the FLAT mask at the top paint rank (owner: the
    Main Window's draw_state — an ownerless mask rect never terminates the
    mask build's parent walk), which is what keeps a cached window blitted
    under the corner from copying over them, and what gives them their
    depth for the shadow pass — the same mark a window gets."""
    global _pressed_button
    from meltygui.core.melty import Melty
    from meltygui.core.runtime.toggles import shadow_depth_at
    if not titlebar_enabled() or _hosted_frame == Melty.frame_count:
        return      # off, or the root's header painted them this frame (draw_header_controls)
    window = _studio_window()
    io = imgui.get_io()
    disp_w = io.display_size.x
    mx, my = io.mouse_pos.x, io.mouse_pos.y
    maximized = _maximized(window)
    buttons = _button_layout(disp_w, maximized)
    over_button = next((i for i, (kind, icon, (x0, y0, x1, y1)) in enumerate(buttons)
                        if x0 <= mx <= x1 and y0 <= my <= y1), None)
    # Top-most of everything painted: the highest paint rank + a little depth.
    layer = Melty.nested_layer_max - 1
    rank = shadow_depth_at(2, layer)
    owner = _main_window_ds()
    if Melty.cache is not None and owner is not None:
        for i, (kind, icon, (x0, y0, x1, y1)) in enumerate(buttons):
            Melty.cache.mask_mark_rect(owner, layer, rank, x0, y0, x1 - x0, y1 - y0,
                                       f"titlebar_button_{i}", corner_radius=Melty.px(6.0))
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.max_depth - 1)
    _paint_buttons(draw_list, buttons, over_button)


def _paint_buttons(dl, buttons, over_button):
    """The controls (`buttons` = _button_layout's list), each painted EXACTLY like a window header's
    close button (draw_header_end): the same flat_button call — colour
    (9, 1, 1), glyph + px(15) by glyph + px(8), rounded, theme-mixed glyph,
    hover brightening, a depth mark for the shadow pass (drop shadow + lit
    rim) — run under Toggles.Melty.melty_window_tint set in the style
    manager, the way a header runs under its window's tint (that tint is
    what make_color_rgb mixes the colour against). The previous tint is
    restored afterwards."""
    from meltygui.view.header_view import flat_button
    from meltygui.core.cache.tile_cache import add_shadow
    from meltygui.core.melty import Melty
    from meltygui.core.runtime.toggles import Toggles
    # The header close button's colour (draw_header_end).
    # [tint=(0.9, 0.15, 0.15)]
    close_color = (9, 1, 1)
    # The move toggle while left-drag move is OFF for this app: the
    # hyprbars button's fade — the glyph this grey (text_color, the
    # full-range bypass) on a disc this much darker (tint_value; 0.16 lit).
    # [tint=(0.9, 0.15, 0.15)]
    move_off_glyph = (0.4, 0.4, 0.4)
    # [tint=(0.9, 0.15, 0.15)]
    move_off_disc_value = 0.06
    style_manager = Melty.style_manager
    previous_tint = style_manager.get_tint() if style_manager is not None else None
    chrome_tint = Toggles.Melty.melty_window_tint
    if style_manager is not None and chrome_tint and len(chrome_tint) >= 3:
        style_manager.set_imgui_tint(*chrome_tint[:4])
    try:
        for i, (kind, icon, (x0, y0, x1, y1)) in enumerate(buttons):
            # The header close's own lift over its surface (+2 over the
            # flat-mask mark paint_window_controls stamped for the button).
            add_shadow((x0, y0, x1 - x0, y1 - y0), corner_radius=Melty.px(6.0), clip=False,
                       layer=Melty.nested_layer_max - 1, depth=2)
            faded = kind == "move" and not hypr_left_drag.enabled()
            flat_button(icon, None, view_id=f"titlebar_button_{i}",
                        width=x1 - x0, height=y1 - y0, pos=(x0, y0),
                        hovered=(over_button == i), layout=False, draw_list=dl,
                        color=close_color,
                        **({"text_color": move_off_glyph, "tint_value": move_off_disc_value}
                           if faded else {}))
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
        from meltygui.editor.pending_save import PendingSave
        if not PendingSave.needs_merge():
            return False
        from meltygui.core.runtime.extensions import get, call
        from meltygui.core.diagnostics.notifications import notify
        if get('conflicts_open') is None:
            notify('External changes conflict with pending edits; save or discard them before closing.', tint=(1.0, 0.7, 0.2))
            return True
        call('conflicts_open', auto_merge=True)
        notify("Unmerged external changes — merge before closing "
               "(Ctrl+M applies the staged merge)", tint=(1.0, 0.7, 0.2))
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
        return True
    except Exception:
        return False


def _close_window(window):
    """The close button's action. Flags the loop to exit and wakes it, so
    the should_close check runs right after THIS frame instead of on the
    next input event (the loop parks in glfw.wait_events). The surface
    itself is hidden by the loop's exit path (LSDStudio._run_visualization's
    finally) BEFORE the seconds-long teardown — never here, mid-frame: the
    frame that fires this still swaps, and swapping onto a surface GLFW has
    just unmapped is what the compositor-close callback path never does."""
    from meltygui.core.windowing.glfw_utils import request_render
    glfw.set_window_should_close(window, True)
    request_render()      # posts the empty event that wakes wait_events


def draw_titlebar(window):
    """Per-frame entry point — call inside the imgui frame on the viz thread.

    Handles decoration sync, the top-right window controls' hit logic
    (their paint is paint_window_controls, earlier in the frame), the top
    drag strip (double-click = maximize unless
    Toggles.Melty.disable_double_click_maximize; with Toggles.Melty.move_drag_anywhere
    an unclaimed left-drag anywhere moves too), edge/corner resize and the
    right-drag resize. The WM gestures go through _NET_WM_MOVERESIZE on X11
    and wayland_move (xdg_toplevel.move/resize) on Wayland.
    """
    global _pressed_button, _wm_move_started, _rdrag
    if not sync_decoration(window):
        _pressed_button = None
        _wm_move_started = False
        _rdrag = None
        return

    from meltygui.core.melty import Melty
    from meltygui.core.runtime.toggles import Toggles

    io = imgui.get_io()
    disp_w, disp_h = io.display_size.x, io.display_size.y
    mx, my = io.mouse_pos.x, io.mouse_pos.y
    maximized = _maximized(window)
    sync_window_geometry(window)
    sync_input_region(window)
    # The strip and edge gestures hand the drag to the window manager
    # (_begin_wm_move / _begin_wm_resize) - on Wayland only once wayland_move
    # is ready. The right-drag resize is app-driven (set_window_size) and
    # runs on both; on Wayland it reads the surface relative pointer and
    # always grabs the bottom-right corner (the top-left is pinned).
    wayland = _on_wayland()
    gestures = _wm_gestures_available()

    # --- the control buttons in the top corners, per the desktop's setup --
    # (titlebar_buttons: re-read while frames render, if a changed desktop
    # setting lands without a restart; the focus hook re-reads too)
    titlebar_buttons.refresh_if_stale(Toggles.Melty.titlebar_button_refresh_s)
    hypr_left_drag.refresh_if_stale(Toggles.Melty.titlebar_button_refresh_s)
    buttons = _button_layout(disp_w, maximized)
    strip_left, strip_right = _button_bands(buttons, disp_w)

    over_button = None
    for i, (kind, icon, (x0, y0, x1, y1)) in enumerate(buttons):
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
    # (a root header hosting the controls clicks them itself -
    # draw_header_controls - so only the strip / anywhere gating hits
    # over_button on such a frame)
    hosted = _hosted_frame == Melty.frame_count
    if hosted:
        _pressed_button = None
    elif over_button is not None and imgui.is_mouse_clicked(0):
        _pressed_button = over_button
    if _pressed_button is not None and imgui.is_mouse_released(0):
        if over_button == _pressed_button and _pressed_button < len(buttons):
            _activate_button(buttons[_pressed_button][0], window)
        _pressed_button = None

    # --- drag strip along the top edge -------------------------------------
    # Register as the lowest-priority drag subscriber while the cursor is in
    # the strip; the handler's down-capture means a view that uses
    # click-and-drag always wins, and the strip only sees the drag when
    # nothing else claimed it. Clean clicks are untouched (dragged fires
    # only past the handler's drag threshold). Double-click = maximize,
    # same lowest-priority rule.
    # drag_anywhere_enabled (Toggles.Melty.move_drag_anywhere, gated by the
    # desktop's left-drag-move toggle where it exists) widens the drag (not
    # the double-click) to the whole window at the same worst priority: even
    # a drag nobody else claimed - bare background - moves the OS window.
    # Toggles.Melty.disable_double_click_maximize drops the double-click
    # subscription entirely, so the click reaches the view under the cursor.
    # Both the strip and the right-drag resize below subscribe NON-BLOCKING:
    # a closable meltygui window is an event blocker (core_render registers
    # `blocker=closable`) and a meltygui app's root IS such a window, pinned
    # over the whole surface — a plain subscription at worst priority sat
    # behind that blocker and was dropped before dispatch (the app's left /
    # right drags never reached the OS chrome; the studio's roots leave its
    # background bare, which is why they worked there). A non_blocking
    # subscription survives the blocker pass and still resolves LAST, so a
    # view that claims the drag keeps winning (input_handler: the
    # non-blocking chain stops at the first blocking subscriber).
    strip_h = float(Toggles.Melty.drag_strip_height)
    in_strip = my <= strip_h and strip_left <= mx < strip_right
    strip_double_click = in_strip and not Toggles.Melty.disable_double_click_maximize
    anywhere = drag_anywhere_enabled() and over_button is None
    if gestures and edge is None and (in_strip or anywhere):
        # Move pointer only where the strip would actually get the drag
        # (cursor_gate): bare background, never over a view that claims it.
        Melty.event_handler.register_hovered(
            _STRIP_ID,
            ["non_blocking_left_mouse_dragged", "non_blocking_left_mouse_double_clicked"]
            if strip_double_click else ["non_blocking_left_mouse_dragged"],
            priority=_STRIP_PRIORITY,
            cursor=mouse_cursor.MOVE if Toggles.Melty.window_move_cursor else None,
            cursor_gate="left_mouse_dragged")
    strip_events = (getattr(Melty, "events", None) or {}).get(_STRIP_ID, {})
    if _wm_move_started and not Melty.event_handler.is_down("left_mouse"):
        _wm_move_started = False  # synthetic release landed - re-arm
    if "non_blocking_left_mouse_double_clicked" in strip_events \
            and not Toggles.Melty.disable_double_click_maximize:
        _toggle_maximize(window)
    elif "non_blocking_left_mouse_dragged" in strip_events and not _wm_move_started:
        _wm_move_started = True
        if _begin_wm_move(window):
            return

    # --- right-drag resize, anywhere in the window -------------------------
    # Same worst-priority pattern as the strip, but over the WHOLE window:
    # any view that actually uses a right drag (view orbits, meltygui window
    # resize) captures it first, and plain right-CLICKS are untouched
    # (dragged only fires past the handler's drag threshold, so context
    # menus keep working). The drag is a cursor-driven drag of the OS
    # window's OWN frame edges through the edge physics (os_frame.queue_drag
    # → the roots' frame passes): the bottom-right corner, or the top-left
    # on a DOUBLE right-drag (press-press-drag - the meltygui windows' rule),
    # decided at the press for the whole gesture. The screen's work area is
    # the wall; an edge blocked there grows the window on the other side,
    # just like a meltygui window's edge against the display.
    if not maximized and over_button is None and edge is None:
        Melty.event_handler.register_hovered(
            _RESIZE_ID, ["non_blocking_right_mouse_dragged", "non_blocking_right_mouse_double_dragged"],
            priority=_STRIP_PRIORITY)
    # (the drag events themselves are consumed by poll_os_window_drag at
    # the frame's START - before the root windows solve - so the OS edge
    # moves in the same frame the hand did)
    if _rdrag is not None:
        mouse_cursor.request(_EDGE_CURSOR[_SIZE_TOPLEFT if _rdrag["top_left"] else _SIZE_BOTTOMRIGHT])

    # The buttons themselves are painted earlier in the frame, in the main
    # window draw list (paint_window_controls) - see there for why.


def poll_os_window_drag():
    """Frame START (draw_melty_windows, right after os_frame.begin_frame):
    turn this frame's right-drag events on the background (_RESIZE_ID,
    registered by draw_titlebar) into cursor-driven drags of the OS
    window's frame edges — os_frame.queue_drag with the drag total's
    per-frame increment — so the first root window's pass solves them in
    THIS frame. Polled at the end of the frame (draw_titlebar runs after
    the meltygui windows and os_frame.flush) the drag landed a frame late."""
    global _rdrag
    from meltygui.core.melty import Melty
    handler = getattr(Melty, "event_handler", None)
    if handler is None:
        return
    resize_events = (getattr(Melty, "events", None) or {}).get(_RESIZE_ID, {})
    released = not handler.is_down("right_mouse")
    drag = (resize_events.get("non_blocking_right_mouse_double_dragged")
            or resize_events.get("non_blocking_right_mouse_dragged"))
    if drag is None:
        if released:
            _rdrag = None
        return
    import meltygui.core.windowing.os_frame as os_frame
    if _rdrag is None:
        _rdrag = {"top_left": "non_blocking_right_mouse_double_dragged" in resize_events, "x": 0.0, "y": 0.0}
    index = 0 if _rdrag["top_left"] else 1
    for axis, total in (("x", "total_dx"), ("y", "total_dy")):
        now = float(getattr(drag, total, 0.0) or 0.0)
        inc = now - _rdrag[axis]
        _rdrag[axis] = now
        os_frame.queue_drag(axis, index, inc)
    # The next frame can carry the drag total. Consume its remainder
    # against the old baseline before clearing it, not the whole drag again.
    if released:
        _rdrag = None


# ---------------------------------------------------------------------------
# Rounded window corners - a transparent framebuffer + a final alpha pass
# ---------------------------------------------------------------------------

def wants_transparent_framebuffer():
    """Boot hint (GLFW TRANSPARENT_FRAMEBUFFER): only a frameless window
    with a corner radius or a shadow margin needs per-pixel alpha at the
    compositor."""
    from meltygui.core.runtime.toggles import Toggles
    return titlebar_enabled() and (Toggles.Melty.window_corner_radius > 0
                                   or Toggles.Melty.window_shadow_margin > 0)


def _frame_transparent(window):
    """Was the OS window CREATED with an alpha framebuffer (the boot hint)?
    Only then do the corner cut and the shadow margin make sense — on an
    opaque window the premultiply would paint the corners black."""
    try:
        return bool(window is not None
                    and glfw.get_window_attrib(window, glfw.TRANSPARENT_FRAMEBUFFER))
    except Exception:
        return False


def _maximized(window):
    """Is the OS window maximized? The ONE read every maximize-sensitive
    path takes (the shadow inset, the button glyph, the edge zones, the
    background right-drag's registration). GLFW's MAXIMIZED attribute
    everywhere but Hyprland: Hyprland sends the xdg `maximized` state to
    EVERY toplevel at map, to stop clients drawing their own decorations
    (XDGShell.cpp, "this forces apps to not draw CSD"), and never clears
    it — so GLFW read the studio as maximized for its whole life there:
    inset 0, no edge zones, and draw_titlebar never registered the
    background right-drag (09-09, "right-drag does nothing on
    Hyprland"). There the feed's own flag is the truth: Hyprland's
    `fullscreen == 1` (the maximize fullscreen mode) on the studio's
    client, False until the feed has seen the window."""
    if window is None:
        return False
    if _on_hyprland():
        # Whatever the compositor does with the window geometry, it still
        # forces the maximized state at map (XDGShell.cpp) - reading GLFW's
        # attribute on the state-honouring build zeroed the inset and
        # dropped the right-drag again (09-10, first login on that build).
        import meltygui.core.windowing.geometry_feed as geometry_feed
        frame = geometry_feed._STATE.get("frame") or {}
        return bool(frame.get("maximized", False))
    try:
        return bool(glfw.get_window_attrib(window, glfw.MAXIMIZED))
    except Exception:
        return False


def shadow_reach(fb_w, fb_h):
    """px the shadow pass reaches past a caster's edge on a fb_w × fb_h
    framebuffer, from ShadowCast's own terms: it marches 16 depth slices of
    1/max_steps above the receiver, each contributing
    hit_strength − height·hit_falloff (so the height budget is the smaller
    of the 16 slices and hit_strength/hit_falloff), offsets the sample by
    height·height_scale along the light direction — in UV, so the reach in
    px grows with the framebuffer — and blurs by height·blur_scale. The
    larger of the x/y reaches, rounded UP to 16 px so small resizes don't
    wobble the margin. Measured against the real pass (4072×2136: predicts
    69/49 px right/down, the pass fades out by 57/42). 0 with the shadow
    pass off."""
    import math
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.core.melty import Melty
    if not Toggles.filters or fb_w <= 0 or fb_h <= 0:
        return 0
    total_layers = 100.0 / ((Melty.max_layer - 1.0) * (Melty.max_depth - 1.0))
    depth_step = 1.0 / max(1.0, total_layers * 65535.0 / 2.0)     # post_frame: max_steps = diff / 2
    height = 16.0 * depth_step
    hit_falloff = float(Toggles.shadow_hit_falloff)
    if hit_falloff > 0.0:
        height = min(height, float(Toggles.shadow_hit_strength) / hit_falloff)
    offset_uv = height * (float(Toggles.shadow_height_scale) + float(Toggles.shadow_blur_scale))
    lx, ly = Toggles.shadow_light_dir
    norm = math.hypot(lx, ly) or 1.0
    reach = max(offset_uv * abs(lx) / norm * fb_w, offset_uv * abs(ly) / norm * fb_h)
    return int(math.ceil(reach / 16.0)) * 16


def _monitor_size(window):
    """Pixel size of the primary monitor's current video mode — the most a
    surface can span, so the reach derived from it is a CONSTANT."""
    try:
        mode = glfw.get_video_mode(glfw.get_primary_monitor())
        return int(mode.size.width), int(mode.size.height)
    except Exception:
        return None


def window_inset():
    """px of transparent shadow margin around the content THIS frame: on
    the transparent frameless window, the larger of
    Toggles.Melty.window_shadow_margin and the shadow pass's reach
    (shadow_reach — a fixed margin truncated the shadow mid-fall, a hard
    band); 0 while maximized / fullscreen (the shadow collapses against
    the screen edges, as GTK's does) and 0 everywhere else. The reach is
    taken at the MONITOR's size, not the live surface: derived from the
    surface it changed mid-resize whenever a 16 px rounding boundary went
    by, and every change is a visible jump — the content shrinks by twice
    it, the geometry origin moves so the compositor shifts the surface,
    the right-drag's latched cap goes stale (a gap at the top) — and a
    resize hovering at the boundary flickered the margin on and off. The
    surface simply overhangs a little more than it needs on a small
    window. imgui's display is the content: SplitOverlayRenderer
    .process_inputs shrinks display_size by twice this and shifts the
    pointer; the masks and tiles keep the whole surface."""
    from meltygui.core.runtime.toggles import Toggles
    margin = int(Toggles.Melty.window_shadow_margin)
    if margin <= 0:
        return 0
    window = _studio_window()
    if not _frame_transparent(window) or _maximized(window) or _fullscreen(window):
        return 0
    size = _monitor_size(window)
    if size:
        margin = max(margin, shadow_reach(int(size[0]), int(size[1])))
    return margin


def _fullscreen(window):
    """GLFW-fullscreen (a monitor set on the window). pyglfw hands back a
    ctypes POINTER even when it is NULL — never compare it to None, a null
    pointer is FALSY, not None (that mistake collapsed the margin to 0)."""
    try:
        return window is not None and bool(glfw.get_window_monitor(window))
    except Exception:
        return False


def frame_geometry(fb_w, fb_h):
    """(origin, radius, content_size) of the frame for this frame's REAL
    framebuffer — origin the content's top-left in the surface — or
    ((0, 0), 0, (0, 0)) when the window is not transparent: the shadow
    composite's frame uniforms (no frame → everything is content)."""
    if not _frame_transparent(_studio_window()):
        return (0.0, 0.0), 0.0, (0.0, 0.0)
    inset = float(window_inset())
    ox, oy = content_origin()
    return (ox, oy), frame_corner_radius(), (float(fb_w) - ox - inset, float(fb_h) - oy - inset)


def content_origin():
    """(x, y) of the content's top-left inside the surface: the shadow
    margin; (0, 0) while maximized / fullscreen."""
    window = _studio_window()
    if _maximized(window) or _fullscreen(window):
        return (0.0, 0.0)
    inset = float(window_inset())
    return (inset, inset)


def frame_corner_radius():
    """Corner radius of the content this frame: the toggle, 0 while
    maximized (square against the screen edge)."""
    from meltygui.core.runtime.toggles import Toggles
    if _maximized(_studio_window()):
        return 0.0
    return float(Toggles.Melty.window_corner_radius)


# Coverage of the CONTENT rounded rect - inset `inset` px into the
# framebuffer, `content_size` wide, `radius` corners - at a pixel centre
# (gl_FragCoord, origin bottom-left; the rect is symmetric so the flip is
# free). One pixel of anti-aliasing ramp across the edge.
_COVERAGE_GLSL = """
float coverage(vec2 p) {
    vec2 half_size = content_size * 0.5;
    vec2 d = abs(p - origin - half_size) - (half_size - vec2(radius));
    float dist = length(max(d, vec2(0.0))) + min(max(d.x, d.y), 0.0) - radius;
    return 1.0 - smoothstep(-0.5, 0.5, dist);
}
"""

_CORNER_FRAG = """
#version 330 core
out vec4 FragColor;
""" + _COVERAGE_GLSL + """
void main() {
    FragColor = vec4(0.0, 0.0, 0.0, coverage(gl_FragCoord.xy));
}
"""

@shader_func(fragment=_CORNER_FRAG)
def _corner_alpha_pass(gl_state: GLState = None, content_size=(1.0, 1.0), origin=(0.0, 0.0),
                       radius=0.0, **kwargs):
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)


# The GLState for the passes, module-owned (hotswap keeps it).
_corner_gl = globals().get("_corner_gl")
# The input rect last handed to the compositor (sync_input_region).
_input_rect_applied = globals().get("_input_rect_applied")
# The window geometry last handed to the compositor (sync_window_geometry).
_geometry_applied = globals().get("_geometry_applied")
# True while OUR glfw.set_window_size is in flight - its framebuffer-size
# callback is not a compositor configure (on_surface_resized).
_self_resize = False
# The surface size the framebuffer callback last reported (or the creation
# size, note_surface_size): a callback repeating it is NOT a configure.
_last_surface_size = globals().get("_last_surface_size")


def note_surface_size(window):
    """Record the window's current framebuffer size as the last one seen —
    Surface.__init__, right after creation: GLFW re-fires the framebuffer
    callback with the UNCHANGED size when the surface enters an output or
    gets its fractional scale, and on_surface_resized read that as a
    compositor configure and regrew the surface by the margin once too
    often at every launch (09-12)."""
    global _last_surface_size
    try:
        _last_surface_size = tuple(int(v) for v in glfw.get_framebuffer_size(window))
    except Exception:
        _last_surface_size = None


def _on_hyprland():
    """The Hyprland backend (whatever it does with the window geometry):
    the box is resized through its IPC, and its `maximized` state is the
    feed's flag, not GLFW's."""
    import meltygui.core.windowing.geometry_feed as geometry_feed
    return geometry_feed.backend() == "hyprland"


def _box_is_surface():
    """A Hyprland that does NOT honour the xdg window geometry: the
    compositor's window box IS the surface — it renders the whole surface
    at `at` and ignores the geometry for placement (geometry_feed's module
    docstring). Every place that reads a configure as a GEOMETRY size, or
    resizes by committing a buffer, branches on this. The patched
    compositor (`render:xdg_window_geometry`, 09-09) makes the box the
    CONTENT — then this is False and the GNOME handling applies: the
    geometry is the content rect, a configure names the content and the
    surface is regrown around it by the margin (geometry_feed
    .hypr_honors_geometry)."""
    import meltygui.core.windowing.geometry_feed as geometry_feed
    return geometry_feed.backend() == "hyprland" and not geometry_feed.hypr_honors_geometry()


def set_surface_size(window, width, height, offset=None, box=True):
    """The one way this module resizes the OS surface: flags the resulting
    framebuffer-size callback as ours, so on_surface_resized leaves it
    alone (a compositor configure would be grown by the margin). On
    Hyprland the box is ALSO resized through its IPC — the buffer alone
    leaves the box where it was — and ``offset`` (dx, dy), the move that
    rides the same commit elsewhere, goes into that SAME request
    (geometry_feed.hypr_set_box: resize + move anchored top-left, one
    eval); other backends ignore ``offset`` here and arm the buffer
    offset themselves (apply_pending_surface_size). On a Hyprland that
    honours the window geometry the box is the CONTENT: the IPC gets the
    surface size less the shadow margin on every side. ``box=False``
    (on_surface_resized's regrow after a compositor configure) skips the
    IPC: the box already has that size, and asking again would configure
    the same size back at GLFW and regrow once more, forever."""
    global _self_resize
    _self_resize = True
    try:
        glfw.set_window_size(window, int(width), int(height))
    finally:
        _self_resize = False
    if box and _on_hyprland():
        import meltygui.core.windowing.geometry_feed as geometry_feed
        dx, dy = offset if offset else (0, 0)
        box_w, box_h = int(width), int(height)
        if geometry_feed.hypr_honors_geometry():
            inset = int(window_inset())
            box_w, box_h = max(1, box_w - 2 * inset), max(1, box_h - 2 * inset)
        geometry_feed.hypr_set_box(box_w, box_h, dx, dy)


# An app-side resize requested mid-frame, applied at the next frame's start.
_pending_surface_size = globals().get("_pending_surface_size")


_pending_surface_offset = globals().get("_pending_surface_offset")
_frame_surface_offset = globals().get("_frame_surface_offset")
# The queued request also wants the CONTENT fitted into the work area
# (the launch restore - request_surface_size(fit=True)).
_pending_surface_fit = globals().get("_pending_surface_fit") or False
# Frames a request has waited for the Hyprland feed to see the window (its
# `address:` selector): the launch restore is filed before the feed's
# first poll. Bounded so a dead feed can't hold a request forever.
_pending_surface_wait = globals().get("_pending_surface_wait") or 0
PENDING_SURFACE_WAIT_FRAMES = 300


def request_surface_size(window, width, height, offset=None, fit=False):
    """Queue an app-side resize (the right-drag) for the top of the NEXT
    frame (apply_pending_surface_size, before process_inputs). Applied
    mid-frame it committed a new buffer size and geometry with content laid
    out for the old size — one frame of jelly on every drag step. Later
    requests in the same frame replace earlier ones. ``offset`` = (dx, dy)
    px to MOVE the window by in the same commit (the buffer offset of the
    swap's attach — wayland_move.set_surface_offset; the near-edge push:
    grow right by d and move left by d = the left edge moved out).
    ``fit`` (Hyprland, the launch restore): add to the move whatever keeps
    the CONTENT inside the work area at the new size — the compositor
    centred the window at its INITIAL size and the box grows anchored at
    its top-left, so a restored studio ran off the bottom and right of
    the screen (and the OS-edge walls then hold it there, 09-09)."""
    global _pending_surface_size, _pending_surface_offset, _pending_surface_fit
    _pending_surface_size = (int(width), int(height))
    _pending_surface_offset = tuple(int(v) for v in offset) if offset else None
    _pending_surface_fit = bool(fit)


def fit_offset(rect, size, area, inset):
    """(dx, dy) that moves a surface at ``rect`` (x, y, w, h on screen) to
    where its CONTENT — the surface at its new ``size`` less ``inset`` px
    of shadow margin on every side — lies inside the work ``area`` (x, y,
    w, h). A content larger than the area keeps its near (top / left)
    edge on the area's."""
    out = []
    for i in (0, 1):
        near = rect[i] + inset
        far = rect[i] + size[i] - inset
        d = min(0, (area[i] + area[i + 2]) - far)     # pull back past the far edge
        d = max(d, area[i] - near)                    # never past the near one
        out.append(int(d))
    return tuple(out)


def apply_pending_surface_size(window):
    """LSDStudio's loop, before process_inputs: apply the queued resize so
    this frame lays out at the new size. Returns the size applied."""
    global _pending_surface_size, _pending_surface_offset, _frame_surface_offset
    global _pending_surface_wait, _pending_surface_fit
    import meltygui.core.windowing.wayland_move as wayland_move
    import meltygui.core.windowing.os_frame as os_frame
    wayland_move.clear_surface_offset()          # last frame's offset is spent
    # The roots' passes re-base with the OS near edge's motion lands HERE,
    # with the move it compensates (os_frame: the solve booked it).
    os_frame.apply_rebase()
    size = _pending_surface_size
    if size is None or window is None:
        return None
    offset = _pending_surface_offset
    import meltygui.core.windowing.geometry_feed as geometry_feed
    if (geometry_feed.backend() == "hyprland" and geometry_feed._hypr_selector() is None
            and _pending_surface_wait < PENDING_SURFACE_WAIT_FRAMES):
        # Hyprland resizes the box only through its IPC, addressed by the
        # window handle the feed reports - before its first poll (the
        # launch restore, LSDStudio's create-window path) nothing can be
        # sent, so the request holds for the next frame. Applying it now
        # spent it on a bare glfw.set_window_size Hyprland ignores.
        _pending_surface_wait += 1
        # An app renders on request only (app.py's loop): with nothing
        # else asking, the held request never got its retry frame and a
        # @glfw_window stayed at Hyprland's default floating size until the
        # first hover (09-12). Ask for the frame that retries it.
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
        return None
    _pending_surface_wait = 0
    if _pending_surface_fit and geometry_feed.backend() == "hyprland":
        rect, area = geometry_feed.frame_rect(), geometry_feed.workarea()
        if rect is not None and area is not None:
            inset = int(window_inset())
            if geometry_feed.hypr_honors_geometry():
                # the feed's rect and the box are the CONTENT: fit the surface less its margin
                dx, dy = fit_offset(rect, (size[0] - 2 * inset, size[1] - 2 * inset), area, 0)
            else:
                dx, dy = fit_offset(rect, size, area, inset)
            if dx or dy:
                ox, oy = offset or (0, 0)
                offset = (ox + dx, oy + dy)
                os_frame.expect_own_move(dx, dy)     # ours, not the compositor's
    _pending_surface_fit = False
    _pending_surface_size = None
    _pending_surface_offset = None
    from meltygui.core.runtime.toggles import Toggles
    if Toggles.Melty.push_os_window_edges_trace:
        try:
            was = glfw.get_framebuffer_size(window)
        except Exception:
            was = None
        print(f"[os_frame] apply pending surface size {size} (was {was}) offset {offset}")
    import meltygui.core.windowing.geometry_feed as geometry_feed
    on_hyprland = _on_hyprland()
    # Hyprland ignores a toplevel's buffer offset (the surface stays
    # anchored at `at`): the move rides the box resize's own IPC request.
    set_surface_size(window, *size, offset=offset if on_hyprland else None)
    # The move rides the same commit: armed HERE, right after GLFW's resize
    # (which reset the EGL window's offset to 0) and before anything is
    # drawn. Arming it late - right before the swap - blanked every frame
    # that carried a move: wl_egl_window_resize flags the EGL surface as
    # resized, the driver re-creates its swapchain for that swap, and the
    # frame already rendered on the old buffers was lost (the window
    # vanished while the hand moved, reappeared at random). At frame start
    # the driver's resize handling runs BEFORE the frame is drawn, exactly
    # as for GLFW's own resizes. Nothing else touches the EGL window until
    # the swap.
    _frame_surface_offset = None
    if offset and (offset[0] or offset[1]) and not on_hyprland:
        wayland_move.set_surface_offset(offset[0], offset[1])
    return size


_last_stamped_size = globals().get("_last_stamped_size")


def on_surface_resized(window, width, height):
    """LSDStudio's framebuffer-size callback hook. With the window geometry
    inset to the content, a compositor configure (interactive resize from
    the edge zones, un-maximize, tiling) names a GEOMETRY size — GLFW makes
    the SURFACE that size, which would shrink the content by twice the
    margin. Grow the surface back right here, inside the callback, so the
    frame that follows is already right (a deferred fix would jitter the
    content edge on every configure of a drag). Our own resizes
    (set_surface_size) are flagged and pass through; maximized/fullscreen
    have no margin and pass through too. Returns the size applied."""
    global _last_surface_size, _last_stamped_size
    size = (int(width), int(height))
    if size != _last_stamped_size:
        # Any backend, ours or the compositor's: stamp the gesture for
        # freeze_resize views (Melty.resize_gesture_live); the cache keeps
        # frames coming past the last configure until they settle.
        from meltygui.core.melty import Melty
        from meltygui.core.windowing.glfw_utils import request_render
        _last_stamped_size = size
        Melty.os_resize_time = time.monotonic()
        request_render()
    if not _on_wayland() or not wayland_move.geometry_available():
        return None
    if _self_resize:
        # Our resize (the right-drag, a compensation): the geometry rides in
        # the SAME commit as the new buffer. If from draw_titlebar a frame
        # later it lagged one drag step behind - Mutter pushed the window
        # up against a stale rect (jitter) and the last step's growth never
        # got the push (a gap at the top).
        _last_surface_size = size
        sync_window_geometry(window, size)
        return None
    if size == _last_surface_size:
        # GLFW re-fired the callback with the size it already had (output
        # resize, fractional scale): no configure, nothing to regrow.
        return None
    _last_surface_size = size
    inset = window_inset()      # MAXIMIZED is already current inside GLFW's configure handling
    # A compositor-driven size names the GEOMETRY (the content): the
    # surface is regrown outside it by the margin on every side. The OS
    # edge model (os_frame.begin_frame) reads the new content size next
    # frame and folds it into the roots' passes as the near edge's motion.
    # Hyprland's configure names the SURFACE (the box): never regrow.
    if inset <= 0 or _box_is_surface():
        sync_window_geometry(window, (int(width), int(height)))
        return None
    grown = (int(width) + 2 * inset, int(height) + 2 * inset)
    set_surface_size(window, *grown, box=False)      # its nested callback syncs the geometry; the box IS this size
    return grown


def sync_window_geometry(window, size=None):
    """Wayland: keep xdg_surface.set_window_geometry on the CONTENT rect
    (the surface minus the shadow margin), the whole surface without one —
    re-marshalled only when it changes. From on_surface_resized with the
    new size (same commit as the buffer), and per frame from draw_titlebar
    as the catch-all (margin toggled, maximize state)."""
    global _geometry_applied
    if not _on_wayland() or not wayland_move.geometry_available():
        return False
    fb_w, fb_h = size if size is not None else glfw.get_framebuffer_size(window)
    inset = int(window_inset())
    if _box_is_surface():
        # Hyprland renders the surface at the box and ADDS the surface
        # origin to pointer coordinates (ViewHitTester.cpp) so an inset
        # geometry shifts every click by the margin. The whole surface.
        inset = 0
    rect = (inset, inset, max(1, int(fb_w) - 2 * inset), max(1, int(fb_h) - 2 * inset))
    if rect == _geometry_applied:
        return False
    if wayland_move.set_window_geometry(*rect):
        _geometry_applied = rect
        return True
    return False


def sync_input_region(window):
    """Wayland: keep the surface's input region on the CONTENT rect so
    clicks in the transparent shadow margin fall through to whatever is
    behind (wayland_move.set_input_rect); the whole surface when there is
    no margin. Re-marshalled only when the rect changes."""
    global _input_rect_applied
    if not _on_wayland() or not wayland_move.input_region_available():
        return False
    inset = window_inset()
    ox, oy = content_origin()
    if inset <= 0 and ox == 0 and oy == 0:
        rect = None
    else:
        fb_w, fb_h = glfw.get_framebuffer_size(window)
        rect = (int(ox), int(oy), max(1, int(fb_w - ox - inset)), max(1, int(fb_h - oy - inset)))
    if rect == _input_rect_applied:
        return False
    if wayland_move.set_input_rect(rect):
        _input_rect_applied = rect
        return True
    return False


def composite_window_frame(fb_w, fb_h):
    """Last GL work of the frame (Melty.post_frame, after the overlay): the
    frameless window's ALPHA, one fullscreen pass over the REAL framebuffer
    (fb_w × fb_h, the content inset window_inset() px) from the content's
    rounded-rect coverage `cov` (1 inside, 0 outside, a one-pixel ramp).

    With the shadow pass on (Toggles.filters), ShadowComposite has already
    rewritten everything outside the content as the shadow in premultiplied
    alpha — the OS window's shadow is that pass, nothing else draws one —
    so this only lifts the CONTENT's alpha back to 1: imgui's own blend
    leaves a translucent draw at dst_a = a² + dst_a·(1−a) < 1, through
    which the desktop would bleed. MAX-blended alpha, rgb untouched.

    Without it (no composite ran) the pass PREMULTIPLIES by the coverage
    instead: rgb·cov, alpha = cov. Wayland composites premultiplied alpha,
    so alpha 0 alone is not invisible — a view drawn over a cut corner, or
    the brightness pass lifting the cleared black, would be ADDED onto the
    desktop. No-op unless the window was created transparent; it runs
    maximized too (radius and inset 0), where it is purely the alpha lift."""
    global _corner_gl
    from meltygui.core.runtime.toggles import Toggles
    radius = frame_corner_radius()
    inset = float(window_inset())
    origin = content_origin()
    if fb_w <= 0 or fb_h <= 0 or not is_gl_thread():
        return False
    if not _frame_transparent(_studio_window()):
        return False
    # No early-out on radius/inset/origin all zero (maximized): the window
    # was created with an alpha channel, so the content's alpha must still
    # be lifted to 1 - the compositor blends the desktop through anything
    # below 1 whatever the corners look like.
    content_size = (float(fb_w) - origin[0] - inset, float(fb_h) - origin[1] - inset)
    if _corner_gl is None:
        _corner_gl = GLState()
    saved_mask = gl.glGetBooleanv(gl.GL_COLOR_WRITEMASK)
    blend = gl.glIsEnabled(gl.GL_BLEND)
    blend_func = (int(gl.glGetIntegerv(gl.GL_BLEND_SRC_RGB)), int(gl.glGetIntegerv(gl.GL_BLEND_DST_RGB)),
                  int(gl.glGetIntegerv(gl.GL_BLEND_SRC_ALPHA)), int(gl.glGetIntegerv(gl.GL_BLEND_DST_ALPHA)))
    blend_eq = (int(gl.glGetIntegerv(gl.GL_BLEND_EQUATION_RGB)), int(gl.glGetIntegerv(gl.GL_BLEND_EQUATION_ALPHA)))
    scissor = gl.glIsEnabled(gl.GL_SCISSOR_TEST)
    depth = gl.glIsEnabled(gl.GL_DEPTH_TEST)
    stencil = gl.glIsEnabled(gl.GL_STENCIL_TEST)
    try:
        from meltygui.core.melty import Melty
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, Melty.default_framebuffer())
        gl.glViewport(0, 0, int(fb_w), int(fb_h))
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glEnable(gl.GL_BLEND)
        if Toggles.filters:
            # alpha = max(dst_a, cov); rgb = dst
            gl.glBlendEquationSeparate(gl.GL_FUNC_ADD, gl.GL_MAX)
            gl.glBlendFuncSeparate(gl.GL_ZERO, gl.GL_ONE, gl.GL_ONE, gl.GL_ONE)
        else:
            # rgb = 0·src + dst·src_a ; alpha = src_a·1 + dst_a·0
            gl.glBlendEquationSeparate(gl.GL_FUNC_ADD, gl.GL_FUNC_ADD)
            gl.glBlendFuncSeparate(gl.GL_ZERO, gl.GL_SRC_ALPHA, gl.GL_ONE, gl.GL_ZERO)
        _corner_alpha_pass(_corner_gl, content_size=content_size, origin=origin, radius=radius)
        return True
    finally:
        gl.glColorMask(*[gl.GL_TRUE if bool(m) else gl.GL_FALSE for m in saved_mask])
        gl.glBlendFuncSeparate(*blend_func)
        gl.glBlendEquationSeparate(*blend_eq)
        (gl.glEnable if blend else gl.glDisable)(gl.GL_BLEND)
        (gl.glEnable if scissor else gl.glDisable)(gl.GL_SCISSOR_TEST)
        (gl.glEnable if depth else gl.glDisable)(gl.GL_DEPTH_TEST)
        (gl.glEnable if stencil else gl.glDisable)(gl.GL_STENCIL_TEST)
        gl.glBindVertexArray(0)
