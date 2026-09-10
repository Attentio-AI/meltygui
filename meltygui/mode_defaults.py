import sys


class ModeDefaults:

    default_mode_from_type = {}
    # (module name, class name) -> mode, for classes in heavy-weight modules (torch) that we
    # don't want to import at startup. Resolved to default_mode_from_type once the module loads.
    _pending = {}


def set_default_mode(for_type, mode):
    ModeDefaults.default_mode_from_type[for_type] = mode


def set_default_mode_lazy(module, class_name, mode):
    mod = sys.modules.get(module)
    if mod is not None:
        set_default_mode(getattr(mod, class_name), mode)
    else:
        ModeDefaults._pending[(module, class_name)] = mode


def _resolve_pending():
    for (module, class_name), mode in list(ModeDefaults._pending.items()):
        mod = sys.modules.get(module)
        if mod is not None:
            set_default_mode(getattr(mod, class_name), mode)
            del ModeDefaults._pending[(module, class_name)]


def default_mode_for(for_type, default=None):
    if ModeDefaults._pending:
        _resolve_pending()
    return ModeDefaults.default_mode_from_type.get(for_type, default)


def register_defaults():
    from src.lsd.gl_gui.modes import Modes

    set_default_mode_lazy('torch', 'Tensor', Modes.FILE_TREE)
