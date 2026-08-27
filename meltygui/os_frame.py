"""The OS window as a melty window: its four edges are collision edges in
the columns edge system, one level outside the root melty windows, and the
screen's work area is the wall outside it.

Per axis the OS window owns a frame pair of edge dicts in SCREEN
coordinates (``_STATE["edges"]``) and the work area a wall pair
(``_STATE["screen"]``). Every ROOT melty window's frame pass
(columns._frame_pass) solves against them: the root's edges are shifted
into screen coordinates for the solve, and two zero-floor GAP cells —
[os_near, W_near] and [W_far, os_far] — link its frame to the OS frame,
exactly as a nested layout's cells link it to the enclosing window. So a
drag that pushes a root's edge into the OS edge moves the OS edge (the
surface grows, or the window moves through the attach-offset — see flush),
the OS edge pushed into the screen is clamped there (a wall), and a
cursor-driven drag whose owner is blocked by a wall grows that window on
the OPPOSITE side instead (the flip in columns._solve_collisions — the one
rule melty windows always had against the display, now for both). Nothing
else: no caps, no inference, no sticky bookkeeping — edges move when a
hand drags them or when something pushes them.

The position comes from the GNOME extension's feed (geometry_feed.py; X11
reads glfw). A change we did NOT request — the compositor's move / resize —
is folded into each root's next pass as a drag of the OS edge from where
that root last saw it (``window._os_seen``): a near-edge RESIZE holds the
roots on screen (re-base) and pushes the ones it reaches; a MOVE carries
them along. Without a position (extension not installed, no bus) the OS
edges are immovable walls at the display's edges and the old in-display
pin-and-slide is what remains. Gate: Toggles.Melty.push_os_window_edges.
"""
import glfw

# Smallest OS window the physics allows (content px): the OS frame cell is
# zero, so a drag to it pushes the other OS edge instead of collapsing.
MIN_SIZE = (320.0, 200.0)
# Frames a requested move may stay unseen in the feed before the observed
# position is taken as the truth (a compositor refusal we didn't predict).
INFLIGHT_FRAMES = 12

_AXIS = {"x": 0, "y": 1}

# Survives hotswap (a re-exec reuses the existing dict); the edge dicts
# are the graph nodes and keep their identity across frames.
_STATE = globals().get("_STATE") or {
    "edges": {"x": [{"x": 0.0}, {"x": 0.0}], "y": [{"y": 0.0}, {"y": 0.0}]},
    "screen": {"x": [{"x": 0.0}, {"x": 0.0}], "y": [{"y": 0.0}, {"y": 0.0}]},
    "mode": "walls", "generation": 0,
    "expected": [None, None], "inflight": [None, None], "size_expected": [None, None],
    "pending": {"x": [], "y": []}, "consumed": {"x": False, "y": False},
    "frame": -1, "trace_frame": -1,
}
# The OS near edge's change the roots' CONTENT coordinates have not been
# re-based for yet: a solve moves the model edge in frame N, the surface
# moves at frame N+1's start (the queued size + attach offset) - the
# re-base that keeps the roots on screen lands THERE (apply_rebase), in the
# same commit as the move. Re-based at the solve they were drawn shifted
# to a surface that had not moved yet: one frame of jelly per step.
_STATE.setdefault("unapplied", [0.0, 0.0])


def _trace(msg):
    from src.lsd.gl_gui.toggles import Toggles
    if Toggles.Melty.push_os_window_edges_trace:
        print(f"[os_frame] {msg}")


def _enabled():
    from src.lsd.gl_gui.toggles import Toggles
    return bool(Toggles.Melty.push_os_window_edges)


def mode():
    """"feed" (Wayland, the extension's position), "x11" (glfw's position)
    or "walls" (no position: the OS edges are immovable)."""
    return _STATE["mode"]


def available():
    return _STATE["mode"] != "walls"


def edges(axis):
    """The OS frame pair on ``axis`` (screen coords; content coords in
    walls mode)."""
    return _STATE["edges"][axis]


def screen_edges(axis):
    return _STATE["screen"][axis] if available() else None


# ---------------------------------------------------------------------------
# Private
# ---------------------------------------------------------------------------

