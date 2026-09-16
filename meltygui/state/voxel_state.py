"""Transient diagnostics for one voxel view, independent of other volumes."""
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save


@no_save("cuda_error", "label_warned")
class VoxelState(DictConversion):
    def __init__(self):
        super().__init__()
        self.cuda_error = None
        self.label_warned = False
        self.params_panel = None
