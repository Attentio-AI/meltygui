from src.lsd.gl_gui.model.core_markers import Meta


def window(cls):
    """Mark a class as a top-level ImGui window type."""
    cls.__is_window__ = True
    return cls

def no_render(value, *args, **kwargs) -> Meta:
    new_meta = Meta.get_new_defaults(value=value, *args, **kwargs)
    new_meta.visible_in_ui = False
    return new_meta


def render_with(value, view_function, *args, **kwargs) -> Meta:
    new_meta = Meta.get_new_defaults(value=value, *args, **kwargs)
    new_meta.view_function = view_function
    return new_meta