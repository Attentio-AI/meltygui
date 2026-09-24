"""Image interaction prepares textures; overlays fit them to live view bounds."""
import math

import OpenGL.GL as gl
import meltygui_imgui as imgui
import numpy

from meltygui.core.core_render import render_func
from meltygui.core.graphics.gl_state import GLState
from meltygui.core.graphics.shader_func import shader_func
from meltygui.core.rendering.core_decoration import Core
from meltygui.hdr_color import pack_color
from meltygui.model.texture_model import ImageTexture, texture_filter_target
from meltygui.state.new_core_model import ZoomState
from meltygui.state.texture_state import TextureViewState
from meltygui.view.header_view import draw_header


def texture_uv_size(width, height, view_width, view_height, zoom):
    """Visible texture-coordinate span at fit-relative zoom."""
    ratio = (view_width / view_height) / (width / height)
    return (ratio / zoom, 1.0 / zoom) if ratio > 1 else (1.0 / zoom, 1.0 / (ratio * zoom))


def texture_center(center, span, pixels):
    # Keep at least 20 screen pixels of the image reachable when panning.
    margin = 20.0 * span / pixels
    low, high = -span * 0.5 + margin, 1.0 + span * 0.5 - margin
    return 0.5 if low > high else max(low, min(center, high))


def draw_texture_overlay(draw_state, draw_list):
    """Live geometry and submission; no uploads, filters or raw input polls."""
    state = draw_state.misc.get('_texture_state')
    if state is None or not state.texture_id or state.zoom_state is None:
        return
    zoom = state.zoom_state
    adjustment = draw_state.get_action('double_right_mouse_drag')
    if adjustment is not None:
        draw_list.add_text(adjustment.x, adjustment.y - 30, state.color,
                           f"brightness:{zoom.brightness:.3f}\ncontrast:{zoom.contrast:.3f}")
    view_width, view_height = max(1, draw_state.width), max(1, draw_state.height)
    uv_width, uv_height = texture_uv_size(state.width, state.height, view_width, view_height, zoom.zoom)
    center_u = texture_center(zoom.center_u, uv_width, view_width)
    center_v = texture_center(zoom.center_v, uv_height, view_height)
    uv_left, uv_top = center_u - uv_width * 0.5, center_v + uv_height * 0.5
    left, top = draw_state.abs_left + 2, draw_state.abs_top + 2
    right, bottom = draw_state.abs_left + draw_state.width, draw_state.abs_top + draw_state.height - 2
    scale_x, scale_y = view_width / uv_width, view_height / uv_height
    image_left, image_top = left - uv_left * scale_x, top + (uv_top - 1) * scale_y
    image_right, image_bottom = image_left + scale_x, image_top + scale_y
    clip_left, clip_top = max(left, image_left), max(top, image_top)
    clip_right, clip_bottom = min(right, image_right), min(bottom, image_bottom)
    if clip_right - 3 <= clip_left or clip_bottom <= clip_top:
        return

    def uv(x, y):
        v = uv_top - (y - top) / scale_y
        return uv_left + (x - left) / scale_x, 1.0 - v if state.flip_y else v

    # The framework clips to ancestors and masks higher windows. This local
    # clip keeps the image out of sibling controls and its tile's footer.
    draw_list.push_clip_rect(clip_left, clip_top, clip_right - 3, clip_bottom, True)
    try:
        uv_a = (uv_left, 1.0 - uv_top if state.flip_y else uv_top)
        uv_bottom = uv_top - uv_height
        uv_b = (uv_left + uv_width, 1.0 - uv_bottom if state.flip_y else uv_bottom)
        draw_list.add_image_rounded(state.texture_id, (left, top), (right, bottom),
                                    uv_a, uv_b, rounding=5.0)
        draw_list.add_rect(image_left, image_top, image_right + 1, image_bottom + 1,
                           state.color, 0.0, 0, 1.0)
        if state.dim_outside is not None:
            x0, y0, x1, y1 = state.dim_outside
            sx, sy = scale_x / state.width, scale_y / state.height
            sl, st = max(image_left + x0 * sx, clip_left), max(image_top + y0 * sy, clip_top)
            sr, sb = min(image_left + x1 * sx, clip_right - 3), min(image_top + y1 * sy, clip_bottom)
            if sr > sl and sb > st:
                draw_list.add_image(state.bright_texture_id, (sl, st), (sr, sb), uv(sl, st), uv(sr, sb))
                draw_list.add_rect(sl, st, sr, sb, pack_color(1.0, 1.0, 1.0, 0.9), 0.0, 0, 1.0)
    finally:
        draw_list.pop_clip_rect()
    if state.show_info:
        # Stay inside this view even when the caption sits above a letterbox.
        draw_list.push_clip_rect(draw_state.abs_left, draw_state.abs_top, right, bottom, True)
        try:
            draw_list.add_text(max(left + 5, image_left), clip_top - state.line_height - 5,
                               state.color,
                               f"{state.source_id} - {state.texture_id} - {state.width}x{state.height} - Zoom: {zoom.zoom:.2f}x")
        finally:
            draw_list.pop_clip_rect()


