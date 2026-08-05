import os
import sys
import types
from types import NoneType
from typing import MutableMapping

import glfw
import imgui
from imgui.core import _DrawList

from src.lsd.gl_gui.global_style import GlobalStyle
from src.lsd.gl_gui.melty import Melty, add_to_collection
from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.model.core_model.draw_state import TileMode
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Toggles, Tint
from src.lsd.gl_gui.utils.custom_views import push_style_var, push_style_color, pop_style_color, pop_style_var
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.view.core_conversion.bubbling import _BubblingDict
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.search_glow import draw_search_highlight
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window


def open_file(path, app=None):
    def default_file_manager():
        # Detect platform
        if sys.platform.startswith('darwin'):
            return "open"
        elif os.name == 'nt':
            return "explorer"
        elif os.name == 'posix':
            return "nemo"

    if app is None:
        app = default_file_manager()

    import subprocess
    if os.path.exists(path):
        subprocess.Popen([app, path])
    else:
        print(f"Path does not exist: {path}")



def annotation_item_type(annotation):
    """Item type a collection annotation implies for new entries:
    Dict[str, Lora] -> Lora, List[X] -> X, Optional[T] -> T. None when the
    annotation carries no usable element type."""
    args = [a for a in getattr(annotation, "__args__", ()) if a is not NoneType]
    if not args:
        return None
    return args[-1]


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

    from src.lsd.gl_gui.view.core_views.text_editor import draw_text
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
    # searchable view can reclaim melty text focus before this box renders, so
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
    from src.lsd.gl_gui.view.core_views.new_core_view import button
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
                    from src.lsd.gl_gui.view.core_views.new_core_view import search_activate_target
                    from src.lsd.gl_gui.events.input_handler import InputEvent
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


