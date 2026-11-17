"""A view's value is its first parameter, under any name.

What a caller passes by position goes to it, further values go to the next parameters, and the
returned `(changed, value)` is about it. Passed nothing, it resolves like every other parameter:
from the view's state, then the defaults. A returned edit is then the same as
`draw_state.<name> = value`; an edit to a value the caller passed is the caller's to keep.
`draw_state.<name>` reads the value either way, as it reads every parameter. A passed value is
not copied to the state: the read goes through to the caller's, so the state never keeps it alive
and never saves it.
A parameter without a default is built by its annotation when nobody passes it, once per view; one
without a type is None.
Every case runs under the name `input_value` and under another one: the name means nothing.
"""
import gc
import inspect
import textwrap
import weakref
from collections.abc import Callable, Sequence
from types import FunctionType
from typing import Any, Optional

import pytest

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.conversion.load_save_v2 import dumps, loads
from meltygui.core.rendering.gui_prototype import _bind_native, _new_native, constructor
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


class Made:
    """A plain class, counted as it is built: nothing the session can save."""
    count = 0

    def __init__(self):
        Made.count += 1
        self.ticks = 0


class Refuses:
    """A type that hides that it needs an argument."""
    def __init__(self, *values):
        if not values:
            raise TypeError('Refuses() needs a value')


# The counter with no default: its type builds the 0 it starts from.
TYPED = '''
def count({name}: int, step=1, draw_state=None, view_events=None, press=False):
    seen.append(({name}, step))
    if press or 'press' in (view_events or ()):
        return True, {name} + step
'''


def define(source, name, seen):
    """The view `source` defines, with `name` as its first parameter."""
    scope = {'seen': seen, 'Picked': Picked, 'Made': Made, 'Refuses': Refuses}
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

    def rest(self):
        """A frame in which nobody declares the view: it retires."""
        self.cache._take_later()
        with self.cache.frame():
            pass
        self.cache.flush()

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
    with pytest.raises(AttributeError, match=f'{name} is passed by the caller'):
        getattr(state, name)
    assert name not in state.as_dict() and state.get(name) is None
    del model
    gc.collect()
    assert reference() is None                          # nothing of the frame is left behind


@pytest.mark.parametrize('name', NAMES)
def test_a_view_that_is_gone_keeps_nothing_and_reads_its_own_again(rig, name):
    one = rig(name)
    one.frame(); one.press(); one.frame()               # the view's own value: 1
    model = Model()
    reference = weakref.ref(model)
    one.frame(model)
    state = one.state()                                 # held on to past the view
    assert getattr(state, name) is model
    del model
    one.seen.clear()                                    # (the counter notes what it was given)
    one.rest()
    gc.collect()
    assert reference() is None                          # the state did not keep it
    assert getattr(state, name) == 1 and state.as_dict()[name] == 1
    again = Model()
    one.frame(again)                                    # declared again: read through again
    assert getattr(one.state(), name) is again


@pytest.mark.parametrize('name', NAMES)
def test_a_declared_view_reads_its_passed_value_before_it_has_run(name):
    cache, seen = RetainedGui(graphics=False), []
    body = define(READER, name, seen)
    child = cache.gui(body)

    @cache.gui(scroll=True)
    def parent(input_value=None):
        child(input_value, key='child', left=0.0, top=5000.0, width=100, height=20)     # out of view

    model = Model()
    cache._take_later()
    with cache.frame():
        parent(model, key='root', width=400, height=300)
    cache.flush()
    state = next(state for node, state in cache.states.items() if cache.records[node][2] is body)
    assert seen == []                                   # culled: its body has not run
    assert getattr(state, name) is model
    cache.close()


@pytest.mark.parametrize('name', NAMES)
def test_a_write_under_a_passed_value_shows_once_nothing_is_passed(rig, name):
    one = rig(name, source='''
        def show({name}=None, draw_state=None):
            draw_state.{name} = 999
            seen.append(draw_state.{name})
    ''')
    one.frame(50)
    assert set(one.seen) == {50}                        # the caller's value wins the read
    assert one.frame()[1] == 999 and one.seen[-1] == 999


@pytest.mark.parametrize('name', NAMES)
def test_a_body_that_raises_leaves_nothing_on_the_stack(name):
    runtime = _new_native(graphics=False)
    view = _bind_native(runtime, define('''
        def fail({name}=None):
            raise RuntimeError('body failed')
    ''', name, []))
    model = Model()
    reference = weakref.ref(model)
    try:
        with native_frame(runtime):
            view(model)
    except RuntimeError:
        pass
    assert runtime.stats()['stack_depth'] == 0
    del model
    gc.collect()
    assert reference() is None


def test_a_source_edit_that_moves_the_value_is_followed():
    runtime, seen = _new_native(graphics=False), []

    def panel(x=None, title='', draw_state=None):
        seen.append(draw_state.x)

    def edited(width=0.0, x=None, title='', draw_state=None):
        seen.append(draw_state.x)

    view = _bind_native(runtime, panel)
    with native_frame(runtime):
        view('value')
    panel.__code__, panel.__defaults__ = edited.__code__, edited.__defaults__
    with native_frame(runtime):
        state = view(x='keyword', return_extras=True)[2]        # x is an ordinary parameter now
    assert seen == ['value', 'keyword'] and state.x == 'keyword'


