"""Chat studio: draw-list navigation/transcript over provider-owned dictionaries."""
import colorsys
from bisect import bisect_right
import dataclasses
import math
import time
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path
import uuid

import meltygui_imgui as imgui
import numpy as np
from meltygui.hdr_color import pack_color
import meltygui.window_api as glfw

from meltygui.melty import Melty
from meltygui.chat.messages import Message
from meltygui.chat.messages import AssistantMessage
from meltygui.chat.messages import UserMessage
from meltygui.chat.messages import ToolCall
from meltygui.chat.messages import ReasoningMessage
from meltygui.chat.messages import PythonString
from meltygui.chat.messages import CodeString
from meltygui.chat.messages import Reference
from meltygui.chat.messages import ImageReference
from meltygui.chat.messages import user_message
from meltygui.chat.messages import BashString
from meltygui.chat.messages import ToolOutput
from meltygui.chat.messages import FileTags
from meltygui.chat.messages import CommandExecution
from meltygui.chat.messages import input_text
import meltygui.chat.images as chat_images
from meltygui.models.file_meta import FileMeta
from meltygui.models.file_meta import file_meta_store
from meltygui.files.fast_file_explorer import set_row_tint
from meltygui.toggles import Tint
from meltygui.toggles import Toggles
from meltygui.fonts import Font
from meltygui.state.dict_conversion import DictConversion
from meltygui.core.column_core import ColumnLayout
from meltygui.core.column_core import RowLayout
from meltygui.core.drag_drop_core import DragDrop
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.core_decoration import no_save
from meltygui.rendering.decorators.window_decoration import window
from meltygui.core.render_dispatch import draw_tuple_fast
from meltygui.core.render_dispatch import draw_bg
from meltygui.core.header_runtime import flat_button
from meltygui.core.tile_cache import add_shadow
from meltygui.view.texture_view import draw_texture
import meltygui.accounts.internet_accounts as internet_accounts


from meltygui.state.chat_state import ChatInterfaceState


TRASH_ICON = ""   # FontAwesome trash-alt

def image_cache():
    """The transcript's pictures, decoded once per process (chat/images.py)."""
    cache = getattr(Melty, "chat_image_cache", None)
    if cache is None:
        from meltygui.utils.glfw_utils import request_render
        cache = Melty.chat_image_cache = chat_images.ImageCache(wake=request_render)
    return cache


def image_box(entry, max_width, size=None):
    """The (w, h) a picture takes in the transcript: fitted into the span
    and Toggles.Chat.image_max_height, a placeholder while it decodes."""
    max_height = Melty.px(Toggles.Chat.image_max_height)
    if entry is None or entry.status == "failed":
        return max_width, 0
    if entry.size is None:
        return min(max_width, Melty.px(240)), Melty.px(120)
    if size is not None:
        return min(max_width, size[0]), size[1]
    return chat_images.fitted_size(entry.size, max_width, max_height)


# A conversation counts as ACTIVE (the live dot) while a turn runs here or
# its session was written within this many seconds by anyone - a provider's
# Claude Code or another window; the window asks each backend to look every
# REFRESH_S seconds.
ACTIVE_WINDOW_S = 30
REFRESH_S = 5


def is_active(chat, now=None):
    return bool(chat["running"] or chat.get("external_busy")) or ((now if now is not None else time.time())
                                     - (chat.get("updated") or 0.0)) < ACTIVE_WINDOW_S


# The sidebar's age filter chips: label → hours (0 = every conversation).
AGE_FILTERS = (("1h", 1), ("2h", 2), ("Day", 24), ("2 days", 48), ("All", 0))


def age_cutoff(hours, now=None):
    """The epoch second before which a conversation is out of the filter,
    or None for no filter."""
    return None if not hours else (now if now is not None else time.time()) - hours * 3600


def recent_chat_time(chat):
    # Unsent drafts have no user message yet. The fixed creation time keeps
    # them visible at the top without allowing response activity to reorder them.
    return chat.get("last_user_at") or chat.get("created_at") or 0.0


def sidebar_visible(key, chat, cutoff, selected=None):
    """Whether a conversation stays in the filtered sidebar: recent enough,
    running (active now), or the open one (its transcript is showing)."""
    return (cutoff is None or is_active(chat) or key == selected
            or (chat.get("updated") or 0.0) >= cutoff)


def _running_dot(x, y, tint):
    """A steady green activity indicator, independent of conversation paint."""
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    draw_list.add_circle_filled(x, y, Melty.px(3.5), _color((0.12, 0.75, 0.28)), 16)


def _color(tint, alpha=1):
    return pack_color(*tint[:3], alpha)


