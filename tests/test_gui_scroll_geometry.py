"""State-backed relative geometry and cached viewport scrolling."""
from collections import Counter

import pytest

from meltygui.core.rendering.retained_gui_prototype import RetainedGui


def list_scene(count=1000, graphics=False, state_registry=None):
    cache = RetainedGui(graphics=graphics, state_registry=state_registry)
    calls = Counter()
    ids = {}

    @cache.gui(width=200, height=20)
    def row(input_value, cache=None):
        calls[input_value] += 1
        ids[input_value] = cache.current
        if graphics:
            from meltygui import imgui
            color = 0xFF0000FF if input_value % 2 == 0 else 0xFF00FF00
            imgui.get_window_draw_list().add_rect_filled(0, 0, 200, 20, color)
        cache.region('row', (0, 0, 200, 20))

    @cache.gui(width=200, height=100, scroll=True)
    def viewport(input_value, draw_state=None, cache=None):
        calls['viewport'] += 1
        ids['viewport'] = cache.current
        for i in range(count):
            row(i, key=i, left=0, top=i*20)
        return False, draw_state

    with cache.frame(): state = viewport(None)[1]
    return cache, state, calls, ids, viewport


def test_scroll_culls_and_reuses_child_bodies_without_moving_local_coordinates():
    cache, state, calls, ids, _ = list_scene()
    try:
        assert calls == Counter({**{i: 1 for i in range(5)}, 'viewport': 1})
        hidden = cache.graph.info(ids['viewport'])['children'][-1]
        assert cache.states[hidden].unique == cache.uniques[hidden]
        assert cache.position(hidden) == (0, 999*20)
        assert calls['viewport'] == 1 and len(calls) == 6
        before = cache.graph.geometry_stats()
        state.scroll_offset = (0, 400)
        cache.flush()
        assert calls['viewport'] == 1
        assert set(calls) == {*range(5), *range(20, 25), 'viewport'}
        assert (cache.states[ids[20]].left, cache.states[ids[20]].top) == (0, 400)
        assert cache.position(ids[20]) == (0, 0)
        assert cache.graph.hits(10, 10)[1][0][0] == ids[20]
        after = cache.graph.geometry_stats()
        assert after[0]-before[0] < 100  # Local candidates, not the 1000 declarations.
        previous = calls.copy()
        state.scroll_offset = (0, 0)
        cache.flush()
        assert calls == previous
    finally:
        cache.close()


def test_mid_frame_coordinate_conversion_and_unrelated_root_cache_hits():
    cache = RetainedGui(graphics=False)
    graph = cache.graph
    def add(parent, key, pos, size=(100, 100)):
        n = graph.declare(parent, 1, key, None, {}, (*pos, *size), False)
        graph.begin(n)
        return n
    a = add(0, 'a', (100, 200), (500, 500))
    b = add(a, 'b', (30, 40))
    c = add(b, 'c', (5, 6))
    graph.commit(c, (False, None), False)
    graph.commit(b, (False, None), False)
    graph.commit(a, (False, None), False)
    other = add(0, 'other', (500, 600))
    graph.commit(other, (False, None), False)
    try:
        assert graph.origin(c) == (135, 246)
        assert graph.origin(c, b, True) == (5, 6)
        graph.origin(other)
        graph.state(a).scroll_offset = (10, 20)
        assert graph.origin(c) == (125, 226)
        assert graph.convert_point((0, 0), c, a, False, True) == (35, 46)
        assert graph.convert_point((0, 0), c, other) == (-375, -374)
        before = graph.geometry_stats()[1]
        for _ in range(100):
            assert graph.origin(c) == (125, 226)
            assert graph.origin(other) == (500, 600)
        assert graph.geometry_stats()[1] == before
        graph.state(b).left, graph.state(b).top = (60, 70)
        assert graph.origin(c) == (155, 256)
        assert graph.info(c)['executions'] == 1
    finally:
        cache.close()


