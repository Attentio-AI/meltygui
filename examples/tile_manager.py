"""Run with .venv/bin/python examples/tile_manager.py."""
import meltygui
from meltygui.core.core_render import render_func
from meltygui.examples.tile_manager_demo import draw_tiled_window_manager_demo


@meltygui.glfw_window(name="Tiled editors", app_id="meltygui-tiled-editors",
                     width=1100, height=750)
@render_func(tint=(0.24, 0.30, 0.20), determines_height=False)
def tiled_editors(input_value: object = None, draw_state=None):
    return draw_tiled_window_manager_demo(input_value, width=draw_state.width,
                                          height=draw_state.height)
