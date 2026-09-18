"""Control view functions and supporting definitions."""
from enum import Enum
from meltygui.hdr_color import pack_color
from meltygui.core.melty import Melty
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import Core
from meltygui.state.new_core_model import TileMode
from meltygui.core.runtime.toggles import Tint
from meltygui.view.header_view import draw_header
from meltygui_imgui.core import _DrawList
from types import NoneType
import meltygui_imgui as imgui
import types


@render_func(use_cache=True, show_bg=True, width=20, height=22, tile_mode=TileMode.MAX,
             auto_resize=False, just_shadow=True, selectable=False, no_cursor=True, temp=True)
def empty(input_val):
    pass


@render_func(use_cache=True, selectable=False, disable_scroll=True, indent_size=0, show_bg=False, min_width=5,
             min_height=10, wrap=True, show_add_delete=False, rounding=None, icon=None, tint=(0.0, 0.241, 0.556))
def button(input_value="", width=5, height=14, draw_state=None, alpha=1.00, left_mouse_held=False, shadow=True,
           left_mouse_down=False,
           # DEPRECATED (09-12): a render_func widget costs ~0.7 ms of wrapper per call.
           # New code draws buttons with headers/links (draw a rect + label, the
           # click claimed through the parent view's draw_state.on_click); existing
           # call sites migrate as they are converted. Do not add new callers.
           color=(0.533, 0.068, 0.5), icon=None, highlight_hovered=True, hovered=False, style_manager=None,
           show_button_bg=True,
           factor=1.0, tint_value=0.16, text_value=0.694, saturation=1.2, text_saturation=1.2, text_align="center",
           search_match=False, search_current=False, tint=None, rounding=None, corner_radius=6.0, text_pad=15,
           max_bg_brightness=0.25):
    from meltygui.model.color_model import _brightness_clamp
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.search_view import draw_search_highlight

    if color is not None:
        if not isinstance(color, tuple) or len(color) < 3:
            color = (2.558, 0.5, 0.5)
        if shadow:
            if left_mouse_held:
                draw_state.z_offset = 0
            else:
                draw_state.z_offset = 3.0
        else:
            draw_state.z_offset = 0.0

        if hovered and highlight_hovered:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=tint_value + 0.05,
                                                       factor=factor, saturation_scale=saturation, alpha=1.0)
        else:
            mixed_color = style_manager.make_color_rgb(color[0], color[1], color[2], value=tint_value,
                                                       factor=factor, saturation_scale=saturation, alpha=1.0)
        # Brightness guard: cap the bg's perceived brightness (scale-preserving,
        # same clamp as the window's tint washes) so the bright text keeps
        # contrast even when a vivid/near-white color is passed in.
        mixed_color = _brightness_clamp(mixed_color[0], mixed_color[1], mixed_color[2],
                                        0.0, max_bg_brightness)
        text_color = style_manager.make_color_rgb(color[0], color[1], color[2],
                                                  value=text_value + (1.5 if hovered else 0.0),
                                                  factor=factor, saturation_scale=text_saturation, alpha=1.0)
    else:
        text_color = (1.0, 1.0, 1.0)
        mixed_color = (0, 0, 0)

    button_txt = str(input_value).split("##")[0]
    if icon is not None:
        button_txt = f"{icon} {button_txt}"

    min_size = imgui.calc_text_size(button_txt)
    # text_pad is an authored padding value, so it tracks the UI scale;
    # width/height are NOT scaled here - callers pass physical pixels through
    # them (the drag pickup size through height=), which would scale twice.
    width = max(width, min_size[0] + Melty.px(text_pad))
    height = max(height, min_size[1])
    draw_list: _DrawList = imgui.get_window_draw_list()
    bx0, by0 = imgui.get_cursor_screen_pos()

    imgui.dummy(width, height)
    draw_state.width = width
    draw_state.height = height

    # draw_state.width = btn_size[0]
    # draw_state.height = btn_size[1]

    bx1, by1 = bx0 + width, by0 + height
    # `corner_radius` is an auto-state param: Mode / class defaults, internal
    # draw_state.corner_radius writes all flow into it, and the mirror keeps
    # framework painters (selection highlight, blit mask) in sync. `rounding`
    # is an individual per-call override on top (e.g. the search results,
    # which read as a flat list of items, pass rounding to square the corners).
    rnd = corner_radius if rounding is None else rounding

    if alpha > 0.0 and show_button_bg:
        draw_list.add_rect_filled(bx0, by0, bx1, by1,
                                  pack_color(*mixed_color[:3], alpha), rounding=rnd)

    # Tint fill: button mutes `color` into a dark bg, so to show a window's tint
    # we paint the raw colour over it — at low alpha so it stays a subtle wash.
    elif tint is not None and show_button_bg:
        draw_list.add_rect_filled(bx0, by0, bx1, by1,
                                  pack_color(tint[0], tint[1], tint[2], 0.33),
                                  rounding=rnd)

    # Search match highlight (drawn under the text): the current row radiates a
    # circular gradient glow with its rect cut out so its content stays legible;
    # other matches get a thin outline. Tunable via Toggles.SearchSettings.
    # Lifted to a higher depth channel (restored after) so the halo's glow
    # past this row's rect isn't composited over by sibling rows' backgrounds.
    if search_match:
        if Melty.channels_split:
            draw_list.channels_set_current(
                min(Melty.get_channel() + 2, Melty.max_depth - 1))
        draw_search_highlight(draw_list, bx0, by0, bx1, by1, current=search_current, rounding=rnd)
        if Melty.channels_split:
            draw_list.channels_set_current(Melty.get_channel())

    if text_align == "left":
        draw_list.add_text(draw_state.abs_left + 5,
                           draw_state.abs_top + (height - min_size[1]) / 2.0 - 1,
                           pack_color(*text_color[:3], 1.0), button_txt)
    elif text_align == "right":
        draw_list.add_text(draw_state.abs_left + width - min_size[0] - 5,
                           draw_state.abs_top + (height - min_size[1]) / 2.0 - 1,
                           pack_color(*text_color[:3], 1.0), button_txt)
    else:
        draw_list.add_text(draw_state.abs_left + (width - min_size[0]) / 2.0 + 2,
                           draw_state.abs_top + (height - min_size[1]) / 2.0 - 1,
                           pack_color(*text_color[:3], 1.0), button_txt)

    if left_mouse_down:
        # Effect ledger, exactly as flat_button: a fired button is an
        # observable change with an undo record - the Orchestrator cues
        # replays off it. The button's OWN draw_state and rect flow in, so
        # the cue's chain, press_rect and press_rect all describe the
        # button (a header-owned flat_button can only offer its position).
        if Melty.effect_hook is not None:
            try:
                Melty.effect_hook("button", button_txt, draw_state,
                                  rect=(bx0, by0, width, height))
            except Exception:
                pass
        request_render()
        return True, input_value

    return False, None


