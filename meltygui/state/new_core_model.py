from collections import defaultdict

from enum import Enum

import meltygui.core.windowing.window_api as glfw
import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.runtime.toggles import shadow_depth_at
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing.glfw_utils import print_stack_trace
from meltygui.core.conversion.cache_tree import CacheTree
from meltygui.core.conversion.cache_tree import UNSET_VALUE
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.rendering.core_decoration import exclude
from meltygui.core.rendering.core_decoration import deep_refresh
from meltygui.core.rendering.core_decoration import no_save_exclude
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.core_decoration import defaults


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


class ExpandMode(Enum):
    """How the render_func wrapper treats a collapsed (is_tree) view.

    AUTO   - wrapper owns collapse: the view func is skipped while collapsed
             and the box shrinks to its header (default, legacy behavior).
    MANUAL - the view func is ALWAYS called and reads draw_state.expanded to
             choose its own collapsed rendering (e.g. draw_comment showing
             just the first line). The wrapper skips its collapsed-state
             sizing shortcuts so whatever the func draws is measured normally.
    """
    AUTO = 'auto'
    MANUAL = 'manual'


@no_save_exclude()
class TabState(DictConversion):
    def __init__(self):
        super().__init__()
        self.selected_tabs = []
        self.tab_tints = {}
        self.tab_icons = {}


class ColorPickerState(DictConversion):
    """draw_color_picker's persisted state (injected via
    `picker_state: ColorPickerState = None`): which tab is showing —
    "wide" (the P3 + brightness square, the default), "srgb" (the
    classic square) or "extended" (the classic square with the P3
    extension to its right, the sRGB+ tab)."""

    def __init__(self):
        super().__init__()
        self.tab = "wide"




@no_save("fit_phase")
class ContextMenuWindowState(DictConversion):
    """Per-menu persisted state for the context menu WINDOW, draw_context_menu
    (injected via `menu_state: ContextMenuWindowState = None` — the TabState
    pattern). Not the input tab's host cache, new_core_view.ContextMenuState."""

    def __init__(self):
        super().__init__()
        # True once the menu's first-load auto-fit has sized the window.
        # PERSISTED on purpose: the menu draw_state (so its width/height)
        # survives a restart, and a menu restored open must not be re-fitted -
        # it overwrote the user's size with the default width + a content
        # fit (an underscore draw_state attr never serialized, 08-27).
        self.fit_done = False
        # The fit's two-frame sequence (0: stamp header width, 1: measure
        # height). Session-only - a fit never spans a restart.
        self.fit_phase = 0


@exclude("restore_first_line", "restore_total_lines", "restore_text",
         "restore_gutter_digits", "restore_line_offset", "restore_fold_keys",
         "restore_gutter_rows", "restore_diff_collapsed", "restore_diff_rows",
         "restore_preview_rows")
class TextEditorState(DictConversion):
    """Per-editor persisted UI state for draw_text (injected via
    `text_editor_state: TextEditorState = None` — the TabState pattern:
    created into draw_state.misc and serialized with it)."""

    def __init__(self):
        super().__init__()
        self._completion_explicit = False
        self._token_matches = None
        self._completion_buffer = None
        self._completion_anchor = None
        self._signature_dismissed = None
        # {def_name: bool} - whether that function's parameter window is
        # visible. The def widget reads/writes this bool DIRECTLY each
        # render (visibility IS this bool); persisted, so a fresh session
        # re-opens the panels that were open.
        self.params_windows_open = {}
        # {def_name: True} - whether that function's param panel re-runs
        # Run Visualize automatically on every param edit. The panel's
        # Auto Execute checkbox reads/writes this directly (the
        # params_windows_open pattern); persisted with the editor state.
        self.params_auto_execute = {}
        # Width the Ctrl+B usage picker was drag-resized to, or None while
        # it still fits its content; its height always fits the rows
        # (usage_picker.py).
        self.usage_picker_width = None
        # Viewability snapshot for the instant-restore placeholder: what this
        # editor SHOWED last frame - the visible band's first (display)
        # line, its text, and the buffer's total line count. draw_text
        # writes these at the end of every real draw and, when called with
        # input_value=None (buffer still loading, e.g. draw_text_editor's
        # loading branch), rebuilds a same-shape stand-in from them: blank
        # lines up to the band, the band's text, blank lines below - same
        # line count → same content height → the persisted scroll stays
        # unmoved, and the first frame of a session shows the code the last
        # one did. @exclude: pure passive state (all renders stamp
        # them mid-session), so their per-scroll writes must not invalidate.
        self.restore_first_line = 0
        self.restore_total_lines = 0
        self.restore_text = ""
        # Gutter shape for the stand-in: digit count (0 = no gutter - the
        # width decides where the code column starts, so a missing gutter
        # made the text shift left↔right on the swap-in) and the sequential
        # numbering offset (the number on buffer line 0 minus 1).
        self.restore_gutter_digits = 0
        self.restore_line_offset = 0
        # Per-display-row gutter of the snapshot band, exactly as painted -
        # ints are line NUMBERS (fold-remapped, so they skip across
        # collapsed folds), -1/-2 denote a fold-header chevron row
        # (expanded/collapsed), None a numberless row. Without this the
        # stand-in numbered rows sequentially from restore_line_offset -
        # wrong below every collapsed fold, and chevron-less - so the
        # buffer visibly snapped when the real text landed.
        self.restore_gutter_rows = None
        # This editor's collapsed folds as their LINE-INDEPENDENT keys
        # (ds._fold_keys - scope qualnames / header texts, designed to
        # survive folds). ds._fold_keys is an underscore slot and not
        # serialized, so fold state used to RESET to default_collapsed
        # every session. draw_text seeds the fresh draw seed from this
        # instead. None = never captured (let defaults seed); [] = captured
        # with everything expanded (defaults must NOT re-collapse).
        self.restore_fold_keys = None
        # The compare split's hand-toggled DIFF gap state (ds
        # ._diff_fold_collapsed - (header, last hidden line) buffer-line
        # tuples; no line-independent keys exist for gaps; they re-derive
        # from the live diff). Written by the snapshot block whenever the
        # diff layer is active; a fresh draw_state in NEUTRAL mode
        # (expand_diff None - the active switch is its own truth) maps
        # them onto the current pieces by overlap, exactly like an edit's
        # drift. None = never captured.
        self.restore_diff_collapsed = None
        # The band's diff-gap header rows, as painted: {band row offset:
        # hidden line count} - 0 for an expanded gap's header, N for a
        # collapsed one (its "N lines" label). The diff layer sits stand-in
        # frames out (it needs the real buffer), so without this the
        # stand-in painted a collapsed compare split as CONTINUOUS code -
        # grey mid-row chevrons, no separator bands, no counts, no preview
        # fade - and the collapsed look only arrived with the real text,
        # seconds after boot (Lukas 09-01: "loads uncollapsed then 2
        # seconds later makes the switch"). The stand-in paints its bands
        # and labels from these rows instead. None = never captured.
        self.restore_diff_rows = None
        # The row offsets that were preview-faded (the rows around a
        # collapsed diff gap, Toggles.TextEditor.diff_preview_lines_*), so
        # the stand-in fades the same rows. None = never captured.
        self.restore_preview_rows = None


