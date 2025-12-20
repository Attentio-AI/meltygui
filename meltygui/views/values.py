import inspect
import os
import shutil
import sys
import types
from collections import deque, defaultdict
from copy import copy
from enum import Enum
from inspect import Parameter
from math import sqrt
from types import NoneType

import numpy
from OpenGL import GL as gl

import glfw
from imgui.core import _DrawList
from numpy import uint32

from shader_library.shader_manager.texture_manager import PendingTexture
from src.lsd.gl_gui.model.core_model.core_enums import ProfileMode
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.utils.custom_views import print_colored_traceback, push_style_var, \
    push_style_color, pop_style_color, pop_style_var, end, begin
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace
from src.lsd.gl_gui.melty import Melty, CollectionAction, add_to_collection, \
    ManagedWindow
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line, new_line
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
from src.lsd.gl_gui.view.core_views.core_meta import Meta
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import hotkey
from src.lsd.gl_gui.view.core_views.core_render import render_func, tmp_undo_stack, redo_stack, push_id, pop_id, \
    render_wrapper, annotation_track, listens_for, begin_window, end_window
from src.lsd.gl_gui.model.core_model.draw_state import KeyMod, Hotkey, ZoomState
from src.lsd.gl_gui.view.core_views.cst_proxy import *
import libcst as cst

from src.lsd.gl_gui.view.core_views.decoration.invalidation_decoration import live
from src.lsd.gl_gui.view.core_views.decoration.profile_decoration import profile
from src.lsd.gl_gui.view.core_views.folders_proxy import FolderProxy
from src.lsd.gl_gui.view.core_views.inspect_utils import set_fn_defaults
from collections.abc import MutableMapping
from src.lsd.gl_gui.view.core_views.codec_register import registry as FILE_CODECS


@render_wrapper(wraps=render_func, use_cache=False)
def with_header_minimal(func, **o_kwargs):
    def wrapper(next_kwargs=None, **kwargs):
        if Melty.annotation_mode:
            annotation = annotation_track( wrapper=wrapper, **o_kwargs)
            if annotation is not None: return annotation

        next_kwargs['func'] = func
        next_kwargs['outer_func'] = wrapper
        next_kwargs['y_offset'] = 0
        next_kwargs['show_bg'] = kwargs.get("show_bg", False)
        next_kwargs['is_tree'] = kwargs.get("is_tree", False)
        next_kwargs['min_width'] = kwargs.get('min_width', 200)
        return core_header(**next_kwargs)

    setattr(wrapper, '__name__', f"{func.__name__} --- with_header_minimal")
    return wrapper



@render_wrapper(wraps=render_func, use_cache=False)
def with_header(func, **o_kwargs):
    def wrapper(next_kwargs=None, draw_state=None, **kwargs):
        if Melty.annotation_mode:
            annotation = annotation_track( wrapper=wrapper, **o_kwargs)
            if annotation is not None: return annotation
        next_kwargs['func'] = func
        next_kwargs['outer_func'] = wrapper
        return_val = core_header(**next_kwargs)

        return return_val

    setattr(wrapper, '__name__', f"{func.__name__} --- with_header ")

    return wrapper

source = "x = foo(val=1)\nprint(x)\nsome_list=[0, 1, 2, 3]\n"
module = cst.parse_module(source)
proxy = cst_wrap(module)
name_edits = {}
code_export_str = "Test"

filesystem_proxy = FolderProxy("/home/lukas/test_folder", text_mode=True)
# Main draw function, called by the GUI framework

@live
class TestObj:
    def __init__(self):
        self.test_val = 0.0
        self.test_list = [1, 2, 3, 4, 5]

test_obj = TestObj()

def draw_melty_windows(vis):
    flags = (imgui.WINDOW_NO_BACKGROUND | imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE |
             imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_NAV_FOCUS |
            imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS | imgui.WINDOW_NO_NAV_INPUTS | imgui.WINDOW_NO_NAV |
             imgui.WINDOW_NO_COLLAPSE | imgui.WINDOW_NO_SAVED_SETTINGS)

    imgui.set_next_window_position(0,0)
    # Fill the entire screen
    fb_w, fb_h = map(int, imgui.get_io().display_size)

    imgui.set_next_window_size(fb_w - 300, fb_h)
    title = "main##window_melty"
    opened, _ = begin(title, closable=False, flags=flags)

    Melty.imgui_main_window_hovered = imgui.is_window_hovered()

    Melty.begin_frame()

    imgui.invisible_button("window_blocker", width=fb_w, height=fb_h)
    imgui.set_cursor_screen_pos((0,0))
    imgui.set_item_allow_overlap()

    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)
    Melty.channels_split = True
    Melty.window_stack.append((title, True))

    draw_main(name="Main Window", vis=vis)
    draw_window(test_obj, name="Layer Main", layer=2)

    Melty.end_frame()

    # End frame ###############
    Melty.window_stack.pop()
    draw_list.channels_merge()
    Melty.channels_split = False

    end()

@render_func(use_cache=False)
def draw_main(input_value, vis):
    global test_obj
    draw_window(Melty.profiles_results, show_bg=True, name="Profile Results")
    draw_window(Melty.registered_windows, indent_size=10, is_tree=True, show_add_delete=False, name="Window Manager")

    draw_window(test_obj, name="Layer 1")

    draw_window(input_value=proxy, name="CST Proxy")
    draw_window(filesystem_proxy, name="Filesystem Test")
    draw_window(vis.root.lora_collection, name="Test Window 1")
    draw_window(vis.root.lora_collection.loras, name="Test Window 2")
    draw_window(Melty.last_invalid, show_bg=True, name="Last Invalid")
    draw_window(Melty.registered_windows, indent_size=10, is_tree=True, show_add_delete=False, name="Another widnow manager")

    snapshot_tex = Melty.filter.normalize(Melty.cache.snapshot_tex)
    draw_window(Melty.cache.snapshot_tex, show_bg=True, name="Snapshot Texture")

    changed, new_val = draw_window(0.0, layer=31, name="Test return")
    if changed:
        print("Value changed:", new_val)

    normalized_submask = Melty.filter.normalize(Melty.cache._sub_mask_tex)
    draw_window(normalized_submask, show_bg=True, max_contrast=30,
                max_brightness=30, name="Submask Texture")

    # draw_window(Melty.last_request_render, show_bg=True, name="Last Invalid")

import OpenGL.GL as gl
import numpy


@with_header(is_default_for=PendingTexture, use_cache=False, enable_scroll=False,
             auto_resize=True, indent_size=0)
def draw_pending_texture(input_value:PendingTexture):
    if input_value.texture_id is None:
        imgui.text(f"Uploading... {id(input_value)}")
        return False, None

    return draw_texture(input_value.texture_id, name=f"{input_value.name[:30]}",
                 auto_resize=False, show_header=False, indent_size=0)

@with_header(is_default_for=numpy.uint32, show_bg=True,
             use_cache=False, show_add_delete=False,
             indent_size=1, min_width=100, min_height=100,
             enable_scroll=True, zoom_speed=0.2)
