"""Tile models persist function references; editors use independent view state."""
import inspect
from types import SimpleNamespace

import pytest

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.conversion.load_save_v2 import dumps, loads
from meltygui.core.core_render import render_func, render_func_kwarg_names
from meltygui.core.layout.tile_manager_core import split_tile, join_tiles
from meltygui.core.layout.column_core import MIN_COLUMN_WIDTH
from meltygui.model.tile_model import Split, Tile


@render_func(multi_instance=True, tint=(0.2, 0.4, 0.6))
def tile_test_renderer(input_value: object):
    return False, input_value


def test_layout_round_trip_keeps_types_function_references_and_shared_input():
    shared = {"text": "shared model"}
    tree = Split(children=[Tile(render_func=tile_test_renderer, input_value=shared),
                           Tile(render_func=tile_test_renderer, input_value=shared)])
    restored = loads(dumps(tree))
    assert isinstance(restored, Split)
    first, second = restored.children
    assert isinstance(first, Tile)
    assert first is not second
    assert first.id == tree.children[0].id
    assert first.render_func is second.render_func is tile_test_renderer
    assert first.input_value is second.input_value
    assert first.input_value == shared


def test_split_inherits_renderer_and_model_and_join_preserves_survivor():
    tile = Tile(render_func=tile_test_renderer, input_value={"a": 1})
    tree = Split(children=[tile])
    frame = ({"x": 0.0}, {"x": 600.0}, {"y": 0.0}, {"y": 400.0})
    path = split_tile(tree, (0,), "y", frame)
    original, duplicate = tree.children[0].children
    assert original is tile
    assert duplicate is not tile
    assert duplicate.render_func is tile.render_func
    assert duplicate.input_value is tile.input_value
    assert duplicate.id != tile.id
    assert join_tiles(tree, path, -1) == (0,)
    assert tree.children == [tile]


def test_multi_instance_is_metadata_and_updates_in_place_on_hotswap(monkeypatch):
    from meltygui.code.file_converters import _transfer_wrapper_state
    from meltygui.core.melty import Melty

    monkeypatch.setattr(Melty, "render_funcs_by_name", {})

    @render_func(multi_instance=True)
    def editor(input_value: object):
        return False, input_value

    selected = Tile(render_func=editor)
    replacement = render_func(inspect.unwrap(editor), multi_instance=False)
    _transfer_wrapper_state(editor, replacement, inspect.unwrap(replacement))
    assert selected.render_func is editor
    assert not selected.render_func.multi_instance
    assert "multi_instance" not in editor.__header_defaults__
    assert "multi_instance" not in render_func_kwarg_names()


def test_selection_stores_callable_and_propagates_editor_changes(monkeypatch):
    import meltygui.view.tile_view as tile_view

    def editor(input_value, **kwargs):
        return True, input_value + " edited"

    monkeypatch.setattr(tile_view, "draw_dropdown", lambda *a, **kw: (True, editor))
    monkeypatch.setattr(tile_view.imgui, "get_cursor_screen_pos", lambda: (10, 20))
    monkeypatch.setattr(tile_view.imgui, "set_cursor_screen_pos", lambda pos: None)
    from meltygui.core.melty import Melty
    clips = []
    monkeypatch.setattr(Melty, "push_clip", lambda rect: clips.append(rect))
    monkeypatch.setattr(Melty, "pop_clip", lambda: clips.append(None))
    tile = Tile(input_value="original")
    changed, result = inspect.unwrap(tile_view.draw_tile_content)(
        tile, 300, 200, (editor,))
    assert changed
    assert result is tile
    assert tile.render_func is editor
    assert tile.input_value == "original edited"
    # The editor drew inside a clip of its tile (above the 28 px picker).
    assert clips == [(10, 20, 310, 192), None]
    monkeypatch.setattr(tile_view, "draw_dropdown", lambda *a, **kw: (True, None))
    changed, result = inspect.unwrap(tile_view.draw_tile_content)(
        tile, 300, 200, (editor,))
    assert changed and result is tile
    assert tile.render_func is None
    assert tile.input_value == "original edited"


