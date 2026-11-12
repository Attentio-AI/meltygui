"""`draw_editor`: the code and text editor as native `@gui` views.

    changed, text = draw_editor(text, name='source', width=800, height=600)
    changed, source = draw_editor(source, ...)      # a `Source` (model/source_model.py), edited in place

Structure, outermost first:

- `draw_editor` owns the keyboard and pointer. It turns events into edit
  commands (`editor/keys.py`, `editor/edit.py`), keeps the session in step
  (`Editor.sync`) and places the caret, scroll bar and popovers. Ctrl+B
  (`editor/lookup.py`) is asked here and answered on a later run; while the
  find bar has the keyboard the keys are its field's (`editor/find.py`).
- `pane` is the scrolling viewport. It declares one `block` view for each
  document block near the viewport (`Editor.window` chooses them), so its cost
  does not depend on the file's length.
- `block` paints its lines and declares the inline widgets on them. A block
  runs again only when its stamp changes, so a keystroke repaints one block,
  the blocks below a new line only move, and scrolling repaints none.

To change how a row looks, edit `paint`; to change or add a decoration's
look, its painter and `SHAPES`. To add an inline widget, decorate a function
with `@inline(kind)` (see the built in ones at the end of this file).
Positions never come from `column * cw`: a line's widgets may add cells, so
`paint` asks the line's column map and everything else `Editor.x` / `column`.
"""
import threading
import time
from collections import namedtuple

import meltygui_imgui as imgui

from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_prototype import gui
from meltygui.editor import edit, keys, suggest, syntax
from meltygui.editor.marks import problems, values, pause, given
from meltygui.editor.suggest import ROWS
from meltygui.editor.editor import Editor, FLASH
from meltygui.editor.rows import Numbers
from meltygui.editor.inline import inline, for_token, for_value
from meltygui.editor.syntax import shift
from meltygui.core.runtime.toggles import Tint, Toggles
from meltygui.hdr_color import pack_color
from meltygui.model.source_model import Source
from meltygui.state.editor_state import EditorState, LOADING
import meltygui.core.windowing.window_constants as glfw

# --- look ------------------------------------------------------------------
# Everything the editor paints with is here. Sizes are the levers below;
# colours are the rows of `look()`, which reads the theme (`Tint`) and the
# editor settings (`Toggles.TextEditor`) once a run. Painters only read the
# table (`ed.look`). To restyle, change a row or the setting it reads.
LINE = 1.25                      # line height over the font's
BAR = 10.                        # scroll bar width
FOLD = 16.                       # gutter strip holding the fold chevrons
PAD = 6.                         # space left of the line numbers
CREEP = 1.5                      # rows a second a held selection scrolls, per pixel past the edge
PLAIN = pack_color(.66, .72, .78)        # text without a token colour
FOUND = (.98, .62, .12)                  # the current search hit: its fill and frame
FIELD = 24                               # cells of the find bar's field (it grows with a longer term)
CHIP = 3                                 # cells of an error's chip over the line number
DETAIL = 6.                              # an error's detail window: its vertical padding, and its gap to the chip
BRIGHT = .8                              # a symbol's tint as text (a list row): its brightest channel is lifted to this
Look = namedtuple('Look', 'back gutter number caret band select guide guide_width scope block symbol hit flash folded '
                          'error chip glyph breakpoint pause paused value pill ghost thumb pop pick dim')


def look(tint=None):
    """The colour table for a file tinted `tint` ((r, g, b); None: the theme)."""
    return Look(
        # The body: the host's background shows through; a file tint washes it.
        back=pack_color(*tint[:3], .06) if tint else None,
        gutter=pack_color(*Tint.line_number_bg()[:3]),
        number=pack_color(*Tint.line_number_tint(tint)[:3]),
        caret=pack_color(*Tint.cursor_tint()[:3]),
        band=pack_color(*Tint.cursor_line_tint()[:3], Toggles.TextEditor.cursor_line_alpha),
        select=pack_color(*Tint.text_selection()[:3], .4),
        guide=pack_color(*(tint or Tint.scope_guide())[:3], Toggles.TextEditor.scope_guide_alpha),
        guide_width=Toggles.TextEditor.scope_guide_thickness,
        scope=pack_color(1., 1., 1., .014),                         # a scope without a tint
        block=.055 * Toggles.TextEditor.def_block_alpha,            # strength of a definition's tint wash
        symbol=.14 * Toggles.TextEditor.def_symbol_alpha,           # strength of the wash behind its uses
        hit=pack_color(.75, .60, .15, .45),
        flash=pack_color(.98, .80, .25, .3),
        folded=pack_color(.55, .60, .68),
        error=pack_color(.94, .33, .31),
        chip=pack_color(.62, .13, .15),                             # an error's chip in the gutter
        glyph=pack_color(1., .80, .78),                             # the glyph on it
        breakpoint=pack_color(.86, .24, .24),
        pause=pack_color(.98, .80, .25),
        paused=pack_color(.98, .80, .25, .16),
        value=pack_color(.45, .78, .66),
        pill=pack_color(.45, .78, .66, .13),
        ghost=pack_color(.66, .70, .78, .5),
        thumb=pack_color(1., 1., 1., .18),
        pop=pack_color(.17, .18, .20),
        pick=pack_color(.21, .39, .64),
        dim=pack_color(.50, .53, .57))


def font():
    """Push the code font. Returns (pushed, character width, line height)."""
    from meltygui.core.melty import Melty
    pushed = False
    if Melty.font_mgr is not None:
        from meltygui.core.styling.fonts import Font
        handle = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19)
        if handle is not None:
            imgui.push_font(handle)
            pushed = True
    return pushed, imgui.calc_text_size('0' * 32).x / 32, float(int(imgui.get_text_line_height() * LINE + .5))


def palette():
    return syntax.tools().COLORS if syntax.ready() else {}


# --- painting -------------------------------------------------------------
class Row:
    """What a shape painter gets: the draw list, one row's pixels and its
    column map (`cx`). `tail` is where the next thing after the line's text
    goes; a painter that puts something there moves it on."""
    __slots__ = ('dl', 'ed', 'look', 'line', 'at', 'y', 'ty', 'lh', 'cw', 'left', 'width', 'gutter', 'text', 'cx', 'end',
                 'tail', 'number', 'spots', 'values')


def lit(rgb):
    """A symbol's tint as a text colour: the same hue, lifted to `BRIGHT` when it is darker (tints are
    chosen as background washes, and many are too dark to read). None for none, or for black."""
    most = max(rgb[:3]) if rgb else 0.
    return None if most <= 0. else rgb if most >= BRIGHT else tuple(channel * BRIGHT / most for channel in rgb[:3])


def tone(rgb, alpha, fallback):
    """A mark's colour (an (r, g, b) tuple) at `alpha`, or the shape's own."""
    return pack_color(rgb[0], rgb[1], rgb[2], alpha) if rgb else fallback


def span(row, mark):
    """(x0, x1) of a mark: its columns, or the line's text from its indent."""
    if mark.high is None:
        return row.cx(len(row.text) - len(row.text.lstrip())), max(row.end, row.left + row.cw)
    return row.cx(mark.low), row.cx(mark.high - 1) + row.cw


def label(row, text, color, back=None, frame=None, action=None):
    """Text after the line; `action(ed, line)` makes it clickable."""
    x, wide = row.tail, len(text) * row.cw
    if back is not None:
        row.dl.add_rect_filled(x, row.y + 2, x + wide, row.y + row.lh - 2, back, 3.)
    if frame is not None:
        row.dl.add_rect(x, row.y + 2, x + wide, row.y + row.lh - 2, frame, 3.)
    row.dl.add_text(x, row.ty, color, text)
    if action is not None:
        row.spots.append((row.at, x, x + wide, action, row.line))
    row.tail = x + wide + row.cw


def paint_band(row, mark):
    row.dl.add_rect_filled(row.gutter, row.y, row.width, row.y + row.lh, tone(mark.color, .16, row.look.paused))