@render_func(header_same_line=True, use_cache=True, is_default_for=(types.NoneType),
             shadow=False, is_tree=False, with_header=draw_header, temp=True)
def draw_none(input_value: NoneType):
    imgui.align_text_to_frame_padding()
    imgui.text_colored("None", *(0.164, 0.389, 0.197), 0.4)
    return False, input_value


@render_func(is_default_for=(bool), use_cache=True,
             is_tree=False, min_width=83, shadow=False,
             with_header=draw_header, temp=True, 
             # Give the @render_func a unique tint. Note how in the editor this function has a bg tint 
             # pulled from the render_func! Please note, the supplied tint may be muted and darkened by
             # meltygui at the frameworks discretion when used as a tint. 
             tint=(0.0, 0.527, 0.817))
def draw_bool(
              # Important inputs to functions can be given tints!
              # [tint=(0.85, 0.75, 0.05)] 
              input_value: bool, 
              draw_state, left_mouse_clicked=None, max_width=359,
              max_height=100, min_height=20, header_same_line=True,
              selectable=False, left_mouse_drag=None, left_mouse_held=False, align_header=True,
              left_mouse_down=False):
    
    # Use meltygui #[ comments liberally. Constants in the func should always have tints
    # As a generally rule, local constants are preferable to constants referenced elsewhere.
    # Melty is designed to make local variables easy to find. Scatter constants are actually encouraged
    # inside meltygui. Place constants as close to their usage whenever possible.
    
    # [tint=(0.939, 0.453, 0.245)]
    left_margin = 3
    
    # [tint=(0.994, 0.872, 0.0), show_tint=True]
    text_inset = 11
    
    # [tint=(0.939, 0.836, 0.595, 1.0), show_tint=True]
    cursor_start = imgui.get_cursor_pos_x()

    if input_value:
        bg_color = pack_color(*Tint.checkbox_bg_selected(), 1.0)
        text_color = (*Tint.checkbox_text_true(), 1.0)
        # icons are available as a dropdown! Use f"{}"  is encouraged
        icon = f""
    else:
        bg_color = pack_color(*Tint.checkbox_bg(), 1.0)
        text_color = (*Tint.checkbox_text(), 0.45)
        icon = f""

    # [tint=(0.989, 0.17, 0.497), show_tint=True]
    label = f"{icon} {input_value}"
    icon_w = imgui.calc_text_size(icon)[0]
    label_w = imgui.calc_text_size(label)[0]

    leftover = draw_state.abs_left + draw_state.width - 10 - cursor_start
    cell_width = min(draw_state.content_width, leftover)


    avail = min(draw_state.width - 18, cell_width)
    full_width = left_margin + text_inset * 2 + label_w
    compact = full_width > avail
    
    if compact:
        width = 30
    else:
        width = full_width

    imgui.dummy(width, 21)
    
    # draw list should not be abbrivated ds
    draw_list = imgui.get_window_draw_list()
    outline_color = pack_color(*Tint.checkbox_outline(), 1.0)

    # This is an example of a comment I don't really like. Documenting what something 
    # does is fine but if that's needed it usually means the code is written poorly.
    # Ideally the code should be easy enough that it's obvious what it does.
    # Comments that explain how to change the code, placed precisely in places that
    # may plausiblly be changed in the future is highly discouraged. 
    
    # Align the box to the right edge of the value cell: the leftover space
    # between the content width and the box's own width becomes the left offset.
    # When the box is wider than the cell this goes negative, pinning the right
    # edge and letting the box grow left over the header - so when there's
    # absolutely no room it starts overlapping the header rather than overflowing.
    right_offset = cell_width - width
    box_left = imgui.get_cursor_pos_x() + left_margin + right_offset
    box_right = imgui.get_cursor_pos_x() + width + right_offset
    
    # abs_top and abs_left should be used when the top/left of the view port is needed
    box_top = draw_state.abs_top
    box_bottom = draw_state.abs_top + draw_state.content_height

    # Draw list is always prefered for perforance
    draw_list.add_rect_filled(box_left, box_top, box_right, box_bottom,
                              rounding=4, col=bg_color)
    draw_list.add_rect(box_left, box_top, box_right, box_bottom,
                       rounding=4, col=outline_color, thickness=1.5)

    # The hit target is the box itself, not the whole value cell - the rest of
    # the row (the header) stays usable for its own drag-and-drop. _bounding_hovered
    # keeps occlusion/z-order correct (no clicking through an overlapping window);
    # the rect test narrows it to the drawn box.
    box_hovered = draw_state._bounding_hovered and imgui.is_mouse_hovering_rect(
        box_left, box_top, box_right, box_bottom)

    if box_hovered:
        hover_color = pack_color(*Tint.checkbox_bg_hovered(), 0.2)
        draw_list.add_rect_filled(box_left, box_top, box_right, box_bottom,
                                  rounding=4, col=hover_color)

    if compact:
        # Center just the icon inside the square (which spans [left_margin, width]).
        # Don't abriviate variables names. Use full box_width
        box_w = width - left_margin
        imgui.same_line(left_margin + (box_w - icon_w) / 2.0 + right_offset + 1)
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 2)
        imgui.text_colored(icon, *text_color)
    else:
        imgui.same_line(text_inset + left_margin + right_offset)
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() + 2)
        imgui.text_colored(label, *text_color)
    
    # Melty click events are preferable to imgui ones. left_mouse_down, left_mouse_clicked, left_mouse_drag etc 
    # Are injected automatically when those arguments are present in a @render_func signature.
    if box_hovered and imgui.is_mouse_clicked(0):
        return True, not input_value
    else:
        return False, input_value


