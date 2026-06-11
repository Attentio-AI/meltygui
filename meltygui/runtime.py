import math
import threading as _threading
import time
import types
from contextlib import contextmanager
from collections import defaultdict, deque
from copy import copy
from enum import Enum
from typing import MutableMapping, Optional

import glfw
import imgui
import libcst as cst
from imgui.core import _DrawList

from rtree import index as rtree_index

from src.lsd.gl_gui.notifications import draw_notifications
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils import glfw_utils
from src.lsd.gl_gui.view.attribute_churn import AttributeChurnMonitor
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.invalidation_tracker import InvalidateTracker, Note

from src.lsd.gl_gui.background import Background
from src.lsd.gl_gui.collection_action import CollectionAction
from src.lsd.gl_gui.collision import Collisions
from src.lsd.gl_gui.toggles import Toggles, Counters, Tint, Swoosh, SwooshMode
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import set_window_registrar
from src.lsd.gl_gui.view.core_views.monitor import Monitor
from src.lsd.gl_gui.view.view_utils.imgui_style_manager_class import ImGuiStyleManager
from src.shader_library.shader_manager.texture_manager import TextureManager
from src.shader_library.shader_manager.filter import Filter
from src.lsd.gl_gui.events.input_handler import InputHandler, InputEvent
from src.lsd.gl_gui.events.event_backends import ImGuiBackend, GlfwQueueBackend
from src.lsd.gl_gui.model.core_model.core_enums import generate_id
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace

import OpenGL.GL as gl
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults

_MOUSE_INPUTS = frozenset({'left_mouse', 'right_mouse', 'middle_mouse',
                           'cursor', 'scroll_y', 'scroll_x'})


class SearchTerm(str):
    """The active search string, plus a shared aggregation session.

    A search owner pushes one of these onto Melty.search_stack; it forwards to
    every searchable descendant as their `search_text`. Because it's a str
    subclass, views can match against it directly, while the extra fields let
    all views contribute to a single combined result set:

      - current:  the global index (0..total-1) the owner wants selected
      - scroll_to: whether the view holding `current` should scroll to it
      - offset:   running base index; each view claims [offset, offset+count)
      - total:    grand total across every view (read back by the owner)

    offset/total are reset each frame (a fresh SearchTerm is pushed) and grow
    as views register, in render order, so `current` maps to one match in one
    view deterministically.
    """
    def __new__(cls, value="", current=0, scroll_to=False):
        obj = super().__new__(cls, value)
        obj.current = current
        obj.scroll_to = scroll_to
        obj.offset = 0
        obj.total = 0
        return obj

    def claim(self, count):
        """Register `count` matches for the calling view; return its base
        offset and the local index of the global-current match (or None)."""
        base = self.offset
        self.offset += count
        self.total += count
        if count and base <= self.current < base + count:
            return base, self.current - base
        return base, None


def search_walk(ds, term, session, max_depth=12):
    """Count a subtree's matches into `session` AND mark the current one — the
    single source of truth for both the find UI count and the selection.

    Each searchable view stashes a `_search_matcher(term, session)` closure on
    its draw_state during render (capturing its content). Here we walk the live
    draw_state tree (`ds` plus its descendants, via DrawState.descendants) and
    invoke each matcher, so the whole subtree — including off-screen rows the
    render skips, whose matcher persists from when they last drew — contributes.
    Each matcher claims only its own direct matches; the walk supplies the
    recursion, so siblings and nested views sum without double-counting.

    As it goes it records, on each node, the local index of `session.current`
    when that global match lands in this node (_search_active_local, else None),
    and returns the node holding it. Views read that while drawing to highlight
    the right match, so the count and the selection can never disagree.
    """
    current_node = None
    for node in (ds, *ds.descendants(max_depth=max_depth)):
        matcher = getattr(node, '_search_matcher', None)
        if matcher is None:
            continue
        base = session.offset
        matcher(term, session)
        count = session.offset - base
        if count and base <= session.current < base + count:
            node._search_active_local = session.current - base
            current_node = node
        else:
            node._search_active_local = None
    return current_node

import hashlib
import difflib
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class FileWatch:
    observer = Observer()
    handler = FileSystemEventHandler()
    _watched_dirs = set()
    path_to_draw_states = {}   # path → set of draw_states
    draw_state_to_path = {}
    _ds_hashes = {}            # draw_state → hash (per-view, not per-path)
    _ds_suppress_until = {}    # id(ds) → monotonic time until which to suppress dispatch
    _file_contents = {}
    _path_hash_cache = {}      # resolved path → (mtime, md5); avoids re-reading unchanged files
    _self_write_hashes = {}    # resolved path → md5 of the last IN-PROCESS write (any view)
    output_debug_diff = False
    _write_suppress_window = 1.0  # seconds - for truncate+write event pairs from write_text
    # Global file-event listeners: called with every event's src_path on the
    # OBSERVER thread, before (and regardless of) the per-draw_state dispatch
    # - so a subscriber sees changes to files no view is watching. The symbol
    # index subscribes here (libcst_conversion._on_watch_event). Listeners
    # must be fast/non-blocking (debounce internally); exceptions swallowed.
    global_listeners = []

    @classmethod
    def start(cls):
        cls.handler.on_modified = cls._on_event
        cls.handler.on_created = cls._on_event
        cls.observer.start()

    @classmethod
    def _get_hash(cls, path):
        # Cache by (path, mtime): register_draw_state hashes the file to set a
        # baseline, and can be called repeatedly during frame. Reading + MD5'ing
        # the whole file every time is what made typing in large files stall.
        # While typing the file isn't written (mtime unchanged) so we return the
        # cached digest; a save bumps mtime so a re-read exactly once.
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return None
        cached = cls._path_hash_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, 'rb') as f:
                digest = hashlib.md5(f.read()).hexdigest()
        except OSError:
            return None
        cls._path_hash_cache[path] = (mtime, digest)
        return digest

    @classmethod
    def _read_text(cls, path):
        try:
            with open(path, 'r') as f:
                return f.readlines()
        except (OSError, UnicodeDecodeError):
            return []

    @classmethod
    def _on_event(cls, event):
        if Melty.frame_count < 2:
            return
        # Invalidate any cached file text for the changed path so the next read
        # re-reads from disk. Done before the no-draw_states early-return so a
        # sibling file in a watched dir (cached by the symbol index but with no
        # view of its own) is still invalidated. Keyed the same as
        # path_to_draw_states - str(path.resolve) - so event.src_path matches.
        Melty.code_cache.pop(event.src_path, None)
        for listener in list(cls.global_listeners):
            try:
                listener(event.src_path)
            except Exception:
                pass
        draw_states = cls.path_to_draw_states.get(event.src_path)
        if not draw_states:
            return
        new_hash = cls._get_hash(event.src_path)
        if not new_hash:
            return
        now = time.monotonic()
        debug_printed = False
        for ds in list(draw_states):
            # Same-view writes (set_hash_from_content) open a short suppress
            # window. Multiple fs events may fire during one write (e.g. truncate
            # then flush). Within the window we keep _ds_hashes synced with
            # disk but never dispatch - so the view doesn't reload its own
            # write, or intermediate events it never produced.
            if now < cls._ds_suppress_until.get(id(ds), 0):
                cls._ds_hashes[id(ds)] = new_hash
                continue
            if new_hash != cls._ds_hashes.get(id(ds)):
                if cls.output_debug_diff and not debug_printed:
                    old_lines = cls._file_contents.get(event.src_path, [])
                    new_lines = cls._read_text(event.src_path)
                    diff = difflib.unified_diff(
                        old_lines, new_lines,
                        fromfile=f"{event.src_path} (old)",
                        tofile=f"{event.src_path} (new)",
                    )
                    if ''.join(diff):
                        print(''.join(diff))
                    debug_printed = True

                cls._ds_hashes[id(ds)] = new_hash
                cls.dispatch_event_for(ds)

        if cls.output_debug_diff:
            cls._file_contents[event.src_path] = cls._read_text(event.src_path)

    @classmethod
    def register_draw_state(cls, draw_state, path: Path):
        resolved = str(path.resolve())

        if draw_state in cls.draw_state_to_path:
            return

        old_path = cls.draw_state_to_path.pop(draw_state, None)
        if old_path:
            ds_set = cls.path_to_draw_states.get(old_path)
            if ds_set:
                ds_set.discard(draw_state)
                if not ds_set:
                    cls.path_to_draw_states.pop(old_path, None)
            cls._ds_hashes.pop(id(draw_state), None)
            cls._ds_suppress_until.pop(id(draw_state), None)
            if not cls.path_to_draw_states.get(old_path):
                cls._file_contents.pop(old_path, None)

        if resolved not in cls.path_to_draw_states:
            cls.path_to_draw_states[resolved] = set()
        cls.path_to_draw_states[resolved].add(draw_state)
        cls.draw_state_to_path[draw_state] = resolved
        cls._ds_hashes[id(draw_state)] = cls._get_hash(resolved)

        if cls.output_debug_diff:
            cls._file_contents[resolved] = cls._read_text(resolved)

        parent = str(path.resolve().parent)
        if parent not in cls._watched_dirs:
            cls.observer.schedule(cls.handler, parent, recursive=False)
            cls._watched_dirs.add(parent)

    @classmethod
    def dispatch_event_for(cls, draw_state):
        # The file moved on disk (often a SIBLING def edited in another window,
        # which moved this one's line span). The codec caches the resolved
        # Address on draw_state._addr_cache keyed by (source, mtime); busting it
        # here forces resolve_address to re-resolve the span the next time this
        # view runs, instead of handing back the stale cached Address. Without
        # this, a sibling edit leaves code_state.address pointing at the OLD span.
        if getattr(draw_state, '_addr_cache', None) is not None:
            draw_state._addr_cache = None

        # Invalidate both the parent window AND this view's own tile. The
        # _addr_cache + code_state live on THIS draw_state, so its tile must
        # re-execute resolve_address - invalidating only the parent window can
        # leave this nested code_file_io tile served from cache (stale address).
        if draw_state._tile_id is not None:
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10, force=True)
        if draw_state.parent_window is not None and draw_state.parent_window._tile_id is not None:
            Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=10, force=True)

        draw_state._external_change = True
        request_render()


    @classmethod
    def set_hash_from_content(cls, path: Path, content: str, draw_state=None):
        """Pre-set hash from known content. Call before write.

        Also opens a brief suppress window on the target draw_state(s) so
        intermediate fs events from the upcoming write (e.g. the open("w")
        truncate before the flush) don't trigger a self-reload.

        If draw_state is given, only update that view's hash — other
        views watching the same path will see the write as an external change.
        Otherwise update all draw_states for the path (old behaviour).
        """
        resolved = str(path.resolve())
        new_hash = hashlib.md5(content.encode()).hexdigest()
        suppress_until = time.monotonic() + cls._write_suppress_window

        # Process-wide record: EVERY in-process save announces its content here
        # (codec.save / _do_save call this right before writing). is_self_write
        # lets a SIBLING view of the same file tell "one of us wrote this" from
        # "an outside program wrote this" - the per-draw_state hash above only
        # covers the writer's own view.
        cls._self_write_hashes[resolved] = new_hash

        if draw_state is not None:
            cls._ds_hashes[id(draw_state)] = new_hash
            cls._ds_suppress_until[id(draw_state)] = suppress_until
        else:
            for ds in list(cls.path_to_draw_states.get(resolved, ())):
                cls._ds_hashes[id(ds)] = new_hash
                cls._ds_suppress_until[id(ds)] = suppress_until

        if cls.output_debug_diff:
            cls._file_contents[resolved] = content.splitlines(keepends=True)

    @classmethod
    def is_self_write(cls, path):
        """True when the file's CURRENT on-disk content is the last write made
        by an in-process editor (any code_file_io / _do_save instance) — a
        sibling view syncing through the file, not an outside program. Used to
        reload quietly instead of stamping the "loaded from disk" indication."""
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            return False
        recorded = cls._self_write_hashes.get(resolved)
        if recorded is None:
            return False
        return cls._get_hash(resolved) == recorded

    @classmethod
    def shutdown(cls):
        if cls.observer.is_alive():
            cls.observer.stop()
            cls.observer.join()
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
            shutdown_jedi_pool, shutdown_symbol_index_daemon)
        shutdown_jedi_pool()
        # Stops the warmer daemon + clears its process guard (a future
        # restart-in-place then starts a fresh one) and flushes the portable
        # symbol results to ~/.lsd/symbol_index.json for instant warm starts.
        shutdown_symbol_index_daemon()


