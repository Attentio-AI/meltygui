"""Ctrl+B of the native editor without a window (editor/lookup.py): what a place leads to as plain
rows, from the source alone and from the project's roster, and the session's part over an `Editor`."""
import time
from pathlib import Path

import pytest

from meltygui.editor import lookup
from meltygui.editor.editor import Editor
from meltygui.editor.marks import Mark
from meltygui.editor.rows import Numbers
from meltygui.editor.syntax import tools
from meltygui.model.roster_model import Roster
from meltygui.model.source_model import Source

FILES = {
    'shapes.py': '''class Circle:
    def area(self):
        return 3


def square(side):
    return side * side


def lonely():
    return square(2) + square(3)
''',
    'scene.py': '''from shapes import Circle, square

SIZE = square(4)


def build(count):
    rings = [Circle(index) for index in range(count)]
    return rings, square(count), SIZE, lonely()


def again(count):
    return build(count), build(count + 1)
''',
}


@pytest.fixture
def made(tmp_path):
    tools()
    for name, text in FILES.items():
        (tmp_path / name).write_text(text)
    roster = Roster(str(tmp_path), paths=[str(tmp_path / name) for name in FILES], roots=[str(tmp_path)])
    roster.settle()
    yield roster, tmp_path
    roster.close()


def opened(path, text=None):
    source = Source(Path(path).read_text() if text is None else text, str(path))
    source.ask(now=True)
    assert source.land()
    return source


def place(text, word, nth=0):
    """(line, column) of the `nth` occurrence of `word`."""
    at = -1
    for _ in range(nth + 1):
        at = text.index(word, at + 1)
    return text.count('\n', 0, at), at - text.rfind('\n', 0, at) - 1


def brief(found):
    return found.definition, [(item.path and Path(item.path).name, item.number, item.column, item.text) for item in found.places]


def test_the_source_alone_answers_for_the_symbols_and_locals_of_its_text():
    text = FILES['scene.py']
    source = opened('scene.py', text)
    seek = lambda word, nth=0: lookup.seek(source, None, *place(text, word, nth))
    assert brief(seek('build(count)', 1)) == (False, [(None, 6, 4, 'def build(count):')]), 'a use: its definition, in the buffer (no path)'
    assert brief(seek('build')) == (True, [(None, 12, 11, 'return build(count), build(count + 1)'), (None, 12, 25, 'return build(count), build(count + 1)')])
    assert seek('build').places[0] == lookup.Place(None, 11, 11, 5, False, 12, 'return build(count), build(count + 1)'), 'lines from 0, the number shown from 1'
    assert brief(seek('rings', 1)) == (False, [(None, 7, 4, 'rings = [Circle(index) for index in range(count)]')]), 'a local: from a use to its binding'
    assert brief(seek('count', 2)) == (False, [(None, 6, 10, 'def build(count):')]), 'a parameter likewise'
    assert seek('rings') is None, 'the binding of a local leads nowhere: its uses are no fact'
    assert brief(seek('square', 1)) == (False, [(None, 1, 27, 'from shapes import Circle, square')]), 'without a roster an imported name leads to its import'
    assert seek('return') is None and lookup.seek(source, None, 4, 0) is None and seek('again').places == [], 'a keyword, a blank line, a definition nobody uses'
    part = Numbers(40)
    assert lookup.seek(source, None, *place(text, 'build(count)', 1), part).places[0].number == 45, 'a buffer that is a part of its file shows file lines'
    source.close()


def test_the_roster_answers_across_files_and_names_the_buffers_own_places_first(made):
    roster, folder = made
    scene, shapes = opened(folder / 'scene.py'), opened(folder / 'shapes.py')
    roster.join(scene)
    roster.settle()
    text = FILES['scene.py']
    found = lookup.seek(scene, roster, *place(text, 'Circle(index)'))
    assert brief(found) == (False, [('shapes.py', 1, 6, 'class Circle:')]) and found.places[0].path == str(folder / 'shapes.py')
    assert lookup.seek(scene, roster, *place(text, 'lonely')).places[0].guess, 'a guess says so'
    assert brief(lookup.seek(scene, roster, *place(text, 'SIZE', 1))) == (False, [(None, 3, 0, 'SIZE = square(4)')]), 'the buffer\'s own symbol has no path'
    found = lookup.seek(shapes, roster, *place(FILES['shapes.py'], 'square'))
    assert brief(found) == (True, [
        (None, 11, 11, 'return square(2) + square(3)'), (None, 11, 23, 'return square(2) + square(3)'),
        ('scene.py', 1, 27, 'from shapes import Circle, square'), ('scene.py', 3, 7, 'SIZE = square(4)'), ('scene.py', 8, 18, 'return rings, square(count), SIZE, lonely()'),
    ]), 'the uses of a definition: the buffer\'s first, then each file\'s with the text of its line'
    # The other file is open and edited, not saved: its lines are read from its source, never from the disk.
    at = text.index('SIZE')
    scene.splice(at, at, '# a new line\n')
    scene.ask(now=True)
    assert scene.land()
    roster.settle()
    found = lookup.seek(shapes, roster, *place(FILES['shapes.py'], 'square'))
    assert brief(found)[1][3] == ('scene.py', 4, 7, 'SIZE = square(4)')
    scene.close(), shapes.close()


