"""One nested view under three font/shadow accumulation policies."""
import imgui

from melty import Style, default_scalar_accumulation, glfw_window, pressed
from melty.examples.style_layouts import panel
from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header


def subtract(context, residual):
    return context - residual


def size_curve(context, residual):
    return context * (1 + residual / 20)


def weight_curve(context, residual):
    return context * (1 + residual / 400)


def shadow_curve(context, residual):
    return context + residual / (1 + abs(context))


def nested(depth=0):
    content = {'text': [(.06, .08, f'Level {depth + 1} / Aa 012')]}
    if depth < 3:
        content['children'] = [('child', (.055, .24, .89, .70),
                                Style((.055, .04, .065), font_size=2,
                                      font_weight=100, shadow_offset=2), nested(depth + 1))]
    return content


@glfw_window(name='Font and shadow policies', width=1140, height=550,
             show_name=True, with_header=draw_header, app_id='melty-gui-playground',
             style=Style((.30, .32, .36), absolute=True))
@render_func(use_cache=False)
def scalar_policy_window(input_value, draw_state):
    from melty.examples.gui_playground import toggle_styles
    if pressed('ctrl+d'):
        toggle_styles()
    imgui.text('Same nested view: +2 size, +100 weight, +2 shadow at every child. Ctrl+D toggles styles.')
    w = max(900, draw_state.content_width - 20)
    policies = [('Add', default_scalar_accumulation, default_scalar_accumulation, default_scalar_accumulation),
                ('Subtract', subtract, subtract, subtract),
                ('Nonlinear', size_curve, weight_curve, shadow_curve)]
    panel({'children': [(label, (i / 3 + .01, .02, .31, .96),
                         Style((.055, .04, .065), font_size_fn=size_fn,
                               font_weight_fn=weight_fn, shadow_fn=shadow_fn),
                         {'text': [(.06, .03, label)], 'children': [
                             ('tree', (.02, .14, .96, .84), Style(), nested())]})
                        for i, (label, size_fn, weight_fn, shadow_fn) in enumerate(policies)]},
          name='scalar-policies', style=Style(), font=Font.JETBRAINS_MONO_19,
          width=w, height=450)
    return False, input_value