@pytest.mark.parametrize("height", [28, 200])
def test_toolbar_renderer_receives_bottom_row_beside_picker(monkeypatch, height):
    import meltygui.view.tile_view as tile_view
    from meltygui.core.melty import Melty
    calls, clips = [], []

    def editor(input_value, **kwargs):
        calls.append(kwargs)
        return False, input_value

    editor.__header_defaults__ = {"tile_toolbar": True}
    monkeypatch.setattr(tile_view, "draw_dropdown", lambda *a, **kw: (False, editor))
    monkeypatch.setattr(tile_view.imgui, "get_cursor_screen_pos", lambda: (10, 20))
    monkeypatch.setattr(tile_view.imgui, "set_cursor_screen_pos", lambda pos: None)
    monkeypatch.setattr(Melty, "push_clip", lambda rect: clips.append(rect))
    monkeypatch.setattr(Melty, "pop_clip", lambda: None)
    tile_view.draw_tile_content(Tile(render_func=editor), 600, height)
    assert calls[0]["tile_toolbar_rect"] == (184, height - 28, 416, 28)
    assert calls[0]["height"] == height
    assert clips == [(10, 20, 610, 20 + height)]


class ProbeState(DictConversion):
    def __init__(self):
        super().__init__()
        self.count = 0


def test_tile_instances_keep_state_across_switching_and_reordering(gl_context, monkeypatch):
    from conftest import begin_frame, end_frame
    from test_render_func_integration import _init_melty, _tick_frame
    from meltygui.view.tile_view import draw_tile_content
    import meltygui_imgui as imgui
    from meltygui.core.styling.style_core import ImGuiStyleManager

    runtime = _init_melty()
    monkeypatch.setattr(runtime, "style_manager", ImGuiStyleManager())
    seen = []

    @render_func(multi_instance=True, tint=(0.2, 0.4, 0.6))
    def tile_state_probe(input_value: object, draw_state, state: ProbeState = None):
        cursor_x, cursor_y = imgui.get_cursor_screen_pos()
        assert draw_state.abs_left == pytest.approx(cursor_x, abs=1.0)
        # The wrapper adds its normal vertical content padding inside this box.
        assert draw_state.abs_top <= cursor_y <= draw_state.abs_top + 8
        assert inspect.unwrap(draw_state._parent._view_func) is inspect.unwrap(host)
        seen.append(state)
        return False, input_value

    tiles = [Tile(render_func=tile_state_probe), Tile(render_func=tile_state_probe)]

    @render_func(tint=(0.2, 0.4, 0.6))
    def host(input_value: object):
        x, y = imgui.get_cursor_screen_pos()
        for index, tile in enumerate(tiles):
            imgui.set_cursor_screen_pos((x + index * 350, y))
            draw_tile_content(tile, width=320, height=220)
        return False, input_value

    def frame():
        _tick_frame(runtime)
        begin_frame()
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(800, 600)
        imgui.begin("Tile instance test")
        try:
            host(None, width=750, height=500)
        finally:
            imgui.end()
            end_frame()

    frame()
    first, second = seen[-2:]
    assert first is not second
    first.count = 17
    tiles.reverse()
    frame()
    assert seen[-2:] == [second, first]
    tiles[1].render_func = None
    frame()
    tiles[1].render_func = tile_state_probe
    frame()
    assert seen[-1] is first
    assert first.count == 17
    assert second.count == 0


def test_renderer_choices_follow_registration_and_explicit_override(gl_context, monkeypatch):
    from conftest import begin_frame, end_frame
    from test_render_func_integration import _init_melty, _tick_frame
    from meltygui.core.rendering.parameter_core import view_param_names

    runtime = _init_melty()
    monkeypatch.setattr(runtime, "render_funcs_by_name", {})
    seen = []

    @render_func(tint=(0.2, 0.4, 0.6))
    def choices_probe(input_value: object, multi_instance_renderers=(), draw_state=None):
        seen.append(multi_instance_renderers)
        assert "multi_instance_renderers" not in draw_state.auto_params
        return False, input_value

    def frame(**kwargs):
        _tick_frame(runtime)
        begin_frame()
        try:
            choices_probe(None, **kwargs)
        finally:
            end_frame()

    frame()

    @render_func(multi_instance=True)
    def registered_later(input_value: object):
        return False, input_value

    frame()
    registered_later.multi_instance = False
    frame()
    frame(multi_instance_renderers=(registered_later,))
    assert seen == [(), (registered_later,), (), (registered_later,)]
    assert "multi_instance_renderers" not in view_param_names(
        SimpleNamespace(_view_func=choices_probe))


