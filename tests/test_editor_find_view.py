"""Search in the native editor view, on a hidden GL window (reserved desktop): Ctrl+F and the find
bar, stepping, what a caller's own field gets, Ctrl+Shift+B, and typing after Ctrl+End."""
import pytest

import meltygui.core.windowing.window_constants as glfw
import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.editor import lookup, syntax
from meltygui.editor.find import Search
from meltygui.editor.marks import Mark
from meltygui.model.source_model import Source
from meltygui.state.editor_state import EditorState
from test_editor_view import SOURCE, at, declared, keys, typed

SHIFT, CTRL = glfw.MOD_SHIFT, glfw.MOD_CONTROL
LINES = SOURCE.split('\n')
# `return` stands on these lines: three near the top, then one in each of the 40 functions below.
RETURNS = [line for line, text in enumerate(LINES) if 'return' in text]


@pytest.fixture
def editor():
    syntax.tools()
    state = EditorState()
    host = Host(view.draw_editor, input_value=Source(SOURCE, '/project/sample.py'), name='editor', state=state, left=0., top=0.,
                width=700., height=400., fold_defaults=False)
    host.ed, host.state = state.editor, state
    yield host
    host.close()


def bar(host):
    return declared(host, 'finder')


def test_ctrl_f_opens_the_bar_and_the_keyboard_is_the_fields(editor):
    ed, find = editor.ed, editor.ed.search
    editor.click(*at(editor, 2, 5))
    caret = ed.doc.caret
    keys(editor, (glfw.KEY_F, CTRL))
    assert find.open and find.focused and bar(editor) and bar(editor)[0] in editor.cache.graph.windows(), 'the bar is a popover of the editor'
    changed, value = typed(editor, 'return')
    assert not changed and value.text == SOURCE and find.field.doc.text == 'return' and ed.term == 'return', 'typed into the field, not the text'
    assert len(ed.marks['hits']) == len(RETURNS) == 43 and ed.handle.search == Search('return', 1, 43)
    assert ed.marks['found'].on(RETURNS[0]) == [(RETURNS[0], Mark('found', 8, 14))], 'the first hit from the caret is the current one'
    assert ed.doc.caret == caret == ed.doc.anchor, 'the caret stays'
    keys(editor, glfw.KEY_BACKSPACE, glfw.KEY_BACKSPACE)
    assert ed.term == 'retu' and ed.handle.search.count == 43
    keys(editor, (glfw.KEY_A, CTRL))
    typed(editor, 'zzz')
    editor.frame()
    assert ed.term == 'zzz' and ed.handle.search == Search('zzz', 0, 0) and ed.marks['found'] is None, 'typing over the selected term replaces it'


def test_enter_steps_through_the_hits_around_the_ends_and_reveals_them(editor):
    ed, find = editor.ed, editor.ed.search
    editor.click(ed.gutter - view.FOLD / 2, at(editor, 8, 0)[1])
    assert ed.rows.hidden(RETURNS[0]), 'the first hits are inside a collapsed scope'
    editor.click(*at(editor, 2, 5))
    keys(editor, (glfw.KEY_F, CTRL))
    typed(editor, 'return')
    editor.frame()
    assert not ed.rows.hidden(RETURNS[0]) and ed.marks['flash'].on(RETURNS[0]) == [(RETURNS[0], Mark('flash', 8, 14))], 'unfolded and flashed'
    keys(editor, glfw.KEY_ENTER)
    assert ed.handle.search.index == 2 and ed.doc.text == SOURCE, 'Enter is no line break while the field has the keyboard'
    keys(editor, glfw.KEY_DOWN, glfw.KEY_DOWN, glfw.KEY_DOWN)
    editor.frame()
    assert ed.handle.search.index == 5 and ed.pane.scroll_offset[1] > 0, 'a hit below the viewport is scrolled to'
    top = ed.pane.scroll_offset[1]
    assert top < ed.rows.row(RETURNS[4]) * ed.lh < top + 400 - ed.lh
    keys(editor, (glfw.KEY_ENTER, SHIFT), glfw.KEY_UP, glfw.KEY_UP, glfw.KEY_UP)
    editor.frame()
    assert ed.handle.search.index == 1 and ed.pane.scroll_offset[1] == 0
    keys(editor, (glfw.KEY_ENTER, SHIFT))
    editor.frame()
    assert ed.handle.search.index == 43 and ed.pane.scroll_offset[1] > 2000, 'previous from the first is the last'
    keys(editor, glfw.KEY_ENTER)
    editor.frame()
    assert ed.handle.search.index == 1 and ed.pane.scroll_offset[1] == 0, 'next from the last is the first'


