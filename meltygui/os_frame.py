"""The GLFW window's edges as collision edges — the frame pair one level
outside the root melty windows.

columns.py cascades a drag through cells to a melty window's frame edge;
core_render's corner right-drag grows a window straight at its frame. Both
used to run out of room at the display edge: the far edge was pinned there
and the window slid the other way (push the near edge). With the frameless
OS window free to grow, the display edge is a MOVABLE edge: a far edge
overflowing it during a live drag first pushes the OS surface out by the
overflow (absorb — queued to the next frame's start like the right-drag,
so the frame lays out at one size), and only what the surface cannot take
— at its cap, the workarea (plus the shadow margin the surface may
overhang by) — falls back to the pin-and-slide (clamp_far_edge). Right and
bottom only: growing left/up means moving the window, which a Wayland
client cannot do (X11 could; not wired). Screen edge = the final barrier.

While the surface grows the compositor may slide the studio to keep it on
screen; SplitOverlayRenderer.process_inputs cancels that slide out of the
pointer (relative-pointer motion) so the drag stays 1:1 with the hand.
Gate: Toggles.Melty.push_os_window_edges.
"""
import glfw

_STATE = globals().get("_STATE") or {"push": [0.0, 0.0], "frame": -1,
                                      "grown": [0.0, 0.0], "pusher": [None, None],
                                      "near": {}, "glue": None, "shift": [0.0, 0.0]}
_STATE.setdefault("shift", [0.0, 0.0])
_STATE.setdefault("fit", None)
_STATE.setdefault("fit_last", None)
_STATE.setdefault("slid", [0.0, 0.0])
_STATE.setdefault("cap_hit", [None, None])
_STATE.setdefault("push_near", [0.0, 0.0])
_STATE.setdefault("near_requested", [0.0, 0.0])
_STATE.setdefault("near_slid", [0.0, 0.0])
_STATE.setdefault("near_glue", [None, None])


def _trace(msg):
    from src.lsd.gl_gui.toggles import Toggles
    if Toggles.Melty.push_os_window_edges_trace:
        print(f"[os_frame] {msg}")

_AXIS = {"x": 0, "y": 1}


def _live():
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.toggles import Toggles
    if not Toggles.Melty.push_os_window_edges:
        return False
    from src.lsd.gl_gui.view.core_views.columns import _drag_live
    return _drag_live()


def _surface_and_cap():
    """(surface size, cap size) in surface px, or None. Wayland: the cap
    is the workarea plus twice the shadow margin (the surface overhangs it
    by the margin, the geometry is the content). X11: the workarea from the
    window's own position."""
    from src.lsd.gl_gui import titlebar
    window = titlebar._studio_window()
    if window is None:
        return None
    try:
        size = glfw.get_framebuffer_size(window)
        wa_l, wa_t, wa_r, wa_b = titlebar._workarea_for(window)
        if titlebar._on_wayland():
            cap = (wa_r - wa_l, wa_b - wa_t)
        else:
            wx, wy = glfw.get_window_pos(window)
            cap = (wa_r - wx, wa_b - wy)
    except Exception:
        return None
    return (float(size[0]), float(size[1])), (float(cap[0]), float(cap[1]))


def _fresh():
    from src.lsd.gl_gui.melty import Melty
    if _STATE["frame"] != Melty.frame_count:
        _STATE["frame"] = Melty.frame_count
        _STATE["push"] = [0.0, 0.0]
        _STATE["near"] = {}


