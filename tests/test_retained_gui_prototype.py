"""Behavioral checks for the throwaway retained graph, without a GPU."""
import pytest
from meltygui.core.rendering.retained_gui_prototype import RetainedGui


@pytest.mark.parametrize('position,claimed', [((60, 65), True), ((400, 400), False)])
def test_retained_window_claims_drag_but_background_still_moves_host(position, claimed):
    from meltygui.core.input.input_handler import InputHandler
    cache = RetainedGui(graphics=False)

    @cache.gui(width=100, height=80)
    def view(input_value: object):
        return False, input_value

    view(None, melty_window=True, window_pos=(50, 50))
    handler = InputHandler()
    handler.begin_frame()
    handler.register_hovered('os-background', ['non_blocking_left_mouse_dragged'], priority=10000)
    cache.register_host_pointer(handler, position)
    handler.feed_down('left_mouse', *position, t=1.)
    handler.process_frame()
    handler.feed_move(position[0] + 50, position[1] + 50, t=1.1)
    events, _ = handler.process_frame()
    assert ('os-background' in events) is not claimed
    assert (('gui-pointer', id(cache)) in events) is claimed
    cache.close()


def test_independent_redraw_and_returned_scalar_replay():
    cache = RetainedGui(graphics=False)
    calls = dict(root=0, parent=0, child=0)
    ids = {}
    data = {'value': 1}

    @cache.gui
    def child(input_value: int, cache=None, view_events=None):
        calls['child'] += 1
        ids['child'] = cache.current
        return ('increment' in view_events, input_value + int('increment' in view_events))

    @cache.gui
    def parent(input_value: dict, cache=None):
        calls['parent'] += 1
        ids['parent'] = cache.current
        changed, value = child(input_value['value'])
        input_value['value'] = value
        return changed, input_value

    @cache.gui
    def root(input_value: dict):
        calls['root'] += 1
        return parent(input_value)

    root(data)
    baseline = calls.copy()
    cache.invalidate_id(ids['child'])
    cache.flush()
    assert calls == {**baseline, 'child': baseline['child'] + 1}
    cache.send(ids['child'], 'increment')
    cache.flush()
    assert data['value'] == 2
    before = calls.copy()
    cache.flush()
    assert calls == before  # result consumption converges, no permanent dirty loop
    assert root(data)[0] is True  # root return reaches its external caller once
    assert root(data)[0] is False
    cache.send(ids['child'], 'hover', 'plus')
    cache.flush()
    assert cache.graph.result(ids['child'], False) == (False, 2)
    assert data['value'] == 2  # a pixel-only redraw must not replay old input=1
    cache.close()


def test_conditional_window_reconciliation_under_cached_grandparent():
    cache = RetainedGui(graphics=False)
    state = {'closed': False}
    calls = [0, 0, 0]
    ids = {}

    @cache.gui
    def window(input_value: object, cache=None):
        calls[2] += 1
        ids['window'] = cache.current
        cache.region('click', (0, 0, 20, 20))
        return False, input_value

    @cache.gui
    def owner(input_value: dict, cache=None):
        calls[1] += 1
        ids['owner'] = cache.current
        cache.associate('/tmp/prototype-window-control')
        if not input_value['closed']:
            window(None, melty_window=True)
        return False, input_value

    @cache.gui
    def grandparent(input_value: object):
        calls[0] += 1
        return owner(state)

    grandparent(None)
    assert len(cache.graph.windows()) == 1
    assert cache.graph.contains(ids['window'])
    cache.flush()
    assert calls == [1, 1, 1]
    state['closed'] = True
    cache.invalidate_id(ids['window'])  # ownership removal wins over queued child work
    cache.invalidate('/tmp/prototype-window-control')
    cache.flush()
    assert calls == [1, 2, 1]
    assert cache.graph.windows() == []
    assert not cache.graph.contains(ids['window'])
    assert ids['window'] not in cache.records
    assert ids['window'] not in cache.regions
    state['closed'] = False
    cache.invalidate(state)
    cache.flush()
    assert calls == [1, 3, 2]
    assert len(cache.graph.windows()) == 1
    cache.close()


def test_external_host_frame_retires_roots_and_associated_references():
    import weakref
    import gc
    cache = RetainedGui(graphics=False)

    class Model:
        def __eq__(self, other):
            raise AssertionError('models must not be compared')

        def __hash__(self):
            raise AssertionError('models must not be hashed')

    @cache.gui
    def view(input_value: object):
        return False, input_value

    model = Model()
    reference = weakref.ref(model)
    with cache.frame():
        view(model)
    cache.invalidate(model)
    cache.flush()
    del model
    with cache.frame():
        pass  # external conditional no longer declares the root
    gc.collect()
    assert reference() is None
    assert cache.graph.nodes() == [] and cache.records == {}
    cache.close()


def test_replaced_input_does_not_retain_old_model_or_invalidation_alias():
    import weakref
    import gc
    cache = RetainedGui(graphics=False)

    class Model:
        pass

    @cache.gui
    def view(input_value: object):
        return False, input_value

    model = Model()
    reference = weakref.ref(model)
    view(model)
    view(Model())
    assert cache.invalidate(model) == 0
    del model
    gc.collect()
    assert reference() is None
    cache.close()


