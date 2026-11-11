"""The native editor view end to end, on a hidden GL window (reserved desktop)."""
import pytest

import meltygui.core.windowing.window_constants as glfw
import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.editor import inline, syntax
from meltygui.editor.marks import Marks, Mark
from meltygui.editor.editor import BLOCK
from meltygui.state.core_undo import UndoManager
from meltygui.state.editor_state import EditorState, Breakpoints

SOURCE = '''import os

LIMIT = 10
READY = True
TINT = (0.2, 0.4, 0.8)


def first(value):
    if value:
        return value + 1
    return 0


def second():
    total = 0
    for item in range(LIMIT):
        total += item
    return total
''' + ''.join(f'\n\ndef extra_{n}():\n    return {n}\n' for n in range(40))
SHIFT, CTRL, ALT = glfw.MOD_SHIFT, glfw.MOD_CONTROL, glfw.MOD_ALT


@pytest.fixture(autouse=True)
def undo_stack():
    """The framework's undo stack is process wide: every test starts with it empty."""
    UndoManager.stack.history.clear()
    UndoManager.stack.redo_stack.clear()


@pytest.fixture
def editor(monkeypatch):
    syntax.tools()
    painted = []
    paint = view.paint
    monkeypatch.setattr(view, 'paint', lambda ed, block, *rest: painted.append(block.key) or paint(ed, block, *rest))
    state = EditorState()
    host = Host(view.draw_editor, input_value=SOURCE, name='editor', state=state, left=0., top=0.,
                width=700., height=400., fold_defaults=False)
    host.ed, host.painted, host.state = state.editor, painted, state
    yield host
    host.close()


def at(host, line, column):
    """Window point of a document position."""
    ed = host.ed
    return (ed.x(line, column) + .25 * ed.cw - ed.scroll_x,
            (ed.rows.row(line) + .5) * ed.lh - host.ed.pane.scroll_offset[1])


def declared(host, name='block'):
    return [node for node, record in host.cache.records.items() if record[2].__name__ == name]


def undo(host, redo=False):
    """Ctrl+Z as the framework delivers it: its hotkey pops the undo stack before the views run."""
    UndoManager.redo() if redo else UndoManager.undo()
    return host.frame()


def keys(host, *presses, text=''):
    return host.frame(keys=[(press, 0) if isinstance(press, int) else press for press in presses], text=text)


def typed(host, text):
    """Type printable text as the platform does: a key press and its character."""
    for char in text:
        code = ord(char.upper()) if char.isalnum() else glfw.KEY_SPACE if char == ' ' else glfw.KEY_PERIOD
        host.frame(keys=[(code, 0)], text=char)
    return host.result


def test_click_places_the_caret_and_typing_edits_one_block(editor):
    editor.click(*at(editor, 2, 5))
    assert editor.ed.doc.place(editor.ed.doc.caret) == (2, 5) and editor.ed.focused
    editor.painted.clear()
    changed, text = typed(editor, 'xy')
    assert changed and text.split('\n')[2] == 'LIMITxy = 10'
    assert set(editor.painted) == {editor.ed.doc.blocks[0].key}, 'only the block holding the line repaints'
    changed, text = keys(editor, glfw.KEY_BACKSPACE)
    assert text.split('\n')[2] == 'LIMITx = 10'
    undo(editor)
    changed, text = undo(editor)
    assert changed and text == SOURCE and editor.frame()[0] is False
    changed, text = undo(editor, redo=True)
    assert changed and text.split('\n')[2] == 'LIMITxy = 10' and editor.ed.doc.place(editor.ed.doc.caret) == (2, 7)


def test_undo_of_an_edit_out_of_sight_scrolls_to_it_and_flashes(editor, monkeypatch):
    import meltygui.editor.editor as session
    monkeypatch.setattr(session, 'FLASH', .02)
    monkeypatch.setattr(view, 'FLASH', .02)
    ed = editor.ed
    editor.click(*at(editor, 2, 5))
    typed(editor, 'xy')
    editor.frame((300, 200), wheel=-60)
    editor.frame((300, 200))
    assert ed.pane.scroll_offset[1] > 2000
    changed, text = undo(editor)
    assert changed and text == SOURCE and ed.doc.place(ed.doc.caret) == (2, 5)
    assert ed.pane.scroll_offset[1] == 0, 'the change is brought into view'
    assert ed.marks['flash'].on(2) == [(2, Mark('flash', 5, 6))], 'a removal still flashes one cell'
    import time
    time.sleep(.06)
    editor.frame()
    assert ed.marks['flash'] is None, 'the flash ends by itself'
    changed, text = undo(editor, redo=True)
    assert text.split('\n')[2] == 'LIMITxy = 10' and ed.marks['flash'].on(2) == [(2, Mark('flash', 5, 7))]


def test_a_widget_drag_is_one_undo_step(editor):
    ed = editor.ed
    x, y = at(editor, 2, 9)
    editor.frame((x, y), False)
    editor.frame((x, y), True)
    for step in (10, 20, 40):
        editor.frame((x + step, y), True)
    editor.frame((x + 40, y), False)
    editor.frame()
    assert ed.doc.line(2) == 'LIMIT = 20' and len(UndoManager.stack.history) == 1
    changed, text = undo(editor)
    assert text == SOURCE


