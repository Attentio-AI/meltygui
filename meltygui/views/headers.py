import os
from types import NoneType
from typing import MutableMapping

import imgui
from imgui.core import _DrawList

from server.server_gui import open_file
from src.lsd.gl_gui.melty import Melty, add_to_collection
from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.model.core_model.draw_state import TileMode
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.custom_views import push_style_var, push_style_color, pop_style_color, pop_style_var
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.path_finder import PendingState
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.folders_proxy import FolderProxy


def draw_header(input_value=None, name="", key=None, melty=None, parent_show_add_delete=False, width=0, suffix="",
                collection=None, display_name=None, meta=None, unique=None, is_tree=True,
                show_name=True, name_func=None, show_type=False, show_unique=False,
                on_search=False, trigger_collapse=False, trigger_expand=False,
                draw_state=None, show_tint=False, opacity=1.0, show_add_delete=True,
                on_drag=False, on_action=None, style_manager=None,
                global_style=None, global_toggles=None, **kwargs):

    # ── Constants ──────────────────────────────────────────────
    # Depth-driven name brightness
    depth_scale       = 0.347
    depth_offset      = -1.647
    name_value_factor = 0.769


    spinner_icon_0 = ""
    spinner_icon_1 = ""

    draw_list = imgui.get_window_draw_list()
    spinner_icon_idx = Melty.frame_count % 2
    spinner_icon = [spinner_icon_0, spinner_icon_1][spinner_icon_idx]
    icon_width = imgui.calc_text_size(spinner_icon)[0]/2



    # Name text (value is the base offset, updated to depth below)
    name_style = {
        'value': 0.086, 'saturation': 0.401,
        'alpha': 0.848, 'max_value': 3.205,
        'depth_factor': 0.342
    }
    name_rounding       = 2.117
    max_name_chars      = 40
    min_name_text_width = 62

    # Tree arrow
    arrow_style = {
        'value': 0.993, 'saturation': 1.639,
        'alpha': 0.122, 'max_value': 1.601,
        'depth_factor': 0.332
    }

    # Type / unique label colors
    type_label_color   = (2.865, 1.955, 2.861, 1.0)
    unique_label_color = (-1.535, 0.0, 0.9, 1.0)

    # ── Setup ──────────────────────────────────────────────────
    if display_name is not None:
        name = display_name

    imgui.dummy(0, 0)

    on_change = False
    return_val = on_action
    push_style_var(imgui.STYLE_ALPHA, opacity)

    # ── Depth-driven color computation ─────────────────────────
    depth = max(0.0, Melty.bg_depth)

    depth_intensity = float(depth + depth_offset) * depth_scale
    name_style['value'] = depth_intensity * name_style['depth_factor'] + name_style['value']
    arrow_style['value'] = depth_intensity * arrow_style['depth_factor'] + arrow_style['value']

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
    if hasattr(input_value, "tint") and input_value.tint is not None:
        draw_state._has_popup = True
        tint_changed, tint_value = draw_tuple(input_value.tint, show_name=False, show_header=False)
        if tint_changed:
            input_value.tint = tint_value
        same_line()
    elif show_tint:
        draw_state._has_popup = True
        tint_changed, tint_value = draw_tuple(draw_state.tint, show_name=False, show_header=False)
        if tint_changed:
            draw_state.tint = tint_value
        same_line()

    # ── Add button ─────────────────────────────────────────────
    if show_add_delete and isinstance(input_value, (list, dict)) or hasattr(input_value, "__dict__"):
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
            if imgui.button("##open_folder"):
                if hasattr(input_value, "file_path"):
                    open_file(input_value.file_path)
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

        same_line(spacing=3)

    if draw_state.closable:
        spinner_color = imgui.get_color_u32_rgba(1, 1, 1, 0.1)
        draw_list.add_text(imgui.get_cursor_screen_pos()[0] - icon_width + 10,
                           imgui.get_cursor_screen_pos()[1], spinner_color, spinner_icon)
        imgui.dummy(15, 15)
        imgui.same_line()

    # ── Profiler ───────────────────────────────────────────────
    is_profiling = Toggles.profile_mode == ProfileMode.ON
    if is_profiling:
        from src.lsd.gl_gui.view.core_views.new_core_view import render_profiler_time
        render_profiler_time(
            input_value=draw_state.render_time, brief=True,
            style_manager=style_manager, global_style=global_style,
        )
        same_line(spacing=3)

    pop_style_var(1)

    return on_change, return_val


