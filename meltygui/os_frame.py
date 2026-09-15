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
from src.lsd.gl_gui import window_api as glfw

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
# A move we asked for before the feed's first read (the launch frame): folded
# into the first observation so it never reads as the compositor's.
_STATE.setdefault("own_move_pending", [0.0, 0.0])
# The OS FAR edge's motion the far-hung children have not been re-based
# for yet (compensate_far computes, apply_rebase applies at the next frame
# pass - when the root is laid out at the size that includes it).
_STATE.setdefault("unapplied_far", [0.0, 0.0])
# The OS edges as of the last OS-level solve (solve): what the roots were
# last laid out against. Any difference at the next solve is the OS window
# having moved on its own - a compositor's resize - and is pushed through
# the roots; a landing of our own request only snaps it.
_STATE.setdefault("os_seen", [None, None])
_STATE.setdefault("window_id", None)
# the feed's own far edge (x + width) per axis as last observed - a
# foreign change is classified against THIS (one frame edge), rat
# against our size
_STATE.setdefault("feed_far", [None, None])
_STATE.setdefault("reset_frame", 0)
# The move we last asked for per axis (flush) - a foreign move that exactly
# cancels it is the compositor REFUSING it (a gap we don't see: the
# floating-window top gap, a bar), and the edge we tried to move is walled
# there for the rest of the gesture: `learned` = [near_or_None, far_or_None]
# per axis, applied by _walls_to_edges and removed when the gesture ends or
# the compositor lets the edge past it after all (begin_frame).
_STATE.setdefault("last_offset", [0, 0])
# Outstanding content sizes, oldest first. Retire an acknowledged prefix
# so an older request cannot rewind a newer drag, but a later compositor
# resize to a previously used size is still accepted. The size observation
# may stay unchanged while a request is in flight, bounded by its age.
_STATE.setdefault("size_requests", {"x": [], "y": []})
_STATE.setdefault("size_observed", [None, None])
_STATE.setdefault("size_request_frame", [None, None])
_STATE.setdefault("learned", {"x": [None, None], "y": [None, None]})
_STATE.setdefault("pin_rebases", {})
_STATE.setdefault("move_requests", {"x": [], "y": []})


def _trace(msg):
    from src.lsd.gl_gui.toggles import Toggles
    if Toggles.Melty.push_os_window_edges_trace:
        print(f"[os_frame] {msg}")


def reset(reason="studio start"):
    """Forget the previous session's window: expected position, in-flight
    moves, booked re-bases, the edges the roots were laid out against.
    _STATE outlives a studio restart inside the server process, and a new
    window met the old one's numbers as a giant foreign change — roots
    re-based against a stale origin, pushed, left outside (08-27)."""
    from src.lsd.gl_gui.melty import Melty
    _STATE["expected"] = [None, None]
    _STATE["inflight"] = [None, None]
    _STATE["size_expected"] = [None, None]
    _STATE["unapplied"] = [0.0, 0.0]
    _STATE["own_move_pending"] = [0.0, 0.0]
    _STATE["os_seen"] = [None, None]
    _STATE["pending"] = {"x": [], "y": []}
    _STATE["gestures"] = {}
    _STATE["last_offset"] = [0, 0]
    _STATE["learned"] = {"x": [None, None], "y": [None, None]}
    _STATE["size_requests"] = {"x": [], "y": []}
    _STATE["size_observed"] = [None, None]
    _STATE["size_request_frame"] = [None, None]
    _STATE["unapplied_far"] = [0.0, 0.0]
    _STATE["pin_rebases"] = {}
    _STATE["move_requests"] = {"x": [], "y": []}
    _STATE["window_id"] = None
    _STATE["feed_far"] = [None, None]
    _STATE["generation"] += 1
    _STATE["reset_frame"] = getattr(Melty, "frame_count", 0) or 0
    _trace(f"reset ({reason})")


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


def display_top():
    """The top of the DISPLAY (the work area's top edge — below GNOME's
    bar) in the studio's CONTENT coordinates, comparable straight against a
    window's abs_top. The hard limit a melty window's top may never pass
    (columns._frame_pass, Toggles.Melty.window_top_hard_limit). Against the
    APPLIED surface origin (the model's near edge less what apply_rebase
    has not applied yet — attach's ctx.base convention), so it holds on
    the frames between an OS edge pushed in the model and the surface
    move landing. Negative while the studio sits below the display top: a
    window may rise above the STUDIO's top (pushing the OS edge ahead of
    it) and stops only where the OS edge stops. Walls mode / no model: 0 —
    the studio's top is the display's."""
    if not _enabled():
        return 0.0
    near, _far = _STATE["edges"]["y"]
    applied = near["y"] - _STATE["unapplied"][1]
    if _STATE["mode"] == "walls":
        return applied
    scr_near, _scr_far = _STATE["screen"]["y"]
    return scr_near["y"] - applied


# ---------------------------------------------------------------------------
# Private
# ---------------------------------------------------------------------------

def _observe():
    """((x, y), (work_x, work_y, work_w, work_h), mode, (far_x, far_y),
    window_id) of the content rect on screen — the far edges from the
    SAME source as the position (the feed's own width / height; glfw's on
    X11) — or None when no position is known."""
    from src.lsd.gl_gui import titlebar
    if titlebar._on_wayland():
        from src.lsd.gl_gui import geometry_feed
        geometry_feed.ensure_started()      # a hotswap, not a restart (or a backend switch): start it here
        # the Hyprland backend reports the SURFACE: shrink by the shadow
        # inset to the content (a no-op on the GNOME feed's geometry rect)
        rect, area = geometry_feed.frame_rect(inset=titlebar.window_inset()), geometry_feed.workarea()
        if rect is None or area is None:
            return None
        frame = geometry_feed._current_frame() or {}     # the ACTIVE surface's window (extension.py)
        return ((float(rect[0]), float(rect[1])), tuple(float(v) for v in area), "feed",
                (float(rect[0] + rect[2]), float(rect[1] + rect[3])), frame.get("id"))
    window = titlebar._studio_window()
    if window is None:
        return None
    try:
        x, y = glfw.get_window_pos(window)
        w, h = glfw.get_window_size(window)
        wa_l, wa_t, wa_r, wa_b = titlebar._workarea_for(window)
    except Exception:
        return None
    return ((float(x), float(y)), (wa_l, wa_t, wa_r - wa_l, wa_b - wa_t), "x11",
            (float(x + w), float(y + h)), id(window))


def _root_windows():
    """Top-level windows in this surface, deduped by identity. The studio
    uses its registered windows; GLFW surfaces keep their own live roots
    because the persisted registry also contains other surfaces' windows.
    Include parentless entries in the surface-local root_draw_states too."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.surface import Surface
    seen, roots = set(), []
    if Surface.active is not None:
        candidates = list(getattr(Surface.active, "root_windows", {}).values())
    else:
        candidates = [getattr(managed, "draw_state", None)
                      for managed in list(getattr(Melty, "registered_windows", {}).values())]
    for ds in candidates:
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
    content and rides with the OS frame. NESTED windows are not here: their
    window_pos is parent-relative, they ride with their parent."""
    return [ds for ds in _root_windows()
            if getattr(ds, "closable", False) and ds.window_pos is not None]


