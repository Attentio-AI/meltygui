"""A colour: a chip that opens a picker for the whole Display P3 gamut, up to 32 times white.

    changed, model.tint = draw_color(model.tint, name='tint', left=8.0, top=8.0, width=60.0)
    changed, model.tint = draw_color_picker(model.tint, name='tint', left=8.0, top=40.0)

A colour is an extended sRGB tuple or list, `(r, g, b)` or `(r, g, b, a)` (hdr_color.py): 1.0 is
the desktop's white, above it is brighter, a negative channel is outside sRGB. A list is edited in
place, a tuple comes back as a new one. One with fewer than three channels, such as the `()` a
view that is passed nothing starts from, is filled up from `START` and returned as an edit.

- `draw_color`: the chip. A press opens the picker under it as a temporary popup (`popup=`).
- `draw_color_picker`: the picker. It composes the views below and keeps where the marker and the
  hue bar stand (`hue`, `spot`), so that grey and black, which have no hue, do not move them.
  `selector=` says which colours its field lays out:
    - `meltygui.HDR` (the default): one Display P3 square, saturation across and value down, with
      the HDR band above it and the part sRGB reaches outlined. The hue bar is P3 hue.
    - `meltygui.SRGB_PLUS`: the classic sRGB square bottom left, the same rows going on to the
      edge of P3 right of it, the HDR band above both. The hue bar is sRGB hue.
    - `meltygui.SRGB`: the classic sRGB square alone; a colour outside it is shown at its edge.
  The band is to scale: `BAND` high at `PEAK` times white, lower for less `headroom`, and under
  1.0 the square itself is cut from above. One drag crosses the whole field.
- `SELECTORS`: what tells the three apart, a row of plain functions each (`Selector`). To add a
  picker, write its six functions and a row.
- `field`: the picking area, one fp16 picture. `bar`: the hue, each drawn as its most vivid colour.
- `Picture`: the field's texture, which its view makes, bakes again per hue and deletes when it retires.
- `whole`, `scale`, `white`, `caption`, `chip`, `extent`: a colour with all its channels, the
  band's range, the desktop's white, the text under the channels, a swatch, the picker's size.

The channels are `draw_float` (value_view.py), and the colours of the boxes are its `look(tint)`.
The colour maths is hdr_color.py (`srgb_plus_linear`, `wide_square_linear`, `srgb_extension_coords`) and
model/color_model.py (`_srgb_plus_pick`, `_srgb_plus_marker`), shared with the `@render_func` picker.
Try it: examples/gui_color.py.
"""
import colorsys
import math
from collections import namedtuple

import meltygui_imgui as imgui

from meltygui import hdr_color
from meltygui.core.input import mouse_cursor
from meltygui.core.rendering.gui_prototype import gui
from meltygui.hdr_color import HDR, SRGB, SRGB_PLUS, pack_color
from meltygui.model.color_model import _srgb_plus_marker
from meltygui.model.color_model import _srgb_plus_pick
from meltygui.view.value_view import PAD, ROUND, ROW, SLATE, box, draw_float, look

# Levers: sizes in logical pixels.
PEAK = 32.0                     # the brightest colour a picker can show, in multiples of white: 5 stops
SQUARE = 180.0                  # side of the sRGB square
WIDE = 34.0                     # width of the P3 quadrants right of it
ACROSS = SQUARE + WIDE          # width of every field: `HDR` and `SRGB` lay their one square over all of it
TEXELS = 180                    # side of the `HDR` field's picture, which is stretched over the field
BAND = 72.0                     # height of the HDR quadrants above it at `PEAK`: the pixels per stop
TINY = 1e-6                     # stops of a band that is not there, for the maths that divides by them
FAINT = 1e-4                    # a saturation or value under this has no hue of its own: the bar stays
BAR = 18.0                      # width of the hue bar
GAP = 8.0                       # between the field, the bar and the channels; under a chip
LINE = ROW + 2.0                # one channel row
LETTER = 16.0                   # room for a channel's letter left of its number
CHECK = 6.0                     # side of a chip's checker squares, seen through a clear colour
LOWEST = -1.0                   # the least a colour channel can be dragged or typed to
CHANNELS = 'RGBA'
START = (1.0, 1.0, 1.0)         # the channels a colour that has fewer than three is filled up with
TINT = SLATE                    # the tint the boxes' colours are made from
# Levers: colours.
SEAM = pack_color(1.0, 1.0, 1.0, 0.35)          # the lines between the quadrants, and the sRGB outline
LIGHT = pack_color(1.0, 1.0, 1.0)               # the markers, and a chip's light checker squares
DARK = pack_color(0.0, 0.0, 0.0)                # the markers' outer ring
GREY = pack_color(0.6, 0.6, 0.6)                # a chip's dark checker squares
SHADES = 24                     # segments of the hue bar; more follow the P3 edge more closely


