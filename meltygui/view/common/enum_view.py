"""An enum: its members side by side in one box, the current one lit; a press picks another.

    changed, mode = draw_enum(mode, name='mode', left=8.0, top=8.0, width=240.0)

`draw_any` draws every `Enum` member with it, `IntEnum` and `StrEnum` included (they would
otherwise go to the views of `int` and `str`, and an edit would lose the type).

- The row is as wide as its labels, and the press is the view's only over it (`event.reach`): the
  rest of the box moves the window, like the chip of a boolean.
- Members that do not fit the width are a dropdown instead (choice_view.py), read only a label.
- A `Flag` is a set of its members: each one that is in the value is lit, a press flips it.

The levers: `label` is what a member is called, `lit` and `pick` what a press means, `TINT` the
colours (`look(tint)` of value_view.py), and the sizes are that file's.
"""
from enum import Enum, Flag, IntEnum, IntFlag, StrEnum

import meltygui_imgui as imgui

from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_any import is_default_for
from meltygui.core.rendering.gui_prototype import gui
from meltygui.view.common.choice_view import draw_dropdown
from meltygui.view.common.value_view import PAD, RING, ROUND, ROW, look

TINT = (0.45, 0.33, 0.85)       # the tint an enum's colours are made from
MEMBERS = {}                    # enum class -> its members: one tuple per class, so the dropdown's `choices` never change


def label(member):
    """What a member is called: `FAST_MODE` reads 'Fast mode'. To show names as written, return `member.name`."""
    return str(member.name).replace('_', ' ').capitalize()


def lit(value, member):
    """Whether `member` is drawn as chosen: the value itself, or one of the members a `Flag` holds."""
    if isinstance(value, Flag):
        return member in value
    return member is value


def pick(value, member):
    """The value after a press on `member`: that member, or a `Flag` with it flipped."""
    if isinstance(value, Flag):
        return value ^ member
    return member


@is_default_for(Enum, IntEnum, StrEnum, IntFlag)
@gui(cursor=mouse_cursor.HAND, height=ROW)
def draw_enum(input_value: Enum, editable=True, width=0.0, height=0.0, canvas=None, event=None):
    """Returns (True, member) in the run another member is picked. A wrapping width (`width=None`)
    is the row's."""
    kind = type(input_value)
    members = MEMBERS.get(kind)
    if members is None:
        members = MEMBERS[kind] = tuple(kind)
    # Lever: a segment's width, its label with `PAD` either side. `edges` are where the segments begin and end.
    edges = [0.0]
    for member in members:
        edges.append(edges[-1] + imgui.calc_text_size(label(member)).x + 2.0 * PAD)
    wide = edges[-1]
    fits = wide <= width
    if canvas.width is None:
        width = min(width, wide)
    # The row is the view's, the rest of the box the window's; the dropdown takes its own presses.
    event.reach(0.0, 0.0, wide if editable and fits else 0.0, height)
    pressed = editable and fits and event.left_mouse_down()
    value = input_value
    table = look(TINT)
    draw_list = imgui.get_window_draw_list()
    y = (height - imgui.get_text_line_height()) * 0.5
    if not fits and editable:
        picked, member = draw_dropdown(input_value, name='list', choices=members, label=label, left=0.0, top=0.0,
                                       width=width, height=height)
        if picked:
            value = pick(input_value, member)
        return value != input_value, value
    imgui.dummy(width, height)          # the row is draw list shapes: this is what a wrapping width measures
    if not fits:
        draw_list.push_clip_rect(0.0, 0.0, width, height, True)
        draw_list.add_text(PAD, y, table.dim, label(input_value))
        draw_list.pop_clip_rect()
        return False, input_value
    if pressed:
        for at, member in enumerate(members):
            if edges[at] <= event.x < edges[at + 1]:
                value = pick(input_value, member)
    draw_list.add_rect_filled(0.0, 0.0, wide, height, table.field, ROUND)
    last = len(members) - 1
    for at, member in enumerate(members):
        on = lit(value, member)
        if on:
            corners = imgui.DRAW_ROUND_CORNERS_NONE
            if at == 0:
                corners = imgui.DRAW_ROUND_CORNERS_LEFT
            if at == last:
                corners = imgui.DRAW_ROUND_CORNERS_RIGHT
            if last == 0:
                corners = imgui.DRAW_ROUND_CORNERS_ALL
            draw_list.add_rect_filled(edges[at], 0.0, edges[at + 1], height, table.on, ROUND, corners)
        if at > 0:
            draw_list.add_rect_filled(edges[at] - 0.5, 0.0, edges[at] + 0.5, height, table.outline)
        draw_list.add_text(edges[at] + PAD, y, table.text if on else table.dim, label(member))
    draw_list.add_rect(0.5, 0.5, wide - 0.5, height - 0.5, table.outline, ROUND, 0, RING)
    return value != input_value, value