def test_another_path_is_another_document_not_an_edit(editor):
    ed = editor.ed
    editor.click(*at(editor, 2, 5))
    typed(editor, 'x')
    editor.frame(input_value='other = 1\n', path='/tmp/other.py')
    fresh = editor.state.editor
    assert fresh is not ed and ed.closed and fresh.doc.text == 'other = 1\n'
    UndoManager.undo()
    assert editor.frame()[1] == 'other = 1\n', 'the old document\'s step does not touch the new one'


def test_keys_and_text_of_one_frame_apply_in_press_order(editor):
    editor.click(*at(editor, 1, 0))
    changed, text = editor.frame(keys=[(glfw.KEY_A, 0), (glfw.KEY_BACKSPACE, 0), (glfw.KEY_B, 0)], text='ab')
    assert text.split('\n')[1] == 'b'


def test_enter_indents_and_shifts_the_lines_below(editor):
    editor.click(*at(editor, 8, 13))
    changed, text = keys(editor, glfw.KEY_ENTER)
    lines = text.split('\n')
    assert lines[8] == '    if value:' and lines[9] == '        ' and lines[10] == '        return value + 1'
    assert editor.ed.rows.count == editor.ed.doc.lines == len(lines)


def test_selection_by_drag_double_click_shift_and_clipboard(editor, monkeypatch):
    clip = []
    monkeypatch.setattr(view.imgui, 'set_clipboard_text', clip.append)
    monkeypatch.setattr(view.imgui, 'get_clipboard_text', lambda: clip[-1])
    doc = editor.ed.doc
    editor.frame(at(editor, 2, 0), True)
    editor.frame(at(editor, 3, 4), True)
    editor.frame(at(editor, 3, 4), False)
    assert doc.text[slice(*doc.selection)] == 'LIMIT = 10\nREAD'
    keys(editor, (glfw.KEY_C, CTRL))
    assert clip == ['LIMIT = 10\nREAD']
    editor.click(*at(editor, 7, 6), count=2)
    assert doc.text[slice(*doc.selection)] == 'first'
    keys(editor, (glfw.KEY_V, CTRL))
    assert doc.line(7) == 'def LIMIT = 10' and doc.line(8) == 'READ(value):'
    editor.click(*at(editor, 0, 0))
    keys(editor, (glfw.KEY_DOWN, SHIFT), (glfw.KEY_END, SHIFT))
    assert doc.text[slice(*doc.selection)] == 'import os\n'
    keys(editor, (glfw.KEY_A, CTRL))
    assert doc.selection == (0, len(doc.text))


def test_scrolling_runs_no_block_and_the_caret_scrolls_back_into_view(editor):
    editor.click(*at(editor, 2, 0))
    editor.frame()
    for wheel in (-3, 3):
        editor.frame((300, 200), wheel=wheel)
        editor.frame((300, 200))
    editor.painted.clear()
    editor.frame((300, 200), wheel=-3)
    editor.frame((300, 200))
    offset = editor.ed.pane.scroll_offset[1]
    assert offset == 120 and editor.painted == [], 'already painted rows only move'
    editor.frame((300, 200), wheel=-30)
    editor.frame((300, 200))
    assert editor.ed.pane.scroll_offset[1] > 600
    keys(editor, glfw.KEY_RIGHT)
    assert editor.ed.pane.scroll_offset[1] <= editor.ed.rows.row(2) * editor.ed.lh


def test_fold_toggle_from_the_gutter_hides_rows_and_keeps_editing_exact(editor):
    ed = editor.ed
    rows = ed.rows.count
    x = ed.gutter - view.FOLD / 2
    editor.click(x, at(editor, 7, 0)[1])
    assert ed.rows.count == rows - 3 and ed.rows.folded(7) == 3 and ed.rows.row(13) == 10
    assert editor.state.collapsed == [('scope', 'first')]
    editor.click(*at(editor, 13, 4))
    assert ed.doc.place(ed.doc.caret) == (13, 4), 'rows below the fold map to their own lines'
    changed, text = typed(editor, 'z')
    assert text.split('\n')[13] == 'def zsecond():'
    keys(editor, glfw.KEY_ESCAPE, glfw.KEY_UP, glfw.KEY_UP, glfw.KEY_UP)
    assert ed.doc.line_of(ed.doc.caret) == 7, 'the caret steps over hidden lines'
    undo(editor)
    editor.click(x, at(editor, 7, 0)[1])
    assert ed.rows.count == rows and ed.folds.ranges() == []
    keys(editor, (glfw.KEY_MINUS, CTRL | SHIFT))
    assert ed.rows.count == rows - 47 and not ed.rows.hidden(ed.doc.line_of(ed.doc.caret))
    keys(editor, (glfw.KEY_EQUAL, CTRL | SHIFT))
    assert ed.rows.count == rows


def test_an_edit_inside_a_collapsed_fold_reveals_it(editor):
    ed = editor.ed
    editor.click(*at(editor, 9, 8))
    typed(editor, 'q')
    editor.click(ed.gutter - view.FOLD / 2, at(editor, 7, 0)[1])
    assert ed.rows.hidden(9) and ed.doc.line_of(ed.doc.caret) == 7
    undo(editor)
    assert not ed.rows.hidden(9) and ed.doc.line_of(ed.doc.caret) == 9