@no_save_exclude("selected", "open_path", "cursor_path", "search_query", "search", "_focus_search",)
class DropDownState(DictConversion):
    def __init__(self):
        super().__init__()
        self.selected = None
        # The single chain of branch labels currently expanded, e.g. ("color",
        # "rgb"). One path only - guarantees at most one sub-menu open per level.
        self.open_path = ()
        # Full key-path of the keyboard/hover-highlighted row (includes the leaf),
        # e.g. ("color", "rgb", "red"). Drives arrow-key navigation + the
        # highlight; open_path is derived from it (branch -> itself, leaf -> parent).
        self.cursor_path = ()
        # Live search box state (our own, not the framework search). search_query
        # is the raw text the root menu's draw_text edits; search is its lowercased
        # form, propagated to each level for filtering. _focus_search asks the box
        # to grab text focus once (set when the menu opens); _search_box_tile is
        # the box's tile id so key-nav can tell when the box holds focus.
        self.search_query = ""
        self.search = ""
        self._focus_search = 0
        self._search_box_tile = None
        # True once the search box has held text focus this open. Lets us tell a
        # brand-new open (still acquiring focus) from focus genuinely leaving the
        # dropdown; the latter closes us, keeping text focus and open in lock-step.
        self._had_focus = False
        # The committed selection's key-path + text label (the trigger shows the
        # label, e.g. "red", not the raw value). _picked_path is stamped by the row
        # click / Enter so the path survives the close that resets cursor_path.
        self.selected_path = ()
        self.selected_label = ""
        self._picked_path = ()
        # Keyboard-select mode: once an arrow key is pressed, we keep moving the
        # highlight until the mouse moves. _last_mouse detects that movement.
        self._kbd_mode = False
        self._last_mouse = None
        # The popover's (width, height) when the user drag-resized it -
        # persisted, re-applied on every later open; None = size to content
        # (draw_dropdown's fit). _menu_ds is the popover's draw_state and
        # _menu_fit the last size draw_dropdown stamped on it: a size that
        # differs from _menu_fit is the wrapper's resize handle at work.
        self.menu_size = None
        self._menu_ds = None
        self._menu_fit = None


class ContextMenuItemsState(DropDownState):
    """The `context_menu={label: callable}` popover's dropdown state — the
    root_state of the draw_dd_menu it opens — one per view carrying a menu,
    kept in that view's draw_state.misc like an injected state
    (new_core_view.draw_context_menu_items). Adds where the menu opened."""

    def __init__(self):
        super().__init__()
        # The right-click's position relative to the view's top-left: the
        # popover's window_pos, re-applied every frame so it stays put.
        self.open_at = (0, 0)


@no_save("open_title", "_prev_text_focus", "_hovered_title")
class MenuBarState(DictConversion):
    """draw_menu_bar's injected state (view/core_views/menu_bar.py): which
    title's menu is showing and one DropDownState per title, so every menu
    keeps its own open/cursor paths and its drag-resized menu_size — the
    same per-popover state draw_dropdown keeps, one per menu."""

    def __init__(self):
        super().__init__()
        # The title (top-level key of the bar's dict) whose menu is open;
        # None while the bar is idle. Not saved: an open menu means nothing
        # next session.
        self.open_title = None
        # title -> DropDownState. menu_size saved per menu.
        self.menus = {}
        # The last selection's full key-path, ("File", "Recent", "a.py").
        self.selected_path = ()
        # Who held text focus when a menu opened, handed focus back on close
        # so a click out of the Edit menu leaves the editor typing again.
        self._prev_text_focus = None
        # The title under the pointer at the last run (hover-switching edge-cases).
        self._hovered_title = None


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
        # `_data` itself must raise, not route through here: during deepcopy/
        # pickle reconstruction the slot is briefly unset, and `self._data`
        # would re-enter __getattr__('_data') -> infinite recursion. Dunders
        # raise AttributeError so copy/pickle/unwrap protocol probes (__setstate__,
        # __deepcopy__, ...) read as exceptions instead of a None that some
        # protocols might try to call.
        if name == "_data" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return self._data.get(name, None)


    def __setattr__(self, name, value):
        # The `_data` slot is set directly; everything else is a data key.
        # Without this, copy/pickle reconstruction (which setattrs `_data` back
        # onto a fresh, slot-unset instance) would route into `self._data[...]`
        # and hit the unset slot -> AttributeError. Pairs with __getattr__'s
        # `_data` guard to make AttrDict round-trip through copy/pickle.
        if name == "_data":
            object.__setattr__(self, name, value)
            return
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


# Search state persists across reloads: search_text, search_active, and
# text_search_current are saved so the find box reopens with the last query and
# selected match. text_search_count is derived (recomputed each frame) and stays
# transient below.
@no_save("mouse_btn_state", "mouse_up", "mouse_down", "unique",
         "shadow", "size_change", "drag_released", "clicked", "dragged", "clipped_by_rect",
         "dragged", "expanded_height", "clipped", "fully_clipped", "inside_clip",
         "overhead_time", "scroll_visible", "imgui_is_toggled_open", "top", "left",
         "hotkey_receiver", "use_child", "cst", "bg_color", "depth", "return_item",
         "is_active", "clip_rect", "wrapped_top", "current_tint", "wrapped_left", "multi_line", "relative_pos", "footer_width", "footer_height",
         "min_width", "min_height", "max_height", "is_focused", "drag_window_pos_x", "drag_window_pos_y",
         "drag_mode", "is_hovered_last", "bg_shown", "draw_window_pos_x", "z_offset", "melty_window",
         "misc_used", "draw_window_pos_y", "drag_delta", "screen_pos", "hover_rects", "melty_window", "auto_resize",
         "imgui_is_item_activated", "frame_count", "text_search_count",
         # A context menu open at quit came back open at the next boot,
         # drawn under the view that spawned it (09-13, melty_code_editor).
         "context_menu_open")
