from collections import defaultdict

from enum import Enum

import glfw
import imgui
import libcst as cst

from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.toggles import shadow_depth_at
from src.lsd.gl_gui.utils.glfw_utils import print_stack_trace
from src.lsd.gl_gui.view.core_conversion.cache_tree import CacheTree, UNSET_VALUE
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save, exclude, deep_refresh, no_save_exclude, \
    Core, defaults


class SynthColors(DictConversion):
    def __init__(self):
        super().__init__()
        self.letter_to_color = {}


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
    TOP_CENTER = 'top_center'
    TOP_RIGHT = 'top_right'
    CENTER_LEFT = 'center_left'
    CENTER = 'center'
    CENTER_RIGHT = 'center_right'
    BOTTOM_LEFT = 'bottom_left'
    BOTTOM_CENTER = 'bottom_center'
    BOTTOM_RIGHT = 'bottom_right'


# Anchor classification used for both anchor_offset (window placement) and
# clip-rect pinning. Anything not in left/right is horizontally centered;
# anything not in top/bottom is vertically centered.
class Pin(Enum):
    """Symbolic pin targets for ``pin_to_clip`` when the caller doesn't have a
    draw_state reference handy. Resolved to the declaring view's relatives at
    render time: PARENT -> ``_parent`` (immediate render-tree parent), WINDOW ->
    ``parent_window`` (enclosing Melty window)."""
    PARENT = 'parent'
    GRANDPARENT = 'grandparent'
    WINDOW = 'window'
    # The active clip rect (scissor test) rather than a view's box. Clamped to
    # the parent window so corners track the visible edge - e.g. bottom becomes
    # min(parent_bottom, clip_bottom). Useful for overlays that must fit inside
    # a scrolled/clipped region.
    CLIP = 'clip'


LEFT_ANCHORS = (Anchor.TOP_LEFT, Anchor.CENTER_LEFT, Anchor.BOTTOM_LEFT)
RIGHT_ANCHORS = (Anchor.TOP_RIGHT, Anchor.CENTER_RIGHT, Anchor.BOTTOM_RIGHT)
TOP_ANCHORS = (Anchor.TOP_LEFT, Anchor.TOP_CENTER, Anchor.TOP_RIGHT)
BOTTOM_ANCHORS = (Anchor.BOTTOM_LEFT, Anchor.BOTTOM_CENTER, Anchor.BOTTOM_RIGHT)


class ApplyMode(Enum):
    INSTANT = 'instant'
    ON_RELEASE = 'on_release'
    CONFIRM = 'confirm'


@no_save_exclude()
class TabState(DictConversion):
    def __init__(self):
        super().__init__()
        self.selected_tabs = []


@no_save_exclude()
class DropDownState(DictConversion):
    def __init__(self):
        super().__init__()
        self.selected = None


@exclude("zoom", "center_u", "center_v", "brightness", "contrast", "hue", "saturation")
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

@exclude("_items", "_cursor")
class CursorStack:
    def __init__(self):
        self._items = []
        self._cursor = 0

    def push(self, item):
        # remove anything after cursor if you've popped, then append
        self._items = self._items[:self._cursor]
        self._items.append(item)
        self._cursor += 1

    def pop(self):
        if self._cursor == 0:
            raise IndexError("pop from empty stack")
        self._cursor -= 1
        return self._items[self._cursor]

    def peek(self):
        if self._cursor == 0:
            raise IndexError("peek at empty stack")
        return self._items[self._cursor - 1]

    @property
    def history(self):
        return list(self._items)

    def __repr__(self):
        return f"{self._items} cursor={self._cursor}"

class TileMode(Enum):
    MAX = 'max'
    MIN = 'min'
    NONE = 'none'
    NO_MASK = 'no_mask'

#
# self.text_cursor_pos = 0
# self.text_selection_start = 0
# self.text_selection_end = 0
# self.text_is_focused = False
# self.text_cursor_blink_time = 0.0
# self.text_double_click_time = 0.0
# self.text_last_click_pos = -1
# self.text_click_count = 0
# self.text_h_scroll = 0.0
# self.text_prev_cursor_pos = 0
#
# # Find-in-text search state. text_search_count/current are populated by
# # draw_text each frame and consumed by the header's find UI (count +
# # nav arrows). The underscore fields are private bookkeeping.
# self.text_search_current = 0
# self.text_search_count = 0
# self._text_search_last_term = None
# self._text_search_scroll_to = False


@no_save("mouse_btn_state", "mouse_up", "mouse_down", "unique", "search_active",
         "shadow", "size_change", "drag_released", "clicked", "dragged", "clipped_by_rect",
         "dragged", "expanded_height", "clipped", "fully_clipped",  "header_height", "inside_clip", "layer",
         "overhead_time", "scroll_visible", "depth_and_layer", "imgui_is_toggled_open", "top", "left",
         "hotkey_receiver", "use_child", "cst", "bg_color", "depth", "z_pos", "content_height",
         "is_active", "clip_rect", "wrapped_top", "current_tint", "wrapped_left", "multi_line",
         "min_width", "min_height", "is_focused", "drag_window_pos_x", "drag_window_pos_y", "corner_radius",
         "drag_mode", "is_hovered_last", "bg_shown", "draw_window_pos_x", "z_offset", "melty_window",
         "misc_used", "draw_window_pos_y", "drag_delta", "screen_pos", "hover_rects", "melty_window", "auto_resize",
         "imgui_is_item_activated", "frame_count", "text_search_current", "text_search_count")
@exclude("current_tint", "overhead_time", "premature_break", "drag_mode",
         "clip_rect", "_input_value", "flow_spacing", "expanded_rect", 'max_column', 'text_selection_start', 'text_selection_end',
         'width', "height", "size_change", 'left', 'top', "clipped", "fully_clipped", "melty_window", "text_double_click_time", "text_cursor_blink_time",
         "hovered", "wrapped_top", "params", "scroll_visible", "depth_and_layer", "clipped_by_rect", "multi_line", "text_prev_cursor_pos",
         "text_cursor_pos", "text_h_scroll", "text_selection_start", "text_selection_end", "text_is_focused", "text_cursor_blink_time",
         "premature_break", "wrapped_left", "_did_use_cache", "hover_rects", "window_pos", "content_width",
         "content_region", "value_hash", "drag_window", "content_region", "did_render", "footer_height", "footer_width",
         "bounding_hovered", "dlt_count", "clip_rect",
 "scrolled", "is_hovered_last", "frame_count", "z_pos", "corner_radius",
         "text_search_current", "text_search_count")
