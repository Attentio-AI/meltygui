"""Inline widgets: `@gui` views registered for a token kind or a value type.

    @inline('bool')
    def switch(input_value: str, width=0., height=0.):
        ...draw over the token...
        return changed, new_text

The editor declares the view on every token of that kind (`number`, `bool`,
`color3`, `colorhex`, `icon`, `def_name`, ...) with the token text as
`input_value`; a returned edit replaces the token. Where the view sits:

- by default it *is* the token: same cells, drawn over the text. It steps
  aside while the caret is inside the token, so the text edits like any other
  (`solid=True`: it stays, for a token with nothing to edit inside, an icon).
- `lead=N`: it gets N cells of its own left of the token; the token draws and
  edits as text and the rest of the line moves right.
- `trail=N`: the same, right after the token.
- `double=function`: a double click on the token replaces it with
  `function(text)` (a boolean flips), whether or not the caret is in it. The
  editor does that itself: the view only draws.

The extra cells go through the editor's column map (`Editor.x` and
`Editor.column`), so the caret, clicks and selections stay exact. A type key
(`@inline(float)`) registers the view for live values of that type instead: it
is declared after the line with the value as `input_value`, `cells` wide.
"""
from collections import namedtuple

from meltygui.core.rendering.gui_prototype import gui

# view: the @gui view. caret: a plain click also places the caret (a drag is
# the widget's). cells: width in characters for a live value view. lead and
# trail: cells the view adds before and after its token. solid: it stays
# while the caret is at its token. double: what a double click makes of the
# token's text (None: nothing, the word is selected).
Inline = namedtuple('Inline', 'view caret cells lead trail solid double', defaults=(False, None))
WIDGETS = {}
# Counts registrations: cached line layouts are dropped when it moves.
EPOCH = 0


def inline(key, *, caret=False, cells=12, lead=0, trail=0, solid=False, double=None):
    """Register the decorated function (made an `@gui` view if it is not one)."""
    def register(function):
        global EPOCH
        view = function if getattr(function, '__gui__', False) else gui(function)
        WIDGETS[key] = Inline(view, caret, cells, lead, trail, solid, double)
        EPOCH += 1
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
