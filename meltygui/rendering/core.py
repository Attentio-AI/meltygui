import inspect
import json
import math
import sys
import time
import zlib
from copy import copy
from enum import Enum
from functools import wraps
from typing import Any

import glfw
import imgui

from src.lsd.gl_gui.model.core_model.new_core_model import DrawState, Hotkey, DragMode
from src.lsd.gl_gui.model.core_model.stable_hash import stable_hash
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, print_stack_trace, \
    push_style_var, pop_style_var
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.melty import Melty, ActionType, apply_collection_action, MeltyState, DepthState, \
    delete_from_collection
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int

melty_state_registry = {}
def get_melty_state(unique: int):
    """Get or create a Melty state object for a widget ID."""
    if unique not in melty_state_registry or melty_state_registry[unique] is None:
        melty_state_registry[unique] = MeltyState()

    return melty_state_registry[unique]


# Cache to track used space - key is snapped y position, value is left x used
_floating_text_cache = {}
_floating_text_prev_frame_heights = {}  # Store max label height per line from previous frame


def handle_actions(melty, unique, draw_state, func=None):
    if not imgui.is_mouse_down():
        melty.initial_scroll_offset = (0, 0)
        melty.initial_drag_offset = None
        melty.mouse_down_pos = None

    if (Melty.is_window_enabled() and (imgui.is_window_hovered() or melty.drag_in_progress)) or Melty.blocker_hovered:
        for m_btn in [0, 1, 2]:
            btn_state = draw_state.mouse_btn_state[m_btn]

            if btn_state.drag_released:
                btn_state.drag_released = False
                melty.dragged_item = None
                melty.dragged_tile = None

            was_mouse_down = btn_state.mouse_down
            btn_state._clicked = False

            # Mouse down from glfw
            global_mouse_down = imgui.is_mouse_down(m_btn)
            mouse_pos = imgui.get_mouse_pos()

            if btn_state.drag_released:
                btn_state.drag_released = False

            if draw_state.id in Melty.hovered_drawstate:
                if global_mouse_down and btn_state._mouse_up:
                    if not btn_state.mouse_down or melty.mouse_down_pos is None:
                        melty.total_drag_distance = 0.0
                        melty.total_drag_frames = 0
                        current_mouse_pos = mouse_pos
                        btn_state.mouse_down_pos = mouse_pos
                        btn_state.initial_scroll_offset = Melty.scroll_stack[-1] if len(Melty.scroll_stack) > 0 else (0, 0)
                        melty.initial_scroll_offset = Melty.scroll_stack[-1] if len(Melty.scroll_stack) > 0 else (0, 0)

                        melty.mouse_down_pos = mouse_pos
                        btn_state.initial_screen_pos = (draw_state.left, draw_state.top)
                        btn_state.initial_window_pos = draw_state.window_pos
                        btn_state.initial_window_size = (draw_state.width, draw_state.height)
                        draw_state.drag_mode = get_drag_mode(draw_state)

                        melty.initial_drag_offset = (current_mouse_pos[0] - draw_state.left,
                                                     current_mouse_pos[1] - draw_state.top)
                        melty.mark_event(unique, m_btn, ActionType.DOWN)

                    btn_state.mouse_down = True
                    # Melty.cache.invalidate(tile_id)

                if not global_mouse_down:
                    btn_state._mouse_up = True
            else:
                btn_state._mouse_up = False
            if was_mouse_down and not global_mouse_down:
                btn_state._clicked = True
                melty.mark_event(unique, m_btn, ActionType.CLICK)
                # Melty.cache.invalidate(tile_id)

            if not global_mouse_down:
                btn_state.mouse_down = False
                if btn_state.dragged:
                    melty.total_drag_distance = 0.0
                    melty.total_drag_frames = 0
                    btn_state.drag_released = True
                    melty.initial_scroll_offset = (0,0)
                    btn_state.initial_scroll_offset = None
                    melty.initial_drag_offset = None
                    btn_state.initial_screen_pos = None
                    melty.mouse_down_pos = None
                    melty.mark_event(unique, m_btn, ActionType.DRAG_UP)
                    # Melty.cache.invalidate(tile_id)

                btn_state.dragged = False

            if btn_state.mouse_down:
                current_mouse_pos = mouse_pos
                distance = math.sqrt((current_mouse_pos[0] - btn_state.mouse_down_pos[0]) ** 2 +
                                     (current_mouse_pos[1] - btn_state.mouse_down_pos[1]) ** 2)
                btn_state.drag_delta = (current_mouse_pos[0] - btn_state.mouse_down_pos[0],
                                        current_mouse_pos[1] - btn_state.mouse_down_pos[1])

                if melty.last_mouse_pos is not None:
                    this_m = mouse_pos
                    last_m = melty.last_mouse_pos
                    frame_drag_distance = math.sqrt(
                        (this_m[0] - last_m[0]) ** 2 + (this_m[1] - last_m[1]) ** 2)
                    melty.total_drag_distance += frame_drag_distance
                    melty.total_drag_frames += 1
                if melty.total_drag_distance >= 2 or btn_state.dragged:
                    btn_state.dragged = True
                    melty.drag_in_progress = True

                    if draw_state.window_pos is None:
                        melty.dragged_item = draw_state
                        melty.dragged_tile = Melty.get_tile_id()
                    melty.mark_event(unique, m_btn, ActionType.DRAG)
                    melty.drag_delta = btn_state.drag_delta

        if draw_state.id in Melty.hovered_drawstate:
            if unique not in melty.triggered_actions:
                melty.mark_event(unique, 0, ActionType.HOVERED)

        # Handle scroll
        was_scrolled = False
        if draw_state.height is not None:
            if draw_state.content_height > draw_state.height:
                scroll_y = imgui.get_io().mouse_wheel
                if scroll_y != 0.0:
                    melty.mark_event(unique, scroll_y,
                                     ActionType.SCROLL, value=scroll_y)


        # global_mouse_down = glfw.get_mouse_button(Melty.glfw_window, 0) == glfw.PRESS
        # if not global_mouse_down:
        #     melty.drag_in_progress = False



