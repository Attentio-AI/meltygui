from enum import Enum

import glfw
import imgui


class Action:

    def __init__(self, trigger_condition, clear_condition, re_arm_condition=None):
        self.trigger_condition = trigger_condition
        self.clear_condition = clear_condition
        self.re_arm_condition = re_arm_condition


def drag_released(unique):
    mouse_released = imgui.is_mouse_released(0)
    if mouse_released:
        pass
    drag_released = (imgui.is_mouse_released(0) and
                     unique == Melty.triggered_actions.get('on_drag', None))
    return drag_released

class ActionType(Enum):
    CLICK = 'on_click'
    DOWN = 'on_mouse_down'
    DRAG = 'on_drag'
    DRAG_UP = 'on_drag_up'
    HOVERED = 'on_hover'

class MouseAction:
    def __init__(self, action_type: ActionType, button=0):
        self.action_type = action_type
        self.button = button

class Melty:
    max_depth = 40
    annotation_mode = True
    depth = 0
    current_indent = 0

    @staticmethod
    def indent(amount):
        Melty.current_indent += amount
        imgui.indent(amount)

    @staticmethod
    def unindent(amount):
        Melty.current_indent -= amount
        imgui.unindent(amount)

    @staticmethod
    def check_event(unique, mouse_btn, event_type):
        if unique in Melty.triggered_actions:
            action = Melty.triggered_actions[unique]
            if action.action_type == event_type and action.button == mouse_btn:
                return True
        return False

    @staticmethod
    def inside_window():
        return len(Melty.window_stack) > 0

    @staticmethod
    def mark_event(unique, mouse_btn, event_type: ActionType):
        Melty.triggered_actions[unique] = MouseAction(event_type, mouse_btn)

    @staticmethod
    def shift_down():
        return (glfw.get_key(Melty.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                     glfw.get_key(Melty.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)

    spacing = (4,3)
    padding = (4,3)
    last_frame_actions = {}
    tracked_views = set()

    hover_stack = []
    # One for each mouse button
    triggered_actions = {}
    dragged_item = None
    selected_views = {}
    drag_in_progress = False
    initial_drag_offset = (0,0)

    nearest_drop_target = None
    nearest_drop_distance = None



    max_distance = 200
    drag_drop_target = None
    target_distance = max_distance

    vis = None
    type_defaults = {}
    unique_stack = []
    window_stack = []

    @staticmethod
    def shift_key():
        return (glfw.get_key(Melty.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                glfw.get_key(Melty.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)

    @staticmethod
    def init(**kwargs):
        for key, value in kwargs.items():
            setattr(Melty, key, value)
        Melty.annotation_mode = False

    @staticmethod
    def is_key_pressed(key=glfw.KEY_ESCAPE):
        if imgui.is_any_item_focused() or imgui.is_any_item_active():
            # If any item is focused or active, we don't want to capture key presses
            return False

        if key not in Melty.vis.tracked_keys:
            Melty.vis.tracked_keys.append(key)
            Melty.vis.first_frame_keys.add(key)

        if glfw.get_key(Melty.vis.window, key) == glfw.PRESS:
            if key in Melty.vis.first_frame_keys:
                return True
        return False
