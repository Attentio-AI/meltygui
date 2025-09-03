from functools import wraps

import imgui

from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, tree
from src.lsd.gl_gui.melty import Melty, ActionType
from src.lsd.gl_gui.view.core_views.core_render import render_func, tmp_undo_stack, redo_stack, push_id, pop_id, ui_id, \
    renderer_wrapper


def generate_class_diff(obj, updates):
    clsname = obj.__class__.__name__
    for field, new_val in updates.items():
        old_val = getattr(obj, field)
        print(f"# Diff: {clsname}.{field} changed to {new_val}")




# Main draw function, called by the GUI framework
def draw(vis):
    draw_window(vis.root.lora_collection)
    # draw_any(vis.root.synth_collection, is_window=False)
    # #
    # draw_any("hello there", is_window=True)


def core_draw_window(input_value, name, unique, window_func,
                     window_stack, style_manager, args, kwargs):
    tmp_undo_stack(unique)
    title = name or input_value.__class__.__name__

    previous_tint = style_manager.get_tint()
    if hasattr(input_value, 'tint'):
        style_manager.set_imgui_tint(*input_value.tint)

    window_title = f"{title}##window_{str(unique)}"
    opened, _ = imgui.begin(f"{title}##window_{str(unique)}", True)
    if window_stack is not None:
        window_stack.append(window_title)

    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)

    window_func(*args, **kwargs)
    if window_stack is not None:
        window_stack.pop()

    draw_list.channels_merge()
    imgui.end()

    if hasattr(input_value, 'tint'):
        style_manager.set_imgui_tint(*previous_tint)

    redo_stack(unique)

@render_func
def draw_window(input_value, window_stack=None, style_manager=None,
                show_header=False, is_window=True,
                is_tree=False, name="", unique=0, *args, **kwargs):
    kwargs['input_value'] = input_value
    kwargs['is_window'] = is_window
    kwargs['is_tree'] = is_tree
    kwargs['style_manager'] = style_manager
    kwargs['window_stack'] = window_stack
    kwargs['name'] = name

    core_draw_window(window_func=draw_object, input_value=input_value, window_stack=window_stack,
                     style_manager=style_manager, name=name, unique=unique, args=args, kwargs=kwargs)

@renderer_wrapper(wraps=render_func)
def render_with_foo(func, *args, **kwargs):

    def wrapper(window_stack=None, *args, **kwargs):
        imgui.text("Some wrapper")
        return func(skfs=False, *args, **kwargs)

    return wrapper


@render_func
def render_with_header(func=None, indent_size=10, depth=0,
                   window_stack=None, unique=0, name="", style_manager=None,
                   selected_views=None, clean_args=None, **kwargs):
    # draw_list.channels_set_current(1)

    draw_list = imgui.get_window_draw_list()
    inside_window = len(window_stack) > 0
    if inside_window and kwargs.get("show_bg", True):
        draw_list.channels_set_current(depth - 1)
    imgui.indent(indent_size)

    draw_state = kwargs.get("draw_state", None)
    is_header = func.__name__ == draw_header.__name__
    start_x_pos = imgui.get_cursor_screen_pos()[0]
    start_y_pos = imgui.get_cursor_screen_pos()[1]
    width = imgui.get_content_region_available()[0]
    cutoff = 100

    bg_tint = None
    bg_selected = False
    bg_hovered = False
    if not is_header and kwargs.get("show_header", True):
        changed, action = draw_header(show_bg=False, **kwargs)
        if action == "on_shift_click":
            selected_views[unique] = kwargs['input_value']
        elif action == "on_click":
            selected_views.clear()
            selected_views[unique] = kwargs['input_value']
        elif action == "on_hover":
            bg_hovered = True
        elif action == "on_drag":
            kwargs['show_header'] = False

            window_ags = clean_args.copy()
            core_draw_window(window_func=func, input_value=kwargs['input_value'],
                             window_stack=window_stack,
                             style_manager=style_manager, name=name,
                             unique=unique, args=(), kwargs=window_ags)

        if unique in selected_views:
            bg_selected = True
        rect_size = imgui.get_item_rect_size()
        header_width = rect_size[0]

        space_available = imgui.get_content_region_available()[0] - header_width
        if draw_state.expanded_height is None or draw_state.expanded_height < 70:
            if space_available > cutoff:
                imgui.same_line()

    return_value = None
    if not kwargs.get("is_tree", True) or draw_state.expanded or kwargs.get("is_window", False) or is_header:
        return_value = func(**clean_args)

    end_y_pos = imgui.get_cursor_screen_pos()[1]
    background_height = end_y_pos - start_y_pos - 2

    padding = imgui.get_style().frame_padding.y
    background_height = max(imgui.get_text_line_height() + padding, background_height)

    if inside_window:
        if kwargs.get("show_bg", True):
            draw_list.channels_set_current(max(0, min(Melty.max_depth - 2, depth - 2)))
            background_width = width
            draw_bg(left=start_x_pos, top=start_y_pos,
                    width=background_width, height=background_height,
                    tint=bg_tint, hovered=bg_hovered, selected=bg_selected)
            draw_state.height = background_height
            draw_state.width = background_width
            draw_state.top = start_y_pos
            draw_state.left = start_x_pos
            if draw_state.expanded:
                draw_state.expanded_height = background_height

    imgui.unindent(indent_size)

    if inside_window:
        draw_list.channels_set_current(Melty.max_depth - 1)

    return return_value


