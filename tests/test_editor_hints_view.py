"""Suggestion, hint, snippet and ghost keys through the native editor view, on a hidden GL window (reserved desktop)."""
import pytest

import meltygui.core.windowing.window_constants as glfw
import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.core.runtime.toggles import Toggles, Snippet
from meltygui.editor import syntax
from meltygui.state.core_undo import UndoManager
from meltygui.state.editor_state import EditorState

SOURCE = '''import os

LIMIT = 10


def first(value):
    if value:
        return max(value, 1)
    return 0


def second():
    total = 0
    for item in range(LIMIT):
        total += item
    return total
'''
SHIFT, CTRL, ALT = glfw.MOD_SHIFT, glfw.MOD_CONTROL, glfw.MOD_ALT
KEYS = {' ': glfw.KEY_SPACE, '.': glfw.KEY_PERIOD, '#': glfw.KEY_3, '[': glfw.KEY_LEFT_BRACKET, ',': glfw.KEY_COMMA}


@pytest.fixture(autouse=True)
def undo_stack():
    UndoManager.stack.history.clear()
    UndoManager.stack.redo_stack.clear()


@pytest.fixture
def editor():
    syntax.tools()
    state = EditorState()
    host = Host(view.draw_editor, input_value=SOURCE, name='editor', state=state, left=0., top=0.,
                width=700., height=400., fold_defaults=False)
    host.ed, host.state = state.editor, state
    yield host
    host.close()


@pytest.fixture
def signatures(monkeypatch):
    """Signature lookups answer at once."""
    monkeypatch.setattr(syntax.tools(), '_ensure_signature_help',
                        lambda bag, text, paren, caret, address: (syntax.tools()._callee_at(text, paren), ['first', 'second'], [None, None]))


def at(host, line, column):
    ed = host.ed
    return (ed.x(line, column) + .25 * ed.cw - ed.scroll_x, (ed.rows.row(line) + .5) * ed.lh - ed.pane.scroll_offset[1])


def declared(host, name):
    return [node for node, record in host.cache.records.items() if record[2].__name__ == name]


def keys(host, *presses, text=''):
    return host.frame(keys=[(press, 0) if isinstance(press, int) else press for press in presses], text=text)


def typed(host, text):
    for char in text:
        host.frame(keys=[(ord(char.upper()) if char.isalnum() else KEYS[char], 0)], text=char)
    return host.result


def new_line(host, line=15):
    """The caret on a fresh, indented line below `line`."""
    host.click(*at(host, line, 4))
    keys(host, glfw.KEY_END, glfw.KEY_ENTER)
    return host.ed.doc.line_of(host.ed.doc.caret)


def test_escape_keeps_the_list_closed_at_the_word_until_ctrl_space(editor):
    ed = editor.ed
    new_line(editor)
    typed(editor, 'tot')
    assert ed.suggest.open
    keys(editor, glfw.KEY_ESCAPE)
    typed(editor, 'a')
    assert not ed.suggest.open and not declared(editor, 'choices'), 'typing on at the site Escape closed'
    keys(editor, (glfw.KEY_SPACE, CTRL))
    assert ed.suggest.open
    keys(editor, glfw.KEY_ESCAPE)
    typed(editor, ' tot')
    assert ed.suggest.open and ed.suggest.rows[0][0] == 'total', 'the next word opens it again'
    # Leaving the word by a caret move forgets the Escape too.
    keys(editor, glfw.KEY_ESCAPE)
    keys(editor, glfw.KEY_UP)
    keys(editor, glfw.KEY_DOWN)
    typed(editor, 'a')
    assert ed.suggest.open


def test_no_list_while_naming_a_new_function(editor):
    ed = editor.ed
    editor.click(*at(editor, 3, 0))
    typed(editor, 'def fir')
    assert not ed.suggest.open
    keys(editor, (glfw.KEY_SPACE, CTRL))
    assert ed.suggest.open and 'first' in [name for name, _ in ed.suggest.rows]


def test_tab_hops_between_snippet_stops_until_escape_or_a_click(editor, monkeypatch):
    monkeypatch.setattr(Toggles.TextEditor, 'AC_SNIPPETS', {'#[': Snippet('tint', '# [tint=($0, $1)] $2')})
    ed = editor.ed
    line = new_line(editor)
    typed(editor, '#[')
    assert ed.suggest.rows == [('tint', 'snip')]
    keys(editor, glfw.KEY_ENTER)
    assert ed.doc.line(line) == '    # [tint=(, )] ' and ed.doc.place(ed.doc.caret) == (line, 13)
    typed(editor, '1')
    keys(editor, glfw.KEY_TAB)
    assert ed.doc.place(ed.doc.caret) == (line, 16) and ed.doc.line(line) == '    # [tint=(1, )] ', 'Tab hops, it does not indent'
    typed(editor, '2')
    keys(editor, glfw.KEY_TAB)
    assert ed.doc.place(ed.doc.caret) == (line, 20)
    keys(editor, glfw.KEY_TAB)
    assert ed.doc.line(line) == '    # [tint=(1, 2)]     ', 'no stop left: a plain tab'
    for abandon in (lambda: keys(editor, glfw.KEY_ESCAPE), lambda: editor.click(*at(editor, line, 2))):
        line = new_line(editor, line)
        typed(editor, '#[')
        keys(editor, glfw.KEY_ENTER)
        assert ed.suggest.stops
        abandon()
        assert not ed.suggest.stops
        keys(editor, glfw.KEY_END, glfw.KEY_TAB)
        assert ed.doc.line(line) == '    # [tint=(, )]   ', 'Tab pads to the next stop of four'


