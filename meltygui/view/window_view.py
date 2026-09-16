"""Window view functions and supporting definitions."""
from meltygui.core.melty import ManagedWindow
from meltygui.core.core_render import render_func
from meltygui.core.core_decoration import Core
from meltygui.view.header_view import draw_header
import meltygui_imgui as imgui


@render_func(is_default_for=ManagedWindow, is_tree=False, show_name=False, use_cache=True,
             shadow=False, show_bg=False, selectable=False, show_add_delete=False,
             show_tint=False, wrap=False, with_header=draw_header, temp=True)
def draw_managed_window(input_value, name, draw_state, mouse_down=False, selectable=False, **kwargs):
    from meltygui.view.collection_view import draw_tuple
    from meltygui.view.control_view import button

    try:
        window_draw_state = input_value.draw_state
    except Exception as e:
        imgui.text(f"Error accessing draw_state: {e}")
        return False, input_value

    window_input_value = input_value.input_value
    name = window_draw_state.name

    start_cursor = imgui.get_cursor_screen_pos()
    imgui.dummy(4, 20)
    imgui.same_line()

    if not window_draw_state.persistent and not window_draw_state.seen and window_draw_state.closed:
        Core.melty.delete_window(window_draw_state)

    window_tint = None

    if hasattr(window_input_value, 'tint') and window_input_value.tint is not None:
        changed, new_tint = draw_tuple(window_input_value.tint, name="")
        if changed:
            window_input_value.tint = new_tint
            window_draw_state.tint = window_input_value.tint
        draw_state.tint = window_draw_state.tint
        window_tint = window_input_value.tint

    elif window_draw_state.tint is not None:
        changed, new_tint = draw_tuple(window_draw_state.tint, name="")
        if changed:
            window_draw_state.tint = new_tint
        draw_state.tint = window_draw_state.tint
        window_tint = window_draw_state.tint

    imgui.same_line()

    if mouse_down:
        window_draw_state.closed = not window_draw_state.closed

    button_height = 31
    target_spacing = 81
    target_tint_value = 0.103

    if name == "Window Manager":
        button(f"{name}", color=(0, 0, 0, 0),
               saturation=1.3, width=130, height=button_height)[0]
        return

    # Pass the search-match flags (set by draw_collection for this key) through
    # to the name button so it can draw the find highlight — the visible row is
    # this button, not a header.
    _search_match = kwargs.get("search_match", False)
    _search_current = kwargs.get("search_current", False)
    if window_draw_state.closed:
        if button(f"{name}", color=window_tint, z_offset=-4, tint_value=0.035, factor=0.92, text_value=0.305,
                  saturation=0.872, width=draw_state.content_width - target_spacing, height=button_height,
                  search_match=_search_match, search_current=_search_current)[0]:
            window_draw_state.closed = False
            this_window_right = draw_state.abs_left + draw_state.width
            # summon_window does the anchor math AND clips the result to the
            # screen, so a row low in the list can't put the window with its
            # bottom below the bottom of the screen.
            Core.melty.summon_window(window_draw_state, this_window_right + 10, draw_state.abs_top)
            Core.melty.cache.invalidate_up_by_obj(input_value)
    else:
        if \
        button(f"{name}", saturation=1.315, z_offset=4, color=window_tint, factor=0.659, value=-0.205, text_value=1.357,
               width=draw_state.content_width - target_spacing, height=button_height,
               search_match=_search_match, search_current=_search_current)[0]:
            window_draw_state.closed = True

    imgui.same_line()

    if window_tint is None or not isinstance(window_tint, tuple) or len(window_tint) < 3:
        window_tint = (2.558, 0.5, 0.5)

    imgui.set_cursor_screen_pos((draw_state.abs_left + draw_state.content_width - 20, draw_state.abs_top))
    target_icon = ""  # Target icon (FontAwesome Unicode)
    if \
    button(f"{target_icon}##{name}", height=button_height, color=window_tint, z_offset=2, tint_value=target_tint_value,
           factor=0.799,
           saturation=0.764, shadow=False)[0]:
        this_window_right = draw_state.abs_left + draw_state.width
        from_zero_x = window_draw_state.abs_left - window_draw_state.window_pos[0]
        from_zero_y = window_draw_state.abs_top - window_draw_state.window_pos[1]
        window_draw_state.window_pos = (this_window_right + 10 - from_zero_x, draw_state.abs_top - from_zero_y)
        Core.melty.move_window_to_front(window_draw_state)
        Core.melty.cache.invalidate_up_by_obj(input_value)

    imgui.set_cursor_screen_pos(start_cursor)

    live_tint = (0.409, 0.1, 0.1)

    if window_draw_state.live:
        fa_live_icon = ""
        imgui.text_colored(fa_live_icon, *(live_tint))
        imgui.same_line()
