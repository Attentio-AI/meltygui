"""`draw_color` and `draw_color_picker` (view/common/color_picker_view.py): the sRGB square, the P3 and HDR
quadrants around it, the hue bar and the chip that opens the picker as a popup.

The app is one picker (or one chip) at (10, 10). The tests drive the real router through the GL
test host, frame by frame.
"""
import pytest

from editor_host import Host
from meltygui import hdr_color
from meltygui.view.common.color_picker_view import BAND, BAR, GAP, PEAK, SQUARE, WIDE
from meltygui.hdr_color import DYNAMIC_PERCEPTUAL, HDR, PERCEPTUAL, SRGB, SRGB_PLUS
from meltygui.view.common.color_picker_view import ACROSS, LEAST, SELECTORS, bar, field
from meltygui.view.common.color_picker_view import caption, draw_color, draw_color_picker, extent, scale, white
from meltygui.view.common.value_view import PAD, ROW

LEFT, TOP = 10.0, 10.0
FIELD = (LEFT + PAD, TOP + PAD)                         # the field's corner on the surface
CORNER = (FIELD[0], FIELD[1] + BAND)                    # the sRGB square's
HUE = FIELD[0] + SQUARE + WIDE + GAP + BAR * 0.5        # an x on the hue bar


def picker(input_value=None, selector=SRGB_PLUS):
    across, down = extent(len(input_value), PEAK, selector)
    return draw_color_picker(input_value, name='picker', selector=selector, left=LEFT, top=TOP, width=across,
                             height=down)


def chip(input_value=None):
    return draw_color(input_value, name='chip', selector=SRGB_PLUS, left=LEFT, top=TOP, width=60.0)


@pytest.fixture
def show():
    hosts = []

    def make(view, color, **arguments):
        hosts.append(Host(view, input_value=color, **arguments))
        hosts[-1].frame()
        return hosts[-1]
    yield make
    for host in hosts:
        host.close()


def color(host):
    return host.arguments['input_value']


def rect(host, view):
    """(left, top, width, height) of the one `view` in the host, in its parent."""
    body = view.__gui_definition__[0]
    (node,) = [node for node, record in host.cache.records.items() if record[2] is body]
    return tuple(host.cache.graph.rect(node))


def place(host, selector, hue, room, x, y):
    """Where on the surface the selector's own pixel (x, y) is drawn: its field is scaled to the
    room it was given, and shows the part of its width that has colours."""
    left, top, width, height = rect(host, field)
    own_width = ACROSS * SELECTORS[selector].span(hue, room)
    return LEFT + left + x / own_width * width, TOP + top + y / (room.band + room.square) * height


def test_where_a_colour_stands():
    hue, spot = SELECTORS[SRGB_PLUS].locate((0.2, 0.4, 0.9), 0.0)
    assert hdr_color.extended_from_srgb_extension(hue, spot[0], spot[2], spot[1], spot[3]) == pytest.approx((0.2, 0.4, 0.9))
    assert spot[1] == 0.0 and spot[3] == 1.0                    # inside the square, under the band
    hue, spot = SELECTORS[SRGB_PLUS].locate(hdr_color.p3(1.0, 0.0, 0.0), 0.0)
    assert spot[1] == pytest.approx(1.0, abs=1e-3)              # the far edge of the P3 quadrant
    hue, spot = SELECTORS[SRGB_PLUS].locate(hdr_color.white(8.0), 0.3)
    assert hue == 0.3 and spot[3] == pytest.approx(8.0)         # white has no hue: the bar stays


def test_the_field_is_to_scale_with_the_headroom_asked_for():
    band, square, stops, top, headroom = scale(PEAK)[:5]
    assert (band, square) == (BAND, SQUARE) and 2.0 ** stops == pytest.approx(PEAK)
    assert scale(PEAK * 4.0) == scale(PEAK)                     # `PEAK` times white is the most
    middle = PEAK ** 0.5                                        # half the stops: half the band
    assert scale(middle)[0] == pytest.approx(BAND * 0.5, abs=0.5) and scale(middle)[1] == SQUARE
    assert scale(1.0)[:3] == (0.0, SQUARE, 0.0)                 # white: the square alone
    band, square, stops, top, headroom = scale(0.25)[:5]                      # under white: no band, a lower square
    assert band == 0.0 and 0.0 < square < SQUARE and top == pytest.approx(hdr_color.linear_to_srgb(0.25))
    assert scale(0.0)[:5] == (0.0, 0.0, 0.0, 0.0, 0.0)
    assert extent(3, 1.0)[1] == extent(3)[1] - BAND


