import time
from collections import deque

import glfw
import imgui

class NotificationCenter:
    notifications = deque(maxlen=5)




def notify(text, color=(1,1,1,1)):
    formatted_time = time.strftime("%H:%M:%S", time.localtime())
    color = color if len(color) == 4 else (*color, 1)  # Ensure color has alpha
    NotificationCenter.notifications.appendleft((text, color,  formatted_time))
    from src.lsd.gl_gui.utils.glfw_utils import request_render
    request_render()


def draw_notifications():
    display_size = imgui.get_io().display_size

    # bottom-right corner with some padding
    draw_list = imgui.get_overlay_draw_list()
    for i, (text, color, time_label) in enumerate(NotificationCenter.notifications):
        imgui_color = imgui.get_color_u32_rgba(*color)
        text_size = imgui.calc_text_size(text)
        time_size = imgui.calc_text_size(time_label)
        padding = 10
        x = display_size.x - text_size.x - padding - time_size.x - padding
        y = display_size.y - (text_size.y + padding) * (i + 1)

        # Draw background rectangle with some transparency
        draw_list.add_rect_filled(x - padding, y - padding, x + text_size.x + time_size.x + padding * 2, y + text_size.y + padding,
                                  imgui.get_color_u32_rgba(0, 0, 0, 0.5))

        # Draw the notification text
        draw_list.add_text(x, y, imgui_color, time_label)
        draw_list.add_text(x + time_size.x + padding, y, imgui_color, text)

