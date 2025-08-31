import imgui

from src.lsd.gl_gui.model.core_markers import render_func

# Main draw function, called by the GUI framework
def draw(vis):
    draw_any(vis.root.lora_collection)
    draw_any(vis.root.synth_colors)


@render_func
def draw_object(input_value, meta=None, draw_state=None):
    imgui.same_line()
    imgui.text("Render object")
    return False, None


def generate_class_diff(obj, updates):
    clsname = obj.__class__.__name__
    for field, new_val in updates.items():
        old_val = getattr(obj, field)
        print(f"# Diff: {clsname}.{field} changed to {new_val}")


@render_func
def draw_any(input_value, meta=None, draw_state=None):
    imgui.text("render any")
    return False, None


@render_func
def draw_float(input_value, meta=None, draw_state=None):
    changed, value = False, 0.0
    imgui.text("render float")
    return changed, value
