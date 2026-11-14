"""The focus stack (core/input/focus.py): temporary popups (`popup=` on a call)
and who has the keyboard, on a real window host (hidden GL window, reserved
desktop). A text field holds an anchor, as an editor holds its inline widgets;
the anchor opens a list that takes the keyboard or a panel that does not, and
the list opens a popup of its own."""
import meltygui_imgui as imgui
import pytest

import meltygui.core.windowing.window_constants as glfw
from editor_host import Host
from meltygui.core.rendering.gui_prototype import gui

LOG = []            # (view, name, item) of what each view's event said: keys, text, focus gained / lost, a left press
RUNS = []           # (view, count, kept) of each popup body run
FIELD, ANCHOR, POPUP, SUB, OTHER, NOWHERE = (20., 12.), (220., 12.), (230., 50.), (330., 50.), (20., 210.), (500., 400.)


def note(view, event, names=('keys', 'text', 'focus')):
    """Ask `event` about each of `names` and log what it says."""
    if 'keys' in names:
        LOG.extend((view, 'keys', item) for item in event.keys())
    if 'text' in names and event.text():
        LOG.append((view, 'text', event.text()))
    if 'focus' in names:
        if event.focus_gained():
            LOG.append((view, 'focus', True))
        if event.focus_lost():
            LOG.append((view, 'focus', False))
    if 'pointer' in names and event.left_mouse_down():
        LOG.append((view, 'pointer', 'press'))


@gui(width=80, height=40)
def sub(input_value, event=None):
    event.left_mouse_down()         # asked: a press inside is this popup's
    return False, input_value


@gui(no_save=('count',))
def menu(input_value, count=0, kept=0, width=0., height=0., draw_state=None, cache=None, event=None):
    """A list that takes the keyboard (it asks for keys): Enter picks, a press opens its own popup beside it."""
    note('menu', event)
    draw_state.count, draw_state.kept = count + 1, kept + 1
    RUNS.append(('menu', count, kept))
    sub(None, name='sub', popup=event.left_mouse_down(), left=width, top=0.)
    if event.key_down(glfw.KEY_ENTER):
        cache.focus.close()
        return True, 'picked'
    return False, input_value


@gui()
def panel(input_value, width=0., height=0., event=None):
    """A popup that leaves the keyboard where it is: it asks for no keys."""
    event.left_mouse_down()         # asked: a press inside is this popup's
    return False, input_value


@gui(width=80, height=24)
def anchor(input_value, kind='menu', event=None):
    shown = menu if kind == 'menu' else panel
    changed, value, state = shown(input_value, name='popup', popup=event.left_mouse_down(), return_extras=True,
                                  left=0., top=26., width=100., height=60.)
    LOG.append(('anchor', 'open', state is not None))
    return changed, value


@gui(width=320, height=24)
def field(input_value, kind='menu', event=None):
    note('field', event, ('keys', 'text', 'focus', 'pointer'))
    return anchor(input_value, name='anchor', kind=kind, left=200., top=0.)


@gui(width=120, height=24)
def other(input_value, event=None):
    note('other', event, ('keys', 'focus', 'pointer'))
    return False, input_value


def app(kind='menu', show=True, low=0.):
    result = (False, None)
    if show:
        result = field('value', name='field', kind=kind, left=0., top=low)
    other(None, name='other', left=0., top=200.)
    return result


@pytest.fixture
def host():
    LOG.clear()
    RUNS.clear()
    host = Host(app)
    yield host
    host.close()


def declared(host, name):
    return [node for node, record in host.cache.records.items() if record[2].__name__ == name]


def node(host, name):
    return declared(host, name)[0]


def got(view, name):
    return [item for seen, kind, item in LOG if seen == view and kind == name]


def open_from_field(host):
    """The field has the keyboard, then its anchor is pressed."""
    host.click(*FIELD)
    host.click(*ANCHOR)
    host.frame()


def test_a_popup_is_declared_only_while_it_is_open(host):
    cache = host.cache
    assert not declared(host, 'menu') and not cache.focus.stack and got('anchor', 'open') == [False]
    host.click(*ANCHOR)
    host.frame()
    popup = node(host, 'menu')
    assert [found.node for found in cache.focus.stack] == [popup] and got('anchor', 'open')[-1] is True
    assert cache.graph.is_portal(popup) and cache.title_height(popup) == 0., 'a Melty window without a title'
    assert cache.window_rect(popup) == (200., 26., 100., 60.), 'where its caller put it, in the anchor\'s coordinates'
    arguments = cache.invocations[popup][1]
    assert arguments['movable'] is False and arguments['resizable'] is False
    host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
    host.frame()
    assert not declared(host, 'menu') and not cache.focus.stack and got('anchor', 'open')[-1] is False


def test_a_press_outside_closes_it_and_still_reaches_what_is_under_it(host):
    open_from_field(host)
    host.click(*POPUP)
    host.frame()
    assert declared(host, 'menu'), 'a press inside keeps it'
    host.click(*OTHER)
    host.frame()
    assert not declared(host, 'menu') and not host.cache.focus.stack
    assert host.cache.focus.keyboard == node(host, 'other') and got('other', 'focus') == [True], 'the press was the other view\'s too'
    assert got('other', 'pointer') == ['press']


def test_a_press_on_the_anchor_toggles(host):
    for expected in (True, False, True, False):
        host.click(*ANCHOR)
        host.frame()
        assert bool(declared(host, 'menu')) is expected


