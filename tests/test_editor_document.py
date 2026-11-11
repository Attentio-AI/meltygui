"""Document, edit commands and the line to row mapping of the native editor."""
import random

import pytest

from meltygui.editor import edit, marks
from meltygui.editor.document import Document, Splice, BLOCK
from meltygui.editor.editor import Editor
from meltygui.editor.marks import Lines, Marks, Mark
from meltygui.editor.rows import Rows, Folds
from meltygui.editor.syntax import Syntax, tools, outline
import meltygui.editor.syntax as syntax_module


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
    blocks = list(doc.blocks)
    assert len(blocks) == 11 and all(block.count == BLOCK for block in blocks[:-1]) and blocks[-1].text == ''
    at = doc.start(BLOCK * 3 + 5)
    doc.dirty.clear()
    doc.replace(at, at, 'new\n')
    assert doc.blocks == blocks and doc.dirty == [blocks[3]], 'one block rewritten, none replaced'
    assert blocks[3].count == BLOCK + 1 and doc.first(4) == BLOCK * 4 + 1 and doc.line(BLOCK * 4 + 1) == f'line {BLOCK * 4}'
    assert len(doc.line0) <= 5, 'bases below the edit are found again only when asked for'
    doc.replace(at, at, 'x\n' * (BLOCK * 3))
    assert doc.blocks[3] is blocks[3] and len(doc.blocks) == 15 and max(block.count for block in doc.blocks) <= BLOCK * 2
    assert doc.blocks[-4:] == blocks[-4:] and doc.text == text[:at] + 'x\n' * (BLOCK * 3) + 'new\n' + text[at:]
    assert set(doc.keys) == {block.key for block in doc.blocks}
    doc.replace(0, doc.size, '')
    assert len(doc.blocks) == 1 and doc.lines == 1 and doc.text == ''


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
            assert all(block.count == block.text.count('\n') + (block is doc.blocks[-1]) for block in doc.blocks)
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
    assert len(ed.doc.blocks) == 1 and not doc.changes
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
    ed.closed = True
    undo_stack.undo()
    undo_stack.undo()
    assert other.doc.text == 'other' and doc.text == 'zero\none\n2two\nTHR\n', 'a closed session ignores its steps'


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
    tools()
    folds = Folds()
    heads, spans, parents, names, scopes, initial = outline(text)
    folds.set(heads, spans, parents, names, scopes, initial if defaults else ())
    return folds


def ranges(folds):
    return [folds.range(at) for at in range(len(folds)) if folds.names[at] is not None]


def test_folds_toggle_by_head_line_and_survive_line_shifts_by_key():
    folds = folds_of(SOURCE)
    assert folds.range(folds.at(6)) == (6, 11) and folds.range(folds.at(8)) == (8, 11) and folds.at(7) is None
    folds.flip(folds.at(8))
    assert folds.ranges() == [(8, 11)] and folds.is_closed(folds.at(8))
    rows = Rows(SOURCE.count('\n') + 1, folds.ranges())
    assert rows.folded(8) == 3 and rows.row(13) == 10 and rows.before(13) == 10 and rows.before(10) == 9
    heads, spans, parents, names, scopes, _ = outline('# new first line\n' + SOURCE)
    folds.set(heads, spans, parents, names, scopes)
    assert folds.ranges() == [(9, 12)], 'the collapsed scope moved with its content'
    assert folds.reveal(11) and folds.ranges() == []
    assert [folds.range(at) for at in folds.around(11)] == [(10, 11), (9, 12), (7, 12)]
    assert [folds.range(at)[0] for at in folds.within(8, 10)] == [7, 9, 10]


def test_default_folds_seed_once_and_collapse_all_targets_outer_ranges():
    folds = folds_of(SOURCE, defaults=True)
    assert folds.ranges() == [(0, 1), (3, 4)]
    folds.flip(folds.at(3))
    heads, spans, parents, names, scopes, initial = outline(SOURCE)
    folds.set(heads, spans, parents, names, scopes, initial)
    assert folds.ranges() == [(0, 1)], 'an opened default stays open on the next scan'
    folds.set_all(True)
    assert folds.ranges() == [(0, 1), (3, 4), (6, 11), (13, 14)]
    folds.set_all(False)
    assert folds.ranges() == []


