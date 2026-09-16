"""Dropdown core functions and supporting definitions."""
from meltygui.core.toggles import Toggles
from meltygui.core.melty import Melty
from meltygui.core.core_decoration import Core


def _close_menu(draw_state, state):
    """Close whatever menu is showing: release the popover slot, collapse
    that menu's paths, hand text focus back to whoever had it."""
    from meltygui.core.glfw_utils import request_render

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


def _ds_in_subtree(node, ancestor, max_depth=64):
    """Use the same window ancestry as focus dispatch, including popovers."""
    return ancestor.id in Core.melty.ancestor_closure([node])


def _dd_set_cursor(root_state, cursor_path, is_branch, menu_ds=None):
    """Point the highlight at `cursor_path` and derive the open path from it: a
    branch expands its own sub-menu, a leaf collapses back to its parent level.
    This is the single writer for both hover and keyboard, so they stay in sync.

    When the OPEN path changes, the containing menu's row tiles must re-run:
    a branch row stamps `closed` on its submenu window only when its body
    actually runs, and the blit cache has no kwargs key — a clean row tile
    blit-skips even though its open_path input changed, leaving the submenu
    stamped open forever (one leaked submenu per branch row swept). Keyboard
    nav never leaked because begin_frame cascades invalidate_up from the menu
    tile on every nav key; this is the hover-side equivalent. `menu_ds` is the
    level's menu-window draw_state (row tiles are its direct tile children)."""
    from meltygui.model.dropdown_model import _dd_as_tuple

    if root_state is None:
        return
    new_cursor = tuple(cursor_path)
    new_open = tuple(cursor_path) if is_branch else tuple(cursor_path[:-1])
    old_open = _dd_as_tuple(root_state.open_path)
    if (new_cursor == _dd_as_tuple(root_state.cursor_path)
            and new_open == old_open):
        return
    root_state.cursor_path = new_cursor
    root_state.open_path = new_open
    if new_open != old_open:
        _dd_invalidate_rows(root_state, menu_ds)


def _dd_invalidate_rows(root_state, menu_ds=None):
    """Cascade-invalidate a menu window's tile subtree so every row body
    re-runs and re-stamps `closed` on its submenu from the current open_path.
    Without a menu_ds at hand (close paths), resolve the root menu through the
    search box tile — its parent_window is the root menu. bypass_clip so a
    branch row scrolled out of the menu viewport is cleaned too (otherwise
    scrolling it back in would revive its stale-open submenu). Submenu windows
    are tile ROOTS (not tile children of their spawner rows), so this reaches
    rows, not window interiors — which is all closing needs."""
    from meltygui.core.glfw_utils import request_render

    if menu_ds is None and root_state is not None:
        box_tile = getattr(root_state, "_search_box_tile", None)
        if box_tile is not None:
            box_ds = Melty.cache.key_to_draw_state.get(box_tile)
            menu_ds = getattr(box_ds, "parent_window", None) if box_ds is not None else None
    if menu_ds is None or getattr(menu_ds, "_tile_id", None) is None:
        return
    from meltygui.core.invalidation_tracker import Note
    Melty.cache.invalidate_up(menu_ds._tile_id, force=True, bypass_clip=True,
                              note=Note(name="dd open_path change", tint=(1, 0.6, 0.2)))
    request_render()


def _dd_scroll_cursor_into_view(menu_ds, row_index, row0_offset=0.0, pitch=None):
    """Nudge a menu window's scroll_offset the minimal amount so keyboard-cursor
    row `row_index` is fully visible. Rows are a fixed _DD_ROW_H pitch drawn
    flush from the content origin in the manual-loop path; `row0_offset` covers
    a level that draws chrome above its rows. Stateless — pure geometry from
    the live draw_state, clamped to the wrapper-published _max_scroll_y so this
    writer never fights core_render's own clamp."""
    from meltygui.core.glfw_utils import request_render

    if menu_ds is None or row_index is None or row_index < 0:
        return
    pitch = Toggles.Dropdown.row_height if pitch is None else pitch   # the usage picker's rows are taller
    view_h = menu_ds.abs_clipped_height - menu_ds.header_height - menu_ds.footer_height
    sx, sy = menu_ds.scroll_offset
    row_top = row0_offset + row_index * pitch
    row_bot = row_top + pitch
    new_sy = sy
    if view_h > 0 and row_bot > new_sy + view_h:  # below the viewport: minimal scroll down
        new_sy = row_bot - view_h
        max_y = menu_ds._max_scroll_y
        if max_y is not None:
            new_sy = min(new_sy, max_y)
    if row_top < new_sy:  # above the viewport (top-centers a tiny view)
        new_sy = row_top
    new_sy = max(0.0, new_sy)
    if new_sy != sy:
        menu_ds.scroll_offset = (sx, new_sy)
        # Own the repaint edge: scroll is BAKED into the blit capture at render
        # time and only the wheel-event callback repaints on scroll change, so a
        # clean tile would replay the old-offset capture (the caller's repaint gate
        # doesn't always fire - e.g. a snap-to-top when the filtered list is
        # value-identical). Change-edge-gated by `new_sy != sy` above, never
        # per-frame. bypass_clip: the popup can protrude outside its parent.
        if menu_ds._tile_id is not None:
            from meltygui.core.invalidation_tracker import Note
            Melty.cache.invalidate_up(menu_ds._tile_id, force=True, bypass_clip=True,
                                      note=Note(name="dd scroll-into-view", tint=(1, 0.6, 0.2)))
        request_render()


