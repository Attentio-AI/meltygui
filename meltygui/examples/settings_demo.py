#!/usr/bin/env python3
"""`@glfw_window(settings=...)`: a plain dict of defaults the framework keeps
between runs, edited in the settings window the title bar's cog opens.
Nested dicts are the window's sub-folders. The dict is loaded in place, so
the body reads it directly; the file is
`$XDG_CONFIG_HOME/settings-demo/settings.json`."""
import meltygui_imgui as imgui
from meltygui import glfw_window
from meltygui.core.core_render import render_func

SETTINGS = {
    'greeting': 'hello',
    'font_size': 14,
    'show_counter': True,
    'editor': {'tab_width': 4, 'wrap': False, 'theme': 'dark'},
    'colors': {'accent': (0.2, 0.6, 0.9)},
}


@glfw_window(name='Settings demo', width=560, height=300, settings=SETTINGS)
@render_func(tint=(0.2, 0.5, 0.7), auto_resize=False, min_width=420, min_height=200)
def draw_settings_demo(_, draw_state):
    imgui.text('Click the cog in the title bar to open the settings window.')
    imgui.text(f"greeting = {SETTINGS['greeting']!r}   font_size = {SETTINGS['font_size']}")
    imgui.text(f"editor = {SETTINGS['editor']}")
    if SETTINGS['show_counter']:
        imgui.text(f"accent = {SETTINGS['colors']['accent']}")
    return False, None
