"""Run with .venv/bin/python examples/tile_manager.py."""
import meltygui
import sys
from meltygui.core.core_render import render_func
from meltygui.examples.tile_manager_demo import draw_tiled_window_manager_demo

_rust_cache = '--rust-cache' in sys.argv
_rust_layout = '--rust-layout' in sys.argv
_rust_prototype = '--rust-gui' in sys.argv or _rust_cache

if _rust_cache or _rust_layout:
    from meltygui.examples.retained_gui_demo import draw_retained_demo
    from meltygui.examples.retained_layout_demo import draw_layout_demo

    @meltygui.os_window(name="Rust layout laboratory" if _rust_layout else "Rust retained @gui",
                       app_id="meltygui-rust-layout" if _rust_layout else "meltygui-rust-cache",
                       width=1400 if _rust_layout else 1300, height=1000 if _rust_layout else 850,
                       use_cache=False, live=True)
    def retained_app():
        if _rust_layout:
            return draw_layout_demo()
        return draw_retained_demo()
else:
    @meltygui.glfw_window(name="Rust @gui prototype" if _rust_prototype else "Tiled editors",
                         app_id="meltygui-rust-prototype" if _rust_prototype else "meltygui-tiled-editors",
                         width=1300 if _rust_prototype else 1100, height=850 if _rust_prototype else 750)
    @render_func(tint=(0.24, 0.30, 0.20), determines_height=False,
                 **({'live': True, 'use_cache': False} if _rust_prototype else {}))
    def tiled_editors(input_value: object = None, draw_state=None):
        if '--rust-gui' in sys.argv:
            from meltygui.examples.rust_gui_demo import draw_rust_gui_demo
            return draw_rust_gui_demo(input_value, width=draw_state.width, height=draw_state.height,
                                      name='Rust prototype', use_cache=False)
        return draw_tiled_window_manager_demo(input_value, width=draw_state.width,
                                              height=draw_state.height)