def _button(draw_state, key, label, x, y, width, tint, enabled=True, height=None,
            selected=False, background=True, shadow=True, event="left_mouse_clicked", text_color=None, dimmed=False):
    cursor = imgui.get_cursor_screen_pos()
    try:
        imgui.set_cursor_screen_pos((x, y))
        return flat_button(label, draw_state if enabled else None, key,
            pos=(x, y), width=width, height=height or Melty.px(25), event=event, text_color=text_color,
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


def _tint_chip(meta, draw_state, key, x, y):
    changed, tint = draw_tuple_fast(tuple(meta["tint"]), draw_state, key,
        x=x, y=y, size=Melty.px(17), outline=True, priority_delta=6,
        setter=lambda value: meta.__setitem__("tint", value))
    if changed:
        meta["tint"] = tint
    return changed


def project_tint(project):
    """The tint painted on the project's directory in the shared file-meta
    store (what the file browser and the studio paint), or None."""
    if not project:
        return None
    return FileMeta.painted_tint(file_meta_store().get(str(project)))


def conversation_folder_tint(project):
    """Header folder color; apps may supply a color for unpainted folders."""
    return project_tint(project)


def conversation_tint_setter(meta):
    """A writer for a conversation's own tint in the chat metadata: a tuple
    paints it, None (the picker's clear) unpaints it."""
    def write(value):
        if value is None:
            meta.pop("tint", None)
        else:
            meta["tint"] = tuple(value)
    return write


def _tint_slot(draw_state, key, tint, x, y, hovered, default_tint, setter, show_brush=True, brush_tint=None):
    """A row's tint control, the file browser's: the colour chip when the row
    is painted, a faint paint-brush when not (a click stamps `default_tint`
    in and opens the picker; `show_brush` False draws none — the selected
    conversation and a hovered heading show theirs, the rest stay tidy).
    Returns True when a tint was written."""
    from meltygui.files.fast_file_explorer import tint_control
    size = Melty.px(13)
    text_y = y + max(0.0, (size - imgui.get_text_line_height()) / 2)
    return tint_control(draw_state, key, tuple(tint) if tint else None, x, y, size, text_y, hovered,
                        tuple(default_tint), setter=setter, show_brush=show_brush,
                        brush_color=tuple(c * 0.55 * float(Toggles.Melty.arrow_brightness) for c in
                            _tint_style(tuple(brush_tint or default_tint)).make_color_style_value(input={
                                "value": 7.788, "saturation": 1.559, "max_value": 1.601})[:3]))


def _hovering(x, y, width, height):
    mouse_x, mouse_y = imgui.get_mouse_pos()
    return x <= mouse_x < x + width and y <= mouse_y < y + height


def _reorder(chats, key, direction):
    keys = list(chats)
    index = keys.index(key)
    # Move within this project's group; keep other projects untouched.
    project = dict.__getitem__(chats, key)["project"]
    candidates = [other for other in keys if dict.__getitem__(chats, other)["project"] == project]
    position = candidates.index(key) + direction
    if not 0 <= position < len(candidates):
        return
    other_index = keys.index(candidates[position])
    keys[index], keys[other_index] = keys[other_index], keys[index]
    for item in keys:
        chats[item] = chats.pop(item)


def _soft_wrap(text, columns):
    """Word-wrap each line to `columns` monospace cells; indentation and blank lines survive."""
    if columns <= 0:
        return text
    out = []
    for line in text.split("\n"):
        stripped = line.lstrip(" \t")
        lead = line[:len(line) - len(stripped)]
        room = max(1, columns - len(lead.expandtabs(4)))
        while len(stripped) > room:
            cut = stripped.rfind(" ", 0, room + 1)
            if cut <= 0:
                cut = room  # one word wider than the row: break it
            out.append(lead + stripped[:cut].rstrip())
            stripped = stripped[cut:].lstrip(" ")
        out.append(lead + stripped)
    return "\n".join(out)


def _text_layout(state, key, text, prefix="", font=Font.FONTAWESOME_MONO_19, wrap_width=None, keep=False):
    """Stable display buffer and height; no per-frame string/kwargs churn.

    Measure in the same font as draw_text. Explicit line breaks and a fixed
    height keep offscreen layout identical to onscreen layout, like the
    measured-pane skips in draw_stack_trace. `wrap_width` (px) soft-wraps the
    DISPLAY buffer here — draw_text itself never reflows — so the height is
    the wrapped height and the memo re-runs when the width changes.
    """
    font = Melty.font_mgr.get(font) if Melty.font_mgr else None
    signature = (text, type(text), getattr(text, "language", None), Melty.ui_scale, font, prefix, "trimmed", wrap_width)
    memo = state.text_layouts.get(key)
    if memo is not None and memo[0][0] is text and (memo[0][1:] == signature[1:]
                                                     or keep and memo[0][1:-1] == signature[1:-1]):
        # `keep`: an off-screen leaf mid-resize keeps its last wrap, whatever width it was in.
        return memo[1], memo[2]
    if font is not None:
        imgui.push_font(font)
    try:
        line_px = imgui.get_text_line_height() * 1.2
        display = prefix + text if prefix else text
        if wrap_width is not None:
            # The editor's text is a little in on its left edge; leave that slack.
            columns = int((wrap_width - Melty.px(16)) / max(1.0, imgui.calc_text_size("M").x))
            display = _soft_wrap(str(display), columns)
        if display.endswith(("\n", "\r")) or display is not text:
            trimmed = display.rstrip("\r\n")
            display = type(text)(trimmed, text.language) if isinstance(text, CodeString) else type(text)(trimmed)
        # str(): an EMPTY string subclass splits to [itself], and imgui's typed
        # `text` argument refuses subclasses (MarkdownString) — the 07:49 crash.
        text_width = max((imgui.calc_text_size(line).x for line in str(display).split("\n")), default=0)
        memo = (signature, display, (display.count("\n") + 1) * line_px, text_width)
        state.text_layouts[key] = memo
        return memo[1], memo[2]
    finally:
        if font is not None:
            imgui.pop_font()


def _scroll_position(view, content_height, height, wheel=0, drag_fraction=None):
    maximum = max(0.0, content_height - height)
    offset = view.get("offset", 0.0)
    if wheel:
        speed = min(Toggles.ScrollSettings.scroll_speed,
                    Toggles.ScrollSettings.max_increment_fraction * height)
        offset -= wheel * speed
        view["follow"] = False
    if drag_fraction is not None:
        offset = maximum * drag_fraction
        view["follow"] = False
    offset = max(0.0, min(offset, maximum))
    if view.get("follow"):
        offset = maximum
    elif (wheel < 0 or drag_fraction is not None) and maximum > 0 and offset >= maximum - 1:
        view["follow"] = True
    view["offset"] = offset
    return offset


@contextmanager
def _viewport(draw_state, state, key, width, height, content_height, follow=False):
    """One clipped, independently scrolling region, owned by the window.

    Only its visible footprint advances layout; content offsets never move
    the composer or change the window's own scroll geometry.
    """
    view = state.viewports.setdefault(key, {"offset": 0.0, "follow": follow})
    x, y = imgui.get_cursor_screen_pos()
    rect = (x, y, x + width, y + height)
    wheel = draw_state.on_action("scroll_y_changed", view_id=key + ":wheel",
                                 rect=rect, priority_delta=40)
    offset = _scroll_position(view, content_height, height, wheel.value if wheel is not None else 0)
    maximum = max(0, content_height - height)
    bar_width = Melty.px(7)
    thumb_height = min(height, max(Melty.px(24), height * height / max(1.0, height, content_height)))
    travel = height - thumb_height
    if maximum > 0 and travel > 0:
        thumb_y = y + travel * offset / maximum
        grab = (x + width - bar_width, thumb_y, x + width, thumb_y + thumb_height)
        for event in ("left_mouse_down", "left_mouse_held", "left_mouse_clicked"):
            draw_state.on_action(event, view_id=key + ":grab", rect=grab, priority_delta=50)
        drag = draw_state.on_action("left_mouse_drag", view_id=key + ":grab", rect=grab, priority_delta=50)
        if drag is not None:
            offset = _scroll_position(view, content_height, height,
                                      drag_fraction=offset / maximum + drag.dy / travel)
    Melty.push_clip(rect)
    try:
        yield x, y - offset, Melty.get_clip_rect()
        if maximum > 0 and travel > 0:
            thumb_y = y + travel * offset / maximum
            add_shadow((x + width - bar_width, thumb_y, bar_width, thumb_height),
                       offset=Toggles.Chat.shadow_offset, corner_radius=3)
            imgui.get_window_draw_list().add_rect_filled(
                x + width - bar_width, thumb_y, x + width, thumb_y + thumb_height,
                _color((0.55, 0.65, 0.65)), rounding=3)
    finally:
        Melty.pop_clip()
        imgui.set_cursor_screen_pos((x, y))
        imgui.dummy(width, height)


def _visible(y, height, clip):
    return clip is None or y + height > clip[1] and y < clip[3]


@lru_cache(maxsize=128)
def _tint_style(tint):
    from meltygui.core.style_core import ImGuiStyleManager
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


def _card(x, y, width, height, tint, selected=False, max_bg_value=None, shadow_offset=None, shadow=True):
    # Fills paint on the BODY channel (as flat_button does): a fill one channel
    # below sits under the compositor's mask for this rank and its lit rim /
    # specular is masked out - the card showed with no highlight at all.
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    if shadow:
        add_shadow((x, y, width, height),
                   offset=shadow_offset if shadow_offset is not None else (Toggles.Chat.selected_shadow_offset if selected else Toggles.Chat.shadow_offset),
                   corner_radius=Melty.px(6))
    _, color = draw_bg(left=x, top=y, width=width, height=height,
            style_manager=_tint_style(tuple(tint)), opacity=1, outline=False,
            rounding=Melty.px(6), max_bg_depth=1 if selected else 0,
            max_bg_value=(0.23 if selected else 0.18) if max_bg_value is None else max_bg_value,
            selected=selected)
    return color


_LABEL_WIDTHS = {}


def _label_width(text):
    """Width of `text` in the editor font, memoised: the same tool labels,
    captions and titles are measured every frame."""
    key = (text, Melty.ui_scale, Melty.font_mgr is not None)
    width = _LABEL_WIDTHS.get(key)
    if width is not None:
        return width
    if len(_LABEL_WIDTHS) > 8192:
        _LABEL_WIDTHS.clear()
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    try:
        width = _LABEL_WIDTHS[key] = imgui.calc_text_size(text).x
        return width
    finally:
        if font is not None:
            imgui.pop_font()


def _prose_metrics():
    """(line height, character width) of prose in the editor's mono font."""
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    try:
        return imgui.get_text_line_height() * 1.2, max(1.0, imgui.calc_text_size("M").x)
    finally:
        if font is not None:
            imgui.pop_font()


def prose_offset(text, x, y, mouse, line_px, char_w):
    """The character offset in the pre-wrapped `text` (drawn at x, y, one
    line per `line_px`, `char_w` per character after the 4 px inset) that
    the pointer at `mouse` is on: rows and columns clamp to the text."""
    lines = str(text).split("\n")
    row = max(0, min(len(lines) - 1, int((mouse[1] - y) // line_px)))
    col = max(0, min(len(lines[row]), int(round((mouse[0] - x - Melty.px(4)) / char_w))))
    return sum(len(line) + 1 for line in lines[:row]) + col


def selection_slice(selection, index, length):
    """The (lo, hi) character span of prose leaf `index` inside a transcript
    selection ({anchor: (leaf, offset), head: (leaf, offset)}), None when the
    leaf is outside it or the selection is empty."""
    (first, first_offset), (last, last_offset) = sorted([tuple(selection["anchor"]), tuple(selection["head"])])
    if (first, first_offset) == (last, last_offset) or not first <= index <= last:
        return None
    lo = first_offset if index == first else 0
    hi = last_offset if index == last else length
    return (lo, hi) if hi > lo else None


def selection_text(selection, texts):
    """The selected prose, leaves joined by a blank line."""
    parts = []
    for index, text in enumerate(texts):
        span = selection_slice(selection, index, len(text))
        if span is not None:
            parts.append(str(text)[span[0]:span[1]])
    return "\n\n".join(parts)


def _draw_prose(text, x, y, width, height, tint, clip=None, selected=None, **_):
    """User / assistant prose straight to the draw list: pre-wrapped lines, editor
    font, no render wrapper, no child tile. Rows outside `clip` are skipped.
    ``selected`` = (lo, hi) paints the selection plate behind those characters
    (draw_messages tracks the drag; Ctrl+C copies)."""
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    Melty.push_clip((x, y, x + width, y + height))
    try:
        line_px = imgui.get_text_line_height() * 1.2
        color = _color(_text_tint(tuple(tint)))
        if selected is not None:
            lo, hi = selected
            char_w = max(1.0, imgui.calc_text_size("M").x)
            plate = _color(_text_tint(tuple(tint)), 0.22)
            offset = 0
            for index, line in enumerate(str(text).split("\n")):
                top = y + index * line_px
                c0, c1 = max(lo - offset, 0), min(hi - offset, len(line))
                if c1 > c0 or (hi > offset + len(line) and lo <= offset + len(line) and c0 <= len(line)):
                    # a line inside the span; one that the span runs past gets a half-cell tail
                    tail = 0.5 if hi > offset + len(line) else 0.0
                    if _visible(top, line_px, clip):
                        draw_list.add_rect_filled(x + Melty.px(4) + c0 * char_w, top,
                                                  x + Melty.px(4) + (max(c1, c0) + tail) * char_w, top + line_px,
                                                  plate, rounding=Melty.px(2))
                offset += len(line) + 1
        for index, line in enumerate(str(text).split("\n")):
            top = y + index * line_px
            if _visible(top, line_px, clip) and line:
                draw_list.add_text(x + Melty.px(4), top, color, line)
    finally:
        Melty.pop_clip()
        if font is not None:
            imgui.pop_font()


def _icon_chip(icon, x, y, width, height, color):
    """One fixed-size flat rounded chip per header row, the glyph centred in it.

    Every row's chip is the same box whatever glyph it carries, so a column of
    rows reads as a column of tiles. Returns the x the glyph should be drawn at.
    """
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())  # body channel, see _card
    draw_list.add_rect_filled(x, y + Melty.px(1), x + width, y + height - Melty.px(1),
                              _color(color), rounding=Melty.px(4))
    return x + max(0, (width - _label_width(icon)) / 2)


def _caret(draw_state, key, x, y, width, height, expanded, tint, brightness=1.0):
    """A draw-list expand chevron with its own click subscription.

    Not an imgui button: the transcript is a cached tile, and an imgui item
    only exists on the frames its body runs, so clicks on it were lost. An
    on_action click is replayed on cache-served frames like every body action.
    """
    color = _tint_style(tuple(tint)).make_color_style_value(input={
        "value": 7.788, "saturation": 1.559, "max_value": 1.601})[:3]
    color = _color(tuple(c * brightness * float(Toggles.Melty.arrow_brightness) for c in color))
    cx, cy, radius = x + width / 2, y + height / 2, Melty.px(3)
    points = ((cx - radius, cy - radius / 2), (cx, cy + radius / 2),
              (cx + radius, cy - radius / 2)) if expanded else (
              (cx - radius / 2, cy - radius), (cx + radius / 2, cy), (cx - radius / 2, cy + radius))
    dl = imgui.get_window_draw_list()
    dl.add_line(*points[0], *points[1], color, Melty.px(1.5))
    dl.add_line(*points[1], *points[2], color, Melty.px(1.5))
    return draw_state.on_action("left_mouse_clicked", view_id=key, priority_delta=3,
                                rect=(x, y, x + width, y + height)) is not None


_ELLIPSIS_MEMO = {}


def _ellipsize(text, max_width):
    """`text` cut to `max_width` px with a trailing ellipsis, measured in the
    font pushed by the caller; memoized per (text, width, scale, font)."""
    key = (text, max_width, Melty.ui_scale)
    hit = _ELLIPSIS_MEMO.get(key)
    if hit is not None:
        return hit
    result = text
    if max_width > 0 and imgui.calc_text_size(text).x > max_width:
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if imgui.calc_text_size(text[:mid] + "…").x <= max_width:
                low = mid
            else:
                high = mid - 1
        result = text[:low].rstrip() + "…"
    if len(_ELLIPSIS_MEMO) > 4096:
        _ELLIPSIS_MEMO.clear()
    _ELLIPSIS_MEMO[key] = result
    return result


def _title(text, x, y, width, height, tint, brightness=1.0, ellipsis=False, text_color=None):
    """Navigation labels have no render wrapper or child tile; `brightness` dims
    the text, `ellipsis` trims it with a … instead of clipping."""
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    Melty.push_clip((x, y, x + width, y + height))
    try:
        if ellipsis:
            text = _ellipsize(text, width)
        draw_list.add_text(x, y + max(0, (height - imgui.get_text_line_height()) / 2),
                           _color(tuple(c * brightness for c in (text_color or _text_tint(tuple(tint))))), text)
    finally:
        Melty.pop_clip()
        if font is not None:
            imgui.pop_font()


def _apply_chat_drop(chats, keys, drop):
    """Dock drop indices refer to registered rows, including offscreen rows."""
    if drop is None or drop.kind != "reorder" or drop.key not in keys:
        return False
    project = chats.get(drop.key)["project"]
    group = [i for i, key in enumerate(keys) if chats.get(key)["project"] == project]
    if not min(group) <= drop.insert_index <= max(group) + 1:
        return False
    rows = {key: chats.get(key) for key in keys}
    if not drop.apply(rows):
        return False
    # Collapsed projects retain their order. Mutations are dict-only;
    # the proxy records order without archiving deleted-and-reinserted rows.
    reordered = iter(rows)
    order = [next(reordered) if key in rows else key for key in chats]
    for key in order:
        chats[key] = chats.pop(key)
    return True


def chat_sources(accounts):
    """Every account of a chat kind, in provider order: [(account_id, kind)].
    One tab each in the sidebar's source bar."""
    return [(entry["id"], kind)
            for kind in internet_accounts.KINDS.values() if kind.chat_label
            for entry in accounts.of_kind(kind.name)]


def conversation_source_tag(kind, chat):
    from meltygui.chat.chat_proxy import writer_conflict
    label = "Claude" if kind.name == "anthropic" else kind.chat_label
    locked = chat.get("locked", writer_conflict(getattr(chat, "error", None)))
    return label + " \uf023" if locked else label


def source_initials(label):
    """A provider's two-letter tag for a mixed list: "Claude Code" → "CC",
    "Codex" → "Co"."""
    words = [word for word in str(label).split() if word]
    if len(words) >= 2:
        return "".join(word[0] for word in words[:2]).upper()
    return (words[0][:2] if words else "?").capitalize()


def source_label(account_id, kind, accounts):
    """A tab's text: the provider, plus the account when the kind has several."""
    if len(accounts.of_kind(kind.name)) > 1:
        return f'{kind.chat_label} · {accounts[account_id]["label"]}'
    return kind.chat_label


def pick_source(state, account_id, kinds, additive=False):
    """A source tab click. Plain: that source alone. ``additive`` (Shift held):
    toggle it in or out of the shown set, never down to none. The primary
    account — the transcript's, where New conversation goes — follows the
    click, or falls back to the first shown source when it was toggled out."""
    shown = list(getattr(state, "sources", None) or [state.account])
    if not additive:
        shown = [account_id]
    elif account_id in shown:
        if len(shown) > 1:
            shown.remove(account_id)
    else:
        shown.append(account_id)
    state.sources = shown
    state.account = account_id if account_id in shown else shown[0]
    state.provider = kinds[state.account]


def apply_folder_shortcuts(state, events):
    """Set every current and subsequently discovered folder to the requested state."""
    changed = False
    for key, mods in events:
        if mods & (glfw.MOD_CONTROL | glfw.MOD_SHIFT) == (glfw.MOD_CONTROL | glfw.MOD_SHIFT):
            if key in (glfw.KEY_EQUAL, glfw.KEY_KP_ADD, glfw.KEY_MINUS, glfw.KEY_KP_SUBTRACT):
                state.folders_default_expanded = key in (glfw.KEY_EQUAL, glfw.KEY_KP_ADD)
                state.folder_expanded = {}
                state.revision += 1
                changed = True
    return changed


from meltygui.view.chat_view import draw_chat_sidebar  # the height the list actually uses


def _message_leaves(value, path=(), depth=0):
    """Keep nested arguments/results inspectable without a renderer per dict."""
    if isinstance(value, Reference):
        yield path, value, depth
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _message_leaves(child, path + (str(key),), depth + 1)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _message_leaves(child, path + (str(index),), depth + 1)
    elif value is not None:
        yield path, value, depth


def _code_background(x, y, width, height, color, shadow=None):
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())  # body channel, see _chat
    shadow = Toggles.Chat.shadow_offset if shadow is None else shadow
    if shadow:
        add_shadow((x, y, width, height), offset=shadow, corner_radius=Melty.px(6))
    draw_list.add_rect_filled(x, y, x + width, y + height, _color(color), rounding=Melty.px(6))


def _terminal_layout(state, key, message, width):
    """Cache a passive VT screen by input identity; never start a shell."""
    import pyte
    import re
    command = message["content"]["command"]
    output = message["content"].get("output", "")
    cwd = message["details"].get("cwd", "")
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    try:
        char_width = imgui.calc_text_size("M").x
        line_height = imgui.get_text_line_height() * 1.2
    finally:
        if font is not None:
            imgui.pop_font()
    show_all = state.output_expanded.get(key, False)
    signature = (command, output, cwd, line_height, font, Melty.ui_scale, "preview", show_all)
    memo = state.text_layouts.get((key, "terminal"))
    if (memo is not None and all(a is b for a, b in zip(memo[0][:3], signature[:3]))
            and memo[0][3:] == signature[3:]):
        return memo[1], memo[2]
    parts = output.split("\n", 5)
    has_more = len(parts) > 5 and bool(parts[5].rstrip("\r\n"))
    shown_output = output if show_all or not has_more else "\n".join(parts[:5])
    prompt = ("\x1b[1;32m" + str(cwd) + "\x1b[0m" if cwd else "") + "\x1b[1;32m$\x1b[0m "
    text = prompt + str(command).rstrip("\r\n") + ("\n" + str(shown_output) if shown_output else "")
    # The captured terminal has a maximum content width, independent of the view.
    # Allow two cells per code point for wide glyphs; the sparse grid trims the rest.
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    columns = max(1, max(len(line.expandtabs(8)) for line in plain.split("\n")) * 2)
    screen = pyte.Screen(columns, text.count("\n") + 2)
    screen.set_mode(20)  # LF also returns to column zero, as captured shell output expects.
    pyte.Stream(screen).feed(text)
    last = max((index for index, row in screen.buffer.items() if row), default=0)
    grid = tuple(tuple(screen.buffer[row][col] for col in range(max(screen.buffer[row], default=-1) + 1))
                 for row in range(last + 1))
    value = (grid, char_width, line_height)
    height = len(grid) * line_height + Melty.px(8)
    state.text_layouts[(key, "terminal")] = (signature, value, height, has_more,
        max((len(row) for row in grid), default=0) * char_width + Melty.px(8))
    return value, height


from meltygui.view.chat_view import draw_chat_terminal


def _message_preview(message):
    """The first line a collapsed row would otherwise hide, markdown emphasis stripped."""
    if isinstance(message, CommandExecution):
        source = str(message["content"].get("command", ""))
    elif isinstance(message, ReasoningMessage):
        source = "\n".join(str(value) for _, value, _ in _message_leaves(message["content"])
                          if isinstance(value, str))
    else:
        return ""
    for line in source.splitlines():
        line = line.strip().strip("*_#`").strip()
        if line:
            return line
    return ""


def _message_icon(message):
    """The glyph that leads a row: pencil for writes, terminal for bash, brain for thinking."""
    bash_icon = f""
    write_icon = f""  # pencil-alt: the shipped face is FontAwesome 5, no f040 pencil
    thinking_icon = f""
    if isinstance(message, ReasoningMessage):
        return thinking_icon
    if not isinstance(message, ToolCall):
        return ""
    files = message.get("summary", {})
    if any(entry.get("access", "write") == "write" for entry in files.values()):
        return write_icon
    if isinstance(message["content"].get("command"), BashString):
        return bash_icon
    return ""


def _message_label(message, expanded):
    label = message.label
    if isinstance(message, ReasoningMessage):
        return "" if expanded else _message_preview(message)
    if isinstance(message, ToolCall):
        icon = _message_icon(message)
        if icon:
            label = icon
            # Read-only file references still leave a summary dict behind; only a
            # WRITE row (tags shown) drops the first-line preview.
            writes = any(entry.get("access", "write") == "write" for entry in message.get("summary", {}).values())
            preview = "" if expanded or writes else _message_preview(message)
            if preview:
                label += "  " + preview
        else:
            name = message["details"].get("tool") or message["details"].get("name")
            if name:
                label += " · " + str(name)
    return label


def _message_failed(message):
    details = message["details"]
    return (message.get("status") in ("failed", "declined", "error")
            or details.get("exitCode") not in (None, 0)
            or bool(details.get("error")))


def _failure_badge(x, y, width, height):
    icon = f""
    cursor = imgui.get_cursor_screen_pos()
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())  # body channel, see _card
    # Fixed design colours are scaled by Toggles.Chat.failed_badge_brightness so the
    # badge reads as a status indicator rather than an alarm.
    dim = Toggles.Chat.failed_badge_brightness
    draw_list.add_rect_filled(x, y, x + width, y + height,
                              _color(tuple(c * dim for c in (0.30, 0.055, 0.075))), rounding=Melty.px(4))
    try:
        imgui.set_cursor_screen_pos((x, y))
        flat_button(icon + " Failed", None, "chat-failed", width=width, height=height,
                    color=tuple(c * dim for c in (0.9, 0.16, 0.22)),
                    text_color=tuple(c * dim for c in (1.0, 0.76, 0.77)),
                    alpha=0, hovered=False, layout=False)
    finally:
        imgui.set_cursor_screen_pos(cursor)


def _hold_scroll_anchor(view, rows, height, restore, geometry=None):
    """Pin the top visible row to its screen position across relayouts.

    Each frame records (row key, pixels of that row above the viewport top);
    a frame whose heights moved (`restore`) re-derives the offset from it, so
    the reader's row stays put while rows above it re-wrap. Follow mode wins.
    """
    if geometry is None:
        geometry = _row_geometry(rows)
    tops, ends = geometry
    total = ends[-1] if ends else 0.0
    anchor = view.get("anchor")
    if restore and anchor is not None and not view.get("follow") and anchor[0] in tops:
        view["offset"] = max(0.0, min(tops[anchor[0]] + anchor[1], max(0.0, total - height)))
    offset = view.get("offset", 0.0)
    index = bisect_right(ends, offset)
    view["anchor"] = ((rows[index][0], offset - tops[rows[index][0]])
                      if index < len(rows) else None)


def _row_geometry(rows):
    """Index the transcript once per layout, for scrolling and visible drawing."""
    tops, ends, y = {}, [], 0.0
    for row in rows:
        tops[row[0]] = y
        y += row[3]
        ends.append(y)
    return tops, ends


def _draw_image(ref, x, y, max_width, box_height, caption_height, tint, *, name="image", size=None):
    """An interactive texture with a caption, or a decode placeholder/error."""
    cache = image_cache()
    entry = cache.entry(ref)
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())  # body channel, see _card
    width, height = image_box(entry, max_width, size)
    rendered_size = None
    # Decode can finish between measuring this row and painting it. Keep this
    # frame inside the reserved box; the decode generation reflows the next one.
    if height > box_height:
        scale = max(0, box_height) / height
        width, height = width * scale, height * scale
    caption = chat_images.image_label(ref, entry)
    if entry is not None and entry.status == "ready":
        texture = cache.texture(entry)
        if texture is not None:
            imgui.set_cursor_screen_pos((x, y))
            # Seed the fitted box once; setting width/height each frame would
            # disable the texture view's built-in right-drag resize.
            _, _, image_state = draw_texture(
                np.uint32(texture), name=name, initial={"width": width, "height": height},
                auto_resize=False, fill_height=False, max_width=max_width, return_extras=True,
                min_width=Melty.px(35), min_height=Melty.px(35),
                show_header=False, show_bg=False, with_header=None, show_footer=False, show_info=False, flip_y=True)
            rendered_size = (image_state.width, image_state.height)
            width, height = rendered_size
    elif entry is not None and entry.status == "failed":
        caption = (caption + " · " if caption else "") + "could not decode: " + str(entry.error)
    else:
        draw_list.add_rect_filled(x, y, x + width, y + height, _color(tint, 0.12), rounding=Melty.px(6))
        caption = (caption + " · " if caption else "") + "decoding…"
    _title(caption or ref.label, x, y + height, max_width, caption_height, tint, brightness=0.7, ellipsis=True)
    return rendered_size