def white():
    """Nits of a colour of 1.0, for the caption only: the desktop's white where the compositor
    says (Toggles.HDR.follow_desktop_white), else Toggles.HDR.pq_reference_nits, a guess."""
    from meltygui.core.graphics import wayland_color
    return wayland_color.reference_nits() or wayland_color.desired_reference()


def scale(headroom, selector=HDR):
    """The field of a picker whose brightest row is `headroom` times white (0.0 to `PEAK`, in
    linear light), to one scale for every picker and every display: (band, square, stops, top).
    `band` is the height of the HDR quadrants, `BAND` at `PEAK` and a stop the same pixels in all
    of them; `stops` is what it covers above white. Under 1.0 there is no band and the square is
    cut from above: `square` is the height left of it, `top` the highest channel value it shows
    (above white: of the band's top row). `SRGB` has no band: its headroom ends at white."""
    headroom = min(max(headroom, 0.0), PEAK)
    if selector == SRGB:
        headroom = min(headroom, 1.0)
    top = hdr_color.linear_to_srgb(headroom)
    if headroom > 1.0:
        stops = math.log2(headroom)
        return float(round(BAND * stops / math.log2(PEAK))), SQUARE, stops, top
    return 0.0, float(round(SQUARE * top)), 0.0, top


def extent(channels, headroom=PEAK, selector=HDR):
    """(width, height) of the picker of a colour with `channels` channels (3, or 4 with alpha)
    that reaches `headroom` times white with `selector`."""
    band, square, stops, top = scale(headroom, selector)
    width = PAD + ACROSS + GAP + BAR + PAD
    height = PAD + band + square + GAP + channels * LINE + ROW + PAD
    return width, height


def whole(color):
    """`color` as a list of floats with at least red, green and blue. An empty tuple is what
    `tuple()` builds for a view that was passed nothing: it becomes `START`."""
    found = [float(channel) for channel in color]
    found.extend(START[len(found):])
    return found


def clamp(value):
    return min(max(value, 0.0), 1.0)


# --- the selectors -----------------------------------------------------------
# One row of `SELECTORS` each. In all of them `x` and `y` are pixels in the field from the top
# left corner of the whole square (so `y` is negative in the band), `band` is the band's height
# and `stops` what it covers (never 0: `TINY`), and a `spot` is the selector's own coordinates.
#   bake(hue, band, stops)    -> (pixels, outline): linear light, row 0 on top, the band's rows first;
#                                and the points of a line to draw over it, as fractions of the field
#   pick(x, y, band, stops)   -> spot
#   mark(spot, band, stops)   -> (x, y)
#   locate(rgb, hue)          -> (hue, spot); `hue` is the bar's now, kept for grey and black
#   color(hue, spot)          -> (r, g, b)
#   shade(hue)                -> the packed colour the hue bar shows for `hue`
Selector = namedtuple('Selector', 'bake pick mark locate color shade')


def srgb_bake(hue, band, stops):
    return hdr_color.srgb_plus_linear(hue, int(SQUARE), 0, 0, 0.0), ()


def srgb_pick(x, y, band, stops):
    return clamp(x / ACROSS), clamp(1.0 - y / SQUARE)


def srgb_mark(spot, band, stops):
    return spot[0] * ACROSS, (1.0 - spot[1]) * SQUARE


def srgb_locate(rgb, hue):
    found, saturation, value = colorsys.rgb_to_hsv(clamp(rgb[0]), clamp(rgb[1]), clamp(rgb[2]))
    if saturation > FAINT and value > FAINT:
        hue = found
    return hue, (saturation, value)


