"""`draw_function`: the parameters of a function as values to edit, and a button that calls it.

    ran, _ = draw_function(fit_line, name='fit', left=8.0, top=8.0, width=260.0)

The parameters are a dict the view keeps as its state (`arguments`), drawn by `draw_any`: each
starts at its default, else at what its annotation builds (`int` gives 0), else at None. The
button calls the function with them, in this view's run: it is for a call that returns at once,
slow work belongs on a thread (docs/GUI_VIEW_GUIDE.md, section 11). The view returns
`(True, function)` in the run of a call that succeeded and keeps what it returned as `result`;
a call that raised shows its error under the button and prints the traceback.

A class is a function that returns an instance: `draw_function(Clock)` builds a `Clock`. That
is what a view shows in its own place for a parameter nobody passed whose type needs arguments
(`asking` in core/rendering/gui_prototype.py).

Levers: the sizes and the colour below; `label=` for another text on the button; pass
`arguments=` to keep the dict yourself.
"""
import inspect
import traceback
from collections.abc import Callable

import meltygui_imgui as imgui

from meltygui.core.rendering.gui_any import draw_any
from meltygui.core.rendering.gui_prototype import constructor, gui, needs_arguments
from meltygui.hdr_color import pack_color
from meltygui.view.button_view import draw_button
from meltygui.view.dict_view import ROW as LINE
from meltygui.view.value_view import PAD, ROW

GAP = 6.0                               # space between the parameters, the button and the error
BUTTON = 160.0                          # the button's width, at most
SHOWN = 8                               # parameters in view before the list scrolls
ERROR = pack_color(1.0, 0.45, 0.40)     # the text of a call that raised


def start(parameter):
    """The value a parameter is first shown with: its default, else what its type builds, else None."""
    if parameter.default is not parameter.empty:
        return parameter.default
    kind = constructor(parameter.annotation)
    if kind is None or needs_arguments(kind):
        return None
    try:
        return kind()
    except Exception:
        return None


def passed(function):
    """The parameters of `function` a call passes by name or position: [(name, parameter)]."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return []
    return [(name, parameter) for name, parameter in parameters.items()
            if parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)]


def follow(arguments, function):
    """Make `arguments` hold the parameters of `function`, in their order. A value that is there
    under a parameter's name is kept; the dict keeps its identity."""
    held = dict(arguments)
    arguments.clear()
    for name, parameter in passed(function):
        arguments[name] = held[name] if name in held else start(parameter)


def call(function, arguments):
    """`function` called with `arguments`, positional-only parameters by position."""
    ahead = []
    named = {}
    for name, parameter in passed(function):
        if name not in arguments:
            continue
        if parameter.kind is parameter.POSITIONAL_ONLY:
            ahead.append(arguments[name])
        else:
            named[name] = arguments[name]
    return function(*ahead, **named)


@gui(listen=(), height=None, no_save=('seen', 'result', 'error'))
def draw_function(input_value: Callable, arguments: dict, label='', seen=None, result=None, error='', width=0.0,
                  draw_state=None):
    """Returns (True, function) in the run its button called the function and it returned."""
    if not callable(input_value):
        return False, input_value
    # The code a function runs is what its parameters were read from: a source edit reads them again.
    version = getattr(input_value, '__code__', input_value)
    if seen is not version:
        follow(arguments, input_value)
        draw_state.seen = version
    top = 0.0
    if arguments:
        high = min(len(arguments), SHOWN) * LINE
        draw_any(arguments, name='arguments', left=0.0, top=top, width=width, height=high)
        top += high + GAP
    name = getattr(input_value, '__name__', type(input_value).__name__)
    pressed, _ = draw_button(label or f'{name}()', name='call', left=0.0, top=top, width=min(width, BUTTON))
    top += ROW
    ran = False
    if pressed:
        try:
            draw_state.result = call(input_value, arguments)
            error = ''
            ran = True
        except Exception as failure:
            traceback.print_exc()
            error = f'{type(failure).__name__}: {failure}'
        draw_state.error = error
    if error:
        top += GAP
        imgui.set_cursor_pos((0.0, top))
        imgui.dummy(width, imgui.get_text_line_height())        # the error's line counts in the wrapped height
        draw_list = imgui.get_window_draw_list()
        draw_list.push_clip_rect(0.0, top, width, top + imgui.get_text_line_height(), True)
        draw_list.add_text(PAD, top, ERROR, error.split(chr(10))[0])
        draw_list.pop_clip_rect()
    return ran, input_value