def transcript_entries(messages, state, key):
    """Group uninterrupted actions without hiding the prose around them."""
    if not getattr(state, "concise", False):
        yield from messages.items()
        return
    from itertools import groupby
    for actions, entries in groupby(messages.items(),
            key=lambda entry: isinstance(entry[1], Message) and not isinstance(entry[1], (UserMessage, AssistantMessage))):
        if not actions:
            yield from entries
            continue
        entries = list(entries)
        identifier = "actions:" + entries[0][0]
        counts = {}
        for _, message in entries:
            label = message.label
            counts[label] = counts.get(label, 0) + 1
        summary = ", ".join(f"{count} {label.lower()}{'s' if count > 1 and not label.endswith('s') else ''}"
                            for label, count in counts.items())
        if len(entries) == 1:
            preview = _message_preview(entries[0][1])
            if preview:
                summary += " · " + preview
        failures = sum(_message_failed(message) for _, message in entries)
        if failures:
            summary += f" · {failures} failed"
        group = Message("actionGroup", details={"label": summary})
        yield identifier, group
        if getattr(state, "action_groups", {}).get(key + ":" + identifier, False):
            yield from entries


def chat_activity(chat, provider):
    """Describe the latest reported work; never infer work from idle history."""
    if chat["requests"]:
        return "Waiting for your approval or input"
    if not chat["running"]:
        return "Working in another session" if chat.get("external_busy") else "Recent activity"
    for message in reversed(chat["messages"].values()):
        if isinstance(message, UserMessage):
            break
        if isinstance(message, ToolCall) and message.get("status") not in ("completed", "failed", "declined"):
            progress = message["content"].get("progress")
            detail = str(progress).strip().splitlines()[-1] if progress else _message_preview(message)
            if not detail:
                detail = ", ".join(message.get("summary", {}))
            tool = message["details"].get("tool") or message["details"].get("name") or message.label
            return f"{tool}: {' '.join(detail.split())[:160]}" if detail else str(tool)
        if isinstance(message, ReasoningMessage) and message.get("status") != "completed":
            summary = _message_preview(message)
            return "Thinking: " + " ".join(summary.split())[:160] if summary else "Thinking…"
        if isinstance(message, AssistantMessage):
            return "Writing a response…" if message.get("status") != "completed" else "Working on the next step…"
    return f"{provider} is starting the next step…"