def test_cached_tiles_only_rerun_the_stale_tile(gl_context, monkeypatch):
    """Each tile is its own blit-cache unit, gated at the wrapper's real
    mark_start_offscreen boundary (no offscreen GL capture in tests)."""
    from conftest import begin_frame, end_frame
    from test_render_func_integration import _init_melty, _tick_frame
    from meltygui.view.tile_view import draw_tile_content
    import meltygui_imgui as imgui
    from meltygui.core.styling.style_core import ImGuiStyleManager

    runtime = _init_melty()
    monkeypatch.setattr(runtime, "style_manager", ImGuiStyleManager())
    monkeypatch.setattr(runtime.cache, "enabled", True)
    runs = {"left": 0, "right": 0}
    stale = {"left", "right"}
    marks = []
    cached = [True]

    def cache_gate(draw_state):
        # Dropdown pickers and the host pass through; only the tiles gate.
        value = draw_state._input_value
        if value not in runs or not draw_state.use_cache:
            return True
        marks.append(value)
        return value in stale

    monkeypatch.setattr(runtime.cache, "mark_start_offscreen", cache_gate)
    monkeypatch.setattr(runtime.cache, "mark_end_offscreen", lambda *a, **kw: None)

    @render_func(multi_instance=True, tint=(0.2, 0.4, 0.6))
    def cached_tile_probe(input_value: object):
        runs[input_value] += 1
        return False, input_value

    tiles = [Tile(render_func=cached_tile_probe, input_value="left"),
             Tile(render_func=cached_tile_probe, input_value="right")]

    @render_func(tint=(0.2, 0.4, 0.6))
    def host(input_value: object):
        x, y = imgui.get_cursor_screen_pos()
        for index, tile in enumerate(tiles):
            imgui.set_cursor_screen_pos((x + index * 350, y))
            draw_tile_content(tile, width=320, height=220, use_cache=cached[0])
        return False, input_value

    def frame():
        _tick_frame(runtime)
        begin_frame()
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(800, 600)
        imgui.begin("Cached tile test")
        try:
            host(None, width=750, height=500)
        finally:
            imgui.end()
            end_frame()

    frame()
    assert runs == {"left": 1, "right": 1}
    stale.clear()
    frame()
    assert runs == {"left": 1, "right": 1}
    stale.add("left")
    frame()
    assert runs == {"left": 2, "right": 1}
    assert marks[-2:] == ["left", "right"]
    # Without use_cache the tiles never reach the cache boundary.
    marked = len(marks)
    cached[0] = False
    frame()
    assert runs == {"left": 3, "right": 2}
    assert len(marks) == marked


def test_multi_instance_registers_an_existing_render_function():
    from meltygui.model.tile_model import multi_instance

    @render_func(tint=(0.2, 0.4, 0.6), display_name="Library view")
    def library_view(input_value: object):
        return False, input_value

    assert not library_view.multi_instance
    assert multi_instance(library_view) is library_view
    assert library_view.multi_instance
    # A hotswap re-decorates the library's function: the registration holds.
    assert render_func(inspect.unwrap(library_view)).multi_instance
    assert not multi_instance(library_view, enabled=False).multi_instance
    assert not render_func(inspect.unwrap(library_view)).multi_instance
    with pytest.raises(TypeError):
        multi_instance(lambda value: (False, value))