def floating_text(text: str, x_offset: float = 0, line_height: float = None, tint: tuple = (1, 1, 1, 1),
                  max_width: float = 200):
    cursor_pos = imgui.get_cursor_pos()

    inside_window = len(Melty.window_stack) > 0

    # Get draw list - only use overlay
    draw_list = imgui.get_overlay_draw_list()
    window_pos = imgui.get_window_position()

    # Get current window ID to track space per-window
    window_id = Melty.window_stack[-1] if len(Melty.window_stack) > 0 else 0

    # Account for scroll offset
    scroll_x = imgui.get_scroll_x()
    scroll_y = imgui.get_scroll_y()

    # Check if the cursor position (in content space) is within the visible scrolled region
    content_min = imgui.get_window_content_region_min()
    content_max = imgui.get_window_content_region_max()
    content_height = content_max.y - content_min.y

    # Don't render if outside the current visible region
    if cursor_pos[1] < scroll_y or cursor_pos[1] > scroll_y + content_height:
        return

    # Starting point (cursor position in absolute coordinates, adjusted for scrolling)
    start_x = window_pos.x + cursor_pos[0] - scroll_x
    start_y = window_pos.y + cursor_pos[1] - scroll_y

    padding = 4
    spacing = 8

    # Check if text needs wrapping
    single_line_size = imgui.calc_text_size(text)
    text_line_height = imgui.get_text_line_height()

    # Split text into lines if needed
    text_lines = []
    if single_line_size.x > max_width:
        # Text needs wrapping - manually split by words
        words = text.split(' ')
        current_line = ""

        for word in words:
            test_line = current_line + (" " if current_line else "") + word
            test_size = imgui.calc_text_size(test_line)
            if test_size.x <= max_width:
                current_line = test_line
            else:
                if current_line:
                    text_lines.append(current_line)
                    current_line = word
                else:
                    # First word is too long, just add it anyway
                    text_lines.append(word)
                    current_line = ""
        if current_line:
            text_lines.append(current_line)
    else:
        text_lines = [text]

    # Use imgui's calc_text_size with wrap_width to get proper wrapped dimensions
    wrapped_size = imgui.calc_text_size(text, wrap_width=max_width)
    text_width = wrapped_size.x
    total_text_height = wrapped_size.y

    # Use text height for line snapping if not provided
    if line_height is None:
        line_height = text_line_height + padding * 2

    # Calculate label height
    label_height = total_text_height + padding * 2

    # Snap to line index based on cursor position
    relative_y = cursor_pos[1] - scroll_y
    line_index = round(relative_y / line_height)

    # Calculate actual_y by summing previous lines' heights from previous frame
    actual_y = 0
    for i in range(line_index):
        prev_key = (window_id, i)
        if prev_key in _floating_text_prev_frame_heights:
            actual_y += _floating_text_prev_frame_heights[prev_key]
        else:
            actual_y += line_height  # default height if not set

    snapped_y = window_pos.y + actual_y

    # Track max label height for this line in current frame
    cache_key = (window_id, line_index)
    if cache_key in _floating_text_cache:
        cache_data = _floating_text_cache[cache_key]
        cache_data['max_height'] = max(cache_data.get('max_height', 0), label_height)
        left_x = cache_data.get('left_x', float('inf'))
    else:
        _floating_text_cache[cache_key] = {
            'max_height': label_height,
            'left_x': float('inf')
        }
        left_x = float('inf')

    # Calculate x position relative to window (right edge of label)
    current_x_right = window_pos.x + x_offset

    # Check for horizontal overlap at this y position
    if current_x_right >= left_x:
        current_x_right = left_x - spacing

    # Calculate left edge of this label
    label_left = current_x_right - text_width - padding * 2

    # Update cache for this line's x position
    _floating_text_cache[cache_key]['left_x'] = label_left

    # Text position (left edge)
    text_x = label_left + padding
    text_y = snapped_y

    # End point for the line (right edge of label box, same height)
    end_x = current_x_right - padding
    end_y = text_y

    # Check if mouse is hovering over the text box
    mouse_pos = imgui.get_mouse_pos()
    is_hovered = (mouse_pos.x >= text_x - padding and
                  mouse_pos.x <= text_x + text_width + padding and
                  mouse_pos.y >= text_y - padding and
                  mouse_pos.y <= text_y + label_height - padding)

    # Calculate control points for S-curve
    horizontal_distance = end_x - start_x
    curve_offset = abs(horizontal_distance) * 0.5

    cp1_x = start_x + curve_offset if horizontal_distance > 0 else start_x - curve_offset
    cp1_y = start_y
    cp2_x = end_x - curve_offset if horizontal_distance > 0 else end_x + curve_offset
    cp2_y = end_y

    # Use channels: 0 for lines (back), 1 for boxes (front)
    if is_hovered:
        line_channel = 0
        line_color = imgui.get_color_u32_rgba(1, 1, 1, 1)
        dot_color = imgui.get_color_u32_rgba(1, 1, 1, 1)
        line_thickness = 2.5
    else:
        line_channel = 0
        line_color = imgui.get_color_u32_rgba(tint[0], tint[1], tint[2], tint[3] * 0.5)
        dot_color = imgui.get_color_u32_rgba(tint[0], tint[1], tint[2], tint[3])
        line_thickness = 1.0

    if inside_window:
        draw_list.channels_set_current(line_channel)
    draw_list.add_bezier_cubic(
        start_x, start_y,
        cp1_x, cp1_y,
        cp2_x, cp2_y,
        end_x, end_y,
        line_color,
        line_thickness,
        0
    )

    # Draw the dot
    draw_list.add_circle_filled(
        start_x, start_y,
        3.0,
        dot_color,
        12
    )

    # Draw boxes on front channel
    if inside_window:
        draw_list.channels_set_current(1)

    # Draw background rectangle
    draw_list.add_rect_filled(
        text_x - padding,
        text_y - padding,
        text_x + text_width + padding,
        text_y + label_height - padding,
        imgui.get_color_u32_rgba(0.1, 0.1, 0.1, 1.0)
    )

    # Draw outline in tint color (or yellow if hovered)
    if is_hovered:
        outline_color = imgui.get_color_u32_rgba(1, 1, 1, 1)
        outline_thickness = 2.0
    else:
        outline_color = imgui.get_color_u32_rgba(tint[0], tint[1], tint[2], tint[3])
        outline_thickness = 1.0

    draw_list.add_rect(
        text_x - padding,
        text_y - padding,
        text_x + text_width + padding,
        text_y + label_height - padding,
        outline_color,
        0.0,
        0,
        outline_thickness
    )

    # Draw text lines
    text_color = imgui.get_color_u32_rgba(tint[0], tint[1], tint[2], tint[3])
    line_y = text_y
    for line in text_lines:
        draw_list.add_text(
            text_x,
            line_y,
            text_color,
            line
        )
        line_y += text_line_height


