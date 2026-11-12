"""Pointer gestures of the native editor: word and line drags, the right button pan (hidden GL window, reserved desktop)."""
import pytest

import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.editor import edit, syntax
from meltygui.editor.document import Document
from meltygui.state.core_undo import UndoManager
from meltygui.state.editor_state import EditorState

SOURCE = '''alpha = beta + gamma(delta)
second line of words here
third = [one, two]
''' + ''.join(f'row_{n} = {n}  # ' + 'wide ' * 40 + '\n' for n in range(80))


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
    host.io.mouse_down[1] = False
    host.close()


def at(host, line, column):
    ed = host.ed
    return (ed.x(line, column) + .25 * ed.cw - ed.scroll_x, (ed.rows.row(line) + .5) * ed.lh - ed.pane.scroll_offset[1])


def press(host, line, column, count):
    """Press `count` times at a position and keep the button down."""
    point = at(host, line, column)
    for _ in range(count - 1):
        host.frame(point, True)
        host.frame(point, False)
    host.frame(point, True)


def selected(host):
    return edit.selected(host.ed.doc)


def test_extend_grows_by_word_or_line_around_the_first_one():
    doc = Document('alpha = beta + gamma(delta)\nsecond line\nthird\n')
    edit.select_word(doc, 9)
    low, high = doc.selection
    assert edit.selected(doc) == 'beta'
    edit.extend(doc, 17, 'word', low, high)
    assert edit.selected(doc) == 'beta + gamma' and doc.caret == 20, 'right of it: the word under the pointer joins'
    edit.extend(doc, 2, 'word', low, high)
    assert edit.selected(doc) == 'alpha = beta' and doc.caret == 0, 'left of it: the first word stays, the caret leads'
    edit.extend(doc, 10, 'word', low, high)
    assert edit.selected(doc) == 'beta', 'back inside it'
    edit.extend(doc, 20, 'word', low, high)
    assert edit.selected(doc) == 'beta + gamma(', 'a bracket is a unit of its own'
    edit.select_line(doc, 30)
    low, high = doc.selection
    assert edit.selected(doc) == 'second line\n'
    edit.extend(doc, 42, 'line', low, high)
    assert edit.selected(doc) == 'second line\nthird\n'
    edit.extend(doc, 3, 'line', low, high)
    assert edit.selected(doc) == 'alpha = beta + gamma(delta)\nsecond line\n' and doc.caret == 0
    edit.extend(doc, 3)
    assert doc.selection == (3, high), 'without a unit the caret just follows'


def test_a_drag_after_a_double_click_selects_by_word(editor):
    press(editor, 0, 9, 2)
    assert selected(editor) == 'beta'
    editor.frame(at(editor, 0, 16), True)
    assert selected(editor) == 'beta + gamma'
    editor.frame(at(editor, 1, 9), True)
    assert selected(editor) == 'beta + gamma(delta)\nsecond line'
    editor.frame(at(editor, 0, 1), True)
    assert selected(editor) == 'alpha = beta', 'dragged back over it the first word stays selected'
    editor.frame(at(editor, 0, 1), False)
    assert selected(editor) == 'alpha = beta' and editor.ed.hold is None
    editor.click(*at(editor, 2, 3))
    editor.frame(at(editor, 2, 12))
    assert editor.ed.doc.selection == (editor.ed.doc.index(2, 3),) * 2, 'released: moving the pointer selects nothing'


def test_a_drag_after_a_triple_click_selects_by_line(editor):
    press(editor, 1, 4, 3)
    assert selected(editor) == 'second line of words here\n'
    editor.frame(at(editor, 2, 2), True)
    assert selected(editor) == 'second line of words here\nthird = [one, two]\n'
    editor.frame(at(editor, 0, 20), True)
    assert selected(editor) == 'alpha = beta + gamma(delta)\nsecond line of words here\n'
    editor.frame(at(editor, 0, 20), False)


def test_a_plain_drag_still_selects_by_character(editor):
    press(editor, 0, 2, 1)
    editor.frame(at(editor, 0, 10), True)
    assert selected(editor) == 'pha = be'
    editor.frame(at(editor, 0, 10), False)


def test_a_right_button_drag_pans_both_axes(editor):
    ed, io = editor.ed, editor.io
    editor.click(*at(editor, 1, 3))
    caret = ed.doc.caret
    editor.frame((400., 300.))
    assert editor.cache.hover is not None, 'over the editor the host keeps drags from the OS window frame'
    io.mouse_down[1] = True
    editor.frame((400., 300.))
    claims = editor.cache.imgui_input.pointer_claims
    assert ed.hold[0] == 'pan' and claims[editor.cache.imgui_input.mouse_owner][1], 'the press is the editor\'s, not the host\'s'
    editor.frame((340., 180.))
    editor.frame((340., 180.))
    assert ed.scroll_x == 60. and ed.pane.scroll_offset[1] == 120., 'the text follows the pointer'
    editor.frame((390., 250.))
    editor.frame((390., 250.))
    assert ed.scroll_x == 10. and ed.pane.scroll_offset[1] == 50.
    editor.frame((900., 900.))
    editor.frame((900., 900.))
    assert ed.scroll_x == 0. and ed.pane.scroll_offset[1] == 0., 'it stops at the text\'s corner'
    editor.frame((-4000., -90000.))
    editor.frame((-4000., -90000.))
    reach = ed.rows.count * ed.lh - 400.
    assert ed.pane.scroll_offset[1] == float(int(reach)) and 0 < ed.scroll_x < 4400., 'and at its far end'
    io.mouse_down[1] = False
    editor.frame((390., 250.))
    assert ed.hold is None and ed.doc.caret == caret and ed.doc.selection == (caret, caret), 'the caret is where it was'
    assert not claims[editor.cache.imgui_input.mouse_owner][1]
    editor.frame((300., 200.))
    assert ed.pane.scroll_offset[1] == float(int(reach)), 'released: the pointer pans no more'


def test_a_right_drag_that_starts_on_an_inline_widget_pans_too(editor):
    ed, claims = editor.ed, editor.cache.imgui_input
    editor.frame(at(editor, 3, 8))
    editor.frame(at(editor, 3, 8))
    editor.io.mouse_down[1] = True
    editor.frame(at(editor, 3, 8))
    assert ed.hold[0] == 'pan' and claims.pointer_claims[claims.mouse_owner][1]
    x, y = at(editor, 3, 8)
    editor.frame((x, y - 40.))
    editor.frame((x, y - 40.))
    assert ed.pane.scroll_offset[1] == 40. and ed.doc.text == SOURCE, 'the number under the pointer was not dragged'
    editor.io.mouse_down[1] = False
    editor.frame((x, y - 40.))
