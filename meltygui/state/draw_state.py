from enum import Enum

import glfw
import imgui
import libcst as cst

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save, exclude, deep_refresh


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
        self.initial_window_pos = (0, 0)
        self.initial_scroll_offset = (0, 0)
        self.dlt_count = Melty.save_draw_state_for


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
    WINDOW = 'move'
    RESIZE_BR = 'resize_br'


class ZoomState(DictConversion):
    def __init__(self):
        super().__init__()
        self.zoom_level = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0

        self.center_u = 0.5
        self.center_v = 0.5
        self.zoom = 1.0
        self.brightness = 0.0
        self.contrast = 1.0
        self.hue = 0.0
        self.saturation = 1.0

@no_save("mouse_btn_state", "mouse_up", "mouse_down", "unique", "search_active", "content_height",
         "drag_released","clicked", "dragged", "dragged", "name", "expanded_height", "clipped", "fully_clipped", "left", "top",
         "render_time", "overhead_time", "imgui_is_toggled_open", "z_pos", "hotkey_receiver", "use_child", "cst", "search_text", "bg_color",
         "is_active", "clip_rect", "wrapped_top", "wrapped_left", "min_width", "min_height", "is_focused", "drag_window_pos_x", "drag_window_pos_y", "drag_mode", "is_hovered_last",
         "z_pos", "draw_window_pos_x", "misc_used", "draw_window_pos_y", "drag_delta", "screen_pos", "imgui_is_item_activated", "frame_count")
@exclude("render_time","overhead_time", "clip_rect", "_input_value", "flow_spacing", 'width',
         "hovered", "wrapped_top", "wrapped_left", "_did_use_cache", "content_height", "content_region", "value_hash", "drag_window", "top", "left", "content_region", "did_render",
         "bounding_hovered", "dlt_count", "header_height", "z_pos", "scrolled", "is_hovered_last", "frame_count")
