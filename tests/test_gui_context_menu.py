"""`@context_menu` (core/input/context_menu.py): a right click shows the options of the view under
the pointer and of the views around it, as a popup of the innermost view that has any.

The app: `outer` (a menu) holds `inner` (a menu, passed a value and a label), `asker` (uses the
right click itself) with `nested` (a menu) in it, and `plain` (nothing). `toolbar` is an immediate
view with a menu, run at the root and inside `outer`: it is in every test, and has no menu to show.
`opener` opens `panel`, a popup with `chip` (a menu) in it, and `loose` is a `**kwargs` view.
The tests drive the real router through the GL test host, frame by frame.
"""
import pytest

import meltygui.core.windowing.window_constants as glfw
from editor_host import Host
from meltygui.core.input import context_menu as menus
from meltygui.core.input.context_menu import context_menu
from meltygui.core.rendering.gui_prototype import AFTER, _renderer_name, gui
from meltygui.view import choice_view
from meltygui.view.context_menu_view import GAP, draw_context_menu
from meltygui.view.common.value_view import ROW

PICKS = []          # what the options did
RUNS = []           # the bodies that ran
INNER, NESTED, PLAIN, EMPTY = (30.0, 20.0), (40.0, 110.0), (200.0, 20.0), (500.0, 400.0)
ASKER = (140.0, 110.0)      # on `asker`, beside `nested`
OPENER, CHIP, LOOSE = (20.0, 210.0), (30.0, 245.0), (220.0, 210.0)


def rename(input_value, label, draw_state):
    PICKS.append(('rename', input_value, label, draw_state.label))


def needs(missing):
    PICKS.append(('needs', missing))


@context_menu({'Rename': rename, 'Plain': lambda: PICKS.append('plain'), 'Broken': needs})
@gui(width=120, height=30)
def inner(input_value, label=''):
    RUNS.append('inner')


@context_menu({'Nested': lambda: PICKS.append('nested')})
@gui(width=80, height=30)
def nested(input_value):
    RUNS.append('nested')


@gui(width=160, height=40)
def asker(input_value, event=None):
    if event.right_mouse_clicked():
        PICKS.append('asker clicked')
    nested(None, name='nested', left=10.0, top=5.0)


@gui(width=100, height=30)
def plain(input_value):
    RUNS.append('plain')


@context_menu({'Immediate': lambda: PICKS.append('immediate')})
@gui(use_cache=False)
def toolbar(input_value):
    RUNS.append('toolbar')


@context_menu({'Chip': lambda: PICKS.append('chip')})
@gui(width=80, height=24)
def chip(input_value):
    RUNS.append('chip')


@gui(width=120, height=60)
def panel(input_value, event=None):
    event.left_mouse_down()         # asked: a press inside is this popup's
    chip(None, name='chip', left=10.0, top=10.0)


@gui(width=100, height=24)
def opener(input_value, event=None):
    panel(None, name='panel', popup=event.left_mouse_down(), left=0.0, top=30.0)


@context_menu({'Loose': lambda: PICKS.append('loose')})
@gui(width=100, height=24)
def loose(input_value, **rest):
    RUNS.append('loose')


@context_menu({'Outer': lambda cache: PICKS.append(('outer', cache is not None))})
@gui(width=400, height=180)
def outer(input_value, show=True):
    RUNS.append('outer')
    toolbar(None, name='inside')
    if show:
        inner('row', name='inner', label='first', left=10.0, top=10.0)
    asker(None, name='asker', left=20.0, top=100.0)
    plain(None, name='plain', left=180.0, top=10.0)


def app(show=True, root=False):
    toolbar(None, name='root')
    outer(None, name='outer', show=show, left=0.0, top=0.0)
    opener(None, name='opener', left=10.0, top=200.0)
    loose(None, name='loose', left=200.0, top=200.0)
    if root:
        menus.show(HOST[0].cache, 0)        # what the surface does for an immediate root


HOST = []


@pytest.fixture
def host():
    PICKS.clear()
    RUNS.clear()
    host = Host(app)
    HOST[:] = [host]
    host.frame()
    yield host
    host.close()


def named(host, view):
    body = view.__gui_definition__[0]
    return [node for node, record in host.cache.records.items() if record[2] is body]


