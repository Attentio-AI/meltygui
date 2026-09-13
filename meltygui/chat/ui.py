"""Chat studio: draw-list navigation/transcript over provider-owned dictionaries."""
import colorsys
import dataclasses
import math
import time
from functools import lru_cache
from contextlib import contextmanager
from pathlib import Path
import uuid

import imgui
from src.lsd.gl_gui.hdr_color import pack_color
import glfw

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.chat.messages import (Message, AssistantMessage, UserMessage, ToolCall,
    ReasoningMessage, PythonString, CodeString, Reference, ImageReference, user_message, BashString, ToolOutput,
    FileTags, CommandExecution)
from src.lsd.gl_gui.chat import images as chat_images
from src.lsd.gl_gui.model.file_meta import FileMeta, file_meta_store
from src.lsd.gl_gui.view.playground.fast_file_explorer import set_row_tint
from src.lsd.gl_gui.toggles import Tint, Toggles
from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.view.core_views.columns import ColumnLayout
from src.lsd.gl_gui.view.core_views.drag_drop import DragDrop
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.new_core_view import draw_tuple_fast, draw_bg
from src.lsd.gl_gui.view.core_views.headers import flat_button, draw_header_arrow
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.text_editor import draw_text
from src.lsd.gl_gui.view.playground import internet_accounts


@no_save("revision", "viewports", "text_layouts", "rename")
class ChatInterfaceState(DictConversion):
    def __init__(self):
        super().__init__()
        self.provider = ""
        self.account = ""
        self.selected = {}
        self.drafts = {}
        self.projects = {}
        self.follow = True
        self.answers = {}
        self.revision = 0
        self.viewports = {}
        self.text_layouts = {}
        self.rename = None
        self.message_expanded = {}
        self.output_expanded = {}
        # The sidebar's age filter: conversations active within this many
        # hours (0 = all). AGE_FILTERS lists the choices.
        self.age_hours = 0
        # The sources (account ids) whose conversations the sidebar shows -
        # the tabs lit in the source bar: empty = just `account`.
        self.sources = []


TRASH_ICON = ""   # FontAwesome trash-alt

def image_cache():
    """The transcript's pictures, decoded once per process (chat/images.py)."""
    cache = getattr(Melty, "chat_image_cache", None)
    if cache is None:
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        cache = Melty.chat_image_cache = chat_images.ImageCache(wake=request_render)
    return cache


def image_box(entry, max_width):
    """The (w, h) a picture takes in the transcript: fitted into the span
    and Toggles.Chat.image_max_height, a placeholder while it decodes."""
    max_height = Melty.px(Toggles.Chat.image_max_height)
    if entry is None or entry.status == "failed":
        return max_width, 0
    if entry.size is None:
        return min(max_width, Melty.px(240)), Melty.px(120)
    return chat_images.fitted_size(entry.size, max_width, max_height)


# A conversation counts as ACTIVE (the live dot) while a turn runs here or
# its session was written within this many seconds by anyone - a provider's
# Claude Code or another window; the window asks each backend to look every
# REFRESH_S seconds.
ACTIVE_WINDOW_S = 30
REFRESH_S = 5


def is_active(chat, now=None):
    return bool(chat["running"]) or ((now if now is not None else time.time())
                                     - (chat.get("updated") or 0.0)) < ACTIVE_WINDOW_S


# The sidebar's age filter chips: label → hours (0 = every conversation).
AGE_FILTERS = (("1h", 1), ("2h", 2), ("Day", 24), ("2 days", 48), ("All", 0))


def age_cutoff(hours, now=None):
    """The epoch second before which a conversation is out of the filter,
    or None for no filter."""
    return None if not hours else (now if now is not None else time.time()) - hours * 3600


def sidebar_visible(key, chat, cutoff, selected=None):
    """Whether a conversation stays in the filtered sidebar: recent enough,
    running (active now), or the open one (its transcript is showing)."""
    return (cutoff is None or is_active(chat) or key == selected
            or (chat.get("updated") or 0.0) >= cutoff)


