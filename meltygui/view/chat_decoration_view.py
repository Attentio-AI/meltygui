"""Local chat decorations; callers supply scale, pointer and drawing channel."""
from functools import lru_cache
import colorsys
import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
from meltygui.core.runtime.toggles import Tint, Toggles
from meltygui.core.cache.tile_marks import add_shadow
from meltygui.view.decoration_view import draw_bg
from meltygui.view.header_view import flat_button


@lru_cache(maxsize=128)
def _tint_style(tint):
    from meltygui.core.styling.style_core import ImGuiStyleManager
    style = ImGuiStyleManager()
    style.current_rgb = tint[:3]
    style.hsv = colorsys.rgb_to_hsv(*tint[:3])
    return style


@lru_cache(maxsize=128)
def _text_tint(tint):
    # The editor's neutral foreground, nudged toward the item's hue.
    neutral = (169 / 255, 183 / 255, 198 / 255)
    hue = Tint.dd_text(tint[:3])
    return tuple(a * 0.8 + b * 0.2 for a, b in zip(neutral, hue))


def _running_dot(x, y, tint, *, ui_scale=1.0, channel=None):
    """A steady green activity indicator, independent of conversation paint."""
    draw_list = imgui.get_window_draw_list()
    if channel is not None:
        draw_list.channels_set_current(channel)
    draw_list.add_circle_filled(x, y, (3.5 * ui_scale), _color((0.12, 0.75, 0.28)), 16)


def _color(tint, alpha=1):
    return pack_color(*tint[:3], alpha)


def _button(draw_state, key, label, x, y, width, tint, enabled=True, height=None,
            selected=False, background=True, shadow=True, event="left_mouse_clicked", text_color=None, dimmed=False, *, ui_scale=1.0):
    cursor = imgui.get_cursor_screen_pos()
    try:
        imgui.set_cursor_screen_pos((x, y))
        return flat_button(label, draw_state if enabled else None, key,
            pos=(x, y), width=width, height=height or (25 * ui_scale), event=event, text_color=text_color,
            color=tint, alpha=(1 if enabled and not dimmed else 0.4) if background else 0,
            tint_value=0.23 if selected else 0.16,
            # A backgroundless button casts no shadow: the shadow pass would
            # paint its lit plate over whatever block the label sits in.
            # Shadow=False: only the selected one lifts (the source tabs).
            shadow=background and (shadow or selected),
            shadow_offset=(Toggles.Chat.selected_shadow_offset if selected else Toggles.Chat.shadow_offset)
                          if background and (shadow or selected) else 0, hovered=None if enabled else False)
    finally:
        imgui.set_cursor_screen_pos(cursor)


def _hovering(x, y, width, height, *, pointer):
    mouse_x, mouse_y = pointer
    return x <= mouse_x < x + width and y <= mouse_y < y + height


def _card(x, y, width, height, tint, selected=False, max_bg_value=None, shadow_offset=None, shadow=True, *, ui_scale=1.0, channel=None):
    # Fills paint on the BODY channel (as flat_button does): a fill one channel
    # below sits under the compositor's mask for this rank and its lit rim /
    # specular is masked out - the card showed with no highlight at all.
    draw_list = imgui.get_window_draw_list()
    if channel is not None:
        draw_list.channels_set_current(channel)
    if shadow:
        add_shadow((x, y, width, height),
                   offset=shadow_offset if shadow_offset is not None else (Toggles.Chat.selected_shadow_offset if selected else Toggles.Chat.shadow_offset),
                   corner_radius=(6 * ui_scale))
    _, color = draw_bg(left=x, top=y, width=width, height=height,
            style_manager=_tint_style(tuple(tint)), opacity=1, outline=False,
            rounding=(6 * ui_scale), max_bg_depth=1 if selected else 0,
            max_bg_value=(0.23 if selected else 0.18) if max_bg_value is None else max_bg_value,
            selected=selected)
    return color


def prose_offset(text, x, y, mouse, line_px, char_w, *, ui_scale=1.0):
    """The character offset in the pre-wrapped `text` (drawn at x, y, one
    line per `line_px`, `char_w` per character after the 4 px inset) that
    the pointer at `mouse` is on: rows and columns clamp to the text."""
    lines = str(text).split("\n")
    row = max(0, min(len(lines) - 1, int((mouse[1] - y) // line_px)))
    col = max(0, min(len(lines[row]), int(round((mouse[0] - x - (4 * ui_scale)) / char_w))))
    return sum(len(line) + 1 for line in lines[:row]) + col


def _caret(draw_state, key, x, y, width, height, expanded, tint, brightness=1.0, *, ui_scale=1.0):
    """A draw-list expand chevron with its own click subscription.

    Not an imgui button: the transcript is a cached tile, and an imgui item
    only exists on the frames its body runs, so clicks on it were lost. An
    on_action click is replayed on cache-served frames like every body action.
    """
    color = _tint_style(tuple(tint)).make_color_style_value(input={
        "value": 7.788, "saturation": 1.559, "max_value": 1.601})[:3]
    color = _color(tuple(c * brightness * float(Toggles.Melty.arrow_brightness) for c in color))
    cx, cy, radius = x + width / 2, y + height / 2, (3 * ui_scale)
    points = ((cx - radius, cy - radius / 2), (cx, cy + radius / 2),
              (cx + radius, cy - radius / 2)) if expanded else (
              (cx - radius / 2, cy - radius), (cx + radius / 2, cy), (cx - radius / 2, cy + radius))
    dl = imgui.get_window_draw_list()
    dl.add_line(*points[0], *points[1], color, (1.5 * ui_scale))
    dl.add_line(*points[1], *points[2], color, (1.5 * ui_scale))
    return draw_state.on_action("left_mouse_clicked", view_id=key, priority_delta=3,
                                rect=(x, y, x + width, y + height)) is not None