def test_the_caption_is_a_hex_code_inside_srgb_and_nits_outside():
    assert caption([1.0, 0.5, 0.0]) == '#ff8000'
    assert caption([1.0, 0.5, 0.0, 1.0]) == '#ff8000ff'
    assert caption(list(hdr_color.white(4.0))) == f'4.0x  {4.0 * white():.0f} nits  sRGB'
    assert caption(list(hdr_color.p3(1.0, 0.0, 0.0))).endswith('P3')


def test_a_press_in_the_square_picks_an_srgb_colour_of_the_inputs_type(show):
    host = show(picker, (1.0, 0.0, 0.0))
    host.click(CORNER[0] + SQUARE * 0.5, CORNER[1] + SQUARE * 0.5)
    host.frame()
    assert isinstance(color(host), tuple)
    assert color(host) == pytest.approx((0.5, 0.25, 0.25), abs=0.01)


def test_a_list_is_edited_in_place_and_keeps_its_alpha(show):
    mine = [1.0, 0.0, 0.0, 0.5]
    host = show(picker, mine)
    host.click(CORNER[0] + SQUARE - 1.0, CORNER[1] + SQUARE - 1.0)      # the bottom right corner: black
    host.frame()
    assert color(host) is mine and mine == pytest.approx([0.0, 0.0, 0.0, 0.5], abs=0.01)


def test_a_drag_from_the_square_into_the_quadrants_leaves_srgb_and_white(show):
    host = show(picker, (1.0, 0.0, 0.0))
    host.frame((CORNER[0] + 90.0, CORNER[1] + 90.0), True)
    host.frame((CORNER[0] + SQUARE + WIDE, CORNER[1] + 90.0))   # the far edge of the P3 quadrant
    host.frame()
    assert min(color(host)) < 0.0 and max(color(host)) <= 1.0
    host.frame((CORNER[0] + SQUARE + WIDE, FIELD[1]))           # its top corner: P3 red at the ceiling
    host.frame()
    host.frame(down=False)
    peak = max(hdr_color.srgb_to_linear(channel) for channel in color(host))
    assert min(color(host)) < 0.0 and peak == pytest.approx(PEAK, rel=0.25)


def test_the_hue_bar_turns_a_colour_and_only_the_field_under_grey(show):
    host = show(picker, (1.0, 0.0, 0.0))
    host.click(HUE, FIELD[1] + (BAND + SQUARE) / 3.0)           # a third down: green
    host.frame()
    assert color(host) == pytest.approx((0.0, 1.0, 0.0), abs=0.01)
    grey = (0.5, 0.5, 0.5)
    host = show(picker, grey)
    host.click(HUE, FIELD[1] + (BAND + SQUARE) * 2.0 / 3.0)     # two thirds down: blue
    host.frame()
    assert color(host) == grey
    host.click(CORNER[0] + SQUARE, CORNER[1])                   # the square's top right: the hue itself
    host.frame()
    assert color(host) == pytest.approx((0.0, 0.0, 1.0), abs=0.01)


def test_the_chip_opens_the_picker_under_it(show):
    host = show(chip, (1.0, 0.0, 0.0))
    host.click(LEFT + 10.0, TOP + ROW * 0.5)
    host.frame()
    opened = (LEFT + PAD, TOP + ROW + GAP + PAD + BAND)         # the popup's square
    host.click(opened[0] + SQUARE * 0.5, opened[1] + SQUARE * 0.5)
    host.frame()
    assert color(host) == pytest.approx((0.5, 0.25, 0.25), abs=0.01)


def bare(input_value=None):
    return draw_color_picker(name='picker', left=LEFT, top=TOP, width=extent(3)[0], height=extent(3)[1])