def srgb_color(hue, spot):
    return colorsys.hsv_to_rgb(hue, spot[0], spot[1])


def srgb_shade(hue):
    return pack_color(*colorsys.hsv_to_rgb(hue, 1.0, 1.0))


def plus_bake(hue, band, stops):
    return hdr_color.srgb_plus_linear(hue, int(SQUARE), int(WIDE), int(band), stops), ()


def plus_pick(x, y, band, stops):
    """The spot is (saturation, depth, value, exposure): `depth` is how far into the P3 quadrants
    it is (0.0 inside the square), `exposure` the multiple of white (1.0 under the band)."""
    return _srgb_plus_pick(x, y, SQUARE, WIDE, band, stops)


def plus_mark(spot, band, stops):
    return _srgb_plus_marker(spot[0], spot[1], spot[2], spot[3], SQUARE, WIDE, band, stops)


def plus_locate(rgb, hue):
    """`hue` also seeds the search for a colour outside sRGB."""
    found, saturation, value, depth, exposure = hdr_color.srgb_extension_coords(*rgb, hue_hint=hue)
    if saturation > FAINT and value > FAINT:
        hue = found
    return hue, (saturation, depth, value, exposure)


def plus_color(hue, spot):
    saturation, depth, value, exposure = spot
    return hdr_color.extended_from_srgb_extension(hue, saturation, value, depth, exposure)


def plus_shade(hue):
    """Full saturation and value at the edge of P3: the far corner of the square's top row."""
    return pack_color(*hdr_color.extended_from_srgb_extension(hue, 1.0, 1.0, 1.0))


def hdr_bake(hue, band, stops):
    share = band / (band + SQUARE)
    return hdr_color.wide_square_linear(hue, TEXELS, share, stops), hdr_color.srgb_region_outline(hue, share)


def hdr_pick(x, y, band, stops):
    """The spot is (saturation, value, exposure) in Display P3; `exposure` is the multiple of white."""
    saturation = clamp(x / ACROSS)
    if y < 0.0:
        return saturation, 1.0, 2.0 ** (stops * clamp(-y / max(band, TINY)))
    return saturation, clamp(1.0 - y / SQUARE), 1.0


def hdr_mark(spot, band, stops):
    saturation, value, exposure = spot
    if exposure > 1.0:
        return saturation * ACROSS, -band * min(math.log2(exposure) / stops, 1.0)
    return saturation * ACROSS, (1.0 - value) * SQUARE


def hdr_locate(rgb, hue):
    found, saturation, value, exposure = hdr_color.p3_hsv_from_extended(*rgb)
    if saturation > FAINT and value > FAINT:
        hue = found
    return hue, (saturation, value, exposure)


def hdr_color_of(hue, spot):
    return hdr_color.extended_from_p3_hsv(hue, spot[0], spot[1], spot[2])


def hdr_shade(hue):
    return pack_color(*hdr_color.extended_from_p3_hsv(hue, 1.0, 1.0))


SELECTORS = {
    SRGB: Selector(srgb_bake, srgb_pick, srgb_mark, srgb_locate, srgb_color, srgb_shade),
    SRGB_PLUS: Selector(plus_bake, plus_pick, plus_mark, plus_locate, plus_color, plus_shade),
    HDR: Selector(hdr_bake, hdr_pick, hdr_mark, hdr_locate, hdr_color_of, hdr_shade)
}


