"""A view's value is its first parameter, under any name.

What a caller passes by position goes to it, further values go to the next parameters, and the
returned `(changed, value)` is about it. Passed nothing, it resolves like every other parameter:
from the view's state, then the defaults. A returned edit is then the same as
`draw_state.<name> = value`; an edit to a value the caller passed is the caller's to keep.
`draw_state.<name>` reads the value either way, as it reads every parameter. A passed value is
not copied to the state: the read goes through to the caller's, so the state never keeps it alive
and never saves it.
Every case runs under the name `input_value` and under another one: the name means nothing.
"""
import gc
import textwrap
import weakref
from types import FunctionType

import pytest

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.conversion.load_save_v2 import dumps, loads
from meltygui.core.rendering.gui_prototype import _bind_native, _new_native
from meltygui.core.rendering.retained_gui_prototype import RetainedGui
from meltygui.core.runtime.app_session import AppSession, prune
from native_gui_support import native_frame

NAMES = ['input_value', 'amount']

# One counter. `press` is a pending view event (retained) or an argument (immediate).
COUNTER = '''
def count({name}: int = 0, step=1, draw_state=None, view_events=None, press=False):
    seen.append(({name}, step))
    if press or 'press' in (view_events or ()):
        return True, {name} + step
'''


# A view that says whether its state reads the very value it was passed.
READER = '''
def show({name}=None, draw_state=None):
    seen.append(draw_state.{name} is {name})
'''


class Model:
    """A value that is tracked by identity, to see who still holds it."""


class Picked(DictConversion):
    def __init__(self):
        super().__init__()
        self.items = []


def define(source, name, seen):
    """The view `source` defines, with `name` as its first parameter."""
    scope = {'seen': seen, 'Picked': Picked}
    exec(textwrap.dedent(source.format(name=name)), scope)
    return next(value for value in scope.values() if isinstance(value, FunctionType))


class Rig:
    """The counter as a retained root level view, driven frame by frame."""
    def __init__(self, name, registry=None, source=COUNTER):
        self.name, self.seen = name, []
        self.cache = RetainedGui(graphics=False, **({} if registry is None else {'state_registry': registry}))
        self.view = self.cache.gui(define(source, name, self.seen), width=120, height=30)

    def frame(self, *values, **arguments):
        self.cache._take_later()        # the host's input pass: what asked for this frame
        with self.cache.frame():
            result = self.view(*values, key='v', **arguments)
        self.cache.flush()
        return result

    def press(self):
        self.cache.send(next(iter(self.cache.records)), 'press', True)

    def state(self):
        return next(iter(self.cache.states.values()))


@pytest.fixture
def rig():
    made = []
    yield lambda *arguments, **options: made.append(Rig(*arguments, **options)) or made[-1]
    for one in made:
        one.cache.close()


@pytest.mark.parametrize('name', NAMES)
def test_a_value_reaches_the_first_parameter_by_position_and_by_name(rig, name):
    for passed in (lambda rig: rig.frame(5), lambda rig: rig.frame(**{name: 5})):
        one = rig(name)
        assert passed(one) == (False, 5)        # no return: the value is handed back
        assert one.seen == [(5, 1)]
        assert getattr(one.state(), name) == 5          # read through the state, like every parameter


@pytest.mark.parametrize('name', NAMES)
def test_further_values_go_to_the_next_parameters_as_python_binds_them(rig, name):
    one = rig(name)
    assert one.frame(5, 3) == (False, 5) and one.seen == [(5, 3)]
    assert (getattr(one.state(), name), one.state().step) == (5, 3)
    with pytest.raises(TypeError, match="multiple values for 'step'"):
        one.frame(5, 3, step=4)
    with pytest.raises(TypeError, match='values by position'):
        one.frame(5, 3, None, None, False, 'one too many')


@pytest.mark.parametrize('name', NAMES)
def test_passed_nothing_it_takes_its_default_and_none_without_one(rig, name):
    assert rig(name).frame() == (False, 0)
    bare = rig(name, source='def show({name}): seen.append({name})')
    assert bare.frame() == (False, None) and bare.seen == [None]
    assert bare.frame(None) == (False, None)       # None is a value a caller may pass


@pytest.mark.parametrize('name', NAMES)
def test_a_returned_edit_is_kept_as_state_when_nothing_was_passed(rig, name):
    one = rig(name)
    one.frame()
    for expected in (1, 2, 3):                      # each edit goes on from the last
        one.press()
        assert one.frame() == (True, expected)
    assert getattr(one.state(), name) == 3
    # No caller passes the edit back, so the view runs once more to show it, and then rests.
    assert [one.frame() for _ in range(6)] == [(False, 3)] * 6
    assert [value for value, _ in one.seen] == [0, 0, 1, 2, 3]


@pytest.mark.parametrize('name', NAMES)
def test_a_state_write_and_a_returned_edit_are_the_same(rig, name):
    written = rig(name, source='''
        def count({name}: int = 0, draw_state=None, view_events=None):
            seen.append({name})
            if 'press' in view_events:
                draw_state.{name} = {name} + 1
    ''')
    returned = rig(name)
    for one in (written, returned):
        one.frame()
        one.press(); one.frame()
        one.press(); one.frame()
        assert getattr(one.state(), name) == 2
        assert one.frame(50)[1] == 50               # a passed value wins
        assert one.frame()[1] == 2                  # and the state is there when it stops


