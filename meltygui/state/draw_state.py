from collections import defaultdict
from copy import copy
from dataclasses import dataclass
from enum import Enum

import glfw
import imgui
import libcst as cst

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save, exclude, deep_refresh, no_save_exclude, \
    invalidate_all


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


class ApplyMode(Enum):
    INSTANT = 'instant'
    ON_RELEASE = 'on_release'
    CONFIRM = 'confirm'

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


class AttrDict:
    __slots__ = ('_data',)

    def __init__(self, data):
        object.__setattr__(self, '_data', data)

    def __getattr__(self, name):
        return self._data.get(name, None)


    def __setattr__(self, name, value):
        self._data[name] = value

    def rebind(self, data):
        object.__setattr__(self, '_data', data)

UNSET_VALUE = object()

class TileMode(Enum):
    MAX = 'max'
    MIN = 'min'
    NONE = 'none'
    NO_MASK = 'no_mask'

@no_save("mouse_btn_state", "mouse_up", "mouse_down", "unique", "search_active",
         "shadow", "size_change", "drag_released", "clicked", "dragged",
         "dragged", "expanded_height", "clipped", "fully_clipped",
         "overhead_time", "scroll_visible", "depth_and_layer", "imgui_is_toggled_open",
         "hotkey_receiver", "use_child", "cst", "search_text", "bg_color", "depth", "z_pos",
         "is_active", "clip_rect", "wrapped_top", "current_tint", "wrapped_left", "multi_line",
         "min_width", "min_height", "is_focused", "drag_window_pos_x", "drag_window_pos_y", "corner_radius",
         "drag_mode", "is_hovered_last", "bg_shown", "draw_window_pos_x", "z_offset", "content_width",
         "misc_used", "draw_window_pos_y", "drag_delta", "screen_pos", "hover_rects", "melty_window", "auto_resize",
         "imgui_is_item_activated", "frame_count")
@exclude("current_tint", "overhead_time", "premature_break",
         "clip_rect", "_input_value", "flow_spacing", "expanded_rect", 'max_column',
         'width', "height", "size_change", 'left', 'top', 'content_height',
         "hovered", "wrapped_top", "params", "scroll_visible", "depth_and_layer",
         "premature_break", "wrapped_left", "_did_use_cache", "hover_rects",
         "content_region", "value_hash", "drag_window", "content_region", "did_render",
         "bounding_hovered", "dlt_count", "clip_rect",
         "header_height", "scrolled", "is_hovered_last", "frame_count")
@no_save_exclude('render_time',  "total_z_offset", 'closable', 'invalid_content_height',
                 "header_height", "parent_window", "pressed",
                 'hover_rects', 'nested_window', 'use_cache', 'layer', "header_top", "header_left", "left_offset",
                 "top_offset", 'kwargs', "just_shadow",
                 "header_left_delta", "header_top_delta", "last_seen", "persistent", "shadow_margin", "bg_depth",
                 "anchor_pos", "just_shadow", 'hover_reported', 'explain_convert',
                 'channel', 'next', 'previous', 'index_in_parent', 'relative_pos', 'context_menu_open',
                 'context_menu_ds', '_hover_eligible', 'just_shadow')
