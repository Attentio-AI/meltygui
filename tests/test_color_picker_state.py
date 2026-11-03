"""Picker ownership and HSV continuity across frames, edits and openings."""
import colorsys
from types import SimpleNamespace

import pytest

from meltygui import hdr_color, imgui
from meltygui.state.new_core_model import ColorPickerState


TABS = ("wide", "srgb", "extended")


def picked(tab, h, s, v):
    if tab == "wide":
        return hdr_color.extended_from_p3_hsv(h, s, v), (h, s, v, 1.0)
    if tab == "extended":
        return hdr_color.extended_from_srgb_extension(h, s, v, 0.0, 1.0), (h, s, v, 0.0, 1.0)
    return colorsys.hsv_to_rgb(h, s, v), (h, s, v)


@pytest.mark.parametrize("tab", TABS)
@pytest.mark.parametrize("hue", (0.0, 0.71, 1.0))
@pytest.mark.parametrize("saturation,value", ((0.0, 0.613456), (0.8, 0.0), (0.8, 0.613456)))
def test_open_picker_owns_color_even_when_caller_returns_another_value(tab, hue, saturation, value):
    state = ColorPickerState()
    state.begin((0.2, 0.4, 0.6, 1.0), opening_frame=10)
    state.current_coordinates(tab)
    rgb, coords = picked(tab, hue, saturation, value)
    color = (*rgb, 0.829123)
    state.edit(color, coords)

    # A rounded source echo, a stale cached caller, and an unrelated external
    # value all leave the live picker alone. Replayed opening calls do too.
    for incoming in (tuple(round(c, 2) for c in color), (0.2, 0.4, 0.6, 1.0), (1, 0, 0, 0)):
        for opening in (None, 10):
            state.begin(incoming, opening)
            assert state.color == color
            assert state.current_coordinates(tab) == coords


@pytest.mark.parametrize("tab", TABS)
@pytest.mark.parametrize("hue", (0.0, 0.71, 1.0))
@pytest.mark.parametrize("gray", (0.0, 0.613))
def test_reopening_loads_input_but_retains_undefined_hue_and_saturation(tab, hue, gray):
    state = ColorPickerState()
    state.begin((1.0, 0.0, 0.0), 1)
    state.current_coordinates(tab)
    rgb, coords = picked(tab, hue, 0.8, 0.6)
    state.edit(rgb, coords)
    state.begin((gray, gray, gray), 2)
    assert state.color == (gray, gray, gray)
    h, s, v, *_ = state.current_coordinates(tab)
    assert h == hue
    assert s == (0.8 if gray == 0.0 else 0.0)
    assert v == pytest.approx(gray)

    state.begin((0.0, 1.0, 0.0), 3)
    assert state.current_coordinates(tab)[0] != hue


def test_tab_switch_uses_owned_color_and_pickers_are_independent():
    first, second = ColorPickerState(), ColorPickerState()
    first.begin((1.0, 0.0, 0.0), 1)
    second.begin((0.0, 0.0, 1.0), 1)
    first.current_coordinates("srgb")
    first.edit((0.0, 1.0, 0.0), (1 / 3, 1.0, 1.0))
    first.begin((1.0, 0.0, 0.0))
    assert first.current_coordinates("wide") == pytest.approx(hdr_color.p3_hsv_from_extended(0, 1, 0))
    assert first.current_coordinates("srgb") == (1 / 3, 1.0, 1.0)
    assert second.current_coordinates("srgb") == (2 / 3, 1.0, 1.0)


@pytest.mark.parametrize("tab", TABS)
@pytest.mark.parametrize("channel", ("A", "R"))
def test_channel_rows_keep_exact_other_channels_and_alpha_keeps_hsv(monkeypatch, tab, channel):
    from conftest import begin_frame, end_frame
    from meltygui.view import color_view, control_view, header_view
    from meltygui.core.windowing import glfw_utils

    state = ColorPickerState()
    state.tab = tab
    state.begin((0.2, 0.4, 0.6, 1.0), 1)
    state.current_coordinates(tab)
    rgb, coords = picked(tab, 1.0, 0.0, 0.613456)
    state.edit((*rgb, 0.8), coords)
    monkeypatch.setattr(glfw_utils, "request_render", lambda: None)
    monkeypatch.setattr(control_view, "button", lambda *a, **kw: (False, None))
    monkeypatch.setattr(header_view, "flat_button", lambda *a, **kw: False)
    monkeypatch.setattr(color_view.Tint, "subtle_text", lambda: (1, 1, 1, 1))
    monkeypatch.setattr(color_view, "_wide_square_texture", lambda *a: None)
    monkeypatch.setattr(color_view, "_srgb_plus_texture", lambda *a: None)
    monkeypatch.setattr(imgui, "is_item_active", lambda: False)
    monkeypatch.setattr(imgui, "drag_float", lambda label, value, *a:
                        (True, 0.25) if label.startswith(channel) else (False, round(value, 3)))
    begin_frame()
    imgui.begin("picker regression")
    try:
        changed, color = color_view.draw_color_picker.__wrapped__(
            (1.0, 0.0, 0.0, 1.0), picker_state=state,
            draw_state=SimpleNamespace(content_width=260))
        assert changed
        assert color == ((0.25, *rgb[1:], 0.8) if channel == "R" else (*rgb, 0.25))
        assert state.color == color
        if channel == "A":
            assert state.current_coordinates(tab) == coords
        else:
            assert state.current_coordinates(tab)[:3] != coords[:3]
    finally:
        imgui.end()
        end_frame()