@render_func(is_default_for=(numpy.uint32, ImageTexture), show_bg=False, use_cache=False,
             show_add_delete=False, z_offset=0, fill_height=True, selectable=False,
             indent_size=0, min_width=35, min_height=35, wrap=False, disable_scroll=True,
             zoom_speed=0.3, with_header=draw_header, manual_content_height=True,
             draw_overlay=draw_texture_overlay)
def draw_texture(input_value: numpy.uint32 | ImageTexture, hovered, scroll_y_changed,
                 middle_mouse_drag, double_right_mouse_drag, zoom_state: ZoomState, zoom_speed,
                 header_height=0, min_zoom=0.1, max_zoom=50.0, style_manager=None,
                 max_brightness=5.0, max_contrast=5.0, draw_state=None, jet=False, nearest=False,
                 dim_outside=None, dim_alpha=0.55, show_info=True, flip_y=False,
                 _texture_state: TextureViewState = None, gl_state: GLState = None,
                 left_mouse_double_clicked=None, kp_1_pressed=None, keyboard_available=True, **kwargs):
    """Prepare this view's image and handle injected events; the overlay paints it."""
    _texture_state.texture_id = 0
    original_id = int(input_value)
    imgui.dummy(draw_state.width, max(0, draw_state.height - 20))
    if not gl.glIsTexture(original_id):
        imgui.text(f"Error: {original_id} is not a valid texture")
        return False, input_value
    original_texture = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)
    try:
        gl.glBindTexture(gl.GL_TEXTURE_2D, original_id)
        width = int(gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_WIDTH))
        height = int(gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_HEIGHT))
    finally:
        gl.glBindTexture(gl.GL_TEXTURE_2D, original_texture)
    if width > 16384 or height > 16384:
        imgui.text(f"Error: Texture size {width}x{height} exceeds maximum supported size.")
        return False, input_value
    if width == 0 or height == 0:
        return False, input_value

    view_width, view_height = max(1, draw_state.width), max(1, draw_state.height)
    uv_width, uv_height = texture_uv_size(width, height, view_width, view_height, zoom_state.zoom)
    zoom_state.center_u = texture_center(zoom_state.center_u, uv_width, view_width)
    zoom_state.center_v = texture_center(zoom_state.center_v, uv_height, view_height)

    # Numeric input IDs cannot be Python parameter names. Subscribe through
    # the same event dispatcher so key presses wake cached texture views.
    one = draw_state.on_action('1_pressed')
    two = draw_state.on_action('2_pressed')
    three = draw_state.on_action('3_pressed')
    four = draw_state.on_action('4_pressed')
    forced_zoom = None
    if hovered and keyboard_available:
        if one is not None or kp_1_pressed is not None:
            forced_zoom = 1.0
            zoom_state.center_u = zoom_state.center_v = 0.5
        elif two is not None:
            forced_zoom = 0.5
        elif three is not None:
            forced_zoom = 0.25
        elif four is not None:
            forced_zoom = 0.125
        if forced_zoom is not None:
            zoom_state.brightness, zoom_state.contrast = 0.0, 1.0
            zoom_state.hue, zoom_state.saturation = 0.0, 1.0
    # The dispatcher emits a double click on release only if it did not drag,
    # leaving double-click-and-drag available for an enclosing crop control.
    if left_mouse_double_clicked is not None:
        native_zoom = max(width / view_width, height / view_height)
        native_zoom = max(min_zoom, min(native_zoom, max_zoom))
        at_native = abs(math.log2(zoom_state.zoom / native_zoom)) < 0.01
        forced_zoom = 1.0 if at_native else native_zoom
        zoom_state.center_u = zoom_state.center_v = 0.5
    if forced_zoom is not None:
        zoom_state.zoom = forced_zoom
        uv_width, uv_height = texture_uv_size(width, height, view_width, view_height, forced_zoom)

    if double_right_mouse_drag is not None:
        step = 0.001 if double_right_mouse_drag.shift else 0.005
        if double_right_mouse_drag.ctrl:
            zoom_state.hue += double_right_mouse_drag.dx * step
            zoom_state.saturation -= double_right_mouse_drag.dy * step
        else:
            zoom_state.brightness += double_right_mouse_drag.dx * step
            zoom_state.contrast -= double_right_mouse_drag.dy * step

    zoom_event, zoom_delta = None, 0.0
    if scroll_y_changed is not None and scroll_y_changed.value:
        zoom_event = scroll_y_changed
        zoom_delta = scroll_y_changed.value * zoom_speed * 0.5 * (0.3 if scroll_y_changed.shift else 1.0)
    elif middle_mouse_drag is not None and middle_mouse_drag.ctrl:
        zoom_event = middle_mouse_drag
        zoom_delta = middle_mouse_drag.dy * -0.008
    if middle_mouse_drag is not None and not middle_mouse_drag.ctrl:
        speed = 0.5 if middle_mouse_drag.shift else 1.0
        zoom_state.center_u -= middle_mouse_drag.dx * uv_width / view_width * speed
        zoom_state.center_v += middle_mouse_drag.dy * uv_height / view_height * speed
    if zoom_delta:
        new_zoom = max(min_zoom, min(zoom_state.zoom * (2.0 ** zoom_delta), max_zoom))
        new_width, new_height = texture_uv_size(width, height, view_width, view_height, new_zoom)
        mouse_u, mouse_v = 0.5, 0.5
        if not zoom_event.ctrl:
            mouse_u = (zoom_event.x - draw_state.abs_left - 2) / view_width
            mouse_v = (zoom_event.y - draw_state.abs_top - 2) / view_height
        zoom_state.center_u += (uv_width - new_width) * (mouse_u - 0.5)
        zoom_state.center_v += (uv_height - new_height) * (0.5 - mouse_v)
        zoom_state.zoom = new_zoom
        uv_width, uv_height = new_width, new_height
    zoom_state.center_u = texture_center(zoom_state.center_u, uv_width, view_width)
    zoom_state.center_v = texture_center(zoom_state.center_v, uv_height, view_height)

    # Explicit outputs keep two views of one input independent. GLState owns
    # their lifetime and replaces allocations only when dimensions/sampling change.
    adjusted = texture_filter_target(gl_state, 'texture_adjusted', width, height, nearest)
    bright = texture_filter_target(gl_state, 'texture_color', width, height, nearest)
    filters = Core.melty.filter
    filters.brightness_contrast(original_id, output_texture=adjusted,
                                brightness=zoom_state.brightness, contrast=zoom_state.contrast)
    if jet:
        filters.jet(adjusted, output_texture=bright, offset=zoom_state.hue)
    else:
        filters.hue_saturation(adjusted, output_texture=bright,
                               saturation=zoom_state.saturation, hue_shift=zoom_state.hue)
    output = bright
    if dim_outside is not None:
        output = texture_filter_target(gl_state, 'texture_dimmed', width, height, nearest)
        filters.multiply(bright, output_texture=output, factor=1.0 - dim_alpha)
    else:
        gl_state.drop('texture_dimmed')

    color = (1.0, 1.0, 1.0)
    if style_manager is not None:
        color = style_manager.make_color_rgb(*color, value=0.3, factor=0.9,
                                             saturation_scale=1.0, alpha=1.0)[:3]
    _texture_state.source_id = original_id
    _texture_state.texture_id = output
    _texture_state.bright_texture_id = bright
    _texture_state.width, _texture_state.height = width, height
    _texture_state.zoom_state = zoom_state
    _texture_state.color = pack_color(*color, 1.0)
    _texture_state.flip_y, _texture_state.dim_outside = flip_y, dim_outside
    _texture_state.show_info = show_info
    _texture_state.line_height = imgui.get_text_line_height()
    return False, input_value


IMAGE_BLIT_FRAG = """
#version 330 core
out vec4 FragColor;
uniform sampler2D image;
void main() { FragColor = texelFetch(image, ivec2(gl_FragCoord.xy), 0); }
"""


@shader_func(fragment=IMAGE_BLIT_FRAG)
def image_blit_pass(gl_state: GLState = None, image=None, program=None, **kwargs):
    """Fullscreen copy of `image` (a 2-D GLTexture the size of the target)
    into the bound FBO — the voxel_pass stand-in for cuda_march frames."""
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
