"""Signature-based sibling links: identity, ownership, persistence and caching."""
import inspect
from types import SimpleNamespace
import pytest

from meltygui import DrawState, render_func
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.conversion.load_save_v2 import dumps, loads
from meltygui.core.layout.tile_links import (
    prepare_endpoints, resolve_parameters, candidates, set_binding, bindings_for,
)
from meltygui.core.rendering.injected_state import parameter_annotations
from meltygui.core.rendering.parameter_core import view_param_names
from meltygui.model.tile_model import Split, Tile


class SelectionState(DictConversion):
    _owner_ds = None

    def __init__(self):
        super().__init__()
        self.selected = 'initial'


@render_func()
def source_view(input_value, draw_state=None, selection: SelectionState = None):
    return False, input_value


@render_func()
def consumer_view(input_value, draw_state=None,
                  source: DrawState[source_view] = None,
                  selection: SelectionState = None):
    return False, input_value


@pytest.fixture
def layout(monkeypatch):
    from meltygui.core.melty import Melty
    monkeypatch.setattr(Melty, 'depth', 0)
    monkeypatch.setattr(Melty, 'vis', None)
    monkeypatch.setattr(Melty, 'draw_state_registry', {})
    # Consumer deliberately precedes both sources.
    return Split(children=[Tile(name='Editor', render_func=consumer_view),
                           Tile(name='Left', render_func=source_view),
                           Tile(name='Right', render_func=source_view)])


def test_annotation_contract_and_forward_annotations():
    assert repr(DrawState[source_view]).endswith('.source_view]')
    with pytest.raises(TypeError, match='view function'):
        DrawState[int]
    assert 'source' not in consumer_view.__auto_state_params__()
    assert 'source' not in view_param_names(SimpleNamespace(_view_func=consumer_view))
    namespace = dict(DrawState=DrawState, source_view=source_view)
    exec('from __future__ import annotations\n'
         'def probe(input_value: MissingType, source: DrawState[source_view] = None): pass', namespace)
    assert parameter_annotations(namespace['probe'])['source'] == DrawState[source_view]


def test_both_kinds_resolve_before_source_draws_and_keep_local_ownership(layout):
    endpoints = prepare_endpoints(layout)
    consumer, left, right = [endpoints[t.id] for t in layout.children]
    local = resolve_parameters(consumer, endpoints)['selection']
    assert resolve_parameters(consumer, endpoints)['source'] is None
    assert len(list(candidates(consumer, 'source', endpoints))) == 2
    for name in ('source', 'selection'):
        identity, expected = next(candidates(consumer, name, endpoints))
        set_binding(consumer, name, identity)
        assert resolve_parameters(consumer, endpoints)[name] is expected
    assert resolve_parameters(consumer, endpoints)['source'] is left.draw_state
    assert left.states['selection']._owner_ds is left.draw_state
    assert consumer.states['selection'] is local
    assert right.states['selection'] is not local
    # Unlinking restores exactly the original local instance.
    local.selected = 'retained'
    set_binding(consumer, 'selection', None)
    assert resolve_parameters(consumer, endpoints)['selection'] is local
    assert local.selected == 'retained'


def test_bindings_survive_persistence_reorder_missing_source_and_switch(layout):
    endpoints = prepare_endpoints(layout)
    consumer = endpoints[layout.children[0].id]
    identity, original = next(candidates(consumer, 'source', endpoints))
    set_binding(consumer, 'source', identity)
    restored = loads(dumps(layout))
    restored.children.reverse()
    endpoints = prepare_endpoints(restored)
    consumer = endpoints[consumer.tile.id]
    assert resolve_parameters(consumer, endpoints)['source'] is original
    source_tile = next(t for t in restored.children if t.id == identity[0])
    source_tile.render_func = consumer_view
    endpoints = prepare_endpoints(restored)
    assert resolve_parameters(endpoints[consumer.tile.id], endpoints)['source'] is None
    assert bindings_for(endpoints[consumer.tile.id])['source'] == identity
    source_tile.render_func = source_view
    endpoints = prepare_endpoints(restored)
    assert resolve_parameters(endpoints[consumer.tile.id], endpoints)['source'] is original
    restored.children.remove(source_tile)
    endpoints = prepare_endpoints(restored)
    assert resolve_parameters(endpoints[consumer.tile.id], endpoints)['source'] is None


