"""draw_texture: melty's zoom/pan texture view, in its own module.

It lives apart from new_core_view so that a host wanting only this view (the
hdr-viewer) can import it without new_core_view's converter registry, which
pulls libcst (~80 ms) in behind it. new_core_view re-exports it at the point
where it used to be defined, so registration order and
``from new_core_view import draw_texture`` are unchanged.
"""
import math

import OpenGL.GL as gl
import glfw
import imgui
import numpy
from imgui.core import _DrawList

from src.lsd.gl_gui.hdr_color import pack_color
from src.lsd.gl_gui.model.core_model.draw_state import ZoomState
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.headers import draw_header


@render_func(is_default_for=numpy.uint32, show_bg=False, use_cache=False, show_add_delete=False, z_offset=0,
             fill_height=True, selectable=False,
             indent_size=0, min_width=35, min_height=35, wrap=False, disable_scroll=True,
             zoom_speed=0.3, with_header=draw_header, manual_content_height=True)
def draw_texture(input_value: numpy.uint32, hovered, scroll_y_changed, middle_mouse_drag, double_right_mouse_drag,
                 zoom_state: ZoomState, zoom_speed, header_height=0, min_zoom=0.1,
                 max_zoom=50.0, style_manager=None, max_brightness=5.0, max_contrast=5.0,
                 draw_state=None, jet=False, nearest=False, dim_outside=None, dim_alpha=0.55, show_info=True, flip_y=False, **kwargs):
    original_id = input_value
    texture_id = input_value
    imgui.dummy(draw_state.width, draw_state.height - 20)

    # Ensure we have valid state if this is the first run
    if not hasattr(zoom_state, 'zoom'):
        zoom_state.zoom = 1.0
        zoom_state.center_u = 0.5
        zoom_state.center_v = 0.5

    # Check if opengl texture ID is valid
    if not gl.glIsTexture(texture_id):
        imgui.text(f"Error: {texture_id} is not a valid texture")
        return False, input_value

    # 1. Query Texture Properties
    original_texture = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)

    width = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_WIDTH)
    height = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_HEIGHT)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    if width > 16384 or height > 16384:
        imgui.text(f"Error: Texture size {width}x{height} exceeds maximum supported size.")
        return False, input_value

    if width == 0 or height == 0:
        return False, input_value

    # 2. Canvas Setup (Fill all space)
    view_width = max(1, draw_state.width)
    view_height = max(1, draw_state.height)

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

    mixed_color = (1, 1, 1, 1)
    highlight_color = (1, 1, 1, 1)

    if style_manager is not None:
        mixed_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                   value=0.3,
                                                   factor=0.9,
                                                   saturation_scale=1.0,
                                                   alpha=1.0)
        highlight_color = style_manager.make_color_rgb(*mixed_color[:3],
                                                       value=1.0,
                                                       factor=0.9,
                                                       saturation_scale=1.0,
                                                       alpha=1.0)
    io = imgui.get_io()
    overlay: _DrawList = imgui.get_overlay_draw_list()

    if double_right_mouse_drag:
        b_str = f"{zoom_state.brightness:.3f}"
        overlay.add_text(double_right_mouse_drag.x, double_right_mouse_drag.y - 30,
                         col=pack_color(*highlight_color[:3], 1),
                         text=f"brightness:{zoom_state.brightness:.3}\ncontrast:{zoom_state.contrast:.3}")

        if io.key_shift:
            if io.key_ctrl:
                zoom_state.hue += double_right_mouse_drag.dx * 0.001
                zoom_state.saturation -= double_right_mouse_drag.dy * 0.001
            else:
                zoom_state.brightness += double_right_mouse_drag.dx * 0.001
                zoom_state.contrast -= double_right_mouse_drag.dy * 0.001
        else:
            if io.key_ctrl:
                zoom_state.hue += double_right_mouse_drag.dx * 0.005
                zoom_state.saturation -= double_right_mouse_drag.dy * 0.005
            else:
                zoom_state.brightness += double_right_mouse_drag.dx * 0.005
                zoom_state.contrast -= double_right_mouse_drag.dy * 0.005

        # zoom_state.brightness = max(0.0, min(max_brightness, zoom_state.brightness))
        # zoom_state.contrast = max(0.0, min(max_contrast, zoom_state.contrast))

    if jet:
        texture_id = Core.melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Core.melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Core.melty.filter.jet(texture_id, offset=zoom_state.hue)
    else:
        texture_id = Core.melty.filter.brightness_contrast(
            input_value,
            brightness=zoom_state.brightness,
            contrast=zoom_state.contrast
        )

        # texture_id = Core.melty.filter.swirl(
        #     input_value,
        #     radius=zoom_state.brightness,
        #     angle=zoom_state.contrast
        #
        # )
        texture_id = Core.melty.filter.hue_saturation(
            texture_id,
            saturation=(zoom_state.saturation),
            hue_shift=(zoom_state.hue),
        )

    # texture_id = Core.melty.filter.swirl(
    #     input_value,
    #     radius=1.0,
    #     angle=(zoom_state.brightness * 5),
    # )
    # texture_id = Core.melty.filter.swirl(
    #     texture_id,
    #     angle=zoom_state.brightness,
    #     radius=zoom_state.contrast
    # )

    # `dim_outside=(x0, y0, x1, y1)` (texels, row 0 = top): the image is drawn
    # darkened by `dim_alpha` (the `multiply` filter) and the rectangle is
    # drawn over it from the undimmed texture, then outlined -- the
    # hdr-viewer's crop selection. Translucent fills on the image never
    # showed here (09-14); the image path does.
    bright_texture_id = texture_id
    if dim_outside is not None:
        texture_id = Core.melty.filter.multiply(texture_id, factor=1.0 - dim_alpha)

    # Pixel mode: `nearest=True` shows the texels as hard squares when zoomed
    # in (see hdr-viewer: "Pixel Mode Nearest" menu item), else bilinear.
    # Stamped every frame on the filter chain's OUTPUT texture - that is what
    # gets drawn, and the filter may hand back a different or reused texture
    # from one frame to the next.
    filter_mode = gl.GL_NEAREST if nearest else gl.GL_LINEAR
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture_id)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, filter_mode)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, filter_mode)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    p_min = (draw_state.abs_left + 2, draw_state.abs_top + 2)
    p_max = (draw_state.abs_left + draw_state.width, draw_state.abs_top + draw_state.height - 2)
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
    # Not while a text field has the keyboard (a melty draw_text editor or an
    # imgui input): typing a path with a "4" in it over the image zoomed the
    # hdr-viewer to 12.5 % (09-14).
    typing = Core.melty.text_focused_ds is not None or io.want_text_input
    if hovered and not typing:
        if imgui.is_key_pressed(key_1) or imgui.is_key_pressed(numpad_key_1):  # Key '1'
            forced_zoom = 1.0
            # Reset Pan to Center
            zoom_state.center_u = 0.5
            zoom_state.center_v = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(50):  # Key '2'
            forced_zoom = 0.5
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(51):  # Key '3'
            forced_zoom = 0.25
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0
        elif imgui.is_key_pressed(52):  # Key '4'
            forced_zoom = 0.125
            zoom_state.brightness = 0.0
            zoom_state.contrast = 1.0
            zoom_state.hue = 0.0
            zoom_state.saturation = 1.0

    # Double-click toggles between the image centred at native size (one
    # texel per pixel, which "100 %" scrolling rarely lands on exactly) and
    # fit. Zoom 1.0 is fit, and native is the texture's size over the view's
    # along the fitted axis; "at native" allows a hair of float slop.
    # The toggle fires on the RELEASE of a double click that did not drag:
    # a double-click-and-drag is the hdr-viewer's crop gesture (09-14), and
    # toggling on the press would jump the view under the drag.
    if hovered and imgui.is_mouse_double_clicked(0):
        zoom_state.double_click_armed = True
    double_click = False
    if getattr(zoom_state, 'double_click_armed', False):
        if imgui.is_mouse_dragging(0, 4.0):
            zoom_state.double_click_armed = False
        elif imgui.is_mouse_released(0):
            zoom_state.double_click_armed = False
            double_click = True
    if double_click:
        if view_aspect > tex_aspect:
            native_zoom = height / max(1.0, view_height)
        else:
            native_zoom = width / max(1.0, view_width)
        native_zoom = max(min_zoom, min(native_zoom, max_zoom))
        at_native = abs(math.log2(zoom_state.zoom / native_zoom)) < 0.01
        forced_zoom = 1.0 if at_native else native_zoom
        zoom_state.center_u = 0.5
        zoom_state.center_v = 0.5

    if forced_zoom > 0:
        zoom_state.zoom = forced_zoom
        # Recalculate uv sizes immediately for consistent bounding this frame
        if view_aspect > tex_aspect:
            uv_height_size = 1.0 / zoom_state.zoom
            uv_width_size = uv_height_size * (view_aspect / tex_aspect)
        else:
            uv_width_size = 1.0 / zoom_state.zoom
            uv_height_size = uv_width_size * (tex_aspect / view_aspect)

    # Scroll Logic: zoom_delta is in STOPS (log2): a notch multiplies the
    # zoom by 2^(zoom_speed/2) (~11 % at the default 0.3), shift is 0.3x
    # that. Exponential steps are symmetric, so zooming up and down cancel
    # exactly instead of drifting (the old 1 + delta style gained ~9 % per
    # round trip and the view could never find its way home to fit).
    zoom_step = zoom_speed * 0.5
    if scroll_delta != 0:
        if io.key_shift:
            zoom_delta = scroll_delta * zoom_step * 0.3
        else:
            zoom_delta = scroll_delta * zoom_step
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
        new_zoom = max(min_zoom, min(zoom_state.zoom * (2.0 ** zoom_delta), max_zoom))

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

            # Every step anchors on the mouse, zooming out included: the
            # image is not pulled back to centre on the way to fit (that
            # homing was removed 09-14; the bounding step below still keeps
            # it on screen).
            zoom_state.center_u += diff_w * (mouse_u_ratio - 0.5)
            zoom_state.center_v += diff_h * (0.5 - mouse_v_ratio)

            zoom_state.zoom = new_zoom

            # Update these for Step 5
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

    # Top-down uploads (for example chat images) keep the same pan/zoom
    # geometry and reverse only the texture sampling coordinates.
    uv_a = (uv_x_min, 1.0 - uv_y_max if flip_y else uv_y_max)
    uv_b = (uv_x_max, 1.0 - uv_y_min if flip_y else uv_y_min)

    # 6. Clip and Draw
    scale_u_px = view_width / uv_width_size
    scale_v_px = view_height / uv_height_size

    # Project UV Edges
    raw_img_left = p_min_x + (0.0 - uv_x_min) * scale_u_px
    raw_img_right = p_min_x + (1.0 - uv_x_min) * scale_u_px
    raw_img_top = p_min_y + (uv_y_max - 1.0) * scale_v_px
    raw_img_bottom = p_min_y + (uv_y_max - 0.0) * scale_v_px

    # Intersect with Viewport
    clip_left = max(p_min_x, raw_img_left)
    clip_right = min(p_max[0], raw_img_right)
    clip_top = max(p_min_y, raw_img_top)
    clip_bottom = min(p_max[1], raw_img_bottom)
    draw_list: _DrawList = imgui.get_window_draw_list()

    if imgui.is_mouse_hovering_rect(clip_left, clip_top, clip_right, clip_bottom):
        draw_state.hover_reported = True
    else:
        draw_state.hover_reported = False

    Core.melty.push_clip((clip_left, clip_top, clip_right - 3, clip_bottom))
    draw_list.add_image_rounded(texture_id,
                                a=p_min,
                                b=p_max,
                                uv_a=uv_a,
                                uv_b=uv_b,
                                rounding=5.0)
    draw_list.add_rect(raw_img_left, raw_img_top, raw_img_right + 1, raw_img_bottom + 1,
                       pack_color(*mixed_color[:3], 1.0),
                       0.0, 0, 1.0)
    if dim_outside is not None:
        x0, y0, x1, y1 = dim_outside
        sx = (raw_img_right - raw_img_left) / max(1, width)
        sy = (raw_img_bottom - raw_img_top) / max(1, height)
        sl, st = raw_img_left + x0 * sx, raw_img_top + y0 * sy
        sr, sb = raw_img_left + x1 * sx, raw_img_top + y1 * sy
        sl, st = max(sl, clip_left), max(st, clip_top)
        sr, sb = min(sr, clip_right - 3), min(sb, clip_bottom)
        if sr > sl and sb > st:
            def uv(x, y):
                v = uv_y_max - (y - p_min_y) / scale_v_px
                return uv_x_min + (x - p_min_x) / scale_u_px, 1.0 - v if flip_y else v
            gl.glBindTexture(gl.GL_TEXTURE_2D, bright_texture_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, filter_mode)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, filter_mode)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            draw_list.add_image(bright_texture_id, (sl, st), (sr, sb), uv(sl, st), uv(sr, sb))
            draw_list.add_rect(sl, st, sr, sb, pack_color(1.0, 1.0, 1.0, 0.9), 0.0, 0, 1.0)
    Core.melty.pop_clip()

    if show_info:
        line_height = imgui.get_text_line_height()
        draw_list.add_text(max(p_min_x + 5, raw_img_left), clip_top - line_height - 5,
                           pack_color(*mixed_color[:3], 1.0),
                           text=f"{original_id} - {texture_id} - {width}x{height} - Zoom: {zoom_state.zoom:.2f}x")

    gl.glBindTexture(gl.GL_TEXTURE_2D, original_texture)

    return False, draw_state
