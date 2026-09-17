"""Dropdown view functions and supporting definitions."""
from meltygui.core.runtime.paths import debug_log_path
from meltygui.core.runtime.toggles import Toggles
from meltygui.hdr_color import pack_color
from meltygui.core.melty import Melty
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.window_decoration import window
from meltygui.state.new_core_model import DropDownState
from meltygui.core.runtime.toggles import Tint
from meltygui.view.header_view import draw_header
import meltygui_imgui as imgui
from meltygui.core.core_render import SCROLLBAR_MARGIN
from meltygui.core.core_render import SCROLL_BAR_WIDTH_DEFAULT
import traceback


@render_func(use_cache=True, temp=True)
def draw_drop_down_item(input_value, name="", unique=0, shadow=False, draw_state=None, **kwargs):
    from meltygui.view.control_view import button

    hovered = draw_state._bounding_hovered
    clicked, _ = button(name, name=f"{unique}{name}_dd_item", show_bg=True, height=25,
                        shadow=False, hovered=hovered)

    # Hover highlight: paint a translucent wash over this item's own box (its
    # draw_state is the row, so abs_left/abs_top + width/height frame it exactly)
    # straight onto the window draw list so it sits over the button fill.
    if hovered:
        dl = imgui.get_window_draw_list()
        dl.add_rect_filled(draw_state.abs_left, draw_state.abs_top,
                           draw_state.abs_left + draw_state.width,
                           draw_state.abs_top + draw_state.height,
                           pack_color(1, 1, 1, 0.16),
                           rounding=getattr(draw_state, 'corner_radius', 6))

    if clicked:
        return True, name

    return False, input_value


@render_func(use_cache=True, show_bg=False, selectable=False,
             tint=(0.083, 0.10, 0.144),
             is_tree=False, show_name=True, with_header=draw_header)
