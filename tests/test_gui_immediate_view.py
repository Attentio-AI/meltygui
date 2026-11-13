"""Views follow their caller on a real window host, with real widgets (hidden GL
window, reserved desktop): the cases of test_gui_immediate.py for a checkbox,
a slider and the editor, drawn directly (`use_cache=False`), retained, in a
Melty window and in Melty windows inside each other."""
import meltygui_imgui as imgui
import pytest

import meltygui.core.windowing.window_constants as glfw
from editor_host import Host
from meltygui.core.rendering.gui_prototype import gui
from meltygui.core.rendering.retained_gui_prototype import RetainedGui
from meltygui.state.editor_state import EditorState
from meltygui.view.editor_view import draw_editor

LATER = RetainedGui(graphics=False).graph.SETTLE + 3
# Where the widget's view stands: drawn directly, a retained root, under a retained panel, in a Melty window
# declared by that panel, and in a Melty window inside a Melty window.
SHAPES = ['direct', 'root', 'panel', 'window', 'nested']


class Rig:
    """An app body run every frame around one widget view; `take` is the
    caller's policy for a returned edit. `shown` is what the widget last drew."""
    def __init__(self, shape, widget, value, take):
        self.value, self.take, self.shown, self.runs = value, take, value, 0
        rig = self

        def body(input_value):
            rig.runs += 1
            changed, edited = widget(input_value)
            rig.shown = edited
            return changed, edited

        direct = gui(width=200, height=40, use_cache=False)(body)
        field = gui(width=200, height=40)(body)

        @gui(width=320, height=240)
        def panel(input_value):
            return field(input_value, name='field', melty_window=shape in ('window', 'nested'),
                         initial={'left': 20, 'top': 20}, left=10, top=10)

        @gui(width=500, height=400)
        def outer(input_value):
            return panel(input_value, name='panel', melty_window=True, initial={'left': 20, 'top': 20})

        declare = {'direct': direct, 'root': field, 'nested': outer}.get(shape, panel)

        def app():
            changed, edited = declare(rig.value, name='top', left=0., top=0.)
            if changed:
                rig.value = rig.take(edited)
        self.host = Host(app)
        # The widget view's place in the window, as the host resolved it (a direct view is at the origin).
        cache = self.host.cache
        self.at = next((cache.graph.origin(node) for node, record in cache.records.items() if record[2] is body), (0., 0.))

    def frame(self, count=1, **arguments):
        for _ in range(count):
            self.host.frame(**arguments)
        return self.shown

    def click(self, x, y):
        self.host.click(self.at[0] + x, self.at[1] + y)
        return self.shown

    def close(self):
        self.host.close()


@pytest.fixture
def rig():
    made = []
    yield lambda *arguments: made.append(Rig(*arguments)) or made[-1]
    for one in made:
        one.close()


checkbox = lambda value: imgui.checkbox('On', value)


@pytest.mark.parametrize('shape', SHAPES)
def test_a_checkbox_keeps_a_click_its_caller_takes(rig, shape):
    rig = rig(shape, checkbox, False, lambda value: value)
    assert rig.click(10, 10) is True
    assert rig.frame(LATER) is True and rig.value is True


@pytest.mark.parametrize('shape', SHAPES)
def test_a_checkbox_follows_a_caller_that_keeps_passing_its_value(rig, shape):
    rig = rig(shape, checkbox, False, lambda value: False)
    rig.click(10, 10)
    assert rig.frame(LATER) is False
    runs = rig.runs
    rig.frame(3)
    assert shape == 'direct' or rig.runs == runs      # a retained view rests; a direct one runs every frame
    # The next click starts from the caller's value again.
    rig.click(10, 10)
    assert rig.frame(LATER) is False


@pytest.mark.parametrize('shape', SHAPES)
def test_a_checkbox_ends_on_what_a_late_caller_passes(rig, shape):
    held = []
    rig = rig(shape, checkbox, False, lambda value: held.append(value) or False)
    rig.click(10, 10)
    rig.frame(LATER)
    rig.value = held[0]
    assert rig.frame(LATER) is True


@pytest.mark.parametrize('shape', SHAPES)
def test_every_host_shows_the_same_value_once_the_frames_have_passed(rig, shape):
    """The same clicks against the same caller policies end on the same values as the direct view's."""
    for take in (lambda value: value, lambda value: False, lambda value: not value):
        ends = []
        for where in ('direct', shape):
            one = rig(where, checkbox, False, take)
            for _ in range(3):
                one.click(10, 10)
                one.frame(LATER)
                ends.append((where, one.shown, one.value))
        assert [end[1:] for end in ends[:3]] == [end[1:] for end in ends[3:]]


def editor(state):
    return lambda value: draw_editor(value, name='text', state=state, left=0., top=0., width=190., height=22., single_line=True)


@pytest.mark.parametrize('shape', ['root', 'panel', 'window', 'nested'])
def test_the_editor_keeps_what_is_typed_when_its_caller_takes_it(rig, shape):
    rig = rig(shape, editor(EditorState()), 'query', lambda value: value)
    rig.click(40, 10)
    rig.frame(keys=[(glfw.KEY_END, 0)])
    for char in '123':
        rig.frame(keys=[(getattr(glfw, f'KEY_{char}'), 0)], text=char)
    assert rig.frame(LATER) == 'query123' and rig.value == 'query123'


@pytest.mark.parametrize('shape', ['root', 'panel', 'window', 'nested'])
def test_the_editor_shows_typing_at_once_and_then_follows_a_caller_that_keeps_its_text(rig, shape):
    state = EditorState()
    rig = rig(shape, editor(state), 'query', lambda value: 'query')
    rig.click(40, 10)
    rig.frame(keys=[(glfw.KEY_END, 0)])
    for count, char in enumerate('123', 1):
        assert rig.frame(keys=[(getattr(glfw, f'KEY_{char}'), 0)], text=char) == 'query' + '123'[:count]
    assert rig.frame(LATER) == 'query' and state.editor.doc.text == 'query'
    runs = rig.runs
    rig.frame(3)
    assert rig.runs == runs
    # Typing goes on from the caller's text.
    assert rig.frame(keys=[(glfw.KEY_4, 0)], text='4') == 'query4'
    assert rig.frame(LATER) == 'query'


def test_a_multi_line_editor_follows_a_static_text_and_keeps_its_undo_stack_usable(rig):
    from meltygui.state.core_undo import UndoManager
    UndoManager.stack.history.clear()
    UndoManager.stack.redo_stack.clear()
    state = EditorState()
    text = 'first = 1\nsecond = 2\n'
    rig = rig('root', lambda value: draw_editor(value, name='text', state=state, left=0., top=0., width=190., height=40.),
              text, lambda value: text)
    rig.click(60, 8)
    rig.frame(keys=[(glfw.KEY_X, 0)], text='x')
    assert rig.shown != text
    assert rig.frame(LATER) == text and state.editor.doc.text == text
    UndoManager.undo()
    assert rig.frame(LATER) == text