def test_owned_source_does_not_forward_its_borrowed_state(layout):
    endpoints = prepare_endpoints(layout)
    consumer, left, right = [endpoints[t.id] for t in layout.children]
    right_identity = next(identity for identity, _ in candidates(left, 'selection', endpoints)
                          if identity[0] == right.tile.id)
    set_binding(left, 'selection', right_identity)
    left_identity = next(identity for identity, _ in candidates(consumer, 'selection', endpoints)
                         if identity[0] == left.tile.id)
    set_binding(consumer, 'selection', left_identity)
    assert resolve_parameters(left, endpoints)['selection'] is right.states['selection']
    assert resolve_parameters(consumer, endpoints)['selection'] is left.states['selection']


def test_cache_dependencies_invalidate_and_unsubscribe(monkeypatch):
    from meltygui.core.cache.tile_cache import TileCacheMasked
    cache = TileCacheMasked()
    source, replacement, consumer = SelectionState(), DrawState(), DrawState()
    consumer._tile_id = 'consumer'
    seen = []
    monkeypatch.setattr(cache, 'invalidate_up', lambda key, **kw: seen.append(key))
    cache.parameter_dependencies.bind(consumer, {'source': source})
    cache.invalidate_by_obj(source, 'selected', other_windows=False)
    assert seen == ['consumer']
    cache.parameter_dependencies.bind(consumer, {'source': replacement})
    seen.clear()
    cache.invalidate_by_obj(source, 'selected', other_windows=False)
    assert seen == []
    cache.invalidate_by_obj(replacement, 'selected', other_windows=False)
    assert seen == ['consumer']
    cache.parameter_dependencies.bind(consumer, {})
    seen.clear()
    cache.invalidate_by_obj(replacement, 'selected', other_windows=False)
    cache.invalidate_by_obj({}, other_windows=False)  # unrelated unhashable model
    assert seen == []


def test_hotswap_replaces_signature_metadata_without_replacing_wrapper():
    from meltygui.code.file_converters import _transfer_wrapper_state

    @render_func()
    def target(input_value, state: SelectionState = None):
        return False, input_value

    @render_func()
    def replacement(input_value, source: DrawState[source_view] = None):
        return False, input_value

    _transfer_wrapper_state(target, replacement, inspect.unwrap(replacement))
    assert target.__state_parameters__ == {'source': DrawState[source_view]}
    assert 'source' not in target.__auto_state_params__()


def test_core_render_unlinked_and_explicit_injection(gl_context):
    from conftest import begin_frame, end_frame
    from test_render_func_integration import _init_melty, _tick_frame
    runtime = _init_melty()
    seen = []

    @render_func(show_bg=False, use_cache=False)
    def probe(input_value, draw_state=None, source: DrawState[source_view] = None,
              state: SelectionState = None):
        seen.append((draw_state, source, state))
        return False, input_value

    supplied = DrawState()
    for kwargs in ({}, {'source': supplied}, {}):
        _tick_frame(runtime)
        begin_frame()
        try:
            probe(None, name='sibling injection test', **kwargs)
        finally:
            end_frame()
    assert [entry[1] for entry in seen] == [None, supplied, None]
    assert seen[0][2] is seen[1][2] is seen[2][2]
    for own, _, _ in seen:
        assert 'source' not in own.misc
        assert 'source' not in own.auto_params


def test_transitive_draw_state_dependencies_and_cycles():
    from meltygui.core.cache.parameter_dependencies import ParameterDependencies
    dependencies = ParameterDependencies()
    local, source, consumer = SelectionState(), DrawState(), DrawState()
    source._tile_id, consumer._tile_id = 'source', 'consumer'
    dependencies.bind(source, {'local': local, 'consumer': consumer})
    dependencies.bind(consumer, {'source': source})
    seen = []
    dependencies.invalidate(SimpleNamespace(invalidate_up=lambda key, **kw: seen.append(key)), local)
    assert sorted(seen) == ['consumer', 'source']


