"""Key presses to edit commands.

`COMMANDS` maps `(key, modifiers)` to a function of the `Editor` session and
the view's `Desk` (clipboard, flags, submit). Add a shortcut with `@bind`;
`moves=True` also binds the Shift variant, which extends the selection.
The fold chords and their rules are the last section.
"""
import meltygui.core.windowing.window_constants as glfw

from meltygui.editor import edit
from meltygui.editor.syntax import tools

CTRL, SHIFT, ALT = glfw.MOD_CONTROL, glfw.MOD_SHIFT, glfw.MOD_ALT
MODS = CTRL | SHIFT | ALT
COMMANDS = {}
# Commands that change text: skipped in a read only view.
EDITS = set()
# Rows a page key moves.
PAGE = 20


class Desk:
    """What a command may reach besides the session."""
    __slots__ = ('editable', 'single', 'submit', 'copy', 'clipboard', 'page')

    def __init__(self, editable=True, single=False, submit=None, copy=None, clipboard=None, page=PAGE):
        self.editable, self.single, self.submit = editable, single, submit
        self.copy, self.clipboard, self.page = copy, clipboard, page


def bind(key, mods=0, moves=False, edits=False):
    def register(command):
        if moves:
            COMMANDS[(key, mods)] = lambda ed, desk: command(ed, desk, False)
            COMMANDS[(key, mods | SHIFT)] = lambda ed, desk: command(ed, desk, True)
        else:
            COMMANDS[(key, mods)] = command
            if edits:
                EDITS.add((key, mods))
        return command
    return register


def printable(key, mods):
    """A press that also arrives as typed text."""
    return not mods & (CTRL | ALT) and (glfw.KEY_SPACE <= key <= glfw.KEY_GRAVE_ACCENT
                                         or glfw.KEY_KP_0 <= key <= glfw.KEY_KP_ADD and key != glfw.KEY_KP_ENTER)


def press(ed, desk, key, mods):
    """Run the command bound to a press, as one undo step. True when one ran."""
    chord = (key, mods & MODS)
    command = COMMANDS.get(chord)
    if command is None or (chord in EDITS and not desk.editable):
        return False
    ed.step = None
    command(ed, desk)
    ed.step = None
    return True


# --- caret ---------------------------------------------------------------
@bind(glfw.KEY_LEFT, moves=True)
def _left(ed, desk, select): edit.left(ed.doc, select)

@bind(glfw.KEY_RIGHT, moves=True)
def _right(ed, desk, select): edit.right(ed.doc, select)

# Ctrl+Shift+Left and Right are not bound: they are the framework's navigation
# undo and redo (`Melty.fire_global_hotkeys`), which fire while text has focus.
@bind(glfw.KEY_LEFT, CTRL)
def _word_left(ed, desk): edit.left(ed.doc, word=True)

@bind(glfw.KEY_RIGHT, CTRL)
def _word_right(ed, desk): edit.right(ed.doc, word=True)

@bind(glfw.KEY_UP, moves=True)
def _up(ed, desk, select): edit.vertical(ed.doc, ed.rows, -1, select)

@bind(glfw.KEY_DOWN, moves=True)
def _down(ed, desk, select): edit.vertical(ed.doc, ed.rows, 1, select)

@bind(glfw.KEY_PAGE_UP, moves=True)
def _page_up(ed, desk, select): edit.vertical(ed.doc, ed.rows, -desk.page, select)

@bind(glfw.KEY_PAGE_DOWN, moves=True)
def _page_down(ed, desk, select): edit.vertical(ed.doc, ed.rows, desk.page, select)

@bind(glfw.KEY_HOME, moves=True)
def _home(ed, desk, select): edit.home(ed.doc, select)

@bind(glfw.KEY_END, moves=True)
def _end(ed, desk, select): edit.end(ed.doc, select)

@bind(glfw.KEY_HOME, CTRL, moves=True)
def _top(ed, desk, select): edit.home(ed.doc, select, whole=True)