def paint_wash(row, mark):
    x0, x1 = span(row, mark)
    row.dl.add_rect_filled(x0, row.y, x1, row.y + row.lh, tone(mark.color, row.look.symbol, row.look.scope), 2.)


def paint_hit(row, mark):
    x0, x1 = span(row, mark)
    row.dl.add_rect_filled(x0, row.y + 1, x1, row.y + row.lh - 1, tone(mark.color, .45, row.look.hit), 2.)


def paint_found(row, mark):
    x0, x1 = span(row, mark)
    row.dl.add_rect_filled(x0, row.y + 1, x1, row.y + row.lh - 1, tone(mark.color or FOUND, .35, 0), 2.)
    row.dl.add_rect(x0 - 1, row.y, x1 + 1, row.y + row.lh, tone(mark.color or FOUND, 1., 0), 2.)


def paint_flash(row, mark):
    x0, x1 = span(row, mark)
    row.dl.add_rect_filled(x0 - 3, row.y, x1 + 3, row.y + row.lh, tone(mark.color, .3, row.look.flash), 3.)


def paint_box(row, mark):
    x0, x1 = span(row, mark)
    row.dl.add_rect(x0, row.y + 1, x1, row.y + row.lh - 1, tone(mark.color, 1., row.look.caret), 2.)


def paint_under(row, mark):
    x0, x1 = span(row, mark)
    row.dl.add_line(x0, row.y + row.lh - 2, x1, row.y + row.lh - 2, tone(mark.color, 1., row.look.error), 1.5)


def paint_squiggle(row, mark):
    x0, x1 = span(row, mark)
    y, color, up = row.y + row.lh - 2, tone(mark.color, 1., row.look.error), False
    while x0 < x1:
        row.dl.add_line(x0, y - 2 * up, min(x0 + 3, x1), y - 2 * (not up), color, 1.)
        x0, up = x0 + 3, not up


def roomy(row):
    """Whether the gutter holds an error's chip: over the line number, so the numbers must be shown."""
    return row.gutter - FOLD - CHIP * row.cw >= PAD



def paint_chip(row, mark):
    """An error's chip in place of its line number: a click opens the detail (the message and
    its quick fixes, `popovers`), another closes it. Framed while it is open."""
    if not roomy(row):
        return
    x1 = row.gutter - FOLD + 1
    x0 = x1 - CHIP * row.cw - 1
    icon = f''
    row.dl.add_rect_filled(x0, row.y + 2, x1, row.y + row.lh - 2, tone(mark.color, 1., row.look.chip), 4.)
    if row.ed.detail == row.line:
        row.dl.add_rect(x0, row.y + 2, x1, row.y + row.lh - 2, row.look.glyph, 4.)
    row.dl.add_text((x0 + x1 - imgui.calc_text_size(icon).x) * .5, row.y + (row.lh - row.lh / LINE) * .5, row.look.glyph, icon)
    row.spots.append((row.at, x0, x1, open_detail, row.line))
    row.number = None


def paint_error(row, mark):
    """The message after the line, only where no gutter holds the chip (a single line field, hidden numbers)."""
    if not roomy(row):
        message, fixes = mark.data
        label(row, message.split('\n')[0][:120] + (f'   fix: {fixes[0]}' if fixes else ''), tone(mark.color, 1., row.look.error),
              action=open_detail)


def paint_note(row, mark):
    label(row, str(mark.data), tone(mark.color, 1., row.look.dim))


def paint_value(row, mark):
    value = mark.data
    entry = for_value(value.value)
    if entry is not None:
        # An inline view of the value: the block declares it where the text would go.
        row.values.append((row.at, row.line, row.tail, value, entry))
        row.tail += (entry.cells + 1) * row.cw
    else:
        label(row, f' {value.name} = {value.text if value.text is not None else type(value.value).__name__} ', row.look.value, row.look.pill)


def paint_dot(row, mark):
    row.dl.add_circle_filled(PAD + 4, row.y + row.lh * .5, row.lh * .28, tone(mark.color, 1., row.look.breakpoint))


def paint_arrow(row, mark):
    mid = row.y + row.lh * .5
    row.dl.add_triangle_filled(PAD, mid - 5, PAD + 9, mid, PAD, mid + 5, tone(mark.color, 1., row.look.pause))


def paint_bar(row, mark):
    row.dl.add_rect_filled(0, row.y, 3, row.y + row.lh, tone(mark.color, 1., row.look.pause))


def paint_heat(row, mark):
    row.dl.add_rect_filled(PAD - 2, row.y + 1, row.gutter - FOLD, row.y + row.lh - 1, tone(mark.color, .35, row.look.pill), 3.)


def paint_number(row, mark):
    row.number = tone(mark.color, 1., row.look.error)


def open_detail(ed, line):
    ed.detail = line


def toggle_fold(ed, line):
    ed.toggle(line)


# How each mark shape paints: (layer, painter). To add a shape, write its
# painter above and list it here; a producer can then use it. A shape that is
# not listed is data only (a 'tint' colours its scope, a caller's own notes).
UNDER, MATCH, GLYPH, OVER, EDGE = range(5)
SHAPES = {
    'band': (UNDER, paint_band),            # a wash across the line
    'wash': (UNDER, paint_wash),            # a soft wash behind a span
    'hit': (MATCH, paint_hit),              # a search hit: over the selection, under the text
    'found': (MATCH, paint_found),          # the current search hit
    'flash': (MATCH, paint_flash),          # the text an undo or a jump just landed on
    'color': (GLYPH, None),                 # the glyphs of a span take the colour (`data`: how much, 0..1)
    'box': (OVER, paint_box),               # a frame around a span
    'under': (OVER, paint_under),           # an underline
    'squiggle': (OVER, paint_squiggle),     # a wavy underline
    'error': (OVER, paint_error),           # an error's (message, fixes); painted after the line only without a gutter
    'note': (OVER, paint_note),             # plain text after the line
    'value': (OVER, paint_value),           # a live value after the line
    'dot': (EDGE, paint_dot),               # gutter: a breakpoint
    'arrow': (EDGE, paint_arrow),           # gutter: the paused line
    'bar': (EDGE, paint_bar),               # gutter: a band down its left edge
    'heat': (EDGE, paint_heat),             # gutter: a box behind the number
    'chip': (EDGE, paint_chip),             # gutter: an error's chip in place of the number, opens its detail
    'number': (EDGE, paint_number),         # gutter: the number's colour
}


def recolour(runs, marks, colors):
    """[(column, text, packed colour)] of a line's runs with its 'color'
    marks applied: runs are cut at the marks' columns."""
    mix, found = syntax.tools()._mix_packed, []
    for column, piece, kind in runs:
        base, at, stop = colors.get(kind, PLAIN), column, column + len(piece)
        for mark in marks:
            low, high = (0, stop) if mark.high is None else (mark.low, mark.high)
            if high <= at or low >= stop:
                continue
            if low > at:
                found.append((at, piece[at - column:low - column], base))
                at = low
            cut = min(high, stop)
            found.append((at, piece[at - column:cut - column], mix(base, mark.color, mark.data or 1.)))
            at = cut
        if at < stop:
            found.append((at, piece[at - column:], base))
    return found