@pytest.mark.parametrize('width', [80, 320, 960])
def test_link_status_keeps_toolbar_geometry_stable(layout, monkeypatch, width):
    import meltygui.view.tile_view as tile_view
    endpoints = prepare_endpoints(layout)
    consumer = endpoints[layout.children[0].id]
    calls = []
    monkeypatch.setattr(tile_view, 'draw_dropdown',
                        lambda *a, **kw: (calls.append(kw) or False, None))
    for identity in (None, next(candidates(consumer, 'source', endpoints))[0],
                     ('removed', 'module.long_missing_view_name', None)):
        set_binding(consumer, 'source', identity)
        tile_view.draw_tile_link(consumer, "source", endpoints, width, 28)
    assert {call['width'] for call in calls} == {width}
    assert {call['height'] for call in calls} == {28}
    assert {call['display_label'] for call in calls} == {f'\uf0c1'}
    assert len({call['menu_title'] for call in calls}) == 1
    assert all(call['trigger_caret'] == ('', '') for call in calls)


def test_cached_consumer_tracks_source_and_rebinding(gl_context, monkeypatch):
    from conftest import begin_frame, end_frame
    from test_render_func_integration import _init_melty, _tick_frame
    from meltygui.core.styling.style_core import ImGuiStyleManager
    from meltygui.core.cache.parameter_dependencies import ParameterDependencies
    from meltygui.view.tile_view import draw_tile_content
    import meltygui_imgui as imgui

    runtime = _init_melty()
    monkeypatch.setattr(runtime, 'style_manager', ImGuiStyleManager())
    monkeypatch.setattr(runtime.cache, 'enabled', True)
    monkeypatch.setattr(runtime.cache, 'parameter_dependencies', ParameterDependencies())
    stale, runs, states = set(), [], {}

    @render_func(show_bg=False)
    def source(input_value, draw_state=None, selection: SelectionState = None):
        return False, input_value

    @render_func(show_bg=False)
    def consumer(input_value, draw_state=None, source_ds: DrawState[source] = None):
        runs.append(None if source_ds is None else source_ds.misc['selection'].selected)
        return False, input_value

    tree = Split(children=[Tile(render_func=consumer), Tile(render_func=source)])
    all_endpoints = {}
    initial = [True]

    def gate(draw_state):
        ds = draw_state
        if ds._view_func not in (inspect.unwrap(source), inspect.unwrap(consumer)):
            return True
        first = ds._tile_id not in states
        states[ds._tile_id] = ds
        return first or ds._tile_id in stale

    def invalidate(key, **kwargs):
        stale.add(key)

    monkeypatch.setattr(runtime.cache, 'mark_start_offscreen', gate)
    monkeypatch.setattr(runtime.cache, 'mark_end_offscreen', lambda *a, **kw: None)
    monkeypatch.setattr(runtime.cache, 'invalidate_up', invalidate)

    @render_func(show_bg=False, use_cache=False)
    def host(input_value):
        all_endpoints.clear()
        all_endpoints.update(prepare_endpoints(tree))
        target = all_endpoints[tree.children[0].id]
        if initial[0]:
            set_binding(target, 'source_ds', next(candidates(target, 'source_ds', all_endpoints))[0])
            initial[0] = False
        x, y = imgui.get_cursor_screen_pos()
        for index, tile in enumerate(tree.children):
            imgui.set_cursor_screen_pos((x + index * 350, y))
            draw_tile_content(tile, 320, 220, endpoints=all_endpoints, use_cache=True)
        return False, input_value

    def frame():
        _tick_frame(runtime)
        begin_frame()
        imgui.begin('Sibling cache test')
        try:
            host(None, width=750, height=450)
        finally:
            imgui.end()
            end_frame()
        stale.clear()

    frame()
    frame()
    assert runs == ['initial']
    owned = all_endpoints[tree.children[1].id].states['selection']
    owned.selected = 'updated'
    # Simulate the normal @live signal explicitly: test frame_count is still
    # in the framework's startup invalidation grace period.
    runtime.cache.invalidate_by_obj(owned, 'selected', other_windows=False)
    frame()
    assert runs == ['initial', 'updated']
    target = all_endpoints[tree.children[0].id]
    set_binding(target, 'source_ds', None)
    frame()
    assert runs[-1] is None
    count = len(runs)
    runtime.cache.invalidate_by_obj(owned, 'selected', other_windows=False)
    frame()
    assert len(runs) == count


