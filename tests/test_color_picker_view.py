"""`draw_color` and `draw_color_picker` (view/color_picker_view.py): the sRGB square, the P3 and HDR
quadrants around it, the hue bar and the chip that opens the picker as a popup.

The app is one picker (or one chip) at (10, 10). The tests drive the real router through the GL
test host, frame by frame.
"""
import pytest

from editor_host import Host
from meltygui import hdr_color
from meltygui.view.color_picker_view import BAND, BAR, GAP, PEAK, SQUARE, WIDE
from meltygui.hdr_color import HDR, SRGB, SRGB_PLUS
from meltygui.view.color_picker_view import ACROSS, SELECTORS
from meltygui.view.color_picker_view import caption, draw_color, draw_color_picker, extent, scale, white
from meltygui.view.value_view import PAD, ROW

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


def test_where_a_colour_stands():
    hue, spot = SELECTORS[SRGB_PLUS].locate((0.2, 0.4, 0.9), 0.0)
    assert hdr_color.extended_from_srgb_extension(hue, spot[0], spot[2], spot[1], spot[3]) == pytest.approx((0.2, 0.4, 0.9))
    assert spot[1] == 0.0 and spot[3] == 1.0                    # inside the square, under the band
    hue, spot = SELECTORS[SRGB_PLUS].locate(hdr_color.p3(1.0, 0.0, 0.0), 0.0)
    assert spot[1] == pytest.approx(1.0, abs=1e-3)              # the far edge of the P3 quadrant
    hue, spot = SELECTORS[SRGB_PLUS].locate(hdr_color.white(8.0), 0.3)
    assert hue == 0.3 and spot[3] == pytest.approx(8.0)         # white has no hue: the bar stays


def test_the_field_is_to_scale_with_the_headroom_asked_for():
    band, square, stops, top = scale(PEAK)
    assert (band, square) == (BAND, SQUARE) and 2.0 ** stops == pytest.approx(PEAK)
    assert scale(PEAK * 4.0) == scale(PEAK)                     # `PEAK` times white is the most
    middle = PEAK ** 0.5                                        # half the stops: half the band
    assert scale(middle)[0] == pytest.approx(BAND * 0.5, abs=0.5) and scale(middle)[1] == SQUARE
    assert scale(1.0)[:3] == (0.0, SQUARE, 0.0)                 # white: the square alone
    band, square, stops, top = scale(0.25)                      # under white: no band, a lower square
    assert band == 0.0 and 0.0 < square < SQUARE and top == pytest.approx(hdr_color.linear_to_srgb(0.25))
    assert scale(0.0) == (0.0, 0.0, 0.0, 0.0)
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
    return draw_color_picker(input_value, name='picker', left=LEFT, top=TOP, height=None)


def test_a_wrapping_picker_is_as_high_as_all_of_its_rows(show):
    host = show(wrapped, (1.0, 0.0, 0.0))
    body = draw_color_picker.__gui_definition__[0]
    (node,) = [node for node, record in host.cache.records.items() if record[2] is body]
    assert tuple(host.cache.graph.rect(node)[2:]) == extent(3)
    host.frame(input_value=(1.0, 0.0, 0.0, 0.5))
    host.frame()
    assert tuple(host.cache.graph.rect(node)[2:]) == extent(4)


def dim(input_value=None, headroom=PEAK):
    return draw_color_picker(input_value, name='picker', headroom=headroom, left=LEFT, top=TOP, height=None)


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
    assert tuple(host.cache.graph.rect(node)[2:]) == extent(3, 0.0)
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
