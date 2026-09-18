"""Header view functions and supporting definitions."""
from meltygui.hdr_color import pack_color
from meltygui.hdr_color import style_color
from meltygui.core.melty import Melty
from meltygui.core.melty import add_to_collection
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.state.core_enums import ProfileMode
from meltygui.state.new_core_model import TileMode
from meltygui.core.runtime.toggles import Tint
from meltygui.core.runtime.toggles import Toggles
from meltygui_imgui.core import _DrawList
from types import NoneType
import colorsys
import meltygui_imgui as imgui
import types


def render_search(search_ds, draw_state, unique=None, width=None, regrab_focus=True):
    """Render the find UI for the searchable view whose state lives on
    `search_ds`: the search input, match count, prev/next nav, and close.

    Shared by draw_header (inline, when the view has a header) and draw_search
    (a floating window, when it doesn't). All state — search_text, match count,
    current index — lives on `search_ds`, the owning view's draw_state, so both
    presentations drive the same search.

    `regrab_focus=False` limits the focus claim to first open (plus the Ctrl+F
    one-shot): an always-visible box (the input tab's filter) must not pull
    focus back whenever nothing holds text focus, or other fields on the same
    tab become untypeable once focus clears.
    """
    from meltygui.core.windowing.glfw_utils import request_render
    import meltygui.core.windowing.window_api as glfw


    from meltygui.view.text_view import draw_text
    # Grab focus on first open, and re-grab whenever nothing holds text focus.
    # Window focus management (move-to-front / window activation) clears
    # text_focused_ds when a window comes forward that doesn't contain the
    # focused field. The floating search box lives in its own window, so
    # without re-grabbing it would drop focus after the first keypress. The
    # re-grab runs before draw_text's key handling, so no keystroke is lost,
    # and it won't steal focus from a deliberate click into the editor (which
    # leaves text_focused_ds non-None).
    # `_search_focus_pending` is the one-shot set when Ctrl+F opened the search:
    # claim focus this frame regardless of who holds text focus (the underlying
    # searchable view can reclaim meltygui text focus before this box renders, so
    # "text_focused_ds is None" alone misses the just-opened case). Consume it so
    # later frames fall back to the gentle re-grab and don't fight a deliberate
    # click into the editor.
    # The re-grab (reclaim focus when nothing holds text focus) is gated on this
    # search still being the active one (focused_ds is the owner). A deliberate
    # click away runs clear_focus, which clears focused_ds, so the box releases
    # focus and stays open-but-unfocused. A spurious clear during typing leaves
    # focused_ds intact, so the box reclaims focus and no keystroke is lost.
    # First-open and the Ctrl+F one-shot grab regardless.
    focus_search = ((not search_ds._search_was_active)
                    or (regrab_focus and Melty.text_focused_ds is None
                        and Melty.focused_ds is search_ds)
                    or search_ds._search_focus_pending)
    # One-shot open/Ctrl+F frame (NOT the spurious-clear regrab): select the
    # whole term so typing replaces it and a single delete clears it.
    focus_fresh = ((not search_ds._search_was_active)
                   or search_ds._search_focus_pending)
    search_ds._search_focus_pending = False
    search_ds._search_was_active = True
    search_icon = ""
    imgui.align_text_to_frame_padding()
    imgui.text(search_icon)
    imgui.same_line()

    if width is None:
        width = draw_state.content_width
    _box = draw_text(search_ds.search_text, searchable=False,
                     is_search_box=True,
                     shadow=False, max_height=40,
                     name=search_icon + str(unique), with_header=None,
                     with_header_end=None, max_width=width - 50,
                     with_footer=None, header_same_line=True, tint=search_ds.tint,
                     show_name=False, show_header=False, single_line=True,
                     request_focus=focus_search, select_all_on_focus=focus_fresh,
                     return_extras=True)
    search_change, new_search = _box[0], _box[1]
    _box_ds = _box[2] if len(_box) > 2 else None
    # While the find box holds text focus, mark this search as the active one so
    # Enter/arrow nav routes here — including after clicking back into the box,
    # where the click restores text focus but not focused_ds.
    if _box_ds is not None and Melty.text_focused_ds is _box_ds:
        Melty.focused_ds = search_ds
    if search_change:
        search_ds.search_text = new_search
        # Re-render the owner's whole subtree so every child view recomputes its
        # matches against the new term and the combined count stays in sync.
        Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
        request_render()
    # initial use
    

    # Match count + prev/next navigation. The count and current index are
    # populated by the searchable view's body (e.g. the text editor); the
    # arrows step the active match and ask the body to scroll it into view.

    imgui.same_line()
    from meltygui.view.control_view import button
    fa_x_icon = ""

    imgui.set_cursor_screen_pos((draw_state.abs_left + width-25, imgui.get_cursor_screen_pos()[1]))
    # imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0], imgui.get_cursor_screen_pos()[1] + 2))
    if button(fa_x_icon, name=f"{unique}##fa_x_icon", show_bg=False,
              use_cache=True, height=23, shadow=True, z_offset=3, max_height=40,
              tile_mode=TileMode.MAX, color=(9, 1, 1, 0))[0]:
        search_ds.search_active = False
        search_ds._search_was_active = False
        # Keep search_text so reopening the find bar restores the last query.
        Melty.text_focused_ds = None

    total = search_ds.text_search_count
    if total > 0:
        imgui.align_text_to_frame_padding()
        imgui.text_colored(f"{search_ds.text_search_current + 1}/{total}",
                           0.66, 0.74, 0.82, 1.0)
        imgui.same_line(spacing=2)
        nav = 0
        if imgui.small_button(f"##search_prev{unique}"):
            nav = -1
        imgui.same_line(spacing=2)
        if imgui.small_button(f"##search_next{unique}"):
            nav = 1
        # Enter / Down = find next, Shift+Enter / Up = find prev, Ctrl+Enter =
        # "click" the selected result — but only while the FIND BOX (not the
        # underlying editor) holds text focus, so Enter still inserts newlines
        # when you click into the editor. We gate on the box holding text focus
        # directly rather than on `focused_ds is search_ds`: clicking back into
        # the box runs clear_focus, which nulls focused_ds (the searched view
        # isn't under the click to be protected), so keying off focused_ds
        # silently dropped Enter-nav after a mouse refocus. text_focused_ds is
        # set straight by the box's own click handler, so it survives that.
        # The find box is single-line, so Up/Down don't move its cursor and are
        # free for stepping matches. Drained from the GLFW-callback key queue
        # (not imgui.is_key_pressed) so it isn't dropped on slow frames.
        if _box_ds is not None and Melty.text_focused_ds is _box_ds:
            if any(k == glfw.KEY_DOWN for k, _ in Melty.frame_key_events):
                nav = 1
            elif any(k == glfw.KEY_UP for k, _ in Melty.frame_key_events):
                nav = -1
            # Enter steps to the next match, Shift+Enter to the previous, and
            # HOLDING Enter rapid-fires — imgui's synthesized auto-repeat
            # (io.key_repeat_delay/rate) is read here because GLFW's REPEAT events
            # don't reach the key queue on every platform (Wayland); keep the loop
            # rendering while Enter is held so that cadence is sampled. Ctrl+Enter
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
                    # of the selected match's rect and send a mouse-down to the
                    # front-most view there (the actual clickable, e.g. a managed
                    # window's name button) — exactly what a real click resolves
                    # to. The view's own click handling does the rest (toggle a
                    # window, focus an input, …). Queued + the tile invalidated so
                    # it re-renders and reads the click next frame (the find UI
                    # renders too late to inject for this frame).
                    from meltygui.core.automation.search_core import search_activate_target
                    from meltygui.core.input.input_handler import InputEvent
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
                        # the clicked view actually re-runs and reads the injection.
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
        imgui.align_text_to_frame_padding()
        imgui.text_colored("No results", 0.74, 0.5, 0.5, 1.0)
        # Enter with the find box focused force-recomputes the result set. "No
        # results" can be stale — the searched views may have been rebuilt since
        # the count was last taken (e.g. a fresh load from disk) — so re-run the
        # cross-view walk on demand rather than leaving it stuck at zero. Flagging
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


