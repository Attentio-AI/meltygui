import threading
import types

import imgui

from src.lsd.gl_gui.view.core_conversion import address
from src.lsd.gl_gui.view.core_conversion.address import Address
from src.lsd.gl_gui.view.core_views.core_render import render_func


@render_func(auto_resize=True)
def draw_jump_to(input_value: Address, unique, draw_state=None):
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
        from src.lsd.gl_gui.view.core_conversion.chain_converters import _enclosing_function
        fn = _enclosing_function(str(input_value.path), line_number)

    label = f"{file_name}:{line_number}" if line_number is not None else file_name
    if fn is not None:
        label = f"{fn.__name__}  ({label})"

    # File-header bar: a rounded filled rect spanning the content width, drawn
    # behind the label + jump button. Packed ABGR colors per the codebase idiom.
    from src.lsd.gl_gui.view.core_views.new_core_view import button
    draw_list = imgui.get_window_draw_list()
    x0, y0 = imgui.get_cursor_screen_pos()
    width = draw_state.width if draw_state is not None else 300
    pad_x, pad_y = 8, 5
    row_h = imgui.get_frame_height() + pad_y * 2
    x1, y1 = x0 + width, y0 + row_h
    # Round only the top two corners so the bar reads as a box sitting flush
    # on top of the body below it.
    rounding = 4.0
    top_corners = imgui.DRAW_ROUND_CORNERS_TOP
    draw_list.add_rect_filled(x0, y0, x1, y1, (230 << 24) | (62 << 16) | (52 << 8) | 44, rounding, top_corners)
    draw_list.add_rect(x0, y0, x1, y1, (255 << 24) | (90 << 16) | (78 << 8) | 66, rounding, top_corners)

    # Label (vertically centered against the button frame) + icon jump button.
    imgui.set_cursor_screen_pos((x0 + pad_x, y0 + pad_y))
    imgui.align_text_to_frame_padding()
    imgui.text(label)
    imgui.same_line()
    if button(f"{folder_icon} Open##jump_to{unique}", height=22, name=f"{unique}_jump")[0]:
        from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij

        threading.Thread(
            target=open_in_intellij,
            args=(str(input_value.path),),
            kwargs={"line_number": line_number},
            daemon=True,
        ).start()

    # Reserve the bar's full height so following content doesn't overlap it.
    imgui.set_cursor_screen_pos((x0, y1 + 2))
