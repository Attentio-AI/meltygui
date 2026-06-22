"""Universal dict drag-and-drop for Melty collections.

Driven by draw_collection: any @render_func rendered as an item of a
draw_collection can be picked up by its header and dropped into any slot —
the gaps between items, plus the top and bottom — of any draw_collection on
screen, including a different one (items move between dicts).

How a drag flows, end to end:

  * core_render's wrapper calls DragDrop.register_item for every view each
    frame. Items that qualify (child of a dict/list collection, has a header)
    subscribe their header rect to "left_mouse_drag"; the input handler
    captures the gesture on mouse-down and keeps delivering drag events to
    that view id for the whole drag, whether or not the view re-renders.

  * DragDrop.frame_update (called from Melty.end_frame) watches Melty.events
    for those captured drags and arms the drag once the cursor has moved
    ARM_DISTANCE from the press — header clicks stay clicks.

  * While armed, draw_collection renders the dragged child with closable=True
    (DragDrop.dragged_item_kwargs), so it defers to the window layers and
    floats; width/height are pinned to the size captured at pickup so the
    view doesn't reflow when it leaves the parent wrapper, and a placeholder
    (draw_placeholder) holds its inline slot open so the surrounding layout
    doesn't shift. The drag itself is
    invalidation-free, riding the closable-window fast path: the item's
    cached tile blits at a moving window_pos (glue_window_to_cursor updates
    it on the draw_state at dispatch time, not via kwargs) and _keep_alive
    re-registers the window on its layer on frames where the (blitted)
    source collection didn't run its deferring inline call. Invalidations
    happen only at the gesture's edges — pickup, drop, cancel.

  * Drop points come from the BVH: every draw_collection draw_state whose box
    intersects a DROP_RADIUS square around the cursor contributes one slot
    per gap (rows are the collection's live children, read from ds._children
    and their BVH boxes). A line is drawn on every slot in radius, nearest
    highlighted.

  * The item's start position is also a drop target (the _HOME sentinel),
    drawn as a subtle rect frame around the placeholder (draw_home) rather than
    a slot line. It competes by distance like any slot — the cursor is "at
    home" whenever it sits inside the placeholder rect (distance 0), so it wins
    ties against the adjacent gap lines. Releasing on it cancels the reorder,
    the natural target for a change of mind or an accidental short drag.
    Because _HOME routes through the same "nothing selected → snap back" path
    as releasing over empty space, the cancel needs no special commit logic
    beyond skipping _commit for the sentinel.

  * On release the reorder is applied the same way the undo manager applies a
    restore: a CollectionMutation is registered in Melty.dnd_requests keyed
    by the collection's draw_state; core_render's wrapper tail intercepts
    that draw_state's next return, applies the mutation to the LIVE
    collection in place (identity stable for live models) and reports
    (True, reordered_dict), so the parent writes it back exactly as if the
    user had edited it. Cross-collection moves register a Remove on the
    source and an Insert on the target.

  * The applied mutation and its inverse are recorded into UndoManager as a
    Change(old=inverse, new=mutation) — the undo stack holds "insert x at
    key a"-style records, never dict snapshots. Ctrl+Z routes the inverse
    back through Melty.undo_requests and the wrapper tail applies it to the
    live collection; both halves of a cross-collection move land in the
    same frame, so frame-window grouping undoes them as one step.

The older drag/drop fields on Melty (dragged_item, drag_in_progress,
drag_drop_target, draw_drag_drop_target, ...) are a previous, separate
attempt and are not used here.
"""
import math
from dataclasses import dataclass

import imgui

from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

# Slots farther than this from the cursor are neither drawn nor droppable.
DROP_RADIUS = 260.0
# The cursor must travel this far from the press before the drag arms (the
# dragged window detaches) - keeps header clicks from ever reordering.
ARM_DISTANCE = 2.0

_VIEW_ID = "dnd_item"
_VIEW_ID_SUFFIX = "_" + _VIEW_ID
_EVENTS = ["left_mouse_drag", "left_mouse_drag_released"]

# Sentinel used as cls.nearest when the cursor is over the item's start
# position. It carries no insert index - dropping on it cancels the reorder.
_HOME = object()