def caption(color):
    """The text under the channels: the hex code of an sRGB colour, else how bright it is (the
    multiple of white its brightest channel reaches, and that in nits on this display) and which
    gamut it needs."""
    rgb = color[:3]
    if all(0.0 <= channel <= 1.0 for channel in rgb):
        return '#' + ''.join(f'{int(round(channel * 255.0)):02x}' for channel in color)
    peak = max(hdr_color.srgb_to_linear(channel) for channel in rgb)
    gamut = 'sRGB'
    if min(rgb) < 0.0:
        gamut = 'P3'
    return f'{peak:.1f}x  {peak * white():.0f} nits  {gamut}'


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
    baked = None                # (selector, hue, stops, band) of the pixels the texture holds
    outline = ()                # the line `bake` gave to draw over them

    def show(self, selector, hue, stops, band):
        """The texture id, holding the field `selector` lays out for `hue` with a band `band`
        pixels high that covers `stops`. Up to 2 ms when one of them changed, nothing otherwise."""
        recipe = (selector, hue, stops, band)
        if self.baked == recipe:
            return self.texture
        from meltygui.core.graphics import desktop_gl as gl
        pixels, self.outline = SELECTORS[selector].bake(hue, band, stops)
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
@gui(cursor=mouse_cursor.HAND, no_save=('picture', 'owned'), width=ACROSS)
def field(input_value: tuple, picture: Picture, selector=HDR, hue=0.0, stops=0.0, band=0.0, owned=False, width=0.0,
          height=0.0, draw_state=None, cache=None, event=None):
    """The picking area of `selector`. Its value is that selector's spot. A press or a drag moves
    it, also from outside the view.

    `band` and `stops` are the HDR band's height and range (`scale`). What `height` leaves under
    the band is the square: lower than `SQUARE`, its top rows are cut off."""
    pressed = event.left_mouse_down()
    dragged = event.left_mouse_drag()
    kind = SELECTORS[selector]
    spot = input_value
    hidden = SQUARE - (height - band)       # rows of the square cut off at the top
    spread = max(stops, TINY)               # never 0: the selectors divide by it
    if pressed or dragged:
        # The pointer from the whole square's top edge, as a selector counts it; not above the field.
        spot = tuple(kind.pick(event.x, max(event.y, 0.0) - band + hidden, band, spread))
    if not owned:
        # `Picture()` was built for this view: it ends with it.
        cache.on_retire(cache.current, picture.close)
        draw_state.owned = True
    if height < 1.0:
        return spot != input_value, spot
    draw_list = imgui.get_window_draw_list()
    texture = picture.show(selector, hue, stops, band)
    whole_height = band + SQUARE
    draw_list.add_image(texture, (0.0, 0.0), (width, height), (0.0, hidden / whole_height), (1.0, 1.0))
    if selector == SRGB_PLUS:
        draw_list.add_line(SQUARE, 0.0, SQUARE, height, SEAM, 1.0)   # sRGB left, P3 right
    if band > 0.0:
        draw_list.add_line(0.0, band, width, band, SEAM, 1.0)        # HDR above, SDR below
    # The line the selector drew over its picture (`HDR`: around what sRGB reaches).
    for at in range(len(picture.outline) - 1):
        left, top = picture.outline[at]
        right, bottom = picture.outline[at + 1]
        draw_list.add_line(left * width, top * whole_height - hidden, right * width, bottom * whole_height - hidden,
                           SEAM, 1.0)
    across, down = kind.mark(spot, band, spread)
    down = min(max(band + down - hidden, 0.0), height)              # a colour brighter than the field: its top edge
    draw_list.add_circle(across, down, 6.0, DARK, thickness=1.0)
    draw_list.add_circle(across, down, 5.0, LIGHT, thickness=1.5)
    return spot != input_value, spot


@gui(cursor=mouse_cursor.RESIZE_NS, width=BAR)
def bar(input_value: float, selector=HDR, width=0.0, height=0.0, event=None):
    """The hue of the field, 0.0 at the top to 1.0 at the bottom. A press or a drag sets it.
    Each hue is shown as the most vivid colour `selector` has for it (its `shade`)."""
    pressed = event.left_mouse_down()
    dragged = event.left_mouse_drag()
    hue = input_value
    if pressed or dragged:
        hue = min(max(event.y / max(height, 1.0), 0.0), 1.0)
    draw_list = imgui.get_window_draw_list()
    shade = SELECTORS[selector].shade
    step = height / SHADES
    above = shade(0.0)
    for at in range(SHADES):
        below = shade((at + 1) / SHADES)
        draw_list.add_rect_filled_multicolor(0.0, at * step, width, (at + 1) * step, above, above, below, below)
        above = below
    mark = hue * height
    draw_list.add_rect(0.5, mark - 2.0, width - 0.5, mark + 2.0, LIGHT, 0.0, 0, 1.5)
    return hue != input_value, hue