from meltygui.view.chat_view import draw_chat_queue


from meltygui.view.chat_view import draw_messages


from meltygui.core.chat_core import _cleanup_chat


from meltygui.view.chat_view import draw_chat_requests


from meltygui.view.chat_view import draw_conversation_title


def navigation_heading_control(pane, draw_state, state, x, y, width, height):
    """App extension: draw a trailing heading control; return changed, width used."""
    return False, 0


def navigation_row_sizes(state, edges, opened, top, height, minimum):
    """Remember expanded sizes; redistribute space only when a section toggles.

    Keep edge identities for resize captures and the collision graph.
    RowLayout enforces the floors and caps afterwards.
    """
    previous = getattr(state, "navigation_opened", tuple(opened))
    sizes = list(getattr(state, "navigation_sizes", [height / len(opened)] * len(opened)))
    valid = (isinstance(edges, list) and len(edges) == len(opened) + 1
             and all(isinstance(edge, dict) and "y" in edge for edge in edges))
    if valid:
        for index, expanded in enumerate(previous):
            if expanded:
                sizes[index] = max(minimum, edges[index + 1]["y"] - edges[index]["y"])
    state.navigation_sizes = sizes
    state.navigation_opened = tuple(opened)
    available = max(0, height - minimum * len(opened))
    weights = [max(0, size - minimum) if expanded else 0
               for size, expanded in zip(sizes, opened)]
    total = sum(weights)
    fitted = [minimum + (available * (weight / total if total else 1 / sum(opened))
                         if expanded else 0)
              for weight, expanded in zip(weights, opened)]
    if valid and tuple(previous) != tuple(opened):
        cursor = top
        for index, size in enumerate(fitted[:-1]):
            cursor += size
            edges[index + 1]["y"] = cursor
    return fitted