@bind(glfw.KEY_END, CTRL, moves=True)
def _bottom(ed, desk, select): edit.end(ed.doc, select, whole=True)

@bind(glfw.KEY_A, CTRL)
def _all(ed, desk): edit.select_all(ed.doc)


# --- text ----------------------------------------------------------------
@bind(glfw.KEY_BACKSPACE, edits=True)
def _backspace(ed, desk): edit.backspace(ed.doc)

@bind(glfw.KEY_BACKSPACE, CTRL, edits=True)
def _backspace_word(ed, desk): edit.backspace(ed.doc, word=True)

@bind(glfw.KEY_DELETE, edits=True)
def _delete(ed, desk): edit.delete(ed.doc)

@bind(glfw.KEY_DELETE, CTRL, edits=True)
def _delete_word(ed, desk): edit.delete(ed.doc, word=True)

@bind(glfw.KEY_ENTER, edits=True)
def _enter(ed, desk):
    if desk.single or desk.submit is not None:
        if desk.submit is not None:
            desk.submit(ed.doc.text)
    else:
        edit.enter(ed.doc, ed.syntax)

@bind(glfw.KEY_ENTER, SHIFT, edits=True)
def _enter_below(ed, desk):
    if desk.submit is not None and not desk.single:
        edit.insert(ed.doc, '\n')
    elif not desk.single:
        edit.enter(ed.doc, ed.syntax, below=True)

COMMANDS[(glfw.KEY_KP_ENTER, 0)] = _enter
EDITS.add((glfw.KEY_KP_ENTER, 0))

@bind(glfw.KEY_TAB, edits=True)
def _tab(ed, desk):
    if not desk.single:
        edit.indent(ed.doc)

@bind(glfw.KEY_TAB, SHIFT, edits=True)
def _untab(ed, desk):
    if not desk.single:
        edit.indent(ed.doc, dedent=True)

@bind(glfw.KEY_SLASH, CTRL, edits=True)
def _comment(ed, desk): edit.comment(ed.doc)

@bind(glfw.KEY_I, CTRL, edits=True)
def _icon(ed, desk): edit.icon(ed.doc)

# Undo and redo are not bound here: Ctrl+Z, Ctrl+Shift+Z and Ctrl+Y are the
# framework's hotkeys (`Melty.fire_global_hotkeys`) over its one undo stack,
# where the editor's steps live (history.py).
# Ctrl+B (go to definition, usages) is not bound here either: it is asked of
# the project's roster and answered on a later run, so the view sequences it
# (`typed` in view/editor_view.py, editor/lookup.py), as it does Ctrl+Space.
# Ctrl+Shift+B (the external editor) and Ctrl+F (the find bar, editor/find.py)
# are handled there too.

# --- clipboard -----------------------------------------------------------
@bind(glfw.KEY_C, CTRL)
def _copy(ed, desk):
    text = edit.selected(ed.doc)
    if text:
        desk.copy(text)

@bind(glfw.KEY_X, CTRL, edits=True)
def _cut(ed, desk): desk.copy(edit.cut(ed.doc))

@bind(glfw.KEY_V, CTRL, edits=True)
def _paste(ed, desk):
    text = desk.clipboard()
    if text:
        edit.paste(ed.doc, text.split('\n')[0] if desk.single else text)


# --- folds ---------------------------------------------------------------
# Ctrl+Minus / Ctrl+Equal collapse and expand the scope at the caret and walk
# outward on repeated presses; with Shift they act on every root scope and
# one level below. With a selection all four act only on the folds whose
# header line it covers. The rules are the legacy editor's (`draw_text`,
# "Keyboard folding"; its helpers `fold_root_scopes`, `fold_child_scopes`).
# Every root scope and its children are found where the folds are kept
# (`Rows.levels`), and a chord's targets change in one call (`Rows.set`):
# both cost the same however many folds there are. Only the scope layer is touched:
# the caller's ranges (diff context) answer to their own badges. What a chord
# targets is decided in `chosen`.

# Fold kinds that start collapsed (the first part of a fold's name): expand-all leaves them alone.
DEFAULTS = ('str', 'comment', 'imports')


