"""`draw_function` (view/function_view.py), and the view that asks for a parameter with it, driven
through a hidden window (reserved desktop).

A function's parameters are a dict the view keeps and `draw_any` draws; the button calls the
function with them. A view whose parameter nobody passed, and whose type needs arguments, shows
`draw_function` of that type in its own place until the button has built one.
"""
import pytest

import meltygui.core.windowing.window_constants as glfw
from editor_host import Host
from meltygui.core.rendering.gui_prototype import gui
from meltygui.view.dict_view import ROW as LINE
from meltygui.view.function_view import GAP, draw_function

ENTER = (glfw.KEY_ENTER, 0)
FIELD = 150.0           # an x inside the value column of the parameters


class Camera:
    """A type that needs an argument."""
    built = 0

    def __init__(self, zoom: float, name: str = 'main'):
        Camera.built += 1
        self.zoom, self.name = zoom, name


def fit(slope: float, offset: int = 2, *, label='line', scale):
    return slope * 10 + offset, label, scale


@pytest.fixture
def show():
    hosts = []

    def make(view, width=300.0, **arguments):
        hosts.append(Host(view, name='view', left=0.0, top=0.0, width=width, **arguments))
        return hosts[-1]
    yield make
    for host in hosts:
        host.close()


def press(host, parameters):
    """A click on the button under `parameters` rows of arguments."""
    host.click(20.0, parameters * LINE + GAP + 10.0)


def type_into(host, row, text):
    host.click(FIELD, row * LINE + LINE * 0.5)
    host.frame(text=text)
    host.frame(keys=[ENTER])


def test_it_starts_from_the_defaults_and_what_the_types_build_and_calls_with_them(show):
    host = show(draw_function, input_value=fit, return_extras=True)
    state = host.result[2]
    assert state.arguments == {'slope': 0.0, 'offset': 2, 'label': 'line', 'scale': None}
    assert host.result[:2] == (False, fit) and state.result is None
    type_into(host, 0, '1.5')
    held = state.arguments
    press(host, 4)
    assert state.result == (17.0, 'line', None) and state.error == ''
    assert state.arguments is held                              # edited in place, never replaced
    for _ in range(3):
        host.frame()
    assert state.result == (17.0, 'line', None)                 # kept until the next call


def test_the_run_of_a_call_reports_it(show):
    ran = []

    @gui
    def root(input_value=None):
        ran.append(draw_function(fit, name='fit')[0])

    host = show(root, height=220.0)
    press(host, 4)
    assert ran == [False, True]                                 # told once, in the run of the call


def test_a_call_that_raises_shows_its_error_and_reports_nothing(show, capsys):
    def fail(amount: int):
        raise ValueError(f'no {amount}')

    host = show(draw_function, input_value=fail, return_extras=True)
    state = host.result[2]
    high = host.cache.graph.info(1)['rect'][3]
    press(host, 1)
    assert state.error == 'ValueError: no 0' and state.result is None
    assert host.result[0] is False
    assert host.cache.graph.info(1)['rect'][3] > high           # the error's line is part of the wrapped height
    assert 'ValueError: no 0' in capsys.readouterr().err


def test_positional_only_parameters_and_a_caller_that_keeps_the_arguments(show):
    def scaled(value, /, factor=3):
        return value * factor

    mine = {'value': 4}
    host = show(draw_function, input_value=scaled, arguments=mine, return_extras=True)
    assert mine == {'value': 4, 'factor': 3}                    # filled in place; what was there is kept
    press(host, 2)
    assert host.result[2].result == 12


def test_a_source_edit_reads_the_parameters_again(show):
    def tune(gain: float = 1.0):
        return gain

    def edited(gain: float = 1.0, bias: int = 5):
        return gain + bias

    host = show(draw_function, input_value=tune, return_extras=True)
    state = host.result[2]
    type_into(host, 0, '2.0')
    tune.__code__, tune.__defaults__ = edited.__code__, edited.__defaults__
    host.cache.invalidate_id(1)
    host.frame()
    assert state.arguments == {'gain': 2.0, 'bias': 5}          # the typed value is kept, the new one added
    press(host, 2)
    assert state.result == 7.0


def test_a_class_is_built(show):
    host = show(draw_function, input_value=Camera, return_extras=True)
    type_into(host, 0, '2.5')
    press(host, 2)
    built = host.result[2].result
    assert type(built) is Camera and (built.zoom, built.name) == (2.5, 'main')


def test_a_view_asks_for_a_parameter_whose_type_needs_arguments(show):
    shots = []

    @gui
    def shot(camera: Camera, title: str, width=0.0, height=0.0):
        shots.append((camera, title))

    host = show(shot, height=220.0)
    for _ in range(3):
        host.frame()
    assert shots == [] and host.result == (False, None)         # the form stands in the view's place
    assert [record[2].__name__ for record in host.cache.records.values()][:2] == ['shot', 'draw_function']
    type_into(host, 0, '2.5')
    before = Camera.built
    press(host, 2)
    for _ in range(3):
        host.frame()
    camera, title = shots[-1]
    assert type(camera) is Camera and (camera.zoom, camera.name, title) == (2.5, 'main', '')
    assert Camera.built == before + 1
    assert [record[2].__name__ for record in host.cache.records.values()] == ['shot']      # the form is gone
    assert host.cache.states[1].camera is camera                # held as the view's state
    assert host.result == (False, camera)
    runs = len(shots)
    for _ in range(5):
        host.frame()
    assert len(shots) == runs                                   # and the view rests


def test_a_passed_value_never_asks(show):
    shots = []

    @gui
    def shot(camera: Camera, width=0.0, height=0.0):
        shots.append(camera)

    mine = Camera(1.0)
    before = Camera.built
    host = show(shot, camera=mine, height=220.0)
    assert shots == [mine] and len(host.cache.records) == 1 and Camera.built == before


def test_a_parameter_after_the_value_is_asked_for_too(show):
    seen = []

    @gui
    def scene(title: str, camera: Camera, draw_state=None, cache=None):
        seen.append((title, camera, draw_state.camera is camera, cache is not None))

    host = show(scene, height=220.0)
    assert seen == []
    type_into(host, 0, '4')
    press(host, 2)
    for _ in range(3):
        host.frame()
    title, camera, held, cached = seen[-1]
    assert (title, camera.zoom, held, cached) == ('', 4.0, True, True)