def test_inline_widgets_edit_their_token(editor):
    ed = editor.ed
    assert inline.for_token('bool').view is view.switch
    editor.click(*at(editor, 3, 9))
    editor.frame()
    assert ed.doc.line(3) == 'READY = False' and editor.frame()[1].split('\n')[3] == 'READY = False'
    x, y = at(editor, 2, 9)
    editor.frame((x, y), False)
    editor.frame((x, y), True)
    for step in (10, 20, 40):
        editor.frame((x + step, y), True)
    editor.frame((x + 40, y), False)
    editor.frame()
    assert ed.doc.line(2) == 'LIMIT = 20'


def test_lead_cells_move_the_line_through_the_column_map(editor):
    ed, cw = editor.ed, editor.ed.cw
    assert ed.insets(4) == ([7], [2]) and ed.insets(3) is None, 'only the line with the colour tuple bends'
    assert ed.x(4, 6) == ed.gutter + 6 * cw and ed.x(4, 7) == ed.gutter + 9 * cw and ed.x(4, 20) == ed.gutter + 22 * cw
    assert [ed.column(4, ed.x(4, column)) for column in range(23)] == list(range(23))
    assert ed.column(4, ed.gutter + 7.6 * cw) == 7, 'a point on the added cells answers the column after them'
    swatch = next(node for node in declared(editor, 'colour_tuple'))
    assert editor.cache.graph.rect(swatch)[:3] == (ed.x(4, 7) - 2 * cw, 4 * ed.lh, 2 * cw)
    editor.click(*at(editor, 4, 12))
    assert ed.doc.place(ed.doc.caret) == (4, 12), 'the tuple itself is text: a click places the caret'
    changed, text = typed(editor, '5')
    assert text.split('\n')[4] == 'TINT = (0.2,5 0.4, 0.8)'
    keys(editor, glfw.KEY_BACKSPACE, glfw.KEY_HOME, (glfw.KEY_END, SHIFT))
    assert ed.doc.selection == (ed.doc.start(4), ed.doc.end(4))
    editor.click(*at(editor, 4, 2))
    typed(editor, 'xy')
    assert ed.insets(4) == ([9], [2]), 'the cells follow their token'
    editor.click(ed.x(4, 9) - cw, at(editor, 4, 0)[1])
    editor.frame()
    assert ed.doc.place(ed.doc.caret) == (4, 4) and declared(editor, 'picker'), 'the swatch takes its own click'


def test_widget_cells_appear_once_the_lexer_has_loaded(monkeypatch):
    """Before the lexer module is imported lines paint plain; nothing learnt then may stick."""
    syntax.tools()
    loaded = []
    monkeypatch.setattr(syntax.Syntax, 'live', property(lambda self: bool(loaded)))
    state = EditorState()
    host = Host(view.draw_editor, input_value=SOURCE, name='late', state=state, left=0., top=0., width=700., height=400.,
                language='python', fold_defaults=False)
    try:
        ed = state.editor
        assert ed.insets(4) is None and not declared(host, 'colour_tuple')
        loaded.append(True)
        host.cache.invalidate_id(next(iter(declared(host, 'draw_editor'))))
        host.frame()
        assert ed.insets(4) == ([7], [2]) and declared(host, 'colour_tuple')
    finally:
        host.close()


def test_a_widget_that_is_its_token_steps_aside_while_the_caret_is_inside(editor):
    ed = editor.ed
    before = len(declared(editor, 'drag'))
    editor.click(*at(editor, 2, 9))
    editor.frame()
    assert ed.doc.place(ed.doc.caret) == (2, 9) and ed.inside == (2, 8)
    assert len(declared(editor, 'drag')) == before - 1
    changed, text = typed(editor, '7')
    assert text.split('\n')[2] == 'LIMIT = 170' and ed.inside == (2, 8)
    keys(editor, glfw.KEY_UP)
    editor.frame()
    assert ed.inside is None and len(declared(editor, 'drag')) == before


def test_a_registered_widget_is_declared_for_its_token_kind_and_value_type(editor, monkeypatch):
    seen = []
    monkeypatch.setitem(inline.WIDGETS, 'def_name', None)
    monkeypatch.setitem(inline.WIDGETS, float, None)

    @inline.inline('def_name')
    def badge(input_value, width=0., height=0.):
        seen.append(input_value)

    @inline.inline(float, cells=6)
    def meter(input_value, width=0., height=0.):
        seen.append(input_value)

    class Store:
        __live_values__ = {('line:15#total',): 4.5, ('line:3#LIMIT',): 'ten'}
    editor.frame(live=Store)
    assert 'first' in seen and 'second' in seen and 4.5 in seen
    assert editor.ed.mark('live', 2, 'value').data.text == "'ten'" and inline.for_value(True) is None
    assert inline.for_value(2.5).cells == 6


def test_suggestions_open_while_typing_and_accept_replaces_the_word(editor):
    ed = editor.ed
    editor.click(*at(editor, 15, 9))
    keys(editor, glfw.KEY_END, glfw.KEY_ENTER)
    typed(editor, 'tot')
    assert ed.suggest.open and ed.suggest.rows[0][0] == 'total'
    assert any(editor.cache.records[node][2].__name__ == 'choices' for node in editor.cache.graph.windows())
    changed, text = keys(editor, glfw.KEY_ENTER)
    assert text.split('\n')[16] == '        total' and not ed.suggest.open
    assert editor.cache.graph.windows() == []
    typed(editor, ' os.pa')
    names = [name for name, _ in ed.suggest.rows]
    assert 'path' in names, 'members come from the live module'
    keys(editor, glfw.KEY_ESCAPE)
    assert not ed.suggest.open