def absorb(axis, overflow, owner=None, past_edge=False):
    """A far edge on ``axis`` ("x" = right, "y" = bottom) is ``overflow``
    px past the display during a live drag (negative = that far INSIDE it).

    overflow > 0: push the OS edge out by as much of it as the surface may
    still grow; returns the px absorbed (0 when off, idle, or at the cap),
    the caller pins/slides the rest. The frame's push is the MAX of its
    requests, not their sum: the display edge only has to move far enough
    for the largest overflow, and the same overflow is reported twice for
    one window (core_render's corner clamp, columns._frame_pass).

    overflow < 0 from the ``owner`` that pushed this gesture: STICKY — the
    OS edge comes back by min(what it grew this gesture, the gap), so a
    drag that pushed the studio out and returns pulls it back in. Only the
    pusher unwinds (any other window sits inside the display by its own
    margin every frame). Returns 0. Gesture state resets on release
    (flush).

    ``past_edge``: grow THROUGH the compositor's keep-on-screen edge (the
    cap_hit below) up to the workarea-size cap — the surface grows on and
    Mutter slides the studio the other way to keep the geometry on screen,
    so the OS window's top / left moves 1:1 with the hand. push_far_edge
    asks for it only once the window inside has no slide left (its near
    edge on the display's): the melty window's top reaches the studio's
    top FIRST, then the studio's top gives (Lukas 08-26). The request is
    the frame's TOTAL for the axis (push is a MAX of requests)."""
    if not _live():
        if overflow > 0:
            _trace(f"absorb {axis} overflow={overflow:.0f}: not live (toggle/drag)")
        return 0.0
    i = _AXIS[axis]
    if overflow <= 0:
        grown = _STATE["grown"][i]
        if overflow < 0 and grown > 0 and owner is not None and _STATE["pusher"][i] == id(owner):
            _fresh()
            back = min(grown, float(-overflow))
            _STATE["push"][i] = min(_STATE["push"][i], -back)
            _trace(f"absorb {axis} gap={-overflow:.0f} grown={grown:.0f} → unwind {back:.0f}")
        return 0.0
    sizes = _surface_and_cap()
    if sizes is None:
        _trace(f"absorb {axis} overflow={overflow:.0f}: no surface/cap")
        return 0.0
    _fresh()
    (size, cap) = sizes
    # A Wayland client never knows where its surface sits on the screen, so
    # the cap above is the workarea size - right only for a window parked
    # at the workarea's top/left. Growing past the real edge makes the
    # compositor slide the whole studio the other way to keep the geometry
    # on screen (the OS window's top / left "pushed" before anything inside
    # touched it). That slide is measured every frame from the pointer
    # (note_surface_slide); the first one on an axis that grew this gesture
    # past the workarea edge is HERE - the cap is the surface size now, for
    # the rest of the gesture. One frame of growth overshoots before the
    # slide is seen; that much the studio cannot move.
    hit = _STATE["cap_hit"][i]
    if hit is None and _STATE["slid"][i] > 0 and _STATE["grown"][i] > 0:
        hit = _STATE["cap_hit"][i] = size[i]
        _trace(f"absorb {axis}: compositor slid the surface {_STATE['slid'][i]:.0f} px "
               f"— workarea edge reached, cap {cap[i]:.0f} → {hit:.0f}")
    limit = cap[i] if (hit is None or past_edge) else min(cap[i], hit)
    room = max(0.0, limit - size[i])
    absorbed = min(float(overflow), room)
    _trace(f"absorb {axis} overflow={overflow:.0f} surface={size[i]:.0f} cap={limit:.0f}"
           f"{' (past the compositor edge)' if past_edge and hit is not None else ''} "
           f"room={room:.0f} → {absorbed:.0f}")
    if absorbed <= 0.0:
        return 0.0
    _STATE["push"][i] = max(_STATE["push"][i], absorbed)
    if owner is not None:
        _STATE["pusher"][i] = id(owner)
    return absorbed


def push_far_edge(axis, abs_pos, size, display, owner, cap_size=True):
    """A window's far edge ``abs_pos + size`` past ``display`` during a
    live drag, resolved in order: (1) the OS edge out, up to the
    compositor's edge (absorb); (2) the window pinned at the display edge
    and slid the other way until its near edge is on the display's
    (clamp_far_edge); (3) what is still left THROUGH the compositor's edge
    (absorb past_edge) — the studio's own top / left then moves. Returns
    (size, slide) like clamp_far_edge."""
    overflow = abs_pos + size - display
    absorbed = absorb(axis, overflow, owner)
    new_size, slide = clamp_far_edge(abs_pos, size, display, absorbed, cap_size)
    left_over = overflow - absorbed - slide
    if left_over > 0.5:
        total = absorb(axis, absorbed + left_over, owner, past_edge=True)
        if total > absorbed:
            absorbed = total
            new_size, slide = clamp_far_edge(abs_pos, size, display, absorbed, cap_size)
    return new_size, slide