def draw_footer(input_value=None, name="", key=None, melty=None, parent_show_add_delete=False, width=0, suffix="",
                collection=None, display_name=None, meta=None, unique=None, is_tree=True,
                show_name=True, name_func=None, show_type=False, show_unique=False,
                on_search=False, trigger_collapse=False, trigger_expand=False,
                draw_state=None, show_tint=False, opacity=1.0, show_add_delete=True,
                on_drag=False, on_action=None, style_manager=None,
                global_style=None, global_toggles=None, **kwargs):

    for key, pending in draw_state._all_pending.items():
        if pending is not None:
            if pending.state == PendingState.ERROR:
                from src.lsd.gl_gui.view.core_views.new_core_view import draw_pending
                draw_pending(pending, name=f"{key}", tint=(1, 0, 0))

    imgui.text(f"{name}")


def draw_header_end(input_value=None, name="", key=None, melty=None, parent_show_add_delete=False,
                    collection=None, draw_state=None, closable=False, style_manager=None,
                    global_style=None, global_toggles=None, **kwargs):
    bg_style = {
        "value": 0.01,
        "saturation": 1.0,
        "alpha": 1.0,
        'max_value': 1.0
    }
    bg_style = global_style.get_global_constant("bg_style", default=bg_style, folder="bg_styles")
    search_color = (style_manager.
                    make_color_style_value(input=bg_style, saturation=0.7,
                                           value=1.0))

    # push_style_var(imgui.STYLE_FRAME_PADDING, (4, 0))
    # push_style_var(imgui.STYLE_ITEM_SPACING, (4, 0))

    if parent_show_add_delete:
        bg_style = {
            "value": 0.01,
            "saturation": 1.0,
            "alpha": 1.0,
            'max_value': 1.0
        }
        bg_style = global_style.get_global_constant("bg_style", default=bg_style, folder="bg_styles")
        search_color = (style_manager.
                        make_color_style_value(input=bg_style, saturation=0.7,
                                               value=1.0))
        push_style_color(imgui.COLOR_TEXT, *search_color)
        push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
        if imgui.button(f"\uf1f8##del"):
            melty.to_delete(key, collection)
            print("No selected_views or remove_view method")
        same_line(spacing=0.0)
        pop_style_color(2)

    if closable and not input_value == Melty.registered_windows:
        close_icon = "\uf00d"
        from src.lsd.gl_gui.view.core_views.new_core_view import button
        if button(f"{close_icon}", show_bg=True, shadow=True, z_offset=10, tile_mode=TileMode.MAX, color=(9, 1, 1, 0))[0]:
            draw_state.closed = not draw_state.closed
            Melty.cache.invalidate_up_by_obj(Melty.registered_windows)


    # if show_search or draw_state.search_active:
    #     imgui.same_line()
    #     imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + 2)
    #
    #     icon = "\uf002"
    #     imgui.text_colored(icon, *search_color)
    #     imgui.same_line()
    #     search_width = 150.0
    #     imgui.set_next_item_width(search_width)
    #     search_changed, new_search = imgui.input_text(f"##search{unique}", draw_state.search_text)
    #
    #     if search_changed:
    #         draw_state.search_text = new_search
    #         imgui.set_keyboard_focus_here(-1)
    #         request_render()
    #
    #     if not draw_state.search_active:
    #         draw_state.search_text = ""
    #
    #     if on_search:
    #         draw_state.search_active = True
    #         imgui.set_keyboard_focus_here(-1)
    #         request_render()
    #
    #     draw_state.search_active = imgui.is_item_focused()
    # pop_style_var(2)

    # push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))

    # pop_style_var(2)
