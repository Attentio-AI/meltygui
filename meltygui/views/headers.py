import os
import sys
from types import NoneType
from typing import MutableMapping

import glfw
import imgui
from imgui.core import _DrawList

from src.lsd.gl_gui.global_style import GlobalStyle
from src.lsd.gl_gui.melty import Melty, add_to_collection
from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.model.core_model.draw_state import TileMode
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.custom_views import push_style_var, push_style_color, pop_style_color, pop_style_var
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.view.core_conversion.path_finder import PendingState
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.folders_proxy import FolderProxy


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



def render_search(search_ds, draw_state, unique=None, ):
    """Render the find UI for the searchable view whose state lives on
    `search_ds`: the search input, match count, prev/next nav, and close.

    Shared by draw_header (inline, when the view has a header) and draw_search
    (a floating window, when it doesn't). All state — search_text, match count,
    current index — lives on `search_ds`, the owning view's draw_state, so both
    presentations drive the same search.
    """
    search_icon = ""
    from src.lsd.gl_gui.view.core_views.text_editor import draw_text
    # Grab focus on first open, and re-grab whenever nothing holds text focus.
    # Window focus management (move-to-front / window activation) clears
    # text_focused_ds when a window comes forward that doesn't contain the
    # text field. The floating search box lives in its own window, so
    # without re-grabbing it would drop focus after the first keypress. The
    # re-grab runs before draw_text's key handling, so no keystroke is lost,
    # and it won't steal focus from a user click into the editor (which
    # leaves text_focused_ds non-None).
    focus_search = (not search_ds._search_was_active) or Melty.text_focused_ds is None
    search_ds._search_was_active = True
    search_icon = ""
    imgui.align_text_to_frame_padding()
    imgui.text(search_icon)
    imgui.same_line()
    search_change, new_search = draw_text(search_ds.search_text, searchable=False,
                                          shadow=False,
                                          name=search_icon + str(unique), with_header=None,
                                          with_header_end=None, width=draw_state.content_width-60,
                                          with_footer=None, header_same_line=True, tint=search_ds.tint,
                                          show_name=False, show_header=False, single_line=True,
                                          request_focus=focus_search)
    if search_change:
        search_ds.search_text = new_search
        # Re-render the owner's whole subtree so every child view recomputes its
        # matches against the new term and the combined totals stays in sync.
        Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
        request_render()
    # initial use


    # Match count + prev/next navigation. The count and current index are
    # managed by the searchable view's body (e.g. the text editor); the
    # arrows step the active match and ask the body to scroll it into view.


    imgui.same_line()
    from src.lsd.gl_gui.view.core_views.new_core_view import button
    fa_x_icon = ""
    # imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0], imgui.get_cursor_screen_pos()[1] + 2))
    if button(fa_x_icon, name=f"{unique}##fa_x_icon", show_bg=False,
              use_cache=True, height=25, shadow=True, z_offset=3,
              tile_mode=TileMode.MAX, color=(9, 1, 1, 0))[0]:
        search_ds.search_active = False
        search_ds._search_was_active = False
        search_ds.search_text = ""
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
        # Enter = find next, Shift+Enter = find prev — but only while the search
        # box (not the underlying editor) holds text focus, so Enter still inserts
        # newlines when you click into the editor. Drained from the GLFW-callback
        # key queue (not imgui.is_key_pressed) so it isn't dropped on slow frames.
        if Melty.focused_ds is search_ds and Melty.text_focused_ds is not search_ds:
            _enter = [m for k, m in Melty.frame_key_events
                      if k in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)]
            if _enter:
                nav = -1 if (_enter[-1] & glfw.MOD_SHIFT) else 1

        if nav != 0:
            # total is the combined count across all views; stepping wraps over
            # the entire result set. Flag a scroll and invalidate the owner's
            # subtree so every child view recomputes and the one holding the new
            # global search match scrolls to it.
            search_ds.text_search_current = (search_ds.text_search_current + nav) % total
            search_ds._search_nav_pending = True
            Melty.cache.invalidate_up(search_ds._tile_id, force=True, max_depth=12)
            request_render()
    elif search_ds.search_text:
        imgui.align_text_to_frame_padding()
        imgui.text_colored("No results", 0.74, 0.5, 0.5, 1.0)