def flat_button(label, draw_state, view_id, width=None, height=None,
                color=(0.533, 0.068, 0.5), tint_value=0.16, text_value=1.023,
                factor=1.0, saturation=1.2, text_saturation=0.8, alpha=1.0,
                corner_radius=6.0, text_pad=15, hover_boost=0.05,
                hover_text_boost=2.2, max_bg_brightness=0.25,
                event="left_mouse_clicked", text_offset_x=None,
                style_manager=None, layout=True, draw_list=None,
                shadow=True, shadow_offset=2.0, text_color=None, pos=None,
                hovered=None, style=None, **kwargs):
    """Draw-list button — the fast-dock interaction model instead of a
    @render_func widget (~0.7ms of wrapper per call, measured): a rounded
    rect + centered label straight to the draw list, hover from the live
    mouse position (the owning tile repaints every frame while
    bounding-hovered, so the highlight tracks), and the click claimed
    through the OWNING view's draw_state.on_action rect — the same routing
    the tab bar's dnd clicks already used, so blit-cache event delivery
    holds. Advances the flow like an inline item of the same size (dummy).
    Styling mirrors `button`'s make_color_rgb + brightness-clamp pipeline so
    converted call sites keep their look. alpha=0 draws no bg (label-only
    buttons, e.g. inactive tabs). Returns True on click.

    pos=(x, y) / hovered=: draw-only callers outside any view (the OS-window
    controls in titlebar.py, on the overlay list with their own hit logic)
    place the button and drive its hover state themselves instead of the
    cursor position and the owning draw_state."""
    from meltygui.core.styling.style import Style
    from meltygui.core.cache.tile_cache import add_shadow
    from meltygui.core.layout.header_runtime import _TEXT_COLOR_MEMO
    from meltygui.model.color_model import _brightness_clamp
    import meltygui.core.input.mouse_cursor as mouse_cursor

    if style_manager is None:
        style_manager = Melty.style_manager
    scale = Melty.ui_scale        # Melty.px inlined: ~170 calls a frame
    text = str(label).split("##")[0]
    ts = imgui.calc_text_size(text)
    w = width if width is not None else ts.x + text_pad * scale
    h = height if height is not None else ts.y + 8.0 * scale
    x, y = pos if pos is not None else imgui.get_cursor_screen_pos()
    mx, my = imgui.get_mouse_pos()
    if hovered is None:
        hovered = (draw_state is not None
                   and draw_state._bounding_hovered
                   and not Melty.on_drag
                   and x <= mx < x + w and y <= my < y + h)
    # draw_list: paint into a caller-provided list instead of the window's
    # (e.g. the overlay list a DragDrop ghost rides) — pairs with layout=False.
    dl = draw_list if draw_list is not None else imgui.get_window_draw_list()
    if alpha > 0.0 and color is not None:
        # Shadow under any button that draws a bg — a standalone depth mark
        # (no draw_state for the compositor to shadow; the default offset +2
        # mirrors the legacy active-button z_offset lift). Label-only buttons
        # (alpha=0, e.g. inactive tabs) cast nothing, matching the old
        # per-call-site marks. layout=False draw-only ghosts skip it too —
        # they ride an overlay list outside the mark's snapshotted clip.
        # shadow_offset = the lift (depth delta); a smaller one sits the
        # button lower, so e.g. inactive tabs stay under the active tab.
        rounding = corner_radius * scale
        if shadow and layout:
            add_shadow((x, y, w, h), offset=shadow_offset,
                       corner_radius=rounding)
        if Toggles.dynamic_styles:
            # Legacy callers supply a hue; its strength becomes a residual.
            # Explicit Style values can supply signed shifts or absolutes.
            bg_style = style
            if bg_style is None:
                strength = tint_value + (hover_boost if hovered else 0.0)
                bg_style = Style(tuple(c * strength for c in color[:3]) + (alpha,))
            Melty.add_background(bg_style, rect=(x, y, w, h),
                                 corner_radius=rounding, draw_list=dl)
        else:
            clamp = _brightness_clamp
            bg = style_manager.make_color_rgb(
                color[0], color[1], color[2],
                value=tint_value + (hover_boost if hovered else 0.0),
                factor=factor, saturation_scale=saturation, alpha=1.0)
            bg = clamp(bg[0], bg[1], bg[2], 0.0, max_bg_brightness)
            dl.add_rect_filled(x, y, x + w, y + h,
                               pack_color(bg[0], bg[1], bg[2], alpha),
                               rounding=rounding)
    # text_color: use this exact rgb for the label instead of the theme-mix
    # pipeline below — that pipeline only lets text_value/text_saturation
    # touch `factor` worth of the final color (the rest is the raw `color`),
    # so callers needing FULL-range text control (the editor tabs' hsv
    # knobs) pre-compute the color and pass it here. Hover still brightens.
    if text_color is not None:
        tc_key = (text_color[0], text_color[1], text_color[2], hovered)
        tc = _TEXT_COLOR_MEMO.get(tc_key)
        if tc is None:
            th, tsat, tv = colorsys.rgb_to_hsv(*text_color[:3])
            if hovered:
                tv = min(1.0, tv + 0.25)
            tc = colorsys.hsv_to_rgb(th, tsat, tv)
            if len(_TEXT_COLOR_MEMO) > 2048:
                _TEXT_COLOR_MEMO.clear()
            _TEXT_COLOR_MEMO[tc_key] = tc
    else:
        tc = style_manager.make_color_rgb(
            color[0], color[1], color[2],
            value=text_value + (hover_text_boost if hovered else 0.0),
            factor=factor, saturation_scale=text_saturation, alpha=1.0)
    # text_offset_x: left-align the label at a fixed inset instead of
    # centering — for buttons whose left edge hosts another element (the
    # editor tabs' tint swatch) that centered text would overlap.
    # Optical-centering nudges (same as the fast dock / `button`): glyphs sit
    # low-left of their geometric cell, so shift right and up a hair.
    tx = x + text_offset_x if text_offset_x is not None else x + (w - ts.x) * 0.5
    dl.add_text(tx + 2.0 * scale, y + (h - ts.y) * 0.5 - scale,
                pack_color(tc[0], tc[1], tc[2], 1.0), text)
    # layout=False: draw-only — no dummy (nothing submitted to the window
    # group, so an out-of-flow draw like a DragDrop ghost can't stretch the
    # view's measured content) and no click subscription.
    if not layout:
        return False
    imgui.dummy(w, h)
    if draw_state is None:
        return False
    # `event` picks the trigger: the default full click, or "left_mouse_down"
    # for press-reactive controls (tab switches) that should feel immediate.
    # cursor=ARROW: a button always shows the plain pointer, whatever shape
    # the view it sits in carries (inline buttons in draw_text sit inside
    # the text body's I-beam rect). priority_delta=4 outranks the body's
    # own cursor registrations (draw_text's is at 3), and being an
    # on_action from the owner's body it is replayed on its cache hits.
    fired = draw_state.on_action(event, view_id=view_id,
                                 rect=(x, y, x + w, y + h),
                                 priority_delta=4,
                                 cursor=mouse_cursor.ARROW) is not None
    # Effect ledger: a fired flat button is an observable effect with no
    # undo record — the Orchestrator cues replays off it (view_id names the
    # button; the rect gives the cue press-fraction geometry, so button
    # clicks generalize like leaf-editor presses).
    if fired and Melty.effect_hook is not None:
        try:
            Melty.effect_hook("button", str(view_id).split("##")[0], draw_state,
                              rect=(x, y, w, h))
        except Exception:
            pass
    return fired


