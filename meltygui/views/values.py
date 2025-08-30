import imgui

from src.lsd.gl_gui.markers.core_markers import Meta, render_func


def renderer(view_function):
    return Meta(view_function=view_function)


@render_func
def draw_object(input_value, meta=None, draw_state=None):
    object_dict = input_value.__dict__
    object_type = type(input_value)
    attr_name = meta.name
    for key, value in object_dict.items():
        imgui.text(f"{key}: {value}")

    return False, None


@render_func
def draw_any(input_value, meta=None, draw_state=None):
    imgui.text(meta.name)
    imgui.same_line()

    render_func = meta.view_function
    if callable(render_func):
        changed, value = render_func(input_value)
        if changed:
            return True, value

    imgui.text(str(input_value))
    return False, None


@render_func
def draw_float(input_value, meta=None, draw_state=None):
    # settings = validate(input_value, meta)
    changed, value = False, 0.0

    # todo
    # changed, value = imgui.drag_float(
    #
    # )

    return changed, value


def draw_list(input_value, **meta):
    for i, item in enumerate(input_value[0] if input_value else []):
        imgui.text(f"{i}: {item}")