def test_an_empty_colour_is_filled_up_and_returned_as_an_edit(show):
    host = show(picker, ())
    assert color(host) == (1.0, 1.0, 1.0)
    host = show(chip, ())
    assert color(host) == (1.0, 1.0, 1.0)
    short = [0.2]
    host = show(picker, short)
    assert color(host) is short and short == [0.2, 1.0, 1.0]


def test_a_picker_that_is_passed_nothing_keeps_a_colour_of_its_own(show):
    host = show(bare, None)
    assert host.result[1] == (1.0, 1.0, 1.0)
    host.click(CORNER[0] + SQUARE - 1.0, CORNER[1] + SQUARE - 1.0)      # black
    host.frame()
    assert host.result[1] == pytest.approx((0.0, 0.0, 0.0), abs=0.01)


def wrapped(input_value=None):
    return draw_color_picker(input_value, name='picker', left=LEFT, top=TOP, width=None, height=None)


def test_a_wrapping_picker_is_as_high_as_all_of_its_rows(show):
    host = show(wrapped, (1.0, 0.0, 0.0))
    body = draw_color_picker.__gui_definition__[0]
    (node,) = [node for node, record in host.cache.records.items() if record[2] is body]
    assert tuple(host.cache.graph.rect(node)[2:]) == extent(3)
    host.frame(input_value=(1.0, 0.0, 0.0, 0.5))
    host.frame()
    assert tuple(host.cache.graph.rect(node)[2:]) == extent(4)


def dim(input_value=None, headroom=PEAK):
    return draw_color_picker(input_value, name='picker', headroom=headroom, left=LEFT, top=TOP, width=None,
                             height=None)


def test_a_picker_under_white_picks_nothing_brighter_than_its_headroom(show):
    host = show(dim, (0.0, 0.0, 0.0))
    host.frame(headroom=0.25)
    host.frame()
    host.click(FIELD[0] + 1.0, FIELD[1] + 1.0)                  # the top left of what is left of the square
    host.frame()
    most = hdr_color.linear_to_srgb(0.25)
    assert max(color(host)) == pytest.approx(most, abs=0.02) and min(color(host)) > most - 0.05


def test_a_picker_of_no_headroom_has_no_field_and_still_draws(show):
    host = show(dim, (0.2, 0.4, 0.9))
    host.frame(headroom=0.0)
    host.frame()
    body = draw_color_picker.__gui_definition__[0]
    (node,) = [node for node, record in host.cache.records.items() if record[2] is body]
    assert tuple(host.cache.graph.rect(node)[2:]) == (LEAST, extent(3, 0.0)[1])     # no field: the least width
    assert color(host) == (0.2, 0.4, 0.9)


def test_the_srgb_selector_is_the_square_alone_and_stays_inside_srgb(show):
    assert scale(PEAK, SRGB) == scale(1.0) and extent(3, PEAK, SRGB) == extent(3, 1.0)
    host = show(picker, (1.0, 0.0, 0.0), selector=SRGB)
    host.click(FIELD[0] + ACROSS * 0.5, FIELD[1] + SQUARE * 0.5)        # no band: the square starts at the top
    host.frame()
    assert color(host) == pytest.approx((0.5, 0.25, 0.25), abs=0.01)
    host.click(FIELD[0] + ACROSS - 1.0, FIELD[1] + 1.0)                 # the top right corner: the hue itself
    host.frame()
    assert color(host) == pytest.approx((1.0, 0.0, 0.0), abs=0.02)
    hue, spot = SELECTORS[SRGB].locate(hdr_color.p3(1.0, 0.0, 0.0), 0.5)
    assert spot == (1.0, 1.0)                                           # outside sRGB: shown at its edge