from meltygui.view.chat_view import draw_chat_navigation


def chat_context_menu_items(state, sources):
    """The wrapper owns right-release routing; rows only identify its target."""
    def action(operation):
        target = getattr(state, "chat_menu", None)
        if not target:
            return
        account_id, key = target["account"], target["key"]
        proxy = next((proxy for account, proxy, *_ in sources if account == account_id), None)
        if proxy is None or key not in proxy:
            return
        if operation == "rename":
            state.rename = {"account": account_id, "key": key, "pane": target.get("pane", "all"),
                            "draft": proxy.get(key)["title"], "focus": True}
        elif operation == "fork":
            new_key = proxy.fork(key)
            if new_key:
                state.account = account_id
                state.selected[account_id] = new_key
        else:
            del proxy[key]
        state.revision += 1
    return {"Rename chat": lambda: action("rename"),
            "Fork chat": lambda: action("fork"),
            "Delete chat": lambda: action("delete")}



def chat_models(kind, proxy, selected_model=""):
    models = dict(getattr(proxy, "models", {}))
    if selected_model and selected_model != "default" and selected_model not in models.values():
        models[selected_model] = selected_model
    return models


def is_new_chat(chat):
    return chat.loaded and not chat["messages"] and not chat["running"] and not chat.get("queued_messages")


