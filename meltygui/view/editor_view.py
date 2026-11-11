"""`draw_editor`: the code and text editor as native `@gui` views.

    changed, text = draw_editor(text, name='source', width=800, height=600)

Structure, outermost first:

- `draw_editor` owns the keyboard and pointer. It turns events into edit
  commands (`editor/keys.py`, `editor/edit.py`), keeps the session in step
  (`Editor.sync`) and places the caret, scroll bar and popovers.
- `pane` is the scrolling viewport. It declares one `block` per `BLOCK` rows.
- `block` paints its rows and declares the inline widgets on them. A block
  runs again only when its stamp changes, so a keystroke repaints one block
  and scrolling repaints none.

To change how a row looks, edit `paint`. To add an inline widget, decorate a
function with `@inline(kind)` (see the built in ones at the end of this file).
"""
import meltygui_imgui as imgui

from meltygui.core.rendering.gui_prototype import gui
from meltygui.editor import edit, keys, marks, suggest, syntax
from meltygui.editor.editor import Editor, BLOCK
from meltygui.editor.inline import inline, for_token, for_value
from meltygui.hdr_color import pack_color
from meltygui.state.editor_state import EditorState
import meltygui.core.windowing.window_constants as glfw

# --- look: change colours and sizes here ----------------------------------
LINE = 1.25                      # line height over the font's
BAR = 10.                        # scroll bar width
FOLD = 16.                       # gutter strip holding the fold chevrons
PAD = 6.                         # space left of the line numbers
ROWS = 10                        # suggestion rows shown at once
TINT = .055                      # strength of a definition's tint wash
BACK = pack_color(.118, .122, .133)
GUTTER = pack_color(.137, .141, .153)
NUMBER = pack_color(.42, .44, .47)
PLAIN = pack_color(.66, .72, .78)
CARET = pack_color(.93, .93, .93)
BAND = pack_color(1., 1., 1., .045)
SELECT = pack_color(.21, .39, .64, .75)
HIT = pack_color(.75, .60, .15, .45)
GUIDE = pack_color(1., 1., 1., .07)
SCOPE = pack_color(1., 1., 1., .014)
FOLDED = pack_color(.55, .60, .68)
ERROR = pack_color(.94, .33, .31)
BREAK = pack_color(.86, .24, .24)
PAUSE = pack_color(.98, .80, .25)
PAUSED = pack_color(.98, .80, .25, .16)
VALUE = pack_color(.45, .78, .66)
PILL = pack_color(.45, .78, .66, .13)
GHOST = pack_color(.66, .70, .78, .5)
THUMB = pack_color(1., 1., 1., .18)
POP = pack_color(.17, .18, .20)
PICK = pack_color(.21, .39, .64)
DIM = pack_color(.50, .53, .57)


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


def wash(rgb, alpha):
    return pack_color(rgb[0], rgb[1], rgb[2], alpha)


