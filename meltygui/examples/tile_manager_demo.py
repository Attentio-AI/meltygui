"""Demo for the tiled window manager (core/layout/tile_manager_core.py).

A Blender-style area layout drawn flat on one draw_state: every divider —
the window's frame edges, the column edges of the root split, the row
edges inside each column — is a shared edge object in the window's
collision solve, so dragging any of them pushes and pulls the rest and
out through the window frame, exactly like the columns playground.
"""
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window
from meltygui.model.tile_model import Split
from meltygui.model.tile_model import Tile
from meltygui.core.layout.tile_manager_core import TileManagerState
from meltygui.core.layout.tile_manager_core import draw_tiles


class CounterState(DictConversion):
    def __init__(self):
        super().__init__()
        self.count = 0


@render_func(multi_instance=True, tint=(0.20, 0.36, 0.52), icon=f"\uf1ec",
             display_name="Counter")
def draw_tile_counter(input_value: object, draw_state,
                      counter_state: CounterState = None, style_manager=None):
    """Choose in two tiles to exercise independent, persisted view state."""
    from meltygui.view.header_view import flat_button
    clicked = flat_button(f"Count: {counter_state.count} — add one", draw_state,
                          "increment", width=180, style_manager=style_manager)
    if clicked:
        counter_state.count += 1
    return clicked, input_value


@render_func(multi_instance=True, tint=(0.22, 0.44, 0.30), icon=f"\uf249",
             display_name="Notes")
def draw_tile_notes(input_value: object):
    from meltygui.view.text_view import draw_text
    changed, value = draw_text("" if input_value is None else input_value)
    return changed, value if changed else input_value


@render_func(multi_instance=True, tint=(0.40, 0.32, 0.20), icon=f"\uf0db",
             display_name="Columns")
def draw_tile_columns(input_value: object, layout_frame=None):
    """Two resizable cells over the tile's own edges: a renderer whose columns
    register on the window through ``layout_frame`` (the chat tile, the code
    editor's compare split do the same). Split and join this tile to exercise
    the retirement of the layouts a removed tile leaves behind."""
    from meltygui.view.layout_view import draw_columns
    edges = ({"left_edge": layout_frame[0], "right_edge": layout_frame[1]}
             if layout_frame is not None else {})
    changed, _ = draw_columns({"left": "left cell", "right": "right cell"}, **edges)
    return changed, input_value


class TileManagerDemoModel(DictConversion):
    """The demo app owns the layout, including each tile's selected function."""

    def __init__(self):
        super().__init__()
        self.tree = Split("x", [
            Tile("outliner", tint=(0.20, 0.36, 0.52), render_func=draw_tile_counter),
            Split("y", [
                Tile("viewport", tint=(0.28, 0.28, 0.30), render_func=draw_tile_counter),
                Tile("timeline", tint=(0.42, 0.30, 0.18)),
            ]),
            Split("y", [
                Tile("properties", tint=(0.22, 0.44, 0.30)),
                Tile("modifiers", tint=(0.36, 0.24, 0.44)),
                Tile("materials", tint=(0.50, 0.22, 0.24)),
            ]),
        ])


# auto_resize=False: a fixed-size window gives the tree a stable frame;
# the tiles inside it and every edge moves independently of the rest.
@window
@render_func(tint=(0.24, 0.30, 0.20), auto_resize=False, show_bg=True)
def draw_tiled_window_manager_demo(input_value: object, draw_state,
                                   tile_state: TileManagerState = None,
                                   app_model: TileManagerDemoModel = None,
                                   multi_instance_renderers=()):
    tree = app_model.tree if input_value is None else input_value
    changed = draw_tiles(tree, draw_state, tile_state=tile_state,
                         multi_instance_renderers=multi_instance_renderers)
    return changed, input_value