def chat_project_choices(state, proxies, current, *defaults):
    """Use directory paths as labels so identically named folders stay distinct."""
    projects = {current, *defaults, *state.projects.values(),
                *getattr(state, "added_folders", [])}
    for proxy in proxies.values():
        if proxy is not None:
            projects.update(chat.get("project") for chat in dict.values(proxy))
    return {project: project for project in sorted(filter(None, projects), key=str.casefold)}


def switch_new_chat_project(state, proxies, project):
    """Replace an empty session: providers bind the cwd when creating it."""
    proxy = proxies[state.account]
    key = state.selected[state.account]
    chat = proxy[key]
    if not is_new_chat(chat) or chat.get("external_busy") or chat.get("locked") or chat["project"] == project:
        return False
    new_key = str(uuid.uuid4())
    proxy[new_key] = {"title": chat["title"], "project": project,
                      "created_at": chat.get("created_at", 0), "updated": chat.get("updated", 0)}
    proxy[new_key].metadata.update({field: value for field, value in chat.metadata.items()
        if field in ("permissions", "model", "effort", "model_explicit", "model_selected_at",
                     "permissions_selected_at", "effort_selected_at", "service_tier", "service_tier_selected_at")})
    state.drafts[state.account + ":" + new_key] = state.drafts.pop(state.account + ":" + key, "")
    state.selected[state.account] = new_key
    state.projects[state.account] = project
    del proxy[key]
    return True