def _observe():
    """((x, y), (work_x, work_y, work_w, work_h), mode) of the content rect
    on screen, or None when no position is known."""
    from src.lsd.gl_gui import titlebar
    if titlebar._on_wayland():
        from src.lsd.gl_gui import geometry_feed
        if geometry_feed._STATE["thread"] is None:      # a hotswap, not a restart: start it here
            geometry_feed.start()
        rect, area = geometry_feed.frame_rect(), geometry_feed.workarea()
        if rect is None or area is None:
            return None
        return (float(rect[0]), float(rect[1])), tuple(float(v) for v in area), "feed"
    window = titlebar._studio_window()
    if window is None:
        return None
    try:
        x, y = glfw.get_window_pos(window)
        wa_l, wa_t, wa_r, wa_b = titlebar._workarea_for(window)
    except Exception:
        return None
    return (float(x), float(y)), (wa_l, wa_t, wa_r - wa_l, wa_b - wa_t), "x11"


def _root_windows():
    """Every top-level melty window: the REGISTERED windows (@window — the
    ManagedWindow's draw_state, drawn by the dispatch loop with no window
    stack, so parent_window stays None) plus whatever sits parentless in
    Melty.root_draw_states. Deduped by identity."""
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


def _movable_roots():
    """The roots that hold their SCREEN position when the OS near edge moves:
    closable floating windows. The Main Window (not closable) IS the
    content and rides with the OS frame."""
    return [ds for ds in _root_windows()
            if getattr(ds, "closable", False) and ds.window_pos is not None]


def _rebase(ds, axis, delta):
    """Move a root by ``delta`` on ``axis`` in CONTENT coordinates so it
    keeps its screen position after the content origin moved the other
    way — the press-anchored corner drag's baseline with it."""
    pos = ds.window_pos or (0, 0)
    ds.window_pos = (pos[0] + delta, pos[1]) if axis == "x" else (pos[0], pos[1] + delta)
    base = getattr(ds, "_initial_window_pos_resize", None)
    if base is not None:
        ds._initial_window_pos_resize = ((base[0] + delta, base[1]) if axis == "x"
                                         else (base[0], base[1] + delta))


def _set_mode(new_mode):
    if _STATE["mode"] != new_mode:
        _STATE["mode"] = new_mode
        _STATE["generation"] += 1          # every root forgets where it saw the OS edges
        _STATE["expected"] = [None, None]
        _STATE["inflight"] = [None, None]
        _STATE["size_expected"] = [None, None]
        _trace(f"mode → {new_mode}")


def begin_frame():
    """Start of the frame, right after Melty.display_size is stamped: bring
    the OS model up to date with what is real — the content size (our own
    resizes land at frame start, a compositor's through the resize
    callback) and the observed position — and fold in what we did not
    request."""
    from src.lsd.gl_gui.melty import Melty
    _STATE["frame"] = Melty.frame_count
    _STATE["consumed"] = {"x": False, "y": False}
    display = Melty.display_size
    if not display or not display[0] or not display[1]:
        return
    size = (float(display[0]), float(display[1]))
    observed = _observe() if _enabled() else None
    if observed is None:
        _set_mode("walls")
        for axis, i in _AXIS.items():
            near, far = _STATE["edges"][axis]
            near[axis], far[axis] = 0.0, size[i]
        return
    (pos, area, new_mode) = observed
    _set_mode(new_mode)
    for axis, i in _AXIS.items():
        scr_near, scr_far = _STATE["screen"][axis]
        scr_near[axis], scr_far[axis] = area[i], area[i] + area[i + 2]
        near, far = _STATE["edges"][axis]
        expected = _STATE["expected"][i]
        if expected is None:                      # first sight
            near[axis], far[axis] = pos[i], pos[i] + size[i]
            _STATE["expected"][i] = pos[i]
            _STATE["size_expected"][i] = size[i]
            _trace(f"{axis}: first sight near={pos[i]:.0f} size={size[i]:.0f}")
            continue
        # Position: our requests land a frame or two later; before then the
        # model already has them and the observation is behind.
        if pos[i] != expected:
            flight = _STATE["inflight"][i]
            if flight is not None and Melty.frame_count - flight <= INFLIGHT_FRAMES:
                pass                              # still landing
            else:
                d = pos[i] - expected
                _STATE["expected"][i] = pos[i]
                _STATE["inflight"][i] = None
                _foreign_change(axis, d, size[i])
                continue
        else:
            _STATE["inflight"][i] = None
        # Size: ours lands at frame start (size_expected); anything else is
        # the compositor's - the far edge moved.
        if size[i] != far[axis] - near[axis]:
            if _STATE["size_expected"][i] != size[i]:
                _trace(f"{axis}: foreign far edge {far[axis]:.0f} → {near[axis] + size[i]:.0f}")
            far[axis] = near[axis] + size[i]
            _STATE["size_expected"][i] = size[i]


