"""Search view functions and supporting definitions."""
from meltygui.hdr_color import pack_color
from meltygui.core.melty import Melty
from meltygui.core.core_render import render_func
from meltygui.core.toggles import Toggles
import meltygui_imgui as imgui
import math


@render_func(use_cache=True, layer_offset=1, searchable=False)
def draw_search(input_value=None, draw_state=None, unique=0):
    """Floating find pill for searchable views that have no header. Rendered
    as a Mode.WINDOW_CLEAN from core_render when search is active, pinned to
    the owning view's bottom-right corner (the file browser's search pill):
    one row — the search icon, the query box, "n of m" (or "no match"), and
    the close button. Up / Down, Enter / Shift+Enter step the matches (the
    row has no arrow buttons), Esc closes. State — search_text, count,
    current index — lives on the owning view's draw_state (search_owner)."""
    from meltygui.core.glfw_utils import request_render
    import meltygui.core.window_api as glfw

    owner = input_value
    # draw_search(owner, unique=owner._tile_id, draw_state=draw_state)
    search_ds = owner
    regrab_focus = True

    from meltygui.view.text_view import draw_text
    # Re-grab gated on this search still being active (focused_ds is the owner):
    # a deliberate click away clears focused_ds (via clear_focus) so the box
    # releases focus and stays open-but-unfocused, while a spurious clear during
    # typing leaves focused_ds intact so the box reclaims focus.
    focus_search = ((not search_ds._search_was_active)
                    or (regrab_focus and Melty.text_focused_ds is None
                        and Melty.focused_ds is search_ds)
                    or search_ds._search_focus_pending)
    # One-shot open/Ctrl+F frame (NOT the spurious-clear regrab): select the
    # whole term so typing replaces, and a single delete clears it.
    focus_fresh = ((not search_ds._search_was_active)
                   or search_ds._search_focus_pending)
    search_ds._search_focus_pending = False
    search_ds._search_was_active = True
    search_icon = ""
    imgui.align_text_to_frame_padding()
    imgui.text(search_icon)
    imgui.same_line()
    x_width = 30
    count_width = 84
    box_width, _pill_w = search_pill_layout(search_ds.search_text, search_ds.width)

    # Laid out left to right at fixed offsets (never off the pill's own
    # width - core_render sizes the window to search_pill_layout too). The
    # box draws no background of its own: the query sits flat on the pill.
    box_left, row_top = imgui.get_cursor_screen_pos()
    _box = draw_text(search_ds.search_text, searchable=False, is_search_box=True,
                     width=box_width, max_width=box_width, show_bg=False,
                     shadow=False, name=search_icon + str(unique),
                     with_header_end=None, wrap=False, z_offset=-1, single_line=True,
                     with_footer=None, tint=search_ds.tint,
                     show_name=False, show_header=False,
                     request_focus=focus_search, select_all_on_focus=focus_fresh,
                     return_extras=True)
    search_change, new_search = _box[0], _box[1]
    _box_ds = _box[2] if len(_box) > 2 else None
    # While the find box holds text focus, mark this search as the active one so
    # Enter/arrow nav goes here - including after clicking back into the box.
    if _box_ds is not None and Melty.text_focused_ds is _box_ds:
        Melty.focused_ds = search_ds
    if search_change:
        search_ds.search_text = new_search
        # Re-render the owner's whole subtree so every child view recomputes its
        # matches against the new term and the combined index stays in sync.
        Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
        request_render()
    # The count, on the same row: "n of m" while there are matches, "no
    # match" for a term that finds nothing, populated by the owner's pre-body
    # search walk (core_render). The keys below step the current match.
    total = search_ds.text_search_count
    imgui.same_line()
    count_left = box_left + box_width + 10
    imgui.set_cursor_screen_pos((count_left, imgui.get_cursor_screen_pos()[1]))
    imgui.align_text_to_frame_padding()
    if total > 0:
        imgui.text_colored(f"{search_ds.text_search_current + 1} of {total}",
                           0.62, 0.68, 0.76, 1.0)
    elif search_ds.search_text:
        imgui.text_colored("no match", 0.95, 0.55, 0.5, 1.0)
    else:
        imgui.text_colored("", 0.62, 0.68, 0.76, 1.0)

    imgui.same_line()
    fa_x_icon = ""

    # The close button: a glyph, dim until hovered, with a click rect on
    # the pill (no button chrome).
    close_left = count_left + count_width
    row_h = max(imgui.get_frame_height(), 24.0)
    close_rect = (close_left, row_top, close_left + x_width, row_top + row_h)
    _mx, _my = imgui.get_mouse_pos()
    _close_hover = (close_rect[0] <= _mx < close_rect[2] and close_rect[1] <= _my < close_rect[3]
                    and draw_state._bounding_hovered)
    _glyph_w = imgui.calc_text_size(fa_x_icon).x
    imgui.get_window_draw_list().add_text(
        close_left + (x_width - _glyph_w) * 0.5, row_top + (row_h - imgui.get_font_size()) * 0.5,
        pack_color(1.0, 1.0, 1.0, 0.9 if _close_hover else 0.35), fa_x_icon)
    imgui.set_cursor_screen_pos((close_left, row_top))
    imgui.dummy(x_width, row_h)
    if draw_state.on_action("left_mouse_down", view_id=f"find_close{unique}",
                            rect=close_rect, priority_delta=4) is not None:
        search_ds.search_active = False
        search_ds._search_was_active = False
        # Keep search_text so reopening the find bar restores the last query.
        Melty.text_focused_ds = None
        request_render()

    if total > 0:
        nav = 0
        # Enter / Down = find next, Shift+Enter / Up = find prev, Ctrl+Enter =
        # "click" the selected result - but only while the FIND BOX (not the
        # underlying editor) holds text focus, so Enter still inserts newlines
        # when you click into the editor. We gate on the box holding text focus
        # directly rather than on `focused_ds is search_ds`: clicking back into
        # the box runs clear_focus, which nulls focused_ds (the searched view
        # isn't under the mouse to be protected), so keying off focused_ds
        # silently dropped Enter-nav after a mouse refocus. text_focused_ds is
        # set straight by the box's own key handler, so it survives that.
        # The find box is single-line, so Up/Down don't move its cursor and are
        # free for stepping matches. Drained from the frame-callback key queue
        # (not imgui.is_key_pressed) so it isn't dropped on slow frames.
        if _box_ds is not None and Melty.text_focused_ds is _box_ds:
            if any(k == glfw.KEY_DOWN for k, _ in Melty.frame_key_events):
                nav = 1
            elif any(k == glfw.KEY_UP for k, _ in Melty.frame_key_events):
                nav = -1
            # Enter steps to the next match, Shift+Enter to the previous, and
            # HOLDING Enter rapid-fires - imgui's synthesized auto-repeat
            # (io.key_repeat_delay/rate) is read here because GLFW's REPEAT events
            # don't reach the key queue on every platform (Wayland); keep the loop
            # rendering while it is held so that cadence is sampled. Ctrl+Enter
            # "clicks" the selected result and stays single-shot (from the queue).
            _enter = [m for k, m in Melty.frame_key_events
                      if k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)]
            _enter_repeat = (imgui.is_key_pressed(glfw.KEY_ENTER, repeat=True)
                             or imgui.is_key_pressed(glfw.KEY_KP_ENTER, repeat=True))
            if imgui.is_key_down(glfw.KEY_ENTER) or imgui.is_key_down(glfw.KEY_KP_ENTER):
                request_render()
            if _enter or _enter_repeat:
                if _enter and (_enter[-1] & glfw.MOD_CONTROL):
                    # "Click" the selected result: hit-test the BVH at the center
                    # of the current match's rect and send a mouse down to the
                    # front-most view there (the actual target, e.g. a managed
                    # window's name label) - exactly what a real click resolves
                    # to. The view's own click handling does the rest (toggle a
                    # window, focus an input, ...). Queued + the view invalidated so
                    # it re-renders and reads the click next frame (the find UI
                    # renders too late to inject for this frame).
                    from meltygui.core.search_core import search_activate_target
                    from meltygui.core.input_handler import InputEvent
                    _target = search_activate_target(Melty.search_current_node)
                    if _target is not None and _target.width and _target.height:
                        _cx = _target.abs_left + _target.width / 2.0
                        _cy = _target.abs_top + _target.height / 2.0
                        _hits = [h for h in Melty.bvh_query(_cx, _cy)
                                 if h._tile_id is not None and not h.just_shadow]
                        _top = _hits[0] if _hits else _target
                        Melty.search_click_pending = (
                            _top._tile_id,
                            InputEvent("left_mouse", "down", tile_id=_top._tile_id, x=_cx, y=_cy))
                        # Re-render the owner's subtree next frame (same as nav) so
                        # the top view actually re-runs and reads the injection.
                        Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
                        request_render()
                elif not imgui.get_io().key_ctrl:
                    nav = -1 if imgui.get_io().key_shift else 1

        if nav != 0:
            # total is the combined count across all views; stepping wraps over
            # the whole result set. Flag a scroll and invalidate the owner's
            # subtree so every child view recomputes and the one holding the new
            # global-current match scrolls to it.
            search_ds.text_search_current = (search_ds.text_search_current + nav) % total
            search_ds._search_nav_pending = True
            Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
            request_render()
    elif search_ds.search_text:
        # Enter with the find box focused force-recomputes the result set. "No
        # results" can be stale - the searched views may have been rebuilt since
        # the count was last done (e.g. a fresh load from disk) - so re-run the
        # sub-view walk on demand rather than leaving it stuck at zero. Flagging
        # _search_nav_pending makes the owner's pre-body walk re-count next frame
        # (same key source + box-focus gate as the nav block above).
        if _box_ds is not None and Melty.text_focused_ds is _box_ds:
            if any(k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
                   for k, _ in Melty.frame_key_events):
                search_ds._search_nav_pending = True
                Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
                request_render()
    else:
        imgui.align_text_to_frame_padding()
        imgui.text_colored("", 0.74, 0.5, 0.5, 1.0)

    if not input_value.search_active:
        draw_state.closed = True

    return False, input_value