def chat_effort_levels(kind, proxy, model):
    levels = getattr(proxy, "model_efforts", {}).get(model)
    if levels is None:
        levels = ("low", "medium", "high") if kind.name == "anthropic" else ()
    return tuple(levels)


from meltygui.view.chat_view import draw_effort_slider


def switch_new_chat_source(state, proxies, kinds, key, account_id, model):
    """Move an unsent draft; never move a provider's existing transcript."""
    previous = state.account
    chat = proxies[previous][key]
    if not is_new_chat(chat):
        return False
    if account_id != previous:
        target = proxies[account_id]
        metadata = {field: chat.metadata[field] for field in ("permissions", "tint", "model", "effort")
                    if field in chat.metadata}
        target[key] = {"title": chat["title"], "project": chat["project"],
                       "created_at": chat.get("created_at", 0), "updated": chat.get("updated", 0)}
        target[key].metadata.update(metadata)
        del proxies[previous][key]
        state.selected.pop(previous, None)
        state.drafts[account_id + ":" + key] = state.drafts.pop(previous + ":" + key, "")
        state.account = account_id
        state.provider = kinds[account_id].name
        if account_id not in state.sources:
            state.sources = [*state.sources, account_id]
        state.selected[account_id] = key
    proxies[account_id][key].metadata["model"] = model
    proxies[account_id][key].metadata["model_explicit"] = True
    proxies[account_id][key].metadata["model_selected_at"] = time.time()
    return True


from meltygui.view.chat_view import draw_chat_interface
draw_chat_interface = window(tint=(1.34, 1.62, 1.76), display_name='Chat', icon=f'\uf27a', initial={'width': 1100, 'height': 760})(draw_chat_interface)