def draw_header(input_value=None, name="", key=None, melty=None, parent_show_add_delete=False, width=0, suffix="",
                collection=None, display_name=None, meta=None, unique=None, is_tree=True,
                show_name=True, name_func=None, show_type=False, show_unique=False,
                on_search=False, trigger_collapse=False, trigger_expand=False,
                draw_state=None, show_tint=False, opacity=1.23, show_add_delete=True,
                on_drag=False, on_action=None, style_manager=None,
                **kwargs):

    # ── Constants ──────────────────────────────────────────────
    # Depth-driven name brightness
    depth_scale       = 0.06
    depth_offset      = -30.0
    name_value_factor = 0.777
    # Depth drives text saturation falloff
    sat_depth_factor  = -0.004
    sat_depth_offset  = -1.773
    some_val = 6

    spinner_icon_0 = ""
    spinner_icon_1 = ""

    draw_list = imgui.get_window_draw_list()
    spinner_icon_idx = Melty.frame_count % 2
    spinner_icon = [spinner_icon_0, spinner_icon_1][spinner_icon_idx]
    icon_width = imgui.calc_text_size(spinner_icon)[0]/2


    # Name text (value is the base offset, updated to depth below)
    name_style = {
        'value': 1.034, 'saturation': 0.715,
        'alpha': 0.174, 'max_value': 3.921,
        'depth_factor': 0.741
    }
    name_rounding       = 2.696
    max_name_chars      = 40
    min_name_text_width = 62

    # Tree arrow
    arrow_style = {
        'value': 7.788, 'saturation': 1.559,
        'alpha': 0.071, 'max_value': 1.601,
        'depth_factor': 0.332
    }

    # Type / unique label colors
    type_label_color   = (3.672, 1.944, 2.861, 1.0)
    unique_label_color = (-1.535, 0.0, 0.9, 1.0)

    # ── Setup ──────────────────────────────────────────────────
    if display_name is not None:
        name = display_name

    imgui.dummy(5, 0)

    start_x = imgui.get_cursor_screen_pos()[0]

    on_change = False
    return_val = on_action
    push_style_var(imgui.STYLE_ALPHA, opacity)

    # ── Depth-driven color computation ─────────────────────────
    depth = max(0.0, Melty.bg_depth)

    depth_intensity = float(depth + depth_offset) * depth_scale
    name_style['value'] = depth_intensity * name_style['depth_factor'] + name_style['value']
    arrow_style['value'] = depth_intensity * arrow_style['depth_factor'] + arrow_style['value']

    sat_shift = float(depth + sat_depth_offset) * sat_depth_factor
    name_style['saturation'] = name_style['saturation'] + sat_shift
    arrow_style['saturation'] = arrow_style['saturation'] + sat_shift

    name_color = style_manager.make_color_style_value(input=name_style)
    arrow_color = style_manager.make_color_style_value(input=arrow_style)

    # ── Tree button ─────────────────────────────────────────────
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
            print(f"Expanded: {draw_state.expanded}")
            draw_state.content_height = 0
            draw_state.invalid_content_height = True
            request_render()
        imgui.pop_style_var(1)
        imgui.pop_style_color(2)
        pop_style_color(1)
        same_line()
    else:
        imgui.same_line()

    # ── Type / unique labels ───────────────────────────────────
    if show_type:
        imgui.text_colored(f"({input_value.__class__.__name__})", *type_label_color)
        same_line()
    if show_unique:
        imgui.text_colored(f"({str(Melty.get_tile_id())})", *unique_label_color)
        same_line()
    if show_name and name != "":
        same_line()
        imgui.set_item_allow_overlap()

    from src.lsd.gl_gui.view.core_views.new_core_view import draw_tuple

    # ── Tint widget ────────────────────────────────────────────
    # A dict can carry its tint in __overrides__ (parsed from a `# [tint=(...)]`
    # comment); edit that value directly so the change round-trips to source.
    # The override comment is itself the opt-in, so this isn't conditional on
    # show_tint (which is only set for top-level windows, not nested classes).
    _overrides = input_value.get("__overrides__") if isinstance(input_value, dict) else None
    if isinstance(_overrides, dict) and _overrides.get("tint") is not None:
        draw_state._has_popup = True
        tint_changed, tint_value = draw_tuple(_overrides["tint"], show_name=False, show_header=False)
        if tint_changed:
            _overrides["tint"] = tint_value
            on_change = True
            return_val = input_value
        same_line()
    elif hasattr(input_value, "tint") and input_value.tint is not None and show_tint:
        draw_state._has_popup = True
        tint_changed, tint_value = draw_tuple(input_value.tint, show_name=False, show_header=False)
        if tint_changed:
            input_value.tint = tint_value
            on_change = True
            return_val = input_value
        same_line()
    elif show_tint and draw_state.tint is not None:
        draw_state._has_popup = True
        tint_changed, tint_value = draw_tuple(draw_state.tint, show_name=False, show_header=False)
        if tint_changed:
            draw_state.tint = tint_value
            on_change = True
            return_val = input_value
        same_line()

    # ── Add button ─────────────────────────────────────────────
    if show_add_delete and (isinstance(input_value, (list, dict)) or hasattr(input_value, "__dict__")):
        if show_add_delete:
            if imgui.small_button(f"\uf067##add{unique}"):
                hinted_type = NoneType
                if meta.field_type is not None and hasattr(meta.field_type, "__args__"):
                    if len(meta.field_type.__args__) == 2:
                        hinted_type = meta.field_type.__args__[1]
                add_to_collection(input_value, hinted_type())
                on_change = True
                return_val = input_value
            same_line()

    # ── Name label / edit ──────────────────────────────────────
    has_visible_name = show_name and name not in ("", None, "None")

    if has_visible_name:
        # Folder open button for dictionaries
        if isinstance(input_value, (dict, MutableMapping)):
            push_style_color(imgui.COLOR_BUTTON, 0.0, 0.0, 0.0, 0.0)
            push_style_color(imgui.COLOR_TEXT, *name_color)
            # if imgui.button("##open_folder"):
            #     if hasattr(input_value, "file_path"):
            #         open_file(input_value.file_path)
            pop_style_color(2)
            same_line()

        # File open button for folder proxies
        elif isinstance(collection, FolderProxy):
            push_style_color(imgui.COLOR_BUTTON, 0.0, 0.0, 0.0, 0.0)
            push_style_color(imgui.COLOR_TEXT, name_color[0], name_color[1], name_color[2], 0.5)
            if imgui.button("##open_file"):
                if hasattr(collection, "file_path"):
                    file_path = os.path.join(collection.file_path, str(name))
                    open_file(file_path)
            pop_style_color(2)
            same_line()

        clipped_name = name.split("##")[0][:max_name_chars]
        text_width = imgui.calc_text_size(clipped_name)[0]


        push_style_var(imgui.STYLE_FRAME_ROUNDING, name_rounding)

        if not draw_state._name_edit:
            draw_list: _DrawList = imgui.get_window_draw_list()
            cursor_pos = imgui.get_cursor_screen_pos()
            # Search-match highlight behind the key name (drawn before the text
            # so glyphs are readable). Strong fill + outline for the active
            # (global-current) match; a faint fill for the rest. The flags are
            # set by draw_collection when this header's key matches the query.
            if kwargs.get("search_match", False):
                pad = 2.0
                hx0, hy0 = cursor_pos[0] - pad, cursor_pos[1] - 1
                hx1 = cursor_pos[0] + text_width + pad
                hy1 = cursor_pos[1] + imgui.get_text_line_height() + 1
                if kwargs.get("search_current", False):
                    draw_list.add_rect_filled(hx0, hy0, hx1, hy1, (150 << 24) | (60 << 16) | (170 << 8) | 240)
                    draw_list.add_rect(hx0, hy0, hx1, hy1, (255 << 24) | (90 << 16) | (200 << 8) | 255)
                else:
                    draw_list.add_rect_filled(hx0, hy0, hx1, hy1, (89 << 24) | (80 << 16) | (200 << 8) | 230)
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

    # Record the natural (pre-pad) header width so core_render can fold it into
    # the parent window's running max for the next frame.
    draw_state.header_natural_width = end_x - start_x
    if kwargs.get("align_header", True) and show_name:
        # Pad to the widest header in this window (cached per-window), with the
        # preferred width as a floor. Fall back to the floor when no window.
        parent_window = draw_state.parent_window
        pad_target = Toggles.prefered_header_width
        if parent_window is not None:
            pad_target = max(pad_target, parent_window.max_header_width)
        if end_x - start_x < pad_target:
            imgui.dummy(pad_target - (end_x - start_x), 1)
            imgui.same_line(0)

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
    #         if pending.state == PendingAction.ERROR:
    #             from src.lsd.gl_gui.view.core_views.new_core_view import draw_pending
    #             draw_pending(pending, name=f"{key}", tint=(1, 0, 0))

    imgui.dummy(1,1)



