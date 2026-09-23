"""A live overlay on a frozen blit tile. Resize the window to try it."""
import time

import meltygui
from meltygui import imgui, render_func
from meltygui.core.runtime.toggles import Tint
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.hdr_color import pack_color


def draw_clock(draw_state, draw_list):
    # Use live geometry and the supplied list: neither this text nor its
    # changing position is captured in the body's tile.
    x = draw_state._abs_left() + 12
    y = draw_state._abs_top() + draw_state.header_height + 12
    color = pack_color(*Tint.dd_text(draw_state.current_tint), 1.0)
    draw_list.add_text(x, y, color, f'Live overlay: {time.monotonic():.2f}')


@render_func(use_cache=True, freeze_resize=True, draw_overlay=draw_clock,
             tint=(0.20, 0.30, 0.42), show_bg=True, auto_resize=False)
def draw_cached_panel(input_value: str, draw_state=None):
    imgui.get_window_draw_list().add_text(
        draw_state.abs_left + 12, draw_state.abs_top + 70,
        pack_color(*Tint.dd_text(draw_state.current_tint), 1.0), input_value)
    return False, input_value


@meltygui.glfw_window(name='Render overlays', app_id='meltygui-render-overlay-demo',
                     width=720, height=420)
@render_func(tint=(0.20, 0.30, 0.42), determines_height=False)
def overlay_demo(input_value: object = None, draw_state=None):
    # A clock animates even without input; ordinary overlays can let the app idle.
    request_render()
    return draw_cached_panel('This body is cached. The clock draws every frame.',
                             width=draw_state.width, height=draw_state.height)
