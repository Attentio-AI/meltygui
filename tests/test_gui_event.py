"""The `event` parameter of a `@gui` view (core/input/event.py): what a view is told, what runs it
and what it costs. No GL: each test feeds pointer and key samples to a headless cache
(`Stage.step`) and reads what the views' bodies recorded."""
import pytest

from meltygui.core.input.event import Event, QUIET
from meltygui.core.rendering.gui_imgui_input import CLICK_PIXELS
from meltygui.core.rendering.retained_gui_prototype import RetainedGui

LEFT = (True,)
RIGHT = (False, True)
MIDDLE = (False, False, True)
UP = ()


class Stage:
    """A headless cache, the sample it is fed and the runs of its views: `runs` is [(view, what it recorded)]."""

    def __init__(self):
        self.cache = RetainedGui(graphics=False)
        self.gui = self.cache.gui
        self.runs = []
        self.root = None
        self.position = (500.0, 500.0)
        self.down = [False] * 5
        self.wheel = 0.0
        self.sideways = 0.0

    def mount(self, root):
        """Declare `root` (a function of no arguments that calls the views) and take the first sample."""
        self.root = root
        self.step()

    def step(self, position=None, down=None, wheel=0.0, sideways=0.0, keys=(), text=''):
        """One frame: the pointer at `position` with the buttons `down` held. Returns {view: what its last run recorded}."""
        if position is not None:
            self.position = position
        if down is not None:
            self.down = list(down) + [False] * (5 - len(down))
        self.wheel = wheel
        self.sideways = sideways
        cache = self.cache
        for item in keys:
            cache.imgui_input.key(*item)
        for character in text:
            cache.imgui_input.character(ord(character))
        del self.runs[:]
        cache.imgui_input.process(self)
        cache.flush()
        with cache.frame():
            self.root()
        cache.flush()
        return dict(self.runs)

    # What `ImGuiInput.process` reads of ImGui's IO.
    mouse_pos = property(lambda self: self.position)
    mouse_down = property(lambda self: self.down)
    mouse_wheel = property(lambda self: self.wheel)
    mouse_wheel_horizontal = property(lambda self: self.sideways)
    keys_down = ()
    key_ctrl = key_shift = key_alt = key_super = False


@pytest.fixture
def stage():
    stage = Stage()
    yield stage
    stage.cache.close()


def test_a_toggle_flips_on_a_press_and_runs_only_when_hover_changes(stage):
    value = [False]

    @stage.gui(width=100, height=40)
    def toggle(input_value, event=None):
        flipped = event.left_mouse_down()
        if flipped:
            input_value = not input_value
        stage.runs.append(('toggle', (flipped, event.is_hovered(), input_value)))
        return flipped, input_value

    def root():
        changed, value[0] = toggle(value[0], name='toggle', left=10, top=10)

    stage.mount(root)
    assert stage.step((20, 20)) == {'toggle': (False, True, False)}, 'the pointer came over it'
    assert stage.step((25, 22)) == {}, 'a move inside runs nothing: only the hover was asked about'
    assert stage.step(down=LEFT) == {'toggle': (True, True, True)} and value == [True]
    assert stage.step() == {}, 'held still: nothing'
    assert stage.step(down=UP) == {'toggle': (False, True, True)}, 'the press is told once'
    assert stage.step((300, 300)) == {'toggle': (False, False, True)}, 'the pointer left'
    assert stage.step((301, 300)) == {} and stage.step() == {}, 'an idle frame runs no body'


def test_a_click_is_a_release_in_place_and_a_drag_has_its_delta(stage):
    @stage.gui(width=200, height=100)
    def pad(event=None):
        stage.runs.append(('pad', {
            'down': event.left_mouse_down(), 'drag': event.left_mouse_drag(), 'up': event.left_mouse_up(),
            'clicked': event.left_mouse_clicked(), 'held': event.is_left_mouse_held(),
            'at': (event.x, event.y), 'step': (event.dx, event.dy), 'total': (event.total_dx, event.total_dy)}))

    stage.mount(lambda: pad(left=0, top=0))
    said = stage.step((50, 50), LEFT)['pad']
    assert (said['down'], said['held'], said['at'], said['total']) == (True, True, (50, 50), (0, 0))
    said = stage.step((50 + CLICK_PIXELS, 50), UP)['pad']
    assert said['up'] and said['clicked'] and not said['held'], 'released within CLICK_PIXELS of the press: a click'
    stage.step((50, 50), LEFT)
    said = stage.step((60, 55))['pad']
    assert (said['drag'], said['at'], said['step'], said['total']) == (True, (60, 55), (10, 5), (10, 5))
    said = stage.step((300, 40))['pad']
    assert (said['drag'], said['at'], said['step'], said['total']) == (True, (300, 40), (240, -15), (250, -10)), \
        'the drag is the view\'s past its edge'
    said = stage.step((52, 51), UP)['pad']
    assert said['up'] and not said['clicked'], 'it came back, but it travelled: no click'
    assert said['total'] == (2, 1) and not said['drag']


