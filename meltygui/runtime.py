import math
import time
import types
from collections import defaultdict, deque
from copy import copy
from enum import Enum
from typing import MutableMapping, Optional

import glfw
import imgui
import libcst as cst
from imgui.core import _DrawList

from rtree import index as rtree_index

from src.lsd.gl_gui.view.attribute_churn import AttributeChurnMonitor
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import DecorationManager
from src.lsd.gl_gui.view.invalidation_tracker import InvalidateTracker

from src.lsd.gl_gui.background import Background
from src.lsd.gl_gui.collection_action import CollectionAction
from src.lsd.gl_gui.collision import Collisions
from src.lsd.gl_gui.toggles import Toggles, Counters, Tint, Swoosh
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import set_window_registrar
from src.lsd.gl_gui.view.core_views.monitor import Monitor
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


def search_walk(ds, term, session):
    """Count a subtree's search matches into `session` without rendering.

    Each searchable view stashes a `_search_matcher(term, session)` closure on
    its draw_state during render (capturing its content, term-independently).
    The closure claims its own matches and recurses into children via this
    function, so the whole tree — including off-screen rows the render skips —
    contributes to the combined count. Containers without a matcher just
    recurse into their children in order.
    """
    matcher = getattr(ds, '_search_matcher', None)
    if matcher is not None:
        matcher(term, session)
        return
    children = getattr(ds, '_children', None)
    if children:
        for key in sorted(children.keys(), key=lambda k: (isinstance(k, str), k)):
            child = children[key]
            if child is not None and child is not ds:
                search_walk(child, term, session)

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
    output_debug_diff = False
    _write_suppress_window = 1.0  # seconds - for truncate+write event pairs from write_text

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
        Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10, force=True)
        if draw_state.parent_window is not None:
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
    def shutdown(cls):
        if cls.observer.is_alive():
            cls.observer.stop()
            cls.observer.join()
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import shutdown_jedi_pool
        shutdown_jedi_pool()


