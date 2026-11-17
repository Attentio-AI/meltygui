"""`meltygui.rust`: the public names of the `@gui` draw functions.

    from meltygui.rust import draw_any, is_default_for, Shaped

    changed, settings = draw_any(settings, name='settings', width=420, height=600)

Each name is loaded from the module that owns it when it is first asked for, so importing this
module loads nothing. To publish another view, add a row to `EXPORTS`.
"""
import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # what the IDE completes; keep one import per `EXPORTS` row (tests/test_rust_exports.py)
    from meltygui.core.rendering.gui_any import draw_any, is_default_for
    from meltygui.core.rendering.shaped import Shaped
    from meltygui.core.rendering.keyed import Keyed, keyed_as
    from meltygui.view.dict_view import draw_collection
    from meltygui.view.value_view import draw_bool, draw_int, draw_float, draw_str, draw_none, draw_value
    from meltygui.view.button_view import draw_button
    from meltygui.view.choice_view import draw_dropdown
    from meltygui.view.tiles_view import draw_tiles

# name -> the module that defines it
EXPORTS = {
    'draw_any': 'meltygui.core.rendering.gui_any',
    'is_default_for': 'meltygui.core.rendering.gui_any',
    'Shaped': 'meltygui.core.rendering.shaped',
    'Keyed': 'meltygui.core.rendering.keyed',
    'keyed_as': 'meltygui.core.rendering.keyed',
    'draw_collection': 'meltygui.view.dict_view',
    'draw_bool': 'meltygui.view.value_view',
    'draw_int': 'meltygui.view.value_view',
    'draw_float': 'meltygui.view.value_view',
    'draw_str': 'meltygui.view.value_view',
    'draw_none': 'meltygui.view.value_view',
    'draw_value': 'meltygui.view.value_view',
    'draw_button': 'meltygui.view.button_view',
    'draw_dropdown': 'meltygui.view.choice_view',
    'draw_tiles': 'meltygui.view.tiles_view',
}


def __getattr__(name):
    module = EXPORTS.get(name)
    if module is None:
        raise AttributeError(f'meltygui.rust has no {name!r}')
    value = globals()[name] = getattr(importlib.import_module(module), name)
    return value


def __dir__():
    return sorted(EXPORTS)