@render_func(is_default_for=(str), shadow=False, wrap_text=False, show_bg=False, is_tree=False, wrap=False,
             show_header=False,
             show_add_delete=False, show_name=False, use_cache=True,
             disable_scroll=True, min_width=30, with_header=draw_header, temp=True)
def text(input_value: str, wrap, wrap_text=False, text_color=(1, 1, 1), draw_state=None, font=None):
    _font_pushed = False
    if font is not None and Core.melty.font_mgr is not None:
        _font_handle = Core.melty.font_mgr.get(font)
        if _font_handle is not None:
            imgui.push_font(_font_handle)
            _font_pushed = True

    text_size = imgui.calc_text_size(str(input_value), wrap_width=draw_state.content_width)
    if wrap_text and text_size[1] > imgui.get_text_line_height() * 4 and not wrap:
        imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
        imgui.push_style_color(imgui.COLOR_TEXT, text_color[0], text_color[1], text_color[2], 1.0)
        imgui.text_wrapped(str(input_value))
        imgui.pop_style_color()
        imgui.pop_text_wrap_pos()

    else:
        if text_color is not None:
            imgui.text_colored(str(input_value), text_color[0], text_color[1], text_color[2], 1.0)
        else:
            imgui.text(str(input_value))

    if font is not None:
        imgui.pop_font()

    return False, input_value


