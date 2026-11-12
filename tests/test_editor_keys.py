"""Key chords and edit commands of the native editor that follow the legacy editor (no window)."""
import meltygui.core.windowing.window_constants as glfw
from meltygui.core.runtime.toggles import Toggles
from meltygui.editor import edit, keys
from meltygui.editor.document import Document
from meltygui.editor.editor import Editor
from meltygui.editor.syntax import tools

CTRL, SHIFT = glfw.MOD_CONTROL, glfw.MOD_SHIFT
SOURCE = '''import os
import sys

# a comment
# run

class A:
    """Doc
    more
    """
    def f(self, a):
        if a:
            return 1
        return 2

    def g(self):
        for n in a:
            pass
        return 3

def top():
    while True:
        pass

def last():
    pass
'''
IMPORTS, COMMENT, A, DOC, F, IF, G, FOR, TOP, WHILE, LAST = (0, 1), (3, 4), (6, 18), (7, 8), (10, 13), (11, 12), (15, 18), (16, 17), \
    (20, 22), (21, 22), (24, 25)


def session(text=SOURCE, defaults=False, line=0, column=0):
    tools()
    ed = Editor(text, fold_defaults=defaults)
    # (A text over `INLINE` characters is read on the source's thread: wait for it.)
    ed.source.settle()
    ed.land()
    ed.doc.select(ed.doc.index(line, column))
    return ed


def press(ed, key, mods=0, desk=None):
    return keys.press(ed, desk or keys.Desk(), key, mods)


def closed(ed):
    return ed.rows.closed()


def test_the_fold_table_of_the_source_is_the_one_the_rules_assume():
    ed = session()
    assert ed.rows.ranges() == [IMPORTS, COMMENT, A, DOC, F, IF, G, FOR, TOP, WHILE, LAST]
    assert [name[0] for name in ed.rows.names()] == ['imports', 'comment', 'scope', 'str', 'scope', 'block', 'scope', 'block',
                                                     'scope', 'block', 'scope']


def test_ctrl_minus_collapses_the_innermost_open_scope_and_walks_outward():
    ed = session(line=12, column=12)
    assert press(ed, glfw.KEY_MINUS, CTRL) and closed(ed) == [IF]
    assert ed.doc.place(ed.doc.caret) == (11, len('        if a:')), 'a hidden caret goes to its fold header'
    press(ed, glfw.KEY_MINUS, CTRL)
    assert closed(ed) == [F, IF]
    press(ed, glfw.KEY_MINUS, CTRL)
    assert closed(ed) == [A, F, IF] and ed.doc.line_of(ed.doc.caret) == 6
    press(ed, glfw.KEY_MINUS, CTRL)
    assert closed(ed) == [A, F, IF], 'nothing further out'


def test_ctrl_equal_expands_the_collapsed_scope_at_the_caret_then_what_is_nested_further_out():
    ed = session(defaults=True, line=12, column=12)
    assert closed(ed) == [IMPORTS, COMMENT, DOC]
    for _ in range(3):
        press(ed, glfw.KEY_MINUS, CTRL)
    ed.rows.toggle(21)
    assert closed(ed) == [IMPORTS, COMMENT, A, DOC, F, IF, WHILE]
    press(ed, glfw.KEY_EQUAL, CTRL)
    assert closed(ed) == [IMPORTS, COMMENT, DOC, F, IF, WHILE], 'the outermost collapsed one at the caret opens first'
    press(ed, glfw.KEY_EQUAL, CTRL)
    assert closed(ed) == [IMPORTS, COMMENT, DOC, WHILE], 'then what is collapsed inside the scope around it, not its docstring'
    press(ed, glfw.KEY_EQUAL, CTRL)
    assert closed(ed) == [IMPORTS, COMMENT, DOC], 'then the rest of the text; the default runs stay'
    press(ed, glfw.KEY_EQUAL, CTRL)
    assert closed(ed) == [IMPORTS, COMMENT, DOC], 'nothing left to open'
    # On a default run itself the chord opens it.
    ed.doc.select(0)
    press(ed, glfw.KEY_EQUAL, CTRL)
    assert closed(ed) == [COMMENT, DOC]


def test_ctrl_shift_minus_collapses_roots_one_level_below_and_the_default_runs():
    ed = session(line=12, column=12)
    press(ed, glfw.KEY_MINUS, CTRL | SHIFT)
    assert closed(ed) == [IMPORTS, COMMENT, A, DOC, F, G, TOP, WHILE, LAST], 'roots, their members, every default run: not IF or FOR'
    assert ed.doc.line_of(ed.doc.caret) == 6 and not ed.rows.hidden(6)
    press(ed, glfw.KEY_EQUAL, CTRL | SHIFT)
    assert closed(ed) == [IMPORTS, COMMENT, DOC], 'expand-all leaves the default-collapsed runs alone'


