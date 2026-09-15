import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

from meltygui.runtime import Melty

# Cache to track used space - key is snapped y position, value is max x used
_floating_text_cache = {}
_floating_text_prev_frame_heights = {}  # Store max label height per line from previous frame


def floating_text(text: str, x_offset: float = 0, line_height: float = None, tint: tuple = (1, 1, 1, 1),
                  max_width: float = 200):
    cursor_pos = imgui.get_cursor_screen_pos()

    inside_window = len(Melty.window_stack) > 0

    # Get draw list - only use overlay
    draw_list = imgui.get_overlay_draw_list()
    window_pos = Melty.melty_window_stack[-1][0] if len(Melty.melty_window_stack) else imgui.get_window_position()

    # Get current window ID to track labels per-window
    window_id = Melty.window_stack[-1] if len(Melty.window_stack) > 0 else 0

    # Account for scroll offset
    scroll_x = imgui.get_scroll_x()
    scroll_y = imgui.get_scroll_y()

    # Check if the cursor position (in content space) is within the visible content area
    content_min = imgui.get_window_content_region_min()
    content_max = imgui.get_window_content_region_max()
    content_height = content_max.y - content_min.y

    # Don't render if outside the scrolled content bounds
    if cursor_pos[1] < scroll_y or cursor_pos[1] > scroll_y + content_height:
        return

    # Starting position (cursor position in screen coordinates, adjusted for scroll)
    start_x = cursor_pos[0] - scroll_x
    start_y = cursor_pos[1] - scroll_y

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
                    # Single word is too long, just add it anyway
                    text_lines.append(word)
                    current_line = ""
        if current_line:
            text_lines.append(current_line)
    else:
        text_lines = [text]

    # Use imgui's calc_text_size with wrap_width to get proper wrapped size
    wrapped_size = imgui.calc_text_size(text, wrap_width=max_width)
    text_width = wrapped_size.x
    total_text_height = wrapped_size.y

    # Use text height for line snapping if not specified
    if line_height is None:
        line_height = text_line_height + padding * 2

    # Calculate label height
    label_height = total_text_height + padding * 2

    # Snap to line index based on cursor position
    relative_y = cursor_pos[1] - scroll_y
    line_index = round(relative_y / line_height)

    # Calculate actual y by summing previous lines' heights from previous frame
    actual_y = 0
    for i in range(line_index):
        prev_key = (window_id, i)
        if prev_key in _floating_text_prev_frame_heights:
            actual_y += _floating_text_prev_frame_heights[prev_key]
        else:
            actual_y += line_height  # default height if not set

    snapped_y = actual_y

    # Track max label height for this line in current frame
    window_key = Melty.melty_window_stack[-1][2] if len(Melty.melty_window_stack) > 0 else 0
    cache_key = (window_key, line_index)
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
    current_x_right = window_pos[0] + x_offset

    # Check for horizontal overlap at this y position
    if current_x_right >= left_x:
        current_x_right = left_x - spacing

    # Calculate left edge of this label
    label_left = current_x_right - text_width - padding * 2

    # Update cache with this line's x position
    _floating_text_cache[cache_key]['left_x'] = label_left

    # Text position (left edge)
    text_x = label_left + padding
    text_y = snapped_y

    # End position for the line (right edge of label box, middle height)
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
        line_color = pack_color(1, 1, 1, 1)
        dot_color = pack_color(1, 1, 1, 1)
        line_thickness = 2.5
    else:
        line_channel = 0
        line_color = pack_color(tint[0], tint[1], tint[2], tint[3] * 0.5)
        dot_color = pack_color(tint[0], tint[1], tint[2], tint[3])
        line_thickness = 1.0

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

    # Draw background rectangle
    draw_list.add_rect_filled(
        text_x - padding,
        text_y - padding,
        text_x + text_width + padding,
        text_y + label_height - padding,
        pack_color(0.1, 0.1, 0.1, 1.0)
    )

    # Draw outline in tint color (or yellow if hovered)
    if is_hovered:
        outline_color = pack_color(1, 1, 1, 1)
        outline_thickness = 2.0
    else:
        outline_color = pack_color(tint[0], tint[1], tint[2], tint[3])
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
    text_color = pack_color(tint[0], tint[1], tint[2], tint[3])
    line_y = text_y
    for line in text_lines:
        draw_list.add_text(
            text_x,
            line_y,
            text_color,
            line
        )
        line_y += text_line_height


def draw_vertical_scrollbar(content_height: float,
                            view_height: float,
                            view_width: float,
                            scroll_offset: float,
                            scrollbar_width: float,
                            left: float = 0.0,
                            top: float = 0.0,
                            *,
                            pad: float = 1.0,
                            rounding: float = 3.0,
                            tint=None,
                            bar_top_margin: float = 15.0,
                            min_grab_size: float | None = None):
    # Style & colors
    style = imgui.get_style()

    if min_grab_size is None:
        min_grab_size = float(style.grab_min_size)

    if tint is not None:
        col_grab = pack_color(*tint[:3], 1.0)
    else:
        col_grab = imgui.get_color_u32(imgui.COLOR_SCROLLBAR_GRAB)
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

    # Compute geometry (stick it to the right side of the container)
    track_w = min(scrollbar_width, view_width)
    track_h = view_height - bar_top_margin
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

        # Normalized scroll position -> grab position
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
    grab_y1 = max(inner_y1, min(grab_y1, inner_y2 - (grab_y2 - grab_y1))) + bar_top_margin
    grab_y2 = grab_y1 + max(0.0, min(grab_h, inner_y2 - inner_y1))

    # Draw
    dl = imgui.get_window_draw_list()
    # Track

    # Melty.engine.mask_mark_rect(Melty.depth, track_x1, track_y1, track_w, track_h,
    #                             key=str(Melty.unique_stack[-1]) + "scrollbar")

    dl.add_rect(track_x1, track_y1, track_x2, track_y2, col_border, rounding)
    # Grab
    if grab_y2 > grab_y1 and grab_x2 > grab_x1:
        dl.add_rect_filled(grab_x1, grab_y1, grab_x2, grab_y2, col_grab, rounding)
        dl.add_rect(grab_x1, grab_y1, grab_x2, grab_y2, col_border, rounding)

    return None


def clear_floating_text_cache():
    """Call this at the start of each frame to reset label positioning"""

    # Copy current frame's max heights to previous frame for next frame's use
    _floating_text_prev_frame_heights = {
        key: data['max_height']
        for key, data in _floating_text_cache.items()
        if 'max_height' in data
    }

    # Clear current cache for new frame
    _floating_text_cache.clear()



