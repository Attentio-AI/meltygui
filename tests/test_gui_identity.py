"""Persistent identity contract, without starting an app or reading user sessions."""
import os
import subprocess
import sys

import pytest

from meltygui.core.rendering.gui_prototype import _bind_native, _new_native
from meltygui.core.rendering.retained_gui_prototype import RetainedGui
from native_gui_support import native_frame


def test_identity_survives_process_and_registration_order():
    source = '''
import os
from meltygui.core.rendering.gui_prototype import _bind_native, _new_native
n = _new_native(graphics=False)
def view(input_value, draw_state=None): return False, draw_state.unique
def unrelated(input_value): return False, input_value
if os.environ['ORDER'] == 'first': _bind_native(n, unrelated)
v = _bind_native(n, view)
if os.environ['ORDER'] == 'last': _bind_native(n, unrelated)
n.begin_frame(scope=('app', 'window'))
print(v(None, key=('item', 7))[1])
n.end_frame()
'''
    results = [subprocess.check_output(
        [sys.executable, '-c', source], close_fds=False, text=True,
        env={**os.environ, 'ORDER': order, 'PYTHONHASHSEED': seed}).splitlines()[-1]
        for order, seed in [('first', '1'), ('last', '2')]]
    assert results[0] == results[1]
    assert 2**64 < int(results[0]) < 2**128


def test_keys_names_scope_and_input_independence():
    class Untouchable:
        def __hash__(self): raise AssertionError('input hash')
        def __eq__(self, other): raise AssertionError('input equality')
        def __repr__(self): raise AssertionError('input repr')

    native = _new_native(graphics=False)
    def view(input_value, draw_state=None): return False, draw_state.unique
    render = _bind_native(native, view)
    keys = [None, '', 0, 1, '1', (), ('ab', 'c'), ('a', 'bc')]
    with native_frame(native, scope='left'):
        before = [render(Untouchable(), key=key, name='old')[1] for key in keys]
        after = [render(123, key=key, name='new')[1] for key in reversed(keys)]
        assert before == after[::-1]
        assert len(set(before)) == len(keys)
        for key in (True, 1.5, object(), ('nested', object())):
            with pytest.raises(TypeError, match='identity keys'):
                render(None, key=key)
    with native_frame(native, scope='right'):
        assert render(None, key=None)[1] != before[0]
    with native_frame(native, scope='left'):
        assert render(None, key=None)[1] == before[0]


@pytest.mark.parametrize('cached_parent', [False, True])
@pytest.mark.parametrize('cached_child', [False, True])
def test_cached_and_immediate_share_identity_tree(cached_parent, cached_child):
    cache = RetainedGui(graphics=False)
    native = _new_native(graphics=False, identities=cache.identities)
    seen = {}

    def leaf(input_value, draw_state=None):
        seen['leaf'] = draw_state.unique
        return False, input_value

    def root(input_value, draw_state=None):
        seen['root'] = draw_state.unique
        child(input_value, key='child')
        return False, input_value

    child = (cache.gui if cached_child else lambda f: _bind_native(native, f))(leaf)
    parent = (cache.gui if cached_parent else lambda f: _bind_native(native, f))(root)
    with native_frame(native):
        with cache.frame():
            parent(1)
    expected = dict(seen)
    # The same definitions in a fresh, entirely immediate tree must agree.
    other = _new_native(graphics=False)
    child = _bind_native(other, leaf)
    parent = _bind_native(other, root)
    with native_frame(other):
        parent(2)
    assert seen == expected
    cache.close()


def test_independent_redraw_retirement_rename_and_error_unwind():
    cache = RetainedGui(graphics=False)
    seen = []

    @cache.gui(state_id='stable-child')
    def child(input_value, draw_state=None):
        seen.append(draw_state.unique)
        if input_value == 'error': raise ValueError('deliberate')
        return False, input_value

    @cache.gui
    def parent(input_value):
        if input_value: child(input_value)
        return False, input_value

    with cache.frame(): parent('visible')
    cache.invalidate(child)
    cache.flush()
    with cache.frame(): parent(None)
    child.__wrapped__.__name__ = 'renamed'
    with cache.frame(): parent('visible')
    assert len(seen) >= 3 and len(set(seen)) == 1
    with pytest.raises(ValueError, match='deliberate'):
        with cache.frame(): parent('error')
    with cache.frame(): parent('visible')
    assert len(set(seen)) == 1
    cache.close()


