"""Demo for the tiled window manager (view/core_views/tile_manager.py).

A Blender-style area layout drawn flat on one draw_state: every divider —
the window's frame edges, the column edges of the root split, the row
edges inside each column — is a shared edge object in the window's
collision solve, so dragging any of them pushes and pulls the rest and
out through the window frame, exactly like the columns playground.
"""
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.window_decoration import window
from meltygui.views.tile_manager import Split
from meltygui.views.tile_manager import Tile
from meltygui.views.tile_manager import TileManagerState
from meltygui.views.tile_manager import draw_tiles

# Module-level so hotswap re-exec reuses it and the seeded edges survive
# edits while iterating in draw_tiles. Blender's default screen, roughly:
# a tall outliner column on the left, the viewport over the timeline in
# the middle, and a properties column split three ways on the right.
tiles_demo = Split("x", [
    Tile("outliner", tint=(0.20, 0.36, 0.52)),
    Split("y", [
        Tile("viewport", tint=(0.28, 0.28, 0.30)),
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
@render_func(tint=(0.24, 0.30, 0.20), auto_resize=False, min_width=720,
             min_height=420, show_bg=True)
def draw_tiled_window_manager_demo(input_value, draw_state, tile_state: TileManagerState = None):
    tree = tiles_demo if input_value is None else input_value
    changed = draw_tiles(tree, draw_state, tile_state=tile_state)
    return changed, tree
