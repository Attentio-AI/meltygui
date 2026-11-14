"""`draw_any` on `@gui`: a nested dict drawn and edited by its default views.

    .venv/bin/python examples/gui_any.py            # a small settings dict
    .venv/bin/python examples/gui_any.py 100000     # the same with that many more entries

Drag a number sideways or click it to type, click a text to type, press a True / False chip, press a
row with a chevron to open it. `swatch` shows how an app adds a view of its own: by name ('tint') and
by shape (three floats).
"""
import sys

from meltygui import gui, os_window, imgui
from meltygui.hdr_color import pack_color
from meltygui.rust import Shaped, draw_any, is_default_for

EXTRA = int(sys.argv[1]) if len(sys.argv) > 1 else 0

DATA = {
    'name': 'melty',
    'enabled': True,
    'retries': 3,
    'gain': 0.25,
    'tint': (0.2, 0.55, 0.9),
    'size': (1920, 1080),
    'notes': None,
    'window': {'width': 1280, 'height': 720, 'fullscreen': False,
               'position': {'left': 80, 'top': 40}},
    'training': {'rate': 0.0003, 'epochs': 12, 'optimizer': 'adamw',
                 'schedule': {'warmup': 500, 'decay': 0.98, 'cosine': True}},
}
DATA.update({f'item {at}': (at * .5 if at % 3 == 0 else at if at % 3 == 1 else f'text {at}') for at in range(EXTRA)})


@is_default_for('tint', Shaped(tuple, (3,), float))
@gui(listen=())
def swatch(input_value: tuple, width=0., height=0.):
    """A colour as a swatch and its three numbers."""
    draw_list = imgui.get_window_draw_list()
    draw_list.add_rect_filled(0., 1., height * 2, height - 1., pack_color(*input_value), 4.)
    draw_list.add_text(height * 2 + 8, (height - imgui.get_text_line_height()) * .5, pack_color(.7, .74, .8),
                       '  '.join(f'{part:.2f}' for part in input_value))


@os_window(name='draw_any', app_id='meltygui-gui-any', width=560, height=640, use_cache=False)
def app(width=None, height=None):
    draw_any(DATA, name='data', expand=2, left=8., top=8., width=width - 16., height=height - 16.)
