import time
from collections import deque, defaultdict

import glfw
import imgui

class NotificationCenter:
    max_notifications = 20
    notifications = deque(maxlen=max_notifications)
    tagged_notifications = defaultdict(lambda: deque(maxlen=NotificationCenter.max_notifications))


def notify(text, tint=(1,1,1,1), tag=None, urgent=True):
    formatted_time = time.strftime("%H:%M:%S", time.localtime())
    tint = tint if len(tint) == 4 else (*tint, 1)  # Ensure color has alpha

    if tag is None:
        NotificationCenter.tagged_notifications["General"].appendleft((text, tint, formatted_time))
    else:
        NotificationCenter.tagged_notifications[tag].appendleft((text, tint, formatted_time))

    if urgent:
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()


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


def draw_notifications():
    display_size = imgui.get_io().display_size
    draw_list = imgui.get_overlay_draw_list()

    padding = 2
    column_width = 300                          # also the toast max width
    content_width = column_width - padding * 2  # left-aligned content box
    line_height = imgui.get_text_line_height()

    for c_idx, (tag, notifications) in enumerate(NotificationCenter.tagged_notifications.items()):
        column_left = display_size.x - (column_width * (c_idx + 1)) - 10

        # tag title pinned at the bottom of the screen
        draw_list.add_text(column_left, display_size.y - 30,
                           imgui.get_color_u32_rgba(1, 1, 0, 1), tag)

        # stack toasts upward from just above the tag title (newest at bottom)
        bg_bottom = display_size.y - 30 - padding
        for text, color, time_label in notifications:
            imgui_color = imgui.get_color_u32_rgba(*color)
            time_size = imgui.calc_text_size(time_label)
            text_x = column_left + time_size.x + padding

            lines = _wrap_text(text, content_width - (time_size.x + padding))
            block_height = max(1, len(lines)) * line_height
            bg_top = bg_bottom - (block_height + padding * 2)
            content_top = bg_top + padding

            # background rectangle with some padding
            draw_list.add_rect_filled(column_left - padding, bg_top,
                                      column_left + content_width + padding, bg_bottom,
                                      imgui.get_color_u32_rgba(0, 0, 0, 1.0), rounding=2)

            # time label on the first line, then the wrapped, left-aligned text
            draw_list.add_text(column_left, content_top, imgui_color, time_label)
            for li, line in enumerate(lines):
                draw_list.add_text(text_x, content_top + li * line_height, imgui_color, line)

            bg_bottom = bg_top - padding

