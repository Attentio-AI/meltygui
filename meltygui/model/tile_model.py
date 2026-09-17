"""App-owned tiled layouts; renderer references persist through DictConversion."""
from meltygui.core.conversion.dict_conversion import DictConversion


class Tile(DictConversion):
    """An editor selection and its input; view-local state is injected separately."""

    def __init__(self, name="tile", tint=(0.3, 0.3, 0.3),
                 render_func=None, input_value=None):
        super().__init__()
        self.name = name
        self.tint = tint
        self.render_func = render_func
        self.input_value = input_value


class Split(DictConversion):
    """Same-axis cells and their shared interior divider edges, in window pixels."""

    def __init__(self, axis="x", children=None, edges=None):
        super().__init__()
        self.axis = axis
        self.children = list(children or [])
        self.edges = list(edges or [])