def right_click(host, at, drag=(0.0, 0.0)):
    """Press the right button at `at`, move by `drag`, release, and let the frame after it run."""
    host.frame(at)
    host.io.mouse_down[1] = True
    host.frame(at)
    end = (at[0] + drag[0], at[1] + drag[1])
    host.frame(end)
    host.io.mouse_down[1] = False
    host.frame(end)
    host.frame(end)


def menu(host):
    """The open menu's node, or None."""
    found = named(host, draw_context_menu)
    return found[0] if found else None


def row(host, at):
    """The surface position of the menu's row `at` (rows of the first section)."""
    x, y = host.cache.position(menu(host))
    return x + 10.0, y + at * ROW + ROW * 0.5


def labels(host):
    return [list(options) for _, options in host.cache.imgui_input.menu.sections]


def test_a_right_click_opens_the_menu_as_a_popup_of_the_view_it_is_on(host):
    right_click(host, INNER)
    node = menu(host)
    assert node is not None
    assert host.cache.graph.parent(node) == named(host, inner)[0]       # a child of the clicked view
    assert host.cache.position(node) == INNER                           # at the pointer
    # The clicked view's options first, then those of the view around it.
    assert labels(host) == [['Rename', 'Plain', 'Broken'], ['Outer']]
    assert [view for view, _ in host.cache.imgui_input.menu.sections] == [named(host, inner)[0], named(host, outer)[0]]
    assert host.cache.graph.rect(node)[2:] == (host.cache.imgui_input.menu.width, 4 * ROW + GAP)


def test_a_view_without_a_menu_shows_the_menu_of_the_view_around_it(host):
    right_click(host, PLAIN)
    assert labels(host) == [['Outer']]                  # (and nothing of the immediate view inside `outer`)
    assert host.cache.graph.parent(menu(host)) == named(host, outer)[0]
    assert RUNS.count('toolbar') > 2


def test_nothing_under_the_pointer_has_a_menu(host):
    right_click(host, EMPTY)
    assert menu(host) is None and host.cache.imgui_input.menu is None


def test_a_pick_calls_the_option_with_the_parameters_of_its_view_and_closes(host):
    right_click(host, INNER)
    RUNS.clear()
    host.click(*row(host, 0))
    host.frame()
    # `input_value` is what the caller passed, read through the view's state; `label` and `draw_state` are its own.
    assert PICKS == [('rename', 'row', 'first', 'first')]
    assert menu(host) is None and host.cache.imgui_input.menu is None
    # The option may have changed what they show: its view and the one around it ran again.
    assert 'inner' in RUNS and 'outer' in RUNS and 'plain' not in RUNS


def test_an_option_that_takes_nothing_is_just_called_and_an_outer_one_gets_what_it_names(host):
    right_click(host, INNER)
    host.click(*row(host, 1))
    right_click(host, INNER)
    x, y = host.cache.position(menu(host))
    host.click(x + 10.0, y + 3 * ROW + GAP + ROW * 0.5)        # the outer view's row, below the line
    assert PICKS == ['plain', ('outer', True)]


def test_the_arrows_and_enter_pick_and_escape_closes(host):
    right_click(host, INNER)
    host.frame(keys=[(glfw.KEY_DOWN, 0)])
    host.frame(keys=[(glfw.KEY_DOWN, 0)])
    host.frame(keys=[(glfw.KEY_ENTER, 0)])
    host.frame()
    assert PICKS == ['plain'] and menu(host) is None
    right_click(host, INNER)
    host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
    host.frame()
    assert menu(host) is None and host.cache.imgui_input.menu is None and PICKS == ['plain']


def test_a_press_outside_closes_it_and_a_right_click_elsewhere_opens_it_there(host):
    right_click(host, INNER)
    host.click(*EMPTY)
    host.frame()
    assert menu(host) is None and host.cache.imgui_input.menu is None
    right_click(host, INNER)
    right_click(host, (16.0, 14.0))             # on the same view, beside the open menu
    assert menu(host) is not None and host.cache.position(menu(host)) == (16.0, 14.0)
    right_click(host, PLAIN)                    # on another view: the menu is that one's now
    assert labels(host) == [['Outer']] and len(named(host, draw_context_menu)) == 1


def test_a_right_click_in_the_open_menu_is_the_menus(host):
    right_click(host, INNER)
    before = host.cache.position(menu(host))
    right_click(host, row(host, 1))
    assert host.cache.position(menu(host)) == before and PICKS == []


