"""Behavioral checks for the optional, throwaway native rendering experiment."""
import gc
import weakref

import pytest

pytest.importorskip('meltygui.core.rendering._gui_native')
from functools import partial
from native_gui_support import native_frame
from meltygui.core.rendering.gui_prototype import _new_native, _bind_native
from meltygui.core.conversion.dict_conversion import DictConversion


def test_dynamic_arguments_injection_and_identity():
    runtime = _new_native(graphics=False)
    gui = partial(_bind_native, runtime)
    seen = []

    class MyState(DictConversion):
        def __init__(self):
            super().__init__()
            self.counter = 0

    @gui(custom_default=42)
    def child(input_value: object, draw_state=None, state: MyState = None,
              custom_default=0, surprise=None, **extras):
        state.counter += 1
        draw_state.brand_new_field = surprise
        seen.append((draw_state.unique, state, custom_default, extras['caller_only']))
        assert draw_state.brand_new_field is surprise
        return False, input_value

    @gui()
    def parent(input_value: object):
        for index in range(3):
            child(input_value, key=index, surprise=input_value, caller_only='flexible')
        return False, input_value

    value = {'a': [1, 2]}
    for _ in range(2):
        with native_frame(runtime):
            assert parent(value)[1] is value
            assert runtime.stats()['stack_depth'] == 0
            assert runtime.stats()['calls'] == 4
    assert len({row[0] for row in seen}) == 3
    assert [row[1].counter for row in seen[:3]] == [2, 2, 2]
    assert all(row[2:] == (42, 'flexible') for row in seen)
    assert all(seen[i][1] is seen[i + 3][1] for i in range(3))


def test_explicit_state_is_borrowed_and_unknown_arguments_are_filtered():
    runtime = _new_native(graphics=False)
    own = []

    @partial(_bind_native, runtime)(inject={'session': list})
    def view(input_value: object, session=None):
        own.append(session)
        return False, input_value

    override = ['borrowed']
    with native_frame(runtime):
        view(None, arbitrary_forwarded_argument=1)
        view(None, session=override, arbitrary_forwarded_argument=2)
        view(None)
    assert own[0] is own[2] and own[1] is override


def test_recursion_and_failure_unwind_native_stack():
    runtime = _new_native(graphics=False)

    @partial(_bind_native, runtime)()
    def view(input_value: int, draw_state=None):
        if input_value:
            return view(input_value - 1)
        raise ValueError('body failed')

    with native_frame(runtime):
        with pytest.raises(ValueError, match='body failed'):
            view(4)
        assert runtime.stats()['stack_depth'] == 0
        assert runtime.stats()['calls'] == 5


def test_native_fields_preserve_custom_objects_and_large_integers():
    runtime = _new_native(graphics=False)
    value = object()

    class IntSubclass(int):
        pass

    special = IntSubclass(3)

    @partial(_bind_native, runtime)()
    def view(input_value: object, draw_state=None):
        draw_state.massive = 2 ** 100
        draw_state.special = special
        draw_state.object = value
        draw_state.custom_float = 1.25
        draw_state.flag = True
        assert draw_state.massive == 2 ** 100
        assert draw_state.special is special and draw_state.object is value
        assert draw_state.custom_float == 1.25 and draw_state.flag is True
        del draw_state.flag
        assert draw_state.get('flag', 'missing') == 'missing'
        return False, input_value

    with native_frame(runtime):
        _, _, state = view(None, return_extras=True)
    assert 'custom_float' in state.as_dict()
    runtime.clear()


def test_hot_body_and_signature_edit_preserves_node_and_owned_state():
    runtime = _new_native(graphics=False)

    @partial(_bind_native, runtime)(inject={'state': list})
    def view(input_value: int, draw_state=None, state=None):
        state.append(input_value)
        return False, (draw_state.unique, state)

    with native_frame(runtime):
        _, (identity, state) = view(1)

    def replacement(input_value: int, draw_state=None, state=None, new_argument=7):
        state.append(new_argument)
        return False, (draw_state.unique, state)

    view.__wrapped__.__code__ = replacement.__code__
    view.__wrapped__.__defaults__ = replacement.__defaults__
    with native_frame(runtime):
        _, (updated_identity, updated_state) = view(2, new_argument=19)
    assert updated_identity == identity and updated_state is state
    assert state == [1, 19]


def test_events_do_not_stick_and_caller_can_override():
    runtime = _new_native(graphics=False)

    class Owner:
        events = {'left_mouse_clicked': 'delivered'}

        def on_action(self, names, **kwargs):
            assert names == ['left_mouse_clicked']
            assert kwargs['rect'] == (0, 0, 123, 26)
            return self.events

    owner = Owner()

    @partial(_bind_native, runtime)(height=26, events=('left_mouse_clicked',))
    def view(input_value: object, left_mouse_clicked):
        return bool(left_mouse_clicked), left_mouse_clicked

    with native_frame(runtime,owner, width=123):
        assert view(None) == (True, 'delivered')
        assert view(None, left_mouse_clicked='caller') == (True, 'caller')
        owner.events = {}
        assert view(None) == (False, None)


def test_close_releases_injected_python_objects():
    runtime = _new_native(graphics=False)

    class Resource:
        pass

    @partial(_bind_native, runtime)(inject={'resource': Resource})
    def view(input_value: object, resource=None):
        return False, weakref.ref(resource)

    with native_frame(runtime):
        _, reference = view(None)
    assert reference() is not None
    runtime.clear()
    gc.collect()
    assert reference() is None


def test_caller_overrides_are_transient_and_authored_state_survives_them():
    runtime = _new_native(graphics=False)

    @partial(_bind_native, runtime)(level=2)
    def view(input_value: object, draw_state=None, level=1, edit=False):
        if edit:
            draw_state.level = 9
        return False, level

    with native_frame(runtime):
        assert view(None, level=7)[1] == 7
        assert view(None)[1] == 2
        assert view(None, edit=True)[1] == 2
        assert view(None)[1] == 9
        assert view(None, level=4)[1] == 4
        assert view(None)[1] == 9


def test_two_host_tiles_have_independent_native_state():
    runtime = _new_native(graphics=False)

    @partial(_bind_native, runtime)(inject={'state': list})
    def view(input_value: object, draw_state=None, state=None):
        return False, (draw_state.unique, state)

    with native_frame(runtime,scope='left'):
        _, first = view(None)
    with native_frame(runtime,scope='right'):
        _, second = view(None)
    with native_frame(runtime,scope='left'):
        _, third = view(None)
    assert first[0] == third[0] and first[1] is third[1]
    assert first[0] != second[0] and first[1] is not second[1]