def test_the_list_popover_follows_keys_hover_wheel_and_clicks(editor):
    ed, menu = editor.ed, editor.ed.suggest
    editor.click(*at(editor, 15, 9))
    keys(editor, glfw.KEY_END, glfw.KEY_ENTER)
    typed(editor, 'e')
    assert menu.open and len(menu.rows) > view.ROWS + 4 and (menu.index, menu.first) == (0, 0)
    node = declared(editor, 'choices')[0]
    x, y = editor.cache.position(node)
    assert editor.cache.graph.rect(node)[3] == view.ROWS * ed.lh, 'ten rows shown, the rest scroll'
    keys(editor, glfw.KEY_PAGE_DOWN, glfw.KEY_DOWN)
    assert (menu.index, menu.first) == (view.ROWS + 1, 2)
    editor.frame((x + 20, y + 2.5 * ed.lh), wheel=-3)
    assert menu.first == 5 and ed.pane.scroll_offset[1] == 0, 'the wheel scrolls the list only'
    assert menu.index == 7, 'and the row now under the pointer is the selection'
    editor.frame((x + 22, y + 3.5 * ed.lh))
    assert menu.index == 8, 'the row under the pointer is the selection'
    picked = menu.rows[menu.first + 1][0]
    editor.click(x + 22, y + 1.5 * ed.lh)
    editor.frame()
    assert not menu.open and ed.doc.line(16).strip() == picked, 'a click picks the row'
    assert len(UndoManager.stack.history) == 3, 'the line break, the letter, the pick'


def test_the_quick_fix_chooser_is_the_same_list(editor):
    ed = editor.ed
    editor.frame(errors=[(11, "name 'np' is not defined")], imports={11: ['import numpy as np', 'import numpy']})
    editor.click(ed.x(10, 15), at(editor, 10, 0)[1])
    editor.frame()
    assert ed.detail == 10 and [text for text, _ in ed.fixes.rows] == ['import numpy as np', 'import numpy']
    assert len(declared(editor, 'choices')) == 1 and len(declared(editor, 'note')) == 1
    keys(editor, glfw.KEY_DOWN)
    assert ed.fixes.index == 1
    changed, text = keys(editor, glfw.KEY_ENTER)
    assert changed and text.startswith('import os\nimport numpy\n') and ed.detail is None
    assert not declared(editor, 'choices')


def test_import_fix_and_error_marks(editor):
    ed = editor.ed
    editor.frame(errors=[(11, "name 'np' is not defined")], imports={11: ['import numpy as np']})
    assert ed.mark('errors', 10, 'error').data == ("name 'np' is not defined", ['import numpy as np'])
    editor.click(*at(editor, 10, 4))
    changed, text = keys(editor, (glfw.KEY_ENTER, ALT))
    assert changed and text.startswith('import os\nimport numpy as np\n')
    assert ed.doc.line_of(ed.doc.caret) == 11, 'the caret stays on its line'
    editor.frame(errors=None, imports=None)
    assert len(ed.marks['errors']) == 0
    editor.frame(errors=SyntaxError('bad', ('x', 3, 1, 'x')))
    assert ed.mark('errors', 2, 'error') is not None and len(ed.marks['errors']) == 3


def test_error_detail_popover_applies_a_clicked_fix_and_closes_on_escape(editor):
    ed = editor.ed
    editor.frame(errors=[(11, "name 'np' is not defined")], imports={11: ['import numpy as np']})
    x, y = at(editor, 10, 30)
    editor.click(x, y)
    editor.frame()
    assert ed.detail == 10 and len(editor.cache.graph.windows()) == 2
    keys(editor, glfw.KEY_ESCAPE)
    assert ed.detail is None and editor.cache.graph.windows() == []
    editor.click(x, y)
    editor.frame()
    editor.click(ed.gutter + 20, y + ed.lh * 1.5)
    changed, text = editor.frame()
    assert 'import numpy as np' in editor.ed.doc.text and ed.detail is None