def draw_header_arrow(expanded, color=None, alpha=0.071):
    """The header's transparent tree control, also usable by flat views.
    Dimmed by Toggles.Melty.arrow_brightness (every arrow, everywhere)."""
    from meltygui.core.runtime.toggles import Toggles
    dim = float(Toggles.Melty.arrow_brightness)
    alpha = alpha * dim
    if color is None:
        depth = max(0.0, Melty.bg_depth)
        color = Melty.style_manager.make_color_style_value(input={
            "value": (7.788 + (depth - 30.0) * 0.05 * 0.332) * dim,
            "saturation": 1.559 + (depth - 1.773) * -0.004,
            "alpha": alpha, "max_value": 1.601})
    else:
        color = tuple(c * dim for c in color[:3]) + tuple(color[3:])
    # The arrow's transparency rides IN the colour, packed by style_color:
    # imgui multiplies a style colour's alpha BYTE by STYLE_ALPHA, and in
    # Melty's vertex colour that byte's top bit is the SDR flag (hdr_color).
    # A small style alpha cleared it, so the shader decoded the arrow's
    # saturated bytes as an HDR colour at the top of the range - the arrow
    # came out far too bright. STYLE_ALPHA is pinned to 1 around the button
    # (the header pushes its own opacity) so the packed byte arrives intact.
    imgui.push_style_color(imgui.COLOR_TEXT, *style_color(*color[:3], alpha))
    imgui.push_style_color(imgui.COLOR_BUTTON, 0.0, 0.0, 0.0, 0.0)
    imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.0, 0.0, 0.0, 0.0)
    imgui.push_style_var(imgui.STYLE_ALPHA, 1.0)
    try:
        imgui.set_item_allow_overlap()
        return imgui.arrow_button("##tree", imgui.DIRECTION_DOWN if expanded else imgui.DIRECTION_RIGHT)
    finally:
        imgui.pop_style_var()
        imgui.pop_style_color(3)


