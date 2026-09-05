from contextlib import contextmanager

import imgui

from src.lsd.gl_gui import mouse_cursor
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
from src.lsd.gl_gui.view.invalidation_tracker import Note

MIN_COLUMN_WIDTH = 60
# Minimum span of a ROW cell (the row-axis twin of MIN_COLUMN_WIDTH), and
# the floor every row band / grabber reads when a host hasn't measured
# its height yet.
MIN_ROW_HEIGHT = 40
EDGE_GRAB_WIDTH = 20.0
# Band color when the cursor is over the row (hard-coded for now).
HIGHLIGHT_TINT = (0,0,0, 1.0)

_NOTE = dict(name="draw_columns", tint=(0.5, 0.8, 1.0))


class Columns(dict):
    """Marker dict: values render side by side (draw_columns is its default
    renderer), so column layouts nest by data:
    Columns({"a": ..., "b": Columns({...})})."""


class Rows(dict):
    """Marker dict: values render stacked top to bottom (draw_rows is its
    default renderer) with draggable edges between them — the row-axis twin
    of Columns, and the two nest freely by data:
    Rows({"top": Columns({...}), "bottom": ...})."""


# ---------------------------------------------------------------------------
# Edge model
#
# An EDGE is a dict with a single float on its AXIS - {"x": 123.0} for a
# column edge, {"y": 123.0} for a row edge - in WINDOW coordinates (offset
# from window.abs_left / window.abs_top). Edges are passed around BY
# REFERENCE: a nested columns view does not create its own far edges, it
# adopts the two edge objects of its enclosing cell, so a shared boundary is
# always the same object in both views and can never drift apart.
#
# ALL edges live on the ROOT WINDOW's draw_state, one registry per axis:
# every columns view in that window upserts its edge list into
# window._edge_views (rows views into window._row_views), and drags from any
# view queue on window._pending_drags (window._pending_row_drags). Once per
# frame - triggered by the first layout view that renders - the window
# resolves each queue in one collision solve over the whole cells of that
# axis: Each registered edge list contributes its cells (consecutive
# pairs of its own edges, with each cell's floor / cap), so two edges only
# ever push or pull each other from the cell that spans between them. A
# nested layout shares its far edges with the enclosing cell by reference,
# so contact crosses nesting levels by needed; layouts that share a cell -
# the columns of two different rows, the rows of two different columns -
# are independent however close their lines come (the the sort-by-
# position version this replaced collided them, 08-26). Net motion of the
# window-direct row's far edges drives the window size itself (with a
# recenter so interior lines hold their screen positions); contributors are
# invalidated and every view lines its cells up with the same edges in
# that same frame.
#
# The two axes never interact: an x-edge only ever collides with x-edges.
# Every function below that walks edges takes ``axis`` ("x" default, "y"
# for rows) and reads the coordinate under that key.
# ---------------------------------------------------------------------------

# Per-axis registry attribute names on the window draw_state:
# (edge views, pending drags, frame edge pair, cell specs). Cell specs are
# keyed like the views: key → (floors, caps), one entry per cell in the
# view's list - a cell's floor / cap belong to the VIEW that owns the
# cell, never to the cell's the edge dict: two cells ending on the same
# edge (an outer column and the nested view's last column, the window
# frame and the last column) have their own.
_REGISTRY = {"x": ("_edge_views", "_pending_drags", "_frame_edges", "_edge_cells"),
             "y": ("_row_views", "_pending_row_drags", "_frame_rows", "_row_cells")}


def _axis_min(axis):
    """The flat minimum span between consecutive edges on ``axis``: a
    column's MIN_COLUMN_WIDTH along x, a row's MIN_ROW_HEIGHT along y."""
    return float(MIN_COLUMN_WIDTH if axis == "x" else MIN_ROW_HEIGHT)


def _views(window, axis):
    return getattr(window, _REGISTRY[axis][0])


def _pending(window, axis):
    return getattr(window, _REGISTRY[axis][1])


def _frame(window, axis):
    return getattr(window, _REGISTRY[axis][2], None)


def _specs(window, axis):
    return getattr(window, _REGISTRY[axis][3])


def _column_floor(column_mins, i):
    """Minimum width of column ``i``: its ``column_mins`` entry when given
    (None / 0 / a short list fall through), else MIN_COLUMN_WIDTH."""
    if column_mins and i < len(column_mins) and column_mins[i]:
        return float(column_mins[i])
    return float(MIN_COLUMN_WIDTH)


def _row_floor(row_mins, i):
    """Minimum height of row ``i`` — _column_floor's twin over
    MIN_ROW_HEIGHT."""
    if row_mins and i < len(row_mins) and row_mins[i]:
        return float(row_mins[i])
    return float(MIN_ROW_HEIGHT)


def _column_cap(column_maxes, i, floor):
    """Maximum width of column ``i``: its ``column_maxes`` entry when given
    (never below the column's ``floor`` — a cap under the minimum reads as
    the minimum), else None: unbounded."""
    if column_maxes and i < len(column_maxes) and column_maxes[i]:
        return max(float(column_maxes[i]), float(floor))
    return None


def resolve_column_widths(column_widths, n_cols, content_width,
                          column_mins=None, column_maxes=None, axis="x"):
    """Pixel width per column for ``n_cols`` columns in ``content_width``.

    Entries are pixels; None (or a missing entry — the list may be shorter
    than the column count) takes an equal share of whatever the sized columns
    leave over. Widths never drop below the column's minimum
    (``column_mins`` per column, else MIN_COLUMN_WIDTH) and never pass its
    cap (``column_maxes`` per column, None = unbounded): a flex column
    whose equal share would overshoot its cap takes the cap, and the
    remaining flex columns split what it left on the table.

    The same arithmetic sizes ROWS: ``axis="y"`` reads the row floors
    (MIN_ROW_HEIGHT) for entries ``column_mins`` doesn't cover.
    """
    available = max(0.0, float(content_width))
    spec = list(column_widths)[:n_cols] if column_widths else []
    spec += [None] * (n_cols - len(spec))
    floor_of = _column_floor if axis == "x" else _row_floor
    mins = [floor_of(column_mins, i) for i in range(n_cols)]
    maxes = [_column_cap(column_maxes, i, mins[i]) for i in range(n_cols)]

    def bounded(i, w):
        w = max(float(w), mins[i])
        return w if maxes[i] is None else min(w, maxes[i])

    widths = [None if w is None else bounded(i, w) for i, w in enumerate(spec)]
    remaining = available - sum(w for w in widths if w is not None)
    pool = [i for i, w in enumerate(widths) if w is None]
    while pool:
        # Equal share is floored at the largest min in the pool; any pool
        # column capped under that share takes its cap and drops out, the
        # rest re-split the remainder.
        share = max(max(mins[i] for i in pool), remaining / len(pool))
        capped = [i for i in pool if maxes[i] is not None and maxes[i] < share]
        if not capped:
            for i in pool:
                widths[i] = share
            break
        for i in capped:
            widths[i] = maxes[i]
            remaining -= maxes[i]
        pool = [i for i in pool if i not in capped]
    return widths


def _seed_edges(column_widths, n_cols, content_width, base=0.0,
                column_mins=None, column_maxes=None, axis="x"):
    """Fresh edge dicts for n_cols columns (rows with ``axis="y"``):
    n_cols+1 lines accumulated from ``base`` (window coordinates)."""
    widths = resolve_column_widths(column_widths, n_cols, content_width,
                                   column_mins=column_mins,
                                   column_maxes=column_maxes, axis=axis)
    edges = [{axis: float(base)}]
    for w in widths:
        edges.append({axis: edges[-1][axis] + w})
    return edges


def _edge_min(edges, m, axis="x"):
    """Minimum span between edges[m-1] and edges[m]: the "min" a layout
    stamped on the FAR edge of that column / row (column_mins / row_mins),
    else the axis's flat minimum. The flat solve sorts every edge by
    position and consecutive edges always bound exactly one cell of some
    view, so a per-edge floor slots straight into the contact physics."""
    return float(edges[m].get("min") or _axis_min(axis))


def _edge_max(edges, m):
    """Maximum span between edges[m-1] and edges[m]: the "max" a
    ColumnLayout stamped on the RIGHT edge of that column (column_maxes),
    else None — unbounded. _edge_min's counterpart for the PULL side of a
    drag: a column at its cap can't open any further, so its far edge is
    carried along instead (see _drag_edge)."""
    cap = edges[m].get("max")
    return float(cap) if cap else None


def _clamp_interior(edges, axis="x"):
    """Pack out-of-frame interior edges back inside the far edges at the
    axis's minimum spacing. Edges only ever move on drag CONTACT, so an
    interior edge that lands OUTSIDE its frame — edges persisted from a
    wider window, a width change that ran while the row wasn't registered
    — would otherwise stay there for good: it sorts past the frame edge in
    the flat solve (never pushed) and its grab handle sits off-window where
    no drag can reach it. In-frame, in-order edges are untouched.

    Then the caps: a column persisted WIDER than its maximum (a cap added
    since, a frame widened while the row wasn't registered) pulls its right
    edge in, the overflow rolling rightward through capped neighbours until
    an uncapped column absorbs it; the LAST column can't move the frame, so
    it pulls its LEFT edge out through the same chain a drag runs, walled
    at both frame edges — a frame wider than every cap put together leaves
    the last column over its cap (the frame is the authority; it never
    fights the width) and the pass is idempotent from then on."""
    count = len(edges)
    for m in range(count - 2, 0, -1):
        limit = edges[m + 1][axis] - _edge_min(edges, m + 1, axis)
        if edges[m][axis] > limit:
            edges[m][axis] = float(limit)
    for m in range(1, count - 1):
        floor = edges[m - 1][axis] + _edge_min(edges, m, axis)
        if edges[m][axis] < floor:
            edges[m][axis] = float(floor)
    for m in range(1, count - 1):
        cap = _edge_max(edges, m)
        if cap is not None and edges[m][axis] - edges[m - 1][axis] > cap:
            edges[m][axis] = edges[m - 1][axis] + cap
    cap = _edge_max(edges, count - 1) if count > 2 else None
    if cap is not None and edges[-1][axis] - edges[-2][axis] > cap:
        _drag_edge(edges, count - 2, edges[-1][axis] - cap,
                   walls=frozenset({id(edges[0]), id(edges[-1])}), axis=axis)


def _ensure_window_state(window):
    for views_attr, pending_attr, _frame_attr, specs_attr in _REGISTRY.values():
        if getattr(window, views_attr, None) is None:
            setattr(window, views_attr, {})
        if getattr(window, pending_attr, None) is None:
            setattr(window, pending_attr, [])
        if getattr(window, specs_attr, None) is None:
            setattr(window, specs_attr, {})