@window
class DragDrop:
    item_ds = None        # the dragged item's draw_state
    source_ds = None      # the collection draw_state the item came from
    active = False
    value = None          # the dragged value (live object)
    key = None            # the item's key in the source collection
    grab_offset = (0.0, 0.0)  # cursor - item top-left at pickup
    size = (None, None)       # item (w, h) at pickup; pins the floating window
    home_rect = None          # (abs_left, abs_top) of the inline slot at pickup
    slots = ()                # this frame's (y, x0, x1, h, coll_ds, insert_idx)
    nearest = None

    # ── wrapper hooks (called from core_render for every view) ──────────

    @classmethod
    def register_item(cls, draw_state):
        """Subscribe an eligible collection item's header as a drag handle.

        Runs for every view every frame — keep the early-outs cheap. The
        subscription is what lets the input handler capture the gesture at
        mouse-down; the events are read globally in frame_update, so this
        only needs to have run on the frame the press lands."""
        coll_ds = draw_state._collection_draw_state
        if coll_ds is None or draw_state.closable:
            return
        if not draw_state.header_height:
            return
        if (draw_state.abs_left is None or draw_state.abs_top is None
                or not draw_state.width):
            return
        if not isinstance(coll_ds._raw_input_value, (dict, list)):
            return
        # priority_delta=1 beats lower-depth subscriptions (e.g. a text
        # editor's selection drag registered over its whole body) to the
        # header band only.
        draw_state.on_action(_EVENTS, view_id=_VIEW_ID,
                             rect=draw_state.get_header_rect(),
                             priority_delta=1)

    @classmethod
    def is_dragged_item(cls, draw_state):
        return cls.active and draw_state is cls.item_ds

    @classmethod
    def glue_window_to_cursor(cls, draw_state):
        """Pin the floating window under the grab point. Delta-correct
        window_pos using the live abs position — abs_left/top are linear in
        window_pos, so one step lands exactly regardless of parent offsets,
        anchors or ancestor scroll."""
        left, top = draw_state.abs_left, draw_state.abs_top
        if left is None or top is None:
            return
        mx, my = imgui.get_io().mouse_pos
        cur = draw_state.window_pos or (0.0, 0.0)
        draw_state.window_pos = (int(cur[0] + (mx - cls.grab_offset[0]) - left),
                                 int(cur[1] + (my - cls.grab_offset[1]) - top))

    # ── draw_collection hooks ────────────────────────────────────────

    @classmethod
    def is_dragged_child(cls, collection_ds, key):
        return (cls.active and collection_ds is cls.source_ds
                and key == cls.key)

    @classmethod
    def dragged_item_kwargs(cls):
        """Kwarg overrides draw_collection applies to the dragged child so it
        renders as a detached floating window of its pre-pickup size."""
        kw = {
            "closable": True,
            "detached": True,      # stays out of root_draw_states bookkeeping
            "swoosh": False,
            "auto_resize": False,
            "use_cache": True,
        }
        w, h = cls.size
        if w:
            kw["width"] = w
        if h:
            kw["height"] = h
        return kw

    @classmethod
    def draw_placeholder(cls, horizontal=False, item_spacing_y=1,
                         style_manager=None, draw_bg=None):
        """Hold the dragged item's inline slot open at its pickup size so the
        collection's layout doesn't shift while the item floats. Drawn inside
        the collection body, so it bakes into the collection's tile on the
        one pickup re-render — zero per-frame cost. Rendered with the
        standard draw_bg (passed in by draw_collection — importing it here
        would be circular) one bg level deeper than the collection, where the
        displaced item's own background sat, so it reads as an empty socket."""
        w, h = cls.size
        if not w or not h:
            return
        melty = Core.melty
        # The bg rect comes from the abs position captured at pickup (the
        # item's inline slot) because the cursor here, mid-frame re-render,
        # proved unreliable. The slot doesn't move during the drag (that's
        # the placeholder's whole point) and the bake travels with the tile.
        x, y = cls.home_rect if cls.home_rect is not None else imgui.get_cursor_screen_pos()
        if draw_bg is not None and style_manager is not None:
            # bypass=True calls the raw function - draw_bg is @inline-wrapped
            # and without it the wrapper gives it a draw_state and blakes out
            # itself (everywhere but here). Sibling item bgs draw at their
            # wrapper's get_channel() - 2 = this body's get_channel() - 1
            # (children run one depth deeper); match it, then restore the
            # original channel.

            if melty.channels_split:
                draw_list = imgui.get_window_draw_list()
                draw_list.channels_set_current(
                    max(0, min(melty.get_channel() - 3, melty.max_depth - 1)))

            imgui.get_window_draw_list().add_rect_filled(
                x, y, x + w, y + h, imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.05),
                rounding=5.0)
            draw_bg(bypass=True, left=x, top=y, width=w, height=h - 2,
                    rounding=5.0, bg_offset=1, depth=melty.shadow_depth,
                    opacity=1.0, nested_bg=True, style_manager=style_manager)
            if melty.channels_split:
                imgui.get_window_draw_list().channels_set_current(
                    max(0, min(melty.get_channel(), melty.max_depth - 1)))
        else:
            imgui.get_window_draw_list().add_rect(
                x + 2, y, x + w - 2, y + h - 2,
                imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.10),
                rounding=4.0, thickness=1.0)
        if horizontal:
            imgui.dummy(w, h)
            imgui.same_line(spacing=0)
        else:
            imgui.dummy(0, int(h))
            imgui.dummy(0, item_spacing_y)

    @classmethod
    def draw_home_blank(cls):
        """Blit-layer placeholder: paint the blank socket over the home slot
        of a freshly blitted tile. Called by blit_offscreen (via
        Melty.dnd_home_rect) right after it draws a cached tile image that
        contains the slot — the tile's pixels there can be stale, captured
        while the floating window still overlapped its slot. Draws on the
        current channel, directly over the image."""
        if cls.home_rect is None:
            return
        w, h = cls.size
        if not w or not h:
            return
        x, y = cls.home_rect
        melty = Core.melty
        sm = melty.style_manager
        if sm is not None:
            from src.lsd.gl_gui.view.core_views.new_core_view import draw_bg
            draw_bg(bypass=True, left=x, top=y, width=w, height=h - 2,
                    rounding=5.0, bg_offset=1, depth=melty.shadow_depth,
                    opacity=1.0, nested_bg=True, style_manager=sm)
        else:
            imgui.get_window_draw_list().add_rect(
                x + 2, y, x + w - 2, y + h - 2,
                imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.10),
                rounding=4.0, thickness=1.0)

    # ── per-frame update (called from Melty.end_frame) ───────────────────

    @classmethod
    def frame_update(cls):
        melty = Core.melty
        if not cls.active:
            cls._watch_for_pickup(melty)
            if not cls.active:
                return

        if (cls.item_ds is None or cls.source_ds is None
                or cls.source_ds.closed or cls.source_ds.abs_closed):
            cls._reset()
            return

        mx, my = imgui.get_io().mouse_pos
        cls._compute_slots(mx, my)
        cls._draw_slots()
        cls._draw_home()

        if not melty.event_handler.is_down("left_mouse"):
            # _HOME (or None) means "drop back at the home" - no reorder.
            if cls.nearest is not None and cls.nearest is not _HOME:
                cls._commit()
            cls._reset()
            return

        # NO per-frame invalidation: the floating window is a cached tile
        # blitted at its moving window_pos - the closable-window fast path.
        # The only per-frame work is keeping it registered with its layer,
        # usually done by the deferring inline call in the source
        # collection's body, which a clean (blitted) collection rightly skips.
        cls._keep_alive(melty)

    @classmethod
    def _keep_alive(cls, melty):
        """Re-register the dragged window on its layer for this frame's
        dispatch when the source collection's body didn't run to defer it.
        Runs from end_frame BEFORE the layer loop."""
        layers = melty.layers
        if not layers:
            return
        item = cls.item_ds
        # Not detached yet (the pickup's wake hasn't re-rendered the
        # item with the closable=True): the deferral block hasn't
        # configured the window - let it, next frame, rather than dispatching
        # an inline-configured draw_state as the window.
        active_layer = (item._kwargs or {}).get("active_layer")
        if active_layer is None or not item.closable:
            return
        for layer in layers:
            if item in layer:
                return
        layers[min(int(active_layer), len(layers) - 1)].append(item)

    # ── internals ────────────────────────────────────────────────────────

    @classmethod
    def _watch_for_pickup(cls, melty):
        """Find an armed header drag among this frame's events and pick the
        item up. Events are delivered to the captured view id for the whole
        gesture even when the view itself stopped re-rendering, so reading
        them here (not in the wrapper) survives cache-skipped frames."""
        cache = getattr(melty, "cache", None)
        if cache is None:
            return
        # An active imgui widget (e.g. a drag_float living in a header) owns
        # the gesture - don't also pick the item up.
        if melty.imgui_active or melty.imgui_popup_open:
            return
        for view_id, events in melty.events.items():
            if not (isinstance(view_id, str) and view_id.endswith(_VIEW_ID_SUFFIX)):
                continue
            ev = events.get("left_mouse_drag")
            if ev is None:
                continue
            if math.hypot(ev.total_dx, ev.total_dy) < ARM_DISTANCE:
                continue
            ds = cache.key_to_draw_state.get(view_id[:-len(_VIEW_ID_SUFFIX)])
            if ds is None:
                continue
            cls._begin(ds, ev)
            return

    @classmethod
    def _begin(cls, draw_state, ev):
        coll_ds = draw_state._collection_draw_state
        if coll_ds is None:
            return
        coll = coll_ds._raw_input_value
        key = (draw_state._kwargs or {}).get("key", None)
        if isinstance(coll, dict):
            if key not in coll:
                return
        elif isinstance(coll, list):
            if not (isinstance(key, int) and 0 <= key < len(coll)):
                return
        else:
            return

        cls.active = True
        cls.item_ds = draw_state
        cls.source_ds = coll_ds
        cls.key = key
        cls.value = coll[key]
        down_x, down_y = ev.x - ev.total_dx, ev.y - ev.total_dy
        left = draw_state.abs_left if draw_state.abs_left is not None else down_x
        top = draw_state.abs_top if draw_state.abs_top is not None else down_y
        cls.grab_offset = (max(0.0, down_x - left), max(0.0, down_y - top))
        cls.size = (draw_state.width, draw_state.height)
        # The item's inline position, captured while it still IS inline -
        # the placeholder bg draws at this rect for the whole drag. Mirrored
        # into Melty so blit_offscreen can repaint the socket over cached
        # tiles that contain the slot (see draw_home_blank).
        cls.home_rect = (left, top)
        Core.melty.dnd_home_rect = (left, top, cls.size[0] or 0, cls.size[1] or 0)
        cls.slots = ()
        cls.nearest = None
        # Reflow the collection (the item leaves the UI flow) and re-render
        # the item as a window.
        cls._wake(coll_ds)
        cls._wake(draw_state)

    @classmethod
    def _wake(cls, draw_state):
        """Minimal repaint at a gesture edge: force-dirty just this view's
        tile. invalidate marks the tile plus its ancestor path, so the body
        re-runs next frame while siblings and descendants keep blitting.
        (The previous invalidate_up cascaded over descendants too — whole
        subtrees re-captured their tiles, which is where the blit smearing
        came from.)"""
        cache = getattr(Core.melty, "cache", None)
        if cache is not None and draw_state._tile_id is not None:
            cache.invalidate(draw_state._tile_id, force=True)
        request_render()

    @classmethod
    def _inside_dragged(cls, draw_state):
        node, hops = draw_state, 0
        while node is not None and hops < 64:
            if node is cls.item_ds:
                return True
            parent = node._parent
            if parent is node:
                break
            node = parent
            hops += 1
        return False

    @classmethod
    def _is_drop_collection(cls, draw_state):
        if getattr(getattr(draw_state, "_view_func", None), "__name__", None) != "draw_collection":
            return False
        coll = draw_state._raw_input_value
        if not isinstance(coll, (dict, list)):
            return False
        if coll is cls.value:           # a dict can't be dropped into itself
            return False
        return not cls._inside_dragged(draw_state)

    @classmethod
    def _compute_slots(cls, mx, my):
        melty = Core.melty
        slots = []
        r = DROP_RADIUS
        seen = set()
        for rid in melty._bvh.intersection((mx - r, my - r, mx + r, my + r)):
            if rid in seen:
                continue
            seen.add(rid)
            ds = melty._bvh_id_to_ds.get(rid)
            if ds is None or ds.closed or ds.abs_closed:
                continue
            if not cls._is_drop_collection(ds):
                continue
            cls._collection_slots(ds, mx, my, slots)
        slots.sort(key=lambda s: s[0])
        cls.slots = slots
        nearest = slots[0] if slots else None
        # The start position competes on distance but is drawn as a dot frame
        # (draw_home), not a slot line. It wins ties (<=) so that while the
        # cursor still sits inside the placeholder (distance 0) it beats the
        # gap lines hugging the placeholder edges - otherwise a barely-moved
        # drag snaps to one of those and reorders. _HOME routes to the
        # same "nothing there → snap back" drop path as blank space.
        home_dist = cls._home_distance(mx, my)
        if (home_dist is not None and home_dist <= DROP_RADIUS
                and (nearest is None or home_dist <= nearest[0])):
            cls.nearest = _HOME
        else:
            cls.nearest = nearest

    @classmethod
    def _collection_slots(cls, ds, mx, my, out):
        """Append every slot of one collection: above its first live row,
        between consecutive rows, and below the last (an empty/collapsed
        collection gets a single append-at-end slot under its header)."""
        coll = ds._raw_input_value
        clip = ds.abs_clip_rect
        if clip is None:
            return
        cl, ct, cr, cb = clip
        # A collection's clip can be its full CONTENT box (content heights of
        # tens of thousands of lines past the window), and rows that are
        # themselves collapsed collections have content-height bboxes too -
        # midpoints between such rows land well of the window. Clamp the
        # slot band to the enclosing window's rect and the display so we
        # never draw outside what's actually visible.
        pw = ds.parent_window
        if (pw is not None and pw is not ds and pw.abs_left is not None
                and pw.abs_top is not None and pw.width and pw.height):
            cl = max(cl, pw.abs_left)
            ct = max(ct, pw.abs_top)
            cr = min(cr, pw.abs_left + pw.width)
            cb = min(cb, pw.abs_top + pw.height)
        disp = imgui.get_io().display_size
        cl, ct = max(cl, 0), max(ct, 0)
        cr, cb = min(cr, disp[0]), min(cb, disp[1])
        if cr <= cl or cb <= ct:
            return

        if isinstance(coll, dict):
            keys = list(coll.keys())
        else:
            keys = None  # list has positional keys, idx is the key

        rows = []
        for idx, child in (ds._children or {}).items():
            if child is None or child is cls.item_ds:
                continue
            if child._collection_draw_state is not ds:
                continue
            # Deliberately NOT abs_closed (it counts a row's OWN collapsed
            # state - collapsed headers are visible and must keep slots) and
            # NOT _bvh_bbox liveness (bvh_query lazily EVICTS collapsed rows'
            # boxes, so any unrelated query over one would drop it from slot
            # math until the next collection re-render). Rows hidden by a
            # collapsed/closed ancestor never get here: the collection itself
            # is filtered as a query by its own abs_closed. Stale rows are
            # filtered by the key-at-idx ghost guard and the clip rect clamp.
            if child.closed:
                continue
            # Ghost guard: a child whose key left the collection never
            # re-renders, so its stale box would otherwise still make slots.
            if keys is not None:
                if idx >= len(keys):
                    continue
                child_key = (child._kwargs or {}).get("key", None)
                if child_key != keys[idx]:
                    continue
            elif idx >= len(coll):
                continue
            top = child.abs_top
            if top is None:
                continue
            # Visible height, not stored height: a collapsed view occupies
            # only its header band, whatever its (possibly stale, expanded)
            # height claims. Only the final slot below the last row actually
            # uses this - every other slot anchors purely on abs_top.
            visible_h = child.height or 0
            if not child.expanded:
                visible_h = min(visible_h, child.header_height or visible_h)
            rows.append((idx, top, top + visible_h, child.abs_left))
        rows.sort(key=lambda r: (r[1], r[0]))

        # Indent each slot line to where this collection's rows actually sit so
        # the line's left edge tracks the content indent - a nested collection's
        # lines read as visibly deeper at a glance, instead of every collection
        # drawing its lines flush at the same static collection offset. A row's
        # abs_left always carries the accumulated indent_size of every enclosing
        # collection; `content_left` (the collection's own indent_size off its
        # left) is the fallback for a missing row left or the collapsed collection.
        indent = ds._kwargs.get("indent_size", 0) or 0
        content_left = ds.abs_left + indent
        x1 = min(cr, ds.abs_left + (ds.width or 0) - 4)
        y_min, y_max = ct - 6, cb + 6

        def _slot_x0(row_left):
            return max(cl, row_left if row_left is not None else content_left)

        if not rows:
            x0 = _slot_x0(None)
            if x1 <= x0:
                return
            y = ds.abs_top + (ds.header_height or 0) + 4
            cls._add_slot(out, ds, len(coll), x0, x1, y, y_min, y_max, mx, my)
            return

        # One "insert before me" slot per row, anchored on the row's live
        # abs_top - an exact screen coordinate regardless of how tall (or
        # collapsed) the rows above it are. Heights only ever matter to
        # the single append-at-end slot under the last row.
        for idx, top, _bottom, left in rows:
            x0 = _slot_x0(left)
            if x1 <= x0:
                continue
            cls._add_slot(out, ds, idx, x0, x1, top - 2, y_min, y_max, mx, my)
        last_idx, _top, last_bottom, last_left = rows[-1]
        x0 = _slot_x0(last_left)
        if x1 > x0:
            cls._add_slot(out, ds, last_idx + 1, x0, x1, last_bottom + 3,
                          y_min, y_max, mx, my)

    @classmethod
    def _add_slot(cls, out, ds, insert_idx, x0, x1, y, y_min, y_max, mx, my):
        if y < y_min or y > y_max:
            return
        dx = max(x0 - mx, 0.0, mx - x1)
        dist = math.hypot(dx, my - y)
        if dist > DROP_RADIUS:
            return
        # A slot covered by a window stacked above its own window is neither
        # visible nor droppable. Use the point on the line the distance was
        # measured from (nearest to the cursor) - BVH point query, cheap and
        # accurate, handles half-covered windows per usual.
        if cls._slot_occluded(ds, min(max(mx, x0), x1), y):
            return
        out.append((dist, x0, x1, y, ds, insert_idx))

    @classmethod
    def _slot_occluded(cls, coll_ds, x, y):
        """True when the topmost closable window at (x, y) isn't one of the
        slot collection's own ancestor windows — i.e. the slot lies behind
        another window there. The floating dragged window (and anything in
        it) never occludes: the slot under the user's hand is the one they
        most want."""
        owners = set()
        win = coll_ds.parent_window
        hops = 0
        while win is not None and hops < 32:
            owners.add(id(win))
            nxt = win.parent_window
            if nxt is win:
                break
            win = nxt
            hops += 1
        for hit in Core.melty.bvh_query(x, y):
            if not hit.closable:
                continue
            if cls._inside_dragged(hit):
                continue
            return id(hit) not in owners
        return False

    @classmethod
    def _draw_slots(cls):
        if not cls.slots:
            return
        melty = Core.melty
        overlay = imgui.get_overlay_draw_list()
        if melty._overlay_channels_active:
            # The overlay list is split into max_layer channels; max_layer - 1
            # is the global top channel, the only one the split renderer never
            # stencil-masks on higher windows (window_index can pick any
            # mid channel, which is why the lines vanished on busy windows).
            overlay.channels_set_current(melty.max_layer - 5)
        # The lines ride the global top overlay channel (never stencil-masked),
        # which would also draw them over the floating dragged window: carve
        # its rect out of every line by hand. Derive the rect from the live
        # mouse (where the glue puts the window this frame), not the
        # draw_state, which is a frame behind during fast motion.
        mx, my = imgui.get_io().mouse_pos
        w, h = cls.size
        ix0, iy0 = mx - cls.grab_offset[0], my - cls.grab_offset[1]
        ix1, iy1 = ix0 + (w or 0), iy0 + (h or 0)

        for slot in cls.slots:
            dist, x0, x1, y, _ds, _idx = slot
            nearest = slot is cls.nearest
            # Color each line from its own collection's stashed tint - the
            # same brightened-tint helper the swoosh and selection highlights
            # use, so slots read as part of the window they'd drop into.
            rgb = melty._highlight_rgb(_ds.current_tint)
            if nearest:
                col = imgui.get_color_u32_rgba(*rgb, 0.95)
                thickness = 3.0
            else:
                fade = max(0.0, 1.0 - dist / DROP_RADIUS)
                col = imgui.get_color_u32_rgba(*rgb, 0.10 + 0.40 * fade)
                thickness = 2.0
            if iy0 - 2.0 <= y <= iy1 + 2.0:
                # Line crosses the window's band: keep the spans beside it.
                if ix0 > x0:
                    overlay.add_line(x0, y, min(x1, ix0), y, col, thickness)
                if ix1 < x1:
                    overlay.add_line(max(x0, ix1), y, x1, y, col, thickness)
            else:
                overlay.add_line(x0, y, x1, y, col, thickness)
            if nearest:
                if not (ix0 <= x0 <= ix1 and iy0 - 4 <= y <= iy1 + 4):
                    overlay.add_circle_filled(x0, y, 3.5, col)
                if not (ix0 <= x1 <= ix1 and iy0 - 4 <= y <= iy1 + 4):
                    overlay.add_circle_filled(x1, y, 3.5, col)

    @classmethod
    def _home_distance(cls, mx, my):
        """Distance from the cursor to the start drop zone — the item's
        pickup slot (the placeholder). 0 anywhere inside the rect, so the
        whole original footprint reads as "drop back here". None when there's
        no captured home rect/size to measure against."""
        if cls.home_rect is None:
            return None
        w, h = cls.size
        if not w or not h:
            return None
        x, y = cls.home_rect
        dx = max(x - mx, 0.0, mx - (x + w))
        dy = max(y - my, 0.0, my - (y + h))
        return math.hypot(dx, dy)

    @classmethod
    def _draw_home(cls):
        """Frame the start slot so it reads as a droppable target: a faint
        rect while dragging anywhere, lifted (but kept subtle — this is a
        cancel zone, not a reorder target) when the cursor is over it
        (cls.nearest is _HOME). Rides the same top overlay channel as the slot
        lines — which is ABOVE the floating dragged window (it's on the top
        layer), so the frame would paint over the dragged view on a short
        drag. The frame is aligned to the home socket's own background rect
        (see draw_placeholder / draw_home_blank — left=x, top=y, width=w,
        height=h-2, rounding=5.0). To keep it off the dragged view without
        losing the rounded corners, draw the SAME rounded rect clipped to the
        strips around the window's live rect (top & bottom full-width, left &
        right middle-band only) — the corners live in the full-width top/bottom
        strips, so they survive; the strips don't overlap, so the
        semi-transparent outline never double-draws at a seam."""
        if cls.home_rect is None:
            return
        w, h = cls.size
        if not w or not h:
            return
        melty = Core.melty
        overlay = imgui.get_overlay_draw_list()
        if melty._overlay_channels_active:
            overlay.channels_set_current(melty.max_layer - 5)
        x, y = cls.home_rect
        src = cls.source_ds
        rgb = melty._highlight_rgb(src.current_tint) if src is not None else (1.0, 1.0, 1.0)
        if cls.nearest is _HOME:
            col = imgui.get_color_u32_rgba(*rgb, 0.45)
            thickness = 1.75
        else:
            col = imgui.get_color_u32_rgba(*rgb, 0.16)
            thickness = 1.5
        rounding = 5.0
        # Inset 1px on every side so the highlight sits ever so slightly
        # inside the socket background rect (x, y, w, h-2).
        fx0, fy0, fx1, fy1 = x + 1, y + 1, x + w - 1, y + h - 3

        # The floating item window's live rect - same source as _draw_slots
        # (the mouse, where the glue puts the window this frame, a frame ahead
        # of the draw_state during fast motion).
        mx, my = imgui.get_io().mouse_pos
        ix0, iy0 = mx - cls.grab_offset[0], my - cls.grab_offset[1]
        ix1, iy1 = ix0 + w, iy0 + h

        if fx1 <= ix0 or fx0 >= ix1 or fy1 <= iy0 or fy0 >= iy1:
            # No overlap with the dragged view: draw the rounded frame whole.
            overlay.add_rect(fx0, fy0, fx1, fy1, col, rounding=rounding,
                             thickness=thickness)
            return

        # Overlap: clip the rounded frame to the non-overlapping strips that
        # make the frame minus the live rect, redrawing the whole rounded
        # rect on each so its corners stay round wherever they're not over the
        # dragged view.
        band_t, band_b = max(fy0, iy0), min(fy1, iy1)
        strips = (
            (fx0, fy0, fx1, iy0),       # top (full width - holds top corners)
            (fx0, iy1, fx1, fy1),       # bottom (full width - holds bottom corners)
            (fx0, band_t, ix0, band_b), # left middle band
            (ix1, band_t, fx1, band_b), # right middle band
        )
        for cx0, cy0, cx1, cy1 in strips:
            if cx1 <= cx0 or cy1 <= cy0:
                continue
            overlay.push_clip_rect(cx0, cy0, cx1, cy1, True)
            overlay.add_rect(fx0, fy0, fx1, fy1, col, rounding=rounding,
                             thickness=thickness)
            overlay.pop_clip_rect()

    @classmethod
    def _commit(cls):
        """Register the reorder with Melty.dnd_requests — core_render's
        wrapper tail intercepts the target draw_state's next return and
        reports (True, reordered_collection), undo-manager style."""
        melty = Core.melty
        _dist, _x0, _x1, _y, target_ds, insert_idx = cls.nearest
        src_ds, key = cls.source_ds, cls.key

        if target_ds is src_ds:
            melty.dnd_requests[src_ds] = Reorder(key, insert_idx)
            cls._wake(src_ds)
        else:
            src = src_ds._raw_input_value
            if isinstance(src, dict):
                if key not in src:
                    return
                value = src[key]
            elif isinstance(src, list):
                if not (isinstance(key, int) and 0 <= key < len(src)):
                    return
                value = src[key]
            else:
                return
            melty.dnd_requests[src_ds] = Remove(key)
            melty.dnd_requests[target_ds] = Insert(key, value, insert_idx)
            cls._wake(src_ds)
            cls._wake(target_ds)
        request_render()

    @classmethod
    def _reset(cls):
        src, item = cls.source_ds, cls.item_ds
        cls.active = False
        cls.item_ds = None
        cls.source_ds = None
        cls.key = None
        cls.value = None
        cls.home_rect = None
        Core.melty.dnd_home_rect = None
        cls.slots = ()
        cls.nearest = None
        if item is not None:
            item.window_pos = (0, 0)
            # Undo the floating-render override. dragged_item_kwargs() forced
            # auto_resize=False + a fixed width so the item floated at its
            # pickup size; core_render's `fixed_size = not draw_state.auto_resize`
            # makes that False sticky, so once re-homed the item would stay
            # frozen at the pickup width instead of scaling to fill its new
            # container. Restore auto-resize so it re-derives its size in place.
            item.auto_resize = True
            cls._wake(item)
        if src is not None:
            cls._wake(src)