def _dd_pick(root_state, path):
    """Keep a selection until the owning dropdown consumes it.

    Submenus render as independent floating windows, often after their parent
    has already drawn. A one-frame return value cannot reliably cross that
    boundary; the shared dropdown state owns the pending selection.
    """
    from meltygui.core.glfw_utils import request_render

    root_state._picked_path = tuple(path)
    root_state._pending_pick = tuple(path)
    owner = Melty.popover_focused_ds
    if owner is not None:
        owner.invalidate_up()
    request_render()


def _dd_close(root_state):
    """Reset popover state on close: collapse the open/cursor paths, clear the
    search query, and release the search box's text focus if it held it."""
    from meltygui.model.dropdown_model import _dd_as_tuple

    if root_state is None:
        return
    menu = getattr(root_state, "_menu_ds", None)
    if menu is not None:
        menu.closed = True
    root_state._pending_pick = None
    open_was = _dd_as_tuple(root_state.open_path)
    root_state.open_path = ()
    root_state.cursor_path = ()
    root_state.search_query = ""
    root_state.search = ""
    root_state._focus_search = 0
    root_state._had_focus = False
    # A branch row left stamped open would blit-skip on reopen and revive its
    # submenu window even though open_path was reset - dirty the rows now (the
    # flags persist while the menu is hidden) so the first reopen render
    # re-stamps every submenu closed.
    if open_was:
        _dd_invalidate_rows(root_state)
    box_tile = getattr(root_state, "_search_box_tile", None)
    tf = Melty.text_focused_ds
    if tf is not None and box_tile is not None and getattr(tf, "_tile_id", None) == box_tile:
        Melty.text_focused_ds = None


def _dd_handle_keys(collection, root_state, search="", text_focused=False):
    """Arrow-key navigation while the popover is open. Up/Down move within the
    current level, Right (or Enter on a branch) descends, Left collapses to the
    parent, Enter on a leaf picks it. Returns the picked leaf value, or
    UNSET_VALUE when nothing was chosen this frame. Reads the GLFW-callback key
    queue so it works without the menu being hovered."""
    from meltygui.core.cache_tree import UNSET_VALUE
    from meltygui.model.dropdown_model import _dd_as_tuple
    from meltygui.model.dropdown_model import _dd_rows_at
    from meltygui.core.glfw_utils import request_render
    import meltygui.core.window_api as glfw

    if root_state is None:
        return UNSET_VALUE
    keys = list(Core.melty.frame_key_events)

    def pressed(*codes):
        return any(k in codes for k, _ in keys)

    down = pressed(glfw.KEY_DOWN)
    up = pressed(glfw.KEY_UP)
    right = pressed(glfw.KEY_RIGHT)
    left = pressed(glfw.KEY_LEFT)
    enter = pressed(glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
    # While typing a query, Left/Right are the search box's text cursor, not tree
    # navigation, so the dropdown doesn't double-act on them.
    if text_focused and search:
        left = right = False
    if not (down or up or right or left or enter):
        return UNSET_VALUE

    # A nav key fired: switch to keyboard-select mode so hover stops moving the
    # cursor until the mouse actually moves again (cleared in draw_dropdown).
    root_state._kbd_mode = True

    # Navigation level = the cursor's parent; rows = its (search-filtered)
    # siblings. Fall back to the root level if the cursor path went stale.
    cursor = _dd_as_tuple(getattr(root_state, "cursor_path", ()))
    level = cursor[:-1]
    rows = _dd_rows_at(collection, level, search)
    if not rows:
        level = ()
        rows = _dd_rows_at(collection, level, search)
        cursor = ()
    if not rows:
        return UNSET_VALUE

    level_keys = [r[0] for r in rows]
    had_cursor = bool(cursor) and cursor[-1] in level_keys
    idx = level_keys.index(cursor[-1]) if had_cursor else 0

    # Left collapses the current sub-menu and highlights its parent row.
    if left and level:
        root_state.cursor_path = tuple(level)
        root_state.open_path = tuple(level[:-1])
        request_render()
        return UNSET_VALUE

    # From no selection, the first Up/Down just focuses on the first row; otherwise
    # it steps (wrapping). Right/Enter act on whatever row is current.
    if had_cursor:
        if down:
            idx = (idx + 1) % len(rows)
        elif up:
            idx = (idx - 1) % len(rows)
    key, value, label, is_branch = rows[idx]
    new_cursor = tuple(level) + (key,)

    # Right / Enter on a branch descends into its first visible child.
    if (right or enter) and is_branch:
        kids = _dd_rows_at(collection, new_cursor, search)
        if kids:
            ck, cv, _cl, cbr = kids[0]
            _dd_set_cursor(root_state, new_cursor + (ck,), cbr)
            request_render()
            return UNSET_VALUE

    if enter and not is_branch:
        _dd_set_cursor(root_state, new_cursor, False)
        root_state._picked_path = tuple(new_cursor)
        return value

    _dd_set_cursor(root_state, new_cursor, is_branch)
    request_render()
    return UNSET_VALUE


def _dd_update_menu_size(state, menu_ds, min_width=None, max_height=None):
    from meltygui.core.glfw_utils import request_render
    from meltygui.view.dropdown_view import _dd_menu_fit

    current = (menu_ds.width, menu_ds.height)
    last_fit = getattr(state, "_menu_fit", None)
    if (getattr(menu_ds, "_initial_window_size", None) is not None
            and last_fit is not None and current != last_fit and all(current)):
        state.menu_size = current
    elif getattr(state, "menu_size", None) is None:
        fit = _dd_menu_fit(menu_ds, min_width=min_width, max_height=max_height)
        if fit is not None and fit != current:
            menu_ds.width, menu_ds.height = fit
            request_render()
    state._menu_fit = (menu_ds.width, menu_ds.height)
