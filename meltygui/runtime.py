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
    CLICK = 'click'
    SHIFT_CLICK = 'shift_click'
    HOVERED = 'hovered'
    DRAG = 'drag'
    NONE = 'none'

class Melty:
    actions = {
        'on_hover': Action(trigger_condition=lambda is_hovered, unique: is_hovered,
                           clear_condition=lambda is_hovered, unique: not is_hovered,
                           re_arm_condition=lambda is_hovered, unique: not is_hovered),
        'on_click': Action(trigger_condition= lambda is_hovered, unique: is_hovered and imgui.is_mouse_down(0) and not Melty.shift_key(),
                            clear_condition= lambda is_hovered, unique: 'on_click' in Melty.triggered_actions,
                           re_arm_condition= lambda is_hovered, unique: not imgui.is_mouse_down(0)),
        'shift_click': Action(trigger_condition=lambda is_hovered, unique: is_hovered and imgui.is_mouse_down(0) and
                                                               Melty.shift_key(),
                           clear_condition=lambda is_hovered, unique: 'shift_click' in Melty.triggered_actions,
                           re_arm_condition=lambda is_hovered, unique: not imgui.is_mouse_down(0)),
        'on_right_click': Action(trigger_condition=lambda is_hovered, unique: is_hovered and imgui.is_mouse_down(1),
                           clear_condition=lambda is_hovered, unique: not imgui.is_mouse_down(0),
                           re_arm_condition=lambda is_hovered, unique: not imgui.is_mouse_down(1)),
        'on_middle_click': Action(trigger_condition=lambda is_hovered, unique: is_hovered and imgui.is_mouse_down(2),
                                 clear_condition=lambda is_hovered, unique: 'on_right_click' in Melty.triggered_actions,
                                 re_arm_condition=lambda is_hovered, unique: not imgui.is_mouse_down(2)),
        'on_drag': Action(trigger_condition=lambda is_hovered, unique: is_hovered and imgui.is_mouse_down(0) and
                          'on_drag' not in Melty.triggered_actions,
                           clear_condition=lambda is_hovered, unique: not imgui.is_mouse_down(0),
                           re_arm_condition=lambda is_hovered, unique: not imgui.is_mouse_down(0)),

        'on_drag_released': Action(trigger_condition=lambda is_hovered, unique: 'on_drag' in Melty.last_triggered_actions and
                                                                    'on_drag' not in Melty.triggered_actions and
                                                                    Melty.last_triggered_actions['on_drag'] == unique,
                          clear_condition=lambda is_hovered, unique:  not imgui.is_mouse_down(0) and unique == Melty.last_triggered_actions.get('on_drag_released', None),
                          re_arm_condition=lambda is_hovered, unique: not imgui.is_mouse_down(0) and unique == Melty.last_triggered_actions.get('on_drag_released', None)),
        'any_drag_released': Action(trigger_condition=lambda is_hovered, unique: 'on_drag' in Melty.last_triggered_actions and
                                                                    'on_drag' not in Melty.triggered_actions,
                                   clear_condition=lambda is_hovered, unique: not imgui.is_mouse_down(
                                       0) and is_hovered == Melty.last_triggered_actions.get('on_drag_released', None),
                                   re_arm_condition=lambda is_hovered, unique: not imgui.is_mouse_down(
                                       0) and is_hovered == Melty.last_triggered_actions.get('on_drag_released', None)),


    }

    action_stack = {}
    prior_action_stack = {}
    triggered_actions = {}
    last_triggered_actions = {}
    cleared_actions = set()

    vis = None
    type_defaults = {}
    unique_stack = []
    window_stack = []

    selected_views = {}

    @staticmethod
    def shift_key():
        return (glfw.get_key(Melty.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                glfw.get_key(Melty.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)

    @staticmethod
    def init(**kwargs):
        for key, value in kwargs.items():
            setattr(Melty, key, value)

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