@render_func(is_default_for=(str), shadow=False, show_bg=False, wrap=False, selectable=False,
             is_tree=False, show_add_delete=False, use_cache=True, min_height=20,
             disable_scroll=True, with_header=draw_header, temp=True)
def draw_str(input_value: str, draw_state, editable=True, wrap=False, min_width=110, immediate_return=False, alpha=1.0):
    from meltygui.view.text_view import draw_text

    if not editable:
        imgui.push_style_var(imgui.STYLE_ALPHA, alpha)

        text_size = imgui.calc_text_size(str(input_value), wrap_width=draw_state.content_width)
        imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
        imgui.text_wrapped(str(input_value))
        imgui.pop_text_wrap_pos()

        imgui.pop_style_var(1)
        return False, input_value

    some_int = 29
    line_count = input_value.count('\n') + 1
    line_height = imgui.get_text_line_height()
    text_height = imgui.calc_text_size(str(input_value))[1] + line_height * 2
    if line_count == 1:
        padding = imgui.get_style().frame_padding.y
        height = imgui.get_text_line_height() + padding

    else:
        text_bottom = draw_state.abs_top + text_height
        clamped_bottom = text_bottom
        height = clamped_bottom - draw_state.abs_top

    show_controls = True

    if not show_controls:
        imgui.push_style_var(imgui.STYLE_ALPHA, 0)

    if False:
        if not wrap:
            item_width = draw_state.content_width - 1
        else:
            item_width = min_width

        if immediate_return:
            imgui.set_next_item_width(item_width)
            changed, value = imgui.input_text("##str", str(input_value))
        else:
            imgui.set_next_item_width(item_width)
            changed, value = imgui.input_text("##str", str(input_value),
                                              flags=imgui.INPUT_TEXT_ENTER_RETURNS_TRUE)
    else:
        # disable scrolling
        changed, value = draw_text(str(input_value), name=draw_state.name + "##innder", show_bg=True,
                                   editable=True, with_header=draw_header, width=draw_state.content_width - 10,
                                   show_name=False, is_tree=False, temp=True,
                                   # A VALUE field holds prose/data, not code -
                                   # the auto-suggestion popup is noise here
                                   # (params-panel input boxes especially).
                                   autocomplete=False)

    if not show_controls:
        imgui.pop_style_var(1)

    if changed:
        return True, value
    return changed, value


@render_func
def draw_float_ctx(input_value):
    imgui.text('Float content menu')
    imgui.dummy(30, 30)
    draw_float(0.0, name="test")
    imgui.text(f"WxH {input_value.width} {input_value.height}")
    imgui.text(f"Content width {input_value.content_width} {input_value.height}")

    imgui.text(f"Abs Left/Top {input_value.abs_left} {input_value.abs_top}")
    imgui.text(f"Header WxH {input_value.header_width} {input_value.header_height}")

    draw_list: _DrawList = imgui.get_overlay_draw_list()
    draw_list.add_rect(upper_left_x=input_value.abs_left, upper_left_y=input_value.abs_top,
                       lower_right_x=input_value.abs_left + input_value.width,
                       lower_right_y=input_value.abs_top + input_value.height,
                       col=pack_color(1, 0, 0, 0.5), thickness=1.0)