@window
def draw_header(input_value=None, name="", key=None, melty=None, parent_show_add_delete=False, width=7, suffix="",
                collection=None, icon=None, display_name=None, meta=None, unique=None, is_tree=True,
                show_name=True, name_func=None, show_type=False, show_unique=False, name_color=None,
                on_search=False, trigger_collapse=False, trigger_expand=False, header_same_line=False,
                draw_state=None, show_tint=False, opacity=1.23, show_add_delete=True, new_item_type=types.NoneType,
                show_add_types=None, on_drag=False, on_action=None, style_manager=None, font=None,
                **kwargs):
    # Constants
    _font_pushed = False


    if font is not None and Melty.font_mgr is not None:
        _font_handle = Melty.font_mgr.get(font)
        if _font_handle is not None:
            imgui.push_font(_font_handle)
            _font_pushed = True
            
    # Depth drives name brightness
    depth_scale       = 0.06
    depth_offset      = -30.0
    # Depth drives text saturation falloff
    sat_depth_factor  = -0.004
    sat_depth_offset  = -1.773
    spinner_icon_0 = ""
    spinner_icon_1 = ""

    draw_list = imgui.get_window_draw_list()
    spinner_icon_idx = Melty.frame_count % 2
    spinner_icon = [spinner_icon_0, spinner_icon_1][spinner_icon_idx]
    icon_width = imgui.calc_text_size(spinner_icon)[0]/2
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
    arrow_style['value'] = depth_intensity * arrow_style['depth_factor'] + arrow_style['value']
    arrow_style['saturation'] = arrow_style['saturation'] + sat_shift
    arrow_color = style_manager.make_color_style_value(input=arrow_style)

    # ── Tree arrow ─────────────────────────────────────────────
    imgui.dummy(5, 0)
    start_x = imgui.get_cursor_screen_pos()[0]

    on_change = False
    return_val = on_action
    push_style_var(imgui.STYLE_ALPHA, opacity)
    if display_name is not None:
        name = display_name
    imgui.align_text_to_frame_padding()

    if is_tree:
        push_style_color(imgui.COLOR_TEXT, *arrow_color[:3])
        imgui.set_cursor_screen_pos(imgui.get_cursor_screen_pos())
        imgui.dummy(0, 0)
        imgui.same_line(spacing=0)

        imgui.push_style_color(imgui.COLOR_BUTTON, 0.0, 0.0, 0.0, 0.0)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.0, 0.0, 0.0, 0.0)
        imgui.push_style_var(imgui.STYLE_ALPHA, arrow_style['alpha'])
        imgui.set_item_allow_overlap()

        arrow_dir = imgui.DIRECTION_DOWN if draw_state.expanded else imgui.DIRECTION_RIGHT
        if imgui.arrow_button("##tree", arrow_dir):
            draw_state.expanded = not draw_state.expanded
            draw_state.content_height = 0
            draw_state.invalid_content_height = True
            request_render()
        imgui.pop_style_var(1)
        imgui.pop_style_color(2)
        pop_style_color(1)
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
    from src.lsd.gl_gui.view.core_views.anywhere import get_source_for
    _aw_tint = draw_state.locate_tint

    def _aw_source_info(_ds=draw_state):
        # Popover caption: the LAST-KNOWN driving source. Reads the cache
        # set_anywhere maintains; resolves (one collection) only on first
        # popover open, then caches on the ds — never per frame.
        _last = getattr(_ds, "_sa_last_source", None)
        _src = _last.get("tint") if _last else None
        if _src is None:
            _src = get_source_for("tint", _ds)
            if _src is not None:
                if _last is None:
                    _last = {}
                    _ds._sa_last_source = _last
                _last["tint"] = _src
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
        _aw_ch, _aw_val = RenderFuncs.draw_tuple(
            _aw_tint, show_name=False, show_header=False,
            name=f"aw_tint", info=_aw_source_info)
        if _aw_ch:
            draw_state.locate_tint = _aw_val
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
            if RenderFuncs.button(f"\uf067##add{unique}", name=f"\uf067##add{unique}")[0]:
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
            if RenderFuncs.button(f"{add_label}##add_typed{unique}", name=f"\uf067##add_typed{unique}")[0]:
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
            if kwargs.get("search_match", False):
                hx0, hy0 = cursor_pos[0], cursor_pos[1]
                hx1 = cursor_pos[0] + text_width
                hy1 = cursor_pos[1] + imgui.get_text_line_height()
                draw_search_highlight(draw_list, hx0, hy0, hx1, hy1,
                                      current=kwargs.get("search_current", False))
            packed_name_color = imgui.get_color_u32_rgba(*name_color[:3], 1.0)
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

    if draw_state.closable and show_tint:
        spinner_color = imgui.get_color_u32_rgba(1, 1, 1, 0.1)
        draw_list.add_text(imgui.get_cursor_screen_pos()[0] - icon_width + 10,
                           imgui.get_cursor_screen_pos()[1], spinner_color, spinner_icon)
        imgui.dummy(15, 15)
        imgui.same_line()

    end_x = imgui.get_cursor_screen_pos()[0]

    # ── Profiler ───────────────────────────────────────────────
    is_profiling = Toggles.profile_mode == ProfileMode.ON
    if is_profiling:
        from src.lsd.gl_gui.view.core_views.new_core_view import render_profiler_time
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

    # Depth-driven color computation
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
    arrow_style['value'] = depth_intensity * arrow_style['depth_factor'] + arrow_style['value']
    arrow_style['saturation'] = arrow_style['saturation'] + sat_shift
    arrow_color = style_manager.make_color_style_value(input=arrow_style)


    # ── Tree arrow ─────────────────────────────────────────────
    imgui.dummy(5, 0)
    start_x = imgui.get_cursor_screen_pos()[0]

    on_change = False
    return_val = on_action
    push_style_var(imgui.STYLE_ALPHA, opacity)
    if display_name is not None:
        name = display_name
    imgui.align_text_to_frame_padding()

    if is_tree:
        push_style_color(imgui.COLOR_TEXT, *arrow_color[:3])
        imgui.set_cursor_screen_pos(imgui.get_cursor_screen_pos())
        imgui.dummy(0, 0)
        imgui.same_line(spacing=0)

        imgui.push_style_color(imgui.COLOR_BUTTON, 0.0, 0.0, 0.0, 0.0)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.0, 0.0, 0.0, 0.0)
        imgui.push_style_var(imgui.STYLE_ALPHA, arrow_style['alpha'])
        imgui.set_item_allow_overlap()

        arrow_dir = imgui.DIRECTION_DOWN if draw_state.expanded else imgui.DIRECTION_RIGHT
        if imgui.arrow_button("##tree", arrow_dir):
            draw_state.expanded = not draw_state.expanded
            draw_state.content_height = 0
            draw_state.invalid_content_height = True
            request_render()
        imgui.pop_style_var(1)
        imgui.pop_style_color(2)
        pop_style_color(1)
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
    from src.lsd.gl_gui.view.core_views.anywhere import get_source_for
    _aw_tint = draw_state.locate_tint

    def _aw_source_info(_ds=draw_state):
        # Popover caption: the LAST-KNOWN driving source. Reads the cache
        # set_anywhere maintains; resolves (one collection) only on first
        # popover open, then caches on the ds — never per frame.
        _last = getattr(_ds, "_sa_last_source", None)
        _src = _last.get("tint") if _last else None
        if _src is None:
            _src = get_source_for("tint", _ds)
            if _src is not None:
                if _last is None:
                    _last = {}
                    _ds._sa_last_source = _last
                _last["tint"] = _src
        return f"  {_src}" if _src else None  # FA location-arrow

    if _aw_tint is not None and (show_tint or (
            isinstance(_overrides, dict) and _overrides.get("tint") is not None)):
        draw_state._has_popup = True
        _aw_ch, _aw_val = RenderFuncs.draw_tuple(
            _aw_tint, show_name=False, show_header=False,
            name=f"aw_tint", info=_aw_source_info)
        if _aw_ch:
            draw_state.locate_tint = _aw_val
        same_line()

    # ── Add button ─────────────────────────────────────────────
    if show_add_delete and (isinstance(input_value, (list, dict, _BubblingDict)) or hasattr(input_value, "__dict__")):
        if show_add_delete:
            if RenderFuncs.button(f"\uf067##add{unique}", name=f"\uf067##add{unique}")[0]:
                hinted_type = new_item_type
                if hinted_type is NoneType:
                    hinted_type = annotation_item_type(kwargs.get("annotation")) or NoneType
                try:
                    new_item = hinted_type()
                except Exception as e:
                    print(f"Could not instantiate {hinted_type} for add: {e}")
                    new_item = None
                add_to_collection(input_value, new_item)
                on_change = True
                return_val = input_value
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
            # Clip the raw draw-list calls (highlight + add_text) to the owning
            # view's bg rect — they bypass layout clipping, so a long name
            # otherwise paints past the background. The bg is drawn by the
            # wrapper at (abs_left, abs_top, width, height) of the view that
            # spawned this header (our _parent), so clip to that same rect.
            # Live _abs_left/_abs_top, not cached abs pos.
            _owner = draw_state._parent
            if _owner is draw_state or _owner is None or not (_owner.width and _owner.width > 5):
                _owner = draw_state.parent_window
            _name_clip = None
            if _owner is not None and _owner.width > 0:
                _o_left, _o_top = _owner._abs_left(), _owner._abs_top()
                # The bg fill is inset by border_inset (~3, see draw_bg) and
                # header content starts outline_margin (3) in from the left, so
                # stop the text the same margin short of the right edge instead
                # of letting it touch the outline/rounded corner.
                _o_right = _o_left + _owner.width - 3
                # Unmeasured first-frame height would collapse the clip and
                # blank the name; fall back to at least one text line.
                _o_bottom = _o_top + max(_owner.height or 0,
                                         imgui.get_text_line_height())
                _name_clip = (_o_left, _o_top, _o_right, _o_bottom)
                Melty.push_clip(_name_clip)
            if kwargs.get("search_match", False):
                hx0, hy0 = cursor_pos[0], cursor_pos[1]
                hx1 = cursor_pos[0] + text_width
                hy1 = cursor_pos[1] + imgui.get_text_line_height()
                draw_search_highlight(draw_list, hx0, hy0, hx1, hy1,
                                      current=kwargs.get("search_current", False))
            packed_name_color = imgui.get_color_u32_rgba(*name_color[:3], 1.0)
            draw_list.add_text(cursor_pos[0], cursor_pos[1], packed_name_color, clipped_name)
            if _name_clip is not None:
                Melty.pop_clip()
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

    if draw_state.closable and show_tint:
        spinner_color = imgui.get_color_u32_rgba(1, 1, 1, 0.1)
        draw_list.add_text(imgui.get_cursor_screen_pos()[0] - icon_width + 10,
                           imgui.get_cursor_screen_pos()[1], spinner_color, spinner_icon)
        imgui.dummy(15, 15)
        imgui.same_line()


    end_x = imgui.get_cursor_screen_pos()[0]

    # ── Profiler ───────────────────────────────────────────────
    is_profiling = Toggles.profile_mode == ProfileMode.ON
    if is_profiling:
        from src.lsd.gl_gui.view.core_views.new_core_view import render_profiler_time
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