@window
def draw_dropdown(input_value, collection, name, draw_state, unique, drop_down_state: DropDownState, shadow=True,
                  text_align="left", open_upwards=None, menu_min_width=None, display_label=None, **kwargs):
    """Root of a recursive dropdown. Renders a trigger button showing the current
    selection; clicking it opens the (click-o-open) root popover. Nested dict
    rows inside the popover open their own sub-menus on hover. Returns
    (changed, selected_leaf) when the user picks a value. `open_upwards`
    defaults to choosing the side with room. True forces above, False below;
    `menu_min_width` sets its width floor.

    Open/closed is a single global slot -- ``Melty.popover_focused_ds`` holds the
    draw_state of whichever dropdown's popover is currently shown. Each dropdown
    decides it's open by identity (``popover_focused_ds is draw_state``), so
    opening one popover implicitly closes every other (they all fail the test).
    ``drop_down_state`` is the per-view scratch object the framework re-injects
    every frame; we stash the last picked leaf on it for the trigger label."""
    from meltygui.core.conversion.cache_tree import UNSET_VALUE
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.control_view import button
    from meltygui.model.dropdown_model import _dd_as_tuple
    from meltygui.core.layout.dropdown_core import _dd_close
    from meltygui.core.layout.dropdown_core import _dd_handle_keys
    from meltygui.model.dropdown_model import _dd_label_for_path
    from meltygui.model.dropdown_model import _dd_path_for_value
    from meltygui.model.dropdown_model import _dd_walk
    import meltygui.core.windowing.window_api as glfw

    from meltygui.core.rendering.mode import Mode

    # DEBUG
    # Is THIS dropdown the one whose popover is showing?
    is_open = Melty.popover_focused_ds is draw_state
    _DD_DBG = False  # TEMP: default False for dropdown-close investigation
    if _DD_DBG:
        _pf = Melty.popover_focused_ds
        if is_open or _pf is not None:
            try:
                _mx, _my = imgui.get_mouse_pos()
                _clk = imgui.is_mouse_clicked(0);
                _dn = imgui.is_mouse_down(0)
            except Exception:
                _mx = _my = -1.0;
                _clk = _dn = None
            with open(debug_log_path("dd_debug.log"), "a") as _f:
                _f.write(f"[DD-DBG] f={Melty.frame_count} name={name!r} ds={id(draw_state)} "
                         f"pf={id(_pf) if _pf is not None else None} is_open={is_open} "
                         f"mouse=({_mx:.0f},{_my:.0f}) clk={_clk} down={_dn} "
                         f"sel_path={getattr(drop_down_state, 'selected_path', ())} "
                         f"open_frame={getattr(Melty, '_popover_open_frame', None)}\n")

    # Label shows the last pick (sticky across frames via drop_down_state),
    # falling back to the raw input value.
    # Title shows the LABEL/key of the current selection (e.g. "red"), not the raw
    # value (which may be a tuple/number); selected_label is stamped at pick-time.
    # The caller's input_value is the selection of record: when that MOVES
    # (the caller adopted a pick, a nav-undo replay, a selection carried
    # over from elsewhere), the text follows it - the label/path of the
    # leaf holding that value - instead of whatever selected_label was last
    # stamped. The stamp stays put while input_value holds still (callers
    # passing a static value rely on the last pick showing). Callers used to
    # re-stamp selected_label themselves after the draw: on the pick frame
    # their input was still the OLD value, so the old label went back into
    # the state, and with this body frozen the wrong text sat until a
    # hover repaint. Arriving here runs on the frame the input changed -
    # which is a frame before body runs (the input is the tile's hash).
    if input_value != getattr(drop_down_state, "_dd_last_input", UNSET_VALUE):
        drop_down_state._dd_last_input = input_value
        _in_path = _dd_path_for_value(collection, input_value)
        if _in_path is not None:
            drop_down_state.selected_path = _in_path
            drop_down_state.selected_label = _dd_label_for_path(collection, _in_path)
    _sel_label = getattr(drop_down_state, "selected_label", "") or ""
    current = display_label if display_label is not None else (_sel_label if _sel_label else (str(input_value) if input_value is not None else ""))
    caret = "" if is_open else ""  # fa-chevron-down / fa-chevron-right

    # Compact mode: in a very narrow slot (e.g. an inline table cell) there's no room
    # for the caret + button chrome, so show NOTHING but the selected value. Still a
    # real (bg-less) button, so it stays clickable to open the popover. Threshold is
    # tunable via compact_below (px).
    #
    # The trigger's slot width: an explicit caller `width` wins, only without one
    # do we fall back to the measured content_width. Measurement must never feed
    # back into the trigger size - an open popover inflates content_width, which
    # would flip compact mode off and balloon the trigger (~240px), wrapping the
    # header row it sits in: the disagreement between the drawn size and the
    # measured item rect is exactly what reads as animation jitter.
    _slot_w = kwargs.get("width") or draw_state.content_width
    compact = False
    bg_offset = 4 if is_open else 7

    # A compact trigger hugs its glyph: minimal text pad and a centered label,
    # so a small chevron/icon cell doesn't balloon to full text label width.
    trigger_pad = kwargs.get("text_pad", 6 if compact else 15)
    trigger_align = "center" if compact else text_align
    trigger_w = max(15 if compact else 18, _slot_w)

    # The label must FIT the fixed trigger width in pixels - a long label
    # overflows the button rect and the measured item rect disagrees with
    # the drawn size, the same disagreement the width comment above calls
    # out as height jitter. Ellipsis-trim via a text width, not a
    # character count (glyph widths vary wildly with font/monospace).
    _label_px = max(4.0, trigger_w - trigger_pad * 2)
    if compact:
        # Show the VALUE itself (not the label/key). For a name->glyph dropdown
        # the trigger must hug the glyph, not become the picked value's name.
        drop_down_display_str = _dd_fit_label(
            str(input_value if input_value is not None else current), _label_px)
    else:
        drop_down_display_str = _dd_fit_label(f"{caret} {str(current)}", _label_px)

    # A caller's trigger_height taller than the 25 px slot also moves the popover anchor down.
    trigger_h = (getattr(draw_state, "content_height", 0) or 25) if compact else max(25, kwargs.get("trigger_height", 25))
    # Colour the trigger by the selected item's embedded tint (input_value is the
    # current selection passed by the caller), falling back to the view's tint.
    trigger_tint = _dd_obj_tint(input_value, draw_state.tint)
    # Quiet text dimming (text_toward_bg - the info tab's info rows): a
    # selection WITHOUT an embedded tint fades toward the background, so a
    # tinted one (the active source in yellow) is what draws the eye.
    _ttb = kwargs.get("text_toward_bg", 0.0)
    trigger_text_value = 1.023
    if _ttb and getattr(input_value, "tint", None) is None:
        trigger_text_value = 1.023 * (1.0 - min(max(float(_ttb), 0.0), 1.0))
    trigger_left, trigger_top = imgui.get_cursor_screen_pos()
    clicked, _ = button(drop_down_display_str, name=f"{name}_dd_trigger{unique}", show_bg=False, width=trigger_w,
                        show_button_bg=kwargs.get("show_button_bg", True),
                        shadow=shadow, tint=trigger_tint,
                        text_value=trigger_text_value,
                        height=kwargs.get("trigger_height", 19), disable_scroll=True,
                        z_offset=0, text_align=trigger_align, bg_offset=bg_offset, text_pad=trigger_pad)

    if clicked:

        was_open = is_open
        if _DD_DBG:
            with open(debug_log_path("dd_debug.log"), "a") as _f:
                _f.write(f"[DD-DBG] CLICK toggle f={Melty.frame_count} name={name!r} was_open={was_open}\n")
        Melty.popover_focused_ds = None if is_open else draw_state
        is_open = Melty.popover_focused_ds is draw_state
        if is_open and not was_open:
            Melty._popover_open_frame = Melty.frame_count  # grace the opening click
            # Fresh open: start with an empty query and give the search box a few
            # frames to grab text focus so the user can type to filter immediately.
            drop_down_state.search_query = ""
            drop_down_state.search = ""
            drop_down_state._focus_search = 8 if len(collection) > 4 else 0
            if len(collection) <= 4:
                Melty.clear_focus(not_this=draw_state)
            # Start the highlight on the last-selected item (expanded to it) rather
            # than the top, so re-opening starts where you left off.
            _sp = _dd_as_tuple(getattr(drop_down_state, "selected_path", ()))
            drop_down_state.cursor_path = _sp
            drop_down_state.open_path = _sp[:-1] if _sp else ()
            drop_down_state._kbd_mode = True
            drop_down_state._last_mouse = None
            drop_down_state._had_focus = False
        if not is_open:
            _dd_close(drop_down_state)
            draw_state.invalidate()

        request_render()

    # The popover is a latching window -- the first call registers it and it
    # stays alive, so we always call it and toggle visibility with `closed`
    # rather than skipping the call (skipping would leave the last-open frame
    # painted). closed=True hides the whole subtree. window_pos pins it just
    # under the trigger (relative to this dropdown window) so it doesn't drift
    # off as a free-floating draggable; temp keeps it ephemeral. root_state
    # carries the single open-path the recursion expands; path_prefix starts
    # empty at the root. A pick bubbles back as (changed, value).
    if is_open:
        # A mouse move switches back to hover mode so the highlight follows the
        # pointer again (until the next arrow key locks keyboard mode).
        _mp = imgui.get_mouse_pos()
        _lm = getattr(drop_down_state, "_last_mouse", None)
        if _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5):
            drop_down_state._kbd_mode = False
        drop_down_state._last_mouse = (_mp[0], _mp[1])

    # Size from the current rows, never the previous window's clipped bounds.
    # The model catalog may arrive after opening, and a search may temporarily
    # leave the items. Neither may become the next menu's permanent size.
    menu_width, menu_height, menu_top = _dd_popup_geometry(
        collection, getattr(drop_down_state, "search_query", ""),
        trigger_top, trigger_h, open_upwards, max(trigger_w, menu_min_width or 0))
    menu_left = max(0, min(trigger_left, imgui.get_io().display_size[0] - menu_width))
    changed, new_item, menu_ds = draw_dd_menu(
        collection, tint=draw_state.tint,
        name=f"{unique}_menu",
        closed=not is_open, temp=True, shadow=False, auto_resize=False,
        window_pos=(menu_left - imgui.get_cursor_screen_pos()[0], menu_top - imgui.get_cursor_screen_pos()[1]),
        width=menu_width, height=menu_height,
        parent_window=draw_state, swoosh=False, disable_scroll=False,
        row_tags=kwargs.get("row_tags"),
        row_tints=kwargs.get("row_tints"),
        row_actions=kwargs.get("row_actions"),
        text_toward_bg=kwargs.get("text_toward_bg", 0.0),
        root_state=drop_down_state, path_prefix=(), return_extras=True)
    drop_down_state._menu_ds = menu_ds
    if is_open:
        pending = getattr(drop_down_state, "_pending_pick", None)
        if pending is not None:
            drop_down_state._pending_pick = None
            drop_down_state._picked_path = _dd_as_tuple(pending)
            changed, new_item = True, _dd_walk(collection, _dd_as_tuple(pending))
        if changed:
            _p = _dd_as_tuple(getattr(drop_down_state, "_picked_path", ()))
            drop_down_state.selected_path = _p
            drop_down_state.selected_label = _dd_label_for_path(collection, _p)
            if _DD_DBG:
                with open(debug_log_path("dd_debug.log"), "a") as _f:
                    _f.write(f"[DD-DBG] CLOSE via menu-pick f={Melty.frame_count} name={name!r} picked={_p}\n")
            Melty.popover_focused_ds = None  # picking dismisses the popover
            _dd_close(drop_down_state)
            draw_state.invalidate()
            request_render()
            return True, new_item

        # The dropdown owns the keyboard while open: its search box holds text
        # focus (taken on open, released on close), so OTHER text editors gate off
        # (they all check Melty.text_focused_ds) and never act on the same keys. We
        # only handle nav keys while we actually hold that focus - that's what
        # keeps the arrow/Enter/Esc collisions with whatever editor was active.
        box_tile = getattr(drop_down_state, "_search_box_tile", None)
        text_focused = (Melty.text_focused_ds is not None and box_tile is not None
                        and getattr(Melty.text_focused_ds, "_tile_id", None) == box_tile)
        if len(collection) <= 4:
            text_focused = True

        # Esc dismisses the open dropdown (and releases its text focus via
        # _dd_close). Ungated: the global text box Esc handler may have already
        # cleared the box's focus this same frame, so we don't require it here.
        if any(k == glfw.KEY_ESCAPE for k, _ in Core.melty.frame_key_events):
            if _DD_DBG:
                with open(debug_log_path("dd_debug.log"), "a") as _f:
                    _f.write(f"[DD-DBG] CLOSE via Esc f={Melty.frame_count} name={name!r}\n")
            Melty.popover_focused_ds = None
            _dd_close(drop_down_state)
            draw_state.invalidate()
            request_render()
            return False, input_value

        # Arrows / Enter only while we own the keyboard, so they don't also drive
        # whatever editor was active when the dropdown opened.
        if text_focused:
            search = getattr(drop_down_state, "search", "") or ""
            picked = _dd_handle_keys(collection, drop_down_state, search=search,
                                     text_focused=text_focused)
            if picked is not UNSET_VALUE:
                _p = _dd_as_tuple(getattr(drop_down_state, "_picked_path", ()))
                drop_down_state.selected_path = _p
                drop_down_state.selected_label = _dd_label_for_path(collection, _p)
                Melty.popover_focused_ds = None
                _dd_close(drop_down_state)
                draw_state.invalidate()
                request_render()
                return True, picked

        # Window press dispatch owns click-away dismissal. It uses the captured
        # press position and both parent links; a second post-imgui check here
        # races deferred menu rendering and mistakes nested menu clicks for exits.

        # Focus settle (bounded, NOT a permanent repaint loop): right after open
        # the search box asks for text focus, but the opening click's
        # request_focus can race it the same frame. While the box hasn't
        # confirmed focus and we're still within the small retry budget, re-run
        # so it asks again; it gets within a frame or two and this stops.
        # Steady-state open repaints nothing - mouse changes invalidate via
        # _dd_set_cursor, keys via begin_frame. The retry must invalidate the
        # BOX's own chain (invalidate_up from its tile): the menu is a separate
        # cached window subtree, so invalidating just this trigger view never
        # re-rendered the box on a reopen and request_focus never re-fired.
        if getattr(drop_down_state, "_focus_search", 0) > 0:
            box_tile = getattr(drop_down_state, "_search_box_tile", None)
            if box_tile is not None:
                Melty.cache.invalidate_up(box_tile, force=True)
            Melty.cache.invalidate(draw_state._tile_id, force=True)
            request_render()

    return False, input_value


