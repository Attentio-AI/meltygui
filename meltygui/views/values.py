import imgui

from src.lsd.gl_gui.view.core_views.core_render import render_func


def generate_class_diff(obj, updates):
    clsname = obj.__class__.__name__
    for field, new_val in updates.items():
        old_val = getattr(obj, field)
        print(f"# Diff: {clsname}.{field} changed to {new_val}")


# Main draw function, called by the GUI framework
def draw(vis):
    draw_any(vis.root.lora_collection)
    draw_any(vis.root.synth_colors, is_window=False)
    #
    draw_any("hello there", is_window=True)


@render_func
def draw_object(input_value, meta=None, draw_state=None):
    imgui.same_line()
    imgui.text("Render object")
    return False, None


@render_func
def draw_any(input_value, meta=None, draw_state=None):
    imgui.text("render any")
    return False, None


@render_func
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