def draw_texture(input_value: numpy.uint32, hovered, scroll_y_changed, middle_mouse_drag, right_mouse_drag, zoom_state: ZoomState, zoom_speed, header_height=0, min_zoom=0.1,
                 max_zoom=50.0, style_manager=None, max_brightness=5.0, max_contrast=5.0,
                 on_scroll=0, draw_state=None):

    original_id = input_value
    texture_id = input_value

    # Ensure we have valid state if this is the first run
    if not hasattr(zoom_state, 'zoom'):
        zoom_state.zoom = 1.0
        zoom_state.center_u = 0.5
        zoom_state.center_v = 0.5

    # Check if opengl texture ID is valid
    if not gl.glIsTexture(texture_id):
        imgui.text(f"Error: {texture_id} is not a valid texture")
        return False, None

    # 1. Query Texture Properties
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)

    width = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_WIDTH)
    height = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_HEIGHT)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    # if draw_state.width is None:
    #     draw_state.width = width
    #
    # if draw_state.height is None:
    #     draw_state.height = height


    if width > 16384 or height > 16384:
        imgui.text(f"Error: Texture size {width}x{height} exceeds maximum supported size.")
        return False, None

    if width == 0 or height == 0:
        return False, None

    # 2. Canvas Setup (Fill available space)
    view_width = max(1, draw_state.width - 2)
    view_height = max(1, draw_state.height - header_height - 2)

    # 3. Calculate Aspect Ratio Corrections
    tex_aspect = width / height
    view_aspect = view_width / view_height

    # Calculate the visible UV width/height based on zoom and aspect ratio.
    if view_aspect > tex_aspect:
        # View is wider: Fit to Height
        uv_height_size = 1.0 / zoom_state.zoom
        uv_width_size = uv_height_size * (view_aspect / tex_aspect)
    else:
        # View is taller: Fit to Width
        uv_width_size = 1.0 / zoom_state.zoom
        uv_height_size = uv_width_size * (tex_aspect / view_aspect)

    # 4. Handle Input and Interaction
    imgui.dummy(view_width, view_height)

    mixed_color = (1, 1, 1, 1)
    highlight_color = (1, 1, 1, 1)

    if style_manager is not None:
        mixed_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                   value=0.3, factor=0.9, saturation_scale=1.0, alpha=1.0)
        highlight_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                   value=1.0, factor=0.9, saturation_scale=1.0, alpha=1.0)
    io = imgui.get_io()
    overlay:_DrawList = imgui.get_overlay_draw_list()

    if right_mouse_drag and not right_mouse_drag.modifiers:
        b_str = f"{zoom_state.brightness:.3f}"
        overlay.add_text(right_mouse_drag.x, right_mouse_drag.y - 30,
                           col=imgui.get_color_u32_rgba(*highlight_color[:3], 1),
                          text=f"brightness:{zoom_state.brightness:.3}\ncontrast:{zoom_state.contrast:.3}" )
        if io.key_shift:
            zoom_state.brightness += right_mouse_drag.dx * 0.001
            zoom_state.contrast -= right_mouse_drag.dy * 0.001
        else:
            zoom_state.brightness += right_mouse_drag.dx * 0.005
            zoom_state.contrast -= right_mouse_drag.dy * 0.005

        zoom_state.brightness = max(0.0, min(max_brightness, zoom_state.brightness))
        zoom_state.contrast = max(0.0, min(max_contrast, zoom_state.contrast))

    if zoom_state.brightness != 0.0 or zoom_state.contrast != 1.0:
        texture_id = Melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

    # texture_id = Melty.filter.swirl(
    #     texture_id,
    #     angle=zoom_state.brightness,
    #     radius=zoom_state.contrast
    # )

    p_min = (imgui.get_item_rect_min()[0], imgui.get_item_rect_min()[1])
    p_max = (imgui.get_item_rect_max()[0], imgui.get_item_rect_max()[1])
    p_min_x, p_min_y = p_min[0], p_min[1]

    scroll_delta = 0
    if scroll_y_changed is not None:
        scroll_delta = scroll_y_changed.value

    # --- Logic: Zoom and Pan ---

    zoom_delta = 0.0

    # 4a. Handle Zoom Triggers (Scroll & Keyboard)

    # Keyboard Shortcuts (1, 2, 3, 4)
    forced_zoom = -1.0
    key_1 = 49
    numpad_key_1 = 321
    if hovered:
        if imgui.is_key_pressed(key_1) or imgui.is_key_pressed(numpad_key_1):  # Key '1'
            forced_zoom = 1.0
            # Reset Pan to Center
            zoom_state.center_u = 0.5
            zoom_state.center_v = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
        elif imgui.is_key_pressed(50):  # Key '2'
            forced_zoom = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
        elif imgui.is_key_pressed(51):  # Key '3'
            forced_zoom = 0.25
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
        elif imgui.is_key_pressed(52):  # Key '4'
            forced_zoom = 0.125
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0

    if forced_zoom > 0:
        zoom_state.zoom = forced_zoom
        # Recalculate uv_size immediately for consistent bounding this frame
        if view_aspect > tex_aspect:
            uv_height_size = 1.0 / zoom_state.zoom
            uv_width_size = uv_height_size * (view_aspect / tex_aspect)
        else:
            uv_width_size = 1.0 / zoom_state.zoom
            uv_height_size = uv_width_size * (tex_aspect / view_aspect)

    # Scroll Logic
    if scroll_delta != 0:
        if io.key_shift:
            zoom_delta = scroll_delta * zoom_speed * 0.3
        else:
            zoom_delta = scroll_delta * zoom_speed
    elif middle_mouse_drag and middle_mouse_drag.modifiers == glfw.MOD_CONTROL:
        zoom_delta = io.mouse_delta.y * -0.008

    # 4b. Handle Pan (Middle Click Drag)
    if middle_mouse_drag and middle_mouse_drag.modifiers != glfw.MOD_CONTROL:
        u_scale = uv_width_size / view_width
        v_scale = uv_height_size / view_height
        if middle_mouse_drag.modifiers == glfw.MOD_SHIFT:
            zoom_state.center_u -= middle_mouse_drag.dx * u_scale * 0.5
            zoom_state.center_v += middle_mouse_drag.dy * v_scale * 0.5
        else:
            zoom_state.center_u -= middle_mouse_drag.dx * u_scale
            zoom_state.center_v += middle_mouse_drag.dy * v_scale

    # 4c. Apply Zoom Logic (Zoom to Cursor)
    if zoom_delta != 0.0:
        zoom_factor = 1.0 + zoom_delta
        new_zoom = max(min_zoom, min(zoom_state.zoom * zoom_factor, max_zoom))

        if new_zoom != zoom_state.zoom:
            mouse_pos = imgui.get_mouse_pos()

            if io.key_ctrl:
                mouse_u_ratio, mouse_v_ratio = (0.5, 0.5)
            else:
                mouse_u_ratio = (mouse_pos[0] - p_min_x) / view_width
                mouse_v_ratio = (mouse_pos[1] - p_min_y) / view_height

            curr_uv_w = uv_width_size
            curr_uv_h = uv_height_size

            # Recalculate new UV dimensions
            if view_aspect > tex_aspect:
                new_uv_h = 1.0 / new_zoom
                new_uv_w = new_uv_h * (view_aspect / tex_aspect)
            else:
                new_uv_w = 1.0 / new_zoom
                new_uv_h = new_uv_w * (tex_aspect / view_aspect)

            diff_w = curr_uv_w - new_uv_w
            diff_h = curr_uv_h - new_uv_h

            zoom_state.center_u += diff_w * (mouse_u_ratio - 0.5)
            zoom_state.center_v += diff_h * (0.5 - mouse_v_ratio)

            zoom_state.zoom = new_zoom

            # Use these for Step 5
            uv_width_size = new_uv_w
            uv_height_size = new_uv_h

    # 5. Calculate Final UVs and Clamp to Bounds
    half_uv_w = uv_width_size * 0.5
    half_uv_h = uv_height_size * 0.5

    # --- Bounding Logic Start ---
    margin_px = 20.0

    pixel_u = uv_width_size / view_width
    pixel_v = uv_height_size / view_height
    margin_u = margin_px * pixel_u
    margin_v = margin_px * pixel_v

    min_u = -half_uv_w + margin_u
    max_u = 1.0 + half_uv_w - margin_u

    if min_u > max_u:
        zoom_state.center_u = 0.5
    else:
        zoom_state.center_u = max(min_u, min(zoom_state.center_u, max_u))

    min_v = -half_uv_h + margin_v
    max_v = 1.0 + half_uv_h - margin_v

    if min_v > max_v:
        zoom_state.center_v = 0.5
    else:
        zoom_state.center_v = max(min_v, min(zoom_state.center_v, max_v))
    # --- Bounding Logic End ---

    uv_x_min = zoom_state.center_u - half_uv_w
    uv_x_max = zoom_state.center_u + half_uv_w
    uv_y_min = zoom_state.center_v - half_uv_h
    uv_y_max = zoom_state.center_v + half_uv_h

    uv_a = (uv_x_min, uv_y_max)
    uv_b = (uv_x_max, uv_y_min)

    # 6. Project and Draw
    scale_u_px = view_width / uv_width_size
    scale_v_px = view_height / uv_height_size

    # Project Texture Edges
    raw_img_left = p_min_x + (0.0 - uv_x_min) * scale_u_px
    raw_img_right = p_min_x + (1.0 - uv_x_min) * scale_u_px
    raw_img_top = p_min_y + (uv_y_max - 1.0) * scale_v_px
    raw_img_bottom = p_min_y + (uv_y_max - 0.0) * scale_v_px

    # Intersect with Viewport
    clip_left = max(p_min_x, raw_img_left)
    clip_right = min(p_max[0], raw_img_right)
    clip_top = max(p_min_y, raw_img_top)
    clip_bottom = min(p_max[1], raw_img_bottom)

    draw_list:_DrawList = imgui.get_window_draw_list()

    Melty.push_clip((clip_left, clip_top, clip_right, clip_bottom))
    draw_list.add_image_rounded(texture_id,
                                a=p_min,
                                b=p_max,
                                uv_a=uv_a,
                                uv_b=uv_b,
                                rounding=5.0)

    Melty.pop_clip()

    draw_list.add_rect(raw_img_left - 1, raw_img_top - 1, raw_img_right + 1, raw_img_bottom + 1,
                       imgui.get_color_u32_rgba(*mixed_color[:3], 1.0),
                       0.0, 0, 1.0)

    line_height = imgui.get_text_line_height()
    draw_list.add_text(max(p_min_x + 5, raw_img_left), clip_top - line_height - 5,
                       imgui.get_color_u32_rgba(*mixed_color[:3],1.0),
                       text=f"{original_id} - {texture_id} - {width}x{height} - Zoom: {zoom_state.zoom:.2f}x")

    return True, draw_state

    # draw_window(Melty.last_request_render, show_bg=True, name="Last Invalid")
@with_header(is_default_for=ManagedWindow, is_tree=False,
             show_bg=True, show_add_delete=False)
def draw_debug(input_value, melty):
    draw_any(melty)

@with_header(is_default_for=ManagedWindow, is_tree=False, show_name=False,
             show_bg=True, show_add_delete=False)
def draw_managed_window(input_value, name, draw_state, style_manager, unique=0, mouse_down=False, **kwargs):

    window_input_value = input_value.input_value
    if hasattr(window_input_value, 'tint') and window_input_value.tint is not None:
        draw_state.tint = window_input_value.tint
    else:
        draw_state.tint = (0.9, 0.991, 0.999)

    window_draw_state = input_value.draw_state

    if mouse_down:
        window_draw_state.closed = not window_draw_state.closed

    imgui.dummy(30, 20)
    imgui.same_line()

    if window_input_value == Melty.registered_windows:
        button(f"{name}", color=(0,0,0,0), saturation=1.3, width=130)[0]
        return
    if window_draw_state.closed:
        if button(f"{name}", color=(0,0,0), saturation=1.3, width=130)[0]:
            print("Opening window")
            window_draw_state.closed = False
    else:
        if button(f"{name}", saturation=1.3, width=130)[0]:
            print("Closing window")
            window_draw_state.closed = True