def test_handle_hover_is_muted_while_another_button_drags(monkeypatch):
    """A right-drag (or any held button) sweeping over a divider or corner
    grip must not light it up; only the handle that owns the live gesture
    stays lit while a button is held."""
    import meltygui.core.layout.tile_manager_core as core

    hovered = SimpleNamespace(hover_eligible=lambda rect=None: True)
    monkeypatch.setattr(core, "_any_button_down", lambda: False)
    assert core.hover_shown(hovered, (0, 0, 10, 10), owns_gesture=False)
    monkeypatch.setattr(core, "_any_button_down", lambda: True)
    assert not core.hover_shown(hovered, (0, 0, 10, 10), owns_gesture=False)
    assert core.hover_shown(hovered, (0, 0, 10, 10), owns_gesture=True)


@pytest.mark.parametrize("handle", ["col_edge_()_1", "row_edge_(0,)_1", "tile_corner_123_tl"])
def test_handle_highlight_survives_press_before_drag(monkeypatch, handle):
    import meltygui.core.layout.tile_manager_core as core
    from meltygui.core.input.input_handler import InputHandler
    from meltygui.core.melty import Melty
    from meltygui.core.rendering.core_decoration import Core
    from meltygui.state.new_core_model import DrawState

    handler = InputHandler()
    monkeypatch.setattr(Melty, "get_latest_mouse", lambda: (5, 5))
    monkeypatch.setattr(Core.melty, "event_handler", handler)
    monkeypatch.setattr(core, "_any_button_down", lambda: handler.is_down("left_mouse"))
    state = SimpleNamespace(_tile_id="tiles", hover_eligible=lambda rect=None: True)
    state.is_drag_captured = lambda **kwargs: DrawState.is_drag_captured(state, **kwargs)
    rect = (0, 0, 10, 10)
    handler.register_hovered(f"tiles_{handle}", ["left_mouse_drag"])
    handler.feed_down("left_mouse", x=5, y=5)
    handler.process_frame()
    assert not handler._drag_activated["left_mouse"]
    assert core.hover_shown(state, rect, False, view_id=handle)
    assert not core.hover_shown(state, rect, False, view_id="another_handle")
    # Stationary held frames must remain lit too, without a new DOWN event.
    handler.process_frame()
    assert core.hover_shown(state, rect, False, view_id=handle)
    monkeypatch.setattr(Melty, "get_latest_mouse", lambda: (25, 5))
    handler.feed_move(25, 5)
    handler.process_frame()
    assert handler._drag_activated["left_mouse"]
    assert core.hover_shown(state, rect, False, view_id=handle)
    assert not core.hover_shown(state, rect, False, view_id="another_handle")
    handler.feed_up("left_mouse", x=5, y=5)
    handler.process_frame()
    assert not state.is_drag_captured(view_id=handle)
    assert core.hover_shown(state, rect, False, view_id=handle)


def test_corner_triangle_sits_in_its_corner_with_hypotenuse_inward():
    from meltygui.core.layout.tile_manager_core import corner_rect, corner_triangle

    rect = (100.0, 200.0, 400.0, 500.0)
    for on_left, on_top in ((True, True), (False, True), (True, False), (False, False)):
        grip = corner_rect(rect, on_left, on_top, 14.0)
        apex, along_x, along_y = corner_triangle(grip, on_left, on_top, 9.0, 1.0)
        # The right angle is 1 px inside the tile's own corner ...
        assert apex[0] == (rect[0] + 1.0 if on_left else rect[2] - 1.0)
        assert apex[1] == (rect[1] + 1.0 if on_top else rect[3] - 1.0)
        # ... and both legs run inward, into the tile.
        assert (along_x[0] > apex[0]) == on_left
        assert (along_y[1] > apex[1]) == on_top
        assert abs(along_x[0] - apex[0]) == abs(along_y[1] - apex[1]) == 9.0