def test_a_right_drag_is_not_a_click(host):
    right_click(host, INNER, drag=(30.0, 0.0))
    assert menu(host) is None and host.cache.imgui_input.menu is None


def test_the_nearer_of_a_menu_and_a_view_that_uses_the_right_click_takes_it(host):
    right_click(host, ASKER)                    # on the asking view: it is nearer than `outer`'s menu
    assert PICKS == ['asker clicked'] and menu(host) is None
    PICKS.clear()
    right_click(host, NESTED)                   # on the view inside it that has a menu: that is nearer
    assert PICKS == [] and labels(host) == [['Nested'], ['Outer']]


def test_a_menu_on_a_view_in_a_popup_is_below_that_popup_which_stays_open(host):
    host.click(*OPENER)
    host.frame()
    assert named(host, panel)
    right_click(host, CHIP)
    assert named(host, panel) and labels(host) == [['Chip']]
    assert host.cache.graph.parent(menu(host)) == named(host, chip)[0]
    host.click(*row(host, 0))
    host.frame()
    assert PICKS == ['chip'] and menu(host) is None
    # (The popup closes with the menu here, because it takes no keys: bug 52bf7426ee1f.)


def test_a_view_with_keyword_arguments_of_any_name_has_its_menu(host):
    right_click(host, LOOSE)
    assert labels(host) == [['Loose']]
    host.click(*row(host, 0))
    host.frame()
    assert PICKS == ['loose']


def test_the_menu_goes_with_the_view_it_is_on(host):
    right_click(host, INNER)
    host.frame(show=False)                      # `outer` no longer declares `inner`
    host.frame()
    assert menu(host) is None


def test_an_option_that_asks_for_what_its_view_lacks_is_reported(host, capsys):
    right_click(host, INNER)
    host.click(*row(host, 2))
    host.frame()
    assert "needs() asks for 'missing'" in capsys.readouterr().err
    assert PICKS == [] and menu(host) is None


def test_the_options_of_an_immediate_root_come_last_and_alone_they_are_the_surfaces(host):
    router = host.cache.imgui_input
    menus.MENUS['the root'] = {'Root': lambda: PICKS.append('root')}
    router.root = 'the root'
    try:
        host.frame(root=True)
        right_click(host, INNER)
        assert labels(host) == [['Rename', 'Plain', 'Broken'], ['Outer'], ['Root']]
        host.click(*EMPTY)
        host.frame()
        right_click(host, EMPTY)                # no view under the pointer: the root's menu, a popup of the surface
        assert labels(host) == [['Root']] and host.cache.graph.parent(menu(host)) == 0
        host.click(*row(host, 0))
        host.frame()
        assert PICKS == ['root'] and menu(host) is None and router.menu is None
    finally:
        del menus.MENUS['the root']


def test_a_view_without_a_menu_is_bound_as_it_always_was(host):
    assert _renderer_name(plain.__gui_definition__[0]) not in AFTER
    assert _renderer_name(inner.__gui_definition__[0]) in AFTER


def test_new_options_registered_for_a_live_view_are_the_ones_shown(host):
    name = _renderer_name(nested.__gui_definition__[0])
    before = menus.MENUS[name]
    menus.MENUS[name] = {'Edited': lambda: PICKS.append('edited')}      # what a source edit's re-run of the decorator does
    try:
        right_click(host, NESTED)
        assert labels(host) == [['Edited'], ['Outer']]
    finally:
        menus.MENUS[name] = before


def test_the_decorator_says_how_it_is_used():
    with pytest.raises(TypeError, match='dict of'):
        context_menu(['Delete'])
    with pytest.raises(TypeError, match='label: function'):
        context_menu({'Delete': 'not a function'})
    with pytest.raises(TypeError, match='above @gui'):
        context_menu({'Delete': print})(lambda input_value: None)


# --- submenus: an option that is a dict of options ---------------------------------------------

def moved(input_value, label):
    PICKS.append(('moved', input_value, label))


@context_menu({'Open': lambda: PICKS.append('open'),
               'Move': {'Up': moved, 'Further': {'To the top': lambda: PICKS.append('top')}}})
@gui(width=120, height=30)
def branch(input_value, label=''):
    RUNS.append('branch')


