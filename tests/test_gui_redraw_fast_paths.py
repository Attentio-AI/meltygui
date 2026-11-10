"""Behavior the redraw fast paths must preserve.

A repeated declaration reuses its resolved rectangle and arguments, capture
arguments are layered instead of copied, and unchanged layout shares one
transaction snapshot. These checks pin the cases where a shortcut must not be
taken, without a GPU.
"""
import pytest

from meltygui.core.rendering._gui_native import same_arguments
from meltygui.core.rendering.retained_gui_prototype import RetainedGui


def test_same_arguments_compares_by_identity_or_exact_primitive():
    shared = [1, 2]
    assert same_arguments({'a': 1, 'b': 'x', 'c': shared, 'd': None}, {'a': 1, 'b': 'x', 'c': shared, 'd': None})
    # Equal but distinct objects are different arguments; nothing is hashed or compared deeply.
    assert not same_arguments({'c': [1, 2]}, {'c': [1, 2]})
    assert not same_arguments({'a': 1}, {'a': 1.0})
    assert not same_arguments({'a': True}, {'a': 1})
    assert not same_arguments({'a': 1}, {'a': 1, 'b': 2})
    # Caller key order decides which keys count as explicit, so it must match.
    assert not same_arguments({'a': 1, 'b': 2}, {'b': 2, 'a': 1})


def test_repeated_call_skips_the_body_and_a_changed_argument_reaches_it():
    cache = RetainedGui(graphics=False)
    seen = []

    @cache.gui(width=100, height=20)
    def view(input_value, level=0):
        seen.append(level)
        return False, input_value

    try:
        model = object()
        with cache.frame(): assert view(model, level=1) == (False, model)
        with cache.frame(): assert view(model, level=1) == (False, model)
        assert seen == [1]
        with cache.frame(): view(model, level=2)
        assert seen == [1, 2]
        # A new input object with identical arguments still invalidates.
        with cache.frame(): view(object(), level=2)
        assert seen == [1, 2, 2]
    finally:
        cache.close()


def test_explicit_caller_geometry_overrides_state_edited_since_the_last_call():
    cache = RetainedGui(graphics=False)
    states = []

    @cache.gui(width=100, height=20)
    def view(input_value, draw_state=None):
        states.append(draw_state)
        return False, input_value

    try:
        with cache.frame(): view(None, left=5, top=7)
        state = states[0]
        assert (state.left, state.top) == (5, 7)
        # An edit between identical calls must not survive an explicit caller value.
        state.left = 40
        with cache.frame(): view(None, left=5, top=7)
        assert (state.left, state.top) == (5, 7)
        # Rewriting the same value changes nothing and needs no new resolution.
        state.top = 7
        with cache.frame(): view(None, left=5, top=7)
        assert (state.left, state.top) == (5, 7)
    finally:
        cache.close()


def test_authored_state_persists_across_identical_calls_without_caller_geometry():
    cache = RetainedGui(graphics=False)
    states = []

    @cache.gui(width=100, height=20)
    def view(input_value, draw_state=None):
        states.append(draw_state)
        return False, input_value

    try:
        with cache.frame(): view(None)
        state = states[0]
        state.left, state.width = 33, 150
        for _ in range(3):
            with cache.frame(): view(None)
            assert (state.left, state.width) == (33, 150)
        del state.left
        with cache.frame(): view(None)
        assert state.left == 0
    finally:
        cache.close()


def test_position_default_edit_re_resolves_a_memoized_declaration():
    cache = RetainedGui(graphics=False)

    @cache.gui
    def view(input_value, left=5, draw_state=None): return False, draw_state
    def changed(input_value, left=15, draw_state=None): return False, draw_state

    try:
        with cache.frame(): state = view(None)[1]
        with cache.frame(): view(None)
        assert state.left == 5
        view.__wrapped__.__code__ = changed.__code__
        view.__wrapped__.__defaults__ = changed.__defaults__
        with cache.frame(): view(None)
        assert state.left == 15
    finally:
        cache.close()


