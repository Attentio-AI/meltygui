"""Tiled window manager: a Blender-style tree of splits drawn flat.

The layout is a TREE of ``Split`` nodes (an axis + children) ending in
``Tile`` leaves. Plain functions lay out the split tree on one host draw_state;
each leaf's editor is a normal render_func with independent injected state.
One ``Split`` is ONE ``ColumnLayout`` (axis "x") or
``RowLayout`` (axis "y") call over its children; a child of the OPPOSITE
axis is the only thing that recurses, and a Split never holds a Split of
its own axis (``normalize`` keeps that invariant, mirroring Blender's
areas: same-axis neighbours are siblings in one flat list).

Every node renders into a FRAME of four shared edge dicts
``(left, right, top, bottom)`` — the enclosing cell's pair on the split's
axis and the pass-through pair on the other — so a Rows in a Columns cell
collides with the outer rows exactly like draw_columns / draw_rows do.
The interior edges of a Split live IN THE TREE (``node.edges``, the
n-1 dicts between its children, window coords); its far edges are never
stored, they are adopted by reference from the frame every frame. The
layouts register on the host window with ``key=path`` and ``band=`` (see
columns.py), so one draw_state hosts the whole tree and every divider,
including the window frame, drags through the same collision solve.

Data ops (``split_tile`` / ``join_tiles`` / ``normalize``) are pure list
edits on the tree; ``resolve_frames`` walks the stored edges from a root
frame without rendering, so they can place a new edge at a midpoint.

Gestures (Blender's): a left-drag from a tile's CORNER inward splits it —
a mostly-horizontal drag cuts a vertical divider (axis "x"), a vertical
one a horizontal divider (axis "y"); the new tile sits on the corner's
side and the new edge follows the cursor for the rest of the drag through
the same collision solve as any divider (``tile_corner_gesture``). The
live gesture lives in the injected ``TileManagerState``. An outward drag
previews joining a leaf sibling across its whole edge; release removes
that neighbour and expands the dragged-from tile. Returning inside the
source or leaving the neighbour cancels the join.
"""
import meltygui_imgui as imgui

from meltygui.model.tile_model import Tile
from meltygui.model.tile_model import Split

import meltygui.core.input.mouse_cursor as mouse_cursor
from meltygui.hdr_color import pack_color
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.cache.tile_marks import snap_int
from meltygui.core.cache.tile_marks import add_shadow
from meltygui.core.layout.column_core import ColumnLayout
from meltygui.core.layout.column_core import RowLayout
from meltygui.core.layout.column_core import MIN_COLUMN_WIDTH
from meltygui.core.layout.column_core import MIN_ROW_HEIGHT
from meltygui.core.layout.column_core import _ensure_window_state
from meltygui.core.layout.column_core import _pending
from meltygui.core.layout.column_core import _views
from meltygui.core.layout.column_core import _specs
from meltygui.core.layout.column_core import _bands
from meltygui.core.layout.column_core import frame_edges
from meltygui.core.layout.column_core import layout_window
from meltygui.core.layout.column_core import _drag_inc
from meltygui.core.layout.column_core import _grab_zone
from meltygui.core.windowing.os_frame import _any_button_down
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.cache.invalidation_tracker import Note

_NOTE = dict(name="draw_tiles", tint=(0.55, 0.85, 0.45))


@no_save("gesture")
class TileManagerState(DictConversion):
    """Injected state of a tile-manager host (declare
    ``tile_state: TileManagerState = None`` on the render_func). ``gesture``
    holds the captured corner and either the new split edge or a join
    preview target, until the button is released. It is never persisted."""

    def __init__(self):
        super().__init__()
        self.gesture = None
        self.content_top = {"y": 0.0}
        self._link_endpoints = {}


# Split/join handles: leave bottom-left clear for the tile selector.
# Each entry is (name, on the left?, on the top?).
CORNERS = (("nw", True, True), ("ne", False, True), ("se", False, False))