def _all_edges(window, axis="x"):
    """Every edge of ``axis`` registered on the window, deduped by identity
    (shared refs appear once)."""
    seen, flat = set(), []
    for _, edge_list in _views(window, axis).values():
        for e in edge_list:
            if id(e) not in seen:
                seen.add(id(e))
                flat.append(e)
    return flat


def _cells_from_lists(edge_lists, axis="x", specs=()):
    """The CELLS of every edge list: consecutive pairs (near, far) with the
    cell's floor and cap — from the list's per-view spec ``(floors, caps)``
    when one is given (``specs`` parallel to ``edge_lists``; a None floor
    is the axis minimum, a None cap unbounded), else from the "min" /
    "max" stamped on the far edge (_edge_min / _edge_max). A pair two
    views both register (a shared cell) merges to the strictest floor /
    cap. A cell is the ONLY thing that links two edges — which is what
    makes the columns of different rows independent: their dividers share
    no cell, just the frame edges."""
    cells = {}
    specs = list(specs) + [None] * (len(edge_lists) - len(specs))
    for edges, spec in zip(edge_lists, specs):
        for m in range(1, len(edges)):
            near, far = edges[m - 1], edges[m]
            if near is far:
                continue
            if spec is not None:
                floors, caps = spec
                floor = floors[m - 1] if m - 1 < len(floors) else None
                # None = the axis minimum; an explicit 0.0 is a GAP cell (the
                # OS level's "os_near, W),) contact at zero width
                floor = _axis_min(axis) if floor is None else float(floor)
                cap = caps[m - 1] if m - 1 < len(caps) else None
                cap = None if not cap else max(float(cap), floor)
            else:
                floor, cap = _edge_min(edges, m, axis), _edge_max(edges, m)
            key = (id(near), id(far))
            if key in cells:
                _near, _far, floor0, cap0 = cells[key]
                floor = max(floor, floor0)
                if cap is None:
                    cap = cap0
                elif cap0 is not None:
                    cap = min(cap, cap0)
            cells[key] = (near, far, floor, cap)
    return list(cells.values())


def _window_graph(window, axis, extra_lists=(), extra_specs=()):
    """The cell graph of every layout registered on the window for
    ``axis`` (per-view specs where the layouts stamped them), plus
    ``extra_lists`` / ``extra_specs`` — the OS level's cells a root
    window's pass adds for the solve only (os_frame.attach)."""
    views, specs = _views(window, axis), _specs(window, axis)
    keys = list(views)
    lists = [views[k][1] for k in keys] + list(extra_lists)
    spec_list = [specs.get(k) for k in keys] + list(extra_specs)
    return _EdgeGraph(_cells_from_lists(lists, axis, specs=spec_list))


class _EdgeGraph:
    """Adjacency over cells: ``ahead[id(near)]`` → ``[(far, floor, cap)]``,
    ``behind[id(far)]`` → ``[(near, floor, cap)]``, ``nodes`` id → edge."""

    def __init__(self, cells):
        self.ahead, self.behind, self.nodes = {}, {}, {}
        for near, far, floor, cap in cells:
            self.nodes[id(near)] = near
            self.nodes[id(far)] = far
            self.ahead.setdefault(id(near), []).append((far, floor, cap))
            self.behind.setdefault(id(far), []).append((near, floor, cap))

    def chain(self, start, goal, walls=frozenset(), forward=True,
              capped_only=False):
        """Weight of the binding chain of cells from ``start`` to ``goal``:
        the LONGEST sum of floors (a push chain — ``capped_only=False``) or
        the SHORTEST sum of caps over capped cells only (a pull chain —
        ``capped_only=True``), walking cells ahead (``forward``) or behind.
        None when no chain links them. Chains never pass through another
        wall: a wall never moves, so nothing propagates past it."""
        links = self.ahead if forward else self.behind
        pick = min if capped_only else max
        memo, on_stack = {}, set()

        def best(edge):
            edge_id = id(edge)
            if edge_id == id(goal):
                return 0.0
            if edge_id in walls or edge_id in on_stack:
                return None
            if edge_id in memo:
                return memo[edge_id]
            on_stack.add(edge_id)
            found = None
            for other, floor, cap in links.get(edge_id, ()):
                if capped_only:
                    if cap is None:
                        continue
                    weight = cap
                else:
                    weight = floor
                rest = best(other)
                if rest is None:
                    continue
                total = weight + rest
                found = total if found is None else pick(found, total)
            on_stack.discard(edge_id)
            memo[edge_id] = found
            return found

        return best(start)


def _solve_graph(graph, edge, target, walls=frozenset(), axis="x"):
    """Move ``edge`` to ``target`` through the cell graph. Edges are
    independent objects: no other edge moves unless the moving edge (or
    one it already carried) makes CONTACT through a cell — two kinds, one
    per side of the moving edge:

      PUSH, ahead: the cell in front closes to its floor (_edge_min) and
      its far edge is shoved on ahead.
      PULL, behind: the cell it leaves behind opens to its cap (_edge_max)
      and its far edge is dragged along behind.

    Either chain runs cell by cell — a pushed edge closes the next cell, a
    pulled edge opens the next — and stops at the first cell with slack;
    consecutive capped cells therefore travel as one, exactly as
    consecutive floor-packed cells do. A cell with no cap never pulls. An
    edge several cells share (a nested layout's far edge) carries every
    cell it bounds.

    ``walls`` is a set of edge ids the cascade must NOT move. Contact stops
    dead at a wall: the *dragged* edge itself is clamped so the pile packs
    against the wall at its floors (push side) or stretches to its summed
    caps (pull side) instead of the chain shoving the wall along. Used by
    _solve_collisions to keep one FRAME edge from moving the other (breaks
    the foreign-width feedback loop — see there; its mirror image is a
    fully-capped row, which refuses a foreign widening the same way);
    interior divider drags pass no walls, so a divider can still push or
    pull a frame edge and slide/grow/shrink the window 1:1 with the
    cursor. Returns True if anything moved."""
    old = edge[axis]
    if target == old or id(edge) not in graph.nodes:
        return False
    forward = target > old
    # Wall clamps first: ahead through the floors, behind through the caps
    # (a pull chain can only reach a wall over capped cells). The
    # binding chain per wall is the longest floor chain / shortest cap
    # chain - the one that would move the wall first.
    for wall_id in walls:
        wall = graph.nodes.get(wall_id)
        if wall is None or wall is edge:
            continue
        push = graph.chain(edge, wall, walls, forward=forward)
        if push is not None:
            target = (min(target, wall[axis] - push) if forward
                      else max(target, wall[axis] + push))
        pull = graph.chain(edge, wall, walls, forward=not forward,
                           capped_only=True)
        if pull is not None:
            target = (min(target, wall[axis] + pull) if forward
                      else max(target, wall[axis] - pull))
    if target == old:
        return False
    edge[axis] = float(target)
    # Propagate contact. Every relaxation moves an edge the way the drag
    # went and never back, so this is a monotone worklist that settles on
    # its own; the guard only ever trips on a cyclic (corrupt) cell graph.
    pending = [edge]
    guard = 64 * (len(graph.nodes) + 1)
    while pending and guard > 0:
        guard -= 1
        current = pending.pop()
        current_id = id(current)
        if forward:
            for far, floor, _cap in graph.ahead.get(current_id, ()):    # push ahead
                if id(far) in walls:
                    continue
                need = current[axis] + floor
                if far[axis] < need:
                    far[axis] = need
                    pending.append(far)
            for near, _floor, cap in graph.behind.get(current_id, ()):  # pull behind
                if cap is None or id(near) in walls:
                    continue
                need = current[axis] - cap
                if near[axis] < need:
                    near[axis] = need
                    pending.append(near)
        else:
            for near, floor, _cap in graph.behind.get(current_id, ()):  # push ahead
                if id(near) in walls:
                    continue
                need = current[axis] - floor
                if near[axis] > need:
                    near[axis] = need
                    pending.append(near)
            for far, _floor, cap in graph.ahead.get(current_id, ()):    # pull behind
                if cap is None or id(far) in walls:
                    continue
                need = current[axis] + cap
                if far[axis] > need:
                    far[axis] = need
                    pending.append(far)
    return True


def _drag_edge(edges, k, target, walls=frozenset(), axis="x"):
    """Move edge k of ONE ordered edge list to ``target`` — the single-list
    case of _solve_graph (consecutive edges of one list are its cells);
    _clamp_interior's cap repair runs through here."""
    graph = _EdgeGraph(_cells_from_lists([edges], axis))
    _solve_graph(graph, edges[k], target, walls=walls, axis=axis)


