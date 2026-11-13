"""Document, edit commands and the line to row mapping of the native editor."""
import random

import pytest

from meltygui.editor import edit, marks
from meltygui.editor.document import Document, Splice, BLOCK
from meltygui.editor.editor import Editor
from meltygui.editor.marks import Lines, Marks, Mark
from meltygui.editor.rows import Rows
from meltygui.editor.syntax import Syntax, tools
from meltygui.model.source_model import Source


def mapped(lines, folds=()):
    """Rows over `lines` lines with the `(head, last)` ranges collapsed."""
    rows = Rows(lines)
    rows.gaps([(head, last) for head, last in folds if head >= 0 and last >= 0])
    return rows


def rows_of(text, defaults=False, collapsed=()):
    """(rows, source): a session's rows with the fold ranges of `text` landed."""
    source, rows = Source(text), Rows(text.count('\n') + 1, list(collapsed), not defaults)
    read(source, rows)
    return rows, source


def read(source, rows):
    source.ask(now=True, rows=rows)
    assert source.land()
    rows.land(source.native, [], 0, 0)


def follow(line, change):
    """Where a whole line mark on `line` is after `change` (None: its line was removed): the rule, spelled out."""
    first, last = change.line, change.line + change.gone
    if line < first or (line == first and change.column):
        return line
    return line + change.came - change.gone if line >= last else None


def starts(text):
    return [0] + [i + 1 for i, c in enumerate(text) if c == '\n']


def table(doc):
    return [doc.start(line) for line in range(doc.lines)]


def test_replace_keeps_the_line_table_and_reports_the_splice():
    doc = Document('one\ntwo\nthree')
    assert doc.lines == 3 and doc.line(1) == 'two' and doc.place(5) == (1, 1)
    assert doc.replace(4, 7, 'TWO\nand') == Splice(1, 0, 0, 3, 1, 3)
    assert doc.text == 'one\nTWO\nand\nthree' and table(doc) == starts(doc.text)
    assert doc.caret == 11 and doc.selection == (11, 11)
    assert doc.replace(3, 8, "") == Splice(0, 3, 2, 0, 0, 3)
    assert doc.text == 'oneand\nthree' and table(doc) == starts(doc.text)
    assert doc.replace(0, 0, '') is None


def test_a_document_is_a_face_on_its_source_and_holds_no_text():
    source = Source('one\ntwo\n')
    doc = Document(source=source)
    assert doc.source is source and (doc.lines, doc.size, doc.line(1), doc.line(2)) == (3, 8, 'two', '')
    assert not any(isinstance(getattr(doc, name), str) for name in Document.__slots__), 'no copy of the text'
    assert doc.replace(4, 7, 'TWO') == Splice(1, 0, 0, 3, 0, 3) and source.text == 'one\nTWO\n' and source.native.version == doc.version
    assert doc.text is doc.text and doc.text == 'one\nTWO\n', 'joined on demand, once per version'
    assert Document('é\nx').place(3) == (1, 1) and Document('').lines == 1 and Document('a\n').rows(0, 9) == ['a', '']


@pytest.fixture(autouse=True)
def undo_stack():
    """The framework's undo stack is process wide: every test starts with it empty."""
    from meltygui.state.core_undo import UndoManager
    UndoManager.stack.history.clear()
    UndoManager.stack.redo_stack.clear()
    return UndoManager


def command(ed, function, *arguments, **options):
    """Run one edit command the way a key press does: as its own undo step."""
    ed.step = None
    found = function(ed.doc, *arguments, **options)
    ed.step = None
    return found


def test_random_edits_match_a_rebuilt_line_table_and_undo_restores_everything(undo_stack, monkeypatch):
    from meltygui.core.runtime.toggles import Toggles
    monkeypatch.setattr(Toggles.CodeEditor, 'undo_typing_max_chars', 0)     # every edit its own step
    random.seed(7)
    ed = Editor('alpha\nbeta\n\ngamma delta\n', language=None)
    doc = ed.doc
    history = [doc.text]
    for _ in range(100):
        low = random.randint(0, len(doc.text))
        high = random.randint(low, min(len(doc.text), low + 6))
        command(ed, Document.replace, low, high, random.choice(['', 'x', '\n', 'ab\ncd', '  ', '\n\n']))
        assert table(doc) == starts(doc.text)
        history.append(doc.text)
    for expected in reversed(history[:-1]):
        if doc.text == expected:
            continue
        undo_stack.undo()
        assert doc.text == expected and table(doc) == starts(doc.text)
    while undo_stack.stack.can_redo():
        undo_stack.redo()
    assert doc.text == history[-1]


def test_an_edit_rewrites_its_own_block_and_the_blocks_below_only_move():
    text = ''.join(f'line {n}\n' for n in range(BLOCK * 10))
    doc = Document(text)
    blocks = doc.blocks(0, doc.lines)
    assert [found[1:] for found in blocks] == [(BLOCK * n, BLOCK + (n == 9), 0) for n in range(10)], 'the last block holds the empty last line'
    at = doc.start(BLOCK * 3 + 5)
    doc.replace(at, at, 'new\n')
    after = doc.blocks(0, doc.lines)
    assert [found[0] for found in after] == [found[0] for found in blocks], 'one block rewritten, none replaced'
    assert after[3][1:] == (BLOCK * 3, BLOCK + 1, doc.version) and after[4][1:] == (BLOCK * 4 + 1, BLOCK, 0), 'the blocks below only move'
    assert doc.line(BLOCK * 4 + 1) == f'line {BLOCK * 4}'
    doc.replace(at, at, 'x\n' * (BLOCK * 3))
    grown = doc.blocks(0, doc.lines)
    assert grown[3][0] == blocks[3][0] and len(grown) == 14 and max(found[2] for found in grown) <= BLOCK * 2
    assert [found[0] for found in grown[-6:]] == [found[0] for found in blocks[-6:]] and len({found[0] for found in grown}) == 14
    assert doc.text == text[:at] + 'x\n' * (BLOCK * 3) + 'new\n' + text[at:]
    assert doc.blocks(BLOCK * 3, BLOCK * 3) == grown[3:4] and doc.blocks(doc.lines - 1, doc.lines + 5) == grown[-1:]
    doc.replace(0, doc.size, '')
    assert len(doc.blocks(0, 9)) == 1 and doc.lines == 1 and doc.text == ''


