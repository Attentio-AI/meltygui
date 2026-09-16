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
from meltygui.views.columns import ColumnLayout
from meltygui.views.columns import RowLayout
from meltygui.views.drag_drop import DragDrop
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.core_decoration import no_save
from meltygui.rendering.decorators.window_decoration import window
from meltygui.views.new_core_view import draw_tuple_fast
from meltygui.views.new_core_view import draw_bg
from meltygui.views.headers import flat_button
from meltygui.views.blit_offscreen import add_shadow
from meltygui.views.texture_view import draw_texture
from meltygui.editor.text_editor import draw_text
import meltygui.accounts.internet_accounts as internet_accounts


@no_save("revision", "viewports", "text_layouts", "rename")
class ChatInterfaceState(DictConversion):
    def __init__(self):
        super().__init__()
        self.provider = ""
        self.account = ""
        self.selected = {}
        self.drafts = {}
        self.draft_generations = {}
        self.projects = {}
        self.follow = True
        self.answers = {}
        self.revision = 0
        self.viewports = {}
        self.text_layouts = {}
        self.rename = None
        self.chat_menu = None
        self.message_expanded = {}
        self.concise = False
        self.action_groups = {}
        self.output_expanded = {}
        self.image_sizes = {}
        # The sidebar's age filter: conversations active within this many
        # hours (0 = all). AGE_FILTERS lists the choices.
        self.age_hours = 0
        # The sources (account ids) whose conversations the sidebar shows -
        # the tabs lit in the source bar: empty = just `account`.
        self.sources = []
        self.folder_expanded = {}
        self.folders_default_expanded = True
        self.sections_expanded = {"recent": True, "all": True, "accounts": True}


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
    from meltygui.views.utils.imgui_style_manager_class import ImGuiStyleManager
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