def paint(ed, block, first, top, width, numbers, show_scopes):
    """Draw the lines of `block` (its first document line is `first`, its
    first row `top`) in block-local coordinates: per row the marks under the
    text, the selection, the text, the marks over it, then the gutter.
    Returns the widget sites [(row, line, column, x, text, entry)], the live
    value sites and the click spots."""
    dl = imgui.get_window_draw_list()
    doc, rows, colors, look = ed.doc, ed.rows, palette(), ed.look
    cw, lh, gutter = ed.cw, ed.lh, ed.gutter
    texts, states = doc.rows(first, block.count), ed.syntax.states(first, block.count)
    lines = [line for line in range(first, first + block.count) if not rows.hidden(line)] if rows.runs \
        else range(first, first + block.count)
    if not lines:
        return (), (), ()
    head, tail = lines[0], lines[-1]
    left = gutter - ed.scroll_x
    low, high = doc.selection
    # The offset each line starts at.
    starts, start = [], doc.start(first)
    for text in texts:
        starts.append(start)
        start += len(text) + 1
    # Columns on screen: long lines only submit this band.
    band0 = int(ed.scroll_x // cw)
    band1 = band0 + int((width - gutter) // cw) + 2
    # The marks on these lines, by layer and line, in set order.
    layers = ({}, {}, {}, {}, {})
    for found in ed.marks.values():
        for line, mark in found.on(head, tail) if found is not None else ():
            shape = SHAPES.get(mark.shape)
            if shape is not None:
                layers[shape[0]].setdefault(line, []).append((shape[1], mark))
    under, match, glyph, over, edge = layers
    dl.push_clip_rect(gutter, 0, width, len(lines) * lh, True)
    if show_scopes:
        for start, last, indent, definition in rows.guides(head, tail):
            y0 = (rows.row(max(start, head)) - top) * lh
            y1 = (rows.row(min(last, tail)) - top + 1) * lh
            x = left + indent * cw
            if definition:
                # A tinted definition is the scope headed by a 'tint' mark.
                tint = ed.mark('tints', start, 'tint')
                dl.add_rect_filled(x, y0, width, y1, tone(tint and tint.color, look.block, look.scope))
            if start >= head:
                y0 += lh
            if y1 > y0:
                dl.add_line(x + .5, y0, x + .5, y1, look.guide, look.guide_width)
    row = Row()
    row.dl, row.ed, row.look, row.lh, row.cw, row.left, row.width, row.gutter = dl, ed, look, lh, cw, left, width, gutter
    sites, row.values, row.spots = [], [], []
    wide = 0
    for at, line in enumerate(lines):
        y = at * lh
        local = line - first
        text = texts[local]
        start = starts[local]
        stop = start + len(text)
        state = states[local]
        # The line's column map: `inset` is None on a line without widget cells.
        inset = ed.syntax.insets(text, state)
        if inset is None:
            def cx(column):
                return left + column * cw
        else:
            def cx(column, inset=inset):
                return left + (column + shift(inset, column)) * cw
        end = cx(len(text) - 1) + cw if text else left
        wide = max(wide, int((end - left) / cw))
        row.line, row.at, row.y, row.ty, row.text, row.cx, row.end = line, at, y, y + (lh - lh / LINE) * .5, text, cx, end
        row.tail = end + cw
        for draw, mark in under.get(line, ()):
            draw(row, mark)
        if low < high and low <= stop and high > start:
            dl.add_rect_filled(cx(max(low, start) - start), y,
                               (cx(min(high, stop) - start - 1) + cw if min(high, stop) > start else left) + (high > stop) * cw,
                               y + lh, look.select)
        for draw, mark in match.get(line, ()):
            draw(row, mark)
        ty = row.ty
        runs = ed.syntax.runs(text, state)
        for column, piece, kind in runs:
            if column >= band1 and inset is None:
                break
            entry = for_token(kind)
            if entry is not None:
                sites.append((at, line, column, cx(column), piece, entry))
            if line in glyph:
                continue
            if column + len(piece) <= band0 and inset is None:
                continue
            if len(piece) > band1 - band0:
                cut = max(band0 - column, 0)
                piece, column = piece[cut:band1 - column], column + cut
            if piece.isascii():
                dl.add_text(cx(column), ty, colors.get(kind, PLAIN), piece)
            else:
                # Glyphs outside the mono face each take their own cell.
                color, x = colors.get(kind, PLAIN), cx(column)
                for offset, char in enumerate(piece):
                    dl.add_text(x + offset * cw, ty, color, char)
        if line in glyph:
            for column, piece, color in recolour(runs, [mark for _, mark in glyph[line]], colors):
                x = cx(column)
                for offset, char in enumerate(piece) if not piece.isascii() else ((0, piece),):
                    dl.add_text(x + offset * cw, ty, color, char)
        hidden = rows.folded(line)
        if hidden:
            label(row, f' {hidden} lines ', look.folded, frame=look.folded, action=toggle_fold)
        for draw, mark in over.get(line, ()):
            draw(row, mark)
    block.wide = wide
    dl.pop_clip_rect()
    if gutter:
        dl.add_rect_filled(0, 0, gutter, len(lines) * lh, look.gutter)
        for at, line in enumerate(lines):
            row.line, row.at, row.y, row.number = line, at, at * lh, look.number
            for draw, mark in edge.get(line, ()):
                draw(row, mark)
            number = ed.numbers.number(line) if numbers and row.number is not None else None
            if number is not None:
                text = str(number)
                dl.add_text(gutter - FOLD - len(text) * cw, row.y + (lh - lh / LINE) * .5, row.number, text)
            if rows.fold(line) is not None:
                x, mid = gutter - FOLD * .5 - 2, row.y + lh * .5
                if rows.folded(line):
                    dl.add_triangle_filled(x - 3, mid - 5, x + 4, mid, x - 3, mid + 5, look.folded)
                else:
                    dl.add_triangle_filled(x - 5, mid - 3, x + 5, mid - 3, x, mid + 4, look.number)
    return sites, row.values, row.spots


@gui(listen=(), cursor=mouse_cursor.TEXT)
def block(input_value, at=0, stamp=0, first=0, width=0., height=0., widgets=True, numbers=True, scopes=True):
    """The lines of document block `at` (its key) and the inline widgets on
    them. `stamp` and `first` only exist to run the body again when the
    lines or their numbers changed."""
    ed = input_value
    found, placed = ed.blocks.get(at), ed.placed.get(at)
    if found is None or placed is None:
        # An edit merged this block away: the pane is about to stop declaring it.
        return False, ed
    pushed, _, _ = font()
    try:
        sites, values, ed.spots[at] = paint(ed, found, placed[0], placed[1], width, numbers, scopes)
    finally:
        if pushed:
            imgui.pop_font()
    changed = False
    cw, lh = ed.cw, ed.lh
    if widgets:
        for row, line, column, x, text, entry in sites:
            # Where the widget sits: its own cells before or after the token, or the token's.
            cells = entry.lead or entry.trail or len(text)
            if entry.lead:
                x -= cells * cw
            elif entry.trail:
                x += len(text) * cw
            elif ed.inside == (line, column):
                continue        # the caret is in this token: it edits as text
            if x < ed.gutter or x + cells * cw > width:
                continue
            edited, value = entry.view(text, key=(row, column), left=x, top=row * lh, width=cells * cw, height=lh)
            if edited and isinstance(value, str) and value != text:
                # One undo step for a whole drag: the pointer is the widget's until it lets go.
                ed.dragging = True
                edit.set_token(ed.doc, line, column, len(text), value)
                ed.dragging = False
                changed = True
    for row, line, x, value, entry in values:
        if x >= ed.gutter and x + entry.cells * cw <= width:
            entry.view(value.value, key=('live', row, value.name), left=x, top=row * lh, width=entry.cells * cw, height=lh)
    return changed, ed


@gui(listen=())
def band(input_value, color=0):
    """The current line highlight, moved by its `top`."""
    imgui.get_window_draw_list().add_rect_filled(0, 0, 8192, 8192, color)


@gui(listen=())
def caret(input_value, shown=True, ghost='', color=0, faint=0, width=0., height=0.):
    """The caret bar and the ghost suggestion after it, moved by `left`/`top`."""
    dl = imgui.get_window_draw_list()
    if shown:
        dl.add_rect_filled(0, 1, 2, height - 1, color)
    if ghost:
        pushed, _, lh = font()
        dl.add_text(2, (lh - lh / LINE) * .5, faint, ghost.split('\n')[0])
        if pushed:
            imgui.pop_font()


@gui(listen=())
def trail(input_value, lines=(), shift=0., color=0, width=0., height=0.):
    """The lines of a ghost suggestion after its first, dim over the rows
    below the caret: the text starts `shift` pixels left of the view."""
    dl = imgui.get_window_draw_list()
    pushed, _, lh = font()
    for at, line in enumerate(lines):
        dl.add_text(-shift, at * lh + (lh - lh / LINE) * .5, color, line)
    if pushed:
        imgui.pop_font()


@gui(listen=(), cursor=mouse_cursor.HAND)
def margin(input_value):
    """Nothing: the pointer's shape over the gutter, a hand. What is there is pressed (a fold, a
    breakpoint, an error's chip, a line's number), never typed in. Its presses are the editor's."""


@gui(listen=())
def end(input_value):
    """Nothing: its position is the last row, which gives the pane its scroll range."""


@gui(scroll=True, listen=())
def pane(input_value, stamp=0, width=0., height=0., widgets=True, numbers=True, scopes=True,
         focused=False, ghost='', single=False):
    """The scrolling viewport: the blocks near it, in content coordinates."""
    ed = input_value
    lh = ed.lh
    x, y = ed.where(ed.doc.caret)
    if not single:
        _, _, ed.band = band(None, name='band', color=ed.look.band, width=width, height=lh,
                             initial={'left': 0., 'top': y}, return_extras=True)
    changed = False
    base = ed.base
    for found, first, row, count in ed.shown:
        edited, _ = block(ed, key=found.key, at=found.key, stamp=max(found.stamp, base) + found.edit, first=first if numbers else 0,
                          left=0., top=row * lh, width=width, height=count * lh, widgets=widgets, numbers=numbers,
                          scopes=scopes)
        changed |= edited
    end(None, name='end', left=0., top=(ed.rows.count - 1) * lh, width=1., height=lh)
    # A ghost suggestion: its first line after the caret, the others over the rows below (Tab inserts all of it).
    ghost, *rest = ghost.split('\n')[:1 if single else None]
    _, _, ed.caret = caret(None, name='caret', shown=focused, ghost=ghost, color=ed.look.caret, faint=ed.look.ghost,
                           width=max(2., len(ghost) * ed.cw + 4), height=lh,
                           initial={'left': x - ed.scroll_x, 'top': y}, return_extras=True)
    if rest:
        trail(None, name='trail', lines=tuple(rest), shift=ed.scroll_x, color=ed.look.ghost,
              left=ed.gutter, top=y + lh, width=max(width - ed.gutter - BAR, 1.), height=len(rest) * lh)
    return changed, ed


# --- popovers -------------------------------------------------------------
@gui(listen=('pointer', 'hover', 'wheel'))
def choices(input_value, which='suggest', rows=(), index=0, first=0, tick=0, width=0., height=0., view_events=None):
    """The list popover: the suggestions (`which='suggest'`), an error's
    quick fixes ('fixes') or the usage picker ('lookup'), each a
    `suggest.Menu` on the session. The keyboard
    stays with the editor, which moves the selection; here the pointer does:
    hovering selects, the wheel scrolls, a click picks (the editor acts on
    `Menu.picked` on its next run). `rows`, `index`, `first` and `tick` only
    exist to run the body again when the list or what its rows show changed."""
    ed, look = input_value, input_value.look
    menu = getattr(ed, which)
    dl = imgui.get_window_draw_list()
    pushed, cw, lh = font()
    picked = False
    for across, down in view_events.get('wheel', ()):
        menu.scroll(-int(down))
    for event in view_events.get('hover', ()):
        if event.kind == 'hover':
            menu.point(int(event.y // lh))
    for event in view_events.get('pointer', ()):
        if event.kind == 'press' and not event.button:
            menu.point(int(event.y // lh))
            menu.picked = menu.index
            picked = True
    dl.add_rect_filled(0, 0, width, height, look.pop, 4.)
    for at in range(menu.first, min(menu.first + ROWS, len(menu.rows))):
        y = (at - menu.first) * lh
        ty = y + (lh - lh / LINE) * .5
        text, tag = menu.rows[at]
        tint, suffix = menu.extra(at)
        if at == menu.index:
            dl.add_rect_filled(0, y, width, y + lh, look.pick, 3.)
        dl.add_text(6, ty, tone(lit(tint), 1., look.caret), text)
        if suffix:
            # (A parameter list stands right after its name; anything else two cells on.)
            dl.add_text(6 + (len(text) + 2 * (suffix[0] != '(')) * cw, ty, look.dim, suffix)
        dl.add_text(width - (len(tag) + 1) * cw, ty, look.dim, tag)
    if len(menu.rows) > ROWS:
        # Where the shown rows are in the list.
        size = height * ROWS / len(menu.rows)
        top = (height - size) * menu.first / (len(menu.rows) - ROWS)
        dl.add_rect_filled(width - 4, top, width - 1, top + size, look.thumb, 2.)
    if pushed:
        imgui.pop_font()
    return picked, ed


def pop(ed, name, which, left, top, height, at=None):
    """Declare the list popover for the session's menu `which`, below the row at `top` or above it
    when it would not fit; `at` puts its top edge there instead."""
    menu, cw, lh = getattr(ed, which), ed.cw, ed.lh
    count = min(len(menu.rows), ROWS)
    wide = (menu.cells or menu.wide() + 4) * cw + 12
    below = top + lh
    if at is None:
        at = below if below + count * lh <= height or top < count * lh else top - count * lh
    choices(ed, name=name, melty_window=True, content_offset=(0., 0.), which=which, rows=menu.rows, index=menu.index,
            first=menu.first, tick=menu.tick, left=left, width=wide, height=count * lh, top=at)


@gui(listen=('pointer',))
def note(input_value, lines=(), width=0., height=0., color=0, frame=0, view_events=None):
    """A text popover: the full error message (a little window: `frame` is its outline) or a signature hint."""
    look = input_value.look
    dl = imgui.get_window_draw_list()
    pushed, cw, lh = font()
    dl.add_rect_filled(0, 0, width, height, look.pop, 4.)
    if frame:
        dl.add_rect(.5, .5, width - .5, height - .5, frame, 4.)
    top = (height - len(lines) * lh) * .5
    for at, line in enumerate(lines):
        dl.add_text(8, top + at * lh + (lh - lh / LINE) * .5, color or look.caret if at == 0 else look.dim, line)
    if pushed:
        imgui.pop_font()


@gui(listen=('pointer',))
def finder(input_value, text='', low=0, high=0, caret=0, focused=False, count='', missed=False, width=0., height=0., view_events=None):
    """The find bar: the glass, the term in its field with the field's own
    caret and selection, the count, the close cross. The keyboard stays
    with the editor, which gives it to the field (`Find.typed`); here a
    press on the cross closes and one on the field puts its caret there
    (the editor acts on `Find.clicked` on its next run). The parameters
    besides the session only exist to run the body again when they change."""
    ed, look = input_value, input_value.look
    dl = imgui.get_window_draw_list()
    pushed, cw, lh = font()
    left, cross = 3.5 * cw, width - 2.5 * cw
    pressed = False
    for event in view_events.get('pointer', ()):
        if event.kind == 'press' and not event.button:
            ed.search.clicked = 'close' if event.x >= cross else max(int((event.x - left) / cw + .5), 0)
            pressed = True
    y = (height - lh) * .5
    ty = y + (lh - lh / LINE) * .5
    dl.add_rect_filled(0, 0, width, height, look.pop, 5.)
    dl.add_text(cw, ty, look.dim, f'')
    if low < high:
        dl.add_rect_filled(left + low * cw, y, left + high * cw, y + lh, look.select)
    dl.add_text(left, ty, look.caret, text)
    if focused:
        dl.add_rect_filled(left + caret * cw, y + 1, left + caret * cw + 2, y + lh - 1, look.caret)
    dl.add_text(cross - (len(count) + 1.5) * cw, ty, look.error if missed else look.dim, count)
    dl.add_text(cross + .5 * cw, ty, look.dim, f'')
    if pushed:
        imgui.pop_font()
    return pressed, ed


def bar(ed, width, height):
    """Declare the find bar in the view's lower right corner (where the legacy editor's is)."""
    find, cw, lh = ed.search, ed.cw, ed.lh
    field, found = find.field.doc, find.state(ed)
    arrived = ed.marks.get('hits') is not None
    count = f'{found.index} of {found.count}' if found.count else 'no match' if ed.term and arrived else ''
    low, high = field.selection
    wide = (max(field.size + 1, FIELD) + 18) * cw
    finder(ed, name='find', melty_window=True, content_offset=(0., 0.), text=field.text, low=low, high=high, caret=field.caret,
           focused=find.focused and ed.focused, count=count, missed=bool(ed.term) and arrived and not found.count,
           left=max(width - BAR - wide - 6, 0.), top=max(height - lh - 16, 0.), width=wide, height=lh + 8)


# --- the editor -----------------------------------------------------------
def session(state, value, language, fold_defaults, wake, path, waker=None):
    """The view's `Editor`, created on first use and restored from `state`.
    `wake` runs this view again from Python; `waker` is the same for the
    source's reading thread, which holds no interpreter.
    Another `path` (or another `Source` as the value) is another document: a
    new session, not an edit."""
    ed = state.editor
    if value is LOADING:
        if ed is None or not ed.standin:
            # The rows the last session showed, where they were: blank lines around the snapshot.
            below = max(state.view_rows - state.view_first - state.view_text.count('\n') - 1, 0)
            ed = state.editor = Editor('\n' * state.view_first + state.view_text + '\n' * below, language, False)
            ed.standin, ed.path = True, path
            ed.numbers = Numbers(table=[None] * state.view_first + list(state.view_numbers))
            ed.sources['numbers'] = (None, None)
            if language == 'python':
                syntax.warm(wake)
        ed.wake, ed.waker = wake, waker
        return ed
    source = value if isinstance(value, Source) else None
    if ed is None or ed.path != path or ed.standin or (source is not None and ed.source is not source):
        if ed is not None:
            ed.close()
        ed = state.editor = Editor(value if source is None else '', language, fold_defaults, state.collapsed, state.gaps, source)
        ed.path = path
        ed.doc.select(state.caret, state.anchor)
        if language == 'python':
            syntax.warm(wake)
    elif source is None and value is not ed.gave and value != ed.gave:
        # Not what this view returned: the caller's text. The host hands a run its own last edit until
        # the caller passes another value, or still passes the old one a few frames on (retained.rs, `SETTLE`).
        ed.load(value)
        ed.returned = ed.doc.version
    ed.given = value
    ed.wake, ed.waker = wake, waker
    return ed


def spot(ed, x, y):
    """(action, line) of what was painted besides text at view x and content
    y (a fold label, an error's chip in the gutter), or None."""
    row = int(y // ed.lh)
    for found, _, top, count in ed.shown:
        if top <= row < top + count:
            for at, low, high, action, line in ed.spots.get(found.key, ()):
                if at == row - top and low <= x < high:
                    return action, line
    return None


def slide(ed, scroll_x, width):
    """Scroll sideways to `scroll_x`, no further than the longest line on screen."""
    reach = max((max((found.wide for found, *_ in ed.shown), default=0) + 4) * ed.cw - (width - ed.gutter - BAR), 0.)
    scroll_x = min(max(scroll_x, 0.), max(reach, ed.scroll_x))
    if scroll_x != ed.scroll_x:
        ed.scroll_x = scroll_x
        ed.touch()


def sideways(ed, low, high, width):
    """Scroll sideways, as little as it takes, so that the text from offset
    `low` to `high` (what was landed on) is in view; a span over several
    lines by its first."""
    doc = ed.doc
    (line, column), (last, stop) = doc.place(low), doc.place(high)
    x0, x1 = ed.x(line, column), ed.x(line, stop if last == line else len(doc.line(line)))
    room = width - ed.gutter - BAR - ed.cw
    if x0 - ed.gutter < ed.scroll_x or room <= 0:
        scroll_x = max(x0 - ed.gutter - ed.cw, 0.)
    elif x1 - ed.gutter > ed.scroll_x + room:
        scroll_x = min(x1 - ed.gutter - room, x0 - ed.gutter)
    else:
        return
    if scroll_x != ed.scroll_x:
        ed.scroll_x = scroll_x
        ed.touch()


def press(ed, event, desk, scroll_y, width, height, widgets):
    """A pointer press, drag or release in view coordinates. The left button
    places the caret and selects (`ed.hold` says what it holds: text, by
    character or, after a double or triple click, by word or line; the
    scroll bar; a widget). The other buttons are not the editor's: a right
    drag resizes the window around it, as everywhere."""
    doc = ed.doc
    x, y = event.x, event.y
    line, column = ed.spot(x, y, scroll_y)
    index = doc.index(line, column)
    if event.kind == 'press' and not event.button:
        opened = ed.detail
        ed.hold = ed.detail = ed.step = None
        ed.creep = (0., time.monotonic())
        ed.suggest.drop()
        ed.lookup.drop()
        ed.search.focused = False       # the bar stays open: the keyboard is the text's
        if x >= width - BAR and not desk.single:
            ed.hold = ('bar', y, scroll_y)
        elif x < ed.gutter:
            found = spot(ed, x, y + scroll_y)
            if found is not None:
                found[0](ed, found[1])
                if ed.detail == opened:
                    ed.detail = None        # a press on the chip of the open detail closes it
            elif x >= ed.gutter - FOLD and ed.rows.fold(line) is not None:
                ed.toggle(line)
            elif ed.slots.get('break') is not None:
                ed.slots['break'](line)
            else:
                edit.select_line(doc, index)
        else:
            found = spot(ed, x, y + scroll_y)
            if found is not None:
                return found[0](ed, found[1])
            found = ed.widget(line, x + ed.scroll_x, False) if widgets and event.clicks == 2 else None
            if found is not None and found[0].double is not None:
                # A double click on a token whose widget takes it (a boolean flips), whether the first
                # click put the caret in it or not.
                was = doc.line(line)[found[1]:found[1] + found[2]]
                edit.set_token(doc, line, found[1], found[2], found[0].double(was))
                return
            found = ed.widget(line, x + ed.scroll_x) if widgets else None
            if found is not None:
                # The widget takes the press; a plain click may still place the caret.
                ed.hold = ('widget', index if found[0].caret else None, x, y)
                return
            if event.clicks == 2:
                edit.select_word(doc, index)
            elif event.clicks == 3:
                edit.select_line(doc, index)
            else:
                edit.move(doc, index, select=event.shift)
            # A drag from a double or triple click extends by word or line around the one it selected.
            ed.hold = ('text', (None, None, 'word', 'line')[event.clicks], *doc.selection) if event.clicks > 1 else ('text',)
    elif event.button:
        return
    elif ed.hold is not None:
        if ed.hold[0] == 'text' and event.kind == 'hold':
            # Held past the top or bottom edge: the selection creeps on, faster the further out.
            now = time.monotonic()
            beyond = y if y < 0 else y - height
            ed.creep = (ed.creep[0] + beyond * CREEP * min(now - ed.creep[1], .05), now)
            rows = int(ed.creep[0])
            if rows:
                ed.creep = (ed.creep[0] - rows, now)
                edge = scroll_y if beyond < 0 else scroll_y + height - ed.lh
                edit.extend(doc, doc.index(ed.rows.line(int(edge // ed.lh) + rows), column), *ed.hold[1:])
        elif ed.hold[0] == 'text' and event.kind == 'drag':
            edit.extend(doc, index, *ed.hold[1:])
        elif ed.hold[0] == 'bar':
            reach = max(ed.rows.count * ed.lh - height, 0.)
            ed.scroll_to = min(max(ed.hold[2] + (y - ed.hold[1]) * ed.rows.count * ed.lh / max(height, 1.), 0.), reach)
        elif ed.hold[0] == 'widget' and event.kind == 'release' and ed.hold[1] is not None:
            if abs(x - ed.hold[2]) + abs(y - ed.hold[3]) < 3:
                edit.move(doc, ed.hold[1])
        if event.kind == 'release':
            ed.hold = ed.step = None


def typed(ed, events, desk, complete, path, source, ghost, wake, roster):
    """Run the frame's key presses and typed text in order. Returns whether
    text was typed (suggestions follow typing only)."""
    doc, menu = ed.doc, ed.suggest
    if ed.search.focused:
        # The find bar's field has the keyboard (editor/find.py).
        ed.search.typed(ed, events, desk)
        return False
    pending = ''.join(events.get('text', ()))
    wrote = False
    for key, mods in events.get('keys', ()):
        ed.step = None      # every press is its own undo step (typing folds by the framework's rule)
        if keys.printable(key, mods):
            # The character itself arrives as text; keep both in press order.
            if pending and desk.editable:
                edit.insert(doc, pending[0])
                pending, wrote = pending[1:], True
            continue
        chord = mods & keys.MODS
        if key == glfw.KEY_ESCAPE:
            if ed.search.open and not (menu.open or ed.lookup.open or ed.detail is not None):
                ed.search.close(ed)     # nothing else to close: the find bar goes
            menu.escape(ed)
            ed.lookup.drop()
            ed.detail = None
            continue
        # An open list takes the navigation keys: the suggestions, else the usage picker, else the quick fixes of the open detail.
        found = menu if menu.open else ed.lookup if ed.lookup.open else ed.fixes if ed.detail is not None and ed.fixes.open else None
        if found is not None and not chord:
            if key in (glfw.KEY_UP, glfw.KEY_DOWN):
                found.step(-1 if key == glfw.KEY_UP else 1)
                continue
            if key in (glfw.KEY_PAGE_UP, glfw.KEY_PAGE_DOWN):
                found.page(-1 if key == glfw.KEY_PAGE_UP else 1)
                continue
            if key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER, glfw.KEY_TAB) and (desk.editable or found is ed.lookup):
                found.picked = found.index
                continue
        if key == glfw.KEY_TAB and not chord and ghost and desk.editable:
            # What the caret shows of it (`pane`): all of it, its first line in a single line field.
            edit.insert(doc, ghost.split('\n')[0] if desk.single else ghost)
            continue
        if key == glfw.KEY_TAB and not chord and menu.hop(doc):
            continue
        if key == glfw.KEY_SPACE and chord == keys.CTRL and complete and desk.editable:
            ed.sync()
            menu.refresh(ed, False, explicit=True, path=path, source=source, wake=wake, roster=roster)
            continue
        if key == glfw.KEY_P and chord == keys.CTRL and complete and desk.editable and not desk.single:
            ed.sync()
            menu.hint(ed, path, wake, asked=True)
            continue
        if key == glfw.KEY_F and chord == keys.CTRL and not desk.single:
            menu.close()
            ed.lookup.drop()
            ed.detail = None
            ed.search.show(ed)
            return wrote        # (what else was pressed this frame is the field's from now on: dropped)
        if key == glfw.KEY_B and chord == keys.CTRL | keys.SHIFT and not desk.single:
            ed.lookup.outside(ed, path)
            continue
        if key == glfw.KEY_B and chord == keys.CTRL and not desk.single:
            # Go to the definition of the name at the caret, or to its uses (editor/lookup.py): answered on a later run.
            ed.sync()
            menu.close()
            ed.detail = None
            ed.lookup.ask(ed, roster)
            continue
        if key == glfw.KEY_ENTER and chord == keys.ALT and desk.editable:
            found = ed.mark('errors', doc.line_of(doc.caret), 'error')
            if found is not None and len(found.data[1]) > 1:
                ed.detail = doc.line_of(doc.caret)      # several imports could bind the name: the chooser
            elif found is not None and found.data[1]:
                suggest.fix_import(ed, found.data[1][0], path)
            continue
        if keys.press(ed, desk, key, mods):
            menu.close()
            ed.lookup.drop()
            ed.detail = None
    if pending and desk.editable:
        ed.step = None
        edit.insert(doc, pending.split('\n')[0] if desk.single else pending)
        wrote = True
    ed.step = None
    return wrote


@gui(listen=('keys', 'text', 'pointer', 'hold', 'wheel', 'scroll', 'focus'), no_save=('on_submit', 'completion_source', 'roster', 'on_open'))
def draw_editor(input_value: str = '', state: EditorState = None, editable=True, single_line=False,
                line_numbers=True, language='python', widgets=True, scopes=True, fold_defaults=True,
                first_line=1, file_tint=None, folds=None, search='', errors=None, imports=None, tints=None, live=None,
                breakpoints=None, paused=None, marks=None, path=None, complete=True,
                completion_source=None, ghost='', on_submit=None, roster=None, on_open=None, focus=False,
                width=None, height=None, cache=None, view_events=None):
    """Edit `input_value`; returns `(changed, text)`. While the text is
    still loading pass `LOADING` (state/editor_state.py): the rows the last
    session showed are painted from the state's snapshot, read only.

    A code file's value is a `Source` (model/source_model.py): it is edited
    in place and returned, so no edit copies the text, and the caller's
    models read its facts. Several editors may be on one source and code
    that is no editor may `splice` it: each session follows the rest. Read
    the text with `source.text` where all of it is needed. A `str` is for
    small fields (a search box, a message): it is joined again for every
    edit returned, which costs the whole text each time. Pass the returned
    text back to keep an edit: the view shows what its caller passes.

    What is shown besides the text comes from plain values, read again only
    when the object passed changes: `errors` [(line, message)] or an
    exception (a `marks.Findings` says which version of the text its lines
    are of; an error is a chip over its line number that opens the message),
    `imports` {line: [statement]} (quick fixes), `tints` (a
    `marks.tints` set), `live` (a live value store), `breakpoints` ({source site key:
    {'enabled': bool}}, as a file's metadata keeps them: a gutter click
    toggles the statement on its line, by key, never by line; a change
    from outside arrives as another mapping),
    `paused` (the debugger's line), `marks` ({name: `Marks`}: any further
    decorations, see `editor/marks.py`), `folds` (extra collapsible `(head,
    last)` ranges such as unchanged diff context), `search` (the term of a
    caller's own search field: its hits are washed, the first from the caret
    is revealed, `state.pane.find(1 / -1)` steps and `state.pane.search` is
    (term, index, count); Ctrl+F opens the editor's own find bar without any
    of it, see editor/find.py), `ghost` (a suggestion drawn after the caret,
    Tab accepts it).
    Lines in parameters are file lines. The buffer is the part of its file
    that starts at `first_line`; or `line_numbers` is a list with each buffer
    line's file line (None: no number, as in a diff). `file_tint` ((r, g, b))
    tints the body, the gutter numbers and the guides. `path` lets member
    and signature lookups resolve the file's own imports.
    `on_submit(text)` makes Enter submit. `focus=True` takes the keyboard.
    Ctrl+Shift+B opens the caret's line in the external editor.
    Ctrl+B goes from a use to its definition and from a definition's name
    to its uses (several: a picker). `roster` (model/roster_model.py:
    `project(path)`) makes that the project's; without one it is the text's
    own symbols. A place in another file is the caller's to show:
    `on_open(path, line, column)` (a file line; the column counts from 0).
    Without it such places are listed but only the buffer's own are jumped to.
    Give the same callable on every run (as for `on_submit`): another object,
    such as a bound method taken anew, runs the view again.
    The injected `state` gives the caller a handle on the running editor:
    `state.pane` (`editor.Pane`: jump, select, focus; geometry).
    """
    node = cache.current
    wake = lambda: cache.wake(node)
    ed = session(state, input_value, language if not single_line else None, fold_defaults, wake, path, cache.waker(node))
    doc, menu, events = ed.doc, ed.suggest, view_events
    pushed, ed.cw, ed.lh = font()
    if pushed:
        imgui.pop_font()
    # The look and the line numbering: a change of either repaints every block.
    found = look(file_tint)
    if found != ed.look:
        ed.look = found
        ed.touch()
    table = line_numbers if isinstance(line_numbers, (list, tuple)) else None
    if not ed.standin and ed.changed('numbers', (first_line, table)):
        ed.numbers = Numbers(first_line, table)
        ed.touch()
    numbers = ed.numbers
    gutter = 0. if single_line else (PAD + numbers.digits(doc.lines) * ed.cw * bool(line_numbers) + FOLD
                                     + 12 * (breakpoints is not None))
    if gutter != ed.gutter:
        ed.gutter = gutter
        ed.touch()
    # Background results first: what the source finished reading since the last run.
    ed.land()
    scroll_y = ed.pane.scroll_offset[1] if ed.pane is not None else 0.
    ed.scroll_to = None
    view = height
    editable = editable and not ed.standin
    desk = keys.Desk(editable, single_line, on_submit, imgui.set_clipboard_text, imgui.get_clipboard_text,
                     max(int(view // ed.lh) - 1, 1))
    # Decorations: each parameter that shows something is a producer of marks,
    # run again when the objects it reads are others than last time.
    ed.produce('errors', (errors, imports, numbers), problems)
    ed.produce('live', (live, numbers), values)
    ed.produce('paused', (paused, numbers), pause)
    ed.watch(breakpoints)
    ed.produce('tints', (tints,), given)
    for name, found in (marks or {}).items():
        ed.produce(name, (found,), given)
    ed.slots['break'] = ed.stop if breakpoints is not None else None
    if ed.changed('folds', (folds,)):
        ed.ranges(folds)
    if ed.changed('search', (search,)):
        ed.search.seek(ed, search)
    before = (doc.caret, doc.anchor, doc.version)
    # --- input ---
    # (Listening for the pointer is what keeps left drags from moving the window: gui_imgui_input.py.)
    for command in ed.asks:
        # What the caller asked of the handle since the last run.
        if command[0] == 'jump':
            found = ed.jump(*command[1:5])
            ed.landing = found and (*found, command[5])
        elif command[0] == 'select':
            doc.select(*command[1:])
        elif command[0] == 'focus':
            cache.imgui_input.take_keyboard(node)
        elif command[0] == 'find':
            ed.search.step(ed, command[1])
    asked = bool(ed.asks)
    ed.asks.clear()
    # What Ctrl+B asked for, once its lookup is in: a jump, the picker, or a red flash.
    asked |= ed.lookup.arrive(ed, roster, on_open)
    if ed.search.clicked is not None and ed.search.press(ed) and not ed.focused:
        cache.imgui_input.take_keyboard(node)
    for gained in events.get('focus', ()):
        ed.focused = gained
        if not gained:
            menu.close()
            ed.lookup.drop()
            ed.detail = None
        ed.tick += 1
    if focus and not ed.focused:
        cache.imgui_input.take_keyboard(node)
    for event in (*events.get('pointer', ()), *events.get('hold', ())):
        press(ed, event, desk, scroll_y, width, height, widgets and editable)
    wrote = typed(ed, events, desk, complete, path, completion_source, ghost, wake, roster) \
        if ('keys' in events or 'text' in events) else False
    for across, down in events.get('wheel', ()):
        slide(ed, ed.scroll_x - across * 40., width)
    # A new search term's first hit, once its hits are in (landed above, or read at once for a small text).
    ed.search.settle(ed)
    # A row picked in a list popover, by Enter or a click.
    if menu.picked is not None and menu.open:
        ed.step = None
        menu.index = min(menu.picked, len(menu.rows) - 1)
        menu.accept(ed, path)
    elif ed.fixes.picked is not None and ed.detail is not None and ed.fixes.open:
        ed.step = None
        suggest.fix_import(ed, ed.fixes.rows[min(ed.fixes.picked, len(ed.fixes.rows) - 1)][0], path)
        ed.detail = None
    elif ed.lookup.picked is not None and ed.lookup.open:
        asked |= ed.lookup.take(ed, on_open)
    menu.picked = ed.fixes.picked = ed.lookup.picked = None
    # --- follow the edits ---
    edited = ed.sync()
    if edited or doc.version != before[2]:
        if complete and editable and not single_line:
            menu.refresh(ed, wrote, path=path, source=completion_source, wake=wake, roster=roster)
            # Editing inside a call's parentheses arms its hint. An open list hides it: the call is
            # looked for (a scan back from the caret) once the list closes.
            if menu.open:
                menu.owed = True
            else:
                menu.hint(ed, path, wake, edited=True)
    elif menu.pending is not None and menu.pending.done():
        if menu.arrived():
            menu.refresh(ed, True, path=path, source=completion_source, wake=wake, roster=roster)
        menu.hint(ed, path, wake)
    if ed.landing is not None:
        # An undo or redo changed text that may be out of sight: unfold it, bring it into view, flash it.
        ed.scroll_to = ed.reveal(*ed.landing[:2], scroll_y, view, ed.landing[2])
        sideways(ed, *ed.landing[:2], width)
        ed.landing = None
        threading.Timer(FLASH, wake).start()
    elif ed.flash and time.monotonic() >= ed.flash:
        ed.flash = 0.
        ed.put('flash', None)
    moved = (doc.caret, doc.anchor) != before[:2]
    if moved or edited:
        ed.show(doc.caret)
        low, high = doc.selection
        old_low, old_high = ed.selection
        if (low != high or old_low != old_high) and (low, high) != (old_low, old_high):
            ed.touch_lines(doc.line_of(min(low, old_low, doc.size)), doc.line_of(min(max(high, old_high), doc.size)))
        ed.selection = (low, high)
        ed.lookup.close()       # (a lookup under way stays: its answer is for the caret it was asked at)
        if not edited:
            menu.leave(ed)
            if '\n' in ghost:
                ed.tick += 1        # the ghost's further lines are placed by the pane: it runs again
        if not edited and not menu.explicit:
            menu.close()
            menu.hint(ed, path, wake) if complete and editable and not single_line else None
        if not ed.standin:
            state.caret, state.anchor = doc.caret, doc.anchor
    if moved or edited or 'focus' in events:
        ed.enter()
    if not ed.standin:
        # Each fold layer's collapsed set, for the next session. (A snapshot has no folds: the saved ones stay.)
        if ed.saved != ed.rows.stamp:
            ed.saved, collapsed = ed.rows.stamp, ed.rows.collapsed()
            if collapsed != state.collapsed:
                state.collapsed = collapsed
        gaps = ed.rows.closed(1)
        if gaps != state.gaps:
            state.gaps = gaps
    # --- scroll: keep the caret in view, follow the bar ---
    x, y = ed.where(doc.caret)
    reach = max(ed.rows.count * ed.lh - view, 0.)
    target = scroll_y if ed.scroll_to is None else ed.scroll_to
    # Only the user's own caret movement (or the caller's jump) scrolls: a widget or quick fix edit elsewhere does not.
    if (moved or edited) and ('keys' in events or 'text' in events or asked or (ed.hold is not None and ed.hold[0] == 'text')):
        target = min(target, y)
        target = max(target, y + ed.lh - view)
        text_width = width - gutter - BAR - ed.cw
        scroll_x = min(ed.scroll_x, x - gutter)
        scroll_x = max(scroll_x, x - gutter - text_width, 0.)
        if scroll_x != ed.scroll_x:
            ed.scroll_x = scroll_x
            ed.touch()
    target = float(int(min(max(target, 0.), reach)))
    if ed.pane is not None and target != scroll_y:
        ed.pane.scroll_offset = (0., target)
        scroll_y = target
    ed.window(scroll_y, view)
    # The pointer is the editor's over its rows; the empty space below the last one is the window's (a left drag moves it).
    below = min(ed.rows.count * ed.lh - scroll_y, height)
    cache.imgui_input.reach(node, None if single_line else (0., 0., width, below))
    if ed.caret is not None:
        ed.caret.left, ed.caret.top = x - ed.scroll_x, y
    if ed.band is not None:
        ed.band.top = y
    # --- views ---
    if ed.detail is not None and ed.mark('errors', ed.detail, 'error') is None:
        ed.detail = None        # its error went: the detail closes
    dl = imgui.get_window_draw_list()
    if ed.look.back is not None:
        dl.add_rect_filled(0, 0, width, height, ed.look.back)
    shown = ghost if editable and not menu.open else ''
    changed, _, ed.pane = pane(ed, name='pane', stamp=ed.tick, left=0., top=0., width=width, height=height,
                               widgets=widgets and editable, numbers=bool(line_numbers), scopes=scopes,
                               focused=ed.focused and not ed.search.focused, ghost=shown, single=single_line, return_extras=True)
    if reach and not single_line:
        size = max(view * view / (reach + view), 24.)
        top = (view - size) * scroll_y / reach
        dl.add_rect_filled(width - BAR + 2, top, width - 2, top + size, ed.look.thumb, 3.)
    if gutter and below > 0:
        margin(None, name='margin', left=0., top=0., width=gutter, height=below)
    popovers(ed, x - ed.scroll_x, y - scroll_y, width, height)
    if ed.search.open:
        bar(ed, width, height)
    if changed:
        # An inline widget edited the text: run again to follow it.
        cache.invalidate_id(node)
    if moved or edited:
        ed.rest()
    if ed.standin:
        return False, input_value
    if ed.tick != ed.kept:
        # What is shown changed: keep the viewport snapshot for the next session's first frame.
        ed.kept = ed.tick
        state.view_first, state.view_rows, state.view_text, state.view_numbers = ed.snapshot()
    done = doc.version != ed.returned
    ed.returned = doc.version
    if ed.given is ed.source:
        return done, ed.given
    ed.gave = doc.text
    return done, ed.gave


def popovers(ed, x, y, width, height):
    """Declare the open popovers at the caret: suggestions, the usage picker,
    the signature hint, an error's detail with its quick fixes."""
    menu, cw, lh = ed.suggest, ed.cw, ed.lh
    below = y + lh
    if menu.open:
        pop(ed, 'suggestions', 'suggest', x, y, height)
    elif ed.lookup.open:
        pop(ed, 'usages', 'lookup', x, y, height)
    elif menu.signature is not None and ed.detail is None:
        name, parameters, active = menu.signature
        label = f'{name}(' + ', '.join(f'[{item}]' if at == active else item for at, item in enumerate(parameters)) + ')'
        note(ed, name='signature', melty_window=True, content_offset=(0., 0.), lines=(label,),
             left=x, top=y - lh - 4 if y > lh + 4 else below, width=(len(label) + 2) * cw, height=lh)
    found = ed.mark('errors', ed.detail, 'error') if ed.detail is not None else None
    if found is not None:
        # A little window beside the chip, above the error's line (below it when there is no room): the
        # message, then its quick fixes. Levers: `DETAIL` is its padding and its gap to the line.
        message, fixes = found.data
        lines = message.split('\n')
        if [text for text, _ in ed.fixes.rows] != fixes:
            ed.fixes.set([(statement, 'import') for statement in fixes])
        wide = (max(len(item) for item in lines) + 2) * cw
        tall, list_tall = len(lines) * lh + DETAIL, min(len(fixes), ROWS) * lh
        left = ed.gutter - FOLD + DETAIL if roomy(ed) else ed.gutter      # (the session has a row's `gutter` and `cw`)
        line_y = ed.rows.row(ed.detail) * lh - (ed.pane.scroll_offset[1] if ed.pane is not None else 0.)
        top = line_y - tall - list_tall - 2
        if top < 0:
            top = line_y + lh + 2
        note(ed, name='detail', melty_window=True, content_offset=(0., 0.), lines=tuple(lines), color=ed.look.error,
             frame=ed.look.chip, left=left, top=top, width=wide, height=tall)
        if fixes:
            pop(ed, 'fixes', 'fixes', left, 0., height, at=top + tall)


# --- built in inline widgets ----------------------------------------------
def flip(text):
    return 'False' if text == 'True' else 'True'


@inline('bool', caret=True, double=flip)
def switch(input_value: str, width=0., height=0.):
    """True / False: a frame with a dot on the side that is on. A double click flips it (the editor
    does, `press`: the first click only puts the caret in the word, as in any text)."""
    dl = imgui.get_window_draw_list()
    imgui.invisible_button('##flip', width, height)
    dl.add_rect(0, 1, width, height - 1, pack_color(.80, .47, .20, .9 if imgui.is_item_hovered() else .45), 4.)
    dl.add_circle_filled(width - 4 if input_value == 'True' else 4, height - 3, 2., pack_color(.80, .47, .20))
    return False, input_value


@inline('number', caret=True)
def drag(input_value: str, width=0., height=0., origin=None, draw_state=None):
    """A numeric literal: drag sideways to change it, click to edit its digits."""
    kind, value, back, _ = syntax.tools()._parse_number_token(input_value)
    imgui.invisible_button('##drag', width, height)
    hovered, active = imgui.is_item_hovered(), imgui.is_item_active()
    if hovered or active:
        imgui.get_window_draw_list().add_line(0, height - 2, width, height - 2, pack_color(.41, .59, .73), 1.5)
    if kind is None or not active:
        if origin is not None:
            draw_state.origin = None
        return False, input_value
    travel = imgui.get_mouse_drag_delta(0, 0.).x
    if origin is None:
        if abs(travel) < 3:
            return False, input_value
        draw_state.origin = origin = value
    step = 1 if kind == 'int' else max(abs(origin), 1.) * .01
    moved = back(type(origin)(origin + int(travel / 4) * step))
    return moved != input_value, moved


def _swatch(input_value, width, height, rgb, write, color, draw_state):
    """Shared colour widget body: a swatch in the colour, in its own cells
    left of the literal; a click opens a picker."""
    dl = imgui.get_window_draw_list()
    if imgui.invisible_button('##colour', width, height):
        # A state write does not run the view again: carry on with the new value.
        draw_state.color = color = None if color else list(rgb)
    dl.add_rect_filled(1, 3, width - 3, height - 3, pack_color(*rgb[:3]), 3.)
    if not color:
        return False, input_value
    edited, picked = picker(color, name='picker', melty_window=True, content_offset=(0., 0.),
                            left=0., top=height + 2, height=len(color) * 24 + 12)
    if edited:
        draw_state.color = list(picked)
        return True, write(picked)
    return False, input_value


@gui(width=230, height=110)
def picker(input_value, width=0., height=0.):
    """Channel sliders over a preview of the colour."""
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled(0, 0, width, height, look().pop, 4.)
    dl.add_rect_filled(width - 34, 6, width - 6, height - 6, pack_color(*input_value[:3]), 3.)
    value, changed = list(input_value), False
    imgui.push_item_width(width - 70)
    for at, label in enumerate('rgba'[:len(value)]):
        imgui.set_cursor_pos((6, 6 + at * 24))
        moved, value[at] = imgui.slider_float(label, value[at], 0., 1.)
        changed |= moved
    imgui.pop_item_width()
    return changed, value


@inline('color3', lead=2)
def colour_tuple(input_value: str, width=0., height=0., color=None, draw_state=None):
    """An `(r, g, b[, a])` tuple of floats."""
    try:
        rgb = [float(part) for part in input_value.strip('()').split(',') if part.strip()]
    except ValueError:
        return False, input_value
    if len(rgb) not in (3, 4):
        return False, input_value
    return _swatch(input_value, width, height, rgb,
                   lambda picked: '(' + ', '.join(f'{part:.3g}' if part % 1 else f'{part:.1f}' for part in picked) + ')',
                   color, draw_state)


@inline('colorhex', lead=2)
def colour_hex(input_value: str, width=0., height=0., color=None, draw_state=None):
    """A `'#rrggbb'` string."""
    rgb = syntax.tools()._parse_hex_color(input_value)
    if rgb is None:
        return False, input_value
    quote = input_value[0]
    return _swatch(input_value, width, height, rgb,
                   lambda picked: quote + '#' + ''.join(f'{int(part * 255 + .5):02x}' for part in picked) + quote,
                   color, draw_state)


# The icon picker registers itself for 'icon' tokens (it reads this module's look and font, so it is imported last).
from meltygui.view import icon_picker_view  # noqa: E402,F401
