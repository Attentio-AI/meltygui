"""`draw_color` and `draw_color_picker` (view/color_picker_view.py): the sRGB square, the P3 and HDR
quadrants around it, the hue bar and the chip that opens the picker as a popup.

The app is one picker (or one chip) at (10, 10). The tests drive the real router through the GL
test host, frame by frame.
"""
import pytest

from editor_host import Host
from meltygui import hdr_color
from meltygui.view.color_picker_view import BAND, BAR, GAP, NITS, SQUARE, WIDE
from meltygui.view.color_picker_view import caption, draw_color, draw_color_picker, extent, locate, reach, white
from meltygui.view.value_view import PAD, ROW

LEFT, TOP = 10.0, 10.0
FIELD = (LEFT + PAD, TOP + PAD)                         # the field's corner on the surface
CORNER = (FIELD[0], FIELD[1] + BAND)                    # the sRGB square's
HUE = FIELD[0] + SQUARE + WIDE + GAP + BAR * 0.5        # an x on the hue bar


def picker(input_value=None):
    across, down = extent(len(input_value))
    return draw_color_picker(input_value, name='picker', left=LEFT, top=TOP, width=across, height=down)


def chip(input_value=None):
    return draw_color(input_value, name='chip', left=LEFT, top=TOP, width=60.0)


@pytest.fixture
def show():
    hosts = []

    def make(view, color):
        hosts.append(Host(view, input_value=color))
        hosts[-1].frame()
        return hosts[-1]
    yield make
    for host in hosts:
        host.close()


def color(host):
    return host.arguments['input_value']


def test_where_a_colour_stands():
    hue, spot = locate((0.2, 0.4, 0.9), 0.0)
    assert hdr_color.extended_from_srgb_extension(hue, spot[0], spot[2], spot[1], spot[3]) == pytest.approx((0.2, 0.4, 0.9))
    assert spot[1] == 0.0 and spot[3] == 1.0                    # inside the square, under the band
    hue, spot = locate(hdr_color.p3(1.0, 0.0, 0.0), 0.0)
    assert spot[1] == pytest.approx(1.0, abs=1e-3)              # the far edge of the P3 quadrant
    hue, spot = locate(hdr_color.white(8.0), 0.3)
    assert hue == 0.3 and spot[3] == pytest.approx(8.0)         # white has no hue: the bar stays


def test_the_band_reaches_the_nits_asked_for():
    assert 2.0 ** reach(NITS) * white() == pytest.approx(NITS)
    assert reach(1.0) == 1.0                                    # never less than a stop


def test_the_caption_is_a_hex_code_inside_srgb_and_nits_outside():
    assert caption([1.0, 0.5, 0.0]) == '#ff8000'
    assert caption([1.0, 0.5, 0.0, 1.0]) == '#ff8000ff'
    assert caption(list(hdr_color.white(4.0))) == f'{4.0 * white():.0f} nits  sRGB'
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
    peak = max(hdr_color.srgb_to_linear(channel) for channel in color(host)) * white()
    assert min(color(host)) < 0.0 and peak == pytest.approx(NITS, rel=0.25)


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