def _frame_pinned(ds):
    """A root whose frame the caller dictates every frame — an app's root
    pinned to the OS window (core_render stamps `_frame_pinned` for a
    closable, never-draggable window with width AND height passed). It
    RIDES with the surface: it cannot hold a screen position (the pin
    re-stamps window_pos next frame), so it is never re-based."""
    return bool(getattr(ds, "_frame_pinned", False))


def _pinned_children(axis, side):
    """The DIRECT nested closable windows of the frame-pinned roots that
    hang from the root's ``side`` ("near" / "far") corner on ``axis``
    (os_frame._driver_of: parent_anchor_pos). A pinned root's frame IS
    the OS window's, so its children are the studio roots of that world:
    they hold their SCREEN position when the OS edge they hang from
    moves — the near-hung ones through apply_rebase (the surface's origin
    moved), the far-hung ones through compensate_far (the root's far
    corner moved with the OS far edge: uncompensated, a context menu hung
    from the app's right corner and dragged right pushed the OS edge out,
    hung further right for it and pushed again — the app grew to the
    screen in a dozen frames, 09-13). Pins with a booked actual anchor
    defer to rebase_pin after parent layout instead."""
    pinned = {id(ds) for ds in _movable_roots() if _frame_pinned(ds)}
    if not pinned:
        return []
    return [ds for ds in _all_windows()
            if id(getattr(ds, "parent_window", None) or 0) in pinned
            and getattr(ds, "closable", False) and ds.window_pos is not None
            and id(ds) not in _STATE["pin_rebases"]
            and _driver_of(ds, axis) == side]


def _has_pin_anchor(ds):
    return getattr(ds, "_pin_target", None) is not None


def pin_origin(ds):
    """The actual pin anchor in screen coordinates, outside an edge solve.

    Pin.PARENT can point at a fixed-width text view or a split column;
    its right corner need not move with the OS window's right edge.
    """
    base = ds.clip_anchor_base
    if base is None:
        return None
    y = ds._pinned_base_y(base[1], ds.anchor_offset[1])
    return (applied_origin("x") + base[0], applied_origin("y") + y)


def _book_pin_rebases():
    """Remember drawn anchors before an own surface request. Only numbers
    are retained; closed windows drop out on the next request.
    """
    pending = _STATE["pin_rebases"]
    _STATE["pin_rebases"] = {
        id(ds): pending.get(id(ds), pin_origin(ds))
        for ds in _all_windows()
        if _open(ds) and _frame_pinned(getattr(ds, "parent_window", None))
        and _has_pin_anchor(ds)
    }


def rebase_pin(ds):
    """After parent layout, before the child's edge pass: keep its screen
    position through an OS resize using the pin's measured displacement.
    The parent has not been laid out yet at apply_rebase (frame start).
    """
    previous = _STATE["pin_rebases"].pop(id(ds), None)
    if previous is None or not _has_pin_anchor(ds):
        return
    current = pin_origin(ds)
    if current is not None:
        for axis, i in _AXIS.items():
            delta = previous[i] - current[i]
            if delta:
                _rebase(ds, axis, delta)


def _rebased_windows(axis="x"):
    """The windows that hold their SCREEN position when the OS NEAR edge
    moves (apply_rebase / a foreign near resize): the movable roots that
    are not frame-pinned, plus a pinned root's direct children hung from
    its near corner (_pinned_children). Without this a nested window that
    pushed the OS near edge out ended outside the surface by the push: its
    own pass slid it as if its parent held the screen, the parent was
    re-based, then pinned back (09-13)."""
    held = [ds for ds in _movable_roots() if not _frame_pinned(ds)]
    return held + _pinned_children(axis, "near")


def compensate_far(axis, d):
    """The OS FAR edge moved by ``d`` (a pass or the OS-level solve): a
    pinned root's children hung from its far corner ride that corner in
    content coordinates, so they are re-based by -d to hold the screen —
    exactly what apply_rebase does for the near edge, and like it BOOKED
    here and applied at the next frame start: the root is laid out at
    the model's size at frame start (surface.frame ← content_size), so
    the corner moves THEN; re-based in the same frame the child drew a
    push to the left of where it belonged for one frame (09-13)."""
    if d:
        _STATE["unapplied_far"][_AXIS[axis]] += d


def _depth(ds):
    depth, node = 0, getattr(ds, "parent_window", None)
    while node is not None and depth < 64:
        depth += 1
        node = getattr(node, "parent_window", None)
    return depth


def _all_windows():
    """Every melty window that collides at the OS level — the roots AND
    the nested windows (Melty.root_draw_states holds those under their
    parent's id) — parents before children (write-backs of a child are
    relative to its parent's motion). Deduped by identity."""
    from src.lsd.gl_gui.melty import Melty
    seen, windows = set(), []
    for ds in _root_windows():
        seen.add(id(ds))
        windows.append(ds)
    for group in list(getattr(Melty, "root_draw_states", {}).values()):
        for ds in list(group):
            if id(ds) in seen:
                continue
            seen.add(id(ds))
            windows.append(ds)
    windows.sort(key=_depth)
    return windows


def _rebase(ds, axis, delta):
    """Move a root by ``delta`` on ``axis`` in CONTENT coordinates so it
    keeps its screen position after the content origin moved the other
    way — the press-anchored corner drag's baseline with it."""
    pos = ds.window_pos or (0, 0)
    ds.window_pos = (pos[0] + delta, pos[1]) if axis == "x" else (pos[0], pos[1] + delta)
    # press-anchored baselines ride along: the corner resize's and the
    # window move's (the window dragging the studio along to stay under
    # the hand - its position is re-derived from this every frame)
    for attr in ("_initial_window_pos_resize", "_initial_window_pos"):
        base = getattr(ds, attr, None)
        if base is not None:
            setattr(ds, attr, (base[0] + delta, base[1]) if axis == "x" else (base[0], base[1] + delta))


def _set_mode(new_mode):
    if _STATE["mode"] != new_mode:
        _STATE["mode"] = new_mode
        _STATE["generation"] += 1          # every root forgets where it saw the OS edges
        _STATE["gestures"] = {}
        _STATE["expected"] = [None, None]
        _STATE["inflight"] = [None, None]
        _STATE["size_expected"] = [None, None]
        _STATE["size_requests"] = {"x": [], "y": []}
        _STATE["size_observed"] = [None, None]
        _STATE["size_request_frame"] = [None, None]
        _trace(f"mode → {new_mode}")


