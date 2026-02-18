from enum import Enum

import glfw
import imgui
import libcst as cst

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save, exclude, deep_refresh, no_save_exclude


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

class Anchor(Enum):
    TOP_LEFT = 'top_left'
    TOP_RIGHT = 'top_right'
    BOTTOM_LEFT = 'bottom_left'
    BOTTOM_RIGHT = 'bottom_right'

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

@no_save("mouse_btn_state", "mouse_up", "mouse_down", "unique", "search_active",
        "shadow", "size_change", "drag_released","clicked", "dragged",
         "dragged", "expanded_height", "clipped", "fully_clipped",
         "overhead_time", "scroll_visible", "depth_and_layer", "imgui_is_toggled_open",
         "hotkey_receiver", "use_child", "cst", "search_text", "bg_color", "depth", "z_pos",
         "is_active", "clip_rect", "wrapped_top", "current_tint", "wrapped_left", "multi_line",
         "min_width", "min_height", "is_focused", "drag_window_pos_x", "drag_window_pos_y", "corner_radius",
         "drag_mode", "is_hovered_last", "bg_shown", "draw_window_pos_x", "z_offset",
         "misc_used", "draw_window_pos_y", "drag_delta", "screen_pos", "hover_rects", "melty_window", "auto_resize",
         "imgui_is_item_activated", "frame_count")
@exclude("current_tint", "overhead_time", "premature_break",
         "clip_rect", "_input_value", "flow_spacing", "expanded_rect", 'max_column',
         'width', "height", "size_change", 'left', 'top',
         "hovered", "wrapped_top", "params", "scroll_visible", "depth_and_layer",
         "premature_break", "wrapped_left", "_did_use_cache", "hover_rects",
         "content_region", "value_hash", "drag_window", "content_region", "did_render",
         "bounding_hovered", "dlt_count", "clip_rect",
         "header_height", "scrolled", "is_hovered_last", "frame_count")
@no_save_exclude( 'render_time', 'content_height', 'invalid_content_height', "header_height", "parent_window",
                  'hover_rects', 'nested_window', 'use_cache', 'layer', "header_top", "header_left", "left_offset", "top_offset",
                 "header_left_delta", "header_top_delta", "last_seen", "persistent", "shadow_margin", "bg_depth",
                 'channel', 'next', 'previous', 'index_in_parent', 'relative_pos', 'context_menu_open', 'context_menu_ds')
@deep_refresh('scroll_offset', "closed")
class DrawState(DictConversion):
    """Holds per-widget runtime state (expand/collapse, etc.)."""

    def __init__(self):
        super().__init__()
        self._children = {}
        self._parent = None
        self._is_header = False
        self._view_func = None
        self._kwargs = None
        self._cursor_pos = (0, 0)
        self._parent_ctx = None
        self.next = None
        self.previous = None
        self.index_in_parent = 0
        self.misc = {}
        self._clean_args = {}
        self._func = None
        self.misc_used = set()
        self.closed = False
        self.frame_count = 0
        self.corner_radius = 6
        self.nested_window = False
        self.use_cache = False
        self.depth = 0
        self.layer = 0
        self.channel = 0
        self.last_seen = None
        self.z_offset = 0
        self.shadow_margin = 0
        self.bg_depth = 0

        self.context_menu_open = False
        self.context_menu_ds = None

        self.relative_pos = None

        self._first_draw_state = None
        self.melty_window = False
        self.persistent = True
        self.selected = False

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
        self.live = False
        self._shadow_depth = 0
        self.shadow = True
        self.cst = None
        self.window_pos = None
        self.window_size = None
        self._initial_window_pos = None
        self._initial_window_size = (300, 300)
        self.drag_mode = DragMode.NONE
        self.use_child = False
        self.z_pos = 0
        self.size_change = False
        self.depth_and_layer = (0,0)
        self.content_height = 0
        self.invalid_content_height = True
        self.auto_resize = True
        self._tile_id = None
        self.imgui_is_toggled_open = False

        self._previous_hash = None
        self.tint = None
        self.current_tint = None

        self.unique = None  # stable UI ID
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
        self.left_offset = 0
        self.anchor_offset = (0,0)
        self.top_offset = 0
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
        self._scroll_child = None
        self._collection_draw_state = None
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
        self.parent_window = None
        self.result = None
        self.params = {}
        self.wrapped_top = 0
        self.wrapped_left = 0
        self.header_height = 0
        self.header_top = 0
        self.header_left = 0
        self.header_width = 0
        self.header_end_width = 0
        self.clip_rect = None
        self.dlt_count = Melty.save_draw_state_for
        self.premature_break = False
        self.header_left_delta = 0
        self.header_top_delta = 0
        self.multi_line = False
        self.footer_width = 0
        self.footer_height = 0

        self.content_width = 0
        self.content_height = 0

        # Profiling
        self.render_time = 0.0
        self.overhead_time = 0.0

        self.expanded_rect = (0,0,200,400)

        self.max_column = 1
        self._window_stack = None
        self._is_nested = False


    def mark_column(self, column):
        self.max_column = max(self.max_column, column)

    @property
    def shadow_depth(self):
        depth, active_layer = self.depth_and_layer

        divisor = max(1.0, depth - 13.0)
        depth_and_layer = active_layer * Melty.max_depth + (depth * (20.0 / (divisor)))
        depth_and_layer *= Melty.layer_inc

        return depth_and_layer

    @property
    def seen(self):
        debounce = 1
        return self.last_seen is not None and Melty.frame_count - self.last_seen < debounce

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
        width = self.width or 0
        height = self.height or 0

        top = self.top or 0
        left = self.left or 0

        right = left + width
        bottom = top + height

        is_outside = left >= right or top >= bottom
        if is_outside:
            return (0, 0, 0, 0)

        return (left, top, right - 1, bottom - 1)

    def draw_rect(self, rounding=0, tint=None, rect=None):
        if Melty.channels_split:
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_set_current(Melty.get_channel() + 1)

        if tint is None:
            tint = getattr(self._input_value, 'tint', None)

        draw_list = imgui.get_overlay_draw_list()
        draw_list.add_rect(self.left, self.top, self.left + self.width, self.top + self.height,
                           imgui.get_color_u32_rgba(*tint[:3], 1.0) if tint is not None else
                           imgui.get_color_u32_rgba(1, 1, 1, 1),
                           rounding=rounding, thickness=1)

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

        if rect is None:
            rect = (self.left or 0, (self.top or 0) - 3, (self.width or 0), (self.height or 0) + 3)
            if imgui.is_mouse_hovering_rect(rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]):
                return True
        else:
            if imgui.is_mouse_hovering_rect(rect[0], rect[1] - 3, rect[2], rect[3] + 3):
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
                                                 priority=priority - priority_delta,
                                                 tile_id=self._tile_id,
                                                 )

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
        else:
            if self.top is None or self.left is None or self.width is None or self.height is None:
                return False
            rect = (self.left, self.top - 3, self.width, self.height + 10)

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
