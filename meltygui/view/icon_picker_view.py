"""The icon picker: an inline widget on the editor's icon glyphs.

A Font Awesome glyph in the text (token kind `icon`; Ctrl+I inserts the
placeholder) is a chip: a click opens the picker under it, a grid of the
glyphs of `model/icon_model.py`. The picker takes the keyboard while it is
open: typing filters by name, the arrows and the pointer move the selection,
Enter or a click replaces the glyph, Escape or a click elsewhere closes.
It opens on the glyph the text has, which is offered even when the font
table does not list it.

`icon` is the chip (registered with `@inline('icon')`; it is one token for a
run of glyphs and picks for the one clicked), `icons` the picker: a temporary
popup (`popup=`), so opening, closing and the keyboard are core/input/focus.py's.
The grid's size is the levers below; what is offered is `catalog`.
"""
import meltygui_imgui as imgui

import meltygui.core.windowing.window_constants as glfw
from meltygui.core.rendering.gui_prototype import gui
from meltygui.editor.inline import inline
from meltygui.editor.keys import printable
from meltygui.hdr_color import pack_color
from meltygui.view.editor_view import LINE, font, look

# Levers: the grid's columns and rows, a cell's size in pixels, the space around the grid.
COLUMNS = 12
ROWS = 9
CELL = 26.
PAD = 4.
CATALOG = []


def catalog():
    """[(glyph, name)] of every glyph the picker offers, in the font table's order."""
    if not CATALOG:
        from meltygui.model.icon_model import FA_ICONS
        CATALOG.extend((glyph, label.split(None, 1)[-1]) for group in FA_ICONS.values() for label, glyph in group.items())
    return CATALOG


def offered(current, query):
    """The glyphs whose name holds `query`; `current` leads them when the table does not have it."""
    found = catalog()
    if current and all(glyph != current for glyph, _ in found):
        found = [(current, 'in the text')] + found
    wanted = query.lower()
    # The name itself first, then the names that start with what was typed.
    return sorted([item for item in found if wanted in item[1]],
                  key=lambda item: (item[1] != wanted, not item[1].startswith(wanted))) if wanted else found


def size(lh):
    """(width, height) of the picker under a line `lh` pixels high."""
    return COLUMNS * CELL + 2 * PAD, lh + 6 + ROWS * CELL + 2 * PAD


