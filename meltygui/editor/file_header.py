"""File labels shared by the file UI and chat change summaries."""
from pathlib import Path
import meltygui_imgui as imgui
from meltygui.runtime import Melty
from meltygui.toggles import Toggles, Tint
from meltygui.hdr_color import pack_color
from meltygui.views.headers import flat_button
from meltygui.views.blit_offscreen import add_shadow
from meltygui.editor.source_ui import _tab_text_color

_ELLIPSIZE_MEMO = globals().get("_ELLIPSIZE_MEMO", {})


def _ellipsize_search(text, max_width):
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if imgui.calc_text_size(text[:mid] + "\u2026").x <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + "\u2026"


def _ellipsize(text, max_width):
    """`text` cut to fit `max_width` pixels in the current font with a
    trailing ellipsis; unchanged when it already fits."""
    # Memoized per (text, width, font scale): the commit-file column runs
    # this for each row every frame, and each miss is a calc_text_size
    # binary search.
    memo_key = (text, max_width, Melty.ui_scale)
    hit = _ELLIPSIZE_MEMO.get(memo_key)
    if hit is not None:
        return hit
    if max_width <= 0 or imgui.calc_text_size(text).x <= max_width:
        result = text
    else:
        result = _ellipsize_search(text, max_width)
    if len(_ELLIPSIZE_MEMO) > 4096:
        _ELLIPSIZE_MEMO.clear()
    _ELLIPSIZE_MEMO[memo_key] = result
    return result


def draw_changed_file_header(path, tint, draw_state, view_id, width, height=23.0,
                             added=None, removed=None, active=False, prefix="", shadow_offset=None,
                             background=True):
    """Shared draw-list file header for the diff column and chat tool summaries.

    `shadow_offset` overrides the tab-state lift for BOTH the wash and the
    button (0 = flat); None keeps the CodeEditor tab offsets.
    `background=False` draws the label only — no wash, no button bg, no
    shadow (the files column passes it for a file with no painted tint).
    Inactive labels use the column's own compare_file_text_* knobs, brighter
    than the tab bar's inactive tabs: the column has no bg to read against.
    """
    if active:
        text_color = _tab_text_color(tint, Toggles.CodeEditor.tab_active_text_brightness,
            Toggles.CodeEditor.tab_active_text_saturation, Toggles.CodeEditor.tab_active_text_min_brightness)
        brightness, saturation = Toggles.CodeEditor.tab_active_bg_brightness, Toggles.CodeEditor.tab_active_bg_saturation
        maximum, alpha = Toggles.CodeEditor.tab_active_bg_max_brightness, 0.9
        shadow = Toggles.CodeEditor.tab_active_shadow_offset
    else:
        text_color = _tab_text_color(tint, Toggles.CodeEditor.compare_file_text_brightness,
            Toggles.CodeEditor.tab_inactive_text_saturation, Toggles.CodeEditor.compare_file_text_min_brightness)
        brightness, saturation = Toggles.CodeEditor.tab_inactive_bg_brightness, Toggles.CodeEditor.tab_inactive_bg_saturation
        maximum, alpha = Toggles.CodeEditor.tab_inactive_bg_max_brightness, Toggles.CodeEditor.tab_inactive_bg_alpha
        shadow = Toggles.CodeEditor.tab_inactive_shadow_offset
    if shadow_offset is not None:
        shadow = shadow_offset
    if not background:
        alpha, shadow = 0.0, 0.0
    x, y = imgui.get_cursor_screen_pos()
    dl = imgui.get_window_draw_list()
    channel = Melty.get_channel()
    if Melty.channels_split:
        dl.channels_set_current(channel - 1)
    try:
        if background:
            wash = Melty.style_manager.make_color_rgb(*tint[:3],
                value=Toggles.CodeEditor.compare_file_bg_value, factor=0.8, saturation_scale=1.0)
            if shadow_offset is None or shadow_offset:
                add_shadow((x, y, width, height), offset=1.0 if shadow_offset is None else shadow_offset, corner_radius=4.0)
            dl.add_rect_filled(x, y, x + width, y + height,
                              pack_color(*wash[:3], 1.0), rounding=4.0)
        clicked = flat_button("", draw_state, view_id=view_id, width=width, height=height,
            color=tuple(tint[:3]), factor=0.1, tint_value=brightness, saturation=saturation,
            max_bg_brightness=maximum, alpha=alpha, shadow_offset=shadow, event="left_mouse_down")
    finally:
        if Melty.channels_split:
            dl.channels_set_current(channel)
    inset = Melty.px(2)
    counts = f"+{added} -{removed}" if added is not None and removed is not None else ""
    size = imgui.calc_text_size(counts)
    reserve = size.x + Melty.px(8) if counts else 0
    label = _ellipsize((prefix + "  " if prefix else "") + Path(path).name, max(0, width - inset * 2 - reserve))
    label_size = imgui.calc_text_size(label)
    dl.add_text(x + inset + Melty.px(2), y + (height - label_size.y) * 0.5 - Melty.px(1),
                pack_color(*text_color[:3], 1.0), label)
    if counts:
        left, top = x + width - inset - size.x, y + (height - size.y) * 0.5
        dl.add_text(left, top, pack_color(*Tint.change_count(added=True), 1.0), f"+{added}")
        dl.add_text(left + imgui.calc_text_size(f"+{added} ").x, top,
                    pack_color(*Tint.change_count(added=False), 1.0), f"-{removed}")
    return clicked

