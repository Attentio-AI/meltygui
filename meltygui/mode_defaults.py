

class ModeDefaults:

    default_mode_from_type = {}


def set_default_mode(for_type, mode):
    ModeDefaults.default_mode_from_type[for_type] = mode


def register_defaults():
    from src.lsd.gl_gui.modes import Modes
    from src.lsd.gl_gui.model.dict_conversion import DictConversion
    from torch import Tensor

    set_default_mode(Tensor, Modes.FILE_TREE)