def _foreign_change(axis, d, size):
    """The window's near edge is ``d`` px from where we expected it and we
    asked for nothing: the compositor moved or resized the studio. Far
    edge still where it was → a NEAR RESIZE: the roots hold their screen
    positions (re-base) and get pushed by the edge where it reaches them
    (each root's pass folds the drag from ``_os_seen``). Otherwise a MOVE
    by ``d`` (the roots ride along, nothing to solve) plus whatever the
    far edge did on top."""
    i = _AXIS[axis]
    near, far = _STATE["edges"][axis]
    old_far = far[axis]
    near[axis] += d
    if abs((near[axis] + size) - old_far) < 0.5:
        _trace(f"{axis}: foreign near resize {d:+.0f} — roots hold the screen")
        for ds in _movable_roots():
            _rebase(ds, axis, -d)
        far[axis] = near[axis] + size
    else:
        _trace(f"{axis}: foreign move {d:+.0f} (size {old_far - (near[axis] - d):.0f} → {size:.0f})")
        for ds in _root_windows():
            seen = getattr(ds, "_os_seen", None)
            if seen and axis in seen:
                seen[axis] = (seen[axis][0] + d, seen[axis][1] + d)
        far[axis] = near[axis] + size
    _STATE["size_expected"][i] = size


# ---------------------------------------------------------------------------
# The solve: what a root's frame needs gets, and what it hands back
# ---------------------------------------------------------------------------

class Context:
    """One root window's view of the OS level for one axis' solve. The OS
    and screen edge dicts are shifted into the WINDOW's coordinates
    (by -base) for the solve and back in detach — the window's own edges
    are never touched unless the solve moves them."""
    __slots__ = ("axis", "base", "os_near0", "lists", "specs", "walls",
                 "drags", "os_ids", "shifted")

    def __init__(self, axis):
        self.axis = axis
        self.base = 0.0
        self.os_near0 = 0.0
        self.lists, self.specs, self.drags = [], [], []
        self.walls = frozenset()
        self.os_ids = frozenset()
        self.shifted = ()


def queue_drag(axis, index, inc):
    """A cursor-driven drag of the OS window's own frame edge (``index`` 0
    = near / left / top, 1 = far) by ``inc`` px — the background right-drag
    (titlebar). Solved by the first root pass of the frame, else by flush."""
    if inc:
        _STATE["pending"][axis].append((int(index), float(inc)))