def begin_number_field(draw_state, wrap, min_width):
    """The shared look of the numeric value fields (draw_float / draw_int):
    draw_bool's chip palette on imgui's drag widget, so every primitive value
    in a row of settings reads as one family. Returns the field width; pair
    with ``end_number_field`` after the widget."""
    from meltygui.utils.render_utils import push_style_var
    # Change the numeric field's shape here; the height matches draw_bool's chip.
    field_rounding = 4.0
    field_padding = (6.0, 2.5)
    # How strongly the chip's hover / pressed wash shows over the field.
    hovered_wash = 0.35
    active_wash = 0.6
    # Lines the field's right edge up with draw_str's box and draw_bool's chip.
    field_right_inset = 2.0

    width = min_width if wrap else max(min_width, draw_state.content_width - field_right_inset)
    field_bg = Tint.checkbox_bg()
    wash = Tint.checkbox_bg_hovered()

    def washed(amount):
        return tuple(bg + (over - bg) * amount for bg, over in zip(field_bg, wash))

    push_style_var(imgui.STYLE_FRAME_ROUNDING, field_rounding)
    push_style_var(imgui.STYLE_FRAME_PADDING, field_padding)
    imgui.push_style_color(imgui.COLOR_FRAME_BACKGROUND, *field_bg, 1.0)
    imgui.push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED, *washed(hovered_wash), 1.0)
    imgui.push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE, *washed(active_wash), 1.0)
    imgui.push_style_color(imgui.COLOR_TEXT, *Tint.checkbox_text_true(), 1.0)
    imgui.set_next_item_width(width)
    return width


def end_number_field():
    """Pop ``begin_number_field``'s style and ring the widget just drawn with
    the chip outline."""
    # Change the ring here (draw_bool's chip uses the same outline tint).
    field_rounding = 4.0
    outline_thickness = 1.5
    imgui.pop_style_color(4)
    imgui.pop_style_var(2)
    field_min, field_max = imgui.get_item_rect_min(), imgui.get_item_rect_max()
    imgui.get_window_draw_list().add_rect(
        field_min.x, field_min.y, field_max.x, field_max.y,
        pack_color(*Tint.checkbox_outline(), 1.0),
        rounding=field_rounding, thickness=outline_thickness)


@render_func(is_default_for=(float), shadow=False, use_cache=False, wrap=False, tint=(0.0, 0.62, 0.72),
             is_tree=False, with_header=draw_header, align_header=True, temp=True)
def draw_float(input_value: float,
               draw_state,
               wrap=False,
               min_width=80,
               max_height=100, min_height=20,
               min_value=-98.703,
               max_value=99.264,
               speed=0.0042):
    begin_number_field(draw_state, wrap, min_width)
    changed, value = imgui.drag_float("", input_value,
                                      format='%.3f',
                                      change_speed=speed,
                                      min_value=min_value,
                                      max_value=max_value)
    end_number_field()

    if changed:
        return True, value

    return False, input_value


@render_func(shadow=False, use_cache=False, wrap=False, is_tree=False,
             with_header=draw_header, align_header=True, temp=True, tint=(0.071, 0.354, 0.511))
def draw_button(input_value="", draw_state=None, label="", tint=(1.0, 1.0, 1.0, 1.0), min_width=80,
                min_height=14, wrap=False, height=23):
    """A VALUE-ROW button — draw_float's shape with a button where the slider
    sits, so it composes with the standard header (name label, tint, layout)
    that `def button` fights. `changed` IS the click; input_value passes
    through untouched (the caller acts on the click, e.g. the info tab's +
    stamping a param into a source).

    Styled from draw_state.tint (raw imgui.button's stock style reads pure
    white in the meltygui theme). The label wraps: it's drawn OVER a label-less
    button with a text-wrap pos, and the button height grows to fit."""
    from meltygui.core.cache.tile_cache import add_shadow

    w = min_width if wrap else (draw_state.content_width or min_width)
    _t = tint
    r, g, b = (_t[:3] if isinstance(_t, (tuple, list)) and len(_t) >= 3
               else (0.45, 0.47, 0.55))
    pad_x, pad_y = 6, 2
    _lbl = str(label)
    ts = imgui.calc_text_size(_lbl, wrap_width=max(10.0, w - pad_x * 2))
    h = height or max(min_height, ts[1] + pad_y * 2)

    imgui.push_style_color(imgui.COLOR_BUTTON, r * 0.28, g * 0.28, b * 0.28, 0.6)
    imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, r * 0.42, g * 0.42, b * 0.42, 0.8)
    imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE, r * 0.55, g * 0.55, b * 0.55, 0.95)
    pos = imgui.get_cursor_screen_pos()
    # Shadow on the button rect only - the default shadow (decorator
    # shadow=True) would mark the whole value row, name label included.
    add_shadow((pos[0], pos[1], w, h),
               corner_radius=imgui.get_style().frame_rounding)
    clicked = imgui.button("##btn", width=w, height=h)
    imgui.pop_style_color(3)

    # Wrapped label over the button; restore the flow cursor after.
    flow = imgui.get_cursor_screen_pos()
    imgui.set_cursor_screen_pos((pos[0] + pad_x, pos[1] + pad_y))
    imgui.push_text_wrap_pos(imgui.get_cursor_pos_x() + w - pad_x * 2)
    imgui.text(_lbl)
    imgui.pop_text_wrap_pos()
    imgui.set_cursor_screen_pos(flow)
    return clicked, input_value