def _observe_size(axis, size):
    """Retire acknowledged requests; only outstanding sizes may lag the model."""
    i = _AXIS[axis]
    recent = _STATE["size_requests"][axis]
    previous = _STATE["size_observed"][i]
    _STATE["size_observed"][i] = size
    sent = _STATE["size_request_frame"][i]
    if sent is not None and _STATE["frame"] - sent > INFLIGHT_FRAMES:
        recent.clear()
    matches = [k for k, requested in enumerate(recent) if abs(size - requested) < 1.0]
    if matches:
        del recent[:matches[-1] + 1]
    elif previous is None or abs(size - previous) >= 1.0:
        recent.clear()                   # a new, unrequested compositor size
    if not recent:
        _STATE["size_request_frame"][i] = None
    return bool(recent)


def begin_frame():
    """Start of the frame, right after Melty.display_size is stamped: bring
    the OS model up to date with what is real — the content size (our own
    resizes land at frame start, a compositor's through the resize
    callback) and the observed position — and fold in what we did not
    request."""
    from src.lsd.gl_gui.melty import Melty
    # Surfaces saved before this code was hotswapped have no move history.
    _STATE.setdefault("move_requests", {"x": [], "y": []})
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
            _STATE["os_seen"][i] = (0.0, size[i])
        return
    (pos, area, new_mode, feed_far, window_id) = observed
    _set_mode(new_mode)
    if _STATE["window_id"] is not None and window_id != _STATE["window_id"]:
        reset(f"window {_STATE['window_id']} → {window_id}")
    _STATE["window_id"] = window_id
    for axis, i in _AXIS.items():
        scr_near, scr_far = _STATE["screen"][axis]
        scr_near[axis], scr_far[axis] = area[i], area[i] + area[i + 2]
        near, far = _STATE["edges"][axis]
        requested_size = (bool(_STATE["size_requests"][axis])
                          and _STATE["size_expected"][i] is not None
                          and abs(feed_far[i] - pos[i] - _STATE["size_expected"][i]) < 1.0)
        size_pending = _observe_size(axis, size[i])
        expected = _STATE["expected"][i]
        if expected is None:                      # first sight
            # A launch-fit move requested before this sight lands a frame
            # or two later: see the model where it will be, or the landing
            # reads as a foreign near resize and the roots get re-based.
            pending = _STATE["own_move_pending"][i]
            _STATE["own_move_pending"][i] = 0.0
            at = pos[i] + pending
            near[axis], far[axis] = at, at + size[i]
            _STATE["expected"][i] = at
            if pending:
                _STATE["inflight"][i] = Melty.frame_count
            _STATE["size_expected"][i] = size[i]
            _STATE["os_seen"][i] = (near[axis], far[axis])
            _STATE["feed_far"][i] = feed_far[i]
            _trace(f"{axis}: first sight near={pos[i]:.0f} size={size[i]:.0f}")
            continue
        # Position: our requests land a frame or two later; before then the
        # model already has them and the observation is behind.
        if pos[i] != expected:
            flight = _STATE["inflight"][i]
            asked = _STATE["last_offset"][i]
            # A move whose SIZE landed at a different position is not
            # necessarily "still landing": the compositor can clamp only
            # part of a move, or shift a resize to respect its own inset.
            # Fold that constraint in now, so
            # _foreign_change learns the wall this frame, not INFLIGHT_FRAMES
            # later with the far edge ratcheting meanwhile.
            # ... read off the FEED's own far edge (its size = far - pos):
            # only the compositor's committed state shows the new size at
            # the old position; a request merely in flight shows the old
            # size ( (glfw's framebuffer alone landed a frame earlier).
            refused = (asked and (pos[i] - expected) * asked < 0
                       and _STATE["size_expected"][i] is not None
                       and abs((feed_far[i] - pos[i]) - _STATE["size_expected"][i]) < 1.0)
            # With a size floor, all moves with different sizes. An
            # older requested box is an in-flight reply, not a refusal of
            # the latest move. Rebasing to it would send the offset twice.
            moves = _STATE["move_requests"][axis]
            observed_box = (pos[i], feed_far[i] - pos[i])
            older_reply = any(abs(observed_box[0] - at) < 1.0 and abs(observed_box[1] - extent) < 1.0
                              for at, extent in moves)
            if older_reply:
                refused = False
            constrained = requested_size and _any_button_down() and not older_reply
            if flight is not None and Melty.frame_count - flight <= INFLIGHT_FRAMES and not (refused or constrained):
                pass                              # still landing
            else:
                _STATE["move_requests"][axis].clear()
                d = pos[i] - expected
                _STATE["expected"][i] = pos[i]
                _STATE["inflight"][i] = None
                far_seen = _STATE["feed_far"][i]
                far_held = far_seen is not None and abs(feed_far[i] - far_seen) < 1.0
                _STATE["feed_far"][i] = feed_far[i]
                _foreign_change(axis, d, size[i], far_held, constrained=constrained)
                continue
        else:
            _STATE["inflight"][i] = None
            _STATE["move_requests"][axis].clear()
            _STATE["last_offset"][i] = 0
            _STATE["feed_far"][i] = feed_far[i]
        # Size: ours lands at frame start (size_expected); anything else is
        # the compositor's - the far edge moved.
        if size[i] != far[axis] - near[axis]:
            if size_pending:
                # an OLDER request of ours landed; the newest is still in
                # flight and the model already holds it - nothing to fold
                pass
            elif _STATE["size_expected"][i] != size[i]:
                _trace(f"{axis}: foreign far edge {far[axis]:.0f} → {near[axis] + size[i]:.0f}")
                far[axis] = near[axis] + size[i]
            else:
                # our resize landed: the integer size snaps the model's far
                # edge by a fraction - not a move, nothing to solve
                seen = _STATE["os_seen"][i]
                far[axis] = near[axis] + size[i]
                if seen is not None and abs(seen[1] - far[axis]) < 1.0:
                    _STATE["os_seen"][i] = (seen[0], far[axis])
            if not size_pending:
                _STATE["size_expected"][i] = size[i]
    for axis in _AXIS:
        _walls_to_edges(axis)


def _walls_to_edges(axis):
    """An OS edge already PAST the screen wall (the studio dragged partly
    off the screen by the compositor's own move; Hyprland's reserved strip
    moving the work area's edge under it) is where it is: the wall on that
    side moves out to the edge for this frame's solves. Left at the work
    area, the solver read the wall's floor chain as violated and clamped
    the edge back onto the wall on the first OS-level drag — the studio
    teleported by the whole overhang and grew by as much on the other
    side (a top-left right-drag on a studio 475 px off the left edge,
    09-09). Pinned there, a drag further out is blocked and flips (the
    far side grows), a drag inward moves the edge, and the next frame's
    stamp follows it back toward the work area."""
    if _STATE["mode"] == "walls":
        return
    near, far = _STATE["edges"][axis]
    scr_near, scr_far = _STATE["screen"][axis]
    learned = _STATE["learned"][axis]
    # A wall the compositor taught us (a refused move): forget once the
    # hand lets go, or once the edge is seen past it after all.
    if not _any_button_down():
        learned[0] = learned[1] = None
    if learned[0] is not None and near[axis] < learned[0] - 1.5:
        learned[0] = None
    if learned[1] is not None and far[axis] > learned[1] + 1.5:
        learned[1] = None
    if learned[0] is not None:
        scr_near[axis] = max(scr_near[axis], learned[0])
    if learned[1] is not None:
        scr_far[axis] = min(scr_far[axis], learned[1])
    scr_near[axis] = min(scr_near[axis], near[axis])
    scr_far[axis] = max(scr_far[axis], far[axis])


