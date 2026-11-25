"""The default `@gui` views: what `draw_any` draws a value with (`@is_default_for` above `@gui`).

A new default view goes here, one file per kind of value. A view that is the default for nothing
stays in `meltygui/view/`. `meltygui.view.common.<module>` loads that module when it is first asked
for; `stubs.py` (repository root) writes __init__.pyi.
"""
import importlib


def __getattr__(name):
    path = f'{__name__}.{name}'
    try:
        return importlib.import_module(path)
    except ModuleNotFoundError as error:
        if error.name != path:
            raise
        raise AttributeError(f'{__name__} has no {name!r}') from None
