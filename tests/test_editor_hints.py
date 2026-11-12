"""When the suggestion list and the signature hint show, and snippet tab stops (no window)."""
import pytest

from meltygui.core.runtime.toggles import Toggles, Snippet
from meltygui.editor import edit
from meltygui.editor.editor import Editor
from meltygui.editor.syntax import tools


def session(text, caret=None):
    tools()
    ed = Editor(text)
    ed.sync()
    ed.doc.select(len(text) if caret is None else caret)
    return ed


def type_in(ed, text, **options):
    for char in text:
        ed.step = None
        edit.insert(ed.doc, char)
        ed.step = None
        ed.sync()
        ed.suggest.refresh(ed, True, **options)


@pytest.fixture
def signatures(monkeypatch):
    """Signature lookups answer at once: (name, parameters, types) of whatever is called."""
    asked = []

    def lookup(bag, text, paren, caret, address):
        asked.append(paren)
        return tools()._callee_at(text, paren), ['first', 'second'], [None, None]
    monkeypatch.setattr(tools(), '_ensure_signature_help', lookup)
    return asked


def test_tab_hops_through_the_stops_of_an_accepted_snippet(monkeypatch):
    monkeypatch.setattr(Toggles.TextEditor, 'AC_SNIPPETS', {'#[': Snippet('tint', '# [tint=($0, $1, $2)] $3')})
    ed = session('x = 1\n\ny = 2\n', caret=6)
    type_in(ed, '#[')
    ed.suggest.accept(ed)
    assert ed.doc.line(1) == '# [tint=(, , )] ' and ed.doc.place(ed.doc.caret) == (1, 9)
    assert len(ed.suggest.stops) == 3
    type_in(ed, '0.25')
    assert ed.suggest.hop(ed.doc) and ed.doc.place(ed.doc.caret) == (1, 15), 'typing at a stop does not move the later ones'
    type_in(ed, '0.5')
    # An edit above the snippet does not move them either.
    caret = ed.doc.caret
    ed.doc.replace(0, 0, 'import os\n', caret=caret + 10)
    ed.sync()
    assert ed.suggest.hop(ed.doc) and ed.doc.place(ed.doc.caret) == (2, 20)
    assert ed.suggest.hop(ed.doc) and ed.doc.line(2)[:ed.doc.place(ed.doc.caret)[1]] == '# [tint=(0.25, 0.5, )] '
    assert not ed.suggest.hop(ed.doc), 'no stop left: Tab is Tab again'


def test_escape_and_a_click_abandon_the_stops_and_a_plain_pick_has_none(monkeypatch):
    monkeypatch.setattr(Toggles.TextEditor, 'AC_SNIPPETS', {'#[': Snippet('tint', '# [tint=($0, $1)]')})
    for leave in (lambda ed: ed.suggest.escape(ed), lambda ed: ed.suggest.drop()):
        ed = session('')
        type_in(ed, '#[')
        ed.suggest.accept(ed)
        assert ed.suggest.stops
        leave(ed)
        assert not ed.suggest.stops and not ed.suggest.hop(ed.doc)
    ed = session('def run(counter):\n    return ')
    type_in(ed, 'cou')
    ed.suggest.stops = [3]
    ed.suggest.accept(ed)
    assert ed.suggest.stops == []


def test_no_list_right_after_a_name_defining_keyword():
    for text in ('counter = 1\ndef ', 'counter = 1\nclass ', 'counter = 1\nfor ', 'counter = 1\nwith open(counter) as ', 'counter = 1\nf = lambda '):
        ed = session(text)
        type_in(ed, 'cou')
        assert not ed.suggest.open, text
    ed.suggest.refresh(ed, False, explicit=True)
    assert ed.suggest.open and ed.suggest.rows[0][0] == 'counter', 'Ctrl+Space asks anyway'
    for text in ('counter = 1\nreturn ', 'counter = 1\nwith ', 'counter = 1\nfor n in '):
        ed = session(text)
        type_in(ed, 'cou')
        assert ed.suggest.open and ed.suggest.rows[0][0] == 'counter', text