@render_func(use_cache=True, show_bg=True, shadow=True, selectable=False, temp=True,
             closable=True, popover=True, melty_window=False, auto_resize=True, with_header=None,
             max_height=420, min_width=300, swoosh=False, min_height=33, keep_in_view=True)
def draw_dd_menu(input_value, draw_state, root_state=None, unique=0, path_prefix=(), tint=None,
                 show_search=None, text_align="right", row_tags=None, row_tints=None,
                 row_suffixes=None, row_actions=None, text_toward_bg=0.0,
                 full_render=False, row_code=None, **kwargs):
    """One level of the dropdown, drawn as its own temp popover window. Iterates
    the level's entries and renders each as a row (`_dd_menu_row`); a leaf click
    or a pick inside a nested sub-menu bubbles back up as (changed, value).

    Expansion is driven by `root_state.open_path` (a single chain of keys, see
    DropDownState) — NOT by each row testing its own hover. A row renders its
    sub-menu only when `open_path` runs through it, so at most one sub-menu is
    open per level and hidden siblings can never resurface. `path_prefix` is this
    level's key chain from the root; each row's full path is prefix + its key.

    `show_search` draws the root-level filter box. Autocomplete (the code editor's
    suggestion popup) passes False: the editor itself owns text focus and the
    half-typed identifier IS the filter, so a second focus-stealing search box
    would fight it. The caller pre-filters the rows in that case.

    The popover and its rows are CACHED tiles with NO kwargs cache key —
    a clean row tile blit-skips even when its cursor/open path inputs changed.
    Repaints are therefore driven by explicit invalidation only: an open_path
    change cascades invalidate_up from the menu tile inside _dd_set_cursor
    (this is what re-runs a stale branch row so it stamps its submenu window
    closed — see the leak note there), keys via the begin_frame popover hook
    (or the code editor's per-event invalidate_up) — invalidate_up
    specifically, since it cascades to the row tiles; a plain invalidate
    leaves the inner dd_rows collection clean and it blit-skips."""
    from meltygui.core.conversion.cache_tree import UNSET_VALUE
    from meltygui.core.styling.fonts import Font
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.text_view import draw_text
    from meltygui.model.dropdown_model import _dd_as_tuple
    from meltygui.model.dropdown_model import _dd_first_match_leaf
    from meltygui.model.dropdown_model import _dd_row_lookup
    from meltygui.core.layout.dropdown_core import _dd_set_cursor
    from meltygui.model.dropdown_model import _dd_visible_entries

    if show_search is None:
        show_search = len(input_value) > 4
    if show_search and not path_prefix and root_state is not None:
        # Root owns the search box. Single-line so Up/Down/Enter pass through to
        # menu nav; it auto-focuses once when the menu opens (_focus_search).
        q = getattr(root_state, "search_query", "") or ""
        box = draw_text(q, name=f"dd_search{unique}", show_name=False, searchable=False,
                        single_line=True, is_search_box=True, is_tree=False,
                        with_header=None, with_footer=None, show_bg=True, shadow=False,
                        request_focus=getattr(root_state, "_focus_search", 0) > 0,
                        font=Font.JETBRAINS_MONO_19,
                        tint=tint, return_extras=True)
        q_changed, new_q = box[0], box[1]
        box_ds = box[2] if len(box) > 2 else None
        if box_ds is not None:
            root_state._search_box_tile = box_ds._tile_id
            # Focus retry is a bounded countdown: the first popover's
            # move-to-front clears text focus the frame the box first grabs it
            # (apply_move_to_front(): parent != front window), so a one-shot
            # request is lost. Re-request for a few frames until it lands, then
            # clear (0). Draw_dropdown drives the re-runs while this is > 0.
            if Melty.text_focused_ds is box_ds:
                root_state._focus_search = 0
            elif getattr(root_state, "_focus_search", 0) > 0:
                root_state._focus_search -= 1
        new_search = str(new_q or "").strip().lower()
        if q_changed:
            root_state.search_query = new_q
            # Jump the cursor onto the first matching leaf, auto-expanding all
            # branches above it, so the leaf is visible and one Enter selects it
            # (instead of Enter-to-open-then-Enter-to-pick).
            if new_search:
                leaf = _dd_first_match_leaf(input_value, new_search)
                if leaf is not None:
                    _dd_set_cursor(root_state, leaf, False, menu_ds=draw_state)
                else:
                    _dd_set_cursor(root_state, (), False, menu_ds=draw_state)
            else:
                _dd_set_cursor(root_state, (), False, menu_ds=draw_state)
        root_state.search = new_search

    search = str(getattr(root_state, "search", "") or "")
    # Cursor/open paths are passed down as ROW INPUTS (not read by the row's
    # root_state): a row is a use_cache=True tile, so its highlight only repaints
    # when an input changes. Throwing the paths through makes keyboard nav and the
    # default-selection-on-open repaint (hover repaints via a separate hook).
    open_path = _dd_as_tuple(getattr(root_state, "open_path", ()))
    cursor_path = _dd_as_tuple(getattr(root_state, "cursor_path", ()))

    # Stale-submenu sweep (root level only - the registry is keyed by full row
    # path, so one sweep covers every depth): a branch row stamps its submenu
    # `closed` only when the row body runs, and an active search can filter
    # the row itself out of `rows` while its submenu is open - the submenu
    # window then floats on with nothing left to close it. dd_menu_row
    # registers its submenu draw_state in root_state._dd_submenu_ds; the
    # root body re-runs on every query/open_path change, so close anything
    # here that's no longer in the open path. The row re-registers/reopens it
    # if it ever comes back on-path.
    if not path_prefix and root_state is not None:
        for _sp, _sds in list(getattr(root_state, "_dd_submenu_ds", {}).items()):
            if (_sds is not None and open_path[:len(_sp)] != _sp
                    and not getattr(_sds, "closed", True)):
                _sds.closed = True
                request_render()

    ancestor_matched = bool(search) and any(search in str(k).lower() for k in path_prefix)
    rows = _dd_visible_entries(input_value, "" if ancestor_matched else search)
    # if not rows and search:
    #     imgui.dummy(180, 6)
    #     text("  No matches", width=180, height=_DD_ROW_H, name="dd_nomatch",
    #          text_colour=(1, 1, 1))
    #     return False, None

    # Per-row kwargs shared by both render paths. Each row is a
    # (key, value, label, is_branch) tuple handed to dd_menu_row, which computes
    # its own path/cursor/open state from path_prefix + root_state. full_render is
    # threaded down so nested sub-menus inherit the same render path.
    row_kwargs = dict(show_bg=False, shadow=False, path_prefix=tuple(path_prefix),
                      root_state=root_state, tint=tint, text_align=text_align, z_offset=0,
                      row_tags=row_tags, row_tints=row_tints, row_suffixes=row_suffixes,
                      cursor_path=cursor_path,
                      open_path=open_path, full_render=full_render)

    # Manual row loop (the dd_collection full_render path is gone - its
    # per-row render_func tiles cost more than they saved; virtualization is
    # done directly below instead). Branch rows still go through the
    # dd_menu_row render_func (they own a nested submenu + a real draw_state);
    # leaf rows - the bulk of a big list - are drawn inline by _dd_leaf_row
    # with raw imgui, skipping the per-row wrapper overhead that made long
    # menus crawl while interacting.
    result = (False, input_value)

    # Viewport culling for big flat menus (the 967-icon picker): rows are a
    # fixed _DD_ROW_H pitch, so a leaf row scrolled outside the window band
    # skips its draw entirely and reserves the space with a dummy. The width
    # is the cached widest label width so the auto_resize width doesn't
    # jitter with the visible set (the cache recomputes once per search
    # change, not per frame). Branch rows are never culled: a parent stamps
    # `closed` on their submenu window each run (see the leak note in
    # _dd_set_cursor). Gated on a known height - the first frame draws
    # everything once - and on row count so small menus keep the simple path.
    _cull_top = _cull_bot = None
    if root_state is not None and len(rows) >= 40 and draw_state.height:
        _wt = draw_state._abs_top()
        _cull_top, _cull_bot = _wt, _wt + draw_state.height
        _wkey = (len(rows), search)
        if getattr(root_state, "_row_w_key", None) != _wkey:
            _mw = 0.0
            for (_k, _v, _lbl, _b) in rows:
                _sfx = _dd_row_lookup(row_suffixes, _v) if row_suffixes else None
                _tag = _dd_row_lookup(row_tags, _v) if row_tags else None
                _mw = max(_mw, imgui.calc_text_size(str(_lbl) + (_sfx or ""))[0]
                          + (_dd_tag_width(_tag) + 16.0 if _tag else 0.0))
            root_state._row_w_key = _wkey
            root_state._row_w = _mw + 24.0
    # Shared label column for code rows: every row's code preview starts at
    # the same x (the widest label, capped - a long scope name does not eat
    # the code's width), so the embedded editors are up line to line -
    # per-row label widths made each row's code start jagged.
    code_label_w = None
    if row_code:
        _ws = [imgui.calc_text_size(str(_l).rstrip())[0]
               for (_k, _v, _l, _b) in rows
               if not _b and _dd_row_lookup(row_code, _v) is not None]
        if _ws:
            code_label_w = min(max(_ws), Toggles.Dropdown.code_label_max_width)
    row_width = _dd_row_width(draw_state)
    for idx, row in enumerate(rows):
        key, value, label, is_branch = row
        if is_branch:
            changed, picked = dd_menu_row(row, name=f"ddrow_{idx}_{key}", **row_kwargs)
            if changed:
                result = (True, picked)
        else:
            if _cull_top is not None:
                _cx0, _cy0 = imgui.get_cursor_screen_pos()
                if _cy0 + Toggles.Dropdown.row_height <= _cull_top or _cy0 >= _cull_bot:
                    # Offscreen leaf: reserve its draw rect (width from the
                    # cached widest label so measure/auto-resize stay stable)
                    # and pin the cursor a row down, like a hidden row does.
                    imgui.dummy(getattr(root_state, "_row_w", 1.0), Toggles.Dropdown.row_height)
                    imgui.set_cursor_screen_pos((_cx0, _cy0 + Toggles.Dropdown.row_height))
                    continue
            picked = _dd_leaf_row(key, value, label, draw_state, root_state,
                                  tuple(path_prefix), cursor_path, tint=tint,
                                  row_tags=row_tags, row_tints=row_tints,
                                  row_suffixes=row_suffixes,
                                  row_actions=row_actions,
                                  text_toward_bg=text_toward_bg,
                                  row_code=row_code,
                                  code_label_w=code_label_w,
                                  row_width=row_width)
            if picked is not UNSET_VALUE:
                result = (True, picked)
    return result