def _solve_collisions(window, axis="x", os_ctx=None):
    """Apply every queued drag of ``axis`` against the FULL cell graph of
    that axis — every registered edge list's cells at once — so contact
    chains cross view boundaries through shared edges and nothing else.
    Returns True if anything moved. (No standing repair pass: edges only
    move while a drag is applied.)

    ``os_ctx`` (os_frame.attach, root windows only) adds the OS level to
    the graph: the OS window's frame pair, the screen walls, and two
    zero-floor gap cells linking this window's frame to the OS frame — so
    a cursor-driven drag pushed through the frame moves the OS edge, and
    the OS edge pushed into the screen is clamped like any wall. Its own
    drags (the OS window's frame edges, foreign motion since this window
    last saw them) ride along in the same solve. Only the HAND moves the
    OS window: a foreign size write of this window (its content grew)
    sees the OS edges as walls.

    THE FLIP: a cursor-driven drag whose owner is blocked by a wall grows
    that window on the OPPOSITE side by the remainder — the rule melty
    windows always had against the display ("pin the bottom, let the top
    rise"), now for the OS window too, and the only way a near edge ever
    moves outward on its own: W's right edge blocked at the screen → W's
    left edge moves left → pushes the OS left edge → the OS window grows
    left (and moves) → the screen's left edge stops it."""
    from src.lsd.gl_gui import os_frame
    pending_attr = _REGISTRY[axis][1]
    pending = getattr(window, pending_attr)
    setattr(window, pending_attr, [])
    os_items = list(os_ctx.drags) if os_ctx is not None else []
    if not pending and not os_items and not (os_ctx is not None and os_ctx.move):
        return False
    fe = _frame(window, axis) or ()
    local_graph = _window_graph(window, axis)
    if os_ctx is not None and len(fe) == 2:
        gap_lists, gap_specs = os_frame.gap_lists(os_ctx, fe[0], fe[1])
        os_graph = _window_graph(window, axis, extra_lists=list(os_ctx.lists) + gap_lists,
                                 extra_specs=list(os_ctx.specs) + gap_specs)
        base_walls, os_ids = os_ctx.walls, os_ctx.os_ids
        os_pair = tuple(os_frame.edges(axis))
    else:
        os_graph, base_walls, os_ids, os_pair = local_graph, frozenset(), frozenset(), None
    # A FRAME edge drag must never shove the OTHER frame edge. The danger
    # case is the foreign-width invariant drag (window_edge_pass queues
    # right→window.width): when an outside writer re-stamps width below the
    # fully-compressed pile span every frame, that drag pushes clean through
    # the pile into the left frame edge, the rebase dumps the mismat
    # into window_pos, width springs back, and the loop re-fires each frame
    # - the window flies off screen with no end limit. Walling the
    # opposite frame edge clamps the drag at the pile span instead, so the
    # mismatch resolves by width snapping back (stable), never by sliding.
    # INTERIOR divider drags keep an empty wall set: pushing the window's
    # left edge with a divider (slide or grow) is normal normal and only
    # ever moves 1:1 with the cursor.
    frame_ids = {id(e) for e in fe}
    # ``moved`` = THIS window's frame moved. An OS edge moving on its own
    # (the OS window's frame, a foreign move every root folds in) is not a
    # change of this window - and reporting it as one invalidated every
    # view of every root on every frame of an OS-window gesture (08-27:
    # 120 → 65 fps on the corner, 12 fps on a move). The OS level reaches
    # this window's interior only THROUGH its frame pair, so the frame
    # pair before/after tells.
    frame_before = tuple(e[axis] for e in fe)
    moved = False
    if os_ctx is not None and os_ctx.move and os_pair is not None and len(fe) == 2:
        # A HAND MOVE of this window (its position already applied by the
        # wrapper's move drag): the OS edge its frame now overlaps is
        # pushed out to it — up to the screen, where the OS edge stops
        # and the window keeps moving (a move is not clamped: windows
        # may be dragged partly off the display, Lukas 08-27 - not
        # above the screen limit, clamped after the solve in _frame_pass,
        # Toggles.Melty.window_top_hard_limit). The push is
        # the OS edge pushed to the window's edge with the screen as the
        # wall; the window's own edges are not part of it.
        os_near, os_far = os_pair
        if os_far[axis] < fe[1][axis]:
            _solve_graph(os_graph, os_far, fe[1][axis], walls=base_walls, axis=axis)
        if os_near[axis] > fe[0][axis]:
            _solve_graph(os_graph, os_near, fe[0][axis], walls=base_walls, axis=axis)
    for item in os_items + pending:
        # Optional third slot marks a CURSOR-DRIVEN drag (the right-drag
        # corner resize queues frame edges around it): those move 1:1 with the
        # cursor, so the foreign-width feedback loop the walls guard against
        # can't occur - a cursor drag on one frame edge is allowed to push
        # the other (slide the window), just like an interior divider.
        edge, target = item[0], item[1]
        cursor_driven = len(item) > 2 and bool(item[2])
        is_os = os_pair is not None and (edge is os_pair[0] or edge is os_pair[1])
        # Only the HAND moves the OS window: a foreign size write of this
        # window (its content grew) solves in its own graph, so it
        # overflows the screen window as intended instead of being
        # clamped by it or growing the studio.
        graph = os_graph if (cursor_driven or is_os) else local_graph
        walls = set(base_walls)                      # the screen never moves
        if not cursor_driven:
            if is_os:
                walls |= os_ids - {id(edge)}         # a foreign OS edge IS where it is; the other holds
            elif id(edge) in frame_ids:
                walls |= frame_ids - {id(edge)}
        walls = frozenset(walls)
        if cursor_driven and id(edge) in frame_ids:
            # The cap is a STOP, not a slide: clamp the cursor's target at
            # max_height BEFORE the solve. Solved at the raw target and
            # pulled back by _hold_frame_max after, the residual flipped
            # onto the opposite edge (the min hold's slide) - the window
            # kept moving while its height stayed capped (Lukas 09-04).
            target = _cap_frame_target(window, edge, target, axis)
        if _solve_graph(graph, edge, target, walls=walls, axis=axis) and not is_os:
            moved = True
        if is_os and not cursor_driven:
            # A foreign OS edge IS where it is: the pile packed against it
            # as much as it could (the wall clamp), the rest overflows.
            # Forced HERE, before this window's own drags run - forced
            # after the solve (detach) it ate away the push those drags
            # gave the same edge, so the OS edge advanced only every other
            # frame, with a double step (one sub-pixel the begin_frame gives
            # the far edge when a drag lands on every frame "moves").
            edge[axis] = float(target)
        if not cursor_driven:
            continue
        if id(edge) in frame_ids:
            _hold_frame_min(window, edge, axis)
            _hold_frame_max(window, edge, axis)
        residual = target - edge[axis]
        if abs(residual) <= 1e-6:
            continue
        pair = os_pair if is_os else (fe if len(fe) == 2 else None)
        if pair is None:
            continue
        opposite = pair[0] if residual > 0 else pair[1]
        if opposite is edge:                         # dragged inward, blocked: nothing to flip
            continue
        _solve_graph(graph, opposite, opposite[axis] - residual, walls=walls, axis=axis)
    if tuple(e[axis] for e in fe) != frame_before:
        moved = True
    return moved


def _hold_frame_min(window, dragged, axis="x"):
    """A cursor-driven FRAME edge dragged past the window's min_width
    (min_height on the row axis) pushes the OTHER frame edge along — the
    window slides (right edge dragged left) or grows (left edge dragged
    right) — instead of stopping short of the cursor. Without this the
    solve left the frame pair narrower than min_width, the wrapper's
    `width = max(width, min_width)` re-stamp (after the pass) widened it
    again, and the next pass's foreign-width invariant dragged the cursor
    edge BACK every frame: the right edge yanked between the cursor and
    min_width while the left edge kept sliding, width ratcheting up
    mid-drag. Interior edges need no cascade: the pushed edge only ever
    moves AWAY from them."""
    fe = _frame(window, axis)
    if not fe:
        return
    near, far = fe
    floor = float((window.min_width if axis == "x" else window.min_height) or 0)
    if far[axis] - near[axis] >= floor:
        return
    if dragged is far:
        near[axis] = far[axis] - floor
    else:
        far[axis] = near[axis] + floor


def _cap_frame_target(window, dragged, target, axis="x"):
    """`target` for a cursor-driven FRAME edge, held within the window's
    max_height (row axis only): the far edge no farther than near + cap,
    the near edge no nearer than far − cap. Other axes / no cap: as is."""
    if axis != "y":
        return target
    cap = getattr(window, "max_height", None)
    if not cap:
        return target
    fe = _frame(window, axis)
    if not fe:
        return target
    near, far = fe
    if dragged is far:
        return min(target, near[axis] + float(cap))
    if dragged is near:
        return max(target, far[axis] - float(cap))
    return target


def _hold_frame_max(window, dragged, axis="x"):
    """The mirror of _hold_frame_min for the window's max_height (the row
    axis only — there is no max_width): a cursor-driven FRAME edge dragged
    past the cap STOPS at it — the dragged edge is pulled back to
    cap-distance from the other, which never moves (the usage picker's
    content-height ceiling, enforced mid-drag, Lukas 09-04)."""
    if axis != "y":
        return
    cap = getattr(window, "max_height", None)   # test stand-ins lack the slot
    if not cap:
        return
    fe = _frame(window, axis)
    if not fe:
        return
    near, far = fe
    if far[axis] - near[axis] <= cap:
        return
    if dragged is far:
        far[axis] = near[axis] + float(cap)
    else:
        near[axis] = far[axis] - float(cap)


def _drag_live():
    """Mirror of the frozen-blit gate's activity test in blit_offscreen: a
    mouse drag (any button) is in flight. Programmatic edge moves (foreign
    width writes) fall outside it, so they still invalidate normally."""
    from src.lsd.gl_gui.melty import Melty
    return (imgui.is_mouse_down(0) or imgui.is_mouse_down(1)
            or imgui.is_mouse_down(2) or Melty.on_drag)


def _defer_freeze_settle(window, draw_state):
    """Record a freeze_resize view whose per-frame edge invalidate was
    skipped mid-drag; window_edge_pass settles it once on release."""
    pending = getattr(window, "_freeze_settle", None)
    if pending is None:
        pending = window._freeze_settle = {}
    pending[id(draw_state)] = draw_state


def release_row(draw_state):
    """Drop this host's registered edge rows (both axes). Hosts that
    sometimes render WITHOUT columns (the code editor leaving a compare
    split) must call this on their column-less frames: window_edge_pass
    only evicts rows whose ds is CLOSED, and a host whose ds IS its window
    never closes while open — the stale row keeps feeding edge_under_cursor
    and the frame-edge solve, so right-drag resizes latch an invisible
    stale divider and the window width freezes. ColumnLayout / RowLayout
    re-register on construction, so releasing before building them later
    in the same frame is safe."""
    window = draw_state.parent_window or draw_state
    for axis, key in (("x", ("row", draw_state.id)), ("y", ("rows", draw_state.id))):
        views = getattr(window, _REGISTRY[axis][0], None)
        if views is not None:
            views.pop(key, None)
        specs = getattr(window, _REGISTRY[axis][3], None)
        if specs is not None:
            specs.pop(key, None)
    draw_state._column_container = False
    draw_state._row_container = False


def _has_layout_ancestor(draw_state, flag):
    """True when another layout container carrying ``flag``
    (``_column_container`` / ``_row_container``) sits between this view
    and its window. Used to decide window-frame adoption: a row with NO
    such ancestor is the window's row — however many plain wrapper views
    (window bodies, code_file_io, …) sit in between — and adopts the
    window's frame edges on that axis; a row under another container of
    its axis gets its far edges from the enclosing cell instead (passed
    refs, or its own as the deep-nesting fallback). The climb is scoped to
    the parent window and guards the root ds's self-parent loop. A Rows
    host is transparent to a Columns view and vice versa: the axes don't
    share edges."""
    window = draw_state.parent_window
    node = draw_state._parent
    while node is not None and node is not node._parent:
        if node is window:
            break
        if getattr(node, flag, False):
            return True
        node = node._parent
    return False


def _has_columns_ancestor(draw_state):
    return _has_layout_ancestor(draw_state, "_column_container")


def _has_rows_ancestor(draw_state):
    return _has_layout_ancestor(draw_state, "_row_container")


