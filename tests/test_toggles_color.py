"""rgb_to_hsv must accept every colour a tint can produce, pure black included."""
import colorsys

from meltygui.core.runtime.toggles import rgb_to_hsv


def test_black_has_no_hue_or_saturation():
    # 2026-09-18: black set maxc to 1.0, skipped the grey early-out and
    # divided by a zero range inside draw_text's background adjustment.
    assert rgb_to_hsv(0.0, 0.0, 0.0) == (0.0, 0.0, 0.0)


def test_matches_colorsys():
    for colour in ((0.3, 0.3, 0.3), (1.0, 0.0, 0.0), (0.2, 0.7, 0.4), (0.0, 0.0, 0.5)):
        assert rgb_to_hsv(*colour) == colorsys.rgb_to_hsv(*colour)


def test_channels_a_hair_below_zero_are_black():
    # 2026-09-18 07:49, from line_number_bg: no positive channel, a nonzero range.
    assert rgb_to_hsv(0, -0.01347, -0.01861) == (0.0, 0.0, 0.0)