def test_a_file_that_is_one_class_folds_its_members():
    ed = session('class Only:\n    def a(self):\n        pass\n\n    def b(self):\n        pass\n')
    press(ed, glfw.KEY_MINUS, CTRL | SHIFT)
    assert closed(ed) == [(1, 2), (4, 5)], 'a lone root is looked through'


def test_with_a_selection_the_chords_act_on_the_folds_whose_header_it_covers():
    ed = session()
    ed.doc.select(ed.doc.start(16), ed.doc.start(10))       # lines 10..15: the selection ends at column 0 of line 16
    press(ed, glfw.KEY_MINUS, CTRL | SHIFT)
    assert closed(ed) == [F, IF, G], 'the outermost covered ones and one level below: FOR is headed outside the selection'
    assert ed.doc.caret != ed.doc.anchor, 'the selection stays for the next press'
    press(ed, glfw.KEY_EQUAL, CTRL)
    assert closed(ed) == [IF], 'one level out: the collapsed ones in sight'
    press(ed, glfw.KEY_EQUAL, CTRL)
    assert closed(ed) == []
    press(ed, glfw.KEY_MINUS, CTRL)
    assert closed(ed) == [F, G], 'the outermost covered folds'
    press(ed, glfw.KEY_EQUAL, CTRL | SHIFT)
    assert closed(ed) == []
    # A selection that covers no header line falls back to the caret rules.
    ed.doc.select(ed.doc.index(12, 14), ed.doc.index(12, 4))
    press(ed, glfw.KEY_MINUS, CTRL)
    assert closed(ed) == [IF]


def test_expand_all_inside_a_selection_keeps_default_runs_and_one_press_opens_a_run_under_it():
    ed = session(defaults=True)
    ed.doc.select(ed.doc.start(9), ed.doc.start(6))
    ed.rows.toggle(6)
    press(ed, glfw.KEY_EQUAL, CTRL | SHIFT)
    assert closed(ed) == [IMPORTS, COMMENT, DOC]
    ed.doc.select(ed.doc.start(9), ed.doc.start(7))
    press(ed, glfw.KEY_EQUAL, CTRL)
    assert closed(ed) == [IMPORTS, COMMENT], 'a selection over a default run opens it'


def test_a_fold_chord_keeps_the_carets_row_where_it_is_on_screen():
    class Pane:
        scroll_offset = (0., 180.)
    ed = session(line=24)
    ed.pane, ed.lh = Pane(), 18.
    press(ed, glfw.KEY_MINUS, CTRL | SHIFT)
    assert ed.rows.row(24) == 24 - 16 and ed.scroll_to == 180. - 16 * 18., "16 lines above the caret were hidden"


def test_the_old_bracket_chords_and_word_selection_chords_are_gone():
    for chord in ((glfw.KEY_LEFT_BRACKET, CTRL | SHIFT), (glfw.KEY_RIGHT_BRACKET, CTRL | SHIFT),
                  (glfw.KEY_LEFT, CTRL | SHIFT), (glfw.KEY_RIGHT, CTRL | SHIFT)):
        assert chord not in keys.COMMANDS
    ed = session('one two three', column=4)
    assert not press(ed, glfw.KEY_RIGHT, CTRL | SHIFT) and ed.doc.selection == (4, 4), "navigation undo's chord"
    assert press(ed, glfw.KEY_RIGHT, CTRL) and ed.doc.caret == 7
    assert press(ed, glfw.KEY_LEFT, CTRL) and ed.doc.caret == 4
    assert press(ed, glfw.KEY_RIGHT, SHIFT) and ed.doc.selection == (4, 5)


def test_tab_aligns_a_continuation_line_to_its_open_bracket():
    doc = Document('call(first,\n  second)\n')
    doc.select(doc.index(1, 1))
    edit.indent(doc)
    assert doc.text == 'call(first,\n     second)\n' and doc.place(doc.caret) == (1, 5)
    edit.indent(doc)
    assert doc.text == 'call(first,\n        second)\n', 'aligned already: a plain tab'
    doc.select(doc.index(1, 0))
    edit.indent(doc, dedent=True)
    assert doc.text == 'call(first,\n     second)\n' and doc.place(doc.caret) == (1, 5), 'Shift+Tab pulls it down to the bracket'
    # A hanging bracket asks for one level under its line.
    doc = Document('    call(\nsecond)\n')
    doc.select(doc.index(1, 0))
    edit.indent(doc)
    assert doc.text == '    call(\n        second)\n'
    # Past the indent, with a selection, or outside brackets Tab is what it was.
    doc = Document('call(first,\n  second)\n')
    doc.select(doc.index(1, 4))
    edit.indent(doc)
    assert doc.text == 'call(first,\n  se    cond)\n'
    doc = Document('a = 1\n  b = 2\n')
    doc.select(doc.index(1, 0))
    edit.indent(doc)
    assert doc.text == 'a = 1\n      b = 2\n'
    doc = Document('call(first,\n  second)\n')
    doc.select(doc.index(1, 8), doc.index(1, 0))
    edit.indent(doc)
    assert doc.text == 'call(first,\n      second)\n'


