"""Ctrl+B in the native editor view, on a hidden GL window (reserved desktop): the key, the jump,
the usage picker and the caller's part for places in other files."""
import time

import pytest

import meltygui.core.windowing.window_constants as glfw
import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.editor import lookup, syntax
from meltygui.editor.marks import Mark
from meltygui.model.roster_model import Roster
from meltygui.model.source_model import Source
from meltygui.state.editor_state import EditorState
from test_editor_lookup import FILES, place
from test_editor_view import at, declared, keys

CTRL = glfw.MOD_CONTROL
# scene.py with its last use of `build` far below the first screen.
SCENE = FILES['scene.py'] + ''.join(f'\n\ndef extra_{n}():\n    return {n}\n' for n in range(40)) + '\n\ntail = build(1)\n'
TAIL = SCENE.split('\n').index('tail = build(1)')


@pytest.fixture
def project(tmp_path):
    """open(name, **arguments): a host with the editor on that file of a small project, given its roster."""
    syntax.tools()
    texts = {**FILES, 'scene.py': SCENE}
    for name, text in texts.items():
        (tmp_path / name).write_text(text)
    roster = Roster(str(tmp_path), paths=[str(tmp_path / name) for name in texts], roots=[str(tmp_path)])
    roster.settle()
    hosts = []

    def open(name, **arguments):
        source, state = Source(texts[name], str(tmp_path / name)), EditorState()
        roster.join(source)
        host = Host(view.draw_editor, input_value=source, name=name, state=state, left=0., top=0., width=700., height=400.,
                    fold_defaults=False, path=str(tmp_path / name), roster=roster, **arguments)
        host.ed, host.state, host.folder = state.editor, state, tmp_path
        hosts.append(host)
        return host
    yield open
    for host in hosts:
        host.close()
    roster.close()


def press_b(host):
    """Ctrl+B, then the frames until its answer was taken: the lookup runs on a pool thread and wakes the view."""
    host.frame(keys=[(glfw.KEY_B, CTRL)])
    ed = host.ed
    for _ in range(200):
        if ed.lookup.pending is None and not ed.lookup.owed:
            break
        if ed.lookup.pending is not None:
            ed.lookup.pending.result()
        time.sleep(.002)
        host.frame()
    host.frame()
    return host.result


def test_ctrl_b_on_a_use_jumps_to_its_definition_out_of_sight_and_flashes_it(project):
    editor = project('scene.py')
    ed = editor.ed
    editor.state.pane.focus()
    editor.state.pane.jump(TAIL + 1, token='build')
    editor.frame()
    editor.frame()
    assert ed.doc.place(ed.doc.caret) == (TAIL, 7) and ed.pane.scroll_offset[1] > 1000
    editor.frame(keys=[(glfw.KEY_B, CTRL)])
    assert ed.doc.place(ed.doc.caret) == (TAIL, 7), 'the press only asks: nothing is looked up in its frame'
    changed, value = press_b(editor)
    assert ed.doc.place(ed.doc.caret) == (5, 4) and ed.doc.caret == ed.doc.anchor and not changed, 'the caret is on the name of the definition'
    assert ed.pane.scroll_offset[1] == 0 and ed.marks['flash'].on(5) == [(5, Mark('flash', 4, 9))], 'brought into view and flashed'
    assert editor.state.caret == ed.doc.caret and not ed.lookup.open and declared(editor, 'choices') == []


def test_ctrl_b_on_a_definition_opens_the_picker_and_enter_jumps_into_a_fold(project):
    editor = project('scene.py')
    ed, menu = editor.ed, editor.ed.lookup
    editor.click(ed.gutter - view.FOLD / 2, at(editor, 10, 0)[1])
    assert ed.rows.hidden(11), 'the scope holding two of the uses is collapsed'
    editor.click(*at(editor, 5, 6))
    press_b(editor)
    assert menu.open and menu.rows == [('return build(count), build(count + 1)', 'scene.py:12')] * 2 + [('tail = build(1)', f'scene.py:{TAIL + 1}')]
    node = declared(editor, 'choices')[0]
    assert editor.cache.graph.rect(node)[3] == 3 * ed.lh and node in editor.cache.graph.windows(), 'the picker is the list popover'
    assert ed.doc.place(ed.doc.caret) == (5, 6), 'several uses: no jump yet'
    keys(editor, glfw.KEY_DOWN)
    assert menu.index == 1
    changed, value = keys(editor, glfw.KEY_ENTER)
    assert not changed and value.text == SCENE, 'Enter picks: it is no line break'
    assert not menu.open and ed.doc.place(ed.doc.caret) == (11, 25) and not ed.rows.hidden(11), 'the fold over the place opened'
    assert ed.marks['flash'].on(11) == [(11, Mark('flash', 25, 30))]
    editor.frame()
    assert declared(editor, 'choices') == []


