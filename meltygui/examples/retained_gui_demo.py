"""Window-owned Rust @gui experiment. Run tile_manager.py --rust-cache."""
from collections import Counter
import meltygui_imgui as imgui

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save_exclude
from meltygui import gui
from meltygui.hdr_color import pack_color


@no_save_exclude('calls', 'ids')
class RetainedDemo(DictConversion):
    def __init__(self):
        super().__init__()
        self.control = {'closed': False, 'value': 10, 'peer': True}
        self.window_data = {'clicks': 0}
        self.calls = Counter()
        self.ids = {}
        self.accent = 0
        self.path = '/prototype/number-color'


# Local demo palette. Change these to distinguish retained texture boundaries.
white = pack_color(.94, .96, 1., 1.)
muted = pack_color(.65, .77, .86, 1.)

def text(x, y, label, color=white):
    imgui.get_window_draw_list().add_text(x, y, color, label)

def background(draw_state, color):
    imgui.get_window_draw_list().add_rect_filled(0, 0, draw_state.width, draw_state.height,
                                                pack_color(*color))

def control(cache, draw_state, key, label, x, y, width=110):
    hovered = draw_state.get('hover') == key
    color = (.30, .43, .55, 1.) if hovered else (.20, .31, .40, 1.)
    imgui.get_window_draw_list().add_rect_filled(x, y, x + width, y + 30, pack_color(*color), 4)
    text(x + 9, y + 6, label)
    cache.region(key, (x, y, width, 30))

@gui(width=450, height=115)
def number(input_value: int, draw_state=None, cache=None, view_events=None, demo=None):
    demo.calls['number'] += 1
    demo.ids['number'] = cache.current
    cache.associate(demo.path)
    if 'hover' in view_events:
        draw_state.hover = view_events['hover']
    delta = int(bool(view_events.get('plus'))) - int(bool(view_events.get('minus')))
    value = input_value + delta
    background(draw_state, (.14 + .035 * demo.accent, .24, .29, 1.))
    text(12, 10, f'Native cached integer: {value}')
    control(cache, draw_state, 'minus', '- 1', 12, 43)
    control(cache, draw_state, 'plus', '+ 1', 132, 43)
    text(12, 88, 'Edits return through the real owner call.', muted)
    return bool(delta), value

@gui(width=340, height=170)
def window_content(input_value: dict, draw_state=None, cache=None, view_events=None, demo=None):
    demo.calls['window'] += 1
    demo.ids['window'] = cache.current
    if 'hover' in view_events:
        draw_state.hover = view_events['hover']
    if view_events.get('close'):
        demo.control['closed'] = True
        cache.invalidate(demo.control)
    if view_events.get('change'):
        input_value['clicks'] += 1
    background(draw_state, (.16, .24, .32, 1.))
    text(12, 12, 'Declared by the cached owner')
    text(12, 39, f'Window clicks: {input_value["clicks"]}')
    control(cache, draw_state, 'change', 'Click me', 12, 73, 135)
    control(cache, draw_state, 'close', 'Close via owner', 157, 73, 167)
    text(12, 128, 'Moving this window reuses its texture.', muted)
    return bool(view_events.get('change')), input_value

@gui(width=480, height=265)
def owner(input_value: dict, draw_state=None, cache=None, demo=None):
    demo.calls['owner'] += 1
    demo.ids['owner'] = cache.current
    background(draw_state, (.10, .17, .23, 1.))
    text(12, 12, 'Conditional owner')
    text(12, 39, f'Window declared: {not input_value["closed"]}')
    text(12, 64, f'Caller received value: {input_value["value"]}', muted)
    imgui.set_cursor_screen_pos((12, 99))
    changed, value = number(input_value['value'], demo=demo)
    input_value['value'] = value
    # This is the actual conditional under test, not a separate visibility
    # predicate or a call kept alive by the old window replay registry.
    if not input_value['closed']:
        window_content(demo.window_data, demo=demo, melty_window=True,
                       initial={'window_pos': (555, 190)})
    if input_value.get('peer', True):
        peer(None, demo=demo, melty_window=True, initial={'window_pos': (755, 325)})
    text(12, 236, f'Value after result replay: {value}', muted)
    return changed, input_value

