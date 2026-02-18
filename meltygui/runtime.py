from collections import defaultdict, deque
from copy import copy
from enum import Enum
from typing import MutableMapping, Optional

import glfw
import imgui
import libcst as cst

from src.lsd.gl_gui.collection_action import CollectionAction
from src.shader_library.shader_manager.texture_manager import TextureManager
from src.shader_library.shader_manager.filter import Filter
from src.lsd.gl_gui.view.events.input_handler import InputHandler, InputEvent
from src.lsd.gl_gui.view.events.pynput_backend import PynputBackend, ImGuiBackend
from src.lsd.gl_gui.model.core_model.core_enums import generate_id
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import global_hotkeys

import OpenGL.GL as gl


class Melty:
    selected = set()
    last_selected = None

    draw_state_stack = []
    root_draw_states = set()
    root_draw_states_by_layer = defaultdict(lambda: list())

    filter = Filter()

    window_drag = False
    on_drag = False
    on_scroll = False
    on_scroll_buffer = deque(maxlen=5)

    # list, full with 32 Nones
    max_depth = 32
    nested_layer_boost = 5
    top_layer_boost = 4
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

    windows = []
    collection_stack = []
    glfw_window = None
    clip_stack = []
    clip_stack_holder = {}
    registered_windows = defaultdict(lambda: ManagedWindow())
    scroll_stack = []
    tile_id_stack = []
    wrap_stack = []
    previous_select = None

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
    type_to_default_view_func = defaultdict(lambda: set())

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

    @classmethod
    def begin_frame(cls):
        style = imgui.get_style()
        cls.seen_unique = set()
        cls.original_spacing = style.item_spacing
        cls.original_window_padding = style.window_padding
        cls.original_frame_padding = style.frame_padding

        cls.layer_inc = 0.04 / ((Melty.max_layer - 1.0) * (Melty.max_depth - 1.0)) * 65535.0

        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)
        is_popup_open = imgui.is_popup_open("", flags=imgui.POPUP_ANY_POPUP)
        Melty.imgui_popup_open = is_popup_open

        cls.backend.pump()

        cls.last_draw_state = [(None, None)] * cls.max_layer

        cls.imgui_active = cls.imgui_active_pending
        cls.imgui_active_pending = False
        cls.bg_depth = 0

        cls.imgui_blockers = cls.pending_blockers
        cls.pending_blockers = [None] * cls.max_layer

        cls.events, cls.events_by_type = cls.event_handler.process_frame()


        cls.window_drag = (("left_mouse_drag" in cls.events_by_type) or ("left_mouse_held" in cls.events_by_type))
        cls.on_drag = (("left_mouse_drag" in cls.events_by_type) or ("left_mouse_down" in cls.events_by_type))and (not cls.imgui_active)

        cls.event_handler.begin_frame()

        # if ("right_mouse_drag" in cls.events_by_type):
        #     right_mouse_drag_events = cls.events_by_type["right_mouse_drag"]
        #     for event in right_mouse_drag_events:
        #         Melty.cache.invalidate(event)
        #
        # if ("middle_mouse_drag" in cls.events_by_type):
        #     right_mouse_drag_events = cls.events_by_type["middle_mouse_drag"]
        #     for event in right_mouse_drag_events:
        #         Melty.cache.invalidate(event)

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
            if first_event.tile_id is not None and not cls.on_drag:
                Melty.cache.invalidate(first_event.tile_id)

        Melty.all_uniques = set()

        Melty.hovered_drawstate_pending = set()

        Melty.clip_stack = []
        # cls._root_by_module[module_id] = root
        # cls._gen_by_module.setdefault(module_id, 0)
        # cls._path_stack.clear()
        fb_w, fb_h = map(int, imgui.get_io().display_size)  # or your true GL FB size if HiDPI
        cls.cache.mask_begin_frame((fb_w, fb_h))


        from src.lsd.gl_gui.view.core_views.core_render_helpers import clear_floating_text_cache
        clear_floating_text_cache()

    @classmethod
    def is_wrapped(cls):
        if len(cls.wrap_stack) == 0:
            return False
        else:
            return cls.wrap_stack[-1]

    @classmethod
    def post_frame(cls):
        pass

    @classmethod
    def end_frame(cls):

        cls.returned_values = copy(cls.pending_return_values)
        cls.pending_returned_values = {}

        cls.apply_move_to_front()

        # cls.draw_blockers_to()
        # Manually mask windows
        # for window in Melty.registered_windows.values():
        #     draw_state = window.draw_state
        #     if not draw_state.closed:
        #         unique = f"{draw_state.id}"
        #         Melty.cache.mask_mark_view(draw_state.z_pos - 2, draw_state.left,
        #                                    draw_state.top, draw_state.width, draw_state.height,
        #                                    f"window_mask_{unique}", 4)

        cls.root_draw_states_by_layer = defaultdict(list)
        to_discard = set()
        for idx, ds in enumerate(cls.root_draw_states):
            if ds.closed:
                to_discard.add(ds)
            else:
                cls.root_draw_states_by_layer[ds.layer].append(ds)

        for ds in to_discard:
            cls.root_draw_states.discard(ds)

        for idx in range(len(cls.layers)):
            layer = cls.layers[idx]
            imgui.set_cursor_screen_pos((0, 0))
            Melty.active_layer = idx
            Melty.active_layer_stack = []

            if not Melty.channels_split:
                imgui.get_window_draw_list().channels_split(Melty.max_depth)
                imgui.get_window_draw_list().channels_set_current(Melty.max_depth - 1)
                Melty.channels_split = True

            for view in layer:

                if view is not None:
                    # Melty.shadow_depth = 0.0
                    draw_state = view[3]
                    # draw_state.depth_and_layer = (Melty.shadow_depth, Melty.active_layer)

                    # if draw_state._window_stack is not None:
                    #     Melty.melty_window_stack = draw_state._window_stack

                    parent_ctx = view[5]
                    current_z_pos = view[6]
                    cursor_pos = view[7]
                    Melty.depth = current_z_pos
                    Melty.bg_depth = draw_state.bg_depth - 1
                    # Melty.shadow_depth = draw_state.depth_and_layer[0]
                    # Melty.active_layer = draw_state.depth_and_layer[1]

                    cls.cache.insert_parent(parent_ctx)

                    view_func = view[0]
                    input_value = draw_state._input_value
                    kwargs = view[2]
                    kwargs['layer_unique'] = draw_state.unique
                    imgui.set_cursor_screen_pos(cursor_pos)

                    return_val = view_func(input_value, **kwargs)
                    if return_val is not None:
                        cls.pending_return_values[draw_state.id] = return_val


                    cls.cache.remove_parent()

            # Sort by y position (draw_state.top)

            for d_idx, draw_state in enumerate(cls.root_draw_states_by_layer[idx - 1]):
                # Melty.cache.mask_mark_view(draw_state.z_pos, draw_state.left,
                #                            draw_state.top, draw_state.width, draw_state.height,
                #                            f"view_mask_{draw_state.id}", 4)

                Melty.depth = draw_state.depth + d_idx
                Melty.cache.draw_tile(draw_state)
                last_bounding_hovered = draw_state._bounding_hovered
                new_bounding_hovered = draw_state.is_bounding_hovered()
                hover_changed = last_bounding_hovered != new_bounding_hovered
                draw_state._bounding_hovered = new_bounding_hovered
                if (draw_state.width is None or draw_state.height is None or hover_changed or
                        draw_state._bounding_hovered or draw_state._imgui_popover_open):
                    Melty.cache.invalidate(draw_state._tile_id)
                    # draw_state.invalidate_rect()

            Melty.depth = 0
            if Melty.channels_split:
                # Flatten layers into single channel
                imgui.get_window_draw_list().channels_set_current(0)
                imgui.get_window_draw_list().channels_merge()
                Melty.channels_split = False

        cls.layers = []

        is_popup_open = imgui.is_popup_open("", flags=imgui.POPUP_ANY_POPUP)
        Melty.imgui_popup_open = is_popup_open
        #
        from src.lsd.gl_gui.view.core_views.core_render import get_melty_state
        melty = get_melty_state()

        # melty.last_mouse_pos = imgui.get_mouse_pos()
        # # Did mouse move
        # if len(melty.hover_stack) > 0:
        #     last = melty.hover_stack[0]
        #     hovered_draw_state = Melty.vis.root.draw_state_registry.get(last, None)
        #     if hovered_draw_state is not None:
        #         hovered_draw_state._hovered = True
        #         Melty.hovered_drawstate_pending.add(hovered_draw_state.id)
        #
        # if len(melty.hotkey_stack) > 0:
        #     last = melty.hotkey_stack[0]
        #     hovered_draw_state = Melty.vis.root.draw_state_registry.get(last, None)
        #     if hovered_draw_state is not None:
        #         hovered_draw_state.hotkey_receiver = True

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
        # cls._root_by_module[module_id] = root
        # cls._gen_by_module.setdefault(module_id, 0)
        # cls._path_stack.clear()

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

            cls.pending_delete_window = None
            request_render()
            return

        if cls.pending_move_to_front is None or Melty.imgui_popup_open:
            return

        if not cls.imgui_active:
            window_key = cls.pending_move_to_front[0]
            window_z_pos = len(Melty.registered_windows) + Melty.top_layer_boost
            cls.pending_move_to_front[1].layer = window_z_pos
            draw_state = cls.pending_move_to_front[1]
            if cls.pending_move_to_front[1]._is_nested:
                draw_state.layer += Melty.nested_layer_boost
                # draw_state.z_pos = (draw_state.layer * Melty.max_depth) + draw_state.depth
            if window_key in Melty.registered_windows:
                # Remove and re-insert to move to end (top)
                window = Melty.registered_windows.pop(window_key)
                Melty.registered_windows[window_key] = window
            else:
                print(f"Warning: Tried to move window to front but {window_key} not found in registered_windows")
                print(f"Registered windows: {list(Melty.registered_windows.keys())}")

        if not cls.window_drag:
            print("big invalidate")
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
                fix_sized_ds.left,
                fix_sized_ds.top,
                fix_sized_ds.left + fix_sized_ds.width - margin,
                fix_sized_ds.top + fix_sized_ds.height
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
    def invalidate(cls, parent=None, value=None, attr_name=None):
        # if len(cls.dirty_objects) > 1000:
        #     cls.all_dirty = True
        #     cls.dirty_objects.clear()
        #     return
        cls.last_invalid_attr = f"{parent.__class__.__name__} {str(attr_name)}"
        cls.last_invalid.append(cls.last_invalid_attr)
        Melty.cache.invalidate_by_obj(cls.last_invalid)
        Melty.cache.invalidate_by_obj(value)

        if attr_name is not None:
            Melty.cache.invalidate_by_obj(parent, attr_name)

        else:
            if value is not None and (hasattr(value, "__dict__") or isinstance(value, (dict, list, set))):
                Melty.cache.invalidate_by_obj(value)

            elif parent is not None:
                Melty.cache.invalidate_by_obj(parent)

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
            cls.global_attrs[key] = value
        cls.annotation_mode = False

    @classmethod
    def init_ui(cls, **kwargs):
        pass
        # cls.backend.start()

    @classmethod
    def is_key_pressed(cls, key=glfw.KEY_ESCAPE):
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

    elif hasattr(collection, '__dict__'):
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

class ManagedWindow:
    def __init__(self, input_value=None, draw_state=None, window_args=None, name=None):
        self.input_value = input_value
        self.draw_state = draw_state
        self.window_args = window_args
        self.name = name

