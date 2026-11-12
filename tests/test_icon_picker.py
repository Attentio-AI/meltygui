"""The icon picker on the editor's icon glyphs (hidden GL window, reserved desktop)."""
import pytest

import meltygui.core.windowing.window_constants as glfw
import meltygui.view.editor_view as view
import meltygui.view.icon_picker_view as picker
from editor_host import Host
from meltygui.editor import inline, syntax
from meltygui.state.core_undo import UndoManager
from meltygui.state.editor_state import EditorState

STAR, COFFEE, HEART = '', '', ''
SOURCE = f'label = "{STAR} first"\npair = "{STAR}{HEART}"\nplain = 1\n'
CTRL = glfw.MOD_CONTROL


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


def at(host, line, column):
    ed = host.ed
    return (ed.x(line, column) + .4 * ed.cw - ed.scroll_x, (ed.rows.row(line) + .5) * ed.lh - ed.pane.scroll_offset[1])


def declared(host, name):
    return [node for node, record in host.cache.records.items() if record[2].__name__ == name]


def keys(host, *presses, text=''):
    return host.frame(keys=[(press, 0) if isinstance(press, int) else press for press in presses], text=text)


def typed(host, text):
    for char in text:
        host.frame(keys=[(ord(char.upper()), 0)], text=char)
    return host.result


def state_of(host, name='icons'):
    return host.cache.states[declared(host, name)[0]]


def test_the_catalog_is_the_font_table_and_search_ranks_the_name_first():
    found = picker.catalog()
    assert len(found) > 900 and (STAR, 'star') in found and (COFFEE, 'coffee') in found
    assert picker.offered(STAR, 'star')[0] == (STAR, 'star') and all('star' in name for _, name in picker.offered(STAR, 'star'))
    assert picker.offered(STAR, 'STAR-H')[0][1] == 'star-half'
    assert picker.offered('', '')[0] == ('', 'in the text') and picker.offered(STAR, '') is found
    assert picker.offered(STAR, 'no such icon') == []


def test_an_icon_glyph_is_a_chip_also_with_the_caret_next_to_it(editor):
    ed = editor.ed
    assert inline.for_token('icon').solid and len(declared(editor, 'icon')) == 2, 'one chip per run of glyphs'
    editor.click(*at(editor, 2, 3))
    keys(editor, glfw.KEY_END, glfw.KEY_ENTER, (glfw.KEY_I, CTRL))
    assert ed.doc.line(3) == STAR and len(declared(editor, 'icon')) == 3, 'the placeholder Ctrl+I put in is one at once'


def test_a_click_opens_the_picker_and_enter_replaces_the_glyph(editor):
    ed, focus = editor.ed, editor.cache.imgui_input
    editor.click(*at(editor, 2, 3))
    editor.click(*at(editor, 0, 9))
    assert len(declared(editor, 'icons')) == 1 and ed.doc.line_of(ed.doc.caret) == 2, 'the click is the chip\'s, not the caret\'s'
    editor.frame()
    assert focus.keyboard == declared(editor, 'icons')[0] and not ed.focused, 'the picker has the keyboard'
    found = picker.catalog()
    assert found[state_of(editor).index] == (STAR, 'star'), 'it opens on the glyph the text has'
    typed(editor, 'coffee')
    assert state_of(editor).query == 'coffee' and ed.doc.text == SOURCE, 'typing searches; the text is not typed into'
    changed, text = keys(editor, glfw.KEY_ENTER)
    editor.frame()
    assert editor.ed.doc.line(0) == f'label = "{COFFEE} first"' and not declared(editor, 'icons')
    assert focus.keyboard == declared(editor, 'draw_editor')[0] and ed.focused, 'the editor types again'
    typed(editor, 'z')
    assert ed.doc.line(2) == 'plaz' + 'in = 1'
    UndoManager.undo()
    UndoManager.undo()
    editor.frame()
    assert ed.doc.text == SOURCE, 'the pick is an undo step'


def test_arrows_move_the_selection_and_backspace_widens_the_search(editor):
    editor.click(*at(editor, 0, 9))
    editor.frame()
    start = state_of(editor).index
    keys(editor, glfw.KEY_RIGHT)
    keys(editor, glfw.KEY_DOWN)
    assert state_of(editor).index == start + 1 + picker.COLUMNS
    keys(editor, glfw.KEY_END)
    last = state_of(editor)
    assert last.index == len(picker.catalog()) - 1 and last.first == (last.index // picker.COLUMNS) - picker.ROWS + 1, 'kept in sight'
    typed(editor, 'starx')
    assert state_of(editor).query == 'starx' and picker.offered(STAR, 'starx') == []
    keys(editor, glfw.KEY_ENTER)
    assert declared(editor, 'icons') and editor.ed.doc.text == SOURCE, 'nothing offered: Enter picks nothing'
    keys(editor, glfw.KEY_BACKSPACE)
    assert state_of(editor).query == 'star' and state_of(editor).index == 0


def test_escape_a_second_click_and_a_click_elsewhere_close_it_without_a_change(editor):
    ed = editor.ed
    for close in (lambda: keys(editor, glfw.KEY_ESCAPE), lambda: editor.click(*at(editor, 0, 9)),
                  lambda: editor.click(*at(editor, 2, 2))):
        editor.click(*at(editor, 0, 9))
        editor.frame()
        assert len(declared(editor, 'icons')) == 1
        close()
        editor.frame()
        editor.frame()
        assert not declared(editor, 'icons') and ed.doc.text == SOURCE
    # Opened again it starts afresh: no query left over.
    editor.click(*at(editor, 0, 9))
    editor.frame()
    typed(editor, 'he')
    keys(editor, glfw.KEY_ESCAPE)
    editor.frame()
    editor.click(*at(editor, 0, 9))
    editor.frame()
    assert state_of(editor).query == '' and picker.catalog()[state_of(editor).index][0] == STAR


def test_a_click_in_the_grid_picks_for_the_glyph_that_was_clicked_in_a_run(editor):
    ed = editor.ed
    editor.click(*at(editor, 1, 9))
    editor.frame()
    node = declared(editor, 'icons')[0]
    state = editor.cache.states[node]
    assert picker.catalog()[state.index][0] == HEART, 'the second glyph of the run'
    x, y = editor.cache.position(node)
    lh = ed.lh
    column, row = 3, 2
    wanted = picker.catalog()[(state.first + row) * picker.COLUMNS + column][0]
    point = (x + picker.PAD + (column + .5) * picker.CELL, y + lh + 6 + picker.PAD + (row + .5) * picker.CELL)
    editor.frame(point)
    assert editor.cache.states[node].index == (state.first + row) * picker.COLUMNS + column, 'the pointer selects'
    editor.click(*point)
    editor.frame()
    assert ed.doc.line(1) == f'pair = "{STAR}{wanted}"' and not declared(editor, 'icons')