@render_func(use_cache=True, auto_resize=False, closable=True, show_bg=True, melty_window=True, draggable=True)
def draw_window(input_value, unique, inner_func=None, style_manager=None, **kwargs):
    window_name = kwargs.get('name', 'Managed Window')
    draw_state = kwargs.get('draw_state', None)
    cursor_pos = imgui.get_cursor_screen_pos()
    if draw_state.bounding_width is not None and draw_state.width > 0 and draw_state.height > 0:
        imgui.set_cursor_screen_pos(cursor_pos)

        # if draw_state.expanded:
        #     if Melty.channels_split:
        #         draw_list = imgui.get_window_draw_list()
        #         draw_list.channels_set_current(Melty.get_channel() + 1)
        #     imgui.invisible_button(str(unique) + "window_blocker", width=draw_state.width, height=draw_state.height)
        #     imgui.set_cursor_screen_pos(cursor_pos)
        #     imgui.set_item_allow_overlap()
        # # else:
        #     imgui.button("##", width=draw_state.width, height=20)
        #     imgui.set_cursor_screen_pos(cursor_pos)
        #     imgui.set_item_allow_overlap()

    if draw_state.width is not None and draw_state.height is not None and draw_state.expanded:
        loading_icon_0 = "\uf00d"
        loading_icon_1 = "\uf067"
        frame_spacing = 1
        alpha = 0.25
        icon_cursor = imgui.get_cursor_screen_pos()
        icon_x = icon_cursor[0] + draw_state.width - 20
        icon_y = icon_cursor[1] + draw_state.height - 15
        if (Melty.frame_count // frame_spacing) % 2 == 0:
            draw_list = imgui.get_overlay_draw_list()
            draw_list.add_text(icon_x, icon_y,
                               imgui.get_color_u32_rgba(1, 1, 1, alpha),
                               loading_icon_0)
        else:
            draw_list = imgui.get_overlay_draw_list()
            draw_list.add_text(icon_x, icon_y,
                               imgui.get_color_u32_rgba(1, 1, 1, alpha),
                               loading_icon_1)

    previous_tint = style_manager.get_tint()
    if hasattr(input_value, 'tint') and getattr(input_value, "tint") is not None:
        style_manager.set_imgui_tint(*getattr(input_value, "tint"))

    meta = kwargs.get("meta", None)
    if meta is None:
        if hasattr(Meta, 'get_child_meta'):
            meta = Meta.get_child_meta(None, field_name=kwargs.get("name", ''), value=input_value)

    if inner_func is None:
        if meta.view_function is None or 'draw_any' in meta.view_function.__name__:
            meta.view_function = draw_collection
    else:
        meta.view_function = inner_func

    return_val = meta.view_function(input_value, **kwargs)


    if hasattr(input_value, 'tint'):
        style_manager.set_imgui_tint(*previous_tint)

    return return_val

def draw(vis):
    draw_melty_windows(vis)


def export_code(test_param_2: int = 5):
    # print(f"hello {test_param_2}")
    global code_export_str
    code_export_str = proxy.node.code


######################## libCST START ##########################

@render_wrapper(wraps=render_func)
def cst_header(func, **o_kwargs):
    def wrapper(next_kwargs=None, draw_state=None, **kwargs):
        if Melty.annotation_mode:
            annotation = annotation_track( wrapper=wrapper, **o_kwargs)
            if annotation is not None: return annotation

        next_kwargs['func'] = func
        next_kwargs['outer_func'] = wrapper
        next_kwargs['show_add_delete'] = False
        next_kwargs['show_bg'] = kwargs.get('show_bg', True)
        next_kwargs['is_tree'] = kwargs.get('is_tree', True)
        next_kwargs['y_offset'] = Melty.collection_spacing

        return core_header(**next_kwargs)

    setattr(wrapper, '__name__', f"{func.__name__} --- with_header ")

    return wrapper

@cst_header(is_default_for=cst.SimpleStatementLine, header_same_line=True)
def draw_cst_single_line(input_value: cst.SimpleStatementLine):
    # An Assign has one or more targets, an AssignEqual token, and a value
    draw_any(input_value.body)


@cst_header(is_default_for=cst.Comment)
def draw_comment(input_value: cst.Comment, **kwargs):
    # imgui.text(f"# {input_value.value}")
    pass

@cst_header(is_default_for=cst.SimpleWhitespace, header_same_line=True, show_name=False)
def draw_cst_simple_whitespace(input_value: cst.SimpleWhitespace):
    # An Assign has one or more targets, an AssignEqual token, and a value
    # imgui.same_line()
    pass

    # imgui.button("SWS")
    # imgui.set_item_allow_overlap()
    # imgui.same_line()


# --- Assignments ---
def assign_name(input_value: cst.Assign):
    if len(input_value.targets) == 1:
        target = list(input_value.targets.values())[0]
        if isinstance(target, cst.AssignTarget):
            if isinstance(target.target, cst.Name):
                return target.target.value
            else:
                return str(target.target)
        else:
            return str(target)
    else:
        return "Multiple Targets"

@cst_header(is_default_for=cst.Assign, header_same_line=True, name_func=assign_name)
def draw_cst_assign(input_value: cst.Assign):
    # An Assign has one or more targets, an AssignEqual token, and a value
    # draw_collection(input_value.targets)
    imgui.text_colored("=", *(1, 1.1, 1, 0.5))
    imgui.same_line()
    draw_any(input_value.value)


# --- Names ---
@cst_header(is_default_for=cst.AssignTarget)
def draw_assign_target(input_value: cst.AssignTarget):
    draw_any(input_value.target)


# --- Names ---
@render_func(is_default_for=cst.Name)
def draw_cst_name(input_value: cst.Name):
    # imgui.text(f"{input_value.value}")
    pass


@cst_header(is_default_for=(cst.Expr, cst.Element), header_same_line=True, show_header=False,
            show_bg=False, show_name=False)
def draw_cst_expr(input_value):
    # Just render the wrapped expression
    draw_any(input_value.value)
# --- Function Calls ---

# Call name
def call_name(call: cst.Call):
    if isinstance(call.func, cst.Name):
        return call.func.value
    else:
        return ""

@cst_header(is_default_for=cst.Call, header_same_line=True, name_func=call_name)
def draw_cst_call(input_value: cst.Call):
    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text("(")
    imgui.same_line()
    draw_any(input_value.args, horizontal=True)
    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text(")")

#
# @with_header(is_default_for=cst.Module, use_cache=True, show_add_delete=False)
# def draw_cst_module(input_value: cst.Module):
#     return draw_any(input_value.body, show_name=False)

@cst_header(is_default_for=cst.List, show_name=False, show_bg=False,
            header_same_line=True)
def draw_cst_list(input_value: cst.List):
    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text("[")
    imgui.same_line()
    draw_any(input_value.elements, horizontal=True)
    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text("]")

@render_func(is_default_for=CSTDictProxy, header_same_line=True,
             show_bg=False, indent_size=0, draggable=True)
def draw_cst_dict(input_value: CSTDictProxy):
    show_indices = False

    if len(input_value) > 0:
        # Show line numbers for dicts of simple statements
        if isinstance(list(input_value.values())[0], cst.SimpleStatementLine):
            show_indices = True
    # kwargs['show_name'] = False

    draw_collection(input_value, header_same_line=True, show_bg=False, enable_scroll=False,
                    show_name=False, show_indices=show_indices, indent_size=0)


# --- Parameters ---
@cst_header(is_default_for=cst.Parameters)
def draw_cst_parameters(input_value: cst.Parameters):
    first = True
    for param in input_value.params:
        if not first:
            imgui.same_line()
            imgui.text(",")
            imgui.same_line()
        draw_any(param)
        first = False


# --- Individual Parameter ---
@cst_header(is_default_for=cst.Param)
def draw_cst_param(input_value: cst.Param):
    draw_any(input_value.name)
    if input_value.default:
        imgui.same_line()
        imgui.text_colored("=", *(1,1,1,0.5))
        imgui.same_line()
        draw_any(input_value.default)


# @with_header(is_default_for=cst.Module, show_add_delete=False)
# def draw_cst_module(input_value: cst.Module):
#
#     return draw_any(input_value.body, show_name=False)

# --- Arguments ---
def arg_name(arg: cst.Arg):
    if arg.keyword:
        return arg.keyword.value
    elif isinstance(arg.value, cst.Name):
        return arg.value.value
    else:
        return None
@with_header_minimal(is_default_for=cst.Arg, show_bg=True, name_attrib="keyword",
                     name_func=arg_name, show_add_delete=False, header_same_line=True)
def draw_cst_arg(input_value: cst.Arg):
    draw_any(input_value.value)


# --- Individual Parameter ---
@render_func(is_default_for=cst.UnaryOperation, header_same_line=True, show_add_delete=False)
def draw_cst_int(input_value, width=None):
    int_str = input_value.value
    cast_str_to_int = int(int_str, 0)
    if cast_str_to_int == 25:
        pass

    changed, new_val = draw_int(cast_str_to_int, indent_size=0, show_name=False,
                                show_add_delete=False)
    if changed:
        input_value.value = str(new_val)


# --- Individual Parameter ---
@render_func(is_default_for=(cst.UnaryOperation, cst.Integer), header_same_line=True, show_add_delete=False)
def draw_cst_int(input_value, width=None):
    # Format the magnitude to match the original literal's base/prefix/case.
    def _format_like(template: str, magnitude: int) -> str:
        if template.startswith(("0x", "0X")):
            s = hex(magnitude)  # '0x2a'
            return s if template.startswith("0x") else "0X" + s[2:].upper()
        elif template.startswith(("0o", "0O")):
            s = oct(magnitude)  # '0o52'
            return s if template.startswith("0o") else "0O" + s[2:]
        elif template.startswith(("0b", "0B")):
            s = bin(magnitude)  # '0b101010'
            return s if template.startswith("0b") else "0B" + s[2:]
        else:
            return str(magnitude)

    # Determine current signed value and the template string to preserve formatting.
    if input_value.__class__ == cst.UnaryOperation:
        expr = input_value.expression  # expect an Integer
        op = input_value.operator
        inner_text = expr.value
        try:
            magnitude = int(inner_text, 0)
        except ValueError:
            magnitude = 0

        sign = -1 if isinstance(op, cst.Minus) else 1
        current_val = sign * magnitude
        fmt_template = inner_text
        is_unary = True
    else:  # cst.Integer
        inner_text = input_value.value
        current_val = int(inner_text, 0)
        fmt_template = inner_text
        is_unary = False

    changed, new_val = draw_int(current_val, indent_size=0, show_name=False, show_add_delete=False)
    if not changed:
        return False, input_value

    if is_unary:
        if new_val < 0:
            # Keep UnaryOperation with Minus; update inner Integer magnitude.
            if not isinstance(input_value.operator, cst.Minus):
                input_value.operator = cst.Minus()
            input_value.expression.value = _format_like(fmt_template, -new_val)
            return True, input_value
        else:
            # Collapse to a plain Integer.
            replacement = cst.Integer(value=_format_like(fmt_template, new_val))
            return True, replacement
    else:
        if new_val < 0:
            # Expand to UnaryOperation(Minus(), Integer(abs)).
            replacement = cst.UnaryOperation(
                operator=cst.Minus(),
                expression=cst.Integer(value=_format_like(fmt_template, -new_val)),
            )
            return True, replacement
        else:
            # Stay as Integer; update literal text.
            input_value.value = _format_like(fmt_template, new_val)
            return True, input_value


# @render_func(is_default_for=cst.Integer, header_same_line=True, show_add_delete=False)
# def draw_cst_int(input_value, width=None):
#     int_str = input_value.value
#     cast_str_to_int = int(int_str, 0)
#     if cast_str_to_int == 25:
#         pass
#
#
#     changed, new_val = draw_int(cast_str_to_int, indent_size=0, show_name=False,
#                                 show_add_delete=False)
#     if changed:
#         input_value.value = str(new_val)

####################### libCST END ##########################


@render_wrapper(wraps=render_func)
def render_with_foo(func, **kwargs):

    def wrapper(window_stack=None, **kwargs):
        imgui.text("Some wrapper")
        return func(skfs=False, **kwargs)

    return wrapper

@render_func(use_cache=False)
def draw_drag_drop_target(input_value, draw_state, on_drag, do_flow, depth,
                          collection, key, melty, y_offset, enable_flow, min_width,
                          unique, tag, style_manager, global_style, offset=0, indent_size=10):

    if Melty.active_layer == Melty.drag_layer:
        return False, 0.0

    cursor_y_screen = imgui.get_cursor_screen_pos()[1]

    if collection == input_value or not Melty.is_window_enabled():
        return False, 0.0

    if melty.initial_drag_offset is None:
        return False, 0.0

    if key is None:
        pass
    # ----------------- top spacing -----------
    falloff = 25.0 # Higher is gentler
    if enable_flow:
        drop_gap = 6.0
    else:
        drop_gap = 0.0

    drag_delta_curve = 1.0 - max(0.0, min(1.0, 1.0 - abs(melty.drag_delta[1] / 15.0)))

    mouse_pos = imgui.get_mouse_pos()
    cursor_top = imgui.get_cursor_screen_pos()[1]
    cursor_left = imgui.get_cursor_screen_pos()[0]
    static_offset = drop_gap
    distance_to_mouse = abs(mouse_pos[1] - cursor_y_screen -
                            melty.initial_drag_offset[1] - drop_gap + static_offset)
    bell_curve = max(0.0, min(1.0, 1.0 - (distance_to_mouse / falloff)))

    window_size = imgui.get_window_size()
    window_pos = imgui.get_window_position()
    window_rect = (window_pos[0], window_pos[1],
                   window_pos[0] + window_size[0],
                   window_pos[1] + window_size[1])
    mouse_over_window = imgui.is_mouse_hovering_rect(*window_rect)

    if melty.drag_in_progress:
        if melty.dragged_item is None:
            melty.drag_in_progress = False

        elif id(melty.dragged_item._input_value)== id(collection):
            return False, 0.0

    if melty.drag_in_progress and do_flow and not on_drag and mouse_over_window:
        flow_spacing = drop_gap * bell_curve * drag_delta_curve
    else:
        flow_spacing = 0.0
        drag_delta_curve = 1.0

    if tag == "top":
        Melty.flow_spacing += (flow_spacing)
        # imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + (flow_spacing))

    draw_list = imgui.get_window_draw_list()
    # if Melty.channels_split:
    #     draw_list.channels_set_current(min(Melty.max_depth - 1, depth + 2))

    # line_width = imgui.get_style().frame_padding.y * 2.0
    # color = style_manager.make_color_rgb(*(1.0, 1.0, 1.0), factor=1.0,
    #                                      value=1.0, alpha=1.0, saturation_scale=0.3)

    # cursor_bottom = imgui.get_cursor_screen_pos()[1]
    # ------------------ end spacing -----------
    cursor_bottom = cursor_top + max(2.0, flow_spacing)

    if tag == "bottom":
        # span = cursor_bottom - cursor_top
        cursor_bottom += 0
        cursor_top += 0

    if melty.drag_in_progress and not on_drag and do_flow and mouse_over_window:
        if draw_state.height is not None:
            active_drop = (melty.drag_drop_target == draw_state.unique
                           and tag == melty.drag_drop_target_tag)

            if Melty.channels_split:
                draw_list.channels_set_current(min(Melty.get_channel() + 1, Melty.max_depth - 1))

                if active_drop:
                    draw_list.channels_set_current(min(Melty.get_channel() + 2, Melty.max_depth - 1))
                    cursor_bottom += ((1.0 - drag_delta_curve) * drop_gap)

            if distance_to_mouse < melty.nearest_drop_distance:
                melty.nearest_drop_distance = distance_to_mouse
                melty.nearest_drop_target = draw_state.unique
                melty.nearest_drop_target_tag = tag

                melty.drag_drop_action.target_unique = draw_state.unique
                melty.drag_drop_action.target_tag = tag
                melty.drag_drop_action.target_key = key
                melty.drag_drop_action.target_collection = collection
                melty.drag_drop_action.target_draw_state = draw_state

                if melty.drag_drop_action.target_key is None:
                    pass

            height_as_factor = 800.0
            drag_distance = sqrt(melty.drag_delta[0] ** 2 + melty.drag_delta[1] ** 2)
            initial_fade_offset = max(min(1.0, melty.total_drag_distance / 10.0), 0.0)
            if melty.total_drag_frames < 1:
                initial_fade_offset = 0.0
            opacity = max(0.0, min(1.0, 1.0 - (distance_to_mouse / (height_as_factor * 0.3))))
            opacity *= initial_fade_offset
            # opacity = 1.0 if active_drop else opacity

            bg_tint = Melty.get_bg_color(-1)
            bg_style = global_style.get_global_constant("bg_style", folder="bg_styles")

            color = style_manager.make_custom_styled(*bg_tint, input=bg_style,
                                                        value=1.3,
                                                        alpha=opacity, saturation=0.8)

            # color = style_manager.make_color_rgb(*bg_tint, factor=0.0,
            #                                      value=1.0, alpha=opacity, saturation_scale=1.0)
            inactive_color = style_manager.make_custom_styled(*bg_tint, input=bg_style,
                                                              value=0.7,
                                                              alpha=opacity, saturation=0.8)
            # if draw_state.width == None:
            #     draw_state.width = min_width
            # if draw_state.left == None:
            #     draw_state.left = 1

            padding = imgui.get_style().frame_padding.x

            color = color if active_drop else inactive_color

            top = cursor_top - 1
            bottom = max(cursor_top, cursor_bottom - 1)
            left = cursor_left + offset
            right = cursor_left + draw_state.width - indent_size
            width = right - left
            height = bottom - top

            draw_list.add_rect_filled(left, top, right, bottom,
                                    col=imgui.get_color_u32_rgba(*color), rounding=4.0)

            if opacity > 0:
                Melty.cache.mask_mark_rect(Melty.max_depth - 1, left, top, width, height,
                                               key=f"{left}x{top}_flow")
            #
            # draw_list.add_line(draw_state.left, draw_state.top - 2 - offset,
            #                    draw_state.left + draw_state.width,
            #                    draw_state.top - 2 - offset,
            #                    col=imgui.get_color_u32_rgba(*color), thickness=3)

    return False, flow_spacing

def draw_header_end(global_style, unique, style_manager, show_search,
                    on_search, draw_state, collection, key, closable, is_tree,
                    melty, show_add_delete=True, parent_show_add_delete=False,
                    **kwargs):
    push_id(f"header_end_{unique}")
    bg_style = {
        "value": 0.01,
        "saturation": 1.0,
        "alpha": 1.0,
        'max_value': 1.0
    }
    bg_style = global_style.get_global_constant("bg_style", default=bg_style, folder="bg_styles")
    search_color = (style_manager.
                    make_color_style_value(input=bg_style, saturation=0.7,
                                           value=1.0))


    push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))

    imgui.begin_group()
    pop_style_var(2)

    if parent_show_add_delete:
        imgui.same_line()
        push_style_color(imgui.COLOR_TEXT, *search_color)
        push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
        if imgui.button(f"\uf1f8##del"):
            melty.to_delete(key, collection)
            print("No selected_views or remove_view method")
        same_line(spacing=0.0)
        pop_style_color(2)

    if closable:
        imgui.same_line()
        cursor_start = imgui.get_cursor_screen_pos()
        if draw_state.width is not None and draw_state.left is not None and draw_state.expanded:
            imgui.set_cursor_screen_pos((draw_state.left + draw_state.width - 20 + imgui.get_window_position()[0],
                                       imgui.get_cursor_screen_pos()[1]))

        close_icon = "\uf00d"
        if button(f"{close_icon}", color=(1, 1, 1, 0))[0]:
            draw_state.closed = not draw_state.closed
            Melty.cache.invalidate_up_by_obj(Melty.registered_windows)
        imgui.set_cursor_screen_pos(cursor_start)

    if is_tree:
        if not draw_state.expanded:
            imgui.same_line()
            imgui.dummy(20, 1)

    # if show_search or draw_state.search_active:
    #     imgui.same_line()
    #     imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + 2)
    #
    #     icon = "\uf002"
    #     imgui.text_colored(icon, *search_color)
    #     imgui.same_line()
    #     search_width = 150.0
    #     imgui.set_next_item_width(search_width)
    #     search_changed, new_search = imgui.input_text(f"##search{unique}", draw_state.search_text)
    #
    #     if search_changed:
    #         draw_state.search_text = new_search
    #         imgui.set_keyboard_focus_here(-1)
    #         request_render()
    #
    #     if not draw_state.search_active:
    #         draw_state.search_text = ""
    #
    #     if on_search:
    #         draw_state.search_active = True
    #         imgui.set_keyboard_focus_here(-1)
    #         request_render()
    #
    #     draw_state.search_active = imgui.is_item_focused()

    push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
    imgui.end_group()

    pop_id()
    pop_style_var(2)