def test_presses_in_a_row_count_up(stage):
    @stage.gui(width=200, height=100)
    def pad(event=None):
        stage.runs.append(('pad', (event.left_mouse_down(), event.left_mouse_double_clicked(), event.clicks)))

    stage.mount(lambda: pad(left=0, top=0))
    seen = []
    for _ in range(4):
        seen.append(stage.step((50, 50), LEFT)['pad'])
        stage.step(down=UP)
    assert seen == [(True, False, 1), (True, True, 2), (True, False, 3), (True, False, 1)]
    assert stage.step((90, 50), LEFT)['pad'] == (True, False, 1), 'a press somewhere else starts over'


def test_each_button_goes_to_the_view_that_asked_about_it(stage):
    @stage.gui(width=100, height=100)
    def menu(event=None):
        """Asks about the right button only: a context menu."""
        stage.runs.append(('menu', (event.right_mouse_down(), event.right_mouse_clicked(), event.is_right_mouse_held())))

    @stage.gui(width=300, height=300)
    def board(event=None):
        stage.runs.append(('board', (event.left_mouse_down(), event.middle_mouse_down(), event.middle_mouse_clicked())))
        menu(left=20, top=20)

    stage.mount(lambda: board(left=0, top=0))
    routing = stage.cache.imgui_input
    assert stage.step((50, 50), RIGHT) == {'menu': (True, False, True)}
    assert not routing.left, 'a view that asked about the right button does not take the left drag from the window'
    assert stage.step(down=UP) == {'menu': (False, True, False)}, 'a right click is told on the release'
    assert stage.step(down=LEFT) == {'board': (True, False, False)}, 'the left press over the menu is the board\'s'
    assert routing.left
    stage.step(down=UP)
    assert stage.step(down=MIDDLE)['board'] == (False, True, False)
    assert stage.step(down=UP)['board'] == (False, False, True)


def test_reach_narrows_where_a_view_takes_the_buttons(stage):
    @stage.gui(width=200, height=40)
    def chip(event=None):
        event.reach(0.0, 0.0, 60.0, 40.0)
        stage.runs.append(('chip', event.left_mouse_down()))

    stage.mount(lambda: chip(left=0, top=0))
    routing = stage.cache.imgui_input
    stage.step((30, 20))
    assert routing.left
    stage.step((120, 20))
    assert not routing.left, 'beside the chip the box is the window\'s'
    assert stage.step(down=LEFT) == {} and routing.grab is None
    stage.step(down=UP)
    assert stage.step((30, 20), LEFT) == {'chip': True}


def test_mouse_moved_follows_the_pointer_and_hover_alone_does_not(stage):
    @stage.gui(width=100, height=100)
    def follower(event=None):
        stage.runs.append(('follower', (event.mouse_moved(), event.is_hovered(), event.x, event.y)))

    @stage.gui(width=100, height=100)
    def lamp(event=None):
        stage.runs.append(('lamp', event.is_hovered()))

    def root():
        follower(left=0, top=0)
        lamp(left=200, top=0)

    stage.mount(root)
    assert stage.step((10, 10)) == {'follower': (True, True, 10, 10)}
    assert stage.step((40, 30)) == {'follower': (True, True, 40, 30)}
    assert stage.step() == {}, 'it rests: nothing'
    assert stage.step((210, 10)) == {'follower': (False, False, 40, 30), 'lamp': True}
    assert stage.step((220, 20)) == {}
    assert stage.step((20, 20), LEFT) == {'lamp': False, 'follower': (True, True, 20, 20)}, 'a press hovers what it lands on'
    assert stage.step((30, 20)) == {}, 'with a button held the hover stays and no move is told'


def test_the_wheel_goes_to_the_view_that_asked_unless_a_scroll_view_took_it(stage):
    @stage.gui(width=100, height=400, listen=())
    def tall():
        pass

    @stage.gui(width=100, height=100, scroll=True)
    def pane(event=None):
        stage.runs.append(('pane', event.scrolled()))
        tall(left=0, top=0)

    @stage.gui(width=100, height=100)
    def dial(event=None):
        stage.runs.append(('dial', (event.mouse_wheel(), event.mouse_wheel_horizontal())))

    def root():
        pane(left=0, top=0)
        dial(left=200, top=0)

    stage.mount(root)
    assert stage.step((210, 10), wheel=2.0) == {'dial': (2.0, 0.0)}
    assert stage.step(sideways=-1.0) == {'dial': (0.0, -1.0)}
    assert stage.step() == {}
    assert stage.step((10, 10), wheel=-1.0) == {'pane': True}, 'the scroll view took the wheel and was told it scrolled'
    assert stage.step() == {}


