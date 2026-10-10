"""Interactive layout experiment: the same edits under auto and fixed parents."""
from collections import Counter
import meltygui_imgui as imgui

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save_exclude
from meltygui import gui
from meltygui.examples.retained_gui_demo import control, text


@no_save_exclude('calls', 'ids')
class LayoutDemo(DictConversion):
    def __init__(self):
        super().__init__()
        self.data = {'rows': 3, 'color': False}
        self.width = 460
        self.show = True
        self.horizontal = False
        self.window = True
        self.calls = Counter()
        self.ids = {}


def mark(demo, name, cache):
    demo.calls[name] += 1
    demo.ids[name] = cache.current


@gui(width=None, height=None)
def changing_content(input_value: dict, draw_state=None, cache=None, demo=None, label=''):
    mark(demo, label + '/content', cache)
    # Raw drawing reserves its layout extent with dummy, just like ImGui items.
    height = input_value['rows'] * 24
    color = 0xFF8A7047 if input_value['color'] else 0xFF695535
    imgui.get_window_draw_list().add_rect_filled(0, 0, draw_state.width, height, color)
    for row in range(input_value['rows']):
        text(8, row * 24 + 4, f'Row {row + 1} (shared data)')
    imgui.dummy(draw_state.width, height)
    if input_value['rows']:
        imgui.push_text_wrap_pos(draw_state.width)
        imgui.text_unformatted('Narrow the boundary to wrap this text. Its measured height moves the cached sibling.')
        imgui.pop_text_wrap_pos()
    return False, input_value


@gui(width=140, height=30)
def cached_sibling(input_value: object, cache=None, demo=None, label=''):
    mark(demo, label + '/sibling', cache)
    imgui.get_window_draw_list().add_rect_filled(0, 0, 140, 30, 0xFF52764B)
    text(7, 6, 'Cached sibling')
    return False, input_value


@gui(width=280, height=None, tint=(.11, .17, .22, 1.))
def owned_window(input_value: dict, cache=None, demo=None, label=''):
    mark(demo, label + '/window', cache)
    imgui.text('This window measures its content.')
    changing_content(input_value, demo=demo, label=label + '/window')
    return False, input_value


@gui(width=None, height=None)
def layout_parent(input_value: object, draw_state=None, cache=None, demo=None, label=''):
    mark(demo, label + '/parent', cache)
    if demo.show:
        changing_content(demo.data, demo=demo, label=label,
                         width=draw_state.width - 155 if demo.horizontal else None)
    if demo.horizontal and demo.show:
        imgui.same_line()
    cached_sibling(None, demo=demo, label=label)
    if demo.window and label == 'Auto':
        owned_window(demo.data, demo=demo, label=label, melty_window=True,
                     initial={'window_pos': (1030, 230)})
    return False, input_value


@gui(width=480, height=400)
def layout_panel(input_value: object, draw_state=None, cache=None, demo=None, fixed=False):
    label = 'Fixed' if fixed else 'Auto'
    mark(demo, label + '/panel', cache)
    imgui.get_window_draw_list().add_rect_filled(0, 0, draw_state.width, 400, 0xFF30261D)
    imgui.text('Fixed parent (170px)' if fixed else 'Measured parent height')
    layout_parent(None, demo=demo, label=label, height=170 if fixed else None)
    imgui.push_text_wrap_pos(draw_state.width)
    imgui.text_unformatted('Ancestor footer: follows the parent extent')
    imgui.pop_text_wrap_pos()
    return False, input_value


@gui(width=1240, height=78)
def layout_controls(input_value: LayoutDemo, draw_state=None, cache=None, view_events=None):
    if 'hover' in view_events:
        draw_state.hover = view_events['hover']
    for key, label, x, y in (
            ('grow', 'Add row', 0, 0), ('shrink', 'Remove row', 155, 0),
            ('color', 'Pixels only', 310, 0), ('width', 'Narrow / wide', 465, 0),
            ('flow', 'Column / row', 620, 0), ('show', 'Show / hide', 775, 0),
            ('window', 'Owned window', 930, 0), ('empty', 'Empty content', 0, 38),
            ('reset', 'Reset counters', 155, 38)):
        control(cache, draw_state, key, label, x, y, 145)
    if any(view_events.get(key) for key in ('grow', 'shrink', 'color', 'empty')):
        if view_events.get('grow'):
            input_value.data['rows'] = min(12, input_value.data['rows'] + 1)
        if view_events.get('shrink'):
            input_value.data['rows'] = max(0, input_value.data['rows'] - 1)
        if view_events.get('color'):
            input_value.data['color'] = not input_value.data['color']
        if view_events.get('empty'):
            input_value.data['rows'] = 0
        cache.invalidate(input_value.data)
    if view_events.get('width'):
        input_value.width = 300 if input_value.width == 460 else 460
    if view_events.get('flow'):
        input_value.horizontal = not input_value.horizontal
    if view_events.get('show'):
        input_value.show = not input_value.show
    if view_events.get('window'):
        input_value.window = not input_value.window
    if any(view_events.get(key) for key in ('flow', 'show', 'window')):
        cache.invalidate(layout_parent)
    if view_events.get('reset'):
        input_value.calls.clear()
    return False, input_value


@gui(use_cache=False)
def draw_layout_demo(input_value: object, demo: LayoutDemo = None, cache=None):
    imgui.text('Retained @gui layout laboratory')
    imgui.text('Same model, independent views. Compare which bodies execute after each edit.')
    layout_controls(demo)
    imgui.text('Pixels only: no parent execution. Size: parent reflows, sibling texture stays cached.')
    layout_panel(None, demo=demo, fixed=False, width=demo.width, key='auto')
    imgui.same_line()
    layout_panel(None, demo=demo, fixed=True, width=demo.width, key='fixed')
    imgui.text('Execution counters (moving the owned window should not change them):')
    for label in ('Auto', 'Fixed'):
        imgui.text(label + ': ' + '   '.join(f'{part}={demo.calls[label + "/" + part]}'
                                          for part in ('content', 'sibling', 'parent', 'panel', 'window')))
    imgui.text('Measured geometry:')
    for name, node in sorted(demo.ids.items()):
        if node in cache.records and not name.endswith('panel'):
            x, y, w, h = cache.graph.info(node)['rect']
            imgui.text(f'{name:24s}  ({x:g}, {y:g})  {w:g} x {h:g}')
    renders, packets, texture_bytes, _ = cache.gpu.stats()
    imgui.text(f'Texture renders {renders}   Captured packets {packets}   Texture memory {texture_bytes / 1048576:.2f} MiB')
    return False, input_value