@hotkey(glfw.KEY_O)
def toggle_offscreen():
    if Melty.cache.enabled:
        Melty.cache.set_enabled(False)
    else:
        Melty.cache.set_enabled(True)


import imgui


def draw_vertical_scrollbar(content_height: float,
                            view_height: float,
                            view_width: float,
                            scroll_offset: float,
                            scrollbar_width: float,
                            left: float = 0.0,
                            top: float = 0.0,
                            *,
                            pad: float = 0.0,
                            rounding: float = 3.0,
                            min_grab_size: float | None = None):
    # Style & colors
    style = imgui.get_style()
    if min_grab_size is None:
        min_grab_size = float(style.grab_min_size)


    col_track = imgui.get_color_u32_rgba(0,0,0,0.1)
    col_grab = imgui.get_color_u32_rgba(1,1,1, 0.3)
    col_border = imgui.get_color_u32(imgui.COLOR_BORDER)

    # Early clamps & deriveds
    view_height = max(0.0, float(view_height))
    view_width = max(0.0, float(view_width))
    content_height = max(0.0, float(content_height))
    scrollbar_width = max(0.0, float(scrollbar_width))

    max_scroll = max(0.0, content_height - view_height)
    scroll_offset = float(max(0.0, min(scroll_offset, max_scroll)))

    # Anchor the container at the current cursor position in screen space
    origin_x, origin_y = (left, top)

    bar_margin = 4.0
    bar_margin_x = 1.0

    # Track geometry (stick it to the right edge of the container)
    track_w = min(scrollbar_width, view_width)
    track_h = view_height
    track_x1 = origin_x + (view_width - track_w) - bar_margin_x
    track_y1 = origin_y + bar_margin
    track_x2 = track_x1 + track_w - bar_margin_x
    track_y2 = track_y1 + track_h - bar_margin * 2

    # Compute grab size & position
    if content_height <= 0.0 or track_h <= 0.0:
        grab_h = 0.0
        t = 0.0
    else:
        # Proportional size with a minimum; cap to track height.
        ratio = view_height / content_height if content_height > 0.0 else 1.0
        grab_h = max(min_grab_size, ratio * track_h)
        grab_h = min(grab_h, track_h)

        # Normalized scroll position -> grab top
        travel = max(0.0, track_h - grab_h)
        t = 0.0 if max_scroll == 0.0 else (scroll_offset / max_scroll)
        t = max(0.0, min(1.0, t))  # clamp just in case

    grab_y1 = track_y1 + (max(0.0, track_h - grab_h) * t)
    grab_y2 = grab_y1 + grab_h

    # Inner padding for nicer visuals
    inner_x1 = track_x1 + pad
    inner_x2 = track_x2 - pad
    inner_y1 = track_y1 + pad
    inner_y2 = track_y2 - pad
    grab_x1 = inner_x1
    grab_x2 = inner_x2
    grab_y1 = max(inner_y1, min(grab_y1, inner_y2 - (grab_y2 - grab_y1)))
    grab_y2 = grab_y1 + max(0.0, min(grab_h, inner_y2 - inner_y1))

    # Draw
    dl = imgui.get_window_draw_list()
    # Track
    track_w = track_x2 - track_x1
    track_h = track_y2 - track_y1
    dl.add_rect_filled(track_x1, track_y1, track_x2, track_y2, col_track, rounding)
    # Melty.cache.mask_mark_rect(Melty.depth, track_x1, track_y1, track_w, track_h,
    #                            key=str(Melty.unique_stack[-1]) + "scrollbar")

    dl.add_rect(track_x1, track_y1, track_x2, track_y2, col_border, rounding)
    # Grab
    if grab_y2 > grab_y1 and grab_x2 > grab_x1:
        dl.add_rect_filled(grab_x1, grab_y1, grab_x2, grab_y2, col_grab, rounding)
        dl.add_rect(grab_x1, grab_y1, grab_x2, grab_y2, col_border, rounding)

    return {
        "offset": scroll_offset,
        "track_min": (track_x1, track_y1),
        "track_max": (track_x2, track_y2),
        "grab_min": (grab_x1, grab_y1),
        "grab_max": (grab_x2, grab_y2),
        "visible": content_height > view_height
    }


bg_style_default = {
    "value": 0.01,
    "saturation": 1.2,
    "alpha": 1.0,
    'max_value': 1.0
}
def get_bg_color(depth, rounding, global_style, style_manager, auto_resize):

    depth_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles") * 0.95
    depth_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles") - 1.3
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))

    hovered_offset = 0.0

    def mix_colors(c1, c2, fac):
        return (c1[0] * (1 - fac) + c2[0] * fac,
                c1[1] * (1 - fac) + c2[1] * fac,
                c1[2] * (1 - fac) + c2[2] * fac)

    global bg_style_default
    bg_style = global_style.get_global_constant("bg_style", default=bg_style_default, folder="bg_styles")
    outline_factor = global_style.get_global_constant("outline_factor", default=1.0, folder="bg_styles") * 1.4

    if not auto_resize:
        outline_factor *= 1.3

    if auto_resize:
        bleed_factor = 0.2
    else:
        bleed_factor = 0.0
    bg_bleed = Melty.get_bg_color(-1)
    bg_bleed = style_manager.make_custom_styled(*bg_bleed, input=bg_style,
                                                value=0.6,
                                                alpha=1.0, saturation=1.8)
    bg_color = (style_manager.
                make_color_style_value(input=bg_style, value=max(0, dynamic_value) + hovered_offset))
    bg_color = mix_colors(bg_color, bg_bleed, bleed_factor)
    return bg_color

