"""Key presses to edit commands.

`COMMANDS` maps `(key, modifiers)` to a function of the `Editor` session and
the view's `Desk` (clipboard, flags, submit). Add a shortcut with `@bind`;
`moves=True` also binds the Shift variant, which extends the selection.
"""
import meltygui.core.windowing.window_constants as glfw

from meltygui.editor import edit

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

@bind(glfw.KEY_LEFT, CTRL, moves=True)
def _word_left(ed, desk, select): edit.left(ed.doc, select, word=True)

@bind(glfw.KEY_RIGHT, CTRL, moves=True)
def _word_right(ed, desk, select): edit.right(ed.doc, select, word=True)

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

# Undo and redo are not bound here: Ctrl+Z, Ctrl+Shift+Z and Ctrl+Y are the
# framework's hotkeys (`Melty.fire_global_hotkeys`) over its one undo stack,
# where the editor's steps live (history.py).

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
@bind(glfw.KEY_LEFT_BRACKET, CTRL | SHIFT)
def _fold(ed, desk):
    """Collapse the innermost open range around the caret."""
    folds = ed.folds
    for at in folds.around(ed.doc.line_of(ed.doc.caret)):
        if not folds.is_closed(at):
            return ed.toggle(folds.range(at)[0])

@bind(glfw.KEY_RIGHT_BRACKET, CTRL | SHIFT)
def _unfold(ed, desk):
    line = ed.doc.line_of(ed.doc.caret)
    found = ed.fold(line)
    if found is not None and found[0].is_closed(found[1]):
        ed.toggle(line)

@bind(glfw.KEY_MINUS, CTRL | SHIFT)
def _fold_all(ed, desk): ed.fold_all(True)

@bind(glfw.KEY_EQUAL, CTRL | SHIFT)
def _unfold_all(ed, desk): ed.fold_all(False)