@pytest.mark.parametrize('name', NAMES)
def test_a_replaced_state_object_reads_the_passed_value_too(rig, name):
    one = rig(name, source=READER)
    one.frame(Model())
    old, node = one.state(), next(iter(one.cache.records))
    one.cache.declarations[node].native.clear()         # the runtime forgets its states: the next run makes one
    model = Model()
    one.frame(model)
    assert one.state() is not old and getattr(one.state(), name) is model


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


# --- a parameter nobody passes is built by its type --------------------------
@pytest.mark.parametrize('annotation, built', [
    (int, int), (str, str), (bool, bool), (float, float), (dict, dict), (list[int], list), (dict[str, int], dict),
    (tuple[int, int], tuple), (Made, Made), (Picked, Picked),
    (inspect.Parameter.empty, None), (object, None), (Any, None), (type(None), None), (int | None, None),
    (Optional[int], None), (Callable, None), (Sequence[int], None), (type[Made], None), ('Unresolved', None)])
def test_which_annotations_build_a_value(annotation, built):
    assert constructor(annotation) is built


@pytest.mark.parametrize('name', NAMES)
def test_a_parameter_without_a_default_is_built_by_its_type_once(rig, name):
    one = rig(name, source='''
        def show({name}: int, made: Made, items: list[int], title: str, plain, maybe: int | None, whatever: object,
                 draw_state=None):
            seen.append(({name}, made, items, title, plain, maybe, whatever))
            assert draw_state.{name} == {name} and draw_state.made is made and draw_state.items is items
    ''')
    before = Made.count
    assert one.frame() == (False, 0)
    one.cache.invalidate_id(next(iter(one.cache.records)))
    one.frame()
    first, again = one.seen
    assert first[0] == 0 and type(first[1]) is Made and first[2:] == ([], '', None, None, None)
    assert again[1] is first[1] and again[2] is first[2]        # held: the same objects on every run
    assert Made.count == before + 1                             # however often the view runs


@pytest.mark.parametrize('name', NAMES)
def test_a_built_value_is_state_like_one_with_a_default(rig, name):
    one = rig(name, source=TYPED)
    assert one.frame() == (False, 0)
    one.press()
    assert one.frame() == (True, 1) and getattr(one.state(), name) == 1     # an edit is kept
    assert one.frame(40) == (False, 40)                                     # a passed value wins
    assert one.frame() == (False, 1)                                        # and the view's own is back after it
    assert one.frame(None) == (False, None)                                 # None is a value a caller may pass


@pytest.mark.parametrize('name', NAMES)
def test_a_built_value_survives_a_restart_or_is_built_again(name):
    """An edit comes back as it was saved. An object the session cannot save comes back as None,
    which a typed parameter never is: its type builds it again."""
    for source, expected in ((TYPED, lambda value: value == 1),
                             ('def show({name}: Made): seen.append({name})', lambda value: type(value) is Made)):
        session = AppSession()
        one = Rig(name, registry=session.draw_state_registry, source=source)
        one.frame()
        if source is TYPED:
            one.press()
            one.frame()
        one.cache.prepare_save(); one.cache.close()
        loaded = loads(dumps(session), run_on_load=False)
        prune(loaded)
        before = Made.count
        again = Rig(name, registry=loaded.draw_state_registry, source=source)
        assert expected(again.frame()[1])
        assert Made.count == before + (source is not TYPED)
        again.cache.close()


@pytest.mark.parametrize('name', NAMES)
def test_an_authored_none_does_not_stand_for_a_typed_parameter(rig, name):
    one = rig(name, source='''
        def show({name}: Made, draw_state=None):
            seen.append({name})
    ''')
    built = one.frame()[1]
    setattr(one.state(), name, None)                    # what a restart makes of an edit it could not save
    one.cache.invalidate_id(next(iter(one.cache.records)))
    assert one.frame()[1] is built


def test_an_edited_annotation_builds_the_value_again():
    runtime, seen = _new_native(graphics=False), []

    def panel(x: int, title: str):
        seen.append((x, title))

    def edited(x: str, title: list):
        seen.append((x, title))

    view = _bind_native(runtime, panel)
    with native_frame(runtime):
        view()
    panel.__code__, panel.__annotations__ = edited.__code__, edited.__annotations__
    with native_frame(runtime):
        view()
    assert seen == [(0, ''), ('', [])]


def test_a_type_that_cannot_build_its_parameter_names_the_view():
    runtime = _new_native(graphics=False)
    view = _bind_native(runtime, define('def show({name}: Refuses): pass', 'model', []))
    with pytest.raises(TypeError, match=r'show\(\) was passed no model and Refuses\(\) could not build one: Refuses\(\) needs a value'):
        with native_frame(runtime):
            view()
    with native_frame(runtime):
        assert view(5) == (False, 5)                    # a passed value never builds


def test_a_name_the_framework_fills_is_not_built():
    runtime = _new_native(graphics=False)
    strict = _bind_native(runtime, define('def panel({name}=None, *, canvas: dict): pass', 'value', []))
    with pytest.raises(TypeError, match='missing gui argument: canvas'):
        with native_frame(runtime):
            strict()


@pytest.mark.parametrize('name', NAMES)
def test_an_immediate_view_builds_its_parameters_too(name):
    runtime, seen = _new_native(graphics=False), []
    view = _bind_native(runtime, define(TYPED, name, seen))

    def call(*values, **arguments):
        with native_frame(runtime):
            return view(*values, **arguments)

    assert call() == (False, 0) and call(press=True) == (True, 1) and call() == (False, 1)
    assert call(50) == (False, 50) and call() == (False, 1)
