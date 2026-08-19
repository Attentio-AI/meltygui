from contextlib import contextmanager

import imgui

from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
from src.lsd.gl_gui.view.invalidation_tracker import Note

MIN_COLUMN_WIDTH = 60
MIN_ROW_HEIGHT = 20
EDGE_GRAB_WIDTH = 20.0
# Band color when the cursor is over the row (hard-coded for now).
HIGHLIGHT_TINT = (0,0,0, 1.0)

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


def _drag_edge(edges, k, target, walls=frozenset()):
    """Move edge k of the sorted list to `target`. Edges are independent
    objects: no other edge moves unless the moving edge (or one it already
    carried) closes to MIN_COLUMN_WIDTH — then it is carried, and the chain
    stops at the first edge with slack. Pulling away never drags anything
    along; only contact pushes.

    ``walls`` is a set of edge ids the cascade must NOT move. Contact stops
    dead at a wall: the *dragged* edge itself is clamped so the pile packs
    against the wall at MIN_COLUMN_WIDTH spacing instead of the chain
    shoving the wall along. Used by _solve_collisions to keep one FRAME
    edge from pushing the other (breaks the foreign-width feedback loop —
    see there); interior divider drags pass no walls, so a divider can
    still push a frame edge and slide/grow the window 1:1 with the
    cursor."""
    old = edges[k]["x"]
    if target == old:
        return
    if target > old:
        for m in range(k + 1, len(edges)):
            if id(edges[m]) in walls:
                target = min(target, edges[m]["x"] - (m - k) * MIN_COLUMN_WIDTH)
                break
        edges[k]["x"] = float(target)
        for m in range(k + 1, len(edges)):
            if id(edges[m]) in walls:
                break
            need = edges[m - 1]["x"] + MIN_COLUMN_WIDTH
            if edges[m]["x"] >= need:
                break
            edges[m]["x"] = need
    else:
        for m in range(k - 1, -1, -1):
            if id(edges[m]) in walls:
                target = max(target, edges[m]["x"] + (k - m) * MIN_COLUMN_WIDTH)
                break
        edges[k]["x"] = float(target)
        for m in range(k - 1, -1, -1):
            if id(edges[m]) in walls:
                break
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
    frame_ids = {id(e) for e in (getattr(window, "_frame_edges", None) or [])}
    moved = False
    for item in pending:
        # Optional third slot marks a CURSOR-DRIVEN drag (the right-drag
        # corner resize queues frame edges around it): those move 1:1 with the
        # cursor, so the foreign-width feedback loop the walls guard against
        # can't occur - a cursor drag on one frame edge is allowed to push
        # the other (slide the window), just like an interior divider.
        edge, target = item[0], item[1]
        cursor_driven = len(item) > 2 and bool(item[2])
        flat.sort(key=lambda e: e["x"])
        k = next((i for i, e in enumerate(flat) if e is edge), None)
        # TEMP edge-debug: snapshot the flat population per applied drag.
        # 'F' marks the window's frame edges, '*' the dragged edge - if the
        # contact push never reaches an F on the left, adoption is broken
        # (the row's far edge isn't the frame edge object).
        try:
            with open("/tmp/lsd_edge_debug.log", "a") as _f:
                snap = " ".join(
                    f"{'*' if e is edge else ''}"
                    f"{'F' if id(e) in frame_ids else ''}{e['x']:.0f}"
                    for e in flat)
                _f.write(f"drag k={k} target={target:.0f} "
                         f"win={getattr(window, 'name', window.id)} "
                         f"rows={list(window._edge_views.keys())} "
                         f"flat=[{snap}]\n")
        except Exception:
            pass
        if k is None or target == edge["x"]:
            continue
        if cursor_driven:
            walls = frozenset()
        else:
            walls = frame_ids - {id(edge)} if id(edge) in frame_ids else frozenset()
        _drag_edge(flat, k, target, walls=walls)
        moved = True
    return moved


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
    """Drop this host's registered edge row. Hosts that sometimes render
    WITHOUT columns (the code editor leaving a compare split) must call this
    on their column-less frames: window_edge_pass only evicts rows whose ds
    is CLOSED, and a host whose ds IS its window never closes while open —
    the stale row keeps feeding edge_under_cursor and the frame-edge solve,
    so right-drag resizes latch an invisible stale divider and the window
    width freezes. ColumnLayout re-registers on construction, so releasing
    before building columns later in the same frame is safe."""
    window = draw_state.parent_window or draw_state
    views = getattr(window, "_edge_views", None)
    if views is not None:
        views.pop(("row", draw_state.id), None)
    draw_state._column_container = False


