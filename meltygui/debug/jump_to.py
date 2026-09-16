import types

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

import meltygui.code.fileref as address
from meltygui.code.fileref import Address
from meltygui.rendering.core_render import render_func


def draw_jump_to(input_value: Address, unique, width=30, error_msg=None,
                 draw_state=None):
    file_name = input_value.path.name if input_value.path is not None else "Unknown file"
    line_number = input_value.start + 1 if input_value.start is not None else None
    # Unicode escape (not a literal string) for the Font Awesome folder icon - a
    # pasted PUA char gets stripped to empty on save, which is why it vanished.
    folder_icon = ""  # FA folder

    # Label with the enclosing function name + line number. Prefer the function
    # already attached to the address (.source); otherwise retrieve it from the
    # line via the cached _enclosing_function helper.
    fn = input_value.source if isinstance(input_value.source, types.FunctionType) else None
    if fn is None and line_number is not None and input_value.path is not None:
        from meltygui.code.chain_converters import _enclosing_function
        fn = _enclosing_function(str(input_value.path), line_number)

    label = f"{file_name}:{line_number}" if line_number is not None else file_name
    if fn is not None:
        label = f"{fn.__name__}  ({label})"

    # File-header bar: a rounded filled rect spanning the content width, drawn
    # behind the label + jump button. Packed ABGR colors per the codebase idiom.
    # When there's an error, the bar grows a second row to hold the message, and
    # both the fill and the outline tint red so the header reads as "this file has
    # a problem".
    draw_list = imgui.get_window_draw_list()
    x0, y0 = imgui.get_cursor_screen_pos()
    pad_x, pad_y = 8, 3
    row_h = imgui.get_frame_height() + pad_y * 2
    msg = str(error_msg).split('\n', 1)[0] if error_msg else None
    msg_row_h = (imgui.get_text_line_height() + 4) if msg else 0
    x1, y1 = x0 + width, y0 + row_h + msg_row_h
    # Round only the top two corners so the bar reads as a box sitting flush
    # on top of the body below it.
    rounding = 4.0
    top_corners = imgui.DRAW_ROUND_CORNERS_TOP
    if msg:
        fill_col = pack_color(70 / 255, 30 / 255, 40 / 255, 235 / 255)    # dark red-tinted fill
        line_col = pack_color(150 / 255, 60 / 255, 70 / 255, 1.0)         # red outline
    else:
        fill_col = pack_color(44 / 255, 52 / 255, 62 / 255, 230 / 255)
        line_col = pack_color(66 / 255, 78 / 255, 90 / 255, 1.0)
    draw_list.add_rect_filled(x0, y0, x1, y1, fill_col, rounding, top_corners)
    draw_list.add_rect(x0, y0, x1, y1, line_col, rounding, top_corners)

    # Row 1: label (vertically centered against the button frame) + icon jump
    # button. flat_button (draw-list + on_action through the EDITOR's
    # draw_state - this bar is drawn inside draw_text's body), not a
    # @render_func button: the old widget re-rendered its full wrapper every
    # editor frame. The measured rect is stashed in the editor's state so its
    # selection pass can null the PRESS inside it (the old button's own
    # draw_state used to claim that press; without the null, clicking Open
    # would also place the caret in the document under the floating bar).
    from meltygui.views.headers import flat_button
    from meltygui.melty import Melty
    imgui.set_cursor_screen_pos((x0 + pad_x, y0 + pad_y))
    _open_label = f"{folder_icon} Open"
    _bw = imgui.calc_text_size(_open_label).x + Melty.px(15)
    _bh = Melty.px(18.0)
    _bx, _by = imgui.get_cursor_screen_pos()
    if draw_state is not None:
        draw_state._jump_btn_rect = (_bx, _by, _bx + _bw, _by + _bh)
    if flat_button(f"{_open_label}##jump_to{unique}", draw_state,
                   view_id=f"jump_open{unique}", width=_bw, height=_bh):
        from meltygui.extensions import open_source as open_in_editor
        open_in_editor(str(input_value.path), line_number=line_number,
                       token=fn.__name__ if fn is not None else None)

    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text(label)

    # Row 2: the full error message, in red, spanning the bar. Truncated to the
    # bar width so a long message can't overflow.
    if msg:
        avail = max(0, width - 2 * pad_x)
        if imgui.calc_text_size(msg).x > avail:
            ch_w = max(1.0, imgui.calc_text_size("x").x)
            keep = max(3, int(avail / ch_w) - 1)
            msg = msg[:keep] + "…"
        imgui.set_cursor_screen_pos((x0 + pad_x, y0 + row_h - 2))
        imgui.text_colored(msg, 1.0, 0.5, 0.46, 1.0)

    # Reserve the bar's full height so following content doesn't overlap it.
    imgui.set_cursor_screen_pos((x0, y1 + 2))