def _running_dot(x, y, tint):
    """The live indicator of a running conversation: a dot in the row's tint
    with a breathing halo. Keeps frames coming while it shows."""
    from src.lsd.gl_gui.utils.glfw_utils import request_render
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())  # body channel, see _draw
    phase = 0.5 + 0.5 * math.sin(glfw.get_time() * 3.5)
    color = _text_tint(tuple(tint))
    draw_list.add_circle_filled(x, y, Melty.px(4 + 4 * phase), _color(color, 0.12 + 0.2 * phase), 24)
    draw_list.add_circle_filled(x, y, Melty.px(3.5), _color(color, 0.95), 16)
    request_render()


def _color(tint, alpha=1):
    return pack_color(*tint[:3], alpha)


def _button(draw_state, key, label, x, y, width, tint, enabled=True, height=None,
            selected=False, background=True, shadow=True):
    cursor = imgui.get_cursor_screen_pos()
    try:
        imgui.set_cursor_screen_pos((x, y))
        return flat_button(label, draw_state if enabled else None, key,
            pos=(x, y), width=width, height=height or Melty.px(25),
            color=tint, alpha=(1 if enabled else 0.4) if background else 0,
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


def conversation_tint_setter(meta):
    """A writer for a conversation's own tint in the chat metadata: a tuple
    paints it, None (the picker's clear) unpaints it."""
    def write(value):
        if value is None:
            meta.pop("tint", None)
        else:
            meta["tint"] = tuple(value)
    return write


def _tint_slot(draw_state, key, tint, x, y, hovered, default_tint, setter, show_brush=True):
    """A row's tint control, the file browser's: the colour chip when the row
    is painted, a faint paint-brush when not (a click stamps `default_tint`
    in and opens the picker; `show_brush` False draws none — the selected
    conversation and a hovered heading show theirs, the rest stay tidy).
    Returns True when a tint was written."""
    from src.lsd.gl_gui.view.playground.fast_file_explorer import tint_control
    size = Melty.px(17)
    text_y = y + max(0.0, (size - imgui.get_text_line_height()) / 2)
    return tint_control(draw_state, key, tuple(tint) if tint else None, x, y, size, text_y, hovered,
                        tuple(default_tint), setter=setter, show_brush=show_brush)


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
    thumb_height = min(height, max(Melty.px(24), height * height / max(height, content_height)))
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
    from src.lsd.gl_gui.view.view_utils.imgui_style_manager_class import ImGuiStyleManager
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


def _card(x, y, width, height, tint, selected=False, max_bg_value=None):
    # Fills paint on the BODY channel (as flat_button does): a fill one channel
    # below sits under the compositor's mask for this rank and its lit rim /
    # specular is masked out - the card showed with no highlight at all.
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())
    add_shadow((x, y, width, height),
               offset=Toggles.Chat.selected_shadow_offset if selected else Toggles.Chat.shadow_offset,
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
    open_icon = f"\uf078"
    closed_icon = f"\uf054"
    icon = open_icon if expanded else closed_icon
    _title(icon, x + max(0, (width - _label_width(icon)) / 2), y, width, height, tint,
           brightness * float(Toggles.Melty.arrow_brightness))
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


def _title(text, x, y, width, height, tint, brightness=1.0, ellipsis=False):
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
                           _color(tuple(c * brightness for c in _text_tint(tuple(tint)))), text)
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


