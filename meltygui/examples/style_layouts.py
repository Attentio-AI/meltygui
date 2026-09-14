"""Three composed layouts for exploring residual styles in the GUI playground."""
import imgui

import melty
from melty import Style, glfw_window, pressed
from src.lsd.gl_gui.hdr_color import pack_color
from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header


strength = 0.22
type_size = 0
type_weight = 0


def stronger():
    global strength
    strength = min(0.8, strength + 0.1)


def softer():
    global strength
    strength = max(0.02, strength - 0.1)


def larger_type():
    global type_size
    type_size = min(10, type_size + 1)


def smaller_type():
    global type_size
    type_size = max(-6, type_size - 1)


def heavier_type():
    global type_weight
    type_weight = min(200, type_weight + 100)


def lighter_type():
    global type_weight
    type_weight = max(-300, type_weight - 100)


@render_func(show_bg=False, show_header=False, use_cache=False, auto_resize=False)
def panel(input_value, draw_state, style=None):
    """A real Melty context for each rectangle; children inherit its background."""
    if Toggles.dynamic_styles:
        Melty.add_background(style)
    else:
        imgui.get_window_draw_list().add_rect_filled(
            draw_state.abs_left, draw_state.abs_top,
            draw_state.abs_left + draw_state.width, draw_state.abs_top + draw_state.height,
            pack_color(0.2, 0.22, 0.25, 1), 8)
    x, y = draw_state.abs_left, draw_state.abs_top
    w, h = draw_state.width, draw_state.height
    # Paint children first, then text: crossing glyphs sample the actual surface.
    for key, rect, child_style, content in input_value.get('children', ()):
        left, top, width, height = rect
        imgui.set_cursor_screen_pos((x + left * w, y + top * h))
        panel(content, name=key, style=child_style, width=width * w, height=height * h)
    dl = imgui.get_window_draw_list()
    for left, top, label in input_value.get('text', ()):
        dl.add_text(x + left * w, y + top * h, pack_color(0.58, 0.58, 0.58, 1), label)
    imgui.set_cursor_screen_pos((x, y))
    imgui.dummy(w, h)
    return False, input_value


def terraces(delta, depth=0):
    content = {'text': [(0.04, 0.06, f'Level {depth + 1}') ]}
    if depth < 3:
        content['children'] = [('inset', (0.07, 0.23, 0.86, 0.71), Style(delta, shadow_offset=2),
                                terraces(delta, depth + 1))]
    return content


def workspace():
    tile = {'text': [(0.07, 0.15, 'Shared card'), (0.07, 0.39, 'Same residual'),
                     (0.07, 0.65, 'Same text suggestion')]}
    def column(label, base):
        return (label, base, {'text': [(0.06, 0.06, label)], 'children': [
            ('card', (0.06, 0.23, 0.88, 0.68), Style((strength, 0, strength)), tile)]})
    columns = [column('Night', Style((0.07, 0.10, 0.16), absolute=True)),
               column('Paper', Style((0.94, 0.91, 0.83), absolute=True)),
               column('Ocean', Style((0.04, 0.43, 0.49), absolute=True))]
    return {'children': [(label, (i / 3 + .012, .04, .309, .92), base, content)
                         for i, (label, base, content) in enumerate(columns)]}


def crossing():
    veil = Style((.15, .45, .95, .35), absolute=True)
    return {'children': [
        ('night', (0, 0, .5, 1), Style((.035, .045, .07), absolute=True),
         {'children': [('veil', (.16, .56, .84, .30), veil, {})]}),
        ('paper', (.5, 0, .5, 1), Style((.95, .92, .84), absolute=True),
         {'children': [('veil', (0, .56, .84, .30), veil, {})]})],
        'text': [(.30, .18, 'One text run crosses both surfaces'),
                 (.30, .36, 'The renderer chooses the contrast'),
                 (.12, .66, 'Same translucent blue / different inherited surfaces')]}


@glfw_window(name='Style compositions — GUI playground', width=1060, height=1000,
             show_name=True, with_header=draw_header, app_id='melty-gui-playground',
             style=Style(), disable_scroll=False)
@render_func(use_cache=False)
def style_compositions(input_value, draw_state):
    from melty.examples.gui_playground import toggle_styles, toggle_root, reset
    if pressed('ctrl+d'):
        toggle_styles()
    if pressed('ctrl+l'):
        toggle_root()
    melty.draw_menu_bar({
        'Styles': {'Enable / disable (Ctrl+D)': toggle_styles,
                   'Light / dark root (Ctrl+L)': toggle_root, 'Reset': reset},
        'Residual': {'Stronger': stronger, 'Softer': softer},
        'Typography': {'Larger (+1)': larger_type, 'Smaller (-1)': smaller_type,
                       'Heavier (+100)': heavier_type, 'Lighter (-100)': lighter_type},
    }, name='composition-menu')
    imgui.text(f'Dynamic styles: {"ON" if Toggles.dynamic_styles else "OFF"}  |  Residual strength: {strength:.2f}')
    w = max(520, draw_state.content_width - 20)
    imgui.text('01  /  TERRACES     Nested colour + shadow depth residuals (+2 per level)')
    panel({'children': [
        ('warm', (.01, .02, .32, .96), Style((strength, 0, 0)), terraces((strength, 0, 0))),
        ('cool', (.34, .02, .32, .96), Style((0, strength / 2, strength)), terraces((0, strength / 2, strength))),
        ('shade', (.67, .02, .32, .96), Style((-strength,) * 3), terraces((-strength,) * 3))]},
        name='terraces', style=Style(), width=w, height=300)
    imgui.text('02  /  CONTEXT CARDS     The same component on three absolute surfaces')
    panel(workspace(), name='workspace', style=Style(), width=w, height=240)
    imgui.text('03  /  CROSSING     Text adapts per pixel; the inset composites translucent blue')
    panel(crossing(), name='crossing', style=Style(), width=w, height=220)
    imgui.text('04  /  TYPE     Native-size hinted fonts; children add +3 pixels and +100 weight')
    panel({'text': [(.03, .05, f'Parent / size {18.5 + type_size:g}, weight {400 + type_weight}')], 'children': [
        ('medium', (.03, .23, .94, .70), Style((.08, .04, .10), font_size=3, font_weight=100),
         {'text': [(.03, .07, f'Child / +3 pixels, weight {500 + type_weight}')], 'children': [
             ('semibold', (.04, .35, .92, .55),
              Style((.08, .04, .10), font_size=3, font_weight=100),
              {'text': [(.03, .22, f'Grandchild / +6 pixels, weight {600 + type_weight}  |  Il1 0O')]})]})]},
        name='typography', font=Font.JETBRAINS_MONO_19,
        style=Style(font_size=type_size, font_weight=type_weight), width=w, height=300)
    return False, input_value
