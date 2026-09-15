"""Keep saved objects readable and re-saveable after their class is deleted."""
import importlib
import logging
from functools import lru_cache

from meltygui.state.object import DictConversion


@lru_cache(maxsize=None)
def missing_saved_class(class_path):
    logging.getLogger("load_save_v2").warning(
        "Saved class %s is unavailable; preserving its state in a placeholder", class_path)
    return type(class_path.rsplit(".", 1)[-1], (DictConversion,), {
        "__missing_saved_path__": class_path,
        "default_instance": None,
    })


def restore_saved_class(class_path):
    # Try the original path again on every load, so restoring the source also
    # restores real instances from a session saved with placeholders.
    from meltygui.state.module_names import canonical_name
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
        if isinstance(value, type):
            return value
    return missing_saved_class(class_path)