def note_surface_slide(slide_x, slide_y):
    """From SplitOverlayRenderer._cancel_surface_slide, every frame a
    button is held: how far the SURFACE has moved under the hand since
    the press (surface-pointer travel − screen-pointer travel; positive =
    the surface moved left / up, the compositor's keep-on-screen push
    against a surface growing right / down). Read by absorb; and each new
    step of it on an axis with a near push armed is the studio moving
    toward the hand — the near GLUE applies it (see push_near)."""
    new = [float(slide_x), float(slide_y)]
    old = _STATE["slid"]
    _STATE["slid"] = new
    for i, axis in enumerate(("x", "y")):
        delta = new[i] - old[i]
        if delta > 0 and _STATE["near_glue"][i] is not None:
            _apply_near_glue(axis, delta)


# ---------------------------------------------------------------------------
# NEAR edges (left / top), the push-up's trick turned around. A Wayland
# client cannot see the window, but the compositor sees it for us: a
# surface growing past the screen's far edge is slid the other way to keep
# its geometry on screen (whatorb past_edge rides for the push-up). So a
# near edge dragged past the display's left / top grows the surface on the
# FAR side through the edge, and Mutter slides the studio toward the hand.
# Until the far side reaches the screen the growth only extends the studio
# there (the hand runs ahead by that much - the push-up has the same
# property); from then on the studio's near edge follows 1:1. The slide is
# measured from the pointer (note_surface_slide), and the GLUE applies each
# step: every root window re-based by it (the UI stays put on screen; a
# press-anchored corner drag's baseline with it, since that path re-derives
# pos/size from it every frame), the pushed window reframed so its near
# edge follows the OS edge out (far edge and interior hold), and the
# dragged column / row edge (if any) does the slide so its cascade stays
# packed. Two request shapes: _frame_pass reports the INCREMENTAL the
# window past the edge (the reframe puts the edge back on it every frame),
# the direct corner path reports the TOTAL from its baseline (total=True:
# only what is not already requested goes out). Nothing unwinds: the
# studio's near edge cannot come back, and the far growth is dropped with
# the rest of the sticky bookkeeping on release. The earlier
# xdg_toplevel.resize handoff (request_near / the configure glue for the
# content shift) is superseded by this and left in place unused.
# ---------------------------------------------------------------------------

def push_near(axis, over, owner, edge=None, total=False):
    """``owner``'s near edge on ``axis`` is ``over`` px past the display's
    near edge in a live drag (the caller keeps it ON the edge meanwhile —
    columns.reframe_axis). Grows the surface on the far side by it, up to
    the workarea cap; returns the px requested this call."""
    if over <= 0 or not near_push_available() or not _live():
        return 0.0
    i = _AXIS[axis]
    _fresh()
    if total:
        outstanding = _STATE["near_requested"][i] - _STATE["near_slid"][i] + _STATE["push_near"][i]
        over = float(over) - outstanding
        if over <= 0:
            _STATE["near_glue"][i] = (owner, edge)
            return 0.0
    sizes = _surface_and_cap()
    if sizes is None:
        return 0.0
    (size, cap) = sizes
    room = max(0.0, cap[i] - size[i] - _STATE["push"][i] - _STATE["push_near"][i])
    req = min(float(over), room)
    _STATE["near_glue"][i] = (owner, edge)
    _trace(f"push_near {axis} over={over:.0f} room={room:.0f} → {req:.0f}")
    if req <= 0:
        return 0.0
    _STATE["push_near"][i] += req
    return req