def draw_header(input_value=None, name="", key=None, meltygui=None, parent_show_add_delete=False, width=7, suffix="",
                collection=None, icon=None, display_name=None, meta=None, unique=None, is_tree=True,
                show_name=True, name_func=None, show_type=False, show_unique=False, name_color=None,
                on_search=False, trigger_collapse=False, trigger_expand=False, header_same_line=False,
                draw_state=None, show_tint=False, opacity=1.23, show_add_delete=True, new_item_type=types.NoneType,
                show_add_types=None, on_drag=False, on_action=None, style_manager=None, font=None,
                **kwargs):
    # Constants
    from meltygui.core.conversion.bubbling import _BubblingDict
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.utils.render_utils import pop_style_var
    from meltygui.utils.render_utils import push_style_var
    from meltygui.view.search_view import draw_search_highlight
    from meltygui.core.layout.cursor_core import same_line
    from meltygui.core.layout.header_core import _jump_to_view_source
    from meltygui.model.collection_model import annotation_item_type

    _font_pushed = False


    if font is not None and Melty.font_mgr is not None:
        _font_handle = Melty.font_mgr.get(font)
        if _font_handle is not None:
            imgui.push_font(_font_handle)
            _font_pushed = True
          

    # Depth drives name brightness
    depth_scale       = 0.05
    depth_offset      = -30.0
    # Depth drives text saturation falloff
    sat_depth_factor  = -0.004
    sat_depth_offset  = -1.773

    draw_list = imgui.get_window_draw_list()
    depth = max(0.0, Melty.bg_depth)
    depth_intensity = float(depth + depth_offset) * depth_scale

    sat_shift = float(depth + sat_depth_offset) * sat_depth_factor
    # Name text (value is the base offset, updated by depth below)
    name_style = {
        'value': 1.188, 'saturation': 0.778,
        'alpha': 0.174, 'max_value': 3.921,
        'depth_factor': 0.729
    }
    name_rounding       = 2.696
    max_name_chars      = 40
    min_name_text_width = 62

    # Type / unique label colors
    type_label_tint   = (3.672, 1.944, 2.861, 1.0)
    unique_label_tint = (-1.535, 0.0, 0.9, 1.0)

    # Depth-driven color computati
    # on
    name_style['value'] = depth_intensity * name_style['depth_factor'] + name_style['value']
    name_style['saturation'] = name_style['saturation'] + sat_shift

    if name_color is not None:
        name_color = style_manager.make_color_style_rgb(*name_color, input=name_style, factor=0.1)
    else:
        name_color = style_manager.make_color_style_value(input=name_style, value=0.5)
    arrow_style = {
        'value': 7.788, 'saturation': 1.559,
        'alpha': 0.071, 'max_value': 1.601,
        'depth_factor': 0.332
    }
    

    # imgui.same_line(False) # does not work
    # imgui.set_cursor_position(0,0) # does not work
    
    # imgui.set_cursor_pos((0,0))
    # def set_cursor(tuple_in):
    #     pass
        
    # set_cursor((0,0)) # fine
    # set_cursor(0,0) # This one works
    # set_cursor(*(0,0)) # does not work
    
    
    arrow_style['value'] = depth_intensity * arrow_style['depth_factor'] + arrow_style['value']
    arrow_style['saturation'] = arrow_style['saturation'] + sat_shift
    arrow_color = style_manager.make_color_style_value(input=arrow_style)

    # ── Tree arrow ─────────────────────────────────────────────
    imgui.dummy(5, 0)
    start_x, start_y = imgui.get_cursor_screen_pos()

    on_change = False
    return_val = on_action
    push_style_var(imgui.STYLE_ALPHA, opacity)
    if display_name is not None:
        name = display_name
    imgui.align_text_to_frame_padding()

    if is_tree:
        imgui.set_cursor_screen_pos(imgui.get_cursor_screen_pos())
        imgui.dummy(0, 0)
        imgui.same_line(spacing=0)

        if draw_header_arrow(draw_state.expanded, arrow_color, arrow_style['alpha']):
            draw_state.expanded = not draw_state.expanded
            # Effect ledger: expand/collapse is draw_state UI state — no
            # undo record — but it IS the gate the orchestration machinery
            # keys on (the field-behind-a-collapsed-parent story). The
            # direction rides in the kind so replay verification catches a
            # wrong toggle (an "expand" replayed onto an already-expanded
            # view collapses it — the cue then fails honestly).
            if Melty.effect_hook is not None:
                try:
                    Melty.effect_hook(
                        "expand" if draw_state.expanded else "collapse",
                        str(getattr(draw_state, "name", "?")).split("##")[0],
                        draw_state)
                except Exception:
                    pass
            draw_state.content_height = 0
            draw_state.invalid_content_height = True
            request_render()
        same_line()
    else:
        imgui.same_line(spacing=0)

    # ── Type / unique labels ───────────────────────────────────
    if show_type:
        imgui.text_colored(f"({input_value.__class__.__name__})", *type_label_tint)
        same_line()
    if show_unique:
        imgui.text_colored(f"({str(Melty.get_tile_id())})", *unique_label_tint)
        same_line()
    if show_name and name != "":
        same_line(spacing=0)
        imgui.set_item_allow_overlap()

    # ── Tint widget ────────────────────────────────────────────
    # A dict can carry its tint in __overrides__ (parsed from a `# [tint=(...)]`
    # comment); edit that store directly so the change round-trips to source.
    # The override comment is itself the opt-in, so this isn't gated on
    # show_tint (which is only set for top-level windows, not nested classes).
    # (Legacy per-storage tint chain removed — the anywhere swatch below IS
    # the tint widget: one loop for every source, set_anywhere on write.)
    _overrides = input_value.get("__overrides__") if isinstance(input_value, dict) else None

    # ── Anywhere tint swatch (outlined) ── one attribute, both directions:
    # `draw_state.locate_tint` READS the framework-resolved tint
    # (draw_state._kwargs, in-flight cache included) and ASSIGNING it writes
    # back to whichever source DRIVES it — code, comment, decoration,
    # instance attr. (The property pair lives in anywhere.py; this is its
    # first caller.) Outlined so the swatch reads apart from the labels.
    from meltygui.core.rendering.parameter_core import get_source_for
    _aw_attr = "tint"
    _aw_tint = draw_state.locate_tint
    if Toggles.dynamic_styles and draw_state.locate_style is not None:
        _aw_attr = "style"
        _aw_tint = draw_state.locate_style

    # The caption re-resolves this often while it still reads the draw_state
    # fallback: the view's code hosts load in the background, so the first
    # popover open (which creates them) resolves before the source that
    # really drives the tint has parsed — a meltygui app's `@glfw_window(tint=)`
    # showed as "draw_state" until a write forced a fresh collection (09-12).
    # [tint=(0.85, 0.55, 0.25)]
    source_retry_frames = 30

    def _aw_source_info(_ds=draw_state, _attr=_aw_attr):
        # Popover caption: the LAST-KNOWN driving source. Reads the cache
        # set_anywhere maintains; resolves (one collection) on first popover
        # open, then caches on the ds — re-resolving only while the answer is
        # the draw_state fallback (see source_retry_frames), never per frame.
        _last = getattr(_ds, "_sa_last_source", None)
        _src = _last.get(_attr) if _last else None
        _frame = Melty.frame_count
        _stale = (_src == "draw_state"
                  and _frame - getattr(_ds, "_sa_source_frame", -source_retry_frames) >= source_retry_frames)
        if _src is None or _stale:
            _src = get_source_for(_attr, _ds)
            _ds._sa_source_frame = _frame
            if _src is not None:
                if _last is None:
                    _last = {}
                    _ds._sa_last_source = _last
                _last[_attr] = _src
        return f"  {_src}" if _src else None  # FA location-arrow

    if _aw_tint is not None and (show_tint or (
            isinstance(_overrides, dict) and _overrides.get("tint") is not None)):
        draw_state._has_popup = True
        # Named on the header's own `unique` — NEVER Melty.get_tile_id(),
        # which reflects the current tile pass and shifts during edits
        # (popover open, forced invalidations), re-keying the widget mid-drag
        # so its fresh draw_state re-measures and the row wraps. The outline
        # is drawn by draw_tuple around the chip itself (outline=True) — the
        # widget's draw_state box is far wider than the swatch.
        imgui.same_line(spacing=0)
        # draw_tuple_fast, not the draw_tuple render_func: every window
        # header paid a full wrapper call per frame for this 17 px chip
        # (~0.19 ms each, use_cache=False). The chip claims its own 17×17
        # footprint here since the fast path draws without layout.
        from meltygui.view.collection_view import draw_tuple_fast
        _aw_x, _aw_y = imgui.get_cursor_screen_pos()
        _aw_ch, _aw_val = draw_tuple_fast(
            _aw_tint, draw_state, view_id="aw_tint", x=_aw_x, y=_aw_y,
            size=17, outline=True, info=_aw_source_info,
            setter=lambda value, _ds=draw_state, _attr=_aw_attr: setattr(_ds, "locate_" + _attr, value),
            view_owner=draw_state)
        imgui.dummy(17, 17)
        if _aw_ch:
            setattr(draw_state, "locate_" + _aw_attr, _aw_val)
        same_line()

    # ── Add button ─────────────────────────────────────────────
    if show_add_delete and (isinstance(input_value, (list, dict, _BubblingDict)) or hasattr(input_value, "__dict__")):
        def _instantiate_and_add(item_type):
            if item_type is NoneType:
                item_type = annotation_item_type(kwargs.get("annotation")) or NoneType
            try:
                new_item = item_type()
            except Exception as e:
                print(f"Could not instantiate {item_type} for add: {e}")
                new_item = None
            add_to_collection(input_value, new_item)


        if show_add_delete:
            if flat_button(f"\uf067##add{unique}", draw_state,
                           view_id=f"hdr_add{unique}"):
                _instantiate_and_add(new_item_type)
                on_change = True
                return_val = input_value
            same_line()

        # ── Typed add button ───────────────────────────────────
        # A second + button whose item type comes from show_add_types — a
        # {display_name: type} dict (a bare list/tuple keys itself by
        # __name__). With several entries a small chevron dropdown picks the
        # type: the pick persists BY DISPLAY NAME on draw_state.misc (a plain
        # str survives save/load and hotswap; the type is re-resolved from
        # show_add_types each frame) and the + relabels to "+ <name>". A
        # single entry needs no choice — no chevron, the + is pre-bound to
        # it. Until a type is picked the + falls back to the same hint-based
        # default as the plain add button.
        if show_add_types:
            if not isinstance(show_add_types, dict):
                show_add_types = {t.__name__: t for t in show_add_types}
            if len(show_add_types) == 1:
                sel_name = next(iter(show_add_types))
            else:
                sel_name = draw_state.misc.get("add_type_name")
            sel_type = show_add_types.get(sel_name)
            add_label = f"\uf067 {sel_name}" if sel_type is not None else "\uf067"
            if flat_button(f"{add_label}##add_typed{unique}", draw_state,
                           view_id=f"hdr_add_typed{unique}"):
                _instantiate_and_add(sel_type if sel_type is not None else new_item_type)
                on_change = True
                return_val = input_value
            if len(show_add_types) > 1:
                same_line(spacing=0)
                _dd_pos = imgui.get_cursor_screen_pos()
                type_changed, picked = RenderFuncs.draw_dropdown(
                    "\uf078", collection=show_add_types, z_offset=-1,
                    name=f"add_type{unique}", width=20, show_header=False, show_bg=False, show_button_bg=False,
                    show_name=False, shadow=False)
                if type_changed and any(v is picked for v in show_add_types.values()):
                    draw_state.misc["add_type_name"] = next(
                        k for k, v in show_add_types.items() if v is picked)
                    draw_state.invalidate()
                    request_render()
                # The dropdown's (hidden) popover window leaves the imgui cursor on
                # a new line; same_line() would chain off that, so restore the
                # cursor to just right of the chevron for the name that follows.
                # Width mirrors the trigger's own sizing (passed width, with the
                # glyph + compact pad 6 as the floor) so the name never starts
                # inside the button.
                _chev_w = max(24, imgui.calc_text_size("\uf078")[0] + 6)
                imgui.set_cursor_screen_pos((_dd_pos[0] + _chev_w + 3, _dd_pos[1]))
            else:
                same_line()
                

    # ── Name label / edit ──────────────────────────────────────
    has_visible_name = show_name and name not in ("", None, "None")

    if has_visible_name:
        clipped_name = name.split("##")[0][:max_name_chars] + " "
        if header_same_line:
            text_width = imgui.calc_text_size(clipped_name)[0] - 4
        else:
            text_width = imgui.calc_text_size(clipped_name)[0]

        if icon is not None:
            imgui.align_text_to_frame_padding()
            # The icon glyph renders 2px below the name text baseline.
            _ix, _iy = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((_ix, _iy - 2))
            imgui.text_colored(icon, *Tint.icon_tint())
            imgui.same_line()
            _nx, _ny = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((_nx, _ny + 2))

        push_style_var(imgui.STYLE_FRAME_ROUNDING, name_rounding)

        if not draw_state._name_edit:

            draw_list: _DrawList = imgui.get_window_draw_list()
            cursor_pos = imgui.get_cursor_screen_pos()
            # Search-match highlight behind the key name (drawn before the text
            # so glyphs stay readable). Strong fill + outline for the active
            # (global-current) match; a faint fill for the rest. The flags are
            # set by draw_collection when this header's key matches the query.
            # Lifted to a HIGHER depth channel (restore after, the
            # DrawState.draw_rect idiom): the halo spills past this row's rect,
            # and at the content channel a sibling row's background composites
            # over the spill — cropped edges and bake-order flicker.
            if kwargs.get("search_match", False):
                hx0, hy0 = cursor_pos[0], cursor_pos[1]
                hx1 = cursor_pos[0] + text_width
                hy1 = cursor_pos[1] + imgui.get_text_line_height()
                if Melty.channels_split:
                    draw_list.channels_set_current(
                        min(Melty.get_channel() + 2, Melty.max_depth - 1))
                draw_search_highlight(draw_list, hx0, hy0, hx1, hy1,
                                      current=kwargs.get("search_current", False))
                if Melty.channels_split:
                    draw_list.channels_set_current(Melty.get_channel())
            packed_name_color = pack_color(*name_color[:3], 1.0)
            draw_list.add_text(cursor_pos[0], cursor_pos[1], packed_name_color, clipped_name)
            imgui.dummy(text_width, imgui.get_frame_height())
            pop_style_var(1)
        else:
            name_width = min(text_width, min_name_text_width)
            imgui.set_next_item_width(name_width)
            edit_flags = imgui.INPUT_TEXT_ENTER_RETURNS_TRUE | imgui.INPUT_TEXT_AUTO_SELECT_ALL
            changed, new_name = imgui.input_text(f"##edit{name}_{unique}", name, flags=edit_flags)
            pop_style_var(1)

            if changed or imgui.is_key_pressed(imgui.KEY_ESCAPE) or not imgui.is_item_active():
                draw_state._name_edit = False

        same_line(spacing=0)

    end_x = imgui.get_cursor_screen_pos()[0]

    # ── Ctrl+B on the header → the view function's source ──────
    # Same landing as a global-search Code hit (_jump_to_symbol_def): the
    # editor opens on the `def` of whatever function renders this view.
    # Hover-scoped to the header's own rect, so the editor's Ctrl+B (usage
    # jump, hover-routed to the text body) is untouched.
    header_rect = (start_x, start_y, end_x, start_y + imgui.get_frame_height())
    if draw_state.on_action("ctrl_b_down", view_id=f"hdr_jump{unique}",
                            rect=header_rect) is not None:
        # Fire-and-forget flash on the header itself so the jump reads as
        # "from here" while the editor comes to front (same flash as the
        # editor's own jump landing / merge Apply).
        Melty.emphasize(f"hdr_jump {draw_state.name}",
                        (header_rect[0] - 3, header_rect[1] - 2,
                         header_rect[2] + 3, header_rect[3] + 2))
        _jump_to_view_source(draw_state)

    # ── Profiler ───────────────────────────────────────────────
    is_profiling = Toggles.profile_mode == ProfileMode.ON
    if is_profiling:
        from meltygui.view.diagnostic_view import render_profiler_time
        render_profiler_time(
            input_value=draw_state.render_time, brief=True,
            style_manager=style_manager,
        )
        same_line(spacing=3)

    pop_style_var(1)

    if _font_pushed:
        imgui.pop_font()
    # Record the natural (pre-pad) header width so core_render can fold it into
    # the parent window's running max for the next frame.
    draw_state.header_natural_width = end_x - start_x
    if kwargs.get("align_header", True) and show_name:
        # Pad to the widest header in this window (tracked per-window), with the
        # preferred width as a floor. Falls back to the floor when no window.
        parent_window = draw_state.parent_window
        pad_target = Toggles.Collection.preferred_header_width
        if parent_window is not None:
            pad_target = max(pad_target, parent_window.max_header_width)
        if end_x - start_x < pad_target:
            imgui.dummy(pad_target - (end_x - start_x), 1)
            imgui.same_line(8)

    return on_change, return_val