def core_header(func, outer_func, render_func, input_value=None, melty_window=False, auto_resize=True,
                collection=None, key=None, indent_size=10, depth=0, draw_state=None,
                window_stack=None, is_tree=True, is_window=False, spacing=Melty.spacing, padding=Melty.padding, show_name=True,
                on_mouse_down=False, no_measure=False, show_search=False, on_search=False,
                closable=False,
                show_header=True, show_bg=True, unique=0, name="", style_manager=None, global_style=None, parent_show_add_delete=True,
                selected_views=None, on_drag=False, on_drag_up=False, do_flow=True, melty=None, enable_flow=True, header_same_line=False,
                on_hover=False, next_kwargs=None, meta=None, on_same_line=False, y_offset=0, width=None, min_width=1, **kwargs):

        return_value = None
        changed = False

        if meta is not None:
            meta.tmp_draw_state = draw_state

        draw_list = imgui.get_window_draw_list()

        if Melty.channels_split and show_bg:
            draw_list.channels_set_current(min(Melty.max_depth - 1, Melty.get_channel()))

        if not show_bg:
            y_offset = 0
        prev_tint = None
        if hasattr(input_value, "tint") and input_value.tint is not None and show_bg:
            prev_tint = style_manager.get_tint()
            style_manager.set_imgui_tint(*input_value.tint)
        elif draw_state.tint is not None and show_bg:
            prev_tint = style_manager.get_tint()
            style_manager.set_imgui_tint(*draw_state.tint)
        elif hasattr(collection, "__tint__") and collection.__tint__ is not None and show_bg:
            if name in collection.__tint__:
                prev_tint = style_manager.get_tint()
                style_manager.set_imgui_tint(*collection.__tint__[name])

        start_x_pos = imgui.get_cursor_screen_pos()[0]
        start_y_pos = imgui.get_cursor_screen_pos()[1]
        # ----------------- top spacing -----------
        # if not show_name:
        #     enable_flow = False

        if not melty_window and enable_flow and Melty.window_enabled and melty.drag_in_progress:
            _, flow_spacing = draw_drag_drop_target(do_flow=True, enable_flow=enable_flow,
                                                    collection=collection, key=key, on_drag=False,
                                                    draw_state=draw_state, tag="top")
        # ------------------ end spacing -----------
        width = draw_state.width
        # if width is None:
        #     if draw_state.width is not None and draw_state.width > 0:
        #         width = draw_state.width
        #     else:
        #         width = min_width
        #     # draw_state.width = min_width
        # else:
        #     if min_width == 0:
        #         min_width = 1e9
        #     # draw_state.width = max(width, min_width)
        #
        # content_region = draw_state.content_region[0]
        # width = min(width, content_region)
        draw_state.content_region = imgui.get_content_region_available()
        draw_state._left_rel = imgui.get_cursor_pos()[0]

        cutoff = 50
        y_margin = y_offset / 2.0
        imgui.dummy(0, snap_int(y_margin))

        if on_drag:
            imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() - int(Melty.flow_spacing))
            Melty.flow_spacing = 0.0

        bg_tint = None
        bg_selected = False

        current_cursor = imgui.get_cursor_screen_pos()
        if show_header:
            next_kwargs['highlight'] = on_hover
            if on_drag and not melty_window:
                next_kwargs['opacity'] = 0.0
            next_kwargs.pop('spacing', None)
            next_kwargs.pop('padding', None)
            next_kwargs.pop('melty_window', False)
            next_kwargs['is_tree'] = is_tree

            next_kwargs['input_value'] = input_value
            next_kwargs['spacing'] =(spacing[0], Melty.spacing[1])
            next_kwargs['padding'] = (padding[0], Melty.padding[1])
            next_kwargs['show_search'] = show_search
            next_kwargs['on_search'] = on_search
            next_kwargs['key'] = key
            next_kwargs['collection'] = collection
            next_kwargs['closable'] = closable

            # ------------------ HEADER -----------------

            changed, return_value = draw_header(**next_kwargs)
            rect_size = imgui.get_item_rect_size()
            header_width = rect_size[0]

            # # Auto indent is decided here
            if header_same_line:
                same_line(spacing=0.0)

            else:
                space_available = width - header_width
                if (not isinstance(input_value, (dict, list, tuple, defaultdict)) and not hasattr(input_value, '__dict__')):
                    if draw_state.height is not None:

                        if draw_state.expanded_height is None:
                            draw_state.expanded_height = draw_state.height

                        height = max(draw_state.height, draw_state.expanded_height)

                        if height is None or height < 79 or on_same_line:
                            if (space_available > cutoff and draw_state.expanded) or on_same_line:
                                header_same_line = True
                                same_line(spacing=0.0)
            #
            if not header_same_line and (not on_drag or melty_window):
                # # ----------------- end header for collections ---------------
                # # This is the version with auto indent, probably a dict header
                draw_header_end(**next_kwargs)


        bg_color = (0, 0, 0, 1)
        if show_bg:
            bg_color = get_bg_color(len(Melty.bg_color_stack) * 2, rounding=4.0,
                                    global_style=global_style,
                                    style_manager=style_manager,
                                    auto_resize=auto_resize)
            Melty.bg_color_stack.append(bg_color)
            style_manager.get_tint()
            Melty.bg_stack.append(style_manager.get_tint())

        if not is_tree or draw_state.expanded:
            next_kwargs.pop('spacing', None)
            next_kwargs.pop('padding', None)

            # end_header_with = draw_state._end_header_size[0]

            current_x = imgui.get_cursor_screen_pos()[0]
            # space_used = max(0, current_x - start_x_pos)
            # space_available = width - space_used

            # padding_x = imgui.get_style().frame_padding.x
            # request_width = space_available - end_header_with - padding_x - 7
            # min_width = min(imgui.get_content_region_available()[0],
            #                 min_width - space_used)
            # min_width = max(min_width, request_width)

            parent_rect = Melty.get_clip_rect()
            imgui.set_next_item_width(120)

            ######################## MAIN FUNC CALL ########################
            current_cursor = imgui.get_cursor_screen_pos()
            header_height = current_cursor[1] - start_y_pos
            draw_state._header_height = header_height
            #
            # if not header_same_line:
            #     Melty.indent(indent_size)

            draw_list = imgui.get_window_draw_list()

            if Melty.channels_split and show_bg:
                draw_list.channels_set_current(min(Melty.max_depth - 1, Melty.get_channel()))
            clip_start = imgui.get_cursor_screen_pos()
            clipped = False
            if draw_state.width is not None and draw_state.height is not None:
                if draw_state.width > 0 and draw_state.height > 0:
                    clipped = True
                    Melty.push_clip((clip_start[0], clip_start[1],
                                     clip_start[0] + draw_state.width - 1, clip_start[1] + draw_state.height - 1))
            next_kwargs['header_height'] = header_height

            imgui.set_cursor_pos_x(imgui.get_cursor_pos_x() + indent_size)

            return_val = func(**next_kwargs)

            imgui.set_cursor_pos_x(imgui.get_cursor_pos_x() - indent_size)

            # if not header_same_line:
            #     Melty.unindent(indent_size)

            ############### END MAIN FUNC CALL #############################
            if return_val is not None and len(return_val) == 2:
                func_changed, func_return_val = return_val

                if func_changed:
                    return_value = func_return_val
                changed |= func_changed

            # if header_same_line and show_bg:
            #     # ----------------- end header single item---------------
            #     # This is the version for single items probably
            #     draw_header_end(**next_kwargs)
            if clipped:
                Melty.pop_clip()

        if not on_drag:
            imgui.dummy(0, 1)

        same_line(spacing=0)
        imgui.dummy(0, snap_int(y_margin))

        background_width = width
        background_height = draw_state.height  if draw_state.height is not None else 0
        draw_list = imgui.get_window_draw_list()


        if Melty.channels_split:
            if show_bg:
                channel = max(0, min(Melty.max_depth - 2, Melty.get_channel() - 1))
                draw_list.channels_set_current(channel)

                _, bg_color = draw_bg(bypass=True, left=start_x_pos, top=y_margin + start_y_pos + 1, bg_color=bg_color,
                        width=background_width, height=(background_height - Melty.spacing[1] / 2.0 - y_offset),
                        tint=bg_tint, depth=Melty.depth, selected=bg_selected, global_style=global_style,
                        style_manager=style_manager, auto_resize=auto_resize)
                draw_state.bg_color = bg_color

            if draw_state.height is not None:
                push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
                pop_style_var(2)


        if show_bg:
            style_manager.get_tint()
            Melty.bg_stack.pop()
            Melty.bg_color_stack.pop()
        if not on_drag or melty_window:
            next_kwargs['do_flow'] = True

        # if melty_window:
        #     imgui.set_cursor_screen_pos((initial_cursor_pos[0],
        #                                  initial_cursor_pos[1]))
        if prev_tint is not None:
            style_manager.set_imgui_tint(*prev_tint)
        if on_drag_up and not melty_window:
            melty.drag_in_progress = False
            new_action = copy(melty.drag_drop_action)
            new_action.operation = OperationType.MOVE
            new_action.source_key = key
            new_action.source_unique = unique
            new_action.source_collection = collection
            new_action.source_draw_state = draw_state

            return False, new_action

        return changed, return_value

def seperator(height):
    imgui.dummy(0, snap_int(height / 2))
    imgui.separator()
    imgui.dummy(0, snap_int(height / 2))