def test_the_tab_keys_align_through_the_bindings():
    ed = session('call(first,\n  second)\n', line=1)
    press(ed, glfw.KEY_TAB)
    assert ed.doc.line(1) == '     second)'
    ed = session('call(first,\n          second)\n', line=1, column=3)
    press(ed, glfw.KEY_TAB, SHIFT)
    assert ed.doc.line(1) == '     second)'


def test_ctrl_backspace_and_ctrl_delete_remove_one_unit():
    ed = session('value = call((arg', column=17)
    press(ed, glfw.KEY_BACKSPACE, CTRL)
    assert ed.doc.text == 'value = call(('
    press(ed, glfw.KEY_BACKSPACE, CTRL)
    assert ed.doc.text == 'value = call(', 'one bracket, not the run of them'
    ed = session('x = [[1.5, 2]]', column=4)
    press(ed, glfw.KEY_DELETE, CTRL)
    assert ed.doc.text == 'x = [1.5, 2]]' and ed.doc.caret == 4
    press(ed, glfw.KEY_DELETE, CTRL)
    press(ed, glfw.KEY_DELETE, CTRL)
    assert ed.doc.text == 'x = .5, 2]]', 'a word stops at punctuation'
    ed = session('a ==  b', column=2)
    press(ed, glfw.KEY_DELETE, CTRL)
    assert ed.doc.text == 'a   b', 'a run of operators is one unit'
    press(ed, glfw.KEY_DELETE, CTRL)
    assert ed.doc.text == 'a b', 'and so is a run of spaces'


def test_enter_splits_a_string_only_while_the_setting_is_on(monkeypatch):
    def entered():
        ed = session('name = "one two"\n', column=12)
        ed.source.settle()
        ed.land()
        press(ed, glfw.KEY_ENTER)
        return ed.doc.text
    monkeypatch.setattr(Toggles.TextEditor, 'enter_splits_strings', True)
    assert entered() == 'name = ("one "\n        "two")\n'
    monkeypatch.setattr(Toggles.TextEditor, 'enter_splits_strings', False)
    assert entered() == 'name = "one \ntwo"\n'


def test_paste_reindents_text_that_brings_its_own_indent():
    doc = Document('def f():\n    ')
    doc.select(doc.size)
    edit.paste(doc, '        value = 1')
    assert doc.text == 'def f():\n    value = 1', 'one line with leading whitespace lands at the caret'
    doc = Document('def f():\n    x = ')
    doc.select(doc.size)
    edit.paste(doc, '  1')
    assert doc.text == 'def f():\n    x =   1', 'mid line it is pasted as it is'


def test_ctrl_i_inserts_the_placeholder_icon():
    ed = session('label = ""', column=9)
    press(ed, glfw.KEY_I, CTRL)
    assert ed.doc.text == f'label = "{tools().GENERIC_ICON}"' and ed.doc.caret == 10
    assert not press(ed, glfw.KEY_I, CTRL, keys.Desk(editable=False)) and len(ed.doc.text) == 11


def test_the_folds_of_the_all_chords_are_the_legacy_levels_on_real_files():
    """`Rows.levels` (Rust) against the legacy helpers it replaced in `chosen`, and `Rows.set` against a toggle each."""
    from pathlib import Path
    root, compared = Path(__file__).parent.parent / 'meltygui', 0
    for name in ('editor/editor.py', 'editor/keys.py', 'editor/marks.py', 'model/source_model.py', 'model/roster_model.py',
                 'view/editor_view.py', 'view/icon_picker_view.py', 'core/runtime/extensions.py', 'state/editor_state.py'):
        ed = session((root / name).read_text())
        rows = ed.rows
        ranges = rows.ranges()
        skip = {item for item, name in zip(ranges, rows.names()) if name[0] in keys.DEFAULTS}
        roots = tools().fold_root_scopes(ranges, skip)
        both = roots + tools().fold_child_scopes(ranges, roots)
        assert sorted(rows.levels(True)) == sorted(head for head, _ in set(both) | skip), name
        assert sorted(rows.levels(False)) == sorted(head for head, _ in set(both) - skip), name
        # One call for all of them leaves what a toggle each did.
        other = session((root / name).read_text()).rows
        for head in rows.levels(True):
            if other.fold(head) == (0, False):
                other.toggle(head)
        assert rows.set(rows.levels(True), True) and not rows.set(rows.levels(True), True)
        assert rows.closed() == other.closed() and rows.count == other.count and sorted(map(repr, rows.collapsed())) == sorted(map(repr, other.collapsed())), name
        assert rows.set(rows.levels(False), False) and all(item in skip for item in rows.closed()), name
        compared += len(both)
    assert compared > 200
