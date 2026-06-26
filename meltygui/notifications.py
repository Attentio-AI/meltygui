import locale
import time
from collections import deque, defaultdict

import glfw
import imgui

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


def _draw_column_entry(draw_list, column_left, content_width, line_height, padding,
                       bg_bottom, label, label_color, content, content_color, opacity=1.0):
    """Draw one stacked entry: a left-aligned `label` (time / tag name) followed
    by the wrapped, tinted `content`. `label_color`/`content_color` are RGBA
    tuples; `opacity` scales every alpha so the whole toast fades with age.
    Returns the next bg_bottom (above this one)."""
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

    # background rectangle with some transparency
    draw_list.add_rect_filled(column_left - padding, bg_top,
                              column_left + content_width + padding, bg_bottom,
                              imgui.get_color_u32_rgba(0, 0, 0, opacity), rounding=2)

    # label on the first line, then the wrapped, left-aligned content
    draw_list.add_text(column_left, content_top, label_u32, label)
    for li, line in enumerate(lines):
        draw_list.add_text(content_x, content_top + li * line_height, content_u32, line)

    return bg_top - padding


def draw_notifications():
    display_size = imgui.get_io().display_size
    draw_list = imgui.get_overlay_draw_list()

    padding = 2
    column_width = 300                          # also the toast max width
    content_width = column_width - padding * 2  # left-aligned content box
    line_height = imgui.get_text_line_height()
    title_color = imgui.get_color_u32_rgba(1, 1, 0, 1)

    for c_idx, (tag, notifications) in enumerate(NotificationCenter.tagged_notifications.items()):
        if tag in NotificationCenter.ignore_tags:
            continue
        column_left = display_size.x - (column_width * (c_idx + 1)) - 10

        # tag title pinned at the bottom of the screen
        draw_list.add_text(column_left, display_size.y - 30, title_color, tag)

        # stack toasts upward from just above the tag title (newest at bottom)
        bg_bottom = display_size.y - 30 - padding
        for text, color, time_label, created_at in notifications:
            opacity = _fade_opacity(created_at)
            bg_bottom = _draw_column_entry(draw_list, column_left, content_width,
                                           line_height, padding, bg_bottom,
                                           time_label, color, text, color, opacity)

    # dedicated "Live" column to the left of the tagged notification columns;
    # each row is one tag's current value, tinted, updated in place over time.
    if NotificationCenter.live_values:
        c_idx = len(NotificationCenter.tagged_notifications)
        column_left = display_size.x - (column_width * (c_idx + 1)) - 10
        label_color = (0.6, 0.6, 0.6, 1)

        draw_list.add_text(column_left, display_size.y - 30, title_color, "Live")

        bg_bottom = display_size.y - 30 - padding
        for tag, (value_str, color, _time_label, created_at) in NotificationCenter.live_values.items():
            opacity = _fade_opacity(created_at)
            bg_bottom = _draw_column_entry(draw_list, column_left, content_width,
                                           line_height, padding, bg_bottom,
                                           tag + " ", label_color, value_str, color, opacity)