@deep_refresh('scroll_offset')
class DrawState(DictConversion):
    """Holds per-widget runtime state (expand/collapse, etc.)."""

    def __init__(self):
        super().__init__()
        self._children = []
        self._parent = None
        self.misc = {}
        self.misc_used = set()
        self.closed = False
        self.frame_count = 0

        self._queued_windows = []
        self.drag_window_pos_x = None
        self.drag_window_pos_y = None
        self._bounding_hovered = False
        # Imgui state mirror
        self.is_active = False
        self.is_focused = False
        self.scroll_offset = (0, 0)
        self._imgui_is_active = False
        self._imgui_is_activated = False
        self._imgui_is_focused = False
        self._imgui_is_hovered = False
        self._imgui_is_item_hovered = False

        self._imgui_block_hovered = False
        self._imgui_is_edited = False
        self._imgui_popover_open = False
        self.imgui_is_item_activated = False
        self.clipped = True
        self.fully_clipped = True
        self.scroll_visible = False
        self._unmanaged_window = False

        self.cst = None
        self.window_pos = None
        self.window_size = None
        self._initial_window_pos = None
        self._initial_window_size = None
        self.drag_mode = DragMode.NONE
        self.use_child = False
        self.z_pos = None
        self.content_height = 0
        self.invalid_content_height = False
        self.auto_resize = True
        self._tile_id = None
        self.imgui_is_toggled_open = False

        self._previous_hash = None
        self.tint = None

        self.unique = 0  # stable UI identifier
        self.expanded = True
        self.name = ""
        self.height = 0
        self.expanded_height = None
        self.width = 0
        self.min_width = 0
        self.min_height = 0
        self.drag_window = False
        self._left_rel = None
        self._top_rel = None
        self._min_width = None
        self.top = None
        self.left = None
        self._draggable = False
        self.search_text = ""
        self.search_active = False
        self._flow_spacing = 0.0
        self.enabled = True
        self._end_header_size = (0,0)
        self._header_height = 0
        self._max_indent = 0
        self._name_edit = False
        self._screen_pos = (0, 0)
        self._did_use_cache = False
        self.track_mouse = False

        self.mouse_btn_state = {0: MouseState(),
                                1: MouseState(),
                                2: MouseState()}
        self._mouse_up = False
        self.mouse_down = False
        self.drag_released = False
        self._hovered = False
        self.hotkey_receiver = False
        self._scrolled = False
        self._clicked = False
        self.dragged = False
        self._screen_pos = (0, 0)
        self.drag_delta = (0, 0)
        self._input_value = None
        self._collection = None
        self._has_popup = False
        self.is_hovered_last = False

        self.result = None
        self.params = {}
        self.wrapped_top = 0
        self.wrapped_left = 0
        self.header_height = 0
        self.clip_rect = None
        self.dlt_count = Melty.save_draw_state_for

        # Profiling
        self.render_time = 0.0
        self.overhead_time = 0.0

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

    def get_resize_handle(self):
        if self.left is None or self.width is None or self.top is None or self.height is None:
            return (0, 0, 0, 0)
        left = self.left
        top = self.top
        right = left + self.width
        bottom = top + self.height + 2

        margin = 20
        return (right - margin, bottom - margin, right, bottom)

    def get_drag_mode(self):
        if self.auto_resize:
            return DragMode.WINDOW

        mx, my = imgui.get_mouse_pos()
        rect_br = self.get_resize_handle()
        inside_br = (rect_br[0] <= mx <= rect_br[2] and rect_br[1] <= my <= rect_br[3])

        # Bottom right
        if inside_br:
            return DragMode.RESIZE_BR

        return DragMode.WINDOW

    def is_glfw_mouse_hovering_rect(self, x1, y1, x2, y2):

        global_mouse = glfw.get_cursor_pos(Melty.glfw_window)
        mx, my = global_mouse
        # basic collision check
        if x1 <= mx <= x2 and y1 <= my <= y2:
            return True
        return False


    def get_rect(self):
        if self.left is None or self.top is None or self.width is None or self.height is None:
            return (0,0,0,0)

        width = self.width
        height = self.height

        top = self.top
        left = self.left

        if self.window_pos is not None:
            top = self.window_pos[1]
            left = self.window_pos[0]

        right = left + width
        bottom = top + height

        # do clipping
        if self.clip_rect is not None:
            left = max(left, self.clip_rect[0])
            top = max(top, self.clip_rect[1])
            right = min(right, self.clip_rect[2])
            bottom = min(bottom, self.clip_rect[3])

        is_outside = left >= right or top >= bottom
        if is_outside:
            return (0, 0, 0, 0)

        return (left, top, right, bottom)

    def draw_rect(self, rounding=0, tint=None, rect=None):
        if Melty.channels_split:
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_set_current(Melty.get_channel() + 1)

        if tint is None:
            tint = getattr(self._input_value, 'tint', None)

        draw_list = imgui.get_window_draw_list()
        if rect is None:
            rect = self.get_rect()
        draw_list.add_rect(rect[0], rect[1], rect[2], rect[3],
                           imgui.get_color_u32_rgba(*tint[:3], 1.0) if tint is not None else
                           imgui.get_color_u32_rgba(1, 1, 1, 1),
                           rounding=rounding, thickness=1.5)

        if Melty.channels_split:
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_set_current(Melty.get_channel())

    def inside_clip(self, child_draw_state=None):
        clip_rect = self.clip_rect

        if child_draw_state is None:
            child_draw_state = self

        if clip_rect is None:
            return True, False, False

        clip_left, clip_top, clip_right, clip_bottom = clip_rect

        if child_draw_state is not None:
            left = child_draw_state.left
            top = child_draw_state.top
            width = child_draw_state.width
            height = child_draw_state.height

            if top is None or left is None:
                return True, False, False

            if width is None or height is None:
                return True, False, False

            if (top + height < clip_top or top > clip_bottom):
                if top > clip_bottom:
                    return False, True, False
                else:
                    return False, False, False
        return True, False, False


    def hover_eligible(self, rect=None):
        if self.closed or not Melty.imgui_main_window_hovered:
            return False
        # if (self._imgui_is_active or self._imgui_is_edited or self._imgui_is_activated or
        #         self._imgui_is_focused or self._imgui_popover_open):
        #     return False

        # if (Melty.imgui_any_item_hovered or self._imgui_is_active or
        #         self._imgui_is_edited or self._imgui_is_activated):
        #     return False

        mouse_x, mouse_y = imgui.get_mouse_pos()
        # if Melty.imgui_any_item_hovered:
        #     return False
        if not Melty.inside_clip(rect=(mouse_x, mouse_y, 1, 1)):
            return False

        if rect is None:
            if self.top is None or self.left is None or self.width is None or self.height is None:
                return False
            rect = (self.left, self.top, self.width, self.height)
            if imgui.is_mouse_hovering_rect(rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]):
                return True
        else:
            if imgui.is_mouse_hovering_rect(rect[0], rect[1], rect[2], rect[3]):
                return True


        return False

    def on_action(self, event_names, view_id=None, priority=None, priority_delta=0, rect=None):
        if view_id is None:
            view_id = self._tile_id
        else:
            view_id = str(self._tile_id) + "_" + str(view_id)

        single_event = False
        if isinstance(event_names, str):
            event_names = [event_names]
            single_event = True

        if self.hover_eligible(rect):
            if priority is None:
                max_layer_depth = Melty.max_depth * Melty.max_layer + Melty.max_depth
                layer_and_depth = Melty.active_layer * Melty.max_depth + Melty.depth
                priority = max_layer_depth - layer_and_depth

            Melty.event_handler.register_hovered(view_id, event_names,
                                                 tile_id=self._tile_id,
                                                 priority=priority - priority_delta)

        if single_event:
            if view_id in Melty.events:
                return Melty.events.get(view_id, None).get(event_names[0], None)
            return None

        return_events = {}
        if view_id in Melty.events:
            for event_name in Melty.events[view_id]:
                return_events[event_name] = Melty.events[view_id][event_name]
        return return_events

    def is_bounding_hovered(self):
        if (self._imgui_is_active or self._imgui_is_edited or self._imgui_is_item_hovered or self._imgui_popover_open):
            return True
        mouse_x, mouse_y = imgui.get_mouse_pos()
        if not Melty.inside_clip(rect=(mouse_x, mouse_y, 1, 1)):
            return False

        if not self.auto_resize and self.window_pos is not None:
            rect = (self.window_pos[0], self.window_pos[1], self.width, self.height)
        else:
            if self.top is None or self.left is None or self.width is None or self.height is None:
                return False
            rect = (self.left, self.top, self.width, self.height + 10)

        if imgui.is_mouse_hovering_rect(rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]):
            if imgui.is_window_hovered() or Melty.imgui_popup_open:
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
