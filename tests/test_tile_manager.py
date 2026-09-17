"""Tile models persist function references; editors use independent view state."""
import inspect
from types import SimpleNamespace

import pytest

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.conversion.load_save_v2 import dumps, loads
from meltygui.core.core_render import render_func, render_func_kwarg_names
from meltygui.core.layout.tile_manager_core import split_tile, join_tiles
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
    tile = Tile(input_value="original")
    changed, result = inspect.unwrap(tile_view.draw_tile_content)(
        tile, SimpleNamespace(width=300, height=200), (editor,))
    assert changed
    assert result is tile
    assert tile.render_func is editor
    assert tile.input_value == "original edited"
    monkeypatch.setattr(tile_view, "draw_dropdown", lambda *a, **kw: (True, None))
    changed, result = inspect.unwrap(tile_view.draw_tile_content)(
        tile, SimpleNamespace(width=300, height=200), (editor,))
    assert changed and result is tile
    assert tile.render_func is None
    assert tile.input_value == "original edited"


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
        seen.append(state)
        return False, input_value

    tiles = [Tile(render_func=tile_state_probe), Tile(render_func=tile_state_probe)]

    @render_func(tint=(0.2, 0.4, 0.6))
    def host(input_value: object):
        x, y = imgui.get_cursor_screen_pos()
        for index, tile in enumerate(tiles):
            imgui.set_cursor_screen_pos((x + index * 350, y))
            draw_tile_content(tile, key=tile.id, width=320, height=220)
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