def window_edge_pass(window):
    """Every window's FRAME edges — left/right on the x axis, top/bottom on
    the y axis — as draggable edge objects, run once per frame per window
    (idempotent; called from core_render's melty-window path for ALL
    windows, and from the layouts as a fallback).

    Per axis the window owns two edge dicts seeded at [0, size] (window
    coords) and registered in the same solve as every column / row edge.
    Native resizes (corner drag, right-click draw, programmatic size
    writes) are folded in through the invariant far.pos == window.size:
    any foreign size change queues a drag of the far frame edge, so it runs
    the SAME collision solve and frame and lines can never desync. After
    the solve the window lines up with its edges exactly like cells do:
    near edge off 0 → window_pos slides + everything re-bases; far edge off
    the size → the size follows."""
    from src.lsd.gl_gui.melty import Melty
    if getattr(window, "_edges_frame", None) == Melty.frame_count:
        return
    window._edges_frame = Melty.frame_count

    if not getattr(window, "expanded", True) or not window.width or not window.height:
        return
    _ensure_window_state(window)

    # freeze_resize views whose per-frame edge invalidates were skipped
    # mid-drag (see the edge-solve loop below) settle with ONE invalidate on
    # release - box-unchanged views have no size mismatch to trigger the
    # normal settled resize recapture, so without it they'd blit stale
    # forever.
    pending_settle = getattr(window, "_freeze_settle", None)
    if pending_settle and not _drag_live():
        for ds in pending_settle.values():
            if not getattr(ds, "closed", False) and not ds.size_change:
                ds.invalidate(note=Note(reason="freeze settle", **_NOTE))
        window._freeze_settle = {}
        request_render()

    moved = _frame_pass(window, "x")
    moved = _frame_pass(window, "y") or moved

    if moved:
        seen = set()
        for axis in _REGISTRY:
            for ds, _ in _views(window, axis).values():
                if id(ds) in seen:
                    continue
                seen.add(id(ds))
                # A view whose box already disagrees with its tile
                # (size_change) is live-rendered by blit_offscreen as-is -
                # cached image and cached masks are both refused mid-resize,
                # and the tile is rebuilt once on mouse release.
                # Invalidating it here would only queue per-frame tile
                # copies at the wrong size. Only views whose own box is
                # UNCHANGED while their edges move (interior layout drags)
                # still need the explicit invalidate or they blit stale.
                # freeze_resize views are the exception BOTH ways: mid-drag
                # they WANT to blit stale (that's the freeze contract -
                # invalidating would force a live body re-render every drag
                # frame, since a box-unchanged view never hits the size
                # size-mismatch gate), so their invalidate is deferred to the
                # release settle above.
                if not ds.size_change:
                    if getattr(ds, "freeze_resize", False) and _drag_live():
                        _defer_freeze_settle(window, ds)
                    else:
                        ds.invalidate(note=Note(reason="edge solve", **_NOTE))


def _hand_moved(window, frame):
    """Was ``window`` moved by hand this frame — itself (the move drag
    stamps ``_hand_move_frame``) or through an ANCESTOR it rides with (a
    nested window's window_pos is parent-relative: dragging the parent
    moves the child on screen, and the child's frame must push the OS
    edge it reaches exactly as the parent's does)."""
    node, depth = window, 0
    while node is not None and depth < 64:
        if getattr(node, "_hand_move_frame", None) == frame:
            return True
        # An ANCESTOR resized by hand this frame (_frame_pass stamps
        # _hand_resize_frame) moves the cell the child hangs from, so the
        # child rides exactly as under a hand move and must collide with
        # the OS edges the same way. The window's OWN resize is not a move
        # of it (its frame drags solve in its pass already).
        if node is not window and getattr(node, "_hand_resize_frame", None) == frame:
            return True
        node = getattr(node, "parent_window", None)
        depth += 1
    return False