@with_header(is_default_for=(MutableMapping, defaultdict), use_cache=False, enable_scroll=True)
def draw_collection(input_value, draw_state, depth, style_manager,
                    meta, suffix, melty, show_search=True, on_collapse=False, on_drag_up=False, y_offset=0,
                    on_expand=False, width=None, indent_size=10, global_style=None, global_toggles=None, show_add_delete=True,
                    show_instance_vars=True, unique=0, horizontal=False, show_indices=False, **kwargs):
    """
    Universal collection renderer
    """
    if isinstance(input_value, defaultdict):
        pass
    changed = False
    # ----- SIMPLE NORMALIZER (lowercase; remove spaces, '_' and '-') -----
    _TRANS = str.maketrans("", "", " _-")
    def norm_string(s) -> str:
        if s is None:
            return ""
        try:
            s = str(s)
        except Exception:
            s = ""
        return s.lower().translate(_TRANS)

    if hasattr(input_value, 'children') and isinstance(input_value.children, (list, dict, defaultdict, deque)):
        input_value = input_value.children

    search_token = norm_string(draw_state.search_text) if show_search else ""

    # --- configure per collection type ---
    ordered_driver = input_value
    if isinstance(input_value, (dict, list, tuple, set, defaultdict, MutableMapping, deque)):
        use_tint = True
        use_child_meta = True
        apply_change = True
        parent_type = input_value.__class__
        if isinstance(input_value, (dict, defaultdict, MutableMapping)):
            keys = input_value.keys()
            collection = input_value
        else:
            keys = range(len(input_value))
            collection = list(input_value)

    elif hasattr(input_value, "__dict__") and depth < Melty.max_depth:
        if hasattr(type(input_value), "__field_defaults__") and hasattr(input_value, 'to_dict'):
            type(input_value).__field_defaults__.update(input_value.__dict__)
            keys = type(input_value).__field_defaults__.keys()
        else:
            keys = input_value.__dict__.keys()
        collection = input_value.__dict__
        use_tint = False
        use_child_meta = True
        apply_change = True
        parent_type = input_value.__class__
    else:

        return False, input_value

    # --- unified loop ---
    drew_any = False
    collection_spacing = 0
    all_meta = []
    content_height = 0.0

    current_cursor = imgui.get_cursor_screen_pos()
    # imgui.set_cursor_screen_pos((current_cursor[0], current_cursor[1] - draw_state.scroll_offset[1]))

    start_cursor = imgui.get_cursor_pos()[1]
    keys = list(keys)[:]
    children_draw_states = []
    rect = Melty.get_clip_rect()

    needs_content_height = ((draw_state.content_height < draw_state.height if draw_state.height is not None else True) or
                            draw_state.invalid_content_height)

    # draw_list = imgui.get_window_draw_list()
    # if rect is not None:
    #     draw_list.add_rect(rect[0] + 1, rect[1] + 1, rect[2] - 1, rect[3] - 1,
    #                        imgui.get_color_u32_rgba(1,1,1, 1.0),
    #                        rounding=2.0, thickness=2.0)
    premature_break = False
    for idx, key in enumerate(keys):
        if isinstance(collection, dict) and key not in collection:
            continue

        if hasattr(input_value, "__dict__") and hasattr(input_value, key):
            item = getattr(input_value, key, None)
        else:
            item = collection[key]

        if callable(item):
            pass

        # Snap cursor to nearest pixel
        cursor_pos = imgui.get_cursor_screen_pos()
        imgui.set_cursor_screen_pos((snap_int(cursor_pos[0]), snap_int(cursor_pos[1])))

        # visual separator (object extras)
        if key is None and item is None:
            seperator(Melty.spacing[1])
            continue

        if hasattr(type(input_value), "__excluded_attrs__"):
            if not global_toggles.force_show_excluded:
                if str(key) in type(input_value).__excluded_attrs__:
                    continue
        display_name = None

        # apply global skip to all types
        if isinstance(key, (float, Enum, NoneType)):
            key_str = f"{input_value.__class__.__name__}"
        elif isinstance(key, int):
            key_str = f"{key}"
        else:
            key_str = str(key)


        if ((key_str.startswith("__") and key_str.endswith("__")) or
                key_str.endswith("meta") or key_str.startswith("_")):
            continue

        # ----- SEARCH CHECK (keys + item.name if present) -----
        if search_token:
            name_field = getattr(item, "name", None) or getattr(item, "__name__", "")
            if ((search_token not in norm_string(key_str)) and
                    (search_token not in norm_string(name_field))):
                continue

        # meta selection
        if use_child_meta and hasattr(Meta, 'get_child_meta'):
            item_meta = Meta.get_child_meta(parent_type, field_name=key, value=item)
        else:
            item_meta = meta

        item_meta.collection_type = meta.field_type

        # view function & suffix
        view_fn = getattr(item_meta, "view_function", draw_collection)
        if view_fn is None:
            view_fn = draw_any

        id_val = getattr(input_value, "unique_id", "")

        trigger_collapse = False
        if isinstance(input_value, (dict, defaultdict, MutableMapping)) and on_collapse:
            trigger_collapse = True

        trigger_expand = False
        if isinstance(input_value, (dict, defaultdict, MutableMapping)) and on_expand:
            trigger_expand = True

        prev_tint = None
        try:
            if isinstance(collection, FolderProxy):
                codec = FILE_CODECS.for_name(key)
                if codec is not None and hasattr(codec, 'tint'):
                    prev_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*codec.tint)

            if hasattr(input_value, "__tint__") and getattr(input_value, "__tint__"):
                if key in input_value.__tint__:
                    prev_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*input_value.__tint__[key])

            y_offset = Melty.collection_spacing
            all_meta.append(item_meta)
            if show_indices or isinstance(collection, (list, tuple, set, deque)):
                display_name = f"{str(idx)}"

            if horizontal:
                if item_meta is not None and hasattr(item_meta, 'tmp_draw_state'):
                    imgui.same_line()
                    if item_meta.tmp_draw_state.width is not None:
                        space_left = imgui.get_content_region_available()[0] - item_meta.tmp_draw_state.width
                        if space_left < 0:
                            imgui.new_line()

            item_changed, out_val, extras = draw_any(item, return_extras=True, indent_size=10, key=key,
                                                     meta=item_meta, trigger_collapse=trigger_collapse,
                             trigger_expand=trigger_expand, y_offset=y_offset, on_collapse=on_collapse, on_expand=on_expand,
                             collection=ordered_driver, name=key_str, display_name=display_name,
                                             parent_show_add_delete=show_add_delete,
                                             show_add_delete=show_add_delete)

            if 'draw_state' in extras:
                children_draw_states.append(extras['draw_state'])
                extras['draw_state']._parent = draw_state
                if rect is not None:
                    if (cursor_pos[1] > Melty.get_clip_rect()[3] and not needs_content_height and Melty.frame_count > 2):
                        premature_break = True
                        break


            if isinstance(out_val, CollectionAction):
                # perform the move; this should mutate the plain dicts you attached
                result = Melty.to_apply(out_val)
                item_changed, out_val = False, None

            if item_changed and apply_change and key is not None:
                if isinstance(input_value, (dict, defaultdict, MutableMapping)):
                    input_value[key] = out_val
                elif isinstance(input_value, list):
                    input_value[key] = out_val
                elif isinstance(input_value, deque):
                    input_value[key] = out_val
                elif isinstance(input_value, tuple):
                    temp = list(input_value)
                    temp[key] = out_val
                    input_value = parent_type(temp)
                else:
                    setattr(input_value, key_str, out_val)

            changed |= item_changed
            drew_any = True

        except Exception as e:
            print(f"Error rendering field '{key_str}' of {type(input_value).__name__}: {e}")
            print_colored_traceback(*sys.exc_info())

        finally:
            if prev_tint is not None:
                style_manager.set_imgui_tint(*prev_tint)

    end_pos = imgui.get_cursor_pos()[1]
    content_height = (end_pos - start_cursor) + draw_state.header_height

    draw_state._children = children_draw_states

    # imgui.set_cursor_screen_pos((current_cursor[0], current_cursor[1] + draw_state.scroll_offset[1]))
    # current_cursor = imgui.get_cursor_screen_pos()

    if not melty.drag_in_progress and not premature_break:
        draw_state.content_height = snap_int(content_height)
        draw_state.invalid_content_height = False

    # ----------------- top spacing -----------
    last_key = list(keys)[-1] if len(keys) > 0 else None

    if not drew_any:
        last_key = None

    last_meta = all_meta[-1] if len(all_meta) > 0 else None
    if hasattr(last_meta, 'tmp_draw_state'):
        last_draw_state = last_meta.tmp_draw_state if last_meta is not None else draw_state

        if Melty.window_enabled and melty.drag_in_progress:
            # last_item = collection[last_key] if (isinstance(collection, dict) and last_key in collection) else None
            _, flow_spacing = draw_drag_drop_target(do_flow=True, enable_flow=True, melty=melty, offset=0,
                                                    collection=ordered_driver, key=last_key, on_drag=False,
                                                    draw_state=last_draw_state, tag="bottom")
    # ------------------ end spacing -----------

    # if drew_any and len(keys) == 1:
    #     imgui.dummy(0, 2)

    return changed, input_value


def draw_bg(left=0, top=0, width=20, height=20, depth=0,
            global_style=None, outline=True, bg_color=None,
            style_manager=None, tint=None, outline_tint=None, selected=False,
            hovered=False, auto_resize=False, **kwargs):
    # Render background
    def current_indent_px():
        return Melty.current_indent

    depth = len(Melty.bg_stack) * 2
    rounding = 5.0

    right =  left + width
    bottom =  top + height

    thickness = 1.0
    half_thickness = 0.5
    rect = (snap_int(left) + thickness, snap_int(top) + thickness, snap_int(right) - thickness, snap_int(bottom) - thickness)
    rect_outline = (snap_int(left) + half_thickness, snap_int(top) + half_thickness,
                    snap_int(right) - half_thickness, snap_int(bottom) - half_thickness)
    # Outline

    rounding = min(max(10.0, current_indent_px()), rounding)

    depth_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles") * 0.95
    depth_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles") - 1.3
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))
    bg_style = {
        "value": 0.01,
        "saturation": 1.2,
        "alpha": 1.0,
        'max_value': 1.0
    }
    hovered_offset = 0.0
    if selected:
        hovered_offset = 0.1
    elif hovered:
        hovered_offset = 0.3

    def mix_colors(c1, c2, fac):
        return (c1[0] * (1 - fac) + c2[0] * fac,
                c1[1] * (1 - fac) + c2[1] * fac,
                c1[2] * (1 - fac) + c2[2] * fac)

    bg_style = global_style.get_global_constant("bg_style", default=bg_style, folder="bg_styles")
    outline_saturation = global_style.get_global_constant("outline_saturation", default=0.5, folder="bg_styles")
    outline_offset = global_style.get_global_constant("outline_offset", default=0.0, folder="bg_styles") - 0.05
    outline_factor = global_style.get_global_constant("outline_factor", default=1.0, folder="bg_styles") * 1.4

    if not auto_resize:
        outline_factor *= 1.3
        outline_saturation = 0.9


    if auto_resize:
        bleed_factor = 0.2
    else:
        bleed_factor = 0.0
    bg_bleed = Melty.get_bg_color(-1)
    bg_bleed = style_manager.make_custom_styled(*bg_bleed, input=bg_style,
                                                value=0.6,
                                                alpha=1.0, saturation=1.8)

    outline_color = (style_manager.
                     make_color_style_value(input=bg_style, saturation=outline_saturation,
                                                  value=max(0, dynamic_value * outline_factor + outline_offset)))
    outline_color = mix_colors(outline_color, bg_bleed, 0.01)
    # if tint is not None:
    #     outline_color = imgui.get_color_u32_rgba(*tint)

    if outline:
        outline_color = imgui.get_color_u32_rgba(*outline_color, 1.0)

        if outline_tint is not None:
            outline_color = imgui.get_color_u32_rgba(*outline_tint)
        imgui.get_window_draw_list().add_rect(*rect_outline, col=outline_color, rounding=rounding, thickness=2.0)

    if bg_color is None:
        bg_color = (style_manager.
                    make_color_style_value(input=bg_style, value=max(0, dynamic_value) + hovered_offset))

        bg_color = mix_colors(bg_color, bg_bleed, bleed_factor)

    imgui_bg_color = imgui.get_color_u32_rgba(bg_color[0], bg_color[1], bg_color[2], 1.0)

    if tint is not None:
        imgui_bg_color = imgui.get_color_u32_rgba(*tint)

    imgui.get_window_draw_list().add_rect_filled(*rect, col=imgui_bg_color, rounding=rounding)

    return False, bg_color

def open_file(path, app=None):
    def default_file_manager():
        # Detect platform
        if sys.platform.startswith('darwin'):
            return "open"
        elif os.name == 'nt':
            return "explorer"
        elif os.name == 'posix':
            return "nemo"

    if app is None:
        app = default_file_manager()

    import subprocess
    if os.path.exists(path):
        codec = FILE_CODECS.for_name(path)
        if codec is not None and codec.default_app is not None:
            if hasattr(codec, 'default_app'):
                app = codec.default_app
                # Check if app exists
                if not shutil.which(app):
                    print(f"App not found: {app}, falling back to default.")
                    app = "nemo"

        subprocess.Popen([app, path])
    else:
        print(f"Path does not exist: {path}")