def test_body_parameters_see_current_geometry_and_explicit_arguments():
    cache = RetainedGui(graphics=False)
    seen = []

    @cache.gui(width=120, height=30, tag='decorated')
    def view(input_value, left=0, top=0, width=0, height=0, scroll_offset=None, tag=None, depth=None,
             unique=None, cache=None, view_events=None, draw_state=None):
        seen.append(dict(left=left, top=top, width=width, height=height, scroll_offset=scroll_offset, tag=tag,
                         depth=depth, unique=unique, cache=cache, view_events=view_events, state=draw_state))
        return False, input_value

    try:
        with cache.frame(): view('value', left=4, top=6, tag='called')
        first = seen[-1]
        assert (first['left'], first['top'], first['width'], first['height']) == (4, 6, 120, 30)
        assert first['scroll_offset'] == (0, 0) and first['tag'] == 'called' and first['depth'] == 0
        assert first['cache'] is cache and first['view_events'] == {} and first['unique'] == first['state'].unique
        # Geometry edited on the state reaches the next execution without a new declaration.
        first['state'].width = 200
        cache.flush()
        assert seen[-1]['width'] == 200 and seen[-1]['tag'] == 'called'
    finally:
        cache.close()


def test_var_keyword_body_still_receives_every_framework_value():
    cache = RetainedGui(graphics=False)
    seen = []

    @cache.gui(width=120, height=30)
    def view(input_value, **extra):
        seen.append(extra)
        return False, input_value

    try:
        with cache.frame(): view('value', custom=3)
        extra = seen[-1]
        assert extra['custom'] == 3 and extra['width'] == 120 and extra['height'] == 30
        assert {'left', 'top', 'scroll_offset', 'draw_state', 'unique', 'depth', 'cache', 'view_events'} <= extra.keys()
    finally:
        cache.close()


def test_retired_view_releases_its_memoized_caller_arguments():
    import gc, weakref
    cache = RetainedGui(graphics=False)

    class Payload: pass
    show = [True]

    # Declared parameters are mirrored on persistent state by design; a
    # **kwargs value lives only in the declaration's arguments.
    @cache.gui(width=50, height=10)
    def child(input_value, **extra): return False, input_value

    @cache.gui(width=100, height=40)
    def root(input_value, **extra):
        if show[0]: child(None, payload=extra['payload'])
        return False, input_value

    try:
        payload = Payload()
        reference = weakref.ref(payload)
        with cache.frame(): root(0, payload=payload)
        show[0] = False
        del payload
        # The parent re-executes without the child and without the payload.
        with cache.frame(): root(1)
        gc.collect()
        assert reference() is None
    finally:
        cache.close()


def test_layout_snapshot_shared_by_nested_owners_restores_after_a_caught_child_failure():
    cache = RetainedGui(graphics=False)
    mode = ['build']

    @cache.gui(width=80, height=40)
    def child(input_value=None):
        if mode[0] == 'fail':
            cache.geometry.declare(2, axis=1, key='child-rows', mins=0)
            raise ValueError('child failed')
        return False, input_value

    @cache.gui(width=300, height=200)
    def root(input_value=None):
        layout = cache.geometry.declare(2, axis=0, key='columns', mins=0)
        with layout.cell(0):
            if mode[0] == 'fail':
                # The child's rollback restores a snapshot this owner also holds.
                with pytest.raises(ValueError, match='child failed'):
                    child()
                cache.geometry.declare(3, axis=1, key='late-rows', mins=0)
                raise RuntimeError('root failed')
            child()
        return False, input_value

    try:
        root()
        geometry = cache.geometry
        before = (set(geometry.layouts), set(geometry.frames), set(geometry.bindings),
                  [axis.stats()[:2] for axis in geometry.axes], [axis.values() for axis in geometry.axes])
        mode[0] = 'fail'
        cache.invalidate(root)
        cache.invalidate(child)
        with pytest.raises(RuntimeError, match='root failed'):
            root()
        after = (set(geometry.layouts), set(geometry.frames), set(geometry.bindings),
                 [axis.stats()[:2] for axis in geometry.axes], [axis.values() for axis in geometry.axes])
        assert after == before
        assert not geometry.transactions and not geometry.staged
        mode[0] = 'build'
        root()
        assert set(geometry.layouts) == before[0]
    finally:
        cache.close()


def test_hit_regions_follow_the_latest_execution():
    cache = RetainedGui(graphics=False)
    regions = [True]

    @cache.gui(width=100, height=40)
    def view(input_value, cache=None):
        if regions[0]: cache.region('button', (0, 0, 10, 10))
        return False, input_value

    try:
        with cache.frame(): view(0)
        node = next(iter(cache.records))
        assert cache.regions[node] == [('button', (0, 0, 10, 10))]
        regions[0] = False
        with cache.frame(): view(1)
        assert not cache.regions.get(node)
    finally:
        cache.close()