# --- the public views --------------------------------------------------------
@gui(listen=(), width=extent(3)[0])
def draw_color_picker(input_value: tuple, selector=HDR, headroom=PEAK, hue=0.0, spot=None, shown=None, width=0.0, height=0.0,
                      draw_state=None):
    """The picker of a colour `(r, g, b[, a])`. `selector` is the field it picks from:
    `meltygui.HDR`, `meltygui.SRGB_PLUS` or `meltygui.SRGB` (the module docstring). `headroom` is the brightest colour it shows as a
    multiple of white, 0.0 to `PEAK`: the field is to scale, so less makes the HDR quadrants
    lower, and under 1.0 the square too (`scale`). Give it the size
    `extent(len(colour), headroom, selector)` says, or
    `height=None` to wrap it; it only paints, so its empty space
    moves the window it is in.

    Its box first, since the parts lie on it. Then input before drawing: the channels, then where the colour stands, then the bar and the
    field, so each part is drawn from what the ones before it changed."""
    color = whole(input_value)
    kind = SELECTORS[selector]
    band, square, stops, top = scale(headroom, selector)
    lowest = LOWEST
    if selector == SRGB:
        lowest = 0.0
    table = look(TINT)
    draw_list = imgui.get_window_draw_list()
    across, down = extent(len(color), headroom, selector)
    # The swatch and the text under the channels are draw list shapes, which a wrapping size
    # (`height=None`) does not measure: this reserves the picker's whole extent.
    imgui.dummy(across, down)
    box(draw_list, table, width, min(height, down))     # first: the parts are drawn over it
    rows = PAD + band + square + GAP
    for at in range(len(color)):
        typed, number = draw_float(color[at], key=CHANNELS[at], left=PAD + LETTER, top=rows + at * LINE,
                                   width=width - 2.0 * PAD - LETTER)
        if typed and at == 3:
            color[at] = min(max(number, 0.0), 1.0)
        elif typed:
            color[at] = min(max(number, lowest), top)
    # Where the colour stands. `shown` is the selector and the colour `hue` and `spot` stand for:
    # another one (the picker just opened, a channel was typed, the caller passed something else,
    # the selector changed) is located again.
    rgb = tuple(color[:3])
    if shown != (selector, rgb):
        hue, spot = kind.locate(rgb, hue)
    turned, hue = bar(hue, name='hue', selector=selector, left=PAD + ACROSS + GAP, top=PAD, height=band + square)
    moved, spot = field(spot, name='field', selector=selector, hue=hue, stops=stops, band=band, left=PAD, top=PAD,
                        height=band + square)
    if turned or moved:
        rgb = tuple(kind.color(hue, spot))
        color[:3] = rgb
    draw_state.hue = hue
    draw_state.spot = spot
    draw_state.shown = (selector, rgb)
    middle = (ROW - imgui.get_text_line_height()) * 0.5
    for at in range(len(color)):
        draw_list.add_text(PAD, rows + at * LINE + middle, table.dim, CHANNELS[at])
    last = rows + len(color) * LINE
    chip(draw_list, PAD, last, PAD + 2.0 * ROW, last + ROW, color, table.outline)
    draw_list.add_text(PAD + 2.0 * ROW + GAP, last + middle, table.text, caption(color))
    changed = color != list(input_value)
    if not changed:
        return False, input_value
    if isinstance(input_value, list):
        input_value[:] = color
        return True, input_value
    return True, type(input_value)(color)


@gui(cursor=mouse_cursor.HAND, height=ROW)
def draw_color(input_value: tuple, selector=HDR, headroom=PEAK, width=0.0, height=0.0, event=None):
    """A chip of the colour; a press opens the picker under it. Returns (True, colour) in every
    run the picker changed it. The chip is drawn from what the picker returned."""
    pressed = event.left_mouse_down()
    color = whole(input_value)
    across, down = extent(len(color), headroom, selector)
    changed, value = draw_color_picker(input_value, name='picker', popup=pressed, selector=selector,
                                       headroom=headroom, left=0.0, top=height + GAP, width=across, height=down)
    if not changed and len(value) < len(color):
        # The picker is closed and never ran: the colour is filled up here.
        changed = True
        value = type(input_value)(color)
    chip(imgui.get_window_draw_list(), 0.0, 0.0, width, height, value, look(TINT).outline)
    return changed, value