@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False, temp=True, show_add_delete=False,
             with_header=None, disable_scroll=True, min_width=300, swoosh=False, z_offset=-3)
def dd_menu_row(input_value, draw_state, text_align="right", path_prefix=(),
                root_state=None, tint=None, row_tags=None, row_tints=None,
                cursor_path=(), open_path=(), full_render=True, **kwargs):
    """A single BRANCH menu row (leaves go through the raw _dd_leaf_row).
    `input_value` is the row TUPLE (key, value, label, is_branch) handed in by
    draw_dd_menu's manual loop. `cursor_path` / `open_path` are
    passed IN (not read from root_state) so they're cache-key inputs: a row is a
    use_cache=True tile, so its keyboard highlight / open chevron only repaint when
    an input changes. Leaves are a button returning the VALUE on click; branch rows
    show a chevron and own a nested `draw_dd_menu` to their right, shown only while
    on the open path. Hovering points the shared cursor here; the highlight is
    painted over the row box when hovered / keyboard-current.

    `row_tags` (optional) maps a value -> short dim string drawn right-aligned —
    the code editor's completion popup uses it for the kind label (func/class/…)."""
    from meltygui.view.control_view import button
    from meltygui.model.dropdown_model import _dd_as_tuple
    from meltygui.core.layout.dropdown_core import _dd_invalidate_rows
    from meltygui.core.layout.dropdown_core import _dd_pick
    from meltygui.model.dropdown_model import _dd_row_lookup
    from meltygui.core.layout.dropdown_core import _dd_set_cursor

    key, value, label, is_branch = input_value
    row_path = tuple(path_prefix) + (key,)
    open_path = _dd_as_tuple(open_path)
    cursor_path = _dd_as_tuple(cursor_path)
    sub_open = open_path[:len(row_path)] == row_path
    is_cursor = cursor_path == row_path
    tag = _dd_row_lookup(row_tags, value)

    hovered = draw_state._bounding_hovered
    # Colour the row by its value's embedded tint (e.g. a Lora's .tint), falling
    # back to the per-row override (autocomplete's symbol tints), then the menu
    # tint for plain values.
    row_tint = _dd_row_lookup(row_tints, value)
    tint = _dd_obj_tint(value, row_tint or tint)
    fa_chrevron_right = f"\uf054"

    kbd_mode = getattr(root_state, "_kbd_mode", True)
    if hovered and not kbd_mode:
        _dd_set_cursor(root_state, row_path, is_branch,
                       menu_ds=draw_state.parent_window)

    active = is_cursor if kbd_mode else hovered
    if active:
        dl = imgui.get_window_draw_list()
        dl.add_rect_filled(draw_state.abs_left, draw_state.abs_top,
                           draw_state.abs_left + draw_state.width,
                           draw_state.abs_top + draw_state.height,
                           pack_color(1, 1, 1, 0.16),
                           rounding=getattr(draw_state, 'corner_radius', 6))

    chevron = f"  {fa_chrevron_right}" if is_branch else "    "  # fa-chevron-right
    if not full_render:
        # Lightweight row: draw the label with raw imgui.text() instead of the
        # full button/draw_text render_func. There's no click report from
        # imgui.text(), so derive one from this row's hover (the caller's
        # bounding-box hover) plus a fresh left mouse-down.
        imgui.text(f"{label}{chevron}")
        clicked = hovered and imgui.is_mouse_clicked(0)
    elif is_branch:
        # +0.9 value on the active row lifts the label over the white@0.16 wash
        # (button's own +1.5 hover boost keys off `hovered`, which is False for
        # the keyboard-cursor row); hue is retained, only brightness moves.
        clicked, _ = button(f"{label}{chevron}", name=f"{label}_ddrow", width=draw_state.content_width - 10,
                            height=Toggles.Dropdown.row_height, hovered=hovered, text_color=Tint.dd_text(requested_tint=tint),
                            text_saturation=1.349, shadow=False,
                            rounding=0, show_button_bg=False, show_bg=False, use_cache=True,
                            text_align=text_align, tint=tint)
    else:
        clicked, _ = button(f"{label}{chevron}", name=f"{label}_ddrow", show_button_bg=False,
                            width=draw_state.content_width - 10, height=Toggles.Dropdown.row_height, hovered=hovered,
                            text_saturation=0.716, z_offset=0, shadow=False, show_bg=False, use_cache=True,
                            text_align=text_align, tint=tint)

    # In keyboard-select mode the arrow keys paint the highlight; hover neither
    # moves the cursor nor paints, until the mouse moves (draw_dropdown clears it).

    # ONE highlight, framed to this row's box. Mouse mode keys off the live hover;
    # keyboard mode keys off the cursor. Using a separate source per mode (rather
    # than hover OR cursor) avoids briefly painting both the stale-cursor row and
    # the freshly-hovered row, which doubled the wash and looked inconsistent.

    # Dim kind tag, right-aligned over the row (drawn last so it sits above the
    # highlight). The name is left-aligned by the caller's text_align. A long
    # label could run under a long tag, so the tag gets an opaque backing rect
    # first: the parent window's actual bg fill (bg_color_stack top = nearest
    # show_bg ancestor) with the active-row wash (white @ 0.16, see above)
    # re-composed in, so the mask is invisible on both active and highlighted
    # rows while still cutting off the label.
    if tag:
        _dd_paint_tag(imgui.get_window_draw_list(),
                      draw_state.abs_left + draw_state.width - 10,
                      draw_state.abs_top, draw_state.height, tag, active)

    if is_branch:
        # Always call the sub-menu (so off-path ones stay registered but hidden
        # via closed=True, never leaving a stale painted frame); only the on-path
        # branch actually draws. Pinned to the right of this row with window_pos.
        search = str(getattr(root_state, "search", "") or "")
        if any(search in str(key).lower() for key in row_path):
            search = ""
        popup_width, popup_height, _ = _dd_popup_geometry(value, search, 0, 0, False)
        popup_left, popup_top = _dd_submenu_position(
            draw_state.abs_left, draw_state.abs_top, draw_state.width,
            popup_width, popup_height, *imgui.get_io().display_size)
        cursor_x, cursor_y = imgui.get_cursor_screen_pos()
        changed, picked, _sub_ds = draw_dd_menu(value, name=f"{label}_submenu", tint=tint,
                                       closed=not sub_open, temp=True, use_cache=False,
                                       window_pos=(popup_left - cursor_x, popup_top - cursor_y),
                                       width=popup_width, height=popup_height, auto_resize=False,
                                       show_add_delete=False,
                                       parent_window=draw_state, disable_scroll=False,
                                       full_render=full_render, row_tints=row_tints,
                                       root_state=root_state, path_prefix=row_path,
                                       return_extras=True)
        # Register the submenu window on the shared root state so the ROOT
        # menu body can sweep it close even when THIS row stops rendering -
        # a search query can filter the row itself out of this level while its
        # submenu is stamped open, and a row that doesn't render can never
        # restamp `closed` (the nested-window leak, one level down).
        if root_state is not None and _sub_ds is not None:
            _reg = getattr(root_state, "_dd_submenu_ds", None)
            if _reg is None:
                _reg = root_state._dd_submenu_ds = {}
            _reg[row_path] = _sub_ds
        if _sub_ds is not None:
            # The closed -> open EDGE: the submenu's row tiles are cached and
            # blit-skip while clean, so a reopened submenu came up as a blank
            # panel until the pointer entered it (a meltygui app's menu bar,
            # 09-14). Cascade-invalidate its rows the frame it opens.
            was_open = getattr(_sub_ds, "_dd_was_open", False)
            if sub_open and not was_open:
                _dd_invalidate_rows(root_state, _sub_ds)
            _sub_ds._dd_was_open = sub_open
        if changed:
            return True, picked
    elif clicked:
        _dd_pick(root_state, row_path)
        return True, value

    return False, value