def test_swapping_parameters_between_same_sources_invalidates_binding():
    from meltygui.core.cache.parameter_dependencies import ParameterDependencies
    dependencies = ParameterDependencies()
    first, second, consumer = DrawState(), DrawState(), DrawState()
    assert dependencies.bind(consumer, {'left': first, 'right': second})
    assert not dependencies.bind(consumer, {'left': first, 'right': second})
    assert dependencies.bind(consumer, {'left': second, 'right': first})
    assert dependencies.subscribers(first) == (consumer,)
    assert dependencies.subscribers(second) == (consumer,)


def test_changed_picker_input_invalidates_even_with_rebuilt_collection(gl_context, monkeypatch):
    from conftest import begin_frame, end_frame
    from test_render_func_integration import _init_melty, _tick_frame
    runtime = _init_melty()
    seen, invalidated = [], []

    @render_func(show_bg=False, use_cache=False)
    def picker_probe(input_value: str, draw_state=None, collection=None):
        seen.append(draw_state)
        return False, input_value

    monkeypatch.setattr(runtime.cache, 'invalidate', lambda key, **kw: invalidated.append(key))
    for value in ('Linked', 'Linked', 'Linked', 'Linked', 'Source unavailable'):
        _tick_frame(runtime)
        begin_frame()
        try:
            if value == 'Source unavailable':
                invalidated.clear()
            picker_probe(value, name='rebuilt collection probe', collection={'label': value})
        finally:
            end_frame()
    assert seen[-1]._tile_id in invalidated


def test_auto_uses_tree_edges_retains_ties_and_ignores_size(layout):
    from meltygui.core.layout.tile_links import AUTO, tree_distance
    consumer, near, far = layout.children
    near.id, far.id = 'z-near', 'a-far'
    group = Split('y', [consumer, near], edges=[{'y': 500.0}])
    layout.children = [far, group]
    endpoints = prepare_endpoints(layout)
    target = endpoints[consumer.id]
    assert tree_distance(target.path, endpoints[near.id].path) == 2
    assert tree_distance(target.path, endpoints[far.id].path) == 3
    set_binding(target, 'source', AUTO)
    set_binding(target, 'selection', AUTO)
    assert resolve_parameters(target, endpoints)['source'] is endpoints[near.id].draw_state
    assert resolve_parameters(target, endpoints)['selection'] is endpoints[near.id].states['selection']
    group.edges[0]['y'] = 10_000.0
    assert resolve_parameters(target, endpoints)['source'] is endpoints[near.id].draw_state
    # Equal graph distance keeps the previous source, even when its ID sorts last.
    layout.children = [far, consumer, near]
    endpoints = prepare_endpoints(layout)
    assert resolve_parameters(endpoints[consumer.id], endpoints)['source'] is endpoints[near.id].draw_state
    # Without history, the stable tile ID breaks ties, independent of traversal order.
    consumer._auto_link_sources = {}
    assert resolve_parameters(endpoints[consumer.id], endpoints)['source'] is endpoints[far.id].draw_state
    layout.children.reverse()
    consumer._auto_link_sources = {}
    endpoints = prepare_endpoints(layout)
    assert resolve_parameters(endpoints[consumer.id], endpoints)['source'] is endpoints[far.id].draw_state


def test_auto_fallback_return_and_manual_source_is_pinned(layout):
    from meltygui.core.layout.tile_links import AUTO
    consumer, first, second = layout.children
    endpoints = prepare_endpoints(layout)
    target = endpoints[consumer.id]
    for name in ('source', 'selection'):
        set_binding(target, name, AUTO)
    layout.children = [consumer]
    endpoints = prepare_endpoints(layout)
    target = endpoints[consumer.id]
    assert resolve_parameters(target, endpoints) == {'source': None, 'selection': target.states['selection']}
    assert bindings_for(target)['source'] == AUTO
    layout.children = [consumer, first]
    endpoints = prepare_endpoints(layout)
    target = endpoints[consumer.id]
    assert resolve_parameters(target, endpoints)['source'] is endpoints[first.id].draw_state
    identity = next(candidates(target, 'source', endpoints))[0]
    set_binding(target, 'source', identity)
    layout.children = [first, Split('y', [consumer, second])]
    endpoints = prepare_endpoints(layout)
    assert resolve_parameters(endpoints[consumer.id], endpoints)['source'] is endpoints[first.id].draw_state


