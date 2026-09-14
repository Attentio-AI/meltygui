"""Closable Melty LoRA windows sharing data and rendering, with different policies."""
import imgui
import melty
from melty import Style, glfw_window, pressed, default_scalar_accumulation, default_tint_accumulation
from melty.examples.lora_data import lora_preview, nested_style_kwargs
from melty.examples.tint_functions import darken, wave
from melty.examples.scalar_policies import subtract, size_curve, weight_curve, shadow_curve
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header


loras = lora_preview()
children = nested_style_kwargs()
node = children
while node:
    node['style'] = Style((.035, .025, .045), font_size=.5, font_weight=25, shadow_offset=.4)
    node = node.get('child_kwargs')

policies = (
    ('Loras / Add', default_tint_accumulation, default_scalar_accumulation,
     default_scalar_accumulation, default_scalar_accumulation),
    ('Loras / Subtract', darken, subtract, subtract, subtract),
    ('Loras / Nonlinear', wave, size_curve, weight_curve, shadow_curve),
)


def reopen():
    for name, *_ in policies:
        Melty.open_window(name)


@render_func(use_cache=False)
def lora_contents(input_value, draw_state):
    changed, _ = melty.draw_any(input_value, name='Loras', icon='',
                               style=Style((.035, .025, .045)),
                               initial={'expanded': True}, child_kwargs=children)
    return changed, input_value


@glfw_window(name='LoRA accumulation comparison', width=1800, height=1040,
             show_name=True, with_header=draw_header, app_id='melty-gui-playground', style=Style())
@render_func(use_cache=False)
def lora_comparison(input_value, draw_state):
    from melty.examples.gui_playground import toggle_styles
    if pressed('ctrl+d'):
        toggle_styles()
    melty.draw_menu_bar({'Windows': {'Reopen LoRA windows': reopen},
                         'Styles': {'Enable / disable (Ctrl+D)': toggle_styles}}, name='lora-comparison-menu')
    imgui.text('Same LoRA data and render function. Each Melty window can be moved, resized, scrolled, and closed.')
    for i, (name, tint_fn, size_fn, weight_fn, shadow_fn) in enumerate(policies):
        lora_contents(loras, name=name, as_window=True, shadow=True, with_header=draw_header,
                      force_initial=True,
                      style=Style((.50, .53, .57), absolute=True, tint_fn=tint_fn,
                                  font_size_fn=size_fn, font_weight_fn=weight_fn, shadow_fn=shadow_fn),
                      initial={'window_pos': (12 + i * 592, 15), 'width': 580,
                               'height': 920, 'expanded': True})
    return False, input_value