def test_the_hdr_selector_is_one_p3_square_under_the_band(show):
    host = show(picker, hdr_color.p3(1.0, 0.0, 0.0), selector=HDR)
    host.click(FIELD[0] + ACROSS - 1.0, CORNER[1] + 1.0)                # the square's top right: P3 red
    host.frame()
    assert color(host) == pytest.approx(hdr_color.p3(1.0, 0.0, 0.0), abs=0.03)
    host.click(FIELD[0] + 1.0, FIELD[1] + 1.0)                          # the band's top left: white at the ceiling
    host.frame()
    red, green, blue = color(host)
    assert hdr_color.srgb_to_linear(red) == pytest.approx(PEAK, rel=0.1) and green == pytest.approx(red, rel=0.02)
    hue, spot = SELECTORS[HDR].locate(hdr_color.white(8.0), 0.3)
    assert hue == 0.3 and spot == pytest.approx((0.0, 1.0, 8.0))        # white has no hue: the bar stays


def test_a_change_of_selector_finds_the_colour_again(show):
    teal = (0.1, 0.6, 0.5)
    host = show(picker, teal, selector=SRGB)
    host.frame(selector=HDR)
    host.frame(selector=SRGB_PLUS)
    host.frame()
    assert color(host) == teal                                          # looking at it another way is no edit


def test_the_perceptual_selector_keeps_lightness_and_chroma_when_the_hue_turns(show):
    kind = SELECTORS[PERCEPTUAL]
    teal = (0.1, 0.6, 0.5)
    hue, spot = kind.locate(teal, 0.0)
    teal_hue = hue
    assert kind.color(hue, spot) == pytest.approx(teal, abs=1e-6)       # there and back
    hue, grey = kind.locate((0.5, 0.5, 0.5), 0.3)
    assert hue == 0.3 and grey[0] < 1e-6                                # grey has no hue: the bar stays
    # Another hue, the same spot: the same lightness, and the chroma where that hue reaches it.
    turned = kind.color(hue + 0.25, spot)
    again = kind.locate(turned, 0.0)[1]
    assert again[1] == pytest.approx(spot[1], abs=1e-4) and again[0] <= spot[0] + 1e-4
    host = show(sized, teal, selector=PERCEPTUAL, width=700.0, height=900.0)     # large: a pixel is a small step
    assert color(host) == teal
    room = scale(PEAK, PERCEPTUAL)
    x, y = kind.mark(spot, room)
    host.click(*place(host, PERCEPTUAL, teal_hue, room, x, y))               # a press on its own marker
    host.frame()
    assert color(host) == pytest.approx(teal, abs=0.02)
    left, top, wide, high = rect(host, field)
    host.click(LEFT + left + wide - 1.0, place(host, PERCEPTUAL, teal_hue, room, x, y)[1])   # the row's far right: past P3
    host.frame()
    picked = kind.locate(color(host), 0.0)[1]
    assert picked[1] == pytest.approx(spot[1], abs=0.02)                # the row's lightness
    assert picked[0] == pytest.approx(hdr_color.oklab_max_chroma(picked[1], kind.locate(color(host), 0.0)[0] * 6.283185307, PEAK), abs=0.01)


def test_the_perceptual_field_is_the_shape_of_the_gamut(show):
    kind = SELECTORS[PERCEPTUAL]
    room = scale(PEAK, PERCEPTUAL)
    pixels, lines = kind.bake(0.4, room)
    inside = pixels[..., 3]
    assert 0.2 < inside.mean() < 0.8                                    # neither a full rectangle nor empty
    assert inside[inside.shape[0] // 2, 0] == 1.0 and inside[0, -1] == 0.0  # grey is in, the bright far corner is not
    outline, white_line = lines
    assert white_line[0][1] == pytest.approx(1.0 - 1.0 / PEAK ** (1.0 / 3.0))
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in outline)
    assert len(kind.bake(0.4, scale(1.0, PERCEPTUAL))[1]) == 1         # no line at white


def test_jzazbz_is_absolute_and_returns_what_it_was_given():
    assert hdr_color.linear_to_jzazbz((1.0, 1.0, 1.0), 100.0)[0] == pytest.approx(0.167, abs=0.002)
    assert hdr_color.linear_to_jzazbz((1.0, 1.0, 1.0), 10000.0)[0] == pytest.approx(0.989, abs=0.002)
    for rgb in ((0.2, 0.5, 0.1), (4.0, -0.2, 0.3)):
        back = hdr_color.jzazbz_to_linear(hdr_color.linear_to_jzazbz(rgb, 377.0), 377.0)
        assert back == pytest.approx(rgb, abs=1e-4)


