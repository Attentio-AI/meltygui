"""App-owned tiled layouts; renderer references persist through DictConversion."""
from meltygui.core.conversion.dict_conversion import DictConversion


def multi_instance(renderer, enabled=True):
    """Offer an existing render function in every tile's editor picker, the
    same registration as ``@render_func(multi_instance=True)`` for a function
    defined elsewhere (a library's view):

        from meltygui import multi_instance
        from some_library import draw_library_view
        multi_instance(draw_library_view)

    The picker shows the function's own ``display_name`` / ``icon`` / ``tint``
    decoration. Returns ``renderer``; ``enabled=False`` withdraws it."""
    if not getattr(renderer, "__render_func__", False):
        raise TypeError(f"multi_instance needs a @render_func function, not {renderer!r}; a plain "
                        "callable is offered by passing it in the host's multi_instance_renderers")
    from meltygui.core.melty import Melty
    identifier = f"{renderer.__module__}.{renderer.__qualname__}"
    # Remembered by name so the registration survives a hotswap of the function.
    (Melty.multi_instance_registered.add if enabled else Melty.multi_instance_registered.discard)(identifier)
    renderer.multi_instance = bool(enabled)
    return renderer


class Tile(DictConversion):
    """An editor selection and its input; view-local state is injected separately."""

    def __init__(self, name="tile", tint=(0.3, 0.3, 0.3),
                 render_func=None, input_value=None):
        super().__init__()
        self.name = name
        self.tint = tint
        self.render_func = render_func
        self.input_value = input_value
        self.links = {}


class Split(DictConversion):
    """Same-axis cells and their shared interior divider edges, in window pixels."""

    def __init__(self, axis="x", children=None, edges=None):
        super().__init__()
        self.axis = axis
        self.children = list(children or [])
        self.edges = list(edges or [])
