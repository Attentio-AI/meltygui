"""A colour: a chip that opens a picker for the whole Display P3 gamut, up to 10,000 nits.

    changed, model.tint = draw_color(model.tint, name='tint', left=8.0, top=8.0, width=60.0)
    changed, model.tint = draw_color_picker(model.tint, name='tint', left=8.0, top=40.0)

A colour is an extended sRGB tuple or list, `(r, g, b)` or `(r, g, b, a)` (hdr_color.py): 1.0 is
the desktop's white, above it is brighter, a negative channel is outside sRGB. A list is edited in
place, a tuple comes back as a new one.

- `draw_color`: the chip. A press opens the picker under it as a temporary popup (`popup=`).
- `draw_color_picker`: the picker. It composes the views below and keeps where the marker and the
  hue bar stand (`hue`, `spot`), so that grey and black, which have no hue, do not move them.
- `field`: the picking area, four quadrants in one fp16 picture. Bottom left is the classic sRGB
  square (saturation across, value down). Right of it the same rows go on to the edge of P3. Above
  both the top row is lifted to `nits`. One drag crosses all of it.
- `bar`: the hue of the square.
- `Picture`: the field's texture, which its view makes, bakes again per hue and deletes when it retires.
- `locate`, `reach`, `white`, `caption`, `chip`, `extent`: the colour's place in the field, the
  band's range, the desktop's white, the text under the channels, a swatch, the picker's size.

The channels are `draw_float` (value_view.py), and the colours of the boxes are its `look(tint)`.
The colour maths is hdr_color.py (`srgb_plus_linear`, `srgb_extension_coords`) and
model/color_model.py (`_srgb_plus_pick`, `_srgb_plus_marker`), shared with the `@render_func` picker.
Try it: examples/gui_color.py.
"""
import colorsys
import math

import meltygui_imgui as imgui

from meltygui import hdr_color
from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_prototype import gui
from meltygui.hdr_color import pack_color
from meltygui.model.color_model import _srgb_plus_marker
from meltygui.model.color_model import _srgb_plus_pick
from meltygui.view.value_view import PAD, ROUND, ROW, SLATE, box, draw_float, look

# Levers: sizes in logical pixels.
NITS = 10000.0                  # the brightest colour the band reaches, unless the caller says
SQUARE = 180.0                  # side of the sRGB square
WIDE = 34.0                     # width of the P3 quadrants right of it
BAND = 72.0                     # height of the HDR quadrants above it
BAR = 18.0                      # width of the hue bar
GAP = 8.0                       # between the field, the bar and the channels; under a chip
LINE = ROW + 2.0                # one channel row
LETTER = 16.0                   # room for a channel's letter left of its number
CHECK = 6.0                     # side of a chip's checker squares, seen through a clear colour
LOWEST = -1.0                   # the least a colour channel can be dragged or typed to
CHANNELS = 'RGBA'
TINT = SLATE                    # the tint the boxes' colours are made from
# Levers: colours.
SEAM = pack_color(1.0, 1.0, 1.0, 0.35)          # the lines between the quadrants
LIGHT = pack_color(1.0, 1.0, 1.0)               # the markers, and a chip's light checker squares
DARK = pack_color(0.0, 0.0, 0.0)                # the markers' outer ring
GREY = pack_color(0.6, 0.6, 0.6)                # a chip's dark checker squares
HUES = tuple(pack_color(*colorsys.hsv_to_rgb(step / 6.0, 1.0, 1.0)) for step in range(7))


def white():
    """Nits of a colour of 1.0: the desktop's white (Toggles.HDR.follow_desktop_white), else
    Toggles.HDR.pq_reference_nits."""
    from meltygui.core.graphics import wayland_color
    return wayland_color.reference_nits() or wayland_color.desired_reference()


def reach(nits):
    """Stops above white the band covers so that its top row shows `nits`. At least one: a band
    needs a range."""
    return max(math.log2(nits / white()), 1.0)


def extent(channels):
    """(width, height) of the picker of a colour with `channels` channels (3, or 4 with alpha)."""
    width = PAD + SQUARE + WIDE + GAP + BAR + PAD
    height = PAD + BAND + SQUARE + GAP + channels * LINE + ROW + PAD
    return width, height


def locate(rgb, hue):
    """Where `rgb` stands: (hue, spot). `hue` is where the bar is now: it stays there for grey and
    black, and seeds the search for a colour outside sRGB."""
    found, saturation, value, depth, exposure = hdr_color.srgb_extension_coords(*rgb, hue_hint=hue)
    if saturation > 0.0 and value > 0.0:
        hue = found
    return hue, (saturation, depth, value, exposure)