def _apply_near_glue(axis, delta):
    from src.lsd.gl_gui.view.core_views.columns import reframe_axis, _ensure_window_state, _pending
    from src.lsd.gl_gui.utils.glfw_utils import request_render
    i = _AXIS[axis]
    window, edge = _STATE["near_glue"][i]
    _STATE["near_slid"][i] += delta
    seen = set()
    for ds in _root_windows():
        seen.add(id(ds))
        _rebase(ds, axis, delta)
    if id(window) not in seen:
        _rebase(window, axis, delta)
    # the pushed window's near edge goes back out past the OS edge
    reframe_axis(window, axis, -delta)
    frame = getattr(window, "_frame_edges" if axis == "x" else "_frame_rows", None) or ()
    if edge is not None and not any(edge is fe for fe in frame):
        _ensure_window_state(window)
        _pending(window, axis).append((edge, edge[axis] - delta, True))
    _trace(f"near glue {axis}: studio slid {delta:.0f}, roots re-based, edge followed")
    request_render()


def _rebase(ds, axis, delta):
    """Move a root window by ``delta`` on ``axis`` in content coordinates
    so it keeps its SCREEN position after the studio slid the other way —
    a press-anchored corner drag's baseline with it."""
    pos = ds.window_pos or (0, 0)
    ds.window_pos = (pos[0] + delta, pos[1]) if axis == "x" else (pos[0], pos[1] + delta)
    base = getattr(ds, "_initial_window_pos_resize", None)
    if base is not None:
        ds._initial_window_pos_resize = ((base[0] + delta, base[1]) if axis == "x"
                                         else (base[0], base[1] + delta))


def pending():
    _fresh()
    return tuple(_STATE["push"])


def flush():
    """End of the frame (draw_melty_windows, AFTER Melty.end_frame — the
    root windows, whose drags do the absorbing, are painted in end_frame's
    dispatch): the frame's pushes become ONE surface resize, applied at the
    next frame's start. Returns the requested size or None."""
    _fresh()
    _end_glue_if_over()
    if _STATE["near"]:
        _begin_near_handoff()
    if not _live():
        # gesture over: the sticky bookkeeping starts fresh next time
        _STATE["grown"] = [0.0, 0.0]
        _STATE["pusher"] = [None, None]
        _STATE["cap_hit"] = [None, None]
        _STATE["slid"] = [0.0, 0.0]
        _STATE["push_near"] = [0.0, 0.0]
        _STATE["near_requested"] = [0.0, 0.0]
        _STATE["near_slid"] = [0.0, 0.0]
        _STATE["near_glue"] = [None, None]
    px, py = _STATE["push"]
    nx, ny = _STATE["push_near"]
    if px == 0.0 and py == 0.0 and nx == 0.0 and ny == 0.0:
        return None
    _STATE["push"] = [0.0, 0.0]
    _STATE["push_near"] = [0.0, 0.0]
    sizes = _surface_and_cap()
    if sizes is None:
        return None
    (w, h), _cap = sizes
    for i, d in enumerate((px, py)):
        _STATE["grown"][i] = max(0.0, _STATE["grown"][i] + d)
    for i, d in enumerate((nx, ny)):
        _STATE["near_requested"][i] += d
    px, py = px + nx, py + ny
    from src.lsd.gl_gui import titlebar
    size = (int(round(w + px)), int(round(h + py)))
    _trace(f"flush: surface {w:.0f}x{h:.0f} + ({px:.0f}, {py:.0f}) → request {size}"
           f" (grown this gesture {tuple(round(g) for g in _STATE['grown'])},"
           f" near {tuple(round(g) for g in _STATE['near_requested'])})")
    titlebar.request_surface_size(titlebar._studio_window(), *size)
    return size