@pytest.mark.parametrize('name', NAMES)
def test_an_edit_to_a_passed_value_is_the_callers_and_the_state_reads_it(rig, name):
    one = rig(name)
    held = 10
    one.frame(held)
    one.press()
    changed, held = one.frame(held)
    assert (changed, held) == (True, 11)
    assert getattr(one.state(), name) == 11         # the edit, before the caller has passed it back
    assert [one.frame(held) for _ in range(6)] == [(False, 11)] * 6
    assert getattr(one.state(), name) == 11
    # A caller that does not take it is followed, as before, and so is what the state holds.
    one.press()
    assert one.frame(11) == (True, 12)
    assert getattr(one.state(), name) == 12
    assert [one.frame(11) for _ in range(6)][-1] == (False, 11)
    assert getattr(one.state(), name) == 11
    # Read is not owned: passed nothing, the view is back at its default.
    assert one.frame() == (False, 0)


@pytest.mark.parametrize('name', NAMES)
def test_the_state_reads_a_passed_value_and_keeps_no_reference_to_it(rig, name):
    one = rig(name, source=READER)
    model = Model()
    reference = weakref.ref(model)
    one.frame(model)
    assert one.seen == [True]                           # in the body
    assert getattr(one.state(), name) is model          # and between runs, while the view is declared
    assert one.state().as_dict()[name] is model and one.state().get(name) is model
    one.frame(None)                                     # the caller passes something else
    del model
    gc.collect()
    assert reference() is None
    assert getattr(one.state(), name) is None


@pytest.mark.parametrize('name', NAMES)
def test_an_immediate_view_reads_a_passed_value_only_while_it_runs(name):
    runtime, seen = _new_native(graphics=False), []
    view = _bind_native(runtime, define(READER, name, seen))
    model = Model()
    reference = weakref.ref(model)
    with native_frame(runtime):
        state = view(model, return_extras=True)[2]
    assert seen == [True]
    with pytest.raises(AttributeError, match="its caller's"):
        getattr(state, name)
    assert name not in state.as_dict() and state.get(name) is None
    del model
    gc.collect()
    assert reference() is None                          # nothing of the frame is left behind


@pytest.mark.parametrize('name', NAMES)
def test_a_passed_value_is_never_stored_or_saved(name):
    session = AppSession()
    one = Rig(name, registry=session.draw_state_registry)
    one.frame(); one.press(); one.frame()           # the view's own value: 1
    one.frame(50)
    assert getattr(one.state(), name) == 50         # the caller's, over the view's own
    assert one.state().save_state()[0][name] == 1   # what is saved is what the view owns
    one.cache.prepare_save(); one.cache.close()
    again = Rig(name, registry=loads(dumps(session), run_on_load=False).draw_state_registry)
    assert again.frame() == (False, 1)
    again.cache.close()
    # A view that owns nothing saves nothing under its value's name.
    lone = Rig(name)
    lone.frame(50)
    assert name not in lone.state().save_state()[0]
    lone.cache.close()


@pytest.mark.parametrize('name', NAMES)
def test_the_kept_value_survives_a_restart(name):
    session = AppSession()
    one = Rig(name, registry=session.draw_state_registry)
    one.frame(); one.press(); one.frame()
    one.cache.prepare_save(); one.cache.close()
    loaded = loads(dumps(session), run_on_load=False)
    prune(loaded)
    again = Rig(name, registry=loaded.draw_state_registry)
    assert again.frame() == (False, 1) and again.seen == [(1, 1)]
    again.cache.close()


@pytest.mark.parametrize('name', NAMES)
def test_an_annotated_state_is_injected_as_the_value_and_a_passed_one_is_borrowed(rig, name):
    one = rig(name, source='def show({name}: Picked = None): seen.append({name})')
    own = Picked()
    injected = one.frame()[1]
    assert type(injected) is Picked and one.frame()[1] is injected
    assert one.frame(own)[1] is own and one.frame()[1] is injected


@pytest.mark.parametrize('name', NAMES)
def test_the_cache_follows_the_value_however_it_is_passed(rig, name):
    for passed in (lambda rig, model: rig.frame(model), lambda rig, model: rig.frame(**{name: model})):
        one = rig(name, source='def show({name}=None): seen.append(list({name}))')
        model = [0]
        passed(one, model)
        model[0] = 1
        passed(one, model)
        assert one.seen == [[0]]                    # edited in place: not a changed argument
        one.cache.invalidate(model)
        passed(one, model)
        assert one.seen == [[0], [1]]


def test_a_first_parameter_the_framework_fills_is_not_a_value(rig):
    one = rig('width', source='def panel({name}=0.0, height=0.0, title=""): seen.append(({name}, title))')
    assert one.frame(width=200.0, title='a') == (False, None)
    assert one.seen == [(200.0, 'a')]
    with pytest.raises(TypeError, match='0 values by position'):
        one.frame(None, 'b')


@pytest.mark.parametrize('name', NAMES)
def test_an_immediate_view_follows_the_same_rules(name):
    runtime, seen = _new_native(graphics=False), []
    view = _bind_native(runtime, define(COUNTER, name, seen))

    def call(*values, **arguments):
        with native_frame(runtime):
            return view(*values, **arguments)

    assert call() == (False, 0) and call(5) == (False, 5) and call(**{name: 5}) == (False, 5)
    assert call(5, 3, press=True) == (True, 8)      # passed: the edit is the caller's
    assert call() == (False, 0)
    assert call(press=True) == (True, 1) and call() == (False, 1)       # owned: the edit is kept
    assert call(50) == (False, 50) and call() == (False, 1)