def _dd_fit_label(s, px):
    """`s` ellipsis-trimmed to render within `px` (imgui text metrics), so a
    fixed-width trigger never draws wider than its own button rect."""
    if px <= 0:
        return ""
    if imgui.calc_text_size(s)[0] <= px:
        return s
    while s and imgui.calc_text_size(s + "...")[0] > px:
        s = s[:-1]
    return s + "..."


def _dd_obj_tint(obj, fallback=None):
    """An object's embedded tint (a 3+-tuple `.tint`, e.g. on a Lora), else
    `fallback`. Used to colour each row by its value and the trigger by the
    selected value."""
    t = getattr(obj, "tint", None)
    if isinstance(t, (tuple, list)) and len(t) >= 3:
        return tuple(t)
    return fallback


def _dd_submenu_position(left, top, row_width, width, height, display_w, display_h):
    right = left + row_width
    x = right if right + width <= display_w else left - width
    return max(0, min(x, display_w - width)), max(0, min(top, display_h - height))


def _dd_popup_geometry(collection, search, trigger_top, trigger_height,
                       open_upwards=None, min_width=None):
    """Content-sized popup contained in the display, independent of old bounds."""
    from meltygui.model.dropdown_model import _dd_visible_entries
    from meltygui.core.cache.tile_cache import snap_int

    rows = _dd_visible_entries(collection, (search or "").strip().lower())
    display_w, display_h = imgui.get_io().display_size
    natural_height = (len(rows) + (2 if len(collection) > 4 else 1)) * Toggles.Dropdown.row_height
    above = max(0, trigger_top)
    below = max(0, display_h - trigger_top - trigger_height)
    upwards = (below < min(natural_height, Toggles.Dropdown.max_height) and above > below
               if open_upwards is None else open_upwards)
    height = min(natural_height, Toggles.Dropdown.max_height, above if upwards else below)
    width = min(display_w, max(
        Toggles.Dropdown.min_width if min_width is None else min_width,
        max((imgui.calc_text_size(row[2])[0] + 48 for row in rows), default=0)))
    top = trigger_top - height if upwards else trigger_top + trigger_height
    return snap_int(width), snap_int(height), snap_int(top)