# --- painting -------------------------------------------------------------
def paint(ed, index, width, numbers, show_scopes):
    """Draw the rows of block `index` in block-local coordinates. Returns the
    widget sites [(row, line, column, text, entry)] and live value sites."""
    dl = imgui.get_window_draw_list()
    doc, rows, folds, colors = ed.doc, ed.rows, ed.folds, palette()
    cw, lh, gutter = ed.cw, ed.lh, ed.gutter
    first = index * BLOCK
    lines = rows.span(first, first + BLOCK)
    if not lines:
        return (), ()
    head, tail = lines[0], lines[-1]
    left = gutter - ed.scroll_x
    low, high = doc.selection
    # Columns on screen: long lines only submit this band.
    band0 = int(ed.scroll_x // cw)
    band1 = band0 + int((width - gutter) // cw) + 2
    dl.push_clip_rect(gutter, 0, width, len(lines) * lh, True)
    if show_scopes:
        # A tinted definition is the scope with the same last line and indent.
        tinted = {(item[1], item[2]): item[3] for item in ed.tinted} if ed.tinted else ()
        for start, last, indent, depth, scope in ed.scopes:
            if start > tail:
                break
            if last < head or rows.hidden(start):
                continue
            top = (rows.row(max(start, head)) - first) * lh
            bottom = (rows.row(min(last, tail)) - first + 1) * lh
            x = left + indent * cw
            if scope:
                rgb = tinted.get((last, indent)) if tinted else None
                dl.add_rect_filled(x, top, width, bottom, wash(rgb, TINT) if rgb else SCOPE)
            if start >= head:
                top += lh
            if bottom > top:
                dl.add_line(x + .5, top, x + .5, bottom, GUIDE, 1.)
    sites, values = [], []
    hits = ed.hits_in(doc.start(head), doc.end(tail)) if ed.hits else ()
    paused = ed.sources.get('paused')
    for at, line in enumerate(lines):
        y = at * lh
        start, stop = doc.start(line), doc.end(line)
        text = doc.text[start:stop]
        if paused is not None and paused - 1 == line:
            dl.add_rect_filled(gutter, y, width, y + lh, PAUSED)
        for column, length, rgb in ed.washes.get(line, ()):
            dl.add_rect_filled(left + column * cw, y, left + (column + length) * cw, y + lh, wash(rgb, TINT * 2.5), 2.)
        if low < high and low <= stop and high > start:
            dl.add_rect_filled(left + (max(low, start) - start) * cw, y,
                               left + (min(high, stop) - start + (high > stop)) * cw, y + lh, SELECT)
        for hit0, hit1 in hits:
            if hit0 < stop and hit1 > start:
                dl.add_rect_filled(left + (max(hit0, start) - start) * cw, y + 1,
                                   left + (min(hit1, stop) - start) * cw, y + lh - 1, HIT, 2.)
        ty = y + (lh - lh / LINE) * .5
        for column, piece, kind in ed.syntax.runs(line, text):
            if column >= band1:
                break
            if column + len(piece) <= band0:
                continue
            if len(piece) > band1 - band0:
                cut = max(band0 - column, 0)
                piece, column = piece[cut:band1 - column], column + cut
            if piece.isascii():
                dl.add_text(left + column * cw, ty, colors.get(kind, PLAIN), piece)
            else:
                # Glyphs outside the mono face each take their own cell.
                color = colors.get(kind, PLAIN)
                for offset, char in enumerate(piece):
                    dl.add_text(left + (column + offset) * cw, ty, color, char)
            entry = for_token(kind)
            if entry is not None:
                sites.append((at, line, column, piece, entry))
        x = left + (len(text) + 1) * cw
        hidden = rows.folded(line)
        if hidden:
            label = f' {hidden} lines '
            dl.add_rect(x, y + 2, x + len(label) * cw, y + lh - 2, FOLDED, 3.)
            dl.add_text(x, ty, FOLDED, label)
            x += (len(label) + 1) * cw
        for value in ed.live.get(line, ()):
            entry = for_value(value.value)
            if entry is not None:
                values.append((at, line, x, value, entry))
                x += (entry.cells + 1) * cw
                continue
            label = f' {value.name} = {value.text if value.text is not None else type(value.value).__name__} '
            dl.add_rect_filled(x, y + 2, x + len(label) * cw, y + lh - 2, PILL, 3.)
            dl.add_text(x, ty, VALUE, label)
            x += (len(label) + 1) * cw
        message = ed.errors.get(line)
        if message is not None:
            dl.add_line(left + (len(text) - len(text.lstrip())) * cw, y + lh - 2, left + max(len(text), 1) * cw,
                        y + lh - 2, ERROR, 1.5)
            fix = ed.fixes.get(line)
            dl.add_text(x, ty, ERROR, message.split('\n')[0][:120] + (f'   fix: {fix[0]}' if fix else ''))
    dl.pop_clip_rect()
    if gutter:
        dl.add_rect_filled(0, 0, gutter, len(lines) * lh, GUTTER)
        breaks = ed.sources.get('breakpoints')
        for at, line in enumerate(lines):
            y = at * lh
            ty = y + (lh - lh / LINE) * .5
            mid = y + lh * .5
            if breaks is not None and line + 1 in breaks:
                dl.add_circle_filled(PAD + 4, mid, lh * .28, BREAK)
            if paused is not None and paused - 1 == line:
                dl.add_triangle_filled(PAD, mid - 5, PAD + 9, mid, PAD, mid + 5, PAUSE)
            if numbers:
                label = str(line + 1)
                dl.add_text(gutter - FOLD - len(label) * cw, ty, ERROR if line in ed.errors else NUMBER, label)
            item = folds.at(line)
            if item is not None:
                x = gutter - FOLD * .5 - 2
                if rows.folded(line):
                    dl.add_triangle_filled(x - 3, mid - 5, x + 4, mid, x - 3, mid + 5, FOLDED)
                else:
                    dl.add_triangle_filled(x - 5, mid - 3, x + 5, mid - 3, x, mid + 4, NUMBER)
    return sites, values


@gui(listen=())
def block(input_value, index=0, stamp=0, width=0., height=0., widgets=True, numbers=True, scopes=True):
    """`BLOCK` rows of text and the inline widgets on them. `stamp` only
    exists to run the body again when the rows changed."""
    ed = input_value
    pushed, _, _ = font()
    try:
        sites, values = paint(ed, index, width, numbers, scopes)
    finally:
        if pushed:
            imgui.pop_font()
    changed = False
    cw, lh, left = ed.cw, ed.lh, ed.gutter - ed.scroll_x
    if widgets:
        for at, line, column, text, entry in sites:
            x = left + column * cw
            if x < ed.gutter or x + len(text) * cw > width:
                continue
            edited, value = entry.view(text, key=(at, column), left=x, top=at * lh, width=len(text) * cw, height=lh)
            if edited and isinstance(value, str) and value != text:
                edit.set_token(ed.doc, line, column, len(text), value)
                changed = True
    for at, line, x, value, entry in values:
        if x >= ed.gutter and x + entry.cells * cw <= width:
            entry.view(value.value, key=('live', at, value.name), left=x, top=at * lh, width=entry.cells * cw, height=lh)
    return changed, ed


@gui(listen=())
def band(input_value):
    """The current line highlight, moved by its `top`."""
    imgui.get_window_draw_list().add_rect_filled(0, 0, 8192, 8192, BAND)


@gui(listen=())
def caret(input_value, shown=True, ghost='', width=0., height=0.):
    """The caret bar and the ghost suggestion after it, moved by `left`/`top`."""
    dl = imgui.get_window_draw_list()
    if shown:
        dl.add_rect_filled(0, 1, 2, height - 1, CARET)
    if ghost:
        pushed, _, lh = font()
        dl.add_text(2, (lh - lh / LINE) * .5, GHOST, ghost.split('\n')[0])
        if pushed:
            imgui.pop_font()


@gui(scroll=True, listen=())
def pane(input_value, stamp=0, width=0., height=0., widgets=True, numbers=True, scopes=True,
         focused=False, ghost='', single=False):
    """The scrolling viewport: one block per `BLOCK` rows, in content coordinates."""
    ed = input_value
    lh, blocks = ed.lh, ed.stamps
    span = BLOCK * lh
    x, y = ed.where(ed.doc.caret)
    if not single:
        _, _, ed.band = band(None, name='band', width=width, height=lh, initial={'left': 0., 'top': y},
                             return_extras=True)
    changed = False
    last = len(blocks) - 1
    low, high = ed.window
    drawn = ed.drawn
    for index, mark in enumerate(blocks):
        if low <= index <= high:
            drawn[index] = mark
        elif drawn[index] >= 0:
            # Out of sight: keep its old picture until it scrolls near.
            mark = drawn[index]
        rows = BLOCK if index < last else ed.rows.count - index * BLOCK
        edited, _ = block(ed, key=index, index=index, stamp=mark, left=0., top=index * span,
                          width=width, height=rows * lh, widgets=widgets, numbers=numbers, scopes=scopes)
        changed |= edited
    _, _, ed.caret = caret(None, name='caret', shown=focused, ghost=ghost, width=max(2., len(ghost) * ed.cw + 4),
                           height=lh, initial={'left': x - ed.scroll_x, 'top': y}, return_extras=True)
    return changed, ed


# --- popovers -------------------------------------------------------------
@gui(listen=('pointer',))
def choices(input_value, rows=(), index=0, first=0, width=0., height=0., view_events=None):
    """A list popover: suggestions or quick fixes. A click picks a row by
    setting `ed.picked`; the editor reads it on its next run."""
    ed = input_value
    dl = imgui.get_window_draw_list()
    pushed, cw, lh = font()
    dl.add_rect_filled(0, 0, width, height, POP, 4.)
    picked = False
    for event in view_events.get('pointer', ()):
        if event.kind == 'press':
            ed.picked = first + int(event.y // lh)
            picked = True
    for at, (name, kind) in enumerate(rows[first:first + ROWS]):
        y = at * lh
        if first + at == index:
            dl.add_rect_filled(0, y, width, y + lh, PICK, 3.)
        dl.add_text(6, y + (lh - lh / LINE) * .5, CARET, name)
        dl.add_text(width - (len(kind) + 1) * cw, y + (lh - lh / LINE) * .5, DIM, kind)
    if pushed:
        imgui.pop_font()
    return picked, ed


@gui(listen=('pointer',))
def note(input_value, lines=(), width=0., height=0., color=CARET, view_events=None):
    """A text popover: the full error message or a signature hint."""
    dl = imgui.get_window_draw_list()
    pushed, cw, lh = font()
    dl.add_rect_filled(0, 0, width, height, POP, 4.)
    for at, line in enumerate(lines):
        dl.add_text(6, at * lh + (lh - lh / LINE) * .5, color if at == 0 else DIM, line)
    if pushed:
        imgui.pop_font()


# --- the editor -----------------------------------------------------------
def session(state, text, language, fold_defaults, wake):
    """The view's `Editor`, created on first use and restored from `state`."""
    ed = state.editor
    if ed is None:
        ed = state.editor = Editor(text, language)
        ed.fold_defaults = fold_defaults
        ed.folds.collapsed = {tuple(key) for key in state.collapsed}
        ed.folds.seeded = bool(state.collapsed)
        ed.doc.select(state.caret, state.anchor)
        if language == 'python':
            syntax.warm(wake)
    elif text is not ed.given and text != ed.given and text != ed.doc.text:
        ed.load(text)
        ed.returned = ed.doc.version
    ed.given = text
    return ed


def remark(ed, name, value, build):
    """Rebuild one kind of marks when its parameter object changed."""
    if ed.sources.get(name, ed) is value:
        return False
    ed.sources[name] = value
    build(value)
    ed.touch(0)
    return True


def press(ed, event, desk, scroll_y, width, height, widgets):
    """A pointer press, drag or release in view coordinates."""
    doc = ed.doc
    x, y = event.x, event.y
    line, column = ed.spot(x, y, scroll_y)
    index = doc.index(line, column)
    if event.kind == 'press':
        ed.hold = ed.detail = None
        ed.suggest.close()
        if x >= width - BAR and not desk.single:
            ed.hold = ('bar', y, scroll_y)
        elif x < ed.gutter:
            if x >= ed.gutter - FOLD and ed.folds.at(line) is not None:
                ed.toggle(line)
            elif ed.sources.get('breakpoints') is not None:
                ed.sources['breakpoints'].toggle(line + 1)
                ed.touch_lines(line, line)
            else:
                edit.select_line(doc, index)
        else:
            text = doc.line(line)
            if column > len(text):
                if ed.rows.folded(line):
                    return ed.toggle(line)
                if line in ed.errors:
                    ed.detail = line
                    return
            if widgets and column < len(text):
                for start, piece, kind in ed.syntax.runs(line, text):
                    if start <= column < start + len(piece):
                        entry = for_token(kind)
                        if entry is not None:
                            ed.hold = ('widget', index if entry.caret else None, x, y)
                            return
                        break
            if event.clicks == 2:
                edit.select_word(doc, index)
            elif event.clicks == 3:
                edit.select_line(doc, index)
            else:
                edit.move(doc, index, select=event.shift)
            ed.hold = ('text',)
    elif ed.hold is not None:
        if ed.hold[0] == 'text' and event.kind == 'drag':
            edit.move(doc, index, select=True)
        elif ed.hold[0] == 'bar':
            reach = max(ed.rows.count * ed.lh - height, 0.)
            ed.scroll_to = min(max(ed.hold[2] + (y - ed.hold[1]) * ed.rows.count * ed.lh / max(height, 1.), 0.), reach)
        elif ed.hold[0] == 'widget' and event.kind == 'release' and ed.hold[1] is not None:
            if abs(x - ed.hold[2]) + abs(y - ed.hold[3]) < 3:
                edit.move(doc, ed.hold[1])
        if event.kind == 'release':
            ed.hold = None


def typed(ed, events, desk, complete, tree, path, source, ghost, wake):
    """Run the frame's key presses and typed text in order. Returns whether
    text was typed (suggestions follow typing only)."""
    doc, menu = ed.doc, ed.suggest
    pending = ''.join(events.get('text', ()))
    wrote = False
    for key, mods in events.get('keys', ()):
        if keys.printable(key, mods):
            # The character itself arrives as text; keep both in press order.
            if pending and desk.editable:
                edit.insert(doc, pending[0], merge=True)
                pending, wrote = pending[1:], True
            continue
        chord = mods & keys.MODS
        if key == glfw.KEY_ESCAPE:
            menu.close()
            ed.detail = menu.signature = None
            continue
        if menu.open and not chord:
            if key in (glfw.KEY_UP, glfw.KEY_DOWN):
                menu.step(-1 if key == glfw.KEY_UP else 1)
                continue
            if key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER, glfw.KEY_TAB):
                menu.accept(ed, path)
                continue
        if key == glfw.KEY_TAB and not chord and ghost and desk.editable:
            edit.insert(doc, ghost)
            continue
        if key == glfw.KEY_SPACE and chord == keys.CTRL and complete and desk.editable:
            ed.sync()
            menu.refresh(ed, False, explicit=True, tree=tree, path=path, source=source, wake=wake)
            continue
        if key == glfw.KEY_ENTER and chord == keys.ALT and desk.editable:
            fix = ed.fixes.get(doc.line_of(doc.caret))
            if fix:
                suggest.fix_import(ed, fix[0], path)
            continue
        if keys.press(ed, desk, key, mods):
            menu.close()
            ed.detail = None
    if pending and desk.editable:
        edit.insert(doc, pending.split('\n')[0] if desk.single else pending, merge=True)
        wrote = True
    return wrote


@gui(listen=('keys', 'text', 'pointer', 'wheel', 'scroll', 'focus'), no_save=('on_submit', 'completion_source'))
def draw_editor(input_value: str = '', state: EditorState = None, editable=True, single_line=False,
                line_numbers=True, language='python', widgets=True, scopes=True, fold_defaults=True,
                folds=None, search='', errors=None, imports=None, tints=None, live=None, live_offset=0,
                breakpoints=None, paused=None, code_tree=None, path=None, complete=True,
                completion_source=None, ghost='', on_submit=None, focus=False,
                width=None, height=None, cache=None, view_events=None):
    """Edit `input_value`; returns `(changed, text)`.

    Marks are plain values, rebuilt only when the object passed changes:
    `errors` [(line, message)] or an exception, `imports` {line: [statement]}
    (quick fixes), `tints` (a `collect_def_tints` result), `live` (a live
    value store), `breakpoints` (a `Breakpoints` set, edited in place by
    gutter clicks), `paused` (the debugger's line), `folds` (extra collapsible
    `(head, last)` ranges such as unchanged diff context), `search` (a term to
    highlight), `ghost` (a suggestion drawn after the caret, Tab accepts it).
    Lines in parameters are 1 based file lines. `code_tree` (a parse from
    `cst_module_to_dict`) and `path` improve suggestions. `on_submit(text)`
    makes Enter submit. `focus=True` takes the keyboard.
    """
    node = cache.current
    wake = lambda: cache.wake(node)
    ed = session(state, input_value, language if not single_line else None, fold_defaults, wake)
    doc, menu, events = ed.doc, ed.suggest, view_events
    pushed, ed.cw, ed.lh = font()
    if pushed:
        imgui.pop_font()
    digits = len(str(doc.lines)) if line_numbers and not single_line else 0
    gutter = 0. if single_line else PAD + max(digits, 3) * ed.cw * bool(line_numbers) + FOLD + 12 * (breakpoints is not None)
    if gutter != ed.gutter:
        ed.gutter = gutter
        ed.touch(0)
    scroll_y = ed.pane.scroll_offset[1] if ed.pane is not None else 0.
    ed.scroll_to = None
    view = height
    desk = keys.Desk(editable, single_line, on_submit, imgui.set_clipboard_text, imgui.get_clipboard_text,
                     max(int(view // ed.lh) - 1, 1))
    # Marks follow their parameter objects.
    remark(ed, 'errors', errors, lambda value: setattr(ed, 'errors', marks.errors(value)))
    remark(ed, 'imports', imports, lambda value: setattr(ed, 'fixes', marks.fixes(value)))
    remark(ed, 'live', live, lambda value: setattr(ed, 'live', marks.live(value, live_offset)))
    remark(ed, 'paused', paused, lambda value: None)
    remark(ed, 'breakpoints', breakpoints, lambda value: None)
    if remark(ed, 'folds', folds, lambda value: setattr(ed, 'extra', tuple(value or ()))):
        ed.scanned = False
    if ed.sources.get('tints', ed) is not tints or ed.sources.get('tint_version') != doc.version:
        ed.sources['tint_version'] = doc.version
        if remark(ed, 'tints', tints, lambda value: None) or tints:
            ed.tinted, ed.washes = marks.tints(tints, doc)
    before = (doc.caret, doc.anchor, doc.version)
    # --- input ---
    for gained in events.get('focus', ()):
        ed.focused = gained
        if not gained:
            menu.close()
            ed.detail = None
        ed.tick += 1
    if focus and not ed.focused:
        cache.imgui_input.take_keyboard(node)
    for event in events.get('pointer', ()):
        press(ed, event, desk, scroll_y, width, height, widgets and editable)
    wrote = typed(ed, events, desk, complete, code_tree, path, completion_source, ghost, wake) \
        if ('keys' in events or 'text' in events) else False
    for across, down in events.get('wheel', ()):
        ed.scroll_x = max(ed.scroll_x - across * 40., 0.)
        ed.touch(0)
    if ed.picked is not None:
        # A popover row was clicked.
        if ed.detail is not None and ed.fixes.get(ed.detail):
            suggest.fix_import(ed, ed.fixes[ed.detail][min(ed.picked, len(ed.fixes[ed.detail]) - 1)], path)
            ed.detail = None
        elif menu.open:
            menu.index = min(ed.picked, len(menu.rows) - 1)
            menu.accept(ed, path)
        ed.picked = None
    # --- follow the edits ---
    for change in doc.changes:
        delta = change.added - change.removed
        if delta:
            # Marks and breakpoints follow their lines until fresh ones arrive.
            ed.live, ed.errors, ed.fixes = (marks.moved(found, change.line, delta)
                                            for found in (ed.live, ed.errors, ed.fixes))
            if breakpoints is not None:
                breakpoints.moved(change.line + 1, delta)
    edited = ed.sync()
    if edited or doc.version != before[2]:
        if complete and editable and not single_line:
            menu.refresh(ed, wrote, tree=code_tree, path=path, source=completion_source, wake=wake)
            if not menu.open:
                menu.hint(ed, path, wake)
    elif menu.pending is not None and menu.pending.done():
        menu.pending = None
        menu.refresh(ed, True, tree=code_tree, path=path, source=completion_source, wake=wake)
        menu.hint(ed, path, wake)
    if not remark(ed, 'search', search, ed.find) and search and ed.sources.get('searched') != doc.version:
        ed.find(search)
    ed.sources['searched'] = doc.version
    moved = (doc.caret, doc.anchor) != before[:2]
    if moved or edited:
        ed.show(doc.caret)
        low, high = doc.selection
        old_low, old_high = ed.selection
        if (low != high or old_low != old_high) and (low, high) != (old_low, old_high):
            ed.touch_lines(doc.line_of(min(low, old_low, len(doc.text))), doc.line_of(min(max(high, old_high), len(doc.text))))
        ed.selection = (low, high)
        if not edited and not menu.explicit:
            menu.close()
            menu.hint(ed, path, wake) if complete and editable and not single_line else None
        state.caret, state.anchor = doc.caret, doc.anchor
    collapsed = sorted(ed.folds.collapsed, key=repr)
    if collapsed != state.collapsed:
        state.collapsed = collapsed
    # --- scroll: keep the caret in view, follow the bar ---
    x, y = ed.where(doc.caret)
    reach = max(ed.rows.count * ed.lh - view, 0.)
    target = scroll_y if ed.scroll_to is None else ed.scroll_to
    # Only the user's own caret movement scrolls: a widget or quick fix edit elsewhere does not.
    if (moved or edited) and ('keys' in events or 'text' in events or (ed.hold is not None and ed.hold[0] == 'text')):
        target = min(target, y)
        target = max(target, y + ed.lh - view)
        text_width = width - gutter - BAR - ed.cw
        scroll_x = min(ed.scroll_x, x - gutter)
        scroll_x = max(scroll_x, x - gutter - text_width, 0.)
        if scroll_x != ed.scroll_x:
            ed.scroll_x = scroll_x
            ed.touch(0)
    target = float(int(min(max(target, 0.), reach)))
    if ed.pane is not None and target != scroll_y:
        ed.pane.scroll_offset = (0., target)
        scroll_y = target
    ed.look(scroll_y, view)
    if ed.caret is not None:
        ed.caret.left, ed.caret.top = x - ed.scroll_x, y
    if ed.band is not None:
        ed.band.top = y
    # --- views ---
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled(0, 0, width, height, BACK)
    shown = ghost if editable and not menu.open else ''
    changed, _, ed.pane = pane(ed, name='pane', stamp=ed.tick, left=0., top=0., width=width, height=height,
                               widgets=widgets and editable, numbers=bool(line_numbers), scopes=scopes,
                               focused=ed.focused, ghost=shown, single=single_line, return_extras=True)
    if reach and not single_line:
        size = max(view * view / (reach + view), 24.)
        top = (view - size) * scroll_y / reach
        dl.add_rect_filled(width - BAR + 2, top, width - 2, top + size, THUMB, 3.)
    popovers(ed, x - ed.scroll_x, y - scroll_y, width, height)
    if changed:
        # An inline widget edited the text: run again to follow it.
        cache.invalidate_id(node)
    done = doc.version != ed.returned
    ed.returned = doc.version
    return done, doc.text


def popovers(ed, x, y, width, height):
    """Declare the open popovers at the caret: suggestions, the signature
    hint, an error's detail with its quick fixes."""
    menu, cw, lh = ed.suggest, ed.cw, ed.lh
    below = y + lh
    if menu.open:
        rows = menu.rows
        first = max(menu.index - ROWS + 1, 0)
        wide = (max(len(name) + len(kind) for name, kind in rows) + 4) * cw + 12
        tall = min(len(rows), ROWS) * lh
        choices(ed, name='suggestions', melty_window=True, content_offset=(0., 0.), rows=rows,
                index=menu.index, first=first, left=x, width=wide, height=tall,
                top=below if below + tall <= height or y < tall else y - tall)
    elif menu.signature is not None and ed.detail is None:
        name, parameters, active = menu.signature
        label = f'{name}(' + ', '.join(f'[{item}]' if at == active else item for at, item in enumerate(parameters)) + ')'
        note(None, name='signature', melty_window=True, content_offset=(0., 0.), lines=(label,),
             left=x, top=y - lh - 4 if y > lh + 4 else below, width=(len(label) + 2) * cw, height=lh)
    if ed.detail is not None and ed.detail in ed.errors:
        fixes = ed.fixes.get(ed.detail, ())
        lines = ed.errors[ed.detail].split('\n')
        wide = (max(len(item) for item in lines + list(fixes)) + 10) * cw
        line_y = ed.rows.row(ed.detail) * lh - (ed.pane.scroll_offset[1] if ed.pane is not None else 0.) + lh
        note(None, name='detail', melty_window=True, content_offset=(0., 0.), lines=tuple(lines), color=ERROR,
             left=ed.gutter, top=line_y, width=wide, height=len(lines) * lh)
        if fixes:
            choices(ed, name='fixes', melty_window=True, content_offset=(0., 0.),
                    rows=tuple((item, 'import') for item in fixes), index=0, left=ed.gutter,
                    top=line_y + len(lines) * lh, width=wide, height=len(fixes) * lh)


# --- built in inline widgets ----------------------------------------------
@inline('bool')
def switch(input_value: str, width=0., height=0.):
    """True / False: a click flips it."""
    dl = imgui.get_window_draw_list()
    clicked = imgui.invisible_button('##flip', width, height)
    on = input_value == 'True'
    hovered = imgui.is_item_hovered()
    dl.add_rect(0, 1, width, height - 1, pack_color(.80, .47, .20, .9 if hovered else .45), 4.)
    dl.add_circle_filled(width - 4 if on else 4, height - 3, 2., pack_color(.80, .47, .20))
    return clicked, ('False' if on else 'True') if clicked else input_value


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
    """Shared colour widget body: an underline in the colour; a click opens a picker."""
    dl = imgui.get_window_draw_list()
    if imgui.invisible_button('##colour', width, height):
        # A state write does not run the view again: carry on with the new value.
        draw_state.color = color = None if color else list(rgb)
    dl.add_rect_filled(0, height - 4, width, height - 1, pack_color(*rgb[:3]), 1.)
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
    dl.add_rect_filled(0, 0, width, height, POP, 4.)
    dl.add_rect_filled(width - 34, 6, width - 6, height - 6, pack_color(*input_value[:3]), 3.)
    value, changed = list(input_value), False
    imgui.push_item_width(width - 70)
    for at, label in enumerate('rgba'[:len(value)]):
        imgui.set_cursor_pos((6, 6 + at * 24))
        moved, value[at] = imgui.slider_float(label, value[at], 0., 1.)
        changed |= moved
    imgui.pop_item_width()
    return changed, value


@inline('color3')
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


@inline('colorhex')
def colour_hex(input_value: str, width=0., height=0., color=None, draw_state=None):
    """A `'#rrggbb'` string."""
    rgb = syntax.tools()._parse_hex_color(input_value)
    if rgb is None:
        return False, input_value
    quote = input_value[0]
    return _swatch(input_value, width, height, rgb,
                   lambda picked: quote + '#' + ''.join(f'{int(part * 255 + .5):02x}' for part in picked) + quote,
                   color, draw_state)