def test_random_edits_over_many_blocks_match_a_plain_string():
    random.seed(3)
    ref = ''.join(f'line {n}\n' for n in range(200))
    doc = Document(ref)
    for step in range(1500):
        low = random.randint(0, len(ref))
        high = random.randint(low, min(len(ref), low + random.choice([0, 0, 1, 5, 300])))
        new = random.choice(['', 'x', '\n', 'ab\ncd', '  ', '\n\n', 'q\n' * 70])
        doc.splice(low, high, new)
        ref = ref[:low] + new + ref[high:]
        assert doc.text == ref and doc.lines == ref.count('\n') + 1 and doc.size == len(ref)
        if step % 50 == 0:
            assert table(doc) == starts(ref)
            assert [doc.line(line) for line in range(doc.lines)] == ref.split('\n')
            for index in random.sample(range(len(ref) + 1), min(40, len(ref) + 1)):
                line = ref.count('\n', 0, index)
                assert doc.place(index) == (line, index - starts(ref)[line])
                assert doc.slice(index, index + 90) == ref[index:index + 90]
            found = doc.blocks(0, doc.lines)
            assert sum(count for _, _, count, _ in found) == doc.lines and [line for _, line, _, _ in found] == [
                sum(count for _, _, count, _ in found[:at]) for at in range(len(found))]
            assert all(doc.rows(line, count) == ref.split('\n')[line:line + count] for _, line, count, _ in found[::7])
    text, base = doc.near(len(ref) // 2)
    assert ref[base:base + len(text)] == text and base <= len(ref) // 2 < base + len(text)


def test_typing_folds_into_word_steps_on_the_framework_stack_and_redo_replays_them(undo_stack):
    ed = Editor('', language=None)
    doc, stack = ed.doc, undo_stack.stack
    for char in 'hello world':
        command(ed, edit.insert, char)
    assert doc.text == 'hello world' and [step.new for step in stack.history] == ['hello ', 'world']
    undo_stack.undo()
    assert doc.text == 'hello ' and doc.caret == 6 and ed.landing == (6, 6, True)
    undo_stack.undo()
    assert doc.text == '' and doc.caret == 0 and not stack.can_undo()
    undo_stack.redo()
    assert doc.text == 'hello ' and doc.caret == 6
    command(ed, edit.insert, 'x')
    assert [step.new for step in stack.history] == ['hello ', 'x'] and not stack.can_redo(), 'nothing folds across a redo'
    for _ in range(3):
        command(ed, edit.backspace)
    assert doc.text == 'hell' and [(step.old, step.new) for step in stack.history][1:] == [
        ('', 'x'), (' x', ''), ('o', '')], 'a backspace run stops where a word starts, as the framework rule says'
    command(ed, edit.move, 1)
    command(ed, edit.insert, 'E')
    command(ed, edit.insert, 'Y')
    doc.select(4, 2)
    command(ed, edit.insert, 'Z')
    assert doc.text == 'hEZll' and [step.new for step in stack.history][-2:] == ['EY', 'Z']
    undo_stack.undo()
    assert doc.text == 'hEYell' and doc.selection == (2, 4), 'the selection the edit replaced comes back'
    assert all(change.draw_state is ed and change.display_name == 'text' for change in stack.history)


def test_one_command_is_one_step_and_a_step_holds_only_its_splices(undo_stack):
    tools()
    text = ''.join(f'value_{n} = {n}\n' for n in range(4000))
    ed = Editor(text)
    doc, stack = ed.doc, undo_stack.stack
    doc.select(doc.start(2000) + 3, doc.start(2000))
    command(ed, edit.enter)
    assert len(stack.history) == 1 and len(stack.history[0].parts) == 2, 'the selection removed and the line break: one step'
    assert sum(len(old) + len(new) for _, old, new in stack.history[0].parts) < 20, 'never the whole text'
    doc.select(doc.start(10), doc.start(7))
    command(ed, edit.comment)
    assert doc.line(8) == '# value_8 = 8' and len(stack.history) == 2
    undo_stack.undo()
    undo_stack.undo()
    assert doc.text == text and doc.selection == (doc.start(2000), doc.start(2000) + 3)


def test_a_text_replaced_from_outside_is_a_splice_that_keeps_caret_and_steps(undo_stack):
    ed = Editor('one\ntwo\nthree\n', language=None)
    doc, stack = ed.doc, undo_stack.stack
    command(ed, edit.move, doc.start(2) + 5)
    command(ed, edit.insert, '!')
    command(ed, edit.move, doc.start(1))
    command(ed, edit.insert, '2')
    assert doc.text == 'one\n2two\nthree!\n'
    version = doc.version
    ed.load('zero\none\n2two\nthree!\n')
    assert doc.version == version + 1 and doc.place(doc.caret) == (2, 1) and len(stack.history) == 2, 'not a step'
    assert len(ed.doc.blocks(0, 99)) == 1 and ed.seen == doc.version
    undo_stack.undo()
    assert doc.text == 'zero\none\ntwo\nthree!\n', 'the step moved with its text'
    undo_stack.undo()
    assert doc.text == 'zero\none\ntwo\nthree\n'
    undo_stack.redo()
    undo_stack.redo()
    ed.load('zero\none\n2two\nthr\n')
    assert [step.new for step in stack.history] == ['2'], 'a step the outside change reached into is gone'
    command(ed, edit.insert, 'x')
    undo_stack.undo()
    ed.load('zero\none\n2two\nTHR\n')
    assert not stack.can_redo(), 'redo of this editor does not survive an outside change'
    other = Editor('other', language=None)
    command(other, edit.insert, 'y')
    ed.close()
    undo_stack.undo()
    undo_stack.undo()
    assert other.doc.text == 'other' and doc.text == 'zero\none\ntwo\nTHR\n', "the steps of a session that closed are still the text's"


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
    tools()
    session = Editor('    x = 1  # note here')
    doc, syntax = session.doc, session.syntax
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
    doc = Document('a\n  b\nc')
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
    assert rows.runs == 0
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
        rows = mapped(lines, folds)
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


def test_folds_toggle_by_head_line_and_survive_line_shifts_by_name():
    rows, source = rows_of(SOURCE)
    assert rows.fold(6) == (0, False) and rows.fold(7) is None and {(6, 11), (8, 11)} <= set(rows.ranges())
    assert rows.toggle(8) and rows.closed() == [(8, 11)] and rows.fold(8) == (0, True) and not rows.toggle(7)
    assert rows.folded(8) == 3 and rows.row(13) == 10 and rows.before(13) == 10 and rows.before(10) == 9
    assert rows.collapsed() == [('scope', 'A', 'f')] and rows.stamp == 1
    # An edit above moves it; a later session finds it again by its name.
    rows.follow(0, 0, 0, 1)
    assert rows.closed() == [(9, 12)] and rows.count == SOURCE.count('\n') + 2 - 3 and rows.hidden(10) and not rows.hidden(9)
    rows, source = rows_of('# new first line\n' + SOURCE, collapsed=[['scope', 'A', 'f']])
    assert rows.closed() == [(9, 12)], 'the collapsed scope is where its content is'
    assert rows.reveal(11) and rows.closed() == [] and rows.collapsed() == []
    assert rows.enclosing(11) == 10 and rows.enclosing(13) is None
    assert rows.guides(8, 10) == [(7, 12, 0, True), (9, 12, 4, True), (10, 11, 8, False)]
    rows.toggle(7)
    assert rows.guides(8, 10) == [(7, 12, 0, True)], 'scopes whose first line is hidden have no guide'


def test_default_folds_seed_once_and_collapse_all_targets_outer_ranges():
    rows, source = rows_of(SOURCE, defaults=True)
    assert rows.closed() == [(0, 1), (3, 4)] and sorted(rows.collapsed()) == [('imports',), ('str', '"""Doc')]
    rows.toggle(3)
    read(source, rows)
    assert rows.closed() == [(0, 1)], 'an opened default stays open on the next scan'
    rows.fold_all(True)
    assert rows.closed() == [(0, 1), (3, 4), (6, 11), (13, 14)]
    rows.fold_all(False)
    assert rows.closed() == []


def test_a_fold_toggled_while_its_text_is_read_keeps_its_state():
    rows, source = rows_of(SOURCE, defaults=True)
    source.ask(rest=30, rows=rows)
    assert rows.toggle(6) and rows.toggle(3)
    source.settle()
    read(source, rows)
    assert rows.closed() == [(0, 1), (6, 11)]


def test_rows_tell_whether_the_lines_asked_about_changed():
    rows, source = rows_of(SOURCE)
    source.splice(len(SOURCE), len(SOURCE), 'def more():\n    pass\n', 1)
    rows.follow(15, 0, 0, 2)
    source.ask(now=True, rows=rows)
    assert source.land() and not rows.land(source.native, [], 0, 14), 'the new scope is below the lines shown'
    assert (15, 16) in rows.ranges()
    source.splice(0, 0, 'if x:\n    pass\n', 2)
    rows.follow(0, 0, 0, 2)
    source.ask(now=True, rows=rows)
    assert source.land() and rows.land(source.native, [], 0, 14)


def test_folds_follow_random_edits_like_a_fresh_scan_for_untouched_ranges():
    """Between scans the ranges follow edits: every range the edit left
    alone must be exactly where a fresh scan finds it, and the row mapping
    is the one of the collapsed ranges where they are now."""
    tools()
    random.seed(5)
    base = ''.join(f'class A{n}:\n    x = 1\n    def f(self, a):\n        if a == {n}:\n            return 1\n'
                   f'        return 2\n\ndef top{n}():\n    pass\n\n' for n in range(8))
    for _ in range(120):
        doc = Document(base)
        rows, _ = rows_of(base)
        rows.gaps([(3, 5), (40, 44), (70, 78)])
        before = dict(zip(rows.names(), rows.ranges()))
        for head, _ in random.sample(rows.ranges(), 6):
            rows.toggle(head)
        for _ in range(3):
            line = random.randint(0, doc.lines - 1)
            low = doc.start(line) + random.choice([0, 0, len(doc.line(line))])
            high = low if random.random() < .6 else min(doc.size, doc.start(min(line + random.randint(1, 4), doc.lines - 1)))
            change = doc.splice(low, high, random.choice(['\n', '\n\n', '', 'pass\n', '    y = 2\n    z = 3\n']))
            rows.follow(change.line, change.column, change.gone, change.came)
            visible, row_of, hidden = naive(doc.lines, rows.closed() + rows.closed(1))
            assert rows.span(0, doc.lines) == visible and rows.count == len(visible), change
            assert [rows.row(line) for line in range(doc.lines)] == [row_of[line] for line in range(doc.lines)], change
            assert [rows.hidden(line) for line in range(doc.lines)] == [line in hidden for line in range(doc.lines)]
            assert [rows.before(line) for line in range(doc.lines)] == [len([v for v in visible if v < line]) for line in range(doc.lines)]
        fresh, _ = rows_of(doc.text)
        after = dict(zip(fresh.names(), fresh.ranges()))
        first, last = change.line, change.line + change.gone
        heads = [head for head, _ in rows.ranges()]
        assert heads == sorted(heads)
    # One edit: the ranges it left alone are where a fresh scan finds them.
    for _ in range(120):
        doc = Document(base)
        rows, _ = rows_of(base)
        before = dict(zip(rows.names(), rows.ranges()))
        line = random.randint(0, doc.lines - 1)
        low = doc.start(line) + random.choice([0, 0, len(doc.line(line))])
        high = low if random.random() < .6 else min(doc.size, doc.start(min(line + random.randint(1, 4), doc.lines - 1)))
        change = doc.splice(low, high, random.choice(['\n', '\n\n', '', 'pass\n', '    y = 2\n    z = 3\n']))
        rows.follow(change.line, change.column, change.gone, change.came)
        fresh, _ = rows_of(doc.text)
        after = dict(zip(fresh.names(), fresh.ranges()))
        first, last = change.line, change.line + change.gone
        checked = 0
        for name, found in zip(rows.names(), rows.ranges()):
            old = before.get(name)
            # Compare where the edit did not change what the range holds.
            if name in after and (old[1] < first or old[0] > last) and after[name][1] - after[name][0] == old[1] - old[0]:
                assert found == after[name], (name, change)
                checked += 1
        assert checked >= len(before) - 8


def test_lines_follow_edits_through_a_moving_gap():
    random.seed(2)
    for _ in range(200):
        plain = sorted(random.sample(range(400), 30))
        lines = Lines(plain)
        for _ in range(30):
            at, delta = random.randint(0, len(plain)), random.randint(-2, 3)
            if at and plain[at - 1] > (plain[at] + delta if at < len(plain) else 10 ** 6):
                continue
            lines.move(at, delta)
            plain[at:] = [line + delta for line in plain[at:]]
            if plain != sorted(plain):
                return
            probe = random.randint(-5, 420)
            assert lines.find(probe) == next((i for i, line in enumerate(plain) if line >= probe), len(plain))
            assert [lines.line(i) for i in range(len(plain))] == plain
        low = random.randint(0, len(plain))
        high = random.randint(low, len(plain))
        lines.drop(low, high)
        del plain[low:high]
        assert [lines.line(i) for i in range(len(plain))] == plain


def test_marks_follow_edits_with_their_text():
    """A span mark stays on the characters it was made for; a line mark
    follows its line by the rule `follow` spells out above."""
    random.seed(9)
    words = ['alpha', 'beta', 'gamma']
    for _ in range(150):
        text = '\n'.join(' '.join(random.choice(words + ['x', 'yy']) for _ in range(random.randint(0, 6)))
                         for _ in range(random.randint(1, 30)))
        doc = Document(text)
        source = Source(text)
        source.ask('alpha', now=True)
        assert source.land()
        # The same hits twice: as plain marks, and as the source keeps them (`Hits`); both must follow alike.
        found = Marks([(line, Mark('hit', low, high)) for line, low, high in source.read('alpha')[3]], doc.version)
        kept = marks.Hits(source.hits, doc.version)
        notes = Marks([(line, Mark('note', data=line)) for line in range(doc.lines)], doc.version)
        where = {line: line for line in range(doc.lines)}
        for _ in range(6):
            low = random.randint(0, doc.size)
            high = random.randint(low, min(doc.size, low + random.choice([0, 0, 1, 3, 40])))
            new = random.choice(['', 'z', '\n', 'q\nq', ' '])
            if any(at < edge < at + 5 for at in range(doc.size) if doc.text.startswith('alpha', at) for edge in (low, high)):
                continue        # an edit inside a word changes the word: nothing to check
            change = doc.splice(low, high, new)
            found.follow(change)
            kept.follow(change)
            notes.follow(change)
            assert kept.on(0, doc.lines) == found.on(0, doc.lines) and len(kept) == len(found), change
            where = {name: follow(line, change) for name, line in where.items() if follow(line, change) is not None}
        for line, mark in found.on(0, doc.lines):
            assert doc.line(line)[mark.low:mark.high] == 'alpha', (doc.text, line, mark)
        assert sorted((line, mark.data) for line, mark in notes.on(0, doc.lines)) == sorted(
            (line, name) for name, line in where.items())
    stale = Marks([(0, Mark('note'))], version=0)
    doc = Document('a\nb')
    doc.replace(0, 0, '\n')
    assert marks.current(stale, doc) is None, 'computed before the document existed'
    fresh = Marks([(1, Mark('note'))], version=doc.version)
    doc.replace(0, 0, '\n\n')
    assert marks.current(fresh, doc).on(0, 9)[0][0] == 3 and follow(1, doc.since(doc.version - 1)[-1]) == 3


def test_string_state_and_colour_runs_come_from_the_source():
    tools()
    text = 'a = 1\nb = """x\ny\n"""\nc = 2\n' + 'd = 3\n' * (BLOCK * 6)
    ed = Editor(text)
    syntax, doc = ed.syntax, ed.doc
    assert list(syntax.states(0, 5)) == [0, 0, 1, 1, 0]
    assert syntax.runs('y', 1) == [(0, 'y', 'string_doc')] and syntax.runs('c = 2')[-1] == (4, '2', 'number')
    at = text.index('"""x')
    doc.replace(at, at + 3, '')
    ed.sync()
    assert list(syntax.states(2, 3)) == [0, 0, 1] and syntax.state(BLOCK * 3) == 1, 'the closer now opens a string through everything below'
    assert syntax.quiet(BLOCK * 3, 2) and not syntax.quiet(0, 2) and syntax.legacy(BLOCK * 3) == ('"""', 'string_doc')
    assert syntax.quiet(3, 2) and not syntax.quiet(3, 0) and not syntax.quiet(1, 4), 'inside the string that opens on line 3, not before it'
    doc.replace(doc.start(1), doc.start(1), "f = rf\'\'\'{a}  # no comment\n")
    ed.sync()
    assert list(syntax.states(1, 2)) == [0, 6] and syntax.runs('{a}', 6)[0] == (0, '{', 'fstring_delim'), 'an f-string keeps its fields below'
    assert Syntax(language=None).runs('x = 1') == [(0, 'x = 1', 'default')]


def test_typing_inside_a_line_is_read_again_when_the_source_lints():
    """An edit that keeps its line's structure cannot change the folds, so nothing is read for it; what the
    lint finds it can change."""
    source = Source('def area(side): pass\nvalue = area(1)\nother = 2\n')
    ed = Editor(source=source)
    ed.land()

    def type(at, text):
        ed.doc.select(at)
        ed.rest()
        ed.doc.replace(at, at, text)
        assert ed.sync()

    type(source.text.index('1)') + 1, ', 2')
    source.settle()
    assert not ed.land() and source.version != ed.doc.version, 'without lint the calm edit asks for nothing'
    source.lint = True
    type(source.text.index('= 2') + 3, ' + nowhere')
    source.settle()
    assert ed.land() and source.version == ed.doc.version
    assert source.problems == [(2, 'area() takes 1 positional argument but 2 were given'), (3, "name 'nowhere' is not defined")]


def test_the_string_state_far_down_is_known_once_the_text_was_read():
    source = Source('x = 1\n' * 5000 + 's = """\n' + 'y\n' * 5000)
    assert list(source.native.states(9000, 1)) == [255], 'the calling thread lexes a few chunks, never the file'
    source.ask()
    source.settle()
    assert list(source.native.states(9000, 2)) == [1, 1] and list(source.native.states(10, 1)) == [0]
    source.splice(0, 0, '"""', 1)
    assert list(source.native.states(3, 1)) == [1] and list(source.native.states(5001, 1)) == [255], 'an edit lexes the chunks near it again'
    source.ask()
    source.settle()
    assert list(source.native.states(5000, 2)) == [1, 0], 'and the reading the rest'


def test_analysis_takes_undefined_names_from_the_source_and_tells_it_the_live_names(monkeypatch):
    import time
    import types
    from meltygui.code import code_checks
    from meltygui.model import editor_model

    def wait(done):
        deadline = time.monotonic() + 20
        while not done() and time.monotonic() < deadline:
            time.sleep(.01)
        assert done()

    source = Source('import os\nprint(os.sep, missing, sys.argv)\nvalue = (\ndef area(side): pass\narea(1, 2)\nlen(1, 2)\n')
    analysis = editor_model.Analysis(source)
    try:
        source.ask(now=True)
        assert source.land() and source.lint
        assert (5, 'area() takes 1 positional argument but 2 were given') in analysis.errors, 'a misfitting call is the source\'s too'
        wait(lambda: analysis.analysed == source.native.version)
        assert (2, "name 'missing' is not defined") in analysis.errors and analysis.errors is analysis.errors
        # What a call reaches in the running process: the worker tells the source its signature, which is read again.
        wait(lambda: 'builtins.len' in analysis.signed and source.stale)
        source.stale = False
        source.ask(now=True)
        assert source.land() and (6, 'len() takes 1 positional argument but 2 were given') in analysis.errors
        assert any(line == 3 for line, _ in analysis.errors), 'the syntax error is the worker\'s'
        # Which import would bring a name is asked of the running process by the worker, once per name.
        assert analysis.imports == {} or analysis.imports == {2: ['import sys']}
        wait(lambda: 'sys' in analysis.suggested and 'missing' in analysis.suggested)
        assert analysis.imports == {2: ['import sys']} and analysis.imports is analysis.imports and analysis.suggested['missing'] == []
        # The running module turns out to have the name: told to the source, which is read again.
        module = types.ModuleType('running')
        module.missing = 1
        monkeypatch.setattr(code_checks, '_module_for', lambda path: module)
        source.stale = False        # (an editor does this when it asks again)
        editor_model.hotswapped()
        wait(lambda: source.stale)
        source.ask(now=True)
        assert source.land() and not any('missing' in message for _, message in analysis.errors)
    finally:
        analysis.close()


def test_the_worker_compiles_only_the_statement_that_changed(monkeypatch):
    import time
    from meltygui.model import editor_model
    compiled = []
    flaws = editor_model.flaws
    monkeypatch.setattr(editor_model, 'flaws', lambda text, path=None: compiled.append(text.lstrip('\n')) or flaws(text, path))

    def settled(source, analysis):
        source.ask(now=True)
        assert source.land()
        analysis.feed()
        deadline = time.monotonic() + 20
        while analysis.checked != source.version and time.monotonic() < deadline:
            time.sleep(.01)
        assert analysis.checked == source.version

    text = ''.join(f'def f{n}(a):\n    return a + {n}\n\n' for n in range(400))
    source = Source(text, version=1)
    analysis = editor_model.Analysis(source)
    try:
        settled(source, analysis)
        assert len(compiled) == 400 and analysis.syntax == [] and analysis.errors == []
        del compiled[:]
        at = text.index('return a + 250') + len('return a + 250')
        source.splice(at, at, ' +', 2)
        settled(source, analysis)
        assert 1 <= len(compiled) <= 40 and any('def f250' in block for block in compiled), 'the statements in the edited chunk, not the file'
        assert analysis.syntax == [(752, 'invalid syntax')] and (752, 'invalid syntax') in analysis.errors
        del compiled[:]
        source.splice(0, 0, '\n', 3)
        settled(source, analysis)
        assert analysis.syntax == [(753, 'invalid syntax')], 'the error is told on the line the statement stands on now'
        source.splice(at + 1, at + 3, '', 4)
        settled(source, analysis)
        assert analysis.syntax == [] and analysis.errors == []
    finally:
        analysis.close()


def test_findings_stay_on_their_text_until_the_worker_reads_it_again():
    from meltygui.editor.marks import Findings
    from meltygui.model import editor_model
    from meltygui.model.source_model import LOG
    text = 'a = 1\n\ndef f():\n    return 1 +\n\nb = nowhere\n'
    source = Source(text, version=1)
    analysis = editor_model.Analysis(source)
    analysis.close()        # no worker: `compile` is called here, so the test decides when the text is read again

    def landed():
        source.ask(now=True)
        assert source.land()
        return analysis.errors

    try:
        landed()
        analysis.compile()
        found = analysis.errors
        assert isinstance(found, Findings) and found.version == source.version
        assert found == [(4, 'invalid syntax'), (6, "name 'nowhere' is not defined")]
        # Lines typed above: the reading lands at once, the worker has not compiled yet.
        source.splice(0, 0, '\n\n', 2)
        found = landed()
        assert analysis.checked < source.version == found.version
        assert found == [(6, 'invalid syntax'), (8, "name 'nowhere' is not defined")], 'the syntax error moved with its line'
        # A line break inside the statement, before the broken line.
        at = source.text.index('    return')
        source.splice(at, at, '\n', 3)
        assert landed() == [(7, 'invalid syntax'), (9, "name 'nowhere' is not defined")]
        analysis.compile()
        assert analysis.syntax == [(7, 'invalid syntax')] and analysis.errors == [(7, 'invalid syntax'), (9, "name 'nowhere' is not defined")]
        # The broken line goes: so does its finding, before the worker looked again.
        at = source.text.index('    return')
        source.splice(at, at + len('    return 1 +\n'), '    pass\n', 4)
        assert landed() == [(9, "name 'nowhere' is not defined")]
        # More splices than the log keeps: what cannot be followed is dropped until the worker tells it again.
        source.splice(0, 0, 'c = 1 +\n', 5)
        landed()
        analysis.compile()
        assert (1, 'invalid syntax') in analysis.errors
        for version in range(6, 8 + 2 * LOG):
            source.splice(source.native.size, source.native.size, '\n', version)
        assert (1, 'invalid syntax') not in landed()
        analysis.compile()
        assert (1, 'invalid syntax') in analysis.errors
    finally:
        analysis.close()


def test_the_source_tells_its_models_of_every_splice_and_every_landed_reading():
    class Model:
        fed = 0

        def feed(self):
            self.fed += 1

    source, model = Source('a = 1\n', version=1), Model()
    source.models.add(model)
    source.splice(0, 0, 'b = 2\n', 2)
    assert model.fed == 1
    source.ask(now=True)
    assert source.land() and model.fed == 2 and not source.land() and model.fed == 2
    del model
    source.splice(0, 0, '\n', 3)       # a model that is gone is not held


SHARED = '''import os


class Shape:
    """A shape with sides."""
    sides = 4

    def area(self, scale=1):
        total = self.sides * scale
        if total > 10:
            return total
        return 0

    def name(self):
        return 'shape'


def build(count):
    found = []
    for at in range(count):
        found.append(Shape())
    return found


def main():
    shapes = build(3)
    print(len(shapes), os.sep)
'''


def same_as_fresh(ed, text):
    """The session's rows and search hits are those of a new session on `text` with the same folds collapsed."""
    fresh = Editor(text, fold_defaults=False, collapsed=sorted(ed.rows.collapsed(), key=repr))
    fresh.find(ed.term)
    fresh.source.settle()           # (a large text is read on the source's thread)
    fresh.land()
    lines = text.count('\n') + 1
    assert ed.doc.lines == lines and ed.rows.count == fresh.rows.count
    assert [(ed.rows.row(line), ed.rows.hidden(line)) for line in range(lines)] == [(fresh.rows.row(line), fresh.rows.hidden(line)) for line in range(lines)]
    hits = lambda session: [(line, mark.low, mark.high) for line, mark in session.marks['hits'].on(0, lines)] if session.marks['hits'] else []
    assert hits(ed) == hits(fresh)


def test_two_sessions_on_one_source_follow_each_other_through_edits_undos_and_redos(undo_stack, monkeypatch):
    """Both sessions see one text; each keeps its own folds, search hits and caret. After every step the rows
    and hits of each are those of a fresh session on that text, and the caret of the session that did not
    act still stands before the character it stood before."""
    from meltygui.core.runtime.toggles import Toggles
    monkeypatch.setattr(Toggles.CodeEditor, 'undo_typing_max_chars', 0)     # every edit its own step
    random.seed(5)
    source = Source(SHARED)
    sessions = [Editor(source=source, fold_defaults=False), Editor(source=source, fold_defaults=False)]
    sessions[0].find('shape')
    sessions[1].find('total')
    assert sessions[0].toggle(7) and sessions[1].toggle(17), 'each session folds for itself'
    text, ids, fresh = SHARED, list(range(len(SHARED))), len(SHARED)
    done, undone = [], []           # (text before, text after, who) of the steps on the stack, and of the ones undone
    anchors = [None, None]          # the ids around each caret: (before, after)
    kept = 0

    def anchor(which):
        caret = sessions[which].doc.caret
        anchors[which] = (ids[caret - 1] if caret else None, ids[caret] if caret < len(ids) else None)

    for which in (0, 1):
        sessions[which].doc.select(random.randrange(len(text)))
        anchor(which)
    from meltygui.core.rendering.core_decoration import Core
    frame = Core.melty.frame_count
    for step in range(400):
        # (Steps a frame apart: within one user action the framework undoes the steps of several views together.)
        monkeypatch.setattr(Core.melty, 'frame_count', frame + 10 * (step + 1))
        head, pick, who = source.head, random.random(), random.randrange(2)
        ed = sessions[who]
        if pick < .55 or not (done or undone):
            low = random.randrange(len(text) + 1)
            high = min(len(text), low + random.choice((0, 0, 0, 1, 3, 12)))
            new = random.choice(('x', 'value', '\n', '\n    pass\n', '', 'shape', ' total ', '(', '"""'))
            ed.land()
            if command(ed, lambda doc: doc.replace(low, high, new)) is None:
                anchor(who)         # (nothing changed; the caret went where the edit would have put it)
                continue
            after = text[:low] + new + text[high:]
            done.append((text, after, who))
            undone.clear()
            text = after
        elif pick < .75 and done:
            undo_stack.stack.undo()
            before, after, who = done.pop()
            undone.append((before, after, who))
            text = before
        elif pick < .9 and undone:
            undo_stack.stack.redo()
            before, after, who = undone.pop()
            done.append((before, after, who))
            text = after
        else:
            ed.land()
            line = random.randrange(ed.doc.lines)
            ed.toggle(line)
        for _, _, low, gone, came, _ in source.since(head):
            ids[low:low + gone] = range(fresh, fresh + came)
            fresh += came
        assert source.text == text and len(ids) == len(text), step
        for which, session in enumerate(sessions):
            session.land()
            session.read()
        sessions[0].land()
        for which, session in enumerate(sessions):
            assert session.doc.text == text and session.seen == source.head, (step, which)
            same_as_fresh(session, text)
            caret = session.doc.caret
            assert 0 <= caret <= len(text) and 0 <= session.doc.anchor <= len(text)
            if which != who and anchors[which] is not None:
                before, after = anchors[which]
                # (A caret whose line a fold now hides moves to the fold's header: `Editor.settle`.)
                spot = ids.index(after) if after in ids else ids.index(before) + 1 if before in ids else None
                if spot is not None and not session.rows.hidden(session.doc.line_of(spot)):
                    assert caret == spot, (step, which, 'the caret left its character')
                    kept += 1
            anchor(which)
    assert len(done) + len(undone) > 50 and kept > 200, (len(done), len(undone), kept)


def test_an_edit_from_outside_reaches_every_session_and_moves_their_undo_steps(undo_stack):
    source = Source(SHARED)
    one, two = Editor(source=source, fold_defaults=False), Editor(source=source, fold_defaults=False)
    woken = []
    one.wake, two.wake = (lambda: woken.append(1)), (lambda: woken.append(2))
    at = SHARED.index('sides = 4') + len('sides = 4')
    one.doc.select(at)
    command(one, edit.insert, '0')
    two.land()
    two.doc.select(source.text.index('def main'))
    assert woken == [2] and two.doc.text == source.text, 'the other session is woken for an edit, not the one that made it'
    # Someone who is no editor writes the text: a pending save, a `CodeDict`, a reload.
    del woken[:]
    source.splice(0, 0, '# header\n\n')
    assert sorted(woken) == [1, 2]
    for ed in (one, two):
        assert ed.land() is not None and ed.doc.text == source.text and ed.seen == source.head
    assert one.doc.slice(one.doc.caret - 10, one.doc.caret) == 'sides = 40' and two.doc.slice(two.doc.caret, two.doc.caret + 8) == 'def main'
    one.read()
    two.land()
    for ed in (one, two):
        same_as_fresh(ed, source.text)
    # The step made before the outside edit still undoes its own characters, where they are now.
    undo_stack.stack.undo()
    assert source.text == '# header\n\n' + SHARED and one.land() is not None and two.land() is not None
    assert one.doc.slice(one.doc.caret - 9, one.doc.caret) == 'sides = 4', 'the caret returns in the session that made the edit'
    assert two.doc.slice(two.doc.caret, two.doc.caret + 8) == 'def main', 'the other keeps its place'
    undo_stack.stack.redo()
    assert source.text == '# header\n\n' + SHARED.replace('sides = 4', 'sides = 40')
    # An outside edit that reaches into a step drops it: there is nothing exact left to undo.
    source.splice(source.text.index('sides = 40'), source.text.index('sides = 40') + 10, 'sides = 5')
    assert not undo_stack.stack.history and not undo_stack.stack.can_redo()


def test_a_session_closes_and_the_other_goes_on(undo_stack, monkeypatch):
    from meltygui.core.rendering.core_decoration import Core
    from meltygui.core.runtime.toggles import Toggles
    monkeypatch.setattr(Toggles.CodeEditor, 'undo_typing_max_chars', 0)     # every edit its own step
    frame = Core.melty.frame_count
    source = Source(SHARED)
    one, two = Editor(source=source, fold_defaults=False), Editor(source=source, fold_defaults=False)
    one.doc.select(0)
    command(one, edit.insert, 'A')
    two.land()
    two.doc.select(two.doc.size)
    monkeypatch.setattr(Core.melty, 'frame_count', frame + 10)      # (another user action than the one before)
    command(two, edit.insert, 'B')
    monkeypatch.setattr(Core.melty, 'frame_count', frame + 20)
    wakes = pytest.importorskip('meltygui.core.rendering._gui_native').Wakes()
    one.waker, two.waker = (wakes.handle(), 1), (wakes.handle(), 2)
    one.close()
    assert source.each() == [two]
    command(two, edit.insert, 'C')
    source.ask()
    source.settle()
    assert wakes.take() == [2] and two.land() and source.text == 'A' + SHARED + 'BC'
    for expected in ('A' + SHARED + 'B', 'A' + SHARED, SHARED):
        undo_stack.stack.undo()
        assert source.text == expected and two.land() is not None and two.doc.text == expected
    assert two.doc.caret == len(SHARED), 'the step of the session that closed undid the text; the caret of the other followed'
    two.read()
    same_as_fresh(two, SHARED)
    del one
    import gc
    gc.collect()
    assert source.each() == [two]


def test_a_reading_serves_every_session_with_its_own_folds_and_hits():
    text = SHARED * 300
    source = Source(text)
    one, two = Editor(source=source, fold_defaults=False), Editor(source=source, fold_defaults=False)
    source.settle()
    assert one.land() and two.land() and one.toggle(7) and two.toggle(17) and two.toggle(24)
    wakes = pytest.importorskip('meltygui.core.rendering._gui_native').Wakes()
    one.waker, two.waker = (wakes.handle(), 1), (wakes.handle(), 2)
    one.term, two.term = 'shape', 'total'
    one.ask()
    source.settle()
    assert sorted(wakes.take()) == [1, 2], 'one reading, every session woken'
    assert one.land() and two.land() and not one.land() and not two.land()
    assert len(one.marks['hits']) == 300 * SHARED.lower().count('shape') and len(two.marks['hits']) == 300 * SHARED.count('total')
    lines = text.count('\n') + 1
    assert two.rows.count < one.rows.count < lines, 'each session keeps its own folds'
    for ed in (one, two):
        same_as_fresh(ed, text)


def test_the_column_map_round_trips_over_added_cells(monkeypatch):
    from meltygui.editor import inline
    from meltygui.editor.syntax import shift, unshift
    random.seed(4)
    for _ in range(200):
        columns = sorted(random.sample(range(1, 40), random.randint(1, 5)))
        totals, total = [], 0
        for _ in columns:
            total += random.randint(1, 4)
            totals.append(total)
        inset = (columns, totals)
        for column in range(45):
            assert unshift(inset, column + shift(inset, column)) == column
        at = random.randrange(len(columns))
        inside = columns[at] + (totals[at - 1] if at else 0) + .4
        assert unshift(inset, inside) == columns[at], 'a point on added cells answers the column after them'
    tools()
    syntax = Syntax()
    assert syntax.insets('x = 1') is None
    monkeypatch.setitem(inline.WIDGETS, 'number', inline.Inline(None, False, 12, 2, 3))
    monkeypatch.setattr(inline, 'EPOCH', inline.EPOCH + 1)
    assert syntax.insets('x = 12 + 3') == ([4, 6, 9, 10], [2, 5, 7, 10]), 'lead before each number, trail after it'


def test_editor_follows_edits_at_once_and_takes_fresh_folds_from_the_source():
    tools()
    ed = Editor(SOURCE, fold_defaults=False)
    assert ed.rows.ranges()[-1] == (13, 14)
    ed.toggle(13)
    assert ed.rows.count == ed.doc.lines - 1
    edit.move(ed.doc, ed.doc.start(6))
    edit.enter(ed.doc)
    assert ed.sync() and ed.rows.closed() == [(14, 15)] and ed.rows.hidden(15), 'followed without a scan'
    assert not ed.land(), 'the fresh folds wait for the text to rest'
    ed.doc.replace(ed.doc.start(14), ed.doc.start(14) + 7, 'def low')
    ed.sync()
    ed.source.settle()
    assert ed.land() and ed.rows.closed() == [] and ('scope', 'low') in ed.rows.names()
    ed.find('return')
    assert [line for line, _ in ed.marks['hits'].on(0, 99)] == [11, 12]
    ed.doc.replace(0, 0, '\n')
    ed.sync()
    assert [line for line, _ in ed.marks['hits'].on(0, 99)] == [12, 13], 'hits follow until the search runs again'
    ed.load(ed.doc.text.replace('return 2', 'return 22'))
    ed.source.settle()
    ed.land()
    assert ed.doc.line(13) == '        return 22' and [mark.low for _, mark in ed.marks['hits'].on(13)] == [8]


def test_the_worker_tells_findings_on_the_lines_a_statement_stands_on_now(monkeypatch):
    """A statement is compiled on its own, and read for the attributes live modules lack, once: its findings
    are kept by its own lines and told where it stands, message included."""
    import time
    import types
    from meltygui.code import code_checks
    from meltygui.model import editor_model
    module = types.ModuleType('running')
    module.os = __import__('os')
    monkeypatch.setattr(code_checks, '_module_for', lambda path: module)

    def settled(source, analysis):
        source.ask(now=True)
        assert source.land()
        analysis.feed()
        deadline = time.monotonic() + 20
        while analysis.checked != source.version and time.monotonic() < deadline:
            time.sleep(.01)
        assert analysis.checked == source.version

    text = 'import os\n\ndef f():\n    return os.nothing_here\n\nvalue = (1,\n         2]\n'
    source = Source(text, 'running.py', version=1)
    analysis = editor_model.Analysis(source)
    try:
        settled(source, analysis)
        assert analysis.found == [(4, "module 'os' has no attribute 'nothing_here'")]
        assert analysis.syntax == [(7, "closing parenthesis ']' does not match opening parenthesis '(' on line 6")]
        assert set(analysis.found + analysis.syntax) <= set(analysis.errors)
        read = dict(analysis.compiled)
        source.splice(0, 0, '# a line above\n\n', 2)
        settled(source, analysis)
        assert analysis.found == [(6, "module 'os' has no attribute 'nothing_here'")]
        assert analysis.syntax == [(9, "closing parenthesis ']' does not match opening parenthesis '(' on line 8")]
        assert all(analysis.compiled[key] is read[key] for key in read if key in analysis.compiled), 'nothing that stayed was read again'
    finally:
        analysis.close()


def test_a_mark_set_that_shows_the_same_repaints_nothing():
    """Results arrive after every edit (errors, tints, spelling, breakpoints): the blocks are painted again
    only when the set shows something else on them."""
    ed = Editor('\n'.join(f'value_{n} = {n}' for n in range(400)))
    ed.window(0., 400.)
    error = lambda line: marks.Marks([(line, marks.Mark('error', data=('wrong', [])))])
    ed.put('errors', error(3))
    painted = ed.base
    ed.put('errors', error(3))
    assert ed.base == painted and ed.marks['errors'].on(3)[0][1].data == ('wrong', [])
    ed.put('errors', error(4))
    assert ed.base > painted
    painted = ed.base
    # Out of sight: the blocks there are painted when they are declared again.
    ed.put('notes', error(350))
    ed.put('notes', error(351))
    ed.put('notes', None)
    assert ed.base == painted and ed.marks['notes'] is None
    ed.put('notes', error(5))
    ed.put('notes', None)
    assert ed.base == painted + 2, 'a set that comes or goes in sight'


def test_a_worker_parks_while_a_frame_draws_and_the_render_thread_never(monkeypatch):
    """`glfw_utils.park`: what the native editor's Python workers call between pieces of their work."""
    import threading
    import time
    from meltygui.core.melty import Melty
    from meltygui.core.windowing import glfw_utils
    monkeypatch.setattr(glfw_utils, '_render_thread_id', threading.get_ident())
    monkeypatch.setattr(Melty, '_frame_draw_start', 0.0)
    assert glfw_utils.park() == 0, 'no frame: nothing to wait for'
    slept, results = [], []
    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            Melty._frame_draw_start = 0.0       # the frame ends
    monkeypatch.setattr(glfw_utils.time, 'sleep', sleep)
    monkeypatch.setattr(Melty, '_frame_draw_start', time.monotonic())
    assert glfw_utils.park() == 0 and not slept, 'never the render thread'
    thread = threading.Thread(target=lambda: results.append(glfw_utils.park()))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive() and slept == [glfw_utils.PARK_SLICE] * 3 and Melty._frame_draw_start == 0
    # A frame stamp that never ended (an aborted frame) parks nobody.
    monkeypatch.setattr(Melty, '_frame_draw_start', time.monotonic() - glfw_utils.PARK_STALE - 1)
    thread = threading.Thread(target=lambda: results.append(glfw_utils.park()))
    thread.start()
    thread.join(timeout=5)
    assert len(results) == 2 and len(slept) == 3