def test_the_dynamic_field_grows_with_the_brightness_on_axes_that_stand_still():
    kind = SELECTORS[DYNAMIC_PERCEPTUAL]
    # (Through PQ: `carry`. A float output shows the same field at every brightness.)
    dim = scale(PEAK, DYNAMIC_PERCEPTUAL, 400.0, 100.0, carry=25.0)      # the slider at 25%
    bright = scale(PEAK, DYNAMIC_PERCEPTUAL, 400.0, 400.0, carry=25.0)
    shown = [kind.bake(0.4, room) for room in (dim, bright)]
    assert shown[0][0][..., 3].mean() < shown[1][0][..., 3].mean()      # less of the field is reachable when dim
    white_rows = [lines[1][0][1] for pixels, lines in shown]
    assert white_rows[0] > white_rows[1]                                # and white stands lower
    teal = (0.1, 0.6, 0.5)
    spots = [kind.locate(teal, 0.0, room)[1] for room in (dim, bright)]
    assert spots[0][1] < spots[1][1]                                    # the same colour is darker light
    for room in (dim, bright):
        hue, spot = kind.locate(teal, 0.0, room)
        assert kind.color(hue, spot, room) == pytest.approx(teal, abs=1e-4)


def test_a_dynamic_picker_picks_without_a_compositor_to_ask(show, monkeypatch):
    from meltygui.model.display_model import Display
    monkeypatch.setattr(Display, 'read', lambda self: False)
    teal = (0.1, 0.6, 0.5)
    host = show(picker, teal, selector=DYNAMIC_PERCEPTUAL)
    assert color(host) == teal
    room = scale(PEAK, DYNAMIC_PERCEPTUAL, white(), white())
    hue, spot = SELECTORS[DYNAMIC_PERCEPTUAL].locate(teal, 0.0, room)
    x, y = SELECTORS[DYNAMIC_PERCEPTUAL].mark(spot, room)
    host.click(*place(host, DYNAMIC_PERCEPTUAL, hue, room, x, y))       # a press on its own marker
    host.frame()
    assert color(host) == pytest.approx(teal, abs=0.03)


def test_a_dynamic_spot_emits_the_same_light_at_every_brightness():
    """The goal of the selector: a place in the field is one light. With the compositor's shadow
    stages on, the colour value sent for it differs with the brightness; what the panel then emits
    does not."""
    from meltygui.model.display_model import Tone
    from meltygui.view.common.color_picker_view import seen
    kind = SELECTORS[DYNAMIC_PERCEPTUAL]
    forth = hdr_color.srgb_to_primaries(hdr_color.BT2020_XY)
    panel = (forth, hdr_color._mat_inv(forth))
    rooms = [scale(PEAK, DYNAMIC_PERCEPTUAL, 400.0, 400.0 * gain, Tone(2.3, 400.0 * gain, 0.33), panel)
             for gain in (0.24, 0.5, 1.0, 1.5)]
    for hue, spot in ((0.4, (0.05, 0.12)), (0.1, (0.02, 0.05)), (0.7, (0.08, 0.2)), (0.25, (0.0, 0.03))):
        emitted = []
        values = []
        for room in rooms:
            value = kind.color(hue, spot, room)
            values.append(value)
            emitted.append(seen(tuple(hdr_color.srgb_to_linear(channel) for channel in value), room))
            assert kind.locate(value, 0.0, room)[1] == pytest.approx(spot, abs=2e-4)    # and it is found again
        assert values[0] != pytest.approx(values[-1], abs=1e-3)                         # another value is sent
        for light in emitted[1:]:
            assert light == pytest.approx(emitted[0], rel=2e-3, abs=2e-3)               # the same nits come out


def sized(input_value=None, selector=HDR, **size):
    return draw_color_picker(input_value, name='picker', selector=selector, left=LEFT, top=TOP, **size)