def _frame_pass(window, axis):
    """One axis of window_edge_pass: seed / register the frame pair, fold
    the foreign size change in, floor the window's minimum at the pile,
    offer the frame drag handles, solve, and line the window up with its
    frame edges. Returns True when any edge of the axis moved."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.toggles import Toggles
    frame_attr = _REGISTRY[axis][2]
    size = window.width if axis == "x" else window.height
    fe = getattr(window, frame_attr, None)
    if not fe:
        fe = [{axis: 0.0}, {axis: float(size)}]
        setattr(window, frame_attr, fe)
    views, specs = _views(window, axis), _specs(window, axis)
    views[window.id] = (window, fe)
    # The frame pair is a cell too (it keeps the window's own span in the
    # graph): floored at the window's minimum (never below the flat floor)
    # and uncapped - the stamps on the far edge belong to the LAST column /
    # row, never to the frame. The floor is what an OS edge pushing the
    # window meets (os_frame): the window compresses to it, then slides.
    declared = float((window.min_width if axis == "x" else window.min_height) or 0)
    specs[window.id] = ([max(_axis_min(axis), declared)], [None])
    near, far = fe

    for key, (ds, _) in list(views.items()):
        if ds is not window and getattr(ds, "closed", False):
            del views[key]
            specs.pop(key, None)

    # Foreign size change since last frame → the far frame edge follows it
    # THROUGH the collision solve. Third slot None = NOT a cursor drag (the
    # OS-edge hooks below skip it; _solve_collisions keeps its position).
    if abs(far[axis] - size) > 0.5:
        _pending(window, axis).append((far, float(size), None))

    # The frame can never out-compress the window: keep min_width /
    # min_height at the fully-compressed span so a pending shrink always
    # triggers its collision pass instead of fighting the resize latch.
    # Raise-only, re-stamped every frame (the wrapper rewrites the minimum
    # from resolved kwargs each frame).
    # The compressed span is the LONGEST chain of cell floors from the near
    # frame edge to the far one - NOT the sum over every edge, which
    # double-counts layouts stacked in different rows.
    graph = _window_graph(window, axis)
    span = graph.chain(near, far)
    need = snap_int(near[axis] + max(_axis_min(axis), span or 0.0))
    if axis == "x":
        if (window.min_width or 0) < need:
            window.min_width = need
    elif (window.min_height or 0) < need:
        window.min_height = need

    # Frame edge drag handles: full window height for the left/right pair,
    # full window width for the top/bottom pair. The band straddles the
    # edge, so the top handle's inner rect covers the header's top rows -
    # the buttons sit above it (flat_button priority_delta 4), the
    # window move sits below (priority_delta 0 / -2).
    win_x, win_y = window.abs_left, window.abs_top
    seen_handles = set()
    for k, e in enumerate(fe):
        if axis == "x":
            x = win_x + e["x"]
            rect = (x - EDGE_GRAB_WIDTH / 2, win_y,
                    x + EDGE_GRAB_WIDTH / 2, win_y + window.height)
            cursor, total = mouse_cursor.RESIZE_EW, "total_dx"
        else:
            y = win_y + e["y"]
            rect = (win_x, y - EDGE_GRAB_WIDTH / 2,
                    win_x + window.width, y + EDGE_GRAB_WIDTH / 2)
            cursor, total = mouse_cursor.RESIZE_NS, "total_dy"
        view_id = f"win_edge_{axis}_{k}"
        drag = window.on_action("left_mouse_drag", view_id=view_id,
                                rect=rect, priority_delta=1, cursor=cursor)
        press = window.on_action("left_mouse_down", view_id=view_id,
                                 rect=rect, priority_delta=1)
        if press:
            # Resize press, before any edge motion: freeze views snap their
            # clean pre-drag capture this frame (mark_start_offscreen).
            Melty.resize_press_frame = Melty.frame_count
        if not drag:
            continue
        handle = f"win_{axis}_{k}"
        seen_handles.add(handle)
        inc = _drag_inc(window, handle, drag, total=total)
        if inc:
            _pending(window, axis).append((e, e[axis] + inc))
    totals = getattr(window, "_drag_totals", None)
    if totals:
        # Prune ONLY this axis's "win_<axis>_*" namespace: when a layout
        # host ds IS the window (code editor compare split), its divider
        # handles live in this same dict - deleting them here resets the
        # divider's drag baseline every frame, so each frame re-applies
        # the FULL total and the edge flings away from the cursor. The other
        # axis's frame handles are pruned by its own pass.
        prefix = f"win_{axis}_"
        for h in [h for h in totals
                  if isinstance(h, str) and h.startswith(prefix)
                  and h not in seen_handles]:
            del totals[h]

    # ---- The OS level. A root window's frame pair collides with the OS
    # window's pair (os_frame: zero-floor gap cells link them, the screen
    # is the wall outside) - attach shifts the OS and screen dicts into
    # THIS window's coordinates for the solve (and back in detach), so the
    # window's own edges are never written unless the solve moves them; an
    # idle frame moves nothing. Nested windows solve only their own frame.
    from src.lsd.gl_gui import os_frame
    # a cursor-driven drag of THIS window's frame (handle / corner right-drag;
    # a foreign size write queues None): a hand resize, stamped for the
    # nested windows that hang off the moved corner (_hand_moved)
    hand_resize = any(len(item) < 3 or bool(item[2]) for item in _pending(window, axis))
    os_ctx = os_frame.attach(window, axis, has_pending=bool(_pending(window, axis)),
                             hand_move=_hand_moved(window, Melty.frame_count))
    moved = _solve_collisions(window, axis, os_ctx)
    if hand_resize and moved:
        window._hand_resize_frame = Melty.frame_count

    if os_ctx is not None:
        os_frame.detach(window, axis, os_ctx)    # books the OS near edge's motion for apply_rebase

    # The display's TOP is a hard limit for a hand move
    # (Toggles.Melty.window_top_hard_limit): the solve above took the OS edge
    # as far as the screen lets it, the remainder - the window's top still
    # above the display top - is clamped by sliding the window back down.
    # Written to window_pos directly (a near-edge shift through the frame
    # pair would be a RESIZE: interior edges hold their screen position),
    # so it is a pure move; the press baseline is untouched, the window
    # re-tracks the hand if the pointer is moved. Nested windows too, pinned
    # or not (Lukas 08-28): window_pos is an additive offset on top of the
    # parent / OS anchor, so the shift holds - the child slides down inside
    # the parent, the parent is never moved for it.
    if (axis == "y" and Toggles.Melty.window_top_hard_limit
            and _hand_moved(window, Melty.frame_count)):
        limit = os_frame.display_top()
        top = float(window.abs_top or 0)
        if top < limit - 1e-6:
            pos = window.window_pos or (0, 0)
            window.window_pos = (pos[0], pos[1] + (limit - top))

    # Line the WINDOW up with its frame edges - the same rule cells follow:
    # near edge off 0 → window_pos slides and every edge re-bases so
    # interior lines hold their screen position; far edge off the size →
    # the size snaps. (The OS near edge's own motion re-bases every root
    # when the surface actually moves - os_frame.apply_rebase, next frame
    # below - not here.)
    d_near = near[axis]
    if d_near:
        pos = window.window_pos or (0, 0)
        window.window_pos = ((pos[0] + d_near, pos[1]) if axis == "x"
                             else (pos[0], pos[1] + d_near))
        if axis == "x":
            window.width = snap_int(window.width - d_near)
        else:
            window.height = snap_int(window.height - d_near)
        for e in _all_edges(window, axis):
            e[axis] -= d_near
        moved = True
    size = window.width if axis == "x" else window.height
    if abs(far[axis] - size) > 0.5:
        size = snap_int(far[axis])
        if axis == "x":
            window.width = size
        else:
            window.height = size
        moved = True
    # Kill snap drift so the invariant check doesn't re-fire every frame.
    far[axis] = float(size)
    return moved


def reframe_window(window, d_left, d_right, extra_edges=()):
    """Move the window's LEFT frame edge by ``d_left`` and its RIGHT frame
    edge by ``d_right`` (screen px, negative = leftward) mid-body, keeping
    every registered interior edge at its SCREEN position — the same
    "line the window up with its frame edges" rebase window_edge_pass does
    post-solve, callable by a body that must change the frame itself (the
    code editor growing left to keep its main pane in place when a compare
    split opens). ``extra_edges`` are edge dicts not currently registered
    (a row released at the top of the body and re-registered later) that
    must ride along too. Frame edges end up at [0, width] so the next pass
    sees no foreign change and re-solves nothing. Registered views are
    invalidated like an edge-solve move (freeze_resize views included:
    this is a one-shot layout change, not a drag)."""
    if not d_left and not d_right:
        return
    _ensure_window_state(window)
    fe = getattr(window, "_frame_edges", None)
    if not fe:
        fe = [{"x": 0.0}, {"x": float(window.width or 0)}]
        window._frame_edges = fe
    pos = window.window_pos or (0, 0)
    window.window_pos = (pos[0] + d_left, pos[1])
    window.width = snap_int((window.width or 0) - d_left + d_right)
    if d_left:
        seen = set()
        for e in list(_all_edges(window)) + list(extra_edges):
            if e is fe[0] or e is fe[1] or id(e) in seen:
                continue
            seen.add(id(e))
            e["x"] -= d_left
    fe[0]["x"] = 0.0
    fe[1]["x"] = float(window.width)
    for ds, _ in window._edge_views.values():
        if not ds.size_change:
            ds.invalidate(note=Note(reason="reframe window", **_NOTE))
    request_render()


def reframe_axis(window, axis, d_near):
    """Move the window's NEAR frame edge (left for "x", top for "y") by
    ``d_near`` px (negative = outward) keeping every registered interior
    edge at its SCREEN position — reframe_window's rule on either axis
    with the far edge fixed. The glue of the OS-edge handoff (os_frame):
    the compositor moved the studio's left/top edge, every root window was
    re-based by the same amount, and the glued window's near edge follows
    the OS edge back out while its interior stays put."""
    if not d_near:
        return
    _ensure_window_state(window)
    frame_attr = _REGISTRY[axis][2]
    size = window.width if axis == "x" else window.height
    fe = getattr(window, frame_attr, None)
    if not fe:
        fe = [{axis: 0.0}, {axis: float(size or 0)}]
        setattr(window, frame_attr, fe)
    pos = window.window_pos or (0, 0)
    if axis == "x":
        window.window_pos = (pos[0] + d_near, pos[1])
        window.width = snap_int((window.width or 0) - d_near)
        size = window.width
    else:
        window.window_pos = (pos[0], pos[1] + d_near)
        window.height = snap_int((window.height or 0) - d_near)
        size = window.height
    seen = set()
    for e in _all_edges(window, axis):
        if e is fe[0] or e is fe[1] or id(e) in seen:
            continue
        seen.add(id(e))
        e[axis] -= d_near
    fe[0][axis] = 0.0
    fe[1][axis] = float(size)
    for ds, _ in _views(window, axis).values():
        if not ds.size_change:
            ds.invalidate(note=Note(reason="reframe axis", **_NOTE))
    request_render()


def _across_band(ds, axis):
    """The span a view's edges of ``axis`` cover on the OTHER axis (absolute
    screen coords): its visible clip when it has one, else its box from the
    last measured band extent. An x-edge row spans a y band; a y-edge
    column of rows spans an x band."""
    clip = getattr(ds, "abs_clip_rect", None)
    if axis == "x":
        if clip:
            return clip[1], clip[3]
        top = ds.abs_top
        return top, top + max(getattr(ds, "_edge_lines_height", 0.0),
                              MIN_ROW_HEIGHT)
    if clip:
        return clip[0], clip[2]
    left = ds.abs_left
    return left, left + max(getattr(ds, "_edge_lines_width", 0.0),
                            MIN_COLUMN_WIDTH)


def _edge_under_cursor(window, axis, along, across_abs, before=False):
    """The edge of ``axis`` a right-drag resize should move: the nearest
    edge strictly AHEAD of the cursor (``along``, window coords on the
    axis) — or strictly BEHIND it with ``before=True`` — among the views
    whose band on the other axis contains ``across_abs`` (absolute), i.e.
    the far edge of the INNERMOST cell under the cursor. Falls back to the
    window's frame edge on that side (every window registers its pair over
    the full window) — None only if no edges exist yet."""
    _ensure_window_state(window)
    best, best_pos = None, None
    for ds, edge_list in _views(window, axis).values():
        lo, hi = _across_band(ds, axis)
        if not (lo - 1 <= across_abs <= hi + 1):
            continue
        for e in edge_list:
            pos = e[axis]
            if before:
                if pos < along - 0.5 and (best_pos is None or pos > best_pos):
                    best, best_pos = e, pos
            elif pos > along + 0.5 and (best_pos is None or pos < best_pos):
                best, best_pos = e, pos
    if best is None:
        fe = _frame(window, axis)
        if fe:
            best = fe[0] if before else fe[1]
    return best


def edge_under_cursor(window, cursor_x_window, cursor_y_abs, left=False):
    """The column edge a right-drag resize should move, given the drag-start
    cursor (``cursor_x_window`` in window coords — offset from window.abs_left
    — and ``cursor_y_abs`` in absolute screen coords).

    Returns the nearest edge dict strictly to the RIGHT of the cursor among
    the rows whose visible band vertically contains the cursor — i.e. the
    right edge of the INNERMOST column under the cursor. With ``left=True``
    (the left+right-drag top-left corner resize) it's the nearest edge
    strictly to the LEFT instead. Every window registers its own frame edges
    (``window.id`` entry, full-window span), so a window with NO columns — or
    a drag in the outermost column — lands on the window's frame edge on
    that side and the frame resizes exactly as a plain resize.
    Returns None only if no edges exist yet."""
    return _edge_under_cursor(window, "x", cursor_x_window, cursor_y_abs,
                              before=left)


def row_edge_under_cursor(window, cursor_y_window, cursor_x_abs, above=False):
    """edge_under_cursor's row-axis twin: the nearest row edge strictly
    BELOW the drag-start cursor (``cursor_y_window`` in window coords —
    offset from window.abs_top; ``cursor_x_abs`` absolute) among the rows
    views whose band horizontally contains it — the bottom edge of the
    INNERMOST row under the cursor; ``above=True`` (top-left corner mode)
    takes the nearest edge strictly ABOVE. A window with no rows — or a
    drag in the outermost row — lands on the window's top/bottom frame
    edge, so the frame resizes exactly as a plain resize."""
    return _edge_under_cursor(window, "y", cursor_y_window, cursor_x_abs,
                              before=above)


def _grab_zone(edges, k, axis="x"):
    """Grab span for edge k of this view's list along its axis:
    EDGE_GRAB_WIDTH centered on the line, but split at the midpoint toward
    each neighbouring edge so adjacent zones never overlap."""
    pos = edges[k][axis]
    lo, hi = pos - EDGE_GRAB_WIDTH / 2, pos + EDGE_GRAB_WIDTH / 2
    if k > 0:
        lo = max(lo, (edges[k - 1][axis] + pos) / 2)
    if k < len(edges) - 1:
        hi = min(hi, (pos + edges[k + 1][axis]) / 2)
    return lo, hi


def _drag_inc(draw_state, handle, drag, total="total_dx"):
    """Per-frame pixel delta for a drag handle, from the drag's running
    total (``total_dx``, or ``total_dy`` for a row edge) against the last
    seen total. State is PER-HANDLE (dict), never a shared slot: with a
    shared slot, two events alive in the same frame alternate ownership and
    each re-fires its full total against a zero baseline — moving edges
    that were never dragged. Entries are pruned by the caller when their
    handle has no event, so a new gesture always starts from a clean
    baseline."""
    totals = getattr(draw_state, "_drag_totals", None)
    if totals is None:
        totals = draw_state._drag_totals = {}
    last = totals.get(handle, 0.0)
    now = getattr(drag, total)
    totals[handle] = now
    return now - last


class ColumnLayout:
    """Manual-cell access to the shared edge system, for renderers that draw
    their own cells instead of routing values through draw_columns (e.g. the
    code editor's structured|text tabs).

        cols = ColumnLayout(draw_state, n_cols, column_edges=column_edges,
                            column_widths=column_widths)
        for idx in range(n_cols):
            with cols.cell(idx) as width:
                any_renderer(..., width=width)
        cols.finish()

    Declare ``column_edges=None`` as a named param on the host render_func
    and pass its resolved value through — ColumnLayout stamps
    draw_state.column_edges on seed/adoption changes, so auto-state persists
    the edge dicts exactly like draw_columns. Edge ownership, registration
    on the root window, drag handles and the collision solve are the same
    machinery; only the cell CONTENT is the caller's."""

    def __init__(self, draw_state, n_cols, column_edges=None,
                 column_widths=None, left_edge=None, right_edge=None,
                 resizable=True, padding=6.0, border_color=(0.0, 0.0, 0.0, 0.9),
                 padding_y=None, column_mins=None, column_maxes=None):
        self.draw_state = draw_state
        self.n_cols = n_cols
        n_lines = self.n_lines = n_cols + 1
        # Cells inset by `padding` on every side and the whole row carries
        # ONE filled rounded band (drawn before the cells, so their rounded
        # backgrounds read as holes in it): the band shows through the
        # padding and fills the space between columns, so the row reads as
        # a single rounded rectangle. border_color=None (or padding 0)
        # disables it. `padding_y` overrides the VERTICAL inset alone
        # (default: same as padding) - a bandless host (the code editor's
        # compare split) keeps the horizontal padding around its dividers
        # without pushing the cells down below the row origin.
        # ``column_mins`` - per-column minimum widths (None entries fall
        # back to MIN_COLUMN_WIDTH), stamped as "min" on each column's
        # RIGHT edge so the column collision solve, the frame-fit clamp and
        # the window's min-width floor all honour them (see _edge_min).
        # ``column_maxes`` - per-column maximum widths (None = unbounded),
        # stamped as "max" the same way: a column at its cap drags its far
        # edge along instead of opening further, capped neighbours travel
        # as one, and a persisted over-cap column is packed back down on
        # construction (see _edge_max / _drag_edge / _clamp_interior).
        self.padding = float(padding)
        self.padding_y = float(padding if padding_y is None else padding_y)
        self.border_color = border_color
        self._cell_radius = {}

        window = draw_state.parent_window or draw_state
        _ensure_window_state(window)
        window_edge_pass(window)  # idempotent; usually ran via the wrapper
        self.window = window
        self.win_x = window.abs_left

        origin = imgui.get_cursor_screen_pos()
        self.top = origin[1]
        self._bottom = self.top

        # Mark this host so descendants' adoption checks can see it.
        draw_state._column_container = True

        # Far edges belong to the CONTAINER: the enclosing cell when given;
        # else the WINDOW frame whenever no other columns container sits
        # above this view (plain wrappers like window bodies / code_file_io
        # are exceptions - their host IS the window content, so the row IS
        # the window's row); else own edges (deep-nesting fallback).
        if left_edge is None and right_edge is None and not _has_columns_ancestor(draw_state):
            frame_edges = getattr(window, "_frame_edges", None)
            if frame_edges:
                left_edge, right_edge = frame_edges

        stored = column_edges if isinstance(column_edges, list) else []
        ok = (len(stored) == n_lines and
              all(isinstance(e, dict) and "x" in e for e in stored))
        seed_valid = True
        if ok:
            edges = list(stored)
        else:
            if left_edge is not None and right_edge is not None:
                base, extent = left_edge["x"], right_edge["x"] - left_edge["x"]
            else:
                base = origin[0] - self.win_x
                extent = float(draw_state.content_width or 0)
            # A seed taken before the container has laid out (content_width
            # ~0) stays TRANSIENT: render with it this frame, don't persist,
            # so a later frame re-seeds at the new extent instead of
            # locking up an all-minimum-width pile.
            min_total = sum(_column_floor(column_mins, i)
                            for i in range(n_cols))
            seed_valid = extent > min_total
            extent = max(extent, min_total)
            edges = _seed_edges(column_widths, n_cols, extent, base=base,
                                column_mins=column_mins,
                                column_maxes=column_maxes)
        owned = [True] * n_lines
        if left_edge is not None:
            edges[0] = left_edge
            owned[0] = False
        if right_edge is not None:
            edges[-1] = right_edge
            owned[-1] = False
        # Per-column minimums and maximums ride the edge dicts (re-stamped
        # every construction, so a hotswapped value applies live; columns
        # without one keep the flat MIN_COLUMN_WIDTH floor / no cap).
        for i in range(n_cols):
            floor = (column_mins[i] if column_mins and i < len(column_mins)
                     else None)
            if floor:
                edges[i + 1]["min"] = float(floor)
            else:
                edges[i + 1].pop("min", None)
            cap = _column_cap(column_maxes, i, _column_floor(column_mins, i))
            if cap is not None:
                edges[i + 1]["max"] = cap
            else:
                edges[i + 1].pop("max", None)
        # Interior dividers always stay inside the frame - see
        # _clamp_interior. (Pixel widths otherwise: a frame resize only
        # affects the divider it touches.)
        _clamp_interior(edges)
        if seed_valid and (not ok or any(a is not b for a, b in zip(stored, edges))):
            # Stamp so auto-state persists the list; in-place x mutations on
            # the dicts persist without re-stamping.
            draw_state.column_edges = edges
        self.edges = edges
        self.owned = owned

        # Keyed ("row", id), NOT bare draw_state.id: when the host ds IS the
        # window (a window body that draws its own cells, e.g. the code
        # editor's compare split), draw_state.id == window.id and this entry
        # and window_edge_pass's frame-edge entry (window.id) clobber each
        # other - the interior edges then never reach _all_edges, so
        # _solve_collisions doesn't find a dragged edge and silently drops the
        # drag (the divider reads as "not draggable"). Keys are opaque
        # (consumers iterate them); the eviction pass matches by ds.
        window._edge_views[("row", draw_state.id)] = (draw_state, edges)
        # The cells' floors & caps, owned by THIS view (not _REGISTRY): the
        # solve reads these, not the far-edge state, so a nested layout
        # ending on the same edge can't read (or wipe) this row's specs.
        _specs(window, "x")[("row", draw_state.id)] = (
            [_column_floor(column_mins, i) for i in range(n_cols)],
            [_column_cap(column_maxes, i, _column_floor(column_mins, i))
             for i in range(n_cols)])

        # Grab handles for OWNED edges (foreign far edges already have the
        # container's handles on the same line).
        self.active_edge = None
        # True when the cursor sits in one of this row's column edge grab
        # zones - i.e. exactly when a left_mouse_drag resize would trigger.
        # Drives the band highlight below. Recomputed per-frame: while the row
        # is bounding-hovered core_render re-renders the tile every frame, so
        # this needs no stored state or invalidation of its own.
        self.edge_hovered = False
        if resizable:
            height = max(getattr(draw_state, "_edge_lines_height", 0.0),
                         MIN_ROW_HEIGHT)
            seen_handles = set()
            for k in range(n_lines):
                if not owned[k]:
                    continue
                lo, hi = _grab_zone(edges, k)
                rect = (self.win_x + lo, self.top,
                        self.win_x + hi, self.top + height)
                if draw_state.hover_eligible(rect=rect):
                    self.edge_hovered = True
                drag = draw_state.on_action("left_mouse_drag",
                                            view_id=f"col_edge_{k}",
                                            rect=rect, priority_delta=1,
                                            cursor=mouse_cursor.RESIZE_EW)
                press = draw_state.on_action("left_mouse_down",
                                             view_id=f"col_edge_{k}",
                                             rect=rect, priority_delta=1)
                if press:
                    # Resize press, before any drag motion: freeze hosts
                    # snap their clean pre-drag capture this frame
                    # (mark_start_offscreen).
                    from src.lsd.gl_gui.melty import Melty
                    Melty.resize_press_frame = Melty.frame_count

                if not drag:
                    continue
                self.active_edge = k
                seen_handles.add(k)
                inc = _drag_inc(draw_state, k, drag)
                if inc:
                    window._pending_drags.append(
                        (edges[k], edges[k]["x"] + inc))

            totals = getattr(draw_state, "_drag_totals", None)
            if totals:
                # Mirror of the window_edge_pass prune scoping: our handles
                # are the int edge indices; leave the window's "frame total"
                # baselines (and a Row host's "row_*") alone - same dict
                # when the host ds IS the window or hosts both rows.
                for h in [h for h in totals
                          if isinstance(h, int) and h not in seen_handles]:
                    del totals[h]
            if self.active_edge is not None:
                # size-changing views already update live (see the re-solve
                # gate in window_edge_pass). freeze_resize hosts skip the
                # per-frame invalidate too - mid-drag they serve the frozen
                # tile and update once on release (window_edge_pass).
                if not draw_state.size_change:
                    if getattr(draw_state, "freeze_resize", False):
                        _defer_freeze_settle(window, draw_state)
                    else:
                        draw_state.invalidate(note=Note(reason="edge drag",
                                                        **_NOTE))
                # request_render()

        # The host's VISIBLE box (live clip, screen coords) - the band's
        # authority for adopted sides, so content margins / the scrollbar
        # reserve never shave it and the padding reads even all around.
        self.clip = Core.melty.get_clip_rect() or draw_state.abs_clip_rect

        # ----- the row band: one filled rounded rectangle spanning the
        # effective bounds, drawn BEFORE the cells so they render on top -
        # their rounded backgrounds are the holes, the band shows through
        # the padding and fills the space between columns. ADOPTED far
        # edges are the container's boundary, so the band takes the
        # container's visible box (the clip) there - a owned row's band
        # lands exactly in the panel band's middle. -----
        if self.padding > 0 and self.border_color is not None:
            radius = getattr(draw_state, "_band_radius", 6.0) + self.padding
            # Highlight the band when a divider drag is in progress
            # (self.active_edge) or the cursor sits over a draggable edge
            # (self.edge_hovered) - i.e. exactly where a resize drag is firing
            # or would. Both were computed against the drag grab zones above.
            band_color = (HIGHLIGHT_TINT
                          if self.edge_hovered or self.active_edge is not None
                          else self.border_color)
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_set_current(Core.melty.get_channel() - 1)
            draw_list.add_rect_filled(
                snap_int(self.win_x + self._band_left()),
                snap_int(self.top),
                snap_int(self.win_x + self._band_right()),
                snap_int(self._band_bottom()),
                imgui.get_color_u32_rgba(*band_color),
                rounding=radius)
            draw_list.channels_set_current(Core.melty.get_channel())

    def width(self, idx):
        return self.edges[idx + 1]["x"] - self.edges[idx]["x"]

    def _band_left(self):
        """Band left bound. An ADOPTED far edge sits on the container's
        boundary, where margins/rounding would shave the band — so the band
        takes the container's VISIBLE box (the clip) on that side instead.
        An owned edge is the row's own line and the band spans exactly to
        it."""
        if self.owned[0]:
            return self.edges[0]["x"]
        if self.clip is not None:
            return self.clip[0] - self.win_x
        return self.edges[0]["x"] + self.padding

    def _band_right(self):
        if self.owned[-1]:
            return self.edges[-1]["x"]
        if self.clip is not None:
            return self.clip[2] - self.win_x
        return self.edges[-1]["x"] - self.padding

    def _band_bottom(self):
        """Band bottom (SCREEN y): the host's visible viewport — its clip —
        not the measured content height, so a short row still frames the
        whole pane, there's no settle lag, and a cell holding a
        height-filling child (a Rows) has room to fill. No clip: the
        measured height."""
        if self.clip is not None:
            return self.clip[3]
        return self.top + max(self.draw_state.height or 0.0, MIN_ROW_HEIGHT)

    def _bound_left(self, idx):
        return self._band_left() if idx == 0 else self.edges[idx]["x"]

    def _bound_right(self, idx):
        return (self._band_right() if idx == self.n_cols - 1
                else self.edges[idx + 1]["x"])

    def inner_width(self, idx):
        """Content width of column idx: the effective span minus padding."""
        return max(0.0, self._bound_right(idx) - self._bound_left(idx)
                   - 2 * self.padding)

    def cell_rect(self, idx, height=None):
        """The screen-space (x, y, w, h) content box `cell(idx, height)`
        clips to — for a caller decorating a cell from OUTSIDE it, e.g. a
        card shadow (cast beyond the box, it would be scissored away by
        the cell's own clip). Without an explicit ``height`` the box runs
        to the band bottom (the host's visible viewport), the same
        authority the band paints to."""
        pad = self.padding
        pad_y = self.padding_y
        x0 = snap_int(self.win_x + self._bound_left(idx) + pad)
        y0 = snap_int(self.top + pad_y)
        if height is not None:
            y1 = snap_int(self.top) + snap_int(height) - snap_int(pad_y)
        else:
            y1 = snap_int(self._band_bottom()) - snap_int(pad_y)
        return x0, y0, snap_int(self.inner_width(idx)), y1 - y0

    @contextmanager
    def cell(self, idx, height=None):
        """Position the cursor at column idx's content origin (inset by
        padding from the effective bounds), clip to the content box, and
        yield the content width. Render the cell inside the with-block.
        `height` is the FULL cell band including padding — the content box
        is inset from it."""
        pad = self.padding
        pad_y = self.padding_y
        left_b = self._bound_left(idx)
        inner_w = self.inner_width(idx)
        x0, y0, box_w, box_h = self.cell_rect(idx, height)
        imgui.set_cursor_screen_pos((self.win_x + left_b + pad,
                                     self.top + pad_y))
        Core.melty.push_clip((x0, y0, x0 + box_w, y0 + box_h))
        # The group makes the cell origin the LINE START of everything
        # inside: without it only the first item sits at x0 because imgui's
        # newline returns the cursor to the imgui window's content x, so a
        # cell stacking multiple rows (eg compare split's inner column) drew
        # every row after the first left of the cell's clip and lost its
        # leading pixels (the row chevrons).
        imgui.begin_group()
        try:
            yield inner_w
        finally:
            imgui.end_group()
            Core.melty.pop_clip()
            bottom = imgui.get_cursor_screen_pos()[1]
            self._bottom = max(self._bottom, bottom + pad_y)

    def note_child(self, idx, child_ds):
        """Optional: record column idx's child draw_state so the band's
        outer rounding can follow the children's actual corner radius
        (default 6 when never noted)."""
        if child_ds is not None:
            radius = getattr(child_ds, "corner_radius", None)
            if radius is not None:
                self._cell_radius[idx] = float(radius)

    def finish(self):
        """Measure the row — the UNIFORM band height the panel draws at next
        frame — stamp the band rounding from the noted child corners, and
        leave the flow cursor below the row."""
        ds = self.draw_state
        new_h = max(self._bottom - self.top, MIN_ROW_HEIGHT)
        prev_h = getattr(ds, "_edge_lines_height", None)
        ds._edge_lines_height = new_h
        ds._band_radius = (max(self._cell_radius.values())
                           if self._cell_radius else 6.0)
        if prev_h is None:
            # FIRST measure only: the band rounding and grab zones drew with
            # defaults this frame; catch up once. A HEIGHT CHANGE never
            # invalidates here - the band's pixels come from the row's clip
            # (or draw_state.height in the no-clip fallback), not from the
            # measured height, and the measured height only feeds shadow
            # rects (grab zones, edge_under_cursor bands) that read the fresh
            # stamp on the next body run without a repaint. The old height-delta
            # settle force-climbed every ancestor tile on any >1px height
            # wiggle (typing in an editor cell), recomposing unrelated columns
            # up the ancestor chain.
            if not ds.size_change:
                ds.invalidate(note=Note(reason="row band settle", **_NOTE))
            request_render()
        imgui.set_cursor_screen_pos((self.win_x + self.edges[0]["x"],
                                     self._bottom))
        imgui.dummy(0, 0)


class RowLayout:
    """ColumnLayout's row-axis twin: manual-cell access to the shared edge
    system for cells stacked top to bottom.

        rows = RowLayout(draw_state, n_rows, row_edges=row_edges,
                         row_heights=row_heights)
        for idx in range(n_rows):
            with rows.cell(idx) as height:
                any_renderer(..., height=height, width=rows.inner_width())
        rows.finish()

    Declare ``row_edges=None`` as a named param on the host render_func and
    pass its resolved value through — RowLayout stamps draw_state.row_edges
    on seed/adoption changes, so auto-state persists the ``{"y": …}`` edge
    dicts exactly like draw_rows. Edges register on the root window's
    ``_row_views`` and solve in the y-axis flat pass; the window's top and
    bottom frame edges are the far edges of a window-direct rows view, so a
    top/bottom frame drag squeezes the first/last row and an interior edge
    pushed into the frame slides or grows the window — the same physics
    columns run along x. The orthogonal extent is simpler than a column's:
    a row's WIDTH is pinned by the host's visible box, so no measure step
    is needed to size the cells."""

    def __init__(self, draw_state, n_rows, row_edges=None, row_heights=None,
                 top_edge=None, bottom_edge=None, resizable=True,
                 padding=6.0, border_color=(0.0, 0.0, 0.0, 0.9),
                 padding_x=None, row_mins=None, row_maxes=None):
        self.draw_state = draw_state
        self.n_rows = n_rows
        n_lines = self.n_lines = n_rows + 1
        # Same band + padding contract as ColumnLayout; ``padding_x``
        # overrides the HORIZONTAL inset alone. ``row_mins`` / ``row_maxes``
        # stamp "min" / "max" on each row's BOTTOM edge (see _edge_min /
        # _edge_max).
        self.padding = float(padding)
        self.padding_x = float(padding if padding_x is None else padding_x)
        self.border_color = border_color
        self._cell_radius = {}

        window = draw_state.parent_window or draw_state
        _ensure_window_state(window)
        window_edge_pass(window)  # idempotent; usually ran via the wrapper
        self.window = window
        self.win_x, self.win_y = window.abs_left, window.abs_top

        # The host's VISIBLE box (or clip, screen coords): the rows's full
        # horizontal extent, and the authority of the adopted top/bottom.
        self.clip = Core.melty.get_clip_rect() or draw_state.abs_clip_rect
        origin = imgui.get_cursor_screen_pos()
        # Where the flow cursor stood: an adopted top edge (the window frame
        # top sits at the header's top) never starts the first row above
        # this.
        self.origin_y = origin[1]
        if self.clip is not None:
            self.left, self.right = self.clip[0], self.clip[2]
        else:
            self.left = origin[0]
            self.right = origin[0] + float(draw_state.content_width or 0)
        self._right = self.left

        # Mark the host so descendants' adoption climbs can ignore it.
        draw_state._row_container = True

        # Far edges belong to the CONTAINER: the enclosing cell when given;
        # else the WINDOW's top/bottom frame when no other rows
        # container sits above this view; else own edges (fallback).
        if top_edge is None and bottom_edge is None and not _has_rows_ancestor(draw_state):
            frame_rows = _frame(window, "y")
            if frame_rows:
                top_edge, bottom_edge = frame_rows

        stored = row_edges if isinstance(row_edges, list) else []
        ok = (len(stored) == n_lines and
              all(isinstance(e, dict) and "y" in e for e in stored))
        seed_valid = True
        if ok:
            edges = list(stored)
        else:
            if top_edge is not None and bottom_edge is not None:
                # Share over the VISIBLE span: the adopted top may sit above
                # the flow origin (window frame top → header top), and the
                # shares only split the room the rows actually get.
                base = max(top_edge["y"], self.origin_y - self.win_y)
                extent = bottom_edge["y"] - base
            else:
                base = self.origin_y - self.win_y
                extent = float(draw_state.height or 0)
            min_total = sum(_row_floor(row_mins, i) for i in range(n_rows))
            seed_valid = extent > min_total
            extent = max(extent, min_total)
            edges = _seed_edges(row_heights, n_rows, extent, base=base,
                                column_mins=row_mins, column_maxes=row_maxes,
                                axis="y")
        owned = [True] * n_lines
        if top_edge is not None:
            edges[0] = top_edge
            owned[0] = False
        if bottom_edge is not None:
            edges[-1] = bottom_edge
            owned[-1] = False
        self.edges = edges
        self.owned = owned
        for i in range(n_rows):
            floor = row_mins[i] if row_mins and i < len(row_mins) else None
            if floor:
                edges[i + 1]["min"] = float(floor)
            else:
                edges[i + 1].pop("min", None)
            cap = _column_cap(row_maxes, i, _row_floor(row_mins, i))
            if cap is not None:
                edges[i + 1]["max"] = cap
            else:
                edges[i + 1].pop("max", None)
        # The LEAD: an adopted top edge can sit well above where the first
        # row begins (window frame top → the header and anything drawn
        # before the rows). The flat solve measures the first row from the
        # edge, so its floor is raised by that lead - the VISIBLE first row
        # keeps its minimum when the window frame edge is dragged, and the
        # frame-fit clamp places it correctly.
        lead = self._band_top() - edges[0]["y"]
        if lead > 0.5:
            edges[1]["min"] = _row_floor(row_mins, 0) + lead
        _clamp_interior(edges, axis="y")
        if seed_valid and (not ok or any(a is not b for a, b in zip(stored, edges))):
            draw_state.row_edges = edges

        # Keyed ("rows", id) - see ColumnLayout's ("row", id) note: a host
        # ds that IS the window must not clobber the spec entry.
        _views(window, "y")[("rows", draw_state.id)] = (draw_state, edges)
        floors = [_row_floor(row_mins, i) for i in range(n_rows)]
        if lead > 0.5:
            floors[0] += lead
        _specs(window, "y")[("rows", draw_state.id)] = (
            floors,
            [_column_cap(row_maxes, i, _row_floor(row_mins, i))
             for i in range(n_rows)])

        self.active_edge = None
        self.edge_hovered = False
        if resizable:
            seen_handles = set()
            for k in range(n_lines):
                if not owned[k]:
                    continue
                lo, hi = _grab_zone(edges, k, axis="y")
                rect = (self.left, self.win_y + lo, self.right, self.win_y + hi)
                if draw_state.hover_eligible(rect=rect):
                    self.edge_hovered = True
                drag = draw_state.on_action("left_mouse_drag",
                                            view_id=f"row_edge_{k}",
                                            rect=rect, priority_delta=1,
                                            cursor=mouse_cursor.RESIZE_NS)
                press = draw_state.on_action("left_mouse_down",
                                             view_id=f"row_edge_{k}",
                                             rect=rect, priority_delta=1)
                if press:
                    from src.lsd.gl_gui.melty import Melty
                    Melty.resize_press_frame = Melty.frame_count
                if not drag:
                    continue
                self.active_edge = k
                handle = f"row_{k}"
                seen_handles.add(handle)
                inc = _drag_inc(draw_state, handle, drag, total="total_dy")
                if inc:
                    _pending(window, "y").append(
                        (edges[k], edges[k]["y"] + inc))

            totals = getattr(draw_state, "_drag_totals", None)
            if totals:
                # Own namespace only ("row_*"): a ColumnLayout on the same
                # host keeps its intents, the window its "win_*".
                for h in [h for h in totals
                          if isinstance(h, str) and h.startswith("row_")
                          and h not in seen_handles]:
                    del totals[h]
            if self.active_edge is not None:
                if not draw_state.size_change:
                    if getattr(draw_state, "freeze_resize", False):
                        _defer_freeze_settle(window, draw_state)
                    else:
                        draw_state.invalidate(note=Note(reason="edge drag",
                                                        **_NOTE))

        # ----- the column band: a filled rounded rectangle, the cells'
        # rounded backgrounds its holes (see ColumnLayout) -----
        if self.padding > 0 and self.border_color is not None:
            radius = getattr(draw_state, "_band_radius", 6.0) + self.padding
            band_color = (HIGHLIGHT_TINT
                          if self.edge_hovered or self.active_edge is not None
                          else self.border_color)
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_set_current(Core.melty.get_channel() - 1)
            draw_list.add_rect_filled(
                snap_int(self.left),
                snap_int(self.win_y + self._band_top()),
                snap_int(self.right),
                snap_int(self.win_y + self._band_bottom()),
                imgui.get_color_u32_rgba(*band_color),
                rounding=radius)
            draw_list.channels_set_current(Core.melty.get_channel())

    def height(self, idx):
        return self.edges[idx + 1]["y"] - self.edges[idx]["y"]

    def _band_top(self):
        """Band top bound (window coords). An owned edge is the row's own
        line; an ADOPTED one is the container's boundary, so the band
        starts at the flow origin — never above the container's visible
        box."""
        if self.owned[0]:
            return self.edges[0]["y"]
        top = self.origin_y - self.win_y
        if self.clip is not None:
            top = max(top, self.clip[1] - self.win_y)
        return top

    def _band_bottom(self):
        """Band bottom bound (window coords): the adopted edge's line, but
        never past the container's visible box (a window-direct view ends
        at the window's content clip, a cell-nested one at its cell)."""
        if self.owned[-1]:
            return self.edges[-1]["y"]
        bottom = self.edges[-1]["y"]
        if self.clip is not None:
            bottom = min(bottom, self.clip[3] - self.win_y)
        else:
            bottom -= self.padding
        return bottom

    def _bound_top(self, idx):
        return self._band_top() if idx == 0 else self.edges[idx]["y"]

    def _bound_bottom(self, idx):
        return (self._band_bottom() if idx == self.n_rows - 1
                else self.edges[idx + 1]["y"])

    def inner_height(self, idx):
        """Content height of row idx: the effective span minus padding."""
        return max(0.0, self._bound_bottom(idx) - self._bound_top(idx)
                   - 2 * self.padding)

    def inner_width(self):
        """Content width every row gets: the visible box minus the
        horizontal padding."""
        return max(0.0, self.right - self.left - 2 * self.padding_x)

    def cell_rect(self, idx, width=None):
        """The screen-space (x, y, w, h) content box `cell(idx, width)`
        clips to. ``width`` is the FULL cell band including padding; by
        default the box runs to the visible box's right."""
        pad = self.padding
        pad_x = self.padding_x
        x0 = snap_int(self.left + pad_x)
        y0 = snap_int(self.win_y + self._bound_top(idx) + pad)
        if width is not None:
            x1 = snap_int(self.left) + snap_int(width) - snap_int(pad_x)
        else:
            x1 = snap_int(self.right) - snap_int(pad_x)
        y1 = snap_int(self.win_y + self._bound_bottom(idx)) - snap_int(pad)
        return x0, y0, x1 - x0, y1 - y0

    @contextmanager
    def cell(self, idx, width=None):
        """Position the cursor at row idx's content origin, clip to the
        content box, and yield the content HEIGHT (pass it as ``height=``
        to the child, with ``width=inner_width()`` — a passed height pins
        the child fixed-size, so its width must be pinned too)."""
        x0, y0, box_w, box_h = self.cell_rect(idx, width)
        imgui.set_cursor_screen_pos((x0, y0))
        Core.melty.push_clip((x0, y0, x0 + box_w, y0 + box_h))
        imgui.begin_group()
        try:
            yield self.inner_height(idx)
        finally:
            imgui.end_group()
            Core.melty.pop_clip()
            right = imgui.get_item_rect_max()[0]
            self._right = max(self._right, right + self.padding_x)

    def note_child(self, idx, child_ds):
        if child_ds is not None:
            radius = getattr(child_ds, "corner_radius", None)
            if radius is not None:
                self._cell_radius[idx] = float(radius)

    def finish(self):
        """Stamp the measured band width (edge_under_cursor's fallback
        band) and the band rounding, and leave the flow cursor below the
        last row."""
        ds = self.draw_state
        prev_w = getattr(ds, "_edge_lines_width", None)
        ds._edge_lines_width = max(self._right - self.left, MIN_COLUMN_WIDTH)
        ds._band_radius = (max(self._cell_radius.values())
                           if self._cell_radius else 6.0)
        if prev_w is None:
            if not ds.size_change:
                ds.invalidate(note=Note(reason="row band settle", **_NOTE))
            request_render()
        imgui.set_cursor_screen_pos((self.left,
                                     self.win_y + self._band_bottom()))
        imgui.dummy(0, 0)


@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False,
             is_default_for=Columns)