@gui(width=280, height=135)
def peer(input_value: object, draw_state=None, cache=None, view_events=None, demo=None):
    demo.calls['peer'] += 1
    if 'hover' in view_events:
        draw_state.hover = view_events['hover']
    if view_events.get('pulse'):
        draw_state.pulses = draw_state.get('pulses', 0) + 1
    background(draw_state, (.23, .17, .28, 1.))
    text(12, 12, 'Independent peer window')
    text(12, 39, f'Local pulses: {draw_state.get("pulses", 0)}')
    control(cache, draw_state, 'pulse', 'Local pulse', 12, 70, 150)
    text(12, 112, 'Click to raise. Drag to overlap.', muted)
    return False, input_value

@gui(width=510, height=325)
def root(input_value: object, draw_state=None, demo=None):
    demo.calls['root'] += 1
    background(draw_state, (.12, .22, .29, 1.))
    text(12, 12, 'Cached grandparent')
    imgui.set_cursor_screen_pos((12, 45))
    # The caller does not consume edits here: owner holds the mutable model.
    owner(demo.control, demo=demo)
    return False, input_value


@gui(width=1050, height=36)
def toolbar(input_value: RetainedDemo, draw_state=None, cache=None, view_events=None):
    demo = input_value
    if 'hover' in view_events:
        draw_state.hover = view_events['hover']
    for key, label, x, width in (
            ('toggle', 'Toggle conditional window', 0, 245),
            ('path', 'Invalidate number by path', 255, 245),
            ('unique', 'Invalidate number by ID', 510, 230),
            ('peer', 'Toggle peer window', 750, 200)):
        control(cache, draw_state, key, label, x, 0, width)
    if view_events.get('toggle'):
        demo.control['closed'] = not demo.control['closed']
        cache.invalidate(demo.control)
    if view_events.get('path'):
        demo.accent = (demo.accent + 1) % 4
        cache.invalidate(demo.path)
    if view_events.get('unique') and 'number' in demo.ids:
        demo.accent = (demo.accent + 1) % 4
        cache.invalidate_id(demo.ids['number'])
    if view_events.get('peer'):
        demo.control['peer'] = not demo.control.get('peer', True)
        cache.invalidate(demo.control)
    return False, input_value


@gui(use_cache=False)
def draw_retained_demo(input_value: object, demo_state: RetainedDemo = None, cache=None):
    imgui.text('Rust retained @gui: command replay, explicit invalidation, owned windows')
    imgui.text('Idle and moving windows execute no cached view bodies. Pixel-only edits do not execute ancestors.')
    toolbar(demo_state)
    imgui.text('Blue boundary: grandparent texture. Inner boundary: owner texture. Number: independent child texture.')
    root(None, demo=demo_state)
    imgui.dummy(1, 40)
    imgui.text('Body executions: ' + '   '.join(f'{name} {demo_state.calls[name]}'
                                              for name in ('root', 'owner', 'number', 'window', 'peer')))
    captures, packets, texture_bytes, command_bytes = cache.gpu.stats()
    imgui.text(f'Native texture renders: {captures}   Captured draw lists: {packets}   '
               f'Textures: {texture_bytes / 1048576:.2f} MiB   Commands: {command_bytes / 1024:.1f} KiB')
    imgui.text(f'Live view nodes: {len(cache.graph.nodes())}   Owned windows: {len(cache.graph.windows())}   '
               f'Model value: {demo_state.control["value"]}')
    imgui.text('Window annotation owns native state, input, frames and cleanup. Fixed-size internal windows; GL backend.')
    return False, input_value
