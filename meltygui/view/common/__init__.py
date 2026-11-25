"""The `@gui` views: the defaults `draw_any` draws a value with (`@is_default_for` above `@gui`) and
the common widgets (button, dropdown, context menu, editor, tiles).

A new `@gui` view goes here, as `<thing>_view.py`; `meltygui/view/` beside it is the legacy
`@render_func` views. `meltygui.view.common.<module>` loads that module when it is first asked
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