def test_a_long_list_is_cut_and_counted(made, monkeypatch):
    roster, folder = made
    shapes = opened(folder / 'shapes.py')
    monkeypatch.setattr(lookup, 'MOST', 3)
    found = lookup.seek(shapes, roster, *place(FILES['shapes.py'], 'square'))
    assert len(found.places) == 3 and found.more == 2 and [item.path for item in found.places[:2]] == [None, None]
    shapes.close()


def test_lines_are_picked_without_splitting_the_text_and_brought_forward_over_edits():
    text = ''.join(f'line {at}\n' for at in range(5000)) + 'last'
    assert lookup.pick(text, [0, 7, 4999, 5000, 9000]) == {0: 'line 0', 7: 'line 7', 4999: 'line 4999', 5000: 'last'}
    ed = Editor('a\nb\nc\nd\n')
    version = ed.doc.version
    ed.doc.splice(2, 2, 'x\ny\n')
    assert [lookup.ahead(ed.doc, version, line) for line in range(4)] == [0, 1, 4, 5] and lookup.ahead(ed.doc, ed.doc.version, 3) == 3


def session(source, word, nth=0):
    ed = Editor('', source=source)
    ed.doc.select(ed.doc.index(*place(source.text, word, nth)))
    return ed


def answered(ed, roster=None, on_open=None):
    """Ctrl+B and its answer, as the view sequences them."""
    ed.lookup.ask(ed, roster)
    ed.lookup.pending.result()
    return ed.lookup.arrive(ed, roster, on_open)


def test_one_place_is_jumped_to_and_several_open_the_picker(made):
    roster, folder = made
    scene = opened(folder / 'scene.py')
    ed = session(scene, 'build(count)', 1)
    assert answered(ed, roster) and ed.doc.place(ed.doc.caret) == (5, 4) and ed.landing == (ed.doc.caret, ed.doc.caret + 5, True)
    assert not ed.lookup.open and ed.lookup.pending is None
    # On the definition: two uses, the picker.
    ed.landing = None
    assert not answered(ed, roster) and ed.lookup.open and ed.landing is None
    assert ed.lookup.rows == [('return build(count), build(count + 1)', 'scene.py:12')] * 2 and ed.lookup.cells == 37 + 11 + 4
    ed.lookup.step(1)
    ed.lookup.picked = ed.lookup.index
    assert ed.lookup.take(ed, None) and ed.doc.place(ed.doc.caret) == (11, 25) and not ed.lookup.open
    scene.close()


def test_a_place_in_another_file_goes_to_the_caller(made):
    roster, folder = made
    scene = opened(folder / 'scene.py')
    ed, opened_at = session(scene, 'Circle(index)'), []
    caret = ed.doc.caret
    assert not answered(ed, roster, lambda *where: opened_at.append(where))
    assert opened_at == [(str(folder / 'shapes.py'), 1, 6)] and ed.doc.caret == caret and ed.marks['flash'] is None
    # Without a caller for it the place is not jumped to: the name flashes.
    assert not answered(ed, roster) and ed.doc.caret == caret
    assert ed.marks['flash'].on(6) == [(6, Mark('flash', 13, 19, lookup.MISS))]
    scene.close()


def test_nothing_to_jump_to_flashes_the_word_and_a_moved_caret_drops_the_answer(made):
    roster, folder = made
    scene = opened(folder / 'scene.py')
    ed = session(scene, 'again')
    assert not answered(ed, roster) and ed.marks['flash'].on(10) == [(10, Mark('flash', 4, 9, lookup.MISS))] and ed.flash > time.monotonic()
    ed.put('flash', None)
    ed.doc.select(ed.doc.index(*place(scene.text, 'build(count)')))
    ed.lookup.ask(ed, roster)
    ed.lookup.pending.result()
    ed.doc.select(0)
    assert not ed.lookup.arrive(ed, roster, None) and ed.doc.caret == 0 and ed.marks['flash'] is None and ed.lookup.pending is None
    scene.close()


def test_a_press_on_text_typed_since_the_reading_waits_for_its_reading(made, monkeypatch):
    roster, folder = made
    scene = opened(folder / 'scene.py')
    ed = session(scene, 'build(count)')
    ed.land()
    # A large text reads on the source's thread: the press is owed until that reading landed.
    monkeypatch.setattr('meltygui.editor.editor.INLINE', 0)
    ed.doc.splice(0, 0, '# typed\n')
    ed.sync()
    ed.doc.select(ed.doc.index(*place(scene.text, 'build(count)', 1)))
    ed.lookup.ask(ed, roster)
    assert ed.lookup.owed and ed.lookup.pending is None and not ed.lookup.arrive(ed, roster, None)
    scene.settle()
    ed.land()
    assert not ed.lookup.arrive(ed, roster, None) and not ed.lookup.owed, 'sent once the reading is the text\'s'
    ed.lookup.pending.result()
    assert ed.lookup.arrive(ed, roster, None) and ed.doc.place(ed.doc.caret) == (6, 4)
    scene.close()