def attach(window, axis, has_pending=True):
    """Called by columns._frame_pass for a ROOT window before its solve:
    the OS-level cells / walls / drags to add, in the WINDOW's coordinates
    (the OS and screen dicts shifted by -base, base = the screen coordinate
    of the window's near edge; detach shifts them back). None when the OS
    level is off, or when there is nothing to solve at all — no queued
    drag of the window (``has_pending``), none of the OS window's own, no
    OS edge moved since this window last saw it — so an idle frame
    touches nothing."""
    from src.lsd.gl_gui.melty import Melty
    if (not _enabled() or getattr(window, "parent_window", None) is not None
            or _STATE["frame"] != Melty.frame_count):     # only on a frame begin_frame set up
        return None
    near, far = _STATE["edges"][axis]
    seen_all = getattr(window, "_os_seen", None)
    if seen_all is None or getattr(window, "_os_gen", None) != _STATE["generation"]:
        seen_all = window._os_seen = {}
        window._os_gen = _STATE["generation"]
    seen = seen_all.get(axis)
    cur = (near[axis], far[axis])
    os_moved = seen is not None and (abs(seen[0] - cur[0]) > 1e-6 or abs(seen[1] - cur[1]) > 1e-6)
    own = bool(_STATE["pending"][axis]) and not _STATE["consumed"][axis]
    if seen is None:
        seen_all[axis] = cur
    if not (has_pending or os_moved or own):
        return None
    ctx = Context(axis)
    ctx.os_ids = frozenset({id(near), id(far)})
    ctx.os_near0 = near[axis]
    i = _AXIS[axis]
    # the window's frame origin: its own coordinate is relative to the
    # screen origin its frame was last re-based for (the model's near
    # origin less what apply_rebase has not applied yet)
    ctx.base = (near[axis] - _STATE["unapplied"][i]
                + float((window.abs_left if axis == "x" else window.abs_top) or 0))
    if _STATE["mode"] == "walls":
        ctx.lists.append([near, far])
        ctx.specs.append(([MIN_SIZE[i]], [None]))
        ctx.walls = ctx.os_ids
        ctx.shifted = (near, far)
    else:
        scr_near, scr_far = _STATE["screen"][axis]
        ctx.lists.append([scr_near, near, far, scr_far])
        ctx.specs.append(([0.0, MIN_SIZE[i], 0.0], [None, None, None]))
        ctx.walls = frozenset({id(scr_near), id(scr_far)})
        ctx.shifted = (scr_near, near, far, scr_far)
    # the OS edges' motion since this window last saw them: a drag from
    # there to here through this window's cells (foreign: the other OS
    # edge and the screen are walls - the edge IS where it is, the pile
    # packs against it or overflows)
    if os_moved:
        # (the foreign drag is forced to its floor right after it is
        # solved - columns._solve_collisions - so the edge ends where it
        # really is whatever the pile could give)
        near[axis], far[axis] = seen
        if seen[0] != cur[0]:
            ctx.drags.append((near, cur[0] - ctx.base, None))
        if seen[1] != cur[1]:
            ctx.drags.append((far, cur[1] - ctx.base, None))
    for e in ctx.shifted:
        e[axis] -= ctx.base
    # the OS window's own drags, once per frame
    if own:
        _STATE["consumed"][axis] = True
        for index, inc in _STATE["pending"][axis]:
            edge = (near, far)[index]
            ctx.drags.append((edge, edge[axis] + inc, True))
        _STATE["pending"][axis] = []
    return ctx


def gap_lists(ctx, window_near, window_far):
    """The two zero-floor gap cells linking a root's frame pair (screen
    coords) to the OS frame pair."""
    near, far = _STATE["edges"][ctx.axis]
    return ([[near, window_near], [window_far, far]],
            [([0.0], [None]), ([0.0], [None])])


def detach(window, axis, ctx):
    """After the solve: force foreign OS edges to where they really are
    (a root the compositor pushed past its floor overflows), book the OS
    near edge's motion for the re-base that lands with the move
    (apply_rebase — every root then keeps its screen position), remember
    where this window saw the edges. Returns the OS near edge's motion
    this pass."""
    near, far = _STATE["edges"][axis]
    for e in ctx.shifted:
        e[axis] += ctx.base
    d_os = near[axis] - ctx.os_near0
    if d_os:
        _STATE["unapplied"][_AXIS[axis]] += d_os
        _trace(f"{axis}: OS near edge moved {d_os:+.0f} (pushed by {getattr(window, 'name', '?')})")
    window._os_seen[axis] = (near[axis], far[axis])
    return d_os


def apply_rebase():
    """Frame START, from titlebar.apply_pending_surface_size — the moment
    the queued size / move is applied: move every root by the OS near
    edge's booked motion in CONTENT coordinates so it keeps its SCREEN
    position, in the same commit as the surface's move."""
    for axis, i in _AXIS.items():
        d = _STATE["unapplied"][i]
        if not d:
            continue
        _STATE["unapplied"][i] = 0.0
        for ds in _movable_roots():
            _rebase(ds, axis, -d)
        _trace(f"{axis}: roots re-based {-d:+.0f} with the move")