# ─── collection mutations ─────────────────────────────────────────────────
# Reversible in-place edits. These are what Melty.dnd_requests carries and -
# crucially - what lands on the undo stack: a Change pair (old=inverse,
# new=mutation) instead of value snapshots, so undoing a drag never copies a
# dict ("insert x at key a", "remove y at key b"). apply() mutates the LIVE
# dict/list at interception frame and returns (changed, coll, inverse), the
# inverse computed against the pre-apply state. UndoManager.undo()/redo()
# route either side through Melty.undo_requests and the wrapper tail applies
# it - the same (changed, value) return path as any user edit. Identity is
# checked by duck type (__collection_mutation__) in core_render and
# core_undo so neither needs an import from here.


def _free_key(coll, key):
    """A key that collides with nothing in `coll` BY STRING — draw_state
    uniques are built from stringified keys, so an int 4 dropped beside an
    existing '4' would give two rows the same identity (shared draw_state,
    heights fighting every frame). Compare str-to-str, not just `in`."""
    taken = {str(k) for k in coll}
    if str(key) not in taken:
        return key
    base = str(key)
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


class CollectionMutation:
    __collection_mutation__ = True


@dataclass(frozen=True)
class Reorder(CollectionMutation):
    """Move existing `key` to before the element currently at `insert_idx`
    (pre-removal coordinates). For lists `key` is the from-index."""
    key: object
    insert_idx: int

    def apply(self, coll):
        if isinstance(coll, dict):
            items = list(coll.items())
            cur = next((i for i, (k, _) in enumerate(items) if k == self.key), None)
            if cur is None:
                return False, coll, None
            pair = items.pop(cur)
            idx = self.insert_idx - 1 if cur < self.insert_idx else self.insert_idx
            idx = max(0, min(idx, len(items)))
            items.insert(idx, pair)
            if idx == cur:
                return False, coll, None    # dropped back where it was
            coll.clear()
            coll.update(items)
            return True, coll, Reorder(self.key, cur if cur < idx else cur + 1)
        if isinstance(coll, list):
            if not (isinstance(self.key, int) and 0 <= self.key < len(coll)):
                return False, coll, None
            value = coll.pop(self.key)
            idx = self.insert_idx - 1 if self.key < self.insert_idx else self.insert_idx
            idx = max(0, min(idx, len(coll)))
            coll.insert(idx, value)
            if idx == self.key:
                return False, coll, None
            return True, coll, Reorder(idx, self.key if self.key < idx else self.key + 1)
        return False, coll, None