def caption(color):
    """The text under the channels: the hex code of an sRGB colour, else how bright it is and
    which gamut it needs."""
    rgb = color[:3]
    if all(0.0 <= channel <= 1.0 for channel in rgb):
        return '#' + ''.join(f'{int(round(channel * 255.0)):02x}' for channel in color)
    peak = max(hdr_color.srgb_to_linear(channel) for channel in rgb) * white()
    gamut = 'sRGB'
    if min(rgb) < 0.0:
        gamut = 'P3'
    return f'{peak:.0f} nits  {gamut}'


def chip(draw_list, left, top, right, bottom, color, outline):
    """A swatch of `color` with a checker under it where the colour is clear."""
    alpha = 1.0
    if len(color) > 3:
        alpha = min(max(color[3], 0.0), 1.0)
    if alpha < 1.0:
        draw_list.push_clip_rect(left, top, right, bottom, True)
        draw_list.add_rect_filled(left, top, right, bottom, LIGHT, ROUND)
        for row in range(int((bottom - top) / CHECK) + 1):
            for column in range(row % 2, int((right - left) / CHECK) + 1, 2):
                x = left + column * CHECK
                y = top + row * CHECK
                draw_list.add_rect_filled(x, y, x + CHECK, y + CHECK, GREY)
        draw_list.pop_clip_rect()
    draw_list.add_rect_filled(left, top, right, bottom, pack_color(color[0], color[1], color[2], alpha), ROUND)
    draw_list.add_rect(left + 0.5, top + 0.5, right - 0.5, bottom - 0.5, outline, ROUND, 0, 1.0)


class Picture:
    """The field for one hue as an fp16 texture of linear light, so every texel is the real wide
    or bright colour (a vertex colour has 255 steps over its whole range). Its view owns it:
    `show` makes and bakes it, `close` deletes it (`cache.on_retire`). Both run on the render thread."""
    texture = 0
    baked = None                # (hue, stops) of the pixels the texture holds

    def show(self, hue, stops):
        """The texture id, holding the field of `hue` with `stops` of band. About 2 ms when either
        changed, nothing otherwise."""
        recipe = (hue, stops)
        if self.baked == recipe:
            return self.texture
        from meltygui.core.graphics import desktop_gl as gl
        pixels = hdr_color.srgb_plus_linear(hue, int(SQUARE), int(WIDE), int(BAND), stops)
        rows, columns = pixels.shape[:2]
        fresh = self.texture == 0
        if fresh:
            self.texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture)
        if fresh:
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, columns, rows, 0, gl.GL_RGBA, gl.GL_FLOAT, pixels)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        self.baked = recipe
        return self.texture

    def close(self):
        if self.texture:
            from meltygui.core.graphics import desktop_gl as gl
            gl.glDeleteTextures([self.texture])
        self.texture = 0
        self.baked = None


# --- the parts ---------------------------------------------------------------
@gui(cursor=mouse_cursor.HAND, no_save=('picture', 'owned'), width=SQUARE + WIDE, height=BAND + SQUARE)
def field(input_value: tuple, picture: Picture, hue=0.0, stops=4.0, owned=False, width=0.0, height=0.0,
          draw_state=None, cache=None, event=None):
    """The picking area. Its value is the spot `(saturation, depth, value, exposure)`: `depth` is
    how far into the P3 quadrants it is (0.0 inside the square), `exposure` the multiple of white
    (1.0 under the band). A press or a drag moves it, also from outside the view."""
    pressed = event.left_mouse_down()
    dragged = event.left_mouse_drag()
    spot = input_value
    if pressed or dragged:
        saturation, depth, value, exposure = _srgb_plus_pick(event.x, event.y - BAND, SQUARE, WIDE, BAND, stops)
        spot = (saturation, depth, value, exposure)
    if not owned:
        # `Picture()` was built for this view: it ends with it.
        cache.on_retire(cache.current, picture.close)
        draw_state.owned = True
    draw_list = imgui.get_window_draw_list()
    draw_list.add_image(picture.show(hue, stops), (0.0, 0.0), (width, height))
    draw_list.add_line(SQUARE, 0.0, SQUARE, height, SEAM, 1.0)       # sRGB left, P3 right
    draw_list.add_line(0.0, BAND, width, BAND, SEAM, 1.0)            # HDR above, SDR below
    across, down = _srgb_plus_marker(spot[0], spot[1], spot[2], spot[3], SQUARE, WIDE, BAND, stops)
    draw_list.add_circle(across, BAND + down, 6.0, DARK, thickness=1.0)
    draw_list.add_circle(across, BAND + down, 5.0, LIGHT, thickness=1.5)
    return spot != input_value, spot


