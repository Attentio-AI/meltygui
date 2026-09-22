"""Decoration view functions and supporting definitions."""
from math import sqrt
from meltygui.hdr_color import pack_color
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.window_decoration import window
from meltygui.core.runtime.toggles import mix
import meltygui_imgui as imgui


@render_func(use_cache=False)
def draw_drag_drop_target(input_value, draw_state, on_drag, do_flow, depth,
                          collection, key, meltygui, y_offset, enable_flow, min_width,
                          unique, tag, style_manager, offset=0, indent_size=10):
    from meltygui.core.styling.global_style import GlobalStyle

    if Core.melty.active_layer == Core.melty.drag_layer:
        return False, 0.0

    cursor_y_screen = imgui.get_cursor_screen_pos()[1]

    if collection == input_value or not Core.melty.is_window_enabled():
        return False, 0.0

    if meltygui.initial_drag_offset is None:
        return False, 0.0

    if key is None:
        pass
    # ----------------- top spacing -----------
    falloff = 25.0  # Higher is gentler
    if enable_flow:
        drop_gap = 6.0
    else:
        drop_gap = 0.0

    drag_delta_curve = 1.0 - max(0.0, min(1.0, 1.0 - abs(meltygui.drag_delta[1] / 15.0)))

    mouse_pos = imgui.get_mouse_pos()
    cursor_top = imgui.get_cursor_screen_pos()[1]
    cursor_left = imgui.get_cursor_screen_pos()[0]
    static_offset = drop_gap
    distance_to_mouse = abs(mouse_pos[1] - cursor_y_screen -
                            meltygui.initial_drag_offset[1] - drop_gap + static_offset)
    bell_curve = max(0.0, min(1.0, 1.0 - (distance_to_mouse / falloff)))

    window_size = imgui.get_window_size()
    window_pos = imgui.get_window_position()
    window_rect = (window_pos[0], window_pos[1],
                   window_pos[0] + window_size[0],
                   window_pos[1] + window_size[1])
    mouse_over_window = imgui.is_mouse_hovering_rect(*window_rect)

    if meltygui.drag_in_progress:
        if meltygui.dragged_item is None:
            meltygui.drag_in_progress = False

        elif id(meltygui.dragged_item._input_value) == id(collection):
            return False, 0.0

    if meltygui.drag_in_progress and do_flow and not on_drag and mouse_over_window:
        flow_spacing = drop_gap * bell_curve * drag_delta_curve
    else:
        flow_spacing = 0.0
        drag_delta_curve = 1.0

    if tag == "top":
        Core.melty.flow_spacing += (flow_spacing)
        # imgui.set_cursor_pos_y(imgui.get_cursor_pos()[1] + (flow_spacing))

    draw_list = imgui.get_window_draw_list()
    # if Core.melty.channels_split:
    #     draw_list.channels_set_current(min(Core.melty.max_depth - 1, depth + 2))

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

    if meltygui.drag_in_progress and not on_drag and do_flow and mouse_over_window:
        if draw_state.height is not None:
            active_drop = (meltygui.drag_drop_target == draw_state.unique
                           and tag == meltygui.drag_drop_target_tag)

            if Core.melty.channels_split:
                draw_list.channels_set_current(min(Core.melty.get_channel() + 1, Core.melty.max_depth - 1))

                if active_drop:
                    draw_list.channels_set_current(min(Core.melty.get_channel() + 2, Core.melty.max_depth - 1))
                    cursor_bottom += ((1.0 - drag_delta_curve) * drop_gap)

            if distance_to_mouse < meltygui.nearest_drop_distance:
                meltygui.nearest_drop_distance = distance_to_mouse
                meltygui.nearest_drop_target = draw_state.unique
                meltygui.nearest_drop_target_tag = tag

                meltygui.drag_drop_action.target_unique = draw_state.unique
                meltygui.drag_drop_action.target_tag = tag
                meltygui.drag_drop_action.target_key = key
                meltygui.drag_drop_action.target_collection = collection
                meltygui.drag_drop_action.target_draw_state = draw_state

                if meltygui.drag_drop_action.target_key is None:
                    pass

            height_as_factor = 800.0
            drag_distance = sqrt(meltygui.drag_delta[0] ** 2 + meltygui.drag_delta[1] ** 2)
            initial_fade_offset = max(min(1.0, meltygui.total_drag_distance / 10.0), 0.0)
            if meltygui.total_drag_frames < 1:
                initial_fade_offset = 0.0
            opacity = max(0.0, min(1.0, 1.0 - (distance_to_mouse / (height_as_factor * 0.3))))
            opacity *= initial_fade_offset
            # opacity = 1.0 if active_drop else opacity

            bg_tint = Core.melty.get_bg_color(-1)
            bg_style = GlobalStyle.get_global_constant("bg_style", folder="bg_styles")

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
            bottom = max(draw_state.abs_top, cursor_bottom - 1)
            left = draw_state.abs_left + offset
            right = draw_state.abs_left + draw_state.width - indent_size
            width = draw_state.width
            height = draw_state.height

            draw_list.add_rect_filled(left, top, right, bottom,
                                      col=pack_color(*color), rounding=4.0)

            if opacity > 0:
                Core.melty.cache.mask_mark_rect(draw_state, Core.melty.max_depth - 1, draw_state.shadow_depth, left,
                                                top, width,
                                                height,
                                                key=f"{left}x{top}_flow")
            #
            # draw_list.add_line(draw_state.left, draw_state.abs_top - 2 - offset,
            #                    draw_state.left + draw_state.width,
            #                    draw_state.abs_top - 2 - offset,
            #                    col=pack_color(*color), thickness=3)

    return False, flow_spacing


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

    col_track = pack_color(0, 0, 0, 0.1)
    col_grab = pack_color(1, 1, 1, 0.3)
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
    # Core.melty.cache.mask_mark_rect(Core.melty.depth, track_x1, track_y1, track_w, track_h,
    #                            key=str(Core.melty.unique_stack[-1]) + "scrollbar")

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