def test_escape_closes_the_bar_and_the_text_has_the_keyboard_again(editor):
    ed, find = editor.ed, editor.ed.search
    editor.click(*at(editor, 2, 5))
    keys(editor, (glfw.KEY_F, CTRL))
    typed(editor, 'limit')
    assert len(ed.marks['hits']) == 2
    keys(editor, glfw.KEY_ESCAPE)
    editor.frame()
    assert not find.open and not find.focused and bar(editor) == [] and ed.marks['hits'] is None and ed.marks['found'] is None
    changed, value = typed(editor, 'x')
    assert changed and value.text.split('\n')[2] == 'LIMITx = 10', 'typed where the caret was'
    # The term is kept: the next Ctrl+F shows it selected, and finds it again.
    keys(editor, (glfw.KEY_F, CTRL))
    assert find.field.doc.text == 'limit' and find.field.doc.selection == (0, 5) and ed.handle.search.count == 2
    # A selection inside one line is the term.
    keys(editor, glfw.KEY_ESCAPE)
    editor.click(*at(editor, 14, 6), count=2)
    assert ed.doc.slice(*ed.doc.selection) == 'total'
    keys(editor, (glfw.KEY_F, CTRL))
    assert ed.term == 'total' and ed.handle.search.count == 3


def test_a_press_in_the_text_takes_the_keyboard_back_and_the_cross_closes(editor):
    ed, find = editor.ed, editor.ed.search
    editor.click(*at(editor, 2, 5))
    keys(editor, (glfw.KEY_F, CTRL))
    typed(editor, 'limit')
    editor.click(*at(editor, 3, 5))
    assert find.open and not find.focused and len(ed.marks['hits']) == 2, 'the bar and its hits stay'
    changed, value = typed(editor, 'y')
    assert changed and value.text.split('\n')[3] == 'READYy = True'
    x, y = editor.cache.position(bar(editor)[0])
    wide = editor.cache.graph.rect(bar(editor)[0])[2]
    editor.click(x + 5 * ed.cw, y + 8)
    editor.frame()
    assert find.focused and find.field.doc.caret == 2, 'a press in the field puts its caret there'
    typed(editor, 's')
    assert find.field.doc.text == 'lismit' and ed.doc.line(3) == 'READYy = True'
    editor.click(x + wide - ed.cw, y + 8)
    editor.frame()
    assert not find.open and bar(editor) == [] and ed.marks['hits'] is None
    # Escape in the text closes a bar that has not the keyboard.
    keys(editor, (glfw.KEY_F, CTRL))
    editor.click(*at(editor, 3, 5))
    keys(editor, glfw.KEY_ESCAPE)
    assert not find.open


def test_a_callers_own_term_is_stepped_and_read_through_the_pane(editor):
    ed, pane = editor.ed, editor.state.pane
    editor.click(*at(editor, 12, 0))
    editor.frame(search='total')
    editor.frame()
    assert pane.search == Search('total', 1, 3) and not ed.search.open and bar(editor) == []
    pane.find(1)
    editor.frame()
    pane.find(1)
    pane.find(1)
    editor.frame()
    assert pane.search.index == 1, 'around the end'
    pane.find(-1)
    editor.frame()
    assert pane.search.index == 3 and ed.marks['found'].on(17) == [(17, Mark('found', 11, 16))]
    # The editor's own bar takes the term over while it is open; closed, the caller's is shown again.
    keys(editor, (glfw.KEY_F, CTRL), (glfw.KEY_A, CTRL))
    typed(editor, 'limit')
    assert pane.search == Search('limit', 2, 2)
    keys(editor, glfw.KEY_ESCAPE)
    editor.frame()
    assert pane.search.term == 'total' and pane.search.count == 3
    editor.frame(search='')
    assert pane.search == Search('', 0, 0) and ed.marks['hits'] is None


def test_a_hit_far_to_the_right_is_scrolled_to_sideways():
    syntax.tools()
    state = EditorState()
    host = Host(view.draw_editor, input_value='near = 1\nfar = ' + 'x' * 300 + ' needle\n', name='wide', state=state, left=0., top=0.,
                width=400., height=200.)
    try:
        ed = state.editor
        host.frame(search='needle')
        host.frame()
        assert ed.scroll_x > 0 and ed.gutter <= ed.x(1, 313) - ed.scroll_x <= 400 - view.BAR, 'the end of the hit is inside the view'
        host.frame(search='near')
        host.frame()
        assert ed.scroll_x == 0
    finally:
        host.close()


def test_ctrl_shift_b_opens_the_carets_line_outside(editor, monkeypatch):
    opened = []
    monkeypatch.setattr(lookup, 'external', lambda path, line: opened.append((path, line)))
    editor.click(*at(editor, 9, 4))
    keys(editor, (glfw.KEY_B, CTRL | SHIFT))
    assert opened == [('/project/sample.py', 10)] and editor.ed.lookup.pending is None, 'no lookup: the line goes to the external editor'


def test_typing_right_after_ctrl_end_lands_at_the_end(editor):
    """Click, Ctrl+End, then text: the first letters after the jump are typed (review of the cleanslate demo)."""
    ed = editor.ed
    editor.click(*at(editor, 2, 5))
    changed, value = editor.frame(keys=[(glfw.KEY_END, CTRL), (glfw.KEY_C, 0)], text='c')
    assert changed and value.text == SOURCE + 'c' and ed.doc.caret == ed.doc.size, 'the jump and the letter in one frame'
    editor.frame(keys=[(glfw.KEY_HOME, CTRL)])
    editor.frame(keys=[(glfw.KEY_END, CTRL)])
    changed, value = typed(editor, 'ab')
    assert changed and value.text == SOURCE + 'cab', 'and frames apart'