@render_func()
def button(input_value="", color=None, width=None, height=None, style_manager=None, value=0.5, saturation=0.8, unique=0):
    if height is None:
        height = imgui.get_frame_height()

    if width is None:
        width = imgui.calc_text_size(input_value)[0] + imgui.get_style().frame_padding.x * 2
        width = max(height, width)

    if color is not None:
        mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2],
                                                value=0.35, factor=0.9, saturation_scale=saturation, alpha=1.0)
        text_color = style_manager.make_color_rgb(color[0], color[1], color[2],
                                                 value=1.0, factor=0.5, saturation_scale=0.8, alpha=1.0)
        rounding = 4.0

        imgui.push_style_var(imgui.STYLE_FRAME_ROUNDING, rounding)
        alpha = color[3] if len(color) > 3 else 1.0

        imgui.push_style_color(imgui.COLOR_BUTTON, *mixed_color[:3], alpha)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, *(min(mixed_color[0]+0.1,1.0),
                                                            min(mixed_color[1]+0.1,1.0),
                                                            min(mixed_color[2]+0.1,1.0),
                                                            max(0.3, alpha)))
        imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE, *(max(mixed_color[0]-0.1,0.0),
                                                              max(mixed_color[1]-0.1,0.0),
                                                              max(mixed_color[2]-0.1,0.0),
                                                            alpha))
        imgui.push_style_color(imgui.COLOR_TEXT, *text_color[:3], 1.0)

    clicked = imgui.button(input_value + f"##{unique}", width, height)
    if color is not None:
        imgui.pop_style_color(4)
        imgui.pop_style_var(1)

    return clicked, input_value


def draw_header(input_value=None, name="", suffix="", collection=None, display_name=None, meta=None, unique=None, is_tree=True,
                show_name=True, name_func=None, show_type=False, show_unique=False,
                on_search=False, trigger_collapse=False, trigger_expand=False,
                draw_state=None, show_tint=True, opacity=1.0, show_add_delete=True,
                on_drag=False, on_action=None, style_manager=None,
                global_style=None, global_toggles=None, **kwargs):

    if display_name is not None:
        name = display_name

    on_change = False
    return_val = on_action
    push_style_var(imgui.STYLE_ALPHA, opacity)
    value_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles")
    value_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles")

    depth = len(Melty.bg_stack)
    depth_factor = global_style.get_global_constant("depth_factor", default=1.0, folder="bg_styles")
    depth_offset = global_style.get_global_constant("depth_offset", default=0.0, folder="bg_styles") + 0.2
    dynamic_value = max(0, (float(depth + depth_offset) * depth_factor))
    bg_style = global_style.get_global_constant("bg_style", default=None, folder="bg_styles")
    saturation = -0.5

    saturation = bg_style['saturation'] + saturation
    name_color = (style_manager.
                  make_color_style_value(input=bg_style, saturation=saturation,
                                         value=max(0, dynamic_value * value_factor + value_offset)))
    hover_color = (style_manager.
                  make_color_style_value(input=bg_style, saturation=1.2, alpha=1.0,
                                         value=0.67))

    outline_color = (style_manager.
                   make_color_style_value(input=bg_style, saturation=0.8, alpha=1.0,
                                          value=0.9))
    if is_tree:
        # if trigger_collapse:
        #     draw_state.expanded = False
        # if trigger_expand:
        #     draw_state.expanded = True

        push_style_color(imgui.COLOR_TEXT, *outline_color)
        push_style_var(imgui.STYLE_FRAME_PADDING, (2,4))
        push_style_var(imgui.STYLE_ITEM_SPACING, (0,3))

        # no background
        imgui.push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, *(0.0, 0.0, 0.0, 0.0))
        imgui.set_item_allow_overlap()

        if imgui.arrow_button(f"##tree{unique}", imgui.DIRECTION_DOWN if draw_state.expanded else imgui.DIRECTION_RIGHT):
            draw_state.expanded = not draw_state.expanded
            request_render()
        imgui.set_item_allow_overlap()
        imgui.pop_style_color(2)

        # draw_state.expanded = tree(f"{down_icon}##tree", draw_state.expanded, width=50)

        pop_style_var(2)
        pop_style_color(1)
        same_line()

    if show_name and name != "" and name is not None and name != "None":
        if isinstance(input_value, (dict, MutableMapping)):
            folder_icon = "\uf07b"
            # imgui.text_colored(folder_icon, *name_color)

            push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
            push_style_color(imgui.COLOR_TEXT, *name_color)
            if imgui.button(f"{folder_icon}##open_folder"):
                if hasattr(input_value, "file_path"):
                    open_file(input_value.file_path)

            pop_style_color(2)
            same_line(spacing=0.0)
        elif isinstance(collection, (FolderProxy)):
            file_icon = "\uf15b"
            # imgui.text_colored(folder_icon, *name_color)

            push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
            push_style_color(imgui.COLOR_TEXT, name_color[0], name_color[1], name_color[2], 0.5)
            if imgui.button(f"{file_icon}##open_file"):
                if hasattr(collection, "file_path"):
                    folder_path = collection.file_path
                    file_path = os.path.join(folder_path, str(name))
                    open_file(file_path)

            pop_style_color(2)
            same_line(spacing=0.0)

        imgui.align_text_to_frame_padding()
        padding = imgui.get_style().frame_padding.x
        text_width = imgui.calc_text_size(name)[0]
        push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
        push_style_color(imgui.COLOR_BUTTON, *(0.0, 0.0, 0.0, 0.0))
        push_style_color(imgui.COLOR_TEXT, *name_color)

        if Melty.is_window_enabled():
            push_style_color(imgui.COLOR_BUTTON_HOVERED, *hover_color)
        else:
            push_style_color(imgui.COLOR_BUTTON_HOVERED, *(0.0, 0.0, 0.0, 0.0))

        push_style_color(imgui.COLOR_BUTTON_ACTIVE, *hover_color)
        push_style_var(imgui.STYLE_FRAME_ROUNDING, 2.0)

        if on_drag:
            push_style_color(imgui.COLOR_BUTTON, *hover_color)
            push_style_var(imgui.STYLE_FRAME_BORDERSIZE, 2.0)
            push_style_color(imgui.COLOR_BORDER, *outline_color)

        min_name_width = 30.0
        name_width = max(text_width + padding * 2, min_name_width)
        if not draw_state._name_edit:
            imgui.button(f"{name}", width=name_width)
            rect_min = imgui.get_item_rect_min()
            if imgui.is_mouse_double_clicked(0) and imgui.is_item_hovered():
                draw_state._name_edit = True
                # imgui.set_keyboard_focus_here(0)

            pop_style_color(4)
            pop_style_var(2)

            if imgui.is_item_hovered():
                button_rect = (rect_min[0], rect_min[1], rect_min[0] + name_width,
                               rect_min[1] + imgui.get_item_rect_size()[ 1])
                #
                # Melty.cache.mask_mark_rect(Melty.depth + 2, button_rect[0], button_rect[1],
                #                             button_rect[2], button_rect[3],
                #                            key=str(Melty.unique_stack[-1]) + "scrollbar")
        else:
            imgui.set_next_item_width(name_width)
            # Selected text on focus
            flags = imgui.INPUT_TEXT_ENTER_RETURNS_TRUE | imgui.INPUT_TEXT_AUTO_SELECT_ALL
            changed, new_name = imgui.input_text(f"##edit{name}_{unique}", name,
                                                 flags=flags)
            pop_style_color(4)
            pop_style_var(2)
            if changed:
                draw_state._name_edit = False

            if imgui.is_key_pressed(imgui.KEY_ESCAPE):
                draw_state._name_edit = False

            if not imgui.is_item_active():
                draw_state._name_edit = False
        if on_drag:
            pop_style_color(2)
            pop_style_var(1)

        same_line()

    if show_type:
        imgui.text_colored(f"({input_value.__class__.__name__})", *(0.8, 0.0, 0.5, 1.0))
        same_line()

    if show_unique:
        imgui.text_colored(f"({str(Melty.get_tile_id())})", *(0.4, 0.0, 0.9, 1.0))
        same_line()
    if show_name and name != "":
        same_line()
        imgui.set_item_allow_overlap()

    if show_tint and hasattr(input_value, "tint") and input_value.tint is not None:
        draw_state._has_popup = True
        tint_changed, tint_value = draw_tuple(input_value.tint, show_header=False)
        if tint_changed:
            input_value.tint = tint_value
            # Melty.cache.invalidate_by_obj(input_value, name)
        same_line()

    if show_add_delete and isinstance(input_value, (list, dict)) or hasattr(input_value, "__dict__"):
        bg_style = global_style.get_global_constant("bg_style", default=None, folder="bg_styles")
        if show_add_delete:
            if imgui.button(f"\uf067##add{unique}", width=20):
                # Use str as default hinted type
                hinted_type = NoneType
                if meta.field_type is not None and hasattr(meta.field_type, "__args__"):
                    if len(meta.field_type.__args__) == 2:
                        hinted_type = meta.field_type.__args__[1]
                add_to_collection(input_value, hinted_type())
                on_change = True
                return_val = input_value
            same_line(spacing=0.0)

    same_line(spacing=0.0)

    do_profile = global_toggles.profiler == ProfileMode.ON
    if do_profile:
        profile_time = draw_state.render_time
        render_profiler_time(input_value=profile_time, brief=True,
                             style_manager=style_manager, global_style=global_style)
    pop_style_var(1)

    return on_change, return_val

def render_profiler_time(input_value=None, brief=False, style_manager=None,
                         global_style=None):
    """
    Renders the time taken for a specific operation in the profiler.
    """
    in_ms = input_value * 1000.0
    if brief:
        if in_ms >= 0.99:
            formatted_value = f"{(in_ms):.1f}ms"
        else:
            formatted_value = f"{(in_ms):.2f}ms"
        if formatted_value.startswith("0."):
            formatted_value = formatted_value[1:]
    else:
        formatted_value = f"{in_ms:.2f} ms"
    golden_yellow = (2.0, 0.5, 0)
    dynamic_saturation_factor = global_style.profiler["object_attr"][
        "dynamic_saturation_factor"]
    dynamic_saturation_offset = global_style.profiler["object_attr"][
        "dynamic_saturation_offset"]
    saturation = global_style.profiler["object_attr"]["saturation"]
    value = global_style.profiler["object_attr"]["value"]
    dynamic_sat = (float(in_ms + dynamic_saturation_offset) * dynamic_saturation_factor)
    text_tint = style_manager.make_color_rgb(*golden_yellow, factor=1.0 - dynamic_sat,
                                                       value=min(1.0, max(0, value + dynamic_sat * 0.5)),
                                                       alpha=1.0,
                                                       saturation_scale=max(0, saturation - dynamic_sat))[:3]
    imgui.text_colored(f"{formatted_value}", *text_tint)
    return False, input_value


#
# @render_func(use_cache=False)
# @listens_for(Hotkey(glfw.KEY_O, "on_search", KeyMod.CTRL))
# @listens_for(Hotkey(glfw.KEY_MINUS, "on_collapse", KeyMod.CTRL, invert=False))
# @listens_for(Hotkey(glfw.KEY_EQUAL, "on_expand", KeyMod.CTRL, repeat=False))
# def draw_object(input_value=None, draw_state=None, meta=None, name="", style_manager=None,
#                 depth=0, unique=0, suffix="", collection=None, key=None, *args, **kwargs):
#     # if is_tree and not draw_state.expanded:
#     #     return False, None
#     is_collection = isinstance(input_value, (dict, list, tuple, set, defaultdict, MutableMapping)) or (
#             hasattr(input_value, "__dict__") and depth < Melty.max_depth)
#     if is_collection:
#
#         # render collections
#         changed, new_value = draw_collection(input_value=input_value, collection=collection,
#                                              name=name, key=key, **kwargs)
#     else:
#         return_value = None
#         push_id(unique)
#         try:
#             imgui.text(f"Render object {name}")
#         except Exception as e:
#             print_colored_traceback(e)
#         finally:
#             pop_id()
#             if return_value is None:
#                 changed, new_value = False, None
#             elif isinstance(return_value, tuple) and len(return_value) == 2:
#                 changed, new_value = return_value
#             else:
#                 imgui.text("Unsupported return of render_func")
#                 changed, new_value = False, None
#     return changed, new_value