def test_body_hot_replacement_invalidates_without_changing_identity():
    cache = RetainedGui(graphics=False)

    @cache.gui
    def view(input_value: object):
        return False, input_value

    view(4)
    node = cache.graph.nodes()[0]

    def replacement(input_value: object, extra=2):
        return True, input_value + extra

    view.__wrapped__.__code__ = replacement.__code__
    view.__wrapped__.__defaults__ = replacement.__defaults__
    # The changed signal repeats forever in this artificial function; execute one
    # dirty root, whose returned result is consumed externally, without polling it.
    cache.flush()
    assert cache.graph.nodes() == [node]
    assert view(4) == (True, 6)
    cache.close()


def test_stale_caller_echo_does_not_undo_independent_edit():
    cache = RetainedGui(graphics=False)

    @cache.gui
    def number(input_value: int, view_events=None):
        return bool(view_events.get('plus')), input_value + int(bool(view_events.get('plus')))

    number(10)
    node = cache.graph.nodes()[0]
    cache.send(node, 'plus')
    cache.flush()
    assert number(10) == (True, 11)
    assert number(10) == (False, 11)
    cache.send(node, 'hover', True)
    cache.flush()
    assert number(10) == (False, 11)
    assert number(11) == (False, 11)  # caller acknowledges the return
    assert number(10) == (False, 10)  # now an intentional reset is a new input
    cache.close()


def test_fresh_injection_dynamic_args_and_state_alias():
    cache = RetainedGui(graphics=False)
    seen = []

    @cache.gui
    def view(input_value: object, draw_state=None, completely_new=3, view_events=None, **kwargs):
        seen.append((completely_new, dict(view_events)))
        return False, input_value

    _, _, state = view(None, completely_new=9, custom_caller='hello', return_extras=True)
    node = cache.graph.nodes()[0]
    cache.send(node, 'click')
    cache.flush()
    cache.invalidate(state)
    cache.flush()
    assert seen == [(9, {}), (9, {'click': True}), (9, {})]
    view(None, completely_new=10, custom_caller='hello')
    assert seen[-1][0] == 10
    cache.close()


def test_failed_owner_keeps_previous_declarations_and_retires_new_ones():
    cache = RetainedGui(graphics=False)
    broken = [False]

    @cache.gui
    def child(input_value: object):
        return False, input_value

    @cache.gui
    def owner(input_value: object):
        child(None, key='old', melty_window=True)
        if broken[0]:
            child(None, key='new', melty_window=True)
            raise ValueError('deliberate')
        return False, input_value

    owner(None)
    original = cache.graph.windows()
    broken[0] = True
    cache.invalidate(owner)
    with pytest.raises(ValueError, match='deliberate'):
        cache.flush()
    assert cache.graph.windows() == original
    cache.close()


def test_window_movement_is_compositor_only_and_does_not_poll_owners():
    cache = RetainedGui(graphics=False)
    count = [0]

    @cache.gui
    def window(input_value: object):
        count[0] += 1
        return False, input_value

    @cache.gui
    def owner(input_value: object):
        count[0] += 1
        return window(None, melty_window=True)

    owner(None)
    node = cache.graph.windows()[0]
    for x in range(20):
        cache.graph.move_window(node, x, 30)
        cache.flush()
    assert count == [2]
    assert cache.graph.info(node)['rect'][:2] == (19, 30)
    cache.close()


def test_native_hit_query_matches_clipping_and_z_order_after_mutations():
    """Compare against the original Python geometry rules, including portal escape."""
    cache = RetainedGui(graphics=False)
    g = cache.graph
    def add(parent, key, rect, portal=False):
        node = g.declare(parent, 1, key, None, {}, rect, portal)
        g.begin(node)
        return node
    root = add(0, 'root', (10.25, 20.5, 180, 140))
    clipped = add(root, 'clip', (12.5, 15.25, 55, 40))
    leaf = add(clipped, 'leaf', (25.25, 20.5, 100, 90))
    g.commit(leaf, (False, None), False)
    escaped = add(clipped, 'portal', (80.25, 60.5, 100, 80), True)
    content = add(escaped, 'body', (5.25, 6.5, 160, 120))
    g.commit(content, (False, None), False)
    g.commit(escaped, (False, None), False)
    g.commit(clipped, (False, None), False)
    upper = add(root, 'upper', (105.5, 95.25, 110, 70), True)
    g.commit(upper, (False, None), False)
    g.commit(root, (False, None), False)
    def check():
        cache._positions.clear()
        for x in range(-10, 300, 7):
            for y in range(-10, 300, 9):
                portal = next((n for n in reversed(g.windows())
                               if (lambda r: r[0] <= x < r[0] + r[2] and
                                   r[1] <= y < r[1] + r[3] + 28)(cache.window_rect(n))), None)
                expected = []
                for n in reversed(g.nodes()):
                    ancestor, owner = n, None
                    while ancestor:
                        info = g.info(ancestor)
                        if info['portal']:
                            owner = ancestor
                            break
                        ancestor = info['parent']
                    if owner == portal and cache._inside_clip(n, x, y):
                        expected.append((n, *cache._position(n)))
                assert g.hits(x, y) == (portal, expected)
    try:
        check()
        g.raise_window(escaped)
        check()
        g.move_window(escaped, -8.25, 12.5)
        g.allocate(root, (20.5, 40.25, 100, 90))
        check()
        g.retire(upper)
        check()
    finally:
        cache.close()
