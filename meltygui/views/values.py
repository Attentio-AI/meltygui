import imgui

from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, tree, Root
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

@render_func
def draw_header(input_value=None, name="", unique=None, is_tree=True,
                show_name=True, show_type=True, show_unique=True,
                draw_state=None, is_window=False):

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
                child_meta = Root.type_defaults.get(type(v), meta)
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
