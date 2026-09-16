"""Injected state for code views."""
from meltygui.state.dict_conversion import DictConversion


class SourcePreviewState(DictConversion):
    def __init__(self):
        super().__init__()
        self.path = None
        self.line = None
        self.token = None