def test_a_picker_given_a_size_fits_its_field_into_it_undistorted(show):
    host = show(sized, (1.0, 0.0, 0.0), selector=SRGB_PLUS, width=500.0, height=600.0)
    assert rect(host, draw_color_picker)[2:] == (500.0, 600.0)
    under = GAP + 3 * (ROW + 2.0) + ROW
    room_wide, room_high = 500.0 - 2.0 * PAD - GAP - BAR, 600.0 - 2.0 * PAD - under
    shape = ACROSS / (BAND + SQUARE)                                     # the field's own aspect, kept
    high = min(room_wide / shape, room_high)
    wide = high * shape
    assert wide < room_wide and high == room_high                       # the height is what limits it here
    assert rect(host, field) == pytest.approx((PAD, PAD, wide, high))
    assert rect(host, bar)[:2] == pytest.approx((500.0 - PAD - BAR, PAD))       # the bar at the picker's right edge
    assert rect(host, bar)[0] > PAD + wide + GAP                                # with room left beside the field
    # The pointer is scaled to the selector's own pixels: the middle of the square.
    room = scale(PEAK, SRGB_PLUS)
    host.click(*place(host, SRGB_PLUS, 0.0, room, SQUARE * 0.5, BAND + SQUARE * 0.5))
    host.frame()
    assert color(host) == pytest.approx((0.5, 0.25, 0.25), abs=0.01)


def test_wrap_means_wrap_on_each_axis_alone(show):
    under = GAP + 3 * (ROW + 2.0) + ROW
    # A height that wraps is the picker's fixed least, whatever the width.
    for wide in (400.0, 700.0):
        host = show(sized, (1.0, 0.0, 0.0), width=wide, height=None)
        assert rect(host, draw_color_picker)[2:] == (wide, extent(3)[1])
        assert rect(host, field)[2:] == pytest.approx((ACROSS, BAND + SQUARE))
    # A width that wraps is as wide as the shape is at the height that was given.
    host = show(sized, (1.0, 0.0, 0.0), width=None, height=500.0)
    high = 500.0 - 2.0 * PAD - under
    wide = high * ACROSS / (BAND + SQUARE)
    assert rect(host, field)[2:] == pytest.approx((wide, high))
    assert rect(host, draw_color_picker)[2:] == pytest.approx((PAD + wide + GAP + BAR + PAD, 500.0))
    # Too narrow for its own height: the field shrinks whole, it is not squeezed.
    host = show(sized, (1.0, 0.0, 0.0), width=150.0, height=None)
    left, top, wide, high = rect(host, field)
    assert wide == pytest.approx(150.0 - 2.0 * PAD - GAP - BAR) and wide / high == pytest.approx(ACROSS / (BAND + SQUARE))


def test_a_wrapped_perceptual_picker_is_as_wide_as_the_hues_colours(show):
    kind = SELECTORS[PERCEPTUAL]
    room = scale(PEAK, PERCEPTUAL)
    green = kind.color(0.40, (0.1, 0.8), room)
    cyan = kind.color(0.55, (0.05, 0.8), room)
    widths = []
    for value in (green, cyan):
        host = show(sized, value, selector=PERCEPTUAL, width=None, height=None)
        hue = kind.locate(value, 0.0, room)[0]
        across = (BAND + SQUARE) * kind.aspect(room) * kind.span(hue, room)     # its own height, undistorted
        assert rect(host, field)[2:] == pytest.approx((across, BAND + SQUARE), abs=1.0)
        whole = max(PAD + across + GAP + BAR + PAD, LEAST)
        assert rect(host, draw_color_picker)[2] == pytest.approx(whole, abs=1.0)    # the right edge follows the shape
        assert rect(host, bar)[0] == pytest.approx(whole - PAD - BAR, abs=1.0)      # and the bar stands at it
        widths.append(across)
        # The scale across is the hue's own whatever the width: a press on the marker picks the colour again.
        x, y = kind.mark(kind.locate(value, 0.0, room)[1], room)
        host.click(*place(host, PERCEPTUAL, hue, room, x, y))
        host.frame()
        assert color(host) == pytest.approx(value, abs=0.03)
    assert widths[1] < widths[0] * 0.8                                           # cyan reaches less far than green