def level(ranges):
    """The outermost of sorted nested `ranges`: those inside no other one of them."""
    found, end = [], -1
    for item in ranges:
        if item[0] > end:
            found.append(item)
            end = item[1]
    return found


def covered(doc, ranges):
    """The folds whose header line the selection covers. A selection that
    ends at column 0 does not cover that line."""
    low, high = doc.selection
    if low == high:
        return []
    first, last = doc.line_of(low), doc.line_of(high)
    if last > first and high == doc.start(last):
        last -= 1
    return [item for item in ranges if first <= item[0] <= last]


def near(rows, line, close):
    """The caret rule where it names one fold without reading the fold
    table: the innermost open scope collapses, the collapsed one the caret is
    on expands (a caret is never inside a collapsed fold). None: no such fold."""
    if close:
        head = rows.enclosing(line)
        return [] if head is None else [(head, head)]
    return [(line, line)] if rows.fold(line) == (0, True) else None


def chosen(ed, close, every):
    """(ranges to collapse or expand, the view stays on the caret): what a fold chord acts on."""
    rows, doc = ed.rows, ed.doc
    line = doc.line_of(doc.caret)
    found = None if every or doc.caret != doc.anchor else near(rows, line, close)
    if found is not None:
        return found, False
    if every and doc.caret == doc.anchor:
        # Every root scope and one level below: chosen where the folds are kept (`Rows.levels`), whatever their number.
        return [(head, head) for head in rows.levels(close)], True
    ranges = rows.ranges()
    shut = set(rows.closed())
    skip = {item for item, name in zip(ranges, rows.names()) if name[0] in DEFAULTS}
    inside = covered(doc, ranges)
    if inside:
        outer = level(inside)
        if close:
            return (outer + tools().fold_child_scopes(inside, outer) + [item for item in inside if item in skip]
                    if every else outer), True
        if every:
            return [item for item in inside if item not in skip], True
        # One level out: the collapsed ones in sight.
        return level([item for item in inside if item in shut]), True
    if every:
        return [(head, head) for head in rows.levels(close)], True
    found = near(rows, line, close)
    if found is not None:
        return found, False
    # Everything at the caret is open: expand what is collapsed inside the innermost scope around it,
    # on the next press inside the one around that, at last in the whole text.
    around = [item for item in ranges if item[0] <= line <= item[1]]
    for first, last in [*reversed(around), (-1, doc.lines)]:
        nested = [item for item in shut if first <= item[0] and item[1] <= last and item != (first, last) and item not in skip]
        if nested:
            return nested, True
    return [], False


def fold(ed, close, every=False):
    """Run a fold chord. Returns whether a fold changed."""
    rows, doc = ed.rows, ed.doc
    targets, anchored = chosen(ed, close, every)
    row = rows.row(doc.line_of(doc.caret))
    # (All of them in one call: the rows are laid out once.)
    if not rows.set([head for head, _ in targets], close):
        return False
    # A selection end left inside hidden lines moves to its fold header's end; the selection stays.
    doc.select(*(doc.end(rows.line(rows.row(doc.line_of(at)))) if rows.hidden(doc.line_of(at)) else at
                 for at in (doc.caret, doc.anchor)))
    if anchored and ed.pane is not None:
        # Many rows moved: the caret's row stays where it is on screen.
        ed.scroll_to = ed.pane.scroll_offset[1] + (rows.row(doc.line_of(doc.caret)) - row) * ed.lh
    ed.touch()
    return True


@bind(glfw.KEY_MINUS, CTRL)
def _fold(ed, desk): fold(ed, True)

@bind(glfw.KEY_EQUAL, CTRL)
def _unfold(ed, desk): fold(ed, False)

@bind(glfw.KEY_MINUS, CTRL | SHIFT)
def _fold_all(ed, desk): fold(ed, True, every=True)

@bind(glfw.KEY_EQUAL, CTRL | SHIFT)
def _unfold_all(ed, desk): fold(ed, False, every=True)