@gui(cursor=mouse_cursor.RESIZE_NS, width=BAR)
def bar(input_value: float, width=0.0, height=0.0, event=None):
    """The hue of the square, 0.0 at the top to 1.0 at the bottom. A press or a drag sets it."""
    pressed = event.left_mouse_down()
    dragged = event.left_mouse_drag()
    hue = input_value
    if pressed or dragged:
        hue = min(max(event.y / height, 0.0), 1.0)
    draw_list = imgui.get_window_draw_list()
    step = height / 6.0
    for at in range(6):
        above = HUES[at]
        below = HUES[at + 1]
        draw_list.add_rect_filled_multicolor(0.0, at * step, width, (at + 1) * step, above, above, below, below)
    mark = hue * height
    draw_list.add_rect(0.5, mark - 2.0, width - 0.5, mark + 2.0, LIGHT, 0.0, 0, 1.5)
    return hue != input_value, hue


# --- the public views --------------------------------------------------------
@gui(listen=(), width=extent(3)[0])
def draw_color_picker(input_value: tuple, nits=NITS, hue=0.0, spot=None, shown=None, width=0.0, height=0.0,
                      draw_state=None):
    """The picker of a colour `(r, g, b[, a])`. `nits` is the top of the HDR band. Give it the
    size `extent(len(colour))` says; it only paints, so its empty space moves the window it is in.

    Its box first, since the parts lie on it. Then input before drawing: the channels, then where the colour stands, then the bar and the
    field, so each part is drawn from what the ones before it changed."""
    color = [float(channel) for channel in input_value]
    stops = reach(nits)
    table = look(TINT)
    draw_list = imgui.get_window_draw_list()
    box(draw_list, table, width, height)                # first: the parts are drawn over it
    highest = hdr_color.linear_to_srgb(2.0 ** stops)
    rows = PAD + BAND + SQUARE + GAP
    for at in range(len(color)):
        typed, number = draw_float(color[at], key=CHANNELS[at], left=PAD + LETTER, top=rows + at * LINE,
                                   width=width - 2.0 * PAD - LETTER)
        if typed and at == 3:
            color[at] = min(max(number, 0.0), 1.0)
        elif typed:
            color[at] = min(max(number, LOWEST), highest)
    # Where the colour stands. `shown` is the colour `hue` and `spot` stand for: another one (the
    # picker just opened, a channel was typed, the caller passed something else) is located again.
    rgb = tuple(color[:3])
    if shown != rgb:
        hue, spot = locate(rgb, hue)
    turned, hue = bar(hue, name='hue', left=PAD + SQUARE + WIDE + GAP, top=PAD, height=BAND + SQUARE)
    moved, spot = field(spot, name='field', hue=hue, stops=stops, left=PAD, top=PAD)
    if turned or moved:
        saturation, depth, value, exposure = spot
        rgb = hdr_color.extended_from_srgb_extension(hue, saturation, value, depth, exposure)
        color[:3] = rgb
    draw_state.hue = hue
    draw_state.spot = spot
    draw_state.shown = rgb
    middle = (ROW - imgui.get_text_line_height()) * 0.5
    for at in range(len(color)):
        draw_list.add_text(PAD, rows + at * LINE + middle, table.dim, CHANNELS[at])
    last = rows + len(color) * LINE
    chip(draw_list, PAD, last, PAD + 2.0 * ROW, last + ROW, color, table.outline)
    draw_list.add_text(PAD + 2.0 * ROW + GAP, last + middle, table.text, caption(color))
    changed = color != [float(channel) for channel in input_value]
    if not changed:
        return False, input_value
    if isinstance(input_value, list):
        input_value[:] = color
        return True, input_value
    return True, type(input_value)(color)


@gui(cursor=mouse_cursor.HAND, height=ROW)
def draw_color(input_value: tuple, nits=NITS, width=0.0, height=0.0, event=None):
    """A chip of the colour; a press opens the picker under it. Returns (True, colour) in every
    run the picker changed it. The chip is drawn from what the picker returned."""
    pressed = event.left_mouse_down()
    across, down = extent(len(input_value))
    changed, value = draw_color_picker(input_value, name='picker', popup=pressed, nits=nits, left=0.0,
                                       top=height + GAP, width=across, height=down)
    chip(imgui.get_window_draw_list(), 0.0, 0.0, width, height, value, look(TINT).outline)
    return changed, value