@gui(no_save=('query', 'index', 'first'))
def icons(input_value: str = '', query='', index=None, first=0, width=0.0, height=0.0,
          draw_state=None, cache=None, event=None):
    """The picker popup for the glyph `input_value`. Returns `(True, glyph)`
    when one was picked. `query` is what was typed, `index` the selected
    glyph among the offered ones (None: just opened), `first` the first row
    shown; none is saved, so every opening starts afresh."""
    pushed, cw, lh = font()
    head = lh + 6
    found = offered(input_value, query)
    fresh = index is None
    if fresh:
        # Just opened: the selection starts on the glyph the text has.
        index = next((at for at, (glyph, _) in enumerate(found) if glyph == input_value), 0)
    picked = False
    # The keyboard: typing filters, the arrows move the selection, Enter picks. Asking for the
    # keys is what gives the open picker the keyboard.
    pressed = event.keys()
    typed = event.text()
    was = query
    for key, mods in pressed:
        if printable(key, mods):
            continue
        step = {glfw.KEY_LEFT: -1, glfw.KEY_RIGHT: 1, glfw.KEY_UP: -COLUMNS, glfw.KEY_DOWN: COLUMNS,
                glfw.KEY_PAGE_UP: -COLUMNS * ROWS, glfw.KEY_PAGE_DOWN: COLUMNS * ROWS,
                glfw.KEY_HOME: -len(found), glfw.KEY_END: len(found)}.get(key)
        if step is not None:
            index += step
        elif key == glfw.KEY_BACKSPACE:
            query = query[:-1]
            typed = ''
        elif key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER):
            picked = True
    query += typed
    if query != was:
        found = offered(input_value, query)
        index = 0
    first -= int(event.mouse_wheel())
    # The pointer over the grid selects; a press picks.
    clicked = event.left_mouse_down()
    if clicked or event.mouse_moved():
        column = int((event.x - PAD) // CELL)
        row = int((event.y - head - PAD) // CELL)
        at = (first + row) * COLUMNS + column
        if 0 <= column < COLUMNS and 0 <= row < ROWS and at < len(found):
            index = at
            if clicked:
                picked = True
    # Keep the selection among the offered glyphs and, when the keyboard moved it, in sight.
    index = min(max(index, 0), max(len(found) - 1, 0))
    if fresh or pressed or typed:
        first = min(max(first, index // COLUMNS - ROWS + 1), index // COLUMNS)
    first = min(max(first, 0), max((len(found) - 1) // COLUMNS - ROWS + 1, 0))
    draw_state.query, draw_state.index, draw_state.first = query, index, first
    table = look()
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled(0, 0, width, height, table.pop, 4.)
    dl.add_text(PAD + 2, 3 + (lh - lh / LINE) * .5, table.caret if query else table.dim, query or 'type to search')
    if found:
        name = found[index][1]
        dl.add_text(width - PAD - 2 - len(name) * cw, 3 + (lh - lh / LINE) * .5, table.dim, name)
    dl.add_line(PAD, head, width - PAD, head, table.thumb, 1.)
    for at in range(first * COLUMNS, min((first + ROWS) * COLUMNS, len(found))):
        glyph = found[at][0]
        x, y = PAD + at % COLUMNS * CELL, head + PAD + (at // COLUMNS - first) * CELL
        if at == index:
            dl.add_rect_filled(x, y, x + CELL, y + CELL, table.pick, 4.)
        elif glyph == input_value:
            dl.add_rect(x + 1, y + 1, x + CELL - 1, y + CELL - 1, table.dim, 4.)
        dl.add_text(x + (CELL - imgui.calc_text_size(glyph).x) * .5, y + (CELL - lh / LINE) * .5, table.caret, glyph)
    rows = (len(found) + COLUMNS - 1) // COLUMNS
    if rows > ROWS:
        # Where the shown rows are among all of them.
        tall = ROWS * CELL
        dl.add_rect_filled(width - 3, head + PAD + tall * first / rows, width - 1, head + PAD + tall * (first + ROWS) / rows,
                           table.thumb, 1.)
    if pushed:
        imgui.pop_font()
    if picked and found:
        cache.focus.close()        # the editor types again
        return True, found[index][0]
    return False, input_value


@inline('icon', solid=True)
@gui(no_save=('shown',))
def icon(input_value: str, width=0., height=0., shown=0, draw_state=None):
    """The chip over a run of icon glyphs: a press on one opens the picker
    for it (`shown` is its place in the run), a press on the open one closes it."""
    dl = imgui.get_window_draw_list()
    count = max(len(input_value), 1)
    cell = width / count
    pressed, over = False, None
    for at in range(count):
        imgui.set_cursor_pos((at * cell, 0.))
        imgui.invisible_button(f'##icon{at}', cell, height)
        if imgui.is_item_activated():
            # A state write does not run the view again: carry on with the new value.
            draw_state.shown = shown = at
            pressed = True
        if imgui.is_item_hovered():
            over = at
    shown = min(shown, count - 1)
    wide, tall = size(height)
    edited, picked, opened = icons(input_value[shown:shown + 1], name='icons', popup=pressed, return_extras=True,
                                   left=shown * cell, top=height + 2, width=wide, height=tall)
    for at in {over, shown if opened is not None else None} - {None}:
        dl.add_rect(at * cell - 1, 1, (at + 1) * cell + 1, height - 1,
                    pack_color(1., 1., 1., .5 if at == shown and opened is not None else .25), 4.)
    if not edited:
        return False, input_value
    return True, input_value[:shown] + picked + input_value[shown + 1:]
