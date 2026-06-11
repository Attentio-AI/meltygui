import imgui

from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
from src.lsd.gl_gui.view.invalidation_tracker import Note

MIN_COLUMN_WIDTH = 30
MIN_ROW_HEIGHT = 20
EDGE_GRAB_WIDTH = 20.0

_NOTE = dict(name="draw_columns", tint=(0.5, 0.8, 1.0))


class Columns(dict):
    """Marker dict: values render side by side (draw_columns is its default
    renderer), so column layouts nest by data:
    Columns({"a": ..., "b": Columns({...})})."""


# ---------------------------------------------------------------------------
# Edge model
#
# An EDGE is a dict with a single float - {"x": 123.0} - in WINDOW
# coordinates (offset from window.abs_left). Edges are passed around BY
# REFERENCE: a nested columns view does not create its own far edges, it
# adopts the two edge objects of its enclosing cell, so a shared boundary is
# always the same object in both views and can never drift apart.
#
# ALL edges live on the ROOT WINDOW's draw_state: every columns view in the
# tree upserts its edge list into window._edge_views, and drags from any
# views land on window._pending_drags. Once per frame - triggered by the
# first columns view that moves - the window resolves the queue in a
# single flat collision solve over every edge sorted by x. Consecutive
# edges always bound exactly one column of some view, so a flat
# MIN_COLUMN_WIDTH separation gives column contact chains across nesting
# levels for free. Net motion of the window-direct row's far edges drives
# the window frame itself (with a re-base to the lines hold their
# screen positions); contributors are invalidated and every view lines its
# cells up with the new edges in that same frame.
# ---------------------------------------------------------------------------


def resolve_column_widths(column_widths, n_cols, content_width):
    """Pixel width per column for ``n_cols`` columns in ``content_width``.

    Entries are pixels; None (or a missing entry — the list may be shorter
    than the column count) takes an equal share of whatever the sized columns
    leave over. Widths never drop below MIN_COLUMN_WIDTH.
    """
    available = max(0.0, float(content_width))
    spec = list(column_widths)[:n_cols] if column_widths else []
    spec += [None] * (n_cols - len(spec))

    fixed_total = sum(max(float(w), MIN_COLUMN_WIDTH)
                      for w in spec if w is not None)
    flex_count = sum(1 for w in spec if w is None)
    share = (max(MIN_COLUMN_WIDTH, (available - fixed_total) / flex_count)
             if flex_count else 0.0)

    return [share if w is None else max(float(w), MIN_COLUMN_WIDTH)
            for w in spec]


def _seed_edges(column_widths, n_cols, content_width, base=0.0):
    """Fresh edge dicts for n_cols columns: n_cols+1 lines accumulated from
    ``base`` (window coordinates)."""
    widths = resolve_column_widths(column_widths, n_cols, content_width)
    edges = [{"x": float(base)}]
    for w in widths:
        edges.append({"x": edges[-1]["x"] + w})
    return edges


def _ensure_window_state(window):
    if getattr(window, "_edge_views", None) is None:
        window._edge_views = {}
    if getattr(window, "_pending_drags", None) is None:
        window._pending_drags = []


def _all_edges(window):
    """Every edge registered on the window, deduped by identity (shared
    refs appear once)."""
    seen, flat = set(), []
    for _, edge_list in window._edge_views.values():
        for e in edge_list:
            if id(e) not in seen:
                seen.add(id(e))
                flat.append(e)
    return flat


def _drag_edge(edges, k, target):
    """Move edge k of the sorted list to `target`. Edges are independent
    objects: no other edge moves unless the moving edge (or one it already
    carried) closes to MIN_COLUMN_WIDTH — then it is carried, and the chain
    stops at the first edge with slack. Pulling away never drags anything
    along; only contact pushes."""
    old = edges[k]["x"]
    if target == old:
        return
    edges[k]["x"] = float(target)
    if target > old:
        for m in range(k + 1, len(edges)):
            need = edges[m - 1]["x"] + MIN_COLUMN_WIDTH
            if edges[m]["x"] >= need:
                break
            edges[m]["x"] = need
    else:
        for m in range(k - 1, -1, -1):
            need = edges[m + 1]["x"] - MIN_COLUMN_WIDTH
            if edges[m]["x"] <= need:
                break
            edges[m]["x"] = need


def _solve_collisions(window):
    """Apply every queued drag against the FULL edge population, one flat
    sorted-by-x list — contact chains cross view boundaries naturally.
    Returns True if anything moved. (No standing repair pass: edges only
    move while a drag is applied.)"""
    pending, window._pending_drags = window._pending_drags, []
    if not pending:
        return False
    flat = _all_edges(window)
    moved = False
    for edge, target in pending:
        flat.sort(key=lambda e: e["x"])
        k = next((i for i, e in enumerate(flat) if e is edge), None)
        if k is None or target == edge["x"]:
            continue
        _drag_edge(flat, k, target)
        moved = True
    return moved