def clear_floating_text_cache():
    """Call this at the start of each frame to reset label positioning"""
    global _floating_text_cache, _floating_text_prev_frame_heights

    # Copy current frame's max heights to previous frame for next frame's use
    _floating_text_prev_frame_heights = {
        key: data['max_height']
        for key, data in _floating_text_cache.items()
        if 'max_height' in data
    }

    # Clear the cache for new labe
    _floating_text_cache.clear()

def get_draw_state(unique: int) -> DrawState:
    """Get or create a ViewState object for a widget ID."""
    if unique not in Melty.vis.root.draw_state_registry or Melty.vis.root.draw_state_registry[unique] is None:
        Melty.vis.root.draw_state_registry[unique] = DrawState()
        Melty.vis.root.draw_state_registry[unique].unique = unique

    Melty.vis.root.draw_state_registry[unique].delete_countdown = Melty.save_draw_state_for
    return Melty.vis.root.draw_state_registry[unique]

def strhash(s: str) -> int:
    """Stable 32-bit hash of a string."""
    return zlib.crc32(s.encode("utf-8")) & 0xffffffff

def combine(h: int, s: str) -> int:
    """Order-sensitive, stable combine (FNV-style)."""
    return ((h * 16777619) ^ strhash(s)) & 0xffffffff

def ui_id(datatype=None, suffix=None, idx=0) -> int:
    """
    Generate a stable UI ID from the call stack + optional metadata.

    - meta: optional Meta object to fold in attribute name/type
    - max_depth: limit to avoid walking the whole interpreter stack
    """
    h = 0
    frame = sys._getframe(2)  # skip ui_id itself
    code = frame.f_code
    func_name = code.co_name
    cls_name = ""
    if "self" in frame.f_locals:
        cls_name = frame.f_locals["self"].__class__.__name__
    scope = f"{cls_name}.{func_name}" if cls_name else func_name
    h = combine(h, scope)
    datatype = datatype if datatype is not None else Any
    h = combine(h, str(datatype))
    suffix_int = strhash(str(suffix))

    unique = h if suffix is None else (((h * 16777619) ^ suffix_int) + (idx + 1))
    unique = strhash(str(unique))

    return unique


class WrapType(Enum):
    CHILD = 1
    ID = 2
    GROUP=3
    WINDOW=4

channels_split_stack = False
child_stack_holder = {}

def begin_window(unique_id, *args, **kwargs):
    return_val = imgui.begin(unique_id, *args, **kwargs)
    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)
    Melty.channels_split = True
    return return_val

def begin_group(unique_id=0, *args, **kwargs):
    return imgui.begin_group()

def end_group():
    imgui.end_group()

def end_window(unique_id):
    imgui.get_window_draw_list().channels_merge()
    Melty.channels_split = False
    imgui.end()


    # draw_list = imgui.get_window_draw_list()
    # draw_list.channels_split(Melty.max_depth)
    # Melty.channels_split = True


id_stack = []
stack_holder = {}
def push_id(unique_id):
    global id_stack
    if Melty.imgui_crashed:
        return
    imgui.push_id(str(unique_id))
    id_stack.append(unique_id)

def pop_id():
    global id_stack
    if Melty.imgui_crashed:
        return

    imgui.pop_id()
    id_stack.pop()

    #
    # global id_stack
    # if Melty.imgui_crashed:
    #     return
    #
    # imgui.pop_id()
    # id_stack.pop()

def tmp_undo_stack(undo_point_id):
    """Context manager to temporarily clear the ID stack."""
    global id_stack
    global stack_holder
    stack_holder[undo_point_id] = id_stack[:]
    for _ in stack_holder[undo_point_id]:
        imgui.pop_id()
    id_stack = []

def redo_stack(undo_point_id):
    """Restore the ID stack to a previously saved state."""
    global id_stack
    global stack_holder
    if undo_point_id in stack_holder:
        saved_stack = stack_holder.pop(undo_point_id)
        for uid in saved_stack:
            imgui.push_id(str(uid))
        id_stack = saved_stack


# Hotkey decorator
def listens_for(hotkey):
    def decorator(func):
        Melty.hotkey_registry[func] = Melty.hotkey_registry.get(func, {})
        if isinstance(hotkey, (list, tuple)):
            for hk in hotkey:
                Melty.hotkey_registry[func][hk.name] = hk
        elif isinstance(hotkey, Hotkey):
            Melty.hotkey_registry[func][hotkey.name] = hotkey
        return func
    return decorator


def render_wrapper(*o_args, **o_kwargs):
    first_arg = o_args[0] if o_args else None
    if not callable(first_arg):
        def class_wrapper(the_func):
            return render_wrapper(the_func, *o_args, **o_kwargs)
        return class_wrapper

    r_func = o_args[0] if o_args else None
    header_defaults = {}

    @wraps(r_func)
    def wrapper(*args, **kwargs):
        first_arg = args[0] if args else None
        if not callable(first_arg):
            def class_wrapper(the_func):
                return wrapper(the_func, *args, **kwargs)
            return class_wrapper

        if 'inner_func' in kwargs:
            inner_func = kwargs.get('inner_func', None)
        else:
            inner_func = r_func


        func = first_arg if callable(first_arg) else None
        # use the specified wrapper if r_func
        wrap_sig = inspect.signature(inner_func)
        sig = inspect.signature(func)
        params = sig.parameters
        wrap_params = wrap_sig.parameters

        param_types = [params[p].annotation for p in params]
        wrap_param_types = [wrap_params[p].annotation for p in wrap_params]
        name_to_param_type = {}
        for idx, param_name in enumerate(params):
            name_to_param_type[param_name] = param_types[idx]

        wrap_name_to_param_type = {}
        for idx, param_name in enumerate(wrap_params):
            wrap_name_to_param_type[param_name] = wrap_param_types[idx]

        param_defaults = {p: params[p].default for p in params if params[p].default is not inspect.Parameter.empty}
        wrap_defaults = {p: wrap_params[p].default for p in wrap_params if wrap_params[p].default is not inspect.Parameter.empty}

        params = params | wrap_params
        param_types = param_types + wrap_param_types
        name_to_param_type = name_to_param_type | wrap_name_to_param_type
        param_defaults = param_defaults | wrap_defaults

        wanted_params = list(params.keys())
        wanted_params.remove("args") if "args" in wanted_params else None
        wanted_params.remove("o_kwargs") if "o_kwargs" in wanted_params else None


        def add_default(value):
            kwargs.pop('is_default_for', None)
            from src.lsd.gl_gui.view.core_views.core_presets import Meta
            new_meta = Meta()
            new_meta.view_function = wrapper(*args, **kwargs)
            # for k, v in param_defaults.items():
            #     setattr(new_meta, k, v)
            # for k, v in kwargs.items():
            #     setattr(new_meta, k, v)
            Melty.type_defaults[value] = new_meta

        is_default_for = kwargs.get('is_default_for', None)
        if isinstance(is_default_for, (tuple, list)):
            for a_type in is_default_for:
                add_default(a_type)
        elif isinstance(is_default_for, type):
            add_default(is_default_for)
        try:
            wrap_func = None
            if 'wraps' in o_kwargs:
                wrap_func = o_kwargs.get('wraps', None)
                func = wrap_func(func, header_defaults=kwargs, param_defaults=param_defaults, **kwargs)
            if 'show_name' in o_kwargs:
                pass

            out_func = r_func(func, param_types=param_types, wanted_params=wanted_params,
                              wanted_params_inner=wrap_defaults, header_defaults=kwargs,
                          param_defaults=param_defaults, name_to_param_type=name_to_param_type,
                          **o_kwargs)

            if wrap_func is not None:
                o_kwargs.update(kwargs)

                out_func = wrap_func(out_func, inner_func=r_func, **kwargs)

            return out_func
        except Exception as e:
            print_colored_traceback(*sys.exc_info())
            return False, None
    #
    # do_wrap = o_kwargs.pop('wraps', None)
    # if callable(do_wrap):
    # #     wrapper = do_wrap(wrapper, **o_kwargs)
    # wrap_func = None
    # if 'wraps' in o_kwargs:
    #     wrap_func = o_kwargs.pop('wraps', None)
    #     wrapper = wrap_func(inner_func, **o_kwargs)

    return wrapper