def test_a_callers_marks_paint_by_shape_and_follow_edits_made_since_they_were_computed(editor, monkeypatch):
    ed, drawn = editor.ed, []
    for shape in ('squiggle', 'heat'):
        layer, painter = view.SHAPES[shape]
        monkeypatch.setitem(view.SHAPES, shape, (layer, lambda row, mark, painter=painter: drawn.append(
            (mark.shape, row.line, mark.low, mark.high)) or painter(row, mark)))
    spell = Marks([(2, Mark('squiggle', 0, 5)), (2, Mark('color', 0, 5, (1., 0., 0.), .5)), (8, Mark('heat'))],
                  version=ed.doc.version)
    editor.click(*at(editor, 1, 0))
    keys(editor, glfw.KEY_ENTER)
    typed(editor, 'ab')
    keys(editor, glfw.KEY_ESCAPE)
    editor.frame(marks={'spell': spell})
    assert ed.marks['spell'] is spell and spell.version == ed.doc.version
    assert ('squiggle', 3, 0, 5) in drawn and ('heat', 9, 0, None) in drawn, 'painted where the text is now'
    editor.click(*at(editor, 3, 2))
    typed(editor, 'x')
    assert spell.on(3)[0][1].high == 6, 'a span the edit lands in grows with it'
    stale = Marks([(0, Mark('squiggle', 0, 2))], version=-5)
    editor.frame(marks={'spell': stale})
    assert ed.marks['spell'] is spell, 'a set too old to follow is not shown'
    assert view.recolour([(0, 'abc def', 'default'), (7, '12', 'number')], [Mark('color', 2, 8, (1., 0., 0.), 1.)], {}) == [
        (0, 'ab', view.PLAIN), (2, 'c def', view.pack_color(1., 0., 0.)), (7, '1', view.pack_color(1., 0., 0.)),
        (8, '2', view.PLAIN)]


def test_a_click_beside_a_line_hits_only_what_is_painted_there(editor):
    ed = editor.ed
    y = at(editor, 7, 0)[1]
    editor.click(ed.gutter - view.FOLD / 2, y)
    label = ed.x(7, len('def first(value):')) + ed.cw * 3
    editor.click(label + 40 * ed.cw, y)
    assert ed.rows.folded(7) == 3 and ed.doc.place(ed.doc.caret) == (7, 17), 'past the label a click places the caret'
    editor.click(label, y)
    assert ed.rows.folded(7) == 0, 'the label itself unfolds'
    editor.frame(errors=[(11, 'broken')])
    editor.click(ed.x(10, 40), at(editor, 10, 0)[1])
    assert ed.detail is None and ed.doc.place(ed.doc.caret) == (10, 12)
    editor.click(ed.x(10, 15), at(editor, 10, 0)[1])
    assert ed.detail == 10


def test_file_lines_map_through_the_numbering_for_everything_a_caller_names(editor):
    ed, marks = editor.ed, Breakpoints({131})
    editor.frame(first_line=121, errors=[(131, 'broken')], breakpoints=marks, paused=124)
    assert ed.numbers.number(0) == 121 and ed.mark('errors', 10, 'error') and ed.mark('breaks', 10, 'dot')
    assert ed.mark('paused', 3, 'arrow') and ed.gutter == view.PAD + 3 * ed.cw + view.FOLD + 12
    editor.click(8, at(editor, 4, 0)[1])
    assert marks == {125, 131}, 'a gutter click toggles the file line'
    editor.click(*at(editor, 2, 0))
    keys(editor, glfw.KEY_ENTER)
    assert marks == {126, 132} and ed.mark('errors', 11, 'error')
    table = [7, 8, None, 9] + [None] * 200
    editor.frame(line_numbers=table, first_line=1, errors=[(9, 'here')], breakpoints=None, paused=None)
    assert ed.numbers.number(2) is None and ed.mark('errors', 3, 'error') and ed.gutter == view.PAD + 5 * ed.cw + view.FOLD
    assert editor.state.pane.line(3) == 9 and editor.state.pane.row(9) == 3 and editor.state.pane.row(55) is None


def test_the_look_comes_from_the_theme_and_settings_in_one_table(editor, monkeypatch):
    from meltygui.core.runtime.toggles import Tint, Toggles
    ed = editor.ed
    assert ed.look == view.look() and ed.look.caret == view.pack_color(*Tint.cursor_tint()[:3]) and ed.look.back is None
    editor.painted.clear()
    monkeypatch.setattr(Toggles.TextEditor, 'scope_guide_alpha', .9)
    editor.frame(file_tint=(.2, .4, .8))
    assert ed.look.guide == view.pack_color(.2, .4, .8, .9) and ed.look.number == view.pack_color(*Tint.line_number_tint((.2, .4, .8))[:3])
    assert ed.look.back is not None and editor.painted, 'a changed look repaints the blocks'
    editor.painted.clear()
    editor.frame()
    assert not editor.painted


def test_the_pane_handle_takes_commands_and_gives_geometry(editor):
    ed, pane = editor.ed, editor.state.pane
    assert pane is ed.handle and pane.version == ed.doc.version and pane.line_height == ed.lh and pane.char_width == ed.cw
    assert pane.origin == (ed.gutter, 0.) and pane.y(3) == 2 * ed.lh and pane.rows == ed.rows.count
    editor.click(ed.gutter - view.FOLD / 2, at(editor, 13, 0)[1])
    assert ed.rows.hidden(15) and pane.row(16) == pane.row(14) == 13
    line = SOURCE.split('\n').index('def extra_35():')
    pane.jump(line + 1, token='extra_35', select=True)
    editor.frame()
    assert ed.doc.place(ed.doc.caret) == (line, 12) and ed.doc.slice(*ed.doc.selection) == 'extra_35'
    top = ed.pane.scroll_offset[1]
    assert top < pane.row(line + 1) * ed.lh < top + 400 - ed.lh and pane.origin == (ed.gutter, -top), 'brought into view'
    assert ed.marks['flash'].on(line) == [(line, Mark('flash', 4, 12))]
    pane.jump(16)
    editor.frame()
    assert not ed.rows.hidden(15) and ed.doc.place(ed.doc.caret) == (15, 0), 'a jump into a fold opens it'
    pane.jump(100000)
    pane.select(5, 2)
    pane.focus()
    editor.frame()
    assert ed.doc.selection == (2, 5) and ed.focused
    typed(editor, 'q')
    assert ed.doc.line(0) == 'imqt os'