def draw_chat_sidebar(sources, draw_state, state, width, height, cutoff=None, new_conversation=None, pane="all"):
    """Immediate layout; visible text leaves own their cached render tiles.
    ``sources`` is ``[(account_id, proxy, kind, label)]`` (a lone ChatProxy is
    accepted as the one source): their conversations MIX into one list,
    newest first (Recent uses the last user message, All uses last activity). With no ``cutoff`` (the All chip) a folder appears once,
    ranked by its newest conversation, holding all of its conversations.
    With a ``cutoff`` (epoch seconds, `age_cutoff`) the list is strictly
    chronological and a folder heading repeats wherever its conversations
    resume after another folder's, so a run of headings reads as a feed;
    conversations last active before the cutoff are out — except active
    ones and the selected one. Several sources: each row names its provider
    at the right. Selecting a row makes its source the primary account.
    ``new_conversation(account_id, proxy, project)`` serves every heading's
    + (a conversation in that folder, in the source of the run's newest
    conversation)."""
    from meltygui.chat.chat_proxy import ChatProxy
    if isinstance(sources, ChatProxy):
        sources = [(state.account, sources, None, "")]
    if not hasattr(state, "folder_expanded"):
        state.folder_expanded = {}
    changed = False
    archive = None
    trash_icon = TRASH_ICON
    trash_width = Melty.px(25)
    row_width = max(Melty.px(80), width - Melty.px(7))
    gap, pad = Melty.px(2), Melty.px(3)
    memos = state.viewports.setdefault("sidebar-cards", {})
    # The cards lay out the same until a source's conversations, the window's
    # state (a selection, a fold, a rename), the filter minute or the width change:
    # keep them between frames (scrolling changes none).
    signature = (tuple((account_id, getattr(chats, "revision", None), len(chats) if chats is not None else 0)
                       for account_id, chats, _, _ in sources),
                 getattr(state, "folders_default_expanded", True), tuple(state.folder_expanded.items()), state.revision, int(cutoff // 60) if cutoff else None, row_width, Melty.ui_scale,
                 new_conversation is not None, tuple(sorted(state.selected.items())), state.account)
    memo = memos.get(pane)
    if memo is not None and memo[0] == signature:
        cards = memo[1]
    else:
        entries = []
        for account_id, chats, kind, _label in sources:
            if chats is None:
                continue
            selected_key = state.selected.get(account_id)
            source_tint = tuple(kind.tint) if kind is not None else (0.6, 0.6, 0.6)
            for key, chat in chats.items():
                if (not any(part.startswith(".") and part not in (".", "..") for part in Path(chat["project"]).parts)
                        and sidebar_visible(key, chat, cutoff, selected_key)):
                    entries.append((-(recent_chat_time(chat) if pane == "recent" else (chat.get("updated") or 0.0)), account_id, chats, kind, key, chat, source_tint))
        entries.sort(key=lambda entry: entry[0])
        runs = []                       # [(project, [entry])], newest first
        if cutoff is None:
            by_project = {}
            for entry in entries:
                by_project.setdefault(entry[5]["project"], []).append(entry)
            runs = list(by_project.items())
        else:
            for entry in entries:
                if runs and runs[-1][0] == entry[5]["project"]:
                    runs[-1][1].append(entry)
                else:
                    runs.append((entry[5]["project"], [entry]))
        cards = []
        for index, (project, rows) in enumerate(runs):
            first_account, first_chats = rows[0][1], rows[0][2]
            # Recent feeds repeat folders: key each run to its oldest chat,
            # so a new conversation at its head does not move its fold state.
            folder_key = (pane, project, rows[-1][1], rows[-1][4]) if cutoff is not None else (pane, project)
            expanded = state.folder_expanded.get(folder_key, getattr(state, "folders_default_expanded", True))
            # The folder's tint is its directory's in the file-meta store; an
            # unpainted folder (and its unpainted rows) wears the source's.
            painted = project_tint(project)
            heading_tint = painted or rows[0][6]
            folder_label = (Path(project).name or project) if project else "No project"
            label, heading_height = _text_layout(state, "project:" + project, folder_label)
            heading_height += Melty.px(2)
            children = []
            if expanded:
                for _, account_id, chats, kind, key, chat, source_tint in rows:
                    text, row_height = _text_layout(state, "chat:" + account_id + ":" + key, " ".join(str(chat["title"]).split()))
                    children.append((account_id, chats, kind, key, chat.metadata, text, row_height + Melty.px(2),
                                     tuple(heading_tint)))
            card_height = pad * 2 + heading_height + sum(row[6] + gap for row in children)
            cards.append((index, project, folder_key, label, heading_height, rows, children, card_height,
                          painted, heading_tint, first_account, first_chats))
        memos[pane] = (signature, cards)
    total = sum(card[7] + 5 for card in cards)
    with _viewport(draw_state, state, "sidebar:" + pane + ":" + ":".join(source[0] for source in sources),
                   width, height, total) as (x, y, clip):
        for (index, project, folder_key, label, heading_height, rows, children, card_height,
             painted, heading_tint, first_account, first_chats) in cards:
            run_id = f"{pane}:{index}:{project}"
            if painted and _visible(y, card_height, clip):
                _card(x, y, row_width, card_height, heading_tint)   # an unpainted card has no plate
            heading_y = y + pad
            if _visible(heading_y, heading_height, clip):
                expanded = state.folder_expanded.get(folder_key, getattr(state, "folders_default_expanded", True))
                arrow_top = max(heading_y, clip[1]) if clip else heading_y
                arrow_bottom = min(heading_y + heading_height, clip[3]) if clip else heading_y + heading_height
                arrow_tint = heading_tint if painted else tuple(Melty.bg_color_stack[-1] if Melty.bg_color_stack else Melty.get_bg_color(-1))[:3]
                if _caret(draw_state, "project-arrow:" + run_id, x + pad, arrow_top,
                          Melty.px(17), max(0, arrow_bottom - arrow_top), expanded,
                          arrow_tint, brightness=0.55):
                    state.folder_expanded[folder_key] = not expanded
                    changed = True
                heading_hovered = _hovering(x, heading_y, row_width, heading_height)
                changed |= _tint_slot(draw_state, "project:" + run_id, painted,
                                      x + Melty.px(23), heading_y + max(0, (heading_height - Melty.px(13)) / 2),
                                      heading_hovered, heading_tint,
                                      lambda value, _p=project: set_row_tint(_p, value),
                                      show_brush=heading_hovered, brush_tint=arrow_tint)
                # The heading's +: a new conversation in this folder, in the
                # source of the run's newest conversation.
                plus_x = x + row_width - pad - trash_width
                if _button(draw_state, "project-heading:" + run_id, "", x + Melty.px(46), arrow_top,
                           max(0, plus_x - x - Melty.px(46)), heading_tint,
                           height=max(0, arrow_bottom - arrow_top), background=False,
                           event="left_mouse_down"):
                    state.folder_expanded[folder_key] = True
                    changed |= not expanded
                if new_conversation is not None and _button(
                        draw_state, "new-in:" + run_id, "+", plus_x, heading_y, trash_width, heading_tint,
                        not first_chats.loading and not first_chats.error, height=heading_height, background=False, text_color=_tint_style(tuple(heading_tint)).make_color_style_value(input={
                            "value": 7.788, "saturation": 1.559, "max_value": 1.601})[:3]):
                    new_conversation(first_account, first_chats, project)
                    changed = True
                _title(label, x + Melty.px(46), heading_y,
                       plus_x - x - Melty.px(46), heading_height, arrow_tint,
                       brightness=0.65 * float(Toggles.Melty.arrow_brightness), ellipsis=True,
                       text_color=_tint_style(tuple(arrow_tint)).make_color_style_value(input={
                           "value": 7.788, "saturation": 1.559, "max_value": 1.601})[:3])
                if not expanded and any(is_active(entry[5]) for entry in rows):
                    _running_dot(plus_x - trash_width / 2, heading_y + heading_height / 2, heading_tint)
            child_y = heading_y + heading_height + gap
            for account_id, chats, kind, key, child_meta, text, row_height, row_tint in children:
                row_id = pane + ":" + account_id + ":" + key
                rect = (x + pad, child_y, x + row_width - trash_width, child_y + row_height)
                renaming = getattr(state, "rename", None)
                editing = (renaming is not None and renaming["account"] == account_id
                           and renaming["key"] == key and renaming.get("pane", pane) == pane)
                # Rows order by time: no drag reordering, and nothing registered
                # for a row scrolled out of the list.
                if not _visible(child_y, row_height, clip):
                    child_y += row_height + gap
                    continue
                if draw_state.on_action("right_mouse_down", view_id="chat-menu:" + row_id, priority_delta=3,
                                        rect=(x + pad, child_y, x + row_width - pad, child_y + row_height)):
                    state.chat_menu = {"account": account_id, "key": key, "pane": pane}
                    changed = True
                tint = row_tint
                selected = account_id == state.account and key == state.selected.get(account_id)
                if selected:
                    _card(x + pad, child_y, row_width - pad * 2, row_height, tint, selected=True,
                          max_bg_value=0.32, shadow_offset=Toggles.Chat.selected_chat_shadow_offset)
                # A row cut by the list's edge takes the pointer only where it
                # shows: hit rects ignore the clip, and a full-length one would
                # reach over the filter chips above or the button below.
                hit_top = max(child_y, clip[1]) if clip else child_y
                hit_bottom = min(child_y + row_height, clip[3]) if clip else child_y + row_height
                # Select on press: CLICKED waits 250 ms for the label's rename
                # double-click. DOWN remains immediate and enables renaming.
                if hit_bottom - hit_top >= 1 and _button(draw_state, "chat:" + row_id, "", x + pad, hit_top,
                           row_width - pad * 2 - trash_width, tint, height=hit_bottom - hit_top,
                           background=False, event="left_mouse_down"):
                    state.selected[account_id] = key
                    state.account = account_id
                    if kind is not None:
                        state.provider = kind.name
                    changed = True
                row_rect = (x + pad, child_y, x + row_width - pad, child_y + row_height)
                hovered = draw_state.on_action("cursor_hover", view_id="chat-hover:" + row_id,
                                               rect=row_rect) is not None
                if not is_active(chats.get(key)) and hovered and hit_bottom - hit_top >= 1 and _button(
                        draw_state, "archive:" + row_id, trash_icon, x + row_width - pad - trash_width, hit_top,
                        trash_width, tint, height=hit_bottom - hit_top, background=False):
                    archive = (chats, account_id, key)
                if draw_state.on_action("left_mouse_double_clicked", view_id="rename:" + row_id,
                                        rect=rect, priority_delta=5) is not None:
                    state.rename = {"account": account_id, "key": key, "pane": pane, "draft": text, "focus": True}
                    renaming = state.rename
                    editing = True
                    state.selected[account_id] = key
                    state.account = account_id
                    changed = True
                title_width = row_width - pad * 2
                right = x + row_width - pad
                if hovered and not is_active(chats.get(key)):
                    right -= trash_width
                if kind is not None:
                    tag = conversation_source_tag(kind, chats.get(key))
                    tag_tint = (Toggles.Chat.claude_tag_tint if kind.name == "anthropic"
                                else Toggles.Chat.codex_tag_tint if kind.name == "codex" else tuple(kind.tint))
                    tag_dot_width = Melty.px(12) if is_active(chats.get(key)) else 0
                    tag_width = _label_width(tag) + Melty.px(5) + tag_dot_width
                    right -= tag_width
                    imgui.get_window_draw_list().add_rect_filled(right, child_y, right + tag_width,
                        child_y + row_height, _color(tag_tint, 0.28), rounding=Melty.px(4))
                    if tag_dot_width:
                        _running_dot(right + Melty.px(6), child_y + row_height / 2, tint)
                    _title(tag, right + tag_dot_width + Melty.px(5), child_y,
                           tag_width - tag_dot_width - Melty.px(5), row_height,
                           tag_tint, text_color=tag_tint)
                    right -= Melty.px(5)
                size = chats.get(key).get("size_bytes")
                size_label = (f"{size / 1_000_000:.0f} MB" if size >= 10_000_000 else f"{size / 1_000_000:.2f} MB") if size is not None else "— MB"
                size_width = _label_width(size_label) + Melty.px(8)
                right -= size_width
                _title(size_label, right, child_y, size_width, row_height, tint, brightness=0.5)
                title_width = max(0, right - x - pad - Melty.px(5))
                if not editing:
                    _title(text, x + pad + Melty.px(5), child_y, title_width, row_height, tint, ellipsis=True)
                else:
                    imgui.set_cursor_screen_pos((x + pad + Melty.px(5), child_y))
                    request_focus = editing and renaming["focus"]
                    edited, value, text_ds = draw_text(renaming["draft"] if editing else text,
                        wrap=False, name="chat-row:" + row_id,
                        request_focus=request_focus, select_all_on_focus=request_focus, single_line=True,
                        return_extras=True,
                        width=title_width, height=row_height, is_tree=False,
                        editable=editing, focusable=editing, syntax_highlight=False, autocomplete=False,
                        show_header=False, show_bg=False, shadow=False, show_widgets=False,
                        show_file_header=False, show_jump_bar=False, scope_collapse=False,
                        disable_scroll=True, freeze_resize=True, use_cache=True, imgui_padding=False,
                        tint=tint, text_tint=_text_tint(tuple(tint)), fim="")
                if editing:
                    if Melty.text_focused_ds is text_ds:
                        renaming["focused"] = True
                        renaming["focus"] = False
                    if edited:
                        renaming["draft"] = value
                        changed = True
                    keys_pressed = {event[0] for event in Melty.frame_key_events}
                    if glfw.KEY_ESCAPE in keys_pressed:
                        state.rename = None
                        changed = True
                    elif (glfw.KEY_ENTER in keys_pressed or glfw.KEY_KP_ENTER in keys_pressed
                          or renaming.get("focused", False) and Melty.text_focused_ds is not text_ds):
                        if renaming["draft"].strip():
                            chats.get(key)["title"] = renaming["draft"].strip()
                        state.rename = None
                        changed = True
                child_y += row_height + gap
            y += card_height + 5
    if archive is not None:
        chats, account_id, key = archive
        del chats[key]
        if state.selected.get(account_id) == key:
            state.selected[account_id] = next(iter(chats), None)
        if getattr(state, "rename", None) is not None and state.rename["key"] == key:
            state.rename = None
        changed = True
    return changed, min(total, height)  # the height the list actually uses


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


@render_func(tint=(0.2, 0.8, 0.4), use_cache=True, show_bg=False, with_header=None,
             with_footer=None, shadow=False, imgui_padding=False, disable_scroll=True)
def draw_chat_terminal(input_value, draw_state=None):
    from meltygui.widgets.terminal_playground import _resolve
    grid, char_width, line_height = input_value
    x, y = draw_state.abs_left + Melty.px(4), draw_state.abs_top + Melty.px(4)
    draw_list = imgui.get_window_draw_list()
    font = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) if Melty.font_mgr else None
    if font is not None:
        imgui.push_font(font)
    try:
        clip = Melty.get_clip_rect()
        first = max(0, int((clip[1] - y) // line_height)) if clip else 0
        last = min(len(grid), int((clip[3] - y) // line_height) + 1) if clip else len(grid)
        for index in range(first, last):
            row = grid[index]
            top = y + index * line_height
            column = 0
            visible_columns = min(len(row), max(0, int((draw_state.width - Melty.px(8)) / char_width) + 1))
            while column < visible_columns:
                cell = row[column]
                end = column + 1
                while end < visible_columns and row[end][1:] == cell[1:]:
                    end += 1
                foreground = _resolve(cell.fg, (0.85, 0.85, 0.85), cell.bold)
                background = _resolve(cell.bg, (0.018, 0.022, 0.025))
                if cell.reverse:
                    foreground, background = background, foreground
                left = x + column * char_width
                if cell.bg != "default" or cell.reverse:
                    draw_list.add_rect_filled(left, top, x + end * char_width, top + line_height, _color(background))
                draw_list.add_text(left, top, _color(foreground), "".join(char.data for char in row[column:end]))
                column = end
        imgui.dummy(draw_state.width, len(grid) * line_height + Melty.px(8))
    finally:
        if font is not None:
            imgui.pop_font()
    return False, input_value


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


def draw_chat_queue(proxy, selected, chat, draw_state, state, key, width, tint):
    queued = chat.get("queued_messages", {})
    if not queued:
        return False
    changed = False
    x, y = imgui.get_cursor_screen_pos()
    paused = chat.get("queue_paused", False)
    label = f"{len(queued)} queued" + (" · paused" if paused else " · sends after this turn")
    _title(label, x, y, max(1, width - Melty.px(90)), Melty.px(28), tint, brightness=0.7, ellipsis=True)
    if paused and _button(draw_state, key + ":resume", "Resume", x + width - Melty.px(85), y,
                          Melty.px(85), tint, height=Melty.px(28), enabled=not chat.get("locked", False)):
        proxy.resume_queue(selected)
        changed = True
    imgui.set_cursor_screen_pos((x, y))
    imgui.dummy(width, Melty.px(28))
    with _viewport(draw_state, state, key + ":queue", width, Melty.px(28) * min(3, len(queued)),
                   Melty.px(28) * len(queued)) as (x, y, clip):
        for index, (identifier, message) in enumerate(list(queued.items())):
            top = y + index * Melty.px(28)
            if not _visible(top, Melty.px(28), clip):
                continue
            _title(input_text(message).replace("\n", " "), x, top, max(1, width - Melty.px(175)),
                   Melty.px(28), tint, brightness=0.7, ellipsis=True)
            if _button(draw_state, key + ":send-now:" + identifier, "Send now", x + width - Melty.px(170), top,
                       Melty.px(85), tint, not chat.get("locked", False), height=Melty.px(28), background=False):
                changed |= proxy.send_queued(selected, identifier)
            if _button(draw_state, key + ":remove:" + identifier, "Remove", x + width - Melty.px(80), top,
                       Melty.px(72), tint, height=Melty.px(28), background=False):
                proxy.cancel_queued(selected, identifier)
                changed = True
    return changed


def draw_messages(messages, draw_state, state, key, width, height,
                  conversation_tint=(0.4, 0.8, 0.7), revision=None):
    """Immediate-mode message layout; only visible text leaves use cached views.

    Message subclasses describe semantics, leaf types describe rendering.
    Resources stay inert references: no image loading, opening paths or I/O.
    """
    if not hasattr(state, "message_expanded"):
        state.message_expanded = {}
    if not hasattr(state, "output_expanded"):
        state.output_expanded = {}
    if not hasattr(state, "image_sizes"):
        state.image_sizes = {}
    # User and assistant prose sit inset in their span.
    # [tint=(0.95, 0.6, 0.25)]
    user_inset = Melty.px(12)
    # [tint=(0.95, 0.6, 0.25)]
    user_pad = Melty.px(6)
    rows = []
    viewport_width = max(Melty.px(60), width - Melty.px(7))
    row_width = min(viewport_width, Melty.px(Toggles.Chat.transcript_max_width))
    # Resize: wrapping every leaf per frame is O(transcript), and re-wrapped
    # heights above the viewport shove the scroll around. While the width is
    # moving only VISIBLE prose re-wraps (off-screen leaves keep their last
    # wrap), and the top visible row is pinned to its screen position through
    # the gesture; the frame the width settles re-wraps everything and pins again.
    view = state.viewports.setdefault(key, {"offset": 0.0, "follow": True})
    prose_index, prose_texts = {}, []      # every prose leaf in order: the selection's coordinates
    width_moving = view.get("width") not in (None, row_width)
    settling = view.get("unsettled", False) and not width_moving
    view["width"], view["unsettled"] = row_width, width_moving
    row_memos = view.setdefault("rows", {})   # per-row layouts (text_layouts holds visible leaf wraps)
    viewport_top = view.get("offset", 0.0)
    layout_y = 0
    # User bubbles align to the right of the centered transcript, with a
    # narrower cap and a small left inset even when the window is narrow.
    # [tint=(0.95, 0.6, 0.25)]
    chat_indent = max(Melty.px(50), row_width - Melty.px(Toggles.Chat.user_message_max_width))
    # Code blocks darken the window's PAINTED fill: `Melty.bg_color_stack`
    # top, what the wrapper's draw_bg actually returned (`bg_stack` holds the
    # recipe, `draw_state.bg_color` the nested recipe; both hold the wrong shade).
    # The factors and shadow offsets live in Toggles.Chat.
    bash_darken = Toggles.Chat.bash_darken
    python_darken = Toggles.Chat.python_darken
    bash_shadow = Toggles.Chat.bash_shadow_offset
    # Rows: [icon chip][content]. Every non-user row's content — command
    # previews, file tags and their wrapped rows, terminals, thinking text,
    # assistant prose — sits in ONE column at icon_column; the chips hang in
    # the gutter to its left. Hovering an icon row swaps its glyph for the
    # expand caret (no caret column of its own); an expanded row's icon and
    # Failed badge share its first content line. Previews are dimmed to recede.
    # [tint=(0.95, 0.6, 0.25)]
    icon_column = Melty.px(26)
    icon_brightness = Toggles.Chat.icon_brightness
    command_text_brightness = Toggles.Chat.command_text_brightness
    # A terminal block never grows wider than this; long lines clip at its edge.
    # [tint=(0.95, 0.6, 0.25)]
    terminal_max_width = Melty.px(Toggles.Chat.terminal_max_width)
    window_color = tuple(Melty.bg_color_stack[-1] if Melty.bg_color_stack else Melty.get_bg_color(-1))[:3]
    header_height = max(Melty.px(23), imgui.get_text_line_height() + Melty.px(4))
    # The layout pass - every message, visible or not - is the transcript's
    # cost; it only changes with the content (the proxy's revision, the
    # message count), the width, or an expand / collapse. Otherwise the
    # previous frame's rows serve (the draw pass below is per visible row).
    pictures = image_cache()
    pictures.watch(draw_state)
    signature = (row_width, chat_indent, terminal_max_width, revision, len(messages), id(messages), pictures.generation,
                 getattr(state, "concise", False), frozenset(getattr(state, "action_groups", {}).items()),
                 hash(frozenset(k for k, v in state.message_expanded.items() if v)),
                 hash(frozenset(k for k, v in state.output_expanded.items() if v)))
    cached = view.get("layout")
    if cached is not None and cached[0] == signature and not width_moving and not settling and not view.get("relayout"):
        rows, prose_index, prose_texts = cached[1], cached[2], cached[3]
    else:
        previous_user = False
        for message_id, message in transcript_entries(messages, state, key):
            if not isinstance(message, Message):
                continue  # older cached sessions are replaced by the provider factory
            if isinstance(message, ReasoningMessage) and not any(
                    isinstance(value, str) and value.strip() for _, value, _ in _message_leaves(message["content"])):
                continue
            row_key = key + ":" + message_id
            user = isinstance(message, UserMessage)
            leading_space = Melty.px(20) if rows and (user or previous_user) else 0
            previous_user = user
            if message.get("kind") == "actionGroup":
                full_height = header_height + Melty.px(4) + leading_space
                rows.append((row_key, message, [], full_height, False, False, [], header_height,
                             row_width, False, True, "", leading_space))
                layout_y += full_height
                continue
            terminal = isinstance(message, CommandExecution)
            has_header = not isinstance(message, (UserMessage, AssistantMessage))
            collapsible = has_header and isinstance(message, (ToolCall, ReasoningMessage))
            # Every collapsible row starts collapsed; the caret / file tags open it.
            expanded = not collapsible or state.message_expanded.get(row_key, False)
            # A collapsed row of a finished tool call lays out the same every
            # frame: keep its row between frames (most of an old transcript).
            row_signature = None
            if collapsible and not expanded and message.get("status") in ("completed", "failed"):
                row_signature = (id(message), leading_space, message.get("status"), row_width, chat_indent, header_height,
                                 terminal_max_width, len(message["content"]), id(message.get("summary")))
            elif (not has_header and message.get("status") != "running" and not width_moving
                  and not any(isinstance(value, ImageReference) for value in message["content"].values())):
                # Finished assistant: its text is the same object until set_text replaces it.
                row_signature = (id(message), leading_space, id(getattr(message, "source_text", None)), message.get("status"),
                                 row_width, chat_indent, header_height, terminal_max_width, len(message["content"]))
            if row_signature is not None:
                memo = row_memos.get(row_key)
                if memo is not None and memo[0] == row_signature:
                    row = memo[1]
                    if not has_header:
                        for path, display, *_ in row[2]:      # all prose leaves keep their selection slots
                            if isinstance(display, str) and not isinstance(display, CodeString):
                                prose_index[(row_key, path)] = len(prose_texts)
                                prose_texts.append(str(display))
                    rows.append(row)
                    layout_y += row[3]
                    continue
            summary = message.get("summary")
            files = {filename: counts for filename, counts in summary.items()
                     if counts.get("access", "write") == "write"} if isinstance(summary, FileTags) else {}
            leaves = []
            show_more = False
            content_width = 0
            if expanded and terminal:
                display, leaf_height = _terminal_layout(state, row_key, message, row_width)
                leaves.append((("terminal",), display, leaf_height, 0, ""))
                memo = state.text_layouts[(row_key, "terminal")]
                show_more, content_width = memo[3], memo[4]
            elif expanded:
                content = message["content"] or message["details"]
                for path, value, depth in _message_leaves(content):
                    if isinstance(message, ReasoningMessage) and isinstance(value, str) and not value.strip():
                        continue
                    indent = min(depth - 1, 4) * Melty.px(10) if isinstance(message, ToolCall) else 0
                    indent = 0 if isinstance(message, ReasoningMessage) else max(0, indent)  # its icon column indents it
                    prose = isinstance(message, (UserMessage, AssistantMessage))
                    # Assistant prose starts at the row edge so it lines up with the tool rows'
                    # arrow; user prose keeps its inset inside the card.
                    indent = user_inset if isinstance(message, UserMessage) else 0 if prose else indent
                    image_entry = image_cache().entry(value) if isinstance(value, ImageReference) else None
                    if isinstance(value, ImageReference):
                        # A picture: its bounding box plus the caption line under it
                        # (a payload is not measured or treated as text).
                        # The same span the draw pass hands _draw_image (or leaf_width).
                        span = ((row_width - chat_indent if isinstance(message, UserMessage) else row_width) - indent
                                - (0 if isinstance(message, UserMessage) else icon_column)
                                - (user_inset if isinstance(message, UserMessage) else 0))
                        if isinstance(message, AssistantMessage):
                            span = min(span, terminal_max_width)
                        image_width, image_height = image_box(
                            image_entry, max(Melty.px(40), span), state.image_sizes.get(row_key + ":image:" + repr(path)))
                        display, leaf_height = value, image_height + header_height
                    elif isinstance(value, str):
                        # Only plain prose wraps; code, tool output and everything else keeps its lines.
                        wraps = prose and not isinstance(value, CodeString)
                        off_screen = layout_y > viewport_top + height or layout_y + Melty.px(2000) < viewport_top
                        display, leaf_height = _text_layout(state, (row_key, path), value,
                            wrap_width=(row_width - chat_indent - 2 * user_inset if isinstance(message, UserMessage)
                                        else min(row_width - icon_column, terminal_max_width)) if wraps else None,
                            keep=width_moving and off_screen)
                        if wraps:
                            prose_index[(row_key, path)] = len(prose_texts)
                            prose_texts.append(str(display))
                    else:
                        display, leaf_height = value, header_height
                    # Tool field labels and language captions are drawn directly.
                    caption = (" / ".join(p for p in path if not p.isdecimal())
                               if isinstance(message, ToolCall) else
                               getattr(value, "language", "") if isinstance(value, CodeString) else "")
                    if caption:
                        leaf_height += header_height
                    leaves.append((path, display, leaf_height, indent, caption))
                    measured = (state.text_layouts[(row_key, path)][3] if isinstance(value, str) else
                                # A picture sits past the icon column: the card must grow around it
                                image_width + (0 if isinstance(message, UserMessage) else icon_column)
                                if isinstance(value, ImageReference) else imgui.calc_text_size(str(value)).x)
                    content_width = max(content_width, indent + measured + Melty.px(8),
                                        indent + _label_width(caption) + Melty.px(8))
            tags = []
            icon = _message_icon(message)
            content_x = 0 if isinstance(message, UserMessage) else icon_column
            tags_x = content_x
            tag_x, tag_y = tags_x, 0
            available_width = (min(row_width, terminal_max_width) if terminal else row_width) - (Melty.px(100) if _message_failed(message) else 0)
            if files:
                for filename, counts in files.items():
                    count_label = (f"  +{counts['added']} -{counts['removed']}"
                                   if counts.get("added") is not None else "")
                    tag_width = min(max(Melty.px(30), available_width - Melty.px(20)),
                                    imgui.calc_text_size(Path(filename).name + count_label).x + Melty.px(16))
                    if tag_x > tags_x and tag_x + tag_width > available_width:
                        tag_x = tags_x  # wrapped rows line up after the icon and caret
                        tag_y += header_height + Melty.px(2)
                    tags.append((filename, counts, tag_x, tag_y, tag_width))
                    tag_x += tag_width + Melty.px(3)
            summary_height = (tag_y + header_height if tags else
                              0 if isinstance(message, ReasoningMessage) and expanded else
                              0 if icon and expanded and leaves else
                              header_height if has_header else
                              user_pad if isinstance(message, UserMessage) else 0)
            gap = 0 if isinstance(message, UserMessage) else Melty.px(4)
            full_height = (summary_height + sum(leaf[2] for leaf in leaves) + gap
                           + (user_pad if isinstance(message, UserMessage) else 0)
                           + (header_height if show_more else 0))
            fitted_width = row_width
            if isinstance(message, UserMessage):
                # Keep the prose renderer's 4px inset plus trailing glyph margin
                # inside the leaf clip, as well as the bubble's outer padding.
                fitted_width = min(row_width - chat_indent,
                                   max(Melty.px(40), content_width + user_inset))
            if isinstance(message, ToolCall):
                header_width = (max(tag[2] + tag[4] for tag in tags) if tags else
                                tags_x + _label_width(_message_label(message, expanded)))
                header_width += Melty.px(104) if _message_failed(message) else Melty.px(8)
                fitted_width = min(row_width, max(content_width, header_width, Melty.px(110) if show_more else 0))
                if terminal:
                    fitted_width = min(fitted_width, terminal_max_width)  # collapsed labels clip at the cap too
            full_height += leading_space
            row = (row_key, message, leaves, full_height, collapsible, expanded, tags, summary_height, fitted_width, show_more, has_header, icon, leading_space)
            if row_signature is not None and not (width_moving or settling):
                # (a row wrapped mid-resize keeps stale off-screen wraps: never memoised)
                row_memos[row_key] = (row_signature, row)
            rows.append(row)
            layout_y += full_height
        view["layout"] = (signature, rows, prose_index, prose_texts)
        view["row_geometry"] = _row_geometry(rows)
    geometry = view["row_geometry"]
    ends = geometry[1]
    total = ends[-1] if ends else 0.0
    # A row the reader just expanded / collapsed / showed more of re-laid out
    # this frame: hold their row, and drop follow so a tall block opening near
    # the bottom doesn't fling the viewport off its end.
    relayout = view.pop("relayout", False)
    if relayout:
        view["follow"] = False
    _hold_scroll_anchor(view, rows, height, width_moving or settling or relayout, geometry)
    changed = False
    selecting = False        # a prose leaf took this frame's selection
    with _viewport(draw_state, state, key, width, height, total, follow=True) as (x, y, clip):
        x += max(0, (viewport_width - row_width) / 2)
        # The viewport returns the scrolled origin. Skip entire off-screen rows
        # before touching their leaves, tints, file metadata or event handlers.
        first = bisect_right(ends, clip[1] - y) if clip is not None else 0
        y += ends[first - 1] if first else 0.0
        for row_index in range(first, len(rows)):
            if clip is not None and y >= clip[3]:
                break
            row_key, message, leaves, full_height, collapsible, expanded, tags, summary_height, fitted_width, show_more, has_header, icon, leading_space = rows[row_index]
            y += leading_space
            full_height -= leading_space
            if message.get("kind") == "actionGroup":
                if not hasattr(state, "action_groups"):
                    state.action_groups = {}
                expanded = state.action_groups.get(row_key, False)
                label = ("▾ " if expanded else "▸ ") + message["details"]["label"]
                if _button(draw_state, row_key, "", x, y, row_width, conversation_tint,
                           height=header_height, background=False):
                    state.action_groups[row_key] = not expanded
                    view["relayout"] = True
                    changed = True
                _title(label, x, y, row_width, header_height, conversation_tint, brightness=0.65, ellipsis=True)
                y += full_height
                continue
            content_x = 0 if isinstance(message, UserMessage) else icon_column
            tint = conversation_tint
            # User messages carry the chat tint; assistant prose stays unboxed.
            underlying = window_color
            prose = isinstance(message, (UserMessage, AssistantMessage))
            side = row_width - fitted_width if isinstance(message, UserMessage) else 0
            row_span = fitted_width if isinstance(message, UserMessage) else row_width
            if isinstance(message, UserMessage) and _visible(y, full_height, clip):
                underlying = _card(x + side, y, row_span, full_height, tint,
                                   max_bg_value=Toggles.Chat.user_message_bg_value) or underlying
            if isinstance(message, ToolCall):
                underlying = tuple(c * (bash_darken if isinstance(message, CommandExecution) else python_darken)
                                   for c in underlying[:3])
                # The header row carries only its icon chip; the block background
                # starts under it, covering the expanded content.
                if expanded and full_height > summary_height + Melty.px(4) and _visible(y, full_height, clip):
                    _code_background(x + content_x, y + summary_height, fitted_width - content_x,
                                     full_height - summary_height - Melty.px(4),
                                     underlying, shadow=bash_shadow if isinstance(message, CommandExecution) else None)
            if tags:
                from meltygui.editor.file_header import draw_changed_file_header
                from meltygui.models.file_meta import FileMeta
                from meltygui.models.file_meta import file_meta_store
                metadata = file_meta_store()     # shared with the studio's tints
                painted = FileMeta.painted_tint if metadata else None
                for filename, counts, tag_x, tag_y, tag_width in tags:
                    file_y = y + tag_y
                    if _visible(file_y, header_height, clip):
                        file_path = str(Path(message["details"].get("cwd") or "") / filename)
                        file_tint = (painted(metadata.get(file_path)) if painted else None) or underlying
                        imgui.set_cursor_screen_pos((x + tag_x, file_y))
                        if draw_changed_file_header(filename, file_tint, draw_state,
                                view_id=row_key + ":file:" + filename, width=tag_width, height=header_height,
                                added=counts["added"], removed=counts["removed"], active=True,
                                prefix="", shadow_offset=Toggles.Chat.file_tag_shadow_offset):
                            state.message_expanded[row_key] = not expanded
                            view["relayout"] = True
                            changed = True
            if (summary_height and has_header or collapsible) and _visible(y, header_height, clip):
                header_x = x + content_x
                # An icon row shows its caret IN the icon while the pointer is over
                # the row. priority_delta 3 = the wrapper level: a lower delta is
                # pruned by the enclosing wrapper's own blocker and never delivers.
                row_hovered = not icon or draw_state.on_action(
                    "cursor_hover", view_id=row_key + ":hover", priority_delta=3,
                    rect=(x, y, x + fitted_width, y + full_height)) is not None
                if icon:
                    chip_color = (underlying if isinstance(message, ToolCall)
                                  else tuple(c * bash_darken for c in window_color))
                    glyph_x = _icon_chip(icon, x, y, icon_column - Melty.px(2), header_height, chip_color)
                    if not (collapsible and row_hovered):
                        _title(icon, glyph_x, y, icon_column, header_height, chip_color, icon_brightness)
                if collapsible and row_hovered:
                    if _caret(draw_state, row_key + ":expand", x, y, icon_column - Melty.px(2), header_height,
                              expanded, chip_color if icon else tint, icon_brightness if icon else 1.0):
                        state.message_expanded[row_key] = not expanded
                        view["relayout"] = True
                        changed = True
                failed = _message_failed(message)
                if not tags and not (isinstance(message, ReasoningMessage) and expanded):
                    label = _message_label(message, expanded)
                    if icon and label.startswith(icon):
                        label = label[len(icon):].lstrip()  # the icon is already on the row
                    if label:
                        # +4 for the same inner inset prose, tag text and terminal output sit at.
                        _title(label, header_x + Melty.px(4), y, fitted_width - (header_x - x) - Melty.px(4) - (Melty.px(100) if failed else 0),
                               header_height, underlying if isinstance(message, ToolCall) else tint,
                               command_text_brightness if icon else 1.0)
                if failed:
                    # Collapsed: the header line reserved room at its end. Expanded:
                    # the block's bottom-right corner, on the Show more / less line.
                    badge_y = y if summary_height else y + full_height - Melty.px(4) - header_height
                    _failure_badge(x + fitted_width - Melty.px(96), badge_y, Melty.px(92), header_height)
            leaf_y = y + summary_height
            for path, value, leaf_height, indent, caption in leaves:
                if _visible(leaf_y, leaf_height, clip):
                    text_y = leaf_y
                    left = x + side + indent + content_x
                    leaf_width = row_span - indent - content_x - (user_inset if isinstance(message, UserMessage) else 0)
                    if isinstance(message, AssistantMessage):
                        leaf_width = min(leaf_width, terminal_max_width)  # assistant prose caps like a terminal
                    if not isinstance(message, ToolCall) and isinstance(value, (PythonString, BashString, ToolOutput)):
                        is_bash = isinstance(value, (BashString, ToolOutput))
                        _code_background(left, leaf_y, leaf_width, leaf_height,
                                         tuple(c * (bash_darken if is_bash else python_darken) for c in underlying[:3]),
                                         shadow=bash_shadow if is_bash else None)
                    if isinstance(value, BashString):
                        caption = "$ bash"
                    if caption:
                        _title(caption, left, text_y, leaf_width, header_height, tint)
                        text_y += header_height
                    if path == ("terminal",):
                        imgui.set_cursor_screen_pos((x + content_x, text_y))
                        draw_chat_terminal(value, name=row_key + ":terminal", width=fitted_width - content_x, height=leaf_height)
                    elif isinstance(value, ImageReference):
                        image_name = row_key + ":image:" + repr(path)
                        # A user bubble contains its image; resizing can still grow
                        # into the conversation's full user-message span.
                        if isinstance(message, UserMessage):
                            leaf_width = row_width - chat_indent - indent - user_inset
                        previous_size = image_box(image_cache().entry(value), leaf_width,
                                                  state.image_sizes.get(image_name))
                        size = _draw_image(value, left, text_y, leaf_width,
                                           leaf_height - header_height * (2 if caption else 1), header_height, tint,
                                           name=image_name, size=state.image_sizes.get(image_name))
                        if size is not None and size != state.image_sizes.get(image_name):
                            state.image_sizes[image_name] = size
                            if size != previous_size:
                                view["relayout"] = True
                                changed = True
                    elif isinstance(value, Reference):
                        # Never paint data URLs / encoded image data as text.
                        name = value.get("name") or value.get("path") or ""
                        _title(value.label + (" · " + str(name) if name else "") + " (preview pending)",
                               left, text_y, leaf_width, header_height, tint)
                    elif isinstance(value, str) and prose and not isinstance(value, CodeString):
                        # Selection: a drag on a leaf starts it, the drag's
                        # head follows the pointer across leaves, Ctrl+C copies
                        # (below). All draw-list: no editor per leaf.
                        index = prose_index.get((row_key, path))
                        selection = view.get("selection")
                        span = None
                        if index is not None:
                            line_px, char_w = _prose_metrics()
                            leaf_rect = (left, text_y, left + leaf_width, text_y + leaf_height)
                            if draw_state.on_action("left_mouse_down", view_id=row_key + ":select:" + repr(path),
                                                    rect=leaf_rect, priority_delta=3) is not None:
                                offset = prose_offset(value, left, text_y, imgui.get_mouse_pos(), line_px, char_w)
                                selection = view["selection"] = {"anchor": (index, offset), "head": (index, offset),
                                                                 "dragging": True}
                                selecting = True
                                changed = True
                            elif selection and selection.get("dragging") and imgui.is_mouse_down(0):
                                mouse = imgui.get_mouse_pos()
                                if text_y <= mouse[1] < text_y + leaf_height:
                                    head = (index, prose_offset(value, left, text_y, mouse, line_px, char_w))
                                    if head != tuple(selection["head"]):
                                        selection["head"] = head
                                        changed = True
                            if selection:
                                span = selection_slice(selection, index, len(str(value)))
                        _draw_prose(value, left, text_y, leaf_width, leaf_height, tint, clip, selected=span)
                    elif isinstance(value, str):
                        imgui.set_cursor_screen_pos((left, text_y))
                        draw_text(value, wrap=False, name=row_key + ":" + repr(path),
                            width=leaf_width,
                            height=leaf_height - (header_height if caption else 0),
                            editable=False, syntax_highlight=isinstance(value, (PythonString, BashString)),
                            syntax_language="bash" if isinstance(value, BashString) else "python",
                            autocomplete=False, show_widgets=False, show_header=False, with_footer=None,
                            show_file_header=False, show_jump_bar=False, scope_collapse=False,
                            disable_scroll=True, freeze_resize=True, imgui_padding=False,
                            use_cache=True, tint=tint, text_tint=_text_tint(tuple(tint)),
                            is_tree=False, show_bg=False, shadow=False, bg_offset=-1, fim="")
                    else:
                        _title(str(value), left, text_y, leaf_width, header_height, tint)
                leaf_y += leaf_height
            if show_more and _visible(leaf_y, header_height, clip):
                show_all = state.output_expanded.get(row_key, False)
                if _button(draw_state, row_key + ":output-more", "Show less" if show_all else "Show more",
                           x + Melty.px(4), leaf_y, Melty.px(102), underlying,
                           height=header_height, background=False):
                    state.output_expanded[row_key] = not show_all
                    changed = True
            y += full_height
    selection = view.get("selection")
    if selection is not None:
        if selection.get("dragging"):
            if imgui.is_mouse_down(0):
                changed = True           # keep frames coming while the drag runs
            else:
                selection["dragging"] = False
                if tuple(selection["anchor"]) == tuple(selection["head"]):
                    view["selection"] = None      # a press without a drag selects nothing
                changed = True
        elif imgui.is_mouse_clicked(0) and not selecting:
            view["selection"] = None              # a click elsewhere clears it
            changed = True
        elif imgui.get_io().key_ctrl and any(k == glfw.KEY_C for k, _ in Melty.frame_key_events):
            text = selection_text(selection, prose_texts)
            if text:
                imgui.set_clipboard_text(text)
    return changed


def _cleanup_chat(draw_state):
    """The window is going: close idle sessions. One with a turn streaming
    stays up (its worker and process keep going, the session file fills in)
    — the studio reuses it when the window reopens; an app waits for it
    before exiting (melty_claude's `finish_turns`)."""
    for account in internet_accounts.accounts.values():
        proxy = account.get("_chat_proxy")
        if proxy is None:
            continue
        if any(chat["running"] for chat in proxy.values()):
            continue
        account.pop("_chat_proxy", None)
        proxy.close()


def draw_chat_requests(requests, draw_state, state, key, width, height, tint):
    """Pending approvals stay in a bounded scroll area above the composer."""
    line_height = imgui.get_text_line_height() * 1.2
    layouts = []
    total = 0
    for request_id, request in requests.items():
        if "answer" in request:
            continue
        text, text_height = _text_layout(state, (key, request_id), request.get("text", "Tool permission"), wrap_width=width)
        questions = request.get("data", {}).get("questions", []) if request["kind"] != "approval" else []
        layouts.append((request_id, request, text, text_height, questions))
        total += text_height + len(questions) * Melty.px(76) + Melty.px(36)
    changed = False
    with _viewport(draw_state, state, key, width, height, total) as (x, y, clip):
        for request_id, request, text, text_height, questions in layouts:
            _draw_prose(text, x, y, width - Melty.px(7), text_height, tint, clip)
            y += text_height
            answers = state.answers.setdefault(str(request_id), {})
            for question in questions:
                _title(question["question"], x, y, width, Melty.px(24), tint, ellipsis=True)
                y += Melty.px(26)
                imgui.set_cursor_screen_pos((x, y))
                edited, value = draw_text(answers.get(question["id"], ""), name=key + ":" + question["id"],
                    width=width - Melty.px(7), height=50, wrap=False, syntax_highlight=False,
                    show_header=False, fim="", is_tree=False)
                if edited:
                    answers[question["id"]] = value
                    changed = True
                y += Melty.px(50)
            decisions = ("accept", "decline") if request["kind"] == "approval" else ("answer",)
            for index, decision in enumerate(decisions):
                top, bottom = max(y, clip[1]), min(y + Melty.px(30), clip[3])
                if bottom > top and _button(draw_state, f"answer:{request_id}:{decision}", decision.title(),
                    x + index * Melty.px(100), top, Melty.px(95), tint, height=bottom - top):
                    request["answer"] = ({"decision": decision} if decision != "answer" else
                        {"answers": {identifier: {"answers": [value]} for identifier, value in answers.items()}})
                    changed = True
            y += Melty.px(36)
    return changed


def draw_conversation_title(chat, draw_state, state, selected, x, y, width, height, tint):
    """Double-click the header title to rename the same chat shown in the lists."""
    changed = False
    renaming = state.rename
    editing = (renaming is not None and renaming.get("pane") == "header"
               and renaming["account"] == state.account and renaming["key"] == selected)
    if not editing and draw_state.on_action(
            "left_mouse_double_clicked", view_id="rename:header:" + state.account + ":" + selected,
            rect=(x, y, x + width, y + height), priority_delta=5) is not None:
        state.rename = renaming = {"account": state.account, "key": selected, "pane": "header",
                                   "draft": chat["title"], "focus": True}
        editing = changed = True
    if not editing:
        _title(chat["title"], x, y, width, height, tint, ellipsis=True)
        return changed
    imgui.set_cursor_screen_pos((x, y))
    request_focus = renaming["focus"]
    edited, value, text_ds = draw_text(renaming["draft"],
        name="chat-title:" + state.account + ":" + selected, wrap=False,
        request_focus=request_focus, select_all_on_focus=request_focus, single_line=True,
        return_extras=True, width=width, height=height, is_tree=False,
        editable=True, focusable=True, syntax_highlight=False, autocomplete=False,
        show_header=False, show_bg=False, shadow=False, show_widgets=False,
        show_file_header=False, show_jump_bar=False, scope_collapse=False,
        disable_scroll=True, freeze_resize=True, use_cache=True, imgui_padding=False,
        tint=tint, text_tint=_text_tint(tuple(tint)), fim="")
    focused = Melty.text_focused_ds is text_ds
    if focused:
        renaming["focused"] = True
        renaming["focus"] = False
    if edited:
        renaming["draft"] = value
        changed = True
    keys_pressed = {event[0] for event in Melty.frame_key_events} if focused else set()
    if glfw.KEY_ESCAPE in keys_pressed:
        state.rename = None
        changed = True
    elif (glfw.KEY_ENTER in keys_pressed or glfw.KEY_KP_ENTER in keys_pressed
          or renaming.get("focused", False) and not focused):
        if renaming["draft"].strip():
            chat["title"] = renaming["draft"].strip()
        state.rename = None
        changed = True
    return changed


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


@render_func(tint=(0.35, 0.55, 0.75), auto_resize=False, use_cache=True,
             disable_scroll=True, imgui_padding=False, show_header=False, show_bg=False)
def draw_chat_navigation(input_value, draw_state=None, state: ChatInterfaceState = None,
                         row_edges=None, new_conversation=None, **kwargs):
    """Conversation lists and Internet Accounts share a resizable left column."""
    changed = False
    if not hasattr(state, "sections_expanded"):
        state.sections_expanded = {"recent": True, "all": True, "accounts": True}
    panes = ("accounts", "all", "recent")
    opened = [state.sections_expanded.get(pane, True) for pane in panes]
    heading_height = Melty.px(26)
    tint = Toggles.Chat.navigation_tint
    def section_heading(pane, x, y, width):
        control_changed, control_width = navigation_heading_control(
            pane, draw_state, state, x, y, width, heading_height)
        width = max(0, width - control_width)
        expanded = state.sections_expanded.get(pane, True)
        toggled = _caret(draw_state, "section:" + pane, x, y, Melty.px(18), heading_height, expanded, tint)
        label = {"all": "All chats", "recent": "Recent chats", "accounts": "Internet Accounts"}[pane]
        _title(label, x + Melty.px(20), y, max(0, width - Melty.px(20)), heading_height, tint, brightness=0.7)
        header_clicked = _button(draw_state, "section-label:" + pane, "", x + Melty.px(20), y,
                           max(0, width - Melty.px(20)), tint, height=heading_height,
                           background=False, shadow=False, event="left_mouse_down")
        if toggled:
            state.sections_expanded[pane] = not expanded
        elif header_clicked:
            state.sections_expanded[pane] = True
        return control_changed or toggled or (header_clicked and not expanded)
    window = draw_state.parent_window or draw_state
    top = imgui.get_cursor_screen_pos()[1] - window.abs_top
    minimum = heading_height + 8  # RowLayout's top and bottom padding.
    row_heights = navigation_row_sizes(state, row_edges, opened, top, draw_state.height, minimum)
    # Leave unused space below the headings when every section is closed.
    # Otherwise the last capped row would have to fill the whole column.
    extent = draw_state.height if any(opened) else minimum * len(panes)
    rows = RowLayout(draw_state, 3, row_edges=row_edges,
                     top_edge={"y": top}, bottom_edge={"y": top + extent},
                     row_heights=row_heights,
                     row_mins=[minimum] * 3,
                     row_maxes=[None if expanded else minimum for expanded in opened],
                     persist=True, resizable=True,
                     padding=4, padding_x=0, border_color=None)
    for index, pane in enumerate(panes):
        with rows.cell(index) as height:
            width = rows.inner_width()
            x, top = imgui.get_cursor_screen_pos()
            changed |= section_heading(pane, x, top, width)
            if not opened[index]:
                continue
            y = top + heading_height
            if pane == "accounts":
                imgui.set_cursor_screen_pos((x, y))
                edited, _ = internet_accounts.draw_internet_accounts(
                    internet_accounts.accounts, name="chat-internet-accounts",
                    width=width, height=max(0, height - heading_height),
                    show_header=False, show_name=False, show_bg=False, shadow=False,
                    # This embedded view borrows the chat view's accounts;
                    # collapsing it should not close their running backends.
                    on_cleanup=None)
                changed |= edited
                continue
            if pane == "recent":
                chip_x, chip_height = x, Melty.px(22)
                hours_selected = getattr(state, "age_hours", 0) or 24
                for label, hours in AGE_FILTERS:
                    if not hours:
                        continue
                    chip_width = _label_width(label) + Melty.px(16)
                    if chip_x > x and chip_x + chip_width > x + width:
                        chip_x, y = x, y + chip_height + Melty.px(3)
                    if _button(draw_state, "age:" + label, label, chip_x, y, chip_width, tint,
                               height=chip_height, selected=hours == hours_selected, shadow=False,
                               background=hours == hours_selected):
                        state.age_hours = hours
                        changed = True
                    chip_x += chip_width + Melty.px(4)
                y += chip_height + Melty.px(5)
            imgui.set_cursor_screen_pos((x, y))
            edited, _ = draw_chat_sidebar(input_value, draw_state, state, width,
                max(0, height - (y - top)),
                cutoff=age_cutoff(getattr(state, "age_hours", 0) or 24) if pane == "recent" else None,
                new_conversation=new_conversation, pane=pane)
            changed |= edited
    rows.finish()
    return changed, input_value


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


def draw_effort_slider(draw_state, key, value, levels, x, y, width, height, tint):
    """A discrete slider using the owning view's replayable input actions."""
    if not levels or value not in levels:
        _title("Effort: " + ("Unavailable" if not levels else "Loading…"), x, y, width, height,
               tint, brightness=0.6, ellipsis=True)
        return False, value
    selected = levels.index(value)
    left, right = x + Melty.px(8), x + width - Melty.px(8)
    rect = (x, y, x + width, y + height)
    pressed = draw_state.on_action("left_mouse_down", view_id=key, rect=rect, priority_delta=4)
    dragged = draw_state.on_action("left_mouse_drag", view_id=key + ":drag", rect=rect, priority_delta=4)
    changed = False
    if len(levels) > 1 and (pressed is not None or dragged is not None):
        fraction = max(0, min(1, (imgui.get_mouse_pos()[0] - left) / max(1, right - left)))
        index = round(fraction * (len(levels) - 1))
        changed = index != selected
        selected = index
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    track_y = y + height - Melty.px(5)
    color = _text_tint(tuple(tint))
    draw_list.add_line(left, track_y, right, track_y, _color(color, 0.3), Melty.px(2))
    for index in range(len(levels)):
        tick_x = left + (right - left) * index / max(1, len(levels) - 1)
        draw_list.add_circle_filled(tick_x, track_y, Melty.px(2), _color(color, 0.4), 12)
    knob_x = left + (right - left) * selected / max(1, len(levels) - 1)
    draw_list.add_circle_filled(knob_x, track_y, Melty.px(4), _color(color), 16)
    _title("Effort: " + levels[selected].title(), x + Melty.px(4), y,
           width - Melty.px(8), height - Melty.px(9), tint, brightness=0.8, ellipsis=True)
    return changed, levels[selected]


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


@window(tint=(1.34, 1.62, 1.76), display_name="Chat", icon=f"", initial={"width": 1100, "height": 760})
@render_func(auto_resize=False, min_width=700, min_height=500, show_bg=False, shadow=False, selectable=False,
             on_cleanup=_cleanup_chat, use_cache=True, disable_scroll=True, imgui_padding=False, indent_size=0)
def draw_chat_interface(input_value=None, draw_state=None, bg_offset=-2, state: ChatInterfaceState = None,
                        column_edges=None, new_project=None, default_project=None,
                        ctrl_shift_equal_down=False, ctrl_shift_minus_down=False, **kwargs):
    """The chat window. A new conversation runs in `new_project` when given
    (an app started for one project), else in the selected conversation's
    project, else `default_project` (an app's cwd; the studio: its checkout)."""
    # Ephemeral layout state also adopts already-open windows on hotswap.
    if not hasattr(state, "viewports"):
        state.viewports = {}
        state.text_layouts = {}
    folder_events = []
    if ctrl_shift_equal_down:
        folder_events.append((glfw.KEY_EQUAL, glfw.MOD_CONTROL | glfw.MOD_SHIFT))
    if ctrl_shift_minus_down:
        folder_events.append((glfw.KEY_MINUS, glfw.MOD_CONTROL | glfw.MOD_SHIFT))
    folders_changed = apply_folder_shortcuts(state, folder_events)
    accounts = internet_accounts.accounts
    if not accounts.loaded:
        accounts.load()
    accounts.ensure_kinds()
    sources = chat_sources(accounts)
    if not sources:
        return False, input_value
    kinds = {account_id: kind for account_id, kind in sources}
    if state.account not in kinds:
        state.account = next((account_id for account_id, kind in sources if kind.chat_available), sources[0][0])
    shown = [account_id for account_id in (getattr(state, "sources", None) or [state.account]) if account_id in kinds]
    if not shown:
        shown = [state.account]
    if state.account not in shown:
        state.account = shown[0]
    state.sources = shown
    kind = kinds[state.account]
    state.provider = kind.name
    account = accounts[state.account]
    changed = folders_changed

    def wake():
        # Like Fast Dock's external-change edge: the worker has queued new data.
        from meltygui.utils.glfw_utils import request_render
        if Melty.cache is not None:
            Melty.cache.invalidate_up_by_obj(state, force=True)
            if getattr(draw_state, "_tile_id", None) is not None:
                Melty.cache.invalidate_up(draw_state._tile_id, force=True, max_depth=8)
        request_render()

    # A session per shown source (None: a registered placeholder, kept blank).
    proxies = {account_id: kinds[account_id].chats(accounts[account_id], wake=wake) for account_id in shown}
    proxy = proxies[state.account]
    for shown_proxy in proxies.values():
        if shown_proxy is not None:
            shown_proxy.wake = wake
    # Drain every open session, shown or not: a hidden session still streams.
    for other in accounts.values():
        other_proxy = other.get("_chat_proxy")
        if other_proxy is not None and not other_proxy.closed:
            # Only the displayed transcript needs eager history refreshes.
            # Source tabs can leave several proxies alive in this window.
            other_proxy.active_key = (state.selected.get(state.account)
                                      if other_proxy is proxy else None)
            changed |= other_proxy.drain()
            other_proxy.reconcile()  # collect edits made in another view before applying metadata
    x, y = imgui.get_cursor_screen_pos()
    tint = kind.tint
    notice = "" if proxy is None else (proxy.error or ("Loading conversations…" if proxy.loading else ""))
    model_error = getattr(proxy, "models_error", None)
    if model_error:
        notice = (notice + "\n" if notice else "") + "Could not load models: " + model_error
    if notice:
        for line in notice.split("\n"):
            imgui.get_window_draw_list().add_text(*imgui.get_cursor_screen_pos(), _color((0.95, 0.65, 0.45)), line)
            imgui.dummy(1, imgui.get_text_line_height())
        if proxy.error or model_error:
            x, y = imgui.get_cursor_screen_pos()
            if _button(draw_state, "reconnect-chat", "Reconnect", x, y, Melty.px(110), tint):
                kind.close_chat(account)
                return True, input_value
            imgui.dummy(1, Melty.px(30))
    selected = state.selected.get(state.account)
    if proxy is not None and selected not in proxy:
        selected = next(iter(proxy), None)
        state.selected[state.account] = selected
    body_top = imgui.get_cursor_screen_pos()[1]
    body_bottom = draw_state.abs_top + (draw_state.height or Melty.px(760)) - Melty.px(16)
    body_height = max(Melty.px(180), body_bottom - body_top)
    columns = ColumnLayout(draw_state, 2, column_edges=column_edges,
                           column_widths=[260, None], column_mins=[180, 400],
                           padding=Melty.px(Toggles.Chat.column_gap), padding_y=0, border_color=None)
    with columns.cell(0, height=body_height) as width:
        x, y = imgui.get_cursor_screen_pos()
        top = y
        # The source bar: a tab per chat account, in its provider's tint, the
        # shown ones lifted. Click: that source; Shift+click: toggle it into
        # / out of the shown set. Tabs wrap in a narrow column.
        tab_height, tab_x = Melty.px(24), x
        for account_id, source_kind in sources:
            label = source_label(account_id, source_kind, accounts)
            tab_width = _label_width(label) + Melty.px(18)
            if tab_x > x and tab_x + tab_width > x + width:
                tab_x, y = x, y + tab_height + Melty.px(3)
            if _button(draw_state, "source:" + account_id, label, tab_x, y, tab_width, source_kind.tint,
                       enabled=source_kind.chat_available, height=tab_height, selected=account_id in shown,
                       shadow=False):
                pick_source(state, account_id, {a: k.name for a, k in sources}, additive=imgui.get_io().key_shift)
                changed = True
                # The sessions for this frame were opened above with the old
                # set: the next frame (asked for now) draws the new one.
                from meltygui.utils.glfw_utils import request_render
                request_render()
            tab_x += tab_width + Melty.px(4)
        y += tab_height + Melty.px(Toggles.Chat.header_margin)
        shown_sources = [(account_id, proxies[account_id], kinds[account_id],
                          source_label(account_id, kinds[account_id], accounts)) for account_id in shown]

        def start_conversation(account_id, chats, project=None):
            """A fresh conversation in that source: in `project` (a heading's +),
            else `new_project` when the app was started for one, else its
            selected conversation's project, else `default_project`."""
            from meltygui.paths import application_root
            key = str(uuid.uuid4())
            current = chats.get(state.selected.get(account_id))
            project = (project or new_project or (current["project"] if current else None)
                       or state.projects.get(account_id) or default_project or str(application_root()))
            created = time.time()
            chats[key] = {"title": "New conversation", "project": project,
                          "created_at": created, "updated": created}
            state.selected[account_id] = key
            state.account = account_id
            state.provider = kinds[account_id].name

        # Every few seconds each shown backend looks for sessions written
        # elsewhere (a terminal's Claude Code): their rows light up too.
        now = time.time()
        for shown_proxy in proxies.values():
            if shown_proxy is not None and now - getattr(shown_proxy, "refreshed", 0.0) > REFRESH_S:
                shown_proxy.refreshed = now
                shown_proxy.refresh()
        imgui.set_cursor_screen_pos((x, y))
        edited, _ = draw_chat_navigation(shown_sources, name="chat-navigation",
            state=state, new_conversation=start_conversation,
            context_menu=chat_context_menu_items(state, shown_sources),
            width=width, height=max(0, body_height - (y - top) - Melty.px(35)),
            tint=Toggles.Chat.navigation_tint)
        changed |= edited
        y = top + body_height - Melty.px(30)
        # The New conversation button follows the list directly (a list taller
        # than the column scrolls and the button sits at the column's foot);
        # it starts one in the primary folder (a heading's + picks its folder).
        imgui.set_cursor_screen_pos((x, y))
        if _button(draw_state, "new-chat", "+ New conversation", x, y, min(width, Melty.px(190)), tint,
                   proxy is not None and not proxy.loading and not proxy.error):
            start_conversation(state.account, proxy)
            changed = True
        imgui.dummy(width, Melty.px(25))
    # Sidebar selection takes effect in the same frame.
    selected = state.selected.get(state.account)
    proxy = proxies.get(state.account)
    kind = kinds[state.account]
    account = accounts[state.account]
    tint = kind.tint
    with columns.cell(1, height=body_height) as width:
        if proxy is not None and selected in proxy:
            chat = proxy[selected]
            meta = chat.metadata
            chat_tint = tuple(project_tint(chat["project"]) or kind.tint)
            x, y = imgui.get_cursor_screen_pos()
            changed |= draw_conversation_title(chat, draw_state, state, selected, x, y,
                max(1, width - Melty.px(100)), Melty.px(28), chat_tint)
            concise = getattr(state, "concise", False)
            if _button(draw_state, "concise-view", "✓ Concise" if concise else "Concise",
                       x + width - Melty.px(95), y, Melty.px(95), chat_tint,
                       height=Melty.px(28), background=False):
                state.concise = not concise
                changed = True
            imgui.set_cursor_screen_pos((x, y))
            imgui.dummy(width, Melty.px(28))
            x, y = imgui.get_cursor_screen_pos()
            project_label, project_height = _text_layout(state, "directory:" + selected,
                "\uf07b  " + (chat["project"] or "No working directory"), wrap_width=max(1, width))
            folder_tint = tuple(conversation_folder_tint(chat["project"]) or chat_tint)
            _title(project_label, x, y, width, project_height, folder_tint,
                   text_color=folder_tint)
            imgui.set_cursor_screen_pos((x, y))
            imgui.dummy(width, project_height + Melty.px(6))
            x, y = imgui.get_cursor_screen_pos()
            transcript_key = "history:" + state.account + ":" + selected
            # Reserve the composer before laying out the history. Scrolling
            # changes only message coordinates inside this history region.
            active = is_active(chat)
            project_editable = is_new_chat(chat) and not chat.get("external_busy") and not chat.get("locked")
            working = chat["running"] or bool(chat.turn_id) or "send" in chat.inflight
            action_extra_height = Melty.px(36) if working else 0
            effort_extra_height = Melty.px(36) if project_editable and kind.name in ("codex", "anthropic") else 0
            controls_extra_height = action_extra_height + effort_extra_height
            queue_count = len(chat.get("queued_messages", {}))
            queue_height = Melty.px(28) * (1 + min(3, queue_count)) if queue_count else 0
            requests_height = min(Melty.px(240), body_height * 0.35) if chat["requests"] else 0
            status_message = chat.error or ("Loading conversation…" if chat.loading else "")
            if chat.get("locked") and not status_message:
                from meltygui.chat.writer_locks import lock_message
                status_message = lock_message(chat.get("lock_owner"))
            locked = chat.get("locked", False)
            status_pad = Melty.px(12) if locked and status_message else 0
            status_icon = Melty.px(24) if status_pad else 0
            status_text, status_height = _text_layout(state, transcript_key + ":status", status_message,
                wrap_width=max(1, width - 2 * status_pad - status_icon)) if status_message else ("", 0)
            status_height += 2 * status_pad
            history_height = max(Melty.px(40), body_bottom - imgui.get_cursor_screen_pos()[1]
                                 - requests_height - status_height - queue_height - controls_extra_height - Melty.px(120 + (26 if active else 0)))
            changed |= draw_messages(chat["messages"], draw_state, state, transcript_key,
                                 width, history_height, conversation_tint=chat_tint, revision=proxy.revision)
            if active:
                x, y = imgui.get_cursor_screen_pos()
                _running_dot(x + Melty.px(10), y + Melty.px(12), chat_tint)
                _title(chat_activity(chat, kind.chat_label), x + Melty.px(22), y, width - Melty.px(22), Melty.px(24),
                       chat_tint, brightness=0.8, ellipsis=True)
                imgui.set_cursor_screen_pos((x, y))
                imgui.dummy(1, Melty.px(26))
            if status_message:
                x, y = imgui.get_cursor_screen_pos()
                if locked:
                    imgui.get_window_draw_list().add_rect_filled(x, y, x + width, y + status_height,
                        _color((0.5, 0.12, 0.12), 0.5), rounding=Melty.px(7))
                    _title("\uf023", x + status_pad, y + status_pad, status_icon,
                           Melty.px(23), (0.95, 0.55, 0.5))
                _draw_prose(status_text, x + status_pad + status_icon, y + status_pad,
                    width - 2 * status_pad - status_icon, status_height - 2 * status_pad,
                    (0.95, 0.7, 0.65) if locked else (0.95, 0.65, 0.4))
                imgui.dummy(1, status_height)
            if requests_height:
                changed |= draw_chat_requests(chat["requests"], draw_state, state,
                    transcript_key + ":requests", width, requests_height, chat_tint)
            changed |= draw_chat_queue(proxy, selected, chat, draw_state, state, transcript_key, width, chat_tint)
            draft_key = state.account + ":" + selected
            recovered = chat.pop("recovered_draft", None)
            if recovered:
                current = state.drafts.get(draft_key, "")
                state.drafts[draft_key] = recovered + ("\n\n" + current if current else "")
                changed = True
            locked = chat.get("locked", False)
            draft = state.drafts.get(draft_key, "")
            draft_generation = getattr(state, "draft_generations", {}).get(draft_key, 0)
            edited, draft = draw_text(draft, name="Message:" + selected + ":" + str(draft_generation),
                height=85, width=width, wrap=False, syntax_highlight=False, line_numbers=None, fim="", show_header=False,
                use_cache=True, is_tree=False, shadow=False, bg_offset=-1)
            if edited:
                state.drafts[draft_key] = draft
                changed = True
            x, y = imgui.get_cursor_screen_pos()
            can_send = bool(draft.strip()) and chat.loaded and not locked and not chat.get("external_busy") and (not chat.error or chat.get("retryable_error"))
            send = _button(draw_state, "send", "Send", x, y, Melty.px(85), chat_tint,
                           can_send, height=Melty.px(30), shadow=False)
            queue = working and _button(draw_state, "queue", "Queue", x + Melty.px(95), y,
                                        Melty.px(85), chat_tint, can_send, height=Melty.px(30), shadow=False)
            if send or queue:
                if proxy.queue_message(selected, user_message(draft), interrupt=bool(send)):
                    state.drafts[draft_key] = ""
                    if not hasattr(state, "draft_generations"):
                        state.draft_generations = {}
                    state.draft_generations[draft_key] = draft_generation + 1
                changed = True
            if working and _button(draw_state, "stop", "Stop", x + Melty.px(190), y,
                                   Melty.px(85), chat_tint, not locked,
                                   height=Melty.px(30), shadow=False):
                proxy.stop_chat(selected)
                changed = True
            buttons_y = y
            y += action_extra_height
            from meltygui.views.new_core_view import draw_dropdown
            permissions = {"Ask permission": "ask", "Full access": "full"}
            if not getattr(proxy, "inherits_defaults", False) and is_new_chat(chat) and meta.get("model") in (None, "", "default"):
                default_model = getattr(proxy, "default_model", None)
                if default_model:
                    meta["model"] = default_model
                    changed = True
            inherits_defaults = getattr(proxy, "inherits_defaults", False)
            defaults = proxy.defaults_for(chat["project"]) if inherits_defaults else {}
            models = chat_models(kind, proxy, meta.get("model", ""))
            if inherits_defaults:
                default_model = defaults.get("model") or getattr(proxy, "default_model", None)
                models = {"Default · " + (default_model or "Codex model"): "default", **models}
                access = ("Full access" if defaults.get("sandbox_mode") == "danger-full-access"
                          and defaults.get("approval_policy") == "never" else "Configured access")
                permissions = {"Default · " + access: "default", **permissions}
            new_chat = is_new_chat(chat)
            if new_chat:
                # New drafts may choose any available source, including ones
                # whose conversations are currently hidden in the sidebar.
                grouped_models = {}
                for source_id, source_kind in sources:
                    if not source_kind.chat_available:
                        continue
                    if source_id not in proxies:
                        proxies[source_id] = source_kind.chats(accounts[source_id], wake=wake)
                    source_proxy = proxies[source_id]
                    if source_proxy is None:
                        continue
                    options = chat_models(source_kind, source_proxy,
                        meta.get("model", "") if source_id == state.account else "")
                    if getattr(source_proxy, "inherits_defaults", False):
                        source_defaults = source_proxy.defaults_for(chat["project"])
                        default_model = source_defaults.get("model") or getattr(source_proxy, "default_model", None)
                        options = {"Default · " + (default_model or "Codex model"): "default", **options}
                    grouped_models[source_label(source_id, source_kind, accounts)] = {
                        label: (source_id, model) for label, model in options.items()}
                model_choices = grouped_models
            else:
                model_choices = models
            from meltygui.chat.codex_settings import effective_settings
            from meltygui.chat.codex_settings import fast_service_tier
            effective = effective_settings(chat, defaults, getattr(proxy, "default_model", None)) if kind.name == "codex" else {}
            fast_tier = fast_service_tier(proxy, effective.get("model")) if kind.name == "codex" else None
            fast_width = Melty.px(80) if fast_tier else 0
            has_effort = kind.name in ("codex", "anthropic")
            effort_width = Melty.px(150) + fast_width if has_effort and not project_editable else 0
            controls_start = 0 if working else Melty.px(95)
            control_x = x + controls_start
            control_count = 3 if project_editable else 2
            control_slot = max(Melty.px(12), (width - controls_start - effort_width) / control_count - Melty.px(6))
            for index, (field, choices, default) in enumerate((("permissions", permissions, "ask"), ("model", model_choices, ""))):
                current = meta.get(field, "default" if inherits_defaults else default)
                if inherits_defaults and (not current or field == "model" and not meta.get("model_explicit")):
                    current = "default"
                if field == "model" and kind.name == "codex" and chat.get("codex_settings"):
                    current = effective.get("model") or current
                labels = models if field == "model" else permissions
                label = next((label for label, value in labels.items() if value == current),
                             current if current and current != "default" else "Loading models…")
                slot = min(_label_width(label) + Melty.px(46), Melty.px(240),
                           control_slot)
                imgui.set_cursor_screen_pos((control_x, y))
                edited, value = draw_dropdown(label, collection=choices, display_label=label,
                    name=field + ":" + draft_key, width=slot - Melty.px(4), height=Melty.px(30), trigger_height=Melty.px(30),
                    show_header=False, shadow=False, tint=chat_tint,
                    text_pad=8)
                control_x += slot + Melty.px(6)
                if edited:
                    if field == "model" and new_chat:
                        source_id, model = value
                        changed |= switch_new_chat_source(state, proxies, kinds, selected, source_id, model)
                    else:
                        meta[field] = value
                        meta[field + "_selected_at"] = time.time()
                        if field == "model":
                            meta["model_explicit"] = True
                        changed = True
            if project_editable:
                current_chat = proxies[state.account][state.selected[state.account]]
                project = current_chat["project"]
                label = "\uf07b  " + (Path(project).name or project or "Working directory")
                imgui.set_cursor_screen_pos((control_x, y))
                project_choices = chat_project_choices(state, proxies, project, new_project, default_project)
                edited, value = draw_dropdown(project,
                    collection={"\uf07b  " + label: value for label, value in project_choices.items()},
                    row_tints={path: project_tint(path) for path in project_choices},
                    display_label=label, name="project:" + draft_key,
                    width=max(1, x + width - control_x), height=Melty.px(30), trigger_height=Melty.px(30),
                    show_header=False, shadow=False, tint=tuple(project_tint(project) or kind.tint), text_pad=8)
                if edited:
                    changed |= switch_new_chat_project(state, proxies, value)
            if has_effort:
                if project_editable:
                    control_x = x + Melty.px(95)
                model = meta.get("model")
                if model in (None, "", "default") or inherits_defaults and not meta.get("model_explicit"):
                    model = defaults.get("model") or getattr(proxy, "default_model", None)
                if kind.name == "codex":
                    model = effective.get("model") or model
                if fast_tier:
                    tier = effective.get("service_tier", getattr(proxy, "model_default_service_tiers", {}).get(model))
                    fast = tier == fast_tier or tier in ("fast", "priority") and fast_tier in ("fast", "priority")
                    if _button(draw_state, "fast:" + draft_key, "✓ Fast" if fast else "Fast",
                               control_x, y + effort_extra_height, fast_width - Melty.px(6), chat_tint,
                               selected=fast, height=Melty.px(30), shadow=False):
                        meta["service_tier"] = None if fast else fast_tier
                        meta["service_tier_selected_at"] = time.time()
                        changed = True
                    control_x += fast_width
                levels = chat_effort_levels(kind, proxy, model)
                effort = effective.get("effort") if kind.name == "codex" else meta.get("effort")
                if effort in (None, "", "default") or effort not in levels:
                    if kind.name == "codex":
                        effort = defaults.get("model_reasoning_effort") or getattr(proxy, "model_default_efforts", {}).get(model)
                    else:
                        effort = proxy.default_effort_for(chat["project"], model)
                    if effort and effort not in levels and levels:
                        order = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
                        eligible = [level for level in levels if level in order and effort in order
                                    and order.index(level) <= order.index(effort)]
                        effort = eligible[-1] if eligible else levels[0]
                edited, effort = draw_effort_slider(draw_state, "effort:" + draft_key,
                    effort, levels, control_x, y + effort_extra_height,
                    max(Melty.px(80), x + width - control_x), Melty.px(30), chat_tint)
                if edited:
                    meta["effort"] = effort
                    meta["effort_selected_at"] = time.time()
                    changed = True
            imgui.set_cursor_screen_pos((x, buttons_y))
            imgui.dummy(1, Melty.px(30) + controls_extra_height)
    columns.finish()
    if proxy is not None:
        proxy.reconcile()
    if changed:
        state.revision += 1
        # A click's effect (a row expanded, a filter picked) lays out on the
        # NEXT frame; ask for it now instead of waiting for the next input.
        from meltygui.utils.glfw_utils import request_render
        request_render()
    return changed, input_value
    if changed:
        state.revision += 1
    return changed, input_value
