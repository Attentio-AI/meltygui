"""Standalone column layout — independent of core_render's legacy column=
machinery (_column_cursor / _columns_top / column_offsets stay untouched).

draw_columns is an ordinary container render_func: it places one child per
column at an explicit screen position and pins the child's width AND height.
A child with both passed is fixed_size in the wrapper, so it pushes itself
onto Melty.fixed_size_stack and its content (and all descendants) wrap at
the column width — the wrapper does the rest, nothing here touches it.

Columns are rigid bodies with their own position and width
(``column_layout`` auto-state, [x, width] per column, where x is an offset
from the window's left edge — the content origin). That reference frame is
the point: when the window moves or its left edge is dragged, columns ride
it in the SAME frame automatically; there are no compensation passes, so
nothing ever lags or readjusts. NOTHING else moves unless dragged or
bumped — and it's EDGES that move, not columns: a moving edge that
reaches a neighbour first consumes that column's width (its far edge
stays put) down to the hard MIN_COLUMN_WIDTH floor; only then does the
far edge move and carry the push onward (a min-width column translates
as a unit). A push past a container's bound escalates: rightward
it widens the enclosing draw_columns cell (recursing through nesting) and
finally the parent window; leftward a train that hits the left bound
NUDGES it — the cell's left edge gives way, and at the top the window's
left edge slides (window_pos shifts, width grows). A left nudge moves the
origin, so the layout is readjusted by the same amount and renders one
frame behind the window's move — accepted cost. Pushes MUTATE state and
stay: dragging back does not un-push anything.

ESCALATION IS DRAG-DRIVEN ONLY. The glue that keeps the last edge on the
container's right bound is bound-FOLLOWING: it clamps at the row's
fully-compressed minimum and never escalates — if it could, any frame
where the bound falls below that minimum (a corner drag re-deriving
width from its latch, a content-sized window re-measuring) would nudge
window_pos again and again, scooting the window off screen. Hands move
the world; the glue only follows it. The columns' minimum is instead
stamped onto window.min_width, so core_render's own resize handler
refuses to shrink a window below its row. Window "fixedness" is read
from the RAW
auto_resize kwarg (draw_state.auto_resize is False for every closable
window, including content-hugging ones — useless here): content-driven
windows get no glue on their outermost container (their width already
follows the row through the footprint commit — the same-edge semantics
emerge for free), no width writes, no left strip.

``column_widths`` keeps its ownership semantics: a caller that passes it
owns the layout every frame (packed, dividers inert); a caller that omits
it gets the stateful rigid-body layout above.

Heights settle by feedback: each cell renders at last frame's measured
content height; the real height is read off the cell's draw_state after
the call and fed back, converging one frame later.
"""
import imgui

from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
from src.lsd.gl_gui.view.invalidation_tracker import Note

COLUMN_GAP = 8
# Fundamental rule: no column is ever pushed below this, regardless of what its
# child view would tolerate. Pushes consume a column's width down to this
# floor before they start moving the column's far edge.
MIN_COLUMN_WIDTH = 30
MIN_CELL_HEIGHT = 20
EDGE_GRAB_WIDTH = 14.0
WINDOW_EDGE_GRAB = 14.0

_PUSH_NOTE = dict(name="draw_columns", tint=(0.5, 0.8, 1.0))


class Columns(dict):
    """Marker dict: values render side by side (draw_columns is its default
    renderer), so column layouts nest by data:
    Columns({"a": ..., "b": Columns({...})})."""


def resolve_column_widths(column_widths, n_cols, content_width, gap):
    """Pixel width per column for ``n_cols`` columns in ``content_width``.

    Entries are pixels; None (or a missing entry — the list may be shorter
    than the column count) takes an equal share of whatever the sized columns
    leave over. Widths never drop below MIN_COLUMN_WIDTH.
    """
    available = max(0.0, content_width - gap * (n_cols - 1))
    spec = list(column_widths)[:n_cols] if column_widths else []
    spec += [None] * (n_cols - len(spec))

    fixed_total = sum(max(float(w), MIN_COLUMN_WIDTH)
                      for w in spec if w is not None)
    flex_count = sum(1 for w in spec if w is None)
    share = (max(MIN_COLUMN_WIDTH, (available - fixed_total) / flex_count)
             if flex_count else 0.0)

    return [share if w is None else max(float(w), MIN_COLUMN_WIDTH)
            for w in spec]


