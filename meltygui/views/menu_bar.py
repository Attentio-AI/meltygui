"""draw_menu_bar — a File / Edit / View… bar over the dropdown's menus.

    menu = {"File": {"Save": save, "Recent": {"a.py": open_a}, "Quit": quit},
            "View": {"Wrap": toggle_wrap}}
    changed, picked = draw_menu_bar(menu, name="menu")

The dict's FIRST level is the bar: one flat_button title per key. Every
value below it is drawn by draw_dd_menu exactly as a dropdown's popover
(nested dicts open sub-menus on hover / Right, rows highlight, the popover
keeps a drag-resized size). A leaf is either a callable — RUN on pick — or a
plain value; either way the pick comes back as ``(True, value)`` so the
caller can react, like a dropdown pick. Immediate mode: pass the dict every
frame, nothing of it is kept.

Open / closed is the dropdowns' single slot — the BAR holds
``Melty.popover_focused_ds`` while one of its menus is showing, so any click
outside the bar and its menus hands the slot on (Melty.clear_focus) and the
next run draws every menu closed. While open the bar also holds
``Melty.text_focused_ds`` so the editors gate off and Esc / arrows / Enter
are the menu's: Up / Down / Right / Enter as in a dropdown, Left at a menu's
root level and Right on a leaf move to the previous / next TITLE, as a native
menu bar does. Hovering another title while a menu is open switches to it.
"""
import traceback
from colorsys import rgb_to_hsv, hsv_to_rgb

from src.lsd.gl_gui import window_api as glfw
import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.core_model.draw_state import DropDownState, MenuBarState
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_views.headers import flat_button
from src.lsd.gl_gui.view.core_views.new_core_view import (
    draw_dd_menu, _dd_close, _dd_handle_keys, _dd_as_tuple, _dd_walk, _dd_menu_fit,
    _DD_MENU_MIN_W, _DD_MENU_MAX_H)


@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False, is_tree=False,
             with_header=None, disable_scroll=True, show_add_delete=False)