class Melty:

    draw_state_registry = None
    style_manager = None

    focused_ds = None
    text_focused_ds = None
    selected = set()
    last_selected = None
    large_font = None
    font_mgr = None

    root_draw_states = defaultdict(lambda: list())
    root_draw_states_by_layer = defaultdict(lambda: list())

    filter = Filter()
    detached = False

    seen_values = []

    window_drag = False
    on_drag = False
    on_scroll = False
    on_scroll_buffer = deque(maxlen=5)

    mode_stack = []
    search_stack = []

    _converters = {}
    _converter_to_type = {}
    converter_flags_by_type = {}
    converter_flags = {}

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
    # id(ds) for every draw_state whose bbox is under the cursor this frame - a
    # begin_frame snapshot of bvh_query. hover_eligible / is_bounding_hovered do
    # O(1) membership against this instead of imgui.is_mouse_hovering_rect.
    bvh_hover_ids = set()

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
    def get_default_view_function(cls, draw_state=None, real_type=None, collection_type=None, attrib_key=None):
        if draw_state is not None:
            real_type = draw_state._kwargs.get("real_type", type(draw_state._input_value))
            collection_type = draw_state._kwargs.get("type_collection", type(draw_state._collection))
            attrib_key = draw_state._kwargs.get("key", draw_state._kwargs.get("name", None))

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

    # rtree.delete only removes an entry when given the EXACT bbox it was
    # inserted with. So _bvh_bbox always mirrors what's currently in the rtree
    # for that draw_state - register/update/unregister keep them in lockstep,
    # and every delete uses _bvh_bbox (not the live, possibly-changed bbox).
    @classmethod
    def bvh_register(cls, draw_state):
        bbox = draw_state.bbox
        if bbox is None:
            return None
        rid = cls._bvh_next_id
        cls._bvh_next_id += 1
        draw_state._bvh_id = rid
        cls._bvh_id_to_ds[rid] = draw_state
        cls._bvh.insert(rid, bbox)
        draw_state._bvh_bbox = bbox
        return rid

    @classmethod
    def bvh_unregister(cls, draw_state):
        rid = draw_state._bvh_id
        if rid is None:
            return
        bbox = draw_state._bvh_bbox
        if bbox is not None:
            try:
                cls._bvh.delete(rid, bbox)
            except Exception:
                pass
        cls._bvh_id_to_ds.pop(rid, None)
        draw_state._bvh_id = None
        draw_state._bvh_bbox = None

    @classmethod
    def bvh_update(cls, draw_state):
        rid = draw_state._bvh_id
        if rid is None:
            return
        old_bbox = draw_state._bvh_bbox
        if old_bbox is not None:
            try:
                cls._bvh.delete(rid, old_bbox)
            except Exception:
                pass
        new_bbox = draw_state.bbox
        if new_bbox is not None:
            cls._bvh.insert(rid, new_bbox)
            draw_state._bvh_bbox = new_bbox
        else:
            cls._bvh_id_to_ds.pop(rid, None)
            draw_state._bvh_id = None
            draw_state._bvh_bbox = None

    @classmethod
    def prune_bvh(cls, stale_after=2):
        """Drop draw_states that haven't rendered in `stale_after` frames — closed
        windows, removed collection items, or objects replaced when their `unique`
        changed. pos_changed only runs while a view renders, so without this they
        linger in the index forever, producing duplicate hits and a wrong topmost."""
        fc = cls.frame_count
        stale = [rid for rid, ds in cls._bvh_id_to_ds.items()
                 if ds is None or ds.last_seen is None or (fc - ds.last_seen) > stale_after]
        for rid in stale:
            ds = cls._bvh_id_to_ds.get(rid)
            if ds is not None:
                cls.bvh_unregister(ds)
            else:
                cls._bvh_id_to_ds.pop(rid, None)

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
        """Hit test — returns DrawStates under the point, front-most first.

        rtree yields hits in tree order, not stacking order, so callers taking
        [0] as 'the topmost view under the cursor' would get an arbitrary one.
        Sort by shadow_depth — the same front-ness key the occlusion test uses."""
        hits = [
            cls._bvh_id_to_ds[rid]
            for rid in cls._bvh.intersection((x, y, x, y))
            if rid in cls._bvh_id_to_ds and (not cls._bvh_id_to_ds[rid].closed or not cls._bvh_id_to_ds[rid].closable)
        ]
        hits.sort(key=lambda ds: ds.z_pos or 0, reverse=True)
        return hits

    @classmethod
    def begin_frame(cls):
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

        # Route the keyboard to the focused text view. While a text editor holds
        # focus, any held key force-invalidates its tile (and its parent window
        # subtree, so the cached window re-descends into the editor) BEFORE the
        # views draw this frame. That lets draw_text re-execute and catch
        # imgui's is_key_pressed edge in the SAME frame the key goes down, even
        # when the mouse isn't hovering. This must run in begin_frame, not
        # end_frame: end_frame invalidation lands one frame too late, after the
        # key edge has already passed, which is why typing only worked while
        # hovering (the hover path keeps the tile dirty before each draw).
        if cls.text_focused_ds is not None and cls.glfw_window is not None:
            focused = cls.text_focused_ds
            # Esc releases text focus globally - no more needed. Re-render the
            # (now-)focused text view so its cursor disappears this frame, then
            # clear focus, which also unblocks global hotkeys via is_key_pressed.
            if glfw.get_key(cls.glfw_window, glfw.KEY_ESCAPE) == glfw.PRESS:
                # Close any active search globally - no hover required. This must
                # clear search_active (not just text focus): otherwise the search
                # box's re-grab-when-unfocused logic would immediately reclaim
                # focus and the find bar would never dismiss off-hover.
                owner = cls.focused_ds
                if owner is not None and getattr(owner, 'search_active', False):
                    owner.search_active = False
                    owner.search_text = ""
                    owner._search_was_active = False
                    if owner.parent_window is not None:
                        cls.cache.invalidate_up(owner.parent_window._tile_id, force=True)
                    cls.cache.invalidate(owner._tile_id, force=True)
                    cls.focused_ds = None
                if focused.parent_window is not None:
                    cls.cache.invalidate_up(focused.parent_window._tile_id, force=True)
                cls.cache.invalidate(focused._tile_id, force=True)
                cls.text_focused_ds = None
                if Toggles.text_focus_stack_trace:
                    print_stack_trace()
                request_render()
            else:
                for k in range(32, 349):  # GLFW_KEY_SPACE through GLFW_KEY_LAST
                    if glfw.get_key(cls.glfw_window, k) == glfw.PRESS:
                        if focused.parent_window is not None:
                            cls.cache.invalidate_up(focused.parent_window._tile_id, force=True)
                        cls.cache.invalidate(focused._tile_id, force=True)
                        request_render()
                        break

        # Evict draw_states that stopped rendering (closed/removed/created) so
        # the index doesn't keep stale, duplicate hits with wrong layering.
        cls.prune_bvh()

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


        cls.window_drag = ((("left_mouse_drag" in cls.events_by_type) or ("left_mouse_held" in cls.events_by_type)) or
                           (("right_mouse_drag" in cls.events_by_type) or ("right_mouse_held" in cls.events_by_type)))

        left_mouse_drag_event = cls.events_by_type.get("left_mouse_drag", None)
        right_mouse_drag_event = cls.events_by_type.get("right_mouse_drag", None)
        if right_mouse_drag_event is not None:
            is_window_resize = "window_resize" in str(right_mouse_drag_event.keys())
        else:
            is_window_resize = False

        if left_mouse_drag_event is not None:
            is_window_drag = "window_move" in str(left_mouse_drag_event.keys()) or "corner_drag" in str(left_mouse_drag_event.keys())
        else:
            is_window_drag = False

        cls.on_drag = (is_window_drag or is_window_resize or ("left_mouse_down" in cls.events_by_type)) and (not cls.imgui_active)

        cls.event_handler.begin_frame()

        if ("right_mouse_drag" in cls.events_by_type):
            right_mouse_drag_events = cls.events_by_type["right_mouse_drag"]
            for event in right_mouse_drag_events:
                Melty.cache.invalidate(event)
        #
        if ("middle_mouse_drag" in cls.events_by_type):
            right_mouse_drag_events = cls.events_by_type["middle_mouse_drag"]
            for event in right_mouse_drag_events:
                Melty.cache.invalidate(event)

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
                        Melty.cache.invalidate_up(first_event.tile_id, max_depth=6, force=True)

        Melty.all_uniques = set()

        Melty.hovered_drawstate_pending = set()

        Melty.clip_stack = []
        # cls._root_by_module[module_id] = root
        # cls._gen_by_module.setdefault(module_id, 0)
        # cls._path_stack.clear()
        fb_w, fb_h = map(int, imgui.get_io().display_size)  # or your true GL FB size if HiDPI
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
        kinks of hard min/max. Returns (x0, y0, x1, y1).
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

        xa, ya = clamp(cx, ax0, ax1), clamp(cy, ay0, ay1)
        xb, yb = clamp(cx, bx0, bx1), clamp(cy, by0, by1)
        return xa, ya, xb, yb

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
    def _draw_swoosh(overlay_dl, px, py, pw, ph, nx, ny, nw, nh, rgb,
                     p_round=0.0, n_round=0.0, p_clip=None):
        """Draw a curved connector from the parent view's outline to the nested
        view. The line is thick at both endpoints and tapers thin in the middle.
        `rgb` is the resolved highlight color (see _highlight_rgb); p_round /
        n_round are the parent/nested corner radii so the ends meet the rounded
        edge. p_clip, if given, is the parent's absolute clip rect
        (left, top, right, bottom): the parent end is anchored against the
        *visible* (clipped) part of the parent rect so the cap dot never lands
        on a region that's been scrolled/clipped away. Tunables live on
        Swoosh.*."""
        # Clamp the parent rect to its visible region so the connector anchors on
        # what's actually on screen rather than a clipped-off edge.
        if p_clip is not None:
            cl, ct, cr, cb = p_clip
            vx0, vy0 = max(px, cl), max(py, ct)
            vx1, vy1 = min(px + pw, cr), min(py + ph, cb)
            if vx1 > vx0 and vy1 > vy0:
                px, py, pw, ph = vx0, vy0, vx1 - vx0, vy1 - vy0

        # Anchor both ends at the center of the rects' shared edge (smoothed),
        # so the connector stays centered and glides as the rects move.
        x0, y0, x1, y1 = Melty._closest_perimeter_points(
            px, py, px + pw, py + ph,
            nx, ny, nx + nw, ny + nh,
        )

        # Pull the ends onto the rounded-corner boundary so the end dots sit flush
        # against the visible edge rather than the square corner.
        x0, y0 = Melty._round_rect_point(x0, y0, px, py, px + pw, py + ph, p_round)
        x1, y1 = Melty._round_rect_point(x1, y1, nx, ny, nx + nw, ny + nh, n_round)

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

        # Quadratic bezier control point: midpoint bowed perpendicular to the chord
        # by an amount scaled by curve_factor.
        mx, my = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        perp_x, perp_y = -seg_dy / seg_len, seg_dx / seg_len
        bow = seg_len * Swoosh.curve * curve_factor
        cxp, cyp = mx + perp_x * bow, my + perp_y * bow

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
            l0, l1 = left[i], left[i + 1]
            r0, r1 = right[i], right[i + 1]
            overlay_dl.add_triangle_filled(l0[0], l0[1], r0[0], r0[1], l1[0], l1[1], col)
            overlay_dl.add_triangle_filled(r0[0], r0[1], r1[0], r1[1], l1[0], l1[1], col)

        # add_triangle_filled has hard (aliased) edges, but add_polyline is
        # antialiased (DRAW_LIST_ANTI_ALIASED_LINES, on by default). Stroke the
        # ribbon's two long edges to feather them; the square ends are covered by
        # the AA cap circles below.
        if Swoosh.aa_width > 0.0:
            overlay_dl.add_polyline(left, col, flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)
            overlay_dl.add_polyline(right, col, flags=imgui.DRAW_NONE, thickness=Swoosh.aa_width)

        # Round caps over the flat (square) ends of the ribbon so the endpoints
        # read as dots rather than squared-off edges.
        cap_r = end_hw * Swoosh.cap_scale
        overlay_dl.add_circle_filled(x0, y0, cap_r, col)
        overlay_dl.add_circle_filled(x1, y1, cap_r, col)

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
            Melty.bg_stack = draw_state._bg_stack


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
                Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=7, force=True, frame_delta=2)
                if draw_state.parent_window is not None:
                    Melty.cache.invalidate_up(draw_state.parent_window._tile_id,max_depth=4, frame_delta=2)
                request_render()

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

        Melty.cache.invalidate_up(parent_window._tile_id,
                                  max_depth=10, force=True)


    @classmethod
    def refresh_nested_windows(cls, draw_state):
        parent_window = draw_state.parent_window if draw_state.parent_window is not None else \
        Melty.melty_window_stack[
            -1] if len(Melty.melty_window_stack) > 0 else draw_state
        cls.nested_window_refresh = parent_window

    @classmethod
    def end_frame(cls):
        cls.apply_move_to_front()

        cls.apply_refresh_nested_windows()
        # Reset overlay routing to the top (global, unmasked) channel so
        # end_frame draws - FPS counter, selection rects, debug text - don't
        # accidentally land on whatever per-window channel a view last set.
        if cls._overlay_channels_active:
            imgui.get_overlay_draw_list().channels_set_current(cls.max_depth - 1)

        Melty.mode_stack = []

        from src.lsd.gl_gui.view.mode import Mode
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_with_modes
        draw_with_modes(Counters, name="counters", modes=(Mode.CODE_UI, Mode.CODE_PLAIN_TEXT), mode=Mode.WINDOW)

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
                else:
                    cls.root_draw_states_by_layer[ds.abs_layer].append(ds)

        for ds_id, discard_ds in to_discard:
            cls.root_draw_states[ds_id].remove(discard_ds)

        selected_by_layer = defaultdict(list)
        for selected_ds in cls.selected:
            selected_by_layer[selected_ds.abs_layer].append(selected_ds)

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


            selected_at_layer = selected_by_layer.get(idx, [])
            # for draw_state in selected_at_layer:
            #     draw_list = imgui.get_window_draw_list()
            #     clip_rect = draw_state.abs_clip_rect
            #     draw_list.push_clip_rect(clip_rect[0], clip_rect[1], clip_rect[2], clip_rect[3], True)
            #     selected_rect = draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height
            #     draw_list.add_rect_filled(selected_rect[0], selected_rect[1], selected_rect[0] + selected_rect[2],
            #                               selected_rect[1] + selected_rect[3],
            #                               imgui.get_color_u32_rgba(1, 1, 1, 0.3))
            #     draw_list.pop_clip_rect()

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
                        overlay_dl.add_rect_filled(offset_ds.abs_left, offset_ds.abs_top,
                                                   offset_ds.abs_left + offset_ds.width,
                                                   offset_ds.abs_top + offset_ds.height,
                                                   bg_col, rounding=offset_ds.corner_radius)
                        overlay_dl.add_rect(offset_ds.abs_left, offset_ds.abs_top,
                                            offset_ds.abs_left + offset_ds.width,
                                            offset_ds.abs_top + offset_ds.height,
                                            outline_col, rounding=offset_ds.corner_radius,
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
                    rounding = draw_state.corner_radius
                    overlay_dl.channels_set_current(min(draw_state.window_index, Melty.max_layer -1))

                    overlay_dl.add_rect(draw_state.abs_left, draw_state.abs_top,
                                        draw_state.abs_left + draw_state.width,
                                        draw_state.abs_top + draw_state.height,
                                        outline_col, rounding=rounding,
                                        thickness=Tint.highlight_outline_thickness)

                    Melty._draw_swoosh(
                        overlay_dl,
                        offset_ds.abs_left, offset_ds.abs_top,
                        offset_ds.width, offset_ds.height,
                        draw_state.abs_left, draw_state.abs_top,
                        draw_state.width, draw_state.height,
                        highlight_rgb,
                        p_round=offset_ds.corner_radius,
                        n_round=draw_state.corner_radius,
                        p_clip=parent_clip,
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

        while len(melty.items_to_delete) > 0:
            key, collection = melty.items_to_delete.pop(0)
            delete_from_collection(key, collection)
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

        for selected_ds in cls.selected:
            if not selected_ds._kwargs.get("selectable", True):
                continue
            if selected_ds.width is None or selected_ds.height is None:
                continue

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
            rounding = selected_ds.corner_radius

            clip_rect = selected_ds.abs_clip_rect
            overlay.push_clip_rect(clip_rect[0], clip_rect[1], clip_rect[2], clip_rect[3], True)
            x0, y0 = selected_ds.abs_left, selected_ds.abs_top
            x1, y1 = x0 + selected_ds.width, y0 + selected_ds.height
            overlay.add_rect_filled(x0, y0, x1, y1, bg_col, rounding=rounding)
            overlay.add_rect(x0, y0, x1, y1, outline_col, rounding=rounding,
                             thickness=Tint.select_outline_thickness)
            overlay.pop_clip_rect()

        # Restore the global top channel for any later overlay draws.
        if cls._overlay_channels_active:
            overlay.channels_set_current(cls.max_depth - 1)

        Collisions.handle_collisions()

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
        from src.lsd.gl_gui.screenshot import process_captures
        process_captures(window)

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
        cls.filter.cleanup()
        cls.texture_manager.clear()
        Background.shutdown()
        Monitor.shutdown()
        FileWatch.shutdown()

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
                if cls.text_focused_ds is not None and cls.text_focused_ds.parent_window is draw_state:
                    cls.text_focused_ds = None
                    if Toggles.text_focus_stack_trace:
                        print_stack_trace()
                print(f"Deleted window {window_key}")
                Melty.cache.invalidate_by_obj(Melty.registered_windows)
                Melty.cache.invalidate_up(draw_state._tile_id, max_depth=4, force=True)
            else:
                print(f"Warning: Tried to delete window but {window_key} not found in registered_windows")
                print(f"Registered windows: {list(Melty.registered_windows.keys())}")

            cls.pending_delete_window = None
            request_render()
            return

        if cls.pending_move_to_front is None or Melty.imgui_popup_open:
            return

        if not cls.imgui_active:
            # for widx, window in enumerate(Melty.registered_windows.values()):
            #     if widx == len(Melty.registered_windows) - 1:
            #         break
            #     wds = window.draw_state
            #     wds.layer = wds.abs_layer
            #
            # for root_window in Melty.root_draw_states:
            #     for ds in Melty.root_draw_states[root_window]:
            #         ds.layer = ds.abs_layer

            window_key = cls.pending_move_to_front[0]
            window_z_pos = len(Melty.registered_windows) + Melty.top_layer_boost
            cls.pending_move_to_front[1].layer = window_z_pos
            draw_state = cls.pending_move_to_front[1]
            draw_state.active_layer = window_z_pos

            # if cls.pending_move_to_front[1]._is_nested:
            #     draw_state.layer += Melty.nested_layer_boost + 3
                # draw_state.z_pos = (draw_state.layer * Melty.max_depth) + draw_state.depth

            if cls.text_focused_ds is not None and cls.text_focused_ds.parent_window is not draw_state:
                cls.text_focused_ds = None
                if Toggles.text_focus_stack_trace:
                    print_stack_trace()

            if window_key in Melty.registered_windows:
                # Remove and re-insert to move to end (top)
                window = Melty.registered_windows.pop(window_key)
                Melty.registered_windows[window_key] = window
            else:
                print(f"Warning: Tried to move window to front but {window_key} not found in registered_windows")
                print(f"Registered windows: {list(Melty.registered_windows.keys())}")

        if not cls.window_drag:
            Melty.cache.invalidate_by_obj(Melty.registered_windows)
            Melty.cache.invalidate_up(cls.pending_move_to_front[1]._tile_id, max_depth=4, force=True)
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
        right, bottom = draw_state.left + width, draw_state.top + height
        right, bottom = self.apply_clip((right, bottom))
        width, height = right - x, bottom - y

        return (draw_state.left, draw_state.top, width, draw_state.height)

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

            x = draw_state.left + draw_state.width
            x, y = cls.apply_clip((x, 0))
            width = x - draw_state.left
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

set_window_registrar(
    lambda cls, kwargs: Melty.annotated_window_classes.__setitem__(cls.__name__, (cls, kwargs))
)


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


applied, skipped = DecorationManager.melty.replay(Melty)
DecorationManager.melty = Melty

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



    def to_delete(self, key, collection):
        self.items_to_delete.append((key, collection))


@defaults(tint=(0.2391563206911087, 0.47928887605667114, 0.7674418687820435))
class ManagedWindow:
    def __init__(self, input_value=None, draw_state=None, window_args=None, name=None):
        self.input_value = input_value
        self.draw_state = draw_state
        self.window_args = window_args
        self.name = name
        self.hidden = False