# ---------------------------------------------------------------------------
# LEFT / TOP: the compositor handoff. Moving an OS window left or up means
# moving it, which a Wayland client cannot do - but xdg_toplevel grabbing
# from that edge makes the compositor do exactly that with the pointer.
# The rest of the gesture is the compositor's (no more events reach us
# until release): every configure names the new surface size, and the
# GLUE keeps the dragged window's edge on the OS edge - every root window
# re-based by the delta so the rest of the UI stays put on screen, the
# glued window reframed so its near edge pushes the OS edge out while its
# interior holds, and the column edge that was being dragged (if any) fed
# the delta through the solve so its layout stays packed. Ends when the
# pointer re-enters (the grab is over). The screen is the wall: Mutter
# constrains its own resizes, and past it we are blind.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The CONTENT SHIFT. Mutter anchors an interactive resize from the button
# PRESS, so the first configure after a mid-drag handoff moves the frame by
# the whole distance the pointer travelled since the press - the pre-touch
# position of the melty edge - in one step. The compositor moves the SURFACE
# with that jump; the only way to keep the content where it was is to move
# it by the same amount the other way INSIDE the buffer, in the very
# commit that ful to that configure: content origin = margin + shift. The
# geometry keeps the pre-handoff origin while the glue lives (the compositor's
# frame then covers a transparent strip left/top of the content); when the
# pointer comes back it is normalized onto the content - a client-initiated
# geometry change leaves the content where it is, so nothing visible moves.
# The shift persists in the buffer (an unused strip beyond the margin) until
# maximize/fullscreen collapses it.
# ---------------------------------------------------------------------------

def content_shift():
    return tuple(_STATE["shift"])


def reset_shift():
    _STATE["shift"] = [0.0, 0.0]


def geometry_origin():
    """Buffer origin of the compositor's window geometry: the content's
    origin — or, while a handoff glue lives, the content origin AT the
    handoff, so the compositor's press-anchored jump lands on the content
    we shifted to meet it."""
    from src.lsd.gl_gui import titlebar
    glue = _STATE["glue"]
    if glue is not None:
        return tuple(glue["origin0"])
    return titlebar.content_origin()


def surface_for_geometry(width, height):
    """Surface size for a compositor configure naming a geometry of
    width × height: the geometry's origin on the left/top, the shadow
    margin on the right/bottom."""
    from src.lsd.gl_gui import titlebar
    gx, gy = geometry_origin()
    inset = titlebar.window_inset()
    return (int(round(width + gx + inset)), int(round(height + gy + inset)))


def geometry_rect(fb_w, fb_h):
    """The window geometry for a surface of fb_w × fb_h."""
    from src.lsd.gl_gui import titlebar
    gx, gy = geometry_origin()
    inset = titlebar.window_inset()
    return (int(gx), int(gy), max(1, int(fb_w - gx - inset)), max(1, int(fb_h - gy - inset)))


def _press_travel():
    """(x, y) px the pointer has moved LEFT / UP since the press that
    drives the current gesture — what Mutter's press-anchored resize will
    apply in its first step."""
    from src.lsd.gl_gui.melty import Melty
    try:
        handler = getattr(Melty, "event_handler", None)
        state = getattr(handler, "_states", {}).get("left_mouse") if handler is not None else None
        if state is None:
            return (0.0, 0.0)
        cx, cy = handler.cursor()
        return (max(0.0, float(state.down_x) - float(cx)), max(0.0, float(state.down_y) - float(cy)))
    except Exception:
        return (0.0, 0.0)


def near_push_available():
    """Can a near edge push the studio right now? Wayland with the relative
    pointer (the slide is measured from it)."""
    from src.lsd.gl_gui import titlebar, wayland_move
    from src.lsd.gl_gui.toggles import Toggles
    return (Toggles.Melty.push_os_window_edges and Toggles.Melty.push_os_window_near_edges
            and titlebar._on_wayland() and wayland_move.relative_motion_available())


def request_near(axis, window, edge=None):
    """A cursor-driven drag pushed ``window``'s near edge on ``axis`` past
    the display's near edge this frame; ``edge`` is the edge being dragged
    (None / a frame edge = the window's own edge). Handed off at flush —
    both axes in one frame become the top-left corner."""
    _fresh()
    _STATE["near"][axis] = (window, edge)