def _packed_layout(widths, gap):
    """[x, width] per column, packed left with `gap` between."""
    layout, x = [], 0.0
    for w in widths:
        layout.append([x, float(w)])
        x += w + gap
    return layout


def _widths_pinned(draw_state, column_widths):
    """True when the CALLER owns column_widths this call (passed explicitly).

    The body can't see raw call kwargs (auto-state resolves them away), but
    auto_params holds only internally-written values and an explicit kwarg
    wins over it — so a resolved value that exists with no stored entry, or
    that differs from the stored one, must have been passed by the caller.
    """
    if column_widths is None:
        return False
    stored = (draw_state.__dict__.get("auto_params") or {}).get("column_widths")
    if stored is None:
        return True
    try:
        return bool(list(column_widths) != list(stored))
    except Exception:
        return True


def _layout_to_edges(layout):
    """Flatten [x, w] columns into a sorted edge-position list:
    [L0, R0, L1, R1, ...]."""
    edges = []
    for x, w in layout:
        edges.append(float(x))
        edges.append(float(x + w))
    return edges


def _edges_to_layout(edges, layout):
    """Write resolved edge positions back onto the [x, w] layout."""
    for i in range(len(layout)):
        left, right = edges[2 * i], edges[2 * i + 1]
        layout[i][0] = left
        layout[i][1] = right - left


def _min_sep(k, gap):
    """Minimum separation between consecutive edges k and k+1: a column's
    width floor between its own two edges, the visual gap between columns."""
    return float(MIN_COLUMN_WIDTH) if k % 2 == 0 else float(gap)


def _drag_edge(edges, k, target, gap):
    """Move edge k to `target`. No other edge moves unless the moving edge
    (or one it already carried) comes within its MINIMUM separation — then
    it is carried, and the chain stops at the first edge that's far enough
    away. Edges only move edges they're touching: a column's far edge is
    "touched" by its near edge only at the 30px floor, so a pushed column
    first compresses in place (far edge planted) and only then translates.
    The drag is just one line; everything else is contacts."""
    old = edges[k]
    if target == old:
        return
    edges[k] = float(target)
    if target > old:
        for m in range(k + 1, len(edges)):
            need = edges[m - 1] + _min_sep(m - 1, gap)
            if edges[m] >= need:
                break
            edges[m] = need
    else:
        for m in range(k - 1, -1, -1):
            need = edges[m + 1] - _min_sep(m, gap)
            if edges[m] <= need:
                break
            edges[m] = need


def _min_left_target(layout, k, gap):
    """Lowest position edge k can reach by dragging left with the left bound
    rigid: every column to its left fully compressed to the floor. Mirrors
    _drag_edge's minimum separations exactly — a leftward drag clamped to
    this can never overshoot x=0."""
    own = k // 2
    x = own * (float(MIN_COLUMN_WIDTH) + gap)
    if k % 2:  # a right edge: the next column's floor sits between
        x += float(MIN_COLUMN_WIDTH)
    return x


def _window_is_fixed(window):
    """True when the window's width is a persistent value rather than
    re-measured from content every frame. draw_state.auto_resize is the
    WRONG flag for this — closable forces it False even for content-hugging
    windows. The wrapper's actual sizing condition is the RAW auto_resize
    kwarg (default True) `or not expanded`; reproduce exactly that."""
    if window is None:
        return False
    kwargs = getattr(window, "_kwargs", None) or {}
    content_driven = (kwargs.get("auto_resize", True)
                      or not getattr(window, "expanded", True))
    return not content_driven