@render_func
def draw_bg(left=0, top=0, width=20, height=20, depth=0, show_header=False, show_bg=False,
            global_style=None, global_toggles=None,
            style_manager=None, unique=0, selected_views=None, tint=None, selected=False,
            hovered=False):
    # Render background
    def current_indent_px():
        sx = imgui.get_cursor_start_pos()
        cx = imgui.get_cursor_pos()
        start_x = sx[0]
        cur_x = cx[0]
        return cur_x - start_x - 1

    # float_style = global_styles.get_global_constant(constant_name="bg_style", default_type=Style, folder="bg_styles")
    # float_style.apply(global_styles=global_styles, style_manager=style_manager, depth=depth)
    #
    rounding = global_style.get_global_constant("rounding", default=0.0, folder="bg_styles")

    # Draw rect
    if left == 0:
        left = imgui.get_cursor_screen_pos()[0]

    if top == 0:
        top = imgui.get_cursor_screen_pos()[1]
    right =  left + width + rounding
    bottom =  top + height + rounding

    rect = (left, top, right, bottom)
    rect_outline = (left - 1, top - 1, right + 1, bottom + 1)
    # rounding
    rounding = min(current_indent_px(), rounding)

    depth_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles") * 0.7
    depth_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles")
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))
    bg_style = {
        "value": 0.01,
        "saturation": 1.0,
        "alpha": 1.0,
        'max_value': 1.0
    }
    hovered_offset = 0.0
    if selected:
        hovered_offset = 0.1
    elif hovered:
        hovered_offset = 0.02


    bg_style = global_style.get_global_constant("bg_style", default=bg_style, folder="bg_styles")
    outline_saturation = global_style.get_global_constant("outline_saturation", default=0.5, folder="bg_styles")

    outline_offset = global_style.get_global_constant("outline_offset", default=0.0, folder="bg_styles") + 0.1
    outline_factor = global_style.get_global_constant("outline_factor", default=1.0, folder="bg_styles")

    outline_color = (style_manager.
                     make_color_style_value_imgui(input=bg_style, saturation=outline_saturation,
                                                  value=max(0, dynamic_value * outline_factor + outline_offset)))
    # if tint is not None:
    #     outline_color = imgui.get_color_u32_rgba(*tint)

    imgui.get_window_draw_list().add_rect(*rect_outline, col=outline_color, rounding=rounding, thickness=2.0)
    bg_color = (style_manager.
                make_color_style_value(input=bg_style, value=max(0, dynamic_value) + hovered_offset))
    imgui_bg_color = imgui.get_color_u32_rgba(bg_color[0], bg_color[1], bg_color[2], 1.0)

    if tint is not None:
        imgui_bg_color = imgui.get_color_u32_rgba(*tint)

    imgui.get_window_draw_list().add_rect_filled(*rect, col=imgui_bg_color, rounding=rounding)


@render_func
def draw_header(input_value=None, name="", unique=None, is_tree=True,
                show_name=True, show_type=False, show_unique=False,
                draw_state=None, is_window=False, on_click=False,
                on_hover=False,
                on_right_click=False, show_bg=False, selected_views=None,
                on_drag=False, on_drag_released=False, on_action=None, style_manager=None,
                global_style=None, depth=0, shift_click=False):

    value_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles")
    value_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles")

    depth_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles")
    depth_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles") + 0.2
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))
    bg_style = global_style.get_global_constant("bg_style", default=None, folder="bg_styles")
    saturation = -0.5
    action = ActionType.NONE
    # Make header slightly brighter

    saturation = bg_style['saturation'] + saturation
    name_color = (style_manager.
                  make_color_style_value(input=bg_style, saturation=saturation,
                                         value=max(0, dynamic_value * value_factor + value_offset)))

    region_available = imgui.get_content_region_available()
    if is_tree:
        draw_state.expanded = tree("##tree", draw_state.expanded, width=30)
        imgui.same_line()
    cursor_start = imgui.get_cursor_pos()

    if show_name and name != "":
        imgui.align_text_to_frame_padding()
        imgui.text_colored(f"{name}", *name_color)
        imgui.same_line()

    name_end = imgui.get_cursor_pos()

    if show_type:
        imgui.text_colored(f"({type(input_value).__name__})", *(0.8, 0.0, 0.5, 1.0))
        imgui.same_line()

    if show_unique:
        imgui.text_colored(f"({str(unique)})", *(0.8, 0.0, 0.5, 1.0))
        imgui.same_line()

    if show_name and name != "":
        imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
        imgui.push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
        imgui.same_line()
        imgui.set_item_allow_overlap()
        imgui.set_cursor_pos_x(cursor_start[0])
        imgui.set_cursor_pos_y(cursor_start[1] + imgui.get_style().frame_padding.y)
        button_width = max(5, name_end[0] - cursor_start[0])
        button_height = imgui.get_text_line_height() + imgui.get_style().frame_padding.y
        imgui.set_item_allow_overlap()
        if imgui.invisible_button(f"##block_tree", width=button_width,
                                  height=button_height):
            pass
        imgui.same_line()
        imgui.set_item_allow_overlap()
        imgui.pop_style_var(2)

    return False, on_action