@deep_refresh('scroll_offset', 'closed')
class DrawState(DictConversion):
    """Holds per-widget runtime state (expand/collapse, etc.)."""

    def __init__(self):
        super().__init__()
        self._children = {}
        self._wrapper = None
        self._parent_ctx = None
        self._bg_depth = 0
        self._current_tint = None
        self._bg_stack = None
        self._parent = None
        self._is_header = False
        self._view_func = None
        self._cursor_pos = (0, 0)
        self._height_source = "Not set"
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

        self.total_z_offset = 0
        self.shadow_margin = 0
        self.bg_depth = 0
        self.pressed = False
        self.closable = False
        self.behind = False
        self.tile_mode = TileMode.MAX


        self.context_menu_open = False
        self.context_menu_ds = None

        self.relative_pos = None

        self._first_draw_state = None
        self.melty_window = False
        self.persistent = True
        self.selected = False
        self.just_shadow = False

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
        self.depth_and_layer = (0, 0)
        self.content_height = 0
        self.invalid_content_height = True
        self.auto_resize = True
        self._tile_id = None
        self.imgui_is_toggled_open = False

        self._previous_hash = None
        self.tint = (0,0,0)
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
        self.top_offset = 0
        self._draggable = False
        self.search_text = ""
        self.search_active = False
        self._flow_spacing = 0.0
        self.enabled = True
        self._end_header_size = (0, 0)
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
        self._input_value = UNSET_VALUE
        self._raw_input_value = UNSET_VALUE

        self._input_value_cache = {"external_state": (UNSET_VALUE, 0, -1),  # value, frame, hash
                                   "internal_state":(UNSET_VALUE, 0)}  # value, frame

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

        ### Columns
        self.final_max_column = 1
        self._current_max_column = 1
        self._column_cursor = defaultdict(lambda: [0, 0])  # column -> (x, y)
        self._max_column_height = 0
        ### End Columns
        self._is_nested = False
        self.anchor_pos = Anchor.TOP_LEFT
        self._kwargs = {}
        self.kwargs = AttrDict({})
        self.hover_reported = True
        self._start_z_pos = 3
        self._column_width = 0

        self._show_load = False
        self._show_save = False
        self._internal_pending = None
        self._apply_load = None

        self._save_pending = None
        self._apply_save = None
        self._pending_convert = False
        self._save_pending_for = 0
        self._save_pending_obj = None
        self._all_pending = {"save_pending":None, "load_pending":None}
        self._load_pending_for = 0
        self._content_rect = (100,30)
        self._last_expanded = None
        self.expanded_rect = (0, 0, 0, 0)
        self._collapsed_rect = (0, 0, 0, 0)
        self._hover_eligible_cache = {}  # path, frame
        self._tile_params = {}
        self._load_pending = False
        self._save_pending = False

        self.explain_convert = None

    def tile_params(self):
        self._tile_params['clip_rect'] = copy(self.clip_rect)

        return self._tile_params

    #
    # def __getattr__(self, name):
    #     if name.startswith("kw_"):
    #         key = name[2:]  # strip "arg_" prefix
    #         try:
    #             return self._kwargs[key]
    #         except KeyError:
    #             print(f"Warning: Attempted to access missing argument '{key}' in DrawState.")
    #     raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")
    #
    # def __setattr__(self, name, value):
    #     if name.startswith("kw_"):
    #         self._args[name[3:]] = value
    #     else:
    #         super().__setattr__(name, value)


    @property
    def abs_closed(self):
        if self.closed and self.closable:
            return True
        elif self.parent_window is not None:
            return self.parent_window.abs_closed
        else:
            return False

    @property
    def root_window(self):
        if self.parent_window is None:
            return self
        else:
            return self.parent_window.root_window

    @property
    def anchor_offset(self):
        anchor_margin = 3
        offset = (0, 0)

        if self.anchor_pos == Anchor.TOP_LEFT:
            offset = (0, 0)
        elif self.anchor_pos == Anchor.TOP_RIGHT:
            offset = (-self.width, 0)
        elif self.anchor_pos == Anchor.BOTTOM_LEFT:
            offset = (0, -self.height + -anchor_margin)
        elif self.anchor_pos == Anchor.BOTTOM_RIGHT:
            offset = (-self.width, -self.height + -anchor_margin)
        return offset

    def _abs_left(self):
        parent_left = 0
        if self.parent_window is not None:
            parent_left = self.parent_window._abs_left()
        elif not self.closable:
            parent_left = imgui.get_cursor_screen_pos()[0]

        window_pos_x = self.window_pos[0] if self.window_pos is not None else 0
        # this_left_offset = self.left_offset if not self.melty_window else window_pos_x
        anchor = self.anchor_offset

        this_left = window_pos_x + parent_left + self.left_offset + anchor[0]
        return this_left

    def _abs_top(self):
        parent_top = 0
        if self.parent_window is not None:
            parent_top = self.parent_window._abs_top()
        elif not self.closable:
            parent_top = imgui.get_cursor_screen_pos()[1]

        anchor = self.anchor_offset
        window_pos_y = self.window_pos[1] if self.window_pos is not None else 0
        this_top = window_pos_y + parent_top + self.top_offset + anchor[1]
        return this_top

    @property
    def abs_left(self):
        return self._abs_left()

    @property
    def abs_top(self):
        return self._abs_top()

    def mark_column(self, column):
        self.max_column = max(self.max_column, column)

    def shadow_depth_at(self, depth, active_layer):
        divisor = max(1.0, depth - 13.0)
        depth_and_layer = active_layer * Melty.max_depth + (depth * (20.0 / (divisor)))
        depth_and_layer *= Melty.layer_inc
        return depth_and_layer

    @property
    def shadow_depth(self):
        depth, active_layer = self.depth_and_layer
        return self.shadow_depth_at(depth, active_layer)

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

    def hover_eligible(self, rect=None, ignore_reports=True):
        if self.just_shadow:
            return False
        if rect is None:
            left = self.left if self.left is not None else 0
            top = self.top if self.top is not None else 0
            width = self.width if self.width is not None else 0
            height = self.height if self.height is not None else 0
            rect = (left, top - 3, left + width, top + height + 3)
        if self.closed or not Melty.imgui_main_window_hovered:
            return False

        if not self.clipped:
            return False

        clip_rect = Melty.get_clip_rect()
        if clip_rect is not None:
            left = rect[0]
            top = rect[1]
            right = rect[2]
            bottom = rect[3]

            rect = (max(left, clip_rect[0]), max(top, clip_rect[1]),
                    min(right, clip_rect[2]), min(bottom, clip_rect[3]))


        cached = self._hover_eligible_cache.get(rect, None)
        if cached is None or cached[1] < Melty.frame_count:
            this_frame = Melty.frame_count
            if imgui.is_mouse_hovering_rect(rect[0], rect[1], rect[2], rect[3]):
                if self.hover_reported is None or self.hover_reported or ignore_reports:
                    self._hover_eligible_cache[rect] = (True, this_frame)
                    return True
                else:
                    self._hover_eligible_cache[rect] = (False, this_frame)
                    return False

            self._hover_eligible_cache[rect] = (False, this_frame)
            return False

        return cached[0]

    @property
    def priority(self):
        max_layer_depth = Melty.max_depth * Melty.max_layer + Melty.max_depth
        layer_and_depth = Melty.active_layer * Melty.max_depth + Melty.depth
        return max_layer_depth - layer_and_depth

    def on_action(self, event_names, view_id=None, priority=None, priority_delta=0, rect=None):
        if not Melty.inside_clip(draw_state=self):
            return None
        if self.just_shadow:
            return None
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