def _begin_near_handoff():
    from src.lsd.gl_gui import titlebar, wayland_move
    near = _STATE["near"]
    _STATE["near"] = {}
    if not near or not near_push_available():
        return False
    axes = set(near)
    edges = (wayland_move.EDGE_TOP_LEFT if axes == {"x", "y"}
             else wayland_move.EDGE_LEFT if axes == {"x"} else wayland_move.EDGE_TOP)
    window = titlebar._studio_window()
    if not wayland_move.begin_resize(window, edges):
        _trace(f"near handoff {sorted(axes)}: begin_resize refused")
        return False
    titlebar._release_after_wayland_grab(window)
    size = _content_size()
    travel = _press_travel()
    _STATE["glue"] = {"axes": dict(near), "content": list(size),
                      "origin0": list(titlebar.content_origin()),
                      "travel": [travel[0] if "x" in axes else 0.0, travel[1] if "y" in axes else 0.0],
                      "absorbed": [False, False],
                      "enter_serial": wayland_move.enter_serial()}
    _trace(f"near handoff {sorted(axes)} → compositor resize, content {size}, press travel {travel}")
    return True


def _content_size():
    from src.lsd.gl_gui import titlebar
    window = titlebar._studio_window()
    try:
        w, h = glfw.get_framebuffer_size(window)
    except Exception:
        return (0.0, 0.0)
    ox, oy = titlebar.content_origin()
    inset = titlebar.window_inset()
    return (float(w - ox - inset), float(h - oy - inset))


def on_compositor_size(content_w, content_h):
    """titlebar.on_surface_resized, compositor-driven: the OS window's
    geometry is now content_w × content_h. Apply the glue for the axes
    being handed off."""
    glue = _STATE["glue"]
    if glue is None:
        return False
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.view.core_views.columns import reframe_axis, _ensure_window_state, _pending
    from src.lsd.gl_gui.utils.glfw_utils import request_render
    # content_w/h name the GEOMETRY (from origin0); the content is that
    # minus the strip the shift opened in front of it
    strip = [_STATE["shift"][0] - (glue["origin0"][0] - _margin()), _STATE["shift"][1] - (glue["origin0"][1] - _margin())]
    geom = [float(content_w), float(content_h)]
    applied = False
    for axis, (window, edge) in glue["axes"].items():
        i = _AXIS[axis]
        if not glue["absorbed"][i]:
            # The compositor's first step is the press travel: meet it
            # with an equal content shift so nothing on screen moves.
            t = float(glue["travel"][i])
            _STATE["shift"][i] += t
            strip[i] += t
            glue["absorbed"][i] = True
            if t:
                _trace(f"glue {axis}: absorbed press travel {t:.0f} as content shift")
        content = geom[i] - strip[i]
        d = content - glue["content"][i]
        glue["content"][i] = content
        if not d:
            continue
        # every ROOT window keeps its screen position - the content origin
        # moved by -d on that axis (a glued window missing from the list got
        # the reframe without the re-base: twice the motion - hence the
        # fallback below).
        seen = set()
        for ds in _root_windows():
            seen.add(id(ds))
            pos = ds.window_pos or (0, 0)
            ds.window_pos = (pos[0] + d, pos[1]) if axis == "x" else (pos[0], pos[1] + d)
        if id(window) not in seen:
            pos = window.window_pos or (0, 0)
            window.window_pos = (pos[0] + d, pos[1]) if axis == "x" else (pos[0], pos[1] + d)
        # the glued window's near edge goes back out of the OS edge
        reframe_axis(window, axis, -d)
        # the dragged column edge follows the hand: close the slack the
        # reframe opened between it and the near frame edge
        frame = getattr(window, "_frame_edges" if axis == "x" else "_frame_rows", None) or ()
        if edge is not None and not any(edge is fe for fe in frame):
            _ensure_window_state(window)
            _pending(window, axis).append((edge, edge[axis] - d, True))
        applied = True
    if applied:
        _trace(f"glue: content {tuple(int(v) for v in glue['content'])}")
        request_render()
    return applied


def _margin():
    from src.lsd.gl_gui import titlebar
    return float(titlebar.window_inset())


def _end_glue_if_over():
    glue = _STATE["glue"]
    if glue is None:
        return
    from src.lsd.gl_gui import wayland_move
    if wayland_move.enter_serial() != glue["enter_serial"]:
        _trace("near handoff over (pointer back)")
        _STATE["glue"] = None