def annotation_track(*args, wrapper, **kwargs):
    first_arg = args[0] if args else None
    from src.lsd.gl_gui.view.core_views.core_presets import Meta
    if Melty.annotation_mode:
        # Class decoration mode, with args
        if 'for_type' in kwargs and not isinstance(first_arg, type):
            def class_wrapper(cls):
                inner_args = args[1:]
                return wrapper(cls, *inner_args, **kwargs)

            return class_wrapper

        # Class decoration mode, ie. @render_as_float
        if isinstance(first_arg, type):
            for_type = kwargs.get('for_type', None)
            kwargs.pop('for_type', None)
            kwargs.pop('default_value', None)
            args = args[1:] if len(args) > 1 else ()

            new_meta = Meta()
            for k, v in kwargs.items():
                setattr(new_meta, k, v)
            new_meta.view_function = wrapper
            if for_type is not None:
                first_arg.default_meta_for = getattr(first_arg, 'default_meta_for', {})
                first_arg.default_meta_for[for_type] = new_meta
            else:
                first_arg.meta = new_meta

            return first_arg

        # # View function was used as annotation, ie. some_param: as_float = 0.0
        new_meta = Meta()

        for k, v in kwargs.items():
            setattr(new_meta, k, v)
        new_meta.view_function = wrapper
        return new_meta

    return None

def apply_drag_and_drop():

    ######## -------- apply drag & drop -----------
    did_apply = False
    while len(Melty.actions_to_apply) > 0:
        action = Melty.actions_to_apply.pop(0)
        result = apply_collection_action(action)

        if action.source_collection == action.target_collection:
            Melty.cache.invalidate_up_by_obj(action.source_collection)
        else:
            Melty.cache.invalidate_up_by_obj(action.source_collection)
            Melty.cache.invalidate_up_by_obj(action.target_collection)
        did_apply = True

    Melty.actions_to_apply = []
    if did_apply:
        request_render()


def get_resize_handle(a_ds):
    if a_ds.left is None:
        return (0, 0, 0, 0)
    left = a_ds.left
    top = a_ds.top
    right = left + a_ds.width
    bottom = top + a_ds.height - 1

    margin = 34
    return (right - margin, bottom - margin, right, bottom)

def draw_resize_handle(a_ds):
    if a_ds.width is None:
        return
    if a_ds.height is None:
        return
    if a_ds.bounds_left is None:
        return

    rect_br = get_resize_handle(a_ds)
    draw_list = imgui.get_window_draw_list()
    width = rect_br[2] - rect_br[0]
    height = rect_br[3] - rect_br[1]

    if width <= 0 or height <= 0:
        return

    current_cursor = imgui.get_cursor_screen_pos()
    if a_ds.expanded:
        imgui.set_cursor_screen_pos((rect_br[0], rect_br[1]))
        imgui.invisible_button(str(a_ds.unique) + "resize_btn", width, height)
        imgui.same_line(0)

    alpha = 0.0
    if imgui.is_mouse_hovering_rect(rect_br[0], rect_br[1], rect_br[2], rect_br[3]):
        Melty.blocker_hovered = True
        alpha = 0.5

    # draw_list.add_rect_filled(rect_br[0], rect_br[1], rect_br[2], rect_br[3],
    #                           imgui.get_color_u32_rgba(0.8, 0.8, 0.2, 0.3))

    # Resizeable corner drag
    arrow_size = 13
    margin = 1
    draw_list.add_triangle_filled(
        rect_br[2] - margin - 1, rect_br[3] - arrow_size - margin,
        rect_br[2] - margin - 1, rect_br[3] - margin,
        rect_br[2] - margin - 1 - arrow_size, rect_br[3] - margin,
        imgui.get_color_u32_rgba(1,1,1, alpha)
    )
    # Bottom corner
    if alpha > 0.0:
        Melty.cache.mask_mark_rect(Melty.max_depth - 1,
                                   rect_br[2] - arrow_size - margin - 1,
                                   rect_br[3] - margin - arrow_size, arrow_size, arrow_size,
                                   key=str(a_ds.unique) + "resize")
    if a_ds.expanded:

        imgui.set_cursor_screen_pos(current_cursor)


def get_drag_mode(a_ds):
    mx, my = imgui.get_mouse_pos()
    rect_br = get_resize_handle(a_ds)
    inside_br = (rect_br[0] <= mx <= rect_br[2] and rect_br[1] <= my <= rect_br[3])

    # Bottom corner
    if inside_br:
        return DragMode.RESIZE_BR

    return DragMode.WINDOW


def jet_color(val: float):
    # jet color function
    four_value = 4.0 * val
    r = min(four_value - 1.5, -four_value + 4.5)
    g = min(four_value - 0.5, -four_value + 3.5)
    b = min(four_value + 0.5, -four_value + 2.5)
    return max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, b)), 1.0

