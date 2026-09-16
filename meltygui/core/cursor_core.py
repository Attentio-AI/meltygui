import inspect
import sys

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

from meltygui.core.dict_conversion import DictConversion
from meltygui.core.toggles import Toggles
from meltygui.utils.render_utils import LSDView
from meltygui.utils.render_utils import print_stack_trace
from meltygui.utils.render_utils import push_style_color
from meltygui.utils.render_utils import pop_style_color
from meltygui.utils.render_utils import print_colored_traceback
from meltygui.core.melty import Melty


def set_cursor_pos_y(pos_y):
    current_y = imgui.get_cursor_pos_y()
    imgui.set_cursor_pos_y(pos_y)
    height = imgui.get_cursor_pos_y() - current_y
    if Toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def set_cursor_pos_x(pos_x):
    current_x = imgui.get_cursor_pos_x()
    imgui.set_cursor_pos_x(pos_x)
    width = imgui.get_cursor_pos_x() - current_x
    if Toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def set_cursor_screen_pos(pos):
    imgui.set_cursor_screen_pos(pos)
    if Toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def set_cursor_screen_position(pos):
    imgui.set_cursor_screen_position(pos)
    if Toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def set_cursor_pos(pos):
    current_pos = imgui.get_cursor_pos()
    imgui.set_cursor_pos(pos)
    if Toggles.show_line_breaks:
        if draw_rect(width=5, height=None, color=(1.0, 0.0, 1.0, 0.8)):
            print_stack_trace(skip=-2)


def spacing():
    imgui.spacing()
    if Toggles.show_line_breaks:
        if draw_rect(width=3, height=None, color=(0, 0.1, 0.7, 0.8)):
            print_stack_trace(skip=-2)


def indent(indent_size=None, attr_name=None):
    if Toggles.show_line_breaks:
        if draw_rect(width=3, height=None, color=(0.1, 0.1, 1.0, 0.8)):
            print_stack_trace(skip=-2)
            if attr_name is not None:
                print(f"=======Indenting for {attr_name}=========")

        if not (is_hovered(width=5, height=None) and imgui.is_mouse_down(imgui.MOUSE_BUTTON_MIDDLE)):
            imgui.indent(indent_size)
    else:
        imgui.indent(indent_size)


def same_line(spacing=None):
    if Toggles.show_line_breaks:
        if draw_rect(width=3, height=None, color=(1.0, 0.5, 0.1, 0.5)):
            print_stack_trace(skip=-2)
        if not (is_hovered(width=3, height=None) and imgui.is_mouse_down(imgui.MOUSE_BUTTON_MIDDLE)):
            if spacing is None:
                imgui.same_line()
            else:
                imgui.same_line(spacing=spacing)
    else:
        if spacing is None:
            imgui.same_line()
        else:
            imgui.same_line(spacing=spacing)


def new_line():
    if Toggles.show_line_breaks:
        if draw_rect(width=3, height=None, color=(0.5, 0, 0, 0.5)):
            print_stack_trace(skip=-2)
        if not (is_hovered(width=3, height=None) and imgui.is_mouse_down(imgui.MOUSE_BUTTON_MIDDLE)):
            imgui.new_line()
    else:
        imgui.new_line()



def copy_attributes(source, target):
    """
    Copies attributes from source to target, excluding private attributes.
    If an attribute exists in both, it will be overwritten in the target.
    """
    if not isinstance(source, DictConversion) and not isinstance(target, DictConversion):
        return

    for attr_name, attr_value in source.__dict__.items():
        setattr(target, attr_name, attr_value)


def is_hovered(x=None, y=None, width=5, height=None):
    padding = imgui.get_style().window_padding
    if x is None:
        x = imgui.get_cursor_screen_pos()[0] - padding[0]
    if y is None:
        y = imgui.get_cursor_screen_pos()[1] - padding[1]
    if height is None:
        line_height = imgui.get_text_line_height()
        height = line_height + padding[1] * 2
    return imgui.is_mouse_hovering_rect(x, y, x + max(width, 10), y + height)


def render_dict_as_table(input_value=None, config=None):
    unique = config.unique

    num_columns = 2  # For key / value pair
    imgui.columns(num_columns, f"dict_columns##{unique}", True)
    for key, value in input_value.items():
        imgui.text(f"{key}")
        imgui.next_column()
        imgui.text(f"{value}")
        imgui.next_column()

    imgui.columns(1)  # Reset to single column layout


def draw_rect(x=None, y=None, width=5, height=None, color=(1, 1, 1, 1)):
    padding = imgui.get_style().window_padding
    if x is None:
        x = imgui.get_cursor_screen_pos()[0] - padding[0]
    if y is None:
        y = imgui.get_cursor_screen_pos()[1] - padding[1]
    if height is None:
        line_height = imgui.get_text_line_height()
        height = line_height + padding[1] * 2
    imgui.get_foreground_draw_list().add_rect_filled(
        x, y, x + width, y + height, pack_color(*color))

    clicked = False
    if imgui.is_mouse_hovering_rect(x, y, x + max(width, 10), y + height):
        white = (1.0, 1.0, 1.0, 1.0)
        imgui.get_foreground_draw_list().add_rect(
            x, y, x + width, y + height, pack_color(*white))
        if imgui.is_mouse_clicked(imgui.MOUSE_BUTTON_LEFT):
            clicked = True
    return clicked