@render_func(use_cache=True)
def draw_any(input_value, indent_size=0, **kwargs):

    # # meta handlin
    meta = kwargs.get("meta", None)
    if meta is None:
        if hasattr(Meta, 'get_child_meta'):
            meta = Meta.get_child_meta(None, field_name=kwargs.get("name", ''), value=input_value)

    if meta.view_function is None or 'draw_any' in meta.view_function.__name__ :
        meta.view_function = draw_collection

    if kwargs['global_toggles'].force_show_view_fn:
        imgui.text_colored(f"[{meta.view_function.__name__}]", 0.8, 0.5, 0.9)
    if kwargs['global_toggles'].force_show_unique and hasattr(meta, 'unique'):
        imgui.text_colored(f"[{Melty.get_tile_id()}]", 0.8, 0.5, 0.9)

    return_val = meta.view_function(input_value, **kwargs)

    return return_val


@with_header_minimal(header_same_line=True, is_default_for=(NoneType))
def draw_none(input_value: NoneType):
    imgui.align_text_to_frame_padding()
    imgui.text("None")
    return False, input_value


@with_header_minimal(is_default_for=(bool), header_same_line=True, use_cache=False)
def draw_bool(input_value: bool):
    changed, is_checked = imgui.checkbox("##bool", input_value)
    if changed:
        return True, is_checked

    return False, None


@with_header_minimal(is_default_for=(str))
def draw_str(input_value: str):
    line_count = input_value.count('\n') + 1
    line_height = imgui.get_text_line_height_with_spacing()
    changed, value = False, input_value

    if line_count == 1:
        padding = imgui.get_style().frame_padding.y
        height = imgui.get_text_line_height() + padding
    else:
        height = (max(0, min(200, line_count * line_height + 6)))

    # multi line doesn't work with clipping, patch to fix
    clip_rect = Melty.get_clip_rect()
    space_left_bottom = float('inf')
    space_from_top = float('inf')
    if clip_rect is not None:
        cursor_y = imgui.get_cursor_screen_pos()[1]
        space_left_bottom = clip_rect[3] - cursor_y
        space_from_top = cursor_y - clip_rect[1]

    left_edge = Melty.get_clip_rect()[0] if Melty.get_clip_rect() is not None else 0
    start_x = imgui.get_cursor_screen_pos()[0]
    start_offset = start_x - left_edge

    show_controls = True
    width = min(Melty.get_space_left(), 400 - start_offset)

    if not show_controls:
        imgui.push_style_var(imgui.STYLE_ALPHA, 0)

    if line_count == 1:
        # imgui.set_next_item_width(width)
        changed, value = imgui.input_text("##str", input_value,
                                          flags=imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
    else:
        changed, value = imgui.input_text_multiline("##str", input_value, width=width, height=height)

    if not show_controls:
        imgui.pop_style_var(1)

    if changed:
        return True, value

    return changed, value


@with_header_minimal(is_default_for=('tint'), has_popup=True, use_cache=False)
def draw_tuple(input_value: tuple, unique):
    if len(input_value) > 0 and isinstance(input_value[0], (float, int)):
        if len(input_value) == 4:
            color_list = list(input_value)
            color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL | imgui.COLOR_EDIT_FLOAT |
                           imgui.COLOR_EDIT_NO_TOOLTIP)
            changed, color = imgui.color_edit4(
                f"##picker_edit{unique}",
                color_list[0], color_list[1], color_list[2], color_list[3],
                flags=color_flags)
            if changed:
                input_value = (color[0], color[1], color[2], color[3])
        elif len(input_value) == 3:
            color_list = list(input_value)
            color_flags = (imgui.COLOR_EDIT_NO_INPUTS | imgui.COLOR_EDIT_NO_LABEL |
                           imgui.COLOR_EDIT_NO_ALPHA | imgui.COLOR_EDIT_FLOAT |
                           imgui.COLOR_EDIT_NO_TOOLTIP)
            changed, color = imgui.color_edit3(
                f"##picker_edit{unique}",
                color_list[0], color_list[1], color_list[2],
                flags=color_flags)
            if changed:
                input_value = (color[0], color[1], color[2])
        else:
            str_value = ", ".join([str(v) for v in input_value])
            changed, input_str = imgui.input_text("##tuple", str_value)
            if changed:
                try:
                    new_tuple = eval(f"({input_str},)")
                    if isinstance(new_tuple, tuple):
                        input_value = new_tuple
                except Exception as e:
                    print(f"Error parsing tuple: {e}")
                    pass
    else:
        changed, input_value = draw_collection(input_value=input_value)

    return changed, input_value

class TestClass(DictConversion):
    def __init__(self):
        super().__init__()
        self.value = 2
        self.str_val = "Test"

@with_header_minimal(is_default_for=float, use_cache=False)
def draw_float(input_value:float, min_value=-100.0, max_value=100.0, speed=0.01, unique=0, draw_state=None):

    changed, value = imgui.drag_float("##float", input_value,
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)

    if changed:
        return True, value


@with_header_minimal(is_default_for=(Parameter), wraps=render_func)
def draw_parameter(input_value):

    parameter_default = input_value.default
    if parameter_default is inspect.Parameter.empty:
        imgui.same_line()
        imgui.text("<No Default>")
    else:
        return draw_any(parameter_default, show_name=False, show_add_delete=False)


@with_header(is_default_for=(types.MappingProxyType), show_add_delete=False, wraps=render_func)
def draw_mapping_proxy(input_value):
    # To list first, then back to mapping proxy
    dict_values = dict(input_value)
    changed, new_dict = draw_collection(dict_values, show_bg=False, indent_size=0, show_header=False, show_add_delete=False)
    if changed:
        return True, types.MappingProxyType(new_dict)

    return changed, input_value


@with_header_minimal(wraps=render_func, show_add_delete=False)
def eval_function(input_value, draw_state):
    signature = inspect.signature(input_value)
    params = signature.parameters
    changed, new_val = draw_any(params, name="Parameters", show_add_delete=False)
    if changed:
        set_fn_defaults(input_value, new_val)

    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 4))
    push_style_var(imgui.STYLE_FRAME_PADDING, (8, 6))
    push_style_var(imgui.STYLE_FRAME_ROUNDING, 6)

    function_args = inspect.signature(input_value).parameters
    kwargs = {}
    for name, param in function_args.items():
        if param.default is not inspect.Parameter.empty:
            kwargs[name] = param.default
        else:
            kwargs[name] = None
    try:
        result = input_value(**kwargs)
        draw_any(result, name="Result", show_header=True, show_add_delete=False)
        if draw_state._result != result:
            Melty.cache.invalidate_all()
        draw_state._result = result

    except Exception as e:
        print(f"Error calling function '{input_value.__name__}': {e}")
        print_colored_traceback(*sys.exc_info())

    pop_style_var(3)

    return changed, input_value

@with_header(is_default_for=(types.FunctionType, types.MethodType),
                     wraps=render_func, show_add_delete=False, is_tree=False, show_name=False)
def draw_function(input_value, name, draw_state, unique):
    signature = inspect.signature(input_value)
    params = signature.parameters
    if len(draw_state.params) != len(params):
        param_dict = {}
        for name, param in params.items():
            if param.default is not inspect.Parameter.empty:
                param_dict[name] = param.default
            else:
                param_type = param.annotation
                default_value = param.default
                if default_value is not inspect.Parameter.empty:
                    param_dict[name] = default_value
                else:
                    param_dict[name] = 0

        draw_state.params = param_dict
    if len(draw_state.params) > 0:
        changed, new_val = draw_any(draw_state.params, name="Parameters", show_add_delete=False)
        if changed:
            draw_state.params = new_val

    push_style_var(imgui.STYLE_ITEM_SPACING, (4, 0))
    push_style_var(imgui.STYLE_FRAME_PADDING, (6, 6))
    push_style_var(imgui.STYLE_FRAME_ROUNDING, 4)

    if imgui.button(f"{input_value.__name__}##{unique}"):
        try:
            draw_state.result = input_value(**draw_state.params)
            Melty.cache.invalidate_up_current(force=True)
        except Exception as e:
            print(f"Error calling function '{input_value.__name__}': {e}")
            print_colored_traceback(*sys.exc_info())

    if draw_state.result is not None:
        draw_any(draw_state.result, name="Result", header_same_line=True, show_header=False, show_add_delete=False)

    pop_style_var(3)

    return False, input_value

@with_header_minimal(is_default_for=(int), header_same_line=True, wraps=render_func)
def draw_int(input_value: int, min_value=-100.0, max_value=100.0, speed=0.05, unique=0):
    int_text_width = imgui.calc_text_size(str(input_value))[0]

    imgui.set_next_item_width(int_text_width + 20)

    # draw_window("hello", name=f"{unique}testset", closable=False)
    max_int = 2147483647
    if input_value < max_int:
        changed, value = imgui.drag_int("##int", input_value,
                                          change_speed=speed,
                                          min_value=min_value,
                                          max_value=max_value)
        if changed:
            return True, value

        return changed, value
    return False, input_value

@with_header(show_header=False, show_name=False, show_bg=True)
def draw_debug_label(input_value:str):
    imgui.text(input_value)

@with_header_minimal(is_default_for=Enum, show_add_delete=False, header_same_line=True, wraps=render_func)
def draw_enum(input_value:Enum, global_style=None, style_manager=None, enum_tint=(0.3, 0.3, 0.3)):

    unique = "enum"
    imgui.set_next_item_width(imgui.get_content_region_available().x)
    selected_idx = next(enumerate(input_value.__class__))[1]
    changed = False

    push_style_var(imgui.STYLE_ITEM_SPACING, (2, 4))

    for i, option in enumerate(input_value.__class__):
        a_pretty_name = option.name.replace("_", " ").capitalize()

        label = f"{a_pretty_name}##{unique}{i}"
        active = (input_value == option)
        radio_style = global_style.radio_button
        if active:
            color = style_manager.make_color_style_rgb(*enum_tint, radio_style["active_base"])
            hover = style_manager.make_color_style_rgb(*enum_tint, radio_style["active_hover"])
            pressed = style_manager.make_color_style_rgb(*enum_tint, radio_style["active_pressed"])
        else:
            color = style_manager.make_color_style_rgb(*enum_tint, radio_style["inactive_base"])
            hover = style_manager.make_color_style_rgb(*enum_tint, radio_style["inactive_hover"])
            pressed = style_manager.make_color_style_rgb(*enum_tint, radio_style["inactive_pressed"])

        push_style_color(imgui.COLOR_BUTTON, *color)
        push_style_color(imgui.COLOR_BUTTON_HOVERED, *hover)
        push_style_color(imgui.COLOR_BUTTON_ACTIVE, *pressed)

        clicked = imgui.button(label)

        pop_style_color(1)
        pop_style_color(1)
        pop_style_color(1)

        if clicked:
            selected_idx = option
            changed = True

        same_line()
    new_line()

    enum_class = input_value.__class__
    if changed:
        selected_enum = enum_class(selected_idx)
    else:
        selected_enum = input_value
    pop_style_var(1)

    return changed, selected_enum