def draw_search_highlight_multi(draw_list, segs, *, current):
    """Highlight a match made of per-line segments (a multi-line search term).
    One glow radiates around the segments' BOUNDING box — per-segment glows
    overlap into an unreadable blob — while each segment keeps its own thin
    outline so the exact matched text stays delineated."""

    if len(segs) == 1:
        x0, y0, x1, y1 = segs[0]
        draw_search_highlight(draw_list, x0, y0, x1, y1, current=current)
        return
    # Same pixel-snap as draw_search_highlight - see the comment there.
    segs = [(round(x0), round(y0), round(x1), round(y1))
            for (x0, y0, x1, y1) in segs]
    spec = (Toggles.SearchSettings.ActiveElement if current
            else Toggles.SearchSettings.InactiveElements)
    bx0 = min(s[0] for s in segs)
    bx1 = max(s[2] for s in segs)
    _draw_glow(draw_list, bx0, segs[0][1], bx1, segs[-1][3], spec)
    a = float(spec.outline_alpha)
    if a > 0.0:
        oc = spec.outline_color
        col = pack_color(oc[0], oc[1], oc[2], a)
        for x0, y0, x1, y1 in segs:
            draw_list.add_rect(x0, y0, x1, y1, col,
                               rounding=float(spec.cutout_radius),
                               thickness=float(spec.outline_thickness))


