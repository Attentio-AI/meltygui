import glfw
import imgui


class Melty:

    @classmethod
    def on_click(cls):
        return imgui.is_item


    actions = {
        'on_click': lambda: imgui.is_item_hovered() and imgui.is_mouse_clicked(0),
        'on_left_click': lambda : imgui.is_item_hovered() and imgui.is_mouse_clicked(0),
        'on_right_click': lambda: imgui.is_item_hovered() and imgui.is_mouse_clicked(1),
        'on_hover': lambda: imgui.is_item_hovered()
    }

    action_stack = {}
    prior_action_stack = {}
    triggered_actions = {}

    vis = None
    type_defaults = {}
    unique_stack = []

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