@window
def draw_bg(left=25, top=0, width=0, height=57, depth=0, rounding=6.0, bg_offset=0,
            outline=True, bg_color=None, opacity=0.0,
            style_manager=None, tint=None, outline_tint=None, selected=False,
            hovered=False, pressed=False, nested_bg=False, saturation=1.0, max_bg_depth=None,
            max_bg_value=None, **kwargs):
    # -- Constants ---------------------------------
    from meltygui.core.cache.tile_marks import snap_int
    from meltygui.core.rendering.render_dispatch import _DRAW_BG_COLOUR_MEMO
    from meltygui.core.rendering.render_dispatch import _DRAW_BG_FILL_MEMO
    from meltygui.model.color_model import _clamp_bg_value

    min_value = -0.272
    depth_wrap = 300
    depth_scale = 6.241

    # [tint=(2,1,1)]
    corner_radius = rounding
    border_inset = 2.802
    border_inset_half = 1.5
    if not outline:
        # The inset only exists to seat the fill inside the outline stroke.
        # With no outline the fill IS the view's edge (freeze_resize panes
        # via draw_freeze_bg): keep the inset and content clipped at the
        # view edge hangs a few px past its own background.
        border_inset = 0.0
    stroke_width = 4.0
    # How depth maps to color intensity
    intensity_factor = 0.021
    intensity_offset = 10.018

    some_var = [32, 18, 19]
    # Outline color tuning
    outline_base = 1.765
    outline_depth_mul = 0.786
    outline_sat = {'default': 1.1, 'nested': 1.473}

    # More text
    bleed_mix = {'nested': 0.472, 'default': 0.526}
    bleed_style = {'value': -0.035, 'alpha': 1.112, 'saturation': 6.592}
    outline_bleed_mix = 0.272
    # Hover offsets per interaction state
    hover_offset_by_state = {
        'default': -1.807,
        'selected': -1.401,
        'pressed_hi': -1.813,  # pressed + opacity > 0.5
        'pressed_lo': -0.441,
    }
    bg_style = {
        'value': -0.004, 'saturation': 1.101,
        'alpha': 0.504, 'max_value': 1.8,
    }

    # ── Helpers ────────────────────────────────────────────────
    def current_indent_px():
        return Core.melty.current_indent

    def mix_colors(color_a, color_b, factor):
        return (
            color_a[0] * (1 - factor) + color_b[0] * factor,
            color_a[1] * (1 - factor) + color_b[1] * factor,
            color_a[2] * (1 - factor) + color_b[2] * factor,
        )

    # -- Depth calculation -------------------
    max_depth = 30
    if Core.melty.bg_depth + bg_offset < 2:
        wrapped_depth = min(max_depth, (Core.melty.bg_depth) + bg_offset)
    else:
        wrapped_depth = min(max_depth, (Core.melty.bg_depth % depth_wrap) + bg_offset)

    # Caller-supplied ceiling on the effective depth: after this step the view
    # keeps the color entry for max_bg_depth instead of getting lighter.
    if max_bg_depth is not None:
        wrapped_depth = min(wrapped_depth, max_bg_depth)

    scaled_depth = wrapped_depth * depth_scale
    depth_intensity = (scaled_depth + intensity_offset) * intensity_factor
    max_depth_intensity = 0.652
    depth_intensity = min(depth_intensity, max_depth_intensity)

    # ── Geometry ───────────────────────────────────────────────
    right = left + width
    bottom = top + height

    fill_rect = (
        snap_int(left) + border_inset, snap_int(top) + border_inset,
        snap_int(right) - border_inset, snap_int(bottom) - border_inset,
    )
    outline_rect = (
        snap_int(left) + border_inset_half, snap_int(top) + border_inset_half,
        snap_int(right) - border_inset_half, snap_int(bottom) - border_inset_half,
    )

    # ── Interaction hover offset ───────────────────────────────
    hover_offset = hover_offset_by_state['default']
    if selected:
        hover_offset = hover_offset_by_state['selected']
    elif pressed:
        if opacity > 0.5:
            hover_offset = hover_offset_by_state['pressed_hi']
        else:
            hover_offset = hover_offset_by_state['pressed_lo']

    # ── Outline style ──────────────────────────────────────────
    sat = outline_sat['default']
    depth_mul = outline_depth_mul
    if not nested_bg:
        depth_mul *= 1.00
        sat = outline_sat['nested']

    # ── Background bleed color ─────────────────────────────────
    bleed_factor = bleed_mix['nested'] if nested_bg else bleed_mix['default']

    # The bleed / outline colours are a pure function of the style manager,
    # the two bg-stack colours behind this box and a few scalars - memoized,
    # since ~40 draw_bg calls a frame (inline widgets, flat buttons) each
    # require four hsv round trips for the same handful of params.
    bg_m2 = Core.melty.get_bg_color(-2)
    bg_m1 = Core.melty.get_bg_color(-1)
    outline_value = max(min_value, depth_intensity * depth_mul + outline_base + hover_offset)
    colour_key = (style_manager.hsv, bg_m2, bg_m1, outline_value, sat)
    memo = _DRAW_BG_COLOUR_MEMO.get(colour_key)
    if memo is None:
        bleed_color = style_manager.make_custom_styled(
            *bg_m2, input=bg_style, **bleed_style,
        )
        bleed_base = mix(*bg_m1[:3], *bleed_color[:3], 0.32)
        bleed_color = style_manager.make_custom_styled(
            *bleed_base, input=bg_style, **bleed_style,
        )
        # ── Outline rendering ──────────────────────────────────────
        outline_color = style_manager.make_color_style_value(
            input=bg_style, saturation=sat, value=outline_value,
        )
        outline_color = mix_colors(outline_color, bleed_color, outline_bleed_mix)
        if len(_DRAW_BG_COLOUR_MEMO) > 2048:
            _DRAW_BG_COLOUR_MEMO.clear()
        memo = _DRAW_BG_COLOUR_MEMO[colour_key] = (bleed_color, outline_color)
    bleed_color, outline_color = memo

    if outline:
        packed_outline = pack_color(*outline_color[:3], 1.0)
        if outline_tint is not None:
            packed_outline = pack_color(*outline_tint[:3], 1.0)
        imgui.get_window_draw_list().add_rect(
            *outline_rect, col=packed_outline, rounding=corner_radius, thickness=stroke_width,
        )

    # ── Fill rendering ─────────────────────────────────────────
    if bg_color is None:
        # Fill colour memoized beside the bleed/outline memo: same inputs
        # plus the fill's own saturation / depth intensity / bleed factor.
        fill_key = (colour_key, saturation, depth_intensity, bleed_factor)
        bg_color = _DRAW_BG_FILL_MEMO.get(fill_key)
        if bg_color is None:
            bg_color = style_manager.make_color_style_value(input=bg_style, saturation=bg_style['saturation'] * saturation,
                                                            value=max(min_value, depth_intensity))
            bg_color = mix_colors(bg_color, bleed_color, bleed_factor)
            if len(_DRAW_BG_FILL_MEMO) > 2048:
                _DRAW_BG_FILL_MEMO.clear()
            _DRAW_BG_FILL_MEMO[fill_key] = bg_color

    # Applies to whatever ends up as the fill - depth-ramp color OR a passed
    # bg_color/tint, so the cap holds regardless of the input's hue/brightness.
    bg_color = _clamp_bg_value(bg_color, max_bg_value)
    packed_fill = pack_color(bg_color[0], bg_color[1], bg_color[2], 1.0)
    if tint is not None:
        tinted = _clamp_bg_value(tint, max_bg_value)
        packed_fill = pack_color(*tinted[:3], opacity)

    if opacity > 0.0:
        imgui.get_window_draw_list().add_rect_filled(*fill_rect, col=packed_fill, rounding=corner_radius)

    return False, bg_color