def test_breakpoints_toggle_in_the_gutter_and_follow_edits(editor):
    marks = Breakpoints()
    editor.frame(breakpoints=marks, paused=10)
    editor.click(8, at(editor, 9, 0)[1])
    assert marks == {10}
    editor.click(*at(editor, 2, 0))
    keys(editor, glfw.KEY_ENTER)
    assert marks == {11}
    keys(editor, glfw.KEY_BACKSPACE)
    assert marks == {10}
    editor.click(8, at(editor, 9, 0)[1])
    assert marks == set()


def test_read_only_keeps_navigation_and_copy_but_not_edits(editor):
    editor.frame(editable=False)
    editor.click(*at(editor, 2, 0))
    changed, text = typed(editor, 'x')
    keys(editor, glfw.KEY_BACKSPACE, glfw.KEY_ENTER, (glfw.KEY_V, CTRL))
    assert editor.ed.doc.text == SOURCE and not changed
    keys(editor, (glfw.KEY_RIGHT, SHIFT))
    assert editor.ed.doc.selection == (editor.ed.doc.start(2), editor.ed.doc.start(2) + 1)


def test_single_line_submits_on_enter_and_never_breaks_the_line():
    sent = []
    host = Host(view.draw_editor, input_value='query', name='field', left=0., top=0., width=300., height=22.,
                single_line=True, on_submit=sent.append, state=EditorState())
    try:
        host.click(40, 10)
        host.frame(keys=[(glfw.KEY_END, 0), (glfw.KEY_1, 0)], text='1')
        changed, text = host.frame(keys=[(glfw.KEY_ENTER, 0)])
        assert text == 'query1' and sent == ['query1']
    finally:
        host.close()


def test_ghost_text_is_accepted_with_tab_and_long_lines_scroll_sideways(editor):
    ed = editor.ed
    editor.click(*at(editor, 1, 0))
    editor.frame(ghost='# hint')
    changed, text = keys(editor, glfw.KEY_TAB)
    assert text.split('\n')[1] == '# hint'
    editor.frame(ghost='')
    typed(editor, ' ' + 'word ' * 40)
    assert ed.scroll_x > 0 and ed.where(ed.doc.caret)[0] - ed.scroll_x < 700
    keys(editor, glfw.KEY_HOME)
    assert ed.scroll_x == 0


def test_search_hits_and_external_text_replacement(editor):
    editor.frame(search='total')
    assert len(editor.ed.marks['hits']) == 3
    changed, text = editor.frame(input_value='fresh = 1\n')
    assert text == 'fresh = 1\n' and editor.ed.rows.count == 2 and not changed
    assert len(editor.ed.marks['hits']) == 0


def test_only_blocks_near_the_viewport_are_declared_and_a_new_line_only_moves_the_rest(monkeypatch):
    syntax.tools()
    painted = []
    paint = view.paint
    monkeypatch.setattr(view, 'paint', lambda ed, block, *rest: painted.append(block.key) or paint(ed, block, *rest))
    text = ''.join(f'value_{n} = {n}\n' for n in range(BLOCK * 60))
    state = EditorState()
    host = Host(view.draw_editor, input_value=text, name='long', state=state, left=0., top=0., width=700., height=400.,
                line_numbers=False)
    try:
        ed = host.ed = state.editor
        assert len(ed.doc.blocks) == 61 and len(declared(host)) == len(ed.shown) <= 6
        host.frame((300, 200), wheel=-BLOCK * 30 * ed.lh / 40)
        host.frame((300, 200))
        assert ed.pane.scroll_offset[1] == BLOCK * 30 * ed.lh and len(declared(host)) == len(ed.shown) <= 9
        assert ed.shown[0][1] >= BLOCK * 26 and len(ed.doc.line0) <= 36, 'nothing below the viewport was visited'
        host.click(*at(host, BLOCK * 30 + 3, 4))
        host.frame()
        painted.clear()
        tops = [found[2] for found in ed.shown]
        host.frame(keys=[(glfw.KEY_ENTER, 0)])
        host.frame()
        assert painted == [ed.doc.blocks[30].key], 'the edited block repaints'
        assert [found[2] for found in ed.shown[-2:]] == [top + 1 for top in tops[-2:]], 'the blocks below move down a row'
        assert ed.rows.count == BLOCK * 60 + 2 and ed.doc.blocks[30].count == BLOCK + 1
    finally:
        host.close()


def test_a_large_text_gets_its_folds_from_the_worker(monkeypatch):
    import meltygui.editor.editor as session
    syntax.tools()
    monkeypatch.setattr(session, 'INLINE', 0)
    state = EditorState()
    host = Host(view.draw_editor, input_value=SOURCE, name='async', state=state, left=0., top=0., width=700., height=400.,
                fold_defaults=False)
    try:
        ed = state.editor
        assert len(ed.folds) == 0 and ed.rows.count == ed.doc.lines, 'the first frame does not wait for the scan'
        ed.worker.settle()
        host.frame()
        assert ed.fold(7) is not None
        host.click(ed.gutter - view.FOLD / 2, (7 + .5) * ed.lh)
        assert ed.rows.folded(7) == 3
        host.frame(search='total')
        assert ed.marks['hits'] is None
        ed.worker.settle()
        host.frame()
        assert len(ed.marks['hits']) == 3
    finally:
        host.close()


