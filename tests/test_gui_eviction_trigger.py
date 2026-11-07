"""Eviction eligibility changes without repeatedly walking protected hot tiles."""
import pytest

from meltygui.core.rendering.retained_gui_prototype import RetainedGui


class ObservedGraph:
    def __init__(self, graph):
        self.graph = graph
        self.visibility_checks = 0

    def __getattr__(self, name):
        return getattr(self.graph, name)

    def visible(self, node):
        self.visibility_checks += 1
        return self.graph.visible(node)


def scene():
    cache = RetainedGui(graphics=True)
    graph = cache.graph = ObservedGraph(cache.graph)
    root = graph.declare(0, 1, 'root', None, {}, (0, 0, 100, 100), False, scrollable=True)
    graph.begin(root)
    children = []
    for index in range(3):
        child = graph.declare(root, 2, index, None, {}, (0, index*40, 100, 40), False)
        graph.begin(child)
        graph.commit(child, (False, None), False)
        cache.capture_info[child] = (root, 16000, child)
        cache.capture_children[root].add(child)
        cache.scroll_tiles[child] = 16000
        cache.scroll_tile_bytes += 16000
        children.append(child)
    graph.commit(root, (False, None), False)
    cache.max_scroll_tiles = 1
    return cache, root, children


def test_unchanged_protected_set_skips_scan_but_scroll_releases_offscreen_tiles():
    cache, root, children = scene()
    try:
        cache._evict_scroll_tiles()
        assert cache.graph.visibility_checks == 3
        for _ in range(20):
            cache._evict_scroll_tiles()
        assert cache.graph.visibility_checks == 3
        state = cache.graph.state(children[0])
        cache.graph.state(root).scroll_offset = (0, 80)
        cache._evict_scroll_tiles()
        assert list(cache.scroll_tiles) == [children[2]]
        assert cache.graph.state(children[0]) is state
        assert cache.graph.dirty(children[0])
        assert cache.scroll_tile_bytes == 16000
    finally:
        cache.close()


@pytest.mark.parametrize('field', ['focus', 'capture', 'active'])
def test_releasing_input_protection_retries_eviction(field):
    cache, root, children = scene()
    try:
        cache.graph.state(root).scroll_offset = (0, 80)
        setattr(cache.imgui_input, field, {children[0]} if field == 'active' else children[0])
        cache._evict_scroll_tiles()
        assert list(cache.scroll_tiles) == [children[0], children[2]]
        before = cache.graph.visibility_checks
        cache._evict_scroll_tiles()
        assert cache.graph.visibility_checks == before
        setattr(cache.imgui_input, field, set() if field == 'active' else None)
        cache._evict_scroll_tiles()
        assert list(cache.scroll_tiles) == [children[2]]
    finally:
        cache.close()


@pytest.mark.parametrize('change', ['size', 'measured_size', 'position', 'content_offset'])
def test_geometry_changes_retry_visibility(change):
    cache, root, children = scene()
    try:
        cache._evict_scroll_tiles()
        if change == 'size':
            cache.graph.state(root).height = 40
        elif change == 'measured_size':
            cache.graph.measured(root, 100, 40)
        elif change == 'position':
            cache.graph.state(children[1]).top = 200
            cache.graph.state(children[2]).top = 240
        else:
            cache.graph.state(children[1]).content_offset = (0, 200)
            cache.graph.state(children[2]).content_offset = (0, 200)
        cache._evict_scroll_tiles()
        assert len(cache.scroll_tiles) < 3
    finally:
        cache.close()


def test_budget_changes_and_new_residency_retry_eviction():
    cache, root, children = scene()
    try:
        cache.max_scroll_tiles = 3
        cache.graph.state(root).height = 40
        cache._evict_scroll_tiles()
        assert len(cache.scroll_tiles) == 3
        cache.scroll_tile_budget = 16000
        cache._evict_scroll_tiles()
        assert list(cache.scroll_tiles) == [children[0]]
        # A newly captured offscreen resource must invalidate the settled result.
        child = children[1]
        cache.capture_info[child] = (root, 16000, child)
        cache.capture_children[root].add(child)
        cache.scroll_tiles[child] = 16000
        cache.scroll_tile_bytes += 16000
        cache._scroll_tile_revision += 1
        cache._evict_scroll_tiles()
        assert list(cache.scroll_tiles) == [children[0]]
    finally:
        cache.close()


def test_visibility_revision_tracks_geometry_topology_and_portals():
    cache, root, children = scene()
    try:
        graph = cache.graph
        revision = graph.visibility_revision()
        graph.state(root).height = 100
        graph.state(root).scroll_offset = (0, 0)
        assert graph.visibility_revision() == revision
        graph.begin(root)
        for index, child in enumerate(children):
            graph.declare(root, 2, index, None, {}, graph.rect(child), index == 0)
        graph.commit(root, (False, None), False)
        assert graph.visibility_revision() > revision
        assert children[0] in graph.windows()
        revision = graph.visibility_revision()
        graph.retire(children[1])
        assert graph.visibility_revision() > revision
    finally:
        cache.close()


def test_nested_portal_protects_tile_until_portal_is_removed():
    cache, root, children = scene()
    try:
        graph = cache.graph
        graph.begin(children[0])
        portal = graph.declare(children[0], 3, 'popup', None, {}, (0, 0, 20, 20), True)
        graph.begin(portal)
        graph.commit(portal, (False, None), False)
        graph.commit(children[0], (False, None), False)
        cache.capture_info[portal] = (children[0], 1600, children[0])
        cache.capture_children[children[0]].add(portal)
        cache.scroll_tiles[children[0]] += 1600
        cache.scroll_tile_bytes += 1600
        graph.state(root).scroll_offset = (0, 80)
        cache._evict_scroll_tiles()
        assert children[0] in cache.scroll_tiles
        assert portal in cache.capture_info
        graph.begin(children[0])
        graph.declare(children[0], 3, 'popup', None, {}, (0, 0, 20, 20), False)
        graph.commit(children[0], (False, None), False)
        cache._evict_scroll_tiles()
        assert list(cache.scroll_tiles) == [children[2]]
        assert portal not in cache.capture_info
        assert graph.state(portal) is not None
        assert cache.scroll_tile_bytes == 16000
    finally:
        cache.close()