def draw_footer(input_value=None, name="", key=None, melty=None, parent_show_add_delete=False, width=0, suffix="",
                collection=None, display_name=None, meta=None, unique=None, is_tree=True,
                show_name=True, name_func=None, show_type=False, show_unique=False,
                on_search=False, trigger_collapse=False, trigger_expand=False,
                draw_state=None, show_tint=False, opacity=1.0, show_add_delete=True,
                on_drag=False, on_action=None, style_manager=None,
                **kwargs):

    # for key, pending in draw_state._all_pending.items():
    #     if pending is not None:
    #         if pending.state == PendingState.ERROR:
    #             from src.lsd.gl_gui.view.core_views.new_core_view import draw_pending
    #             draw_pending(pending, name=f"{key}", tint=(1, 0, 0))

    imgui.dummy(1,1)



def draw_header_end(input_value=None, name="", key=None, melty=None, parent_show_add_delete=False,
                    collection=None, draw_state=None, closable=False, style_manager=None,
                    unique=None, **kwargs):


    if closable:
        close_icon = ""
        from src.lsd.gl_gui.view.core_views.new_core_view import button
        if button(f"{close_icon}##{unique}", show_bg=True, shadow=True, z_offset=20, tile_mode=TileMode.MAX, color=(9, 1, 1, 0))[0]:
            draw_state.closed = not draw_state.closed
            Melty.cache.invalidate_up_by_obj(Melty.registered_windows)

            # draw_state._parent.invalidate_up()
            if draw_state.parent_window is not None:
                draw_state.parent_window.invalidate_up()

            draw_state.dlt_count = 0
            # if draw_state.parent_window is not None:
            #     Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10)
    else:
        if parent_show_add_delete and collection is not None and key is not None:
            if RenderFuncs.button(f"\uf1f8##del{unique}", tint=(0.12,0.002037035,0.002037035,0.4), name=f"\uf1f8##del{unique}")[0]:
                Melty.to_delete(key, collection)
            same_line(spacing=0.0)