@exclude("current_tint", "overhead_time", "premature_break", "drag_mode",
         "clip_rect", "_input_value", "flow_spacing", "expanded_rect", 'max_column', 'text_selection_start', 'text_selection_end',
         'width', "size_change", 'left', 'top', "clipped", "fully_clipped", "melty_window", "text_double_click_time", "text_cursor_blink_time",
         "hovered", "wrapped_top", "params", "scroll_visible", "depth_and_layer", "clipped_by_rect", "multi_line", "text_prev_cursor_pos",
         "text_cursor_pos", "text_h_scroll", "text_selection_start", "text_selection_end", "text_is_focused", "text_cursor_blink_time",
         "premature_break", "wrapped_left", "_did_use_cache", "hover_rects", "window_pos", "content_width", "code_state",
         "content_region", "value_hash", "drag_window", "content_region", "did_render", "footer_height", "footer_width",
         "bounding_hovered", "dlt_count", "clip_rect",
 "scrolled", "is_hovered_last", "frame_count", "z_pos",
         "text_search_current", "text_search_count", "observed_content_height",
         # The context menu's Eval tab: typed per keystroke and must not
         # re-render the inspected view (the eval fires on Run / Enter).
         "eval_code")
@no_save_exclude('_window_visibility_initialized', 'render_time',  "total_z_offset", 'closable', 'has_full_tile', 'invalid_content_height',
                  "parent_window", "pressed", "bbox", "", "child_selected", "bg_color",
                 'hover_rects', 'nested_window', 'use_cache', "header_top", "header_left", "left_offset",
                 "top_offset", 'kwargs', "just_shadow",
                 "header_natural_width", "max_header_width", "pin_to_clip", "pin_clip_rect", "pin_clamp",
                 "header_left_delta", "header_top_delta", "last_seen", "persistent", "shadow_margin", "bg_depth",
                 "anchor_pos", "parent_anchor_pos", "pin_to_clip", "pin_clip_rect", "just_shadow", 'hover_reported', 'explain_convert',
                 'channel', 'next', 'previous', 'index_in_parent',
                    '_hover_eligible', 'just_shadow')