def test_state_identity_and_geometry_survive_hide_show_and_checkpoint():
    cache = RetainedGui(graphics=False)
    instances = []
    @cache.gui(inject={'local': list})
    def window(input_value, draw_state=None, local=None):
        instances.append((draw_state, local))
    @cache.gui
    def root(input_value):
        if input_value: window(None, name='window', melty_window=True,
                               initial={'width': 250, 'height': 150, 'left': 10, 'top': 20})
    try:
        with cache.frame(): root(True)
        state, local = instances[-1]
        local.append('kept')
        state.left, state.top = (80, 90)
        state.width, state.height = 350, 220
        cache.flush()
        with cache.frame(): root(False)
        cache.prepare_save()
        assert not cache.graph.windows()
        with cache.frame(): root(True)
        assert instances[-1][0] is state and instances[-1][1] is local
        assert local == ['kept']
        assert cache.graph.info(cache.graph.windows()[0])['rect'] == (80, 90, 350, 220)
    finally:
        cache.close()


def test_nested_wheel_routes_to_inner_then_outer_at_boundary():
    cache = RetainedGui(graphics=False)
    ids = {}
    @cache.gui(width=100, height=20)
    def row(input_value): pass
    @cache.gui(width=100, height=100, scroll=True)
    def inner(input_value, cache=None):
        ids['inner'] = cache.current
        for i in range(20): row(i, key=i, left=0, top=i*20)
    @cache.gui(width=150, height=150, scroll=True)
    def outer(input_value, cache=None):
        ids['outer'] = cache.current
        inner(None, left=10, top=10)
        row(None, key='end', left=0, top=500)
    try:
        with cache.frame(): outer(None)
        assert cache.scroll_at((20, 20), (0, 40))
        cache.flush()
        assert cache.states[ids['inner']].scroll_offset == (0, 40)
        assert cache.states[ids['outer']].scroll_offset == (0, 0)
        cache.states[ids['inner']].scroll_offset = (0, 300)
        assert cache.scroll_at((20, 20), (0, 40))
        cache.flush()
        assert cache.states[ids['outer']].scroll_offset == (0, 40)
        assert cache.graph.hits(20, 200)[1] == []
    finally:
        cache.close()


def test_viewport_gpu_translation_and_cached_pixels(gl_context):
    import numpy as np
    from test_retained_gui_gpu import pixels
    cache, state, calls, ids, _ = list_scene(graphics=True)
    try:
        texture = cache.gpu.texture(ids['viewport'])
        image = np.flipud(pixels(texture, 200, 100))
        assert tuple(image[10, 10]) == (255, 0, 0, 255)
        assert tuple(image[30, 10]) == (0, 255, 0, 255)
        state.scroll_offset = (0, 20)
        cache.flush()
        image = np.flipud(pixels(texture, 200, 100))
        assert tuple(image[10, 10]) == (0, 255, 0, 255)
        assert tuple(image[30, 10]) == (255, 0, 0, 255)
        assert calls['viewport'] == 1 and all(c == 1 for c in calls.values())
        before = calls.copy()
        state.left, state.top = (50, 60)
        cache.flush()
        assert calls == before
        assert cache.graph.hits(60, 70)[1][0][0] == ids[1]
    finally:
        cache.close()


def test_composite_tile_eviction_preserves_state_and_bounds_gpu_memory(gl_context):
    from meltygui import imgui
    cache = RetainedGui()
    cache.max_scroll_tiles = 8
    state_objects = {}
    calls = Counter()

    @cache.gui(width=100, height=20, inject={'local': list})
    def chip(input_value, local=None, draw_state=None):
        calls[input_value] += 1
        state_objects[input_value] = draw_state
        imgui.text(str(input_value))

    @cache.gui(width=200, height=20)
    def row(input_value):
        chip(input_value*2, key='left', left=0, top=0)
        chip(input_value*2+1, key='right', left=100, top=0)

    @cache.gui(width=200, height=100, scroll=True)
    def viewport(input_value, draw_state=None):
        for i in range(100): row(i, key=i, left=0, top=i*20)
        return False, draw_state

    try:
        with cache.frame(): state = viewport(None)[1]
        first = state_objects[0]
        first.local.append('retained')
        for offset in range(100, 1600, 100):
            state.scroll_offset = (0, offset)
            cache.flush()
        assert len(cache.scroll_tiles) <= 8
        assert len(cache.captures) <= 25
        assert cache.gpu.stats()[2] <= 80_000 + 8*32_000
        state.scroll_offset = (0, 0)
        cache.flush()
        assert state_objects[0] is first
        assert first.local == ['retained'] and calls[0] == 2
    finally:
        cache.close()


