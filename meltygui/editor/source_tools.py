"""State owned by optional source-view integrations."""
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save


@no_save('tools')
class SourceToolsState(DictConversion):
    def __init__(self):
        super().__init__()
        self.tools = None
