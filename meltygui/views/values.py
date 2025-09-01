import imgui

from src.lsd.gl_gui.utils.custom_views import print_colored_traceback
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
def draw_window(input_value, name="", unique=0, *args, **kwargs):
    unique = unique
    tmp_undo_stack(unique)
    title = name or input_value.__class__.__name__
    opened, _ = imgui.begin(f"{title}##window_{str(unique)}", True)

    draw_object(input_value, *args, **kwargs)

    imgui.end()
    redo_stack(unique)


@render_func
def draw_object(input_value, meta=None, name="", depth=0, unique=0, suffix="", indent_size=10, *args, **kwargs):
    max_depth = 10
    is_collection = isinstance(input_value, (dict, list, tuple, set)) or (
            hasattr(input_value, "__dict__") and depth < max_depth)
    if is_collection:
        imgui.indent(indent_size)
        # Handle collections
        if isinstance(input_value, dict):
            changed = False
            for k, v in input_value.items():
                # Derive meta for dict items
                item_changed, new_value = meta.view_function(input_value=v, meta=meta, suffix=k, name=k)
                changed |= item_changed
        elif isinstance(input_value, (list, tuple, set)):
            changed = False
            for i, v in enumerate(input_value):
                item_changed, new_value = meta.view_function(input_value=v, meta=meta, suffix=i, name=str(i))
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

                    suffix += f"_{k}"

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

@render_func(is_default_for=(float, str))
def draw_float(input_value:float, min_value=-100.0, max_value=100.0, speed=0.01):
    imgui.text("render float")
    changed, value = imgui.drag_float("##float", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    if changed:
        return True, value

    return changed, value


@render_func
def draw_int(input_value: int, min_value=-100.0, max_value=100.0, speed=0.05):
    imgui.text("render int")
    changed, value = imgui.drag_int("##int", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    if changed:
        return True, value

    return changed, value
