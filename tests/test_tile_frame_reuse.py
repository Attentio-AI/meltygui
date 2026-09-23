"""Chrome and content share this frame's geometry, except after topology edits."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from meltygui.core.layout import tile_manager_core as core
from meltygui.core.melty import Melty
from meltygui.model.tile_model import Split, Tile
from meltygui.view import tile_view
from meltygui.core.layout import tile_links


@pytest.mark.parametrize('clipped', [False, True])
def test_content_reuses_chrome_rectangles_without_second_walk(monkeypatch, clipped):
    tree, host, bounds, content = setup(monkeypatch, clipped)
    monkeypatch.setattr(core, 'resolve_frames', Mock(side_effect=AssertionError('second walk')))
    core.draw_tiles(tree, host)
    assert bounds.call_count == 2
    assert content.call_count == (1 if clipped else 2)
    assert content.call_args_list[0].kwargs['width'] == 384
    assert content.call_args_list[0].kwargs['height'] == 584


def test_topology_change_discards_partial_old_leaf_list(monkeypatch):
    tree, host, bounds, content = setup(monkeypatch, False)
    original = tree.children[0]
    replacement = Tile(name='split child')
    fired = False
    def replace(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            core.split_tile(tree, (0,), "y", args[5], new_tile=replacement, at=300)
            return True
        return False
    monkeypatch.setattr(core, 'tile_corner_gesture', replace)
    monkeypatch.setattr(core, 'retire_layouts', Mock())
    monkeypatch.setattr(core, 'draw_join_preview', Mock())
    core.draw_tiles(tree, host, tile_state=core.TileManagerState())
    assert bounds.call_count == 4  # aborted first leaf, then all three new leaves
    assert [call.args[0] for call in content.call_args_list] == [original, replacement, tree.children[1]]


def setup(monkeypatch, clipped):
    tree = Split('x', [Tile(name='left'), Tile(name='right')], edges=[{'x': 400}])
    frame = ({'x': 0}, {'x': 800}, {'y': 0}, {'y': 600})
    host = SimpleNamespace(abs_left=0, abs_top=0, closable=False)
    monkeypatch.setattr(core, 'layout_window', lambda ds: host)
    monkeypatch.setattr(core, 'frame_edges', lambda ds: frame)
    monkeypatch.setattr(core, 'ColumnLayout', lambda ds, count, **kw:
                        SimpleNamespace(seed_valid=True, edges=kw['column_edges']))
    monkeypatch.setattr(core, 'draw_split_dividers', Mock())
    monkeypatch.setattr(core, 'add_shadow', Mock())
    monkeypatch.setattr(core, 'hover_shown', lambda *a, **kw: False)
    monkeypatch.setattr(core, 'tile_corner_gesture', lambda *a, **kw: False)
    monkeypatch.setattr(Melty, 'get_clip_rect', lambda: (0, 0, 400 if clipped else 800, 600))
    monkeypatch.setattr(core.imgui, 'get_window_draw_list', lambda: Mock())
    monkeypatch.setattr(core.imgui, 'get_cursor_screen_pos', lambda: (0, 0))
    monkeypatch.setattr(core.imgui, 'set_cursor_screen_pos', Mock())
    monkeypatch.setattr(core.imgui, 'dummy', Mock())
    monkeypatch.setattr(tile_links, 'prepare_endpoints', lambda tree: {})
    bounds = Mock(wraps=core.tile_rect)
    monkeypatch.setattr(core, 'tile_rect', bounds)
    content = Mock(return_value=(False, None))
    monkeypatch.setattr(tile_view, 'draw_tile_content', content)
    return tree, host, bounds, content