def _enclosing_cell(draw_state):
    """(container_ds, cell_idx) of the nearest ancestor draw_columns this
    container sits in, or (None, None). The climb is scoped to the parent
    window (stops there; guards the root's self-parent loop)."""
    node = draw_state
    window = draw_state.parent_window
    while node is not None and node is not window and node._parent is not node:
        parent = node._parent
        if parent is None:
            break
        if getattr(parent, "_column_container", False):
            for i, c in parent._children.items():
                if c is node:
                    return parent, i
        node = parent
    return None, None


def _apply_edge_drag(draw_state, layout, k, target, content_width, gap,
                     escalate=True):
    """Step 2 of the two-step drag: resolve edge k's move on the flat edge
    list, write positions back, and escalate ONLY what this drag newly
    pushed past the container bounds (a row already overflowing doesn't
    re-escalate). Returns True if anything moved.

    escalate=False is for bound-FOLLOWING callers (the glue): the move is
    resolved locally and may clip, but it never requests space — only
    drag-driven calls may move cells, windows, or window_pos. Bound
    followers that escalate are feedback loops waiting to happen (a corner
    drag re-derives width from its latch every frame, erasing the growth
    the escalation just made → the same nudge fires forever)."""
    edges = _layout_to_edges(layout)
    if target == edges[k]:
        return False
    old_first, old_last = edges[0], edges[-1]
    _drag_edge(edges, k, target, gap)
    _edges_to_layout(edges, layout)

    if escalate:
        over_right = edges[-1] - max(old_last, content_width)
        if over_right > 0:
            _request_width(draw_state, over_right)
        over_left = min(old_first, 0.0) - edges[0]
        if over_left > 0:
            _request_left(draw_state, over_left)
            # The origin just moved left by over_left; readjust so non-pushed
            # cells stay put on screen (renders one frame behind the window).
            for c in layout:
                c[0] += over_left
    return True


def _request_width(draw_state, amount):
    """A push overflowed this container's right bound by `amount` px: the
    enclosing draw_columns cell's RIGHT edge gives way (same edge-drag rules
    at the parent level, recursing outward); at the top the parent window
    widens. Rightward growth never moves the origin — no readjustment."""
    if amount <= 0:
        return
    parent, idx = _enclosing_cell(draw_state)
    if parent is not None:
        layout, content_width, gap, pinned = parent._col_state
        if pinned:
            return  # the caller owns that layout; the push stops (clips) here
        k = 2 * idx + 1
        target = layout[idx][0] + layout[idx][1] + amount
        _log_mut(f"request_width +{round(amount, 1)} (from nested)", parent,
                 k, target)
        if _apply_edge_drag(parent, layout, k, target, content_width, gap):
            parent.column_layout = layout
            parent.invalidate(note=Note(reason="pushed by nested columns",
                                        **_PUSH_NOTE))
        return

    window = draw_state.parent_window
    if window is None:
        return
    # Fixed windows grow explicitly; content-driven windows follow the
    # content, which just grew so nothing to do.
    if _window_is_fixed(window):
        _log_mut(f"request_width window +{round(amount, 1)}", draw_state,
                 -1, window.width + amount)
        window.width = snap_int(window.width + amount)
        window.expanded = True


def _request_left(draw_state, amount):
    """A push hit this container's left bound with `amount` px to go: the
    enclosing draw_columns cell's LEFT edge gives way (edge-drag rules at
    the parent level, recursing outward); at the top the parent window's
    left edge is nudged (window_pos shifts, width grows)."""
    if amount <= 0:
        return
    parent, idx = _enclosing_cell(draw_state)
    if parent is not None:
        layout, content_width, gap, pinned = parent._col_state
        if pinned:
            return  # the caller owns that layout; the push stops (clips) here
        k = 2 * idx
        _log_mut(f"request_left -{round(amount, 1)} (from nested)", parent,
                 k, layout[idx][0] - amount)
        if _apply_edge_drag(parent, layout, k, layout[idx][0] - amount,
                            content_width, gap):
            parent.column_layout = layout
            parent.invalidate(note=Note(reason="pushed by nested columns",
                                        **_PUSH_NOTE))
        return

    window = draw_state.parent_window
    if window is None:
        return
    _log_mut(f"request_left window nudge -{round(amount, 1)}", draw_state,
             -1, amount)
    # The pos slide applies to every window kind (a content-driven window
    # grows leftward: pos l slides, width follows from the content); the
    # width write only sticks on truly fixed windows.
    pos = window.window_pos or (0, 0)
    window.window_pos = (pos[0] - amount, pos[1])
    if _window_is_fixed(window):
        window.width = snap_int(window.width + amount)
        window.expanded = True