def test_keys_and_text_go_to_the_view_with_the_keyboard(stage):
    @stage.gui(width=100, height=30)
    def field(event=None):
        stage.runs.append(('field', {
            'keys': list(event.keys()), 'enter': event.key_down(257), 'text': event.text(),
            'focused': event.is_focused(), 'gained': event.focus_gained(), 'lost': event.focus_lost()}))

    @stage.gui(width=100, height=30)
    def button(event=None):
        stage.runs.append(('button', event.left_mouse_down()))

    def root():
        field(left=0, top=0)
        button(left=0, top=100)

    stage.mount(root)
    focus = stage.cache.focus
    assert stage.step((10, 10), keys=[(65, 0)], text='a') == {}, 'nobody has the keyboard: what is typed is nobody\'s'
    said = stage.step(down=LEFT)['field']
    assert said['focused'] and said['gained'] and not said['keys'], 'a left press on a view that asks for keys gives it the keyboard'
    said = stage.step(down=UP, keys=[(65, 0), (257, 0)], text='ab')['field']
    assert (said['keys'], said['enter'], said['text'], said['gained']) == ([(65, 0), (257, 0)], True, 'ab', False)
    assert stage.step() == {}, 'told once'
    found = stage.step((10, 110), LEFT)
    assert found['button'] is True and found['field']['lost'] and not found['field']['focused']
    assert focus.keyboard is None, 'the button did not ask for keys: the keyboard is nobody\'s'
    stage.step(down=UP)
    assert stage.step(text='c') == {}


def test_a_view_is_routed_what_its_body_asked_about_and_nothing_else(stage):
    @stage.gui(width=100, height=100)
    def view(input_value, event=None):
        stage.runs.append(('view', None))
        if input_value:
            event.left_mouse_down()

    armed = [False]
    stage.mount(lambda: view(armed[0], name='view', left=0, top=0))
    node = next(iter(stage.cache.inputs))
    assert stage.cache.inputs[node].asked == 0
    assert stage.step((10, 10), LEFT) == {} and not stage.cache.imgui_input.left, 'it asked nothing: the press is the window\'s'
    stage.step(down=UP)
    armed[0] = True
    assert 'view' in stage.step(), 'the argument changed: it runs, and asks'
    assert 'view' in stage.step(down=LEFT) and stage.cache.imgui_input.left


def test_a_view_without_an_event_costs_nothing(stage):
    @stage.gui(width=100, height=100, listen=())
    def painting():
        stage.runs.append(('painting', None))

    @stage.gui(width=100, height=100)
    def loose(**arguments):
        stage.runs.append(('loose', 'event' in arguments))

    def root():
        painting(left=0, top=0)
        loose(left=200, top=0)

    stage.mount(root)
    cache = stage.cache
    assert set(cache.inputs.values()) == {QUIET} and QUIET.asked == 0, 'the painting view shares the Event that asks nothing'
    assert stage.step((10, 10)) == {} and stage.step(down=LEFT) == {} and stage.step(down=UP) == {}
    assert stage.step((210, 10)) == {'loose': False}, 'a **kwargs body is handed no event: it stays a raw-sample view'
    assert len(cache.inputs) == 1 and not cache.fresh


def test_listing_events_is_refused(stage):
    with pytest.raises(TypeError, match='event.left_mouse_down'):
        @stage.gui(listen=('pointer', 'hover'))
        def old(view_events=None):
            pass


def test_a_body_that_fails_keeps_what_it_was_told(stage):
    fail = [False]

    @stage.gui(width=100, height=100)
    def view(event=None):
        pressed = event.left_mouse_down()
        if fail[0]:
            raise ValueError('not now')
        stage.runs.append(('view', pressed))

    stage.mount(lambda: view(left=0, top=0))
    fail[0] = True
    with pytest.raises(ValueError):
        stage.step((10, 10), LEFT)
    fail[0] = False
    assert stage.step() == {'view': True}, 'the press is still there for the run that succeeds'
    assert stage.step() == {}


def test_holding_outside_the_view_runs_it_every_frame_only_when_asked(stage):
    @stage.gui(width=100, height=100)
    def list_view(event=None):
        stage.runs.append(('list', (event.left_mouse_drag(), event.left_mouse_held_outside(), event.y)))

    @stage.gui(width=100, height=100)
    def knob(event=None):
        stage.runs.append(('knob', event.left_mouse_drag()))

    def root():
        list_view(left=0, top=0)
        knob(left=200, top=0)

    stage.mount(root)
    stage.step((10, 10), LEFT)
    assert stage.step((10, 150)) == {'list': (True, False, 150)}
    assert stage.step() == {'list': (False, True, 150)} and stage.step() == {'list': (False, True, 150)}
    stage.step(down=UP)
    stage.step((210, 10), LEFT)
    assert stage.step((210, 150)) == {'knob': True}
    assert stage.step() == {}, 'it did not ask: held still outside costs nothing'


def test_an_event_keeps_its_states_through_clear():
    event = Event()
    event.over = event.keyboard = True
    event.held = 1
    event.downs = event.taps = 1
    event.typed = 'a'
    event.clear()
    assert event.is_hovered() and event.is_focused() and event.is_left_mouse_held()
    assert not event.left_mouse_down() and not event.left_mouse_clicked() and event.text() == ''