def test_unchanged_children_finish_without_their_bodies_when_the_parent_re_executes():
    cache = RetainedGui(graphics=False)
    calls = {'parent': 0, 'child': 0}
    results = []

    @cache.gui(width=40, height=10)
    def child(input_value, label=''):
        calls['child'] += 1
        return False, input_value

    @cache.gui(width=200, height=100)
    def parent(input_value):
        calls['parent'] += 1
        results[:] = [child(item, key=index, label='x', return_extras=index == 0)
                      for index, item in enumerate(input_value)]
        return False, input_value

    try:
        items = [object(), object(), object()]
        with cache.frame(): parent(items)
        assert calls == {'parent': 1, 'child': 3}
        first_state = results[0][2]
        root = next(iter(cache.root_ids))
        cache.invalidate_id(root)
        with cache.frame(): parent(items)
        # Declared again natively: same values, same state object, no body calls.
        assert calls == {'parent': 2, 'child': 3}
        assert [result[:2] for result in results] == [(False, item) for item in items]
        assert results[0][2] is first_state and len(results[1]) == 2
        # A new input for one child executes exactly that child.
        items[1] = object()
        cache.invalidate_id(root)
        with cache.frame(): parent(items)
        assert calls == {'parent': 3, 'child': 4} and results[1] == (False, items[1])
        # Children stay declared: none of them retired along the way.
        assert len(cache.records) == 4
    finally:
        cache.close()


def test_child_edit_still_reaches_the_parent_after_unchanged_declarations():
    cache = RetainedGui(graphics=False)
    seen = []

    @cache.gui(width=40, height=10)
    def child(input_value, view_events=None):
        if view_events.get('bump'):
            return True, input_value + 1
        return False, input_value

    @cache.gui(width=200, height=100)
    def parent(input_value):
        changed, value = child(input_value['count'], key='only')
        if changed:
            input_value['count'] = value
        seen.append((changed, value))
        return False, input_value

    try:
        model = {'count': 1}
        with cache.frame(): parent(model)
        root = next(iter(cache.root_ids))
        child_node = next(node for node in cache.records if node != root)
        cache.invalidate_id(root)
        with cache.frame(): parent(model)
        assert seen[-1] == (False, 1)
        cache.send(child_node, 'bump')
        cache.flush()
        assert model['count'] == 2 and (True, 2) in seen
        with cache.frame(): parent(model)
        assert seen[-1] == (False, 2)
    finally:
        cache.close()


def test_one_definition_shares_its_native_engine_across_identities_and_nesting():
    cache = RetainedGui(graphics=False)
    depths = []

    @cache.gui(width=100, height=20)
    def view(input_value, depth=None, draw_state=None):
        depths.append(depth)
        if input_value:
            view(input_value - 1, key=input_value)
        return False, draw_state

    try:
        with cache.frame(): state = view(3)[1]
        natives = {id(binding.native) for binding in cache.bindings.values()}
        renderers = {id(binding.call) for binding in cache.bindings.values()}
        assert len(cache.bindings) == 4 and len(natives) == 1 and len(renderers) == 1
        # Every capture is its own root, including a function nested in itself.
        assert depths == [0, 0, 0, 0]
        assert len({id(binding.state) for binding in cache.bindings.values()}) == 4
        assert state is next(iter(cache.states.values()))
    finally:
        cache.close()


def test_memoized_raster_policy_terms_match_direct_estimates():
    from meltygui.core.rendering.gui_cache_policy import RasterPolicy
    from meltygui.core.rendering.gui_calibration import DEFAULT_COSTS

    class Direct:
        """The same snapshot under another identity, so nothing is reused."""
        estimate = staticmethod(DEFAULT_COSTS.estimate)

    memoized, fresh = RasterPolicy(), RasterPolicy()
    for replays in range(1, 12):
        for node, pixels, payload, commands in ((1, 4096, 800, 2), (2, 4096, 80000, 40), (3, 250000, 800, 1)):
            observation = (node, replays // 4, replays, 3, payload, commands)
            expected = fresh.choose(observation, Direct(), None, pixels=pixels, raster=False)
            fresh._by_pixels.clear(); fresh._by_bytes.clear()
            assert memoized.choose(observation, DEFAULT_COSTS, None, pixels=pixels, raster=False) == expected
    assert memoized.candidates.keys() == fresh.candidates.keys()
    assert all(memoized.candidates[node] == fresh.candidates[node] for node in fresh.candidates)