def _window_direct(draw_state):
    """True when this columns view is the direct content of its window —
    its frame IS the window frame."""
    window = draw_state.parent_window
    return window is not None and draw_state._parent is window


def window_edge_pass(window):
    """Every window's left/right FRAME edges as draggable edge objects —
    run once per frame per window (idempotent; called from core_render's
    melty-window path for ALL windows, and from draw_columns as a fallback).

    The window owns two edge dicts seeded at [0, width] (window coords) and
    registered in the same solve as every column edge. Native resizes
    (corner drag, right-click draw, programmatic width writes) are folded
    in through the invariant right.x == window.width: any foreign width
    change queues a drag of the right frame edge, so it runs the SAME
    collision solve and frame and lines can never desync. After the solve
    the window lines up with its edges exactly like cells do: left edge
    off 0 → window_pos slides + everything re-bases; right edge off width
    → width follows."""
    from src.lsd.gl_gui.melty import Melty
    if getattr(window, "_edges_frame", None) == Melty.frame_count:
        return
    window._edges_frame = Melty.frame_count

    if not getattr(window, "expanded", True) or not window.width or not window.height:
        return
    _ensure_window_state(window)

    fe = getattr(window, "_frame_edges", None)
    if not fe:
        fe = [{"x": 0.0}, {"x": float(window.width)}]
        window._frame_edges = fe
    window._edge_views[window.id] = (window, fe)
    left, right = fe

    for key, (ds, _) in list(window._edge_views.items()):
        if ds is not window and getattr(ds, "closed", False):
            del window._edge_views[key]

    # Foreign width change since last pass → the right frame edge follows
    # it in the collision solve.
    if abs(right["x"] - window.width) > 0.5:
        window._pending_drags.append((right, float(window.width)))

    # The pile can never out-compress the window: keep min_width at the
    # fully-compressed span so a native shrink always completes its
    # collision pass instead of fighting the resize latch. Raise-only,
    # re-stamped every frame (the wrapper rewrites min_width from resolved
    # contents every frame).
    need = snap_int(left["x"]
                    + MIN_COLUMN_WIDTH * max(1, len(_all_edges(window)) - 1))
    if (window.min_width or 0) < need:
        window.min_width = need

    # Frame edge drag handles, full window height.
    win_x, win_y = window.abs_left, window.abs_top
    active = None
    seen_handles = set()
    for k, e in enumerate(fe):
        x = win_x + e["x"]
        rect = (x - EDGE_GRAB_WIDTH / 2, win_y,
                x + EDGE_GRAB_WIDTH / 2, win_y + window.height)
        drag = window.on_action("left_mouse_drag", view_id=f"win_edge_{k}",
                                rect=rect, priority_delta=1)
        if not drag:
            continue
        active = k
        seen_handles.add(f"win_{k}")
        inc = _drag_inc(window, f"win_{k}", drag)
        if inc:
            window._pending_drags.append((e, e["x"] + inc))
    totals = getattr(window, "_drag_totals", None)
    if totals:
        for h in [h for h in totals if h not in seen_handles]:
            del totals[h]

    moved = _solve_collisions(window)

    # Line the WINDOW up with its frame edges: the same rule cells use.
    d_left = left["x"]
    if d_left:
        pos = window.window_pos or (0, 0)
        window.window_pos = (pos[0] + d_left, pos[1])
        window.width = snap_int(window.width - d_left)
        for e in _all_edges(window):
            e["x"] -= d_left
        moved = True
    if abs(right["x"] - window.width) > 0.5:
        window.width = snap_int(right["x"])
        moved = True
    # Kill snap drift so the invariant check doesn't re-fire every frame.
    right["x"] = float(window.width)

    if moved:
        for ds, _ in window._edge_views.values():
            ds.invalidate(note=Note(reason="edge solve", **_NOTE))
        request_render()

    # Frame edge lines, full height, in this window's layer.
    draw_list = imgui.get_window_draw_list()
    for k, e in enumerate(fe):
        ex = win_x + e["x"]
        alpha = 0.6 if active == k else 0.2
        draw_list.add_line(snap_int(ex), snap_int(win_y + 2),
                           snap_int(ex), snap_int(win_y + window.height - 2),
                           imgui.get_color_u32_rgba(1.0, 1.0, 1.0, alpha), 1.0)


