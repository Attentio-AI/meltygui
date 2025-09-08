from enum import Enum
from typing import Dict

import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.dict_conversion import DictConversion


class SynthColors(DictConversion):
    def __init__(self):
        super().__init__()
        self.letter_to_color = {}


# @window
# class Lora(DictConversion):
#     def __init__(self):
#         super().__init__()
#         self.tint = (0.2, 0.26, 0.34)
#         self.name: str = "Lora"
#         self.rank = 4
#
#         ignore_render
#         self.alpha = 2
#         self.lora_scale = 0.1
#         self.parent_module = None
#         self.adapter = None
#         self.target_modules = ["q_proj", "k_proj", "v_proj", "o_proj"]
class MouseState(DictConversion):
    def __init__(self):
        super().__init__()
        self.mouse_up = False
        self.mouse_down = False
        self.drag_released = False
        self.hovered = False
        self.clicked = False
        self.dragged = False
        self.mouse_down_pos = (0, 0)
        self.initial_screen_pos = (0, 0)
        self.drag_delta = (0, 0)


class DrawState(DictConversion):
    """Holds per-widget runtime state (expand/collapse, etc.)."""

    def __init__(self):
        super().__init__()

        self.unique = 0  # stable UI identifier
        self.expanded = True
        self.value_cache = None
        self.name = ""
        self.height = None
        self.expanded_height = None
        self.width = None
        self.top = None
        self.left = None
        self.search_text = ""
        self.search_active = False

        self.track_mouse = False

        self.mouse_btn_state = {0: MouseState(),
                                1: MouseState(),
                                2: MouseState()}
        self.mouse_up = False
        self.mouse_down = False
        self.drag_released = False
        self.hovered = False
        self.hotkey_receiver = False
        self.clicked = False
        self.dragged = False
        self.screen_pos = (0, 0)

        self.delete_countdown = Melty.save_draw_state_for

        # Profiling
        self.render_time = 0.0
        # add more per-widget state as needed

    def is_hovered(self):
        if self.left is None or self.top is None or self.width is None or self.height is None:
            return False
        rect = (self.left, self.top - 5, self.width, self.height + 10)
        if imgui.is_mouse_hovering_rect(rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]):
            return True
        return False


class KeyMod(Enum):
    CTRL = 'ctrl'
    ALT = 'alt'
    SHIFT = 'shift'


class Hotkey:
    def __init__(self, name, key, mod=None):
        self.name = name
        self.key = key
        self.mod = mod

    def mod_active(self):
        if self.mod == KeyMod.CTRL:
            return imgui.get_io().key_ctrl
        elif self.mod == KeyMod.ALT:
            return imgui.get_io().key_alt
        elif self.mod == KeyMod.SHIFT:
            return imgui.get_io().key_shift

        if self.mod is None:
            return True