def _dd_menu_fit(menu_ds, min_width=None, max_height=None):
    """The popover size that fits its content — what the wrapper's
    auto_resize computed for a closable window: the measured UNCLIPPED
    group rect (_content_rect), width floored at min_width, height capped
    at max_height and the display (rows scroll past it). None until the
    body has measured."""
    from meltygui.core.cache.tile_cache import snap_int

    rect = getattr(menu_ds, "_content_rect", None)
    if not rect or rect[0] <= 0 or rect[1] <= 0:
        return None
    display_w, display_h = imgui.get_io().display_size
    fit_w = snap_int(max(min(rect[0], display_w), Toggles.Dropdown.min_width if min_width is None else min_width))
    fit_h = snap_int(max(min(rect[1], display_h, Toggles.Dropdown.max_height if max_height is None else max_height), Toggles.Dropdown.min_height))
    return (fit_w, fit_h)


def _dd_tag_segments(tag):
    """A row tag as [(text, rgba)] segments. A plain str is ONE segment in
    the default dim colour; a sequence mixes str items with (text, color)
    pairs that keep their own colour — a 3-tuple colour gets the default
    alpha, a None colour the default colour. Empty texts drop out."""

    if not tag:
        return []
    if isinstance(tag, str):
        return [(tag, Toggles.Dropdown.tag_color)]
    out = []
    for item in tag:
        if isinstance(item, str):
            text, color = item, None
        else:
            text, color = item
        color = Toggles.Dropdown.tag_color if color is None else tuple(color)
        if len(color) == 3:
            color = color + (Toggles.Dropdown.tag_color[3],)
        if text:
            out.append((str(text), color))
    return out


def _dd_tag_width(tag):
    """Painted width of a row tag (all segments + gaps), 0 for none."""

    segments = _dd_tag_segments(tag)
    if not segments:
        return 0.0
    return (sum(imgui.calc_text_size(text)[0] for text, _c in segments)
            + Toggles.Dropdown.tag_gap * (len(segments) - 1))


def _dd_paint_tag(draw_list, right, top, height, tag, active,
                  row_tint=None, fade=0.0):
    """The dim tag column, right-aligned to `right` inside a row box
    [top, top + height). Opaque backing rect first so a long label can't
    run under it: the menu's actual painted fill (bg_color_stack top),
    with the row's tint wash and the active-row wash (white @ 0.16)
    re-composed in, so the mask is invisible on plain, tinted and
    highlighted rows alike. `fade` (0..1) pulls every segment's colour
    toward that fill — a faded row's tag fades with its label."""

    segments = _dd_tag_segments(tag)
    if not segments:
        return
    line_h = imgui.get_text_line_height()
    tag_w = _dd_tag_width(tag)
    tag_x = right - tag_w
    tag_y = top + (height - line_h) * 0.5
    bg = Melty.bg_color_stack[-1][:3] if Melty.bg_color_stack else None
    if bg is not None:
        r, g, b = bg
        if row_tint is not None:
            r = r * (1 - Toggles.Dropdown.row_tint_alpha) + row_tint[0] * Toggles.Dropdown.row_tint_alpha
            g = g * (1 - Toggles.Dropdown.row_tint_alpha) + row_tint[1] * Toggles.Dropdown.row_tint_alpha
            b = b * (1 - Toggles.Dropdown.row_tint_alpha) + row_tint[2] * Toggles.Dropdown.row_tint_alpha
        if active:
            r, g, b = r * 0.84 + 0.16, g * 0.84 + 0.16, b * 0.84 + 0.16
        mask = pack_color(min(max(r, 0.0), 1.0),
                                        min(max(g, 0.0), 1.0),
                                        min(max(b, 0.0), 1.0), 1.0)
        draw_list.add_rect_filled(tag_x - 6, top + 1, tag_x + tag_w + 6,
                                  top + height - 1, mask)
    fade = min(max(float(fade or 0.0), 0.0), 1.0)
    x = tag_x
    for text, color in segments:
        red, green, blue, alpha = color
        if fade and bg is not None:
            red = red * (1 - fade) + bg[0] * fade
            green = green * (1 - fade) + bg[1] * fade
            blue = blue * (1 - fade) + bg[2] * fade
        draw_list.add_text(x, tag_y,
                           pack_color(red, green, blue, alpha),
                           text)
        x += imgui.calc_text_size(text)[0] + Toggles.Dropdown.tag_gap