# ---------------------------------------------------------------------------
# The REVERSE push of the OS window shrinking into the root melty windows.
# Run once per frame before the root windows draw (draw_melty_windows,
# right after begin_frame stamps Melty.display_size). The gesture starts on
# the first frame the display size differs from the last frame's and ends
# on a frame it is stable AND the hand is off (no button held, no OS-window
# right-drag, pointer in the window - a compositor resize grab takes the
# pointer away until release, an own right-drag holds the button). While
# it lives, every frame the display CHANGES re-applies from the BASELINE
# (each root window's pos / size / every registered edge, snapshotted the
# frame the gesture started, so the layout is a pure function of the
# initial display size - sticky: back out is back to the start): a window
# whose far edge overflows the display slides until its near edge reaches
# the display edge (the top/left goes at 0), and only the remainder shrinks
# it - a plain size write, while the window's own edge pass folds that as
# the walled far-edge drag that cascades through the columns / rows down to
# their floors; past the fully-compressed pile the write simply overflows
# (the screen is the wall, the pile won't give). On release the current
# layout is kept as the new normal. Gate: Toggles.Melty.os_resize_push_windows.
# ---------------------------------------------------------------------------

def _root_windows():
    """Every top-level window: the REGISTERED windows (@window — the
    ManagedWindow's draw_state; drawn by the dispatch loop with no window
    stack, so parent_window stays None) plus whatever sits parentless in
    Melty.root_draw_states (that registry holds the NESTED windows under
    each parent id — a top-level window is never in it by itself, which is
    why an earlier version of this found nothing). Deduped by identity."""
    from src.lsd.gl_gui.melty import Melty
    seen, roots = set(), []
    for managed in list(getattr(Melty, "registered_windows", {}).values()):
        ds = getattr(managed, "draw_state", None)
        if ds is None or id(ds) in seen or getattr(ds, "parent_window", None) is not None:
            continue
        seen.add(id(ds))
        roots.append(ds)
    for group in list(getattr(Melty, "root_draw_states", {}).values()):
        for ds in list(group):
            if id(ds) in seen or getattr(ds, "parent_window", None) is not None:
                continue
            seen.add(id(ds))
            roots.append(ds)
    return roots


def _pointer_in_window():
    """GLFW's HOVERED: false while a compositor grab (an OS-window resize
    it drives) holds the pointer, and until it re-enters afterwards."""
    from src.lsd.gl_gui import titlebar
    window = titlebar._studio_window()
    if window is None:
        return True
    try:
        return bool(glfw.get_window_attrib(window, glfw.HOVERED))
    except Exception:
        return True


def _os_gesture_live():
    """The hand is on the OS WINDOW: our own right-drag on the background
    (titlebar._rdrag), or a compositor resize grab (the pointer is away)."""
    from src.lsd.gl_gui import titlebar
    return titlebar._rdrag is not None or not _pointer_in_window()


def _melty_gesture_live():
    """A button held INSIDE the UI with no OS gesture: a melty window /
    column / corner drag. Its forward push (absorb) resizes the surface —
    that display change is not an OS resize, and pushing back against the
    very window being dragged is the fight this guards (the drag set the
    size, the reverse push restored the baseline, every frame)."""
    from src.lsd.gl_gui.view.core_views.columns import _drag_live
    return _drag_live() and not _os_gesture_live()


def _gesture_idle():
    """No hand anywhere: nothing dragged, no own right-drag, the pointer
    over the window."""
    from src.lsd.gl_gui.view.core_views.columns import _drag_live
    return not _drag_live() and not _os_gesture_live()


def _fit_snapshot(window):
    from src.lsd.gl_gui.view.core_views.columns import _all_edges, _ensure_window_state
    _ensure_window_state(window)
    edges = {axis: [(e, float(e[axis])) for e in _all_edges(window, axis)] for axis in ("x", "y")}
    return {"pos": tuple(window.window_pos or (0, 0)),
            "abs": (float(window.abs_left or 0), float(window.abs_top or 0)),
            "size": (float(window.width), float(window.height)),
            "edges": edges}