class Melty:

    draw_state_registry = None
    style_manager: ImGuiStyleManager = None

    focused_ds = None
    text_focused_ds = None
    popover_focused_ds = None
    # id() of the front-most draw_state under the cursor within the open popover,
    # last frame - so begin_frame only re-runs the popover when the hovered row
    # changes (not every frame the pointer sits over one).
    _last_popover_hover = None
    # frame_count when a popover last opened - clear_focus grants it a one-frame
    # pass so the opening click can't immediately dismiss it.
    _popover_open_frame = 0
    # frame_count when text focus was last GRANTED (draw_text's request_focus /
    # rebind + click paths stamp this). clear_focus skips clearing a same-frame
    # grant: the click that opens a find bar / dropdown / context menu is routed
    # to window wrappers in the same frame the freshly-opened search box first
    # claims focus, and wrappers that run after the grant used to skip it (the
    # box is never under the opening click, so skip_this couldn't protect it).
    _text_focus_grant_frame = -99

    # Previous frame's imgui io.want_text_input - used to detect when an imgui
    # input widget newly captures the keyboard (rising edge), so a Melty text
    # editor and an imgui input_text never hold focus at once. See begin_frame.
    _prev_imgui_want_text = False
    selected = set()
    last_selected = None
    large_font = None
    font_mgr = None

    root_draw_states = defaultdict(lambda: list())
    root_draw_states_by_layer = defaultdict(lambda: list())

    # Callables posted from worker threads, drained on the render thread at
    # end_frame (_drain_render_tasks) - for work that must not race a frame
    # in progress, e.g. mutating a live view-model tree that frame walkers
    # iterate. post_to_render wakes the loop, so an idle thread drains promptly.
    _render_tasks = []
    _render_tasks_lock = _threading.Lock()

    filter = Filter()
    detached = False

    seen_values = []

    window_drag = False
    on_drag = False
    on_scroll = False
    on_scroll_buffer = deque(maxlen=5)
    last_scroll_time = 0

    mode_stack = []
    search_stack = []
    # Active data-source codec stack. core_render pushes a value's codec
    # (function / call-site / class / decorations) on entry and pops on exit,
    # mirroring mode_stack. Every draw_state in the subtree stashes the top as
    # ds._codec, so any descendant view can ask "which data source am I
    # rendering?" (e.g. to apply the codec's source tint prominently).
    codec_stack = []
    # The draw_state holding the current search match (set by the search owner's
    # pre-body walk). The find UI resolves this to a click target on Ctrl+Enter
    # (see new_core_view.search_activate_target) and injects a mouse-down there.
    search_current_node = None
    # (tile_id, InputEvent) queued by Ctrl+Enter to "click" the selected search
    # result. Applied in begin_frame, right after events are rebuilt and before
    # target renders, so the target reliably reads it (the find UI renders too
    # late in the frame to inject directly). One-shot.
    search_click_pending = None

    _converters = {}
    _converter_to_type = {}
    converter_flags_by_type = {}
    converter_flags = {}

    # Bumped on every REAL scroll_offset change (new_setattr in
    # invalidation_decoration); bumps the DrawState._ancestor_scroll memo so
    # mid-frame scroll deltas invalidate it without per-access parent walks.
    scroll_version = 0

    # list, full with 32 Nones
    max_depth = 32
    nested_layer_boost = 1
    top_layer_boost = 5
    max_layer = 64
    drag_layer = 31
    layers = []
    active_layer = 0
    active_layer_stack = []
    layer_inc = 1

    bg_depth = 0
    seen_unique = set()

    last_draw_state = [(None, None)] * max_layer
    collection_index_stack = []
    hovered_ds = None

    windows = []
    collection_stack = []
    glfw_window = None
    clip_stack = []
    clip_stack_holder = {}
    annotated_window_classes = {}
    registered_windows = defaultdict(lambda: ManagedWindow())
    # Self-registering RenderHost objects (id -> host). draw_main renders each one
    # in its own thread every frame; see view/core_conversion/render_host.py.
    render_hosts = {}
    scroll_stack = []
    tile_id_stack = []
    wrap_stack = []
    previous_select = None
    nested_window_refresh = None

    content_height_stack = []

    cursor = (0, 0)

    last_request_render = ""

    actions_to_apply = []

    init_window_cursor = (0, 0)

    last_invalid_attr = ""
    last_invalid = deque(maxlen=10)

    channels_split = False
    is_melty_window = False
    melty_window_stack = []
    default_font = None
    indent_size = 10
    annotation_mode = True
    # Per-THREAD annotation mode for recompile exec: re-running a class def
    # re-evaluates field annotations that CALL render funcs (`@int:
    # draw_any(...)`) - without interception they render for real on the
    # recompile's background thread (no GL context, FBO failure, imgui
    # ID-stack corruption on the render thread). Flipping the GLOBAL flag
    # would break the render thread mid-frame, so recompiles wrap their exec
    # in annotation_scope, which only the recompiling thread observes.
    _annotation_tls = _threading.local()
    depth = 0
    shadow_depth = 0
    wrapped_depth =0
    current_indent = 0
    indent_count = 0
    unindent_count = 0
    pending_move_to_front = None
    pending_delete_window = None
    imgui_popup_open = False
    imgui_active = False
    imgui_any_item_active = False
    imgui_active_pending = False
    imgui_main_window_hovered = False

    max_indent = 0
    hotkey_registry = {}
    move_draw_state_pending = {}

    # LibCST tracking -----------------------------------------
    _path_stack: list[tuple[str, int | None]] = []  # (field, idx)
    _root_by_module: dict[str, cst.Module] = {}
    _gen_by_module: dict[str, int] = {}

    last_attr = ""

    save_draw_state_for = 1
    spacing = (2, 1)
    padding = (2, 2)
    end_collection_spacing = 4
    collection_spacing = 2
    header_indent = 150

    vis = None
    imgui_crashed = False
    type_defaults = {}
    type_interrupts = {}
    default_view_functions = defaultdict(lambda: list())
    default_kwargs_by_type = defaultdict(lambda: dict())
    default_kwargs_by_attrib_type = defaultdict(lambda: defaultdict(lambda: dict()))

    default_funcs_by_type = defaultdict(lambda: None)
    default_funcs_by_name_type = defaultdict(lambda: defaultdict(lambda: list()))
    default_funcs_by_name = defaultdict(lambda: None)

    default_lenses_by_type = defaultdict(lambda: None)

    # Every @render_func wrapper, keyed by its own name (e.g. "draw_type").
    # Auto-populated by the decorator; the RenderFuncs accessor below resolves
    # against it lazily so modules can reference render_funcs by symbol without
    # importing the (often cycle-prone) module that defines them.
    render_funcs_by_name = {}

    silence_invalidate = False
    unique_stack = []
    suffix_stack = []
    size_stack = []
    window_stack = []
    window_hovered = False
    global_attrs = {}
    depth_state_stack = []
    flow_spacing = 0.0
    bg_stack = []
    bg_color_stack = []
    draw_state_stack = []
    input_value_stack = [None]
    window_enabled = True
    cache = None
    dirty_objects = set()
    all_dirty = False
    hovered_drawstate = set()
    hovered_drawstate_pending = set()
    frame_count = 0
    last_print_invalidate = 0

    # File-text cache keyed by resolved path name. Populated by read_code,
    # invalidated by FileWatch on external change. Lets the symbol-usage index
    # avoid re-reading the same source on every index pass.
    code_cache = {}

    blocker_hovered = False

    all_uniques = set()
    profiles_results = {}
    live_attributes = {}

    event_handler = InputHandler()
    backend = ImGuiBackend(event_handler)
    events = {}
    # Raw (glfw_key, mods) for PRESS/REPEAT recorded by the GLFW callback backend
    # since the last end_frame, in order. The focused text editor drains these
    # instead of polling imgui.is_key_pressed, so keystrokes aren't lost on slow
    # frames. Cleared in end_frame after this frame's views have read them.
    frame_key_events = []

    texture_manager = TextureManager()
    returned_values = {}
    pending_return_values = {}

    # Undo/redo: draw_state -> (value, ui) to restore. UndoManager.undo()/redo()
    # register an entry here on ctrl+z / ctrl+shift+z; core_render's render_func
    # tail intercepts the relevant draw_state's return, reports (True, value)
    # instead of the live value, restores UI (caret/selection/scroll), then pops
    # the entry so it fires for exactly one view.
    undo_requests = {}

    # Drag-and-drop reorders: collection draw_state -> op(collection) ->
    # (changed, collection). Registered by DragDrop._commit on drop; the same
    # wrapper tail that serves undo_requests pops the entry, applies the func to
    # the live collection and reports (True, reordered) so the parent writes
    # it back. See view/core_views/drag_drop.py.
    dnd_requests = {}

    # (x, y, w, h) of the dragged item's home slot while a drag is active,
    # else None. blit_offscreen checks this when blitting a cached tile that
    # contains the slot and repaints the blank socket live over the image -
    # the tile's pixels there can be incorrect (the dragged item overlapped
    # the slot when the tile was captured). Set/cleared by DragDrop.
    dnd_home_rect = None

    empty_event = InputEvent(input_id="", action="")
    events_by_type = {}
    pending_blockers = [None] * max_layer
    imgui_blockers = [None] * max_layer
    original_spacing = None
    original_window_padding = None
    original_frame_padding = None

    fixed_size_stack = []
    nested_collections = 0
    z_pos = 0
    any_window_hovered_pending = False
    any_window_hovered = False
    glfw_close_requested = False

    # BVH spatial index for draw states
    _bvh = rtree_index.Index()
    _bvh_next_id = 0
    _bvh_id_to_ds = {}
    # Bumped on every insert/delete that mutates the index. bvh_query memoizes
    # results by (x, y) and discards the memo whenever this changes. The index
    # mutates mid-frame (views call pos_changed as they render), so this keeps
    # the cached HIT SET exact - a memo only survives between two queries with
    # no intervening index change. The sort key (z_pos/closed) can shift without
    # an index mutation, so a reused result may carry a one-frame-stale ordering,
    # which is within Melty's existing frame-lag tolerance for hover/z-order.
    _bvh_gen = 0
    _bvh_query_cache = {}
    _bvh_query_cache_gen = -1

    # GL error-checking gate state (see _sync_gl_error_checking).
    _gl_check_applied = None       # Last-applied Toggles.gl_check_error value
    _gl_checker_default = None     # initial _registered value, captured once
    # id(ds) for every draw_state whose bbox is under the cursor this frame - a
    # begin_frame snapshot of bvh_query. hover_eligible / is_bounding_hovered do
    # O(1) membership against this instead of imgui.is_mouse_hovering_rect.
    bvh_hover_ids = set()

    # OS-window framebuffer size, stashed once per frame in begin_frame so
    # external code without imgui access (DrawState's nested-window position
    # cap) can read it. None until the first frame / in headless mode.
    display_size = None

    items_to_delete = []
    # Foreground/overlay channel routing. The overlay draw list is channel-split
    # into max_depth channels (like the window draw list); a view adds its
    # overlay to channel = layer_channel(draw_state.layer). The top channel is
    # the unmasked global default. The renderer uses _overlay_channel_ranges to
    # stencil-mask out higher-layer windows per channel during its deferred pass.
    _overlay_channels_active = False
    _overlay_channel_ranges: list = []
    _overlay_probe_logged = False
    _debug_overlay_test = True  # controlled sub-top overlay to verify masking

    @classmethod
    def read_code(cls, path):
        """File text via code_cache, invalidated by FileWatch on change. Returns
        None on read error. Use for repeated reads of the same source (e.g. the
        symbol-usage index) so an unchanged file isn't re-read every pass."""
        key = str(Path(path).resolve())
        text = cls.code_cache.get(key)
        if text is None:                       # absent (not an empty file)
            try:
                text = Path(key).read_text()
            except OSError:
                return None
            cls.code_cache[key] = text
        return text

    @classmethod
    def get_default_view_function(cls, draw_state=None, real_type=None, collection_type=None, attrib_key=None):
        if draw_state is not None:
            real_type = draw_state._kwargs.get("real_type", type(draw_state._input_value))
            collection_type = draw_state._kwargs.get("type_collection", type(draw_state._collection))
            attrib_key = draw_state._kwargs.get("key", draw_state._kwargs.get("name", None))

        # Names often carry an imgui id suffix ("tint##caller_3"); the default
        # registries are keyed by the bare attribute name, so match on the
        # part before the ## tag.
        if isinstance(attrib_key, str) and "##" in attrib_key:
            attrib_key = attrib_key.split("##", 1)[0]

        default_view_function = None

        # Most specific: a per-(collection_type, attribute) override registered
        # by viewdefaults or a field annotation (e.g. `test_tint: draw_tuple`).
        by_name_type = cls.default_funcs_by_name_type.get(collection_type)
        if by_name_type is not None:
            candidate = by_name_type.get(attrib_key)
            if callable(candidate):
                return candidate

        default_by_name = cls.default_funcs_by_name[attrib_key]
        default_by_type = cls.default_funcs_by_type[real_type]
        default_by_type_str = cls.default_funcs_by_name[real_type.__name__]

        # loop over super types
        super_types = real_type.__mro__[1:]
        for t in super_types:
            if default_by_type is not None:
                break
            default_by_type = cls.default_funcs_by_type[t]

        if default_by_name is not None:
            default_view_function = default_by_name
        elif default_by_type_str is not None:
            default_view_function = default_by_type_str

        elif default_by_type is not None:
            default_view_function = default_by_type

        return default_view_function

    # Single source of truth for a draw_state's presence in the rtree.
    #
    # Invariant: each draw_state owns AT MOST ONE BOX in the index, under a rid
    # assigned once for its lifetime (never reused, never dropped). `_bvh_bbox`
    # always mirrors EXACTLY the box currently stored under that rid, or None
    # when the draw_state has no box in the index. bvh_sync is the only writer,
    # so the mirror can't drift: it deletes the current box (matched by the rid,
    # which is why rtree.delete - which needs an exact inserted box - always
    # hits) before adding the new one, and records the new box.
    #
    # This replaces the old register/update/unregister trio, whose fresh-rid-
    # per-register and "set _bvh_bbox without inserting" paths let a single
    # draw_state accumulate several boxes (rtree allows duplicate ids), which
    # appeared as duplicated hits in bvh_query.
    @classmethod
    def bvh_sync(cls, draw_state):
        """Make the index hold exactly the draw_state's current box, or nothing.

        Idempotent and cheap: when the desired box equals what's already stored
        it returns without touching the index, so calling this every render only
        does work when abs_left/abs_top/width/height (i.e. `bbox`) — or the
        view's visibility — actually changed.

        The box is dropped (desired = None) when the view is off-screen
        (`inside_clip` False), closed (`closed`), or its rect is degenerate. So
        a closed or scrolled-out view clears itself the next time it syncs.

        Only `closed` (a cheap bool) is checked here, not `abs_closed`: a view
        hidden by a COLLAPSED ANCESTOR isn't rendered, so pos_changed never
        fires for it and this couldn't clear it regardless. bvh_query handles
        that case by filtering and lazily evicting abs_closed hits. Keeping the
        per-render path off the abs_closed parent-chain walk matters — this runs
        for every view every frame."""
        desired = draw_state.bbox
        if (desired is not None
                and draw_state.inside_clip
                and not draw_state.closed
                and desired[0] < desired[2] and desired[1] < desired[3]):
            pass  # keep desired
        else:
            desired = None

        current = draw_state._bvh_bbox
        if desired == current:
            return

        rid = draw_state._bvh_id
        if rid is None:
            rid = cls._bvh_next_id
            cls._bvh_next_id += 1
            draw_state._bvh_id = rid

        if current is not None:
            cls._bvh.delete(rid, current)
        if desired is not None:
            cls._bvh.insert(rid, desired)
            cls._bvh_id_to_ds[rid] = draw_state
        else:
            cls._bvh_id_to_ds.pop(rid, None)
        draw_state._bvh_bbox = desired
        cls._bvh_gen += 1

    @classmethod
    def bvh_evict(cls, draw_state):
        """Drop a draw_state's box from the index immediately, keeping its rid.

        Lazy GC for views that vanish WITHOUT re-rendering (a collapsed parent
        stops descending its children, so those children never sync themselves
        out). bvh_query calls this when it encounters such a hit. The rid is
        retained, so the view re-syncs cleanly if it ever reappears.

        Deliberately does NOT bump _bvh_gen: the caller only evicts hits it has
        already filtered out (closed/abs_closed), so removing them changes no
        query's result — and leaving gen alone keeps the query memo this frame
        valid instead of forcing a re-scan on the next identical query."""
        if draw_state._bvh_bbox is not None:
            cls._bvh.delete(draw_state._bvh_id, draw_state._bvh_bbox)
            draw_state._bvh_bbox = None
        cls._bvh_id_to_ds.pop(draw_state._bvh_id, None)

    @classmethod
    def _resolve_channel_command_ranges(cls, overlay, idx_boundaries):
        """Partition the merged index buffer into per-channel index ranges.

        Returns a list of (channel_idx, idx_lo, idx_hi) for each non-empty
        channel, where [idx_lo, idx_hi) are positions in the merged index
        buffer. We work in index space — not command space — because
        ChannelsMerge fuses adjacent channels' draws into a single command when
        their clip rect + texture match. A whole-command assignment would then
        lump every channel's indices onto one channel; index ranges stay
        correct regardless of fusion, and the renderer splits commands at these
        boundaries."""
        ranges = []
        prev = 0
        for ch, boundary in enumerate(idx_boundaries):
            if boundary > prev:
                ranges.append((ch, prev, boundary))
            prev = boundary
        return ranges

    @classmethod
    def layer_channel(cls, layer) -> int:
        """Map a draw_state layer to its overlay draw-list channel, using the
        same clamp as the window draw list. The top channel (max_depth - 1) is
        the unmasked global channel; the renderer masks channel C with every
        window whose layer_channel is greater than C."""
        return max(0, min(int(layer), cls.max_depth - 1))

    @classmethod
    def bvh_query(cls, x, y):
        """Hit test — returns all DrawStates under the point.

        Memoized per (x, y) and invalidated whenever the index mutates (via
        _bvh_gen), so the many identical cursor queries issued across a single
        frame — hover-suppression fires one per bounding-hovered view — collapse
        onto a single rtree.intersection call. Callers must treat the returned
        list as read-only (it is shared across cache hits); all current callers
        only iterate or index it."""
        if cls._bvh_query_cache_gen != cls._bvh_gen:
            cls._bvh_query_cache = {}
            cls._bvh_query_cache_gen = cls._bvh_gen
        key = (x, y)
        if key in cls._bvh_query_cache:
            return cls._bvh_query_cache[key]

        # bvh_sync's one-box-per-rid invariant means intersection won't return a
        # rid twice - but dedup by rid anyways as a cheap, exact safety net (a
        # plain set, not the old O(n^2) `ds in hits` scan).
        hits = []
        stale = []
        seen_rids = set()
        for rid in cls._bvh.intersection((x, y, x, y)):
            if rid in seen_rids:
                continue
            seen_rids.add(rid)
            ds = cls._bvh_id_to_ds.get(rid)
            if ds is None:
                continue
            # A view that closed/collapsed without re-rendering still has a stale
            # box here. Filter it out, and lazily evict so it stops being hit.
            if ds.closed or ds.abs_closed:
                stale.append(ds)
                continue
            # A nested window hidden because its spawner scrolled out of sight
            # (end frame's dispatch rebuild) keeps its geometry and BVH boxes -
            # they must NOT be evicted, or its blit-cached widgets would stay
            # hover-dead when it unhides (they only re-sync when they actually
            # re-render). Just skip hits inside any hidden window up the
            # parent_window chain while the flag is set.
            node, hidden = ds, False
            for _ in range(32):
                if node is None:
                    break
                if getattr(node, '_hidden_offscreen', False):
                    hidden = True
                    break
                nxt = node.parent_window
                if nxt is node:
                    break
                node = nxt
            if hidden:
                continue
            # The index stores full, UNCLIPPED bboxes, so a view scrolled partly
            # out of its parent still matches over its hidden region. Reject the
            # hit when the point lies outside the view's visible (clipped) region.
            cl, ct, cr, cb = ds.abs_clip_rect
            if not (cl <= x <= cr and ct <= y <= cb):
                continue
            hits.append(ds)

        for ds in stale:
            cls.bvh_evict(ds)

        hits.sort(key=lambda ds: ds.z_pos or 0, reverse=True)

        cls._bvh_query_cache[key] = hits
        return hits

    @classmethod
    def clear_focus(cls, not_this=None):
        # Clear all focus slots (text / popover / general) EXCEPT any owner that is
        # an ancestor of `not_this`. `not_this` is the view(s) just interacted with
        # (e.g. the bvh hit-stack under the cursor on mouse-up). We protect each
        # seed's full ANCESTOR CLOSURE - including both _parent and parent_window -
        # so clicking anywhere inside a focus owner's subtree keeps it focused even
        # when the seed is several windows up (a dropdown's search box lives in its
        # menu window, whose parent_window is the dropdown trigger that holds the
        # true focus). Protecting only the direct parents dropped that owner,
        # which closed the dropdown / killed search search + arrow input on any click.
        if isinstance(not_this, (list, tuple)):
            seeds = [n for n in not_this if n is not None]
        elif not_this is not None:
            seeds = [not_this]
        else:
            seeds = []

        protect = set()
        for seed in seeds:
            stack = [seed]
            guard = 0
            while stack and guard < 256:
                guard += 1
                node = stack.pop()
                if node is None or getattr(node, "id", None) in protect:
                    continue
                protect.add(node.id)
                parent = getattr(node, "_parent", None)
                pwin = getattr(node, "parent_window", None)
                if parent is not None and parent is not node:
                    stack.append(parent)
                if pwin is not None and pwin is not node:
                    stack.append(pwin)

        # A just-opened popover gets a one-frame grace: the very click that opens
        # it also fires clear_focus, and the opener (e.g. a tiny colour swatch) may
        # not be the bvh hit, so it wouldn't be in `protect`. Without the grace the
        # popover would close on the same click that opened it. We do NOT protect a
        # popover owner's _parent (its containing window) - that kept popovers open when
        # clicking the parent window, defeating click-outside-to-dismiss.
        popover_grace = (cls.frame_count - getattr(cls, "_popover_open_frame", -99)) <= 1

        # Same-frame text-focus grace: a grant stamped THIS frame outlives the
        # click being processed this frame (the click physically happened before
        # the grant, e.g. it's the very click that opened the search box now
        # claiming focus). Clears triggered by later clicks run in later frames
        # and proceed normally.
        text_grace = cls.frame_count == getattr(cls, "_text_focus_grant_frame", -99)

        for ds in (cls.focused_ds, cls.text_focused_ds, cls.popover_focused_ds):

            if ds is None or ds.id in protect:
                continue
            if ds is cls.popover_focused_ds and popover_grace:
                continue
            if ds is cls.text_focused_ds and text_grace:
                continue
            if Toggles.text_focus_stack_trace:
                print_stack_trace(size=5)

            ds.search_active = False
            ds.search_text = ""
            ds._search_was_active = False
            ds.invalidate_up()

            if cls.focused_ds is ds:
                cls.focused_ds = None
            if cls.text_focused_ds is ds:
                cls.text_focused_ds = None
            if cls.popover_focused_ds is ds:
                cls.popover_focused_ds = None



    @classmethod
    def _sync_gl_error_checking(cls):
        """Honor Toggles.gl_check_error, disabling PyOpenGL's per-call
        glGetError round-trip when off (a render-thread hotspot).

        Every GL wrapper shares one _ErrorChecker and calls its _currentChecker
        after each GL call. Swapping that to nullGetError suppresses the driver
        round-trip across all built and future functions at once — live and
        independent of import order, unlike the build-time OpenGL.ERROR_CHECKING
        flag. Re-checked each frame (a bool compare + early return) so the
        toggle takes effect at runtime. Best-effort: any failure leaves GL
        checking in its current state."""
        want = bool(Toggles.gl_check_error)
        if want == cls._gl_check_applied:
            return
        try:
            from OpenGL.raw.GL import _errors
            ec = _errors._error_checker
            if ec is None:
                return
            # Capture the default checker once, before the first swap, so enabling
            # restores the exact original (safeGetError or _getErrors).
            if cls._gl_checker_default is None:
                cls._gl_checker_default = ec._registeredChecker
            ec._registeredChecker = cls._gl_checker_default if want else ec.nullGetError
            ec._currentChecker = ec._registeredChecker
            # Keep newly-built wrappers consistent with the live state.
            import OpenGL
            OpenGL.ERROR_CHECKING = want
            cls._gl_check_applied = want
        except Exception:
            pass

    @classmethod
    def begin_frame(cls):
        cls._sync_gl_error_checking()
        cls.unique_stack = []
        cls.draw_state_stack = []
        cls.flow_spacing = 0.0
        cls.indent_count = 0
        cls.unindent_count = 0

        if cls.draw_state_registry is None:
            cls.draw_state_registry = cls.vis.root.draw_state_registry

        style = imgui.get_style()
        style.frame_rounding = 5.0
        style.item_spacing = (5, 0)
        style.window_padding = (3, 0)
        style.frame_padding = (4, 1)

        cls.any_window_hovered = cls.any_window_hovered_pending
        cls.any_window_hovered_pending = False
        style = imgui.get_style()
        cls.seen_unique = set()
        cls.original_spacing = style.item_spacing
        cls.original_window_padding = style.window_padding
        cls.original_frame_padding = style.frame_padding

        cls.returned_values.update(cls.pending_return_values)
        cls.pending_returned_values = {}

        Counters.nested_window_count = 0

        if cls.glfw_close_requested:
            cls.event_handler.feed_down(input_id="glfw_close", x=0, y=0, t=time.perf_counter())
            cls.glfw_close_requested = False

        cls.layer_inc = 0.04 / ((Melty.max_layer - 1.0) * (Melty.max_depth - 1.0)) * 65535.0

        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)
        is_popup_open = imgui.is_popup_open("", flags=imgui.POPUP_ANY_POPUP)
        Melty.imgui_popup_open = is_popup_open

        # --- Melty/imgui text-focus mutual exclusion (reverse direction) ---
        # A Melty draw_text editor (Melty.text_focused_ds) and an imgui
        # input_text must never both own the keyboard. The forward case - a
        # click exits a Melty editor - is handled by imgui itself (the click
        # lands outside the active input, so imgui deactivates it next frame).
        # The reverse case isn't: clicking an imgui input_text leaves the old
        # Melty editor focused, so we drain keystrokes. Detect imgui newly
        # capturing text (io.want_text_input rising edge - only an imgui input
        # raises it; Melty editors aren't imgui items) and clear Melty focus.
        # Edge-triggered, not level: a level check would re-clear Melty focus
        # for the one frame after a forward click while imgui is still shutting
        # down its old input, so the editor could never keep focus.
        want_text = imgui.get_io().want_text_input
        if want_text and not cls._prev_imgui_want_text and cls.text_focused_ds is not None:
            cls.text_focused_ds = None
            if Toggles.text_focus_stack_trace:
                print_stack_trace()
        cls._prev_imgui_want_text = want_text

        # Route the keyboard to the focused text view. While a text editor holds
        # focus, any held key force-invalidates its tile (and its parent window
        # subtree, so the cached window re-descends into the editor) BEFORE the
        # views draw this frame. That lets draw_text re-execute and catch
        # imgui's is_key_pressed edge in the SAME frame the key goes down, even
        # when the mouse isn't hovering. This must run in begin_frame, not
        # end_frame: end_frame invalidation lands one frame too late, after the
        # key edge has already passed, which is why typing only worked while
        # hovering (the hover path keeps the tile dirty before each draw).
        focused = None
        if cls.text_focused_ds is not None and cls.glfw_window is not None:
            focused = cls.text_focused_ds
            # Esc releases text focus globally - no more needed. Re-render the
            # (now-)focused text view so its cursor disappears this frame, then
            # clear focus, which also unblocks global hotkeys via is_key_pressed.
        if glfw.get_key(cls.glfw_window, glfw.KEY_ESCAPE) == glfw.PRESS:
            # Close any active search globally - no hover required. We must
            # clear search_active (not just text focus): otherwise the search
            # box's Esc-grab-when-unfocused logic would immediately reclaim
            # focus and the find bar would never dismiss off-screen.

            cls.clear_focus()

            if Toggles.text_focus_stack_trace:
                print_stack_trace()
            request_render()
        else:
            for k in range(32, 349):  # GLFW_KEY_SPACE through GLFW_KEY_LAST
                if glfw.get_key(cls.glfw_window, k) == glfw.PRESS:
                    if focused is not None:
                        note = Note(name="Melty, on glfw key press", tint=(1, 0.5, 0), rect=(0, 0, 100, 20))
                        if focused.parent_window is not None:
                            cls.cache.invalidate_up(focused.parent_window._tile_id, force=True, note=note)
                        note = Note(name="Melty, on glfw key press", tint=(1, 0.5, 0), rect=(0, 0, 100, 20))
                        cls.cache.invalidate(focused._tile_id, force=True, note=note)
                        request_render()
                        break

        # Open dropdown popover: re-run its owning view on the EVENTS it responds to
        # rather than every frame - (a) a navigation key is down (Esc/arrows/Enter,
        # delivered with no delay), or (b) the text view under the cursor inside
        # the popover changed (the pointer moved to a different row, so a sub-menu
        # should open/close). The cached owner wouldn't otherwise re-descend into
        # its un-cached menu. A still pointer + no keys repaints nothing. (Typing
        # into the search box is handled by the text-focus block above, which
        # invalidates up to this view through the box's parent.)
        if cls.popover_focused_ds is not None and cls.glfw_window is not None:
            pop = cls.popover_focused_ds
            _need = False
            _nav_keys = (glfw.KEY_ESCAPE, glfw.KEY_UP, glfw.KEY_DOWN,
                         glfw.KEY_LEFT, glfw.KEY_RIGHT, glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
            # Work off the GLFW windowed key QUEUE (frame_key_events) - the same
            # source the popover reads - not glfw.get_key level state. A fast Esc /
            # Enter key is pressed-and-released between frames, so the level check
            # misses it and the popover never re-runs to handle it (arrows survive
            # only/c they're held); the queue keeps every press. glfw.get_key
            # stays as a fallback so a held key keeps re-rendering.
            if any(k in _nav_keys for k, _ in cls.frame_key_events):
                _need = True
            elif any(glfw.get_key(cls.glfw_window, k) == glfw.PRESS for k in _nav_keys):
                _need = True

            if _need:
                if pop.parent_window is not None:
                    cls.cache.invalidate_up(pop.parent_window._tile_id, force=True)
                cls.cache.invalidate_up(pop._tile_id, force=True)


                request_render()

        mouse_pos = imgui.get_mouse_pos()
        ds_under_mouse = Melty.bvh_query(mouse_pos[0], mouse_pos[1])
        cls.bvh_hover_ids = {id(ds) for ds in ds_under_mouse}

        last_hovered = cls.hovered_ds
        cls.hovered_ds = ds_under_mouse[0] if ds_under_mouse else None
        # for ds in ds_under_mouse:
        #     ds._hover_eligible = Melty.frame_count
        #
        # if cls.hovered_ds is not None and not cls.on_drag:
        #     if (not cls.on_drag and not imgui.is_mouse_down(2)):
        #         Melty.cache.invalidate(cls.hovered_ds._parent._tile_id, do_store=False, force=True)

        hovered_id = id(cls.hovered_ds) if cls.hovered_ds is not None else None
        last_hovered_id = id(last_hovered) if last_hovered is not None else None
        #
        # if hovered_id != last_hovered_id:
        #     if last_hovered is not None:
        #         Melty.cache.invalidate(last_hovered, do_store=False, force=True)

        cls.backend.pump()

        cls.last_draw_state = [(None, None)] * cls.max_layer

        cls.imgui_active = cls.imgui_active_pending
        cls.imgui_active_pending = False
        cls.bg_depth = 0

        cls.imgui_blockers = cls.pending_blockers
        cls.pending_blockers = [None] * cls.max_layer

        cls.events, cls.events_by_type = cls.event_handler.process_frame()

        # Apply a Ctrl+Enter "click the selected search result" injection queued
        # last frame - now, before any view renders, so the view reads it via
        # the per-view event merge. Add two names so it reaches whichever the
        # target view reads (button: left_mouse_down; managed window: mouse_down).
        if cls.search_click_pending is not None:
            _click_tid, _click_ev = cls.search_click_pending
            cls.search_click_pending = None
            _click_slot = cls.events.setdefault(_click_tid, {})
            _click_slot["left_mouse_down"] = _click_ev
            _click_slot["mouse_down"] = _click_ev

        cls.window_drag = ((("left_mouse_drag" in cls.events_by_type) or ("left_mouse_held" in cls.events_by_type)) or
                           (("right_mouse_drag" in cls.events_by_type) or ("right_mouse_held" in cls.events_by_type)))

        left_mouse_drag_event = cls.events_by_type.get("left_mouse_drag", None)
        right_mouse_drag_event = cls.events_by_type.get("right_mouse_drag", None)
        if right_mouse_drag_event is not None:
            is_window_resize = "window_resize" in str(right_mouse_drag_event.keys())
        else:
            is_window_resize = False

        if left_mouse_drag_event is not None:
            # dnd_item: an item drag-and-drop gesture gets the same churn
            # suppression (hover invalidation, content-height changes, scroll
            # clamps) as a window drag - both ride the blit fast path.
            is_window_drag = ("window_move" in str(left_mouse_drag_event.keys())
                              or "corner_drag" in str(left_mouse_drag_event.keys())
                              or "dnd_item" in str(left_mouse_drag_event.keys()))
        else:
            is_window_drag = False

        cls.on_drag = (is_window_drag or is_window_resize or ("left_mouse_down" in cls.events_by_type)) and (not cls.imgui_active)

        cls.event_handler.begin_frame()

        if ("right_mouse_drag" in cls.events_by_type):
            right_mouse_drag_events = cls.events_by_type["right_mouse_drag"]
            for event in right_mouse_drag_events:
                Melty.cache.invalidate(event)

        if ("middle_mouse_drag" in cls.events_by_type):
            right_mouse_drag_events = cls.events_by_type["middle_mouse_drag"]
            for event in right_mouse_drag_events:
                note = Note(name=event, reason="middle_mouse_drag", tint=(0, 1, 1))
                Melty.cache.invalidate(event, note=note)

        event_keys = list(cls.events.keys())
        # To string
        event_keys_str = [str(k) for k in event_keys]
        concat_names = "_".join(event_keys_str)

        cls.on_scroll_buffer.append("scroll_y_changed" in cls.events_by_type and "view_scroll" in concat_names)
        cls.on_scroll = any(cls.on_scroll_buffer)


        # for view_id, evts in cls.events.items():
        #     for e in evts:
        #         print(f"  {view_id}: {e.input_id}:{e.action}")

        # cls.texture_manager.upload_pending()

        # # Check live attributes
        # for obj, attributes in cls.live_attributes.items():
        #     for attrib in attributes:
        #         try:
        #             new_value = getattr(obj, attrib)
        #         except Exception:
        #             continue

        Melty.bg_stack = [(0, 0, 0)]

        Melty.active_layer = 0
        cls.frame_count += 1
        cls.blocker_hovered = False

        cls.layers.clear()
        for _ in range(cls.max_layer * 2):
            cls.layers.append([])
        # Handle global hotkeys
        # for hotkey, target in global_hotkeys.items():
        #     if Melty.is_key_pressed(hotkey.key):
        #         if callable(target):
        #             target()

        for view_id, evts in cls.events.items():
            first_event = list(evts.values())[0]
            if first_event.tile_id != "hovered":
                if (first_event.tile_id is not None and not imgui.is_mouse_down(0) and not imgui.is_mouse_down(1)
                        and not imgui.is_mouse_down(2) and not cls.on_scroll):
                        print(first_event)
                        Melty.cache.invalidate_up(first_event.tile_id, max_depth=10, force=True)

        Melty.all_uniques = set()

        Melty.hovered_drawstate_pending = set()

        Melty.clip_stack = []
        # cls._root_by_module[module_id] = root
        # cls._gen_by_module.setdefault(module_id, 0)
        # cls._path_stack.clear()
        fb_w, fb_h = map(int, imgui.get_io().display_size)  # or your true GL FB size if HiDPI
        cls.display_size = (fb_w, fb_h)
        cls.cache.mask_begin_frame((fb_w, fb_h))

        # Channel-split the foreground/overlay draw list the same way as the
        # window draw list (max_depth channels) so per-window overlays can be
        # stencil-masked by higher-layer windows during the deferred pass.
        # Channel = Melty.layer_channel(draw_state.layer). The top channel is
        # the unmasked default, so global overlays appear on top.
        overlay = imgui.get_overlay_draw_list()
        overlay.channels_split(Melty.max_layer)
        overlay.channels_set_current(Melty.max_layer - 1)
        cls._overlay_channels_active = True
        cls._overlay_channel_ranges = []

        from src.lsd.gl_gui.view.core_views.core_render_helpers import clear_floating_text_cache
        clear_floating_text_cache()

    @staticmethod
    def _closest_perimeter_points(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
        """Connection points anchored at the center of the rects' shared edge.

        On each axis the connection coordinate is the center of the overlap span
        between the two rects, then clamped onto each rect. Where the rects
        overlap this lands the connector on the midpoint of their shared edge;
        where they don't it slides out to the facing edges/corners. The overlap
        bounds use a smooth min/max (window Swoosh.edge_softness) so the
        anchor glides as the overlap region changes instead of snapping at the
        kinks of hard min/max.

        When the two rects overlap on both axes the shared-edge center lands
        *inside* their intersection rect — under the (on-top) child, hiding the
        parent's point and forcing the connector across the child. If
        Swoosh.avoid_intersection is on, both endpoints are then slid along their
        own rect's edge, out of the intersection rect, to flank a reentrant
        corner of the union (a corner of the intersection where a parent edge
        meets a child edge). The returned control point bows the curve out
        through that corner into the exterior, so the line hugs the outside of
        the overlap instead of crossing either view. The slide is bounded by the
        exposed edge length, so it degrades to a short hook on heavy overlap and
        falls back to the plain anchor under full containment.

        As the overlap deepens from zero, the endpoints ease from the plain
        shared-edge anchor to the slid corner positions over Swoosh.intersect_soft
        px (and the control point blends with the default bow over the same
        window) so entering the overlap doesn't snap between modes.

        Returns (x0, y0, x1, y1, ctrl, g) where ctrl is a (cx, cy) bezier control
        point (None to use the default perpendicular bow) and g in [0, 1] is the
        blend weight of the overlap mode (the caller blends the bow by g).
        """
        k = Swoosh.edge_softness

        def smin(a, b):
            # Polynomial smooth-min: blends within a window of width k.
            if k <= 0.0:
                return a if a < b else b
            h = max(k - abs(a - b), 0.0) / k
            return (a if a < b else b) - h * h * k * 0.25

        def smax(a, b):
            return -smin(-a, -b)

        def clamp(v, lo, hi):
            return lo if v < lo else hi if v > hi else v

        # Center of the (smoothed) overlap span on each axis = shared-edge center.
        cx = (smax(ax0, bx0) + smin(ax1, bx1)) * 0.5
        cy = (smax(ay0, by0) + smin(ay1, by1)) * 0.5

        # Disjoint anchor (the g=0 end of the transition): plain shared-edge
        # center clamped onto each rect.
        dax, day = clamp(cx, ax0, ax1), clamp(cy, ay0, ay1)
        dbx, dby = clamp(cx, bx0, bx1), clamp(cy, by0, by1)

        if not Swoosh.avoid_intersection:
            return dax, day, dbx, dby, None, 1.0

        # Hard intersection rect of the two rects (the region under the child).
        ix0, iy0 = max(ax0, bx0), max(ay0, by0)
        ix1, iy1 = min(ax1, bx1), min(ay1, by1)
        wx, wy = ix1 - ix0, iy1 - iy0
        if wx <= 0.0 or wy <= 0.0:
            # No 2-D intersection: the shared-edge center is already outside.
            return dax, day, dbx, dby, None, 1.0

        tol = 1e-6
        # Which rect owns each intersection edge (an edge may be shared).
        xL_P, xL_C = abs(ix0 - ax0) < tol, abs(ix0 - bx0) < tol
        xR_P, xR_C = abs(ix1 - ax1) < tol, abs(ix1 - bx1) < tol
        yT_P, yT_C = abs(iy0 - ay0) < tol, abs(iy0 - by0) < tol
        yB_P, yB_C = abs(iy1 - ay1) < tol, abs(iy1 - by1) < tol

        # Reentrant corners of the union: an intersection corner where one rect
        # owns the x-edge and the other owns the y-edge (so a parent edge meets a
        # child edge there). The exterior opens up outside such a corner.
        corners = []
        for ex, exP, exC, xv in (("L", xL_P, xL_C, ix0), ("R", xR_P, xR_C, ix1)):
            for ey, eyP, eyC, yv in (("T", yT_P, yT_C, iy0), ("B", yB_P, yB_C, iy1)):
                p_owns_x = exP and eyC   # parent owns x-edge, child owns y-edge
                p_owns_y = exC and eyP   # child owns x-edge, parent owns y-edge
                if p_owns_x or p_owns_y:
                    corners.append((ex, ey, xv, yv, p_owns_x))
        if not corners:
            # Full containment - no exterior notch (no reentrant corner exists).
            if bx0 <= ax0 and by0 <= ay0 and bx1 >= ax1 and by1 >= ay1:
                # Parent is completely under the (on-top) child: hide the connector.
                return None
            # Child sits inside the parent: anchor to matching edges on the side
            # where it sits closest - left↔left / right↔right when closest
            # horizontally, top↔top / bottom↔bottom when vertically - blending
            # between the two as it rounds a corner. Each endpoint is the exit of a
            # ray cast from its rect's centre through the blended edge target, so
            # the points slide smoothly along the corners (the default slope bow
            # then curves the diagonal as in the non-overlap case).
            acx, acy = (ax0 + ax1) * 0.5, (ay0 + ay1) * 0.5
            bcx, bcy = (bx0 + bx1) * 0.5, (by0 + by1) * 0.5
            # Always anchor to the left and bottom edges so the connection never
            # flips left↔right or top↔bottom as the child crosses centre.
            h_px, h_cx, gh = ax0, bx0, bx0 - ax0   # left edge
            v_py, v_cy, gv = ay1, by1, ay1 - by1   # bottom edge
            # Prefer horizontal edges: pin s to 0 (horizontal/left edge) or 1
            # (vertical/bottom edge), only opening the diagonal blend window when
            # the child is actually near the corner - i.e. the nearer gap is
            # within a zone (Swoosh.envelop_corner × the parent's shorter side).
            # Far from the corner the window collapses, so the line stays flat
            # until it is near to rounding the corner.
            raw = gh / (gh + gv + 1e-6)
            zone = Swoosh.envelop_corner * min(ax1 - ax0, ay1 - ay0)
            m = gh if gh < gv else gv
            prox = (1.0 - m / zone) if (zone > 1e-6 and m < zone) else 0.0
            prox = prox * prox * (3.0 - 2.0 * prox)
            w = Swoosh.envelop_tie * prox
            t = (raw - 0.5 + w) / (2.0 * w) if w > 1e-6 else (1.0 if raw >= 0.5 else 0.0)
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
            s = t * t * (3.0 - 2.0 * t)

            def ray(cx0, cy0, rx0, ry0, rx1, ry1, dx, dy):
                tx = (rx1 - cx0) / dx if dx > 1e-9 else (rx0 - cx0) / dx if dx < -1e-9 else float("inf")
                ty = (ry1 - cy0) / dy if dy > 1e-9 else (ry0 - cy0) / dy if dy < -1e-9 else float("inf")
                t = tx if tx < ty else ty
                return cx0 + dx * t, cy0 + dy * t

            xa, ya = ray(acx, acy, ax0, ay0, ax1, ay1,
                         (h_px - acx) * (1 - s) + (bcx - acx) * s,
                         (bcy - acy) * (1 - s) + (v_py - acy) * s)
            xb, yb = ray(bcx, bcy, bx0, by0, bx1, by1,
                         (h_cx - bcx) * (1 - s), (v_cy - bcy) * s)
            return xa, ya, xb, yb, None, 1.0

        icx, icy = (ix0 + ix1) * 0.5, (iy0 + iy1) * 0.5
        acx, acy = (ax0 + ax1) * 0.5, (ay0 + ay1) * 0.5
        bcx, bcy = (bx0 + bx1) * 0.5, (by0 + by1) * 0.5
        ddx, ddy = bcx - acx, bcy - acy

        # Pick the reentrant corner continuously: most counter-clockwise from the
        # parent->child vector. Rotates as the child is dragged; only flips when
        # the rects are (anti)concentric or the overlap changes corner/edge type.
        best = None
        for cn in corners:
            key = ddx * (cn[3] - icy) - ddy * (cn[2] - icx)
            if best is None or key > best[0]:
                best = (key, cn)
        ex, ey, xv, yv, p_owns_x = best[1]

        hook = Swoosh.intersect_hook

        def lim(avail):
            return max(0.0, min(hook, avail))

        if p_owns_x:
            # parent on the vertical x=xv edge; child on the horizontal y=yv edge
            hp = lim(iy0 - ay0 if ey == "T" else ay1 - iy1)
            nax, nay = xv, (iy0 - hp if ey == "T" else iy1 + hp)
            hc = lim(ix0 - bx0 if ex == "L" else bx1 - ix1)
            nbx, nby = (ix0 - hc if ex == "L" else ix1 + hc), yv
        else:
            # parent on the horizontal y=yv edge; child on the vertical x=xv edge
            hp = lim(ix0 - ax0 if ex == "L" else ax1 - ix1)
            nax, nay = (ix0 - hp if ex == "L" else ix1 + hp), yv
            hc = lim(iy0 - by0 if ey == "T" else by1 - iy1)
            nbx, nby = xv, (iy0 - hc if ey == "T" else iy1 + hc)

        # Control point: bow out through the corner, away from the overlap centre.
        dx, dy = xv - icx, yv - icy
        n = math.hypot(dx, dy) or 1.0
        push = max(hp, hc) * 0.9
        ctrl = (xv + dx / n * push, yv + dy / n * push)

        # Ease from the shared-edge anchor to the slid corner anchor as the
        # overlap deepens. Both coordinates are interpolated; the caller projects
        # the result back onto the rect's (rounded) perimeter so the endpoint
        # slides along the edge and around corners instead of cutting across.
        soft = Swoosh.intersect_soft
        depth = min(wx, wy)
        if soft <= 0.0:
            g = 1.0
        else:
            t = depth / soft
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
            g = t * t * (3.0 - 2.0 * t)
        xa = dax + (nax - dax) * g
        ya = day + (nay - day) * g
        xb = dbx + (nbx - dbx) * g
        yb = dby + (nby - dby) * g
        return xa, ya, xb, yb, ctrl, g

    @staticmethod
    def _round_rect_point(px, py, x0, y0, x1, y1, r):
        """Project a point on a rect's square boundary onto its rounded-corner
        boundary, so the connector meets the visible (rounded) edge instead of
        sitting just off the square corner. Points on the straight portions of
        edges are left unchanged."""
        if r <= 0.0:
            return px, py
        # Corner-arc center: clamp into the inner box inset by r. On a straight
        # edge this stays level with the point (no shift); near a corner it pins
        # to the arc center, and we reproject the point onto that arc.
        cxc = x0 + r if px < x0 + r else (x1 - r if px > x1 - r else px)
        cyc = y0 + r if py < y0 + r else (y1 - r if py > y1 - r else py)
        dx, dy = px - cxc, py - cyc
        d = math.hypot(dx, dy)
        if d > r and d > 1e-6:
            return cxc + dx / d * r, cyc + dy / d * r
        return px, py

    @staticmethod
    def _project_to_rounded_rect(px, py, x0, y0, x1, y1, r):
        """Project an arbitrary point onto the nearest point of a rect's
        rounded-corner perimeter. A rounded rect is the inset box [+r] expanded
        by r, so the nearest boundary point is the nearest point of the inset box
        pushed out by r along the offset direction. Used to keep a connection
        endpoint on the visible edge while both of its coordinates interpolate."""
        ix0, iy0, ix1, iy1 = x0 + r, y0 + r, x1 - r, y1 - r
        if ix1 < ix0:
            ix0 = ix1 = (x0 + x1) * 0.5
        if iy1 < iy0:
            iy0 = iy1 = (y0 + y1) * 0.5
        qx = ix0 if px < ix0 else ix1 if px > ix1 else px
        qy = iy0 if py < iy0 else iy1 if py > iy1 else py
        dx, dy = px - qx, py - qy
        d = math.hypot(dx, dy)
        if d > 1e-6:
            # Outside the inset box: ride the rounded boundary at radius r.
            return qx + dx / d * r, qy + dy / d * r
        # Inside the inset box: drop straight out to the nearest straight edge.
        dl, dr, dt, db = px - x0, x1 - px, py - y0, y1 - py
        m = min(dl, dr, dt, db)
        if m == dl:
            return x0, py
        if m == dr:
            return x1, py
        if m == dt:
            return px, y0
        return px, y1

    @staticmethod
    def _highlight_rgb(tint=None):
        """Super-bright version of a view's tint, used for the nested-view
        highlight (swoosh + outline boxes). Pass the tint stashed on the
        draw_state at draw time (draw_state.current_tint): by this post-draw
        pass the style manager no longer holds it. Falls back to the live tint,
        then to a static tint, when nothing was stashed."""
        sm = Melty.style_manager
        if sm is None:
            return Swoosh.tint
        if tint is None:
            tint = sm.get_tint()
        return sm.make_custom(*tint, Swoosh.value,
                              saturation_scale=Swoosh.saturation)[:3]

    @staticmethod
    def _saturated_rgb(tint=None):
        """Super-bright version of a view's tint, used for the nested-view
        highlight (swoosh + outline boxes). Pass the tint stashed on the
        draw_state at draw time (draw_state.current_tint): by this post-draw
        pass the style manager no longer holds it. Falls back to the live tint,
        then to a static tint, when nothing was stashed."""
        sm = Melty.style_manager
        if sm is None:
            return Swoosh.tint
        if tint is None:
            tint = sm.get_tint()
        return sm.make_custom(*tint,
                              saturation_scale=1.0, value=0.6)[:3]
    @staticmethod
    def _lerp_rgb(a, b, t):
        """Plain RGB lerp for the parent->child connector gradient."""
        return (a[0] + (b[0] - a[0]) * t,
                a[1] + (b[1] - a[1]) * t,
                a[2] + (b[2] - a[2]) * t)

    @staticmethod
    def _draw_ribbon(overlay_dl, px0, py0, px1, py1, nx0, ny0, nx1, ny1,
                     rgb, rgb2=None, p_round=0.0, n_round=0.0):
        """Thick-ribbon connector: instead of the thin tapered line, bridge
        the two views' facing edges with a full band. Each end of the band
        sits on the straight (un-rounded) portion of its view's facing edge,
        centered on the shared overlap span, and is sized from its OWN edge
        length (Swoosh.ribbon_coverage of it, capped by ribbon_max_width) —
        so a small child hanging off a big parent gets a funnel, wide at the
        parent and narrow at the child. When the views are offset the two
        bands land at different positions and the band's boundary curves
        s-curve between them (cubics with tangents perpendicular to the
        edges — the classic node-link shape). Returns True when drawn; False
        when the rects overlap (no facing gap to bridge) or an edge is all
        corner, so the caller falls back to the thin line. Tunables:
        Swoosh.ribbon_*."""
        gap_r, gap_l = nx0 - px1, px0 - nx1
        gap_b, gap_t = ny0 - py1, py0 - ny1
        gx, gy = max(gap_r, gap_l), max(gap_b, gap_t)
        if gx <= 0.0 and gy <= 0.0:
            return False

        # Bridge along the axis with the wider gap. ep/ec are the two facing
        # edge coordinates on that axis; lo/hi bound the straight portion of
        # the facing edge (inset by its corner radius) on the other axis.
        if gx >= gy:
            ep, ec = (px1, nx0) if gap_r >= gap_l else (px0, nx1)
            p_lo, p_hi = py0 + p_round, py1 - p_round
            c_lo, c_hi = ny0 + n_round, ny1 - n_round
            shared = (max(py0, ny0) + min(py1, ny1)) * 0.5
            pt = lambda along, across: (along, across)
        else:
            ep, ec = (py1, ny0) if gap_b >= gap_t else (py0, ny1)
            p_lo, p_hi = px0 + p_round, px1 - p_round
            c_lo, c_hi = nx0 + n_round, nx1 - n_round
            shared = (max(px0, nx0) + min(px1, nx1)) * 0.5
            pt = lambda along, across: (across, along)

        # Per-end half-widths: each end is sized from its own edge, clamped to
        # that edge's full straight span (coverage >= 1 spans the whole edge)
        # and optionally capped in px. Mismatched views make a funnel.
        def end_hw(lo, hi):
            w = (hi - lo) * Swoosh.ribbon_coverage
            if Swoosh.ribbon_max_width > 0.0:
                w = min(w, Swoosh.ribbon_max_width)
            return min(w, hi - lo) * 0.5
        p_hw = end_hw(p_lo, p_hi)
        c_hw = end_hw(c_lo, c_hi)
        if p_hw < 1.0 or c_hw < 1.0:
            return False
        # Center each band on the shared-span center, clamped into its own
        # straight edge: aligned views get a straight band; offset views get
        # bands at different positions with the curves bridge them.
        pc = min(max(shared, p_lo + p_hw), p_hi - p_hw)
        cc = min(max(shared, c_lo + c_hw), c_hi - c_hw)

        # Tangent reach of the boundary cubics: perpendicular to the edges at
        # both ends, scaled with the band centers' distance (not just the
        # gap) so the S stays smooth when the gap is small but the ends
        # large. Signed so the tangents always point out of their view.
        reach = Swoosh.ribbon_curve * math.hypot(ec - ep, cc - pc)
        if ec < ep:
            reach = -reach

        segments = max(2, int(Swoosh.segments))
        sides = []
        for sign in (-1.0, 1.0):
            a0, a3 = pc + sign * p_hw, cc + sign * c_hw
            pts = []
            for i in range(segments + 1):
                t = i / segments
                u = 1.0 - t
                along = (u * u * u * ep + 3 * u * u * t * (ep + reach)
                         + 3 * u * t * t * (ec - reach) + t * t * t * ec)
                across = (u * u * u * a0 + 3 * u * u * t * a0
                          + 3 * u * t * t * a3 + t * t * t * a3)
                pts.append(pt(along, across))
            sides.append(pts)

        # Fill between the two boundary polylines; the band isn't convex, so
        # fill segment quads as triangle pairs. Per-triangle antialiasing is
        # deliberately OFF for the fill: AA feathers a fringe around every
        # triangle, and on the shared interior edges the overlapping fringes
        # over-blend into visible seams - a wireframe across the translucent
        # band. Without AA adjacent triangles rasterize watertight (identical
        # shared vertices), and the band's outer edges are feathered by the
        # boundary strokes below instead.
        #
        # With ribbon_fade_width the fill thins inversely with the local band
        # width - a cross-section contains constant "ink", so a huge ribbon
        # stays airy rather than overwhelming. Width varies along the funnel, so
        # the alpha is per segment: the wide end fades more than the narrow
        # end, graded smoothly over the tessellation. Strokes keep full
        # strength.
        a, b = sides
        fade = Swoosh.ribbon_fade_width
        # Gradient: `sides` runs parent (i=0) -> child (i=segments), so the
        # fill (and the boundary strokes below) lerp from the parent color to
        # the child color along the bridge. rgb2 None/equal means single color.
        grad = rgb2 is not None and tuple(rgb2[:3]) != tuple(rgb[:3])
        if rgb2 is None:
            rgb2 = rgb
        fill = imgui.get_color_u32_rgba(*rgb, Swoosh.ribbon_alpha)
        dl_flags = overlay_dl.flags
        overlay_dl.flags = dl_flags & ~imgui.DRAW_LIST_ANTI_ALIASED_FILL
        try:
            for i in range(segments):
                seg_rgb = (Melty._lerp_rgb(rgb, rgb2, (i + 0.5) / segments)
                           if grad else rgb)
                alpha = Swoosh.ribbon_alpha
                if fade > 0.0:
                    wmid = (math.hypot(b[i][0] - a[i][0], b[i][1] - a[i][1])
                            + math.hypot(b[i + 1][0] - a[i + 1][0],
                                         b[i + 1][1] - a[i + 1][1])) * 0.5
                    if wmid > fade:
                        alpha = Swoosh.ribbon_alpha * fade / wmid
                if grad or fade > 0.0:
                    fill = imgui.get_color_u32_rgba(*seg_rgb, alpha)
                overlay_dl.add_triangle_filled(a[i][0], a[i][1], b[i][0], b[i][1],
                                               a[i + 1][0], a[i + 1][1], fill)
                overlay_dl.add_triangle_filled(b[i][0], b[i][1], b[i + 1][0], b[i + 1][1],
                                               a[i + 1][0], a[i + 1][1], fill)
        finally:
            overlay_dl.flags = dl_flags

        # Stroke the boundary curves (add_polyline is antialiased) for
        # definition and to soften the hard triangle edges. The band's ends
        # sit flush against the view edges, so no caps are needed.
        if Swoosh.ribbon_edge_thickness > 0.0:
            if grad:
                # Per-segment strokes carry the same gradient as the fill (a
                # polyline is one color; consecutive segments share their
                # endpoints, so the joints are tight).
                for i in range(segments):
                    edge = imgui.get_color_u32_rgba(
                        *Melty._lerp_rgb(rgb, rgb2, (i + 0.5) / segments),
                        Swoosh.ribbon_edge_alpha)
                    overlay_dl.add_polyline([a[i], a[i + 1]], edge,
                                            flags=imgui.DRAW_NONE,
                                            thickness=Swoosh.ribbon_edge_thickness)
                    overlay_dl.add_polyline([b[i], b[i + 1]], edge,
                                            flags=imgui.DRAW_NONE,
                                            thickness=Swoosh.ribbon_edge_thickness)
            else:
                edge = imgui.get_color_u32_rgba(*rgb, Swoosh.ribbon_edge_alpha)
                overlay_dl.add_polyline(a, edge, flags=imgui.DRAW_NONE,
                                        thickness=Swoosh.ribbon_edge_thickness)
                overlay_dl.add_polyline(b, edge, flags=imgui.DRAW_NONE,
                                        thickness=Swoosh.ribbon_edge_thickness)
        return True

    @staticmethod
    def _draw_swoosh(overlay_dl, px, py, pw, ph, nx, ny, nw, nh, rgb,
                     rgb2=None, p_round=0.0, n_round=0.0, p_clip=None,
                     mode=None):
        """Draw a curved connector from the parent view's outline to the nested
        view. The line is thick at both endpoints and tapers thin in the middle.
        `rgb` is the resolved highlight color (see _highlight_rgb); `rgb2`,
        when given, is the CHILD end's color — fill, edge strokes and end caps
        all blend from `rgb` at the parent end to `rgb2` at the child end (in
        both line and ribbon modes). p_round /
        n_round are the parent/nested corner radii so the ends meet the rounded
        edge. p_clip, if given, is the parent's absolute clip rect
        (left, top, right, bottom): the parent end is anchored against the
        *visible* (clipped) part of the parent rect so the cap dot never lands
        on a region that's been scrolled/clipped away. `mode` is a SwooshMode
        (or its string value) selecting the connector style per window — the
        swoosh_mode window kwarg lands here; None follows the global
        Swoosh.ribbon toggle. Tunables live on Swoosh.*."""
        if mode is None:
            mode = SwooshMode.RIBBON if Swoosh.ribbon else SwooshMode.LINE
        elif isinstance(mode, str):
            mode = SwooshMode(mode)
        # Clamp the parent rect to its visible region so the connector anchors on
        # what's actually on screen rather than a clipped-off edge. When the
        # parent is scrolled/clipped completely out of view there is no visible
        # edge to anchor to - draw nothing rather than tether to a phantom rect.
        if p_clip is not None:
            cl, ct, cr, cb = p_clip
            vx0, vy0 = max(px, cl), max(py, ct)
            vx1, vy1 = min(px + pw, cr), min(py + ph, cb)
            if vx1 <= vx0 or vy1 <= vy0:
                return
            px, py, pw, ph = vx0, vy0, vx1 - vx0, vy1 - vy0

        # Real (un-grown) view rects: the endpoints must land on these.
        rpx0, rpy0, rpx1, rpy1 = px, py, px + pw, py + ph
        rnx0, rny0, rnx1, rny1 = nx, ny, nx + nw, ny + nh

        # Ribbon mode: a full band between the view edges replaces the thin
        # line whenever the views have a space to bridge; overlapping views fall
        # through to the line, which knows how to route around the intersection.
        if mode is SwooshMode.RIBBON and Melty._draw_ribbon(
                overlay_dl, rpx0, rpy0, rpx1, rpy1,
                rnx0, rny0, rnx1, rny1, rgb, rgb2=rgb2,
                p_round=p_round, n_round=n_round):
            return

        # The overlap transition is computed on the grown rects so it begins
        # as the views approach, before they actually touch (Swoosh.overlap_padding).
        # Only the math uses the grown rects; the endpoints are pulled back onto
        # the real view edges below.
        pad = Swoosh.overlap_padding
        gpx0, gpy0, gpx1, gpy1 = rpx0 - pad, rpy0 - pad, rpx1 + pad, rpy1 + pad
        gnx0, gny0, gnx1, gny1 = rnx0 - pad, rny0 - pad, rnx1 + pad, rny1 + pad

        # Anchor both ends at the center of the rects' shared edge (smoothed),
        # so the connector stays centered and glides as the rects move. When the
        # rects overlap, the ends slide out of the intersection and `ctrl` bows
        # the curve through it (see _closest_perimeter_points).
        geom = Melty._closest_perimeter_points(
            gpx0, gpy0, gpx1, gpy1,
            gnx0, gny0, gnx1, gny1,
        )
        if geom is None:
            # Parent fully hidden under the child: no connector to draw.
            return
        x0, y0, x1, y1, ctrl, ctrl_g = geom

        # Project each endpoint onto the real view's (rounded) perimeter: this
        # pulls it off the grown rect to the real edge and, since both
        # coordinates were interpolated, lets it slide along the edge and around
        # the rounded corners. The control point stays out in the exterior so the
        # bow is preserved.
        x0, y0 = Melty._project_to_rounded_rect(x0, y0, rpx0, rpy0, rpx1, rpy1, p_round)
        x1, y1 = Melty._project_to_rounded_rect(x1, y1, rnx0, rny0, rnx1, rny1, n_round)

        seg_dx, seg_dy = x1 - x0, y1 - y0
        seg_len = math.hypot(seg_dx, seg_dy)
        if seg_len < 1.0:
            return

        # Ramp the bow in with the connector's slope rather than turning it on:
        # the ratio of the shorter axis span to the longer one is 0 when the line
        # is axis-aligned and 1 at 45 degrees, so level runs stay straight and
        # the curve grows smoothly as the line tilts toward diagonal.
        adx, ady = abs(seg_dx), abs(seg_dy)
        slope_ratio = min(adx, ady) / max(adx, ady) if max(adx, ady) > 1e-6 else 0.0
        curve_factor = slope_ratio ** Swoosh.curve_ramp

        # Quadratic bezier control point: bow the curve perpendicular to the
        # chord by an amount scaled by curve_factor. If the ends were slid out
        # of an overlap, _closest_perimeter_points also hands back a control
        # point that bows the curve through the exterior notch; bias toward it
        # by ctrl_g (the overlap-mode weight) so the bow eases in with the slide.
        mx, my = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        perp_x, perp_y = -seg_dy / seg_len, seg_dx / seg_len
        bow = seg_len * Swoosh.curve * curve_factor
        cxp, cyp = mx + perp_x * bow, my + perp_y * bow
        if ctrl is not None:
            # The exterior route's bow also scales with Swoosh.curve (measured
            # from the chord midpoint, designed full at curve≈0.22) so lowering
            # curve flattens it and curve=0 gives a straight connector.
            cg = Swoosh.curve / 0.22
            ncx = mx + (ctrl[0] - mx) * cg
            ncy = my + (ctrl[1] - my) * cg
            cxp = cxp + (ncx - cxp) * ctrl_g
            cyp = cyp + (ncy - cyp) * ctrl_g

        grad = rgb2 is not None and tuple(rgb2[:3]) != tuple(rgb[:3])
        if rgb2 is None:
            rgb2 = rgb
        col = imgui.get_color_u32_rgba(*rgb, Swoosh.alpha)
        segments = max(2, int(Swoosh.segments))
        end_hw = Swoosh.end_thickness
        mid_hw = Swoosh.mid_thickness
        taper = Swoosh.taper

        def bezier(t):
            u = 1.0 - t
            bx = u * u * x0 + 2 * u * t * cxp + t * t * x1
            by = u * u * y0 + 2 * u * t * cyp + t * t * y1
            # derivative for tangent direction
            tx = 2 * u * (cxp - x0) + 2 * t * (x1 - cxp)
            ty = 2 * u * (cyp - y0) + 2 * t * (y1 - cyp)
            return bx, by, tx, ty

        def half_width(t):
            # (2t-1)^taper is 1 at the ends, 0 at the center.
            edge = abs(2.0 * t - 1.0) ** taper
            return mid_hw + (end_hw - mid_hw) * edge

        # Build the two offset edges of the ribbon, then fill it segment by
        # segment (the shape isn't convex, we fill quads as triangle pairs).
        left = []
        right = []
        for i in range(segments + 1):
            t = i / segments
            bx, by, tx, ty = bezier(t)
            tlen = math.hypot(tx, ty)
            if tlen < 1e-6:
                nxn, nyn = perp_x, perp_y
            else:
                nxn, nyn = -ty / tlen, tx / tlen
            hw = half_width(t)
            left.append((bx + nxn * hw, by + nyn * hw))
            right.append((bx - nxn * hw, by - nyn * hw))

        for i in range(segments):
            if grad:
                # Remember t=0 is the parent end, t=1 the child end, so the gradient
                # blends parent color -> child color along its length.
                col = imgui.get_color_u32_rgba(
                    *Melty._lerp_rgb(rgb, rgb2, (i + 0.5) / segments),
                    Swoosh.alpha)
            l0, l1 = left[i], left[i + 1]
            r0, r1 = right[i], right[i + 1]
            overlay_dl.add_triangle_filled(l0[0], l0[1], r0[0], r0[1], l1[0], l1[1], col)
            overlay_dl.add_triangle_filled(r0[0], r0[1], r1[0], r1[1], l1[0], l1[1], col)

        # add_triangle_filled has hard (aliased) edges, but add_polyline is
        # antialiased (DRAW_LIST_ANTI_ALIASED_LINES, on by default). Stroke the
        # ribbon's two long edges to feather them; the square ends are covered by
        # the AA cap circles below. Gradient mode strokes per segment (one
        # color per polyline; shared endpoints keep the joints tight).
        if Swoosh.aa_width > 0.0:
            if grad:
                for i in range(segments):
                    seg_col = imgui.get_color_u32_rgba(
                        *Melty._lerp_rgb(rgb, rgb2, (i + 0.5) / segments),
                        Swoosh.alpha)
                    overlay_dl.add_polyline([left[i], left[i + 1]], seg_col,
                                            flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)
                    overlay_dl.add_polyline([right[i], right[i + 1]], seg_col,
                                            flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)
            else:
                overlay_dl.add_polyline(left, col, flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)
                overlay_dl.add_polyline(right, col, flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)

        # Round caps over the flat (square) ends of the ribbon so the endpoints
        # read as dots rather than chopped-off edges - each in its own endcap color.
        cap_r = end_hw * Swoosh.cap_scale
        overlay_dl.add_circle_filled(x0, y0, cap_r,
                                     imgui.get_color_u32_rgba(*rgb, Swoosh.alpha))
        overlay_dl.add_circle_filled(x1, y1, cap_r,
                                     imgui.get_color_u32_rgba(*rgb2, Swoosh.alpha))

    @classmethod
    def is_wrapped(cls):
        if len(cls.wrap_stack) == 0:
            return False
        else:
            return cls.wrap_stack[-1]

    @classmethod
    def draw(cls, draw_state, cursor_pos=None, detached=False):

        if draw_state is None:
            return

        if draw_state.closed:
            return

        # Melty.depth = 0
        Melty.bg_depth = draw_state._bg_depth

        original_bg_stack = copy(Melty.bg_stack)
        if draw_state._bg_stack is not None:
            if len(draw_state._bg_stack) > 1:
                Melty.bg_stack = draw_state._bg_stack[-2:]
            else:
                Melty.bg_stack = [draw_state._bg_stack[-1]]


        view_func = draw_state._wrapper
        input_value = draw_state._raw_input_value
        kwargs = draw_state._kwargs
        kwargs['layer_unique'] = draw_state.unique
        imgui.set_cursor_screen_pos((int(draw_state.abs_left), int(draw_state.abs_top)))

        if Toggles.debug_z_depth:
            draw_list = imgui.get_overlay_draw_list()
            draw_list.add_text(draw_state.abs_left, draw_state.abs_top - 40, imgui.get_color_u32_rgba(1, 0, 0, 1),
                               f"Layer {draw_state.layer} "
                               f"Depth {draw_state.depth} "
                               f"zpos {draw_state.z_pos} "
                               f"depth_and_layer {draw_state.depth_and_layer} "
                               f"Melty.active_layer {cls.active_layer} "
                               f"Melty.z_pos {cls.z_pos} "
                               f"Melty.depth {cls.depth}")

            draw_list.add_text(draw_state.abs_left, draw_state.abs_top - 20, imgui.get_color_u32_rgba(1, 1, 0, 1),
                               f"kwargs['active_layer'] {kwargs['active_layer']} "
                               )

        kwargs['input_value'] = input_value
        kwargs['detached'] = detached
        return_val = view_func(**kwargs)
        if return_val is not None:
            cls.pending_return_values[draw_state._tile_id] = return_val
            if return_val[0]:
                note=Note(name="delayed return", reason=f"{draw_state.name}", tint=(0, 1, 1))
                if draw_state.parent_window is not None:
                    Melty.cache.invalidate_up(draw_state.parent_window._tile_id, max_depth=6, frame_delta=1, note=note)
                else:
                    Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=6, force=True, frame_delta=1, note=note)

                request_render()
        else:
            print(f"{draw_state.name}")

        Melty.bg_stack = original_bg_stack

    # cls.cache.remove_parent()

    @classmethod
    def init_input_backend(cls, window):
        """Swap to the GLFW-callback input backend (event-queued, frame-rate
        independent). Call once after the imgui GlfwRenderer is created so our
        callbacks chain onto (and preserve) imgui's."""
        try:
            cls.backend = GlfwQueueBackend(cls.event_handler, window)
        except Exception as e:
            print(f"GlfwQueueBackend unavailable, keeping ImGuiBackend: {e}")

    @classmethod
    def apply_refresh_nested_windows(cls, nested_window_refresh=None):
        if nested_window_refresh is None:
            parent_window = cls.nested_window_refresh
            cls.nested_window_refresh = None

        else:
            parent_window = nested_window_refresh

        if parent_window is None:
            return

        to_discard = set()

        parent_ds_id = parent_window.id
        ds_list = cls.root_draw_states.get(parent_ds_id, [])
        for idx, ds in enumerate(ds_list):
            to_discard.add((parent_ds_id, ds))
            if ds.closable:
                cls.apply_refresh_nested_windows(ds)

        for ds_id, discard_ds in to_discard:
            cls.root_draw_states[ds_id].remove(discard_ds)

        note = Note(name="refresh_nested_windows", reason="refresh_nested_windows", tint=(1, 0, 1))
        Melty.cache.invalidate_up(parent_window._tile_id,
                                  max_depth=10, force=True, note=note)


    @classmethod
    def refresh_nested_windows(cls, draw_state):
        parent_window = draw_state.parent_window if draw_state.parent_window is not None else \
        Melty.melty_window_stack[
            -1] if len(Melty.melty_window_stack) > 0 else draw_state
        cls.nested_window_refresh = parent_window

    @classmethod
    def post_to_render(cls, fn):
        """Queue `fn` to run on the render thread between frames (drained at
        end_frame). Safe from any thread; wakes the loop so an idle app runs
        it promptly. For work that must not race a frame in progress — e.g.
        mutating a live view-model tree that frame walkers iterate."""
        with cls._render_tasks_lock:
            cls._render_tasks.append(fn)
        request_render()

    @classmethod
    def _drain_render_tasks(cls):
        if not cls._render_tasks:
            return
        with cls._render_tasks_lock:
            tasks, cls._render_tasks = cls._render_tasks, []
        for fn in tasks:
            try:
                fn()
            except Exception as e:
                print(f"[melty] render task failed: {e}")
                print_stack_trace(exception=e)

    @classmethod
    def _spawner_fully_clipped(cls, ds, _depth=0):
        """True when the view this nested window was spawned from (the same
        anchor the swoosh tethers to) is completely scrolled/clipped out of
        view — or when the window's own parent window is hidden for that
        reason, so chains of nested windows hide together.

        Deliberately a LIVE-geometry test, not a BVH lookup or a "did the
        parent render this frame" test: BVH boxes only catch up when a view
        re-renders, so they are stale during the very scroll that pushes the
        parent away — and a blit-cached parent skips its render while being
        perfectly visible, so render-recency can't distinguish "offscreen"
        from "cached". abs_left/abs_top (and so abs_clip_rect) are computed
        live off the persistent draw_state — ancestor scroll included —
        regardless of how (or whether) the parent was drawn this frame, so
        an empty visible rect means exactly "the spawner is out of sight"."""
        # The floating DnD window rides the cursor and must survive its
        # source view auto-scrolling out from under the drag.
        try:
            from src.lsd.gl_gui.view.core_views.drag_drop import DragDrop
            if DragDrop.is_dragged_item(ds):
                return False
        except Exception:
            pass

        parent = ds._parent
        if parent is not None and parent is not ds:
            anchor = getattr(parent, '_offset_ds', None)
            if anchor is None:
                anchor = parent
            if (anchor.width is not None and anchor.height is not None
                    and anchor.clipped_by_rect is not None):
                vl, vt, vr, vb = anchor.abs_clip_rect
                if vr <= vl or vb <= vt:
                    return True

        pw = ds.parent_window
        if (_depth < 16 and pw is not None and pw is not ds
                and pw.closable and pw._parent is not None):
            return cls._spawner_fully_clipped(pw, _depth + 1)
        return False

    @classmethod
    def end_frame(cls):
        if glfw_utils.frames_left > 0:
            request_render()

        cls.apply_move_to_front()

        # Drain GL resources queued for deletion (released GLStates, shader
        # programs invalidated by an edit) - must run on the render thread with
        # the context current, which is exactly here.
        from src.lsd.gl_gui.gl_state import GLState
        GLState.flush_deletes()

        # Drain callables posted from worker threads (post_to_render) - work
        # that must not race the frame, e.g. attaching symbol usages into a
        # LIVE gp tree that view walkers iterate (inserting a dict key during
        # another thread's iteration raises RuntimeError). Same thread here as
        # the GL delete queue above.
        cls._drain_render_tasks()

        cls.apply_refresh_nested_windows()
        # Reset overlay routing to the top (global, unmasked) channel so
        # end_frame draws - FPS counter, selection rects, debug text - don't
        # accidentally land on whatever per-window channel a view last set.
        if cls._overlay_channels_active:
            imgui.get_overlay_draw_list().channels_set_current(cls.max_depth - 1)

        Melty.mode_stack = []

        from src.lsd.gl_gui.modes import Modes
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_with_modes
        draw_with_modes(Counters, name="counters", modes=(Modes.CODE_UI, Modes.CODE_PLAIN_TEXT), mode=Modes.WINDOW)

        if Toggles.debug_z_depth:
            draw_state = list(cls.selected)[-1] if len(cls.selected) > 0 else None
            if draw_state is not None:
                draw_list = imgui.get_overlay_draw_list()
                draw_list.add_text(draw_state.abs_left, draw_state.abs_top - 40, imgui.get_color_u32_rgba(1, 0, 0, 1),
                                   f"Layer {draw_state.layer} "
                                   f"Depth {draw_state.depth} "
                                   f"zpos {draw_state.z_pos} "
                                   f"depth_and_layer {draw_state.depth_and_layer} "
                                   f"Melty.active_layer {Melty.active_layer} "
                                   f"Melty.z_pos {Melty.z_pos} "
                                   f"Melty.depth {Melty.depth}")

        cls.root_draw_states_by_layer = defaultdict(list)
        dynamic_offset = 0
        empty_parents = set()
        to_discard = set()

        for parent_ds_id, ds_list in cls.root_draw_states.items():
            for idx, ds in enumerate(ds_list):
                if ds.abs_closed or ds.closed:
                    to_discard.add((parent_ds_id, ds))
                    continue
                # Hide - don't discard - nested windows whose spawning view is
                # fully offset-clipped out of sight. The window stays
                # registered (a discard could never come back while the layer
                # rides the blit cache, since only the parent's live call site
                # re-registers it), it just isn't dispatched: no draw, no
                # swoosh, no highlight. It reappears the moment the spawner
                # scrolls back into view.
                try:
                    hidden = cls._spawner_fully_clipped(ds)
                except Exception:
                    hidden = False
                if hidden != getattr(ds, '_hidden_offscreen', False):
                    ds._hidden_offscreen = hidden
                    # bvh_query memoizes per (x, y) keyed on _bvh_gen alone: a
                    # flag flip changes its effective result without an index
                    # mutation, so bump gen to invalidate the stale memo.
                    cls._bvh_gen += 1
                if not hidden:
                    cls.root_draw_states_by_layer[ds.abs_layer].append(ds)

        for ds_id, discard_ds in to_discard:
            cls.root_draw_states[ds_id].remove(discard_ds)

        # Universal item drag-and-drop: pick up armed header drags, draw the
        # drop-point lines and commit/cancel on release. BEFORE the layer
        # dispatch - on frames where the (blitted) source collection doesn't
        # run, deferring inline drawing, this re-registers the floating
        # dragged window into its layer so the loop below still draws it
        # (see view/core_views/drag_drop.py). No per-frame invalidation:
        # the drag rides the closable-window blit fastpath.
        try:
            from src.lsd.gl_gui.view.core_views.drag_drop import DragDrop
            DragDrop.frame_update()
        except Exception as dnd_e:
            print(f"DragDrop.frame_update failed: {dnd_e}")

        for idx in range(len(cls.layers)):
            layer = cls.layers[idx]
            imgui.set_cursor_screen_pos((0, 0))
            Melty.active_layer = idx
            Melty.active_layer_stack = []

            if not Melty.channels_split:
                imgui.get_window_draw_list().channels_split(Melty.max_depth)
                imgui.get_window_draw_list().channels_set_current(Melty.max_depth - 1)
                Melty.channels_split = True

            for draw_state in layer:
                if draw_state is not None:
                    cls.draw(draw_state)

            Melty.depth = 0
            if Melty.channels_split:
                # Flatten layers into single channel
                imgui.get_window_draw_list().channels_set_current(0)
                imgui.get_window_draw_list().channels_merge()
                Melty.channels_split = False
            # Sort by y position (draw_state.abs_top)

            # sort by draw_state.z_pos

            # sorted_root_ds = sorted(cls.root_draw_states_by_layer[idx], key=lambda ds: ds.z_pos)

            for d_idx, draw_state in enumerate(cls.root_draw_states_by_layer[idx]):
                # Melty.cache.mask_mark_view(draw_state.z_pos, draw_state.left,
                #                            draw_state.top, draw_state.width, draw_state.height,
                #                            f"view_mask_{draw_state.id}", 4)

                Melty.active_layer = idx + (d_idx)
                draw_state._nested_index = (d_idx)
                Melty.z_pos = (Melty.active_layer * Melty.max_depth) + Melty.depth

                draw_state.layer = Melty.active_layer
                draw_state.z_pos = Melty.z_pos
                draw_state.depth_and_layer = (Melty.shadow_depth, Melty.active_layer)
                draw_state._kwargs['active_layer'] = Melty.active_layer

                child_highlight = None
                if draw_state._kwargs.get("swoosh", True):
                    if draw_state._parent is not None:
                        offset_ds = draw_state._parent._offset_ds

                        if offset_ds is None:
                            offset_ds = draw_state._parent


                        overlay_dl: _DrawList = imgui.get_overlay_draw_list()
                        # Route to the window's z-order channel so this overlay
                        # sits above the window's own content but is masked by
                        # any higher-layer window (matches the renderer's mask).
                        layer_index = draw_state.window_index

                        overlay_dl.channels_set_current(min(Melty.max_layer - 1, offset_ds.window_index))

                        # Color the highlight using the *parent* window's tint:
                        # the nested view doesn't always carry a tint of its own.
                        # current_tint is stashed at draw time (the style manager's
                        # live tint is gone by this post-draw highlight code).
                        parent_tint = offset_ds.current_tint or (draw_state._kwargs.get("tint", (1, 1, 1))[:3], 1.0)
                        highlight_rgb = Melty._highlight_rgb(parent_tint)
                        outline_col = imgui.get_color_u32_rgba(*highlight_rgb, Tint.highlight_outline_alpha)
                        bg_col = imgui.get_color_u32_rgba(*highlight_rgb, Tint.highlight_bg_alpha)

                        # Parent view: faint fill + matching highlight outline,
                        # clipped to the parent's own clip rect so the highlight
                        # doesn't bleed past where the parent is scrolled/clipped.
                        parent_clip = offset_ds.abs_clip_rect if offset_ds.clipped_by_rect is not None else None
                        if parent_clip is not None:
                            overlay_dl.push_clip_rect(parent_clip[0], parent_clip[1],
                                                      parent_clip[2], parent_clip[3], True)
                        offset_rounding = getattr(offset_ds, 'corner_radius', 6)
                        overlay_dl.add_rect_filled(offset_ds.abs_left, offset_ds.abs_top,
                                                   offset_ds.abs_left + offset_ds.width,
                                                   offset_ds.abs_top + offset_ds.height,
                                                   bg_col, rounding=offset_rounding)
                        overlay_dl.add_rect(offset_ds.abs_left, offset_ds.abs_top,
                                            offset_ds.abs_left + offset_ds.width,
                                            offset_ds.abs_top + offset_ds.height,
                                            outline_col, rounding=offset_rounding,
                                            thickness=Tint.highlight_outline_thickness)
                        if parent_clip is not None:
                            overlay_dl.pop_clip_rect()

                        # The child outline + swoosh depend on the child's geometry,
                        # which only becomes current after cls.draw(draw_state) below.
                        # Stash the params and draw them post-draw to avoid a frame of lag.
                        child_highlight = (overlay_dl, offset_ds,
                                           outline_col, highlight_rgb, parent_clip)

                if not Melty.channels_split:
                    imgui.get_window_draw_list().channels_split(Melty.max_depth)
                    imgui.get_window_draw_list().channels_set_current(min(Melty.active_layer, Melty.max_depth - 1))
                    Melty.channels_split = True


                if draw_state.unique not in cls.seen_unique:
                    cls.draw(draw_state)

                # Now that the child has been drawn this frame, its geometry is
                # current: draw the child outline + swoosh of current bounds.
                if child_highlight is not None and draw_state.width is not None and draw_state.height is not None:
                    overlay_dl, offset_ds, outline_col, highlight_rgb, parent_clip = child_highlight
                    # Route to the *nested* view's own overlay channel (not its
                    # window_index, which collapses to the parent's layer for a
                    # first-level nested view) so the line/outline aren't masked
                    # by the nested window. cls.draw may also have moved the channel.
                    rounding = getattr(draw_state, 'corner_radius', 6)

                    # The child end wears the CHILD window's OWN color: the
                    # highlight outline takes it and the swoosh blends parent ->
                    # child between the two ends. current_tint is intentionally
                    # NOT preferred here - it's the AMBIENT tint at draw
                    # (stashed before the wrapper pushes the window's own tint,
                    # and replayed as ambient context for latched closable
                    # windows), so it reads the enclosing window, not this one.
                    # Prefer the live child tint kwarg, then the ds.tint
                    # first-draw stamp; ambient only when the child declares no
                    # tint of its own (the old single-color look).
                    child_tint = draw_state._kwargs.get("tint")
                    if not (isinstance(child_tint, (tuple, list)) and len(child_tint) >= 3):
                        child_tint = draw_state.tint
                    if not (isinstance(child_tint, (tuple, list)) and len(child_tint) >= 3):
                        child_tint = draw_state.current_tint
                    child_rgb = (Melty._highlight_rgb(tuple(child_tint[:3]))
                                 if child_tint else highlight_rgb)
                    child_outline_col = imgui.get_color_u32_rgba(
                        *child_rgb, Tint.highlight_outline_alpha)

                    overlay_dl.channels_set_current(min(draw_state.window_index, Melty.max_layer -1))

                    overlay_dl.add_rect(draw_state.abs_left, draw_state.abs_top,
                                        draw_state.abs_left + draw_state.width,
                                        draw_state.abs_top + draw_state.height,
                                        child_outline_col, rounding=rounding,
                                        thickness=Tint.highlight_outline_thickness)

                    # overlay_dl.channels_set_current(min(Melty.max_layer - 1, offset_ds.window_index))

                    Melty._draw_swoosh(
                        overlay_dl,
                        offset_ds.abs_left, offset_ds.abs_top,
                        offset_ds.width, offset_ds.height,
                        draw_state.abs_left, draw_state.abs_top,
                        draw_state.width, draw_state.height,
                        highlight_rgb,
                        rgb2=child_rgb,
                        p_round=getattr(offset_ds, 'corner_radius', 6),
                        n_round=rounding,
                        p_clip=parent_clip,
                        mode=draw_state._kwargs.get("swoosh_mode"),
                    )

                if Melty.channels_split:
                    # Flatten layers into single channel
                    imgui.get_window_draw_list().channels_set_current(0)
                    imgui.get_window_draw_list().channels_merge()
                    Melty.channels_split = False


                    # for i in range(draw_state.context_menu_offset):
                    #     if offset_ds._parent is None:
                    #         break
                    #     offset_ds = offset_ds._parent

                # Melty.depth = draw_state.depth + d_idx
                # Melty.cache.draw_tile(draw_state)
                # last_bounding_hovered = draw_state._bounding_hovered
                # new_bounding_hovered = draw_state.is_bounding_hovered()
                # hover_changed = last_bounding_hovered != new_bounding_hovered
                # draw_state._bounding_hovered = new_bounding_hovered
                # if (draw_state.width is None or draw_state.height is None or hover_changed or
                #         draw_state._bounding_hovered != draw_state._imgui_popover_open):
                #     Melty.cache.invalidate(draw_state._tile_id)
                #     # draw_state.draw_rect()
        cls.layers = []

        is_popup_open = imgui.is_popup_open("", flags=imgui.POPUP_ANY_POPUP)
        Melty.imgui_popup_open = is_popup_open
        #
        from src.lsd.gl_gui.view.core_views.core_render import get_melty_state
        melty = get_melty_state()

        melty.hover_stack = []
        melty.hotkey_stack = []
        melty.unique_stack = []
        Melty.draw_state_stack = []

        if not melty.nearest_drop_target is None:
            melty.drag_drop_target = melty.nearest_drop_target
            melty.drag_drop_target_tag = melty.nearest_drop_target_tag

        while len(cls.items_to_delete) > 0:
            key, collection = cls.items_to_delete.pop(0)
            error = delete_from_collection(key, collection)
            if error is not None:
                print(error)
            cls.cache.invalidate_by_obj(collection)
            request_render()

        Melty.hovered_drawstate = Melty.hovered_drawstate_pending
        Melty.imgui_any_item_active = imgui.is_any_item_active()
        Melty.active_layer = 0
        style = imgui.get_style()

        style.item_spacing = Melty.original_spacing
        style.window_padding = Melty.original_window_padding
        style.frame_padding = Melty.original_frame_padding

        overlay: _DrawList = imgui.get_overlay_draw_list()


        window_size = imgui.get_io().display_size
        overlay.add_text(window_size.x - 600, 5, imgui.get_color_u32_rgba(1, 1, 1, 1),
                         f"FPS: {imgui.get_io().framerate:.1f}")

        to_unselect = set()
        for selected_ds in cls.selected:
            if selected_ds.abs_closed or selected_ds.closed:
                to_unselect.add(selected_ds)

            if not selected_ds._kwargs.get("selectable", True):
                to_unselect.add(selected_ds)

        for ds in to_unselect:
            if ds in cls.selected:
                cls.selected.remove(ds)


        for selected_ds in cls.selected:

            if selected_ds.width is None or selected_ds.height is None:
                continue

            draw_fill = True
            if selected_ds.height > 30:
                draw_fill = False

            # Draw the selection rect to the selected view's own overlay
            # channel (same as the nested-view highlight and swoosh) so a
            # higher-layer window stencil-masks it, rather than the rect
            # floating on top of everything on the global top channel.
            overlay.channels_set_current(min(cls.max_layer - 1, selected_ds.window_index))

            # Color from the view's storable tint, brightened the same way as
            # the highlight boxes (current_tint may be None -> falls back to
            # the live tint inside _highlight_rgb).
            select_rgb = cls._highlight_rgb(selected_ds.current_tint)
            bg_col = imgui.get_color_u32_rgba(*select_rgb, Tint.select_bg_alpha)
            outline_col = imgui.get_color_u32_rgba(*select_rgb, Tint.select_outline_alpha)
            rounding = getattr(selected_ds, 'corner_radius', 6)

            clip_rect = selected_ds.abs_clip_rect
            overlay.push_clip_rect(clip_rect[0], clip_rect[1], clip_rect[2], clip_rect[3], True)
            x0, y0 = selected_ds.abs_left, selected_ds.abs_top
            x1, y1 = x0 + selected_ds.width, y0 + selected_ds.height
            if draw_fill:
                overlay.add_rect_filled(x0, y0, x1, y1, bg_col, rounding=rounding)
            overlay.add_rect(x0, y0, x1, y1, outline_col, rounding=rounding,
                             thickness=Tint.select_outline_thickness)
            overlay.pop_clip_rect()

        # Restore the global top channel for any later overlay draws.
        if cls._overlay_channels_active:
            overlay.channels_set_current(cls.max_layer - 1)


        if Toggles.InvalidateTracker.enable:
            for key, note in InvalidateTracker.invalidations.items():
                ds = note.draw_state
                color = note.tint

                frames_past = Melty.frame_count - note.frame
                alpha_from_frame_past = max(0, 1.0 - (frames_past / Toggles.InvalidateTracker.keep_for_frames))
                alpha_from_note = note.tint[3] if len(note.tint) > 3 else 1.0

                invalidation_rect = (ds.abs_left, ds.abs_top,
                                     ds.abs_left + (ds.width or 0),
                                     ds.abs_top + (ds.height or 0))
                text_size = imgui.calc_text_size(f"{note.name} | {note.reason}")
                overlay.add_rect_filled(invalidation_rect[0] + ds.width - text_size.x, invalidation_rect[1],
                                        invalidation_rect[0] + ds.width,
                                        invalidation_rect[1] + text_size.y,
                                        imgui.get_color_u32_rgba(*color[:3], alpha_from_frame_past * alpha_from_note))

                overlay.add_text(invalidation_rect[0] + ds.width - text_size.x, invalidation_rect[1], imgui.get_color_u32_rgba(*(0,0,0),
                                                                                                           alpha_from_frame_past * alpha_from_note),
                                 f"{note.name} |{note.reason}")

                if Toggles.InvalidateTracker.draw_rect:
                    overlay.add_rect(invalidation_rect[0], invalidation_rect[1], invalidation_rect[2], invalidation_rect[3],
                                        imgui.get_color_u32_rgba(*color[:3], alpha_from_frame_past / 2.0 * alpha_from_note), thickness=1.0)



        if Toggles.InvalidateTracker.draw_bvh:
            for key, note in InvalidateTracker.invalidations.items():
                if note.rect is not None:
                    ds = note.draw_state
                    color = note.tint

                    frames_past = Melty.frame_count - note.frame
                    alpha_from_frame_past = max(0, 1.0 - (frames_past / Toggles.InvalidateTracker.keep_for_frames))

                    invalidation_rect = note.rect

                    overlay.add_text(invalidation_rect[0], invalidation_rect[1] - 15, imgui.get_color_u32_rgba(*color,
                                                                                                               alpha_from_frame_past),
                                     f"{note.name} |{note.reason}")


                    overlay.add_rect(invalidation_rect[0], invalidation_rect[1], invalidation_rect[2], invalidation_rect[3],
                                     imgui.get_color_u32_rgba(*color, alpha_from_frame_past), thickness=1.0)

        if Toggles.show_filled_tiles:
            # Mirror the InvalidateTracker overlay loop, but for tiles whose
            # filled_bbox now covers their full area - a transparent green
            # wash so you can see at a glance which views the scroll-driven
            # invalidation has stopped touching.
            fill_col = imgui.get_color_u32_rgba(0.0, 1.0, 0.2, 0.18)
            edge_col = imgui.get_color_u32_rgba(0.0, 1.0, 0.2, 0.55)

            fill_col_fill = imgui.get_color_u32_rgba(1.0, 1.0, 0.2, 0.18)
            edge_col_fill = imgui.get_color_u32_rgba(1.0, 1.0, 0.2, 0.55)
            for tile in cls.cache._tiles.values():
                ds = tile.draw_state
                if ds is None or ds.width is None or ds.height is None:
                    continue
                if tile is None or not cls.cache._tile_fully_filled(tile):
                    x0, y0 = ds.abs_left, ds.abs_top
                    x1, y1 = x0 + ds.width, y0 + ds.height
                    overlay.add_rect_filled(x0, y0, x1, y1, fill_col_fill)
                    overlay.add_rect(x0, y0, x1, y1, edge_col_fill, thickness=1.0)
                else:

                    x0, y0 = ds.abs_left, ds.abs_top
                    x1, y1 = x0 + ds.width, y0 + ds.height
                    overlay.add_rect_filled(x0, y0, x1, y1, fill_col)
                    overlay.add_rect(x0, y0, x1, y1, edge_col, thickness=1.0)

        Collisions.handle_collisions()

        draw_notifications()

        for parent_ds_id, ds_list in cls.root_draw_states.items():
            if len(ds_list) == 0:
                empty_parents.add(parent_ds_id)
        for empty_parent in empty_parents:
            cls.root_draw_states.pop(empty_parent, None)

        # Drop key events now that every view has rendered - including the
        # windows drawn above in this method's layer loop (the editors, the
        # floating search box). Clearing earlier would empty the buffer before
        # those windows read it, which is why text input saw no keys.
        cls.frame_key_events = []



    @classmethod
    def finalize_overlay_channels(cls):
        """Capture per-channel index counts, then merge the foreground draw
        list's channels. MUST run before imgui.end_frame(): ImGui's render path
        requires channels to be merged, and leaving them split collapses the
        per-channel content. We use cumulative index counts (not command
        counts) as boundaries because ChannelsMerge drops trailing empty
        commands and can fuse a channel's first command into the previous
        channel's last command — index counts survive both."""
        if not cls._overlay_channels_active:
            return
        overlay = imgui.get_overlay_draw_list()
        cum_idx = 0
        idx_boundaries = []
        per_channel = []
        for ch in range(cls.max_layer):
            overlay.channels_set_current(ch)
            n = overlay.idx_buffer_size
            per_channel.append(n)
            cum_idx += n
            idx_boundaries.append(cum_idx)
        overlay.channels_merge()
        cls._overlay_channel_ranges = cls._resolve_channel_command_ranges(
            overlay, idx_boundaries
        )
        cls._overlay_channels_active = False

        if cls.frame_count % 120 == 0:
            nz = [(ch, n) for ch, n in enumerate(per_channel) if n > 0]

    @classmethod
    def to_delete(cls, key, collection):
        cls.items_to_delete.append((key, collection))

    @classmethod
    def post_frame(cls, imgui_impl, window):
        imgui_impl.begin_frame_split()
        imgui.render()

        fb_w, fb_h = imgui.get_io().display_size  # or your actual GL viewport size
        draw_data = imgui.get_draw_data()
        imgui_impl.render_except_overlay(draw_data)
        Melty.cache.finalize_captures((int(fb_w), int(fb_h)))

        if Toggles.filters:
            Melty.filter.brightness_contrast(
                input_framebuffer=0,
                output_framebuffer=0,
                brightness=Toggles.brightness,
                contrast=Toggles.contrast,
                width=int(fb_w),
                height=int(fb_h)
            )

            # if Toggles.draw_melty:
            total_layers = 1.0 / ((Melty.max_layer - 1.0) * (Melty.max_depth - 1.0)) * 100.0
            min_val, max_val = 0.0, total_layers
            normalized_sub_mask, _, _ = Melty.filter.normalize(
                Melty.cache._full_mask_tex, min_value=0.0000, max_value=total_layers)
            diff = ((max_val - min_val) * 65535.0)

            # Render the (expensive) shadow_cast pass at a reduced resolution.
            # shadow_cast's math is in UV space, so a low-res mask produces the
            # same soft shadow with far fewer fragment invocations. The composite
            # filter samples the shadow map as a sampler2D (GL_LINEAR), so it
            # upscales automatically over the full-res UI.
            downscale = max(1, int(getattr(Toggles, "shadow_downscale", 1)))
            shadow_size = (
                max(1, int(fb_w) // downscale),
                max(1, int(fb_h) // downscale),
            ) if downscale > 1 else None

            shadow_raw = Melty.filter.shadow_cast(
                normalized_sub_mask,
                max_steps=diff / 2.0,
                output_size=shadow_size,
            )

            if not Toggles.draw_legacy:
                # Resolution of the shadow map when composited - controls the
                # bilateral upsample so the low-res shadow sticks to the crisp
                # rounded-rect edges instead of fringing.
                composite_shadow_size = shadow_size or (int(fb_w), int(fb_h))
                Melty.filter.shadow_composite(
                    input_framebuffer=0,
                    output_framebuffer=0,
                    shadow_map=shadow_raw,
                    depth_map=normalized_sub_mask,
                    shadow_opacity=0.9,
                    shadow_color=(0.0, 0.02, 0.05),  # Slightly blue shadow
                    shadow_size=(float(composite_shadow_size[0]),
                                 float(composite_shadow_size[1])),
                    depth_sharpness=float(getattr(Toggles, "shadow_edge_sharpness", 50.0)),
                )

        # Overlay last, so the highlight/swoosh sits on top of the shadow pass
        # (the split renderer's intended slot: "below overlay" is everything above).
        imgui_impl.render_overlay_only(draw_data)

        # Fulfill any pending MCP window screenshots now: the full frame is in
        # GL_BACK and the GL context is current on this (render) thread.
        from src.lsd.gl_gui.screenshot import process_captures, process_take_screenshot_flags
        process_captures(window)
        # Service deferred context-menu 'window' screenshots (front + settle, then grab).
        process_take_screenshot_flags(window)

        # Run any pending MCP eval_python commands on this (render) thread, where
        # it's safe to touch Melty/imgui state.
        from src.lsd.gl_gui.mcp_eval import process_evals
        process_evals()

        glfw.swap_buffers(window)

        from src.lsd.gl_gui.view.core_views.core_render import apply_drag_and_drop
        apply_drag_and_drop()
        pass

        InvalidateTracker.on_frame_end()
        AttributeChurnMonitor.on_frame_end()

    @classmethod
    def get_latest_mouse(cls):
        return imgui.get_io().mouse_pos

    @classmethod
    def report_imgui_active(cls):
        cls.imgui_active_pending = True
        cls.imgui_active = True

    @classmethod
    def add_blocker(cls, rect, layer=None):
        if layer is None:
            layer = cls.active_layer
        cls.pending_blockers[layer] = rect

    @classmethod
    def on(cls, event_name, tile_id) -> Optional[InputEvent]:
        id_str = tile_id
        if id_str in cls.events:
            if event_name in cls.events[id_str]:
                return cls.events[id_str][event_name]
        return None

    @classmethod
    def to_apply(cls, action: CollectionAction):
        cls.actions_to_apply.append(action)

    @classmethod
    def cleanup(cls):
        # Drop any hanging MCP connections first, before the teardown below - a
        # client holding a streaming/keep-alive connection can otherwise block
        # shutdown. Lazy import keeps melty free of the mcp_server dependency.
        try:
            from src.lsd.gl_gui.mcp_server import notify_melty_shutdown
            notify_melty_shutdown()
        except Exception as e:
            print(f"[melty] mcp shutdown notify failed: {e}")
        try:
            from src.lsd.gl_gui.gl_state import GLState
            GLState.shutdown_all()
        except Exception as e:
            print(f"[melty] gl_state shutdown failed: {e}")
        cls.filter.cleanup()
        cls.texture_manager.clear()
        Background.shutdown()
        Monitor.shutdown()
        FileWatch.shutdown()
        cls.glfw_window = None

    @classmethod
    def get_channel(cls, depth=None):
        if depth is None:
            depth = cls.depth

        if depth < 0:
            depth = 0

        if depth >= cls.max_depth - 3:
            return cls.max_depth - 1

        return depth + 3
        # return max(min(cls.max_depth - 3, cls.depth), 0)

    @classmethod
    def delete_window(cls, draw_state):
        if draw_state is None:
            return
        window_key = draw_state._tile_id
        cls.pending_delete_window = (window_key, draw_state)

    @classmethod
    def find_window(cls, name):
        """The draw_state of a registered top-level window matching `name` — the
        full registered name ("Foo##@window") or just the display name ("Foo").
        Returns None if no such window is registered. Works for closed windows:
        a window's ManagedWindow stays registered (with its draw_state and
        position) while hidden, which is what makes launching one from elsewhere
        — e.g. search — possible without it being open first."""
        target = str(name)
        clean = target.split("##")[0]
        for w in cls.registered_windows.values():
            wn = getattr(w, 'name', None)
            if wn and (wn == target or wn.split("##")[0] == clean):
                return getattr(w, 'draw_state', None)
        return None

    @classmethod
    def open_window(cls, name):
        """Open (un-hide) and raise a registered window by `name`, returning its
        draw_state (or None). Use to launch a closed/"lost" window from anywhere
        — e.g. a search result jumping to its window."""
        ds = cls.find_window(name)
        if ds is not None:
            ds.closed = False
            cls.move_window_to_front(ds)
        return ds

    @classmethod
    def window_tint(cls, name):
        """The display tint of a registered window by name. It lives on the
        ManagedWindow's input_value (the window's own object), NOT its draw_state
        — the draw_state keeps the generic default — so this is the colour to use
        when tinting things by window (e.g. search results). None if unknown or
        the window has no tint."""
        if name is None:
            return None
        clean = str(name).split("##")[0]
        for w in cls.registered_windows.values():
            wn = getattr(w, 'name', None)
            if wn and str(wn).split("##")[0] == clean:
                return getattr(getattr(w, 'input_value', None), 'tint', getattr(w, 'tint', None))
        return None

    @classmethod
    def summon_window(cls, draw_state, x, y):
        """Move a window so its top-left lands at screen (x, y) AND raise it —
        the "summon" the Dock's target button does, so a launched window comes
        to where you are instead of staying put (maybe off-screen). window_pos
        is the unanchored origin, so offset by the window's anchor delta
        (abs - window_pos), same as the Dock summon."""
        if draw_state is None:
            return
        wp = draw_state.window_pos or (0, 0)
        from_zero_x = (draw_state.abs_left or 0) - wp[0]
        from_zero_y = (draw_state.abs_top or 0) - wp[1]
        draw_state.window_pos = (x - from_zero_x, y - from_zero_y)
        cls.move_window_to_front(draw_state)
        if cls.cache is not None and draw_state._tile_id is not None:
            cls.cache.invalidate_up(draw_state._tile_id, force=True, max_depth=4)
        request_render()

    @classmethod
    def move_window_to_front(cls, draw_state):
            if draw_state is None:
                return

            # Child windows aren't registered with the window manager (only
            # top-level windows, where parent_window is None, get registered).
            # If this draw_state isn't itself registered, walk up the
            # parent_window chain to the root window and bring it to front
            # instead, so dragging/clicking a nested view raises its owner.
            if draw_state._tile_id not in Melty.registered_windows:
                node = draw_state
                while (node._tile_id not in Melty.registered_windows
                       and node.parent_window is not None
                       and node.parent_window is not node):
                    node = node.parent_window
                if node._tile_id in Melty.registered_windows:
                    draw_state = node

            window_key = draw_state._tile_id
            cls.pending_move_to_front = (window_key, draw_state)

            # window_key = f"{cls.pending_move_to_front[0]}_window"
            # if window_key in Melty.registered_windows:
            #     # Remove and re-insert to move to end (top)
            #     window = Melty.registered_windows.pop(window_key)
            #     Melty.registered_windows[window_key] = window

        # Melty.cache.invalidate_up(tile_id)

    @classmethod
    def apply_move_to_front(cls):
        if cls.pending_delete_window is not None:
            window_key, draw_state = cls.pending_delete_window
            if window_key in Melty.registered_windows:
                draw_state.last_seen = None
                del Melty.registered_windows[window_key]

                print(f"Deleted window {window_key}")
                Melty.cache.invalidate_by_obj(Melty.registered_windows)
                Melty.cache.invalidate_up(draw_state._tile_id, max_depth=4, force=True)
            else:
                print(f"Warning: Tried to delete window but {window_key} not found in registered_windows")
                print(f"Registered windows: {list(Melty.registered_windows.keys())}")

            # Views under a deleted window give up their GL resources
            # (queued; drained by flush_deletes in end_frame). Their
            # draw_states persist, so a re-created window lazily
            # re-allocates on its next draw.
            from src.lsd.gl_gui.gl_state import GLState
            GLState.on_window_deleted(draw_state)

            cls.pending_delete_window = None
            request_render()
            return

        if cls.pending_move_to_front is None or Melty.imgui_popup_open:
            return

        if not cls.imgui_active:

            window_key = cls.pending_move_to_front[0]
            window_z_pos = len(Melty.registered_windows) + Melty.top_layer_boost
            if window_z_pos != cls.pending_move_to_front[1].layer:
                cls.pending_move_to_front[1].layer = window_z_pos
                draw_state = cls.pending_move_to_front[1]
                draw_state.active_layer = window_z_pos
                if window_key in Melty.registered_windows:
                    # Remove and re-insert to move to end (top)
                    window = Melty.registered_windows.pop(window_key)
                    Melty.registered_windows[window_key] = window
                else:
                    print(f"Warning: Tried to move window to front but {window_key} not found in registered_windows")
                    print(f"Registered windows: {list(Melty.registered_windows.keys())}")

                # if not cls.window_drag and not imgui.is_mouse_down(0):
                #     Melty.cache.invalidate_by_obj(Melty.registered_windows)
                #     note = Note(name="", reason="move_to_front", draw_state=draw_state, tint=(0.5, 1.0, 0.5))
                #     Melty.cache.invalidate_up(cls.pending_move_to_front[1]._tile_id, max_depth=4, force=True, note=note)
                cls.pending_move_to_front = None

    @classmethod
    def draw_blockers_to(cls):
        # Draw invisible buttons for each
        current_pos = imgui.get_cursor_screen_pos()
        blockers_rev = reversed(cls.imgui_blockers[:])
        for layer, rect in enumerate(blockers_rev):
            if rect is not None:
                imgui.set_cursor_screen_pos((rect[0], rect[1]))
                imgui.button(f"melty_blocker_{layer}",
                             rect[2] - rect[0],
                             rect[3] - rect[1])

        imgui.set_cursor_screen_pos(current_pos)

    @classmethod
    def set_channel(cls, layer_idx):
        if cls.channels_split:
            imgui.get_window_draw_list().channels_set_current(layer_idx)


    @classmethod
    def get_tile_id(cls):
        if len(cls.tile_id_stack) > 0:
            return cls.tile_id_stack[-1]
        else:
            return ""

    @classmethod
    def get_parent_tile_id(cls):
        if len(cls.tile_id_stack) > 1:
            return cls.tile_id_stack[-2]
        else:
            return None

    @classmethod
    def push_clip(cls, rect):
        draw_list = imgui.get_window_draw_list()
        current_clip = cls.get_clip_rect()
        if current_clip is not None:
            from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
            clip_new_rect = (
                max(current_clip[0], snap_int(rect[0])),
                max(current_clip[1], snap_int(rect[1])),
                min(current_clip[2], snap_int(rect[2])),
                min(current_clip[3], snap_int(rect[3])),
            )
            rect = clip_new_rect

        draw_list.push_clip_rect(*rect)
        cls.clip_stack.append(rect)

    @classmethod
    def pop_clip(cls):
        if len(cls.clip_stack) == 0:
            return
        draw_list = imgui.get_window_draw_list()
        draw_list.pop_clip_rect()
        cls.clip_stack.pop()

    @classmethod
    def get_clip_rect(cls):
        if len(cls.clip_stack) == 0:
            # fixed_size = cls.fixed_size_stack[-1] if len(cls.fixed_size_stack) > 0 else None
            # # if fixed_size is not None and fixed_size.width is not None and fixed_size.height is not None:
            # #     return (
            # #         fixed_size.left,
            # #         fixed_size.top,
            # #         fixed_size.left + fixed_size.width,
            # #         fixed_size.top + fixed_size.height
            # #     )
            return None

        return cls.clip_stack[-1]

    @classmethod
    def apply_clip_ds(self, draw_state):
        x = draw_state.left
        y = draw_state.top
        left, top = self.apply_clip((x, y))
        width, height = draw_state.width, draw_state.height
        right, bottom = draw_state.abs_left + width, draw_state.abs_top + height
        right, bottom = self.apply_clip((right, bottom))
        width, height = right - x, bottom - y

        return (draw_state.abs_left, draw_state.abs_top, width, draw_state.height)

    @classmethod
    def apply_clip(cls, point, fixed_size_ds=None):
        x,y = point
        fix_sized_ds = cls.fixed_size_stack[-1] if len(cls.fixed_size_stack) > 0 else fixed_size_ds
        if fix_sized_ds is not None and fix_sized_ds.width is not None and fix_sized_ds.height is not None:
            margin = (len(Melty.bg_stack) + 1) * 2.0
            clip_rect = (
                fix_sized_ds.abs_left,
                fix_sized_ds.abs_top,
                fix_sized_ds.abs_left + fix_sized_ds.width,
                fix_sized_ds.abs_top + fix_sized_ds.height
            )
        else:
            clip_rect = cls.get_clip_rect()

        if clip_rect is None:
            return x,y

        clip_left, clip_top, clip_right, clip_bottom = clip_rect
        x = max(clip_left, min(x, clip_right))
        y = max(clip_top, min(y, clip_bottom))
        return (x, y)

    @classmethod
    def apply_clip_x(cls, x):

        x,y = cls.apply_clip((x,0))
        return x

    @classmethod
    def apply_clip_width(cls, draw_state):
        width = draw_state.width
        fix_sized_ds = cls.fixed_size_stack[-1] if len(cls.fixed_size_stack) > 0 else None
        if fix_sized_ds is not None and fix_sized_ds.width is not None:

            x = draw_state.abs_left + draw_state.width
            x, y = cls.apply_clip((x, 0))
            width = x - draw_state.abs_left
        return width


    @classmethod
    def get_clip_size(cls):
        if len(cls.clip_stack) == 0:
            return None
        rect = cls.clip_stack[-1]
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        return width - 1, height - 1

    @classmethod
    def has_clip(cls):
        return len(cls.clip_stack) > 0

    @classmethod
    def get_parent_size(cls):
        if len(cls.clip_stack) < 2:
            return None, None
        rect = cls.clip_stack[-2]
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        return width, height

    @classmethod
    def get_space_left(cls):
        clip_rect = cls.get_clip_rect()
        if clip_rect is None:
            return 40
        cursor_x, _ = imgui.get_cursor_screen_pos()
        space_left = clip_rect[2] - cursor_x - 23
        return space_left

    @classmethod
    def init_complete(cls):
        return cls.frame_count > 2

    @classmethod
    def inside_clip(cls, draw_state=None, rect=None):
        clip_rect = cls.get_clip_rect()
        if clip_rect is None:
            return True
        clip_left, clip_top, clip_right, clip_bottom = clip_rect

        if draw_state is not None:
            left = draw_state.left
            top = draw_state.top
            width = draw_state.width
            height = draw_state.height
        else:
            left, top, width, height = rect

        if top is None or left is None:
            return True

        if width is None or height is None:
            return True

        if (top + height < clip_top or top > clip_bottom):
            return False
        return True

    @classmethod
    def fully_inside_clip(cls, draw_state=None, rect=None):
        clip_rect = cls.get_clip_rect()
        if clip_rect is None:
            return True
        clip_left, clip_top, clip_right, clip_bottom = clip_rect

        if draw_state is not None:
            left = draw_state.left
            top = draw_state.top
            width = draw_state.width
            height = draw_state.height
        else:
            left, top, width, height = rect

        if top is None or left is None:
            return True

        if width is None or height is None:
            return True

        if (top < clip_top or top + height > clip_bottom):
            return False

        return True

    @classmethod
    def undo_clip_n(cls, undo_point_id, n: int):
        """Undo (pop) only the last `n` clip rects and remember them for redo."""
        if not isinstance(n, int):
            raise TypeError("n must be an int")
        if n <= 0:
            return

        if not cls.clip_stack:
            cls.clip_stack_holder[undo_point_id] = []
            return

        n = min(n, len(cls.clip_stack))
        popped = cls.clip_stack[-n:]  # tail in original push order

        # Save only what we popped so redo can reapply just those.
        cls.clip_stack_holder[undo_point_id] = popped

        draw_list = imgui.get_window_draw_list()
        for _ in range(n):
            draw_list.pop_clip_rect()

        # Keep the remaining stack
        cls.clip_stack = cls.clip_stack[:-n]

    @classmethod
    def redo_clip_n(cls, undo_point_id):
        """Redo (push) the clip rects saved by undo_clip_n()."""
        popped = cls.clip_stack_holder.pop(undo_point_id, None)
        if not popped:
            return

        draw_list = imgui.get_window_draw_list()
        for rect in popped:
            draw_list.push_clip_rect(*rect)

        cls.clip_stack.extend(popped)

    @classmethod
    def undo_clip(cls, undo_point_id, n: int | None = None):
        if n is None:
            n = len(cls.clip_stack)
        return cls.undo_clip_n(undo_point_id, n)

    @classmethod
    def redo_clip(cls, undo_point_id):
        return cls.redo_clip_n(undo_point_id)

    @classmethod
    def current_path(cls) -> tuple[tuple[str, int | None], ...]:
        return tuple(cls._path_stack)

    @classmethod
    def push_slot(cls, field: str, idx: int | None):
        cls._path_stack.append((field, idx))

    @classmethod
    def pop_slot(cls):
        cls._path_stack.pop()

    @classmethod
    def current_root(cls, module_id: str) -> cst.Module:
        return cls._root_by_module[module_id]

    @classmethod
    def bump_gen(cls, module_id: str):
        cls._gen_by_module[module_id] += 1

    @classmethod
    def current_gen(cls, module_id: str) -> int:
        return cls._gen_by_module[module_id]

    # LibCST tracking---------------------------------------------------------

    @classmethod
    def indent(cls, amount):
        if amount == 0:
            return
        cls.indent_count += 1
        cls.current_indent += amount
        cls.max_indent = max(cls.max_indent, cls.current_indent)
        imgui.indent(amount)

    @classmethod
    def unindent(cls, amount):
        if amount == 0:
            return
        cls.current_indent -= amount
        imgui.unindent(amount)
        cls.unindent_count += 1

    @classmethod
    def inside_window(cls):
        return len(cls.window_stack) > 0

    @classmethod
    def shift_down(cls):
        return (glfw.get_key(cls.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                glfw.get_key(cls.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)

    @classmethod
    def is_window_enabled(cls):
        return cls.window_enabled
        # if len(cls.window_stack) == 0:
        #     return True
        # return cls.window_stack[-1][1]

    @classmethod
    def get_bg_color(cls, depth=None):
        if depth is None:
            depth = cls.depth
        if len(cls.bg_stack) == 0:
            return 0, 0, 0

        # Allow for negative index from end, but clamp to available range
        if depth < 0:
            depth = len(cls.bg_stack) + depth
        depth = max(0, min(depth, len(cls.bg_stack) - 1))
        return cls.bg_stack[depth][0:3]

    @classmethod
    def shift_key(cls):
        return (glfw.get_key(cls.vis.window, glfw.KEY_LEFT_SHIFT) == glfw.PRESS or
                glfw.get_key(cls.vis.window, glfw.KEY_RIGHT_SHIFT) == glfw.PRESS)

    @classmethod
    def ctrl_key(cls, ):
        return (glfw.get_key(cls.vis.window, glfw.KEY_LEFT_CONTROL) == glfw.PRESS or
                glfw.get_key(cls.vis.window, glfw.KEY_RIGHT_CONTROL) == glfw.PRESS)

    @classmethod
    def init(cls, **kwargs):


        for key, value in kwargs.items():
            setattr(cls, key, value)

        FileWatch.start()

        cls.global_attrs["style_manager"] = getattr(cls, "style_manager", None)

        cls.annotation_mode = False
        # NOTE: do NOT pin RenderFuncs.<name> to its resolved function here
        # (the old `setattr(RenderFuncs, name, func._resolve())` loop). The
        # original version froze whatever wrapper was registered at init, so a
        # later recompile of e.g. `button` never reached RenderFuncs.button
        # call sites. _LazyRenderFunc re-resolves through render_funcs_by_name
        # on every call by design (one dict get - noise compared to a render);
        # leaving the handles in place is what makes recompiles take.


    @classmethod
    def in_annotation_mode(cls):
        """Annotation calls intercepted? — startup's global flag OR this
        thread's recompile-exec scope (annotation_scope)."""
        return cls.annotation_mode or getattr(cls._annotation_tls, "active", False)

    @classmethod
    @contextmanager
    def annotation_scope(cls):
        """Thread-local annotation mode for a recompile's exec: field
        annotations that call render funcs return carriers (annotation_track)
        instead of rendering on a non-GL thread. Other threads — including the
        render thread mid-frame — are unaffected."""
        prev = getattr(cls._annotation_tls, "active", False)
        cls._annotation_tls.active = True
        try:
            yield
        finally:
            cls._annotation_tls.active = prev

    @classmethod
    def init_ui(cls, **kwargs):
        pass
        # cls.backend.start()

    @classmethod
    def is_key_pressed(cls, key=glfw.KEY_ESCAPE):
        # A focused text editor owns the keyboard. Block all global hotkeys so
        # typing (including the editor's own Ctrl shortcuts) never leaks into
        # app-level handlers. Centralized here so call sites don't each have to
        # guard with `and Melty.text_focused_ds is None`.
        if cls.text_focused_ds is not None:
            return False
        if imgui.is_any_item_focused() or imgui.is_any_item_active():
            if not cls.ctrl_key():
                # If any item is focused or active, we don't want to capture key presses
                return False

        if key not in cls.vis.tracked_keys:
            cls.vis.tracked_keys.append(key)
            cls.vis.first_frame_keys.add(key)

        if glfw.get_key(cls.vis.window, key) == glfw.PRESS:
            if key in cls.vis.first_frame_keys:
                return True
        return False

def _register_annotated_window(cls, kwargs):
    # @window(view_func=RenderFuncs.draw_blank) hands us a _LazyRenderFunc - a
    # name placeholder, since the target isn't importable at decoration time
    # (cycles). By the time the window registers, the render func is registered,
    # so turn the placeholder into the real function now: downstream (draw_main's
    # window loop) then sees a plain function reference, resolved once, not a
    # proxy re-resolved every call. Name check avoids importing render_funcs.
    vf = kwargs.get("view_func")
    if type(vf).__name__ == "_LazyRenderFunc":
        real = Melty.render_funcs_by_name.get(vf.__name__)
        if real is not None:
            kwargs["view_func"] = real
    Melty.annotated_window_classes[cls.__name__] = (cls, kwargs)


set_window_registrar(_register_annotated_window)


# The RenderFuncs accessor + CodeGenerator live in render_funcs.py (its own file
# so the generator only ever rewrites that small module). Melty just owns the
# render_funcs_by_name registry the @render_func decorator populates.


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
    CLICK = 'on_click'
    DOWN = 'on_mouse_down'
    DRAG = 'on_drag'
    DRAG_UP = 'on_drag_up'
    HOVERED = 'on_hover'
    SCROLL = 'on_scroll'

class MouseAction:
    def __init__(self, action_type: ActionType, button=0, value=None):
        self.action_type = action_type
        self.button = button
        self.value = value

from enum import Enum


def add_to_collection(collection, item, preferred_key=None):
    """
    Add an item to a collection (list or dict).
    If a dict and preferred_key is given, use it if unique; else generate_id() until unique.
    Returns:
      - None on success, or an error message (str) on failure.
    """
    try:
        if hasattr(collection, "append_to"):
            collection.append_to(item)
            return collection
        elif isinstance(collection, list):
            collection.append(item)
            return None
        elif isinstance(collection, (dict, MutableMapping)):
            if hasattr(item, 'id'):
                preferred_key = item.id
            key = preferred_key
            if key is not None and key in collection:
                key = None
            if key is None:
                key = generate_id()
            collection[key] = item

        elif hasattr(collection, '__dict__') and not isinstance(collection, (types.MappingProxyType)):
            collection = collection.__dict__
            if hasattr(item, 'id'):
                preferred_key = str(item.id)

            key = preferred_key
            if key is not None and key in collection:
                key = None
            if key is None:
                key = generate_id()
            collection[key] = item

        if hasattr(item, 'tint'):
            if item.tint is None or item.tint == (0, 0, 0):
                lighten = 0.2
                item.tint = Melty.bg_stack[-1]
                item.tint = (min(1.0, item.tint[0] + lighten),
                             min(1.0, item.tint[1] + lighten),
                             min(1.0, item.tint[2] + lighten))

        Melty.cache.invalidate_by_obj(collection)
        request_render()
    except Exception as e:
        print_stack_trace(exception=e)

    return collection


def delete_from_collection(key, collection):
    if isinstance(collection, list):
        try:
            idx = int(key)
            if 0 <= idx < len(collection):
                collection.pop(idx)
                return None
            else:
                return f"Index {idx} out of range for list of length {len(collection)}."
        except Exception as e:
            return f"Error removing index {key} from list: {e}"
    elif isinstance(collection, (dict, MutableMapping)):
        if key in collection:
            collection.pop(key)
            return None
        else:
            return f"Key {key!r} not found in dict."
    elif hasattr(collection, '__dict__'):
        collection = collection.__dict__
        if key in collection:
            collection.pop(key)
            return None
        else:
            return f"Key {key!r} not found in object's __dict__."


def _supports_reorder(mp) -> bool:
    return hasattr(mp, "reorder") and callable(getattr(mp, "reorder"))


def _compute_reordered_keys(mp, moving_key: str, anchor_key: str | None, tag: str) -> list[str]:
    keys = list(mp.keys())
    if moving_key in keys:
        keys.remove(moving_key)
    if anchor_key is not None and anchor_key in keys:
        idx = keys.index(anchor_key) + (1 if tag == "bottom" else 0)
    else:
        idx = 0 if tag == "top" else len(keys)
    keys.insert(idx, moving_key)
    return keys


def _reorder_keys_in_mapping(mp, keys: list[str]) -> bool:
    if _supports_reorder(mp):
        mp.reorder(keys)
        return True
    if isinstance(mp, dict):  # was: type(mp) is dict
        old = dict(mp)
        mp.clear()
        for k in keys:
            if k in old:
                mp[k] = old[k]
        for k, v in old.items():
            if k not in mp:
                mp[k] = v
        return True
    return False


def _insert_relative_in_mapping(mp, new_key: str, value, anchor_key: str | None, tag: str) -> bool:
    """
    Insert/ensure key and position it relative to anchor without destructive deletes.
    Returns True if positioned; False if mapping can't be safely reordered.
    """
    if new_key not in mp:
        mp[new_key] = value  # inserts at end (FolderProxy will create dir; others set value)
    keys = _compute_reordered_keys(mp, new_key, anchor_key, tag)
    return _reorder_keys_in_mapping(mp, keys)


def apply_collection_action(action: CollectionAction):
    """
    Returns:
        None on success, or an error message (str) on failure.

    Notes:
      - Uses action.source_unique / action.target_unique with resolved indices
        to infer list-unique bases and shift neighbor draw-states accordingly.
      - Records the moved/copied object's draw state in Melty.move_draw_state_pending
        as { id(obj): action.source_draw_state } for the render loop to remap.
    """

    # ---------------- helpers (no mutation) ----------------
    def _norm_tag(tag):
        if tag is None:
            return "top"
        t = str(tag).lower()
        return t if t in ("top", "bottom") else None

    def _get_existing_id(obj):
        if isinstance(obj, (dict, MutableMapping)) and "id" in obj:
            return str(obj["id"])
        maybe = getattr(obj, "id", None)
        return str(maybe) if maybe is not None else None

    def _resolve_list_index(lst, key_or_index):
        # numeric index?
        try:
            idx = int(key_or_index)
            return idx if 0 <= idx < len(lst) else None
        except Exception:
            pass
        # id string?
        needle = str(key_or_index)
        for i, el in enumerate(lst):
            if isinstance(el, (dict, MutableMapping)) and "id" in el and str(el["id"]) == needle:
                return i
            maybe = getattr(el, "id", None)
            if maybe is not None and str(maybe) == needle:
                return i
        return None

    def _supports_reorder(mp) -> bool:
        return hasattr(mp, "reorder") and callable(getattr(mp, "reorder"))

    def _looks_like_dir_value(val) -> bool:
        # Keep this narrow: FolderProxy directory value
        try:
            import FolderProxy  # or import at top
        except Exception:
            FolderProxy = ()
        return isinstance(val, FolderProxy)

    def _insert_pos_for_list(anchor_index, tag):
        return anchor_index if tag == "top" else anchor_index + 1

    def _unique_key_for_dict(d, preferred: str | None):
        if preferred and preferred not in d:
            return preferred
        gen = globals().get("generate_id")
        if not callable(gen):
            return None
        k = gen()
        while k in d:
            k = gen()
        return k

    # Draw-state of the moved/copied item itself (neighbors handled separately)
    def _record_draw_state(obj):
        try:
            if obj is None or action.source_draw_state is None:
                return
            if not hasattr(Melty, "move_draw_state_pending") or Melty.move_draw_state_pending is None:
                Melty.move_draw_state_pending = {}
            Melty.move_draw_state_pending[id(obj)] = action.source_draw_state
        except Exception:
            pass  # never break the transform

    # --- List neighbor shifting via inferred base (unique(i) = base + i) ---
    def _infer_base(known_unique, known_index):
        try:
            if isinstance(known_unique, int) and isinstance(known_index, int):
                return known_unique - known_index
        except Exception:
            pass
        return None

    def _shift_range_by_base(base: int | None, start_idx: int, end_idx: int, delta: int):
        """
        Shift draw_state_registry keys for indices [start_idx..end_idx] by `delta`,
        using unique(i) = base + i. No-ops if base is None.
        """
        if base is None or delta == 0 or start_idx > end_idx:
            return
        registry = Melty.vis.root.draw_state_registry
        # Stage moves to avoid collisions
        moves = []
        for i in range(start_idx, end_idx + 1):
            old_u = base + i
            ds = registry.get(old_u)
            if ds is not None:
                new_u = old_u + delta  # invariant: Δunique == Δindex
                moves.append((old_u, new_u, ds))
        # Remove then write
        for old_u, _, _ in moves:
            registry.pop(old_u, None)
        for _, new_u, ds in moves:
            if hasattr(ds, "unique"):
                ds.unique = new_u
            registry[new_u] = ds

    # ---------------- normalize inputs ----------------
    src_owner = action.source_collection
    dst_owner = action.target_collection
    if src_owner is None or dst_owner is None:
        return "Both source_collection and target_collection must be set on the action."

    # Capture owner types BEFORE any __dict__ coercion (for __field_defaults__)
    src_owner_type = type(src_owner)
    dst_owner_type = type(dst_owner)
    item = None
    tag = _norm_tag(action.target_tag)
    if tag is None:
        return "target_tag must be 'top' or 'bottom'."

    op = action.operation.value if isinstance(action.operation, Enum) else str(action.operation).lower()
    if op not in ("move", "copy"):
        return "operation must be OperationType.MOVE or OperationType.COPY."
    is_move = (op == "move")

    # Work on raw containers (lists or dict views of objects)
    src = src_owner
    dst = dst_owner
    if not isinstance(src, (list, dict, MutableMapping)) and hasattr(src, "__dict__"):
        src = src.__dict__
    if not isinstance(dst, (list, dict, MutableMapping)) and hasattr(dst, "__dict__"):
        dst = dst.__dict__

    same_collection = (src is dst)

    # If destination is a dict but CLASS exposes a shared __field_defaults__,
    # reorder *dst* to match class_defaults order (order-only; no insertion/rebinding).
    if (not isinstance(dst, list)
            and hasattr(dst_owner_type, "__field_defaults__")
            and type(dst) is dict):
        class_defaults = dst_owner_type.__field_defaults__
        ordered_keys = [k for k in class_defaults.keys() if k in dst]
        extra_keys = [k for k in list(dst.keys()) if k not in class_defaults]
        if ordered_keys or extra_keys:
            old = dict(dst)
            dst.clear()
            for k in ordered_keys:
                dst[k] = old[k]
            for k in extra_keys:
                dst[k] = old[k]

    # ---------------- six explicit cases ----------------

    # 1) LIST -> LIST (includes list-to-self)
    if isinstance(src, list) and isinstance(dst, list):
        s_idx = _resolve_list_index(src, action.source_key)
        if s_idx is None:
            return (f"Source key {action.source_key!r} not found in source list "
                    f"as index or id (len={len(src)}).")

        if len(dst) == 0:
            t_idx = 0
        else:
            t_idx = _resolve_list_index(dst, action.target_key)
            if t_idx is None:
                t_idx = len(dst) - 1  # last element as anchor

        # infer bases from (unique, index)
        base_same = _infer_base(action.source_unique, s_idx) if same_collection else None
        base_src = _infer_base(action.source_unique, s_idx) if not same_collection else None
        base_dst = _infer_base(action.target_unique, t_idx) if not same_collection else None

        # capture lengths BEFORE mutation
        src_len_before = len(src)
        dst_len_before = len(dst)

        # compute insert index (adjust if same list and move across pop)
        insert_at = _insert_pos_for_list(t_idx, tag)
        if is_move and same_collection:
            base = t_idx if tag == "top" else t_idx + 1
            if s_idx < base:
                base -= 1
            insert_at = max(0, min(base, len(dst)))

        item = src[s_idx]
        if is_move and same_collection:
            popped = src.pop(s_idx)
            try:
                dst.insert(insert_at, popped)
            except Exception as e:
                src.insert(s_idx, popped)
                return f"Internal error during same-list move insert: {e}"

            # neighbors in SAME list
            if insert_at < s_idx:
                _shift_range_by_base(base_same, start_idx=insert_at, end_idx=s_idx - 1, delta=+1)
            elif insert_at > s_idx:
                _shift_range_by_base(base_same, start_idx=s_idx + 1, end_idx=insert_at, delta=-1)

            _record_draw_state(popped)

        else:
            # cross-list copy/move OR same-list copy
            try:
                dst.insert(insert_at, item)
            except Exception as e:
                return f"Internal error inserting into target list: {e}"

            # target neighbors shift right from insert_at
            _shift_range_by_base(base_dst, start_idx=insert_at, end_idx=dst_len_before - 1, delta=+1)

            if is_move:
                try:
                    src.pop(s_idx)
                except Exception as e:
                    # rollback best-effort
                    try:
                        dst.pop(insert_at)
                    except Exception:
                        pass
                    return f"Internal error removing from source after insert: {e}"

                # source neighbors collapse left after s_idx
                _shift_range_by_base(base_src, start_idx=s_idx + 1, end_idx=src_len_before - 1, delta=-1)

            _record_draw_state(item)

    # 2) DICT -> DICT (reorder or transfer)
    elif isinstance(src, (dict, MutableMapping)) and isinstance(dst, (dict, MutableMapping)):
        s_key = action.source_key
        t_key = action.target_key
        if s_key not in src:
            return f"Source key {s_key!r} not found in source dict."

        if t_key is None and len(dst) > 0:
            t_key = next(iter(dst.keys()))

        value = src[s_key]

        if same_collection:
            # pure reorder; never use clear/pop on mappings that might have side-effects
            if not (s_key == t_key or t_key is None):
                keys = _compute_reordered_keys(dst, s_key, t_key, tag)
                ok = _reorder_keys_in_mapping(dst, keys)
                if not ok:
                    return "Cannot safely reorder this mapping without destructive deletes."
        else:
            final_key = s_key
            if s_key in dst:
                is_move = False  # collision -> copy

            # If the source owner exposes a true move, use it (duck-typed; generic)
            if is_move and hasattr(src_owner, "move_item") and callable(getattr(src_owner, "move_item")):
                try:
                    # perform the physical move; returns the final key name at dst
                    final_key = src_owner.move_item(dst_owner, s_key, new_name=s_key)
                    # position it relative to t_key without destructive deletes
                    _insert_relative_in_mapping(dst, final_key, dst[final_key], t_key, tag)
                    _record_draw_state(dst[final_key])
                    return
                except NotImplementedError:
                    pass
                except Exception as e:
                    # Fall back to safe copy semantics if hook fails
                    is_move = False

            # No move hook: do a safe insert+reorder only
            ok = _insert_relative_in_mapping(dst, final_key, value, t_key, tag)
            if not ok:
                if type(dst) is dict:
                    tmp = dict(dst)
                    tmp[final_key] = value
                    keys = _compute_reordered_keys(tmp, final_key, t_key, tag)
                    dst.clear()
                    for k in keys:
                        dst[k] = tmp[k]
                else:
                    return "Target mapping cannot be reordered safely."

            # IMPORTANT: never pop a value item unless we actually moved it
            if is_move:
                if _looks_like_dir_value(value) and (_supports_reorder(src) or _supports_reorder(dst)):
                    # Treat as copy for safety (we didn't really move on disk)
                    is_move = False
                else:
                    src.pop(s_key, None)

            _record_draw_state(value)

    # 3) DICT -> LIST (insert into list)
    elif isinstance(src, (dict, MutableMapping)) and isinstance(dst, list):
        s_key = action.source_key
        if s_key not in src:
            return f"Source key {s_key!r} not found in source dict."
        item = src[s_key]

        dst_len_before = len(dst)
        if dst_len_before == 0:
            t_idx = 0
        else:
            t_idx = _resolve_list_index(dst, action.target_key)
            if t_idx is None:
                t_idx = len(dst) - 1
        insert_at = max(0, min(_insert_pos_for_list(t_idx, tag), len(dst)))

        base_dst = _infer_base(action.target_unique, t_idx)

        try:
            dst.insert(insert_at, item)
        except Exception as e:
            return f"Internal error inserting into list: {e}"

        # target neighbors shift right
        _shift_range_by_base(base_dst, start_idx=insert_at, end_idx=dst_len_before - 1, delta=+1)

        if is_move:
            src.pop(s_key, None)

        _record_draw_state(item)

    # 4) LIST -> DICT (remove from list)
    elif isinstance(src, list) and isinstance(dst, (dict, MutableMapping)):
        s_idx = _resolve_list_index(src, action.source_key)
        if s_idx is None:
            return (f"Source key {action.source_key!r} not found in source list "
                    f"as index or id (len={len(src)}).")
        item = src[s_idx]

        src_len_before = len(src)
        base_src = _infer_base(action.source_unique, s_idx)

        t_anchor = action.target_key
        if t_anchor is not None and len(dst) > 0 and t_anchor not in dst:
            t_anchor = list(dst.keys())[-1]

        preferred_id = _get_existing_id(item)
        new_key = _unique_key_for_dict(dst, preferred_id)
        if new_key is None:
            return "generate_id() is not available to create a unique key for list->dict."

        ok = _insert_relative_in_mapping(dst, new_key, item, t_anchor, tag)
        if not ok:
            # Fallback for plain dicts only
            if type(dst) is dict:
                tmp = dict(dst)
                if new_key not in tmp:
                    tmp[new_key] = item
                keys = _compute_reordered_keys(tmp, new_key, t_anchor, tag)
                dst.clear()
                for k in keys:
                    dst[k] = tmp[k]
            else:
                return "Target mapping cannot be reordered safely."

        if is_move:
            try:
                src.pop(s_idx)
            except Exception as e:
                # rollback best-effort
                try:
                    if new_key in dst:
                        del dst[new_key]
                except Exception:
                    pass
                return f"Internal error removing from source list after dict insert: {e}"

            # collapse gap in source list
            _shift_range_by_base(base_src, start_idx=s_idx + 1, end_idx=src_len_before - 1, delta=-1)

        _record_draw_state(item)

    else:
        return "Unsupported collection types. Expected list or dict for both source and target."

    # Melty.cache.invalidate_all()
    # ---------------- reflect order into __field_defaults__ (order-only, in place) ----------------
    if not isinstance(dst, list) and hasattr(dst_owner_type, "__field_defaults__"):
        class_defaults = dst_owner_type.__field_defaults__
        dst_keys = list(dst.keys())
        defaults_keys = list(class_defaults.keys())

        common_in_dst_order = [k for k in dst_keys if k in class_defaults]
        defaults_only_tail = [k for k in defaults_keys if k not in dst]

        new_order = common_in_dst_order + defaults_only_tail
        if new_order != defaults_keys:
            old_vals = {k: class_defaults[k] for k in class_defaults.keys()}
            class_defaults.clear()
            for k in new_order:
                class_defaults[k] = old_vals.get(k)



    return None


# First import only: until here Core.melty is the MockMelty recorder, whose
# pre-init interactions replay onto the real class. On a hotswap reinit
# Core.melty is already the (previous) Melty class - no recorder, nothing
# to replay - and the line would AttributeError, blocking the reload.
if hasattr(Core.melty, "replay"):
    applied, skipped = Core.melty.replay(Melty)
Core.melty = Melty

class DepthState:
    def __init__(self):
        self.flow_spacing = 0.0


class MeltyState:
    def __init__(self):
        self.hover_stack = []
        self.hotkey_stack = []

        self.size_stack = []
        self.triggered_actions = {}

        self.top_event_depth = {}
        self.top_event = {}

        self.dragged_item = None
        self.dragged_tile = None
        self.max_distance = 200

        self.selected_views = {}

        self.drag_in_progress = False

        self.initial_drag_offset = (0, 0)
        self.mouse_down_pos = (0, 0)
        self.total_drag_distance = 0.0
        self.total_drag_frames = 0
        self.last_mouse_pos = None
        self.drag_delta = (0,0)

        self.nearest_drop_target = None
        self.nearest_drop_target_tag = None
        self.nearest_drop_distance = self.max_distance
        self.flow_spacing = 0.0

        self.drag_drop_target = None
        self.drag_drop_target_tag = None
        self.drag_target_key = None
        self.drag_target_collection = None
        self.drag_drop_action = CollectionAction()
        self.initial_scroll_offset = (0,0)

        self.target_distance = self.max_distance

        self.items_to_delete = []


    def check_event_value(self, unique, mouse_btn, event_type):
        if unique in self.triggered_actions:
            action = self.triggered_actions[unique]
            if action.action_type == event_type:
                return action.value
        return None


    def check_event(self, unique, mouse_btn, event_type):
        if unique in self.triggered_actions:
            action = self.triggered_actions[unique]
            if action.action_type == event_type and action.button == mouse_btn:
                return True
        return False

    def mark_event(self, unique, mouse_btn, event_type: ActionType, value=None):
        self.triggered_actions[unique] = MouseAction(event_type, mouse_btn, value)
        depth = Melty.depth
        if event_type not in self.top_event_depth or depth < self.top_event_depth[event_type]:
            self.top_event_depth[event_type] = depth
            self.top_event[event_type] = unique

    def clear_events(self, unique):
        if unique in self.triggered_actions:
            self.triggered_actions.pop(unique)




@defaults(tint=(0.2391563206911087, 0.47928887605667114, 0.7674418687820435))
class ManagedWindow:
    def __init__(self, input_value=None, draw_state=None, window_args=None, name=None):
        self.input_value = input_value
        self.draw_state = draw_state
        self.window_args = window_args
        self.name = name
        self.hidden = False