@context_menu({'Around': {'Refresh': lambda: PICKS.append('refresh')}})
@gui(width=300, height=100)
def trunk(input_value):
    branch('row', name='branch', label='first', left=10.0, top=10.0)


def tree():
    trunk(None, name='trunk', left=0.0, top=0.0)


@pytest.fixture
def nest():
    PICKS.clear()
    RUNS.clear()
    host = Host(tree)
    host.frame()
    yield host
    host.close()


def submenus(host):
    """The open submenus' nodes, outermost first."""
    body = choice_view.options.__gui_definition__[0]
    found = [node for node, record in host.cache.records.items() if record[2] is body]
    return sorted(found, key=lambda node: host.cache.position(node)[0])


def at_row(host, node, at):
    x, y = host.cache.position(node)
    return x + 10.0, y + at * ROW + ROW * 0.5


def test_an_option_that_is_a_dict_opens_a_submenu_beside_its_row(nest):
    right_click(nest, INNER)
    assert labels(nest) == [['Open', 'Move'], ['Around']] and submenus(nest) == []
    nest.frame(row(nest, 1))
    nest.frame()
    (found,) = submenus(nest)
    x, y = nest.cache.position(menu(nest))
    assert nest.cache.graph.parent(found) == menu(nest)
    assert nest.cache.position(found) == (x + nest.cache.imgui_input.menu.width, y + ROW)
    nest.frame(row(nest, 0))                    # the selection moves on: the submenu goes, the menu stays
    nest.frame()
    assert submenus(nest) == [] and menu(nest) is not None


def test_a_pick_in_a_submenu_calls_it_for_its_view_and_closes_all(nest):
    right_click(nest, INNER)
    nest.frame(row(nest, 1))
    nest.frame()
    (found,) = submenus(nest)
    nest.frame(at_row(nest, found, 0))
    nest.click(*at_row(nest, found, 0))
    nest.frame()
    nest.frame()
    assert PICKS == [('moved', 'row', 'first')]
    assert submenus(nest) == [] and menu(nest) is None and nest.cache.imgui_input.menu is None


def test_submenus_nest_and_an_outer_views_submenu_is_its_own(nest):
    right_click(nest, INNER)
    nest.frame(row(nest, 1))
    nest.frame()
    nest.frame(at_row(nest, submenus(nest)[0], 1))
    nest.frame()
    deep = submenus(nest)[1]
    nest.frame(at_row(nest, deep, 0))
    nest.click(*at_row(nest, deep, 0))
    for _ in range(3):
        nest.frame()
    assert PICKS == ['top'] and menu(nest) is None
    right_click(nest, INNER)
    x, y = nest.cache.position(menu(nest))
    nest.frame((x + 10.0, y + 2 * ROW + GAP + ROW * 0.5))       # the outer view's row, below the line
    nest.frame()
    (found,) = submenus(nest)
    nest.frame(at_row(nest, found, 0))
    nest.click(*at_row(nest, found, 0))
    nest.frame()
    nest.frame()
    assert PICKS == ['top', 'refresh'] and menu(nest) is None


def test_the_keyboard_opens_a_submenu_and_picks_in_it(nest):
    right_click(nest, INNER)
    nest.frame(keys=[(glfw.KEY_DOWN, 0)])
    nest.frame(keys=[(glfw.KEY_DOWN, 0)])
    nest.frame(keys=[(glfw.KEY_RIGHT, 0)])
    nest.frame()
    assert len(submenus(nest)) == 1
    nest.frame(keys=[(glfw.KEY_LEFT, 0)])       # back to the menu
    nest.frame()
    assert submenus(nest) == [] and menu(nest) is not None
    nest.frame(keys=[(glfw.KEY_ENTER, 0)])
    nest.frame()
    nest.frame(keys=[(glfw.KEY_DOWN, 0)])
    nest.frame(keys=[(glfw.KEY_ENTER, 0)])
    nest.frame()
    nest.frame()
    assert PICKS == [('moved', 'row', 'first')] and menu(nest) is None


def test_a_submenu_is_checked_like_the_menu():
    with pytest.raises(TypeError, match='label: function'):
        context_menu({'More': {'Delete': 'not a function'}})
    with pytest.raises(TypeError, match='dict of'):
        context_menu({'More': {}})
