"""Search in the native editor without a window (editor/find.py): the hits by position (Rust), the
current hit, next and previous around the ends, what a caller reads, and Ctrl+Shift+B's part."""
import meltygui.editor.editor as session
from meltygui.editor import lookup
from meltygui.editor.editor import Editor
from meltygui.editor.find import Search
from meltygui.editor.marks import Mark
from meltygui.editor.syntax import tools
from meltygui.model.source_model import Source

TEXT = '''total = 0
for item in range(3):
    total += item
print(Total, 'total')
subtotal = total
'''


def editor(text=TEXT, line=0, column=0):
    tools()
    ed = Editor(text)
    ed.doc.select(ed.doc.index(line, column))
    return ed


def current(ed):
    return ed.search.current(ed)


def test_hits_answer_by_position_without_being_listed():
    source = Source(TEXT)
    source.ask('total', now=True)
    assert source.land()
    hits = source.native.hits('total')
    whole = hits.on(0, 99)
    assert whole == [(0, 0, 5), (2, 4, 9), (3, 6, 11), (3, 14, 19), (4, 3, 8), (4, 11, 16)], 'case blind, none overlapping'
    assert [hits.at(index) for index in range(len(hits))] == whole and hits.at(6) is None
    assert [hits.seek(*spot) for spot in ((0, 0), (0, 1), (2, 4), (3, 7), (3, 14), (4, 12), (9, 0))] == [0, 1, 1, 3, 3, 6, 6]
    hits.follow(1, 0, 0, 0, 2, 0)       # two lines typed above the second hit
    assert hits.at(1) == (4, 4, 9) and hits.seek(3, 0) == 1 and hits.seek(4, 5) == 2
    source.close()


def test_next_and_previous_go_from_the_caret_around_the_ends_and_the_caret_stays():
    ed = editor(line=3, column=8)
    find = ed.search
    find.seek(ed, 'total')
    assert find.owed and current(ed) is None
    find.settle(ed)
    assert current(ed) == (3, 14) and not find.owed, 'a new term: the first hit at or after the caret'
    assert ed.landing == (ed.doc.index(3, 14), ed.doc.index(3, 19), True) and ed.marks['found'].on(3) == [(3, Mark('found', 14, 19))]
    assert find.state(ed) == Search('total', 4, 6) == ed.handle.search
    caret = ed.doc.caret
    assert [find.step(ed, 1) and find.state(ed).index for _ in range(4)] == [5, 6, 1, 2], 'next wraps to the first'
    assert [find.step(ed, -1) and find.state(ed).index for _ in range(3)] == [1, 6, 5], 'previous wraps to the last'
    assert ed.doc.caret == caret == ed.doc.anchor, 'stepping moves neither the caret nor the selection'
    # Without a current hit, previous is the last one before the caret.
    ed.put('found', None)
    assert find.step(ed, -1) and current(ed) == (3, 6)
    # Nothing found: no current hit, and the caller reads so.
    find.seek(ed, 'absent')
    find.settle(ed)
    assert not find.step(ed, 1) and find.state(ed) == Search('absent', 0, 0) and ed.marks['found'] is None
    find.seek(ed, '')
    assert find.state(ed) == Search('', 0, 0) and not find.owed and ed.marks['hits'] is None


def test_the_current_hit_stays_on_its_text_and_an_edit_that_takes_it_leaves_none():
    ed = editor()
    find = ed.search
    find.seek(ed, 'total')
    find.settle(ed)
    find.step(ed, 1)
    assert current(ed) == (2, 4)
    ed.doc.splice(0, 0, '# first\n')
    ed.sync()
    assert current(ed) == (3, 4) and find.state(ed).index == 2, 'a line typed above: the hit and its number follow'
    low = ed.doc.index(3, 4)
    ed.doc.splice(low, low + 5, 'sum')
    ed.sync()
    assert current(ed) is None and find.state(ed) == Search('total', 0, 5)
    assert find.step(ed, 1) and current(ed) == (1, 0), 'no current hit any more: the first from the caret'


def test_a_term_typed_into_a_large_text_is_read_once_it_rests(monkeypatch):
    monkeypatch.setattr(session, 'INLINE', 0)
    ed = editor()
    ed.source.settle()
    ed.land()
    asked, ask, stamp = [], ed.source.ask, ed.source.stamp
    monkeypatch.setattr(ed.source, 'ask', lambda term='', rest=0., now=False, rows=None: asked.append((term, rest, now)) or ask(term, rest, now, rows))
    for term in ('t', 'to', 'tot'):
        ed.search.seek(ed, term)
        ed.search.settle(ed)
    assert asked == [('t', session.QUIET, False), ('to', session.QUIET, False), ('tot', session.QUIET, False)], 'never read on the calling thread'
    assert ed.marks['hits'] is None and current(ed) is None and ed.search.owed, 'nothing to land on yet'
    ed.source.settle()
    ed.land()
    assert ed.source.stamp == stamp + 1 and len(ed.marks['hits']) == 6, 'the three requests were one reading'
    ed.search.settle(ed)
    assert current(ed) == (0, 0) and ed.search.state(ed) == Search('tot', 1, 6)


def test_ctrl_shift_b_hands_the_carets_file_line_to_the_external_editor(monkeypatch):
    opened = []
    monkeypatch.setattr(lookup, 'external', lambda path, line: opened.append((path, line)))
    tools()
    ed = Editor('', source=Source(TEXT, '/project/sample.py'))
    ed.doc.select(ed.doc.index(2, 3))
    ed.lookup.outside(ed)
    ed.numbers = session.Numbers(40)
    ed.lookup.outside(ed, '/project/other.py')
    assert opened == [('/project/sample.py', 3), ('/project/other.py', 42)], 'the file line, as the gutter shows it; the view\'s path first'
    lone = editor(line=3, column=7)
    lone.lookup.outside(lone)
    assert len(opened) == 2 and lone.marks['flash'].on(3) == [(3, Mark('flash', 6, 11, lookup.MISS))], 'no file: the word flashes'


def test_a_reading_for_a_search_term_is_not_analysed_again():
    """A term change makes a reading of the same text: the `Analysis` worker must not go over its
    statements again (at 100k lines that pass cost a frame its budget, session benchmark `lookup#38`)."""
    from meltygui.model.editor_model import Analysis
    source = Source(''.join(f'def f{n}(a):\n    return a + {n}\n\n' for n in range(50)), version=1)
    analysis = Analysis(source)
    analysis.close()
    try:
        source.ask(now=True)
        assert source.land()
        analysis.compile()
        first = analysis.compiled
        assert len(first) == 50 and analysis.passed == (source.version, None, False)
        source.ask('return', now=True)
        assert source.land() and len(source.native.hits('return')) == 50
        analysis.compile()
        assert analysis.compiled is first, 'the same text: the pass is skipped'
        source.splice(0, 0, 'value = 1\n', 2)
        source.ask('return', now=True)
        assert source.land()
        analysis.compile()
        assert analysis.compiled is not first and len(analysis.compiled) == 51 and analysis.checked == source.version
    finally:
        source.close()
