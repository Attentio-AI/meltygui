"""Real ImGui layout checks for the opt-in native prototype (no GPU required)."""
import pytest
import meltygui_imgui as imgui

pytest.importorskip('meltygui.core.rendering._gui_native')

from conftest import begin_frame, end_frame
from test_render_func_integration import _init_melty, _tick_frame
from functools import partial
from native_gui_support import native_frame
from meltygui.core.rendering.gui_prototype import _new_native, _bind_native


def test_matching_collection_bodies_and_registry_isolation(monkeypatch):
    from meltygui.examples.rust_gui_demo import make_views, python_gui, sample_data
    from meltygui.core.styling.style_core import ImGuiStyleManager
    from meltygui.state.new_core_model import DrawState
    from meltygui.view.collection_view import draw_collection
    from meltygui.view.control_view import draw_int, draw_float

    melty = _init_melty()
    monkeypatch.setattr(melty, 'render_funcs_by_name', dict(melty.render_funcs_by_name))
    style = ImGuiStyleManager()
    monkeypatch.setattr(melty, 'style_manager', style)
    monkeypatch.setitem(melty.global_attrs, 'style_manager', style)
    before = {name: melty.render_funcs_by_name.get(name)
              for name in ('draw_collection', 'draw_int', 'draw_float')}
    runtime = _new_native()
    native_count, python_count = [0], [0]
    native = make_views(partial(_bind_native, runtime), native_count)
    python = make_views(python_gui, python_count)
    assert all(melty.render_funcs_by_name[name] is renderer for name, renderer in before.items())
    owner = DrawState()
    owner.width, owner.height = 800, 600
    owner.left_offset = owner.top_offset = 0
    owner.window_pos = (0, 0)
    owner.melty_window = True
    data = sample_data(3)
    for _ in range(4):
        for views, counter in ((native, native_count), (python, python_count)):
            _tick_frame(melty)
            melty.channels_split = False
            melty.melty_window_stack = [owner]
            counter[0] = 0
            begin_frame()
            imgui.set_next_window_size(800, 600)
            imgui.begin('gui parity')
            with native_frame(runtime,owner, width=320, mouse_pos=(-100, -100)):
                result = views['draw_collection'](data, width=320, name='Values', return_extras=True)
                assert result[0] is False and result[1] is data
                assert result[2].height > 100
            assert counter[0] == 14
            if melty.channels_split:
                imgui.get_window_draw_list().channels_merge()
            imgui.end()
            end_frame()
    runtime.clear()


def test_native_exception_balances_imgui_group_and_id():
    runtime = _new_native()

    @partial(_bind_native, runtime)()
    def broken(input_value: object):
        raise ValueError('deliberate')

    @partial(_bind_native, runtime)()
    def following(input_value: object):
        imgui.dummy(50, 20)
        return False, input_value

    begin_frame()
    imgui.begin('gui exception cleanup')
    with native_frame(runtime):
        with pytest.raises(ValueError, match='deliberate'):
            broken(None)
        assert following(3) == (False, 3)
    imgui.end()
    end_frame()  # ImGui asserts here if the failed native wrapper leaked scopes.


def test_tile_manager_forwards_explicit_benchmark_cache_policy(monkeypatch):
    from meltygui.core.layout import tile_manager_core as tiles
    from meltygui.core.layout.tile_manager_core import TileManagerState
    from meltygui.core.styling.style_core import ImGuiStyleManager
    from meltygui.core.core_render import render_func
    from meltygui.model.tile_model import Split, Tile
    import meltygui.view.tile_view as tile_view

    melty = _init_melty()
    style = ImGuiStyleManager()
    monkeypatch.setattr(melty, 'style_manager', style)
    monkeypatch.setitem(melty.global_attrs, 'style_manager', style)
    seen = []

    def content(tile, **kwargs):
        seen.append(kwargs['use_cache'])
        return False, tile.input_value

    monkeypatch.setattr(tile_view, 'draw_tile_content', content)
    tree = Split('x', [Tile('left'), Tile('right')])

    @render_func()
    def host(input_value: object, draw_state=None, state: TileManagerState = None, cache_tiles=True):
        tiles.draw_tiles(tree, draw_state, tile_state=state, use_cache=cache_tiles)
        return False, input_value

    for use_cache in (True, False):
        _tick_frame(melty)
        melty.channels_split = False
        begin_frame()
        imgui.set_next_window_size(800, 600)
        imgui.begin('tile cache policy')
        host(None, width=700, height=500, cache_tiles=use_cache)
        if melty.channels_split:
            imgui.get_window_draw_list().channels_merge()
        imgui.end()
        end_frame()
    assert seen == [True, True, False, False]
