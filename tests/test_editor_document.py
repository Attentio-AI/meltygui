"""Document, edit commands and the line to row mapping of the native editor."""
import random

import pytest

from meltygui.editor import edit
from meltygui.editor.document import Document, Change
from meltygui.editor.rows import Rows, Folds
from meltygui.editor.syntax import Syntax, tools, structure


def starts(text):
    return [0] + [i + 1 for i, c in enumerate(text) if c == '\n']


def test_replace_keeps_the_line_table_and_reports_changed_lines():
    doc = Document('one\ntwo\nthree')
    assert doc.lines == 3 and doc.line(1) == 'two' and doc.place(5) == (1, 1)
    assert doc.replace(4, 7, 'TWO\nand') == Change(1, 1, 2, 'two')
    assert doc.text == 'one\nTWO\nand\nthree' and doc.starts == starts(doc.text)
    assert doc.caret == 11 and doc.selection == (11, 11)
    assert doc.replace(3, 8, "") == Change(0, 3, 1, "one\nTWO\nand")
    assert doc.text == 'oneand\nthree' and doc.starts == starts(doc.text)
    assert doc.replace(0, 0, '') is None


def test_random_edits_match_a_rebuilt_line_table_and_undo_restores_everything():
    random.seed(7)
    doc = Document('alpha\nbeta\n\ngamma delta\n')
    history = [doc.text]
    for _ in range(400):
        low = random.randint(0, len(doc.text))
        high = random.randint(low, min(len(doc.text), low + 6))
        doc.replace(low, high, random.choice(['', 'x', '\n', 'ab\ncd', '  ', '\n\n']))
        assert doc.starts == starts(doc.text)
        history.append(doc.text)
    for expected in reversed(history[:-1]):
        if doc.text == expected:
            continue
        doc.undo()
        assert doc.text == expected and doc.starts == starts(doc.text)
    while doc.redo():
        pass
    assert doc.text == history[-1]


def test_typing_merges_into_one_undo_step_and_redo_replays_it():
    doc = Document('')
    for char in 'hello':
        edit.insert(doc, char, merge=True)
    edit.insert(doc, ' ')
    assert doc.text == 'hello ' and len(doc.undone) == 2
    doc.undo(); doc.undo()
    assert doc.text == '' and doc.caret == 0
    doc.redo()
    assert doc.text == 'hello' and doc.caret == 5


def test_caret_movement_selection_and_word_units():
    doc = Document('def run(alpha, beta):\n    return alpha\n')
    edit.right(doc, word=True)
    assert doc.caret == 3
    edit.right(doc, select=True, word=True)
    assert edit.selected(doc) == ' run'
    edit.left(doc)
    assert doc.selection == (3, 3)
    edit.end(doc)
    assert doc.place(doc.caret) == (0, 21)
    rows = Rows(doc.lines)
    edit.vertical(doc, rows, 1)
    assert doc.place(doc.caret) == (1, 16)
    edit.vertical(doc, rows, 1)
    edit.vertical(doc, rows, -2)
    assert doc.place(doc.caret) == (0, 21), 'the goal column survives the short lines'
    edit.home(doc)
    assert doc.caret == 0
    doc.select(30)
    edit.home(doc)
    assert doc.place(doc.caret) == (1, 4)
    edit.home(doc)
    assert doc.place(doc.caret) == (1, 0)
    edit.select_word(doc, 9)
    assert edit.selected(doc) == 'alpha'
    edit.select_line(doc, 9)
    assert edit.selected(doc) == 'def run(alpha, beta):\n'
    edit.select_all(doc)
    assert edit.selected(doc) == doc.text


def test_enter_indents_for_blocks_brackets_and_comments():
    doc = Document('def run():')
    doc.select(len(doc.text))
    edit.enter(doc)
    assert doc.text == 'def run():\n    ' and doc.caret == len(doc.text)
    edit.insert(doc, 'call(a,')
    edit.enter(doc)
    assert doc.text.endswith('call(a,\n         ')
    doc = Document('    x = 1  # note here')
    syntax = Syntax()
    tools()
    doc.select(doc.text.index('here'))
    edit.enter(doc, syntax)
    assert doc.text == '    x = 1  # note \n           # here'
    doc = Document('    value = 1')
    doc.select(4)
    edit.enter(doc, below=True)
    assert doc.text == '    value = 1\n    ' and doc.caret == len(doc.text)


def test_tab_backspace_delete_comment_and_cut():
    doc = Document('a\n  b\nc')
    doc.select(1)
    edit.indent(doc)
    assert doc.text == 'a   \n  b\nc'
    doc.undo()
    doc.select(7, 0)
    edit.indent(doc)
    assert doc.text == '    a\n      b\n    c' and edit.selected(doc) == doc.text
    edit.indent(doc, dedent=True)
    assert doc.text == 'a\n  b\nc'
    edit.comment(doc)
    assert doc.text == '# a\n#   b\n# c'
    edit.comment(doc)
    assert doc.text == 'a\n  b\nc'
    doc = Document('        deep')
    doc.select(8)
    edit.backspace(doc)
    assert doc.text == '    deep'
    doc.select(len(doc.text))
    edit.backspace(doc, word=True)
    assert doc.text == '    '
    doc = Document('one two\nthree')
    edit.delete(doc, word=True)
    assert doc.text == ' two\nthree'
    assert edit.cut(doc) == ' two\n' and doc.text == 'three'
    doc.select(5, 0)
    edit.insert(doc, '(')
    assert doc.text == '(three)'


