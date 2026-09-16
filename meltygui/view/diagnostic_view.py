"""Diagnostic view functions and supporting definitions."""
from meltygui.core.conversion.path_finder import Pending
from meltygui.hdr_color import pack_color
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
from meltygui.state.core_enums import PendingAction
from meltygui.state.core_undo import NavUndo
from meltygui.state.new_core_model import DrawState
from meltygui.view.header_view import draw_header
from meltygui_imgui.core import _DrawList
from pathlib import Path
import meltygui_imgui as imgui
import threading
import types


@render_func(is_default_for=(types.FrameType), use_cache=True, tint=(0.6, 0.2, 0.0),
             header_same_line=False, show_bg=False, align_header=False, closed=False,
             shadow=True, selectable=False, wrap=False, with_header=draw_header,
             indent_size=5, searchable=True, shaodw=False, bg_offset=3)
def draw_frame(input_value: types.FrameType, draw_state, **kwargs):
    from meltygui.core.styling.fonts import Font
    from meltygui.view.control_view import button
    from meltygui.view.text_view import draw_text
    from meltygui.core.rendering.render_dispatch import draw_any

    file_name_truncated = Path(input_value.f_code.co_filename).name
    imgui.text(f"{file_name_truncated}:{input_value.f_lineno} in {input_value.f_code.co_name}")

  
    # threaded open_in_intellij pattern the jump-to-calling button uses.    # Jump-to-error: open the frame's source file at the failing line. Same
    if button(f"{file_name_truncated}:{input_value.f_lineno}",
              height=59, value=0.4, saturation=1.5, name="jump_to_frame")[0]:
        from meltygui.utils.jump_to_code import open_in_intellij

        threading.Thread(
            target=open_in_intellij,
            args=(str(input_value.f_code.co_filename),),
            kwargs={"line_number": input_value.f_lineno},
            daemon=True).start()
    # Loop over the frame's local variables, which are the most relevant to debugging.

    draw_text("Locals", name="Locals", show_header=False,
              font=Font.JETBRAINS_MONO_40, bg_offset=3)

    for var_name, var_value in input_value.f_locals.items():
        # Display the variable name and its value.
        imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] + 40, imgui.get_cursor_screen_pos()[1]))
        imgui.begin_group()
        draw_any(var_value, with_header=draw_header, show_header=True, name=var_name, mode=Modes.READ_ONLY)
        imgui.end_group()


@render_func
def draw_draw_state(input_value, **kwargs):
    pass


def render_profiler_time(input_value=None, brief=False, style_manager=None):
    """
    Renders the time taken for a specific operation in the profiler.
    """
    from meltygui.core.styling.global_style import GlobalStyle

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
    dynamic_saturation_factor = GlobalStyle.profiler["object_attr"][
        "dynamic_saturation_factor"]
    dynamic_saturation_offset = GlobalStyle.profiler["object_attr"][
        "dynamic_saturation_offset"]
    saturation = GlobalStyle.profiler["object_attr"]["saturation"]
    value = GlobalStyle.profiler["object_attr"]["value"]
    dynamic_sat = (float(in_ms + dynamic_saturation_offset) * dynamic_saturation_factor)
    text_tint = style_manager.make_color_rgb(*golden_yellow, factor=1.0 - dynamic_sat,
                                             value=min(1.0, max(0, value + dynamic_sat * 0.5)),
                                             alpha=1.0,
                                             saturation_scale=max(0, saturation - dynamic_sat))[:3]
    imgui.text_colored(f"{formatted_value}", *text_tint)
    return False, input_value


@render_func(is_default_for="ImGuiStyleManager", tint=(0.8, 0.7, 0), use_cache=True, with_header=None)
def draw_style_manager(input_val):
    imgui.text("Style Manager")

    return False, input_val


@render_func(show_header=False, show_name=False, show_bg=True, with_header=draw_header)
def draw_debug_label(input_value: str):
    imgui.text(input_value)


def draw_debug(x, y, label, color=(1, 0, 0), size=16):
    draw_list: _DrawList = imgui.get_overlay_draw_list()
    draw_list.add_circle_filled(x, y, size, pack_color(*color, 1.0))
    draw_list.add_text(x + size + 2, y - size / 2, pack_color(*color, 1.0), label)


@render_func(show_bg=True, use_cache=True, shadow=False, with_header=draw_header)
def draw_undo_manager(input_value, **kwargs):
    """Render both undo timelines — edits (input_value is the UndoManager
    class, handed in by @window) and NavUndo's navigation stack — grouped by
    undo step (one user action), newest first. Changes from the same action
    share a group_id and undo together, so we draw a separator between groups
    and indent the changes within each."""
    for title, stack in (("Edits", input_value.stack), ("Navigation", NavUndo.stack)):
        history = list(stack.history)
        group_count = len({c.group_id for c in history})
        imgui.text(f"{title}: {group_count} undo step(s), {len(history)} change(s)"
                   f"  (redo: {len(stack.redo_stack)})")
        prev_gid = None
        shown = 0
        for change in reversed(history):
            if change.group_id != prev_gid:
                imgui.separator()
                prev_gid = change.group_id
            from_val = str(change.old)[:20]  # truncate long values for readability
            to_val = str(change.new)[:20]
            imgui.text(f"  {change.display_name}: {from_val} -> {to_val}")
            shown += 1
            if shown >= 12:
                imgui.text(f"... and {len(history) - shown} more")
                break
        imgui.dummy(1, 8)
    return False, input_value


@render_func(is_default_for=(DrawState), tint=(0.2, 0.6, 0.8), show_bg=True, shadow=False, with_header=None)
def draw_draw_state_info(input_value: DrawState):
    imgui.text(f"DrawState")
    imgui.text(f"Tile ID: {input_value._tile_id}")
    imgui.text(f"Content WxH: {input_value.content_width} x {input_value.content_height}")


@render_func(use_cache=True, with_header=draw_header, show_bg=True, is_default_for=Pending)
def draw_pending(input_value, draw_state=None):
    imgui.text(input_value.originated.__name__)
    imgui.push_text_wrap_pos(draw_state.left + draw_state.content_width)
    imgui.text_wrapped(str(input_value.status))
    imgui.pop_text_wrap_pos()

    return False, input_value


@render_func(use_cache=True, show_header=False, shadow=True)
def pending_window(input_value, button_name, pending=None, draw_state=None,
                   show_revert=False, show_load=False):
    from meltygui.view.control_view import button
    from meltygui.view.text_view import draw_text
    from meltygui.core.layout.cursor_core import same_line

    draw_text(str(pending.status), width=draw_state.width, name="Status", show_bg=True, shadow=False, with_footer=None)
    imgui.dummy(0, 5)

    if button(str(button_name), width=100, height=25)[0]:
        return True, PendingAction.APPLY
    if show_revert:
        same_line()
        if button("Revert", width=100, height=25, color=(0.8, 0.3, 0.3),
                  factor=0.3, value=0.0, text_value=2.0, saturation=0.4)[0]:
            return True, PendingAction.REVERT
    if show_load:
        same_line()
        if button("Load", width=100, height=25, color=(0.3, 0.5, 0.8), factor=0.8)[0]:
            return True, PendingAction.LOAD

    return False, input_value