def test_unchanged_writes_and_position_queries_do_not_execute_bodies():
    cache, state, calls, ids, _ = list_scene(100)
    try:
        before = calls.copy()
        for _ in range(10):
            state.left, state.top = (0, 0)
            state.scroll_offset = (0, 0)
            state.width = 200
            cache.flush()
        assert calls == before
        assert 'abs_left' not in state.as_dict() and 'abs_top' not in state.as_dict()
        state.left, state.top = (50, 60)
        assert cache.position(ids[0]) == (50, 60)
        cache.flush()
        assert calls == before
    finally:
        cache.close()


def test_scroll_offset_restores_as_an_ordinary_state_parameter():
    from meltygui.core.conversion.load_save_v2 import dumps, loads
    from meltygui.core.runtime.app_session import AppSession, prune
    session = AppSession()
    cache, state, _, _, _ = list_scene(100, state_registry=session.draw_state_registry)
    state.scroll_offset = (0, 400)
    cache.flush()
    cache.close()
    session = loads(dumps(session), run_on_load=False)
    prune(session)
    cache, state, calls, ids, _ = list_scene(100, state_registry=session.draw_state_registry)
    try:
        assert state.scroll_offset == (0, 400)
        assert set(calls) == {*range(20, 25), 'viewport'}
        assert cache.graph.hits(10, 10)[1][0][0] == ids[20]
    finally:
        cache.close()


def test_window_drag_after_ancestor_scroll_preserves_relative_placement():
    from test_gui_collision_layout import attach_native
    cache = RetainedGui(graphics=False)
    ids, calls = {}, Counter()
    @cache.gui(width=100, height=60)
    def window(input_value, cache=None):
        ids['window'] = cache.current
        calls['window'] += 1
    @cache.gui(width=20, height=20)
    def end(input_value): pass
    @cache.gui(width=400, height=200, scroll=True)
    def viewport(input_value, cache=None):
        ids['viewport'] = cache.current
        window(None, melty_window=True, initial={'left': 20, 'top': 120})
        end(None, left=0, top=500)
    try:
        with cache.frame(): viewport(None)
        attach_native(cache, 640, 480)
        cache.states[ids['viewport']].scroll_offset = (0, 80)
        cache.flush()
        assert cache.window_rect(ids['window'])[:2] == (20, 40)
        before = calls.copy()
        cache.geometry.pointer((30, 50), pressed=True, down=True)
        cache.geometry.pointer((55, 65), down=True)
        cache.geometry.pointer((55, 65), released=True)
        cache.flush()
        assert (cache.states[ids['window']].left, cache.states[ids['window']].top) == (45, 135)
        assert cache.window_rect(ids['window'])[:2] == (45, 55)
        assert calls == before
    finally:
        cache.close()


def test_column_hit_and_drag_follow_scrolled_owner_without_body_reexecution():
    cache = RetainedGui(graphics=False)
    ids, layouts, calls = {}, {}, Counter()
    @cache.gui(width=100, height=40)
    def cell(input_value, cache=None):
        ids[input_value] = cache.current
    @cache.gui(width=200, height=40)
    def row(input_value, cache=None):
        calls['row'] += 1
        ids['row'] = cache.current
        with cache.geometry.declare(('a', 'b'), axis=0, key='columns', padding=0, mins=20) as layout:
            layouts['row'] = layout
            for key in ('a', 'b'):
                with layout.cell(key): cell(key, key=key)
    @cache.gui(width=200, height=100, scroll=True)
    def viewport(input_value, cache=None):
        ids['viewport'] = cache.current
        row(None, left=0, top=50)
    try:
        with cache.frame(): viewport(None)
        cache.states[ids['viewport']].scroll_offset = (0, 30)
        cache.flush()
        assert cache.geometry.hit(100, 30)[1] == [layouts['row']]
        before = calls.copy()
        cache.geometry.pointer((100, 30), pressed=True, down=True)
        cache.geometry.pointer((120, 30), down=True)
        cache.geometry.pointer((120, 30), released=True)
        cache.flush()
        assert cache.graph.rect(ids['a']) == (0, 0, 120, 40)
        assert cache.graph.rect(ids['b']) == (120, 0, 80, 40)
        assert cache.position(ids['a']) == (0, 20)
        assert calls == before
    finally:
        cache.close()