@deep_refresh( 'closed', 'search_text', 'search_active')
class DrawState(DictConversion):
    """Holds per-widget runtime state (expand/collapse, etc.)."""

    # _ancestor_scroll cache, keyed on (frame_count, Melty.scroll_version).
    # Class-level defaults so pre-existing/deserialized instances resolve them
    # without __init__; written via object.__setattr__ (never accessed or
    # serialized; to_dict only walks the default instance's fields).
    _anc_scroll_key = None
    _anc_scroll_cache = (0, 0)

    # Hot reload fallback: live instances predating the persisted rename read
    # this, the wrapper's post-body stamp writes the instance attr.
    observed_content_height = 0
    # Same fallback for the persisted Eval snippet (context menu's Eval-tab).
    eval_code = None

    def __init__(self):
        super().__init__()
        self._external_change = False
        # The context menu's Eval-tab snippet for THIS view, persisted so the
        # tab reopens with what was last typed. Stored on the inspected view's
        # draw_state (not the tab's) because the eval fires in this view's
        # render wrapper and a scope-up event retargets the tab at an ancestor.
        self.eval_code = None
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
        self._cursor_start_pos = None

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
        self._window_visibility_initialized = False
        self.frame_count = 0
        # corner_radius is no longer a declared field - it migrated to the
        # auto state system (see core_render's auto-state block): views that
        # tune it declare a named `corner_radius` param (e.g. button), the
        # default bg path stamps it from there, and background painters read
        # it via getattr(ds, 'corner_radius', 6).
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
        self.bg_color = (0, 0, 0, 0)
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
        # The whole UNfiltered call stack (innermost-first list of (filename,
        # lineno, func_name)), captured alongside _call_site. draw_context_menu
        # renders every frame, filtering per-frame: a real call site shows an
        # editable code_file_io, a machinery/ignored frame just a label.
        self._call_stack = []
        # Queue-time call stack for a DEFERRED layer (a view drawn at end of frame).
        # At end-of-frame dispatch the live stack is just the layer-loop machinery,
        # so a view rendered inside a deferred layer has a _call_stack that bottoms
        # out there. The original caller chain that QUEUED the layer is captured
        # preemptively at queue-time (core_render, deferred pass) into this field;
        # draw_context_menu appends it to complete a descendant's stack.
        # _is_deferred_layer marks a queued view; _deferred_stack_requested is the
        # lazy capture gate, set by a descendant's open menu and consumed next frame.
        self._deferred_call_stack = []
        # The same queue-time stack with LOCALS - (path, lineno, func_name,
        # locals) outermost first, the debug tab's format (_call_stack_frames)
        # - so a descendant's stack trace can be spliced onto the chain that
        # queued this layer and rendered as one logical call. None until captured.
        self._deferred_call_stack_frames = None
        self._is_deferred_layer = False
        self._deferred_stack_requested = False
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
        self._abs_top_true_key = None

        self._abs_top_cache = 0
        self._abs_top_true_cache = 0
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
        self._event_names = set()
        # Body-level on_action registrations from the last real render, replayed
        # by the wrapper for blit-cache hits: (frame_count, [entries]). See
        # on_action / replay_body_actions.
        self._body_actions = None
        # Child OS-window requests made from this view's body (the wrapper's
        # glfw_window=True branch) - replayed on a blit-cache hit like
        # _body_actions, see replay_surface_requests.
        self._body_surface_requests = None
        # Rect-scoped event PARAMS from the last real render (event_rect):
        # {param_name: [rect relative to (abs_left, abs_top), ...]}. The
        # wrapper consumes and clears it before each body run.
        self._event_rects = None

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
        self.shadow = False
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
        # True while this view is holding a placeholder height from the
        # core_render.content_height fast path (never fully rendered yet). Cleared
        # the frame it renders for real.
        self._skipped_render = False
        self.auto_resize = True
        self._tile_id = None
        self.imgui_is_toggled_open = False

        self._previous_hash = None
        self.tint = (0.11, 0.12, 0.14)
        self.current_tint = None

        self.unique = None  # stable UI ID
        self.expanded = True
        self.name = ""
        self.height = 0
        self.expanded_height = None
        self.width = 0
        self.min_width = 0
        self.min_height = 0
        # The wrapper's max_height kwarg when the caller passes
        # enforce_max_height=True, mirrored like min_height so the resize
        # handlers (corner drag, the frame-edge solve) cap a closable window's
        # height in realtime; None = uncapped (a bare max_height only bounds
        # what the wrapper sizes - the resize handle stays free from it).
        self.max_height = None
        self.drag_window = False
        self._left_rel = None
        self._top_rel = None
        self._min_width = None
        self.top = None
        self.left = None
        self.left_offset = 0
        self.top_offset = 0

        self.left_offset_true = 0
        self.top_offset_true = 0


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
        # Auto-state params (see the auto-state block in core_render.py).
        # auto_params holds only DIVERGED values - what view code wrote via
        # draw_state.<param> = x - keyed by param name. They inject into the
        # kwargs gauntlet above the default layers (defaults and caller-passed
        # kwargs still win). Non-underscore on purpose: it serializes whenever
        # non-empty, so diverged state persists across sessions while
        # at-default params save nothing. _auto_baseline mirrors last frame's
        # RESOLVED value per param (what the caller handed the view); the
        # divergence scan compares the live attr against it to detect writes.
        # The the draw_state.<param> attrs themselves are dynamic (not
        # declared here), so to_dict never serializes them directly.
        self.auto_params = {}
        self._auto_baseline = {}
        self.hover_reported = True
        self._start_z_pos = 3
        self._column_width = 0
        self._front_layer = False

        # Used to cache abs_clip_rect
        self.clipped_by_rect = None

        # Set by Melty's end_frame tree rebuild: True while this nested
        # window's spawning view is currently scrolled/clipped out of sight, so
        # the window is skipped (not closed) by the layer dispatch and its
        # subtree filtered out of bvh_query hits. Read via getattr (live
        # instances predating the field won't have it).
        self._hidden_offscreen = False

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
        # PERSISTED (non-underscore, @exclude'd): the wrapper's post-body
        # observation. Serialized so a restored view's abs_content_height -
        # and with it needs_scroll and the persisted scroll state - is right
        # on a session's FIRST frame, before the view (or its loading
        # stand-in) has rendered once.
        self.observed_content_height = 0
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
        # view (the owner) currently holds melty's text focus. Consumed (cleared)
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
        # Closure set each render: (term, session) -> claims this view's own
        # matches into the session without imgui. Driven by meltygui.search_walk.
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

    def invalidate_up(self, max_depth=4, frame_delta=0, note=None, force=False):
        Core.melty.cache.invalidate_up(self._tile_id, frame_delta=frame_delta,
                                       max_depth=max_depth, note=note, force=force)

    def invalidate(self, frame_delta=0, note=None):
        Core.melty.cache.invalidate(self._tile_id, frame_delta=frame_delta, note=note)

    def invalidate_by_obj(self, obj=None, frame_delta=0, note=None):
        Core.melty.cache.invalidate_by_obj(obj=obj, frame_delta=frame_delta, note=note)

    def invalidate_up_by_obj(self, obj=None, max_depth=3, frame_delta=0, note=None):
        Core.melty.cache.invalidate_up_by_obj(obj, max_depth=max_depth, frame_delta=frame_delta, note=note)

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
        return (l, t, l + w, t + h - 2)

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


    # ─── locate_* : the set-anywhere accessors ────────────────────────────
    # `locate_<param>` access any param of the view, read and write:
    #
    #     tilt = draw_state.locate_tilt          # framework-resolved value
    #     draw_state.locate_x_dim = 0            # -> the DRIVING source
    #
    # ASYMMETRY on purpose: reading is ONLY the resolved value (_kwargs, with
    # the in-flight set cache and the ds fallback anywhere_value applies) -
    # cheap enough per frame. Only the WRITE walks the source registry, picks
    # the driving source, and drives its deferred save/hotswap.
    #
    # Declared HERE, in the class, so they work on every draw_state from the
    # first frame - independent of whether anything's imported the view
    # module yet. The implementation stays in view/core_views/anywhere.py and
    # is imported lazily per access (this module is model-layer; a top-level
    # import of the view module would cycle) - a sys.modules hit after the
    # first call.
    #
    # NAME ROUTING on read, the two directions are wired differently:
    #   READ  __getattr__ only runs on a MISS (no instance dict entry, no
    #         class property), so every normal attribute read pays nothing.
    #   WRITE there is no miss-only hook for setattr, and a DrawState
    #         __setattr__ would add a Python frame for EVERY attribute write
    #         (~100 per render_func call, per the wrapper's own bookkeeping).
    #         So write routing rides the __setattr__ that already exists: the
    #         @live wrapper calls `_locate_set` when a name starts with
    #         LOCATE_PREFIX (invalidating_decoration ensures the prefix is a single
    #         char compare for every other name).
    #
    # A locate write does NOT invalidate this draw_state - it's exactly the
    # `set_anywhere(...)` call it replaces, and the value only gets there
    # through the source save + hotswap, which invalidates on its own.
    LOCATE_PREFIX = "locate_"
    # Class-level default lets draw_states alive from before a hotswap (whose
    # __init__ never saw the field) read None instead of raising.
    _body_actions = None
    _body_surface_requests = None
    _event_rects = None

    def __getattr__(self, name):
        # Reached only when normal lookup failed. `locate_params` and any
        # future explicit property resolve BEFORE this and never arrive here.
        if name.startswith("locate_"):
            from meltygui.core.rendering.parameter_core import anywhere_value
            return anywhere_value(name[7:], self)
        raise AttributeError(name)

    def _locate_set(self, name, value):
        """The write half, called by @live's __setattr__ for `locate_*` names.
        Writes skip the SET_ANYWHERE_PARAMS whitelist (allow_any) — the point
        of the generic accessor is that any param of the view is settable —
        and fall back to storing the value ON THIS DRAW_STATE (ds_fallback)
        when no source in code sets the param, rather than rewriting the
        view function's signature default."""
        if isinstance(getattr(type(self), name, None), property):
            raise AttributeError(f"{name} is read-only")
        from meltygui.core.rendering.parameter_core import set_anywhere
        set_anywhere(name[7:], value, self, allow_any=True, ds_fallback=True)

    @property
    def locate_params(self):
        """Dict-shaped live view over this view's input parameters: reads
        resolve from _kwargs, item-writes go through set_anywhere.

            ds.locate_params["x_dim"] = 0
            for param, value in ds.locate_params: ...

        The whole-signature companion to `locate_<param>` above — same reads
        and writes, enumerable, and a real dict subclass so it renders and
        routes like any other dict. Read-only as an attribute; mutate it by
        item, which is where the anywhere write happens.

        ONE proxy per draw_state, re-snapshotted on each access: views key
        caches and dirty checks off the identity of the value they were
        handed, so a fresh object per frame would look like a new value every
        frame. Stored via object.__setattr__ — like _anc_scroll_key above, it
        never appears on the default instance, so it isn't serialized."""
        from meltygui.core.rendering.parameter_core import ParamProxy
        proxy = self.__dict__.get('_locate_proxy')
        if proxy is None:
            proxy = ParamProxy(self)
            object.__setattr__(self, '_locate_proxy', proxy)
            return proxy
        return proxy.refresh()

    @property
    def locate_all_params(self):
        """`locate_params` plus the HEADER's params, GROUPED: two nested
        dicts — {'params': <view params>, 'header': <header-only params>} —
        each a live ParamProxy (reads resolve, item-writes go through
        set_anywhere). A separate cached instance, same identity rules as
        locate_params above."""
        from meltygui.core.rendering.parameter_core import GroupedParamProxy
        proxy = self.__dict__.get('_locate_all_proxy')
        if proxy is None or not isinstance(proxy, GroupedParamProxy):
            proxy = GroupedParamProxy(self)
            object.__setattr__(self, '_locate_all_proxy', proxy)
            return proxy
        return proxy.refresh()

    def pos_changed(self):
        """Reconcile this view's BVH box with its current geometry/visibility.

        Called at the end of every render. bvh_sync is the single source of
        truth — it no-ops unless `bbox` (abs_left/abs_top/width/height) or
        visibility (inside_clip/closed/abs_closed) actually changed, and owns
        `_bvh_id`/`_bvh_bbox` so they stay in lockstep with the rtree."""
        Core.melty.bvh_sync(self)

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
        clip_rect = self.abs_clip_rect

        clipped_bottom = min(clip_rect[3], self.abs_top + self.height)
        clipped_top = max(clip_rect[1], self.abs_top)
        clipped_height = clipped_bottom - clipped_top
        return max(0, clipped_height)

    @property
    def abs_content_height(self):
        if self.frame_count < 2:
            # Serve the PERSISTED measurement while this session hasn't
            # re-measured yet: a restored view then scrolls to its persisted
            # scroll_offset on the very first frame, before its content (or
            # a loading stand-in) renders. Returns 0 only for never-measured
            # views, matching the old behavior for genuinely new ones.
            return int(self.observed_content_height)

        content_height = 0
        f = Core.melty.frame_count
        max_bottom = 0
        min_top = float('inf')
        key = (f, self.height, self._parent.abs_clip_rect, self.observed_content_height)
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

            min_top = min(min_top, child.abs_top)
            max_bottom = max(max_bottom, child.abs_top + child.height)

        content_height = max_bottom - min_top if max_bottom > min_top else 0

        # Special case where view wants to scroll but has no children for which to infer its height
        if "determines_height" in self._kwargs:
            content_height = self._content_rect[1]


        self._abs_content_height_cache = content_height

        # if is_scroll_view:
        #     content_height = max(content_height, self.height)

        return int(self.observed_content_height)

    @property
    def abs_layer(self):

        if self.parent_window is not None and self.closable:
            # Nested closable windows live in the dedicated nested layer band
            # when their parent's root is the front window; otherwise they stay
            # parent-relative in the parent band. See Melty.nested_window_layer.
            return Core.melty.nested_window_layer(
                self.parent_window.abs_layer, self._kwargs.get("layer_offset", 4),
                ds=self)
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

        The target's box comes from its cached ``abs_left``/``abs_top``. That
        cache is keyed on Melty.geometry_version, so a target carried by a
        dragged window (its own attributes unchanged, its window's window_pos
        written) misses and re-reads the moved chain: the anchor tracks the
        drag on frames the target itself isn't re-rendering, with no walk per
        read."""
        target = self._pin_target
        if target is None:
            return None
        # Anchor to the target's RAW box, so the float tracks the
        # target's actual position. PARENT/WINDOW/GRANDPARENT do NOT clamp here:
        # intersecting with the target's clip rect makes the anchor corner snap
        # to the clip edge whenever the target is clipped, so the window tracks
        # the *clip* instead of the target (the "only moves when the clamp moves
        # it" bug). Clamping is Pin.CLIP's job - see below.
        tl, tt = target.abs_left, target.abs_top
        rect = (tl, tt, tl + target.width, tt + target.height)
        if self.pin_to_clip is Pin.CLIP:
            # Pin.CLIP clamps the float to the parent window's visible box so
            # corners track the visible edge. The window's box, not the
            # target's captured clip_rect: that one is fixed in screen space
            # and stale once the window itself is dragged. The box is constant
            # while content scrolls and moves with the window.
            win = self.parent_window
            if win is None and len(Core.melty.melty_windows) > 0:
                win = Core.melty.melty_windows[-1]
            if win is not None and win is not self:
                wl, wt = win.abs_left, win.abs_top
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
        on demand. Returns (sx, sy).

        Memoized per (frame_count, Melty.scroll_version): the walk used to run
        on EVERY abs_left/abs_top access just to build their cache keys (~57
        walks per render_func call). Any real scroll_offset change anywhere
        bumps scroll_version (see new_setattr in invalidation_decoration), so
        mid-frame scroll deltas still invalidate exactly like the uncached
        walk did."""
        meltygui = Core.melty
        key = (meltygui.frame_count, getattr(meltygui, 'scroll_version', 0))
        if self._anc_scroll_key == key:
            return self._anc_scroll_cache
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
        # Raw writes so the memo must not re-enter @live setattr tracking.
        # Cache before key, so a matching key always sees a written cache.
        # (The clamp writeback above may have bumped scroll_version, making
        # this key already anyway - the next access just recomputes.)
        object.__setattr__(self, '_anc_scroll_cache', (sx, sy))
        object.__setattr__(self, '_anc_scroll_key', key)
        return sx, sy

    def _pinned_base_y(self, base_y, anchor_y):
        """Bound a pinned nested window's vertical ANCHOR to its parent window:
        the float's TOP can ride at most 200px above the window's top, and its
        TOP can't drop below the window's bottom. (Display top/height when there is no
        parent window.)

        Pinned floats (context menus, live-value windows — pin_to_clip set)
        follow their spawner: pin_rect reads the target's live abs, so a tall
        spawner (e.g. a big scrolling draw_text) that scrolls drags the float
        clean out of the window with it — the spawning VIEW routinely sits far
        above/below the window's own box. Bounding against the WINDOW (not the
        display, not the view) keeps the float within a window-height of travel
        past each edge, so it's always adjacent to the window it belongs to.

        Clamps the ANCHOR (base_y), NOT the final abs top: window_pos_y (the
        user's drag) is deliberately left out of the bound, so a click-drag
        still moves the window freely — clamping the sum ate vertical drag and
        left a dead zone on the way back. Same shape as the swoosh, which
        clamps its parent anchor to the window edge."""
        if not self.closable:
            return base_y
        win = self.parent_window
        if win is not None and win is not self:
            win_top, win_h = win.abs_top, win.height or 0
        else:
            disp = Core.melty.display_size
            win_top, win_h = 0, (disp[1] if disp is not None else 0)
        floor_y = win_top - 200 - anchor_y
        ceil_y = win_top + win_h - anchor_y
        if floor_y <= base_y <= ceil_y:
            return base_y
        try:
            # Exempt the floating DnD window: glue_window_to_cursor assumes abs
            # is linear in window_pos, and a clamp makes its per-frame
            # correction accumulate without bound (see _cap_to_display).
            from meltygui.core.input.drag_drop_core import DragDrop
            if DragDrop.is_dragged_item(self):
                return base_y
        except Exception:
            pass
        return floor_y if base_y < floor_y else ceil_y

    def _cap_to_display(self, pos, axis):
        """Cap a nested window's computed abs position so at least a sliver of
        its box stays on the display. Nested windows follow their spawning
        view's scroll (the ancestor-scroll subtraction in _abs_left/_abs_top),
        which otherwise carries them right off screen. Only the COMPUTED
        position is capped — left_offset / window_pos stay untouched — so
        scrolling back returns the window to its natural spot. A sliver cap
        (not full containment) so a deliberate drag can still tuck a window
        mostly off screen without ever losing its grab handle. The floating
        DnD window is exempt: glue_window_to_cursor assumes abs is linear in
        window_pos, and a clamp would make its per-frame correction
        accumulate without bound."""
        if (not self.closable or self.parent_window is None
                or self.parent_window is self):
            return pos
        size = self.width if axis == 0 else self.height
        disp = Core.melty.display_size
        if size is None or disp is None:
            return pos
        keep = min(48, size)
        if self._kwargs.get("keep_in_view", False):
            # Popup menus stay wholly visible. Ordinary movable windows retain
            # their sliver allowance so you can deliberately tuck them away.
            lo, hi = 0, max(0, disp[axis] - size)
        else:
            lo, hi = keep - size, disp[axis] - keep
        # whether the cap is holding this axis: the OS-edge physics
        # (os_frame._root_extent) pull a scrolled-out nested window out of
        # its parent's collision extent
        capped = not (lo <= pos <= hi)
        if axis == 0:
            self._capped_x = capped
        else:
            self._capped_y = capped
        if not capped:
            return pos
        try:
            from meltygui.core.input.drag_drop_core import DragDrop
            if DragDrop.is_dragged_item(self):
                return pos
        except Exception:
            pass
        return max(min(pos, hi), lo)

    def _abs_left(self):
        # Parent-relative placement reads the dependency-checked absolute
        # origin. The cache marks evaluation before recursing, so a malformed
        # parent cycle returns the last position instead of recursing forever.
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
            this_left = self._cap_to_display(this_left, 0)
        return int(this_left)

    def _abs_top_true(self):
        parent_top = 0
        if self._parent is not None and self._parent is not self:
            parent_top = self._parent._abs_top_true()
            parent_scroll = self._parent.scroll_offset[1] if self._parent.scroll_offset is not None else 0
            parent_top -= parent_scroll

        anchor = self.anchor_offset
        window_pos_y = self.window_pos[1] if self.window_pos is not None else 0

        base = self.clip_anchor_base if self.pin_to_clip else None
        if base is not None:
            # Pin to the clip rect corner rather than the scrolled position of
            # the declaring view (which top_offset tracks).
            this_top = window_pos_y + self._pinned_base_y(base[1], anchor[1]) + anchor[1]
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
            this_top = (window_pos_y + parent_top + self.top_offset_true
                        + self.parent_anchor_offset[1] + anchor[1])
        return int(this_top)


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
            this_top = window_pos_y + self._pinned_base_y(base[1], anchor[1]) + anchor[1]
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
            this_top = self._cap_to_display(this_top, 1)
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

    def children_in_clip(self, clip=None, max_depth=1, include_windows=True):
        """Descendants whose vertical span overlaps `clip` (default: this view's
        abs_clip_rect), top-to-bottom.

        `max_depth` bounds the recursion: 1 (default) returns only direct
        children; higher values also descend into them, including any descendant
        that overlaps `clip`, down at most `max_depth` levels. Pass
        `max_depth=None` for the whole subtree. The same `clip` is carried down
        — positions are absolute (screen space), so a grandchild is kept iff it
        overlaps the original viewport, not its immediate parent's box.

        `include_windows=False` prunes nested windows (closable) and their
        whole subtree — same contract as get_child_keys' include_windows: a
        window owns its own composition and doesn't move with this view, so
        callers reacting to this view's layout (e.g. the scroll-in sweep)
        shouldn't touch it.

        Results are pre-order (each child immediately followed by its own
        in-clip subtree) and deduped by identity across the whole walk."""
        if clip is None:
            clip = self.abs_clip_rect
        if clip is None:
            return []

        direct = self._direct_children_in_clip(clip)
        if not include_windows:
            direct = [c for c in direct if not c.closable]
        if max_depth is not None and max_depth <= 1:
            return direct

        next_depth = None if max_depth is None else max_depth - 1
        seen = set()
        result = []
        for c in direct:
            if id(c) not in seen:
                seen.add(id(c))
                result.append(c)
            for gc in c.children_in_clip(clip, next_depth, include_windows):
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
        each view's _search_matcher (meltygui.search_walk), instead of force-
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

    def _cached_absolute_position(self, axis):
        # abs_left / abs_top are read thousands of times a frame (every
        # on_action, hover test, clip rect, pin and edge pass), so a HIT is
        # one key comparison: no parent read, no walk, no per-level key. Never
        # weaken that (docs/WINDOW_COLLISION_COLUMNS.md, "Absolute-position
        # caching is not negotiable").
        #
        # Freshness comes from invalidation instead. Melty.geometry_version
        # bumps on every real change to an attribute positions are computed
        # from, on ANY draw state (invalidation_decoration.GEOMETRY_ATTRS), and
        # scroll_version does the same for ancestor scroll. So a parent hand
        # resize, native rebase or edge solve later in this same frame, a
        # dragged window carrying its children, and a pin target that moved
        # all miss here on the next read - pinned views included, which is why
        # they need no live walk of their own. The frame count covers inputs
        # that are not draw-state attributes (display size, a DnD flight).
        #
        # Key and cache writes are raw, as in _ancestor_scroll: the memo must
        # not re-enter @live setattr tracking.
        melty = Core.melty
        key = (melty.frame_count, melty.geometry_version, melty.scroll_version)
        if axis == 0:
            key_name, previous, cached = '_abs_left_key', self._abs_left_key, self._abs_left_cache
        else:
            key_name, previous, cached = '_abs_top_key', self._abs_top_key, self._abs_top_cache
        if previous == key or previous is False:
            return cached  # False: a malformed parent cycle keeps the last position
        # Mark evaluation before reading the parent: a parent cycle gets the
        # last position back instead of recursing forever.
        object.__setattr__(self, key_name, False)
        try:
            value = self._abs_left() if axis == 0 else self._abs_top()
            object.__setattr__(self, '_abs_left_cache' if axis == 0 else '_abs_top_cache', value)
            return value
        finally:
            # A geometry write made while computing (the display cap's flags
            # are private, so normally none) leaves the key behind the
            # version: the next read recomputes.
            object.__setattr__(self, key_name, key)

    @property
    def abs_left(self):
        return self._cached_absolute_position(0)

    @property
    def abs_top(self):
        return self._cached_absolute_position(1)

    @property
    def abs_top_true(self):
        # if self.pin_to_clip:
        #     return self._abs_top()
        f = Core.melty.frame_count
        key = (f, self.top_offset_true, self.window_pos,
               self.anchor_pos, self.parent_anchor_pos, self.height)
        if self._abs_top_true_key == key:
            return self._abs_top_true_cache
        self._abs_top_true_key = key
        val = self._abs_top_true()
        self._abs_top_true_cache = val
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
        import libcst as cst   # lazy: libcst takes ~80 ms to import
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
        ox, oy = getattr(Core.melty, "frame_origin", None) or (0, 0)   # shadow margin (+ shift) → content coords
        mx, my = global_mouse[0] - ox, global_mouse[1] - oy
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
                           pack_color(*tint[:3], 1.0) if tint is not None else
                           pack_color(1, 1, 1, 1),
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

    def hover_eligible(self, rect=None, ignore_reports=True, clip_rect=None):
        # `clip_rect`: a caller that already read abs_clamped_rect this call
        # passes it in - on_action's registration computes it for the cursor
        # rect too, ~240 registrations a frame on the code editor window.
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
            if clip_rect is None:
                clip_rect = self.abs_clamped_rect
            if clip_rect is not None:
                mx, my = imgui.get_mouse_pos()
                if not (clip_rect[0] <= mx <= clip_rect[2] and clip_rect[1] <= my <= clip_rect[3]):
                    return False
        else:
            # Custom sub-region: clip it and point-test the cursor directly.
            # Clamp to abs_clamped_rect (bbox ∩ abs clip), not just the bbox -
            # a view scrolled under its parent's header still has the header
            # band inside its bbox, and clipping only to the bbox let it report
            # hover there and steal the header's drag (window_move) event.
            if clip_rect is None:
                clip_rect = self.abs_clamped_rect
            if clip_rect is not None:
                rect = (max(rect[0], clip_rect[0]), max(rect[1], clip_rect[1]),
                        min(rect[2], clip_rect[2]), min(rect[3], clip_rect[3]))
            mx, my = imgui.get_mouse_pos()
            if not (rect[0] <= mx <= rect[2] and rect[1] <= my <= rect[3]):
                return False

        if not (self.hover_reported is None or self.hover_reported or ignore_reports):
            return False
        return True

    def _action_base_priority(self, z_pos=None):
        """on_action's default priority: the wrapper's z ordering inverted
        (0 = topmost). `z_pos=None` reads Melty's live layer/depth — what a
        call from inside this view's body sees."""
        max_layer_depth = Core.melty.max_depth * Core.melty.max_depth + Core.melty.max_depth
        if z_pos is None:
            z_pos = Core.melty.paint_rank * Core.melty.max_depth + Core.melty.depth
        return max_layer_depth - z_pos

    def _register_action(self, view_id, event_names, registered_priority, rect, cursor,
                         debug_priority=0, debug_delta=0, cursor_gate=None):
        """The registration half of on_action: hover-test `rect` (None = this
        view's bbox) and subscribe. Shared by the live call and the cache-hit
        replay so both obey the same z-order / blocker rules."""
        if rect is not None:
            # Cheapest test first: ~330 registrations a frame on the code
            # editor window (frame edges, scrollbars, buttons, dials), and the
            # pointer is inside a handful of them. hover_eligible clips the
            # rect before calling, and the clip can only shrink it, so a
            # pointer outside the input rect is outside the clipped one too -
            # skip the priority / clamped-rect work on those.
            mx, my = imgui.get_mouse_pos()
            if not (rect[0] <= mx <= rect[2] and rect[1] <= my <= rect[3]):
                return
        if not self.hover_eligible(rect):
            return
        cursor_rect = None
        if cursor is not None:
            # The input handler re-tests this rect against the LATEST pointer
            # position when it changes the shape (gl_gui/mouse_cursor.py), so
            # a slow frame can't hold the I-beam after the pointer has left.
            cursor_rect = self.abs_clamped_rect
            if rect is not None and cursor_rect is not None:
                cursor_rect = (max(rect[0], cursor_rect[0]), max(rect[1], cursor_rect[1]),
                               min(rect[2], cursor_rect[2]), min(rect[3], cursor_rect[3]))
            elif rect is not None:
                cursor_rect = tuple(rect)
        Core.melty.event_handler.register_hovered(view_id, event_names,
                                                  priority=registered_priority,
                                                  tile_id=self._tile_id, cursor=cursor,
                                                  cursor_rect=cursor_rect, cursor_gate=cursor_gate)

        overlay = imgui.get_overlay_draw_list()
        overlay.channels_set_current(Core.melty.max_layer - 1)
        if Toggles.InputHandlerToggles.show_debug:
            for name in event_names:
                self._event_names.add(name)

            ds = self
            color = (1,1,1)
            text = f"zpos:{self.layer} priority:{debug_priority} priority_delta:{debug_delta} | {str(self._event_names)}"
            invalidation_rect = (ds.abs_left, ds.abs_top,
                                 ds.abs_left + (ds.width or 0),
                                 ds.abs_top + (ds.height or 0))
            text_size = imgui.calc_text_size(text)
            overlay.add_rect_filled(invalidation_rect[0] + ds.width - text_size.x, invalidation_rect[1],
                                    invalidation_rect[0] + ds.width,
                                    invalidation_rect[1] + text_size.y,
                                    pack_color(*color[:3],
                                                             1))

            overlay.add_text(invalidation_rect[0] + ds.width - text_size.x, invalidation_rect[1],
                             pack_color(*(0, 0, 0),
                                                      1),
                             text)

            overlay.add_rect(invalidation_rect[0], invalidation_rect[1], invalidation_rect[2],
                             invalidation_rect[3],
                             pack_color(*color[:3],
                                                      1),
                             thickness=1.0)

    def replay_body_actions(self):
        """Blit-cache hit: the body did not run, so re-issue every on_action
        it made on its last real render (see on_action). Rects re-anchor to
        the LIVE abs position (the cached property lags a blit-served drag)
        and priorities to the current z_pos, then go through the normal
        hover / blocker gauntlet — a covered or un-hovered rect registers
        nothing, exactly as the live call would."""
        # A fast host (fast_draw_collection: no tile of its own) paints into
        # this tile, so its rows' subscriptions come back with this tile's.
        for child in [*self._children.values(), *self._view_children.values()]:
            if child is not None and child is not self and getattr(child._wrapper, "fast_host", False):
                child.replay_body_actions()
        record = self._body_actions
        if not record or not record[1]:
            return
        if self.just_shadow or not Core.melty.inside_clip(draw_state=self):
            return
        base = self._action_base_priority(z_pos=self.z_pos)
        left, top = self._abs_left(), self._abs_top()
        for view_suffix, event_names, offset, relative_rect, cursor, cursor_gate in record[1]:
            view_id = self._tile_id if view_suffix is None else str(self._tile_id) + "_" + str(view_suffix)
            rect = None
            if relative_rect is not None:
                rect = (left + relative_rect[0], top + relative_rect[1],
                        left + relative_rect[2], top + relative_rect[3])
            self._register_action(view_id, list(event_names), base + offset, rect, cursor,
                                  debug_priority=base + offset, cursor_gate=cursor_gate)

    @property
    def priority(self):
        max_layer_depth = (Core.melty.max_depth *
                           Core.melty.max_layer + Core.melty.max_depth)
        layer_and_depth = (Core.melty.paint_rank *
                           Core.melty.max_depth + Core.melty.depth)
        return max_layer_depth - layer_and_depth

    def get_content_rect(self):
        # An inline header shares the value row; it consumes horizontal space.
        header_height = 0 if self._kwargs.get('header_same_line', False) else self.header_height
        return (self.abs_left, self.abs_top + header_height,
                self.abs_left + self.width, self.abs_top + self.height - self.footer_height)

    def get_header_rect(self):
        # header_width is the measured width of the drawn header, so the drag
        # handle covers only the label band; 0 (no header drawn / not yet
        # measured) falls back to the full view width.
        right = self.abs_left + (self.header_width if self.header_width else self.width)
        return (self.abs_left, self.abs_top, right, self.abs_top + self.header_height)

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

    def get_action(self, event_name, view_id=None):
        """Read an already-delivered event without registering a hit region.

        Geometry owners consume input, resolve their rectangle, then subscribe
        with on_action using that final rectangle for the next input dispatch.
        """
        identity = self._tile_id if view_id is None else f"{self._tile_id}_{view_id}"
        return Core.melty.events.get(identity, {}).get(event_name)

    def replay_surface_requests(self):
        """Blit-cache hit: the body did not run, so the child OS windows it
        requested on its last real render (draw_x(glfw_window=True) - the
        project card's environment picker) made no request this tick. The
        app loop closes a child whose request was not refreshed in a tick
        (_close_stale_children), and a stale close leaves the request open,
        so the next real run reopened it: the window flickered with every
        hover frame that hit the cache. Re-stamp the recorded requests
        instead - the body still wants them, it just did not run."""
        record = self._body_surface_requests
        if not record or not record[1]:
            return
        melty = Core.melty
        for req in record[1]:
            if melty.surface_windows.get(req.tile_id) is req and not req.closed:
                req.tick = melty.app_tick

    def is_drag_captured(self, input_id="left_mouse", view_id=None):
        """True from a handle's captured press through release, before motion too."""
        target = self._tile_id if view_id is None else f"{self._tile_id}_{view_id}"
        handler = Core.melty.event_handler
        return handler is not None and handler.is_drag_captured(input_id, target)

    def on_action(self, event_names, view_id=None, priority=None, priority_delta=0, rect=None, cursor=None,
                  cursor_gate=None):
        """Subscribe this view to `event_names` (or, with an empty list, just
        tag `rect` with a pointer `cursor` shape) for this frame.
        `cursor_gate="left_mouse_dragged"` shows the shape only where this
        subscription is the one that event would be captured by
        (InputHandler.register_hovered) — a drag handle's shape.

        Called from a view BODY it is recorded on the draw_state as well
        (`_body_actions`), because the wrapper skips the body on a blit-cache
        hit and InputHandler forgets every subscription each frame — without
        the record a cached tile could not latch a drag or keep its I-beam
        (`replay_body_actions` re-issues the record on a hit). Recording only
        happens for the frame the wrapper opened the record in, so pre-gate
        calls on cached frames never pile up."""
        if self.parent_window is None and not self.closable:
            priority_delta -= 1

        if not Core.melty.inside_clip(draw_state=self):
            return None
        if self.just_shadow:
            return None
        view_suffix = view_id
        if view_id is None:
            view_id = self._tile_id
        else:
            view_id = str(self._tile_id) + "_" + str(view_id)

        single_event = False
        if isinstance(event_names, str):
            event_names = [event_names]
            single_event = True

        if priority is None:
            priority = self._action_base_priority()
        registered_priority = priority - priority_delta

        record = self._body_actions
        if record is not None and record[0] == Core.melty.frame_count:
            # Priority is kept as an offset from this view's z_pos and the rect
            # relative to its abs position: both are re-derived on replay, so a
            # view that moved (blit-served drag) or changed layer replays right.
            if rect is None:
                relative_rect = None
            else:
                left, top = self.abs_left, self.abs_top
                relative_rect = (rect[0] - left, rect[1] - top, rect[2] - left, rect[3] - top)
            record[1].append((view_suffix, tuple(event_names),
                              registered_priority - self._action_base_priority(z_pos=self.z_pos),
                              relative_rect, cursor, cursor_gate))

        self._register_action(view_id, event_names, registered_priority, rect, cursor,
                              debug_priority=priority, debug_delta=priority_delta,
                              cursor_gate=cursor_gate)

        if single_event:
            return self.get_action(event_names[0], view_id=view_suffix)

        return_events = {}
        if view_id in Core.melty.events:
            for event_name in Core.melty.events[view_id]:
                return_events[event_name] = Core.melty.events[view_id][event_name]
        return return_events

    def event_rect(self, event_names, rect):
        """Narrow a declared event PARAM to `rect` (screen coords) — the hit
        area the wrapper registers it over, instead of the whole content
        rect. Call it from the view body every run, next to the geometry
        that defines the rect; several calls for the same name union their
        rects. Outside the rects the param is simply not subscribed, so a
        press/drag there falls through to whatever sits behind this view in
        z-order — the enclosing window's move handle, a parent's scroll —
        with no forwarding code. draw_text scopes `left_mouse_drag` to its
        text rows this way so the gutter and the space below the last line
        drag the window. Rects are stored relative to the view origin (a
        blit-served window drag moves the view without re-running the body)
        and re-anchored at registration; the wrapper reads the record of the
        LAST body run when it registers the params for this one, then
        clears it, so a run that stops declaring lifts the scope. Not the
        tool for an EXTRA sub-rect the body wants events from — that is
        `on_action(event, view_id=…, rect=…)`."""
        if isinstance(event_names, str):
            event_names = (event_names,)
        left, top = self.abs_left, self.abs_top
        if left is None or top is None:
            return
        relative_rect = (rect[0] - left, rect[1] - top, rect[2] - left, rect[3] - top)
        record = self._event_rects
        if record is None:
            record = self._event_rects = {}
        for name in event_names:
            record.setdefault(name, []).append(relative_rect)

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