def test_listening_views_get_events_and_plain_views_cost_nothing():
    from meltygui.core.rendering.retained_gui_prototype import RetainedGui
    cache = RetainedGui(graphics=False)
    got = []

    @cache.gui(width=100, height=100, listen=())
    def child(view_events=None):
        got.append(('child', dict(view_events)))

    @cache.gui(width=200, height=200, listen=('keys', 'text', 'pointer', 'focus'))
    def owner(view_events=None):
        got.append(('owner', {name: list(items) for name, items in view_events.items()}))
        child(left=50, top=50)

    class IO:
        mouse_pos = (60, 70); mouse_down = [False] * 5; keys_down = [False] * 8
        key_ctrl = key_shift = key_alt = key_super = False
        mouse_wheel = mouse_wheel_horizontal = 0.
    try:
        with cache.frame():
            owner(left=10, top=10)
        cache.imgui_input.process(IO); cache.flush()
        got.clear()
        IO.mouse_down = [True] + [False] * 4
        cache.imgui_input.key(65, 0)
        cache.imgui_input.character(ord('a'))
        cache.imgui_input.process(IO); cache.flush()
        name, events = got[-1]
        assert name == 'owner' and events['focus'] == [True] and events['keys'] == [(65, 0)] and events['text'] == ['a']
        press = events['pointer'][0]
        assert (press.kind, press.x, press.y, press.clicks) == ('press', 50, 60, 1)
        assert not any(name == 'child' for name, _ in got), 'a listening view ignores raw pointer samples'
        IO.mouse_pos = (80, 90)
        cache.imgui_input.process(IO); cache.flush()
        assert got[-1][1]['pointer'][0][:3] == ('drag', 70, 80)
        IO.mouse_down = [False] * 5
        cache.imgui_input.process(IO); cache.flush()
        assert got[-1][1]['pointer'][0].kind == 'release'
        IO.mouse_pos = (500, 500); IO.mouse_down = [True] + [False] * 4
        cache.imgui_input.process(IO); cache.flush()
        assert got[-1][1] == {'focus': [False]} and cache.imgui_input.keyboard is None
    finally:
        cache.close()


def test_pointer_buttons_hold_hover_touch_and_the_declared_cursor():
    from meltygui.core.input import mouse_cursor
    from meltygui.core.rendering.retained_gui_prototype import RetainedGui
    cache = RetainedGui(graphics=False)
    got = []

    @cache.gui(width=40, height=40, listen=('hover', 'touch'), cursor=mouse_cursor.HAND)
    def chip(view_events=None):
        got.append(('chip', {name: list(items) for name, items in view_events.items()}))

    @cache.gui(width=200, height=200, listen=('keys', 'pointer', 'hold', 'focus'), cursor=mouse_cursor.TEXT)
    def owner(view_events=None):
        got.append(('owner', {name: list(items) for name, items in view_events.items()}))
        chip(left=100, top=100)

    class IO:
        mouse_pos = (60, 70); mouse_down = [False] * 5; keys_down = [False] * 8
        key_ctrl = key_shift = key_alt = key_super = False
        mouse_wheel = mouse_wheel_horizontal = 0.

    def step(position=None, down=None):
        if position is not None:
            IO.mouse_pos = position
        if down is not None:
            IO.mouse_down = list(down) + [False] * (5 - len(down))
        got.clear()
        cache.imgui_input.process(IO)
        cache.flush()
        return {name: events for name, events in got}
    try:
        with cache.frame():
            owner(left=10, top=10)
        step()
        assert mouse_cursor._requested == mouse_cursor.TEXT, 'the hovered view declares the shape'
        found = step(down=(False, True))
        press = found['owner']['pointer'][0]
        assert (press.kind, press.button, press.x, press.y) == ('press', 1, 50, 60) and 'focus' not in found['owner']
        assert cache.imgui_input.keyboard is None, 'only a left press moves the keyboard'
        assert step((70, 70))['owner']['pointer'][0][:3] == ('drag', 60, 60)
        assert step() == {}, 'held still inside the view: nothing to do'
        release = step(down=())['owner']['pointer'][0]
        assert (release.kind, release.button) == ('release', 1)
        step(down=(True,))
        found = step((70, 260))
        assert found['owner']['pointer'][0].kind == 'drag'
        hold = step()['owner']['hold'][0]
        assert (hold.kind, hold.x, hold.y, hold.button) == ('hold', 60, 250, 0), 'held still past the edge: once a frame'
        assert step()['owner']['hold'][0].kind == 'hold' and mouse_cursor._requested == mouse_cursor.TEXT
        step(down=())
        hover = step((125, 130))['chip']['hover'][0]
        assert (hover.kind, hover.x, hover.y) == ('hover', 15, 20) and mouse_cursor._requested == mouse_cursor.HAND
        assert 'owner' not in step((126, 130)), 'a view that did not ask for hover is not run by motion'
        assert step((60, 70))['chip']['hover'][0].kind == 'leave'
        assert step((61, 70)) == {}
        cache.imgui_input.touch('down', 7, 130, 135)
        touch = step()['chip']['touch'][0]
        assert (touch.kind, touch.contact, touch.x, touch.y) == ('down', 7, 20, 25)
    finally:
        cache.close()