def draw_footer(input_value=None, name="", key=None, meltygui=None, parent_show_add_delete=False, width=0, suffix="",
                collection=None, display_name=None, meta=None, unique=None, is_tree=True,
                show_name=True, name_func=None, show_type=False, show_unique=False,
                on_search=False, trigger_collapse=False, trigger_expand=False,
                draw_state=None, show_tint=False, opacity=1.0, show_add_delete=True,
                on_drag=False, on_action=None, style_manager=None,
                **kwargs):

    # for key, pending in draw_state._all_pending.items():
    #     if pending is not None:
    #         if pending.state == PendingState.ERROR:
    #             from meltygui.core.rendering.render_dispatch import draw_pending
    #             draw_pending(pending, name=f"{key}", tint=(1, 0, 0))

    imgui.dummy(1,1)


def draw_header_end(input_value=None, name="", show_close=True, key=None, meltygui=None, parent_show_add_delete=False,
                    collection=None, draw_state=None, closable=False, style_manager=None,
                    unique=None, show_tint=False, **kwargs):
    from meltygui.core.layout.cursor_core import same_line

    close_icon = ""
    # Frame-redraw indicator (left of the close button): alternates these
    # two glyphs every frame, so a header that repaints continuously flickers.
    redraw_icon_0 = ""
    redraw_icon_1 = ""
    # [tint=(1.0, 1.0, 1.0)]
    redraw_alpha = 0.1
    redraw_pad_x = 3

    if closable:
        if show_tint:
            # Paint-only (no dummy / same_line): the glyph must not claim
            # layout in the right-aligned end-header group, or it would
            # push the close button left. It hangs off the button's left
            # edge, centered on the row height flat_button gives the
            # button (text + px(8)).
            button_height = imgui.calc_text_size(close_icon)[1] + Melty.px(8.0)
            redraw_icon = [redraw_icon_0, redraw_icon_1][Melty.frame_count % 2]
            # One slot as wide as the wider glyph, each glyph centered in it —
            # anchoring by the glyph's own width made the two alternate ~2px apart.
            slot_width = max(imgui.calc_text_size(redraw_icon_0)[0],
                             imgui.calc_text_size(redraw_icon_1)[0])
            icon_width, icon_height = imgui.calc_text_size(redraw_icon)
            button_x, row_top = imgui.get_cursor_screen_pos()
            icon_x = button_x - redraw_pad_x - slot_width + (slot_width - icon_width) / 2
            icon_y = row_top + (button_height - icon_height) / 2
            redraw_color = pack_color(1, 1, 1, redraw_alpha)
            imgui.get_window_draw_list().add_text(icon_x, icon_y, redraw_color, redraw_icon)
        if show_close:
            if flat_button(f"{close_icon}##{unique}", draw_state,
                           view_id=f"hdr_close{unique}",
                           color=(9, 1, 1)):
                _was_closed = draw_state.closed
                from meltygui.core.windowing.window_visibility import native_user_window_closed
                native_user_window_closed(draw_state, not draw_state.closed)
                from meltygui.state.core_undo import NavUndo
                from meltygui.core.windowing.window_visibility import window_edit_is_local
                if window_edit_is_local(draw_state, 'closed'):
                    NavUndo.record_window(draw_state, _was_closed, draw_state.closed)
                Melty.cache.invalidate_up_by_obj(Melty.registered_windows)
    
                # draw_state._parent.invalidate_up()
                if draw_state.parent_window is not None:
                    draw_state.parent_window.invalidate_up()
    
                draw_state.dlt_count = 0
                # if draw_state.parent_window is not None:
            #     Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10)
    else:
        if parent_show_add_delete and collection is not None and key is not None:
            if flat_button(f"\uf1f8##del{unique}", draw_state,
                           view_id=f"hdr_del{unique}",
                           color=(0.12, 0.002037035, 0.002037035), alpha=0.4):
                Melty.to_delete(key, collection)
            same_line(spacing=0.0)
