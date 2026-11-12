"""Pointer gestures of the native editor: word and line drags, which button is whose (hidden GL window, reserved desktop)."""
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


class Handler:
    """The host's gesture router as `register_host_pointer` sees it: what the views keep from the OS window."""
    kept = ()

    def register_hovered(self, key, subscriptions):
        self.kept = subscriptions


def kept(host, position):
    handler = Handler()
    host.cache.register_host_pointer(handler, position)
    return handler.kept


def test_a_right_drag_over_the_editor_is_the_windows_and_a_left_drag_the_editors(editor):
    ed, io, routing = editor.ed, editor.io, editor.cache.imgui_input
    editor.click(*at(editor, 1, 3))
    caret = ed.doc.caret
    editor.frame((400., 300.))
    held = kept(editor, (400., 300.))
    assert 'left_mouse_dragged' in held and 'right_mouse_dragged' not in held, 'text selection keeps the left drag only'
    io.mouse_down[1] = True
    editor.frame((400., 300.))
    editor.frame((340., 180.))
    editor.frame((340., 180.))
    assert ed.hold is None and ed.scroll_x == 0. and ed.pane.scroll_offset[1] == 0., 'the editor does nothing with it'
    assert not routing.pointer_claims.get(routing.mouse_owner, (False, False))[1], 'the layout may resize with it'
    assert 'right_mouse_dragged' not in kept(editor, (340., 180.)), 'and so may the OS window'
    io.mouse_down[1] = False
    editor.frame((340., 180.))
    assert ed.doc.caret == caret and ed.doc.selection == (caret, caret)


def test_a_right_drag_that_starts_on_an_inline_widget_is_the_windows_too(editor):
    ed, routing = editor.ed, editor.cache.imgui_input
    editor.frame(at(editor, 3, 8))
    editor.frame(at(editor, 3, 8))
    editor.io.mouse_down[1] = True
    editor.frame(at(editor, 3, 8))
    x, y = at(editor, 3, 8)
    editor.frame((x, y - 40.))
    editor.frame((x, y - 40.))
    assert not routing.pointer_claims.get(routing.mouse_owner, (False, False))[1] and 'right_mouse_dragged' not in kept(editor, (x, y - 40.))
    assert ed.pane.scroll_offset[1] == 0. and ed.doc.text == SOURCE, 'the number under the pointer was not dragged'
    editor.io.mouse_down[1] = False
    editor.frame((x, y - 40.))


def test_listening_for_the_pointer_keeps_left_drags_and_nothing_keeps_right_ones():
    import meltygui_imgui as imgui
    from meltygui.core.rendering.gui_prototype import gui

    @gui(listen=('pointer',), width=200, height=100)
    def taking(input_value, view_events=None):
        imgui.text('takes the pointer')

    @gui(width=200, height=100)
    def plain(input_value):
        imgui.text('plain')

    @gui(width=400, height=300)
    def root(input_value, width=0., height=0.):
        taking(None, name='taking', left=0., top=0.)
        plain(None, name='plain', left=0., top=150.)

    host = Host(root, input_value=None, name='root', left=0., top=0.)
    try:
        host.frame((50., 50.))
        assert host.cache.imgui_input.left and kept(host, (50., 50.)) == ['left_mouse_dragged', 'left_mouse_double_dragged', 'left_mouse_double_clicked']
        host.frame((50., 200.))
        assert not host.cache.imgui_input.left and not kept(host, (50., 200.)), 'text and empty space leave both gestures to the window'
        host.frame((50., 50.), True)
        host.frame((50., 200.), True)
        assert host.cache.imgui_input.left, 'a drag that began on the view stays its own past its edge'
        host.frame((50., 200.), False)
        host.frame((50., 200.))
        assert not host.cache.imgui_input.left
    finally:
        host.close()


def test_below_the_last_row_a_left_drag_is_the_windows():
    state = EditorState()
    host = Host(view.draw_editor, input_value='one\ntwo\nthree', name='short', state=state, left=0., top=0., width=700., height=400.)
    try:
        ed, routing = state.editor, host.cache.imgui_input
        below = (300., 3 * ed.lh + 40.)
        host.frame((300., ed.lh * 1.5))
        assert routing.left and 'left_mouse_dragged' in kept(host, (300., ed.lh * 1.5)), 'over a row: text selection'
        host.frame(below)
        assert not routing.left and not kept(host, below), 'below the rows nothing is kept from the window'
        host.click(300., ed.lh * 1.5)
        caret = ed.doc.caret
        host.frame(below, True)
        host.frame((350., below[1] + 30.), True)
        host.frame((350., below[1] + 30.), False)
        assert ed.doc.caret == caret and ed.doc.selection == (caret, caret) and ed.focused, 'the drag there does nothing to the text'
        # A drag that begins on a row is the editor's wherever it goes.
        host.frame((300., ed.lh * .5), True)
        host.frame(below, True)
        assert routing.left and ed.doc.selection[0] != ed.doc.selection[1]
        host.frame(below, False)
        # More rows: the editor reaches further down.
        host.frame(input_value='\n'.join(['row'] * 8))
        host.frame((300., 5.5 * ed.lh))
        assert routing.left
    finally:
        host.close()


def test_the_text_cursor_stays_through_a_selection_drag(editor, monkeypatch):
    from meltygui.core.input import mouse_cursor
    asked = []
    monkeypatch.setattr(mouse_cursor, 'request', asked.append)
    ed = editor.ed
    editor.frame(at(editor, 1, 3))
    assert asked[-1] == mouse_cursor.TEXT
    editor.frame(at(editor, 1, 3), True)
    del asked[:]
    for point in (at(editor, 2, 8), (300., 395.), (300., 460.), (900., -40.)):
        editor.frame(point, True)
        editor.frame(point, True)
    assert asked and set(asked) == {mouse_cursor.TEXT}, 'over other rows, below the text and outside the view'
    editor.frame((900., -40.), False)
    del asked[:]
    editor.frame((900., -40.))
    assert not asked, 'released outside: the shape is the host\'s again'
    # A press that begins where no view declares a shape keeps none, even over the text.
    short = Host(view.draw_editor, input_value='one', name='short', state=EditorState(), left=0., top=0., width=700., height=400.)
    try:
        short.frame((300., 300.), True)
        del asked[:]
        short.frame((300., 8.), True)
        assert not asked
        short.frame((300., 8.), False)
    finally:
        short.close()


def test_the_gutter_shows_a_hand_and_its_presses_are_still_the_editors(editor, monkeypatch):
    from meltygui.core.input import mouse_cursor
    asked = []
    monkeypatch.setattr(mouse_cursor, 'request', asked.append)
    ed = editor.ed
    y = at(editor, 1, 0)[1]
    editor.frame((ed.gutter - 30., y))
    assert asked[-1] == mouse_cursor.HAND, 'numbers, folds, chips and breakpoints are pressed, not typed in'
    editor.frame((ed.gutter + 30., y))
    assert asked[-1] == mouse_cursor.TEXT
    editor.click(ed.gutter - 30., y)
    assert ed.doc.selection == (ed.doc.start(1), ed.doc.start(2)), 'a press on a number still selects its line'
    editor.frame((ed.gutter + 30., y), True)
    editor.frame((ed.gutter - 30., y), True)
    assert asked[-1] == mouse_cursor.TEXT, 'a selection dragged into the gutter keeps the text cursor'
    editor.frame((ed.gutter - 30., y), False)