def draw_chat_sidebar(sources, draw_state, state, width, height, cutoff=None, new_conversation=None):
    """Immediate layout; visible text leaves own their cached render tiles.
    ``sources`` is ``[(account_id, proxy, kind, label)]`` (a lone ChatProxy is
    accepted as the one source): their conversations MIX into one list,
    newest first. With no ``cutoff`` (the All chip) a folder appears once,
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
    from src.lsd.gl_gui.chat.chat_proxy import ChatProxy
    if isinstance(sources, ChatProxy):
        sources = [(state.account, sources, None, "")]
    changed = False
    archive = None
    trash_icon = TRASH_ICON
    trash_width = Melty.px(25)
    row_width = max(Melty.px(80), width - Melty.px(7))
    gap, pad = Melty.px(2), Melty.px(3)
    mixed = len(sources) > 1
    memos = state.viewports.setdefault("sidebar-cards", {})
    # The cards lay out the same until a source's conversations, the window's
    # state (a selection, a fold, a rename), the filter minute or the width change:
    # keep them between frames (scrolling changes none).
    signature = (tuple((account_id, getattr(chats, "revision", None), len(chats) if chats is not None else 0)
                       for account_id, chats, _, _ in sources),
                 state.revision, int(cutoff // 60) if cutoff else None, row_width, Melty.ui_scale,
                 new_conversation is not None, tuple(sorted(state.selected.items())), state.account)
    memo = memos.get("all")
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
                if sidebar_visible(key, chat, cutoff, selected_key):
                    entries.append((-(chat.get("updated") or 0.0), account_id, chats, kind, key, chat, source_tint))
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
            meta = first_chats.projects[project]          # the fold marker lives with the run's newest source
            # The folder's tint is its project's in the file-meta store; an
            # unpainted folder (and its unpainted rows) wears the source's.
            painted = project_tint(project)
            heading_tint = painted or rows[0][6]
            label, heading_height = _text_layout(state, "project:" + project, Path(project).name or "No project")
            heading_height += Melty.px(2)
            children = []
            if meta["expanded"]:
                for _, account_id, chats, kind, key, chat, source_tint in rows:
                    text, row_height = _text_layout(state, "chat:" + account_id + ":" + key, chat["title"])
                    children.append((account_id, chats, kind, key, chat.metadata, text, row_height + Melty.px(2),
                                     tuple(chat.metadata.get("tint") or heading_tint)))
            card_height = pad * 2 + heading_height + sum(row[6] + gap for row in children)
            cards.append((index, project, meta, label, heading_height, rows, children, card_height,
                          painted, heading_tint, first_account, first_chats))
        memos["all"] = (signature, cards)
    total = sum(card[7] + gap for card in cards)
    with _viewport(draw_state, state, "sidebar:" + ":".join(source[0] for source in sources),
                   width, height, total) as (x, y, clip):
        for (index, project, meta, label, heading_height, rows, children, card_height,
             painted, heading_tint, first_account, first_chats) in cards:
            run_id = f"{index}:{project}"
            if painted and _visible(y, card_height, clip):
                _card(x, y, row_width, card_height, heading_tint)   # an unpainted card has no plate
            heading_y = y + pad
            if _visible(heading_y, heading_height, clip):
                imgui.set_cursor_screen_pos((x + pad, heading_y))
                imgui.push_id("project-arrow:" + run_id)
                try:
                    if draw_header_arrow(meta["expanded"]):
                        meta["expanded"] = not meta["expanded"]
                        changed = True
                finally:
                    imgui.pop_id()
                heading_hovered = _hovering(x, heading_y, row_width, heading_height)
                changed |= _tint_slot(draw_state, "project:" + run_id, painted,
                                      x + Melty.px(23), heading_y + max(0, (heading_height - Melty.px(17)) / 2),
                                      heading_hovered, heading_tint,
                                      lambda value, _p=project: set_row_tint(_p, value),
                                      show_brush=heading_hovered)
                # The heading's +: a new conversation in this folder, in the
                # source of the run's newest conversation.
                plus_x = x + row_width - pad - trash_width
                if new_conversation is not None and _button(
                        draw_state, "new-in:" + run_id, "+", plus_x, heading_y, trash_width, heading_tint,
                        not first_chats.loading and not first_chats.error, height=heading_height, background=False):
                    new_conversation(first_account, first_chats, project)
                    changed = True
                _title(label, x + Melty.px(46), heading_y,
                       plus_x - x - Melty.px(46), heading_height, heading_tint, ellipsis=True)
                if not meta["expanded"] and any(is_active(entry[5]) for entry in rows):
                    _running_dot(plus_x - trash_width / 2, heading_y + heading_height / 2, heading_tint)
            child_y = heading_y + heading_height + gap
            for account_id, chats, kind, key, child_meta, text, row_height, row_tint in children:
                row_id = account_id + ":" + key
                rect = (x + Melty.px(46), child_y, x + row_width - trash_width, child_y + row_height)
                renaming = getattr(state, "rename", None)
                editing = renaming is not None and renaming["account"] == account_id and renaming["key"] == key
                # Rows order by time: no drag reordering, and nothing registered
                # for a row scrolled out of the list.
                if not _visible(child_y, row_height, clip):
                    child_y += row_height + gap
                    continue
                tint = row_tint
                selected = account_id == state.account and key == state.selected.get(account_id)
                if selected:
                    _card(x + pad, child_y, row_width - pad * 2, row_height, tint, selected=True)
                # A row cut by the list's edge takes the pointer only where it
                # shows: hit rects ignore the clip, and a full-length one would
                # reach over the filter chips above or the button below.
                hit_top = max(child_y, clip[1]) if clip else child_y
                hit_bottom = min(child_y + row_height, clip[3]) if clip else child_y + row_height
                if hit_bottom - hit_top >= 1 and _button(draw_state, "chat:" + row_id, "", x + pad, hit_top,
                           row_width - pad * 2 - trash_width, tint, height=hit_bottom - hit_top, background=False):
                    state.selected[account_id] = key
                    state.account = account_id
                    if kind is not None:
                        state.provider = kind.name
                    changed = True
                row_rect = (x + pad, child_y, x + row_width - pad, child_y + row_height)
                hovered = draw_state.on_action("cursor_hover", view_id="chat-hover:" + row_id,
                                               rect=row_rect) is not None
                if is_active(chats.get(key)):
                    # An active conversation (my turn here, or a session
                    # written by anyone lately) shows the live indicator in
                    # its trash slot instead of the trash.
                    _running_dot(x + row_width - pad - trash_width / 2, child_y + row_height / 2, tint)
                elif hovered and hit_bottom - hit_top >= 1 and _button(
                        draw_state, "archive:" + row_id, trash_icon, x + row_width - pad - trash_width, hit_top,
                        trash_width, tint, height=hit_bottom - hit_top, background=False):
                    archive = (chats, account_id, key)
                if draw_state.on_action("left_mouse_double_clicked", view_id="rename:" + row_id,
                                        rect=rect, priority_delta=5) is not None:
                    state.rename = {"account": account_id, "key": key, "draft": text, "focus": True}
                    renaming = state.rename
                    editing = True
                    state.selected[account_id] = key
                    state.account = account_id
                    changed = True
                changed |= _tint_slot(draw_state, "chat:" + row_id, child_meta.get("tint"),
                                      x + Melty.px(23), child_y + max(0, (row_height - Melty.px(17)) / 2),
                                      hovered, heading_tint, conversation_tint_setter(child_meta),
                                      show_brush=selected)
                title_width = row_width - Melty.px(46) - trash_width
                if mixed and kind is not None:
                    # Multiple sources in one list: the provider's initials, dim, on the right.
                    tag = source_initials(kind.chat_label)
                    tag_width = _label_width(tag) + Melty.px(6)
                    title_width -= tag_width
                    _title(tag, x + row_width - trash_width - tag_width, child_y, tag_width, row_height, tint,
                           brightness=0.55)
                if not editing:
                    _title(text, x + Melty.px(46), child_y, title_width, row_height, tint, ellipsis=True)
                else:
                    imgui.set_cursor_screen_pos((x + Melty.px(46), child_y))
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
                    renaming["focus"] = False
                    if edited:
                        renaming["draft"] = value
                        changed = True
                    keys_pressed = {event[0] for event in Melty.frame_key_events}
                    if glfw.KEY_ESCAPE in keys_pressed:
                        state.rename = None
                        changed = True
                    elif (glfw.KEY_ENTER in keys_pressed or glfw.KEY_KP_ENTER in keys_pressed
                          or not request_focus and Melty.text_focused_ds is not text_ds):
                        if renaming["draft"].strip():
                            chats.get(key)["title"] = renaming["draft"].strip()
                        state.rename = None
                        changed = True
                child_y += row_height + gap
            y += card_height + gap
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
    from src.lsd.gl_gui.view.playground.terminal_playground import _resolve
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


def _hold_scroll_anchor(view, rows, height, restore):
    """Pin the top visible row to its screen position across relayouts.

    Each frame records (row key, pixels of that row above the viewport top);
    a frame whose heights moved (`restore`) re-derives the offset from it, so
    the reader's row stays put while rows above it re-wrap. Follow mode wins.
    """
    tops, y = {}, 0.0
    for row in rows:
        tops[row[0]] = y
        y += row[3]
    anchor = view.get("anchor")
    if restore and anchor is not None and not view.get("follow") and anchor[0] in tops:
        view["offset"] = max(0.0, min(tops[anchor[0]] + anchor[1], max(0.0, y - height)))
    offset = view.get("offset", 0.0)
    for row in rows:
        if tops[row[0]] + row[3] > offset:
            view["anchor"] = (row[0], offset - tops[row[0]])
            return
    view["anchor"] = None


def _draw_image(ref, x, y, max_width, box_height, caption_height, tint):
    """A picture fitted at (x, y) with its caption under it: the texture
    through the draw list (RGB16F for HDR sources, so an HDR desktop shows
    the highlights), a dim plate while it decodes, the error when it fails."""
    cache = image_cache()
    entry = cache.entry(ref)
    draw_list = imgui.get_window_draw_list()
    if Melty.channels_split:
        draw_list.channels_set_current(Melty.get_channel())  # body channel, see _card
    width, height = image_box(entry, max_width)
    caption = chat_images.image_label(ref, entry)
    if entry is not None and entry.status == "ready":
        texture = cache.texture(entry)
        if texture is not None:
            draw_list.add_image_rounded(texture, (x, y), (x + width, y + height),
                                        uv_a=(0.0, 0.0), uv_b=(1.0, 1.0), rounding=Melty.px(6))
    elif entry is not None and entry.status == "failed":
        caption = (caption + " · " if caption else "") + "could not decode: " + str(entry.error)
    else:
        draw_list.add_rect_filled(x, y, x + width, y + height, _color(tint, 0.12), rounding=Melty.px(6))
        caption = (caption + " · " if caption else "") + "decoding…"
    _title(caption or ref.label, x, y + height, max_width, caption_height, tint, brightness=0.7, ellipsis=True)


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
    # User and assistant prose sit inset in their span.
    # [tint=(0.95, 0.6, 0.25)]
    user_inset = Melty.px(12)
    # [tint=(0.95, 0.6, 0.25)]
    user_pad = Melty.px(6)
    rows = []
    row_width = max(Melty.px(60), width - Melty.px(7))
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
    # Chat layout: user cards sit off the left edge, assistant prose off the right,
    # by the same margin: 50 px in a narrow transcript, 100 px once it is wide.
    # [tint=(0.95, 0.6, 0.25)]
    chat_indent = min(Melty.px(100), max(Melty.px(50), row_width - Melty.px(600)))
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
    signature = (row_width, revision, len(messages), id(messages),
                 hash(frozenset(k for k, v in state.message_expanded.items() if v)),
                 hash(frozenset(k for k, v in state.output_expanded.items() if v)))
    cached = view.get("layout")
    if cached is not None and cached[0] == signature and not width_moving and not settling and not view.get("relayout"):
        rows, prose_index, prose_texts = cached[1], cached[2], cached[3]
    else:
        for message_id, message in messages.items():
            if not isinstance(message, Message):
                continue  # older cached sessions are replaced by the provider factory
            if isinstance(message, ReasoningMessage) and not any(
                    isinstance(value, str) and value.strip() for _, value, _ in _message_leaves(message["content"])):
                continue
            row_key = key + ":" + message_id
            terminal = isinstance(message, CommandExecution)
            has_header = not isinstance(message, (UserMessage, AssistantMessage))
            collapsible = has_header and isinstance(message, (ToolCall, ReasoningMessage))
            # Every collapsible row starts collapsed; the caret / file tags open it.
            expanded = not collapsible or state.message_expanded.get(row_key, False)
            # A collapsed row of a finished tool call lays out the same every
            # frame: keep its row between frames (most of an old transcript).
            row_signature = None
            if collapsible and not expanded and message.get("status") in ("completed", "failed"):
                row_signature = (id(message), message.get("status"), row_width, chat_indent, header_height,
                                 terminal_max_width, len(message["content"]), id(message.get("summary")))
            elif (not has_header and message.get("status") != "running" and not width_moving
                  and not any(isinstance(value, ImageReference) for value in message["content"].values())):
                # Finished assistant: its text is the same object until set_text replaces it.
                row_signature = (id(message), id(getattr(message, "source_text", None)), message.get("status"),
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
                        span = ((row_width - chat_indent if prose else row_width) - indent
                                - (0 if isinstance(message, UserMessage) else icon_column)
                                - (user_inset if isinstance(message, UserMessage) else 0))
                        if isinstance(message, AssistantMessage):
                            span = min(span, terminal_max_width)
                        image_width, image_height = image_box(image_entry, max(Melty.px(40), span))
                        display, leaf_height = value, image_height + header_height
                    elif isinstance(value, str):
                        # Only plain prose wraps; code, tool output and everything else keeps its lines.
                        wraps = prose and not isinstance(value, CodeString)
                        off_screen = layout_y > viewport_top + height or layout_y + Melty.px(2000) < viewport_top
                        display, leaf_height = _text_layout(state, (row_key, path), value,
                            wrap_width=(row_width - chat_indent - 2 * user_inset if isinstance(message, UserMessage)
                                        else min(row_width - chat_indent, terminal_max_width)) if wraps else None,
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
            if isinstance(message, ToolCall):
                header_width = (max(tag[2] + tag[4] for tag in tags) if tags else
                                tags_x + _label_width(_message_label(message, expanded)))
                header_width += Melty.px(104) if _message_failed(message) else Melty.px(8)
                fitted_width = min(row_width, max(content_width, header_width, Melty.px(110) if show_more else 0))
                if terminal:
                    fitted_width = min(fitted_width, terminal_max_width)  # collapsed labels clip at the cap too
            row = (row_key, message, leaves, full_height, collapsible, expanded, tags, summary_height, fitted_width, show_more, has_header, icon)
            if row_signature is not None and not (width_moving or settling):
                # (a row wrapped mid-resize keeps stale off-screen wraps: never memoised)
                row_memos[row_key] = (row_signature, row)
            rows.append(row)
            layout_y += full_height
        view["layout"] = (signature, rows, prose_index, prose_texts)
    total = sum(row[3] for row in rows)
    # A row the reader just expanded / collapsed / showed more of re-laid out
    # this frame: hold their row, and drop follow so a tall block opening near
    # the bottom doesn't fling the viewport off its end.
    relayout = view.pop("relayout", False)
    if relayout:
        view["follow"] = False
    _hold_scroll_anchor(view, rows, height, width_moving or settling or relayout)
    changed = False
    selecting = False        # a prose leaf took this frame's selection
    with _viewport(draw_state, state, key, width, height, total, follow=True) as (x, y, clip):
        for row_key, message, leaves, full_height, collapsible, expanded, tags, summary_height, fitted_width, show_more, has_header, icon in rows:
            content_x = 0 if isinstance(message, UserMessage) else icon_column
            tint = conversation_tint
            # User messages carry the chat tint; assistant prose stays unboxed.
            underlying = window_color
            prose = isinstance(message, (UserMessage, AssistantMessage))
            side = chat_indent if isinstance(message, UserMessage) else 0
            row_span = row_width - chat_indent if prose else row_width
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
                from src.lsd.gl_gui.view.playground.open_files import draw_changed_file_header
                from src.lsd.gl_gui.model.file_meta import FileMeta, file_meta_store
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
                        _draw_image(value, left, text_y, leaf_width, leaf_height - header_height, header_height, tint)
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


@window(tint=(1.34, 1.62, 1.76), display_name="Chat", icon=f"", initial={"width": 1100, "height": 760})
@render_func(auto_resize=False, min_width=700, min_height=500,
             on_cleanup=_cleanup_chat, use_cache=True, disable_scroll=True, imgui_padding=False, indent_size=0)
def draw_chat_interface(input_value=None, draw_state=None, bg_offset=-2, state: ChatInterfaceState = None,
                        column_edges=None, new_project=None, default_project=None, **kwargs):
    """The chat window. A new conversation runs in `new_project` when given
    (an app started for one project), else in the selected conversation's
    project, else `default_project` (an app's cwd; the studio: its checkout)."""
    # Ephemeral layout state also adopts already-open windows on hotswap.
    if not hasattr(state, "viewports"):
        state.viewports = {}
        state.text_layouts = {}
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
    changed = False

    def wake():
        # Like Fast Dock's external-change edge: the worker has queued new data.
        from src.lsd.gl_gui.utils.glfw_utils import request_render
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
            changed |= other_proxy.drain()
            other_proxy.reconcile()  # collect edits made in another view before applying metadata
    x, y = imgui.get_cursor_screen_pos()
    tint = kind.tint
    notice = "" if proxy is None else (proxy.error or ("Loading conversations…" if proxy.loading else ""))
    if notice:
        for line in notice.split("\n"):
            imgui.get_window_draw_list().add_text(*imgui.get_cursor_screen_pos(), _color((0.95, 0.65, 0.45)), line)
            imgui.dummy(1, imgui.get_text_line_height())
        if proxy.error:
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
                from src.lsd.gl_gui.utils.glfw_utils import request_render
                request_render()
            tab_x += tab_width + Melty.px(4)
        y += tab_height + Melty.px(Toggles.Chat.header_margin)
        # The age filter: a chip per AGE_FILTERS entry, the active one lifted.
        # A pick applies to the list in the same frame (read after the loop).
        chip_height, chip_x = Melty.px(22), x
        for label, hours in AGE_FILTERS:
            chip_width = _label_width(label) + Melty.px(16)
            if _button(draw_state, "age:" + label, label, chip_x, y, chip_width, tint,
                       height=chip_height, selected=hours == getattr(state, "age_hours", 0)):
                state.age_hours = hours
                changed = True
            chip_x += chip_width + Melty.px(4)
        age_hours = getattr(state, "age_hours", 0)
        y += chip_height + Melty.px(Toggles.Chat.header_margin)
        imgui.set_cursor_screen_pos((x, y))
        footer_height = Melty.px(30) + (y - top)
        shown_sources = [(account_id, proxies[account_id], kinds[account_id],
                          source_label(account_id, kinds[account_id], accounts)) for account_id in shown]

        def start_conversation(account_id, chats, project=None):
            """A fresh conversation in that source: in `project` (a heading's +),
            else `new_project` when the app was started for one, else its
            selected conversation's project, else `default_project`."""
            key = str(uuid.uuid4())
            current = chats.get(state.selected.get(account_id))
            project = (project or new_project or (current["project"] if current else None)
                       or state.projects.get(account_id) or default_project or str(Path(__file__).resolve().parents[5]))
            chats[key] = {"title": "New conversation", "project": project}
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
        edited, used_height = draw_chat_sidebar(shown_sources, draw_state, state, width,
                                                body_height - footer_height, cutoff=age_cutoff(age_hours),
                                                new_conversation=start_conversation)
        changed |= edited
        # The New conversation button follows the list directly (a list taller
        # than the column scrolls and the button sits at the column's foot);
        # it starts one in the primary folder (a heading's + picks its folder).
        y += used_height + Melty.px(Toggles.Chat.new_conversation_margin)
        imgui.set_cursor_screen_pos((x, y))
        if _button(draw_state, "new-chat", "+ New conversation", x, y, min(width, Melty.px(190)), tint,
                   proxy is not None and not proxy.loading and not proxy.error):
            start_conversation(state.account, proxy)
            changed = True
        imgui.dummy(width, Melty.px(25))
    # Sidebar selection takes effect in the same frame.
    selected = state.selected.get(state.account)
    with columns.cell(1, height=body_height) as width:
        if proxy is not None and selected in proxy:
            chat = proxy[selected]
            meta = chat.metadata
            chat_tint = tuple(meta.get("tint") or project_tint(chat["project"]) or kind.tint)
            x, y = imgui.get_cursor_screen_pos()
            changed |= _tint_slot(draw_state, "title-tint:" + selected, meta.get("tint"), x, y + Melty.px(5),
                                  _hovering(x, y, width, Melty.px(28)), chat_tint, conversation_tint_setter(meta))
            _title(chat["title"], x + Melty.px(26), y,
                   width - Melty.px(26), Melty.px(28), chat_tint)
            imgui.set_cursor_screen_pos((x, y))
            imgui.dummy(width, Melty.px(28))
            x, y = imgui.get_cursor_screen_pos()
            transcript_key = "history:" + state.account + ":" + selected
            for request_id, request in list(chat["requests"].items()):
                if request["kind"] == "approval":
                    for line in request["text"].split("\n"):
                        imgui.get_window_draw_list().add_text(*imgui.get_cursor_screen_pos(), _color((0.95, 0.75, 0.4)), line)
                        imgui.dummy(1, imgui.get_text_line_height())
                    x, y = imgui.get_cursor_screen_pos()
                    for index, decision in enumerate(("accept", "decline")):
                        if _button(draw_state, f"answer:{request_id}:{decision}", decision.title(),
                                   x + index * Melty.px(95), y, Melty.px(90), tint):
                            request["answer"] = {"decision": decision}
                            changed = True
                    imgui.dummy(1, Melty.px(30))
                else:
                    answers = state.answers.setdefault(str(request_id), {})
                    for question in request["data"].get("questions", []):
                        edited, answer = draw_text(answers.get(question["id"], ""), name=question["question"],
                            height=50, wrap=False, syntax_highlight=False, line_numbers=None, fim="", is_tree=False)
                        if edited:
                            answers[question["id"]] = answer
                            changed = True
                    x, y = imgui.get_cursor_screen_pos()
                    if _button(draw_state, f"reply:{request_id}", "Answer", x, y, Melty.px(85), tint):
                        request["answer"] = {"answers": {key: {"answers": [value]} for key, value in answers.items()}}
                        changed = True
                    imgui.dummy(1, Melty.px(30))
            # Reserve the composer before laying out the history. Scrolling
            # changes only message coordinates inside this history region.
            active = is_active(chat)
            history_height = max(Melty.px(40), body_bottom - imgui.get_cursor_screen_pos()[1]
                                 - Melty.px((145 if chat.error or chat.loading else 120) + (26 if active else 0)))
            changed |= draw_messages(chat["messages"], draw_state, state, transcript_key,
                                 width, history_height, conversation_tint=chat_tint, revision=proxy.revision)
            if active:
                # "<Provider> is typing..." with the live dot sits under the transcript
                # while a turn streams - ours, or another client's (a terminal)
                # writing the chat; the same state either way.
                x, y = imgui.get_cursor_screen_pos()
                _running_dot(x + Melty.px(10), y + Melty.px(12), chat_tint)
                _title(f"{kind.chat_label} is typing…", x + Melty.px(22), y, width - Melty.px(22), Melty.px(24),
                       chat_tint, brightness=0.8)
                imgui.set_cursor_screen_pos((x, y))
                imgui.dummy(1, Melty.px(26))
            if chat.error or chat.loading:
                message = chat.error or "Loading conversation…"
                imgui.get_window_draw_list().add_text(*imgui.get_cursor_screen_pos(), _color((0.95, 0.65, 0.4)), message)
                imgui.dummy(1, Melty.px(25))
            draft_key = state.account + ":" + selected
            edited, draft = draw_text(state.drafts.get(draft_key, ""), name="Message:" + selected,
                height=85, width=width, wrap=False, syntax_highlight=False, line_numbers=None, fim="", show_header=False,
                use_cache=True, is_tree=False, shadow=False, bg_offset=-1)
            if edited:
                state.drafts[draft_key] = draft
                changed = True
            x, y = imgui.get_cursor_screen_pos()
            # Stop while a turn runs (ours can be stopped; another client's is
            # shown the same, dimmed - impossible to send into a turn in progress).
            if _button(draw_state, "send-stop", "Stop" if active else "Send", x, y, Melty.px(85),
                       chat_tint, chat["running"] or (not active and bool(draft.strip()) and chat.loaded
                                                      and not chat.error)):
                if chat["running"]:
                    chat["running"] = False
                else:
                    chat["messages"][str(uuid.uuid4())] = user_message(draft)
                    state.drafts[draft_key] = ""
                changed = True
            imgui.dummy(1, Melty.px(30))
    columns.finish()
    if proxy is not None:
        proxy.reconcile()
    if changed:
        state.revision += 1
        # A click's effect (a row expanded, a filter picked) lays out on the
        # NEXT frame; ask for it now instead of waiting for the next input.
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()
    return changed, input_value
    if changed:
        state.revision += 1
    return changed, input_value