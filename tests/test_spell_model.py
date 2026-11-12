"""The spell check model: unknown words as marks, read off the render thread once the text rests."""
import time

import pytest

from meltygui.core.runtime.toggles import Toggles
from meltygui.editor import edit, marks
from meltygui.editor.editor import Editor
from meltygui.editor.syntax import tools
from meltygui.model import spell_model
from meltygui.model.source_model import Source
from meltygui.model.spell_model import Spelling

TEXT = '# a smal comment about the editor\nvalue = 1\n# anothr line, correct words\n'


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(Toggles.TextEditor, 'enable_spell_check', True)
    monkeypatch.setattr(spell_model, 'QUIET', 0.02)


def rested(spelling, limit=30.):
    """Wait for the worker to have checked the text as it is now."""
    spelling.feed()
    until = time.monotonic() + limit
    while spelling.checked != spelling.source.native.version:
        assert time.monotonic() < until, 'the spell check did not finish'
        time.sleep(0.01)
    return spelling.marks


def words(found, source):
    return [(line, source.native.texts(line, 1)[0][mark.low:mark.high]) for line, mark in found.on(0, 10 ** 6)]


def test_nothing_is_checked_while_the_setting_is_off(monkeypatch):
    monkeypatch.setattr(Toggles.TextEditor, 'enable_spell_check', False)
    spelling = Spelling(Source(TEXT))
    spelling.feed()
    assert spelling.marks is None and spelling._thread is None, 'no thread, no dictionary'


def test_unknown_words_get_a_wavy_underline_tagged_with_the_version_read(on):
    source = Source(TEXT)
    spelling = Spelling(source)
    found = rested(spelling)
    assert words(found, source) == [(0, 'smal'), (2, 'anothr')]
    assert {mark.shape for _, mark in found.on(0, 9)} == {'squiggle'} and found.on(0)[0][1].color == (1., 0., 0.)
    assert found.version == source.native.version
    assert spelling.feed() is None and spelling.marks is found, 'a rested text is not read again'
    spelling.close()


def test_the_marks_follow_edits_and_a_new_set_arrives_once_the_text_rests(on):
    tools()
    ed = Editor(TEXT)
    spelling = Spelling(ed.source)
    first = rested(spelling)
    # Typed before the result reached the editor: the set is brought forward to the text as it is.
    edit.move(ed.doc, 0)
    edit.insert(ed.doc, 'x = 2\n')
    ed.sync()
    ed.produce('spelling', (first,), marks.given)
    assert words(ed.marks['spelling'], ed.source) == [(1, 'smal'), (3, 'anothr')]
    # Fixing a word: its mark shrinks with the text until the next reading drops it.
    low = ed.doc.index(1, 4)
    ed.doc.replace(low, low + 4, 'small')
    ed.sync()
    second = rested(spelling)
    assert second is not first and words(second, ed.source) == [(3, 'anothr')]
    ed.produce('spelling', (second,), marks.given)
    assert words(ed.marks['spelling'], ed.source) == [(3, 'anothr')]
    spelling.close()


def test_only_changed_lines_are_looked_up_again(on, monkeypatch):
    from meltygui.editor import spell_check
    looked, find = [], spell_check.find_misspellings
    monkeypatch.setattr(spell_check, 'find_misspellings', lambda text: looked.append(text) or find(text))
    source = Source(TEXT)
    spelling = Spelling(source)
    rested(spelling)
    assert sorted(looked) == sorted(line for line in TEXT.split('\n') if line)
    looked.clear()
    source.splice(source.native.size, source.native.size, 'a nwe line', source.native.version + 1)
    assert words(rested(spelling), source) == [(0, 'smal'), (2, 'anothr'), (3, 'nwe')] and looked == ['a nwe line']
    spelling.close()


def test_switching_the_setting_off_drops_the_marks_and_on_reads_again(on, monkeypatch):
    source = Source(TEXT)
    spelling = Spelling(source)
    assert rested(spelling) is not None
    from meltygui.core.windowing import glfw_utils
    asked = []
    monkeypatch.setattr(glfw_utils, 'request_render', lambda *args, **kwargs: asked.append(1))
    monkeypatch.setattr(Toggles.TextEditor, 'enable_spell_check', False)
    spelling.feed()
    spelling.feed()
    assert spelling.marks is None and asked == [1], 'a frame is asked for once, to take the underlines away'
    monkeypatch.setattr(Toggles.TextEditor, 'enable_spell_check', True)
    assert words(rested(spelling), source) == [(0, 'smal'), (2, 'anothr')]
    spelling.close()


def test_underlining_asks_the_word_list_and_never_builds_the_suggestion_index(on, monkeypatch):
    """Detection is a lookup in the dictionary's words (20 ms to load); the SymSpell index that suggestions
    need (over a second of interpreter time) is built by the first `suggest` only."""
    from meltygui.editor import spell_check
    monkeypatch.setattr(spell_check, '_sym', None)
    monkeypatch.setattr(spell_check, '_get_sym', lambda: pytest.fail('the suggestion index was built for underlining'))
    source = Source(TEXT)
    spelling = Spelling(source)
    assert words(rested(spelling), source) == [(0, 'smal'), (2, 'anothr')]
    spelling.close()
    assert [found[2] for found in spell_check.find_misspellings("It's a wrod, don't worry")] == ['wrod']
