"""Colours: `draw_color` and `draw_color_picker` (meltygui/view/color_picker_view.py).

    .venv/bin/python examples/gui_color.py

Try it on a copy of this file in a folder of its own, so `Paint.pkl` lands there.
What to look at:
- `app`: two chips. A press opens the picker under the chip; a press outside or Escape closes it.
  `Paint.fill` has alpha, `Paint.glow` starts outside sRGB and above white.
- The picker beside them is the same view, called without `popup=`: it edits `Paint.fill` too, so
  a drag in either shows in the other.
- In the picker: the square is sRGB, the strip right of it reaches the edge of P3, the band above
  both reaches 10,000 nits. Drag from the square into either.
"""
from meltygui import gui, os_window, imgui, save
from meltygui.hdr_color import p3, pack_color
from meltygui.rust import draw_color, draw_color_picker
from meltygui.view.color_picker_view import extent

# Levers: sizes in logical pixels, colours.
PAD = 12.0
ROW = 22.0                              # height of a chip and its label
LABEL = 60.0                            # room for a label left of its chip
CHIP = 70.0                             # width of a chip
TEXT = pack_color(0.92, 0.94, 0.96)
ACROSS, DOWN = extent(4)                # the size of the picker of a colour with alpha


@save
class Paint:
    """The app's model, kept between runs in Paint.pkl. The values here are what a first launch starts from."""
    fill = (0.20, 0.55, 0.80, 1.0)
    glow = p3(1.0, 0.25, 0.1, 4.0)


@gui(listen=(), width=LABEL, height=ROW)
def label(input_value: str, height=0.0):
    imgui.get_window_draw_list().add_text(0.0, (height - imgui.get_text_line_height()) * 0.5, TEXT, input_value)


@os_window(name='Colours', app_id='meltygui-color', width=560, height=440, min_width=320, min_height=200, use_cache=False)
def app(width=None, height=None):
    label('Fill', name='fill label', left=PAD, top=PAD)
    changed, Paint.fill = draw_color(Paint.fill, name='fill', left=PAD + LABEL, top=PAD, width=CHIP)
    label('Glow', name='glow label', left=PAD, top=PAD + ROW + PAD)
    changed, Paint.glow = draw_color(Paint.glow, name='glow', left=PAD + LABEL, top=PAD + ROW + PAD, width=CHIP)
    changed, Paint.fill = draw_color_picker(Paint.fill, name='fill picker', left=width - ACROSS - PAD, top=PAD,
                                            width=ACROSS, height=DOWN)