@render_func(is_default_for=(int), shadow=False, use_cache=False, wrap=False, tint=(0.0, 0.45, 0.85),
             is_tree=False, with_header=draw_header, align_header=True, temp=True)
def draw_int(input_value: int, draw_state=None, max_height=100, min_height=20,
             min_width=80, wrap=False, min_value=-1000.0,
             max_value=1000.0, speed=0.1, unique=0):
    max_int = 2147483647
    if input_value < max_int:
        begin_number_field(draw_state, wrap, min_width)
        changed, value = imgui.drag_int("##int", input_value,
                                        change_speed=speed,
                                        min_value=min_value,
                                        max_value=max_value)
        end_number_field()
        if changed:
            return True, value

        return changed, value
    return False, input_value


@render_func(tint=(0.18, 0.46, 0.67), use_cache=True, shadow=False,
             with_header=draw_header, header_same_line=True, wrap=False,
             min_width=80, min_height=22, auto_resize=False)
def draw_int_slider(input_value: int, draw_state=None, min_value=0, max_value=100,
                    left_mouse_down=None, left_mouse_drag=None):
    """A bounded integer edited through the view's injected pointer events."""
    left, top = imgui.get_cursor_screen_pos()
    width = max(1.0, draw_state.content_width)
    height = 22.0
    draw_state.event_rect(('left_mouse_down', 'left_mouse_drag'),
                          (left, top, left + width, top + height))
    value = max(min_value, min(max_value, input_value))
    event = left_mouse_drag if left_mouse_drag is not None else left_mouse_down
    if event is not None and max_value > min_value:
        fraction = max(0.0, min(1.0, (event.x - left) / width))
        value = round(min_value + fraction * (max_value - min_value))
    fraction = (value - min_value) / max(1, max_value - min_value)
    draw_list = imgui.get_window_draw_list()
    draw_list.add_rect_filled(left, top + 4, left + width, top + height - 4,
                              pack_color(*Tint.checkbox_bg(), 1.0), rounding=3)
    draw_list.add_rect_filled(left, top + 4, left + width * fraction, top + height - 4,
                              pack_color(*Tint.checkbox_bg_selected(), 1.0), rounding=3)
    label = str(value)
    label_width, label_height = imgui.calc_text_size(label)
    draw_list.add_text(left + (width - label_width) / 2,
                       top + (height - label_height) / 2,
                       pack_color(*Tint.checkbox_text(), 1.0), label)
    imgui.dummy(width, height)
    changed = event is not None and value != input_value
    return changed, value if changed else input_value


@render_func(is_default_for=Enum, is_tree=False, shadow=False, align_header=False,
             selectable=False, show_add_delete=False,
             parent_show_add_delete=False, with_header=draw_header, temp=True)
def draw_enum(input_value: Enum, draw_state=None, unique=0, style_manager=None, enum_tint=(0.3, 0.3, 0.3)):
    # Delegate to draw_tab_bar so enums get its wrapping + styling for free.
    # Enums are single-select: pass the current value as the lone selection and
    # render every member as a tab; names are the prettified member names.
    from meltygui.view.dropdown_view import draw_dropdown
    from meltygui.view.tab_view import draw_tab_bar

    options = list(input_value.__class__)

    if len(options) > 4:
        changed, selection = draw_dropdown(input_value, collection=options, show_header=False,
                                           name=f"{input_value.__class__.__name__}##{unique}enum")

        if changed:
            return True, selection
    else:

        names = [opt.name.replace("_", " ").capitalize() for opt in options]
        changed, selected = draw_tab_bar([input_value], collection=options, names=names, name=f"{unique}_enum",
                                         wrap=True,
                                         z_offset=-1, rounding=5, as_toggles=False, bg_offset=-3)
        if changed and selected:
            return True, selected[0]
    return False, input_value


@render_func(use_cache=True, show_header=True, selectable=False, with_header=draw_header)
def draw_single(input_value: any, view_func=None, mode: any = None, **kwargs):
    changed, return_val = view_func(input_value, mode=mode)
    return changed, return_val


@render_func(use_cache=False, show_header=True, max_height=30, selectable=False, with_header=draw_header)
def draw_blank(input_value: any, **kwargs):
    return False, None
