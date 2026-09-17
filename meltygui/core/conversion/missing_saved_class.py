"""Keep saved objects readable and re-saveable after their class is deleted."""
import importlib
import logging
import types
from functools import lru_cache

from meltygui.core.conversion.dict_conversion import DictConversion


@lru_cache(maxsize=None)
def missing_saved_class(class_path):
    logging.getLogger("load_save_v2").warning(
        "Saved class %s is unavailable; preserving its state in a placeholder", class_path)
    return type(class_path.rsplit(".", 1)[-1], (DictConversion,), {
        "__missing_saved_path__": class_path,
        "default_instance": None,
    })


def restore_saved_class(class_path):
    # The unpickler's find_class also resolves function globals. A missing
    # renderer can therefore have been preserved as this placeholder too.
    # Recover its original function when available, rather than keeping a
    # class that crashes when the renderer is called with view arguments.
    from meltygui.core.module_names import canonical_name
    class_path = canonical_name(class_path)
    parts = class_path.split(".")
    for boundary in range(len(parts) - 1, 0, -1):
        try:
            value = importlib.import_module(".".join(parts[:boundary]))
            for part in parts[boundary:]:
                value = getattr(value, part)
        except Exception:
            # A syntax error or import-time exception in today's code says
            # nothing about the validity of the saved object's type.
            continue
        if isinstance(value, (type, types.FunctionType, types.BuiltinFunctionType)):
            return value
    return missing_saved_class(class_path)
