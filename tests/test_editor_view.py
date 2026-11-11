"""The native editor view end to end, on a hidden GL window (reserved desktop)."""
import pytest

import meltygui.core.windowing.window_constants as glfw
import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.editor import inline, syntax
from meltygui.editor.editor import BLOCK
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


@pytest.fixture
def editor(monkeypatch):
    syntax.tools()
    painted = []
    paint = view.paint
    monkeypatch.setattr(view, 'paint', lambda ed, index, *rest: painted.append(index) or paint(ed, index, *rest))
    state = EditorState()
    host = Host(view.draw_editor, input_value=SOURCE, name='editor', state=state, left=0., top=0.,
                width=700., height=400., fold_defaults=False)
    host.ed, host.painted, host.state = state.editor, painted, state
    yield host
    host.close()


def at(host, line, column):
    """Window point of a document position."""
    ed = host.ed
    return (ed.gutter + (column + .25) * ed.cw - ed.scroll_x,
            (ed.rows.row(line) + .5) * ed.lh - host.ed.pane.scroll_offset[1])


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
    assert set(editor.painted) == {0}, 'only the block holding the line repaints'
    changed, text = keys(editor, glfw.KEY_BACKSPACE)
    assert text.split('\n')[2] == 'LIMITx = 10'
    keys(editor, (glfw.KEY_Z, CTRL))
    changed, text = keys(editor, (glfw.KEY_Z, CTRL))
    assert text == SOURCE and editor.frame()[0] is False
    changed, text = keys(editor, (glfw.KEY_Z, CTRL | SHIFT))
    assert text.split('\n')[2] == 'LIMITxy = 10'


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
    keys(editor, (glfw.KEY_Z, CTRL))
    editor.click(x, at(editor, 7, 0)[1])
    assert ed.rows.count == rows and ed.folds.closed() == []
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
    keys(editor, (glfw.KEY_Z, CTRL))
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
    assert editor.ed.live[2][0].text == "'ten'" and inline.for_value(True) is None
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


def test_import_fix_and_error_marks(editor):
    ed = editor.ed
    editor.frame(errors=[(11, "name 'np' is not defined")], imports={11: ['import numpy as np']})
    assert ed.errors == {10: "name 'np' is not defined"} and ed.fixes == {10: ['import numpy as np']}
    editor.click(*at(editor, 10, 4))
    changed, text = keys(editor, (glfw.KEY_ENTER, ALT))
    assert changed and text.startswith('import os\nimport numpy as np\n')
    assert ed.doc.line_of(ed.doc.caret) == 11, 'the caret stays on its line'
    editor.frame(errors=None, imports=None)
    assert ed.errors == {} and ed.fixes == {}
    editor.frame(errors=SyntaxError('bad', ('x', 3, 1, 'x')))
    assert list(ed.errors) == [2]


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
    assert len(editor.ed.hits) == 3
    changed, text = editor.frame(input_value='fresh = 1\n')
    assert text == 'fresh = 1\n' and editor.ed.rows.count == 2 and not changed
    assert editor.ed.hits == []


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
