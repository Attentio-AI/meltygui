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

class Melty:


    actions = {
        'on_click': Action(trigger_condition= lambda unique: imgui.is_item_hovered() and imgui.is_mouse_down(0),
                            clear_condition= lambda unique: 'on_click' in Melty.triggered_actions,
                           re_arm_condition= lambda unique: not imgui.is_mouse_down(0)),
        'on_right_click': Action(trigger_condition=lambda unique: imgui.is_item_hovered() and imgui.is_mouse_down(1),
                           clear_condition=lambda unique: unique == Melty.last_triggered_actions.get('on_right_click',
                                                                                                     None),
                           re_arm_condition=lambda unique: not imgui.is_mouse_down(1)),
        'on_middle_click': Action(trigger_condition=lambda unique: imgui.is_item_hovered() and imgui.is_mouse_down(2),
                                 clear_condition=lambda unique: 'on_right_click' in Melty.triggered_actions,
                                 re_arm_condition=lambda unique: not imgui.is_mouse_down(2)),
        'on_drag': Action(trigger_condition=lambda unique: imgui.is_item_hovered() and imgui.is_mouse_down(0) and
                          'on_drag' not in Melty.triggered_actions,
                           clear_condition=lambda unique: not imgui.is_mouse_down(0),
                           re_arm_condition=lambda unique: not imgui.is_mouse_down(0)),
        'on_drag_released': Action(trigger_condition=lambda unique: 'on_drag' in Melty.last_triggered_actions and
                                                                    'on_drag' not in Melty.triggered_actions and
                                                                    Melty.last_triggered_actions['on_drag'] == unique,
                          clear_condition=lambda unique:  not imgui.is_mouse_down(0) and unique == Melty.last_triggered_actions.get('on_drag_released', None),
                          re_arm_condition=lambda unique: not imgui.is_mouse_down(0) and unique == Melty.last_triggered_actions.get('on_drag_released', None)),
        'any_drag_released': Action(trigger_condition=lambda unique: 'on_drag' in Melty.last_triggered_actions and
                                                                    'on_drag' not in Melty.triggered_actions,
                                   clear_condition=lambda unique: not imgui.is_mouse_down(
                                       0) and unique == Melty.last_triggered_actions.get('on_drag_released', None),
                                   re_arm_condition=lambda unique: not imgui.is_mouse_down(
                                       0) and unique == Melty.last_triggered_actions.get('on_drag_released', None)),

        'on_left_click': lambda unique : imgui.is_item_hovered() and imgui.is_mouse_clicked(0),
        'on_hover': lambda unique: imgui.is_item_hovered()
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