def test_folds_follow_random_edits_like_a_fresh_scan_for_untouched_ranges():
    """Between scans the ranges follow edits: every range the edit left
    alone must be exactly where a fresh scan finds it."""
    tools()
    random.seed(5)
    base = ''.join(f'class A{n}:\n    x = 1\n    def f(self, a):\n        if a == {n}:\n            return 1\n'
                   f'        return 2\n\ndef top{n}():\n    pass\n\n' for n in range(8))
    for _ in range(120):
        doc = Document(base)
        folds = folds_of(base)
        before = {folds.names[at]: folds.range(at) for at in range(len(folds))}
        line = random.randint(0, doc.lines - 1)
        low = doc.start(line) + random.choice([0, 0, len(doc.line(line))])
        high = low if random.random() < .6 else min(doc.size, doc.start(min(line + random.randint(1, 4), doc.lines - 1)))
        change = doc.splice(low, high, random.choice(['\n', '\n\n', '', 'pass\n', '    y = 2\n    z = 3\n']))
        folds.follow(change)
        fresh = folds_of(doc.text)
        after = {fresh.names[at]: fresh.range(at) for at in range(len(fresh))}
        first, last = change.line, change.line + change.gone
        checked = 0
        for at, name in enumerate(folds.names):
            old = before.get(name)
            # Compare where the edit did not change what the range holds.
            if name in after and (old[1] < first or old[0] > last) and after[name][1] - after[name][0] == old[1] - old[0]:
                assert folds.range(at) == after[name], (name, change)
                checked += 1
        assert checked >= len(before) - 8 and [folds.heads.line(at) for at in range(len(folds))] == sorted(
            folds.heads.line(at) for at in range(len(folds)))


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
        found = marks.hits(text, 'alpha', doc.version)
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
            notes.follow(change)
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
    assert marks.current(fresh, doc).on(0, 9)[0][0] == 3 and follow(1, doc.log[-1]) == 3


def test_syntax_settles_string_state_block_by_block():
    tools()
    text = 'a = 1\nb = """x\ny\n"""\nc = 2\n' + 'd = 3\n' * (BLOCK * 6)
    doc, syntax = Document(text), Syntax()
    assert syntax.state(doc, 2) is None and syntax.reach(doc, 0)[1]
    assert syntax.state(doc, 2)[0] == '"""' and syntax.state(doc, 3)[0] == '"""' and syntax.state(doc, 4) is None
    assert syntax.tokens('y', syntax.state(doc, 2))[0][1].startswith('string')
    assert [kind for _, kind in syntax.tokens('c = 2')][-1] == 'number'
    assert syntax.reach(doc, len(doc.blocks)) == ([], True) and syntax.frontier == len(doc.blocks)
    at = text.index('"""x')
    doc.replace(at, at + 3, '')
    syntax.edited(doc.low)
    changed, done = syntax.reach(doc, len(doc.blocks))
    assert done and changed == doc.blocks, 'the closer now opens a string through every block below'
    assert syntax.state(doc, 2) is None and syntax.state(doc, 4)[0] == '"""'
    doc.replace(doc.start(1), doc.start(1), 'e = 5  ')
    syntax.edited(doc.low)
    assert syntax.reach(doc, len(doc.blocks)) == ([doc.blocks[0]], True), 'an edit that keeps the string state leaves the blocks below alone'
    assert syntax.state(doc, BLOCK * 3)[0] == '"""', 'the open string runs through the blocks below'
    assert syntax.quiet(doc, doc.start(BLOCK * 3) + 2) is True and syntax.quiet(doc, 2) is False
    assert Syntax(language=None).tokens('x = 1') == [('x = 1', 'default')]


def test_a_long_string_state_change_is_lexed_a_slice_a_frame(monkeypatch):
    tools()
    monkeypatch.setattr(syntax_module, 'SLICE', 4)
    doc, syntax = Document('x = 1\n' * (BLOCK * 20)), Syntax()
    assert syntax.reach(doc, 2)[1] and syntax.frontier == 3, 'only as far as asked'
    doc.replace(0, 0, '"""')
    syntax.edited(doc.low)
    rounds = 0
    while not syntax.reach(doc, len(doc.blocks) - 1)[1]:
        rounds += 1
    assert rounds == 5 and syntax.state(doc, doc.lines - 2)[0] == '"""'


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


def test_editor_follows_edits_at_once_and_takes_fresh_folds_from_the_worker():
    tools()
    ed = Editor(SOURCE, fold_defaults=False)
    assert [ed.folds.range(at) for at in range(len(ed.folds))][-1] == (13, 14)
    ed.toggle(13)
    assert ed.rows.count == ed.doc.lines - 1
    edit.move(ed.doc, ed.doc.start(6))
    edit.enter(ed.doc)
    assert ed.sync() and ed.folds.ranges() == [(14, 15)] and ed.rows.hidden(15), 'followed without a scan'
    assert ed.worker.jobs, 'the rescan waits for the worker'
    ed.doc.replace(ed.doc.start(14), ed.doc.start(14) + 7, 'def low')
    ed.sync()
    ed.worker.settle()
    assert ed.land() and ed.folds.ranges() == [] and ('scope', 'low') in ed.folds.names
    ed.find('return')
    assert [line for line, _ in ed.marks['hits'].on(0, 99)] == [11, 12]
    ed.doc.replace(0, 0, '\n')
    ed.sync()
    assert [line for line, _ in ed.marks['hits'].on(0, 99)] == [12, 13], 'hits follow until the search runs again'
    ed.load(ed.doc.text.replace('return 2', 'return 22'))
    ed.worker.settle()
    ed.land()
    assert ed.doc.line(13) == '        return 22' and [mark.low for _, mark in ed.marks['hits'].on(13)] == [8]
