from enum import Enum

import glfw
import imgui
import libcst as cst

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.view.core_views.core_decoration import no_save, exclude


# class decoration

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

@no_save("mouse_up", "mouse_down", "drag_released", "hovered", "clicked", "dragged",
         "mouse_down_pos", "initial_screen_pos", "drag_delta")
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


class CSTDrawBits:
    def __init__(self):
        super().__init__()
        self.path_key: tuple[tuple[str, int | None], ...] = ()
        self.module_id: str = ""
        self.root_gen: int = 0
        self.anchor: tuple = ()
        self.text_buf: str = ""  # generic edit buffer

class DragMode(Enum):
    NONE = 'none'
    MOVE = 'move'
    RESIZE_BR = 'resize_br'


@no_save("mouse_btn_state", "mouse_up", "mouse_down", "bounding_width", "bounding_height",
         "drag_released", "clicked", "dragged", "render_time", "imgui_is_toggled_open", "z_pos",
         "is_active", "is_focused", "scroll_offset", "drag_window_pos_x", "drag_window_pos_y", "drag_mode")
@exclude("render_time", "bounds_left", "bounds_top", "_input_value", "width", "flow_spacing",
         "hovered", "_did_use_cache", "height", "top", "left", "delete_countdown", "z_pos")
class DrawState(DictConversion):
    """Holds per-widget runtime state (expand/collapse, etc.)."""

    def __init__(self):
        super().__init__()
        self.drag_window_pos_x = None
        self.drag_window_pos_y = None
        # Imgui state mirror
        self.is_active = False
        self.is_focused = False
        self.scroll_offset = (0, 0)
        self.imgui_is_active = False
        self.imgui_is_focused = False
        self.imgui_is_hovered = False
        self.imgui_is_edited = False
        self.imgui_is_item_activated = False
        self.cst = None
        self.window_pos = None
        self.window_size = (100, 100)
        self.drag_mode = DragMode.NONE
        self.use_child = False
        self.z_pos = None

        self.imgui_is_toggled_open = False

        self._previous_hash = None

        self.unique = 0  # stable UI identifier
        self.expanded = True
        self.value_cache = None
        self.name = ""
        self.height = None
        self.expanded_height = None
        self.width = None
        self.bounding_width = 0
        self.bounding_height = 0

        self._left_rel = None
        self._top_rel = None
        self._min_width = None
        self.top = None
        self.left = None
        self.search_text = ""
        self.search_active = False
        self.flow_spacing = 0.0
        self.enabled = True
        self._end_header_size = (0,0)
        self._max_indent = 0
        self._name_edit = False
        self._screen_pos = (0, 0)
        self._did_use_cache = False

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
        self.drag_delta = (0, 0)
        self._input_value = None
        self.is_hovered_last = False

        self.bounds_left = None
        self.bounds_top = None

        self.delete_countdown = Melty.save_draw_state_for

        # Profiling
        self.render_time = 0.0
        # add more per-widget state as needed

    def init_cst_state(self, node, module_id: str):
        self.cst = CSTDrawBits()
        self.cst.path_key = Melty.current_path()
        self.cst.module_id = module_id
        self.cst.root_gen = Melty.current_gen(module_id)
        # super light anchor for re-attachment later (customize as you like)
        if isinstance(node, cst.Name):
            self.cst.anchor = ("Name", node.value)
        elif isinstance(node, cst.Attribute):
            self.cst.anchor = ("Attr", node.attr.value)
        else:
            self.cst.anchor = (type(node).__name__,)

    def proxy_bounds(self, inner_draw_state):
        if inner_draw_state is None or inner_draw_state.left is None:
            return

        if self.left is None:
            self.left = inner_draw_state.left
        else:
            self.left = max(self.left, inner_draw_state.left)

        if self.top is None:
            self.top = inner_draw_state.top
        else:
            self.top = max(self.top, inner_draw_state.top)

        if self.width is None:
            self.width = inner_draw_state.width
        else:
            self.width = max(self.width, inner_draw_state.width)

        if self.height is None:
            self.height = inner_draw_state.height
        else:
            self.height = max(self.height, inner_draw_state.height)

    def is_glfw_mouse_hovering_rect(self, x1, y1, x2, y2):

        global_mouse = glfw.get_cursor_pos(Melty.glfw_window)
        mx, my = global_mouse
        # basic collision check
        if x1 <= mx <= x2 and y1 <= my <= y2:
            return True
        return False

    def is_hovered(self):
        if self.left is None or self.top is None or self.width is None or self.height is None:
            return False
        rect = (self.left, self.top - 5, self.width, self.height + 10)
        if self.is_glfw_mouse_hovering_rect(rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]):
            return True
        return False

    def is_bounding_hovered(self):
        if self.bounds_top is None or self.bounds_left is None or self.bounding_width is None or self.bounding_height is None:
            return False
        rect = (self.bounds_left, self.bounds_top + 5, self.bounding_width, self.bounding_height + 10)
        if self.is_glfw_mouse_hovering_rect(rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]):
            return True
        return False


class KeyMod(Enum):
    CTRL = 'ctrl'
    ALT = 'alt'
    SHIFT = 'shift'


class Hotkey:
    def __init__(self, key=None, name="", mod=None, scoped=True):
        self.name = name
        self.key = key
        self.mod = mod
        self.scoped = scoped

    # hashing and equality based on key and mod only
    def __hash__(self):
        return hash((self.key, self.mod))

    def __eq__(self, other):
        if not isinstance(other, Hotkey):
            return False
        return self.key == other.key and self.mod == other.mod

    def mod_active(self):
        if self.mod == KeyMod.CTRL:
            return imgui.get_io().key_ctrl
        elif self.mod == KeyMod.ALT:
            return imgui.get_io().key_alt
        elif self.mod == KeyMod.SHIFT:
            return imgui.get_io().key_shift

        if self.mod is None:
            return True