def draw_menu_bar(input_value: dict, draw_state, name, unique, menu_bar_state: MenuBarState,
                  bar_height=25.0, title_pad=12.0, title_gap=2.0, **kwargs):
    """The bar: `input_value` is {title: menu}, a menu being what draw_dd_menu
    draws (dict / list, callables or values at the leaves). Returns
    ``(True, picked_value)`` on the frame a leaf is picked (a callable leaf
    has been called by then), else ``(False, input_value)``."""
    # [tint=(0.94, 0.45, 0.25)]
    open_title_alpha = 1.0      # the open title's button background
    # [tint=(0.94, 0.45, 0.25)]
    idle_title_alpha = 0.0      # idle titles are label-only
    titles = list(input_value.keys()) if isinstance(input_value, dict) else []
    state = menu_bar_state
    # The bar draws from the CURRENT tint - the context's (the app's / the
    # enclosing view's) or a caller's tint= the caller pushed - the way
    # every other view's text does. No decorator tint of its own: one used
    # to paint the titles into whatever surrounded them.
    bar_tint = Melty.style_manager.get_tint() or draw_state.tint or (1.0, 1.0, 1.0)
    is_open = Melty.popover_focused_ds is draw_state and state.open_title in input_value

    # The slot moved on without us (a click outside, another menu opened):
    # finish the close so this menu's state and text focus don't linger.
    if state.open_title is not None and not is_open:
        _close_menu(draw_state, state)

    def open_menu(title):
        if state.open_title is not None and state.open_title != title:
            _dd_close(state.menus.get(state.open_title))
        if state.open_title is None:
            # First open of a visit: take the keyboard (editors gate off on
            # Melty.text_focused_ds) and remember whose it was.
            state._prev_text_focus = Melty.text_focused_ds
        Melty.popover_focused_ds = draw_state
        Melty._popover_open_frame = Melty.frame_count      # grace the opening click
        Melty.text_focused_ds = draw_state
        Melty._text_focus_grant_frame = Melty.frame_count
        state.open_title = title
        menu_state = _menu_state(state, title)
        _dd_close(menu_state)
        menu_state._kbd_mode = False
        menu_state._last_mouse = None
        draw_state.invalidate()
        request_render()

    # ── the bar: one flat_button per title, left to right ──
    bar_left, bar_top = draw_state.abs_left, draw_state.abs_top
    mouse_x, mouse_y = imgui.get_mouse_pos()
    bar_hovered = draw_state._bounding_hovered and not Melty.on_drag
    x = bar_left
    title_left = {}
    hovered_title = None
    for title in titles:
        label = str(title)
        text_w = imgui.calc_text_size(label).x
        width = text_w + 2 * title_pad
        title_left[title] = x - bar_left
        hovered = bar_hovered and x <= mouse_x < x + width and bar_top <= mouse_y < bar_top + bar_height
        if hovered:
            hovered_title = title
        showing = is_open and title == state.open_title
        imgui.set_cursor_screen_pos((x, bar_top))
        clicked = flat_button(label, draw_state, view_id=f"menu_bar_{unique}_{label}",
                              width=width, height=bar_height, hovered=hovered,
                              alpha=open_title_alpha if showing else idle_title_alpha,
                              color=bar_tint, shadow=False)
        if clicked:
            if showing:
                _close_menu(draw_state, state)
                is_open = False
            else:
                open_menu(title)
                is_open = True
        x += width + title_gap
    # The bar's own content rect: the titles' row, whatever the popovers do.
    imgui.set_cursor_screen_pos((bar_left, bar_top))
    imgui.dummy(max(1.0, x - bar_left), bar_height)

    # Native menu bar feel: with one menu open, HOVERING another title opens
    # it - on the EDGE (the pointer arrived on a new title), never per frame: a
    # pointer resting on a title would otherwise undo every Left / Right.
    if hovered_title != state._hovered_title:
        state._hovered_title = hovered_title
        if is_open and hovered_title is not None and hovered_title != state.open_title:
            open_menu(hovered_title)

    # A mouse move hands the highlight back to hover (until the next nav key).
    if is_open:
        menu_state = _menu_state(state, state.open_title)
        last = menu_state._last_mouse
        if last is not None and (abs(mouse_x - last[0]) > 0.5 or abs(mouse_y - last[1]) > 0.5):
            menu_state._kbd_mode = False
        menu_state._last_mouse = (mouse_x, mouse_y)

    # ── every menu is a latching popover: called EVERY frame with closed= ──
    # (a window drawn only while open never re-registers on reopen — see
    # draw_context_menu_items). Placed through the CURSOR, never window_pos:
    # the wrapper folds a nested window's window_pos into the bar's content
    # measure, and a bar_height offset would double the bar.
    # Row labels are Tint.dd_text over the menu tint; a dark bar tint made
    # them dim against the popover, so its value is floored like the context
    # menu's (draw_context_menu_items) — raise the floor for brighter labels.
    # [tint=(0.994, 0.872, 0.0)]
    menu_tint_value_floor = 0.5
    hue, saturation, value = rgb_to_hsv(*bar_tint[:3])
    menu_tint = (hsv_to_rgb(hue, saturation, menu_tint_value_floor)
                 if value < menu_tint_value_floor else tuple(bar_tint[:3]))
    picked = None
    cursor = imgui.get_cursor_screen_pos()
    for title in titles:
        items = input_value[title]
        menu_state = _menu_state(state, title)
        showing = is_open and title == state.open_title
        menu_ds = menu_state._menu_ds
        if showing and menu_ds is not None:
            opening = Melty._popover_open_frame == Melty.frame_count
            if opening and menu_state.menu_size is not None:
                menu_ds.width, menu_ds.height = menu_state.menu_size
                menu_state._menu_fit = tuple(menu_state.menu_size)
            if menu_ds.width is None or menu_ds.width < 5:
                menu_ds.width = _DD_MENU_MIN_W
        imgui.set_cursor_screen_pos((bar_left + title_left[title], bar_top + bar_height))
        changed, value, menu_ds = draw_dd_menu(
            items, tint=menu_tint, name=f"{name}##menu_bar_{unique}_{title}",
            closed=not showing, temp=True, shadow=False, auto_resize=False,
            window_pos=(0, 0), max_height=_DD_MENU_MAX_H,
            parent_window=draw_state, swoosh=False, disable_scroll=False,
            show_search=False, root_state=menu_state, path_prefix=(),
            return_extras=True)
        menu_state._menu_ds = menu_ds
        if not showing or menu_ds is None:
            continue
        # Content-fit until the user drag-resizes it (draw_dropdown's rule).
        current = (menu_ds.width, menu_ds.height)
        last_fit = menu_state._menu_fit
        if last_fit is not None and current != last_fit and current[0] and current[1]:
            menu_state.menu_size = current       # the resize handle moved it
            menu_state._menu_fit = current
        elif menu_state.menu_size is None:
            fit = _dd_menu_fit(menu_ds)
            if fit is not None and fit != current:
                menu_ds.width, menu_ds.height = fit
                request_render()
            menu_state._menu_fit = (menu_ds.width, menu_ds.height)
        if changed:
            picked = (title, _dd_as_tuple(menu_state._picked_path), value)
    imgui.set_cursor_screen_pos(cursor)

    # ── keys while open: Esc closes, Left / Right cross titles, the rest is the menu's ──
    if is_open and picked is None:
        keys = [k for k, _mods in Melty.frame_key_events]
        if glfw.KEY_ESCAPE in keys:
            _close_menu(draw_state, state)
            return False, input_value
        title = state.open_title
        menu_state = _menu_state(state, title)
        items = input_value[title]
        cursor_path = _dd_as_tuple(menu_state.cursor_path)
        at_root = len(cursor_path) <= 1
        on_branch = bool(cursor_path) and isinstance(_dd_walk(items, cursor_path), (dict, list))
        step = 0
        if glfw.KEY_LEFT in keys and at_root:
            step = -1
        elif glfw.KEY_RIGHT in keys and not on_branch:
            step = 1
        if step:
            open_menu(titles[(titles.index(title) + step) % len(titles)])
            _menu_state(state, state.open_title)._kbd_mode = True
        else:
            value = _dd_handle_keys(items, menu_state, search="", text_focused=False)
            if value is not UNSET_VALUE:
                picked = (title, _dd_as_tuple(menu_state._picked_path), value)

    # ── a pick: run a callable leaf, close, and report it upstream ──
    if picked is not None:
        title, path, value = picked
        state.selected_path = (title,) + tuple(path)
        _close_menu(draw_state, state)
        if callable(value):
            try:
                value()
            except Exception:
                traceback.print_exc()
        return True, value

    return False, input_value


def _menu_state(state, title):
    """The DropDownState of `title`'s menu, created on first sight."""
    menu_state = state.menus.get(title)
    if not isinstance(menu_state, DropDownState):
        menu_state = state.menus[title] = DropDownState()
    return menu_state


def _close_menu(draw_state, state):
    """Close whatever menu is showing: release the popover slot, collapse
    that menu's paths, hand text focus back to whoever had it."""
    if state.open_title is not None:
        menu_state = state.menus.get(state.open_title)
        _dd_close(menu_state)
        # A picked action may remove this state before its next draw call.
        # Close its surface now rather than waiting for closed=True next frame.
        if menu_state is not None and menu_state._menu_ds is not None:
            menu_state._menu_ds.closed = True
    state.open_title = None
    if Melty.popover_focused_ds is draw_state:
        Melty.popover_focused_ds = None
    if Melty.text_focused_ds is draw_state:
        previous = state._prev_text_focus
        Melty.text_focused_ds = previous if previous is not draw_state else None
        if previous is not None:
            Melty._text_focus_grant_frame = Melty.frame_count
    state._prev_text_focus = None
    draw_state.invalidate()
    request_render()