def test_corner_split_edge_waits_for_the_hand_after_a_clamped_start():
    """A corner split started within a cell's minimum of its far edge lands
    the new edge at the floor, not under the pointer. The edge then waits
    for the hand to reach it and follows from there - never at the original
    offset (it pushed neighbours while the hand was nowhere near them) and
    never by a jump (an edge moves at the hand's speed or not at all)."""
    import meltygui.core.layout.tile_manager_core as core
    from meltygui.core.layout.column_core import _pending, _ensure_window_state

    events = {}

    class Host:
        closable, parent_window = True, None
        abs_left, abs_top = 100.0, 50.0

        def on_action(self, name, view_id=None, **_kw):
            if isinstance(name, (tuple, list)):
                return {event: events[(event, view_id)] for event in name
                        if (event, view_id) in events}
            return events.get((name, view_id))

        def invalidate(self, note=None):
            pass

    host = Host()
    _ensure_window_state(host)
    tile = Tile(name="a")
    tree = Split("x", [tile], [])
    frame = ({"x": 0.0}, {"x": 400.0}, {"y": 0.0}, {"y": 400.0})
    state = core.TileManagerState()
    corner = core.corner_view_id(tile, "ne")
    grip = (0, 0, 0, 0)

    def drag_to(window_x):
        events[("left_mouse_drag", corner)] = SimpleNamespace(
            x=host.abs_left + window_x, y=host.abs_top + 10.0,
            total_dx=window_x - 400.0, total_dy=0.0)
        return core.tile_corner_gesture(tile, frame, host, (0,), tree, frame, state,
                                        "ne", False, True, grip)

    # Pressed on the ne corner (window x 400), pulled 10 px in: too close to
    # the right edge for a 60 px column, so the edge is placed at 340.
    assert drag_to(390.0)
    edge = state.gesture["edge"]
    assert edge["x"] == 400.0 - MIN_COLUMN_WIDTH
    # The hand leads by 50 px: closing 10 of them moves nothing ...
    assert not drag_to(380.0)
    assert _pending(host, "x") == []
    # ... widening the lead moves nothing either (no push from a distance) ...
    assert not drag_to(395.0)
    assert _pending(host, "x") == []
    # ... and once the hand passes the edge, the edge follows the overshoot.
    assert not drag_to(330.0)
    assert _pending(host, "x") == [(edge, 330.0, True)]
    edge["x"] = 330.0                      # what the window's solve does next
    _pending(host, "x").clear()
    assert not drag_to(320.0)
    assert _pending(host, "x") == [(edge, 320.0, True)]


def test_join_retires_layouts_built_over_the_removed_tiles_edges():
    """A tile renderer's own columns / rows adopt the tile's frame edges
    (``layout_frame``). When the tile is joined away its view never renders
    or closes again, so the registration must go with the divider it
    spanned, or the dead edge keeps colliding with the survivors."""
    from meltygui.core.layout import column_core as C
    from meltygui.core.layout.tile_manager_core import retire_layouts, tree_edge_ids

    class Window:
        id = "win"
    window = Window()
    C._ensure_window_state(window)
    host = object()
    left, right, top, bottom = {"x": 0.0}, {"x": 600.0}, {"y": 0.0}, {"y": 400.0}
    divider = {"x": 300.0}
    tree = Split("x", [Tile(name="a"), Tile(name="b")], [divider])
    before = tree_edge_ids(tree)
    # The host's keyed root layout, a renderer's columns inside tile b (its
    # left edge is the divider), a renderer's rows inside tile b (its band
    # is the tile's x pair) and an unrelated layout elsewhere in the window.
    C._views(window, "x")[("row", "host", ())] = (host, [left, divider, right])
    C._views(window, "x")[("row", "b-columns")] = ("b", [divider, {"x": 450.0}, right])
    C._views(window, "y")[("rows", "b-rows")] = ("b", [top, {"y": 200.0}, bottom])
    C._bands(window, "y")[("rows", "b-rows")] = (divider, right)
    C._views(window, "x")[("row", "other")] = ("other", [{"x": 10.0}, {"x": 90.0}])
    for axis in ("x", "y"):
        for key in C._views(window, axis):
            C._specs(window, axis)[key] = ([None], [None])
    join_tiles(tree, (1,), -1)
    assert tree_edge_ids(tree) == set()
    retire_layouts(window, host, dead=before - tree_edge_ids(tree))
    assert set(C._views(window, "x")) == {("row", "other")}
    assert set(C._specs(window, "x")) == {("row", "other")}
    assert C._views(window, "y") == {} and C._bands(window, "y") == {}