def test_a_square_selector_wraps_to_its_own_size(show):
    for selector in (SRGB, SRGB_PLUS, HDR):
        host = show(sized, (1.0, 0.0, 0.0), selector=selector, width=None, height=None)
        assert rect(host, draw_color_picker)[2:] == extent(3, PEAK, selector)


def test_what_the_hardware_cannot_show_is_not_drawn():
    from meltygui.view.common.color_picker_view import within
    kind = SELECTORS[HDR]
    room = scale(PEAK, HDR)
    pixels, lines = kind.bake(0.0, room)
    assert pixels[..., 3].min() == 1.0                                  # a square selector draws every texel
    cut = within(pixels.copy(), 4.0)                                    # the panel ends at 4 times white
    rows = cut.shape[0]
    assert cut[0, 0, 3] == 0.0 and cut[-1, 0, 3] == 1.0                 # the band's top is gone, the square stays
    band_rows = int(rows * room.band / (room.band + room.square))
    assert cut[band_rows:, :, 3].min() == 1.0                           # nothing under white is cut
    assert 0.0 < cut[:band_rows, :, 3].mean() < 1.0                     # the band is cut part of the way
    assert scale(PEAK, HDR, limit=4.0).limit == 4.0 and scale(PEAK, HDR).limit == 0.0


def limited(input_value=None, selector=HDR, hardware_max=None, sdr_white_max=None):
    across, down = extent(len(input_value), PEAK, selector)
    return draw_color_picker(input_value, name='picker', selector=selector, hardware_max=hardware_max,
                             sdr_white_max=sdr_white_max, left=LEFT, top=TOP, width=across, height=down)


def test_the_display_can_be_told_to_the_picker(show, monkeypatch):
    from meltygui.model.display_model import Display
    from meltygui.view.common.color_picker_view import Picture
    monkeypatch.setattr(Display, 'read', lambda self: False)
    rooms = []
    real = Picture.show

    def show_picture(self, selector, hue, room):
        rooms.append(room)
        return real(self, selector, hue, room)
    monkeypatch.setattr(Picture, 'show', show_picture)
    for selector in (HDR, SRGB_PLUS, PERCEPTUAL):
        host = show(limited, (1.0, 0.0, 0.0), selector=selector, hardware_max=1000.0, sdr_white_max=250.0)
        assert rooms[-1].limit == 4.0                                   # 1,000 nits over a white of 250
    host = show(limited, (1.0, 0.0, 0.0), selector=DYNAMIC_PERCEPTUAL, hardware_max=1000.0, sdr_white_max=500.0)
    assert rooms[-1].limit == 2.0 and rooms[-1].full == 500.0 and rooms[-1].white == 500.0
    host = show(limited, (1.0, 0.0, 0.0))
    assert rooms[-1].limit == 0.0                                       # nothing told: nothing cut
    assert caption(list(hdr_color.white(4.0)), 250.0) == '4.0x  1000 nits  sRGB'


def test_a_perceptual_field_gives_chroma_and_lightness_the_same_length(show):
    kind = SELECTORS[PERCEPTUAL]
    room = scale(PEAK, PERCEPTUAL)
    value = kind.color(0.40, (0.1, 0.8), room)
    hue = kind.locate(value, 0.0, room)[0]
    top = PEAK ** (1.0 / 3.0)                                            # the lightness of the top row
    for size in (dict(width=None, height=None), dict(width=600.0, height=500.0), dict(width=300.0, height=700.0)):
        host = show(sized, value, selector=PERCEPTUAL, **size)
        left, upper, wide, high = rect(host, field)
        chroma_shown = 0.4 * top * kind.span(hue, room)                 # the chroma at the field's right edge
        assert wide / chroma_shown == pytest.approx(high / top, rel=0.01)       # pixels per unit, both ways