def _fit_restore(window, base):
    """Back to the gesture's baseline: pos, size, every edge. Returns
    whether anything was off it."""
    from src.lsd.gl_gui.view.core_views.columns import snap_int
    changed = False
    if tuple(window.window_pos or (0, 0)) != base["pos"]:
        window.window_pos = base["pos"]
        changed = True
    width, height = snap_int(base["size"][0]), snap_int(base["size"][1])
    if window.width != width:
        window.width = width
        changed = True
    if window.height != height:
        window.height = height
        changed = True
    for axis, pairs in base["edges"].items():
        for edge, value in pairs:
            if edge[axis] != value:
                edge[axis] = value
                changed = True
    return changed


def _fit_apply(window, base, display):
    """The push for ``display`` from the baseline: slide first (near edge
    down to the display's), the remainder as a size write for the window's
    edge pass to cascade."""
    from src.lsd.gl_gui.view.core_views.columns import snap_int
    pos = list(base["pos"])
    moved = False
    for axis in ("x", "y"):
        i = _AXIS[axis]
        abs0, size0 = base["abs"][i], base["size"][i]
        overflow = abs0 + size0 - display[i]
        if overflow <= 0:
            continue
        slide = min(overflow, max(0.0, abs0))
        pos[i] -= slide
        remaining = overflow - slide
        if remaining > 0:
            # the pass floors this at the compressed pile (walled close-in)
            size = snap_int(max(1.0, size0 - remaining))
            if axis == "x":
                window.width = size
            else:
                window.height = size
        moved = True
    if moved:
        window.window_pos = (pos[0], pos[1])
    return moved


def fit_windows_to_display():
    """The reverse push, once per frame before the root windows draw.
    Returns True when a window was moved / resized this frame."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.toggles import Toggles
    from src.lsd.gl_gui.utils.glfw_utils import request_render
    display = Melty.display_size
    if not Toggles.Melty.os_edges_push_windows or not display or not display[0] or not display[1]:
        _STATE["fit"] = None
        _STATE["fit_last"] = None
        return False
    display = (float(display[0]), float(display[1]))
    last = _STATE["fit_last"]
    _STATE["fit_last"] = display
    if _melty_gesture_live():
        # the UI is resizing the surface (forward push): never a gesture of
        # ours, and the size it leaves behind is the new normal
        _STATE["fit"] = None
        return False
    fit = _STATE["fit"]
    if fit is None:
        if last is None or last == display:
            return False
        # baseline = LAST frame's display: nothing has pushed yet
        fit = _STATE["fit"] = {"display0": last, "display": last, "windows": {}}
        _trace(f"fit: display {tuple(int(v) for v in last)} → {tuple(int(v) for v in display)}, gesture starts")
    stable = display == fit["display"]
    fit["display"] = display
    if stable:
        if _gesture_idle():
            _trace("fit: gesture over, layout kept")
            _STATE["fit"] = None
        return False
    moved = False
    for window in _root_windows():
        if (not getattr(window, "closable", False) or getattr(window, "closed", False)
                or not getattr(window, "expanded", True)
                or not window.width or not window.height or window.window_pos is None):
            continue
        base = fit["windows"].get(id(window))
        if base is None:
            base = fit["windows"][id(window)] = _fit_snapshot(window)
        restored = _fit_restore(window, base)
        moved = _fit_apply(window, base, display) or restored or moved
    if moved:
        request_render()
    return moved


def clamp_far_edge(abs_pos, size, display, absorbed, cap_size=True):
    """The pin-and-slide remainder after ``absorbed`` px went to the OS
    edge: the display effectively extends by that much this frame. Returns
    (size, slide): ``slide`` ≥ 0 px to move the window the OTHER way so
    the far edge pins at the effective display edge — never past the
    display's NEAR edge (the top / left stop at 0: that stop is the
    fill-the-display snap) — and ``size`` capped at what fits from there
    when ``cap_size`` (a size the drag owns; a passed size is left
    alone)."""
    limit = display + absorbed
    if cap_size and size > limit:
        size = limit
    slide = max(0.0, abs_pos + size - limit)
    slide = min(slide, max(0.0, abs_pos))
    if cap_size and abs_pos - slide + size > limit:
        size = max(0.0, limit - (abs_pos - slide))
    return size, slide
