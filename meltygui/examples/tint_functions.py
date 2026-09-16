"""One reusable layout, three root-window tint accumulation policies."""
import math

import meltygui_imgui as imgui
from meltygui import Style
from meltygui import default_tint_accumulation
from meltygui import glfw_window
from meltygui import pressed
from meltygui.examples.style_layouts import panel
from meltygui.examples.style_layouts import terraces
from meltygui.core.core_render import render_func
from meltygui.core.layout.header_runtime import draw_header


def darken(context_tint, tint_res):
    return tuple(c - r for c, r in zip(context_tint, tint_res))


def wave(context_tint, tint_res):
    # Nonlinear, deliberately bounded demo; zero residual remains an identity.
    return tuple(math.sin(math.asin(max(-1, min(1, c))) + r * 3)
                 for c, r in zip(context_tint, tint_res))


@render_func(use_cache=False)
def reusable_window(input_value, draw_state):
    from meltygui.examples.gui_playground import toggle_styles
    if pressed('ctrl+d'):
        toggle_styles()
    imgui.text('Same render function. Same positive residuals.')
    w = max(320, draw_state.content_width - 18)
    panel(terraces((.10, .08, .06)), name='levels',
          style=Style((.10, .08, .06)), width=w, height=310)
    imgui.text('Reusable cards inherit the window policy')
    card = {'text': [(.08, .15, 'Shared component'), (.08, .43, 'Same text suggestion')],
            'children': [('inset', (.08, .65, .84, .25), Style((.10, .08, .06)),
                          {'text': [(.08, .18, 'Nested detail')]})]}
    panel(card, name='card', style=Style((.10, .08, .06), shadow_offset=2),
          width=w, height=150)
    imgui.text('This subtree explicitly restores the default')
    panel({'text': [(.05, .2, 'Default accumulation override')],
           'children': [('nested', (.05, .50, .90, .4), Style((.10, .08, .06)),
                         {'text': [(.04, .15, 'Descendants inherit the override')]})]},
          name='override', style=Style((.10, .08, .06), tint_fn=default_tint_accumulation),
          width=w, height=120)
    return False, input_value


# Register the SAME render function three times. Only root Style() changes.
for title, accumulate in (('Lighten', default_tint_accumulation),
                          ('Darken', darken), ('Wave', wave)):
    glfw_window(name=f'Tint policy / {title}', width=560, height=720,
                show_name=True, with_header=draw_header, app_id='meltygui-gui-playground',
                disable_scroll=False,
                style=Style((.52, .56, .60), absolute=True, tint_fn=accumulate))(
                    reusable_window)
