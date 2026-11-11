"""Inline widgets: `@gui` views registered for a token kind or a value type.

    @inline('bool')
    def switch(input_value: str, width=0., height=0.):
        ...draw over the token...
        return changed, new_text

The editor declares the view over every token of that kind (`number`, `bool`,
`color3`, `colorhex`, `icon`, `def_name`, ...) with the token text as
`input_value`, sized to the token; a returned edit replaces the token. A type
key (`@inline(float)`) registers the view for live values of that type
instead: it is declared after the line with the value as `input_value`.
"""
from collections import namedtuple

from meltygui.core.rendering.gui_prototype import gui

# view: the @gui view. caret: a plain click also places the caret (a drag is
# the widget's). cells: width in characters for a live value view.
Inline = namedtuple('Inline', 'view caret cells')
WIDGETS = {}


def inline(key, *, caret=False, cells=12):
    """Register the decorated function (made an `@gui` view if it is not one)."""
    def register(function):
        view = function if getattr(function, '__gui__', False) else gui(function)
        WIDGETS[key] = Inline(view, caret, cells)
        return view
    return register


def for_token(kind):
    return WIDGETS.get(kind)


def for_value(value):
    """The widget of the value's type, nearest base class first."""
    for base in type(value).__mro__:
        found = WIDGETS.get(base)
        if found is not None:
            return found
    return None