@dataclass(frozen=True)
class Insert(CollectionMutation):
    """Insert `value` at `key` before index `insert_idx`. The key is renamed
    on collision; the returned inverse removes the key actually used. The
    value rides by reference — never a copy."""
    key: object
    value: object
    insert_idx: int

    def apply(self, coll):
        if isinstance(coll, dict):
            # Dicts get string keys: a list index (int) dropped into a dict
            # would otherwise sit invisibly beside its stringified twin.
            key = self.key if isinstance(self.key, str) else str(self.key)
            key = _free_key(coll, key)
            items = list(coll.items())
            idx = max(0, min(self.insert_idx, len(items)))
            items.insert(idx, (key, self.value))
            coll.clear()
            coll.update(items)
            return True, coll, Remove(key)
        if isinstance(coll, list):
            idx = max(0, min(self.insert_idx, len(coll)))
            coll.insert(idx, self.value)
            return True, coll, Remove(idx)
        return False, coll, None


@dataclass(frozen=True)
class Remove(CollectionMutation):
    key: object

    def apply(self, coll):
        if isinstance(coll, dict):
            if self.key not in coll:
                return False, coll, None
            idx = next(i for i, k in enumerate(coll) if k == self.key)
            value = coll.pop(self.key)
            return True, coll, Insert(self.key, value, idx)
        if isinstance(coll, list):
            if not (isinstance(self.key, int) and 0 <= self.key < len(coll)):
                return False, coll, None
            value = coll.pop(self.key)
            return True, coll, Insert(self.key, value, self.key)
        return False, coll, None