def _foreign_change(axis, d, size, far_held, constrained=False):
    """The window's near edge is ``d`` px from where we expected it and we
    asked for nothing: the compositor moved or resized the studio.
    ``far_held`` — the feed's own far edge (x + width) did not move → a
    NEAR RESIZE: the roots hold their screen positions (re-base) and get
    pushed by the edge where it reaches them (solve). Otherwise a MOVE by
    ``d`` (the roots ride along, nothing to solve) plus whatever the far
    edge did on top. Classified from ONE source on purpose: the feed's
    position against our own size read as a resize whenever the two were
    a frame apart."""
    i = _AXIS[axis]
    near, far = _STATE["edges"][axis]
    old_far = far[axis]
    near[axis] += d
    if far_held:
        _trace(f"{axis}: foreign near resize {d:+.0f} — roots hold the screen")
        for ds in _rebased_windows(axis):
            _rebase(ds, axis, -d)
        far[axis] = near[axis] + size
    else:
        _trace(f"{axis}: foreign move {d:+.0f} (size {old_far - (near[axis] - d):.0f} → {size:.0f})")
        for ds in _all_windows():
            seen = getattr(ds, "_os_seen", None)
            if seen and axis in seen:
                seen[axis] = (seen[axis][0] + d, seen[axis][1] + d)
            # A compositor MOVE carries the surface and its drag origin
            # together. Otherwise sticky replay interprets the new origin as
            # resize motion and throws the view back to its old screen spot.
            gesture = (getattr(ds, "_edge_gestures", None) or {}).get(axis)
            if gesture is not None:
                gesture["origin"] += d
                if gesture["os"]:
                    gesture["os"] = {key: value + d for key, value in gesture["os"].items()}
        gesture = _STATE["gestures"].get(axis)
        if gesture is not None:
            gesture["snap"] = [value + d for value in gesture["snap"]]
            for identity, saved in list(gesture.get("windows", {}).items()):
                n, f, floor, span, edge_values = saved
                gesture["windows"][identity] = (n + d, f + d, floor, span, edge_values)
        os_seen = _STATE["os_seen"][i]
        if os_seen is not None:
            _STATE["os_seen"][i] = (os_seen[0] + d, os_seen[1] + d)
        far[axis] = near[axis] + size
    asked = _STATE["last_offset"][i]
    if constrained or (asked and d * asked < 0):
        # The compositor constrained our move/resize, possibly only partly:
        # the displaced edge is clamped THERE. Learn that wall
        # for the gesture - the flip then has no room on that side - and
        # pull the OTHER edge back inside the work area: the size changes
        # land, so the far edge now overhangs by the refused move, and
        # left "where it is" the next flip grew it by one more screen
        # size (the studio's bottom ratcheting below the display).
        scr_near, scr_far = _STATE["screen"][axis]
        learned = _STATE["learned"][axis]
        if d > 0:
            learned[0] = near[axis]
            far[axis] = min(far[axis], scr_far[axis])
        else:
            learned[1] = far[axis]
            near[axis] = max(near[axis], scr_near[axis])
        _trace(f"{axis}: move/resize constrained (offset {asked:+.0f}) — wall learned at "
               f"{learned[0] if d > 0 else learned[1]:.0f}")
    _STATE["size_expected"][i] = size


# ---------------------------------------------------------------------------
# The solve: what a root's frame needs gets, and what it hands back
# ---------------------------------------------------------------------------

class Context:
    """One root window's view of the OS level for one axis' solve. The OS
    and screen edge dicts are shifted into the WINDOW's coordinates
    (by -base) for the solve and back in detach — the window's own edges
    are never touched unless the solve moves them."""
    __slots__ = ("axis", "base", "os_near0", "os_far0", "lists", "specs", "walls",
                 "drags", "os_ids", "shifted", "move")

    def __init__(self, axis):
        self.axis = axis
        self.base = 0.0
        self.os_near0 = 0.0
        self.os_far0 = 0.0
        self.lists, self.specs, self.drags = [], [], []
        self.walls = frozenset()
        self.os_ids = frozenset()
        self.shifted = ()
        self.move = False              # the window was moved by hand this frame


def applied_origin(axis):
    """The surface's content origin on ``axis`` in SCREEN coords as it is
    APPLIED right now: the model's near edge less the motion not yet
    landed (apply_rebase). What a window's content coordinate is relative
    to this frame. Only valid OUTSIDE a window's solve (attach shifts the
    OS dicts into window coordinates until detach — inside, read it off
    ctx.base as columns._replay_hand_drags does). 0 with the OS level off."""
    if not _enabled():
        return 0.0
    near, _far = _STATE["edges"][axis]
    return float(near[axis] - _STATE["unapplied"][_AXIS[axis]])


def queue_drag(axis, index, inc):
    """A cursor-driven drag of the OS window's own frame edge (``index`` 0
    = near / left / top, 1 = far) by ``inc`` px — the background right-drag
    (titlebar). Solved by the first root pass of the frame, else by flush."""
    if inc:
        _STATE["pending"][axis].append((int(index), float(inc)))


