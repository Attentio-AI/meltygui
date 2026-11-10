"""Host hooks of the retained graph, without a GPU: caller-bound nodes,
per-view placement, declaration and failure callbacks. The legacy bridge
(core/rendering/gui_compat.py) is built on them."""
import pytest
from meltygui.core.rendering.retained_gui_prototype import RetainedGui


def test_bound_node_runs_only_where_its_caller_declares_it():
    cache = RetainedGui(graphics=False)
    runs = {'child': 0, 'parent': 0}
    nodes = {}

    @cache.gui()
    def child(input_value):
        runs['child'] += 1
        nodes['child'] = cache.current

    @cache.gui()
    def parent(input_value):
        runs['parent'] += 1
        child(None)

    try:
        parent(None)
        graph = cache.graph
        graph.bind(nodes['child'])
        # The graph's own invalidation: reported once, never run by flush.
        cache.invalidate_id(nodes['child'])
        cache.invalidate_id(nodes['child'])
        cache.flush()
        assert runs == {'child': 1, 'parent': 1}
        assert graph.take_woken() == [nodes['child']]
        assert graph.take_woken() == []
        assert nodes['child'] not in graph.dirty_nodes()
        # It is still dirty: the caller's next execution runs it in place.
        cache.invalidate(parent)
        cache.flush()
        assert runs == {'child': 2, 'parent': 2}
        # The host's own invalidation is not reported back to it.
        graph.invalidate_bound(nodes['child'])
        assert graph.take_woken() == []
        cache.invalidate(parent)
        cache.flush()
        assert runs['child'] == 3
    finally:
        cache.close()


def test_returned_edit_from_an_independent_child_wakes_a_bound_caller():
    cache = RetainedGui(graphics=False)
    nodes, fire = {}, {'now': False}

    @cache.gui()
    def child(input_value: int):
        nodes['child'] = cache.current
        if fire['now']:
            fire['now'] = False
            return True, input_value + 1

    @cache.gui()
    def parent(input_value):
        nodes['parent'] = cache.current
        return child(input_value)

    try:
        assert parent(1) == (False, 1)
        cache.graph.bind(nodes['parent'])
        fire['now'] = True
        cache.invalidate_id(nodes['child'])
        cache.flush()
        # The child ran alone; its caller must run to receive the edit, and
        # only its host may run it.
        assert cache.graph.take_woken() == [nodes['parent']]
        assert parent(1) == (True, 2)
    finally:
        cache.close()


def test_place_and_declare_hooks_replace_flow_placement():
    cache = RetainedGui(graphics=False)
    placed, declared = [], []
    cache.on_declare = lambda node, parent: declared.append((node, parent))

    @cache.gui(place=placed.append)
    def hosted(input_value):
        return False, input_value

    @cache.gui()
    def plain(input_value):
        hosted(input_value)

    try:
        assert hosted('a') == (False, 'a')
        root = placed[-1]
        assert declared == [(root, 0)]
        assert hosted('a') == (False, 'a')
        assert placed == [root, root]
        plain('b')
        nested = placed[-1]
        assert nested != root and (nested, cache.graph.parent(nested)) in declared
    finally:
        cache.close()


def test_failure_hook_contains_a_view_the_graph_ran_on_its_own():
    cache = RetainedGui(graphics=False)
    failures, broken, nodes = [], {'now': False}, {}

    @cache.gui()
    def fragile(input_value):
        nodes['fragile'] = cache.current
        if broken['now']:
            raise ValueError('body failed')

    @cache.gui()
    def sound(input_value):
        nodes['sound'] = cache.current
        fragile(None)

    try:
        sound(None)
        broken['now'] = True
        cache.invalidate_id(nodes['fragile'])
        with pytest.raises(ValueError, match='body failed'):
            cache.flush()
        cache.on_failure = lambda node, error: failures.append((node, str(error))) or cache.graph.bind(node)
        cache.flush()
        assert failures == [(nodes['fragile'], 'body failed')]
        # Bound by the hook: later flushes leave it to its caller.
        cache.flush()
        assert len(failures) == 1
    finally:
        cache.close()