def draw_search_highlight(draw_list, x0, y0, x1, y1, *, current, rounding=0.0):
    """Highlight a search match with its glow (rect cut out) plus an optional
    thin outline. The current match uses the ActiveElement spec; the rest use
    InactiveElements, so both states are tuned independently."""

    # Pixel-snap: the rect usually derives from the live flow cursor at bake
    # time, and sub-pixel drift between bakes makes the gradient triangles
    # shimmer (glyphs don't - they're pixel-snapped). Rounding pins the whole
    # halo to the pixel grid, so successive bakes bake identically.
    x0, y0, x1, y1 = round(x0), round(y0), round(x1), round(y1)
    spec = (Toggles.SearchSettings.ActiveElement if current
            else Toggles.SearchSettings.InactiveElements)
    _draw_glow(draw_list, x0, y0, x1, y1, spec)
    a = float(spec.outline_alpha)
    if a > 0.0:
        oc = spec.outline_color
        draw_list.add_rect(x0, y0, x1, y1, pack_color(oc[0], oc[1], oc[2], a),
                           rounding=max(float(rounding), float(spec.cutout_radius)),
                           thickness=float(spec.outline_thickness))



def search_pill_layout(term, owner_width=None):
    """The find pill's geometry for `term`: ``(box_width, window_width)``.
    The query box grows with the term (a long term stays readable instead
    of scrolling inside a fixed box) from a 200px floor up to what the
    owning view's width leaves for it (`owner_width` minus the pill's
    other parts and a margin), and the window is the row: pad, icon, box,
    gap, count, close, pad. Shared by draw_search (the row) and
    core_render (the window it floats in), so the two always agree."""
    text_w = imgui.calc_text_size(term or "").x
    cap = 520.0 if owner_width is None else max(200.0, owner_width - 220.0)
    box = max(200.0, min(cap, text_w + 40.0))
    return box, box + 158.0