def draw_header_end(input_value=None, name="", key=None, melty=None, parent_show_add_delete=False,
                    collection=None, draw_state=None, closable=False, style_manager=None,
                    unique=None, **kwargs):

    if parent_show_add_delete:
        bg_style = {
            "value": 0.01,
            "saturation": 1.0,
            "alpha": 1.0,
            'max_value': 1.0
        }
        bg_style = GlobalStyle.get_global_constant("bg_style", default=bg_style, folder="bg_styles")
        search_color = (style_manager.
                        make_color_style_value(input=bg_style, saturation=0.7, value=1.0))
        push_style_color(imgui.COLOR_TEXT, *search_color)
        push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
        if imgui.button(f"\uf1f8##del"):
            melty.to_delete(key, collection)
            print("No selected_views or remove_view method")
        same_line(spacing=0.0)
        pop_style_color(2)

    if closable and not input_value == Melty.registered_windows:
        close_icon = ""
        from src.lsd.gl_gui.view.core_views.new_core_view import button
        if button(f"{close_icon}##{unique}", show_bg=True, shadow=True, z_offset=10, tile_mode=TileMode.MAX, color=(9, 1, 1, 0))[0]:
            draw_state.closed = not draw_state.closed
            Melty.cache.invalidate_up_by_obj(Melty.registered_windows)
            # if draw_state.parent_window is not None:
            #     Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10)

