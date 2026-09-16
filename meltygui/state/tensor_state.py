"""View-local state shared by tensor and graph presentation."""
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.conversion.dict_conversion import DictConversion


@no_save("last_error")
class TensorErrorState(DictConversion):
    def __init__(self):
        super().__init__()
        self.last_error = None