def _bare_pass(axis):
    """The OS window's own drags when no root window ran a pass this
    frame: OS frame against the screen alone."""
    from src.lsd.gl_gui.view.core_views.columns import _cells_from_lists, _EdgeGraph, _solve_graph
    near, far = _STATE["edges"][axis]
    i = _AXIS[axis]
    if _STATE["mode"] == "walls":
        lists, specs, walls = [[near, far]], [([MIN_SIZE[i]], [None])], frozenset()
    else:
        scr_near, scr_far = _STATE["screen"][axis]
        lists = [[scr_near, near, far, scr_far]]
        specs = [([0.0, MIN_SIZE[i], 0.0], [None, None, None])]
        walls = frozenset({id(scr_near), id(scr_far)})
    graph = _EdgeGraph(_cells_from_lists(lists, axis, specs=specs))
    for index, inc in _STATE["pending"][axis]:
        edge = (near, far)[index]
        target = edge[axis] + inc
        _solve_graph(graph, edge, target, walls=walls, axis=axis)
        residual = target - edge[axis]
        opposite = near if residual > 0 else far
        if residual and opposite is not edge:           # the flip
            _solve_graph(graph, opposite, opposite[axis] - residual, walls=walls, axis=axis)
    d_os = near[axis] - _STATE.get("bare_near0", near[axis])
    _STATE["pending"][axis] = []
    return d_os


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def flush():
    """End of the frame (after end_frame's window dispatch): the model's
    size / position → ONE surface request, applied at the next frame's
    start (titlebar.apply_pending_surface_size: size + attach-offset move
    in one commit). Returns the requested content size or None."""
    from src.lsd.gl_gui import titlebar
    from src.lsd.gl_gui.melty import Melty
    if not _enabled():
        return None
    for axis in _AXIS:
        if not _STATE["consumed"][axis] and _STATE["pending"][axis]:
            near0 = _STATE["edges"][axis][0][axis]
            _STATE["bare_near0"] = near0
            d_os = _bare_pass(axis)
            if d_os:
                _STATE["unapplied"][_AXIS[axis]] += d_os
            _STATE["consumed"][axis] = True
    window = titlebar._studio_window()
    display = Melty.display_size
    if window is None or not display:
        return None
    size, offset = [0, 0], [0, 0]
    changed = False
    for axis, i in _AXIS.items():
        near, far = _STATE["edges"][axis]
        want = far[axis] - near[axis]
        size[i] = int(round(want))
        if abs(want - float(display[i])) > 0.5:
            changed = True
        if _STATE["mode"] != "walls":
            expected = _STATE["expected"][i]
            if expected is not None and abs(near[axis] - expected) > 0.5:
                offset[i] = int(round(near[axis] - expected))
                _STATE["expected"][i] = expected + offset[i]
                _STATE["inflight"][i] = Melty.frame_count
                changed = True
        _STATE["size_expected"][i] = float(size[i])
    if not changed:
        return None
    inset = int(titlebar.window_inset())
    surface = (size[0] + 2 * inset, size[1] + 2 * inset)
    if _STATE["mode"] == "x11":
        if offset[0] or offset[1]:
            try:
                x, y = glfw.get_window_pos(window)
                glfw.set_window_pos(window, x + offset[0], y + offset[1])
            except Exception:
                pass
        titlebar.request_surface_size(window, *surface)
    else:
        titlebar.request_surface_size(window, *surface, offset=tuple(offset) if any(offset) else None)
    _trace(f"flush: content {size} offset {offset} → surface {surface}")
    return tuple(size)


def clamp_far_edge(abs_pos, size, display, absorbed=0.0, cap_size=True):
    """The in-display pin-and-slide of a window whose far edge passes
    ``display`` (the direct corner path when its frame edges aren't in the
    solve): (size, slide) — slide the window the other way so the far edge
    pins at the display edge, never past the display's near edge (the
    top / left stop at 0), the size capped at what fits when ``cap_size``."""
    limit = display + absorbed
    if cap_size and size > limit:
        size = limit
    slide = max(0.0, abs_pos + size - limit)
    slide = min(slide, max(0.0, abs_pos))
    if cap_size and abs_pos - slide + size > limit:
        size = max(0.0, limit - (abs_pos - slide))
    return size, slide