def _grab_zone(edges, k):
    """Horizontal grab span for edge k of this view's list: EDGE_GRAB_WIDTH
    centered on the line, but split at the midpoint toward each neighbouring
    edge so adjacent zones never overlap."""
    x = edges[k]["x"]
    lo, hi = x - EDGE_GRAB_WIDTH / 2, x + EDGE_GRAB_WIDTH / 2
    if k > 0:
        lo = max(lo, (edges[k - 1]["x"] + x) / 2)
    if k < len(edges) - 1:
        hi = min(hi, (x + edges[k + 1]["x"]) / 2)
    return lo, hi


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
    n_lines = n_cols + 1

    # ----- window anchor: all edges live on the root window -----
    window = draw_state.parent_window or draw_state
    _ensure_window_state(window)
    window_edge_pass(window)  # idempotent; usually already called via the wrapper
    win_x = window.abs_left

    origin = imgui.get_cursor_screen_pos()
    top = origin[1]

    # ----- own edges: load / seed, then adopt the cell container's far
    # edge objects so shared boundaries are shared identity. The container
    # is the enclosing cell - or the WINDOW itself for a window-direct row,
    # whose far edges are the window's frame edges -----
    left_ref = kwargs.get("left_edge")
    right_ref = kwargs.get("right_edge")
    if left_ref is None and right_ref is None and _window_direct(draw_state):
        frame_edges = getattr(window, "_frame_edges", None)
        if frame_edges:
            left_ref, right_ref = frame_edges

    stored = column_edges if isinstance(column_edges, list) else []
    ok = (len(stored) == n_lines and
          all(isinstance(e, dict) and "x" in e for e in stored))
    seed_valid = True
    if ok:
        edges = list(stored)
    else:
        if left_ref is not None and right_ref is not None:
            base, extent = left_ref["x"], right_ref["x"] - left_ref["x"]
        else:
            base = origin[0] - win_x
            extent = float(draw_state.content_width or 0)
        # A seed taken before the parent has laid out (content_width ~0)
        # must stay TRANSIENT: render with it this frame but don't persist,
        # so a later frame re-seeds at the real width instead of locking in
        # an all-minimum-width pile.
        seed_valid = extent > n_cols * float(MIN_COLUMN_WIDTH)
        extent = max(extent, n_cols * float(MIN_COLUMN_WIDTH))
        edges = _seed_edges(column_widths, n_cols, extent, base=base)
    owned = [True] * n_lines
    if left_ref is not None:
        edges[0] = left_ref
        owned[0] = False
    if right_ref is not None:
        edges[-1] = right_ref
        owned[-1] = False
    if (not ok or any(a is not b for a, b in zip(stored, edges))):
        # Stamp so auto state persists the list; in-place x mutations on the
        # edges persist without re-stamping.
        draw_state.column_edges = edges

    window._edge_views[draw_state.id] = (draw_state, edges)

    # ----- drag handles for OWNED edges (foreign boundary edges already have
    # the enclosing view's handles on the same line) -----
    active_edge = None
    if resizable:
        height = max(getattr(draw_state, "_edge_lines_height", 0.0),
                     MIN_ROW_HEIGHT)
        seen_handles = set()
        for k in range(n_lines):
            if not owned[k]:
                continue
            lo, hi = _grab_zone(edges, k)
            rect = (win_x + lo, top, win_x + hi, top + height)
            drag = draw_state.on_action("left_mouse_drag",
                                        view_id=f"col_edge_{k}",
                                        rect=rect, priority_delta=1)
            if not drag:
                continue
            active_edge = k
            seen_handles.add(k)
            inc = _drag_inc(draw_state, k, drag)
            if inc:
                window._pending_drags.append((edges[k], edges[k]["x"] + inc))

        totals = getattr(draw_state, "_drag_totals", None)
        if totals:
            for h in [h for h in totals if h not in seen_handles]:
                del totals[h]
        if active_edge is not None:
            draw_state.invalidate(note=Note(reason="edge drag", **_NOTE))
            request_render()

    # ----- cells: lined up with their edges; nested Columns get the cell's
    # edge objects by reference -----
    changed = False
    for idx, key in enumerate(keys):
        e_left, e_right = edges[idx], edges[idx + 1]
        imgui.set_cursor_screen_pos((win_x + e_left["x"], origin[1]))
        content_width = e_right["x"] - e_left["x"]
        Core.melty.push_clip((snap_int(win_x + e_left["x"]), snap_int(origin[1]),
                              snap_int(win_x + e_left["x"]) + snap_int(content_width),
                              snap_int(origin[1]) + snap_int(draw_state.height)))
        item_kwargs = {"name": f"{key}", "align_header": False,
                       "width": content_width} | child_kwargs
        if isinstance(input_value[key], Columns):
            item_kwargs |= {"left_edge": e_left, "right_edge": e_right}
        item_changed, out_value = draw_any(input_value[key], **item_kwargs)
        if item_changed:
            changed = True
            if isinstance(input_value, (dict, list)):
                input_value[key] = out_value

        Core.melty.pop_clip()

    bottom = imgui.get_cursor_screen_pos()[1]
    draw_state._edge_lines_height = max(bottom - top, MIN_ROW_HEIGHT)

    # ----- the lines (owned only; foreign lines are drawn by the owner) --
    draw_list = imgui.get_window_draw_list()
    line_bottom = top + draw_state._edge_lines_height + 300
    for k in range(n_lines):
        if not owned[k]:
            continue
        ex = win_x + edges[k]["x"]
        alpha = 0.6 if active_edge == k else 0.2
        draw_list.add_line(snap_int(ex), snap_int(top + 2),
                           snap_int(ex), snap_int(line_bottom - 2),
                           imgui.get_color_u32_rgba(1.0, 1.0, 1.0, alpha), 1.0)

    return changed, input_value