@render_wrapper
def render_func(*args, **o_kwargs):
    func = args[0] if args else None
    param_types = o_kwargs.get("param_types", None)
    wanted_params = o_kwargs.get("wanted_params", None)
    header_defaults = o_kwargs.get("header_defaults", None)
    param_defaults = o_kwargs.get("param_defaults", None)
    name_to_param_type = o_kwargs.get("name_to_param_type", None)

    """
    Decorator for render functions.
    - Computes stable UI ID (unique) from callstack+meta.
    - Provides a per-widget viewstate object (with .unique).
    - Injects meta/viewstate only if the function signature wants them.
    - Pushes/pops ImGui ID scope automatically.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        if kwargs.get("bypass", False):
            kwargs.pop("bypass", None)
            return func(*args, **kwargs)

        o_kwargs.update(kwargs)
        annotation = annotation_track(*args, wrapper=wrapper, **o_kwargs)
        if annotation is not None:
            return annotation

        return_value = None
        is_root = Melty.depth == 0

        read_only = kwargs.get("read_only", False)

        first_arg = args[0] if args else None
        input_value = kwargs.get("input_value", first_arg)
        name = kwargs.get("name", "")

        if header_defaults is not None:
            kwargs.update(header_defaults)

        if name == "" and is_root:
            Melty.wrapped_depth = 0
            kwargs["name"] = str(len(melty_state_registry)) + "root"
            name = kwargs["name"]

        name_func = kwargs.get("name_func", None)
        if name_func is not None:
            try:
                name = name_func(input_value)
                if not isinstance(name, str):
                    name = str(name)
            except Exception as e:
                name = str(f"{e}")

        key = kwargs.get("key", None)
        key = key if key is not None else ""
        if name == "" and not is_root:
            name = str(key) + input_value.__class__.__name__

        from src.lsd.gl_gui.view.core_views.core_presets import Meta

        if Melty.depth > Melty.max_depth:
            return False, None

        # ----- Unique computation before pushing ID scope (avoid divergence) -----
        old_suffix = kwargs.get("suffix", None)
        unique_name = kwargs.get("unique_name", name)

        index = key if isinstance(key, int) else 0
        suffix = Melty.unique_stack[-1] if len(Melty.unique_stack) > 0 else (name or "")

        # Keep original behavior of always appending name (even if empty)
        suffix = f"{old_suffix}_{suffix}_{name}"


        if is_root:
            unique = ui_id(datatype=type(input_value), suffix=unique_name + func.__name__)
            suffix = f"{unique_name}_{func.__name__}_{unique}"
        else:
            unique = ui_id(datatype=type(input_value), suffix=suffix + unique_name + str(key) + func.__name__, idx=index)

        computed_unique = unique

        # if 'unique' in kwargs:
        #     unique = kwargs.get("unique", None)
        # -------------------------------------------------------------------------
        start_cursor = imgui.get_cursor_pos()

        start_cursor = imgui.get_cursor_screen_pos()
        end_cursor = imgui.get_cursor_screen_pos()

        if is_root:
            # NOTE: We already computed 'unique' once for root above; do not recompute.
            Melty.unique_stack = []
            Melty.draw_state_stack = []
            Melty.flow_spacing = 0.0
            melty = get_melty_state(unique)
            melty.nearest_drop_distance = melty.max_distance
            melty.nearest_drop_target = None
            melty.nearest_drop_target_tag = None
            Melty.bg_stack = [(0, 0, 0)]
            Melty.indent_count = 0
            Melty.unindent_count = 0


            is_popup_open = imgui.is_popup_open("", flags=imgui.POPUP_ANY_POPUP)
            # if is_popup_open != Melty.imgui_popup_open and not is_popup_open:
            #     Melty.cache.invalidate_all()
            Melty.imgui_popup_open = is_popup_open
        else:
            melty = get_melty_state(Melty.unique_stack[0])

        # After you compute `new_unique` for `x` in the render loop:
        root = Melty.vis.root
        registry = root.draw_state_registry
        pending = Melty.move_draw_state_pending

        if pending:  # any remaps pending?
            ds = pending.pop(id(input_value), None)  # was this object moved?
            if ds is not None:
                # If the draw state has its own unique, retire the old entry
                old_u = getattr(ds, "unique", None)
                if old_u is not None:
                    registry.pop(old_u, None)
                    ds.unique = unique  # keep the DS in sync

                # Install under the new unique (overwrite if needed)
                registry[unique] = ds

                # Optional: clean up empty dict to avoid pointless checks later
                if not pending:
                    # FIX: ensure we reset the same container we read from
                    Melty.move_draw_state_pending = {}

        draw_state = get_draw_state(unique)

        if isinstance(input_value, (type(None), int, float, str, bool, tuple, set)):
            if draw_state._input_value != input_value:
                Melty.cache.invalidate_up_by_obj(kwargs.get("collection", input_value))
                request_render()

        draw_state._input_value = input_value

        # hashable_representation = tuple(sorted(input_value.items()))
        # sorted_dict_string = json.dumps(input_value, sort_keys=True).encode('utf-8')
        # try:
        #     draw_state.value_hash = stable_hash(input_value)
        # except Exception as e:
        #     print("[Unique] Warning: Could not compute stable hash for object of type", type(input_value).__name__,
        #             "with unique", unique, name, "due to:", e)

        draw_state._has_popup = kwargs.get("has_popup", False)
        draw_state.auto_resize = kwargs.get("auto_resize", False)

        tile_id = strhash(str(computed_unique) + str(draw_state.id))
        draw_state._tile_id = tile_id

        # if unique in Melty.all_uniques:
        #     print("[Unique] Warning: Duplicate key detected:", computed_unique, type(input_value).__name__, "in",
        #           type(kwargs.get('collection', object)).__name__, "with name", name,"with func:", func.__name__)
        Melty.all_uniques.add(unique)

        is_initial_draw_state = True
        # nested_call = input_value == Melty.input_value_stack[-1] if len(Melty.input_value_stack) > 0 else False
        Melty.input_value_stack.append(input_value)
        inc_depth = False
        Melty.wrapped_depth = Melty.wrapped_depth + 1
        depth_to_restore = None
        wrapped_depth_to_restore = None
        melty_window = False

        try:

            meta = kwargs.get("meta", None)
            if meta is None:
                # Use type meta as default if available
                if hasattr(type(input_value), "meta"):
                    meta = getattr(type(input_value), "meta")
                else:
                    if hasattr(Meta, 'get_child_meta'):
                        meta = Meta.get_child_meta(None, field_name=kwargs.get("name", ''), value=input_value)
                    else:
                        meta = Meta.get_new_defaults(default_value=input_value)

            if name is not None and name != "":
                meta.name = name

            meta.unique = unique

            def set_default(key, default_value):
                if key in vars(meta) and vars(meta)[key] is not None:
                    default_value = vars(meta)[key]
                if default_value is None:
                    default_value = (param_defaults or {}).get(key, default_value)
                kwargs.setdefault(key, default_value)

            kwargs.update(Melty.global_attrs)

            set_default("input_value", input_value)
            set_default("draw_state", draw_state)
            set_default("name", name)
            set_default("melty", melty)
            set_default("unique", unique)
            set_default("suffix", suffix)
            set_default("window_stack", Melty.window_stack)
            set_default("on_click", melty.check_event(unique, 0, ActionType.CLICK))
            set_default("on_drag", melty.check_event(unique, 0, ActionType.DRAG))
            set_default("on_drag_up", melty.check_event(unique, 0, ActionType.DRAG_UP))
            set_default("on_mouse_down", melty.check_event(unique, 0, ActionType.DOWN))

            set_default("on_hover", melty.check_event(unique, 0, ActionType.HOVERED))

            if melty.top_event.get(ActionType.SCROLL, None) == unique:
                set_default("on_scroll", melty.check_event_value(unique, -1, ActionType.SCROLL))
                melty.top_event.pop(ActionType.SCROLL, None)
                melty.top_event_depth.pop(ActionType.SCROLL, None)

            set_default("on_action", melty.triggered_actions.get(unique, None))
            set_default("func", func)
            set_default("render_func", wrapper)

            if func in Melty.hotkey_registry:
                hotkey_actions = Melty.hotkey_registry.get(func, {})
                for hk_name, hk in hotkey_actions.items():
                    if draw_state.hotkey_receiver or (not hk.scoped and Melty.window_hovered):
                        if hk.mod_active() and Melty.is_key_pressed(hk.key):
                            kwargs.setdefault(hk_name, True)
                        else:
                            kwargs.setdefault(hk_name, False)
            kwargs.setdefault('meta', meta)

            kwargs.update(meta.__dict__)
            for param in wanted_params:
                if param not in kwargs and param != "kwargs" and param != 'args' and param != 'o_kwargs' and param != 'next_kwargs':
                    set_default(param, None)

            is_initial_draw_state = draw_state in Melty.draw_state_stack
            if is_initial_draw_state:
                draw_state._did_use_cache = False

            inc_depth = "draw_state" in wanted_params or is_root
            inc_depth = True

            Melty.unique_stack.append(computed_unique)

            if len(Melty.draw_state_stack) <= Melty.depth:
                Melty.draw_state_stack.append(draw_state)
            else:
                # Melty.unique_stack[Melty.depth] = computed_unique
                Melty.draw_state_stack[Melty.depth] = draw_state

            Melty.depth = Melty.depth + 1

            requested_z = kwargs.get("z_pos", None)
            if requested_z is not None:
                depth_to_restore = Melty.depth
                wrapped_depth_to_restore = Melty.wrapped_depth
                Melty.depth = requested_z
                relative_change = abs(requested_z - Melty.wrapped_depth)
                Melty.wrapped_depth = Melty.wrapped_depth + relative_change

            kwargs['depth'] = Melty.depth
            draw_state._draggable = kwargs.get("draggable", False)

            if draw_state.get_drag_mode() == DragMode.RESIZE_BR:
                melty.hover_stack.append(unique)

            ############################# WINDOW SETUP #####################################################
            window_drag = False
            # if kwargs.get("melty_window", False):
            #     imgui.set_cursor_screen_pos((0, 0))

            if kwargs.get("melty_window", False) and kwargs.get("on_drag", False):
                window_drag = True
                mouse_pos = imgui.get_mouse_pos()

                mouse_down_x = draw_state.mouse_btn_state[0].mouse_down_pos[0]
                mouse_down_y = draw_state.mouse_btn_state[0].mouse_down_pos[1]
                drag_delta = (mouse_pos[0] - mouse_down_x, mouse_pos[1] - mouse_down_y)

                if draw_state.drag_mode == DragMode.WINDOW:
                    start_pos_x = draw_state.mouse_btn_state[0].initial_window_pos[0]
                    start_pos_y = draw_state.mouse_btn_state[0].initial_window_pos[1]
                    pos_x = start_pos_x + drag_delta[0]
                    pos_y = start_pos_y + drag_delta[1]
                    draw_state.window_pos = (pos_x, pos_y)
                    request_render()
                elif draw_state.drag_mode == DragMode.RESIZE_BR:
                    start_pos_x = draw_state.mouse_btn_state[0].initial_window_size[0]
                    start_pos_y = draw_state.mouse_btn_state[0].initial_window_size[1]
                    size_w = start_pos_x + drag_delta[0]
                    size_h = start_pos_y + drag_delta[1]
                    draw_state.width, draw_state.height = (max(size_w, 25), max(size_h, 24))
                    draw_state.window_size = (draw_state.width, draw_state.height)
                    draw_state.expanded = True


            if kwargs.get("window_pos", None) is not None:
                draw_state.window_pos = kwargs.get("window_pos", None)

            if kwargs.get("melty_window", False):
                melty_window = True
                Melty.melty_window_stack.append(unique)
                cursor_pos = imgui.get_cursor_screen_pos()

                if draw_state.window_pos is None and draw_state.width is not None:
                    draw_state.window_pos = Melty.init_window_cursor
                    cursor_spacing = 5
                    Melty.init_window_cursor = (Melty.init_window_cursor[0] +
                                                draw_state.width + cursor_spacing,
                                                Melty.init_window_cursor[1])

                if draw_state.window_pos is not None:
                    imgui.set_cursor_screen_pos((snap_int(cursor_pos[0] + draw_state.window_pos[0]),
                                                 snap_int(cursor_pos[1] + draw_state.window_pos[1])))

            kwargs['melty_window'] = False

            if len(Melty.melty_window_stack) > 0:
                Melty.is_melty_window = True
            else:
                Melty.is_melty_window = False

            ######################## ERROR HANDLING FOR TYPES ########################

            cursor_pos = imgui.get_cursor_pos()
            imgui.set_cursor_pos((snap_int(cursor_pos[0]), snap_int(cursor_pos[1])))
            spacing = kwargs.get('spacing', Melty.spacing)
            padding = kwargs.get('padding', Melty.padding)
            push_style_var(imgui.STYLE_ITEM_SPACING, spacing)
            push_style_var(imgui.STYLE_FRAME_PADDING, padding)

            if not draw_state.expanded:
                kwargs["auto_resize"] = True

            begin_group(unique)
            push_id(unique)

            if not draw_state.expanded:
                kwargs["auto_resize"] = True

            expected_type = param_types[wanted_params.index("input_value")] if "input_value" in wanted_params else None
            annotation_empty = expected_type == inspect.Parameter.empty
            if not annotation_empty:
                if expected_type is not Any and isinstance(expected_type, type):
                    if not isinstance(input_value, expected_type):
                        yellow = (1.0, 1.0, 0.0, 1.0)
                        if imgui.button(f"Fix Type##{unique}"):
                            return True, expected_type()
                        same_line()
                        imgui.text_colored(f"Type mismatch in {func.__name__}\n"
                                           f"Expected {expected_type.__name__}, "
                                           f"got {type(input_value).__name__}", *yellow)
                        return False, None

            if not meta.visible_in_ui:
                return False, None
            ###########################################################
            kwargs['next_kwargs'] = kwargs
            if 'kwargs' in wanted_params:
                clean_args = kwargs
            else:
                clean_args = {k: kwargs[k] for k in wanted_params if k in kwargs}
            ###########################################################

            if melty_window:
                draw_list = imgui.get_window_draw_list()
                if Melty.channels_split:
                    draw_list.channels_set_current((Melty.max_depth - 2))
                draw_resize_handle(draw_state)
            try:
                on_drag = kwargs.get("on_drag", False)
                kwargs['on_drag'] = False
                if on_drag and not melty_window and not window_drag:

                    from src.lsd.gl_gui.view.core_views.new_core_view import draw_drag_drop_target
                    _, flow_spacing = draw_drag_drop_target(do_flow=True, enable_flow=True,
                                                            collection=kwargs.get("collection", None), key=key, on_drag=False,
                                                            draw_state=draw_state, tag="top")

                    next_kwargs = kwargs['next_kwargs']
                    mouse_pos = imgui.get_mouse_pos()
                    mouse_down_x = draw_state.mouse_btn_state[0].mouse_down_pos[0]
                    mouse_down_y = draw_state.mouse_btn_state[0].mouse_down_pos[1]
                    drag_delta = (mouse_pos[0] - mouse_down_x, mouse_pos[1] - mouse_down_y)

                    next_kwargs['opacity'] = 1.0
                    next_kwargs['do_flow'] = False
                    next_kwargs['melty_window'] = melty_window
                    next_kwargs['on_drag'] = False
                    next_kwargs['no_measure'] = True
                    next_kwargs['auto_resize'] = True
                    next_kwargs['enable_scroll'] = False
                    next_kwargs['drag_window'] = False
                    next_kwargs['enable_flow'] = False
                    #next_kwargs['enable_flow'] = False
                    next_kwargs['z_pos'] = Melty.depth + 7
                    Melty.depth = Melty.depth + 7

                    if draw_state.mouse_btn_state[0].initial_window_pos is None:
                        draw_state.mouse_btn_state[0].initial_window_pos = (0, 0)

                    initial_sx, initial_sy = melty.initial_scroll_offset
                    current_sx, current_sy = Melty.scroll_stack[-1] if len(Melty.scroll_stack) > 0 else (0, 0)
                    delta_sx = current_sx - initial_sx
                    delta_sy = current_sy - initial_sy
                    pos_x = drag_delta[0] + draw_state.bounds_left + delta_sx
                    pos_y = drag_delta[1] + draw_state.bounds_top + delta_sy

                    Melty.undo_clip(unique)

                    start_pos = imgui.get_cursor_screen_pos()
                    imgui.set_cursor_screen_pos((pos_x, pos_y))
                    return_value = draw_inner_main(clean_args, draw_state, input_value, next_kwargs, melty, tile_id, unique)
                    kwargs['on_drag'] = True
                    Melty.redo_clip(unique)

                    Melty.depth = Melty.depth - 7

                    imgui.set_cursor_screen_pos(start_pos)

                else:
                    #### MAIN CALL #######################################################
                    return_value = draw_inner_main(clean_args, draw_state, input_value, kwargs, melty, tile_id, unique)
                    #######################

                draw_state._imgui_is_edited = imgui.is_item_edited()
                draw_state._imgui_is_active = imgui.is_item_active()
                draw_state._imgui_is_focused = imgui.is_item_focused()
                draw_state._imgui_scroll_y = imgui.get_scroll_y()
                draw_state._imgui_is_hovered = imgui.is_item_hovered()
                draw_state._imgui_scroll_y = imgui.get_scroll_y()

                if draw_state._has_popup:
                    is_popup_open = Melty.imgui_popup_open
                    if is_popup_open != draw_state._imgui_popover_open and not is_popup_open:
                        Melty.cache.invalidate_up_by_obj(input_value)

                    draw_state._imgui_popover_open = Melty.imgui_popup_open

            except Exception as e:
                print_colored_traceback(*sys.exc_info())
        except Exception as e:
            print_colored_traceback(*sys.exc_info())
        finally:
            def end_of_render():
                pop_style_var(2)
                if Melty.imgui_crashed:
                    return False, None

                # Needs to go after mouse down check
                push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))


                pop_id()
                end_group()
                pop_style_var(2)

                if draw_state.left is not None and draw_state.top is not None:
                    if draw_state.width is not None and draw_state.height is not None:
                        imgui.set_cursor_screen_pos((start_cursor[0],
                                                     start_cursor[1] + draw_state.height))
                item_rect = imgui.get_item_rect_size()
                if len(Melty.clip_stack) > 0:
                    clip_width = Melty.clip_stack[-1][2] - Melty.clip_stack[-1][0]
                    item_rect = (min(item_rect[0], clip_width), item_rect[1])
                original_width_b = draw_state.bounding_width
                original_height_b = draw_state.bounding_height
                # if kwargs.get("auto_resize", True):
                #     draw_state.window_size = None

                if not melty.drag_in_progress:

                    draw_state.bounds_left = snap_int(start_cursor[0])
                    draw_state.bounds_top = snap_int(start_cursor[1])

                    if kwargs.get("auto_resize", True) or draw_state.window_size is not None:
                        draw_state.width = snap_int(item_rect[0])
                        draw_state.height = snap_int(item_rect[1])
                if not kwargs.get("auto_resize", True) and draw_state.window_size is not None:
                    margin = 40

                    display_size = imgui.get_io().display_size
                    clamped_size = (
                        min(draw_state.window_size[0], display_size[0]),
                        min(draw_state.window_size[1], display_size[1] - margin)
                    )
                    draw_state.window_size = clamped_size
                    draw_state.width = draw_state.window_size[0]
                    draw_state.height = draw_state.window_size[1]
                    draw_state.bounding_width = draw_state.window_size[0]
                    draw_state.bounding_height = draw_state.window_size[1]
                else:
                    draw_state.bounding_width = snap_int(item_rect[0])
                    draw_state.bounding_height = snap_int(item_rect[1])

                    if draw_state.window_size is None and melty_window:
                        window_margin = 8
                        draw_state.window_size = snap_int(item_rect[0]) + window_margin, snap_int(item_rect[1])
                        draw_state.width = snap_int(item_rect[0])
                        draw_state.height = snap_int(item_rect[1])

                if (draw_state.bounding_width != original_width_b or
                        draw_state.bounding_height != original_height_b):
                    request_render()


                ######################################## HANDLE ACTIONS #######################
                is_hovered = draw_state.is_hovered()

                melty.triggered_actions.pop(unique, None)
                handle_actions(melty, unique, draw_state, func)

                draw_state._hovered = False
                draw_state.hotkey_receiver = False
                if is_hovered:
                    melty.hover_stack.append(unique)

                if is_hovered and func in Melty.hotkey_registry:
                    melty.hotkey_stack.append(unique)

                ###############################################################################
                if depth_to_restore is not None:
                    Melty.depth = depth_to_restore
                    Melty.wrapped_depth = wrapped_depth_to_restore

                if inc_depth:
                    Melty.depth = Melty.depth - 1
                    Melty.unique_stack.pop()

                Melty.input_value_stack.pop()

                hovered_draw_state = None
                # Root view
                if Melty.depth == 0:
                    melty.last_mouse_pos = imgui.get_mouse_pos()
                    # Did mouse move
                    if len(melty.hover_stack) > 0:
                        last = melty.hover_stack[0]
                        hovered_draw_state = Melty.vis.root.draw_state_registry.get(last, None)
                        if hovered_draw_state is not None:
                            hovered_draw_state._hovered = True
                            Melty.hovered_drawstate_pending.add(hovered_draw_state.id)
                            # Melty.cache.invalidate_by_obj(input_value)
                            # request_render()

                    if len(melty.hotkey_stack) > 0:
                        last = melty.hotkey_stack[0]
                        hovered_draw_state = Melty.vis.root.draw_state_registry.get(last, None)
                        if hovered_draw_state is not None:
                            hovered_draw_state.hotkey_receiver = True
                            # Melty.invalidate(tile_id)

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

                if melty_window:
                    Melty.melty_window_stack.pop()

                if melty_window:
                    imgui.set_cursor_screen_pos((0,0))

                # if kwargs.get("on_drag", False):
                #     imgui.set_cursor_screen_pos((draw_state.cursor_left, draw_state.cursor_top))

                # Melty.vis._frame_id += 1

                if return_value is None:
                    changed, new_value = False, None
                elif isinstance(return_value, tuple) and len(return_value) == 2:
                    changed, new_value = return_value
                else:
                    imgui.text("Unsupported return from render_func")
                    changed, new_value = False, None

                Melty.wrapped_depth = Melty.wrapped_depth - 1

                end_time = time.time()
                draw_state.render_time = end_time - start_time

                return changed, new_value

            changed, new_value = end_of_render()

        return changed, new_value

    def draw_inner_main(clean_args, draw_state, input_value, kwargs, melty, tile_id, unique):
        return_value = None
        start_cursor = imgui.get_cursor_screen_pos()
        use_cache = kwargs.get("use_cache", False)
        collection = kwargs.get("collection", None)
        global_toggles = kwargs.get("global_toggles", {})
        if global_toggles.offscreen_debug:
            depth_tint = (Melty.wrapped_depth * 0.05)
            jet = jet_color(depth_tint)
            floating_text(f"{func.__name__} w:{Melty.wrapped_depth}", tint=jet)
        if draw_state._has_popup:
            draw_state._imgui_popover_open = Melty.imgui_popup_open

        inside_clip = Melty.inside_clip(rect=(start_cursor[0], start_cursor[1],
                                               draw_state.width, draw_state.height))
        if inside_clip != draw_state.clipped and inside_clip:
            Melty.cache.invalidate_by_obj(tile_id)

            # Melty.cache.invalidate_by_obj(collection)

        draw_state.clipped = inside_clip

        if use_cache and Melty.cache.enabled:
            last_bounding_hovered = draw_state.is_bounding_hovered()
            draw_state._bounding_hovered = last_bounding_hovered
            hover_changed = last_bounding_hovered != draw_state._bounding_hovered

            if (draw_state._bounding_hovered or hover_changed or draw_state._hovered or
                    draw_state.width is None or draw_state.height is None or draw_state._imgui_popover_open):
                Melty.cache.invalidate(tile_id)
                Melty.cache.invalidate_by_obj(draw_state)

                # Melty.cache.invalidate_by_obj(collection)
                # Melty.cache.invalidate_by_obj(input_value)
                # Melty.cache.invalidate_current()
            # if melty.dragged_tile == tile_id:
            #     Melty.cache.invalidate_by_obj(draw_state)
            #
            #     # for a_tile in Melty.tile_id_stack:
            #     #     Melty.cache.invalidate(a_tile)
            #     # Melty.cache.invalidate_by_obj(collection)
            #     # Melty.cache.invalidate_by_obj(input_value)
            #     Melty.cache.invalidate(tile_id)
            #     # Melty.cache.invalidate_current()
            #     request_render()
            offscreen_depth = Melty.depth

            if Melty.channels_split:
                draw_list = imgui.get_window_draw_list()
                draw_list.channels_set_current(min(offscreen_depth, Melty.max_depth - 1))

            if Melty.cache.mark_start_offscreen(input_value=input_value, collection=collection,
                                                draw_state=draw_state, key=tile_id,
                                                layer=Melty.depth, caller=func):
                return_value = func(**clean_args)
                # Scroll position relative to view
                draw_state._imgui_scroll_y = imgui.get_scroll_y()
            else:
                pass
                # left, top = imgui.get_item_rect_min()
                # width, height = imgui.get_item_rect_size()
                # Melty.cache.cache_mark_rect(Melty.layer, left, top, width, height,
                #                            key=f"{unique}_mask")

            Melty.cache.mark_end_offscreen()
        else:
            return_value = func(**clean_args)


        return return_value

    return wrapper