def other_axis(axis):
    return "y" if axis == "x" else "x"


def frame_pair(frame, axis):
    """The frame's two edges on ``axis``: (left, right) or (top, bottom)."""
    left, right, top, bottom = frame
    return (left, right) if axis == "x" else (top, bottom)


def child_frame(frame, axis, near, far):
    """The frame a child gets: the parent's frame with the ``axis`` pair
    replaced by the child's cell edges, the other pair passed through."""
    left, right, top, bottom = frame
    if axis == "x":
        return near, far, top, bottom
    return left, right, near, far


def full_edges(node, frame):
    """A Split's complete edge list — far edges from the frame, interior
    edges from the tree — the list its layout is built over."""
    near, far = frame_pair(frame, node.axis)
    return [near, *node.edges, far]


def node_at(tree, path):
    """The node at ``path`` (a tuple of child indices from the root)."""
    node = tree
    for index in path:
        node = node.children[index]
    return node


def walk(tree, path=()):
    """Every ``(path, node)`` of the tree, parents before children, seeded
    or not."""
    yield path, tree
    if isinstance(tree, Split):
        for index, child in enumerate(tree.children):
            yield from walk(child, path + (index,))


def resolve_frames(tree, root_frame, path=()):
    """Walk the tree WITHOUT rendering, yielding ``(path, node, frame)``
    for every node from the stored edges alone. A Split whose interior
    edges have not been seeded yet (fewer than n-1) is yielded, but its
    children are skipped — their frames don't exist until a render seeds
    them."""
    yield path, tree, root_frame
    if not isinstance(tree, Split):
        return
    children = tree.children
    if len(tree.edges) != max(len(children) - 1, 0):
        return
    edges = full_edges(tree, root_frame)
    for index, child in enumerate(children):
        frame = child_frame(root_frame, tree.axis, edges[index], edges[index + 1])
        yield from resolve_frames(child, frame, path + (index,))


def frame_rect(frame, window):
    """A frame as an absolute screen rect ``(x0, y0, x1, y1)``."""
    left, right, top, bottom = frame
    return (window.abs_left + left["x"], window.abs_top + top["y"],
            window.abs_left + right["x"], window.abs_top + bottom["y"])


