import imgui

from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, tree
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_views.core_render import render_func, tmp_undo_stack, redo_stack, push_id, pop_id, ui_id


def generate_class_diff(obj, updates):
    clsname = obj.__class__.__name__
    for field, new_val in updates.items():
        old_val = getattr(obj, field)
        print(f"# Diff: {clsname}.{field} changed to {new_val}")


# Main draw function, called by the GUI framework
def draw(vis):
    draw_window(vis.root.lora_collection)
    draw_any(vis.root.synth_colors, is_window=False)
    #
    draw_any("hello there", is_window=True)


# Render background
# if config is None:
#     root_window = get_root_window()
#     parent = get_parent()
#     depth = get_depth()
# else:
#     root_window = config.root_window
#     parent = config.parent
#     depth = config.depth
#
#     if not config.is_visible():
#         return
#
# if config is None:
#     an_object_unique = unique
# else:
#     an_object_unique = config.unique
#
# size_is_set = parent is not None and root_window is not None and an_object_unique in root_window._attr_size
#
# if config is not None:
#     rounding = config.get_global_constant("rounding", default=0.0, folder="bg_styles")
# else:
#     rounding = get_global_constant("rounding", default=0.0, folder="bg_styles")
#
# # Draw rect
# window_width = imgui.get_content_region_available().x
# padding = imgui.get_style().window_padding[0]
# left = imgui.get_cursor_screen_pos()[0] - padding
# top = imgui.get_cursor_screen_pos()[1] - rounding - 2
# right = window_width + padding + padding + 2 + rounding
#
# # an_object_unique = f"{unique}"
# attr_size = (0, 0)
# if size_is_set:
#     attr_size = root_window._attr_size[an_object_unique]
# line_height = imgui.get_text_line_height() + padding
# bottom = attr_size[1] + header_height
#
# rect = (left, top, right, bottom)
# # Outline
# rounding = min(current_indent_px(), rounding)
#
# depth_factor = get_global_constant("depth_factor", default=1.0, folder="bg_styles")
# depth_offset = get_global_constant("depth_offset", default=0.0, folder="bg_styles")
# dynamic_value = max(0, (float(depth - depth_offset) * depth_factor))
# bg_style = {
#     "value": 0.01,
#     "saturation": 1.0,
#     "alpha": 1.0,
#     'max_value': 1.0
# }
# bg_style = get_global_constant("bg_style", default=bg_style, folder="bg_styles")
# outline_saturation = get_global_constant("outline_saturation", default=0.5, folder="bg_styles")
#
# outline_offset = get_global_constant("outline_offset", default=0.0, folder="bg_styles")
# outline_factor = get_global_constant("outline_factor", default=1.0, folder="bg_styles")
#
# outline_color = (LSDView().style_manager.
#                  make_color_style_value_imgui(input=bg_style, saturation=outline_saturation,
#                                                value=max(0, dynamic_value * outline_factor + outline_offset)))
#
# imgui.get_window_draw_list().add_rect(rect[0] - 2,
#                                       rect[1] - 2,
#                                       rect[0] + rect[2] + 5,
#                                       rect[1] + rect[3] + 1,
#                                       col=outline_color, rounding=rounding, thickness=2.0)
# bg_color = (LSDView().style_manager.
#             make_color_style_value(input=bg_style, value=max(0, dynamic_value)))
# imgui_bg_color = imgui.get_color_u32_rgba(bg_color[0], bg_color[1], bg_color[2], 1.0)
# if config is not None:
#     config.bg_color = bg_color
#     config.parent_bg_color = bg_color
# imgui.get_window_draw_list().add_rect_filled(rect[0] - 1,
#                                              rect[1] - 1,
#                                              rect[0] + rect[2] + 1,
#                                              rect[1] + rect[3] - 1,
#                                              col=imgui_bg_color, rounding=rounding)

def draw_with_func(func=None, clean_args=None, **kwargs):
    draw_state = kwargs.get("draw_state", None)
    is_header = func.__name__ == draw_header.__name__
    if not is_header and kwargs.get("show_header", True):
        draw_header(**kwargs)

    return_value = None
    if not kwargs.get("is_tree", True) or draw_state.expanded or kwargs.get("is_window", False) or is_header:
        return_value = func(**clean_args)

    return return_value


@render_func
def draw_header(input_value=None, name="", unique=None, is_tree=True,
                show_name=True, show_type=True, show_unique=True,
                draw_state=None, is_window=False, on_click=False):

    if on_click:
        print(name)
    draw_bg(width=0, height=20)
    if is_tree:
        draw_state.expanded = tree("##tree", draw_state.expanded)
        imgui.same_line()

    if show_name:
        imgui.text_colored(f"{name}", *(0.8, 0.3, 0.5, 1.0))
        imgui.same_line()

    if show_type:
        imgui.text_colored(f"({type(input_value).__name__})", *(0.8, 0.0, 0.5, 1.0))
        imgui.same_line()

    if show_unique:
        imgui.text_colored(f"({str(unique)})", *(0.8, 0.0, 0.5, 1.0))

    return False, None


@render_func
def draw_bg(width=0, height=20, show_header=False,
            global_styles=None, global_toggles=None):
    pass



@render_func
def draw_window(input_value, is_window=True, is_tree=False, name="", unique=0, *args, **kwargs):
    unique = unique
    tmp_undo_stack(unique)
    title = name or input_value.__class__.__name__
    opened, _ = imgui.begin(f"{title}##window_{str(unique)}", True)

    draw_object(input_value,*args, **kwargs)

    imgui.end()
    redo_stack(unique)


@render_func
def draw_object(input_value, draw_state=None, meta=None, name="",
                depth=0, unique=0, suffix="", is_tree=True, indent_size=10, *args, **kwargs):
    max_depth = 10
    # if is_tree and not draw_state.expanded:
    #     return False, None

    is_collection = isinstance(input_value, (dict, list, tuple, set)) or (
            hasattr(input_value, "__dict__") and depth < max_depth)
    if is_collection:
        imgui.indent(indent_size)
        # Handle collections
        if isinstance(input_value, dict):
            changed = False
            for k, v in input_value.items():
                # Derive meta for dict items
                suffix = f"{suffix}_{str(k)}"
                obj_unique, _, _ = ui_id(meta, suffix=suffix)
                item_changed, new_value = meta.view_function(input_value=v, meta=meta,
                                                             suffix=obj_unique, name=k)
                changed |= item_changed
        elif isinstance(input_value, (list, tuple, set)):
            changed = False
            for i, v in enumerate(input_value):
                suffix = f"{suffix}_{str(i)}"
                obj_unique, _, _ = ui_id(meta, suffix=suffix)
                child_meta = Melty.type_defaults.get(type(v), meta)
                item_changed, new_value = child_meta.view_function(input_value=v, meta=child_meta,
                                                             suffix=obj_unique, name=str(i))
                changed |= item_changed
        elif hasattr(input_value, "__dict__") and depth < max_depth:  # class or module instance
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
        imgui.unindent(indent_size)
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


@render_func(is_default_for=(str))
def draw_str(input_value: str):
    imgui.text("render str")
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
    imgui.text("render float")
    changed, value = imgui.drag_float("##float", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    if changed:
        return True, value

    return changed, value


@render_func(is_default_for=(int))
def draw_int(input_value: int, min_value=-100.0, max_value=100.0, speed=0.05):
    imgui.text("render int")
    changed, value = imgui.drag_int("##int", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    if changed:
        return True, value

    return changed, value