def test_the_picker_closes_on_escape_and_on_a_caret_move_and_a_click_picks(project):
    editor = project('scene.py')
    ed, menu = editor.ed, editor.ed.lookup
    editor.click(*at(editor, 5, 6))
    press_b(editor)
    assert menu.open
    keys(editor, glfw.KEY_ESCAPE)
    assert not menu.open and ed.doc.place(ed.doc.caret) == (5, 6)
    press_b(editor)
    keys(editor, glfw.KEY_RIGHT)
    assert not menu.open and ed.doc.place(ed.doc.caret) == (5, 7), 'a key that is not the list\'s closes it and does its own'
    press_b(editor)
    x, y = editor.cache.position(declared(editor, 'choices')[0])
    editor.frame((x + 22, y + 1.5 * ed.lh))
    assert menu.index == 1, 'the row under the pointer is the selection'
    editor.click(x + 22, y + 2.5 * ed.lh)
    editor.frame()
    assert not menu.open and ed.doc.place(ed.doc.caret) == (TAIL, 7), 'a click on a row jumps there'
    assert ed.pane.scroll_offset[1] > 1000


def test_a_place_in_another_file_is_handed_to_the_caller(project):
    places = []
    editor = project('scene.py', on_open=lambda *where: places.append(where))
    ed = editor.ed
    editor.click(*at(editor, *place(SCENE, 'Circle(index)')))
    caret = ed.doc.caret
    press_b(editor)
    assert places == [(str(editor.folder / 'shapes.py'), 1, 6)] and ed.doc.caret == caret, 'a file line and a column from 0'
    # A definition used in this file and in the other: the picker lists both, the caller gets the other file's.
    other = project('shapes.py', on_open=lambda *where: places.append(where))
    other.click(*at(other, 5, 6))
    press_b(other)
    menu = other.ed.lookup
    assert [tag for _, tag in menu.rows] == ['shapes.py:11', 'shapes.py:11', 'scene.py:1', 'scene.py:3', 'scene.py:8']
    keys(other, glfw.KEY_DOWN, glfw.KEY_DOWN, glfw.KEY_DOWN, glfw.KEY_ENTER)
    assert places[1:] == [(str(editor.folder / 'scene.py'), 3, 7)] and not menu.open and other.ed.doc.place(other.ed.doc.caret) == (5, 6)
    # Without the caller's part the place is listed, and picking it only flashes the name.
    lone = project('shapes.py')
    lone.click(*at(lone, 5, 6))
    press_b(lone)
    assert len(lone.ed.lookup.rows) == 5
    keys(lone, glfw.KEY_PAGE_DOWN, glfw.KEY_ENTER)
    assert lone.ed.doc.place(lone.ed.doc.caret) == (5, 6) and lone.ed.marks['flash'].on(5) == [(5, Mark('flash', 4, 10, lookup.MISS))]
    press_b(lone)
    keys(lone, glfw.KEY_ENTER)
    assert lone.ed.doc.place(lone.ed.doc.caret) == (10, 11), 'the buffer\'s own places still jump'


def test_nothing_to_go_to_flashes_red_and_a_read_only_view_still_jumps(project, monkeypatch):
    import meltygui.editor.editor as session
    monkeypatch.setattr(session, 'FLASH', .02)
    editor = project('scene.py', editable=False)
    ed = editor.ed
    editor.click(*at(editor, 7, 6))
    press_b(editor)
    assert ed.marks['flash'].on(7) == [(7, Mark('flash', 4, 10, lookup.MISS))], 'a keyword leads nowhere: the word says so'
    time.sleep(.06)
    editor.frame()
    assert ed.marks['flash'] is None, 'the flash ends by itself'
    editor.click(*at(editor, 11, 13))
    press_b(editor)
    assert ed.doc.place(ed.doc.caret) == (5, 4) and editor.result[1].text == SCENE


def test_a_single_line_field_has_no_lookup():
    syntax.tools()
    state = EditorState()
    host = Host(view.draw_editor, input_value='build', name='field', state=state, left=0., top=0., width=300., height=24.,
                single_line=True, focus=True)
    try:
        host.frame()
        host.frame(keys=[(glfw.KEY_B, CTRL)])
        assert state.editor.lookup.pending is None and not state.editor.lookup.owed and host.result == (False, 'build')
    finally:
        host.close()
