"""Injected state for graph views."""
from meltygui.core.conversion.dict_conversion import DictConversion
from pathlib import Path


class GraphViewState(DictConversion):
    """Injected per-draw_state state (`graph_view_state: GraphViewState = None`):
    the camera and the selection persist between sessions; the graph and
    the in-flight build (underscored) do not."""
    _owner_ds = None

    def __init__(self):
        super().__init__()
        self.zoom = 1.0
        self.pan_x = 0.0        # px, screen coordinates
        self.pan_y = 0.0
        self.selected = None    # str path of the selected node
        self._graph = None
        self._build = None

    def select(self, path):
        self.selected = str(path) if path is not None else None

    @property
    def selected_path(self):
        return Path(self.selected) if self.selected else None
