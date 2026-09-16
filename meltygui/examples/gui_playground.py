"""Standalone GUI playground: residual backgrounds and suggested text colours."""
import meltygui_imgui as imgui

import meltygui
from meltygui import Style
from meltygui import glfw_window
from meltygui import pressed
from meltygui.hdr_color import pack_color
from meltygui.hdr_color import white
from meltygui.hdr_color import p3
from meltygui.core.melty import Melty
from meltygui.core.toggles import Toggles
from meltygui.core.core_render import render_func
from meltygui.core.header_runtime import draw_header


def toggle_styles():
    Toggles.dynamic_styles = not Toggles.dynamic_styles


def toggle_root():
    Toggles.dynamic_style_root = ((0.88, 0.89, 0.92)
                                 if Toggles.dynamic_style_root[0] < 0.5
                                 else (0.12, 0.13, 0.15))


def reset():
    Toggles.dynamic_styles = True
    Toggles.dynamic_style_root = (0.12, 0.13, 0.15)


@render_func(tint=(0.3, 0.15, 0.15), show_bg=False, show_header=False,
             use_cache=False, auto_resize=False)
def sample(input_value, draw_state, style=None, nested=False):
    if Toggles.dynamic_styles:
        Melty.add_background(style)
    else:
        # Baseline preview is just a fixed traditional background.
        imgui.get_window_draw_list().add_rect_filled(
            draw_state.abs_left, draw_state.abs_top,
            draw_state.abs_left + draw_state.width, draw_state.abs_top + draw_state.height,
            pack_color(0.2, 0.22, 0.25, 1), 5)
    left, top = draw_state.abs_left + 12, draw_state.abs_top + 10
    draw_list = imgui.get_window_draw_list()
    draw_list.add_text(left, top, pack_color(0.6, 0.6, 0.6, 1), input_value)
    for index, suggestion in enumerate(((0.2, 0.2, 0.2), (0.9, 0.9, 0.9), (0.7, 0.12, 0.12))):
        draw_list.add_text(left + index * 210, top + 27, pack_color(*suggestion, 1),
                           ('Dark suggestion', 'Light suggestion', 'Red suggestion')[index])
    if nested:
        imgui.set_cursor_screen_pos((left + 10, top + 58))
        sample('Nested: reduce brightness by 30%', name='nested',
               style=Style((-0.3, -0.3, -0.3)), width=draw_state.width - 44, height=76)
    imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))
    imgui.dummy(draw_state.width, draw_state.height)
    return False, input_value


@glfw_window(name='Melty GUI playground', width=840, height=870, show_name=True,
             with_header=draw_header, app_id='meltygui-gui-playground', style=Style(),
             disable_scroll=False)
@render_func(tint=(0.19, 0.23, 0.29), use_cache=False)
def playground(input_value, draw_state):
    if pressed('ctrl+d'):
        toggle_styles()
    if pressed('ctrl+l'):
        toggle_root()
    meltygui.draw_menu_bar({
        'Styles': {'Enable / disable (Ctrl+D)': toggle_styles,
                   'Light / dark root (Ctrl+L)': toggle_root, 'Reset': reset},
    }, name='menu')
    imgui.text('Dynamic styles: ' + ('ON' if Toggles.dynamic_styles else 'OFF'))
    imgui.text('Same text suggestions on every surface. Ctrl+D compares the old path.')
    width = max(400, draw_state.content_width - 20)
    sample('Inherit the root surface', name='inherit', style=Style(), width=width, height=78)
    sample('Residual: shift toward red (+0.3, 0, 0)', name='red',
           style=Style((0.3, 0, 0)), width=width, height=150, nested=True)
    sample('Residual: shift toward blue (0, 0, +0.4)', name='blue',
           style=Style((0, 0, 0.4)), width=width, height=78)
    sample('Residual: black (-1, -1, -1)', name='black',
           style=Style((-1, -1, -1)), width=width, height=78)
    sample('Absolute: light surface (0.94, 0.92, 0.86)', name='absolute',
           style=Style((0.94, 0.92, 0.86), absolute=True), width=width, height=78)
    sample('Absolute tuple: legacy colour input', name='tuple',
           style=(0.2, 0.45, 0.3), width=width, height=78)
    sample('HDR: four times reference white', name='hdr',
           style=Style(white(4), absolute=True), width=width, height=78)
    sample('Display P3 red: twice reference intensity', name='p3',
           style=Style(p3(1, 0, 0, scale=2), absolute=True), width=width, height=78)
    return False, input_value


from meltygui.examples.lora_data import lora_preview
from meltygui.examples.lora_data import nested_style_kwargs

loras = lora_preview()
lora_styles = nested_style_kwargs()


@glfw_window(name='Loras — GUI playground', width=960, height=950, show_name=True,
             with_header=draw_header, app_id='meltygui-gui-playground', style=Style(),
             disable_scroll=False)
@render_func(tint=(0.19, 0.23, 0.29), use_cache=False)
def lora_window(input_value, draw_state):
    if pressed('ctrl+d'):
        toggle_styles()
    if pressed('ctrl+l'):
        toggle_root()
    meltygui.draw_menu_bar({
        'Styles': {'Enable / disable (Ctrl+D)': toggle_styles,
                   'Light / dark root (Ctrl+L)': toggle_root, 'Reset': reset},
    }, name='lora-menu')
    imgui.text('Studio LoRA models and draw_any, with editable example configurations.')
    imgui.text('Dynamic styles: ' + ('ON' if Toggles.dynamic_styles else 'OFF'))
    changed, _ = meltygui.draw_any(loras, name='Loras', icon='',
                               style=Style((0.04, 0.025, 0.055)),
                               initial={'expanded': True}, child_kwargs=lora_styles)
    return changed, input_value


# This app defaults in; the studio's source default remains False.
reset()

# Register the composition gallery alongside the colour and LoRA windows.
import meltygui.examples.style_layouts as style_layouts
import meltygui.examples.tint_functions as tint_functions
import meltygui.examples.scalar_policies as scalar_policies
import meltygui.examples.lora_policies as lora_policies