def _perimeter_samples(x0, y0, x1, y1, cseg, r):
    """Walk a rounded-rect perimeter clockwise as (base_x, base_y, nx, ny).

    `r` is the cutout corner radius. The corners are quarter-arc fans of `cseg`
    segments centred on the rect's inset corners, so each sample already sits on
    the rounded cutout; offsetting it by ``base + normal * d`` grows the rounded
    rect by `d` (the corner radius becomes ``r + d``). The straight edges need no
    interior samples: the single quad spanning one corner's exit sample to the
    next corner's entry sample (same outward normal) is geometrically exact.
    """
    r = max(0.0, min(r, (x1 - x0) * 0.5, (y1 - y0) * 0.5))
    half = math.pi / 2.0
    # (inset corner centre, start angle, end angle) - screen space, y points
    # down, so angle 0 = +x (right), +pi/2 = down, -pi/2 = up.
    corners = (
        ((x1 - r, y0 + r), -half, 0.0),               # top-right:    up    -> right
        ((x1 - r, y1 - r), 0.0, half),                # bottom-right: right -> down
        ((x0 + r, y1 - r), half, math.pi),            # bottom-left:  down  -> left
        ((x0 + r, y0 + r), math.pi, math.pi + half),  # top-left:     left  -> up
    )
    out = []
    for (cx, cy), a0, a1 in corners:
        for s in range(cseg + 1):
            ang = a0 + (a1 - a0) * (s / cseg)
            nx, ny = math.cos(ang), math.sin(ang)
            out.append((cx + nx * r, cy + ny * r, nx, ny))
    return out


def _draw_glow(draw_list, x0, y0, x1, y1, spec):
    """Radial gradient halo around (x0,y0,x1,y1) with a rounded rect cut out."""
    falloff = float(spec.falloff)
    opacity = float(spec.opacity)
    if falloff <= 0.0 or opacity <= 0.0:
        return

    cr, cg, cb = spec.gradient_color
    rings = max(1, int(spec.rings))
    exp = float(spec.falloff_exp)
    pad = float(spec.inner_pad)
    cseg = max(1, int(spec.corner_segments))
    radius = float(spec.cutout_radius)

    samples = _perimeter_samples(x0 - pad, y0 - pad, x1 + pad, y1 + pad, cseg, radius)
    n = len(samples)

    # Anti-aliased fill feathers every triangle edge, which leaves visible seams
    # where the gradient triangles meet; turn it off for a clean radial gradient
    # (same trick the swoosh ribbon uses), then restore the previous flags.
    flags = draw_list.flags
    draw_list.flags = flags & ~imgui.DRAW_LIST_ANTI_ALIASED_FILL
    try:
        inner = [(bx, by) for (bx, by, _nx, _ny) in samples]  # cutout edge, d=0
        a_inner = opacity
        for k in range(1, rings + 1):
            t = k / rings
            d = t * falloff
            a_outer = opacity * (1.0 - t) ** exp
            outer = [(bx + nx * d, by + ny * d) for (bx, by, nx, ny) in samples]
            col = pack_color(cr, cg, cb, (a_inner + a_outer) * 0.5)
            for i in range(n):
                j = i + 1 if i + 1 < n else 0
                ix0, iy0 = inner[i]
                ix1, iy1 = inner[j]
                ox0, oy0 = outer[i]
                ox1, oy1 = outer[j]
                draw_list.add_triangle_filled(ix0, iy0, ix1, iy1, ox1, oy1, col)
                draw_list.add_triangle_filled(ix0, iy0, ox1, oy1, ox0, oy0, col)
            inner = outer
            a_inner = a_outer
    finally:
        draw_list.flags = flags