@pytest.mark.parametrize('axis,before,supplied', [('x', False, False), ('y', True, False), ('y', False, True)])
def test_split_copies_all_link_settings_without_aliasing(layout, axis, before, supplied):
    from meltygui.core.layout.tile_links import AUTO
    from meltygui.core.layout.tile_manager_core import split_tile, node_at
    original = layout.children[0]
    endpoints = prepare_endpoints(layout)
    target = endpoints[original.id]
    identity = next(candidates(target, 'source', endpoints))[0]
    set_binding(target, 'source', identity)
    set_binding(target, 'selection', AUTO)
    original.links['another.view'] = {'unavailable': ['missing', 'other.source', None]}
    resolve_parameters(target, endpoints)
    frame = ({'x': 0.0}, {'x': 900.0}, {'y': 0.0}, {'y': 600.0})
    layout.edges = [{'x': 300.0}, {'x': 600.0}]
    new_path = split_tile(layout, (0,), axis, frame, before=before,
                          new_tile=Tile(render_func=consumer_view) if supplied else None)
    duplicate = node_at(layout, new_path)
    assert duplicate.id != original.id
    assert duplicate.links == original.links
    assert duplicate.links is not original.links
    assert duplicate._auto_link_sources == original._auto_link_sources
    assert duplicate._auto_link_sources is not original._auto_link_sources
    duplicate.links['another.view']['unavailable'][0] = 'changed'
    assert original.links['another.view']['unavailable'][0] == 'missing'
    endpoints = prepare_endpoints(layout)
    assert resolve_parameters(endpoints[duplicate.id], endpoints)['source'] is endpoints[identity[0]].draw_state
    set_binding(endpoints[duplicate.id], 'source', None)
    assert bindings_for(endpoints[original.id])['source'] == identity
    restored = loads(dumps(layout))
    restored_duplicate = next(endpoint.tile for endpoint in prepare_endpoints(restored).values()
                              if endpoint.tile.id == duplicate.id)
    assert restored_duplicate.links == duplicate.links


def test_each_parameter_picker_lists_only_eligible_sources_and_edits_itself(layout, monkeypatch):
    import meltygui.view.tile_view as tile_view
    from meltygui.core.layout.tile_links import AUTO
    endpoints = prepare_endpoints(layout)
    target = endpoints[layout.children[0].id]
    calls = []
    def dropdown(value, **kwargs):
        calls.append(kwargs)
        return True, AUTO
    monkeypatch.setattr(tile_view, 'draw_dropdown', dropdown)
    for name in target.parameters:
        tile_view.draw_tile_link(target, name, endpoints, 400, 28)
        assert bindings_for(target)[name] == AUTO
        entries = list(calls[-1]['collection'].values())
        assert AUTO in entries and None in entries
        assert {item for item in entries if isinstance(item, tuple)} == {
            identity for identity, _ in candidates(target, name, endpoints)}
    assert len({call['key'] for call in calls}) == 2
    assert calls[0]['menu_title'] == 'source: DrawState[source_view]'
    assert calls[1]['menu_title'] == 'selection: SelectionState'


@pytest.mark.parametrize('width,height,count', [(960, 640, 3), (320, 640, 2), (80, 28, 3), (320, 60, 5), (28, 28, 12)])
def test_parameter_picker_layout_fits_and_does_not_overlap(width, height, count):
    from meltygui.view.tile_view import tile_control_layout
    content_height, editor_width, slots = tile_control_layout(width, height, count)
    assert len(slots) == count
    rectangles = [(0, 0, editor_width, 28)] + [(x, y, x + w, y + 28) for x, y, w in slots]
    for index, (left, top, right, bottom) in enumerate(rectangles):
        assert 0 <= left <= right <= width + 0.001
        assert 0 <= content_height + top < content_height + bottom <= height
        for other_left, other_top, other_right, other_bottom in rectangles[index + 1:]:
            assert right <= other_left or other_right <= left or bottom <= other_top or other_bottom <= top