@no_save_exclude('render_time',  "total_z_offset", 'closable', 'has_full_tile', 'invalid_content_height',
                  "parent_window", "pressed", "bbox", "", "child_selected",
                 'hover_rects', 'nested_window', 'use_cache', "header_top", "header_left", "left_offset",
                 "top_offset", 'kwargs', "just_shadow", "header_width", "header_end_width",
                 "header_natural_width", "max_header_width", "pin_to_clip", "pin_clip_rect", "pin_clamp",
                 "header_left_delta", "header_top_delta", "last_seen", "persistent", "shadow_margin", "bg_depth",
                 "anchor_pos", "parent_anchor_pos", "pin_to_clip", "pin_clip_rect", "just_shadow", 'hover_reported', 'explain_convert',
                 'channel', 'next', 'previous', 'index_in_parent',
                    '_hover_eligible', 'just_shadow')
@deep_refresh( 'closed', 'search_text', 'search_active')
class DrawState(DictConversion):
    """Holds per-widget runtime state (expand/collapse, etc.)."""

    def __init__(self):
        super().__init__()
        self._external_change = False
        self._input_value_cache = UNSET_VALUE
        self._output_value_cache = UNSET_VALUE
        self._file_meta = UNSET_VALUE
        self._loading = False
        self._pending = False
        self._running = False
        self.watch = ""
        self._default_view_func = None

        # Chain cache, split based on type, UNSET_VALUE as a default
        self._chain_stack = CacheTree()

        self._children = {}
        self._view_children = {}
        self._wrapper = None
        self._parent_ctx = None
        self._bg_depth = 0
        self._current_tint = None
        self._bg_stack = None
        self._parent = self
        self._is_header = False
        self._view_func = None
        self._cursor_pos = (0, 0)
        self._cursor_screen_pos = (0, 0)
        self._source = defaultdict(dict)
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
        self.has_full_tile = False
        self.behind = False
        self.tile_mode = TileMode.MAX


        self.context_menu_open = False
        self.context_menu_ds = None
        self.context_menu_offset = 0
        self._offset_ds = None
        # Set on a context menu's target draw_state to reopen it after a deferred
        # per-view screenshot lands (see screenshot.process_take_screenshot_flags).
        self._reopen = False
        # Resolved call site (filename, line) of where this widget was invoked,
        # computed ONCE in the inline render pass of first frame its context menu
        # is opened (see core_render) and cached until app restart. We cache the
        # resolved tuple - NOT the raw frames - so the inspect-arg lens / jump
        # button read a stable value; re-walking the live stack each frame would
        # flip mid-drag (the "skip parents" optimization changes the call stack).
        # Underscore-prefixed → not serialized.
        self._call_site = None
        # The whole filtered caller chain (innermost-first list of (filename,
        # lineno)), captured alongside _call_site; _call_site is just its head.
        # draw_context_menu creates one code_file_io per entry to edit the full
        # stack of draw_x(...) calls that built this view.
        self._call_stack = []
        self._call_site_captured = False
        # Set by the context menu when it walks UP to this draw state via the
        # up-arrow offset; this view never had its own menu open, so the normal
        # context_menu_open capture gate never fired. The flag causes the next
        # inline render will capture the call site anyway (lazy, one-shot).
        self._call_site_requested = False

        # Per-frame caches for the position/clip chain. abs_left/abs_top/abs_clip_rect/
        # pin_rect/clip_anchor_base are pure functions of the draw_state tree but were
        # recomputed many times per frame (the left property is hit by parent walks,
        # pin clamping, clip checks, and BVH lookups), with each computation re-walking
        # the parent chain. With pin_clamp the recursion compounded - ~600K _abs_left
        # calls per ~25 draw_tint_context frames in practice. Cache by frame_count;
        # the recursive internal walks all go through the cached property so an
        # ancestor's value is computed at most once per frame.
        # abs_left/abs_top are keyed by (frame_count, left_offset, top_offset,
        # window_pos) - NOT frame alone. The wrapper sets left_offset/top_offset
        # mid-frame (core_render.py:393, again in the columns branch :1235) to
        # position the widget at the current imgui cursor; a frame-only key
        # cached the first set_offset value and the column re-set never landed,
        # which is what broke column rendering with the earlier cache.
        self._abs_left_key = None
        self._abs_left_cache = 0
        self._abs_top_key = None
        self._abs_top_cache = 0
        self._abs_content_height_cache = 0
        self._abs_content_height_key = None
        # pin_rect/clip_anchor_base keys widen over frame_count too: the wrapper
        # writes pin_target/pin_clip_rect/pin_clamp/parent_anchor_pos mid-frame
        # (core_render.py:995–1041), AFTER earlier hover/clip code may have already
        # cached a pre-anchor value. Frame-only caching made nested-window anchors
        # take a frame to settle.
        self._pin_rect_key = None
        self._pin_rect_cache = None
        self._clip_anchor_base_key = None
        self._clip_anchor_base_cache = None

        self.relative_pos = None

        self._first_draw_state = None
        self.melty_window = False
        self.persistent = True
        self.child_selected = None
        self.just_shadow = False

        self._queued_windows = []
        self.drag_window_pos_x = None
        self.drag_window_pos_y = None
        self._bounding_hovered = False
        # Imgui state mirror
        self.is_active = False
        self.is_focused = False
        self.scroll_offset = (0, 0)
        # Authoritative max scroll_offset.y, published by core_render's scroll
        # handler each frame. Descendants (text editor drag-auto-scroll) read it
        # so their clamp matches exactly & the two don't fight (bottom flicker).
        self._max_scroll_y = None
        self._imgui_is_active = False
        self._imgui_is_activated = False
        self._imgui_is_focused = False
        self._imgui_is_hovered = False
        self._imgui_is_item_hovered = False

        self._imgui_block_hovered = False
        self._imgui_is_edited = False
        self._imgui_popover_open = False
        self.imgui_is_item_activated = False
        self.inside_clip = True
        self.fully_clipped = True

        self._one_full_draw = False
        self.scroll_visible = False
        self._unmanaged_window = False
        self.live = False
        self._shadow_depth = 0
        self.shadow = True
        self.cst = None
        self.window_pos = (0,0)
        self.window_size = None
        self._initial_window_pos = None
        self._initial_window_pos_resize = None
        self._initial_window_size = (300, 300)

        self.drag_mode = DragMode.NONE
        self.use_child = False
        self.z_pos = 0
        self.depth_and_layer = (0, 0)
        self.content_height = 0
        self.invalid_content_height = True
        self.auto_resize = True
        self._tile_id = None
        self.imgui_is_toggled_open = False

        self._previous_hash = None
        self.tint = (0.485, 0.61, 0.76)
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
        self._search_was_active = False
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
        self._bypass_cache = False

        self._input_cache = {"external_state": (UNSET_VALUE, 0, -1),  # value, frame, hash
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
        # Natural (pre-pad) width of this window's own header.
        self.header_natural_width = 0
        # Multi-window header widths: max_header_width is the stable value
        # read while padding headers this frame. _max_header_width_acc is the
        # running max accumulated as children render, committed at window push.
        self.max_header_width = 0
        self.clip_rect = None
        # Parent window's abs_pos when clip_rect was captured. abs_clip_rect
        # shifts the (absolute, not-based) clip_rect by the window's movement
        # since then, so the clip tracks a window drag without a re-render.
        self._clip_win_anchor = None
        self.dlt_count = Core.melty.save_draw_state_for
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
        self.final_max_column = 0
        self._current_max_column = 0
        # Per-divider pixel nudges applied left-to-right on top of the basic
        # content_width/#columns split. offsets[c-1] shifts interior boundary
        # c; values are never clamped (see column_boundary in core_render).
        self.column_offsets = []
        self._column_cursor = defaultdict(lambda: [0, 0])
        self._melty_cursor = (0,0)
        self._melty_content_height = 0

        self._outside_column_height = 0
        self._inner_cursor = 0 # column -> (x, y)
        self._columns_top = None
        self._max_column_height = 0
        self._max_column_index = 0
        self._columns_bottom = 0
        ### End Columns
        self._is_nested = False
        self.anchor_pos = Anchor.TOP_LEFT
        # Which point on the parent the child anchors to. Independent of
        # anchor_pos (the child's own origin). Defaults to TOP_LEFT so parent
        # anchoring is opt-in and legacy top-left layout is preserved.
        self.parent_anchor_pos = Anchor.TOP_LEFT
        # Floating views pin to a clip - resolved live from this Pin enum
        # (PARENT / GRANDPARENT / WINDOW / CLIP) off .parent/parent_window at
        # compute time, so the float tracks the target as it scrolls/moves
        # without snapshotting anything. None = not pinned. Truthy when pinned.
        # See pin_rect / _pin_rect.
        self.pin_to_clip = None
        self._kwargs = {}
        self.kwargs = AttrDict({})
        self.hover_reported = True
        self._start_z_pos = 3
        self._column_width = 0
        self._front_layer = False

        # Used to cache abs_clip_rect
        self.clipped_by_rect = None

        self._show_load = False
        self._show_save = False
        self._read_only = False
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
        self._observed_content_height = 0
        self._last_expanded = None
        self.expanded_rect = (0, 0, 0, 0)
        self._collapsed_rect = (0, 0, 0, 0)
        self._load_pending = False
        self._save_pending = None

        self.explain_convert = None

        # Text input state
        self.text_cursor_pos = 0
        self.text_selection_start = 0
        self.text_selection_end = 0
        self.text_is_focused = False
        self.text_cursor_blink_time = 0.0
        self.text_double_click_time = 0.0
        self.text_last_click_pos = -1
        self.text_click_count = 0
        self.text_h_scroll = 0.0
        self.text_prev_cursor_pos = 0
        self._name_color = (1,1,1)
        # Drag granularity latched on mouse-down ('char' | 'word' | 'line') plus
        # the anchor pos of the anchor word/line, so a double/triple-click drag
        # extends by whole words/lines and keeps the anchor selected (IntelliJ
        # style) instead of collapsing to a single caret.
        self.text_drag_mode = 'char'
        self.text_drag_anchor_lo = 0
        self.text_drag_anchor_hi = 0

        # Find-in-text search state. text_search_count/current are populated by
        # draw_text each frame and consumed by the header's find UI (count +
        # nav arrows). The underscore fields are private bookkeeping.
        self.text_search_current = 0
        self.text_search_count = 0
        self._text_search_last_term = None
        self._text_search_scroll_to = False

        # Cross-view aggregation (search owner side). _search_session is a
        # SearchTerm pushed onto Melty.search_stack each frame; the owner's
        # pre-body search_walk pushes its term here and writes the combined
        # result count into text_search_count for the find UI.
        self._search_session = None
        self._search_last_term = None
        self._search_nav_pending = False
        # Temporary state: set when search is opened (Ctrl+F) so the find box grabs
        # text focus on the activating frame even if the underlying searchable
        # view (the owner) currently holds melty text focus. Consumed (cleared)
        # by render_search after the box requests focus, so it won't keep
        # yanking focus away from a later deliberate click into the editor.
        self._search_focus_pending = False
        # Set by the owner's search_walk to the local index of the global-current
        # match when it lands in THIS view (else None). The view reads it while
        # drawing to highlight/scroll to that match - so the count and the
        # selection come from one source and can't disagree.
        self._search_active_local = None
        # draw_collection's copy of the key holding the global-current match.
        self._search_current_key = None
        # Closure set each render: (term, session) => claims this view's own
        # matches into the session from imgui. Driven by melty.search_walk.
        self._search_matcher = None

        self._print_last_invalid = False
        self._last_invalidate = None
        self._hover_eligible = 0

        # BVH spatial index
        self._bvh_id = None
        self._bvh_bbox = None  # cached bbox for delete operations


        # File watch state (replaces _WatchState for convert_in/convert_out)
        self._address = None
        self._file_mtime = 0.0
        self._file_size = 0
        self._original_load_data = None
        self._original_input_ref = None

        self._stack_trace = None
        self._nested_index = 0

        self.selected = False
        self._cache = None
        self._return_value = None

    def invalidate_up(self, max_depth=4):
        Core.melty.cache.invalidate_up(self._tile_id, max_depth=max_depth)

    def invalidate(self):
        Core.melty.cache.invalidate(self._tile_id)

    @property
    def cursor_screen_pos(self):
        cursor_x = self.abs_left + self._melty_cursor[0]
        cursor_y = self.abs_top + self._melty_cursor[1]
        return cursor_x, cursor_y

    @property
    def clip_size(self):
        # if self.clip_rect is None:
        #     return (self.width or self.min_width, self.height or self.min_height)
        left, top, right, bottom = self.abs_clip_rect
        return (right - left, bottom - top)

    @property
    def bbox(self):
        l, t, w, h = self.abs_left, self.abs_top, self.width, self.height
        if l is None or t is None or not w or not h:
            return None
        return (l, t, l + w, t + h)

    def is_file_stale(self):
        if self._address is None:
            return False
        try:
            s = self._address.path.stat()
            return s.st_mtime != self._file_mtime or s.st_size != self._file_size
        except OSError:
            return True

    def mark_file_current(self):
        if self._address is None:
            return
        try:
            s = self._address.path.stat()
            self._file_mtime = s.st_mtime
            self._file_size = s.st_size
        except OSError:
            pass

    def pos_changed(self):
        """Update BVH index after position/size changes. Call from render_func.

        register/update/unregister own `_bvh_bbox` (it must mirror what's in the
        rtree for deletes to match), so don't touch it here."""
        new_bbox = self.bbox
        if new_bbox == self._bvh_bbox:
            return

        if self._bvh_id is None:
            if new_bbox is not None and self.inside_clip:
                Core.melty.bvh_register(self)
        elif not self.inside_clip:
            Core.melty.bvh_unregister(self)
        else:
            Core.melty.bvh_update(self)

    # @property
    # def inner_cursor(self):
    #     if self._columns_top is None:
    #         self._columns_top = imgui.get_cursor_pos()[1]
    #
    #     return self._columns_top - self.abs_top

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
    def abs_clipped_height(self):
        clip_rect = (self._parent.abs_left,
                     self._parent.abs_top,
                     self._parent.abs_left + self._parent.width,
                     self._parent.abs_top + self._parent.height)

        clipped_bottom = min(clip_rect[3], self.abs_top + self.height)
        clipped_top = max(clip_rect[1], self.abs_top)
        clipped_height = clipped_bottom - clipped_top
        return max(0, clipped_height)

    @property
    def abs_content_height(self):

        content_height = 0
        f = Core.melty.frame_count
        key = (f, self.height, self._parent.abs_clip_rect, self._observed_content_height)
        is_scroll_view = not self._kwargs.get("disable_scroll", True)
        if self._abs_content_height_key == key:
            return self._abs_content_height_cache
        for child in self._view_children.values():
            if child._kwargs.get("column", 0) != 0:
                continue

            if child.closable:
                continue
            if is_scroll_view:
                clipped_bottom = child.abs_top + child.height
                clipped_top = child.abs_top
            else:
                clip_rect = self._parent.abs_clip_rect
                clipped_bottom = min(clip_rect[3], child.abs_top + child.height)
                clipped_top = max(clip_rect[1], child.abs_top)

            clipped_height = clipped_bottom - clipped_top
            content_height += max(0, clipped_height)

        # Special case where view wants to scroll but has no children for which to determine content height
        if "determines_height" in self._kwargs:
            content_height = self._content_rect[1]
        self._abs_content_height_cache = content_height

        # if is_scroll_view:
        #     content_height = max(content_height, self.height)

        return int(content_height)

    @property
    def abs_layer(self):

        if self.parent_window is not None and self.closable:
            return self.parent_window.abs_layer + self._kwargs.get("layer_offset", 4)
        elif self.parent_window is not None:
            return self.parent_window.abs_layer
        else:
            return self.layer

    @property
    def abs_closed(self):

        if self.closed and self.closable:
            return True

        if not self.expanded:
            return True

        if self.parent_window is not None:
            return self.parent_window.abs_closed

        return False

    @property
    def root_window(self):
        if self.parent_window is None:
            return self
        else:
            return self.parent_window.root_window

    @property
    def anchor_offset(self):
        anchor_margin = 2

        if self.anchor_pos in LEFT_ANCHORS:
            offset_x = 0
        elif self.anchor_pos in RIGHT_ANCHORS:
            offset_x = -self.width
        else:  # horizontally centered
            offset_x = int(-self.width / 2)

        if self.anchor_pos in TOP_ANCHORS:
            offset_y = 0
        elif self.anchor_pos in BOTTOM_ANCHORS:
            offset_y = -self.height + -anchor_margin
        else:  # vertically centered
            offset_y = int(-self.height / 2)

        return (offset_x, offset_y)

    @property
    def parent_anchor_offset(self):
        """Offset from the parent window's top-left to the anchor point on the
        parent selected by ``anchor_pos``.

        This is the parent-side counterpart to ``anchor_offset`` (which selects
        the child's own corner). The two are independent: ``parent_anchor_pos``
        picks the point on the parent, ``anchor_pos`` picks the child's corner
        that lands on it. Returns (0, 0) for a TOP_LEFT parent anchor (the
        default), preserving the legacy top-left-relative layout.
        """
        parent = self.parent_window
        if parent is None or parent is self:
            return (0, 0)

        if self.parent_anchor_pos in LEFT_ANCHORS:
            offset_x = 0
        elif self.parent_anchor_pos in RIGHT_ANCHORS:
            offset_x = parent.width
        else:  # horizontally centered
            offset_x = int(parent.width / 2)

        if self.parent_anchor_pos in TOP_ANCHORS:
            offset_y = 0
        elif self.parent_anchor_pos in BOTTOM_ANCHORS:
            offset_y = parent.height
        else:  # vertically centered
            offset_y = int(parent.height / 2)

        return (offset_x, offset_y)

    @property
    def _pin_target(self):
        """The view this pins to, resolved live from the Pin mode off
        _parent/parent_window at call time, so the float tracks it as it
        scrolls/moves. None when not pinned or the relative is missing.

        Pinning only ever targets an ancestor (parent / grandparent / window),
        and ancestors never depend on a descendant's position — so reading the
        target's abs_clip_rect from here can't cycle, and needs no cache."""
        mode = self.pin_to_clip
        if mode is Pin.WINDOW:
            t = self.parent_window
        elif mode is Pin.GRANDPARENT:
            p = self._parent
            t = p._parent if (p is not None and p is not self) else None
        elif mode is Pin.PARENT or mode is Pin.CLIP:
            t = self._parent
        else:
            return None
        return t if (t is not None and t is not self) else None

    @property
    def pin_rect(self):
        """Reference rect ``(left, top, right, bottom)`` the pin anchors to, in
        absolute coords — the live clip rect of the resolved relative
        (``_pin_target``). None when there's nothing to pin to.

        For ``Pin.CLIP`` the rect is intersected with the parent window's box so
        each corner clamps to the visible edge (e.g. bottom = min(parent_bottom,
        clip_bottom)) instead of running past it.

        The target's box is read from its LIVE abs (``_abs_left``/``_abs_top``),
        not its per-frame-cached ``abs_left``/``abs_top``. A pinned float needs
        the target's current position on frames the target itself isn't
        re-rendering — e.g. while its window is dragged: the window moves (its
        own abs cache invalidates on window_pos), but the target's cache key
        doesn't change, so cached ``target.abs_left`` would return last frame's
        anchor. Computing live re-reads the moved window through the parent
        chain, so the anchor tracks the drag instead of lagging a frame."""
        target = self._pin_target
        if target is None:
            return None
        # Anchor to the target's RAW box (live abs), so the float tracks the
        # target's actual position. PARENT/WINDOW/GRANDPARENT do NOT clamp here:
        # intersecting with the target's clip rect makes the anchor corner snap
        # to the clip edge whenever the target is clipped, so the window tracks
        # the *clip* instead of the target (the "only moves when the clamp moves
        # it" bug). Clamping is Pin.CLIP's job - see below.
        tl, tt = target._abs_left(), target._abs_top()
        rect = (tl, tt, tl + target.width, tt + target.height)
        if self.pin_to_clip is Pin.CLIP:
            # Pin.CLIP clamps the float to the parent window's visible box so
            # corners track the visible edge. Use the window's LIVE box
            # (win._abs_left()/_abs_top()), not its cached abs_left or the
            # target's captured clip_rect: both are fixed in screen space and so
            # are stale when the window itself is dragged, which left Pin.CLIP
            # floats clamped to the old position. The live window box is correct
            # in both cases - it's constant while content scrolls and moves with
            # a window window.
            win = self.parent_window
            if win is None and len(Core.melty.melty_windows) > 0:
                win = Core.melty.melty_windows[-1]
            if win is not None and win is not self:
                wl, wt = win._abs_left(), win._abs_top()
                c = (wl, wt, wl + win.width, wt + win.height)
                rect = (max(rect[0], c[0]), max(rect[1], c[1]),
                        min(rect[2], c[2]), min(rect[3], c[3]))
        return rect

    @property
    def clip_anchor_base(self):
        """Anchor reference point on the pinned rect (see ``pin_rect``).

        Picks the corner of the pin rect selected by ``parent_anchor_pos`` — the
        point *on the target* the window attaches to. ``anchor_offset`` (driven
        by ``anchor_pos``) then shifts the window so its own corner lands there,
        so the two are independent: e.g. parent_anchor=BOTTOM_RIGHT /
        anchor=TOP_RIGHT hangs the window off the target's bottom-right corner.
        With both at the TOP_LEFT default the window's top-left sits on the
        target's top-left. Computed live (pin_rect already is), so it tracks the
        target regardless of how it scrolls.
        """
        clip = self.pin_rect
        if clip is None:
            return None
        clip_left, clip_top, clip_right, clip_bottom = clip

        if self.parent_anchor_pos in LEFT_ANCHORS:
            base_x = clip_left
        elif self.parent_anchor_pos in RIGHT_ANCHORS:
            base_x = clip_right
        else:  # horizontally centered
            base_x = (clip_left + clip_right) / 2

        if self.parent_anchor_pos in TOP_ANCHORS:
            base_y = clip_top
        elif self.parent_anchor_pos in BOTTOM_ANCHORS:
            base_y = clip_bottom
        else:  # vertically centered
            base_y = (clip_top + clip_bottom) / 2

        return (base_x, base_y)

    def _ancestor_scroll(self):
        """Sum the scroll_offsets of intermediate ancestors between self and
        ``parent_window`` (exclusive at both ends) by walking the ``_parent``
        chain. Used so abs_left/abs_top respond to ancestor scroll deltas
        without waiting for the descendant to re-render — left_offset is
        stored as the *unscrolled* content position and this delta subtracted
        on demand. Returns (sx, sy)."""
        sx = sy = 0
        node = self._parent
        stop = self.parent_window
        # Cycle guard mirroring the abs_left guard: bail if we revisit self
        # or walk longer than the deepest realistic chain.
        for _ in range(64):
            if node is None or node is self or node is stop:
                break
            so = node.scroll_offset
            if so is not None:
                ny = so[1]
                # Enforce the scroll bound here, at the source, so writers (a
                # text editor's drag-auto-scroll, pans) don't each have to clamp.
                # node._max_scroll_y is the authoritative max core_render
                # published this frame. Clamp any contribution AND write it back
                # so the stored offset can't run past the content ends - that
                # runaway is what made auto-scroll overshoot EOF and lag on the
                # way back. None = the node hasn't published a max yet.
                mx = node._max_scroll_y
                if mx is not None:
                    if ny < 0:
                        ny = 0.0
                    elif ny > mx:
                        ny = mx
                    if ny != so[1]:
                        node.scroll_offset = (so[0], ny)
                sx += so[0]
                sy += ny
            nxt = node._parent
            if nxt is node:
                break
            node = nxt
        return sx, sy

    def _abs_left(self):
        # Recursive parent walks go through the cached `parent_window.abs_left`
        # property - once an ancestor's value is cached for the current key, the
        # walk short-circuits, reducing what was O(depth * widgets) per frame
        # back to O(widgets). The if `depth > 10` print_stack_trace guard is no
        # longer needed: abs_left marks its key BEFORE recursing, so any cycle
        # returns the cached (last-set) value instead of recursing forever.
        parent_left = 0
        if self.parent_window is not None and self.parent_window is not self:
            parent_left = self.parent_window.abs_left

        window_pos_x = self.window_pos[0] if self.window_pos is not None else 0
        anchor = self.anchor_offset

        base = self.clip_anchor_base if self.pin_to_clip else None
        if base is not None:
            # Pin to the clip rect corner rather than the scrolled position of
            # the declaring view (which left_offset tracks).
            this_left = window_pos_x + base[0] + anchor[0]
            if (self.pin_to_clip is Pin.CLIP and self.width is not None
                    and self.parent_window is not None and self.parent_window is not self):
                # Box-level clamp: the rect clamp only repositions the anchor
                # point, so if anchor_offset shifts the box its far edge can
                # still hang off the window. Pull the box back so it stays
                # inside the window horizontally (favoring the left edge if the
                # box is wider than the window).
                win = self.parent_window
                win_left = win.abs_left
                this_left = min(this_left, win_left + win.width - self.width)
                this_left = max(this_left, win_left)
        else:
            # left_offset is stored as the *unscrolled* position in parent_window's
            # content (capture sites add the ancestor scroll). Subtracting the
            # live ancestor scroll here makes abs_left immediately reflect any
            # mid-frame scroll change on an ancestor - what previously had to
            # wait for the descendant to re-render with a new cursor pos.
            sx, _ = self._ancestor_scroll()
            this_left = (window_pos_x + parent_left + self.left_offset - sx
                         + self.parent_anchor_offset[0] + anchor[0])
        return int(this_left)

    def _abs_top(self):
        parent_top = 0
        if self.parent_window is not None and self.parent_window is not self:
            parent_top = self.parent_window.abs_top

        anchor = self.anchor_offset
        window_pos_y = self.window_pos[1] if self.window_pos is not None else 0

        base = self.clip_anchor_base if self.pin_to_clip else None
        if base is not None:
            # Pin to the clip rect corner rather than the scrolled position of
            # the declaring view (which top_offset tracks).
            this_top = window_pos_y + base[1] + anchor[1]
            if (self.pin_to_clip is Pin.CLIP and self.height is not None
                    and self.parent_window is not None and self.parent_window is not self):
                # Box-level clamp: see _abs_left. Keeps the bottom edge from
                # hanging below the window (favoring the top edge if the box
                # is taller than the window).
                win = self.parent_window
                win_top = win.abs_top
                this_top = min(this_top, win_top + win.height - self.height)
                this_top = max(this_top, win_top)
        else:
            # See _abs_left for why this subtracts the live ancestor scroll.
            _, sy = self._ancestor_scroll()
            this_top = (window_pos_y + parent_top + self.top_offset - sy
                        + self.parent_anchor_offset[1] + anchor[1])
        return int(this_top)

    @property
    def abs_clip_rect(self):
        abs_left = self.abs_left
        abs_top = self.abs_top
        box_right = abs_left + self.width
        box_bottom = abs_top + self.height
        clip = self.clip_rect
        if clip is None:
            return (abs_left, abs_top, box_right, box_bottom)

        # clip_rect is stored in absolute screen coords, so it's correct while
        # content scrolls (the clip region is fixed in screen space) but stale
        # when the parent window moves. Shift it by however far the parent window
        # has moved since capture - zero during scroll, the mouse delta during a
        # window drag - so the clamp tracks the window without a re-render. The
        # clip region moves rigidly with the window, so a uniform shift is exact.
        pw = self.parent_window
        anchor = self._clip_win_anchor
        if pw is not None and pw is not self and anchor is not None:
            dx = pw._abs_left() - anchor[0]
            dy = pw._abs_top() - anchor[1]
            clip = (clip[0] + dx, clip[1] + dy, clip[2] + dx, clip[3] + dy)

        # Intersect the LIVE box with the (shifted) clip rect. Both move with the
        # window now, so the clamped edges stay pinned to the unclamped corner.
        return (int(max(abs_left, clip[0])),
                int(max(abs_top, clip[1])),
                int(min(box_right, clip[2])),
                int(min(box_bottom, clip[3])))

    @property
    def abs_clip_rect_local(self):
        abs_left = self.abs_left
        abs_top = self.abs_top
        clipped_by = self.clipped_by_rect
        if clipped_by is None:
            clip =  (abs_left, abs_top, abs_left + self.width, abs_top + self.height)
        else:
            clip = (abs_left + clipped_by[0],
                    abs_top + clipped_by[1],
                    abs_left + self.width - clipped_by[2],
                    abs_top + self.height - clipped_by[3])
        box_right = abs_left + self.width
        box_bottom = abs_top + self.height
        # clip_rect is captured in absolute screen coords, so it's correct while
        # content scrolls (the clip region is fixed in screen space) but stale
        # when the whole window moves. Shift it by however far the parent window
        # has moved since capture - zero during scroll, the drag delta during a
        # window drag - so the clamp tracks the window without a re-render. The
        # clip region moves rigidly with the window, so a uniform shift is exact.
        pw = self.parent_window
        anchor = self._clip_win_anchor
        if pw is not None and pw is not self and anchor is not None:
            dx = pw._abs_left() - anchor[0]
            dy = pw._abs_top() - anchor[1]
            clip = (clip[0] + dx, clip[1] + dy, clip[2] + dx, clip[3] + dy)

        # Intersect the LIVE box with the (shifted) clip rect. Both move with the
        # window now, so the clamped edges stay locked to the unclamped corner.
        return (int(max(abs_left, clip[0])),
                int(max(abs_top, clip[1])),
                int(min(box_right, clip[2])),
                int(min(box_bottom, clip[3])))



    def children_in_clip(self, clip=None, max_depth=1):
        """Descendants whose vertical span overlaps `clip` (default: this view's
        abs_clip_rect), top-to-bottom.

        `max_depth` bounds the recursion: 1 (default) returns only direct
        children; higher values also descend into them, including any descendant
        that overlaps `clip`, down at most `max_depth` levels. Pass
        `max_depth=None` for the whole subtree. The same `clip` is carried down
        — positions are absolute (screen space), so a grandchild is kept iff it
        overlaps the original viewport, not its immediate parent's box.

        Results are pre-order (each child immediately followed by its own
        in-clip subtree) and deduped by identity across the whole walk."""
        if clip is None:
            clip = self.abs_clip_rect
        if clip is None:
            return []

        direct = self._direct_children_in_clip(clip)
        if max_depth is not None and max_depth <= 1:
            return direct

        next_depth = None if max_depth is None else max_depth - 1
        seen = set()
        result = []
        for c in direct:
            if id(c) not in seen:
                seen.add(id(c))
                result.append(c)
            for gc in c.children_in_clip(clip, next_depth):
                if id(gc) not in seen:
                    seen.add(id(gc))
                    result.append(gc)
        return result

    def _direct_children_in_clip(self, clip):
        """Direct children overlapping `clip`, ordered top-to-bottom by live
        abs_top.

        Children self-register into _view_children (core_render, where _parent
        is set) keyed by id. abs_left/abs_top are read live, so this reflects
        the current scroll immediately — unlike a BVH clip query, whose boxes
        only catch up when each child re-renders, so it would miss exactly the
        children that just scrolled in.

        A plain 1-D overlap filter, NOT a binary search: rows can have different
        heights, so ordering by abs_top does not order by bottom edge
        (abs_top + height), and a binary search keyed on either edge skips a
        band of visible rows. We filter (so dict iteration order is irrelevant),
        then sort the survivors top-to-bottom. A child can appear under more
        than one key, and one that has since re-rendered elsewhere leaves a
        stale entry behind — so we dedupe by identity and drop any whose _parent
        is no longer this view."""
        children = self._view_children
        if not children:
            return []
        clip_top, clip_bottom = clip[1], clip[3]

        seen = set()
        result = []
        for c in children.values():
            if c is None or c is self or c._parent is not self:
                continue
            if id(c) in seen:
                continue
            seen.add(id(c))
            top = c.abs_top
            if top < clip_bottom and top + (c.height or 0) > clip_top:
                result.append(c)
        result.sort(key=lambda c: c.abs_top)
        return result

    def descendants(self, max_depth=None):
        """Every descendant draw_state via _view_children, pre-order and deduped
        by identity, bounded by `max_depth` (None = whole subtree).

        The un-clipped sibling of children_in_clip: no viewport filter, so it
        returns rows that have scrolled off-screen too — as long as they
        rendered at least once and are still parented here. Used to walk the
        whole tree without drawing, e.g. to recount search matches by invoking
        each view's _search_matcher (melty.search_walk), instead of force-
        rendering off-screen rows just so they re-register their counts."""
        seen = set()
        result = []

        def _walk(node, depth):
            if depth is not None and depth <= 0:
                return
            children = node._view_children
            if not children:
                return
            for c in list(children.values()):
                if c is None or c is node or c._parent is not node or id(c) in seen:
                    continue
                seen.add(id(c))
                result.append(c)
                _walk(c, None if depth is None else depth - 1)

        _walk(self, max_depth)
        return result

    @property
    def size_change(self):
        if self._tile_id is None or self.width is None or self.height is None:
            return True
        #
        # if (not imgui.is_mouse_down(0)
        #         and not imgui.is_mouse_down(1)
        #         and not imgui.is_mouse_down(2)):
        #     return True

        cache = Core.melty.cache
        if cache is None:
            return False
        t = cache._tiles.get(self._tile_id)
        if t is None:
            return False
        return t.size != (int(self.width), int(self.height))

    @property
    def tile_fully_filled(self) -> bool:
        """Debug helper: True when this view's blit tile has had every pixel
        written from the main framebuffer at least once. Used by the filled-
        tile overlay (Toggles.show_filled_tiles)."""
        if self._tile_id is None:
            return False
        cache = Core.melty.cache
        if cache is None:
            return False
        return cache._tile_fully_filled(cache._tiles.get(self._tile_id))

    @property
    def abs_left(self):
        # Pinned floats resolve their target (parent/grandparent/window) live
        # from the tree each call so they track it as it scrolls - compute live,
        # # no cache. The target's own abs_left is cached, so the walk stays cheap.
        # if self.pin_to_clip:
        #     return self._abs_left()
        # The key covers everything the wrapper writes per-draw_state mid-frame
        # that abs_left's value depends on: left_offset / window_pos (the
        # columns branch and the wrapper re-set these), and anchor_pos /
        # parent_anchor_pos. _ancestor_scroll is in the key so mid-frame scroll
        # deltas to an ancestor invalidate the cache (the non-pinned path
        # subtracts it in _abs_left). Parent-side changes propagate via the
        # cached parent.abs_left.
        f = Core.melty.frame_count
        ancestor_sx, _ = self._ancestor_scroll()
        key = (f, self.left_offset, self.window_pos,
               self.anchor_pos, self.parent_anchor_pos, self.width, ancestor_sx)
        if self._abs_left_key == key:
            return self._abs_left_cache
        self._abs_left_key = key
        val = self._abs_left()
        self._abs_left_cache = val
        return val

    @property
    def abs_top(self):
        # if self.pin_to_clip:
        #     return self._abs_top()
        f = Core.melty.frame_count
        _, ancestor_sy = self._ancestor_scroll()
        key = (f, self.top_offset, self.window_pos,
               self.anchor_pos, self.parent_anchor_pos, self.height, ancestor_sy)
        if self._abs_top_key == key:
            return self._abs_top_cache
        self._abs_top_key = key
        val = self._abs_top()
        self._abs_top_cache = val
        return val

    def mark_column(self, column):
        self.max_column = max(self.max_column, column)

    def shadow_depth_at(self, depth, active_layer):
        divisor = max(0.5, depth - 20.0)
        depth_and_layer = active_layer * Core.melty.max_depth + (depth * (20.0 / (divisor)))
        depth_and_layer *= Core.melty.layer_inc
        return depth_and_layer

    @property
    def shadow_depth(self):
        depth, active_layer = self.depth_and_layer
        return shadow_depth_at(depth, active_layer)


    @property
    def seen(self):
        debounce = 1
        return self.last_seen is not None and Core.melty.frame_count - self.last_seen < debounce

    @property
    def window_index(self):
        nested_offset = self._nested_index if self.parent_window is not None else 0
        return self.abs_layer + nested_offset

        # if self.closable:
        #     layer_index = min(Melty.max_layer - 1, self.abs_layer + self._nested_index - 2)
        # elif self.parent_window is not None:
        #     layer_index = min(Melty.max_layer - 1, self.abs_layer + self.parent_window._nested_index)
        # else:
        #     layer_index = min(Melty.max_layer - 1, self.abs_layer)

        # return layer_index

    def init_cst_state(self, node, module_id: str):
        self.cst = None
        self.cst.path_key = Core.melty.current_path()
        self.cst.module_id = module_id
        self.cst.root_gen = Core.melty.current_gen(module_id)
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

        global_mouse = glfw.get_cursor_pos(Core.melty.glfw_window)
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
        if Core.melty.channels_split:
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_set_current(Core.melty.get_channel() + 1)

        if tint is None:
            tint = getattr(self._input_value, 'tint', None)

        draw_list = imgui.get_overlay_draw_list()
        draw_list.add_rect(self.left, self.top, self.left + self.width, self.top + self.height,
                           imgui.get_color_u32_rgba(*tint[:3], 1.0) if tint is not None else
                           imgui.get_color_u32_rgba(1, 1, 1, 1),
                           rounding=rounding, thickness=1)

        if Core.melty.channels_split:
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_set_current(Core.melty.get_channel())

    def is_inside_clip(self, child_draw_state=None):
        clip_rect = self.abs_clip_rect

        if child_draw_state is None:
            child_draw_state = self

        if clip_rect is None:
            return True, False, False

        clip_left, clip_top, clip_right, clip_bottom = clip_rect

        if child_draw_state is not None:
            left = child_draw_state.abs_left
            top = child_draw_state.abs_top
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

    @property
    def abs_clamped_rect(self):
        clip_rect = self.abs_clip_rect
        if clip_rect is None:
            return (self.abs_left, self.abs_top, self.abs_left + self.width, self.abs_top + self.height)

        clip_left, clip_top, clip_right, clip_bottom = clip_rect

        left = max(self.abs_left, clip_left)
        top = max(self.abs_top, clip_top)
        right = min(self.abs_left + self.width, clip_right)
        bottom = min(self.abs_top + self.height, clip_bottom)

        return (left, top, right, bottom)

    def hover_eligible(self, rect=None, ignore_reports=True):
        if self.just_shadow:
            return False

        if self.closed or not Core.melty.imgui_main_window_hovered:
            return False

        if not self.inside_clip:
            return False

        if rect is None:
            # Lean on the BVH: begin_frame already point-tested each view against
            # the cursor (Melty.bvh_hover_ids), O(1) membership replaces
            # imgui.is_mouse_hovering_rect on this view's own bbox.
            if id(self) not in Core.melty.bvh_hover_ids:
                return False
            # The BVH stores raw bboxes, not clipped ones, so still check the
            # cursor inside the active clip (scrolled-away views aren't hovered).
            clip_rect = self.abs_clamped_rect
            if clip_rect is not None:
                mx, my = imgui.get_mouse_pos()
                if not (clip_rect[0] <= mx <= clip_rect[2] and clip_rect[1] <= my <= clip_rect[3]):
                    return False
        else:
            # Custom sub-region: clip it and point-test the cursor directly.
            clip_rect = (self.abs_left, self.abs_top, self.abs_left + self.width, self.abs_top + self.height)
            if clip_rect is not None:
                rect = (max(rect[0], clip_rect[0]), max(rect[1], clip_rect[1]),
                        min(rect[2], clip_rect[2]), min(rect[3], clip_rect[3]))
            mx, my = imgui.get_mouse_pos()
            if not (rect[0] <= mx <= rect[2] and rect[1] <= my <= rect[3]):
                return False

        if not (self.hover_reported is None or self.hover_reported or ignore_reports):
            return False
        return True

    @property
    def priority(self):
        max_layer_depth = (Core.melty.max_depth *
                           Core.melty.max_layer + Core.melty.max_depth)
        layer_and_depth = (Core.melty.active_layer *
                           Core.melty.max_depth + Core.melty.depth)
        return max_layer_depth - layer_and_depth

    def get_content_rect(self):
        return (self.abs_left, self.abs_top + self.header_height, self.abs_left + self.width, self.abs_top + self.height - self.footer_height)

    def get_header_rect(self):
        return ( self.abs_left, self.abs_top, self.abs_left + self.width, self.abs_top + self.header_height)

    # Transient per-draw_state UI state that should travel with a value undo, so
    # undoing a text edit also restores the caret/selection/scroll to where they
    # were before the edit. Snapshotting is generic (just attribute names): a
    # widget that wants more state restored on undo adds its field names, with no
    # changes to core_render or the renderer itself.
    UNDO_STATE_FIELDS = (
        "text_cursor_pos", "text_prev_cursor_pos",
        "text_selection_start", "text_selection_end", "text_h_scroll",
    )

    def capture_undo_state(self):
        """Snapshot the undo-tracked transient fields as a name->value dict."""
        return {f: getattr(self, f) for f in self.UNDO_STATE_FIELDS}

    def apply_undo_state(self, snapshot):
        """Restore a snapshot produced by capture_undo_state()."""
        if not snapshot:
            return
        for f, v in snapshot.items():
            setattr(self, f, v)

    def on_action(self, event_names, view_id=None, priority=None, priority_delta=0, rect=None):
        if not Core.melty.inside_clip(draw_state=self):
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
                max_layer_depth = Core.melty.max_depth * Core.melty.max_depth + Core.melty.max_depth
                layer_and_depth = Core.melty.active_layer * Core.melty.max_depth + Core.melty.depth
                priority = max_layer_depth - layer_and_depth

            Core.melty.event_handler.register_hovered(view_id, event_names,
                                                      priority=priority - priority_delta,
                                                      tile_id=self._tile_id,
                                                      )

        if single_event:
            if view_id in Core.melty.events:
                return Core.melty.events.get(view_id, None).get(event_names[0], None)
            return None

        return_events = {}
        if view_id in Core.melty.events:
            for event_name in Core.melty.events[view_id]:
                return_events[event_name] = Core.melty.events[view_id][event_name]
        return return_events

    def is_bounding_hovered(self):
        if (self._imgui_is_active or self._imgui_is_edited or self._imgui_is_item_hovered or self._imgui_popover_open):
            return True

        if self.top is None or self.left is None or self.width is None or self.height is None:
            return False

        mouse_x, mouse_y = imgui.get_mouse_pos()
        if not Core.melty.inside_clip(rect=(mouse_x, mouse_y, 1, 1)):
            return False

        if not (Core.melty.imgui_main_window_hovered or Core.melty.imgui_popup_open):
            return False

        # Cursor-over-this-view comes from the BVH hit test, not is_mouse_hovering_rect.
        return id(self) in Core.melty.bvh_hover_ids


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