def tile_rect(frame, draw_state, gap=4.0):
    """A tile's painted box (absolute, ints): its frame inset by ``gap``
    / 2 and clipped to the host's visible box, or None when nothing of it
    shows."""
    window = layout_window(draw_state)
    x0, y0, x1, y1 = frame_rect(frame, window)
    clip = Core.melty.get_clip_rect() or draw_state.abs_clip_rect
    if getattr(draw_state, "closable", False):
        content = draw_state.get_content_rect()
        clip = content if clip is None else (
            max(clip[0], content[0]), max(clip[1], content[1]),
            min(clip[2], content[2]), min(clip[3], content[3]))
    half = gap / 2
    x0, y0 = snap_int(x0 + half), snap_int(y0 + half)
    x1, y1 = snap_int(x1 - half), snap_int(y1 - half)
    if clip is not None:
        x0, y0 = max(x0, clip[0]), max(y0, clip[1])
        x1, y1 = min(x1, clip[2]), min(y1, clip[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def corner_rect(rect, on_left, on_top, size):
    """The ``size`` px square in one corner of ``rect``."""
    x0, y0, x1, y1 = rect
    cx0 = x0 if on_left else x1 - size
    cy0 = y0 if on_top else y1 - size
    return cx0, cy0, cx0 + size, cy0 + size


def corner_view_id(tile, corner):
    """The on_action view id of one corner grip; keyed on the tile OBJECT
    because a split shifts the path while the drag is still captured."""
    return f"tile_corner_{id(tile)}_{corner}"


def corner_triangle(grip, on_left, on_top, size, inset):
    """The three points of the grip's handle: a right triangle tucked into
    the tile's corner with its hypotenuse facing inward, legs ``size`` px,
    ``inset`` px off the tile's edges."""
    gx0, gy0, gx1, gy1 = grip
    if on_left:
        ax, bx = gx0 + inset, gx0 + inset + size
    else:
        ax, bx = gx1 - inset, gx1 - inset - size
    if on_top:
        ay, by = gy0 + inset, gy0 + inset + size
    else:
        ay, by = gy1 - inset, gy1 - inset - size
    return (ax, ay), (bx, ay), (ax, by)


def hover_shown(draw_state, rect, owns_gesture, view_id=None):
    """Whether a drag handle over ``rect`` lights up: always while it owns
    the captured press or live gesture, on hover only while NO mouse button
    is held. A right-drag (or any other button's drag) sweeping across handles must
    not light them up as the cursor passes."""
    if owns_gesture:
        return True
    if view_id is not None and draw_state.is_drag_captured(view_id=view_id):
        return True
    if _any_button_down():
        return False
    return draw_state.hover_eligible(rect=rect)


def draw_tile(tile, frame, draw_state, path=(), tree=None, root_frame=None,
              tile_state=None, gap=4.0):
    """Paint one leaf — no background (``draw_split_dividers`` draws the
    lines between tiles), a shadow lifting the tile off the host, and a
    small triangle in each corner (brighter when hovered or dragged) that
    marks the split / join grip — and run the corner split gesture when
    ``tree`` / ``root_frame`` / ``tile_state`` are given (``draw_tiles``
    passes them). Returns True when the gesture changed the tree."""
    # [tint=(1.0, 0.8, 0.3)]
    # Change the corner grips here: the grab zone size, the visible
    # triangle's leg length and inset from the tile edge, and its resting /
    # hovered (or dragging) colour.
    corner_size = 14.0
    corner_triangle_size = 9.0
    corner_triangle_inset = 1.0
    corner_color = (1.0, 1.0, 1.0, 0.16)
    corner_hover_color = (1.0, 1.0, 1.0, 0.55)
    # Change the tile lift here: how far each tile rises above the host
    # (its shadow spread) and the rounding of that shadow.
    tile_shadow_offset = 1.0
    tile_shadow_radius = 4.0

    rect = tile_rect(frame, draw_state, gap=gap)
    if rect is None:
        return False
    draw_list = imgui.get_window_draw_list()
    x0, y0, x1, y1 = rect
    add_shadow((x0, y0, x1 - x0, y1 - y0), offset=tile_shadow_offset,
               corner_radius=tile_shadow_radius)
    # A renderer may paint its whole tile (the view itself is clipped to the
    # content box inside the grips and above the picker): set
    # `renderer.tile_background = lambda tile: packed colour | None`.
    background = getattr(tile.render_func, "tile_background", None)
    fill = background(tile) if background is not None else None
    if fill is not None:
        draw_list.add_rect_filled(x0, y0, x1, y1, fill, tile_shadow_radius)

    changed = False
    gesture = tile_state.gesture if tile_state is not None else None
    for name, on_left, on_top in CORNERS:
        grip = corner_rect(rect, on_left, on_top, corner_size)
        owns = gesture is not None and gesture["corner"] == corner_view_id(tile, name)
        hot = hover_shown(draw_state, grip, owns, view_id=corner_view_id(tile, name))
        (ax, ay), (bx, by), (cx, cy) = corner_triangle(
            grip, on_left, on_top, corner_triangle_size, corner_triangle_inset)
        draw_list.add_triangle_filled(
            ax, ay, bx, by, cx, cy,
            pack_color(*(corner_hover_color if hot else corner_color)))
        if tree is not None and root_frame is not None and tile_state is not None:
            changed = tile_corner_gesture(
                tile, frame, draw_state, path, tree, root_frame, tile_state,
                name, on_left, on_top, grip) or changed
    return changed


def tile_corner_gesture(tile, frame, draw_state, path, tree, root_frame,
                        tile_state, corner, on_left, on_top, grip):
    """One corner's split / join gesture. The grip is an ``on_action`` sub-rect
    above the edge grab zones (the two overlap at a tile's corners).
    A drag that travels ``split_threshold`` px INTO the tile splits it:
    the dominant direction picks the axis, the new tile goes on the
    corner's side, the new edge lands under the cursor. From then until
    release every frame queues a cursor-driven drag of that edge on the
    window's solve, so it follows the pointer and pushes neighbours like
    any divider. The view id keys on the tile OBJECT, not its path — the
    split shifts the path while the drag is still captured. Returns True
    on the frame the tree changed."""
    # [tint=(1.0, 0.8, 0.3)]
    split_threshold = 8.0

    view_id = corner_view_id(tile, corner)
    draw_state.on_action("left_mouse_down", view_id=view_id, rect=grip,
                         priority_delta=2, cursor=mouse_cursor.RESIZE_ALL)
    drag = draw_state.on_action("left_mouse_drag", view_id=view_id, rect=grip,
                                priority_delta=2)
    gesture = tile_state.gesture
    window = layout_window(draw_state)

    if gesture is not None and gesture["corner"] == view_id:
        if gesture.get("kind") == "join":
            if drag is None:
                tile_state.gesture = None
                target = gesture["target"]
                target_path = locate(tree, target) if target is not None else None
                source_path = locate(tree, tile)
                if target_path is not None and source_path is not None:
                    direction = -1 if target_path[-1] > source_path[-1] else +1
                    join_tiles(tree, target_path, direction)
                    return True
                return False
            tile_state.gesture = {**gesture, "target": join_target(tree, path, root_frame, window, drag)}
            return False
        if drag is None:
            tile_state.gesture = None
            draw_state._drag_totals.pop(view_id, None)
            return False
        # Use the same relative increments as every divider. An absolute cursor
        # target re-applied at a wall accumulates the blocked distance on every
        # frame of sticky replay, even when the hand is stationary.
        axis = gesture["axis"]
        increment = _drag_inc(draw_state, view_id, drag,
                              total="total_dx" if axis == "x" else "total_dy")
        lag = gesture.get("lag", 0.0)
        if lag and increment:
            # The hand still leads the edge (a clamped split): motion toward
            # the edge closes the gap, motion away widens it, and only the
            # overshoot past the edge moves it.
            lag += increment
            if (lag > 0) == (gesture["lag"] > 0):
                gesture["lag"], increment = lag, 0.0
            else:
                gesture["lag"], increment = 0.0, lag
        if increment:
            _ensure_window_state(window)
            edge = gesture["edge"]
            _pending(window, axis).append((edge, edge[axis] + increment, True))
            draw_state.invalidate(note=Note(reason="tile split drag", **_NOTE))
        return False

    if drag is None or gesture is not None:
        return False
    dx, dy = drag.total_dx, drag.total_dy
    if max(abs(dx), abs(dy)) < split_threshold:
        return False
    axis = "x" if abs(dx) >= abs(dy) else "y"
    inward = (dx > 0) == on_left if axis == "x" else (dy > 0) == on_top
    if not inward:
        target = join_target(tree, path, root_frame, window, drag)
        if target is not None:
            tile_state.gesture = {"kind": "join", "corner": view_id, "target": target}
        return False
    before = on_left if axis == "x" else on_top
    near, far = frame_pair(frame, axis)
    floor = MIN_COLUMN_WIDTH if axis == "x" else MIN_ROW_HEIGHT
    along = (drag.x - window.abs_left) if axis == "x" else (drag.y - window.abs_top)
    lo, hi = near[axis] + floor, far[axis] - floor
    if hi < lo:
        return False                             # too small to split
    at = min(max(float(along), lo), hi)
    new_path = split_tile(tree, path, axis, root_frame, at=at, before=before)
    new_node = node_at(tree, new_path[:-1])
    edge_index = new_path[-1] if before else new_path[-1] - 1
    tile_state.gesture = {"corner": view_id, "axis": axis,
                          "edge": new_node.edges[edge_index],
                          "new_tile": node_at(tree, new_path)}
    _drag_inc(draw_state, view_id, drag,
              total="total_dx" if axis == "x" else "total_dy")
    # The edge lands where the minimum let it, not necessarily under the
    # pointer (a split started within a cell's floor of its far edge). The
    # hand's lead is remembered: the edge waits until the hand reaches it
    # and follows from there, instead of moving at that fixed distance for
    # the rest of the drag - pushing its neighbours while the pointer was
    # nowhere near them (09-21). Never a jump to the pointer: an edge moves
    # at the hand's speed or not at all (diagnostics/edge_motion_guard).
    tile_state.gesture["lag"] = along - at
    return True


def join_target(tree, path, root_frame, window, drag):
    """Leaf sibling under the pointer across a whole shared edge.

    Join a subdivided neighbour's own tiles first. Moving back into the
    source or leaving the neighbour cancels the preview.
    """
    if not path:
        return None
    parent = node_at(tree, path[:-1])
    frames = {path: frame for path, _node, frame in resolve_frames(tree, root_frame)}
    for direction in (-1, +1):
        neighbour = path[-1] + direction
        if not 0 <= neighbour < len(parent.children):
            continue
        candidate = parent.children[neighbour]
        if isinstance(candidate, Split):
            continue
        frame = frames.get(path[:-1] + (neighbour,))
        if frame is None:
            continue
        left, top, right, bottom = frame_rect(frame, window)
        if left < drag.x < right and top < drag.y < bottom:
            return candidate
    return None


def draw_join_preview(tree, root_frame, draw_state, tile_state):
    gesture = tile_state.gesture
    if gesture is None or gesture.get("kind") != "join" or gesture["target"] is None:
        return
    # [tint=(1.0, 0.55, 0.15)]
    preview_color = (1.0, 0.55, 0.15, 0.42)
    for _path, tile, frame in resolve_frames(tree, root_frame):
        if tile is gesture["target"]:
            rect = tile_rect(frame, draw_state)
            if rect is not None:
                draw_list = imgui.get_window_draw_list()
                draw_list.add_rect_filled(*rect, pack_color(*preview_color))
                draw_list.add_text(rect[0] + 8, rect[1] + 28,
                                   pack_color(1.0, 1.0, 1.0, 1.0), "Release to merge")
            break


def draw_split_dividers(layout, axis, frame, draw_state, tile_state=None, path=()):
    """Draw one Split's interior edges as lines across its band: black at
    rest, highlighted while the cursor is in that edge's grab zone (the
    same zone the layout drags from) with no button held, or while the
    edge itself is pressed or dragged, by its grab zone or by the corner split
    gesture that created it (``tile_state.gesture["edge"]``). Another
    handle's drag (a right-drag, a corner split elsewhere) passing over the
    zone does not light it."""
    # [tint=(1.0, 0.8, 0.3)]
    # Change the divider look here: resting / hovered colour and line width.
    divider_color = (0.0, 0.0, 0.0, 1.0)
    divider_hover_color = (0.25, 0.25, 0.25, 1.0)
    divider_thickness = 2.0

    window = layout_window(draw_state)
    x0, y0, x1, y1 = frame_rect(frame, window)
    origin = window.abs_left if axis == "x" else window.abs_top
    edges = layout.edges
    gesture = tile_state.gesture if tile_state is not None else None
    gesture_edge = gesture.get("edge") if gesture is not None else None
    draw_list = imgui.get_window_draw_list()
    for k in range(1, len(edges) - 1):
        lo, hi = _grab_zone(edges, k, axis=axis)
        line = snap_int(origin + edges[k][axis])
        if axis == "x":
            grab = (origin + lo, y0, origin + hi, y1)
            ends = (line, y0, line, y1)
        else:
            grab = (x0, origin + lo, x1, origin + hi)
            ends = (x0, line, x1, line)
        dragging = layout.active_edge == k or edges[k] is gesture_edge
        handle = "col_edge" if axis == "x" else "row_edge"
        hot = hover_shown(draw_state, grab, dragging, view_id=f"{handle}_{path}_{k}")
        draw_list.add_line(*ends,
                           pack_color(*(divider_hover_color if hot else divider_color)),
                           divider_thickness)


def draw_tile_node(node, frame, draw_state, path=(), tree=None,
                   root_frame=None, tile_state=None, gap=4.0):
    """Render one node into ``frame``. A Tile paints (and runs its corner
    gestures when ``tree`` / ``root_frame`` / ``tile_state`` are given); a
    Split builds ONE layout over its children (columns for axis "x", rows
    for "y") keyed by ``path`` so many layouts share the host draw_state,
    writes the seeded / clamped interior edges back into the tree, and
    recurses into each child with that child's cell edges as its frame.
    Returns True when a gesture changed the tree — the children are
    snapshotted first, so a split mid-walk renders on the next frame."""
    if not isinstance(node, Split):
        return draw_tile(node, frame, draw_state, path=path, tree=tree,
                         root_frame=root_frame, tile_state=tile_state, gap=gap)
    children = node.children
    if not children:
        return False
    axis = node.axis
    near, far = frame_pair(frame, axis)
    across = frame_pair(frame, other_axis(axis))
    stored = full_edges(node, frame)
    if axis == "x":
        layout = ColumnLayout(draw_state, len(children), column_edges=stored,
                              left_edge=near, right_edge=far, band=across,
                              key=path, persist=False, padding=0.0,
                              border_color=None)
    else:
        layout = RowLayout(draw_state, len(children), row_edges=stored,
                           top_edge=near, bottom_edge=far, band=across,
                           key=path, persist=False, padding=0.0,
                           border_color=None)
    if layout.seed_valid:
        interior = layout.edges[1:-1]
        if any(a is not b for a, b in zip(node.edges, interior)) \
                or len(node.edges) != len(interior):
            node.edges = interior
    edges = layout.edges
    draw_split_dividers(layout, axis, frame, draw_state, tile_state=tile_state, path=path)
    changed = False
    for index, child in enumerate(list(children)):
        if draw_tile_node(child, child_frame(frame, axis, edges[index], edges[index + 1]),
                          draw_state, path + (index,), tree=tree,
                          root_frame=root_frame, tile_state=tile_state, gap=gap):
            changed = True
            break                                # the tree moved under us
    return changed


def tree_edge_ids(tree):
    """The ids of every interior edge dict stored in the tree."""
    return {id(edge) for _path, node in walk(tree)
            if isinstance(node, Split) for edge in node.edges}


def retire_layouts(window, draw_state, dead=frozenset()):
    """Drop the registrations a topology change left behind, before the
    next collision solve: the host's keyed layouts (``key=path``: paths
    and axes moved, they re-register at their new paths next frame) and
    every layout whose edge list or band holds an edge that left the tree
    (``dead``: ids of the removed dividers). The latter are the layouts a
    tile RENDERER built over its tile's frame (a chat tile's columns, a
    code editor's compare split - ``layout_frame`` adopts the tile's edge
    dicts by reference): the joined-away tile's view never renders again
    and never closes, so the window's own eviction (closed views only)
    never reached it, and its cells kept linking the dead divider to the
    surviving edges in every solve (Lukas 09-21: old edges leaking into
    the collisions after a join)."""
    for axis in ("x", "y"):
        views, specs, bands = _views(window, axis), _specs(window, axis), _bands(window, axis)
        for key, (owner, edges) in list(views.items()):
            keyed = isinstance(key, tuple) and len(key) == 3 and owner is draw_state
            if keyed or any(id(e) in dead for e in edges) \
                    or any(id(e) in dead for e in bands.get(key) or ()):
                del views[key]
                specs.pop(key, None)
                bands.pop(key, None)


def draw_tiles(tree, draw_state, tile_state=None, gap=4.0,
               multi_instance_renderers=(), content_top=None):
    """Render a whole tile tree over the host window's frame: the root
    adopts the window's four frame edges (``frame_edges``), so the window
    frame and every divider move through one collision solve. Call from a
    render_func body with its injected ``tile_state`` (without one the
    tiles draw and their edges drag, but corners don't split). Returns
    True when the layout, editor selection or editor content changed.
    ``multi_instance_renderers`` is supplied by the hosting render_func's
    injected parameter of the same name. ``content_top`` optionally reserves
    space above the tiles (an absolute screen y coordinate). The root must be a Split."""
    window = layout_window(draw_state)
    root_frame = frame_edges(window)
    if content_top is not None:
        # Keep the body boundary stable while menus above it change height.
        top = tile_state.content_top if tile_state is not None else {}
        top["y"] = content_top - window.abs_top
        root_frame = (*root_frame[:2], top, root_frame[3])
    before = tree_edge_ids(tree)
    changed = draw_tile_node(tree, root_frame, draw_state, (), tree=tree,
                             root_frame=root_frame, tile_state=tile_state,
                             gap=gap)
    if changed:
        retire_layouts(window, draw_state, dead=before - tree_edge_ids(tree))
    # Render content after topology edits, using the final leaf frames. The
    # conversion's existing identity scopes views independently of tree paths.
    from meltygui.view.tile_view import draw_tile_content
    from meltygui.core.layout.tile_links import prepare_endpoints
    endpoints = prepare_endpoints(tree)
    if tile_state is not None:
        from meltygui.core.layout.tile_links import retire_endpoints
        retire_endpoints(getattr(tile_state, '_link_endpoints', {}), endpoints)
        tile_state._link_endpoints = endpoints
    cursor = imgui.get_cursor_screen_pos()
    for _path, tile, frame in resolve_frames(tree, root_frame):
        if isinstance(tile, Split):
            continue
        if (tile_state is not None and tile_state.gesture is not None
                and tile_state.gesture.get("new_tile") is tile):
            # Instantiate the new editor at the committed split size, not the
            # tiny first drag frame; otherwise its own column widths persist
            # that transient geometry while the corner is still moving.
            continue
        rect = tile_rect(frame, draw_state, gap)
        if rect is None:
            continue
        left, top, right, bottom = rect
        # Shared content padding for every tile; corner grab zones keep their
        # larger hit targets, with this edge strip clear of content controls.
        content_padding = 6.0
        if right - left <= 2 * content_padding or bottom <= top:
            continue
        # Keep the 28px picker/toolbar usable when a row reaches its 40px minimum.
        vertical_inset = min(content_padding, max(0.0, (bottom - top - 28.0) * 0.5))
        imgui.set_cursor_screen_pos((left + content_padding, top + vertical_inset))
        content_changed, _ = draw_tile_content(
            tile, width=right - left - 2 * content_padding,
            height=bottom - top - 2 * vertical_inset,
            multi_instance_renderers=multi_instance_renderers,
            endpoints=endpoints,
            layout_frame=frame,
            # Each tile is its own blit-cache unit: only the tiles whose view
            # was invalidated run their renderer, the rest draw their captured
            # texture. Set False here to render every tile live each frame.
            use_cache=True,
        )
        changed |= content_changed
    imgui.set_cursor_screen_pos(cursor)
    if tile_state is not None:
        draw_join_preview(tree, root_frame, draw_state, tile_state)
    left, right, top, bottom = root_frame
    imgui.dummy(max(0.0, right["x"] - left["x"]), max(0.0, bottom["y"] - top["y"]))
    return changed


# ---------------------------------------------------------------------------
# Data interface - pure edits on the tree
# ---------------------------------------------------------------------------

def midpoint(near, far, axis):
    return {axis: (near[axis] + far[axis]) / 2}


def split_tile(tree, path, axis, root_frame, new_tile=None, at=None,
               before=False):
    """Split the tile at ``path`` along ``axis`` — Blender's area split.
    The new tile lands after the old one (right of / below it), or before
    it with ``before=True``, with a new edge at ``at`` (window coords on
    that axis) or at the midpoint of the tile's frame (frames come from
    ``resolve_frames`` over ``root_frame``). Same axis as the parent: a
    new sibling in the parent's flat list. Otherwise the leaf becomes a
    two-child Split of ``axis``. Returns the new tile's path."""
    if not path:
        raise ValueError("tile layouts require a Split root containing the leaf")
    frames = {p: f for p, _n, f in resolve_frames(tree, root_frame)}
    frame = frames[path]
    near, far = frame_pair(frame, axis)
    edge = midpoint(near, far, axis) if at is None else {axis: float(at)}
    tile = node_at(tree, path)
    if new_tile is None:
        new_tile = Tile(name=f"{tile.name}'", tint=tile.tint,
                        render_func=tile.render_func, input_value=tile.input_value)
    pair = [new_tile, tile] if before else [tile, new_tile]
    new_offset = 0 if before else 1
    parent = node_at(tree, path[:-1]) if path else None
    if parent is not None and parent.axis == axis:
        index = path[-1]
        parent.children[index:index + 1] = pair
        parent.edges.insert(index, edge)
        return path[:-1] + (index + new_offset,)
    split = Split(axis=axis, children=pair, edges=[edge])
    parent.children[path[-1]] = split
    return path + (new_offset,)


def join_tiles(tree, path, direction=+1):
    """Join the tile at ``path`` into its sibling ``direction`` away (+1 =
    the next, -1 = the previous) — Blender's area join: the tile and the
    edge between them go, the neighbour takes the room. Returns the
    neighbour's path after normalization."""
    if direction not in (-1, +1):
        raise ValueError("join direction must be -1 or +1")
    if not path:
        raise ValueError("cannot join the root")
    parent = node_at(tree, path[:-1])
    index = path[-1]
    neighbour = index + direction
    if not 0 <= neighbour < len(parent.children):
        raise ValueError("no neighbour to join into")
    if isinstance(parent.children[index], Split) or isinstance(parent.children[neighbour], Split):
        raise ValueError("join requires two leaf tiles sharing a whole edge")
    seeded = len(parent.edges) == len(parent.children) - 1
    parent.children.pop(index)
    if seeded:
        parent.edges.pop(min(index, neighbour))
    kept = neighbour if neighbour < index else index
    kept_node = parent.children[kept]
    normalize(tree)
    return locate(tree, kept_node)


def locate(tree, target):
    """The path of ``target`` (by identity) in the tree, or None."""
    for path, node in walk(tree):
        if node is target:
            return path
    return None


def normalize(tree):
    """Restore the invariants after an edit: a Split with ONE child is
    replaced by that child (the root keeps its Split shell, so the caller's
    object identity holds), and a Split child of its parent's axis is
    spliced into the parent's list, edges included. Returns the tree."""
    changed = True
    while changed:
        changed = False
        for _path, node in list(walk(tree)):
            if not isinstance(node, Split):
                continue
            children = node.children
            for index, child in enumerate(list(children)):
                if not isinstance(child, Split):
                    continue
                if len(child.children) == 1:
                    children[index] = child.children[0]
                    changed = True
                elif child.axis == node.axis:
                    children[index:index + 1] = child.children
                    # The child's interior edges join the node's list at
                    # the slot before this cell's far edge.
                    node.edges[index:index] = child.edges
                    changed = True
                if changed:
                    break
            if changed:
                break
    # The root itself: one Split child of the root collapses into it.
    if isinstance(tree, Split) and len(tree.children) == 1 \
            and isinstance(tree.children[0], Split):
        only = tree.children[0]
        tree.axis = only.axis
        tree.children = only.children
        tree.edges = only.edges
    return tree