def test_escape_keeps_the_list_closed_at_its_site_until_the_caret_leaves_it():
    ed = session('counter = 1\ncount = 2\nx = ')
    type_in(ed, 'cou')
    assert ed.suggest.open
    ed.suggest.escape(ed)
    assert not ed.suggest.open and ed.suggest.quiet == ed.doc.caret - 3
    type_in(ed, 'n')
    assert not ed.suggest.open, 'typing on in the same word stays quiet'
    ed.suggest.refresh(ed, False, explicit=True)
    assert ed.suggest.open and ed.suggest.quiet is None, 'Ctrl+Space overrides'
    ed.suggest.escape(ed)
    type_in(ed, ' + co')
    assert ed.suggest.open and ed.suggest.quiet is None, 'another word is another site'
    # A caret that moved away and came back has left the site.
    ed.suggest.escape(ed)
    site = ed.doc.caret
    ed.suggest.leave(ed)
    assert ed.suggest.quiet is not None, 'still on the word'
    ed.doc.select(0)
    ed.suggest.leave(ed)
    assert ed.suggest.quiet is None
    ed.doc.select(site)
    type_in(ed, 'u')
    assert ed.suggest.open


def test_escape_at_a_snippet_site_keeps_its_list_closed(monkeypatch):
    monkeypatch.setattr(Toggles.TextEditor, 'AC_SNIPPETS', {'#[': Snippet('tint', '# [tint=($0)]')})
    ed = session('')
    type_in(ed, '#[')
    assert ed.suggest.rows == [('tint', 'snip')]
    ed.suggest.escape(ed)
    type_in(ed, 't')
    assert not ed.suggest.open


def test_the_signature_hint_shows_while_a_call_is_edited_not_on_a_caret_move(signatures):
    text = 'value = compute(1, 2) + other(3)\n'
    inside, second, beyond, other = text.index('1'), text.index('2'), text.index(' +'), text.index('3')
    ed, menu = session(text, inside), None
    menu = ed.suggest
    assert menu.hint(ed) is None and not signatures, 'a caret that was only placed in a call shows nothing and looks nothing up'
    type_in(ed, '4')
    assert menu.hint(ed, edited=True) == ('compute', ['first', 'second'], 0)
    ed.doc.select(second + 1)
    assert menu.hint(ed) == ('compute', ['first', 'second'], 1), 'it stays, on the argument the caret is in'
    ed.doc.select(beyond + 1)
    assert menu.hint(ed) is None
    ed.doc.select(second + 1)
    assert menu.hint(ed) is None, 'coming back by a caret move does not show it again'
    ed.doc.select(other + 1)
    assert menu.hint(ed, asked=True) == ('other', ['first', 'second'], 0), 'Ctrl+P asks for it'
    ed.doc.select(second + 1)
    assert menu.hint(ed) is None, 'another call than the one it showed for'


def test_escape_hushes_the_hint_for_that_call_and_a_click_disarms_it(signatures):
    text = 'value = compute(1, 2) + other(3)\n'
    ed = session(text, text.index('1'))
    menu = ed.suggest
    type_in(ed, '4')
    assert menu.hint(ed, edited=True)
    menu.escape(ed)
    assert menu.signature is None
    type_in(ed, '5')
    assert menu.hint(ed, edited=True) is None, 'further typing in the same call stays quiet'
    assert menu.hint(ed, asked=True), 'Ctrl+P brings it back'
    menu.escape(ed)
    ed.doc.select(ed.doc.size - 2)
    type_in(ed, '6')
    assert menu.hint(ed, edited=True)[0] == 'other', 'another call is not hushed'
    menu.owed = True
    menu.drop()
    assert menu.signature is None and menu.hint(ed) is None, 'a click inside the armed call shows nothing'
    menu.owed = True
    assert menu.hint(ed) and not menu.owed, 'an edit made while the list was open arms it when the list closes'
    type_in(ed, '7')
    assert menu.hint(ed, edited=True)


def test_a_finished_lookup_refreshes_the_list_only_when_it_was_the_members():
    ed = session('')
    menu = ed.suggest
    members, signature = object(), object()
    menu.bag._ac_jedi_future, menu.pending = members, members
    assert menu.arrived() and menu.pending is None
    menu.pending = signature
    assert not menu.arrived() and menu.pending is None