def test_explicit_renderer_identity_survives_function_rename():
    native = _new_native(graphics=False)
    def old(input_value, draw_state=None): return False, draw_state.unique
    def new(input_value, draw_state=None): return False, draw_state.unique
    old_view = _bind_native(native, old, state_id='editor')
    new_view = _bind_native(native, new, state_id='editor')
    with native_frame(native):
        old_id = old_view(None)[1]
    with native_frame(native):
        assert old_id == new_view(None)[1]


@pytest.mark.parametrize('cached_parent', [False, True])
@pytest.mark.parametrize('cached_child', [False, True])
def test_names_and_keys_do_not_shift_anonymous_occurrences(cached_parent, cached_child):
    cache = RetainedGui(graphics=False)
    native = _new_native(graphics=False, identities=cache.identities)
    frames = []

    def body(input_value, draw_state=None):
        return False, draw_state.unique

    child = (cache.gui if cached_child else lambda f: _bind_native(native, f))(body)

    def root(input_value):
        first = child(None)
        if input_value:
            child({'hello', 1}, name='view1', melty_window=True)
        named = child({'hello', 1}, name='view2', melty_window=True)
        keyed = child(None, key='view2', name=str(input_value))
        zero = child(None, key=0)
        second = child(None, name='')
        frames.append(tuple(value for _, value in (first, named, keyed, zero, second)))

    parent = (cache.gui if cached_parent else lambda f: _bind_native(native, f))(root)
    for show in (True, False, True):
        with native_frame(native):
            with cache.frame():
                parent(show)
    assert frames[0] == frames[1] == frames[2]
    assert len(set(frames[0])) == 5
    cache.close()


def test_root_occurrences_and_independent_redraw_do_not_consume_host_occurrences():
    cache = RetainedGui(graphics=False)

    @cache.gui
    def leaf(input_value, draw_state=None):
        return False, draw_state.unique

    @cache.gui
    def parent(input_value, draw_state=None):
        return False, (draw_state.unique, leaf(None)[1], leaf(None)[1])

    snapshots = []
    for _ in range(3):
        with cache.frame():
            first = parent(None)[1]
            cache.invalidate(parent)
            cache.flush()
            second = parent(None)[1]
            snapshots.append((first, second))
    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert len(set(snapshots[0][0] + snapshots[0][1])) == 6
    cache.close()


@pytest.mark.parametrize('cached', [False, True])
def test_occurrence_scopes_unwind_after_error(cached):
    cache = RetainedGui(graphics=False)
    native = _new_native(graphics=False, identities=cache.identities)
    bind = cache.gui if cached else lambda f: _bind_native(native, f)
    seen = []

    @bind
    def child(input_value, draw_state=None):
        seen.append(draw_state.unique)
        if input_value: raise ValueError('deliberate')

    @bind
    def parent(input_value):
        child(input_value)
        child(input_value)

    with pytest.raises(ValueError, match='deliberate'):
        with native_frame(native):
            with cache.frame(): parent(True)
    with native_frame(native):
        with cache.frame(): parent(False)
    assert seen[0] == seen[1] != seen[2]
    assert not cache.identities.active
    cache.close()


@pytest.mark.parametrize('identity', [{'name': 'same'}, {'key': 'same'}])
def test_duplicate_explicit_identity_still_rejected(identity):
    cache = RetainedGui(graphics=False)
    @cache.gui
    def child(input_value): pass
    @cache.gui
    def parent(input_value):
        child(None, **identity)
        child(None, **identity)
    with pytest.raises(RuntimeError, match='duplicate @gui'):
        with cache.frame(): parent(None)
    cache.close()