@render_func
def draw_object(input_value=None, draw_state=None, meta=None, name="", style_manager=None,
                depth=0, unique=0, suffix="", is_tree=True, indent_size=10, *args, **kwargs):
    # if is_tree and not draw_state.expanded:
    #     return False, None


    is_collection = isinstance(input_value, (dict, list, tuple, set)) or (
            hasattr(input_value, "__dict__") and depth < Melty.max_depth)
    if is_collection:
        # Handle collections
        if isinstance(input_value, dict):
            changed = False
            for k, v in input_value.items():
                previous_tint = style_manager.get_tint()
                if hasattr(v, 'tint'):
                    style_manager.set_imgui_tint(*v.tint)
                # Derive meta for dict items
                suffix = f"{suffix}_{str(k)}"
                obj_unique, _, _ = ui_id(meta, suffix=suffix)
                item_changed, new_value = meta.view_function(input_value=v, meta=meta,
                                                             suffix=obj_unique, name=k)
                changed |= item_changed
                if hasattr(v, 'tint'):
                    style_manager.set_imgui_tint(*previous_tint)
        elif isinstance(input_value, (list, tuple, set)):
            changed = False
            for i, v in enumerate(input_value):
                suffix = f"{suffix}_{str(i)}"
                obj_unique, _, _ = ui_id(meta, suffix=suffix)
                child_meta = Melty.type_defaults.get(type(v), meta)
                item_changed, new_value = child_meta.view_function(input_value=v, meta=child_meta,
                                                             suffix=obj_unique, name=str(i))
                changed |= item_changed
        elif hasattr(input_value, "__dict__") and depth < Melty.max_depth:  # class or module instance
            for k, v in vars(input_value).items():
                # skip private attrs, methods, etc.
                if (k.startswith("__") and k.endswith("__")) or k.startswith("_"):
                    continue
                try:
                    parent_type = type(input_value)
                    child_meta = parent_type.get_child_meta(field_name=k, value=v) if (
                        hasattr(parent_type, "get_child_meta")) else meta

                    if child_meta is not None:
                        kwargs['meta'] = child_meta

                    suffix = f"{suffix}_{str(k)}"
                    obj_unique, _, _ = ui_id(child_meta, suffix=suffix)
                    item_changed, new_value = child_meta.view_function(input_value=v, meta=child_meta,
                                                                       suffix=obj_unique, name=k)
                    if item_changed:
                        setattr(input_value, k, new_value)
                except Exception as e:
                    print_colored_traceback()
                    pass
    else:
        return_value = None
        push_id(unique)
        try:
            imgui.text("Render object")
        except Exception as e:
            print_colored_traceback()
        finally:
            pop_id()
            if return_value is None:
                changed, new_value = False, None
            elif isinstance(return_value, tuple) and len(return_value) == 2:
                changed, new_value = return_value
            else:
                imgui.text("Unsupported return from render_func")
                changed, new_value = False, None
    return False, None


@render_func
def draw_any(input_value, *args, meta=None, **kwargs):
    return meta.view_function(input_value, *args, **kwargs)


@render_with_foo(is_default_for=(str))
def draw_str(input_value: str):
    changed, value = imgui.input_text("##str", input_value)
    if changed:
        return True, value

    return changed, value

@render_func(is_default_for=(tuple))
def draw_tuple(input_value: tuple, is_tree=False):
    if len(input_value) == 4:
        color_list = list(input_value)
        color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL | imgui.COLOR_EDIT_FLOAT |
                       imgui.COLOR_EDIT_NO_TOOLTIP)
        changed, color = imgui.color_edit4(
            f"##_color",
            color_list[0], color_list[1], color_list[2], color_list[3],
            flags=color_flags)
        if changed:
            input_value = (color[0], color[1], color[2], color[3])
    else:
        color_list = list(input_value)
        color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL |
                       imgui.COLOR_EDIT_NO_ALPHA | imgui.COLOR_EDIT_FLOAT |
                       imgui.COLOR_EDIT_NO_TOOLTIP)
        changed, color = imgui.color_edit3(
            f"##_color",
            color_list[0], color_list[1], color_list[2],
            flags=color_flags)
        if changed:
            input_value = (color[0], color[1], color[2])
    return changed, input_value

@render_func(is_default_for=(float))
def draw_float(input_value:float, min_value=-100.0, max_value=100.0, speed=0.01):
    changed, value = imgui.drag_float("##float", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    if changed:
        return True, value

    return changed, value


@render_func(is_default_for=(int))
def draw_int(input_value: int, min_value=-100.0, max_value=100.0, speed=0.05):
    changed, value = imgui.drag_int("##int", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    if changed:
        return True, value

    return changed, value