def draw_columns(input_value, column_widths=None, column_edges=None,
                 draw_state=None, resizable=True, child_kwargs=None, **kwargs):
    """Side-by-side cells lined up with shared draggable edges.

    Edges are {"x": float} dicts in window coordinates (see the edge-model
    comment above). This view owns its interior edges (auto-state
    ``column_edges``); when it renders as a cell of another columns view it
    adopts the enclosing cell's two edge objects as its far edges
    (``left_edge``/``right_edge`` kwargs, passed by reference). All edges
    register on the ROOT WINDOW, which solves collisions once per frame and
    drives its own frame from the direct row's far edges; this view just
    queues drags and lines its cells up with its edges.
    """
    if child_kwargs is None:
        child_kwargs = {}

    if isinstance(input_value, dict):
        keys = [k for k in input_value
                if not (isinstance(k, str) and (k.startswith("_") or k.endswith("_")))]
    elif isinstance(input_value, (list, tuple)):
        keys = list(range(len(input_value)))
    else:
        imgui.text(f"draw_columns: no view for {type(input_value).__name__}")
        return False, input_value

    if not keys:
        return False, input_value
    n_cols = len(keys)

    cols = ColumnLayout(draw_state, n_cols, column_edges=column_edges,
                        column_widths=column_widths,
                        left_edge=kwargs.get("left_edge"),
                        right_edge=kwargs.get("right_edge"),
                        resizable=resizable)

    # ----- cells: lined up with their edges; nested Columns get the cell's
    # edge objects by reference -----
    changed = False
    for idx, key in enumerate(keys):
        item = input_value[key]
        with cols.cell(idx) as cell_width:
            item_kwargs = {"name": f"{key}", "align_header": False,
                           "width": cell_width} | child_kwargs
            if isinstance(item, Columns):
                item_kwargs |= {"left_edge": cols.edges[idx],
                                "right_edge": cols.edges[idx + 1]}
            if isinstance(item, (Columns, Rows)):
                # Row edges pass THROUGH a Columns cell: a Rows nested in
                # this cell adopts the enclosing row's edges (handed to us
                # by draw_rows), so its cells will collide with them.
                for key in ("top_edge", "bottom_edge"):
                    if kwargs.get(key) is not None:
                        item_kwargs[key] = kwargs[key]
            item_changed, out_value, cell_ds = draw_any(
                item, return_extras=True, **item_kwargs)
            cols.note_child(idx, cell_ds)  # border follows the child's corners
        if item_changed:
            changed = True
            if isinstance(input_value, (dict, list)):
                input_value[key] = out_value

    cols.finish()

    return changed, input_value