def _has_columns_ancestor(draw_state):
    """True when another columns container sits between this view and its
    window. Used to decide window-frame adoption: a row with NO columns
    ancestor is the window's row — however many plain wrapper views
    (window bodies, code_file_io, …) sit in between — and adopts the
    window's frame edges; a row under another columns container gets its
    far edges from the enclosing cell instead (passed refs, or its own as
    the deep-nesting fallback). The climb is scoped to the parent window
    and guards the root ds's self-parent loop."""
    window = draw_state.parent_window
    node = draw_state._parent
    while node is not None and node is not node._parent:
        if node is window:
            break
        if getattr(node, "_column_container", False):
            return True
        node = node._parent
    return False


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
        # Purge ONLY our own "win_*" namespace: when a ColumnLayout host ds
        # IS the window (code review compare split), its int handles live in
        # this same dict - deleting them here resets the divider to the
        # baseline every frame, so every frame re-applies the FULL total_dx
        # and the edge flings away from the cursor.
        for h in [h for h in totals
                  if isinstance(h, str) and h.startswith("win_")
                  and h not in seen_handles]:
            del totals[h]

    moved = _solve_collisions(window)

    # TEMP edge-debug: rebase positions (only while something moved).
    if moved:
        try:
            with open("/tmp/lsd_edge_debug.log", "a") as _f:
                _f.write(f"  post-solve left={left['x']:.1f} "
                         f"right={right['x']:.1f} width={window.width} "
                         f"pos={window.window_pos}\n")
        except Exception:
            pass

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
            # A view whose box already disagrees with its tile (size_change)
            # is live-rendering by blit_offscreen as-is - the image and
            # cached masks are both refused mid-resize, and the image is
            # rebuilt once on mouse release. Invalidating it here would only
            # queue per-frame tile copies at the stale size. Only cells whose
            # own size is UNCHANGED while their edges move (interior-edge
            # drags) still need the explicit invalidate or they blit stale.
            # freeze_resize views are the exception both ways: mid-drag they
            # WANT to blit stale (that's the freeze contract - invalidating
            # would force a live body re-render every drag frame, while a
            # box-unchanged view never hits the frozen size-mismatch gate),
            # so their invalidate is deferred to the release case above.
            if not ds.size_change:
                if getattr(ds, "freeze_resize", False) and _drag_live():
                    _defer_freeze_settle(window, ds)
                else:
                    ds.invalidate(note=Note(reason="edge solve", **_NOTE))


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