# Rolling log of every layout mutation (who moved what, why) - forensics
# for "this edge moved and nothing should have moved it". Read via eval:
#   from src.lsd.gl_gui.view.core_views import columns; columns.MUTATION_LOG
MUTATION_LOG = []


def _log_mut(reason, draw_state, k, target):
    from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
    MUTATION_LOG.append((Core.melty.frame_count, reason,
                         str(draw_state.name), str(draw_state._tile_id),
                         k, round(float(target), 1)))
    if len(MUTATION_LOG) > 300:
        del MUTATION_LOG[:100]


def _drag_inc(draw_state, handle, drag):
    """Per-frame pixel delta for a drag handle, from total_dx against the
    last seen total. State is PER-HANDLE (dict), never a shared slot: with a
    shared slot, two events alive in the same frame alternate ownership and
    each re-fires its full total against a zero baseline — moving edges that
    were never dragged. Entries are pruned by the caller when their handle
    has no event, so a new gesture always starts from a clean baseline."""
    totals = getattr(draw_state, "_drag_totals", None)
    if totals is None:
        totals = draw_state._drag_totals = {}
    last = totals.get(handle, 0.0)
    totals[handle] = drag.total_dx
    return drag.total_dx - last


@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False,
             is_default_for=Columns)
def draw_columns(input_value, column_widths=None, column_layout=None,
                 draw_state=None, column_gap=COLUMN_GAP, max_cell_height=None,
                 resizable=True, cell_heights=None, child_kwargs=None, **kwargs):
    """Lay out a dict's values (or a list's items) side by side in columns.

    Each value is one column, drawn with draw_any so it routes to its normal
    renderer (a nested Columns value nests the layout). Pass ``column_widths``
    (pixels; None entries share the leftover) to own a packed layout, or omit
    it for the stateful rigid-body layout where edge drags persist.
    ``column_layout`` is auto-state ([x, width] per column) — don't pass it.
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

    if cell_heights is None:
        cell_heights = {}
        draw_state.cell_heights = cell_heights
    for stale in [k for k in cell_heights if k not in keys]:
        del cell_heights[stale]

    pinned = _widths_pinned(draw_state, column_widths)
    if pinned or column_layout is None or len(column_layout) != n_cols:
        widths = resolve_column_widths(column_widths, n_cols,
                                       draw_state.content_width, column_gap)
        layout = _packed_layout(widths, column_gap)
    else:
        # Fresh working copy each frame; any mutation (own drags, pushes from
        # nested containers via _col_state) writes it back to the auto-state.
        layout = [list(c) for c in column_layout]

    draw_state._pinned = pinned
    draw_state._column_container = True
    draw_state._col_state = (layout, draw_state.content_width, column_gap, pinned)

    # The last column's right edge and this container's right bound are THE
    # SAME EDGE. Keeping them glue in sync makes normal window resizing
    # (corner drag, right-click resize, the strips) interact with the
    # columns: shrinking drags the shared edge left - compressing the last
    # column to the bound, then coiling the rigid train - and growing
    # stretches the last column. The glue FOLLOWS the bound: its target is
    # clamped at the row's rigid minimum and it NEVER escalates (a follower
    # that escalates is a feedback loop - see _apply_edge_drag). Skipped for
    # the outermost container of a content-driven window, whose width
    # already follows the row without the layout commit.
    bound = draw_state.content_width
    in_cell = _enclosing_cell(draw_state)[0] is not None
    window = draw_state.parent_window
    fixed_window = _window_is_fixed(window)
    last_k = 2 * n_cols - 1
    if resizable and not pinned:
        # Hard floor, every frame: empty children and legacy state included.
        for c in layout:
            c[1] = max(c[1], float(MIN_COLUMN_WIDTH))
        if in_cell or fixed_window:
            target = max(bound, _min_left_target(layout, last_k, column_gap))
            if abs(layout[-1][0] + layout[-1][1] - target) > 0.5:
                _log_mut("glue last->bound", draw_state, last_k, target)
                if _apply_edge_drag(draw_state, layout, last_k, target,
                                    bound, column_gap, escalate=False):
                    draw_state.column_layout = layout
        if (fixed_window and not in_cell
                and "min_width" not in (window._kwargs or {})):
            # The columns' fully-compressed minimum IS the window's minimum
            # width: core_render's resize latch clamps corner/right-click
            # drags against min_width, so the window itself refuses to
            # shrink beyond its row - enforced through existing machinery
            # instead of fighting the resize latch frame by frame. Skipped
            # if the window has its own min_width kwarg (the wrapper
            # rewrites it from the kwarg every frame - the caller means it).
            window.min_width = snap_int(
                _min_left_target(layout, last_k, column_gap)
                + max(0.0, window.width - bound))

    origin = imgui.get_cursor_screen_pos()
    top = origin[1]

    # ----- edge drags -----
    # Every column's RIGHT edge is a resize handle; the fixed window's edges
    # are handles too (registered by the outermost container, fixed windows
    # only). All drag are per-frame incremental mutation: nothing latches,
    # nothing restores, a pushed column stays where it was pushed.
    active_edge = None
    mutated = False
    seen_handles = set()
    if resizable and not pinned:
        row_height = max(cell_heights.values()) if cell_heights else MIN_CELL_HEIGHT

        # No handle (and no line) for the LAST column's right edge: it IS the
        # window edge - moved by window resizing (corner drag, right-click,
        # the strips), with the glue above carrying the layout along.
        for i in range(n_cols - 1):
            # The grab zone is centered on the VISUAL divider line (column
            # edge plus half the gap) - rect and line must share the same x or
            # the handle feels offset.
            line_x = origin[0] + layout[i][0] + layout[i][1] + column_gap / 2
            rect = (line_x - EDGE_GRAB_WIDTH / 2, top,
                    line_x + EDGE_GRAB_WIDTH / 2, top + row_height)
            drag = draw_state.on_action("left_mouse_drag",
                                        view_id=f"col_edge_{i}",
                                        rect=rect, priority_delta=1)
            if not drag:
                continue
            active_edge = i
            seen_handles.add(f"col_{i}")
            inc = _drag_inc(draw_state, f"col_{i}", drag)
            if not inc:
                continue
            _log_mut(f"handle col_{i} inc={round(inc, 1)}", draw_state,
                     2 * i + 1, layout[i][0] + layout[i][1] + inc)
            # One call either direction: the dragged line moves, columns
            # pushes neighbouring edges rigidly, bounds escalate with the new
            # push (for a nested container that escalation IS the cell edge -
            # same-thing semantics hold at every level).
            mutated = _apply_edge_drag(
                draw_state, layout, 2 * i + 1,
                layout[i][0] + layout[i][1] + inc,
                draw_state.content_width, column_gap) or mutated

        # Left window edge strip (the right one is the last column's right).
        if fixed_window and not in_cell:
            win_left, win_top = window.abs_left, window.abs_top
            rect = (win_left - WINDOW_EDGE_GRAB / 2, win_top,
                    win_left + WINDOW_EDGE_GRAB / 2, win_top + window.height)
            drag = window.on_action("left_mouse_drag",
                                    view_id="col_window_left_edge", rect=rect)
            if drag:
                active_edge = "left"
                seen_handles.add("win_left")
                inc = _drag_inc(draw_state, "win_left", drag)
                if inc:
                    # Columns are referenced to this edge, so they ride it in
                    # the same frame - no compensation, nothing to readjust.
                    _log_mut(f"win left strip inc={round(inc, 1)}",
                             draw_state, -1, window.width - inc)
                    window.expanded = True
                    pos = window.window_pos or (0, 0)
                    window.window_pos = (pos[0] + inc, pos[1])
                    window.width = snap_int(window.width - inc)

        # Per-handle gesture baselines expire any any whose handle had no
        # event this frame, so a finished gesture never pollutes the next.
        totals = getattr(draw_state, "_drag_totals", None)
        if totals:
            for h in [h for h in totals if h not in seen_handles]:
                del totals[h]
        if active_edge is not None:
            draw_state.invalidate(note=Note(reason="edge drag", **_PUSH_NOTE))
            request_render()
        if mutated:
            draw_state.column_layout = layout

    # ----- cells -----
    row_bottom = top
    changed = False
    settled = True

    for idx, key in enumerate(keys):
        item = input_value[key]
        cell_x, cell_width = layout[idx]
        cell_height = max(cell_heights.get(key, MIN_CELL_HEIGHT), MIN_CELL_HEIGHT)
        if max_cell_height:
            cell_height = min(cell_height, max_cell_height)

        # Reposition before EVERY cell: a cache-skipped sibling leaves the
        # cursor wherever its blit ended, so column positions must never be
        # derived from the running cursor.
        imgui.set_cursor_screen_pos((snap_int(origin[0] + cell_x), snap_int(top)))

        # A width change re-renders the cell (its child hash moves) but a
        # cached middle tile would still blit-skip stale grandchildren -
        # cascade the change (invalidate_up goes down too) before redrawing.
        prev_ds = draw_state._children.get(idx)
        if prev_ds is not None and prev_ds.width != snap_int(cell_width):
            prev_ds.invalidate_up(max_depth=6,
                                  note=Note(reason="cell width change", **_PUSH_NOTE))

        item_kwargs = {"name": f"{key}", "align_header": False} | child_kwargs
        item_changed, out_value, cell_ds = draw_any(
            item, width=snap_int(cell_width), height=snap_int(cell_height),
            return_extras=True, **item_kwargs)

        if item_changed:
            changed = True
            if isinstance(input_value, (dict, list)):
                input_value[key] = out_value

        if cell_ds is not None:
            draw_state._children[idx] = cell_ds
            # Content height is observed even though the cell's height is
            # pinned; feed it back so next frame's pin matches the content.
            measured = max(MIN_CELL_HEIGHT,
                           int(cell_ds._observed_content_height +
                               cell_ds.header_height + cell_ds.footer_height))
            if cell_heights.get(key) != measured:
                cell_heights[key] = measured
                settled = False
                cell_ds.invalidate()

        row_bottom = max(row_bottom, top + cell_height)

    # ----- final visuals -----
    # Subtle lines on each column's right edge, brightened while dragged.
    # No hover highlight: a cached tile doesn't re-render on hover, so a
    # hover-dependent visual would run stale.
    if resizable and not pinned:
        draw_list = imgui.get_window_draw_list()
        for i in range(n_cols - 1):
            ex = origin[0] + layout[i][0] + layout[i][1] + column_gap / 2
            alpha = 0.5 if active_edge == i else 0.12
            draw_list.add_line(snap_int(ex), snap_int(top + 2),
                               snap_int(ex), snap_int(row_bottom - 2),
                               imgui.get_color_u32_rgba(1.0, 1.0, 1.0, alpha), 1.0)

    if not settled:
        draw_state.invalidate(note=Note(reason="cell height settle", **_PUSH_NOTE))
        request_render()

    # Commit the row's dimensions so the wrapper makes the container as
    # tall as its tallest cell (and as wide as its rightmost edge), not
    # wherever the last cell left the cursor.
    right_extent = max(c[0] + c[1] for c in layout)
    imgui.set_cursor_screen_pos((snap_int(origin[0] + right_extent),
                                 snap_int(row_bottom)))
    imgui.dummy(0, 0)

    return changed, input_value
