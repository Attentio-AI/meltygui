import functools as _functools
import gc as _gc
import locale
import threading as _threading
import time
from collections import deque, defaultdict
from contextlib import contextmanager as _contextmanager
from itertools import islice

import glfw
import imgui

from src.lsd.gl_gui.fonts import Font

# Use the system's locale time format (e.g. 12-hour AM/PM if configured)
# for %X instead of the default "C" locale 24-hour clock.
try:
    locale.setlocale(locale.LC_TIME, "")
except locale.Error:
    pass


def _timestamp():
    """Locale-aware time string (AM/PM on systems configured for 12-hour)."""
    return time.strftime("%X", time.localtime())


def _fade_opacity(created_at):
    """Opacity that decays with age: 100% fresh, 50% at ~10s, floored at 30%."""
    elapsed = time.time() - created_at
    return max(0.3, 1.0 - 0.05 * elapsed)

class NotificationCenter:
    max_notifications = 20
    notifications = deque(maxlen=max_notifications)
    tagged_notifications = defaultdict(lambda: deque(maxlen=NotificationCenter.max_notifications))
    ignore_tags = ["host"]
    # Single live value per tag (overwritten on each display() call), shown in
    # the dedicated "Live" column. Insertion order = row order.
    live_values = {}


def notify(text, tint=(1,1,1,1), tag=None, urgent=True):
    formatted_time = _timestamp()
    created_at = time.time()
    tint = tint if len(tint) == 4 else (*tint, 1)  # Ensure color has alpha

    if tag is None:
        NotificationCenter.tagged_notifications["General"].appendleft((text, tint, formatted_time, created_at))
    else:
        NotificationCenter.tagged_notifications[tag].appendleft((text, tint, formatted_time, created_at))

    if urgent:
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()