def test_a_selection_held_past_the_edge_scrolls_on_and_blocks_show_the_i_beam(editor, monkeypatch):
    from meltygui.core.input import mouse_cursor
    monkeypatch.setattr(view, 'CREEP', 400.)
    ed = editor.ed
    editor.frame(at(editor, 2, 3), False)
    assert mouse_cursor._requested == mouse_cursor.TEXT
    editor.frame(at(editor, 2, 3), True)
    editor.frame((200, 430), True)
    first = ed.pane.scroll_offset[1]
    for _ in range(12):
        editor.frame((200, 430), True)
    assert ed.pane.scroll_offset[1] > first + 3 * ed.lh, 'the pointer rests below the view: the selection keeps growing'
    low, high = ed.doc.selection
    assert ed.doc.place(low) == (2, 3) and ed.doc.line_of(high) > 20
    editor.frame((200, 430), False)
    rest = ed.pane.scroll_offset[1]
    editor.frame((200, 430), False)
    assert ed.pane.scroll_offset[1] == rest and ed.hold is None


def test_fold_layers_keep_their_own_collapsed_state_over_one_row_mapping(editor):
    ed, state = editor.ed, editor.state
    rows = ed.rows.count
    editor.frame(folds=[(22, 30), (42, 60)])
    assert ed.rows.count == rows - 26 and state.gaps == [(22, 30), (42, 60)] and state.collapsed == []
    assert ed.fold(22)[0] is ed.extra and ed.fold(7)[0] is ed.folds and ed.layers == (ed.folds, ed.extra)
    editor.click(ed.gutter - view.FOLD / 2, at(editor, 7, 0)[1])
    editor.click(ed.gutter - view.FOLD / 2, at(editor, 22, 0)[1])
    assert state.collapsed == [('scope', 'first')] and state.gaps == [(42, 60)], 'each layer toggles on its own'
    assert ed.rows.count == rows - 18 - 3 and not ed.rows.hidden(25) and ed.rows.hidden(9) and ed.rows.hidden(50)
    editor.frame(folds=[(23, 31), (43, 61), (102, 110)])
    assert state.gaps == [(43, 61), (102, 110)], 'a drifted range the user opened stays open; a new one starts closed'
    fresh = EditorState()
    fresh.collapsed, fresh.gaps = list(state.collapsed), list(state.gaps)
    host = Host(view.draw_editor, input_value=SOURCE, name='again', state=fresh, left=0., top=0., width=700., height=400.,
                fold_defaults=False, folds=[(23, 31), (43, 61), (102, 110)])
    try:
        again = fresh.editor
        assert again.rows.hidden(9) and again.rows.hidden(50) and not again.rows.hidden(25) and again.rows.count == ed.rows.count
    finally:
        host.close()


def test_the_viewport_snapshot_paints_before_the_text_arrives(editor):
    from meltygui.state.editor_state import LOADING
    ed, state = editor.ed, editor.state
    editor.click(ed.gutter - view.FOLD / 2, at(editor, 7, 0)[1])
    editor.frame((300, 200), wheel=-90)
    editor.frame((300, 200))
    editor.click(*at(editor, 160, 4))
    scroll = ed.pane.scroll_offset[1]
    first, rows = state.view_first, state.view_rows
    shown = [ed.doc.line(ed.rows.line(row)) for row in range(first, first + 5)]
    assert rows == ed.rows.count and first > 0 and state.view_text.split('\n')[:5] == shown
    assert state.view_numbers[:2] == [ed.rows.line(first) + 1, ed.rows.line(first + 1) + 1]
    fresh = EditorState()
    for name in ('caret', 'anchor', 'collapsed', 'gaps', 'view_first', 'view_rows', 'view_text', 'view_numbers'):
        setattr(fresh, name, getattr(state, name))
    host = Host(view.draw_editor, input_value=LOADING, name='restored', state=fresh, left=0., top=0., width=700., height=400.,
                fold_defaults=False)
    try:
        standin = fresh.editor
        assert standin.standin and standin.rows.count == rows, 'the same height: the scroll position lands where it was'
        assert [standin.doc.line(row) for row in range(first, first + 5)] == shown and standin.doc.line(0) == ''
        assert standin.numbers.number(first) == state.view_numbers[0] and standin.numbers.number(0) is None
        host.click(300, 100)
        changed, value = host.frame(keys=[(glfw.KEY_A, 0)], text='a')
        assert not changed and value is LOADING and standin.doc.line(standin.doc.line_of(standin.doc.caret)) != 'a'
        assert (fresh.caret, fresh.view_first, fresh.view_text) == (state.caret, first, state.view_text), 'nothing saved from the stand-in'
        changed, text = host.frame(input_value=SOURCE)
        real = fresh.editor
        assert real is not standin and not real.standin and text == SOURCE and not changed
        assert real.doc.caret == state.caret and real.rows.folded(7) == 3 and real.rows.count == rows
        assert scroll > 0
    finally:
        host.close()