def edge_under_cursor(window, cursor_x_window, cursor_y_abs, left=False):
    """The column edge a right-drag resize should move, given the drag-start
    cursor (``cursor_x_window`` in window coords — offset from window.abs_left
    — and ``cursor_y_abs`` in absolute screen coords).

    Returns the nearest edge dict strictly to the RIGHT of the cursor among
    the rows whose visible band vertically contains the cursor — i.e. the
    right edge of the INNERMOST column under the cursor. With ``left=True``
    (the ctrl+right-drag top-left corner resize) it's the nearest edge
    strictly to the LEFT instead. Every window registers its own frame edges
    (``window.id`` entry, full-window span), so a window with NO columns — or
    a drag in the outermost column — lands on the window's frame edge on
    that side and the frame resizes exactly as a plain resize.
    Returns None only if no edges exist yet."""
    _ensure_window_state(window)
    best, best_x = None, None
    for ds, edge_list in window._edge_views.values():
        clip = getattr(ds, "abs_clip_rect", None)
        if clip:
            top, bottom = clip[1], clip[3]
        else:
            top = ds.abs_top
            bottom = top + max(getattr(ds, "_edge_lines_height", 0.0),
                               MIN_ROW_HEIGHT)
        if not (top - 1 <= cursor_y_abs <= bottom + 1):
            continue
        for e in edge_list:
            if left:
                if e["x"] < cursor_x_window - 0.5 and (best_x is None or e["x"] > best_x):
                    best, best_x = e, e["x"]
            elif e["x"] > cursor_x_window + 0.5 and (best_x is None or e["x"] < best_x):
                best, best_x = e, e["x"]
    if best is None:
        fe = getattr(window, "_frame_edges", None)
        if fe:
            best = fe[0] if left else fe[1]
    return best


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
                 padding_y=None):
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
        # For edge-debug: log the adoption outcome whenever it changes.
        _adopt = (left_edge is not None
                  and left_edge is (getattr(window, "_frame_edges", None) or [None])[0],
                  _has_columns_ancestor(draw_state),
                  getattr(window, "_frame_edges", None) is not None,
                  window is draw_state)
        if getattr(draw_state, "_edge_debug_adopt", None) != _adopt:
            draw_state._edge_debug_adopt = _adopt
            try:
                with open("/tmp/lsd_edge_debug.log", "a") as _f:
                    _f.write(f"ctor n_cols={n_cols} adopted_frame={_adopt[0]} "
                             f"cols_ancestor={_adopt[1]} have_fe={_adopt[2]} "
                             f"ds_is_window={_adopt[3]} "
                             f"win={getattr(window, 'name', window.id)}\n")
            except Exception:
                pass

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
            seed_valid = extent > n_cols * float(MIN_COLUMN_WIDTH)
            extent = max(extent, n_cols * float(MIN_COLUMN_WIDTH))
            edges = _seed_edges(column_widths, n_cols, extent, base=base)
        owned = [True] * n_lines
        if left_edge is not None:
            edges[0] = left_edge
            owned[0] = False
        if right_edge is not None:
            edges[-1] = right_edge
            owned[-1] = False
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
                                            rect=rect, priority_delta=1)

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
                # baselines alone (same dict when the host ds IS the window).
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

            # The band spans the VISIBLE viewport: its bottom comes from the
            # host's clip, not from measured row height - a short row
            # still frames the whole pane, and there's no settle lag.
            if self.clip is not None:
                band_bottom = self.clip[3]
            else:
                band_bottom = self.top + max(draw_state.height or 0.0,
                                             MIN_ROW_HEIGHT)
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
                snap_int(band_bottom),
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

    def _bound_left(self, idx):
        return self._band_left() if idx == 0 else self.edges[idx]["x"]

    def _bound_right(self, idx):
        return (self._band_right() if idx == self.n_cols - 1
                else self.edges[idx + 1]["x"])

    def inner_width(self, idx):
        """Content width of column idx: the effective span minus padding."""
        return max(0.0, self._bound_right(idx) - self._bound_left(idx)
                   - 2 * self.padding)

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
        x0 = snap_int(self.win_x + left_b + pad)
        y0 = snap_int(self.top + pad_y)
        imgui.set_cursor_screen_pos((self.win_x + left_b + pad,
                                     self.top + pad_y))
        clip_h = height if height is not None else (self.draw_state.height
                                                    or MIN_ROW_HEIGHT)
        Core.melty.push_clip((x0, y0, x0 + snap_int(inner_w),
                              snap_int(self.top) + snap_int(clip_h)
                              - snap_int(pad_y)))
        try:
            yield inner_w
        finally:
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
            item_changed, out_value, cell_ds = draw_any(
                item, return_extras=True, **item_kwargs)
            cols.note_child(idx, cell_ds)  # border follows the child's corners
        if item_changed:
            changed = True
            if isinstance(input_value, (dict, list)):
                input_value[key] = out_value

    cols.finish()

    return changed, input_value