def test_the_dynamic_shape_is_the_same_light_at_every_brightness():
    """Turning the brightness moves only where the field ends at the top. What is drawn below
    that is the same place for the same light: no dark colour outside the gamut slips in when the
    values get small (a strip of them along the bottom did, at 1,000% brightness)."""
    from meltygui.model.display_model import Tone
    kind = SELECTORS[DYNAMIC_PERCEPTUAL]
    forth = hdr_color.srgb_to_primaries((0.68, 0.305, 0.272, 0.666, 0.155, 0.056, 0.3135, 0.329))
    panel = (forth, hdr_color._mat_inv(forth))
    masks = []
    for gain in (0.09, 1.0, 10.0):
        room = scale(PEAK, DYNAMIC_PERCEPTUAL, 400.0, 400.0 * gain, Tone(2.3, 400.0 * gain, 0.33), panel)
        masks.append(kind.bake(0.25, room)[0][..., 3] > 0.5)
    rows = masks[0].shape[0]
    low = slice(int(rows * 0.8), rows)                              # light every one of the three can show
    for mask in masks[1:]:
        differing = (mask[low] != masks[0][low]).mean()
        assert differing < 0.01                                     # a texel at the edge, not a strip
    assert masks[2][low].mean() < 0.5                               # and most of those rows are outside P3


def test_the_hardware_line_of_the_dynamic_field_stays_put_as_the_brightness_climbs():
    """`hardware_max` is light the panel emits. With the compositor's stages on, the value sent
    for a light changes with the brightness; the line where the field ends must not."""
    from meltygui.model.display_model import Tone
    kind = SELECTORS[DYNAMIC_PERCEPTUAL]
    forth = hdr_color.srgb_to_primaries((0.68, 0.305, 0.272, 0.666, 0.155, 0.056, 0.3135, 0.329))
    panel = (forth, hdr_color._mat_inv(forth))
    tops = []
    for gain in (1.0, 1.07, 2.5, 10.0):
        white = 400.0 * gain
        room = scale(PEAK, DYNAMIC_PERCEPTUAL, 400.0, white, Tone(2.3, white, 0.33), panel, 1000.0 / white)
        mask = kind.bake(0.25, room)[0][..., 3] > 0.5
        tops.append(int(mask[:, 0].argmax()))                       # the first row drawn in the grey column
    assert max(tops) - min(tops) <= 1, tops
    limited = scale(PEAK, HDR, limit=4.0)
    assert SELECTORS[HDR].bake(0.0, limited)[0][0, 0, 3] == 0.0     # the other selectors cut in their own bake


def test_the_dynamic_field_ends_where_the_windows_output_does():
    """Through PQ, which ends at 10,000 nits before the display's brightness is applied, the most
    that can leave the panel at 3% is 300 nits: the field shrinks from the top. A float output has
    no such end, and the field shows everything up to the headroom at any brightness."""
    kind = SELECTORS[DYNAMIC_PERCEPTUAL]
    top_light = hdr_color.linear_to_jzazbz((1.0, 1.0, 1.0), min(PEAK * 400.0, 10000.0))[0]

    def first_light(room):
        mask = kind.bake(0.25, room)[0][..., 3] > 0.5
        first = int(mask[:, 0].argmax())                            # the first row drawn in the grey column
        return top_light * (1.0 - (first + 0.5) / mask.shape[0])    # its Jz

    for gain in (0.03, 0.14, 1.0):
        through_pq = scale(PEAK, DYNAMIC_PERCEPTUAL, 400.0, 400.0 * gain, carry=10000.0 / 400.0)
        most = hdr_color.linear_to_jzazbz((1.0, 1.0, 1.0), 10000.0 * gain)[0]
        assert first_light(through_pq) == pytest.approx(most, abs=0.01)
        floating = scale(PEAK, DYNAMIC_PERCEPTUAL, 400.0, 400.0 * gain)
        assert first_light(floating) == pytest.approx(top_light, abs=0.01)      # the whole field, at 3% too
    # ... and with the hardware's limit told, up to that at any brightness.
    for gain in (0.03, 1.0):
        white = 400.0 * gain
        limited = scale(PEAK, DYNAMIC_PERCEPTUAL, 400.0, white, limit=2500.0 / white)
        assert first_light(limited) == pytest.approx(hdr_color.linear_to_jzazbz((1.0, 1.0, 1.0), 2500.0)[0], abs=0.01)