def _dd_row_width(draw_state):
    """A menu row's width: the popover WINDOW's own width less the scrollbar
    reserve. NOT content_width — the wrapper derives that from the PARENT's
    available width (the trigger's 300 px slot) and pins it at min_width,
    so a popover grown to fit long labels painted its tags and hit-tested
    its rows at 300 px while the window was 600+ wide (the tag masked the
    label's tail). The max keeps the old figure where it was the larger."""
    window_width = draw_state.width or 0
    return max(draw_state.content_width or 0,
               window_width - (SCROLL_BAR_WIDTH_DEFAULT + SCROLLBAR_MARGIN))


def _dd_leaf_row(key, value, label, draw_state, root_state, path_prefix,
                 cursor_path, tint=None, row_tags=None, row_tints=None,
                 row_suffixes=None, row_actions=None, left_pad=10,
                 text_toward_bg=0.0, row_code=None, code_label_w=None,
                 row_width=None):
    """Render ONE leaf menu row inline with raw imgui — NO per-row render_func.
    Leaves are the bulk of a big menu, so skipping the dd_menu_row wrapper (its
    own draw_state / cache / BVH / hover machinery, tens of µs each) is the whole
    point: it's what made the 967-icon list crawl while hovering/scrolling. The
    parent draw_dd_menu tile already re-renders every frame it's bounding-hovered
    (core_render hover invalidation), so this row's hover highlight / click stay
    live without a tile of its own. Branch rows still go through dd_menu_row — they
    own a nested submenu and a real draw_state. Returns the picked value when
    clicked, else UNSET_VALUE.

    `draw_state` is the MENU window's draw_state (the level), not a per-row one."""
    from meltygui.core.conversion.cache_tree import UNSET_VALUE
    from meltygui.core.layout.dropdown_core import _dd_pick
    from meltygui.core.layout.dropdown_core import _dd_set_cursor
    from meltygui.editor.source_ui import _RowSpan
    from meltygui.editor.source_ui import _row_code_hosts
    from meltygui.view.text_view import draw_text
    from meltygui.model.dropdown_model import _dd_as_tuple
    from meltygui.model.dropdown_model import _dd_row_lookup
    from meltygui.core.windowing.glfw_utils import request_render

    row_path = tuple(path_prefix) + (key,)
    is_cursor = _dd_as_tuple(cursor_path) == row_path
    kbd_mode = getattr(root_state, "_kbd_mode", True)
    row_fade = float(text_toward_bg or 0.0)

    pos = imgui.get_cursor_screen_pos()
    x, y = pos[0], pos[1]
    w = row_width if row_width else _dd_row_width(draw_state)
    h = Toggles.Dropdown.row_height
    mp = imgui.get_mouse_pos()
    hovered = (x <= mp[0] < x + w) and (y <= mp[1] < y + h)
    if hovered:
        # Clamp hover to the menu window's visible band: rows laid out above the
        # window bounds (scrolled/clipped away) are invisible but this raw imgui
        # math would still hit them - a click on editor text under the popup
        # could pick an unseen row.
        _wt = draw_state._abs_top()
        if not (_wt <= mp[1] < _wt + (draw_state.height or 0)):
            hovered = False
    if hovered and not kbd_mode:
        _dd_set_cursor(root_state, row_path, False, menu_ds=draw_state)

    active = is_cursor if kbd_mode else hovered
    dl = imgui.get_window_draw_list()
    line_h = imgui.get_text_line_height()
    # Per-row symbol tint (the autocomplete popup's background colors): drawn
    # as a colored BACKGROUND wash with near-white text over it - the editor's
    # wash styling - rather than colored text. Under the active highlight so
    # keyboard/hover selection still reads on tinted rows.
    row_tint = _dd_row_lookup(row_tints, value)
    _ROW_TINT_A = Toggles.Dropdown.row_tint_alpha
    _ROW_TINT_V_CAP = 0.55  # max RGB component - near-white text must stay legible
    _ROW_TINT_S_BOOST = 1.25  # saturation bump on capped tints — keeps hue vivid
    if row_tint is not None:
        r, g, b = row_tint[:3]
        v = max(r, g, b)
        if v > _ROW_TINT_V_CAP:
            k = _ROW_TINT_V_CAP / v
            r, g, b = r * k, g * k, b * k
            # Boost saturation by pulling the lower channels up from the max
            # (raw channel arithmetic - hue is the channel ORDER, matters).
            m = max(r, g, b)
            r = max(0.0, m - (m - r) * _ROW_TINT_S_BOOST)
            g = max(0.0, m - (m - g) * _ROW_TINT_S_BOOST)
            b = max(0.0, m - (m - b) * _ROW_TINT_S_BOOST)
        dl.add_rect_filled(x, y + 1, x + w, y + h - 1,
                           pack_color(r, g, b, _ROW_TINT_A),
                           rounding=getattr(draw_state, 'corner_radius', 6))
    if active:
        dl.add_rect_filled(x, y, x + w, y + h,
                           pack_color(1, 1, 1, 0.16),
                           rounding=getattr(draw_state, 'corner_radius', 6))

    # Raw imgui.text_colored() for the label, coloured by the value's embedded
    # tint (e.g. a Lora's .tint) falling back to the menu tint - same source as
    # dd_menu_row. Vertically centred in the fixed-height row: set_cursor pins the
    # next row exactly h below (matches the full menu's item_spacing.y=0), so
    # leaves and dd_menu_row branches line up.
    if row_tint is not None:
        # White nudged toward the wash color: legible on the tinted bar while
        # still reading as that symbol's hue.
        color = tuple(min(1.0, c * 0.25 + 0.75) for c in row_tint[:3])
    else:
        color = _dd_obj_tint(value, tint)
        color = Tint.dd_text(requested_tint=color)
        # Quiet-row dimming: pull the label toward the menu bg so tinted
        # rows (the info tab's active-source yellow) stand out - menu-wide
        # (text_toward_bg). Skipped for washed rows above - their label is
        # already contrast-managed.
        if row_fade and Melty.bg_color_stack:
            _bg = Melty.bg_color_stack[-1]
            _k = min(max(row_fade, 0.0), 1.0)
            color = tuple(c * (1 - _k) + _b * _k
                          for c, _b in zip(color[:3], _bg[:3]))
    imgui.set_cursor_screen_pos((x + left_pad, y + (h - line_h) * 0.5))

    if active:
        # Active-row label: lerp toward white so it clears the white@0.16 wash
        # (the wash lightens the row fill while the label is its plain text
        # value - light-on-light, 2.8:1 in a dark-tinted editor). Keeping 40%
        # of the original chroma leaves per-item tints recognizable.
        color = tuple(min(1.0, c * 0.4 + 0.6) for c in color[:3])
    color = *(color[:3]), 1.0

    tag = _dd_row_lookup(row_tags, value)
    code_row = _dd_row_lookup(row_code, value)
    if code_row is not None:
        # GlobalSearch-mirrored code row (the quick-jump popup): the row
        # label as a dim prefix, then the ACTUAL code line rendered through the
        # real editor - draw_text with the file's live cst-dict parse and a
        # jump_to line-offset shim, so token colors and definition washes are
        # pixel-identical to the jump window; the file line shown in the
        # editor's own gutter (line_numbers=[...]) and the editor's text
        # right-aligns like every other row (width reserved below). Same
        # embed recipe as draw_global_search's code_row block, including
        # use_cache=False (the layer-band masking note there).
        _cp, _cl, _ccode = code_row
        _ty = y + (h - line_h) * 0.5
        # rstrip: dedup keys may carry invisible trailing whitespace - never a
        # visible counter. Elipsize past the
        # label cap so a deep scope name can't eat the code column.
        _lbl = str(label).rstrip()
        if imgui.calc_text_size(_lbl)[0] > Toggles.Dropdown.code_label_max_width:
            while _lbl and imgui.calc_text_size(_lbl + "…")[0] > Toggles.Dropdown.code_label_max_width:
                _lbl = _lbl[:-1]
            _lbl += "…"
        dl.add_text(x + left_pad, _ty,
                    pack_color(color[0], color[1], color[2], 0.9),
                    _lbl)
        # Shared column (widest label in the menu, capped, precomputed by
        # draw_dd_menu) so every row's editor starts at the same x - with the
        # static 5-digit gutter inside draw_text, code aligns line to line.
        _lw = (code_label_w if code_label_w is not None
               else min(imgui.calc_text_size(_lbl)[0], Toggles.Dropdown.code_label_max_width))
        _cx = x + left_pad + _lw + 14.0
        _tw_r = (_dd_tag_width(tag) + 22.0) if tag else 10.0
        _cw = max(60.0, x + w - _tw_r - _cx)
        # Offscreen rows (scrolled past the menu's visible band): skip the
        # draw_text embed - the one-line editor body takes real wrapper
        # time, and a tall menu lays out every row each frame - and reserve
        # the space with a dummy so layout/scroll stay constant.
        _wt = draw_state._abs_top()
        _wb = _wt + (draw_state.height or 0)
        if y + h <= _wt or y >= _wb:
            imgui.set_cursor_screen_pos((_cx, y))
            imgui.dummy(_cw, h)
        else:
            _cdict, _chost = _row_code_hosts(_cp)
            imgui.set_cursor_screen_pos((_cx, y))
            try:
                # use_cache=True: the menu re-renders every bounding-hovered
                # frame, and an uncached editor body per row makes big pickers
                # crawl - cached editor tiles blit-skip when clean. Same
                # masking-cliff caveat as the GlobalSearch code rows.
                _res = draw_text(_ccode, name=f"dd_code_{key}", show_header=False,
                                 show_bg=False, shadow=True, single_line=True,
                                 width=_cw, height=h, use_cache=False, bg_offset=-2,
                                 jump_to=_RowSpan(_cl - 1, _cp), is_search_box=True,
                                 is_tree=False, z_offset=2,
                                 show_jump_bar=False, line_numbers=[_cl],
                                 tint=row_tint, selectable=False,
                                 roster_live_hold=False,   # single-only preview
                                 return_extras=True)
                if _chost is not None:
                    # Repaint when the background parse lands (washes pop in) but
                    # the ROW's own cached tile, not the menu window (the window
                    # invalidation leaves clean row tiles blit-skipped).
                    _row_ds = _res[2] if len(_res) > 2 else None
                    _chost.notify_on_change(_row_ds if _row_ds is not None
                                            else draw_state)
            except Exception:
                traceback.print_exc()
            # The embedded editor's caret sub consumes clicks over the code
            # area - reclaim them with a higher-priority sub on exactly that
            # rect (draw_global_search's code rows use the same technique) so
            # the click still picks the row. Label/tag/action areas keep the
            # raw imgui click path below.
            if draw_state.on_action("left_mouse_down",
                                    view_id=f"dd_code_act_{key}",
                                    rect=(_cx, y, _cx + _cw, y + h),
                                    priority_delta=4) is not None:
                _dd_pick(root_state, row_path)
                imgui.set_cursor_screen_pos((x, y + h))
                return value
        imgui.set_cursor_screen_pos((x, y + h))
    else:
        imgui.text_colored(str(label), *color)

        # Dim '(param, param2)' suffix right after the callable's name (autocomplete
        # rows): same hue as the label at reduced alpha, so it stays subtle on
        # plain, tinted and active rows alike. imgui text (not raw dl.add_text) so
        # the row's measured width includes it and auto-resizes with the popup.
        sfx = _dd_row_lookup(row_suffixes, value)
        if sfx:
            imgui.same_line(spacing=0)
            imgui.text_colored(sfx, color[0], color[1], color[2], 0.45)
        # Claim the tag column's width as row content too, so the menu's
        # auto-resize fits label AND tag side by side instead of the tag
        # masking the label's tail (a wide tag - commit date - hid most of a
        # long commit subject label).
        if tag:
            imgui.same_line(spacing=0)
            imgui.dummy(_dd_tag_width(tag) + 16.0, 1)

        imgui.set_cursor_screen_pos((x, y + h))

    # Dim kind tag, right-aligned (autocomplete's func/class/… label; the
    # Compare With rows' date) - _dd_paint_tag masks the label under it and
    # fades the hovered row.
    if tag:
        _dd_paint_tag(dl, x + w - 10, y, h, tag, active, row_tint=row_tint,
                      fade=row_fade)

    # Per-row ACTION (`row_actions`: dict value→callable, or single callable for
    # every row): a right-aligned trash icon whose click runs the action and
    # CONSUMES the click - no pick, popover stays open, so several rows can
    # be acted on in one visit (the info tab's clear-at-action).
    _act = None
    if row_actions is not None:
        _act = (_dd_row_lookup(row_actions, value)
                if isinstance(row_actions, dict) else row_actions)
    if _act is not None:
        _ax = x + w - 24
        _over_act = hovered and mp[0] >= _ax - 4
        dl.add_text(_ax, y + (h - line_h) * 0.5,
                    pack_color(0.85, 0.32, 0.28,
                                             0.95 if _over_act else 0.4),
                    "")
        if _over_act and imgui.is_mouse_clicked(0):
            _act(value)
            request_render()
            return UNSET_VALUE

    if hovered and imgui.is_mouse_clicked(0):
        _dd_pick(root_state, row_path)
        return value
    return UNSET_VALUE
