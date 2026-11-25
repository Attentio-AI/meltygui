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
    - `meltygui.PERCEPTUAL`: Oklch. Lightness up and chroma across, each in equal perceived steps
      and on one scale for every hue, so the field is the shape of that hue's gamut: empty where
      P3 ends, sRGB outlined, a line at white. The hue bar is Oklch hue. Turning the hue leaves
      the marker where it is; a chroma the new hue cannot reach is cut to its edge and remembered.
    - `meltygui.DYNAMIC_PERCEPTUAL`: an experiment. The same layout in JzCzhz, the perceptual
      space of absolute light, on axes that stand still: the top is `headroom` times white at full
      brightness (at most 10,000 nits). Every pixel of the field stands for one light, in nits, and
      keeps emitting exactly that as the desktop's brightness is turned: only the black edge moves
      and shows more or less of the field. For that the picture is made from what the display does
      to what it is sent (`Display`, model/display_model.py: the brightness, the compositor's
      shadow stages and the panel's primaries), which a thread follows while such a picker is shown.
  The band is to scale: `BAND` high at `PEAK` times white, lower for less `headroom`, and under
  1.0 the square itself is cut from above. One drag crosses the whole field.
- `SELECTORS`: what tells the three apart, a row of plain functions each (`Selector`). To add a
  picker, write its six functions and a row, and its name in hdr_color.py.
- `field`: the picking area, one fp16 picture. `bar`: the hue, each drawn as its most vivid colour.
- `Picture`: the field's texture, which its view makes, bakes again per hue and deletes when it retires.
- `whole`, `scale` (a `Room`), `white`, `caption`, `chip`, `extent`: a colour with all its channels, the
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
from meltygui.core.rendering.gui_any import is_default_for
from meltygui.core.rendering.gui_prototype import gui
from meltygui.core.rendering.shaped import Shaped
from meltygui.hdr_color import DYNAMIC_PERCEPTUAL, HDR, PERCEPTUAL, SRGB, SRGB_PLUS
from meltygui.model.color_model import _srgb_plus_marker
from meltygui.model.color_model import _srgb_plus_pick
from meltygui.model import display_model
from meltygui.model.display_model import Display
from meltygui.view.common.value_view import PAD, ROUND, ROW, SLATE, box, draw_float, look

# Levers: sizes in logical pixels.
PEAK = 32.0                     # the brightest colour a picker can show, in multiples of white: 5 stops
SQUARE = 180.0                  # side of the sRGB square
WIDE = 34.0                     # width of the P3 quadrants right of it
ACROSS = SQUARE + WIDE          # width of a field at its own size: `HDR` and `SRGB` lay their one square over all of it
NARROWEST = 24.0                # the least a field that wraps its shape is wide
LEAST = 150.0                   # the least a picker that wraps is wide: room for a channel's number and the caption
SURVEY = (32, 64)               # rows and columns of the coarse field a shape's width is read from (0.1 ms)
TEXELS = 180                    # side of the `HDR` field's picture, which is stretched over the field
GRAIN = 1.0                     # pixels of the `PERCEPTUAL` field per texel: 2.5 ms a bake at 1.0, 0.7 ms at 2.0
DYNAMIC_GRAIN = 1.5             # the same for `DYNAMIC_PERCEPTUAL`: 3.3 ms a bake at 1.5, 1.8 ms at 2.0
EDGE = 32                       # samples along a row in each of the two passes that find where its gamut ends
DYNAMIC_REACH = 0.36            # JzCzhz chroma at the right edge of its field: the most P3 has at 10,000 nits
CEILING = 10000.0               # nits at which Jzazbz ends (PQ): its field shows nothing brighter
NOMINAL = 203.0                 # nits of white the `DYNAMIC_PERCEPTUAL` hue bar's colours are found at
REACH = 0.4                     # Oklch chroma at the right edge of the `PERCEPTUAL` field, at white's lightness
TURN = 2.0 * math.pi            # a hue of 1.0 as an Oklch angle
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
SEAM = (1.0, 1.0, 1.0, 0.9)           # the lines over a field: the seams of its parts, the edge of sRGB
UNDER = (0.0, 0.0, 0.0, 0.55)         # the wider line under each, which shows it on a bright part
SEAM_WIDTH = 1.0
UNDER_WIDTH = 3.0
LIGHT = (1.0, 1.0, 1.0)               # the markers, and a chip's light checker squares
DARK = (0.0, 0.0, 0.0)                # the markers' outer ring
GREY = (0.6, 0.6, 0.6)                # a chip's dark checker squares
SHADES = 24                     # segments of the hue bar; more follow the P3 edge more closely


def white():
    """Nits of a colour of 1.0, for the caption only: the desktop's white where the compositor
    says (Toggles.HDR.follow_desktop_white), else Toggles.HDR.pq_reference_nits, a guess."""
    from meltygui.core.graphics import wayland_color
    return wayland_color.reference_nits() or wayland_color.desired_reference()


Room = namedtuple('Room', 'band square stops top headroom full white tone panel limit carry')


def primaries(display):
    """The matrices from linear sRGB to the panel's own channels and back, as tuples. The panel's
    primaries are what `display` read of it (the compositor's stages run in those), else the
    compositor's description of the output this window is on, else BT.2020, what an HDR signal is in."""
    from meltygui.core.graphics import wayland_color
    xy = display.primaries or wayland_color.preferred_primaries() or hdr_color.BT2020_XY
    forth = hdr_color.srgb_to_primaries(xy)
    return forth, hdr_color._mat_inv(forth)


def scale(headroom, selector=HDR, full=0.0, white=0.0, tone=None, panel=None, limit=0.0, carry=0.0):
    """The field of a picker whose brightest row is `headroom` times white (0.0 to `PEAK`, in
    linear light), to one scale for every picker and every display: a `Room`. `band` is the
    height of the HDR part, `BAND` at `PEAK` and a stop the same pixels in all of them; `stops` is
    what it covers above white. Under 1.0 there is no band and the square is cut from above:
    `square` is the height left of it, `top` the highest channel value shown (above white: of the
    band's top row). `SRGB` has no band: its headroom ends at white. The field is `band + square`
    high; `PERCEPTUAL` spreads its lightness evenly over that. The rest is the display, which
    only `DYNAMIC_PERCEPTUAL` is given: `full` and `white` are the nits of a colour of 1.0 at full
    brightness and now, `tone` the compositor's shadow stages (display_model.py) and `panel` the
    matrices from linear sRGB to the panel's own channels and back. `limit` is every selector's:
    the multiple of white past which the hardware has no light, so nothing is drawn (0.0: unknown).
    `carry` is the most the window's output can carry, as a multiple of white (0.0: no end, a float
    output; PQ ends at 10,000 nits)."""
    headroom = min(max(headroom, 0.0), PEAK)
    if selector == SRGB:
        headroom = min(headroom, 1.0)
    top = hdr_color.linear_to_srgb(headroom)
    if headroom > 1.0:
        stops = math.log2(headroom)
        band = float(round(BAND * stops / math.log2(PEAK)))
        return Room(band, SQUARE, stops, top, headroom, full, white, tone, panel, limit, carry)
    return Room(0.0, float(round(SQUARE * top)), 0.0, top, headroom, full, white, tone, panel, limit, carry)


def extent(channels, headroom=PEAK, selector=HDR):
    """(width, height) of the picker of a colour with `channels` channels (3, or 4 with alpha)
    that reaches `headroom` times white with `selector`, at the field's own size: what a picker
    that wraps is, where its field is colour from edge to edge. A popup is given this."""
    room = scale(headroom, selector)
    width = PAD + ACROSS + GAP + BAR + PAD
    height = PAD + room.band + room.square + GAP + channels * LINE + ROW + PAD
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
# One row of `SELECTORS` each. `x` and `y` are pixels in the field from its top left corner, `room`
# is the field's `Room` (`scale`), a `spot` is the selector's own coordinates and `hue` the bar's 0.0 to 1.0.
#   bake(hue, room)        -> (pixels, lines): the field in linear light, row 0 on top, clear where the
#                             hardware has no light (`room.limit`: `shorn`); and the
#                             lines to draw over it, each a list of points as fractions of the field
#   pick(x, y, hue, room)  -> spot
#   mark(spot, room)       -> (x, y)
#   locate(rgb, hue, room) -> (hue, spot); `hue` is the bar's now, kept for grey and black
#   color(hue, spot, room) -> (r, g, b)
#   shade(hue)             -> the packed colour the hue bar shows for `hue`
#   span(hue, room)        -> the share of the field's width that has colours in it, from the left:
#                             the part of it a picker shows
#   aspect(room)           -> width over height of the whole field when it is not distorted
Selector = namedtuple('Selector', 'bake pick mark locate color shade span aspect')


def sunk(room):
    """Rows of the whole sRGB / P3 square that are cut off above the field (a headroom under 1.0)."""
    return SQUARE - room.square


def deep(y, room):
    """`y` of the field counted from the whole square's top edge: negative in the band."""
    return y - room.band + sunk(room)


def spread(room):
    """The band's stops, never 0: the square selectors divide by them."""
    return max(room.stops, TINY)


def level(room):
    """Where the band meets the square, as the line a square selector draws over its picture."""
    if room.band <= 0.0:
        return ()
    share = room.band / (room.band + room.square)
    return ([(0.0, share), (1.0, share)],)


def all_of_it(hue, room):
    """The span of a selector whose field is colour from edge to edge."""
    return 1.0


def own_aspect(room):
    """The aspect of a selector that has no measure of its own: its field at the size it was drawn for."""
    return ACROSS / max(room.band + room.square, TINY)


# `SRGB`: the classic square. A spot is (saturation, value).
def srgb_bake(hue, room):
    pixels = hdr_color.srgb_plus_linear(hue, int(SQUARE), 0, 0, 0.0)
    return shorn(pixels[int(sunk(room)):], room), ()


def srgb_pick(x, y, hue, room):
    return clamp(x / ACROSS), clamp(1.0 - deep(y, room) / SQUARE)


def srgb_mark(spot, room):
    return spot[0] * ACROSS, (1.0 - spot[1]) * SQUARE - sunk(room)


def srgb_locate(rgb, hue, room=None):
    found, saturation, value = colorsys.rgb_to_hsv(clamp(rgb[0]), clamp(rgb[1]), clamp(rgb[2]))
    if saturation > FAINT and value > FAINT:
        hue = found
    return hue, (saturation, value)


def srgb_color(hue, spot, room=None):
    return colorsys.hsv_to_rgb(hue, spot[0], spot[1])


def srgb_shade(hue):
    return colorsys.hsv_to_rgb(hue, 1.0, 1.0)


# `SRGB_PLUS`: that square, P3 right of it, HDR above both. A spot is (saturation, depth, value,
# exposure): `depth` is how far into the P3 part it is (0.0 inside the square), `exposure` the
# multiple of white (1.0 under the band).
def plus_bake(hue, room):
    pixels = hdr_color.srgb_plus_linear(hue, int(SQUARE), int(WIDE), int(room.band), spread(room))
    seam = SQUARE / ACROSS
    return shorn(pixels[int(sunk(room)):], room), ([(seam, 0.0), (seam, 1.0)],) + level(room)


def plus_pick(x, y, hue, room):
    return _srgb_plus_pick(x, deep(y, room), SQUARE, WIDE, room.band, spread(room))


def plus_mark(spot, room):
    x, y = _srgb_plus_marker(spot[0], spot[1], spot[2], spot[3], SQUARE, WIDE, room.band, spread(room))
    return x, y + room.band - sunk(room)


def plus_locate(rgb, hue, room=None):
    """`hue` also seeds the search for a colour outside sRGB."""
    found, saturation, value, depth, exposure = hdr_color.srgb_extension_coords(*rgb, hue_hint=hue)
    if saturation > FAINT and value > FAINT:
        hue = found
    return hue, (saturation, depth, value, exposure)


def plus_color(hue, spot, room=None):
    saturation, depth, value, exposure = spot
    return hdr_color.extended_from_srgb_extension(hue, saturation, value, depth, exposure)


def plus_shade(hue):
    """Full saturation and value at the edge of P3: the far corner of the square's top row."""
    return hdr_color.extended_from_srgb_extension(hue, 1.0, 1.0, 1.0)


# `HDR`: one Display P3 square under the band. A spot is (saturation, value, exposure).
def hdr_bake(hue, room):
    whole_height = room.band + SQUARE
    share = room.band / whole_height
    pixels = hdr_color.wide_square_linear(hue, TEXELS, share, spread(room))
    # What sRGB reaches, from fractions of the whole picture to fractions of the field.
    shown = room.band + room.square
    outline = [(x, max(y * whole_height - sunk(room), 0.0) / shown)
               for x, y in hdr_color.srgb_region_outline(hue, share)]
    return shorn(pixels[int(TEXELS * sunk(room) / whole_height):], room), (outline,) + level(room)


def hdr_pick(x, y, hue, room):
    saturation = clamp(x / ACROSS)
    y = deep(y, room)
    if y < 0.0:
        return saturation, 1.0, 2.0 ** (spread(room) * clamp(-y / max(room.band, TINY)))
    return saturation, clamp(1.0 - y / SQUARE), 1.0


def hdr_mark(spot, room):
    saturation, value, exposure = spot
    if exposure > 1.0:
        return saturation * ACROSS, room.band * (1.0 - min(math.log2(exposure) / spread(room), 1.0))
    return saturation * ACROSS, (1.0 - value) * SQUARE + room.band - sunk(room)


def hdr_locate(rgb, hue, room=None):
    found, saturation, value, exposure = hdr_color.p3_hsv_from_extended(*rgb)
    if saturation > FAINT and value > FAINT:
        hue = found
    return hue, (saturation, value, exposure)


def hdr_color_of(hue, spot, room=None):
    return hdr_color.extended_from_p3_hsv(hue, spot[0], spot[1], spot[2])


def hdr_shade(hue):
    return hdr_color.extended_from_p3_hsv(hue, 1.0, 1.0)


# `PERCEPTUAL`: Oklch. A spot is (chroma, lightness): the chroma asked for, which `perceptual_color`
# cuts to what the hue reaches. Lightness 1.0 is white; the field's top row is `summit(room)`.
VIVID = {}                      # hue -> the most vivid P3 colour of that Oklch hue, as the bar asked


def summit(room):
    """The Oklch lightness of the field's top row: of `headroom` times white."""
    return max(room.headroom, TINY) ** (1.0 / 3.0)


def perceptual_bake(hue, room):
    rows = max(int((room.band + room.square) / GRAIN), 1)
    cols = int(ACROSS / GRAIN)
    top = summit(room)
    pixels, narrow = hdr_color.oklch_field_linear(hue * TURN, rows, cols, top, REACH * top, room.headroom)
    # What sRGB reaches: the end of each row's run of sRGB texels, every few rows.
    reach = narrow.sum(1)
    outline = [(reach[row] / cols, (row + 0.5) / rows) for row in range(0, rows, 3)]
    lines = (outline,)
    if top > 1.0:
        white_row = 1.0 - 1.0 / top
        lines += ([(0.0, white_row), (1.0, white_row)],)
    return shorn(pixels, room), lines


def perceptual_pick(x, y, hue, room):
    top = summit(room)
    lightness = clamp(1.0 - y / (room.band + room.square)) * top
    chroma = clamp(x / ACROSS) * REACH * top
    return min(chroma, hdr_color.oklab_max_chroma(lightness, hue * TURN, room.headroom)), lightness


def perceptual_mark(spot, room):
    top = summit(room)
    return spot[0] / (REACH * top) * ACROSS, (1.0 - spot[1] / top) * (room.band + room.square)


def perceptual_locate(rgb, hue, room=None):
    lightness, a, b = hdr_color.linear_to_oklab(tuple(hdr_color.srgb_to_linear(channel) for channel in rgb))
    chroma = math.hypot(a, b)
    if chroma > FAINT:
        hue = math.atan2(b, a) / TURN % 1.0
    return hue, (chroma, lightness)


def perceptual_color(hue, spot, room=None):
    chroma, lightness = spot
    chroma = min(chroma, hdr_color.oklab_max_chroma(lightness, hue * TURN, PEAK))
    linear = hdr_color.oklab_to_linear((lightness, chroma * math.cos(hue * TURN), chroma * math.sin(hue * TURN)))
    return tuple(hdr_color.linear_to_srgb(channel) for channel in linear)


def perceptual_span(hue, room):
    rows, cols = SURVEY
    top = summit(room)
    pixels, narrow = hdr_color.oklch_field_linear(hue * TURN, rows, cols, top, REACH * top, room.headroom)
    return min((float(pixels[..., 3].sum(1).max()) + 1.0) / cols, 1.0)      # a texel more: the edge is inside it


def perceptual_aspect(room):
    """Chroma 0 to `REACH` times the top lightness across, lightness 0 to the top up: a step of
    chroma and a step of lightness are the same distance in Oklab, so they get the same length."""
    return REACH


def perceptual_shade(hue):
    """The colour with the most chroma P3 has at this hue, under white. Found once per hue
    (0.1 ms) on a coarse field and kept in `VIVID`."""
    found = VIVID.get(hue)
    if found is None:
        rows, cols = 32, 64
        pixels, narrow = hdr_color.oklch_field_linear(hue * TURN, rows, cols, 1.0, REACH, 1.0)
        reach = pixels[..., 3].sum(1)
        row = int(reach.argmax())
        linear = pixels[row, max(int(reach[row]) - 1, 0), :3]
        found = VIVID[hue] = tuple(hdr_color.linear_to_srgb(float(channel)) for channel in linear)
    return found


# `DYNAMIC_PERCEPTUAL`: JzCzhz, of emitted nits. A spot is (chroma Cz, lightness Jz) of the light
# the panel emits, both absolute: the same colour stands elsewhere when the brightness changes, and
# the same spot is another colour value. `sent` and `seen` are the two ways between them.
DYNAMIC_VIVID = {}              # hue -> the most vivid P3 colour of that JzCzhz hue at `NOMINAL` nits


def sent(nits, room):
    """The colour value (linear sRGB, 1.0 = white) to send for the panel to emit `nits` (linear
    sRGB, an array): the display undone, which is the brightness, and the compositor's shadow
    stages in the panel's own channels where it has them on."""
    import numpy as np
    if room.tone is None:
        return nits / room.white
    forth, back = room.panel
    asked = display_model.ask(nits @ np.asarray(forth).T, room.tone)
    return (asked @ np.asarray(back).T) / room.white


def seen(linear, room):
    """The nits (linear sRGB) the panel emits for the colour value `linear`: `sent` undone."""
    import numpy as np
    nits = np.asarray(linear, dtype=np.float64) * room.white
    if room.tone is None:
        return nits
    forth, back = room.panel
    return display_model.emit(nits @ np.asarray(forth).T, room.tone) @ np.asarray(back).T


def crest(room):
    """The Jz of the field's top row: white at `headroom`, at full brightness, at most `CEILING` nits."""
    nits = min(max(room.headroom, TINY) * room.full, CEILING)
    return hdr_color.linear_to_jzazbz((1.0, 1.0, 1.0), nits)[0]


BOUNDLESS = 1e9                 # a value to send that no output is asked to carry


def bounds(room, headroom):
    """(send, light) of the dynamic field: the most that can be sent, as a multiple of white, and
    the most light in nits it shows.

    The light is `headroom` times white AT FULL BRIGHTNESS, and the hardware's limit where that
    is known: it does not follow the brightness, since a display turned down can still be sent
    more. What can be sent ends only where the window's output does (`room.carry`): a float
    output has no end, PQ ends at 10,000 nits before the display's brightness is applied, which
    is what makes a PQ window's field shrink from the top as the brightness goes down."""
    send = BOUNDLESS
    if room.carry > 0.0:
        send = room.carry
    light = headroom * room.full
    if room.limit > 0.0:
        light = min(light, room.limit * room.white)
    return send, light


def brink(lightness, hue, room, headroom):
    """The most chroma the panel can emit at this lightness and hue, within `bounds`. Two passes
    of `EDGE` samples along the row, the second between the last one inside and the first one
    outside."""
    import numpy as np
    to_p3 = np.asarray(hdr_color.SRGB_TO_P3).T
    send, light = bounds(room, headroom)
    low = 0.0
    high = DYNAMIC_REACH
    for _ in range(2):
        chromas = np.linspace(low, high, EDGE)
        nits, over = hdr_color.jzczhz_nits(lightness, chromas, hue * TURN)
        # The floor and the ceiling on the light itself, and on what is sent for it where the
        # output ends (jzczhz_field_linear).
        emitted = nits @ to_p3
        inside = hdr_color.above_zero(emitted) & (emitted.max(-1) <= light * (1.0 + 1e-4)) & ~over
        if send < BOUNDLESS:
            inside &= (sent(nits, room) @ to_p3).max(-1) <= send * (1.0 + 1e-4)
        if not inside[0]:
            return low
        if inside.all():
            return high
        first_outside = int(inside.argmin())
        low = float(chromas[first_outside - 1])
        high = float(chromas[first_outside])
    return low


def dynamic_bake(hue, room):
    rows = max(int((room.band + room.square) / DYNAMIC_GRAIN), 1)
    cols = int(ACROSS / DYNAMIC_GRAIN)
    top = crest(room)
    # The limits are on the light a place stands for, in nits, not on the value sent for it (the
    # two differ under the compositor's stages, more the further the brightness is turned up).
    send, light = bounds(room, room.headroom)
    pixels, narrow = hdr_color.jzczhz_field_linear(hue * TURN, rows, cols, top, DYNAMIC_REACH, room.white, send,
                                                   lambda nits: sent(nits, room), light)
    reach = narrow.sum(1)
    outline = [(reach[row] / cols, (row + 0.5) / rows) for row in range(0, rows, 3)]
    lines = (outline,)
    # Where a colour of 1.0 stands now: the light the panel emits for white.
    white_light = hdr_color.linear_to_jzazbz(tuple(seen((1.0, 1.0, 1.0), room)), 1.0)[0]
    white_row = 1.0 - white_light / top
    if white_row > 0.0:
        lines += ([(0.0, white_row), (1.0, white_row)],)
    return pixels, lines


def dynamic_pick(x, y, hue, room):
    lightness = clamp(1.0 - y / (room.band + room.square)) * crest(room)
    chroma = clamp(x / ACROSS) * DYNAMIC_REACH
    return min(chroma, brink(lightness, hue, room, room.headroom)), lightness


def dynamic_mark(spot, room):
    return spot[0] / DYNAMIC_REACH * ACROSS, (1.0 - spot[1] / crest(room)) * (room.band + room.square)


def dynamic_locate(rgb, hue, room):
    linear = tuple(hdr_color.srgb_to_linear(channel) for channel in rgb)
    lightness, a, b = hdr_color.linear_to_jzazbz(tuple(seen(linear, room)), 1.0)
    chroma = math.hypot(a, b)
    if chroma > FAINT:
        hue = math.atan2(b, a) / TURN % 1.0
    return hue, (chroma, lightness)


def dynamic_color(hue, spot, room):
    import numpy as np
    chroma, lightness = spot
    chroma = min(chroma, brink(lightness, hue, room, PEAK))
    jab = (lightness, chroma * math.cos(hue * TURN), chroma * math.sin(hue * TURN))
    linear = sent(np.asarray(hdr_color.jzazbz_to_linear(jab, 1.0)), room)
    return tuple(hdr_color.linear_to_srgb(float(channel)) for channel in linear)


def dynamic_span(hue, room):
    rows, cols = SURVEY
    send, light = bounds(room, room.headroom)
    pixels, narrow = hdr_color.jzczhz_field_linear(hue * TURN, rows, cols, crest(room), DYNAMIC_REACH, room.white, send,
                                                   lambda nits: sent(nits, room), light)
    return min((float(pixels[..., 3].sum(1).max()) + 1.0) / cols, 1.0)


def dynamic_aspect(room):
    """Chroma 0 to `DYNAMIC_REACH` across, lightness 0 to the crest up, one length for a unit of each."""
    return DYNAMIC_REACH / max(crest(room), TINY)


def dynamic_shade(hue):
    """The colour with the most chroma P3 has at this hue under a white of `NOMINAL` nits. Found
    once per hue on a coarse field and kept in `DYNAMIC_VIVID`."""
    found = DYNAMIC_VIVID.get(hue)
    if found is None:
        rows, cols = 32, 64
        top = hdr_color.linear_to_jzazbz((1.0, 1.0, 1.0), NOMINAL)[0]
        pixels, narrow = hdr_color.jzczhz_field_linear(hue * TURN, rows, cols, top, DYNAMIC_REACH, NOMINAL, 1.0)
        reach = pixels[..., 3].sum(1)
        row = int(reach.argmax())
        linear = pixels[row, max(int(reach[row]) - 1, 0), :3]
        found = DYNAMIC_VIVID[hue] = tuple(hdr_color.linear_to_srgb(float(channel)) for channel in linear)
    return found


SELECTORS = {
    SRGB: Selector(srgb_bake, srgb_pick, srgb_mark, srgb_locate, srgb_color, srgb_shade, all_of_it, own_aspect),
    SRGB_PLUS: Selector(plus_bake, plus_pick, plus_mark, plus_locate, plus_color, plus_shade, all_of_it, own_aspect),
    HDR: Selector(hdr_bake, hdr_pick, hdr_mark, hdr_locate, hdr_color_of, hdr_shade, all_of_it, own_aspect),
    PERCEPTUAL: Selector(perceptual_bake, perceptual_pick, perceptual_mark, perceptual_locate, perceptual_color,
                         perceptual_shade, perceptual_span, perceptual_aspect),
    DYNAMIC_PERCEPTUAL: Selector(dynamic_bake, dynamic_pick, dynamic_mark, dynamic_locate, dynamic_color,
                                 dynamic_shade, dynamic_span, dynamic_aspect)
}


def fit(shape, room_width, room_height, own_height, wraps_width, wraps_height):
    """(width, height) of a field whose width over height is `shape`, as large as the room lets
    it be without distorting it. Wrap means wrap: a height that wraps is `own_height`, the
    picker's fixed least, whatever the width; a width that wraps is what the height's shape
    needs. Only a size that was given is filled. The field never leaves the room."""
    if shape <= 0.0 or own_height <= 0.0:
        return NARROWEST, 0.0
    if wraps_height:
        height = own_height
    elif wraps_width:
        height = room_height
    else:
        height = room_width / shape
    height = min(height, room_height)
    width = height * shape
    if width > room_width:
        width = room_width
        height = width / shape
    return max(width, 1.0), height


def shorn(pixels, room):
    """A baked field without what is brighter than the hardware has (`room.limit`): what a
    selector whose pixels are what the panel emits returns from its `bake`."""
    if room.limit > 0.0:
        return within(pixels, room.limit)
    return pixels


def within(pixels, limit):
    """`pixels` (a baked field) with every texel made clear whose brightest P3 channel is more
    than `limit` times white: light the hardware does not have is not drawn."""
    import numpy as np
    wide = pixels[..., :3] @ np.asarray(hdr_color.SRGB_TO_P3, dtype=np.float32).T
    pixels[..., 3] *= wide.max(-1) <= limit * (1.0 + 1e-4)
    return pixels


def caption(color, nits=None):
    """The text under the channels: the hex code of an sRGB colour, else how bright it is (the
    multiple of white its brightest P3 channel reaches, and that in nits where white is `nits`:
    this display's when none is given) and which gamut it needs."""
    if nits is None:
        nits = white()
    rgb = color[:3]
    if all(0.0 <= channel <= 1.0 for channel in rgb):
        return '#' + ''.join(f'{int(round(channel * 255.0)):02x}' for channel in color)
    # The brightest of the display's own primaries: what the panel is asked for.
    peak = max(hdr_color.linear_srgb_to_p3(tuple(hdr_color.srgb_to_linear(channel) for channel in rgb)))
    gamut = 'sRGB'
    if min(rgb) < 0.0:
        gamut = 'P3'
    return f'{peak:.1f}x  {peak * nits:.0f} nits  {gamut}'


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
    draw_list.add_rect_filled(left, top, right, bottom, (color[0], color[1], color[2], alpha), ROUND)
    draw_list.add_rect(left + 0.5, top + 0.5, right - 0.5, bottom - 0.5, outline, ROUND, 0, 1.0)


class Picture:
    """The field for one hue as an fp16 texture of linear light, so every texel is the real wide
    or bright colour (a vertex colour has 255 steps over its whole range). Its view owns it:
    `show` makes and bakes it, `close` deletes it (`cache.on_retire`). Both run on the render thread."""
    texture = 0
    baked = None                # (selector, hue, room) of the pixels the texture holds
    lines = ()                  # the lines `bake` gave to draw over them

    def show(self, selector, hue, room):
        """The texture id, holding the field `selector` lays out for `hue` in `room`. Up to 2 ms
        when one of them changed, nothing otherwise."""
        recipe = (selector, hue, room)
        if self.baked == recipe:
            return self.texture
        from meltygui.core.graphics import desktop_gl as gl
        pixels, self.lines = SELECTORS[selector].bake(hue, room)
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
@gui(cursor=mouse_cursor.HAND, no_save=('picture', 'owned', 'room'))
def field(input_value: tuple, picture: Picture, room: Room = None, selector=HDR, hue=0.0, span=1.0, owned=False,
          width=0.0, height=0.0, draw_state=None, cache=None, event=None):
    """The picking area of `selector` in `room` (what `scale` gave its caller, who keeps it, so
    that it is one object from run to run). Its value is that selector's spot. A press or a drag
    moves it, also from outside the view.

    It fills whatever size it is given (its caller keeps that to the selector's aspect): the
    selector works in its own pixels, `ACROSS` wide and as high as the room, and the pointer and
    the marker are scaled between those and the view's.
    `span` is the share of the picture's width that is shown, from the left."""
    pressed = event.left_mouse_down()
    dragged = event.left_mouse_drag()
    kind = SELECTORS[selector]
    own_width = ACROSS * span
    own_height = max(room.band + room.square, TINY)
    spot = input_value
    if pressed or dragged:
        x = clamp(event.x / max(width, 1.0)) * own_width
        y = clamp(event.y / max(height, 1.0)) * own_height
        spot = tuple(kind.pick(x, y, hue, room))
    if not owned:
        # `Picture()` was built for this view: it ends with it.
        cache.on_retire(cache.current, picture.close)
        draw_state.owned = True
    if height < 1.0:
        return spot != input_value, spot
    draw_list = imgui.get_window_draw_list()
    draw_list.add_image(picture.show(selector, hue, room), (0.0, 0.0), (width, height), (0.0, 0.0), (span, 1.0))
    # What the selector drew over its picture: the seams of its parts, the edge of sRGB.
    # Each twice: a wide dark line, then the light one on it, so it shows on white and on black.
    draw_list.push_clip_rect(0.0, 0.0, width, height, True)
    for color, thickness in ((UNDER, UNDER_WIDTH), (SEAM, SEAM_WIDTH)):
        for line in picture.lines:
            for at in range(len(line) - 1):
                left, top = line[at]
                right, bottom = line[at + 1]
                draw_list.add_line(left / span * width, top * height, right / span * width, bottom * height, color,
                                   thickness)
    draw_list.pop_clip_rect()
    across, down = kind.mark(spot, room)
    across = min(max(across / own_width * width, 0.0), width)       # a colour outside the field: at its edge
    down = min(max(down / own_height * height, 0.0), height)
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
@is_default_for(Shaped(tuple, (3,), float))
@gui(listen=(), no_save=('display', 'watching', 'room'))
def draw_color_picker(input_value: tuple, display: Display, selector=HDR, headroom=PEAK, hardware_max=None,
                      sdr_white_max=None, hue=0.0, spot=None, shown=None, watching=False, room=None, width=0.0,
                      height=0.0, canvas=None, draw_state=None, cache=None):
    """The picker of a colour `(r, g, b[, a])`. `selector` is the field it picks from:
    `meltygui.HDR`, `meltygui.SRGB_PLUS`, `meltygui.SRGB`, `meltygui.PERCEPTUAL` or
    `meltygui.DYNAMIC_PERCEPTUAL` (the module docstring). `headroom` is the brightest colour it shows as a
    multiple of white, 0.0 to `PEAK`: the field is to scale, so less makes the HDR quadrants
    lower, and under 1.0 the square too (`scale`).

    Two facts about the display can be passed in nits where they are known. `sdr_white_max` is
    SDR white at full brightness, for a display that does not say (it is read from the compositor
    where that tells, and guessed from Toggles.HDR.pq_reference_nits otherwise): the caption's
    nits and the dynamic selector use it. `hardware_max` is the brightest the panel gets: no
    selector draws what is brighter, so the field ends where the hardware does.

    Its size is any view's: a number, nothing (it fills its parent) or `None` (it wraps), for
    width and height each. The field is never distorted: it keeps the selector's aspect (for the
    perceptual ones a step of chroma is as long as a step of lightness) and shows the part of its
    width that has colours for the hue, as large as a size that was given lets it be. The hue bar
    stands at the picker's right edge, which follows the shape when the width wraps. Wrap means wrap: a height that wraps is the field's own (`scale`), the
    picker's fixed least, and a width that wraps is as wide as that shape. It only paints, so its empty space moves the window it is in.

    Its box first, since the parts lie on it. Then input before drawing: the channels, then where
    the colour stands, then the bar and the field, so each part is drawn from what the ones
    before it changed."""
    color = whole(input_value)
    kind = SELECTORS[selector]
    # The display, for the one selector that draws from it. Its `Display` is followed by a thread
    # only while that selector is shown, and wakes this view when a reading differed.
    # Nits of a colour of 1.0 at full brightness: the caller's word, else what the display says.
    full_white = sdr_white_max
    if full_white is None:
        full_white = white()
    now_white = full_white                  # ... and as the panel shows it now, where that is known
    full = 0.0
    emitted = 0.0
    tone = None
    panel = None
    if selector == DYNAMIC_PERCEPTUAL:
        if not watching:
            node = cache.current
            display.read()
            display.watch(lambda: cache.wake(node))
            cache.on_retire(node, display.close)
            draw_state.watching = True
        full = full_white
        if sdr_white_max is None and display.white > 0.0:
            full = display.white            # the compositor's own reading
        emitted = full * display.gain
        now_white = emitted
        tone = display.tone()
        if tone is not None:
            panel = primaries(display)
    elif watching:
        display.close()
        draw_state.watching = False
    # One `Room` object for as long as it says the same: the field is passed it, and a new one
    # would run the field on every run of this view.
    limit = 0.0
    if hardware_max is not None:
        limit = hardware_max / max(now_white, TINY)
    carry = 0.0
    if selector == DYNAMIC_PERCEPTUAL:
        # Where this window's own output ends. PQ carries 10,000 nits, counted where 1.0 is the
        # white Melty encodes with; a float output (scene_target) carries anything.
        from meltygui.core.graphics import wayland_color
        if wayland_color.applied() == 'pq':
            carry = CEILING / max(white(), TINY)
    found = scale(headroom, selector, full, emitted, tone, panel, limit, carry)
    if found != room:
        room = found
        draw_state.room = room
    lowest = LOWEST
    if selector == SRGB:
        lowest = 0.0
    # Where the colour stands. `shown` is the selector, the colour and the room that `hue` and
    # `spot` stand for: another one (the picker just opened, the caller passed something else, the
    # selector or the display changed) is located again. Before the layout: a shape follows the hue.
    rgb = tuple(color[:3])
    if shown != (selector, rgb, room):
        hue, spot = kind.locate(rgb, hue, room)
        shown = (selector, rgb, room)
    # The layout. Under the field: the channels, the swatch and its caption. The field is the
    # shape of its colours (the selector's aspect, and the part of its width that has any), as
    # large as the rest of the size lets it be; an axis that wraps takes the field's size.
    count = len(color)
    under = GAP + count * LINE + ROW
    wraps_width = canvas.width is None
    wraps_height = canvas.height is None
    room_width = max(width - 2.0 * PAD - GAP - BAR, 1.0)
    room_height = max(height - 2.0 * PAD - under, 0.0)
    own_height = room.band + room.square
    span = kind.span(hue, room)
    field_width, field_height = fit(kind.aspect(room) * span, room_width, room_height, own_height, wraps_width,
                                    wraps_height)
    across = width
    if wraps_width:
        across = max(PAD + field_width + GAP + BAR + PAD, LEAST)
    down = height
    if wraps_height:
        down = PAD + field_height + under + PAD
    table = look(TINT)
    draw_list = imgui.get_window_draw_list()
    # The swatch and the text under the channels are draw list shapes, which a wrapping size does
    # not measure: this reserves the picker's whole extent.
    imgui.dummy(across, down)
    box(draw_list, table, across, down)                 # first: the parts are drawn over it
    rows = PAD + field_height + GAP
    for at in range(count):
        typed, number = draw_float(color[at], key=CHANNELS[at], left=PAD + LETTER, top=rows + at * LINE,
                                   width=across - 2.0 * PAD - LETTER)
        if typed and at == 3:
            color[at] = min(max(number, 0.0), 1.0)
        elif typed:
            color[at] = min(max(number, lowest), room.top)
    # A channel was typed: the colour stands elsewhere, and its hue may have another shape.
    rgb = tuple(color[:3])
    if shown != (selector, rgb, room):
        before = hue
        hue, spot = kind.locate(rgb, hue, room)
        if hue != before:
            cache.wake_next(cache.current)
    # The bar stands at the picker's right edge, whatever the field leaves free beside it. Wrapped,
    # that edge follows the shape, and so does the bar.
    turned, hue = bar(hue, name='hue', selector=selector, left=across - PAD - BAR, top=PAD, height=field_height)
    if turned:
        # Another hue, another shape: the field takes its size now, and the next run puts the bar,
        # the channels and the box where that leaves them.
        after = kind.span(hue, room)
        if after != span:
            span = after
            field_width, field_height = fit(kind.aspect(room) * span, room_width, room_height, own_height,
                                            wraps_width, wraps_height)
            cache.wake_next(cache.current)
    moved, spot = field(spot, name='field', room=room, selector=selector, hue=hue, span=span, left=PAD, top=PAD,
                        width=field_width, height=field_height)
    if turned or moved:
        rgb = tuple(kind.color(hue, spot, room))
        color[:3] = rgb
    draw_state.hue = hue
    draw_state.spot = spot
    draw_state.shown = (selector, rgb, room)
    middle = (ROW - imgui.get_text_line_height()) * 0.5
    for at in range(count):
        draw_list.add_text(PAD, rows + at * LINE + middle, table.dim, CHANNELS[at])
    last = rows + count * LINE
    draw_list.push_clip_rect(0.0, 0.0, across - PAD, down, True)
    chip(draw_list, PAD, last, PAD + 2.0 * ROW, last + ROW, color, table.outline)
    draw_list.add_text(PAD + 2.0 * ROW + GAP, last + middle, table.text, caption(color, full_white))
    draw_list.pop_clip_rect()
    changed = color != list(input_value)
    if not changed:
        return False, input_value
    if isinstance(input_value, list):
        input_value[:] = color
        return True, input_value
    return True, type(input_value)(color)


@is_default_for(Shaped(tuple, (3,), float))
@gui(cursor=mouse_cursor.HAND, height=ROW)
def draw_color(input_value: tuple, selector=HDR, headroom=PEAK, hardware_max=None, sdr_white_max=None, width=0.0,
               height=0.0, event=None):
    """A chip of the colour; a press opens the picker under it. Returns (True, colour) in every
    run the picker changed it. The chip is drawn from what the picker returned."""
    pressed = event.left_mouse_down()
    color = whole(input_value)
    across, down = extent(len(color), headroom, selector)
    changed, value = draw_color_picker(input_value, name='picker', popup=pressed, selector=selector,
                                       headroom=headroom, hardware_max=hardware_max, sdr_white_max=sdr_white_max,
                                       left=0.0, top=height + GAP, width=across, height=down)
    if not changed and len(value) < len(color):
        # The picker is closed and never ran: the colour is filled up here.
        changed = True
        value = type(input_value)(color)
    chip(imgui.get_window_draw_list(), 0.0, 0.0, width, height, value, look(TINT).outline)
    return changed, value