@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False,
             is_default_for=Rows)
def draw_rows(input_value, row_heights=None, row_edges=None,
              draw_state=None, resizable=True, child_kwargs=None, **kwargs):
    """Stacked cells lined up with shared draggable edges — draw_columns
    along y.

    Edges are {"y": float} dicts in window coordinates. This view owns its
    interior edges (auto-state ``row_edges``); as a cell of another rows
    view it adopts the enclosing cell's two edge objects as its far edges
    (``top_edge``/``bottom_edge`` kwargs, by reference); as the window's
    rows view it adopts the window's top/bottom frame edges. Every cell is
    pinned to its row's height AND the rows' width (a passed height alone
    collapses a child's width), so a Columns cell fills its row.
    """
    if child_kwargs is None:
        child_kwargs = {}

    if isinstance(input_value, dict):
        keys = [k for k in input_value
                if not (isinstance(k, str) and (k.startswith("_") or k.endswith("_")))]
    elif isinstance(input_value, (list, tuple)):
        keys = list(range(len(input_value)))
    else:
        imgui.text(f"draw_rows: no view for {type(input_value).__name__}")
        return False, input_value

    if not keys:
        return False, input_value
    n_rows = len(keys)

    rows = RowLayout(draw_state, n_rows, row_edges=row_edges,
                     row_heights=row_heights,
                     top_edge=kwargs.get("top_edge"),
                     bottom_edge=kwargs.get("bottom_edge"),
                     resizable=resizable)

    changed = False
    cell_width = rows.inner_width()
    for idx, key in enumerate(keys):
        item = input_value[key]
        with rows.cell(idx) as cell_height:
            item_kwargs = {"name": f"{key}", "align_header": False,
                           "height": cell_height,
                           "width": cell_width} | child_kwargs
            if isinstance(item, (Rows, Columns)):
                # A nested Rows adopts this row's edges; a Columns carries
                # them through to any Rows in ITS cells (draw_columns).
                item_kwargs |= {"top_edge": rows.edges[idx],
                                "bottom_edge": rows.edges[idx + 1]}
                # And column edges pass THROUGH a Rows cell to the_columns.
                for key in ("left_edge", "right_edge"):
                    if kwargs.get(key) is not None:
                        item_kwargs[key] = kwargs[key]
            item_changed, out_value, cell_ds = draw_any(
                item, return_extras=True, **item_kwargs)
            rows.note_child(idx, cell_ds)
        if item_changed:
            changed = True
            if isinstance(input_value, (dict, list)):
                input_value[key] = out_value

    rows.finish()

    return changed, input_value