def test_a_popup_that_asks_for_keys_takes_the_keyboard_and_gives_it_back(host):
    focus = host.cache.focus
    open_from_field(host)
    assert focus.keyboard == node(host, 'menu') and got('field', 'focus') == [True, False]
    assert focus.within(node(host, 'field')) and not focus.within(node(host, 'other')), 'still within the field: its own popup has it'
    host.frame(keys=[(glfw.KEY_DOWN, 0)], text='x')
    assert got('menu', 'keys') == [(glfw.KEY_DOWN, 0)] and not got('field', 'text'), 'typing is the popup\'s'
    host.frame(keys=[(glfw.KEY_ENTER, 0)])
    host.frame()
    assert not declared(host, 'menu') and focus.keyboard == node(host, 'field') and got('field', 'focus') == [True, False, True]
    host.frame(text='y')
    assert got('field', 'text') == ['y'], 'the field types again'


def test_what_a_popup_returns_as_it_closes_reaches_its_caller(host):
    open_from_field(host)
    results = []
    for keys in ([(glfw.KEY_ENTER, 0)], (), ()):
        results.append(host.frame(keys=keys))
    assert (True, 'picked') in results, 'the pick came out of the field'
    assert results[-1] == (False, 'value'), 'once'


def test_a_popup_that_does_not_ask_for_keys_leaves_the_keyboard(host):
    focus = host.cache.focus
    host.frame(kind='panel')
    open_from_field(host)
    field_node = node(host, 'field')
    assert declared(host, 'panel') and focus.keyboard == field_node and got('field', 'focus') == [True]
    host.frame(text='a')
    host.click(*POPUP)
    host.frame(text='b')
    assert got('field', 'text') == ['a', 'b'] and focus.keyboard == field_node, 'also after a press in the popup'
    host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
    host.frame()
    assert not declared(host, 'panel') and focus.keyboard == field_node and got('field', 'focus') == [True]


def test_escape_closes_the_innermost_popup_and_is_not_a_key(host):
    open_from_field(host)
    host.click(*POPUP)
    host.frame()
    assert declared(host, 'sub') and [found.node for found in host.cache.focus.stack] == [node(host, 'menu'), node(host, 'sub')]
    host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
    host.frame()
    assert not declared(host, 'sub') and declared(host, 'menu')
    host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
    host.frame()
    assert not declared(host, 'menu')
    assert not got('menu', 'keys') and not got('field', 'keys'), 'no view saw those'
    host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
    assert got('field', 'keys') == [(glfw.KEY_ESCAPE, 0)], 'with nothing open Escape is a key like any other'


def test_a_press_in_a_popup_opened_from_a_popup_keeps_its_parent(host):
    open_from_field(host)
    host.click(*POPUP)
    host.frame()
    host.click(*SUB)
    host.frame()
    assert declared(host, 'sub') and declared(host, 'menu'), 'the child is below the parent'
    host.click(POPUP[0] + 40., POPUP[1])
    host.frame()
    assert declared(host, 'menu') and not declared(host, 'sub'), 'a press in the parent closes the child (and toggles it)'
    host.click(*POPUP)
    host.frame()
    assert declared(host, 'sub')
    host.click(*NOWHERE)
    host.frame()
    assert not declared(host, 'sub') and not declared(host, 'menu') and not host.cache.focus.open


def test_every_opening_starts_afresh_but_saved_state_stays(host):
    open_from_field(host)
    host.frame(keys=[(glfw.KEY_DOWN, 0)])
    assert RUNS[0] == ('menu', 0, 0) and RUNS[-1][1] > 0
    host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
    host.frame()
    kept = RUNS[-1][2]
    del RUNS[:]
    host.click(*ANCHOR)
    host.frame()
    assert RUNS[0] == ('menu', 0, kept + 1), 'count (no_save) is its default again, kept goes on'


def test_the_keyboard_leaving_a_popup_that_held_it_closes_it(host):
    focus = host.cache.focus
    open_from_field(host)
    focus.take(node(host, 'other'))
    host.frame()
    host.frame()
    assert not declared(host, 'menu') and focus.keyboard == node(host, 'other'), 'the taker keeps it: nothing is given back'


def test_a_popup_goes_with_the_view_that_declared_it(host):
    focus = host.cache.focus
    open_from_field(host)
    host.frame(show=False)
    host.frame()
    assert not declared(host, 'menu') and not focus.stack and not focus.open and focus.keyboard is None
    host.frame(show=True)
    host.frame()
    assert not declared(host, 'menu'), 'it does not come back open'


def test_a_popup_stays_on_the_surface(host):
    wide, tall = imgui.get_io().display_size
    host.frame(low=tall - 30.)
    host.click(ANCHOR[0], tall - 18.)
    host.frame()
    x, y, w, h = host.cache.window_rect(node(host, 'menu'))
    assert (x, y + h) == (200., tall - 30. - 2.), 'no room below the anchor: above it, by the same gap'
    host.click(POPUP[0], y + 10.)
    host.frame()
    x, y, w, h = host.cache.window_rect(node(host, 'sub'))
    assert x + w <= wide and y >= 0 and y + h <= tall


def test_a_closed_popup_costs_no_node_and_no_body(host):
    cache = host.cache
    nodes, runs = len(cache.records), cache.executor.runs
    for _ in range(5):
        host.frame()
    assert len(cache.records) == nodes and cache.executor.runs == runs and not cache.focus.shut