def test_the_signature_hint_follows_edits_ctrl_p_and_escape_not_clicks(editor, signatures):
    ed = editor.ed
    inside = at(editor, 7, len('        return max(value'))
    editor.click(*inside)
    assert ed.suggest.signature is None and not declared(editor, 'note'), 'a click into a call shows no hint'
    typed(editor, 's')
    assert ed.suggest.signature == ('max', ['first', 'second'], 0) and len(declared(editor, 'note')) == 1
    keys(editor, glfw.KEY_END, glfw.KEY_LEFT)
    assert ed.suggest.signature == ('max', ['first', 'second'], 1), 'a caret move inside the call keeps it'
    editor.click(*inside)
    assert ed.suggest.signature is None and not declared(editor, 'note'), 'a click disarms it, also inside its call'
    keys(editor, (glfw.KEY_P, CTRL))
    assert ed.suggest.signature is not None and len(declared(editor, 'note')) == 1, 'Ctrl+P asks for it'
    keys(editor, glfw.KEY_ESCAPE)
    assert ed.suggest.signature is None and not declared(editor, 'note')
    keys(editor, glfw.KEY_END, glfw.KEY_LEFT, glfw.KEY_BACKSPACE)
    assert ed.suggest.signature is None, 'editing the call Escape hushed stays quiet'
    keys(editor, glfw.KEY_DOWN)
    keys(editor, glfw.KEY_UP)
    keys(editor, glfw.KEY_BACKSPACE)
    assert ed.suggest.signature is not None, 'until the caret has left it'


def test_a_call_edited_under_the_open_list_shows_its_hint_when_the_list_closes(editor, signatures):
    ed = editor.ed
    editor.click(*at(editor, 7, len('        return max(value, 1')))
    typed(editor, ', va')
    assert ed.suggest.open and not declared(editor, 'note'), 'the list hides the hint'
    keys(editor, glfw.KEY_LEFT)
    assert not ed.suggest.open and ed.suggest.signature == ('max', ['first', 'second'], 2) and len(declared(editor, 'note')) == 1


def test_alt_enter_applies_the_only_import_and_opens_the_chooser_for_several(editor):
    ed = editor.ed
    editor.frame(errors=[(8, "name 'np' is not defined"), (13, "name 'json' is not defined")],
                 imports={8: ['import numpy as np', 'import numpy'], 13: ['import json']})
    editor.click(*at(editor, 12, 4))
    changed, text = keys(editor, (glfw.KEY_ENTER, ALT))
    assert changed and text.startswith('import os\nimport json\n'), 'one candidate is applied'
    editor.click(*at(editor, 8, 4))
    changed, text = keys(editor, (glfw.KEY_ENTER, ALT))
    assert not changed and ed.detail == 8, 'several candidates: nothing is applied yet'
    editor.frame()
    assert [row for row, _ in ed.fixes.rows] == ['import numpy as np', 'import numpy'] and len(declared(editor, 'choices')) == 1
    keys(editor, glfw.KEY_DOWN)
    changed, text = keys(editor, glfw.KEY_ENTER)
    assert changed and text.startswith('import os\nimport json\nimport numpy\n') and ed.detail is None


def test_all_of_a_ghost_is_shown_and_tab_inserts_what_is_shown(editor):
    ed = editor.ed
    line = new_line(editor)
    ghost = 'for n in range(3):\n    total += n\nreturn total'
    editor.frame(ghost=ghost)
    assert len(declared(editor, 'trail')) == 1
    rect, caret = (editor.cache.graph.rect(declared(editor, name)[0]) for name in ('trail', 'caret'))
    assert rect[:2] == (ed.gutter, caret[1] + ed.lh) and rect[3] == 2 * ed.lh, 'its other lines lie over the rows below the caret'
    assert caret[2] == len('for n in range(3):') * ed.cw + 4
    changed, text = keys(editor, glfw.KEY_TAB)
    assert text.split('\n')[line:line + 3] == ['    for n in range(3):', '    total += n', 'return total']
    editor.frame(ghost='only one line')
    assert not declared(editor, 'trail'), 'a one line ghost needs no further rows'
    editor.frame(ghost='')


def test_a_single_line_field_shows_and_inserts_the_first_line_of_a_ghost():
    host = Host(view.draw_editor, input_value='find ', name='field', left=0., top=0., width=300., height=22.,
                single_line=True, state=EditorState())
    try:
        host.click(40, 10)
        host.frame(keys=[(glfw.KEY_END, 0)], ghost='this\nand more')
        assert not declared(host, 'trail')
        changed, text = host.frame(keys=[(glfw.KEY_TAB, 0)])
        assert text == 'find this'
    finally:
        host.close()