@_contextmanager
def lag_span(label, min_ms=50.0):
    """Notify (tag "lag") when the wrapped block ran slower than `min_ms` —
    near-zero cost when fast, so it can sit on hot paths. Includes the thread
    name: a slow span on a worker still stalls the render thread for its
    GIL-held portion, so every entry here is a frame-drop suspect."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        if ms >= min_ms:
            tint = (1.0, 0.25, 0.2) if ms >= 300 else (1.0, 0.65, 0.2)
            notify(f"{label}  {ms:.0f}ms  [{_threading.current_thread().name}]",
                   tint=tint, tag="lag")


def lag_traced(label, min_ms=50.0):
    """Decorator form of lag_span — stamp on any function suspected of
    GIL-held stalls; it reports only when a call actually ran slow."""
    def deco(fn):
        @_functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with lag_span(label, min_ms=min_ms):
                return fn(*args, **kwargs)
        return wrapper
    return deco


# GC watch: a generation-2 collection walks every live object with the GIL
# held - with several large cst dicts resident that's an intermittent multi-
# hundred-ms stall attributed to whichever thread happened to allocate. The
# lazy install keeps a hotswap re-exec (which reuses module globals) from
# stacking callbacks.
_gc_start = {}


def _gc_watch(phase, info):
    gen = info.get("generation")
    if phase == "start":
        _gc_start[gen] = time.perf_counter()
        return
    t0 = _gc_start.pop(gen, None)
    if t0 is not None:
        ms = (time.perf_counter() - t0) * 1000.0
        if ms >= 20.0:
            # urgent=False: a gc callback can fire at ANY allocation point,
            # including mid-operation at boot - never pull request_render (and
            # its lazy glfw import) from here; the next UI frame shows it.
            try:
                notify(f"gc gen{gen}  {ms:.0f}ms  collected={info.get('collected')}"
                       f"  [{_threading.current_thread().name}]",
                       tint=(1.0, 0.25, 0.2) if ms >= 300 else (1.0, 0.65, 0.2),
                       tag="lag", urgent=False)
            except Exception:
                pass


if not globals().get("_GC_WATCH_INSTALLED"):
    _GC_WATCH_INSTALLED = True
    _gc.callbacks.append(_gc_watch)


def _format_value(value):
    """Render a variety of value types into a compact display string."""
    # scalar tensors / numpy scalars into a plain python number
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = value.item()
        except Exception:
            return str(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def display(value, tint=(1, 1, 1, 1), tag=None, urgent=True):
    """Show a single live value in the "Live" column, keyed by `tag`.

    Unlike notify(), which keeps a running list per tag, display() overwrites the
    value for a tag each call so it updates in place over time."""
    formatted_time = _timestamp()
    created_at = time.time()
    tint = tint if len(tint) == 4 else (*tint, 1)  # Ensure color has alpha
    key = tag if tag is not None else "value"
    NotificationCenter.live_values[key] = (_format_value(value), tint, formatted_time, created_at)

    # if urgent:
    #     from src.lsd.gl_gui.utils.glfw_utils import request_render
    #     request_render()


def _wrap_text(text, max_width):
    """Greedy word-wrap `text` into lines no wider than `max_width` px.
    Preserves explicit newlines. A single word wider than the limit is left on
    its own line (it overflows rather than being split mid-word)."""
    if max_width <= 0:
        return [text]
    lines = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split(" "):
            candidate = word if not current else current + " " + word
            if not current or imgui.calc_text_size(candidate).x <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _entry_height(label, content, content_width, line_height, padding):
    """Vertical space one stacked entry consumes: wrapped block + bg padding +
    the inter-entry gap (matches _draw_column_entry's bg_top - padding step)."""
    label_size = imgui.calc_text_size(label)
    lines = _wrap_text(content, content_width - (label_size.x + padding))
    return max(1, len(lines)) * line_height + padding * 3


def _draw_column_entry(draw_list, column_left, content_width, line_height, padding,
                       bg_bottom, label, label_color, content, content_color, opacity=1.0,
                       hit_rects=None, copy_text=None):
    """Draw one stacked entry: a left-aligned `label` (time / tag name) followed
    by the wrapped, tinted `content`. `label_color`/`content_color` are RGBA
    tuples; `opacity` scales every alpha so the whole toast fades with age.
    When `hit_rects` is given, appends (rect, copy_text) so the caller can make
    the entry click-to-copy. Returns the next bg_bottom (above this one)."""
    label_size = imgui.calc_text_size(label)
    content_x = column_left + label_size.x + padding

    lines = _wrap_text(content, content_width - (label_size.x + padding))
    block_height = max(1, len(lines)) * line_height
    bg_top = bg_bottom - (block_height + padding * 2)
    content_top = bg_top + padding

    label_u32 = imgui.get_color_u32_rgba(label_color[0], label_color[1],
                                         label_color[2], label_color[3] * opacity)
    content_u32 = imgui.get_color_u32_rgba(content_color[0], content_color[1],
                                           content_color[2], content_color[3] * opacity)

    rect = (column_left - padding, bg_top, column_left + content_width + padding, bg_bottom)

    # background rectangle with some transparency
    draw_list.add_rect_filled(*rect, imgui.get_color_u32_rgba(0, 0, 0, opacity), rounding=2)

    if hit_rects is not None:
        hit_rects.append((rect, content if copy_text is None else copy_text))

    # label on the first line, then the wrapped, left-aligned content
    draw_list.add_text(column_left, content_top, label_u32, label)
    for li, line in enumerate(lines):
        draw_list.add_text(content_x, content_top + li * line_height, content_u32, line)

    return bg_top - padding


def _draw_column_title(draw_list, x, y, color, title, hit_rects=None, copy_text=None):
    """Draw a column's title and register its text box as a click target that
    copies `copy_text` (the whole column)."""
    draw_list.add_text(x, y, color, title)
    if hit_rects is not None and copy_text:
        size = imgui.calc_text_size(title)
        hit_rects.append(((x, y, x + size.x, y + size.y), copy_text))


# Collapsed columns show only this many of the newest toasts; hovering the
# overlay reveals the full list.
_COLLAPSED_COUNT = 2

# Rect of the most recently copied entry and when it was copied, so the click gets
# a brief visual confirmation. Guarded so a hotswap re-exec keeps the value.
_copy_flash = globals().get("_copy_flash")
_COPY_FLASH_SECONDS = 0.6


def _handle_entry_click(hit_rects):
    """Click-to-copy: if the mouse was just pressed inside one of the drawn
    entry rects, put that entry's text on the clipboard and flash it."""
    global _copy_flash
    if not hit_rects or not imgui.is_mouse_clicked(0):
        return
    mouse_pos = imgui.get_io().mouse_pos
    mx, my = mouse_pos.x, mouse_pos.y
    # Later entries are drawn above earlier ones and never overlap, so the first
    # containing rect is the hit.
    for (x0, y0, x1, y1), text in hit_rects:
        if x0 <= mx <= x1 and y0 <= my <= y1:
            imgui.set_clipboard_text(text)
            _copy_flash = ((x0, y0, x1, y1), time.time())
            return


def _draw_copy_flash(draw_list):
    """Outline the entry that was just copied, fading out over ~0.6s."""
    if _copy_flash is None:
        return
    (x0, y0, x1, y1), copied_at = _copy_flash
    remaining = _COPY_FLASH_SECONDS - (time.time() - copied_at)
    if remaining <= 0:
        return
    alpha = remaining / _COPY_FLASH_SECONDS
    draw_list.add_rect(x0, y0, x1, y1,
                       imgui.get_color_u32_rgba(1, 1, 0, alpha), rounding=2, thickness=1.5)
    from src.lsd.gl_gui.utils.glfw_utils import request_render
    request_render()


def draw_notifications():
    io = imgui.get_io()
    display_size = io.display_size
    draw_list = imgui.get_overlay_draw_list()

    from src.lsd.gl_gui.melty import Melty
    font_handle = Melty.font_mgr.get(Font.JETBRAINS_MONO_14) if Melty.font_mgr else None
    if font_handle is not None:
        imgui.push_font(font_handle)

    try:
        padding = 2
        column_width = 300                          # also the toast max width
        content_width = column_width - padding * 2  # left-aligned content box
        line_height = imgui.get_text_line_height()
        title_color = imgui.get_color_u32_rgba(1, 1, 0, 1)

        tagged_columns = [(tag, notifications) for tag, notifications
                          in NotificationCenter.tagged_notifications.items()
                          if tag not in NotificationCenter.ignore_tags]
        live_entries = [(tag + " ", value_str)
                        for tag, (value_str, _c, _t, _a) in NotificationCenter.live_values.items()]

        # Hover test against the collapsed footprint only of the entries that are
        # always visible. Expansion grows upward, away from the cursor, so the
        # mouse stays inside the zone while expanded and it can't flicker.
        n_columns = len(tagged_columns) + (1 if live_entries else 0)
        if n_columns == 0:
            return
        max_height = 0
        for _tag, notifications in tagged_columns:
            max_height = max(max_height, sum(
                _entry_height(time_label, text, content_width, line_height, padding)
                for text, _color, time_label, _created_at in islice(notifications, _COLLAPSED_COUNT)))
        if live_entries:
            max_height = max(max_height, sum(
                _entry_height(label, value_str, content_width, line_height, padding)
                for label, value_str in live_entries))
        overlay_left = display_size.x - (column_width * n_columns) - 10 - padding
        overlay_top = display_size.y - 30 - padding - max_height
        hovered = (io.mouse_pos.x >= overlay_left and io.mouse_pos.y >= overlay_top)
        limit = None if hovered else _COLLAPSED_COUNT
        hit_rects = []

        for c_idx, (tag, notifications) in enumerate(tagged_columns):
            column_left = display_size.x - (column_width * (c_idx + 1)) - 10

            # tag title pinned at the bottom of the column; clicking it copies
            # the tag's whole history, not just the entries on screen.
            _draw_column_title(draw_list, column_left, display_size.y - 30, title_color, tag,
                               hit_rects, "\n".join(text for text, _c, _t, _a in notifications))

            # stack toasts upward from just above the tag title (newest at bottom)
            bg_bottom = display_size.y - 30 - padding
            for text, color, time_label, created_at in islice(notifications, limit):
                opacity = _fade_opacity(created_at)
                bg_bottom = _draw_column_entry(draw_list, column_left, content_width,
                                               line_height, padding, bg_bottom,
                                               time_label, color, text, color, opacity,
                                               hit_rects=hit_rects)

        # dedicated "Live" column to the left of the tagged notification columns;
        # each entry is a tag's current value, tinted, updated in place over time.
        if live_entries:
            c_idx = len(tagged_columns)
            column_left = display_size.x - (column_width * (c_idx + 1)) - 10
            label_color = (0.6, 0.6, 0.6, 1)

            _draw_column_title(draw_list, column_left, display_size.y - 30, title_color, "Live",
                               hit_rects, "\n".join(f"{label}{value_str}"
                                                    for label, value_str in live_entries))

            bg_bottom = display_size.y - 30 - padding
            for tag, (value_str, color, _time_label, created_at) in NotificationCenter.live_values.items():
                opacity = _fade_opacity(created_at)
                bg_bottom = _draw_column_entry(draw_list, column_left, content_width,
                                               line_height, padding, bg_bottom,
                                               tag + " ", label_color, value_str, color, opacity,
                                               hit_rects=hit_rects)

        _handle_entry_click(hit_rects)
        _draw_copy_flash(draw_list)
    finally:
        if font_handle is not None:
            imgui.pop_font()