def test_paste_reindents_a_block_to_the_caret():
    doc = Document('def f():\n    ')
    doc.select(len(doc.text))
    edit.paste(doc, 'if a:\r\n    b()\n')
    assert doc.text == 'def f():\n    if a:\n        b()\n'


def test_set_token_keeps_the_caret_behind_the_edit():
    doc = Document('x = 10 + y')
    doc.select(9)
    edit.set_token(doc, 0, 4, 2, '1000')
    assert doc.text == 'x = 1000 + y' and doc.text[doc.caret] == 'y'


def naive(lines, folds):
    hidden = set()
    for head, last in folds:
        if 0 <= head < last:
            hidden.update(range(head + 1, min(last, lines - 1) + 1))
    visible = [line for line in range(lines) if line not in hidden]
    row_of = {}
    for line in range(lines):
        row_of[line] = len([v for v in visible if v <= line]) - 1
    return visible, row_of, hidden


def test_rows_without_folds_are_the_identity():
    rows = Rows(5)
    assert rows.count == 5 and [rows.line(r) for r in range(5)] == list(range(5))
    assert rows.row(3) == 3 and not rows.hidden(3) and rows.folded(3) == 0
    assert rows.line(99) == 4 and rows.row(-3) == 0 and rows.span(1, 9) == [1, 2, 3, 4]
    assert Rows(0).count == 1


def test_rows_match_a_naive_mapping_for_random_nested_and_overlapping_folds():
    random.seed(11)
    for _ in range(600):
        lines = random.randint(1, 60)
        folds = []
        for _ in range(random.randint(0, 8)):
            head = random.randint(-1, lines)
            folds.append((head, head + random.randint(-1, 12)))
        rows = Rows(lines, folds)
        visible, row_of, hidden = naive(lines, folds)
        assert rows.count == len(visible)
        assert [rows.line(row) for row in range(rows.count)] == visible
        assert rows.span(0, rows.count + 3) == visible
        for line in range(lines):
            assert rows.row(line) == row_of[line], (lines, folds, line)
            assert rows.hidden(line) == (line in hidden)
            assert rows.line(rows.row(line)) == line or line in hidden
            under = rows.folded(line)
            if under:
                assert line not in hidden and all(line + i in hidden for i in range(1, under + 1))
                assert line + under + 1 >= lines or line + under + 1 not in hidden
            elif line not in hidden:
                assert line + 1 >= lines or line + 1 not in hidden


SOURCE = '''"""Doc
more
"""
import os
import sys

class A:
    x = 1
    def f(self, a):
        if a:
            return 1
        return 2

def top():
    pass
'''


def folds_of(text, defaults=False):
    syntax = Syntax()
    tools()
    folds = Folds()
    ranges, keys, initial = syntax.folds(text)
    folds.set(ranges, keys, initial if defaults else ())
    return folds


def test_folds_toggle_by_head_line_and_survive_line_shifts_by_key():
    folds = folds_of(SOURCE)
    assert folds.at(6) == (6, 11) and folds.at(8) == (8, 11) and folds.at(7) is None
    assert folds.toggle(8) and not folds.toggle(7)
    assert folds.closed() == [(8, 11)] and folds.is_closed((8, 11))
    rows = Rows(SOURCE.count('\n') + 1, folds.closed())
    assert rows.folded(8) == 3 and rows.row(13) == 10
    shifted = '# new first line\n' + SOURCE
    ranges, keys, _ = Syntax().folds(shifted)
    folds.set(ranges, keys)
    assert folds.closed() == [(9, 12)], 'the collapsed scope moved with its content'
    assert folds.reveal(11) and folds.closed() == []


def test_default_folds_seed_once_and_collapse_all_targets_outer_ranges():
    folds = folds_of(SOURCE, defaults=True)
    assert folds.closed() == [(0, 1), (3, 4)]
    folds.toggle(3)
    ranges, keys, initial = Syntax().folds(SOURCE)
    folds.set(ranges, keys, initial)
    assert folds.closed() == [(0, 1)], 'an opened default stays open on the next scan'
    folds.set_all(True)
    assert folds.closed() == [(0, 1), (3, 4), (6, 11), (13, 14)]
    folds.set_all(False)
    assert folds.closed() == []


def test_syntax_tokens_follow_string_state_and_report_recoloured_lines():
    syntax = Syntax()
    tools()
    text = 'a = 1\nb = """x\ny\n"""\nc = 2\n'
    assert syntax.update(text) is None
    assert syntax.tokens(2, 'y') == [('y', 'string')] or syntax.tokens(2, 'y')[0][1].startswith('string')
    assert [kind for _, kind in syntax.tokens(4, 'c = 2')][-1] == 'number'
    broken = text.replace('"""x', 'x', 1)
    assert syntax.update(broken) == 2, 'lines below the removed opener change colour'
    assert syntax.tokens(2, 'y')[0][1] == 'default'
    assert syntax.quiet(broken.index('c = 2')) is True and syntax.quiet(2) is False
    assert Syntax(language=None).tokens(0, 'x = 1') == [('x = 1', 'default')]


def test_structure_changes_only_when_a_line_could_change_fold_ranges():
    assert structure('    if a:') == structure('    if ab:')
    assert structure('    x = 1') != structure('     x = 1')
    assert structure('x') != structure('x:')
    assert structure('# a') != structure('a')