def test_auto_keeps_manual_source_when_it_is_tied_for_closest(layout):
    from meltygui.core.layout.tile_links import AUTO
    endpoints = prepare_endpoints(layout)
    target = endpoints[layout.children[0].id]
    identity, value = list(candidates(target, 'source', endpoints))[-1]
    set_binding(target, 'source', identity)
    set_binding(target, 'source', AUTO)
    assert resolve_parameters(target, endpoints)['source'] is value


def test_link_buttons_keep_a_single_compact_row_and_leave_toolbar_space():
    from meltygui.view.tile_view import tile_control_layout
    content, editor, slots = tile_control_layout(960, 600, 2)
    assert content == 572
    assert editor == 180
    assert slots == [(184, 0, 28), (216, 0, 28)]
    assert all(y == 0 for _, y, _ in slots)


def test_dropdown_title_is_measured_as_a_nonselectable_header(monkeypatch):
    import meltygui.view.dropdown_view as dropdown
    from meltygui.core.runtime.toggles import Toggles
    io = SimpleNamespace(display_size=(800, 600))
    monkeypatch.setattr(dropdown.imgui, 'get_io', lambda: io)
    monkeypatch.setattr(dropdown.imgui, 'calc_text_size', lambda text: (len(text) * 8, 18))
    rows = {'Auto': 'auto', 'Local state': None}
    plain = dropdown._dd_popup_geometry(rows, '', 400, 28, True, 260)
    titled = dropdown._dd_popup_geometry(rows, '', 400, 28, True, 260, 'Counter state')
    assert titled[0] == plain[0]
    assert titled[1] == plain[1] + Toggles.Dropdown.row_height
    assert titled[2] + titled[1] == plain[2] + plain[1] == 400


def test_auto_preview_does_not_change_links_or_tie_memory(layout):
    endpoints = prepare_endpoints(layout)
    consumer, first, second = endpoints.values()
    identity = next(identity for identity, _ in candidates(consumer, 'source', endpoints)
                    if identity[0] == second.tile.id)
    set_binding(consumer, 'source', identity)
    before_links = dict(bindings_for(consumer))
    before_memory = dict(consumer.tile._auto_link_sources)
    from meltygui.core.layout.tile_links import selected_candidate
    assert selected_candidate(consumer, 'source', endpoints, preview_auto=True)[0] == identity
    assert bindings_for(consumer) == before_links
    assert consumer.tile._auto_link_sources == before_memory


def test_hover_preview_tracks_geometry_and_stops_when_not_hovered(monkeypatch):
    from meltygui.core.melty import Melty
    from meltygui.view import dropdown_view
    owner = object()
    menu = SimpleNamespace(closed=False, abs_top=10, height=100)
    target = SimpleNamespace(closed=False, abs_left=200, abs_top=30,
                             width=100, height=80, abs_clip_rect=(200, 30, 300, 110))
    monkeypatch.setattr(Melty, 'emphasis_notes', {})
    monkeypatch.setattr(Melty, 'popover_focused_ds', owner)
    mouse = [25, 25]
    monkeypatch.setattr(dropdown_view.imgui, 'get_mouse_pos', lambda: mouse)
    from unittest.mock import Mock
    overlay = Mock()
    monkeypatch.setattr(dropdown_view.imgui, 'get_overlay_draw_list', lambda: overlay)
    monkeypatch.setattr(Melty, '_overlay_channels_active', False)
    def draw():
        overlay.reset_mock()
        dropdown_view._preview_source(target, menu, owner, (10, 20, 110, 40))
    draw()
    assert overlay.add_rect.call_args.args[:4] == (200, 30, 300, 110)
    assert overlay.add_rect.call_args.kwargs['thickness'] == 1.0
    assert not Melty.emphasis_notes
    target.abs_left = 250
    draw()
    assert overlay.add_rect.call_args.args[:4] == (250, 30, 350, 110)
    mouse[1] = 45
    draw()
    overlay.add_rect.assert_not_called()
    mouse[1] = 25
    menu.closed = True
    draw()
    overlay.add_rect.assert_not_called()
    menu.closed = False
    monkeypatch.setattr(Melty, 'popover_focused_ds', None)
    draw()
    overlay.add_rect.assert_not_called()