def attach(window, axis, has_pending=True, hand_move=False):
    """Called by columns._frame_pass for a ROOT window before its solve:
    the OS-level cells / walls / drags to add, in the WINDOW's coordinates
    (the OS and screen dicts shifted by -base, base = the screen coordinate
    of the window's near edge; detach shifts them back). None when the OS
    level is off, or when there is nothing to solve at all — no queued
    drag of the window (``has_pending``), no hand move of it this frame
    (``hand_move``: its frame pushes the OS edge it overlaps,
    Toggles.Melty.window_move_pushes_os_edges), none of the OS window's
    own drags, no OS edge moved since this window last saw it — so an
    idle frame touches nothing."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.toggles import Toggles
    if not _enabled() or _STATE["frame"] != Melty.frame_count:     # only in a frame begin_frame set up
        return None
    # Nested windows take part exactly like roots (Lukas 08-27): their
    # abs_left / abs_top IS their screen position (OS offsets folded
    # in), and their parent-relative window_pos takes the same px deltas.
    hand_move = bool(hand_move and Toggles.Melty.window_move_pushes_os_edges
                     and _STATE["mode"] != "walls")
    near, far = _STATE["edges"][axis]
    seen_all = getattr(window, "_os_seen", None)
    if seen_all is None or getattr(window, "_os_gen", None) != _STATE["generation"]:
        seen_all = window._os_seen = {}
        window._os_gen = _STATE["generation"]
    seen = seen_all.get(axis)
    cur = (near[axis], far[axis])
    os_moved = seen is not None and (abs(seen[0] - cur[0]) > 1e-6 or abs(seen[1] - cur[1]) > 1e-6)
    if seen is None:
        seen_all[axis] = cur
    if not (has_pending or os_moved or hand_move):
        return None
    ctx = Context(axis)
    ctx.move = hand_move
    ctx.os_ids = frozenset({id(near), id(far)})
    ctx.os_near0 = near[axis]
    ctx.os_far0 = far[axis]
    i = _AXIS[axis]
    # the window's frame origin: its own coordinate is relative to the
    # screen origin its frame was last re-based for (the model's near
    # origin less what apply_rebase has not applied yet)
    ctx.base = near[axis] - _STATE["unapplied"][i] + _screen_pos(window, axis)
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
    if os_moved and hand_move:
        # A hand-moved window is where the cursor put it: the OS edges'
        # motion since it last looked (its PARENT's frame pushing the same
        # edge, this very frame, ahead of the child riding with it) is
        # not a foreign drag to fold through its cells - folded in, the
        # edge the parent pushed OUT pushed the child back next to it. The
        # push block (_solve_collisions - ctx.move) handles the rest.
        os_moved = False
    if os_moved:
        # (the foreign drag is forced to its floor right after it is
        # solved - columns._solve_collisions - so the edge ends where it
        # really is whatever the pile could give)
        near[axis], far[axis] = seen
        endpoints = [(near, seen[0], cur[0]), (far, seen[1], cur[1])]
        # A translating minimum-width frame must open up before its
        # trailing edge pushes through it. Near-first on a rightward move
        # walls that edge against the OLD far edge, and expands the frame
        # instead of carrying the packed column edges along.
        if cur[1] > seen[1]:
            endpoints.reverse()
        for edge, previous, target in endpoints:
            if previous != target:
                ctx.drags.append((edge, target - ctx.base, None))
    for e in ctx.shifted:
        e[axis] -= ctx.base
    return ctx


def gap_lists(ctx, window_near, window_far, rigid=False):
    """The two zero-floor gap cells linking a root's frame pair (screen
    coords) to the OS frame pair. A free root's gaps are uncapped: it
    sits anywhere inside the surface and only a push reaches the OS
    edge. ``rigid`` (a frame-pinned root — an app's root, its frame IS
    the OS window's) caps them at zero too, so the OS edge follows the
    frame edge BOTH ways: a divider pushed past the pile pushes it out,
    a capped column at its maximum PULLS it in, a right-drag on the
    frame edge shrinks the window — exactly what a studio window's own
    frame does, with no special handling on the drags (Lukas 09-13)."""
    near, far = _STATE["edges"][ctx.axis]
    cap = 0.0 if rigid else None
    return ([[near, window_near], [window_far, far]],
            [([0.0], [cap]), ([0.0], [cap])])


def content_size(display):
    """The size an app's root should be laid out at THIS frame: the OS
    model's (far - near per axis) once begin_frame has run for the frame,
    else the display's. They differ only while our own resize request is
    in flight (Hyprland answers the IPC resize a frame or two later); laid
    out at the lagging display size the pinned root's frame was pulled
    back to the old size for a frame and pushed out again the next — the
    dividers froze and jittered against it (Lukas 09-13). Foreign sizes
    are folded into the model at begin_frame, so this never hides one."""
    from src.lsd.gl_gui.melty import Melty
    if not _enabled() or _STATE["frame"] != Melty.frame_count:
        return display
    out = []
    for axis, i in _AXIS.items():
        near, far = _STATE["edges"][axis]
        size = far[axis] - near[axis]
        out.append(float(display[i]) if size <= 0 else float(size))
    return tuple(out)


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
    compensate_far(axis, far[axis] - ctx.os_far0)
    window._os_seen[axis] = (near[axis], far[axis])
    return d_os


def expect_own_move(dx, dy):
    """A window move WE requested outside flush — titlebar's launch fit
    (apply_pending_surface_size: the size and the move that keeps the
    content inside the work area, one commit). Book it like flush books
    its own: the expected position moves with it (in flight, so the feed's
    late report is not a foreign change) and the model's edges — near AND
    far, the whole window moved — with everything that remembers them.
    Without this the fit's move read as the compositor resizing from the
    near edge and every root was re-based by it: a @glfw_window app came up
    with its content shoved right by the fit's distance (09-12). Before the
    feed's first sight the move is parked for begin_frame to fold in."""
    from src.lsd.gl_gui.melty import Melty
    for axis, i in _AXIS.items():
        d = float((dx, dy)[i])
        if not d:
            continue
        if _STATE["expected"][i] is None:
            _STATE["own_move_pending"][i] += d
            _trace(f"{axis}: own move {d:+.0f} parked for first sight")
            continue
        _STATE["expected"][i] += d
        _STATE["inflight"][i] = Melty.frame_count
        near, far = _STATE["edges"][axis]
        near[axis] += d
        far[axis] += d
        for ds in _all_windows():
            seen = getattr(ds, "_os_seen", None)
            if seen and axis in seen:
                seen[axis] = (seen[axis][0] + d, seen[axis][1] + d)
        os_seen = _STATE["os_seen"][i]
        if os_seen is not None:
            _STATE["os_seen"][i] = (os_seen[0] + d, os_seen[1] + d)
        _trace(f"{axis}: own move {d:+.0f} (launch fit)")


def apply_rebase():
    """Frame START, from titlebar.apply_pending_surface_size — the moment
    the queued size / move is applied: move every root by the OS near
    edge's booked motion in CONTENT coordinates so it keeps its SCREEN
    position, in the same commit as the surface's move."""
    for axis, i in _AXIS.items():
        d_far = _STATE["unapplied_far"][i]
        if d_far:
            _STATE["unapplied_far"][i] = 0.0
            for ds in _pinned_children(axis, "far"):
                _rebase(ds, axis, -d_far)
        d = _STATE["unapplied"][i]
        if not d:
            continue
        _STATE["unapplied"][i] = 0.0
        for ds in _rebased_windows(axis):
            _rebase(ds, axis, -d)
        _trace(f"{axis}: roots re-based {-d:+.0f} with the move")


# Edges closer than this occupy one place in the flat chain (tie by role).
CHAIN_TOL = 1.0


# Edge roles in the flat chain (also the tie order): a far edge sits before
# a near edge at the same place, the OS far edge last of the fars, the OS
# near edge first of the nears.
ROOT_FAR, OS_FAR, OS_NEAR, ROOT_NEAR = 0, 1, 2, 3


def _flat_chain_ranked(os_pair, roots, axis):
    """Position-ordered window edges enclosed by the two OS boundaries.

    Preserve real window-edge order even when a window overhangs the OS
    frame. Clamping every sort coordinate into the OS span and then sorting
    by role reversed a fully outside window's near/far pair, making a
    positive-length cycle with its own minimum-size cell.
    """
    near, far = os_pair
    ranked = []
    for _window, window_near, window_far, _floor in roots:
        ranked.extend(((window_near, ROOT_NEAR), (window_far, ROOT_FAR)))
    ranked.sort(key=lambda item: item[0][axis])
    # Make genuinely coincident window edges settle by role. Their real
    # ordering remains intact outside the surface as well as inside it.
    for index in range(1, len(ranked)):
        cursor = index
        while cursor > 0:
            previous, current = ranked[cursor - 1], ranked[cursor]
            if (abs(current[0][axis] - previous[0][axis]) >= CHAIN_TOL
                    or previous[1] <= current[1]):
                break
            ranked[cursor - 1], ranked[cursor] = current, previous
            cursor -= 1
    # The OS edges are the enclosing boundaries, including for an
    # overhanging window: an inward push must still be able to reach it.
    return [(near, OS_NEAR), *ranked, (far, OS_FAR)]


def _flat_chain(os_pair, roots, axis):
    return [edge for edge, _role in _flat_chain_ranked(os_pair, roots, axis)]


def _chain_floor(a, role_a, b, role_b, axis):
    """The floor of the cell between consecutive chain edges ``a`` → ``b``.
    Against an OS edge: 0 (a window touches the OS edge). A window's far
    edge followed by another's near edge: 0 — the windows meet on the
    OUTSIDE and touch (the collision rects do not extend past the
    windows, Lukas 08-27). Anything else — a near edge inside another
    window, a far edge inside another window — keeps the columns' usual
    margin, the axis minimum, never more than the edges are apart now
    (freely placed overlaps must not snap apart on a first push)."""
    if role_a in (OS_FAR, OS_NEAR) or role_b in (OS_FAR, OS_NEAR):
        return 0.0
    if role_a == ROOT_FAR and role_b == ROOT_NEAR:
        return 0.0
    from src.lsd.gl_gui.view.core_views.columns import _axis_min
    return min(_axis_min(axis), max(0.0, b[axis] - a[axis]))


def _screen_pos(ds, axis):
    """A window's content position on ``axis`` as DRAWN (the wrapper's
    abs_left / abs_top — a nested window's sliver cap included: solving on
    the uncapped position made a parent drag push the OS edge out to where
    a scrolled-off child "really" was, and the studio jumped — reverted,
    Lukas 08-27)."""
    return float((ds.abs_left if axis == "x" else ds.abs_top) or 0)


def _window_floor(ds, axis):
    """How far the OS-level solve may compress ``ds`` on ``axis``: its
    declared minimum (raised to its columns' pile by its own pass), never
    below the axis minimum, never above its size."""
    from src.lsd.gl_gui.view.core_views.columns import _axis_min
    size = float(ds.width if axis == "x" else ds.height)
    declared = float((ds.min_width if axis == "x" else ds.min_height) or 0)
    return min(size, max(_axis_min(axis), declared))


def _open(ds):
    return (getattr(ds, "closable", False) and ds.window_pos is not None
            and not getattr(ds, "closed", False) and getattr(ds, "expanded", True)
            and bool(ds.width) and bool(ds.height))


def _root_of(ds):
    node, depth = ds, 0
    while getattr(node, "parent_window", None) is not None and depth < 64:
        node = node.parent_window
        depth += 1
    return node


def _any_button_down():
    """A mouse button is held — a hand gesture is alive (the sticky replay's
    lifetime, here and in columns)."""
    from src.lsd.gl_gui.melty import Melty
    handler = getattr(Melty, "event_handler", None)
    if handler is None:
        return False
    try:
        return any(handler.is_down(b) for b in ("left_mouse", "right_mouse", "middle_mouse"))
    except Exception:
        return False


def _colliding_windows():
    """The windows of the OS-level solve, parents before children: the
    open movable roots and their open nested descendants."""
    roots = [ds for ds in _movable_roots() if _open(ds)]
    root_ids = {id(r) for r in roots}
    nested = [ds for ds in _all_windows()
              if getattr(ds, "parent_window", None) is not None and _open(ds)
              and id(_root_of(ds)) in root_ids]
    return roots + nested


def _driving_parent_edge(child, axis):
    """Which of the PARENT's edges on ``axis`` places ``child``: the
    corner of the parent the child is positioned from
    (draw_state.parent_anchor_pos, the top-left by default) — "near", or
    "far" for a right / bottom parent corner. The parent's OTHER edge is an
    ordinary edge: a child's free edge pushing it compresses the parent."""
    anchor = getattr(child, "parent_anchor_pos", None)
    name = getattr(anchor, "value", anchor) or ""
    if axis == "x":
        return "far" if str(name).endswith("right") else "near"
    return "far" if str(name).startswith("bottom") else "near"


def _driver_of(child, axis):
    """The parent edge that drives ``child`` on ``axis`` — read off the
    draw_state, never guessed: ``parent_anchor_pos`` names the corner of
    the parent the child hangs from in BOTH placement paths (the pin's
    clip_anchor_base and the unpinned parent_anchor_offset) — a right /
    bottom parent anchor is the far edge, anything else the near edge
    (Lukas 08-27: "you have the draw_state, you don't need to guess
    anything, just look at the clip option")."""
    return _driving_parent_edge(child, axis)


def _frame_of(ds, axis, applied):
    """A window's frame as two fresh proxy edges (screen coords) with its
    floor and cap: it may compress to its minimum, never widen."""
    pos = _screen_pos(ds, axis)
    size = float(ds.width if axis == "x" else ds.height)
    floor = _window_floor(ds, axis)
    return {axis: applied + pos}, {axis: applied + pos + size}, floor, size


def _extent_of(root, children, frames, axis):
    """``root``'s collision EXTENT as fresh proxy edges: the union of its
    frame and its ``children``'s frames (all taken from ``frames``, the
    positions phase A left them at), with the floor that keeps every
    child inside — the root compresses down to its own minimum or to the
    farthest child's far edge relative to the root's near edge, whichever
    is larger (a child overhanging the far side leaves no give at all:
    compressing a parent never moves its children, they hang off its near
    edge). Returns (near, far, floor) with floor as the cell's floor."""
    from src.lsd.gl_gui.view.core_views.columns import _axis_min
    r_n, r_f, r_floor, _size0 = frames[id(root)]
    r_size = r_f[axis] - r_n[axis]                # as phase A left it, not as built
    near, far = r_n[axis], r_f[axis]
    floor = r_floor
    for child in children:
        if getattr(child, "_capped_x" if axis == "x" else "_capped_y", False):
            continue                              # drawn at the display's sliver cap: scrolled off
        c_n, c_f, _fl, _sz = frames[id(child)]
        near, far = min(near, c_n[axis]), max(far, c_f[axis])
        if _driver_of(child, axis) == "far":
            # driven by the root's far edge: it moves with that edge when
            # the root compresses - the still leave the root's give
            continue
        # a child inside keeps the minimum margin from the root's far edge
        # (as phase A left); an overhanging child leaves no give at all
        margin = min(_axis_min(axis), max(0.0, r_f[axis] - c_f[axis]))
        floor = max(floor, c_f[axis] - r_n[axis] + margin)
    floor = min(floor, r_size)
    give = max(0.0, r_size - floor)
    return {axis: near}, {axis: far}, max(0.0, (far - near) - give)


def solve():
    """Frame start, after begin_frame and the titlebar's poll: the OS
    window's own drags (queue_drag) and the OS edges' motion since the last
    solve (the compositor's resize) — the GLFW window resizes — solved
    per axis against every root window and nested window, all edges
    independent collidable objects in screen coordinates (Lukas 08-27):
    every window's frame as a cell floored at its minimum and capped at
    its size, the OS frame cell, the screen walls, and a cell between each
    consecutive pair of edges in position order (_chain_floor). No other
    drag collides windows with each other: a melty window's own resize and
    a hand move solve in the window's own pass (attach). A frame-pinned
    app root sends its frame-handle drags here too: it IS the GLFW frame.

    NESTED windows in two phases (Lukas 08-27: "child windows collide
    normally, only revert to adjusting the parent when the collision
    cascades into the parent"). Phase A solves everything as independent
    objects with the PARENTS' edges as walls: a child compresses and
    slides inside its parent, pushes its siblings and the OS edge, but
    nothing can move a parent — a child's position is a function of its
    parent's, and a push that cascades into the parent re-lays the parent
    out and moves the child again (the feedback loop). Whatever the drag
    could not do against those walls — a child pinned against its parent,
    the OS edge on a parent itself — is phase B: the parents as EXTENTS
    (their frame ∪ their children where phase A left them, _extent_of),
    pushed as blocks; the children ride and are never written for it.
    Pushed windows get position / size written back; their own pass packs
    their columns as a foreign size write. The OS near edge's motion is
    booked for apply_rebase like any other."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.view.core_views.columns import (_cells_from_lists, _EdgeGraph,
                                                        _solve_graph, snap_int)
    if not _enabled() or _STATE["frame"] != Melty.frame_count:
        return
    for axis, i in _AXIS.items():
        near, far = _STATE["edges"][axis]
        cur = (near[axis], far[axis])
        seen = _STATE["os_seen"][i]
        own = _STATE["pending"][axis]
        _STATE["pending"][axis] = []
        gestures = _STATE.setdefault("gestures", {})
        released = not _any_button_down()
        # A stationary release still ends the gesture. Do this before
        # the idle fast-forward, or the next drag replays an old position.
        if released and not own:
            gestures.pop(axis, None)
        os_moved = seen is not None and (abs(seen[0] - cur[0]) > 1e-6 or abs(seen[1] - cur[1]) > 1e-6)
        if not own and not os_moved:
            continue
        _book_pin_rebases()
        walls_mode = _STATE["mode"] == "walls"
        applied = near[axis] - _STATE["unapplied"][i]
        near0 = near[axis]
        if os_moved:
            # the OS edges start from where the windows were laid out
            # against (the graph's position order must see them THERE, or a
            # moved edge sorts past the very edges it tries to shift)
            near[axis], far[axis] = seen
        # A frame-pinned app root IS the OS frame, not another obstacle
        # inside it. A duplicate proxy root to wall off the inspector
        # since an inward OS edge could reach and compress it.
        windows = [ds for ds in _colliding_windows()
                   if not _frame_pinned(ds)
                   and not getattr(ds, "_capped_x" if axis == "x" else "_capped_y", False)]
        window_ids = {id(ds) for ds in windows}

        def collision_root(ds):
            while id(getattr(ds, "parent_window", None)) in window_ids:
                ds = ds.parent_window
            return ds

        children_of = {}
        for ds in windows:
            root = collision_root(ds)
            if root is not ds:
                children_of.setdefault(id(root), []).append(ds)
        frames = {id(ds): _frame_of(ds, axis, applied) for ds in windows}
        start = {wid: (n[axis], f[axis]) for wid, (n, f, _fl, _sz) in frames.items()}
        gesture = gestures.get(axis)
        if own:
            from src.lsd.gl_gui.view.core_views.columns import snapshot_edges
            if gesture is None:
                gesture = gestures[axis] = {"snap": list(cur), "totals": [0.0, 0.0], "windows": {}}
            snapshots = gesture.setdefault("windows", {})
            for window in windows:
                identity = id(window)
                n, f, floor, size = frames[identity]
                if identity not in snapshots:
                    snapshots[identity] = (n[axis], f[axis], floor, size, snapshot_edges(window, axis))
                saved_near, saved_far, floor, size, saved_edges = snapshots[identity]
                n[axis], f[axis] = saved_near, saved_far
                frames[identity] = n, f, floor, size
                for edge, value in saved_edges:
                    edge[axis] = value
        os_floor = max([MIN_SIZE[i]] + [_window_floor(ds, axis) for ds in _movable_roots()
                                       if _frame_pinned(ds) and _open(ds)])

        if walls_mode:
            os_list, os_spec = [near, far], ([os_floor], [None])
            walls = frozenset({id(near), id(far)})
        else:
            scr_near, scr_far = _STATE["screen"][axis]
            os_list, os_spec = [scr_near, near, far, scr_far], ([0.0, os_floor, 0.0], [None, None, None])
            walls = frozenset({id(scr_near), id(scr_far)})

        def graph_of(cells):
            """cells: [(ds_or_None, n, f, floor, cap)] → the cell graph with
            the OS list and the flat chain over all of them."""
            lists, specs = [os_list], [os_spec]
            ranked_roots = []
            for _ds, n, f, floor, cap in cells:
                lists.append([n, f]); specs.append(([floor], [max(floor, cap)]))
                ranked_roots.append((None, n, f, floor))
            ranked = _flat_chain_ranked([near, far], ranked_roots, axis)
            if len(ranked) > 1:
                floors = [_chain_floor(a, ra, b, rb, axis)
                          for (a, ra), (b, rb) in zip(ranked, ranked[1:])]
                lists.append([edge for edge, _role in ranked])
                specs.append((floors, [None] * len(floors)))
            return _EdgeGraph(_cells_from_lists(lists, axis, specs=specs))

        # the drags: the OS edges' foreign motion (the edge IS where it is;
        # the other OS edge holds), then the OS window's own drags
        drags = []
        if os_moved:
            for edge, target in ((near, cur[0]), (far, cur[1])):
                if abs(target - edge[axis]) > 1e-6:
                    drags.append((edge, target, False))
        # The OS window's own hand drags are STICKY (Lukas 09-13, the feel of
        # Hyprland's right-drag): the edge's target is the gesture's start
        # plus the accumulated total, not the current position plus this
        # frame's increment - a step lost to the screen wall is not
        # forgotten, and the edge returns to where it began when the hand
        # does. The affected window frames and their layout edges are
        # restored above, so their geometry returns with the OS edge.
        # The gesture lives while a mouse button is held.
        gesture = gestures.get(axis)
        for index, inc in own:
            if gesture is None:
                gesture = gestures[axis] = {"snap": list(cur), "totals": [0.0, 0.0]}
            gesture["totals"][index] += inc
        if own and gesture is not None:
            # replay from the gesture's start: BOTH edges go back to the
            # snapshot - the wall moves the opposite edge by the residual,
            # and with only the opposite edge restored that residual (the
            # whole travel past the wall, growing every frame) lands on
            # the other edge again and again: -98, -198, -545 px for 100
            # px of hand (09-13)
            near[axis], far[axis] = gesture["snap"]
            for index in (0, 1):
                if gesture["totals"][index]:
                    edge = (near, far)[index]
                    drags.append((edge, gesture["snap"][index] + gesture["totals"][index], True))
            if released:
                gestures.pop(axis, None)          # the release frame's own increment replayed, then done

        # ---- phase A: every window its own object. A child's own edges
        # are ordinary: its far edge compresses it to its minimum and then
        # pushes its near edge (the child moves relative to its corner),
        # like any window. The ONLY special case is a child and its DRIVING
        # edge (Lukas 08-27): the parent corner that places it
        # (_driver_of, up the ancestors) is a wall - a child pushing it
        # would move the parent, hence the corner, hence the child again.
        # For the same reason a child may push a parent edge only INWARD
        # (compressing the parent, the pending_waves window pushing its
        # parent's right edge), never OUTWARD (that moves the whole parent,
        # its cell capped at its size): the parent edge a drag would move
        # outward is a wall for that drag. A push that causes the driving edge
        # to move is the cascade phase B answers.
        cells_a = [(ds, n, f, floor, size) for ds, (n, f, floor, size) in ((ds, frames[id(ds)]) for ds in windows)]
        driving_walls, parent_near, parent_far = set(), set(), set()
        for ds in windows:
            parent = getattr(ds, "parent_window", None)
            if parent is None:
                continue
            node = parent
            while node is not None and id(node) in frames:
                pn, pf, _pfl, _psz = frames[id(node)]
                driving_walls.add(id(pn) if _driver_of(ds, axis) == "near" else id(pf))
                parent_near.add(id(pn))
                parent_far.add(id(pf))
                node = getattr(node, "parent_window", None)
        graph_a = graph_of(cells_a)
        residual = []
        for edge, target, cursor in drags:
            outward = parent_far if target > edge[axis] else parent_near   # the parent edges this drag would push OUT
            edge_walls = walls | driving_walls | outward
            if not cursor:
                edge_walls = edge_walls | ({id(near), id(far)} - {id(edge)})
            _solve_graph(graph_a, edge, target, walls=frozenset(edge_walls), axis=axis)
            if abs(target - edge[axis]) > 1e-6:
                residual.append((edge, target, cursor))
        after_a = {wid: (n[axis], f[axis]) for wid, (n, f, _fl, _sz) in frames.items()}

        # ---- phase B: the remainder, parents as blocks (their children flat)
        extents = {}
        if residual:
            cells_b = []
            for ds in windows:
                if collision_root(ds) is not ds:
                    continue                          # folded into its root's extent
                kids = children_of.get(id(ds))
                if kids:
                    n, f, floor = _extent_of(ds, kids, frames, axis)
                    extents[id(ds)] = (n, f, n[axis], f[axis])
                    cells_b.append((ds, n, f, floor, f[axis] - n[axis]))
                else:
                    n0, f0, floor, _size = frames[id(ds)]
                    n, f = {axis: n0[axis]}, {axis: f0[axis]}
                    extents[id(ds)] = (n, f, n[axis], f[axis])
                    cells_b.append((ds, n, f, floor, f[axis] - n[axis]))
            graph_b = graph_of(cells_b)
            for edge, target, cursor in residual:
                edge_walls = walls if cursor else (walls | ({id(near), id(far)} - {id(edge)}))
                _solve_graph(graph_b, edge, target, walls=frozenset(edge_walls), axis=axis)
                left = target - edge[axis]
                if cursor:
                    opposite = near if left > 0 else far
                    if abs(left) > 1e-6 and opposite is not edge:           # the flip
                        _solve_graph(graph_b, opposite, opposite[axis] - left, walls=frozenset(edge_walls), axis=axis)
                else:
                    edge[axis] = float(target)                             # forced: it's there

        # ---- fold back (children: their phase-A motion, parent-relative;
        # roots: phase A + their block's phase B; round off the size)
        for ds in windows:
            wid = id(ds)
            n0, f0 = start[wid]
            n_a, f_a = after_a[wid]
            delta, new_size = n_a - n0, f_a - n_a
            parent = getattr(ds, "parent_window", None)
            if id(parent) in after_a:
                # A parent's solved anchor edge already carries its child.
                # Write back the child's remaining motion in parent coordinates.
                anchor = 0 if _driver_of(ds, axis) == "near" else 1
                delta -= after_a[id(parent)][anchor] - start[id(parent)][anchor]
            if wid in extents:
                n_b, f_b, nb0, fb0 = extents[wid]
                delta += n_b[axis] - nb0
                new_size -= (fb0 - nb0) - (f_b[axis] - n_b[axis])
            if abs(delta) > 1e-6:
                _rebase(ds, axis, delta)
            size = float(ds.width if axis == "x" else ds.height)
            if abs(new_size - size) > 0.5:
                if axis == "x":
                    ds.width = snap_int(new_size)
                else:
                    ds.height = snap_int(new_size)
            seen_all = getattr(ds, "_os_seen", None)
            if seen_all is not None:
                seen_all[axis] = (near[axis], far[axis])
        d_os = near[axis] - near0
        if d_os:
            _STATE["unapplied"][i] += d_os
            _trace(f"{axis}: OS near edge moved {d_os:+.0f} (OS-level solve)")
        compensate_far(axis, far[axis] - cur[1])
        _STATE["os_seen"][i] = (near[axis], far[axis])


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
        recent = _STATE["size_requests"][axis]
        if recent and recent[-1] != size[i]:
            changed = True              # cancel the older request even at the observed size
        if _STATE["mode"] != "walls":
            expected = _STATE["expected"][i]
            if expected is not None and abs(near[axis] - expected) > 0.5:
                moves = _STATE["move_requests"][axis]
                previous = (expected, _STATE["size_expected"][i])
                if not moves or moves[-1] != previous:
                    moves.append(previous)
                # Keep a bounded history even if the OS feed stalls.
                del moves[:-INFLIGHT_FRAMES * 2]
                offset[i] = int(round(near[axis] - expected))
                _STATE["expected"][i] = expected + offset[i]
                _STATE["inflight"][i] = Melty.frame_count
                changed = True
        _STATE["size_expected"][i] = float(size[i])
    if not changed:
        return None
    _book_pin_rebases()
    for axis, i in _AXIS.items():
        recent = _STATE["size_requests"][axis]
        if (recent or size[i] != _STATE["size_observed"][i]) and (not recent or recent[-1] != size[i]):
            recent.append(float(size[i]))
            _STATE["size_request_frame"][i] = Melty.frame_count
    for i in range(2):
        if offset[i]:
            # A size-only follow-up must not overwrite the unacknowledged move.
            _STATE["last_offset"][i] = offset[i]
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
