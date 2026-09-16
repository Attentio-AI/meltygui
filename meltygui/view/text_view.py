"""Text view functions and supporting definitions."""
from meltygui.code.libcst_conversion import CodeLine
from meltygui.completion.fim import FimState
from meltygui.editor.source_tools import SourceToolsState
from meltygui.fonts import Font
from meltygui.hdr_color import pack_color
from meltygui.hdr_color import unpack_color
from meltygui.melty import Melty
from meltygui.melty import SearchTerm
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.core_decoration import Core
from meltygui.rendering.decorators.window_decoration import window
from meltygui.rendering.render_funcs import RenderFuncs
from meltygui.state.new_core_model import DropDownState
from meltygui.state.new_core_model import TextEditorState
from meltygui.toggles import Tint
from meltygui.view.header_view import draw_footer
from meltygui.view.header_view import draw_header
import bisect
import math
import meltygui_imgui as imgui
import re
import time


def draw_icon_selector_plain(input_value, width=20, height=20, name=None,
                             tint=None, text_tint=None, editor_ds=None, **kwargs):
    """Inline Font Awesome icon picker without the @render_func wrapper — the
    glyph is drawn straight into the editor tile as a chip (see
    draw_number_token_plain for why plain: the ~90µs wrapper per widget per
    frame dominates with many inline widgets). Same renderer contract:
    `(glyph) -> (changed, new_glyph)`; a changed return splices the picked
    glyph in for this source char.

    Replaces the old wrapped draw_icon_selector, which routed through
    draw_dropdown: that registered a nested @window trigger PER ICON TOKEN
    (windows outlived their render-order-named call sites — the nested-window
    leak) and ellipsis-trimmed the glyph against the trigger's own text pad
    (the "..." cell). Here the trigger is just a centered add_text.

    The picker is a LATCHED draw_dd_menu popover parented to the EDITOR —
    the same latch pattern as draw_color3_token_plain's color picker: called
    every frame this widget renders with `closed=` toggled; open state lives
    on editor_ds (_icon_open_name) with Melty.popover_focused_ds pointing at
    the menu window, so outside clicks and the nav-key wake ride the standard
    popover machinery. draw_text closes the menu if this widget stops
    rendering while open (scrolled/edited away), so the window can never
    outlive its call site. Caret suppression for presses on the chip comes
    from ds._plain_tv_rects (owns_mouse), like the other plain widgets."""
    from meltygui.editor.text_editor import COLORS
    from meltygui.editor.text_editor import ICON_COLLECTION
    from meltygui.editor.text_editor import _plain_tv_bg
    from meltygui.utils.glfw_utils import request_render
    from meltygui.views.fa_icons import FA_GLYPH_SET
    import meltygui.window_api as glfw

    from meltygui.view.dropdown_view import draw_dd_menu
    from meltygui.core.dropdown_core import _dd_handle_keys
    from meltygui.core.dropdown_core import _dd_close
    from meltygui.code.cache_tree import UNSET_VALUE
    cur = input_value if isinstance(input_value, str) else ""
    x, y = imgui.get_cursor_screen_pos()
    w = max(1.0, width)
    h = max(1.0, height)
    _plain_tv_bg(x, y, w, h, tint=tint, bg_offset=0)
    dl = imgui.get_window_draw_list()
    if text_tint is not None:
        color = pack_color(text_tint[0], text_tint[1], text_tint[2],
                                         text_tint[3] if len(text_tint) > 3 else 1.0)
    else:
        color = COLORS['icon']
    # FA glyphs aren't monospaced - center the glyph's real width in the chip.
    _gw = imgui.calc_text_size(cur)[0] if cur else 0.0
    dl.add_text(x + (w - _gw) * 0.5, y, color, cur)
    io = imgui.get_io()
    hovered = x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h
    if hovered:
        dl.add_rect(x, y, x + w, y + h,
                    pack_color(1, 1, 1, 0.25), 5.0)
    clicked = hovered and imgui.is_mouse_clicked(0)
    if editor_ds is None:
        return False, cur

    menus = getattr(editor_ds, '_icon_menus', None)
    if menus is None:
        menus = editor_ds._icon_menus = {}
    root = getattr(editor_ds, '_icon_dd_root', None)
    if root is None:
        root = editor_ds._icon_dd_root = DropDownState()
    menu_ds = menus.get(name)
    open_prev = getattr(editor_ds, '_icon_open_name', None) == name
    still_open = menu_ds is not None and Melty.popover_focused_ds is menu_ds
    if clicked:
        want_open = not open_prev
    elif open_prev and not still_open:
        want_open = False   # dismissed externally (scroll away, other popover)
    else:
        want_open = open_prev
    if want_open and any(k == glfw.KEY_ESCAPE for k, _ in Melty.frame_key_events):
        want_open = False
        
    # Always include the current glyph so the menu can display/round-trip it
    # even if it isn't one of the defaults.
    coll = ICON_COLLECTION if (not cur or cur in FA_GLYPH_SET) else {cur: cur, **ICON_COLLECTION}

    if want_open and not open_prev:
        # First open: empty query, let the search box grab text focus for a few
        # frames (the opening click's clear_focus can race the box), and land
        # the highlight on the current glyph's row.
        root.search_query = ""
        root.search = ""
        root._focus_search = 8
        root._kbd_mode = True
        root._had_focus = False
        root._last_mouse = None
        _sel = next((k for k, v in coll.items() if v == cur), None)
        root.cursor_path = (_sel,) if _sel is not None else ()
        root.open_path = ()
        root.selected_path = root.cursor_path
        # Snap the menu's scroll to the highlighted row once the window exists
        # (its draw_state lags the first open by a frame) - countdown, not a
        # per-frame check, so wheel scrolling doesn't take over.
        root._snap_frames = 3
        Melty._popover_open_frame = Melty.frame_count  # grace the opening click
        request_render()

    if want_open:
        # A mouse move switches back to mouse mode so the highlight follows the
        # pointer again (until the next arrow key locks keyboard mode).
        _mp = imgui.get_mouse_pos()
        _lm = getattr(root, "_last_mouse", None)
        if _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5):
            root._kbd_mode = False
        root._last_mouse = (_mp[0], _mp[1])

    # Latched menu window - called every frame this widget renders with
    # `closed=` toggled so it persists when the (cached) editor body is
    # skipped. window_pos is relative to the imgui cursor at call time - set
    # it on the chip's top-left so (0, h) anchors just under the glyph.
    imgui.set_cursor_screen_pos((x, y))
    changed, picked, menu_ds = draw_dd_menu(
        coll, name=f"{name}_icon_menu", closed=not want_open, temp=True,
        swoosh=False, window_pos=(0, h), max_height=500, tint=tint,
        parent_window=editor_ds, disable_scroll=False, text_align="left",
        root_state=root, path_prefix=(), return_extras=True)
    menus[name] = menu_ds

    def _close_pick():
        Melty.popover_focused_ds = None
        _dd_close(root)
        editor_ds._icon_open_name = None
        Melty.cache.invalidate_up(editor_ds._tile_id, max_depth=10, force=True)
        request_render()

    if want_open:
        editor_ds._icon_open_name = name
        editor_ds._icon_seen = (name, Melty.frame_count)
        Melty.popover_focused_ds = menu_ds
        if changed and isinstance(picked, str):
            _close_pick()
            return True, picked

        # Arrows / Enter only while the menu's search box owns the keyboard,
        # so they don't also drive whatever editor was focused before.
        box_tile = getattr(root, "_search_box_tile", None)
        text_focused = (Melty.text_focused_ds is not None and box_tile is not None
                        and getattr(Melty.text_focused_ds, "_tile_id", None) == box_tile)
        _nav_hit = False
        if text_focused:
            _nav_hit = any(k in (glfw.KEY_UP, glfw.KEY_DOWN, glfw.KEY_ENTER,
                                 glfw.KEY_KP_ENTER)
                           for k, _ in Melty.frame_key_events)
            kpick = _dd_handle_keys(coll, root,
                                    search=getattr(root, "search", "") or "",
                                    text_focused=True)
            if kpick is not UNSET_VALUE and isinstance(kpick, str):
                _close_pick()
                return True, kpick

        # Keep the keyboard-cursor row visible: on the open snap (countdown -
        # the menu window's draw lags the first frame) and key nav hits.
        # _dd_handle_keys doesn't scroll by itself (the AC popup calls
        # _dd_scroll_cursor_into_view too); rows sit below the search box, so
        # its measured height is the row-0 offset.
        _snap = getattr(root, "_snap_frames", 0)
        if (_snap > 0 or _nav_hit) and menu_ds is not None:
            from meltygui.model.dropdown_model import _dd_rows_at
            from meltygui.core.dropdown_core import _dd_scroll_cursor_into_view
            from meltygui.model.dropdown_model import _dd_as_tuple
            if _snap > 0:
                root._snap_frames = _snap - 1
            _cp = _dd_as_tuple(root.cursor_path)
            if _cp:
                _rkeys = [r[0] for r in
                          _dd_rows_at(coll, (), getattr(root, "search", "") or "")]
                if _cp[-1] in _rkeys:
                    _box = (Melty.cache.key_to_draw_state.get(box_tile)
                            if box_tile is not None else None)
                    _off = (_box.height + 6) if (_box is not None
                                                 and _box.height) else 30
                    _dd_scroll_cursor_into_view(menu_ds, _rkeys.index(_cp[-1]),
                                                row0_offset=_off)

        # Focus settle (bounded): while the box hasn't confirmed focus and the
        # retry budget lasts, re-run its renderer so request_text() again -
        # same recipe as draw_dropdown's settle block.
        if getattr(root, "_focus_search", 0) > 0:
            if box_tile is not None:
                Melty.cache.invalidate_up(box_tile, force=True)
            Melty.cache.invalidate_up(editor_ds._tile_id, max_depth=10, force=True)
            request_render()
    else:
        if getattr(editor_ds, '_icon_open_name', None) == name:
            editor_ds._icon_open_name = None
            _dd_close(root)
        if menu_ds is not None and Melty.popover_focused_ds is menu_ds:
            Melty.popover_focused_ds = None
        if open_prev or clicked:
            request_render()
    return False, cur


@render_func(use_cache=True, show_bg=True, shadow=True, with_header=None, z_offset=3, tint=(0.911, 0.305, 0.0),
             show_name=False, selectable=False, bg_offset=0)
def draw_bool_token(input_value, draw_state=None, text_tint=None, **kwargs):
    """Inline True/False word — whole-token token_views renderer for 'bool'
    tokens. Renders the literal exactly as the editor would (same font, grid
    position and keyword color) so it reads as code. Deliberately NO imgui item
    and NO left_mouse_* subscription: single clicks and drags fall through to
    the editor, so the caret lands anywhere inside the word and selections
    sweep it like plain text. A DOUBLE-click flips the literal — hover shows an
    underline as the hint. (Single-click toggling proved too easy to trip.)"""
    from meltygui.editor.text_editor import COLORS

    word = input_value if input_value in ("True", "False") else "True"
    x, y = imgui.get_cursor_screen_pos()
    w = max(1.0, draw_state.width)
    h = max(1.0, draw_state.height)
    io = imgui.get_io()
    
    hovered = x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h
    draw_list = imgui.get_window_draw_list()
    # text_tint (from the editor, inside a tint-carrying override comment):
    # the word wears the comment's color instead of keyword-blue, so
    # widgets inside colored comments stop shouting.
    if text_tint is not None:
        color = pack_color(text_tint[0], text_tint[1], text_tint[2],
                                         text_tint[3] if len(text_tint) > 3 else 1.0)
    else:
        color = COLORS['bool']
    # if hovered:
    #      draw_list.add_line(x, y + h - 1.5, x + w, y + h - 1.5, color, 0.0)
    draw_list.add_text(x, y, color, word)
    if hovered and imgui.is_mouse_double_clicked(0):
        return True, ("False" if word == "True" else "True")
    return False, input_value


@render_func(use_cache=True, show_bg=True, shadow=True, with_header=None, z_offset=2,
             show_name=False, selectable=False, bg_offset=-1, tint=(0.026, 0.041, 0.056), wrap=True)
def draw_number_token(input_value, draw_state=None, text_tint=None,
                      left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False,
                      **kwargs):
    """Inline drag widget for a numeric literal — whole-token token_views renderer                             
                             
                             
    for 'number' tokens. Ints get drag_int, floats drag_float (unbounded: min=max=0);
    the dragged value is formatted back preserving the literal's shape (base,
    e-notation, decimal places) and spliced into the source like a keystroke.
    A unary sign is merged into the token by tokenize(), so the widget owns it
    and a drag crosses zero in one gesture. In binary-minus contexts (`a - 5`)
    the widget sees only the magnitude; dragging it negative splices `a - -1`,
    which is still valid Python.
    Drag is the widget's ONLY job — there is no typing mode (imgui's temp
    input is disabled via SLIDER_FLAGS_NO_INPUT). Text editing goes through
    the editor itself: a click on the widget places the editor caret at the
    character under the mouse (the _tv_click path in draw_text), and from
    there the literal edits like any other text.
    left_mouse_* are declared (never read) to win the event latch over the editor —
    a drag that starts on the widget latches here, so the editor doesn't grow a
    text selection while a value is being dragged."""
    from meltygui.editor.text_editor import _parse_number_token

    from meltygui.utils.render_utils import push_style_var
    from meltygui.utils.render_utils import pop_style_var
    from meltygui.utils.render_utils import push_style_color
    from meltygui.utils.render_utils import pop_style_color
    s = input_value if isinstance(input_value, str) else str(input_value)
    kind, val, fmt_back, disp = _parse_number_token(s)
    if kind is None:
        imgui.text(s)
        return False, s

    # The call site hands us pad_px of slack per side (the view - or its clip -
    # is that much wider than the token cells), so the frame fills the digits.
    # Editor-look colors: number-blue lettering on a dark frame, like the bool
    # word, with only a subtle hover/active lift instead of imgui's bright blue.
    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
    # Inside a tint-carrying override comment the editor hands us text_tint:
    # digits wear the comment's hue and the frame frame dims toward it too, so
    # number widgets in colored comments stop reading as bright blue sliders.
    if text_tint is not None:
        push_style_color(imgui.COLOR_TEXT, text_tint[0], text_tint[1], text_tint[2])
        push_style_color(imgui.COLOR_FRAME_BACKGROUND,
                         text_tint[0] * 0.22, text_tint[1] * 0.22, text_tint[2] * 0.22)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED,
                         text_tint[0] * 0.32, text_tint[1] * 0.32, text_tint[2] * 0.32)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE,
                         text_tint[0] * 0.42, text_tint[1] * 0.42, text_tint[2] * 0.42)
        _n_colors = 4
    else:
        push_style_color(imgui.COLOR_TEXT, 0.41, 0.59, 0.73)          # number blue
        # Never leave the drag frame on imgui's global theme color (white):
        # same dark editor-look fill the tinted branch uses, from number blue.
        push_style_color(imgui.COLOR_FRAME_BACKGROUND, 0.41 * 0.14, 0.59 * 0.14, 0.73 * 0.14)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED, 0.41 * 0.22, 0.59 * 0.22, 0.73 * 0.22)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE, 0.41 * 0.30, 0.59 * 0.30, 0.73 * 0.30)
        _n_colors = 4
    def _pop_styles():
        pop_style_color(_n_colors)
        pop_style_var()

    imgui.set_next_item_width(draw_state.width)
    if kind == 'int':
        speed = max(0.2, abs(val) * 0.01)
        try:
            changed, new = imgui.drag_int("##num_tv", val, change_speed=speed,
                                          min_value=0, max_value=0,
                                          flags=imgui.SLIDER_FLAGS_NO_INPUT)
        except Exception:
            _pop_styles()
            return False, s
    else:
        # Speed follows the literal's decimal places: one pixel of drag moves
        # the last significant digit (0.001 → 0.001/px), scaling up with
        # magnitude for large values. E-notation has no fixed precision, so
        # it stays purely magnitude-based.
        if '.' in s and 'e' not in s.lower():
            prec = min(6, max(1, len(s.split('.', 1)[1])))
            step = 10.0 ** -prec
        else:
            step = max(1e-6, abs(val) * 0.01)
        speed = max(step, abs(val) * 0.005)
        changed, new = imgui.drag_float("##num_tv", val, change_speed=speed,
                                        min_value=0, max_value=0, format=disp,
                                        flags=imgui.SLIDER_FLAGS_NO_INPUT)
    _pop_styles()
    if changed and new != val:
        return True, fmt_back(new)
    return False, s


def draw_bool_token_plain(input_value, width=20, height=20, name=None,
                          tint=None, text_tint=None, **kwargs):
    """draw_bool_token without the @render_func wrapper — raw imgui drawn
    directly into the editor's tile (the wrapper costs ~90µs/call, which adds
    up with many inline widgets; see draw_bool_token for the interaction
    rationale). Same call shape and (changed, value) return; the call site
    sets the cursor to the cell and passes width/height. No view, no
    draw_state, no event subscription — clicks fall through to the editor
    exactly as before (pass-through IS the bool widget's design; the caret-in
    fallback in draw_text still handles the double-click when the caret hides
    the widget)."""
    from meltygui.editor.text_editor import COLORS
    from meltygui.editor.text_editor import _plain_tv_bg

    word = input_value if input_value in ("True", "False") else "True"
    x, y = imgui.get_cursor_screen_pos()
    w = max(1.0, width)
    h = max(1.0, height)
    _plain_tv_bg(x, y, w, h, tint=tint, bg_offset=0)
    if text_tint is not None:
        color = pack_color(text_tint[0], text_tint[1], text_tint[2],
                                         text_tint[3] if len(text_tint) > 3 else 1.0)
    else:
        color = COLORS['bool']
    imgui.get_window_draw_list().add_text(x, y, color, word)
    io = imgui.get_io()
    if (x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h
            and imgui.is_mouse_double_clicked(0)):
        return True, ("False" if word == "True" else "True")
    return False, input_value


def draw_number_token_plain(input_value, width=20, height=20, name=None,
                            tint=None, text_tint=None, editor_ds=None,
                            max_bg_value=0.15, **kwargs):
    """draw_number_token without the @render_func wrapper — see that docstring
    for the interaction design (drag-only, caret via the editor's _tv_click
    path). What the wrapper used to provide is done inline:
    - Cursor/size come from the call site; drawing goes into the editor tile.
    - push_id(name): no per-view imgui ID scope anymore, and every widget
      shares the "##num_tv" label — without this all drags alias one item.
    - The left_mouse_* subscription that latched drags away from the editor
      is replaced by editor-side suppression: draw_text records this widget's
      rect (ds._plain_tv_rects) and its press/drag handlers skip caret and
      selection for gestures that start inside one. Registering with the
      InputHandler from here doesn't work — registrations are per-RENDERED-
      frame, and with event-driven rendering the editor body is usually
      cached on the frame whose registrations the press resolves against.
      The imgui drag itself needs no meltygui events; it runs off raw input.
    - While the drag is active the EDITOR tile is force-invalidated each
      frame: the imgui item only exists on frames the editor body runs, so a
      cached editor would freeze the drag after its first value change."""
    from meltygui.editor.text_editor import COLORS
    from meltygui.editor.text_editor import _parse_number_token
    from meltygui.editor.text_editor import _plain_tv_bg
    from meltygui.utils.glfw_utils import request_render

    from meltygui.utils.render_utils import push_style_var
    from meltygui.utils.render_utils import pop_style_var
    from meltygui.utils.render_utils import push_style_color
    from meltygui.utils.render_utils import pop_style_color
    s = input_value if isinstance(input_value, str) else str(input_value)
    kind, val, fmt_back, disp = _parse_number_token(s)
    x, y = imgui.get_cursor_screen_pos()
    if kind is None:
        imgui.get_window_draw_list().add_text(x, y, COLORS['number'], s)
        return False, s
    w = max(1.0, width)
    h = max(1.0, height)
    # Chrome (shadow + chip bg + drag-frame fill) only while the pointer is
    # on the chip or a drag is in flight; at rest the number reads as plain
    # text. Mid-drag the pointer can leave the rect, so the widget that was
    # active on the LAST body run (stashed in editor_ds) keeps its chrome.
    io = imgui.get_io()
    hovered = (x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h)
    show_chrome = hovered or (
        editor_ds is not None
        and getattr(editor_ds, '_tv_active_key', None) == name)
    # Hover-edge invalidation (same pattern as _fnrun_hover): the chrome only
    # draws when the cached editor tile repaints, so flip it on the edges.
    if editor_ds is not None:
        _hov_reg = getattr(editor_ds, '_tv_hover', None)
        if _hov_reg is None:
            _hov_reg = editor_ds._tv_hover = {}
        if _hov_reg.get(name) != show_chrome:
            _hov_reg[name] = show_chrome
            editor_ds.invalidate()
            request_render()
    if show_chrome:
        # Legibility guard (same pattern as button's max_bg_brightness): the
        # depth ramp + bleed can push the chip's fill bright in deeply nested
        # views, washing out the light digits - cap the painted value.
        # Tinted-comment chips are part of the comment's surface, not raised
        # above it - no shadow there.
        _plain_tv_bg(x, y, w, h, tint=tint, bg_offset=-1,
                     max_bg_value=max_bg_value,
                     shadow_offset=None if text_tint is not None else 1.0)

    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
    if not show_chrome:
        # Text only; the drag frame paints nothing at rest.
        if text_tint is not None:
            _ta = text_tint[3] if len(text_tint) > 3 else 1.0
            push_style_color(imgui.COLOR_TEXT, text_tint[0], text_tint[1],
                             text_tint[2], _ta)
        else:
            push_style_color(imgui.COLOR_TEXT, 0.41, 0.59, 0.73)  # number blue
        push_style_color(imgui.COLOR_FRAME_BACKGROUND, 0, 0, 0, 0)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED, 0, 0, 0, 0)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE, 0, 0, 0, 0)
        _n_colors = 4
    elif text_tint is not None:
        # The 4th text_tint component is the fade-out (see presentation dim
        # on colored comment widgets) - the drag-frame fill honors it too,
        # or it would paint over the dimmed chip at full opacity.
        _ta = text_tint[3] if len(text_tint) > 3 else 1.0
        push_style_color(imgui.COLOR_TEXT, text_tint[0], text_tint[1], text_tint[2], _ta)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND,
                         text_tint[0] * 0.14, text_tint[1] * 0.14, text_tint[2] * 0.14, _ta)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED,
                         text_tint[0] * 0.22, text_tint[1] * 0.22, text_tint[2] * 0.22, _ta)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE,
                         text_tint[0] * 0.30, text_tint[1] * 0.30, text_tint[2] * 0.30, _ta)
        _n_colors = 4
    else:
        push_style_color(imgui.COLOR_TEXT, 0.41, 0.59, 0.73)          # number chip
        # Never leave the dragged frame on imgui's default theme color (bright):
        # same dark editor-look fill the tinted branch uses, from number blue.
        push_style_color(imgui.COLOR_FRAME_BACKGROUND, 0.41 * 0.14, 0.59 * 0.14, 0.73 * 0.14)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED, 0.41 * 0.22, 0.59 * 0.22, 0.73 * 0.22)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE, 0.41 * 0.30, 0.59 * 0.30, 0.73 * 0.30)
        _n_colors = 4

    imgui.push_id(name or "num_tv")
    imgui.set_next_item_width(w)
    changed, new, active = False, val, False
    try:
        if kind == 'int':
            speed = max(0.2, abs(val) * 0.01)
            changed, new = imgui.drag_int("##num_tv", val, change_speed=speed,
                                          min_value=0, max_value=0,
                                          flags=imgui.SLIDER_FLAGS_NO_INPUT)
        else:
            # Speed rules identical to draw_number_token: one displayed digit
            # per step, scaling with magnitude; e-notation magnitude-only.
            if '.' in s and 'e' not in s.lower():
                prec = min(6, max(1, len(s.split('.', 1)[1])))
                step = 10.0 ** -prec
            else:
                step = max(1e-6, abs(val) * 0.01)
            speed = max(step, abs(val) * 0.005)
            changed, new = imgui.drag_float("##num_tv", val, change_speed=speed,
                                            min_value=0, max_value=0, format=disp,
                                            flags=imgui.SLIDER_FLAGS_NO_INPUT)
        active = imgui.is_item_active()
    except Exception:
        changed = False
    finally:
        imgui.pop_id()
        pop_style_color(_n_colors)
        pop_style_var()

    if active:
        if editor_ds is not None:
            Melty.cache.invalidate_up(editor_ds._tile_id, max_depth=10, force=True)
        request_render()
    # Chrome liveness for the show_chrome check above: track which widget
    # holds the drag so its chrome survives the pointer sliding off the chip.
    if editor_ds is not None:
        if active:
            editor_ds._tv_active_key = name
        elif getattr(editor_ds, '_tv_active_key', None) == name:
            editor_ds._tv_active_key = None
    if changed and new != val:
        return True, fmt_back(new)
    return False, s


@render_func(use_cache=True, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, z_offset=3, tint=(0.85, 0.45, 0.05))
def draw_color3_token(input_value, draw_state=None,
                      left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False,
                      **kwargs):
    """Inline color swatch for a color tuple — ACCESSORY (lead_cells) renderer
    for 'color3' tokens (`(1.0, 0.5, 0.2)` or RGBA `(1.0, 0.5, 0.2, 0.5)` —
    the fourth channel is alpha — with ints 0/1 allowed as channels; merged by
    tokenize). The editor draws the tuple TEXT itself, normally — fully
    editable, caret/selection like any code — and this widget only gets the
    lead area to its LEFT, where it draws a swatch (split alpha preview when 4
    channels). Clicking the swatch opens the MELTY color-picker popover (same
    pattern as draw_tuple's swatch: popover_focused_ds identity is the open
    state, the picker window is latched — drawn every frame with closed=
    toggled — anchored under the swatch, dismissed by outside click / Esc).
    NEVER imgui's built-in popup: meltygui windowing has diverged (shadows,
    z-order, cached render tiles) and they don't compose. Edits splice the
    reformatted tuple back — changed channels become float literals, untouched
    channels keep their original text — so the token re-merges.
    Draws nothing if the tuple doesn't parse (the text is still there).
    left_mouse_* declared (never read) for the event latch — see draw_number_token."""
    from meltygui.editor.text_editor import _fmt_color_channel
    from meltygui.utils.glfw_utils import request_render
    import meltygui.window_api as glfw

    from meltygui.debug.mode import Mode
    from meltygui.view.color_view import draw_color_picker
    s = input_value if isinstance(input_value, str) else str(input_value)
    parts = [p.strip() for p in s.strip('()').split(',')]
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return False, s
    if len(vals) not in (3, 4):
        return False, s
    has_alpha = len(vals) == 4
    r, g, b = vals[0], vals[1], vals[2]
    a = vals[3] if has_alpha else 1.0

    # Square-ish swatch inset in the lead area, vertically centered on the line;
    # the extra cell width to its right is the gap before the text.
    _sw = max(6.0, min(draw_state.width - 3, draw_state.height - 4))
    _cx, _cy = imgui.get_cursor_screen_pos()
    imgui.set_cursor_screen_pos((_cx, _cy + (draw_state.height - _sw) * 0.5))
    is_open = Melty.popover_focused_ds is draw_state
    # ALPHA_PREVIEW_HALF splits the swatch - half composited at the real alpha
    # over a checkerboard, half opaque - so RGBA transparency shows in the chip.
    flags = imgui.COLOR_EDIT_NO_TOOLTIP | (imgui.COLOR_EDIT_ALPHA_PREVIEW_HALF if has_alpha else 0)
    if imgui.color_button("##color3_tv", r, g, b, a,
                          flags=flags,
                          width=_sw, height=_sw):
        Melty.popover_focused_ds = None if is_open else draw_state
        if not is_open:
            Melty._popover_open_frame = Melty.frame_count  # grace the opening click
        request_render()
    is_open = Melty.popover_focused_ds is draw_state  # reflect the close this frame

    # Fixed-size popover (closable windows don't auto-resize; the picker body is
    # live imgui the framework can't measure): SV square + N channel rows + hex.
    from meltygui.view.color_view import color_picker_height
    from meltygui.view.color_view import color_picker_width
    from meltygui.view.color_view import color_picker_top_offset
    picker_h = color_picker_height(len(vals))
    color_changed, new_color = draw_color_picker(
        tuple(vals), name=f"{draw_state.name}_picker", closed=not is_open,
        window_pos=(0, color_picker_top_offset()), parent_window=draw_state, width=color_picker_width(), height=picker_h,
        mode=Mode.POPOVER)
    if is_open:
        if any(k == glfw.KEY_ESCAPE for k, _ in Melty.frame_key_events):
            Melty.popover_focused_ds = None
            request_render()
        # Keep re-rendering while a picker slider/square is being dragged so the
        # live imgui interaction updates each frame despite the editor's cache.
        if Melty.imgui_any_item_active or imgui.is_mouse_down(0):
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10, force=True)
            request_render()
        if color_changed and new_color is not None:
            out = [old_text if new_v == old_v else _fmt_color_channel(new_v)
                   for old_text, old_v, new_v in zip(parts, vals, new_color)]
            return True, "(" + ", ".join(out) + ")"
        if color_changed:
            # The picker's delete affordance returned None - meaningless for a
            # code literal. Just dismiss the popover and leave the text alone.
            Melty.popover_focused_ds = None
            request_render()
    return False, s


def draw_color3_token_plain(input_value, width=20, height=20, name=None,
                            editor_ds=None, **kwargs):
    """draw_color3_token without the @render_func wrapper — raw swatch drawn
    into the editor tile (see draw_number_token_plain for why: the ~90µs
    wrapper per widget per frame dominates with many inline widgets). Same
    ACCESSORY contract and (changed, value) return; interaction design in
    draw_color3_token's docstring. What the wrapper used to provide, inline:
    - The swatch had its own draw_state whose identity WAS the popover open
      state. Now the PICKER window's draw_state (latched via return_extras,
      like the AC popup) fills that role in Melty.popover_focused_ds — it's a
      real windowed view, so the nav-key wake (invalidate_up on the popover
      owner) climbs through parent_window=editor_ds and re-runs this code for
      Esc handling.
    - Toggle state can't be read back from popover_focused_ds alone: the
      swatch has no ds, so it is never in clear_focus's protect closure, and
      the very click meant to CLOSE the popover clears the slot before this
      body runs (reading the slot would then re-open it). editor_ds holds a
      per-widget latch (_c3_open_name) of what we last rendered; a latch-open
      widget whose picker lost the slot was dismissed externally (outside
      click, another popover) and closes.
    - Caret suppression for swatch presses comes from ds._plain_tv_rects —
      the call site registers the lead-area rect (owns_mouse), replacing the
      wrapper's left_mouse_* event latch.
    - While the picker is being dragged the EDITOR tile is force-invalidated
      each frame: the edit round-trips through the source splice, so a cached
      editor would freeze the value after its first change."""
    from meltygui.editor.text_editor import _color_swatch_plain
    from meltygui.editor.text_editor import _fmt_color_channel

    s = input_value if isinstance(input_value, str) else str(input_value)
    parts = [p.strip() for p in s.strip('()').split(',')]
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return False, s
    if len(vals) not in (3, 4):
        return False, s
    def _splice(new_color):
        return "(" + ", ".join(
            old_text if new_v == old_v else _fmt_color_channel(new_v)
            for old_text, old_v, new_v in zip(parts, vals, new_color)) + ")"
    return _color_swatch_plain(s, vals, _splice, width, height, name, editor_ds)


def draw_colorhex_token_plain(input_value, width=20, height=20, name=None,
                              editor_ds=None, **kwargs):
    """Inline color swatch for a hex-color STRING literal (`'#8888c6'`,
    `"#fff"`, RGBA `'#8888c680'`) — the 'colorhex' token kind, split off by
    _split_icons. Same ACCESSORY contract and picker as
    draw_color3_token_plain; an edit writes the color back as lowercase
    `#rrggbb` (`#rrggbbaa` when the literal carried alpha) inside the
    original quotes, so a 3-digit short form expands on its first edit."""
    from meltygui.editor.text_editor import _color_swatch_plain
    from meltygui.editor.text_editor import _parse_hex_color

    s = input_value if isinstance(input_value, str) else str(input_value)
    vals = _parse_hex_color(s)
    if vals is None or len(vals) not in (3, 4):
        return False, s
    quote = s[0]

    def _splice(new_color):
        chans = ''.join(f"{round(max(0.0, min(1.0, v)) * 255):02x}"
                        for v in new_color[:len(vals)])
        return f"{quote}#{chans}{quote}"
    return _color_swatch_plain(s, vals, _splice, width, height, name, editor_ds)


@render_func(show_bg=False, shadow=False, with_header=None, show_name=False,
             use_cache=False, selectable=False, is_tree=False)
def draw_fnrun_params_panel(input_value=None, draw_state=None, unique=0,
                            fnrun_file=None, fnrun_line=None,
                            fnrun_name=None, editor_ds=None,
                            auto_execute=False, editor_state=None, **kwargs):
    """Body of the def-widget's params window: a Run + instrumented-run row
    above the parameters dict. Both resolve + run the def exactly like the
    widget's inline play/eye buttons, using the panel's CURRENT (possibly
    just-edited) values; status lands in the editor's _fnrun_status, so the
    inline buttons show the same flash/error, and the eye triggers the same
    post-run live-view delivery. The parameters dict renders through its
    normal type routing; (changed, value) propagate to the widget's splice
    logic untouched."""
    from meltygui.editor.text_editor import _draw_fnrun_console
    from meltygui.editor.text_editor import _fnrun_console
    from meltygui.editor.text_editor import _fnrun_params_from_node
    from meltygui.editor.text_editor import _fnrun_queue_panel_splices
    from meltygui.editor.text_editor import _fnrun_start
    from meltygui.utils.glfw_utils import request_render

    from meltygui.view.header_view import flat_button
    from meltygui.views.new_core_view import draw_any
    run = flat_button(f" Run##fnpprun{unique}", draw_state,
                      f"fnpprun::{unique}", height=28,
                      color=(0.499, 0.844, 0.488), corner_radius=5.0,
                      shadow=True)
    imgui.same_line(spacing=6)
    live = flat_button(f" Run Visualize##fnpplive{unique}", draw_state,
                       f"fnpplive::{unique}", height=28, color=(0.13, 0.55, 0.13),
                        corner_radius=5.0, 
                       shadow=True)
    imgui.same_line(spacing=6)
    # Auto Execute: while on, any param edit below triggers Run Visualize
    # with the fresh values. The persisted per-def bool
    # (ScriptEditorState.params_auto_execute - the params_windows's
    # pattern) is read DIRECTLY every frame, like the visibility bool: the
    # `auto_execute` kwarg was captured when the def widget's body last ran
    # inside the editor's blit-cached tile, so it goes stale the moment the
    # checkbox writes the dict - the kwarg is only the no-state fallback.
    if editor_state is not None and fnrun_name:
        auto_execute = bool(editor_state.params_auto_execute.get(fnrun_name))
    _ae_ch, _ae_val = RenderFuncs.draw_bool(bool(auto_execute), name=f"Auto Execute##fnppae{unique}")
    if _ae_ch:
        auto_execute = _ae_val
        if editor_state is not None and fnrun_name:
            editor_state.params_auto_execute[fnrun_name] = _ae_val
        draw_state.invalidate()
        request_render()
    # Ctrl+Enter over the panel = the instrument button. Registered BLOCKING
    
    # with a priority above draw_main's root actions (draw_function_token's
    # pattern), so the main recompile-all flow never runs while the mouse
    # is over this window; the panel shell is use_cache=False, so the
    # subscription re-registers every time the window renders.
    if draw_state.on_action("ctrl_enter_down", priority_delta=1024):
        live = True
    # Params render BEFORE the run block (buttons already laid out above),
    # so an auto-executed run - and any click-triggered one - compiles
    # against the panel's just-edited node.
    _ch, _val = draw_any(input_value, name="parameters", child_kwargs={"syntax_highlight":False}, show_add_delete=False)
    if _ch and auto_execute:
        live = True
    if (run or live) and fnrun_file and fnrun_name and editor_ds is not None:
        _fnrun_start(editor_ds, fnrun_file, fnrun_line, fnrun_name,
                     instrumented=live,
                     params=_fnrun_params_from_node({'parameters': input_value}),
                     queue_if_busy=bool(_ch and auto_execute))
        draw_state.invalidate()
    if editor_ds is not None:
        console = _fnrun_console(editor_ds, (str(fnrun_file), fnrun_name))
        _draw_fnrun_console(console, draw_state, unique)
    # Keep errors in the panel's layout/scroll space, avoid gutter clipping.
    statuses = getattr(editor_ds, '_fnrun_status', {})
    status_key = (str(fnrun_file), fnrun_name)
    status = statuses.get(status_key)
    if status is not None and status[0] == 'err':
        imgui.text_wrapped(status[1])
        if flat_button(f"Dismiss error##fnpperr{unique}", draw_state,
                       f"fnpperr::{unique}", height=24):
            statuses.pop(status_key, None)
            if editor_ds is not None:
                editor_ds.invalidate()
            draw_state.invalidate()
            request_render()
    # A params edit writes the CODE from right here - this panel is the only
    # place that knows it happened, and it must not wait for the def widget
    # (which renders only while the def line is in the viewport: routing the
    # write through it left the text - what every run compiles - without the
    # panel's values whenever the def was scrolled away). With auto_execute
    # the edit already ran above; the write-back rides a trailing window so
    # a drag doesn't pay splice → reparse per tick. Otherwise it's immediate.
    if _ch and editor_ds is not None and fnrun_file and fnrun_name:
        _skey = (str(fnrun_file), fnrun_name)
        _pt = getattr(editor_ds, '_fnrun_hold_timer', None)
        if _pt is not None:
            _pt.cancel()
        if auto_execute:
            from meltygui.toggles import Toggles
            _hd = Toggles.TextEditor.fnrun_text_sync_debounce_ms / 1000.0
            import threading as _thr

            def _flush(_ed=editor_ds, _sk=_skey, _dn=fnrun_name,
                       _hint=fnrun_line or 0, _vals=input_value):
                _fnrun_queue_panel_splices(_ed, _sk, _dn, _hint, values=_vals)

            _t = _thr.Timer(max(_hd, 0.01),
                            lambda: Melty.post_to_render(_flush))
            _t.daemon = True
            editor_ds._fnrun_hold_timer = _t
            _t.start()
        else:
            _fnrun_queue_panel_splices(editor_ds, _skey, fnrun_name,
                                       fnrun_line or 0, values=input_value)
    return _ch, _val


def draw_run_fn_token_plain(input_value, width=20, height=20, name=None,
                            tint=None, text_tint=None, editor_ds=None,
                            file_path=None, def_line=None, def_name=None,
                            code_root=None, def_buf_line=None,
                            tv_text=None, def_disp_line=None,
                            editor_state=None, fn_tint=None,
                            **kwargs):
    """Run buttons for a function definition — GUTTER widget for 'def_name'
    tokens (`gutter: True` in DEFAULT_TOKEN_VIEWS): the name text draws
    normally in the code and this widget is drawn by the gutter pass IN
    PLACE OF the def line's number, sized to the number strip (`width` /
    `height` = the cell). Two flat_buttons: the double-wide PLAY runs the
    INSTRUMENTED twin (run_instrumented — live_view_forward's path), so
    every assignment publishes a snapshot and its marker anchors right in
    this editor via the snapshot overlay (inline, running always means
    inspecting); the sliders open the params panel. Both wear the
    function's definition tint (`fn_tint`, the def-block tint) when it has
    one. Errors print the colored traceback and surface on the run button
    (red, with a persistent wrapped message in the params panel until
    dismissed or replaced by the next run).

    Plain (wrapper-less) like the other token widgets. The click routes
    through flat_button's on_action claim on the EDITOR draw_state — the
    fast-dock model, so blit-cache event delivery holds — and owns_mouse
    registers the lead rect in _plain_tv_rects so a press here never moves
    the caret. Status is keyed by (file, def name), not the render-order
    `name` (that shifts as widgets scroll into view) and not the line
    (that shifts on edits)."""
    from meltygui.editor.text_editor import _fnrun_auto_exec_on_edit
    from meltygui.editor.text_editor import _fnrun_def_node_for
    from meltygui.editor.text_editor import _fnrun_detach
    from meltygui.editor.text_editor import _fnrun_panel_sync_entry
    from meltygui.editor.text_editor import _fnrun_param_src
    from meltygui.editor.text_editor import _fnrun_start
    from meltygui.editor.text_editor import _fnrun_sync_panel_params
    from meltygui.perf_trace import trace as _ptrace
    from meltygui.utils.glfw_utils import request_render

    from meltygui.view.header_view import flat_button
    x, y = imgui.get_cursor_screen_pos()
    if editor_ds is None:
        return False, input_value

    statuses = getattr(editor_ds, '_fnrun_status', None)
    if statuses is None:
        statuses = editor_ds._fnrun_status = {}
    skey = (str(file_path), def_name)
    status = statuses.get(skey)

    # Hover-edge invalidation: flat_button's hover styling only shows when the
    # (cached) editor tile repaints, so paint the tile exactly on the edges.
    io = imgui.get_io()
    hovered = (x <= io.mouse_pos.x < x + width
               and y <= io.mouse_pos.y < y + height)
    hov_reg = getattr(editor_ds, '_fnrun_hover', None)
    if hov_reg is None:
        hov_reg = editor_ds._fnrun_hover = {}
    if hov_reg.get(skey) != hovered:
        hov_reg[skey] = hovered
        editor_ds.invalidate()

    # Function tint (the def-block tint) colors both buttons when the def
    # has one; otherwise the live blue / neutral grey defaults.
    _has_tint = fn_tint is not None and len(fn_tint) >= 3
    _c_live = (tuple(fn_tint[:3]) if _has_tint
               else (0.40, 0.53, 0.78))    # draw_function_live's lab blue
    _c_pp = tuple(fn_tint[:3]) if _has_tint else (0.55, 0.58, 0.66)
    if status is not None and status[0] == 'err':
        _c_live = (0.85, 0.25, 0.20)
    # Two buttons: the DOUBLE-WIDE instrumented run (play glyph) -
    # live_view_forward's twin path via run_instrumented, so every
    # assignment's snapshot marker lands right in THIS editor through the
    # snapshot overlay - and the params-panel toggle (sliders glyph).
    _gap = 3.0
    _unit = max(6.0, (width - 4.0 - 2 * _gap) / 3.0)
    _bw_run = 2 * _unit + _gap
    _bh = max(6.0, height - 4.0)
    _by = y + (height - _bh) * 0.5
    imgui.set_cursor_screen_pos((x, _by))
    live_clicked = flat_button(f"\uf04b##{name}lv", editor_ds,
                               f"fnrunlv::{name}",
                               width=_bw_run, height=_bh, color=_c_live,
                               corner_radius=4.0, shadow=True)
    imgui.set_cursor_screen_pos((x + _bw_run + _gap, _by))
    params_clicked = flat_button(f"\uf1de##{name}pp", editor_ds,
                                 f"fnrunpp::{name}",
                                 width=_unit, height=_bh,
                                 color=_c_pp,
                                 corner_radius=4.0, shadow=True)

    # ── Params panel: the def's `parameters` sub-dict from the cst tree
    # (already injected into draw_text - no cost), rendered with draw_any as
    # a latching closable window. An edit round-trips as PARTIAL CODE
    # INSERTION: the changed param's expression is spliced into the
    # signature via the editor's token-edit channel (_fnrun_splices), so it
    # saves/undoes like a keystroke, and the node is updated in-place so
    # the next run picks the change up immediately.
    # Visibility IS a persisted bool: TextEditorState.params_windows_open
    # (the TabState pattern - kept in draw_text's draw_state.misc and
    # serialized with it). Read directly every frame - no open-prev latch;
    # the toggle click and the header X just write the bool.
    _pp_vis = (editor_state.params_windows_open
               if editor_state is not None else {})
    if params_clicked:
        _pp_vis[def_name] = not _pp_vis.get(def_name, False)
    elif live_clicked and not _pp_vis.get(def_name, False):
        # A manual run SHOWS the params panel (the run itself happens below,
        # in the live_clicked branch) - it never closes an open panel.
        _pp_vis[def_name] = True
    _pp_wins = getattr(editor_ds, '_fnrun_params_wins', None)
    if _pp_wins is None:
        _pp_wins = editor_ds._fnrun_params_wins = {}
    _pw = _pp_wins.get(skey)
    # Header-X lands during the DEFERRED closing frame (after our stamp
    # closing frame) - the persistent window ds carries closed=True now;
    # mirror it into the visibility bool before reading it.
    if (_pw is not None and _pw.closed and not params_clicked
            and _pp_vis.get(def_name)):
        _pp_vis[def_name] = False
    _pp_want = bool(_pp_vis.get(def_name))
    _params_node = None
    if _pp_want or params_clicked or (_pw is not None and not _pw.closed):
        # Lazy node resolution - only for the toggle click, while the panel
        # is open, or for the one closing click; closed-panel frames pay
        # nothing (the resolve walk is click-scale cheap, not frame-scale).
        _def_node = _fnrun_def_node_for(editor_ds, skey, code_root,
                                        def_name, def_buf_line, tv_text)
        _params_node = (_def_node.get('parameters', {})
                        if isinstance(_def_node, dict) else None)
    # DISPLAYED-node latch (also read further down): the background reparse
    # rebuilds the tree mid-typing, so the node handed to the panel is held
    # across frames and updated in place panel-side.
    _shown_map = getattr(editor_ds, '_fnrun_shown_nodes', None)
    if _shown_map is None:
        _shown_map = editor_ds._fnrun_shown_nodes = {}
    if _params_node is None and _pp_want:
        # Transient miss - the cst tree is mid-rebuild after an edit (a
        # newline shifting the def used to land here every time), or a
        # held/failed parse. Keep the panel up on the node it last showed;
        # closing is the USER's act (toggle click / header X), never a
        # parse hiccup's. Only a def with no node ever seen closes.
        _params_node = _shown_map.get(skey)
        if _params_node is None:
            _pp_vis[def_name] = False
            _pp_want = False
    if _params_node is not None and (
            _pp_want or (_pw is not None and not _pw.closed)):
        from meltygui.debug.mode import Mode
        # Seed geometry through `initial`, never a per-frame window_pos:
        # the panel starts at the parent's right edge and keeps user drags
        # and resizing on subsequent frames. The default position is (0, 0),
        # so force the initial seed only while no panel instance exists.
        _panel_width = 400
        _left, _top, _right, _bottom = editor_ds.abs_clip_rect
        _panel_initial = {
            "width": _panel_width,
            "window_pos": (
                max(_left + 16, _right - _panel_width - 16) - editor_ds.abs_left,
                _top + 16 - editor_ds.abs_top),
        }
        # DISPLAYED node latch: the background reparse (small-file average
        # ~119ms) rebuilds the tree MID-TYPING, so restamping the fresh
        # `_params_node` per run showed half-typed defaults in the panel
        # instead of the panel-sync debounce below - the reparse was a
        # second, undebounced channel into the panel. Hold the last shown
        # node and hand THAT to the panel; fresh signature values are
        # merged in place in the debounce-expiry branch below.
        _shown = _shown_map.get(skey)
        if _shown is None:
            _shown = _shown_map[skey] = _fnrun_detach(_params_node)
        # Per-param "source the panel last SAW": a param whose rendered
        # source still equals that was not touched in the panel, and the
        # splice below must leave its expression alone: the shown node can be
        # an older parse than the code (that is the latch's point), and
        # writing the changed param back snapped typed-in defaults
        # back to stale values ("the inputs revert when a live view
        # opens"). Seeded from the node on first show; advanced by the
        # text→panel sync and by each splice.
        _seen_map = getattr(editor_ds, '_fnrun_param_seen', None)
        if _seen_map is None:
            _seen_map = editor_ds._fnrun_param_seen = {}
        _seen = _seen_map.get(skey)
        if _seen is None:
            _seen = _seen_map[skey] = {
                _pk: _fnrun_param_src(_pv) for _pk, _pv in _shown.items()
                if isinstance(_pk, str) and not _pk.startswith('__')}
        # Mode.WINDOW replaces caller `initial` with its own defaults.
        # Apply its chrome as kwargs so this panel can supply its own seed.
        _panel_kwargs = dict(Mode.WINDOW.get_config_for(_shown).kwargs)
        _panel_kwargs["initial"] = _panel_initial
        _pch, _pnv, _pw = draw_fnrun_params_panel(
            _shown, name=f"{def_name} params##fnpp::{def_name}",
            **_panel_kwargs, closed=not _pp_want,
            parent_window=editor_ds, return_extras=True, swoosh=False,
            force_initial=_pw is None,
            fnrun_file=file_path, fnrun_line=def_line, fnrun_name=def_name,
            editor_ds=editor_ds,
            auto_execute=bool(editor_state.params_auto_execute.get(def_name))
            if editor_state is not None else False,
            editor_state=editor_state)
        _pp_wins[skey] = _pw
        if _pw.closed and _pp_want and not params_clicked:
            _pp_vis[def_name] = False   # X-closed inline (same-frame close)
            _pp_want = False
        # TEMP diag: which link of the panel→splice hop fires (remove with
        # the other fnrun diag once the value window round-trip is solid).
        _ptrace("fnrun widget panel-ret", def_name=def_name, pch=bool(_pch),
                pnv=type(_pnv).__name__, tv=tv_text is not None)

        # The panel writes its own edits to the code (draw_fnrun_params_panel
        # → _fnrun_queue_panel_splices); this widget only syncs TEXT → PANEL.
        if isinstance(_params_node, dict) and tv_text is not None:
            # The window renders later than this widget. Keep its input node
            # stable, and merge it only after the trailing debounce.
            from meltygui.toggles import Toggles
            _dbc = Toggles.TextEditor.fnrun_text_sync_debounce_ms / 1000.0
            _ent = _fnrun_panel_sync_entry(editor_ds, skey)
            _due = False
            if _ent[0] is not tv_text:
                _ent[0] = tv_text
                _ent[1] = time.monotonic() + _dbc
                if _dbc <= 0:
                    _due = True
                else:
                    import threading as _thr
                    _pt = getattr(editor_ds, '_fnrun_sync_timer', None)
                    if _pt is not None:
                        _pt.cancel()
                    _t = _thr.Timer(_dbc, request_render)
                    _t.daemon = True
                    editor_ds._fnrun_sync_timer = _t
                    _t.start()
            elif _ent[1] is not None:
                if time.monotonic() >= _ent[1]:
                    _ent[1] = None
                    _due = True
                else:
                    # Inside the quiet window: the timer above produces the
                    # expiry frame; keep this tile un-cached until then so
                    # the widget actually re-runs on it.
                    editor_ds.invalidate()
            if _due:
                _synced = _fnrun_sync_panel_params(
                    _shown, _seen, _ent[2], _params_node, tv_text,
                    def_disp_line or 0)
                if (_synced and _pw is not None
                        and _pw._tile_id is not None):
                    Melty.cache.invalidate_up(_pw._tile_id, force=True,
                                              max_depth=8)
                    request_render()
    _fnrun_auto_exec_on_edit(editor_ds, editor_state, skey, file_path,
                             def_line, def_name, code_root, def_buf_line,
                             tv_text, def_disp_line, status)
    if live_clicked:
        _fnrun_start(editor_ds, file_path, def_line, def_name, instrumented=True)
    return False, input_value


@render_func(is_default_for=(CodeLine), show_bg=True, use_cache=True, disable_scroll=False, with_header=draw_header,
             shadow=False, max_bg_depth=0, max_bg_value=0.05,
             show_name=False, with_footer=draw_footer, determines_height=False, saturation=1.7,
             selectable=False, searchable=True, bg_offset=-0.6, show_add_delete=False)
@window
def draw_text(input_value: str, height=None,
              left_mouse_down=False,
              left_mouse_drag=False, left_mouse_held=False,
              horizontal_scroll_drag=False, search_text="", 
              ctrl_b_down=False, ctrl_shift_b_down=False,
              ctrl_minus_down=False, ctrl_equal_down=False,
              ctrl_shift_minus_down=False, ctrl_shift_equal_down=False,
              single_line=False, is_search_box=False, focusable=True,
              draw_state=None, text_editor_state: TextEditorState = None,
              request_focus=False, select_all_on_focus=False,
              wrap=False, line_height=1.2, font=Font.FONTAWESOME_MONO_19, jump_to=None,
              code_tree=None, code_dict=None, error=None, token_views=None,
              live_store=None,
              import_fixes=None,
              syntax_highlight=True, syntax_language="python", text_tint=None, is_diff=False, line_numbers=None,
              completion_source=None, show_jump_bar=True, show_file_header=True,
              manual_search=False, fold_ranges=None, scope_collapse=True,
              default_collapsed_lines=None,
              diff_fold_ranges=None, expand_diff=None,
              gutter_indent=False,
              scroll_bar_width=8.0, scroll_bar_brightness=5.9,
              autocomplete=True, unique=0,
              show_widgets=True, show_root_backgrounds=True,
              highlight_token_matches=True, roster_live_hold=True,
              roster_world=None, roster_table=None,
              fim="", fim_state: FimState = None,
              source_tools: SourceToolsState = None):
    """`show_widgets=False` hides every inline token widget (run/eye buttons,
    number drags, bool switches, icon pickers -- the token_views layer).
    `highlight_token_matches=False` turns off the caret-rest same-token wash
    for this editor (embeds like global-search rows: the wash, drawn under
    the definition tints, read as washed-out symbol colours there).
    `show_root_backgrounds=False` skips the definition block wash of ROOT
    symbols (blocks no other block in this buffer contains) -- for embeds
    that paint the enclosing class's background themselves (global search
    rows), so the wash isn't drawn twice. Honoured only while
    Toggles.TextEditor.root_symbol_tints is False.
    `roster_live_hold=False` marks this buffer a READ-ONLY preview of its
    file (global-search rows): its def tints resolve against the roster's
    pending table instead of installing the buffer as the file's live
    override (see roster_tints.collect_def_tints).
    `roster_world` (a symbol_roster.World) / `roster_table` (a
    detached_table): the buffer shows ANOTHER version of its file (the merge
    window's disk / sync-frame / staged panes) — its definition tints resolve
    through that world's tables / its own detached table, never the
    studio's hold for the file; either also lets the file path come from
    `file_key` when there is no `jump_to`."""
    from meltygui.editor.text_editor import COLORS
    from meltygui.editor.text_editor import DEFAULT_TOKEN_VIEWS
    from meltygui.editor.text_editor import GENERIC_ICON
    from meltygui.editor.text_editor import _COMMENT_TINT_CACHE
    from meltygui.editor.text_editor import _FIM_NON_TRIGGER_KEYS
    from meltygui.editor.text_editor import _FOLD_SEED_VER
    from meltygui.editor.text_editor import _FoldLineNumbers
    from meltygui.editor.text_editor import _KEY_CHAR_MAP
    from meltygui.editor.text_editor import _LoadingSentinel
    from meltygui.editor.text_editor import _REPEATABLE_KEYS
    from meltygui.editor.text_editor import _WinVCols
    from meltygui.editor.text_editor import _ac_apply_auto_import
    from meltygui.editor.text_editor import _ac_import_rows
    from meltygui.editor.text_editor import _ac_lex_state
    from meltygui.editor.text_editor import _ac_live_context
    from meltygui.editor.text_editor import _ac_param_suffixes
    from meltygui.editor.text_editor import _ac_pick_insert
    from meltygui.editor.text_editor import _apply_import_fix
    from meltygui.editor.text_editor import _bg_adjust
    from meltygui.editor.text_editor import _block_open_extra
    from meltygui.model.color_model import _brightness_clamp
    from meltygui.editor.text_editor import _build_vcols
    from meltygui.editor.text_editor import _call_context
    from meltygui.editor.text_editor import _carry_diff_collapse
    from meltygui.editor.text_editor import _char_pos_to_xy
    from meltygui.editor.text_editor import _code_tree_errors
    from meltygui.editor.text_editor import _comment_continuation
    from meltygui.editor.text_editor import _comment_tint_color
    from meltygui.editor.text_editor import _comment_tints
    from meltygui.editor.text_editor import _compile_check_more
    from meltygui.editor.text_editor import _completion_context
    from meltygui.editor.text_editor import _completion_pool
    from meltygui.editor.text_editor import _completion_popup_rect
    from meltygui.editor.text_editor import _completion_replace_end
    from meltygui.editor.text_editor import _completion_selection
    from meltygui.editor.text_editor import _def_tints
    from meltygui.editor.text_editor import _defining_keyword_before
    from meltygui.editor.text_editor import _delete_selection
    from meltygui.editor.text_editor import _diagnostic_line_text
    from meltygui.editor.text_editor import _diff_usage_maps
    from meltygui.editor.text_editor import _display_edit_splice
    from meltygui.editor.text_editor import _display_splice_shift
    from meltygui.editor.text_editor import _draw_cst_token_views
    from meltygui.editor.text_editor import _draw_fim_ghost
    from meltygui.editor.text_editor import _draw_signature_hint
    from meltygui.editor.text_editor import _enclosing_editor_window
    from meltygui.editor.text_editor import _ensure_member_completions
    from meltygui.editor.text_editor import _ensure_signature_help
    from meltygui.editor.text_editor import _exception_errors
    from meltygui.editor.text_editor import _fade_packed
    from meltygui.editor.text_editor import _filter_completions
    from meltygui.editor.text_editor import _fim_poll
    from meltygui.editor.text_editor import _find_matches
    from meltygui.editor.text_editor import _fnrun_auto_exec_scan
    from meltygui.editor.text_editor import _fnrun_resolve_splices
    from meltygui.editor.text_editor import _focus_in_context_menu_over
    from meltygui.editor.text_editor import _fold_build
    from meltygui.editor.text_editor import _fold_carry
    from meltygui.editor.text_editor import _fold_header_map
    from meltygui.editor.text_editor import _fold_headless_set
    from meltygui.editor.text_editor import _fold_normalize_ranges
    from meltygui.editor.text_editor import _fold_reassemble
    from meltygui.editor.text_editor import _fold_rekey
    from meltygui.editor.text_editor import _fold_update_inline
    from meltygui.editor.text_editor import _get_indent
    from meltygui.editor.text_editor import _get_line_end
    from meltygui.editor.text_editor import _get_line_start
    from meltygui.editor.text_editor import _has_selection
    from meltygui.editor.text_editor import _import_line_context
    from meltygui.editor.text_editor import _in_comment_override
    from meltygui.editor.text_editor import _indent_lines
    from meltygui.editor.text_editor import _index_to_line_col
    from meltygui.editor.text_editor import _is_highlightable_word
    from meltygui.editor.text_editor import _kind_tag
    from meltygui.editor.text_editor import _line_col_to_index
    from meltygui.editor.text_editor import _line_offsets
    from meltygui.editor.text_editor import _line_offsets_cached
    from meltygui.editor.text_editor import _line_starts
    from meltygui.editor.text_editor import _log_usage_mismatch
    from meltygui.editor.text_editor import _missing_name
    from meltygui.editor.text_editor import _mix_packed
    from meltygui.editor.text_editor import _open_bracket_indent
    from meltygui.editor.text_editor import _open_usage_ref
    from meltygui.editor.text_editor import _parse_col_shift
    from meltygui.editor.text_editor import _pending_line_delta
    from meltygui.editor.text_editor import _pos_in_string_or_comment
    from meltygui.editor.text_editor import _recover_def_pos
    from meltygui.editor.text_editor import _reindent_paste
    from meltygui.editor.text_editor import _same_guide_shape
    from meltygui.editor.text_editor import _scope_fold_ranges
    from meltygui.editor.text_editor import _scope_guide_segments
    from meltygui.editor.text_editor import _scope_guide_tints
    from meltygui.editor.text_editor import _scroll_into_view
    from meltygui.editor.text_editor import _sel_range
    from meltygui.editor.text_editor import _select_unit_left
    from meltygui.editor.text_editor import _select_unit_right
    from meltygui.editor.text_editor import _site_span
    from meltygui.editor.text_editor import _snippet_context
    from meltygui.editor.text_editor import _split_gaps_at_collapsed
    from meltygui.editor.text_editor import _split_tokens_at
    from meltygui.editor.text_editor import _string_neutral_ranges
    from meltygui.editor.text_editor import _string_split
    from meltygui.editor.text_editor import _text_splice
    from meltygui.editor.text_editor import _toggle_comment
    from meltygui.editor.text_editor import _tree_usages_on_line
    from meltygui.editor.text_editor import _typing_hot
    from meltygui.editor.text_editor import _uj_file_tint
    from meltygui.editor.text_editor import _uj_log
    from meltygui.editor.text_editor import _unclosed_opener
    from meltygui.editor.text_editor import _unit_left_of
    from meltygui.editor.text_editor import _update_line_open
    from meltygui.editor.text_editor import _update_line_widths
    from meltygui.editor.text_editor import _usage_jump_targets
    from meltygui.editor.text_editor import _usage_spans
    from meltygui.editor.text_editor import _usage_target_count
    from meltygui.editor.text_editor import _usage_user_counts
    from meltygui.editor.text_editor import _usage_wash_color
    from meltygui.editor.text_editor import _window_tokens
    from meltygui.editor.text_editor import _word_boundary_left
    from meltygui.editor.text_editor import _word_boundary_right
    from meltygui.editor.text_editor import _word_match_ranges
    from meltygui.editor.text_editor import _word_under_cursor
    from meltygui.editor.text_editor import _xy_to_char_index
    from meltygui.editor.text_editor import fold_child_scopes
    from meltygui.editor.text_editor import fold_project_jump
    from meltygui.editor.text_editor import fold_root_scopes
    from meltygui.editor.text_editor import jump_emph_cols
    from meltygui.perf_trace import trace as _ptrace
    from meltygui.utils.glfw_utils import request_render
    from meltygui.view.code_view import draw_jump_to
    from meltygui.view.search_view import draw_search_highlight_multi
    from meltygui.views.blit_offscreen import add_glow
    from meltygui.views.blit_offscreen import add_shadow
    from meltygui.views.blit_offscreen import clear_glows
    import meltygui.mouse_cursor as mouse_cursor
    import meltygui.window_api as glfw

    ds = draw_state
    # ── Instant restore (input_value==LOADING) ─────────────────────────────
    # A caller whose real buffer is still loading (draw_code_editor's
    # loading_frame) passes the LOADING sentinel: rebuild a same-shape
    # stand-in from the persisted viewport snapshot (TextEditorState - see
    # its restore_ fields): blank lines up to the visible band, the band's
    # captured text, blank lines after. Same line count → same content
    # height → the persisted scroll lands unmoved, so this editor's first
    # frame shows the code the last one did, before any disk read. The real
    # text swaps in through the normal content-change path when it loads;
    # edits to the stand-in are discarded (changed forced back at the
    # return). The snapshot capture at the tail skips restore, so the
    # stand-in never overwrites the real snapshot. (A sentinel, not None:
    # the render_func() serves its input cache for None without actually
    # running this body.)
    restore_active = isinstance(input_value, _LoadingSentinel)
    _restore_caret = None
    if restore_active:
        # The stand-in is display-SHAPED (line count) but not length-faithful
        # - mostly bare newlines. The persisted caret (a char offset into the
        # REAL buffer) would clamp to the stand-in's short tail - the file's
        # LAST line - and the caret-rest would slam the display scroll to
        # EOF (and permanently corrupt the caret). Caret state is frozen
        # across stand-in frames: stashed here, put back at the return, so
        # both caret-followers sit restore frames out (they're mid-body,
        # before the put-back).
        _restore_caret = (ds.text_cursor_pos, ds.text_selection_start,
                          ds.text_selection_end)
        if text_editor_state is not None and text_editor_state.restore_text:
            _before = max(0, int(text_editor_state.restore_first_line))
            _snap_lines = text_editor_state.restore_text.count("\n") + 1
            _after = max(0, int(text_editor_state.restore_total_lines)
                         - _before - _snap_lines)
            input_value = ("\n" * _before + text_editor_state.restore_text
                           + "\n" * _after)
        else:
            input_value = ""

    cursor_pos = imgui.get_cursor_screen_pos()  # ← cursor_pos = (13.5, 13.5)

    # --- Perf instrumentation (typing latency) --------------------------------
    # Section marks: each _pf(label) closes the section since the previous mark.
    # One summary line per edited frame — plus any frame >= 8ms — goes to the
    # pe_trace timeline (/tmp/lsd_symbol_perf.log) so draw_text's own cost can
    # be read against the background reparse/index lines around it.
    # [tint=(0.483, 0.397, 0.054, 1.0), show_tint=True\]
    _pf_t0 = time.perf_counter()
    _pf_cpu0 = time.thread_time()   # wall≫cpu in the summary = GIL starvation
    _pf_marks = []
    _pf_tok = [0.0, 0]   # accumulated _window() cache-miss time, miss count
    _pf_info = {}        # extra facts for the summary line (span counts, cache hits)
    def _pf(
            label):
        _pf_marks.append((label, time.perf_counter()))

    # --- Collapsible line ranges (fold_ranges=[(s, e), ...]) -----------------
    # Each (start, end) tuple (0-based inclusive buffer lines) is a fold: a
    # badge at the end of line `start` toggles it, and while collapsed lines
    # start+1..end are spliced OUT of the text the body sees - layout, caret,
    # search and tokenize all run on the display text, so all of the linear
    # y = origin_y + line * line_px sites need remapping. The hidden segments
    # are spliced back IN before the changed line, so the caller always
    # round-trips the FULL buffer. Collapse state lives on the draw_state
    # (ds._fold_collapsed, a set of the range tuples).
    _fold_segments, _fold_folds, _fold_d2b = [], None, None
    _fold_full = input_value    # the FULL buffer, kept across the display
                                # substitution below - tree-derived overlays
                                # (washes, error markers) resolve against it
                                # and are then remapped into display coords.
    _fold_bl = _fold_remap_spans = None
    # scope_collapse=True derives fold_ranges from the buffer itself: one
    # fold per Python def/class scope (nested scopes collapse - see
    # _scope_fold_ranges). Re-derived only when the buffer changes; an empt
    # fold_ranges passes. Gated on syntax_highlight - plain-text buffers have
    # no Python context.
    _fold_default_col = None
    # diff_fold_ranges (compare splits): a SECOND, caller-owned span set -
    # the unchanged gaps between code blocks (open_files._diff_gap_folds)
    # - that lives ALONGSIDE the scope folds instead of replacing them. Both
    # sets splice into the one display layout below, each with its own
    # collapse state: scope folds keep ds._fold_keys / _fold_collapsed
    # (keyboard shortcuts, default-collapsed seeding, session restore) while
    # diff folds track on ds._diff_fold_collapsed, seeded by `expand_diff`.
    # In-function import, same cycle-avoidance as the main Toggles import
    # further down (which harmlessly re-binds the same name).
    from meltygui.toggles import Toggles
    _fold_key_of = None
    # not restore_active: the loading stand-in is ALREADY display-shaped
    # (the snapshot captured fold-spliced display text), so fold processing
    # on it is meaningless - worse, the per-frame key⟷tuple round-trip
    # ("harvested back to keys before the build") projects the seeded
    # restore_fold_keys onto the placeholder's foldless ranges and harvests
    # back an EMPTY set, destroying the persisted fold state before the
    # real text even lands. The fold layer sits the stand-in frames out.
    if (scope_collapse and not fold_ranges and syntax_highlight
            and Toggles.TextEditor.scope_fold_ranges
            and not restore_active
            and not single_line and not is_search_box):
        _sc = getattr(ds, '_scope_rng_cache', None)
        # Keyed on text identity AND _FOLD_SEED_VER: a hotswap that changed
        # the default_collapsed sources must not serve a pre-swap scan to
        # the versioned reseed below. Cache: (text, scan_result, ver,
        # provisional) - provisional entries were splice-carried, not
        # scanned, and are replaced by a real scan once input quiets.
        _sc_ok = (_sc is not None and len(_sc) >= 4
                  and _sc[2] == _FOLD_SEED_VER)
        _sc_hit = _sc_ok and _sc[0] is input_value
        if not _sc_hit and _sc_ok and _typing_hot():
            # Typing burst: the O(file) rescan (~22ms on a large buffer) is
            # this editor's primary per-keystroke cost - skip it and carry
            # the held ranges across the edit (see _fold_carry). A None
            # return (collapsed-fold header verification failed) falls
            # through to the real scan below: never serve ranges that point
            # onto the wrong lines.
            _carried = _fold_carry(_sc[0], input_value, _sc[1],
                                   getattr(ds, '_fold_keys', None))
            if _carried is not None:
                _sc = (input_value, _carried, _FOLD_SEED_VER, True)
                ds._scope_rng_cache = _sc
                _sc_hit = True
        if not _sc_hit or (_sc[3] and not _typing_hot()):
            _new_sc = (input_value, _scope_fold_ranges(input_value),
                       _FOLD_SEED_VER, False)
            if _sc_ok and getattr(ds, '_fold_keys', None):
                # The rescan may have re-identified folds (a collapsed
                # comment block's header line edited by a value drag / typing,
                # a def renamed): move each collapse key onto its fold range
                # now range where its fold landed, or the fold pops open the
                # moment the scan lands (see _fold_rekey). Against the
                # held entry - the old text, not the splice-carried
                # provisional (same text, old keys) on the trailing rescan.
                ds._fold_keys = _fold_rekey(_sc[0], _sc[1], input_value,
                                            _new_sc[1], ds._fold_keys)
                if getattr(ds, '_fold_search_exp_keys', None):
                    ds._fold_search_exp_keys = _fold_rekey(
                        _sc[0], _sc[1], input_value, _new_sc[1],
                        ds._fold_search_exp_keys)
            _sc = _new_sc
            ds._scope_rng_cache = _sc
        if _sc[3]:
            request_render()   # provisional: the trailing rescan needs a frame
        fold_ranges, _fold_default_col, _fold_key_of = _sc[1]
    # --- DIFF fold layer (diff_fold_ranges + expand_diff) --------------------
    # Collapse state per diff span lives on ds._diff_fold_collapsed, a set of
    # the normalized range tuples (no keys - the spans re-derive from the
    # live diff every frame and are carried across drift by overlap below).
    # `expand_diff` is the OWNER's tri-state switch (draw_code_editor /
    # merge_files auto-state - one value for the whole editor): True keeps
    # EVERY span expanded proactively, False keeps every span collapsed
    # (new gaps included; only the search reveal opens spans, restored by
    # its own machinery), None is neutral - each span's badge state is
    # tracked individually and carried across drift by overlap. A manual
    # badge toggle bumps ds._diff_manual_gen; the owner watches that and
    # clears its switch to None in the same frame, so the toggle sticks.
    _diff_rngs, _diff_rng_set = [], frozenset()
    if (diff_fold_ranges and not restore_active and not single_line
            and not is_search_box):
        # Caller gaps → the layer's working ranges: normalize, then
        # string-neutral (multiline string delimiters must never be within
        # one line). A gap stays one range - one header row, one badge,
        # one separator band per unchanged stretch (Lukas 09-01: "a single
        # line between each change"). Gaps and scope folds share the union
        # layout WITHOUT nesting: _fold_build hides the union of what every
        # collapsed range hides, so a gap straddling a def and the def's own
        # fold coexist, each with its chevron (the earlier prune of
        # straddling scopes lost their chevrons, the nest-split before it
        # fragmented every gap). A gap EQUAL to a scope range loses to the
        # scope (same lines, the scope's chevron does the job). Memoized:
        # the gaps re-derive every frame on buffer change.
        _n_lines = len(_line_starts(input_value))
        _gaps_norm = _fold_normalize_ranges(_n_lines, diff_fold_ranges)
        # COLLAPSED scope folds (last frame's projection of the durable
        # state) affect the gaps - _split_gaps_at_collapsed - so they join
        # the memo key (set equality: a few hundred lines at most).
        _scope_col_now = getattr(ds, '_fold_collapsed', None) or set()
        _dsm = getattr(ds, '_diff_split_memo', None)
        if (_dsm is not None and _dsm[0] == _gaps_norm
                and _dsm[1] is fold_ranges and _dsm[2] is input_value
                and _dsm[5] == _scope_col_now):
            _diff_rngs = _dsm[3]
            ds._diff_gap_index = _dsm[4]
        else:
            _forest = _fold_normalize_ranges(_n_lines, fold_ranges or ())
            # Split around collapsed straddlers and clamp for string
            # safety to a FIXED POINT: the clamp's cut can turn a scope
            # nested in the raw gap into a straddler of a piece (and a
            # split piece can trigger a new clamp) - 2 or 3 rounds in
            # practice, 6 as a guard.
            _pieces = list(_gaps_norm)
            for _round in range(6):
                _next = _split_gaps_at_collapsed(_pieces, _forest, _scope_col_now)
                if syntax_highlight:
                    _next = _string_neutral_ranges(ds, input_value, _next)
                if _next == _pieces:
                    break
                _pieces = _next
            _forest = set(_forest)
            _pieces = [g for g in _pieces if g not in _forest]
            _diff_rngs = _fold_normalize_ranges(_n_lines, _pieces)
            # Piece -> owning-gap ordinal, for the two-level owner (pane
            # sync / switch inference in open_files): the panes' gaps
            # correspond by index across the compare split, their pieces
            # don't (each side splits against its own scope structure).
            _gap_starts = [g[0] for g in _gaps_norm]
            _gap_of = {}
            for _p in _diff_rngs:
                _gi = bisect.bisect_right(_gap_starts, _p[0]) - 1
                if _gi >= 0:
                    _gap_of[_p] = _gi
            ds._diff_gap_index = _gap_of
            ds._diff_split_memo = (_gaps_norm, fold_ranges, input_value,
                                   _diff_rngs, _gap_of, set(_scope_col_now))
        _diff_rng_set = frozenset(_diff_rngs)
        _diff_col = getattr(ds, '_diff_fold_collapsed', None)
        _diff_sx = getattr(ds, '_diff_search_exp', None)
        if _diff_sx is None:
            _diff_sx = ds._diff_search_exp = set()
        # A manual-gen bump not yet seen by THIS frame (a badge click last
        # frame, an external fold_project_jump between frames) suspends the
        # active switch for one frame - the owner's watch clears the switch
        # to None in the same frame it sees the bump, so without this the
        # enforcement would undo the hand's change one frame earlier.
        _gen = getattr(ds, '_diff_manual_gen', 0)
        _fresh_manual = _gen != getattr(ds, '_diff_manual_seen_self', 0)
        ds._diff_manual_seen_self = _gen
        # A FLIP of the owner's switch (collapse-all ↔ expand-all) is the
        # only reflow that should keep the view where it was - a set change
        # from drift under a held switch must NOT re-anchor (it would fight
        # the typing scroll).
        _switch_flip = (expand_diff is not None
                        and getattr(ds, '_diff_switch_seen', '?') != expand_diff)
        ds._diff_switch_seen = expand_diff
        if _fresh_manual:
            if _diff_col is None:
                _diff_col = set()
        elif expand_diff is not None:
            # ACTIVE switch: every span kept folded (False - new gaps,
            # drift, external writes included; only the search reveal opens
            # spans, restored by its own machinery) or open (True), every
            # frame. Change-edge gated: writes only when different.
            _new_col = (set(_diff_rngs) - _diff_sx if expand_diff is False
                        else set())
            if _diff_col != _new_col:
                if _switch_flip:
                    # Hold one reference line across the reflow: the CARET's
                    # line when a caret is set and on-screen, otherwise the
                    # viewport's midpoint. Mapped to its buffer line through
                    # the OLD layout now (line_px/height from last frame's
                    # stamps); the post-build block projects it into the new
                    # layout and arms the _fold_scroll_anchor hold.
                    _lp = getattr(ds, '_diff_line_px', None)
                    _ofc = getattr(ds, '_fold_cache', None)
                    if _lp and _ofc is not None and _ofc[0] is input_value:
                        _sy = ds.scroll_offset[1]
                        _mid_dl = (_sy + (ds.height or 0) * 0.5) / _lp
                        _odisp, _od2b = _ofc[2][0], _ofc[2][3]
                        _cp = ds.text_cursor_pos
                        _cp_full_flip = None
                        if _cp is not None:
                            # The caret rides the flip: its FULL-buffer
                            # offset (through the OLD layout) is re-projected
                            # into the new layout post-build - without this
                            # the display offset is stale, gets clamped by
                            # the shorter collapsed text, and the vertical
                            # caret-follow yanks the view away from the
                            # anchor.
                            _cp_c = min(max(_cp, 0), len(_odisp))
                            _cdl = _odisp.count('\n', 0, _cp_c)
                            if _od2b:
                                _col = _cp_c - (_odisp.rfind('\n', 0, _cp_c)
                                                + 1)
                                _cbl = _od2b[min(_cdl, len(_od2b) - 1)]
                                _cp_full_flip = (
                                    _line_starts(input_value)[_cbl] + _col)
                            else:
                                _cp_full_flip = _cp_c
                            # Anchor line: the CARET's when it's on-screen;
                            # an off-screen caret keeps the midpoint
                            # (anchoring it would pull the view away).
                            if (_sy / _lp - 1.0 <= _cdl
                                    <= (_sy + (ds.height or 0)) / _lp + 1.0):
                                _mid_dl = _cdl + 0.5
                        if _od2b:
                            _mi = min(max(int(_mid_dl), 0), len(_od2b) - 1)
                            _mid_bl = _od2b[_mi] + (_mid_dl - int(_mid_dl))
                        else:
                            _mid_bl = _mid_dl
                        ds._diff_flip_anchor = (_mid_bl, _mid_dl, _sy,
                                                _cp_full_flip)
                ds.invalidate()
                request_render()
            _diff_col = _new_col

        elif _diff_col is None:
            # Neutral seed: a fresh draw_state adopts the diff pieces the
            # last session left collapsed (TextEditorState
            # .restore_diff_collapsed - the snapshot block at the tail
            # writes it; the tuples are buffer lines, so they land on
            # today's pieces by overlap across any drift). None captured
            # = expanded.
            _saved = (text_editor_state.restore_diff_collapsed
                      if text_editor_state is not None else None)
            if _saved:
                _saved = sorted(tuple(_r) for _r in _saved)
                _diff_col = _carry_diff_collapse(_saved, set(_saved),
                                                 _diff_rngs)
            else:
                _diff_col = set()
        elif getattr(ds, '_diff_prev_ranges', None) != _diff_rngs:
            # Neutral drift (an edit moved the span around): carry each
            # span's state onto the new span(s) overlapping it; a gap
            # overlapping nothing from last frame starts expanded.
            _diff_col = _carry_diff_collapse(
                getattr(ds, '_diff_prev_ranges', None) or [], _diff_col,
                _diff_rngs)
        ds._diff_fold_collapsed = _diff_col
        if getattr(ds, '_diff_prev_ranges', None) != _diff_rngs:
            ds._diff_prev_ranges = list(_diff_rngs)
    elif (getattr(ds, '_diff_fold_collapsed', None) is not None
          and not restore_active):
        # Compare off: drop the diff state so the next compare reseeds from
        # the switch. (Restore stand-in frames keep it - the real buffer is
        # about to land.)
        ds._diff_fold_collapsed = None
        ds._diff_prev_ranges = None
        ds._diff_search_exp = None
        ds._diff_gap_index = None
    # Stamped for the badge painters (gutter chevrons, fold labels): a range
    # in this set wears the diff tint (Toggles.TextEditor.diff_fold_tint)
    # and its badge toggles the DIFF collapse set.
    ds._diff_rng_set = _diff_rng_set
    # Expand-all (the owner's toggle True): the diff spans are all open and
    # stay open, so their chevrons / badges are noise - the gutter and badge
    # passes draw nothing for them (Lukas 09-01: "hide the diff spans
    # altogether"). The painter still carries the ranges, so a flip back to
    # collapse-all keeps its viewport anchor. Neutral mode keeps the
    # chevrons: an expanded span there is one the hand can re-collapse.
    _hide_expanded_diff = expand_diff is True and bool(_diff_rng_set)
    if ((fold_ranges or _diff_rngs) and not restore_active
            and not single_line and not is_search_box):
        if _fold_key_of is not None:
            # Collapse state is stored as line-independent KEYS (ds._fold_keys
            # / _fold_search_exp_keys); the range-tuple sets every editor
            # below mutates are a per-frame PROJECTION through this frame's
            # key->range map, harvested back to keys right before the build.
            # Tuples never need shifting on edits - a key re-projects onto
            # wherever the fold is now; a key whose fold vanished simply
            # projects to nothing until it reappears. Map stashed on the
            # draw_state for the external writers (fold_project_jump).
            # Inverse map memoized on the key map's identity (~2.5k entries
            # on a big file - rebuilding it every frame was 0.4 ms).
            _fro = getattr(ds, '_fold_range_of_memo', None)
            if _fro is not None and _fro[0] is _fold_key_of:
                _fold_range_of = _fro[1]
            else:
                _fold_range_of = {k: r for r, k in _fold_key_of.items()}
                ds._fold_range_of_memo = (_fold_key_of, _fold_range_of)
            ds._fold_key_of = _fold_key_of
            # Session restore: a brandless draw_state (keys and tuples both
            # unset) gets the persisted fold keys captured last session
            # (TextEditorState.restore_fold_keys - the snapshot block at the
            # tail writes them). The keys are line-independent, so they
            # project onto wherever those folds live in the loaded text; on
            # a restore stand-in they project to nothing (already-spliced
            # display text) and re-project correctly when the real buffer
            # lands. Stamping _fold_seed_ver keeps the default_collapsed
            # union below from re-collapsing folds the user had expanded -
            # the restored set (even an EMPTY one) IS the user's state.
            if (getattr(ds, '_fold_keys', None) is None
                    and getattr(ds, '_fold_collapsed', None) is None
                    and text_editor_state is not None
                    and text_editor_state.restore_fold_keys is not None):
                ds._fold_keys = set(text_editor_state.restore_fold_keys)
                ds._fold_seed_ver = _FOLD_SEED_VER
            if (getattr(ds, '_fold_keys', None) is None
                    and getattr(ds, '_fold_collapsed', None) is not None):
                # Legacy tuple-based state (pre-key session): adopt once.
                ds._fold_keys = {_fold_key_of[r] for r in ds._fold_collapsed
                                 if r in _fold_key_of}
            if getattr(ds, '_fold_keys', None) is not None:
                ds._fold_collapsed = {_fold_range_of[k] for k in ds._fold_keys
                                      if k in _fold_range_of}
            if getattr(ds, '_fold_search_exp_keys', None) is not None:
                ds._fold_search_exp = {
                    _fold_range_of[k] for k in ds._fold_search_exp_keys
                    if k in _fold_range_of}
        # Default-collapsed seeding is VERSIONED: the seed normally fires once
        # per FILE (draw_states are session-lived, surviving hotswap and file
        # close&open), so when the default_collapsed SOURCES change (bump
        # _FOLD_SEED_VER) already-seeded editors union the new defaults in
        # once instead of never seeing them. Union, not replace - the user's
        # own collapsed scopes stay collapsed. The stamp only advances on a
        # frame where the default scan actually ran (_fold_default_col computed)
        # so a diff-mode edit can't swallow the one-shot union.
        if getattr(ds, '_fold_collapsed', None) is None:
            ds._fold_collapsed = set(_fold_default_col or ())
            # Caller-declared default-collapsed folds: `default_collapsed_lines`
            # (0-based buffer HEADER lines) marks the folds STARTING on those
            # lines collapsed on this editor's first sight - the stack trace
            # view seeds each pane with its root scope folded. First-sight only,
            # like the built-in defaults: a badge toggle owns the state after.
            if default_collapsed_lines and fold_ranges:
                _dcl = set(default_collapsed_lines)
                ds._fold_collapsed |= {r for r in fold_ranges
                                       if r[0] in _dcl}
            ds._fold_seed_ver = _FOLD_SEED_VER
        elif (_fold_default_col is not None
              and getattr(ds, '_fold_seed_ver', 0) != _FOLD_SEED_VER):
            ds._fold_seed_ver = _FOLD_SEED_VER
            _new = set(_fold_default_col) - ds._fold_collapsed
            if _new:
                ds._fold_collapsed |= _new
                ds.invalidate()
        if getattr(ds, '_fold_search_exp', None) is None:
            # Folds auto-expanded to reveal the current search match, pending
            # re-collapse when the selection moves on (see the search-driven
            # return block below).
            ds._fold_search_exp = set()
        # Badge click against LAST frame's rects: this frame's layout depends
        # on the toggle, so it must resolve before the display text is built.
        _fold_toggled = None
        if left_mouse_down:
            for _fr, _rng in (getattr(ds, '_fold_badge_rects', None) or []):
                if (_fr[0] <= left_mouse_down.x < _fr[2]
                        and _fr[1] <= left_mouse_down.y < _fr[3]):
                    _fold_toggled = _rng
                    # A diff span toggles the DIFF collapse set; scope /
                    # caller ranges toggle the classic set (the durable one).
                    _is_diff_toggle = (_rng in _diff_rng_set
                                       and ds._diff_fold_collapsed is not None)
                    _tgt = (ds._diff_fold_collapsed if _is_diff_toggle
                            else ds._fold_collapsed)
                    if _rng in _tgt:
                        _tgt.discard(_rng)
                    else:
                        _tgt.add(_rng)
                    if _is_diff_toggle:
                        # Tell the owner a hand took over: draw_code_editor /
                        # merge_files watch this and clear their expand_diff
                        # switch to None (else the active switch would
                        # re-assert next frame and undo the click).
                        ds._diff_manual_gen = getattr(
                            ds, '_diff_manual_gen', 0) + 1
                    # A manual toggle overrides any pending search-restore.
                    ds._fold_search_exp.discard(_rng)
                    if getattr(ds, '_diff_search_exp', None):
                        ds._diff_search_exp.discard(_rng)
                    ds.invalidate()
                    request_render()


                    break
        # Keyboard folding — Ctrl+Minus/Equal collapse/expand the scope at
        # the caret (repeated presses walk outward: Ctrl+- folds the next
        # enclosing open scope, Ctrl+= opens the folds in the next
        # enclosing scope), Ctrl+Shift+Minus/Equal every ROOT scope. With a
        # SELECTION all four act only INSIDE it - on the folds whose HEADER
        # line the selection covers (the badges it sweeps over): Ctrl+-/=
        # fold/open the outermost of those (one level per press), the Shift
        # pair is collapse/expand-all restricted to them; the selection
        # survives the reflow so a follow-up press can act on it again. A
        # selection over no header line switches to the caret rules. The
        # events are hover-routed like any event param but gated on text
        # focus (same rationale as Ctrl+B - only the focused editor may act
        # on its caret). Resolved here, before the display build, for the
        # same reason as the badge click - this frame's layout depends on
        # the toggle.
        _fold_kb_all, _cp_full, _fstarts = False, None, None
        _fold_old_dline = None      # caret's display line BEFORE the toggle
        _fold_sel_full = None       # (start, end) of a kept selection, FULL buffer
        if ((ctrl_minus_down or ctrl_equal_down or ctrl_shift_minus_down
             or ctrl_shift_equal_down)
                and (Melty.text_focused_ds is ds
                     or (Melty.text_focused_ds is not None
                         and getattr(Melty.text_focused_ds, '_tile_id', None)
                         == ds._tile_id))):
            # SCOPE ranges only - the Ctrl+-/= family never touches the
            # diff spans (they answer to expand_diff and their own badges).
            _rngs = _fold_normalize_ranges(len(_line_starts(input_value)),
                                           fold_ranges or ())
            _fstarts = _line_starts(input_value)
            # The caret and selection live in DISPLAY coords - last frame's
            # layout maps them back to full-buffer coords (identity when
            # nothing is collapsed).
            _pc = getattr(ds, '_fold_cache', None)
            _pdisp = _pd2b = None
            if (_pc is not None and _pc[0] is input_value
                    and _pc[2][3] is not None):
                _pdisp, _pd2b = _pc[2][0], _pc[2][3]

            def _disp_to_full(_off):
                """Display offset → (full offset, full line, display line)."""
                if _pd2b is None:                # identity layout last frame
                    _off = min(_off, len(input_value))
                    _ln = input_value.count('\n', 0, _off)
                    return _off, _ln, _ln
                _off = min(_off, len(_pdisp))
                _col = _off - (_pdisp.rfind('\n', 0, _off) + 1)
                _dl = _pdisp.count('\n', 0, _off)
                _ln = _pd2b[min(_dl, len(_pd2b) - 1)]
                return _fstarts[_ln] + _col, _ln, _dl

            _cp_full, _cline, _fold_old_dline = _disp_to_full(
                ds.text_cursor_pos or 0)
            # Selection folds: the folds whose header line the selection
            # covers. Empty → the caret rules below, unchanged.
            _sel_folds = None
            if _has_selection(ds):
                _sel_fs, _sel_ls, _ = _disp_to_full(ds.text_selection_start)
                _sel_fe, _sel_le, _ = _disp_to_full(ds.text_selection_end)
                _sel_lo, _sel_hi = min(_sel_ls, _sel_le), max(_sel_ls, _sel_le)
                # An end sitting at column 0 (the drag ran onto the next
                # line) doesn't cover that line.
                if _sel_hi > _sel_lo and max(_sel_fs, _sel_fe) == _fstarts[_sel_hi]:
                    _sel_hi -= 1
                _sel_folds = [r for r in _rngs if _sel_lo <= r[0] <= _sel_hi]
                if _sel_folds:
                    _fold_sel_full = (_sel_fs, _sel_fe)
                else:
                    _sel_folds = None
            if _sel_folds is not None:
                # Outermost of the covered folds = those not nested in
                # another covered fold (sorted, strictly nested - same walk
                # as the root scan below).
                _skip = set(_fold_default_col or ())
                _outer, _open_end = [], -1
                for _r in _sel_folds:
                    if _r[0] > _open_end:
                        _outer.append(_r)
                        _open_end = _r[1]
                if ctrl_shift_minus_down:
                    # Collapse-all inside: the outermost plus every
                    # default-collapsed run covered (nested ones too - the
                    # same asymmetry as the whole-buffer variant).
                    _targets = (set(_outer)
                                | set(fold_child_scopes(_sel_folds, _outer))
                                | (_skip & set(_sel_folds)))
                    ds._fold_collapsed.update(_targets)
                elif ctrl_minus_down:
                    _targets = set(_outer)
                    ds._fold_collapsed.update(_targets)
                elif ctrl_shift_equal_down:
                    # Expand-all inside: everything covered except the
                    # default-collapsed runs.
                    _targets = {r for r in _sel_folds if r not in _skip}
                    ds._fold_collapsed.difference_update(_targets)
                else:
                    # One level out: the VISIBLE collapsed covered folds -
                    # those not hidden inside another collapsed covered
                    # fold. Default-collapsed runs included: a selection
                    # over one is the caret sitting ON it.
                    _targets, _open_end = set(), -1
                    for _r in _sel_folds:
                        if _r in ds._fold_collapsed and _r[0] > _open_end:
                            _targets.add(_r)
                            _open_end = _r[1]
                    ds._fold_collapsed.difference_update(_targets)
                ds._fold_search_exp.difference_update(_targets)
                _fold_kb_all = True
                ds.invalidate()
                request_render()
            elif ctrl_shift_minus_down or ctrl_shift_equal_down:
                # Root scopes: the first nesting level with at least two
                # scopes (see fold_root_scopes - a lone top-level class or
                # function is looked through so collapse-all folds its
                # members, not the whole span).
                # Default-collapsed ranges (top import block, comment runs)
                # are ASYMMETRIC: collapse-all folds them along with the
                # roots (nested comment runs included, so they're still
                # folded if their root is later expanded), but expand-all
                # leaves them untouched - they only expand via their own
                # badge or the caret-scoped shortcuts.
                # Both go ONE level deeper than the roots as well
                # (fold_child_scopes): collapse-all folds a function's own
                # if/for/with blocks and inner defs, so expanding that
                # function later shows them again - but not the blocks
                # inside THOSE; expand-all flips the same set back open.
                _skip = set(_fold_default_col or ())
                _roots = fold_root_scopes(_rngs, _skip)
                _children = fold_child_scopes(_rngs, _roots)
                if ctrl_shift_minus_down:
                    _targets = set(_roots) | set(_children) | _skip
                    ds._fold_collapsed.update(_targets)
                else:
                    _targets = [r for r in _roots + _children
                                if r not in _skip]
                    ds._fold_collapsed.difference_update(_targets)
                ds._fold_search_exp.difference_update(_targets)
                _fold_kb_all = True
                ds.invalidate()
                request_render()
            else:
                _at = [r for r in _rngs if r[0] <= _cline <= r[1]]
                _rng = None
                if ctrl_minus_down:
                    # Innermost not-yet-collapsed scope at the caret.
                    _rng = next((r for r in reversed(_at)
                                 if r not in ds._fold_collapsed), None)
                    if _rng is not None:
                        ds._fold_collapsed.add(_rng)
                else:
                    # Outermost collapsed scope at the caret - the VISIBLE
                    # one (an inner collapsed fold is hidden by its outer).
                    _rng = next((r for r in _at
                                 if r in ds._fold_collapsed), None)
                    if _rng is not None:
                        ds._fold_collapsed.discard(_rng)
                    else:
                        # Everything at the caret is already open: walk
                        # OUTWARD - expand the collapsed folds nested in the
                        # innermost scope at the caret, then (next press) in
                        # the surrounding one, ... up to the whole buffer -
                        # so Ctrl+= repeated progressively reveals more, the
                        # inverse of Ctrl+- collapsing progressively outward.
                        # Default-collapsed folds (docstrings, comment runs,
                        # imports) are skipped like expand-all does; they
                        # open only when the caret touches them.
                        _skip = set(_fold_default_col or ())
                        _scopes = list(reversed(_at)) + [
                            (0, input_value.count('\n'))]
                        for _sr in _scopes:
                            _inside = {r for r in ds._fold_collapsed
                                       if _sr[0] <= r[0] and r[1] <= _sr[1]
                                       and r != _sr and r not in _skip}
                            if _inside:
                                ds._fold_collapsed -= _inside
                                ds._fold_search_exp -= _inside
                                _fold_kb_all = True   # multi-fold caret path
                                ds.invalidate()
                                request_render()
                                break
                if _rng is not None:
                    _fold_toggled = _rng
                    ds._fold_search_exp.discard(_rng)
                    ds.invalidate()
                    request_render()
        # --- Search-driven temporary expansion ---------------------------
        # While the find UI cycles matches, the fold(s) hiding the CURRENT
        # match auto-expand and re-collapse when the selection moves on
        # (pending set: ds._fold_search_exp). The expansion COMMITS - the
        # pending set is dropped, folds stay open - when the search ends or
        # the editor itself takes text focus (the search UI takes it).
        # Edge-keyed on (term, current-match) so a manual re-collapse of
        # an auto-expanded fold isn't fought the very next frame. Matches
        # are found in the FULL buffer (input_value here, pre-splice); the
        # search section below shares _search_match_cache and projects them
        # into display coords on fold/scroll.
        _sq = str(search_text or (ds.search_text if ds.search_active else ""))
        if _sq and Melty.text_focused_ds is not ds:
            _scur = getattr(ds, '_search_active_local', None)
            _sk = (_sq, _scur)
            if getattr(ds, '_fold_search_seen', None) != _sk:
                ds._fold_search_seen = _sk
                _sli = None
                if _scur is not None:
                    _smc = getattr(ds, '_search_match_cache', None)
                    if (_smc is None or _smc[0] is not input_value
                            or _smc[1] != _sq):
                        _smc = (input_value, _sq,
                                _find_matches(input_value, _sq))
                        ds._search_match_cache = _smc
                    if _scur < len(_smc[2]):
                        _sli = input_value.count('\n', 0, _smc[2][_scur][0])
                _fold_sch = False
                _diff_col_live = getattr(ds, '_diff_fold_collapsed', None)
                _diff_exp_live = getattr(ds, '_diff_search_exp', None)
                if _sli is not None:
                    for _r in [r for r in ds._fold_collapsed
                               if r[0] < _sli <= r[1]]:
                        ds._fold_collapsed.discard(_r)
                        ds._fold_search_exp.add(_r)
                        _fold_sch = True
                    # A collapsed DIFF gap hiding the match expands the same
                    # way and restored through its own pending set.
                    if _diff_col_live is not None and _diff_exp_live is not None:
                        for _r in [r for r in _diff_col_live
                                   if r[0] < _sli <= r[1]]:
                            _diff_col_live.discard(_r)
                            _diff_exp_live.add(_r)
                            _fold_sch = True
                # Folds expanded for an EARLIER match restore once the
                # current match leaves them (moves on, or left this editor).
                for _r in [r for r in ds._fold_search_exp
                           if _sli is None or not (r[0] < _sli <= r[1])]:
                    ds._fold_search_exp.discard(_r)
                    ds._fold_collapsed.add(_r)
                    _fold_sch = True
                if _diff_col_live is not None and _diff_exp_live is not None:
                    for _r in [r for r in _diff_exp_live
                               if _sli is None or not (r[0] < _sli <= r[1])]:
                        _diff_exp_live.discard(_r)
                        _diff_col_live.add(_r)
                        _fold_sch = True
                if _fold_sch:
                    # MANY folds could move at once - reuse the collapse/
                    # expand-all caret projection: map the caret to full
                    # coords via LAST frame's layout here, project into the
                    # new layout after the build.
                    _fstarts = _line_starts(input_value)
                    _cp = min(ds.text_cursor_pos or 0, len(input_value))
                    _pc = getattr(ds, '_fold_cache', None)
                    if (_pc is not None and _pc[0] is input_value
                            and _pc[2][3] is not None):
                        _pdisp, _pd2b = _pc[2][0], _pc[2][3]
                        _cp = min(_cp, len(_pdisp))
                        _col = _cp - (_pdisp.rfind('\n', 0, _cp) + 1)
                        _dl = _pdisp.count('\n', 0, _cp)
                        _cp_full = (_fstarts[_pd2b[min(_dl, len(_pd2b) - 1)]]
                                    + _col)
                    else:
                        _cp_full = _cp
                    # Deliberately NO _fold_old_dline: that arms the
                    # collapse-all scroll-anchor hold, which re-asserts the
                    # PRE-expand scroll for several frames and stomps the
                    # search section's scroll-to-match below (Enter on a
                    # match inside a collapsed fold expanded it but the
                    # view stayed put). The search scroll owns the view.
                    _fold_kb_all = True
                    ds.invalidate()
                    request_render()
        elif (ds._fold_search_exp
              or getattr(ds, '_diff_search_exp', None)
              or getattr(ds, '_fold_search_seen', None) is not None):
            # Search over, or focus moved into this editor: commit - the
            # auto-expanded folds stay open. Seen-key resets too, so a
            # reopened search with the same term immediately re-runs the expand.
            ds._fold_search_exp.clear()
            if getattr(ds, '_diff_search_exp', None):
                ds._diff_search_exp.clear()
            ds._fold_search_seen = None
        if _fold_key_of is not None:
            # Harvest: every mutation above worked on the projected tuples;
            # convert back so the KEYS stay the single durable truth.
            ds._fold_keys = {_fold_key_of[r] for r in ds._fold_collapsed
                             if r in _fold_key_of}
            ds._fold_search_exp_keys = {
                _fold_key_of[r] for r in ds._fold_search_exp
                if r in _fold_key_of}
        # The range tuple is memoized on the fold_ranges list's identity:
        # the scope scan hands the same list every frame (~2.4k ranges on a
        # big file, so the tuple was 2.3k genexpr calls a frame).
        # The diff spans are few and re-built per frame, so their tuple is
        # rebuilt inline and simply concatenated on - _fold_build normalizes
        # the union (partial scope/diff overlaps merge deterministically).
        _frt = getattr(ds, '_fold_ranges_tuple', None)
        if _frt is None or _frt[0] is not fold_ranges:
            _frt = (fold_ranges, tuple(tuple(r) for r in (fold_ranges or ())))
            ds._fold_ranges_tuple = _frt
        _fold_union_col = ds._fold_collapsed
        if getattr(ds, '_diff_fold_collapsed', None):
            _fold_union_col = ds._fold_collapsed | ds._diff_fold_collapsed
        # Headless candidates memoized on the range tuple and toggle (the
        # scan hands the same list every frame; ~2.4k lookups otherwise).
        _fhc = getattr(ds, '_fold_headless_cache', None)
        _fh_tog = Toggles.TextEditor.hide_meta_comment_folds
        if _fhc is None or _fhc[0] is not _frt[1] or _fhc[1] != _fh_tog:
            _fhc = (_frt[1], _fh_tog,
                    _fold_headless_set(_fold_key_of, _frt[1]))
            ds._fold_headless_cache = _fhc
        fold_layout_key = (_frt[1] + tuple(_diff_rngs), frozenset(_fold_union_col),
               _fhc[2])
        _fc = getattr(ds, '_fold_cache', None)
        _pf_info['fold_text_hit'] = _fc is not None and _fc[0] is input_value
        if _fc is not None and _fc[0] is input_value and _fc[1] == fold_layout_key:
            _fold_built = _fc[2]
        else:
            _fold_built = (_fold_update_inline(_fc[0], input_value, _fc[2])
                           if _fc is not None and _fc[1] == fold_layout_key else None)
            if _fold_built is None:
                _fold_built = _fold_build(input_value, fold_layout_key[0], _fold_union_col,
                                          fold_layout_key[2])
            ds._fold_cache = (input_value, fold_layout_key, _fold_built)
        _disp, _fold_segments, _fold_folds, _fold_d2b = _fold_built
        # Caret keeps its glyph across a toggle: offsets up to the toggled
        # fold's anchor are identical in both layouts, so the NEW layout's
        # anchor/hidden-length adjust the old offset directly.
        if _fold_toggled is not None and ds.text_cursor_pos is not None:
            _fi = next((f for f in _fold_folds if f[0] == _fold_toggled), None)
            if _fi is not None:
                _a, _hl = _fi[5], _fi[6]
                _cp = ds.text_cursor_pos
                if _fi[2]:                       # now collapsed
                    ds.text_cursor_pos = (_cp - _hl if _cp > _a + _hl
                                          else min(_cp, _a))
                else:                            # now expanded
                    # The OLD layout's collapsed fold knows what was
                    # hidden where - a headless comment fold anchored on
                    # the line above and hid the header too, which the
                    # expanded version (header-anchored) can't tell.
                    _fo = None
                    if _fc is not None and _fc[0] is input_value:
                        _fo = next((f for f in _fc[2][2]
                                    if f[0] == _fold_toggled and f[2]), None)
                    if _fo is not None:
                        _a, _hl = _fo[5], _fo[6]
                    if _cp > _a:
                        ds.text_cursor_pos = _cp + _hl
            ds.text_selection_start = ds.text_selection_end = ds.text_cursor_pos
        elif _fold_kb_all and ds.text_cursor_pos is not None:
            # Collapse/expand-all can move MANY folds at once, so the single-
            # toggle anchor shift above doesn't apply - project the caret's
            # full-char offset into the NEW layout instead. A caret inside a
            # now-hidden line clamps to its covering fold header's end.
            if _fold_d2b is None:
                def _full_to_disp(_off):
                    return _off
            else:
                _dstarts = _line_starts(_disp)

                def _full_to_disp(_off):
                    _b = bisect.bisect_right(_fstarts, _off) - 1
                    _i = max(bisect.bisect_right(_fold_d2b, _b) - 1, 0)
                    if _fold_d2b[_i] == _b:
                        return _dstarts[_i] + (_off - _fstarts[_b])
                    return (_dstarts[_i + 1] - 1 if _i + 1 < len(_dstarts)
                            else len(_disp))
            ds.text_cursor_pos = _full_to_disp(_cp_full)
            if _fold_sel_full is not None:
                # Selection-toped fold: the selection rides along so the
                # next press can act on the same span again.
                ds.text_selection_start = _full_to_disp(_fold_sel_full[0])
                ds.text_selection_end = _full_to_disp(_fold_sel_full[1])
            else:
                ds.text_selection_start = ds.text_selection_end = ds.text_cursor_pos
            # Anchor the VIEW to the caret: collapse/expand-all reflows the
            # whole layout, so a kept scroll offset lands somewhere random.
            # Stash (new display line, old display line, pre-expand scroll):
            # the scroll write itself happens after line_px is known - and
            # RE-PEATS for a few frames, because ds.invalidate() above
            # resets the measured content height and the wrapper's scroll
            # limit (core_render max_scroll_y) wipes a one-shot write to 0
            # (collapse) or drags it back to the stale pre-expand max
            # (expand) before the new height lands.
            if _fold_old_dline is not None:
                _new_dl = (bisect.bisect_right(
                    _line_starts(_disp), ds.text_cursor_pos) - 1)
                ds._fold_scroll_anchor = (_new_dl, _fold_old_dline,
                                          ds.scroll_offset[1], 8)
        # expand_diff flip: hold the viewport's MIDPOINT line fixed. The
        # midpoint's buffer line was mapped through the OLD layout at the
        # flip (the diff state machine above); project it into NEW
        # layout's display line - a midline now hidden inside a collapsed
        # gap lands on the gap's header row - and arm the same multi-frame
        # scroll-anchor as the keyboard collapse/expand-all above.
        _dfa = getattr(ds, '_diff_flip_anchor', None)
        if _dfa is not None:
            ds._diff_flip_anchor = None
            _mid_bl, _old_mid_dl, _o_sy, _cp_full_flip = _dfa
            if _fold_d2b is None:
                _new_mid_dl = _mid_bl
            else:
                _bi = int(_mid_bl)
                _new_mid_dl = (max(bisect.bisect_right(_fold_d2b, _bi) - 1, 0)
                               + (_mid_bl - _bi))
            ds._fold_scroll_anchor = (_new_mid_dl, _old_mid_dl, _o_sy, 8)
            if _cp_full_flip is not None:
                # Re-project the caret into the new layout (a caret inside a
                # now-hidden body clamps to its covering fold header's end -
                # the kb collapse-all's rule), and sync prev so the vertical
                # caret-follow reads no move: the anchor holds the view.
                if _fold_d2b is None:
                    _new_cp = min(_cp_full_flip, len(input_value))
                else:
                    _ffs = _line_starts(input_value)
                    _dstarts_f = _line_starts(_disp)
                    _bl_f = bisect.bisect_right(_ffs, _cp_full_flip) - 1
                    _i_f = max(bisect.bisect_right(_fold_d2b, _bl_f) - 1, 0)
                    if _fold_d2b[_i_f] == _bl_f:
                        _new_cp = _dstarts_f[_i_f] + (_cp_full_flip
                                                      - _ffs[_bl_f])
                    else:
                        _new_cp = (_dstarts_f[_i_f + 1] - 1
                                   if _i_f + 1 < len(_dstarts_f)
                                   else len(_disp))
                ds.text_cursor_pos = _new_cp
                ds.text_selection_start = ds.text_selection_end = _new_cp
                ds.text_prev_cursor_pos = _new_cp
        if _fold_segments:
            input_value = _disp
            # Full → display coordinate bridges for the tree-derived overlays
            # (error markers, usage washes, def tints). Those all resolve in
            # FULL-buffer coordinates (fold_full); these helpers project the
            # RESULTS into display coords instead of disabling the features.
            _fold_fstarts = _line_starts(_fold_full)
            _fold_dstarts = _line_starts(input_value)

            def _fold_bl(b):
                # 0-based buffer line -> 0-based display line. disp_to_buf is
                # sorted, so the last visible buffer line <= b IS b itself
                # when visible - and a hidden line's covering fold HEADER
                # otherwise (errors on hidden lines light up the collapsed
                # header rather than vanishing).
                return max(bisect.bisect_right(_fold_d2b, b) - 1, 0)

            def _fold_off(o):
                # Full char offset -> disp offset; None while the offset's
                # line is hidden (a wash inside a collapsed body draws nowhere).
                b = bisect.bisect_right(_fold_fstarts, o) - 1
                i = bisect.bisect_right(_fold_d2b, b) - 1
                if i < 0 or _fold_d2b[i] != b:
                    return None
                return _fold_dstarts[i] + (o - _fold_fstarts[b])

            def _fold_remap_spans(spans, slot):
                # Char-offset span tuples (start, end, *rest) -> display
                # coords, hidden spans dropped. Memoized per slot on (spans
                # identity, fold layout identity) - both held in runtime so
                # id reuse doesn't alias - because downstream memos (like
                # usage-heat aggregation) key on the RESULT's id, so it must
                # be stable frame to frame.
                if not spans:
                    return spans
                _mms = getattr(ds, '_fold_span_memo', None)
                if _mms is None:
                    _mms = ds._fold_span_memo = {}
                _mm = _mms.get(slot)
                if (_mm is not None and _mm[0] is spans
                        and _mm[1] is _fold_built):
                    return _mm[2]
                out = []
                for sp in spans:
                    s = _fold_off(sp[0])
                    if s is None:
                        continue
                    e = _fold_off(sp[1])
                    if e is None:
                        e = s + (sp[1] - sp[0])   # span runs through the seam
                    out.append((s, e) + tuple(sp[2:]))
                out = tuple(out)
                _mms[slot] = (spans, _fold_built, out)
                return out

    # Plain-text mode (codec tells "not Python source"): no Darcula colors and
    # no inline token widgets - both are artifacts of the Python tokenizer.
    if not syntax_highlight or not show_widgets:
        token_views = {}

    elif token_views is None:
        token_views = DEFAULT_TOKEN_VIEWS   # an experiment fallback (see a     

    # Symbol-usage source: the parse arrives as `code_tree` in the
    # address_to_general_parse routes, as `code_dict` in the CODE_UI routes
    # (cst_module_to_dict - which is also where the run_jedi() pass attaches
    # __symbol_usages__). Links are file-absolute, so the buffer's file offset
    # comes from the parse's line_offset when set, else from the jump_to span.
    # The FILE route passes the syntax-ERROR MARKER dict ({'__error__', ...})
    # as code_tree while the real (possibly blank-line-repaired) parse rides    # in code_dict - the marker must not shadow the parse, or every tree-
    # derived wash (class blocks, symbol tints) vanishes for the whole
    # duration of the mid-edit syntax error.
    _usage_tree = code_tree if (code_tree is not None
                                and not (isinstance(code_tree, dict)
                                         and "__error__" in code_tree)) else code_dict

    _usage_off = getattr(_usage_tree, 'line_offset', 0) or 0
    if not _usage_off and jump_to is not None:
        _usage_off = getattr(jump_to, 'start', 0) or 0
    # Bridge disk→pending coordinates: index sites are computed over the
    # PENDING file text, while the span start above is a DISK coordinate. An
    # unsaved disk edit above this span that changed the line count shifts
    # every site - fold that shift into the offset (0 when nothing is pending).
    if not getattr(jump_to, 'pending_coords', False):   # search results: already pending
        _usage_off += _pending_line_delta(getattr(jump_to, 'path', None), _usage_off)

    def _view_usage_spans(vpath):
        """Usage spans in DISPLAY coordinates: always collected/resolved
        against the FULL buffer (fold-independent, so the resolve caches stay
        hot across collapse/expand), then projected through the fold remap
        when folds are collapsed."""
        # Late-bound resolve target: with no collapsed fold the display
        # text IS the full buffer, and `text` is the one the glyph pass
        # will draw THIS frame - edits earlier in this body run re-write
        # it - while _fold_full is the frame-start buffer. Resolving
        # against _fold_full put every wash below an inserted newline one
        # line off for exactly the edit frame (the snap-back flicker).
        spans = _usage_spans(ds, _fold_full if _fold_segments else text,
                             _usage_tree, _usage_off, vpath)
        if _fold_remap_spans is not None:
            spans = _fold_remap_spans(spans, 'usage')
            if _disp_sp is not None:
                # A frame with a fold collapsed: the resolve + remap above
                # are frame-start; shift across this run's splice so the
                # washes track the glyphs (see _display_splice_shift).
                _, _, spans, _ = _display_splice_shift(_disp_sp, text,
                                                       spans=spans)
        return spans
    # Per-editor state for the code-suggestions popup. Lives here (not gated on
    # focus) because the popup's menu window is latched and must be drawn EVERY
    # frame with closed_state toggled, even when the editor is unfocused.
    if getattr(ds, '_ac_state', None) is None:
        ds._ac_state = DropDownState()
    ac_state = ds._ac_state

    # Same deal for the usage-jump popup (multi-user symbol Ctrl+B).
    if getattr(ds, '_uj_model', None) is None:
        from meltygui.model.code_model import UsagePickerModel
        ds._uj_model = UsagePickerModel()
    uj_model = ds._uj_model
    # Similarly for the import quick-fix chooser (Alt+Enter on a missing-import line).
    if getattr(ds, '_qf_state', None) is None:
        ds._qf_state = DropDownState()
    qf_state = ds._qf_state
    # Imported in-function to avoid a module-load import cycle (toggles pulls in
    # decoration/window machinery). For the spell-check button + squiggles below.
    from meltygui.toggles import Toggles
    # Error markers to highlight in red: the routed code_tree's parse errors plus
    # any exception routed in via the mode route (e.g. draw_modes hands us the
    # chain_in failure so the offending source line lights up here). Computed up
    # front so the message can ride along into the file header bar.
    if Toggles.TextEditor.check_syntax_errors:
        _ct_errors = _code_tree_errors(code_tree) if code_tree is not None else None
        _err_markers = list(_ct_errors) if _ct_errors else []
        _err_markers += _exception_errors(error)
    else:
        _ct_errors, _err_markers = None, []
    # Fast-path syntax markers (Toggles.TextEditor.fast_syntax_check): the
    # staleness section at the end of the body re-runs a bare compile() per
    # edit and leaves (buffer, SyntaxError-or-None) on ds._fast_err_state.
    # When that record reflects THIS buffer (identity - every real edit is a
    # new string), a found error replaces the background markers outright:
    # they describe an older buffer, this one carries the current line, and
    # being current it survives the stale-hide below. A clean fast check
    # replaces nothing - parsing/lint markers keep the normal debounced flow
    # (compile() says nothing about lint findings).
    _fast_fresh_err = False

    if (Toggles.TextEditor.check_syntax_errors
            and Toggles.TextEditor.fast_syntax_check):
        _fs = getattr(ds, '_fast_err_state', None)
        # Identity against the FULL buffer (_fold_full is input_value when no
        # fold is collapsed): the fast check at the bottom always checks the
        # full text, never the fold-spliced display text.
        if _fs is not None and _fs[0] is _fold_full and _fs[1] is not None:
            # The CALLER's `error=` marker describes THIS very buffer (the
            # stack trace view's raising line) - it rides along; only the
            # background code_tree markers survive the stale and replaced here.
            _err_markers = _exception_errors(_fs[1]) + _exception_errors(error)
            # Errors past the first (see _compile_check_more), THIS buffer.
            _fx = getattr(ds, '_fast_err_extra', None)
            if _fx is not None and _fx[0] is _fold_full:
                for _xe in _fx[1]:
                    _err_markers += _exception_errors(_xe)
            _fast_fresh_err = True
    # Import quick-fix bookkeeping. `_qf_fixes` maps line → candidate import
    # statements, fed from the SEPARATE suggestions channel (`import_fixes`,
    # from ModesState.last_imports) - independent of the error markers, so a
    # transient error like a half-typed `json.` never hides the fix. Applied
    # fixes are remembered per payload IDENTITY (`_qf_applied`) and filtered
    # out immediately - a spanless fix doesn't change the buffer, so the
    # stale suggestion/marker would otherwise linger until the next scan; a
    # fresh scan (new identity) resets the memory and re-offers anything the
    # fix didn't actually cure.
    if getattr(ds, '_qf_applied_ct', None) != (id(code_tree), id(import_fixes)):
        ds._qf_applied_ct = (id(code_tree), id(import_fixes))
        ds._qf_applied = set()
    if getattr(ds, '_qf_applied', None):
        _err_markers = [(l, m) for l, m in _err_markers
                        if _missing_name(m) not in ds._qf_applied]
    # Fast-path import suggestions (Toggles.TextEditor.fast_syntax_check): the
    # per-edit section at the end of the body leaves (buffer, {line: [stmts]},
    # background-payload-at-scan-time) on ds._fast_imports_state. It feeds the
    # quick-fix rows when it reflects THIS buffer AND the background channel
    # hasn't swapped in a new payload since the scan (identity on both) - a
    # landed relint is irrelevant (it re-reads the file's pending binds);
    # the fast scan only bridges the debounce gap. The applied-fix memory
    # (_qf_applied) still keys on the background payload identity above and
    # filters the fast rows below, so a just-applied fix isn't re-offered per
    # keystroke while the file's import cache catches up
    _active_fixes = import_fixes
    if Toggles.TextEditor.fast_syntax_check:
        _fi = getattr(ds, '_fast_imports_state', None)
        if _fi is not None and _fi[0] is _fold_full and _fi[2] is import_fixes:
            _active_fixes = _fi[1]
    _qf_fixes = {}


    _qf_names = {}   # line → {names the fixes would bind} - drives the underlines
    if _active_fixes:
        from meltygui.code.chain_converters import _import_bound_name
        for _ln, _stmts in _active_fixes.items():
            try:
                _ln = int(_ln)
            except (TypeError, ValueError):
                continue
            _row = [_s for _s in _stmts
                    if not (ds._qf_applied and _import_bound_name(_s) in ds._qf_applied)]
            if _row:
                _qf_fixes[_ln] = _row
                _qf_names[_ln] = {n for n in (_import_bound_name(_s) for _s in _row) if n}
    # Fold remap: markers and quick-fix rows carry 1-based FULL-buffer lines;
    # project them onto the display. A marker on a hidden line clamps to its
    # containing fold's line (the shadowed header shows something went wrong
    # inside), and same-header quick-fix rows merge.
    # The caller's `error=` marker (the stack trace view's raising line)
    # describes THIS buffer - it survives the stale-hide and is fold-mapped
    # like every other marker.
    _caller_markers = _exception_errors(error)
    if _fold_bl is not None:
        _err_markers = [(_fold_bl(l - 1) + 1, m) for l, m in _err_markers]
        _caller_markers = [(_fold_bl(l - 1) + 1, m) for l, m in _caller_markers]
        _rqf, _rqn = {}, {}
        for _ln, _row in _qf_fixes.items():
            _dl = _fold_bl(_ln - 1) + 1
            _rqf.setdefault(_dl, []).extend(_row)
            _rqn.setdefault(_dl, set()).update(_qf_names.get(_ln, ()))
        _qf_fixes, _qf_names = _rqf, _rqn
    # Suppression (clearing _err_markers and _err_msg while keyboard editing) is
    # applied AFTER the keyboard recompute below, so it can read this frame's
    # popup state and the freshly-stamped edit time - see _ERR_SUPPRESS_SEC.
    
    # Jump-to-source button drawn inline at the top (before the monospace font
    # push, so it uses the normal UI font), above the text body. The first error
    # message (if any) is no longer shown inline here - it floats in a small
    # right-aligned box above the error line (see after the body is drawn).
    bar_height = 0.0
    _err_msg = None
    # show_jump_bar=False: jump_to serves ONLY as the buffer's edit scroll
    # offset (tree-derived washes) - no header bar. The search's code rows
    # use a bare offset shim that isn't a full Address, so the bar (which
    # reads .source/.file for its label) must not draw for them.
    show_jump_bar = False # Pin to false
    # _err_msg feeds the floating error box after the body (its gate) -
    # set whenever the buffer knows its file offset, jump bar or not: the
    # bar was pinned it, and gated to it the box never drew (09-04).
    if jump_to is not None:
        _err_msg = _err_markers[0][1] if _err_markers else None
    if jump_to is not None and show_jump_bar:
        if not show_file_header:
            # No floating bar: _err_msg still feeds the floating error box
            # after the body, but nothing is drawn inline here. Clear the stashed
            # OpenRectRect so a stale one can't swallow presses.
            draw_state._jump_btn_rect = None
        else:
            # Float the jump-to-file bar at the top of the visible viewport instead
            # of letting it scroll away with the code. If the body has scrolled up
            # under its clip rect, shift the bar down by that overflow so it stays
            # pinned to the clip top; at scroll 0 the content top equals the clip top
            # so float_dy is 0 and the bar sits in its natural place. Drawing it at the
            # shifted (on-screen) cursor also keeps draw_jump_to's own clip rect from
            # collapsing once the content top passes above the viewport.
            _bx, _by = imgui.get_cursor_screen_pos()
            float_dy = max(0.0, draw_state.abs_clip_rect[1] - _by)
            # The float only holds while there's still view BELOW the clip top - once
            # the view's bottom edge rises to meet the bar, the bar rides that edge
            # up and scrolls away like everything else. The bar's natural position
            # is the view top, so its maximum downward shift before its bottom
            # touches the view bottom is height - bar_height (last frame's measure).
            _bar_h = getattr(draw_state, "_float_bar_height", None) or 34.0
            if draw_state.height:
                float_dy = max(0.0, min(float_dy, draw_state.height - _bar_h))
            imgui.set_cursor_screen_pos((_bx, _by + float_dy))
            draw_jump_to(jump_to, width=draw_state.content_width, unique=unique,
                         draw_state=draw_state)
            bar_height = imgui.get_cursor_screen_pos()[1] - (_by + float_dy)
            draw_state._float_bar_height = bar_height
            # Resume body layout at the real (unscrolled) content top so the code
            # lines keep their normal positions; only the bar was floated. The text
            # clip below is raised by bar_height so glyphs never paint over the bar.
            imgui.set_cursor_screen_pos((_bx, _by + bar_height))
    # Manual search row: with manual_search=True the caller skips the floating
    # Find window and this body renders the shared search row itself - at the top
    # of the editor, on the line below the file-editor's nav buttons. Same
    # float-at-clip-top pattern as the jump bar above: the row is PINNED to
    # the visible viewport top (it must not scroll away with the code), while
    # layout continues at the content position so the text keeps its normal
    # coordinates; its height folds into bar_height so the text clip/culling
    # below start at the floating row.
    if manual_search and ds.search_active and not single_line and not is_search_box:
        from meltygui.view.search_view import draw_search
        _msx, _msy = imgui.get_cursor_screen_pos()
        _ms_h = 50.0
        # Pin at the view's absolute top (plus the jump bar's band when that
        # is showing): abs_top doesn't move with scroll, so the row stays put.
        imgui.set_cursor_screen_pos((_msx, draw_state.abs_top + bar_height))
        draw_search(input_value=ds, width=draw_state.content_width,
                    min_width=100, height=_ms_h, shadow=False,
                    name=f"Find{unique}", return_extras=True)
        bar_height += _ms_h
        imgui.set_cursor_screen_pos((_msx, _msy + _ms_h))
    _pf("head+jump_bar")
    _font_pushed = False
    if font is not None and Melty.font_mgr is not None:
        _font_handle = Melty.font_mgr.get(font)
        if _font_handle is not None:
            imgui.push_font(_font_handle)
            _font_pushed = True

    # Character advance. Every caller uses JetBrains Mono (monospace), so one
    # character advance lets us position and measure text by character count
    # instead of calling imgui.calc_text_size per glyph/slice each frame.
    char_w = imgui.calc_text_size("0").x
    changed = False
    original_input = input_value
    # Edit splice for THIS body run's display text (frame-start -> edited),
    # initialized after key handling ("text is final now"); None on non-edit
    # frames and while no fold is collapsed. Feeds _display_splice_shift so
    # fold-remapped washes track the glyphs on the edit frame. Pre-edit
    # _viewed_spans calls inside key handlers leave it as None on purpose:
    # they measure against the not-yet-edited buffer.
    _disp_sp = None

    # No line limit: the editor shows the WHOLE span. Off-screen lines are
    # already viewport-culled in every draw loop below (rect_min_y/rect_max_y)
    # and tokenization is cached by text value, so a long function costs an
    # O(n) position walk per frame, not per-line GPU remeasurements. The old
    # `max_lines = 1000` cap truncated the visible/editable text AND - because
    # its save-time rebuild re-stitched the hidden tail with no newline - ate one
    # boundary newline per save, progressively merging lines at line 1000 of any
    # longer function (the draw_text / abs_clip_rect_local corruptions).
    text = input_value
    io = imgui.get_io()
    line_px = imgui.get_text_line_height() * line_height
    # Collapse/expand toggle view anchoring (see the fold section): hold the
    # caret's line at its pre-toggle scroll position by asserting the absolute
    # target scroll - old scroll shifted by the caret's display-line delta -
    # each frame until the re-measured content height settles (the wrapper's
    # max_scroll_y clamp reads 0/stale height for a frame or two after the
    # toggle's invalidate and would wipe a single line).
    _fsa = getattr(ds, '_fold_scroll_anchor', None)
    _fold_scroll_shift = 0.0
    if _fsa is not None:
        _t_line, _o_line, _o_sy, _fsa_left = _fsa
        if getattr(ds, 'scroll_visible', False):
            # Whole pixels only: line_px is fractional (font height ×
            # line_height), so the anchor target has a sub-pixel part while
            # wheel scrolling keeps scroll_offset integral (core_render ceils
            # it). A fractional scroll shifts the editor's rasterization
            # phase, so lines snapped up/down a pixel after each collapse/
            # expand-all. Rounding also makes the round trip exact:
            # round(round(x) - x) == 0, so expand lands back on the original
            # scroll. The max clamp floors for the same reason.
            _target = float(round(max(0.0, _o_sy + (_t_line - _o_line) * line_px)))
            _mx = getattr(ds, '_max_scroll_y', None)
            if _mx is not None and not ds.invalid_content_height:
                _target = min(_target, float(math.floor(_mx)))   # live max only - stale would cap
            # The wrapper already positioned this frame's content cursor with
            # the PRE-write scroll, so a bare write only shows NEXT frame —
            # the toggle frame flashed the un-adjusted view. origin_y below
            # subtracts this shift so the very first frame paints anchored.
            _fold_scroll_shift = _target - ds.scroll_offset[1]
            ds.scroll_offset = (ds.scroll_offset[0], _target)
        _fsa_left -= 1
        if _fsa_left <= 0 or not getattr(ds, 'scroll_visible', False):
            ds._fold_scroll_anchor = None
        else:
            ds._fold_scroll_anchor = (_t_line, _o_line, _o_sy, _fsa_left)
            request_render()   # the hold needs the follow-up frames
    # Viewport tokenization: tokenize ONLY the clipped line range each frame
    # (see the `_line_open` / `_window_tokens` machinery above), so the per-frame
    # syntax cost is O(visible) instead of O(buffer). `_window()` returns
    # (start_line, start_offset, tokens, vcols) for the current text + visible
    # range, cached on the draw_state. It's lazy + keyed by (text, range), so a
    # click (pre-edit text) and the render (post-edit text) each get a window
    # for their state, but the render loop reuses the click's computation.
    def _window():
        nlines = len(_line_starts(text))
        # Visible line band from the clip rect (Y only) - the SAME live
        # abs_clip_rect + bar_height the draw-cull below uses, so the window
        # always covers exactly the lines that get drawn. A few lines of margin
        # keep caret/selection edges just past the clip correct and absorb a
        # frame of drag-scroll.
        _clip = draw_state.abs_clip_rect
        # origin_y, not `top`: on a fold toggle frame the buffer was
        # rewritten mid-body and origin_y carries the same-frame
        # compensation (_fold_scroll_shift) - banding off the stale `top`
        # tokenized the pre-anchor viewport (and, past the shrunken
        # buffer's end, triggered the plain path below). Every call site
        # runs after origin_y is updated; on normal frames the two are
        # identical.
        if line_px:
            v0 = int((_clip[1] + bar_height - origin_y) / line_px) - 3
            v1 = int((_clip[3] - origin_y) / line_px) + 3
        else:
            v0, v1 = 0, nlines - 1
        v0 = max(0, min(v0, nlines - 1))
        v1 = max(v0, min(v1, nlines - 1))
        # Horizontal band (long lines only): the visible column span from the
        # clip rect's X extent and the h-scroll, widened to a margin and
        # quantized to `long_line_band_cols` steps so the cache survives
        # small h-scrolls; the band only joins the key when some line in the
        # window is longer than `long_line_cols` (short lines: band=None,
        # and the key/tokens are exactly what they were before).
        band = None
        _long_cols = Toggles.TextEditor.long_line_cols
        if len(text) > _long_cols and char_w > 0:
            _offs_b = _line_offsets_cached(text)
            _v0b, _v1b = max(0, v0 - 12), v1
            _nl = len(_offs_b)
            if any(((_offs_b[ln + 1] - 1 if ln + 1 < _nl else len(text)) - _offs_b[ln])
                   > _long_cols for ln in range(_v0b, min(_v1b, _nl - 1) + 1)):
                _text_x0 = left + gutter_w + gutter_margin
                _step = max(64, Toggles.TextEditor.long_line_band_cols)
                _bc0 = int((_clip[0] - _text_x0 + ds.text_h_scroll) / char_w)
                _bc1 = int((_clip[2] - _text_x0 + ds.text_h_scroll) / char_w) + 1
                _bc0 = max(0, (_bc0 // _step - 1) * _step)
                _bc1 = (_bc1 // _step + 2) * _step
                band = (_bc0, _bc1, _long_cols)
        key = (text, v0, v1, syntax_highlight, syntax_language, id(token_views) if token_views else 0, band,
               getattr(ds, '_lv_trail_gen', 0))
        if getattr(ds, '_win_key', None) == key:
            return ds._win_data

        _pf_miss_t = time.perf_counter()
        if syntax_highlight:
            if syntax_language == "bash":
                from meltygui.editor.bash_syntax import window_tokens as bash_window_tokens
                wl, start_off, toks = bash_window_tokens(ds, text, _line_offsets_cached(text), v0, v1)
            else:
                if getattr(ds, '_lo_text', None) != text:
                    ds._lo_offs, ds._lo_open = _update_line_open(
                        getattr(ds, '_lo_text', None), getattr(ds, '_lo_offs', None),
                        getattr(ds, '_lo_open', None), text)
                    ds._lo_text = text
                wl, start_off, toks = _window_tokens(text, ds._lo_offs, ds._lo_open, v0, v1,
                                                     band=band)
            win_len = sum(len(t) for t, _ in toks)
            # Positional trailing gaps (variable- and value labels): the usage
            # overlay stamps {def_start: (frame, {(line0, col): cells})} on
            # the ds; fold the visible entries into window-relative indexes
            # so _build_vcols opens the gaps, then publish each gap's start
            # cell back (ds._lv_trail_cells) for the overlay to paint into.
            pos_trails = None
            _trail_cells_out = {}
            _tv_subs = getattr(ds, '_lv_trail_views', None)
            if _tv_subs:
                _offs_t = _line_offsets_cached(text)
                # The stamps are LAST frame's - coordinates of the frame-start
                # text. On an edit frame this window executes before the overlay
                # re-stamps, so without a remap every gap below an inserted /
                # deleted line sat one line off for that frame (the math
                # found no gap, the code jittered) - the same one-frame lag
                # _display_splice_shift fixes for the washes. Shift each stamp
                # across the frame's edit splice (frame-start → edited text);
                # stamps inside the edited region drop for the frame.
                _tr_sp = _offs_prev = None
                if text is not original_input and isinstance(original_input, str):
                    _trm = getattr(ds, '_lv_trail_splice', None)
                    if (_trm is None or _trm[0] is not original_input
                            or _trm[1] is not text):
                        _trm = (original_input, text,
                                _display_edit_splice(original_input, text),
                                _line_offsets_cached(original_input))
                        ds._lv_trail_splice = _trm
                    _tr_sp, _offs_prev = _trm[2], _trm[3]
                pos_trails = {}
                _tc_src = []
                for _f, _sub in _tv_subs.values():
                    for (_tl, _tc), _cells in _sub.items():
                        if _tr_sp is not None:
                            if _tl >= len(_offs_prev):
                                continue
                            _i_old = _offs_prev[_tl] + _tc
                            if _i_old >= _tr_sp[1]:
                                _i_new = _i_old + _tr_sp[2]
                            elif _i_old < _tr_sp[0]:
                                _i_new = _i_old
                            else:
                                continue        # inside the edit: re-stamped next frame
                            _tl = bisect.bisect_right(_offs_t, _i_new) - 1
                            if _tl < 0:
                                continue
                            _tc = _i_new - _offs_t[_tl]
                        if v0 <= _tl <= v1 and _tl < len(_offs_t):
                            _wi = _offs_t[_tl] + _tc - start_off
                            pos_trails[_wi] = _cells
                            _tc_src.append(((_tl, _tc), _wi, _cells))
                pos_trails = pos_trails or None
            # The gaps must move the DRAWN glyphs too, not just the caret
            # math: split the tokens so every gap boundary begins a token
            # (the glyph pass shifts x at gap-starting tokens - see
            # _lv_gaps there) and publish the absolute-index map.
            _gap_map = {}
            if pos_trails:
                toks = _split_tokens_at(toks, sorted(pos_trails))
                _gap_map = {start_off + _wi: _c
                            for _wi, _c in pos_trails.items()}
            ds._lv_gap_map = _gap_map
            arr = _build_vcols(text[start_off:start_off + win_len], toks, token_views,
                               pos_trails=pos_trails) \
                if (token_views or pos_trails) else None
            if arr is not None and pos_trails:
                for _key_t, _wi, _cells in _tc_src:
                    if 0 <= _wi < len(arr):
                        _trail_cells_out[_key_t] = arr[_wi] - _cells
            ds._lv_trail_cells = _trail_cells_out
            vcols = _WinVCols(arr, start_off) if arr is not None else None
        else:
            # Plain mode: the visible lines as ONE 'default' token (the segment
            # loop below splits it at newlines). No strings → no line_open needed.
            offs = _line_offsets_cached(text)
            wl, start_off = v0, offs[v0]
            end_off = offs[v1 + 1] if v1 + 1 < len(offs) else len(text)
            if band is not None:
                # Same horizontal cut as the syntax path, one 'default' token
                # per visible band (no lexer state to track in plain mode).
                _bc0, _bc1, _long_cols = band
                toks = []
                for ln in range(v0, v1 + 1):
                    ls = offs[ln]
                    le = offs[ln + 1] - 1 if ln + 1 < len(offs) else len(text)
                    if le - ls > _long_cols:
                        a, b = min(le, ls + _bc0), min(le, ls + _bc1)
                        if a > ls:
                            toks.append((text[ls:a], 'clipped'))
                        if b > a:
                            toks.append((text[a:b], 'default'))
                        if le > b:
                            toks.append((text[b:le], 'clipped'))
                    elif le > ls:
                        toks.append((text[ls:le], 'default'))
                    if le < end_off:
                        toks.append(('\n', 'default'))
            else:
                win_text = text[start_off:end_off]
                toks = [(win_text, 'default')] if win_text else []
            vcols = None
            ds._lv_trail_cells = {}
            ds._lv_gap_map = {}
        ds._win_key = key
        ds._win_data = (wl, start_off, toks, vcols)
        _pf_tok[0] += time.perf_counter() - _pf_miss_t
        _pf_tok[1] += 1
        return ds._win_data

    def _get_vcols():
        return _window()[3]
        
        
    left = imgui.get_cursor_screen_pos()[0]
    top = imgui.get_cursor_screen_pos()[1]

    # --- Line-number gutter ---
    # Shown only when the routed address (jump_to) supplies a starting line, so
    # a function body span shows its true file line numbers. Plain buffers with
    # no address or single-line cells (search box, inline text editors) get no
    # gutter. gutter_w is folded into origin_x, so every downstream operation
    # (scroll, search, cursor, mouse hit-testing) shifts with it; the numbers
    # themselves are drawn in their own clip column at the end so
    # horizontally-scrolled code never slides underneath them.
    # Explicit per-line numbers (diff mode passes the real file line for each
    # +/- line — they're non-contiguous, so no sequential offset can express
    # them) take priority over the jump_to.start sequential numbering.
    # Folded buffers: display lines map to NON-contiguous buffer lines, so the
    # sequential jump_to.start numbering would lie below a collapsed fold -
    # hand the gutter the per-display-line numbers instead.
    if _fold_d2b is not None:
        if line_numbers is not None:
            line_numbers = _FoldLineNumbers(_fold_d2b, line_numbers)
        elif jump_to is not None and getattr(jump_to, 'start', None) is not None:
            line_numbers = _FoldLineNumbers(_fold_d2b, offset=jump_to.start)
    show_gutter = (not single_line and not is_search_box
                   and (line_numbers is not None
                        or (jump_to is not None
                            and getattr(jump_to, 'start', None) is not None)))
    if show_gutter and line_numbers is not None:
        line_offset = 0
        # Fixed strip, sized for 5-digit lines: explicit line_numbers rows
        # render side by side (usage-picker / global-search code previews,
        # diff lines), and a per-row digit count made every row's code start
        # at a different column — line 980 got a 3-char strip, 6719 a 4-char
        # strip. One fixed strip lines the code up row to row.
        gutter_digits = 5
        gutter_w = gutter_digits * char_w + 12.0
    elif show_gutter:
        line_offset = jump_to.start
        last_line_no = line_offset + len(_line_starts(text))
        gutter_digits = max(len(str(last_line_no)), 2)
        gutter_w = gutter_digits * char_w + 12.0
    else:
        line_offset = 0
        gutter_w = 0.0

    # Instant-restore gutter: the loading stand-in arrives with neither
    # line_numbers nor jump_to, so the gutter vanished for the loading beat
    # and the code column jumped by gutter_w on the swap-in. Reproduce last
    # frame's gutter exactly - same digit count (the width) and the same
    # per-row NUMBERS AND CHEVRONS (restore_gutter_rows: the band's gutter
    # as painted). The sequential restore_line_offset numbering is only the
    # fallback for a pre-rows snapshot - sequential numbers lie below every
    # collapsed fold and carry no fold arrows, so the gutter visibly
    # snapped (213 → 304) the frame the real buffer landed.
    _restore_hdr = None
    # Stand-in DIFF chrome: {display line: hidden count} for the band's
    # diff-gap headers (0 = expanded) and the preview band hidden display lines,
    # both replayed from the snapshot - the diff paint needs the real
    # buffer, so on stand-in frames these are what makes a collapsed
    # diff split look collapsed (bands, "N lines", tinted chevrons,
    # the fade) instead of continuous code until the text lands.
    _restore_diff = None
    _restore_preview = None
    if restore_active and text_editor_state is not None:
        _rb = max(0, int(text_editor_state.restore_first_line))
        _rdr = text_editor_state.restore_diff_rows
        if _rdr:
            _restore_diff = {_rb + _ro: int(_rn) for _ro, _rn in _rdr.items()}
        _rpr = text_editor_state.restore_preview_rows
        if _rpr:
            _restore_preview = {_rb + _ro for _ro in _rpr}
    ds._diff_restore_rows = _restore_diff      # tests / overlays
    if (restore_active and text_editor_state is not None
            and text_editor_state.restore_gutter_digits > 0):
        show_gutter = True
        line_offset = int(text_editor_state.restore_line_offset)
        gutter_digits = int(text_editor_state.restore_gutter_digits)
        gutter_w = gutter_digits * char_w + 12.0
        _rr = text_editor_state.restore_gutter_rows
        if _rr:
            # Band rows carry their captured number / chevron; the visible
            # padding rows around the band show nothing (the paint treats
            # rows past len(line_numbers) - and None entries - as numberless).
            _rb = max(0, int(text_editor_state.restore_first_line))
            line_numbers = [None] * _rb
            _restore_hdr = {}
            for _rn in _rr:
                if _rn is not None and _rn < 0:
                    # chevron row: range None (a stand-in fold can't toggle
                    # - the paint skips the badge rect), -2 = collapsed.
                    _restore_hdr[len(line_numbers)] = (None, _rn == -2)
                    line_numbers.append(None)
                else:
                    line_numbers.append(_rn)

    # Live-marker open/close column: when the live view has registered
    # markers on this editor (per-line registry stamped by
    # draw_live_view_marker), widen the gutter by one button cell so each
    # marker line gets a raw draw-list toggle next to its number. This
    # persists across frames (rebuilt by each overlay pass), so the width is
    # stable - it only appears at all for buffers that have live markers.
    _lv_btn_w = (15.0 if (show_gutter
                          and Toggles.TextEditor.enable_live_view
                          and getattr(ds, "_lv_gutter_markers", None))
                 else 0.0)
    gutter_w += _lv_btn_w

    # Breathing room between the number strip and the code: folded into the
    # text inset (origin_x → rect_min_x below) only - the strip itself keeps
    # gutter_w, so the numbers stay snug in their column and the margin
    # reads the editor background.
    gutter_margin = 5.0 if gutter_w > 0 else 0.0
    # gutter_indent: an extra inset of one indentation level (4 columns -
    # the editor's `indent = '    '`) between the gutter and column 0, so
    # root-level guides and glyphs don't sit flush against the numbers.
    # Folded into the same margin, so every consumer of the text inset
    # (origin_x, rect_min_x, h-scroll limits, caret hit-test) follows.
    if gutter_indent:
        gutter_margin += 4 * char_w

    text_visible_width = draw_state.content_width - gutter_w - gutter_margin
    # Snapshot the clip rect in the same scroll frame as `left`/`top`. Those
    # come from the imgui cursor the wrapper positioned at abs_top *before* this
    # func ran; the drag handlers just below then mutate scroll_offset (here and
    # in _scroll_into_view, which is an ancestor) mid-render. abs_clip_rect
    # is computed live from abs_top, so reading it after those mutations makes
    # the clip lead the content - which imgui already placed at the pre-mutation
    # scroll - by one frame's drag delta, showing as a clip that lags the text.
    # Capturing it here keeps content and clip in the same frame; the scroll
    # delta lands next frame, when the wrapper re-positions the content too.
    clip_rect_snapshot = draw_state.abs_clip_rect

    # Right-click drag pans both axes. Vertical uses the framework's
    # scroll_offset (the framework skips writing it while button 2 is down,
    # so our edits aren't clobbered mid-drag). Horizontal uses our own
    # text_h_scroll since the framework only manages vertical scroll.
    if horizontal_scroll_drag:
        ds.text_h_scroll -= horizontal_scroll_drag.dx
        ds.text_h_scroll -= horizontal_scroll_drag.dx
        sx, sy = ds.scroll_offset
        ds.scroll_offset = (sx, sy - horizontal_scroll_drag.dy)

    origin_x = left + gutter_w + gutter_margin - ds.text_h_scroll
    # _fold_scroll_shift: same-frame compensation for the fold-anchor scroll
    # write above (larger scroll = content up = smaller origin), so the
    # collapse/expand next frame paints at the anchored position instead of
    # flashing the stale one.
    origin_y = top - _fold_scroll_shift
    # Scroll value origin_y was captured against. Anything later in THIS body
    # run that rewrites ds.scroll_offset (a usage jump centering its target)
    # leaves origin_y stale by exactly the delta - consumers that run after
    # such a write (the vertical cursor-follow) must shift by
    # (_origin_sy - ds.scroll_offset[1]) to get live coords.
    _origin_sy = ds.scroll_offset[1]

    # Keystrokes come from the GLFW-callback queue (Melty.frame_key_events:
    # ordered (glfw_key, mods) for PRESS/REPEAT this frame), so nothing is
    # dropped on slow frames the way imgui.is_key_pressed (current frame only)
    # would. But GLFW doesn't emit REPEAT actions on any platform, so in the
    # focused editor we supplement the queue with imgui's synthesized auto-repeat
    # (io.key_repeat_delay/rate) for held keys - skipping any key GLFW already
    # reported this frame so we never double-input. `pressed(k)` is membership;
    # the per-char loop iterates in order.
    _frame_keys = list(Melty.frame_key_events)
    if Melty.text_focused_ds is ds:
        _glfw_this_frame = {k for k, _m in _frame_keys}
        _repeat_mods = ((glfw.MOD_SHIFT if io.key_shift else 0)
                        | (glfw.MOD_CONTROL if io.key_ctrl else 0)
                        | (glfw.MOD_ALT if getattr(io, 'key_alt', False) else 0))
        _any_down = False
        for _rk in _REPEATABLE_KEYS:
            if imgui.is_key_down(_rk):
                _any_down = True
            if _rk not in _glfw_this_frame and imgui.is_key_pressed(_rk, repeat=True):
                _frame_keys.append((_rk, _repeat_mods))
        # The loop otherwise sleeps on wait_events between GLFW events; keep it
        # rendering while a key is held so imgui's repeat cadence is sampled.
        if _any_down:
            request_render()

    _fired = {k for k, _m in _frame_keys}
    pressed = lambda k: k in _fired
    _pf("setup")
    # --- Mouse handling ---
    is_focused = Melty.text_focused_ds is ds
    # A rebuilt cache can hand us a fresh draw_state object for the same tile;
    # rebind focus by tile id so a cache hit doesn't silently drop it.
    if (not is_focused and Melty.text_focused_ds is not None
            and getattr(Melty.text_focused_ds, '_tile_id', None) == ds._tile_id):

        if Toggles.TextEditor.text_focus_stack_trace:
            print(f"[focus-grant] rebind -> {ds.name} ({ds._tile_id}) "
                  f"from ds {id(Melty.text_focused_ds)}")
        Melty.text_focused_ds = ds
        Melty._text_focus_grant_frame = Melty.frame_count
        is_focused = True
    if request_focus:
        if Toggles.TextEditor.text_focus_stack_trace and Melty.text_focused_ds is not ds:
            print(f"[focus-grant] request_focus -> {ds.name} ({ds._tile_id})")
        Melty.text_focused_ds = ds
        # Stamp the grant frame so the same-frame request_focus grace (see
        # Melty.clear_focus) protects this claim from the very click that
        # opened the search box / dropdown / menu owning it.
        Melty._text_focus_grant_frame = Melty.frame_count
        is_focused = True
        # Select-all on a fresh grant (the find box on Ctrl+F): the caller
        # requests this only on the one-shot open/re-open frame, so typing
        # replaces the prefilled term and a single delete clears it.
        if select_all_on_focus and text:
            ds.text_selection_start = 0
            ds.text_selection_end = len(text)
            ds.text_cursor_pos = len(text)

    def _goto_usage_ref(ref, token=None):
        """Route one picked UsageRef: a site that lands inside THIS buffer's
        rendered span just moves the caret — the editor's own cursor-follow
        scroll brings it into view on the next body run — instead of
        round-tripping through the external jump (open tab + jump_to_line),
        which re-summons the editor window and loses the local context.
        Anything outside the span (other file, or a line outside a span
        buffer's range) still goes through _open_usage_ref.

        `token` is the symbol's spelling: when given, the caret lands ON that
        token at the target (definition refs carry the def-STATEMENT line with
        col 0 — the caret used to sit on `def`; caller refs record the
        statement-start col). Verify-recovered by _site_span, so a drifted or
        unfindable token falls back to the old statement-start placement."""
        _line = getattr(ref, 'line', None)
        _rpath = getattr(ref, 'path', None)
        _vp = getattr(jump_to, 'path', None) if jump_to is not None else None
        if _line is not None and _rpath is not None and _vp is not None:
            try:
                import os
                _same = os.path.realpath(str(_rpath)) == os.path.realpath(str(_vp))
            except OSError:
                _same = False
            _li = _line - 1 - _usage_off   # file line to FULL-buffer line index
            if _same and 0 <= _li <= _fold_full.count('\n'):
                # Source line (the symbol the jump left from) - decides below
                # whether to center or keep the current scroll. Display
                # coords, not the projected target line.
                _src_li = text.count(
                    '\n', 0, max(0, min(ds.text_cursor_pos, len(text))))
                # Record on the undo timeline before moving the caret: local
                # jumps bypass open_to_line (the only other recorder), so
                # Ctrl+Shift+Left had nothing to step back to after a same-
                # file jump - broken targets appeared. The timeline stores
                # FILE lines, so the display source line maps back through the
                # fold layout first.
                from meltygui.state.core_undo import NavUndo
                _nav_win = _enclosing_editor_window(ds)
                _nav_inst = ((getattr(_nav_win, 'instance', 0) or 0)
                             if _nav_win is not None else 0)
                _nav_src_li = (_fold_d2b[min(_src_li, len(_fold_d2b) - 1)]
                               if _fold_d2b is not None else _src_li)
                NavUndo.record_location(
                    (str(_vp), _nav_src_li + 1 + _usage_off, _nav_inst),
                    (str(_vp), _line, _nav_inst))
                # Target resolution runs in FULL-text space (_fold_full is
                # `text` rendered without folds) - the recorded site, ±4 verify,
                # and def recovery all describe the full file; the projection
                # below maps the final pos/line into display space.
                _offs = _line_offsets(_fold_full)
                _ls = _offs[_li]
                _le = (_offs[_li + 1] - 1) if _li + 1 < len(_offs) else len(_fold_full)
                _pos = None
                _tsp = None
                if token:
                    # Caret ON the jumped-to token (leaf of a dotted path),
                    # not the statement start.
                    _tsp = _site_span(_fold_full, _line,
                                      getattr(ref, 'column', 0) or 0,
                                      token.rsplit('.', 1)[-1], _usage_off)
                    if _tsp is not None:
                        _pos = _tsp[0]
                    else:
                        # Wide-drift recovery: the recorded line is faler
                        # than ±4 lines - retarget to the buffer's unique
                        # class/def statement for the token.
                        _rdp = _recover_def_pos(_fold_full, token)
                        if _rdp is not None:
                            _pos = _rdp
                            _li = _fold_full.count('\n', 0, _pos)
                            _line = _li + 1 + _usage_off
                            _uj_log(f"goto LOCAL def-recover -> line {_line}")
                if _pos is None:
                    _pos = min(_ls + (getattr(ref, 'column', 0) or 0), _le)
                    if _pos == _ls:
                        # No column info - land on the code, not the indent.
                        while _pos < _le and _fold_full[_pos] in ' \t':
                            _pos += 1
                # Fold projection: expands any collapsed fold hiding the
                # target, then maps pos/line into display space. The caret,
                # scroll targets and emphasis below all use the projected
                # values; the emphasis columns need the FULL pos (columns are
                # per-line, identical in both spaces).
                _fpos = _pos
                _pos, _li = fold_project_jump(ds, _fold_full, _pos, _li)
                ds.text_cursor_pos = _pos
                ds.text_selection_start = ds.text_selection_end = _pos
                # Center distant targets so they land with context; a nearby
                # one (< 30 lines) keeps the current scroll, with a minimal
                # edge nudge if it sits just past the viewport. When this
                # editor owns its scrollbar, center by writing the scroll in
                # CONTENT coords (same exact math as the cross-file picke
                # path) - the screen-space _scroll_into_view walk proved
                # frame-timing sensitive (Ctrl+B landed ~1500 lines off while
                # the picker path, ms later in the same body, worked fine).
                _far = abs(_li - _src_li) >= 30
                _sy0 = ds.scroll_offset[1]
                if _far and getattr(ds, 'scroll_visible', False):
                    _target = (_li * line_px
                               - max(0.0, (ds.height or 0) - line_px) * 0.5)
                    _mx = getattr(ds, '_max_scroll_y', None)
                    if _mx is not None:
                        _target = min(_target, _mx)
                    ds.scroll_offset = (ds.scroll_offset[0], max(0.0, _target))
                    from meltygui.notifications import notify
                    notify(f"scroll goto-local ds={ds.name} line={_line} "
                           f"li={_li} src_li={_src_li} "
                           f"sy={_sy0:.0f}->{ds.scroll_offset[1]:.0f} "
                           f"line_px={line_px} h={ds.height} max_y={_mx}",
                           tag="scroll")
                else:
                    # Same live-origin compensation as the caret-follow - a
                    # scroll write earlier in this path leaves origin_y stale.
                    _ty = (origin_y + (_origin_sy - ds.scroll_offset[1])
                           + _li * line_px)
                    _scroll_into_view(ds, _ty, _ty + line_px, center=_far)
                _uj_log(f"goto LOCAL scroll sy={_sy0:.0f}->{ds.scroll_offset[1]:.0f} "
                        f"far={_far} scroll_visible={getattr(ds, 'scroll_visible', None)} "
                        f"li={_li} src_li={_src_li}")
                Melty.text_focused_ds = ds
                Melty._text_focus_grant_frame = Melty.frame_count
                ds.text_cursor_blink_time = time.time()
                ds.invalidate()
                # Success flash on the target token - same yellow emphasis
                # (and same rect derivation) the cross-file jump gets from
                # draw_code_editor's consumption.
                _eli = _li                    # projected display line
                _cols = jump_emph_cols(_fold_full, _fpos, span=_tsp)

                def _local_jump_rect(ds=ds, li=_eli, cols=_cols):
                    lp = getattr(ds, '_diff_line_px', None) or 16
                    inset = getattr(ds, '_diff_top_inset', 0)
                    y0 = ds.abs_top + inset + li * lp - ds.scroll_offset[1]
                    if y0 < ds.abs_top - lp or y0 > ds.abs_top + (ds.height or 0):
                        return None
                    x0, x1 = ds.abs_left, ds.abs_left + (ds.width or 0)
                    cw = getattr(ds, '_diff_char_w', None)
                    ox = getattr(ds, '_diff_origin_x_off', None)
                    if cols is not None and cw and ox is not None:
                        x0 = max(x0, ds.abs_left + ox + cols[0] * cw - 3)
                        x1 = min(x1, ds.abs_left + ox + cols[1] * cw + 3)
                        if x1 <= x0:
                            return None
                    return (x0, y0 - 1, x1, y0 + lp + 1)

                Melty.emphasize(f"jump_line {ds.name}", _local_jump_rect)
                _uj_log(f"goto LOCAL line={_line} pos={_pos}")
                request_render()
                return
        # External jump: the picked site opens in another tab, so this buffer's
        # per-file draw_text would never run again - its latched picker window
        # would never see another closed=True call and stuck open (a classic
        # orphaned-popover leak). Force-close it before switching away.
        ds._uj_open = False
        _pop = Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None))
        if _pop is not None and not _pop.closed:
            _pop.closed = True
            if _pop._tile_id is not None:
                Melty.cache.invalidate_up(_pop._tile_id, force=True, bypass_clip=True)
        request_render()
        _uj_log(f"goto EXTERNAL {getattr(ref.path, 'name', ref.path)}:"
                f"{getattr(ref, 'line', None)} token={token!r}")
        _open_usage_ref(ref, token=token,
                        editor_window=_enclosing_editor_window(ds))

    def _open_usage_rows(_targets, _names, anchor, gutter_line=None):
        """Build the Code-tab-style picker rows for `_targets` (best first)
        and open the picker: file → scope chain → usage line, the keyboard
        cursor on the BEST match (the first target's row). `anchor` is the
        buffer index the picker hangs under; `gutter_line` docks it beside
        a gutter heat box instead."""
        from meltygui.editor.usage_picker import build_usage_rows
        from meltygui.editor.usage_picker import scroll_row_into_view
        _rows, _best = build_usage_rows(_targets, _names)
        if not _rows:
            return False
        uj_model.set_rows(_rows, _best,
                          max_rows=Toggles.TextEditor.SymbolUsages.picker_max_rows)
        ds._uj_names = _names
        ds._uj_anchor = anchor
        ds._uj_anchor_gutter = gutter_line
        ds._uj_open = True
        ds._uj_open_frame = Melty.frame_count
        # The picker window is LATCHED - its scroll_offset survives a
        # close, so a reopen would come up mid-list with the cursor row
        # scrolled offscreen. Bring the best row into view.
        scroll_row_into_view(
            Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None)), _best)
        request_render()
        return True

    def _pick_usage_row(_row):
        """Land a picker row: the file row opens the file, every other row
        jumps to its line with the caret on its token (a scope row's def
        name, a site's symbol)."""
        if _row.kind == "more":
            uj_model.expand()      # list them all, picker stays open
            from meltygui.editor.usage_picker import scroll_row_into_view
            scroll_row_into_view(
                Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None)),
                uj_model.index)
            request_render()
            return
        ds._uj_open = False
        if _row.kind == "file" or _row.ref is None:
            from meltygui.extensions import open_source as open_in_editor
            _uj_log(f"pick FILE {_row.path}")
            open_in_editor(str(_row.path),
                           editor_window=_enclosing_editor_window(ds))
            return
        _uj_log(f"pick {_row.kind} {getattr(getattr(_row.ref, 'path', None), 'name', None)}:"
                f"{getattr(_row.ref, 'line', None)} token={_row.token!r}")
        _goto_usage_ref(_row.ref, token=_row.token)

    def _present_usage_targets(_us, _su, _targets, force_picker):
        """Land a resolved usage jump: one counterpart opens straight in
        IntelliJ style; several (or `force_picker`) open the usage-jump
        picker under the symbol at buffer index `_us`. Always True."""
        if len(_targets) > 1 or force_picker:
            # ref -> symbol spelling, so a picker lands the caret ON the
            # symbol (see _goto_usage_ref).
            _names = {t: getattr(_su, 'name', None) for t in _targets}
            if _open_usage_rows(_targets, _names, _us):
                return True
        _goto_usage_ref(_targets[0],
                        token=getattr(_su, 'name', None))
        return True

    def _usage_recheck(pos):
        """The tree has no jump targets at `pos` — double-check against FRESH
        data before Ctrl+B flashes red, since the background usage graph can
        hold a symbol in a stale no-callers state. Recomputes usage data for
        just the caret's line (full cross-file caller walk), SYNCHRONOUSLY on
        the UI thread — deliberately, to get a feel for the real cost (timed
        into /tmp/uj_debug.log and stdout). Returns a display-coordinate span
        (start, end, SymbolUsage, at_def) for the symbol under the caret, or
        None when the fresh data agrees there's nothing to jump to."""
        _vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
        if _vpath is None:
            return None
        # Caret's display line -> full buffer line (folds splice the display
        # text) -> 1-based file line via the view's offset.
        _dl = text.count('\n', 0, pos)
        _fl = (_fold_d2b[_dl] if _fold_d2b is not None
               and _dl < len(_fold_d2b) else _dl)
        _file_line = _usage_off + _fl + 1
        _t0 = time.monotonic()
        from meltygui.code.libcst_conversion import usage_data_for_line
        _su_map = usage_data_for_line(str(_vpath), _file_line)
        _ms = (time.monotonic() - _t0) * 1000
        _uj_log(f"recheck {ds.name!r} line={_file_line} "
                f"took {_ms:.1f}ms symbols={len(_su_map)}")
        print(f"[usage-recheck] {getattr(_vpath, 'name', _vpath)}:{_file_line} "
              f"took {_ms:.1f}ms ({len(_su_map)} symbols)")
        # Safety check: the fresh result should AGREE with what the graph
        # already holds for this line - a difference is a stuck-stale-graph
        # bug caught in the act, so diff them and dump a forensic block
        # (_USAGE_MISMATCH_LOG) with everything needed to debug it later.
        # Guarded: the check must never fail the jump it rides on.
        try:
            _old_map = _tree_usages_on_line(_usage_tree, _file_line)
            _new_map = {getattr(_s, 'name', _k) or _k: _s
                        for _k, _s in _su_map.items()
                        if not (isinstance(_k, str) and "\x1f" in _k)
                        and any(s[0] == _file_line
                                for s in (getattr(_s, 'sites', None) or ()))}
            _diffs = _diff_usage_maps(_old_map, _new_map, _file_line)
            if _diffs:
                from meltygui.code.libcst_conversion import usage_graph_source
                from meltygui.editor.pending_save import PendingSave
                _log_usage_mismatch(
                    _vpath, _file_line, _diffs, _old_map, _new_map,
                    ctx=dict(frame=Melty.frame_count,
                             editor=repr(ds.name),
                             recheck_ms=f"{_ms:.1f}",
                             usage_off=_usage_off,
                             view_start=getattr(jump_to, 'start', None),
                             view_lines=len(_line_starts(_fold_full)),
                             graph_source=usage_graph_source(
                                 str(_vpath), _file_line, _file_line + 1),
                             pending_gen=PendingSave.pending_gen_for(_vpath),
                             tree_syms_on_line=len(_old_map),
                             fresh_syms_on_line=len(_new_map)))
        except Exception as _ce:
            _uj_log(f"recheck consistency check RAISED "
                    f"{type(_ce).__name__}: {_ce}")
        if not _su_map:
            return None
        # Same per-site resolution as _collect_usage_spans, narrowed to the
        # caret's line: full-buffer coordinates first, fold remap after.
        import os
        try:
            _vreal = os.path.realpath(str(_vpath))
        except OSError:
            _vreal = None
        _spans = []
        for _key, _su in _su_map.items():
            _name = getattr(_su, 'name', _key) or _key
            _d = getattr(_su, 'definition', None)
            _dp = getattr(_d, 'path', None) if _d is not None else None
            try:
                _def_here = (_dp is not None and _vreal is not None
                             and os.path.realpath(str(_dp)) == _vreal)
            except OSError:
                _def_here = False
            _is_local = isinstance(_key, str) and "\x1f" in _key
            for (_ln, _col) in getattr(_su, 'sites', None) or ():
                if _ln != _file_line:
                    continue
                _sp = _site_span(_fold_full, _ln, _col, _name, _usage_off)
                if _sp is None:
                    continue
                _at_def = (_def_here and _ln == getattr(_d, 'line', None)
                           and (_col == getattr(_d, 'column', None)
                                if _is_local else True))
                _spans.append((_sp[0], _sp[1], _su, _at_def))
        if _fold_remap_spans is not None:
            _spans = _fold_remap_spans(tuple(_spans), 'uj_recheck')
        # Narrowest span containing pos wins (same rule as _try_usage_jump).
        _best = None
        for _sp in _spans:
            if _sp[0] <= pos < _sp[1] and (_best is None
                                           or _sp[1] - _sp[0] < _best[1] - _best[0]):
                _best = _sp
        return _best

    def _roster_ctrl_b(pos):
        """Ctrl+B via the symbol roster at DISPLAY index `pos`: maps the caret
        into full-buffer coordinates, asks roster_tints.ctrl_b_lookup, and
        maps the returned symbol span back through the fold remap. Returns
        (start, end, sym, at_def, targets) or None."""
        _vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
        if _vpath is None:
            return None
        try:
            from meltygui.editor.roster_tints import ctrl_b_lookup
            _dl = text.count('\n', 0, pos)
            _col = pos - (text.rfind('\n', 0, pos) + 1)
            _fl = (_fold_d2b[_dl] if _fold_d2b is not None
                   and _dl < len(_fold_d2b) else _dl)
            _full = _fold_full if _fold_segments else text
            _fls = _line_starts(_full)
            if not (0 <= _fl < len(_fls)):
                return None
            _bpos = _fls[_fl] + _col
            _r = ctrl_b_lookup(_full, _bpos, _vpath, _usage_off)
        except Exception as _e:
            _uj_log(f"roster ctrl+b RAISED {type(_e).__name__}: {_e}")
            return None
        if _r is None:
            _uj_log(f"roster ctrl+b: no symbol at pos={pos}")
            return None
        _s, _e, _sym, _at_def, _targets = _r
        if _fold_remap_spans is not None:
            _sp = _fold_remap_spans(((_s, _e, _sym, _at_def),), 'uj_roster')
            if _sp:
                _s, _e = _sp[0][0], _sp[0][1]
        _uj_log(f"roster ctrl+b: {getattr(_sym, 'name', '?')} at_def={_at_def} "
                f"targets={len(_targets)}")
        return _s, _e, _sym, _at_def, _targets

    def _try_usage_jump(pos, force_picker=False):
        """Usage jump at buffer index `pos` (Ctrl+B): one counterpart opens
        straight in IntelliJ; several open the usage-jump picker under the
        symbol. `force_picker` opens the picker even for a SINGLE counterpart
        instead of jumping straight. When the tree yields no targets at `pos`
        (no span, or a span whose symbol shows no users), a synchronous
        single-line recheck (_usage_recheck) gets one more chance before
        False is returned and the caller flashes red. With
        Toggles.TextEditor.SymbolUsages.ctrl_b_always_recheck the recheck
        runs FIRST on every press and its fresh result wins; the background
        graph is only the fallback when it resolves nothing under the
        caret."""
        _vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
        _view_span = (_usage_off + 1, _usage_off + len(_line_starts(_fold_full)))
        _rechecked = False
        # Try roster first (Toggles.TextEditor.SymbolUsages.ctrl_b_roster):
        # textual resolution over pending/live spans - a usage jumps to its
        # definition, a definition lists its usages (trigram index +
        # resolve-back). A miss stays localized in this file's project;
        # the legacy background graph must not supply a foreign definition.
        if Toggles.TextEditor.SymbolUsages.ctrl_b_roster and _vpath is not None:
            _rr = _roster_ctrl_b(pos)
            if _rr is not None and _rr[4]:
                _rs, _re_, _rsym, _rat_def, _rtargets = _rr
                return _present_usage_targets(_rs, _rsym, _rtargets, force_picker)
            return False
        if Toggles.TextEditor.SymbolUsages.ctrl_b_always_recheck:
            _rechecked = True
            _fresh = _usage_recheck(pos)
            if _fresh is not None:
                _us, _ue, _su, _at_def = _fresh
                _targets = _usage_jump_targets(_su, at_def=_at_def,
                                               view_path=_vpath,
                                               view_span=_view_span)
                if _targets:
                    return _present_usage_targets(_us, _su, _targets,
                                                  force_picker)
        _n_spans = 0
        # NARROWEST span containing pos wins, not the first: a dotted member
        # span (`GlobalStyle.get_global_constant`, anchored at the start of
        # the dotted expression) fully covers the same base-name span, so a
        # caret inside `GlobalStyle` used to hit whichever came first in the
        # sort and jump TO the METHOD. Specific-over-general resolves this:
        # caret in the base chars → the base symbol; caret in the member chars
        # → only the dotted span contains it.
        _best = None
        for _sp in _view_usage_spans(_vpath):
            _n_spans += 1
            if _sp[0] <= pos < _sp[1] and (_best is None
                                           or _sp[1] - _sp[0] < _best[1] - _best[0]):
                _best = _sp
        if _best is not None:
            _us, _ue, _su, _at_def = _best
            _targets = _usage_jump_targets(_su, at_def=_at_def,
                                           view_path=_vpath,
                                           view_span=_view_span)
            if _targets:
                return _present_usage_targets(_us, _su, _targets, force_picker)
            _uj_log(f"try_jump zero targets pos={pos} "
                    f"sym={getattr(_su, 'name', None)!r} — rechecking")
        else:
            _uj_log(f"try_jump MISS pos={pos} spans_scanned={_n_spans} "
                    f"vpath={getattr(_vpath, 'name', _vpath)} — rechecking")
        if not _rechecked:
            _fresh = _usage_recheck(pos)
            if _fresh is not None:
                _us, _ue, _su, _at_def = _fresh
                _targets = _usage_jump_targets(_su, at_def=_at_def,
                                               view_path=_vpath,
                                               view_span=_view_span)
                if _targets:
                    return _present_usage_targets(_us, _su, _targets,
                                                  force_picker)
        return False

    def _line_usage_picker(line):
        """Gutter heat-box click: the usage-jump picker for ALL usage spans
        on `line`, as ONE flat list — each row is "symbol  scope" (symbol
        prefix only when several symbols share the line, capped at 2 dots),
        with the file:line tag on the right. ALWAYS the picker, even for a
        single target — a gutter click asks to SEE the users, not jump.
        True if it opened (a line with no jump targets returns False)."""
        _vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
        _lss = _line_starts(text)
        if not (0 <= line < len(_lss)):
            return False
        _l0 = _lss[line]
        _l1 = _lss[line + 1] if line + 1 < len(_lss) else len(text) + 1
        _vspan = (_usage_off + 1, _usage_off + len(_line_starts(_fold_full)))
        _groups, _seen, _anchor, _names = {}, set(), None, {}
        for _us, _ue, _su, _at_def in _view_usage_spans(_vpath):
            if _us < _l0:
                continue
            if _us >= _l1:
                break
            if _anchor is None:
                _anchor = _us
            # The symbol's label is its text in the buffer - repeated
            # occurrences on the line collapse into one group, and their
            # jump destinations dedupe per group.
            _sym = text[_us:_ue] or getattr(_su, 'name', '?')
            for _t in _usage_jump_targets(_su, at_def=_at_def,
                                          view_path=_vpath, view_span=_vspan):
                _k = (_sym, str(getattr(_t, 'path', None)),
                      getattr(_t, 'line', None))
                if _k not in _seen:
                    _seen.add(_k)
                    _groups.setdefault(_sym, []).append(_t)
                    _names[_t] = getattr(_su, 'name', None) or _sym
        if not _groups:
            return False
        # Every symbol's targets in ONE tree (file → scope → line): the
        # usage line already names the symbol, so no per-symbol prefix.
        _targets = [_t for _refs in _groups.values() for _t in _refs]
        if not _open_usage_rows(_targets, _names, _anchor, gutter_line=line):
            return False
        _uj_log(f"gutter OPEN line={line} rows={len(uj_model.rows)}")
        # The picker only shows while its editor owns text focus. The gutter
        # press never reaches the caret/focus path (the event is claimed), so
        # grant it here, same as _goto_usage_ref's local jump.
        Melty.text_focused_ds = ds
        Melty._text_focus_grant_frame = Melty.frame_count
        request_render()
        return True

    # A press inside a PLAIN owns_mouse token widget (number drag - rects
    # recorded by last body run's token loop) belongs to the widget, not the
    # text: skip caret/focus/selection for the whole gesture, matching what the
    # old render_func widget's click latch did. The flag is re-evaluated on
    # EVERY press (no stale latch), and cleared on release below. Caret
    # placement for a clean click still happens via the _try_click raw-mouse
    # path in the token loop.
    # A press on the jump bar's flat close button (rect stashed by
    # set_jump_to) belongs to the button: its click resolves via on_action;
    # the press must not place the caret in the text under the floating bar
    # (the old @render_func button's own draw_state used to claim it).
    if left_mouse_down:
        _jb = getattr(ds, "_jump_btn_rect", None)
        if (_jb is not None
                and _jb[0] <= left_mouse_down.x < _jb[2]
                and _jb[1] <= left_mouse_down.y < _jb[3]):
            left_mouse_down = None
    if left_mouse_down:
        ds._plain_tv_gesture = any(
            _r[0] <= left_mouse_down.x < _r[2] and _r[1] <= left_mouse_down.y < _r[3]
            for _r in getattr(ds, '_plain_tv_rects', ()))
        if ds._plain_tv_gesture:
            left_mouse_down = None

    # Live-marker gutter button press: claim it from the DELIVERED event, not
    # a raw imgui click read - the press that wakes a blit-cached tile is
    # already a frame old when the body runs, so is_mouse_clicked in the
    # gutter pass almost never saw it. The stash is consumed by the gutter
    # pass below (same body run); nulling the event here also keeps the button
    # click from placing the caret / granting text focus.
    if (left_mouse_down and _lv_btn_w
            and left <= left_mouse_down.x < left + _lv_btn_w):
        _lv_pressed_line = int((left_mouse_down.y - origin_y) // line_px)
        if _lv_pressed_line in (getattr(ds, "_lv_gutter_markers", None) or {}):
            ds._lv_btn_pressed_line = _lv_pressed_line
            left_mouse_down = None

    # Usage-heat gutter click: a press in the number column on a line with
    # usage spans opens the usage-jump picker for that line (see
    # _line_usage_picker - always the picker, single-ref included). A line
    # with no jump targets falls through to the normal click path, so plain
    # gutter clicks still place the caret at line start.
    if (left_mouse_down and gutter_w
            and Toggles.TextEditor.usage_heat_gutter
            and left + _lv_btn_w <= left_mouse_down.x < left + gutter_w
            and not any(_br[0] <= left_mouse_down.x < _br[2]
                        and _br[1] <= left_mouse_down.y < _br[3]
                        for _br, _ in (getattr(ds, '_fold_badge_rects', None)
                                       or []))):
        # (fold-arrow presses are excluded above - the toggle handler at the
        # top of the body owns those; without this a click on the arrow of a
        # heat-carrying header ALSO opens the usage picker)
        _uh_line = int((left_mouse_down.y - origin_y) // line_px)
        if _line_usage_picker(_uh_line):
            left_mouse_down = None

    if left_mouse_down:
        if Toggles.TextEditor.text_focus_stack_trace and Melty.text_focused_ds is not ds:
            print(f"[focus-grant] click -> {ds.name} ({ds._tile_id})")
        Melty.text_focused_ds = ds
        Melty._text_focus_grant_frame = Melty.frame_count
        is_focused = True
        # A fresh click anywhere in the editor dismisses the usage-jump picker
        # (Ctrl+B re-opens it when it applies). Clicks on the picker itself
        # never land here - it's in its own window, so this hover-routed
        # event doesn't fire.
        ds._uj_open = False
        # ...and abandons any pending snippet tabstops: a placed caret means
        # the user left the fill-in flow, and a later edit must indent fresh.
        ds._ac_tabstops = None
        # ...and disarms the param hint: a caret placed by CLICK never shows it,
        # even inside the function it's armed for. It re-arms on the next edit
        # within parens, or Ctrl+P (see the signature-help block).
        ds._ac_sig_request_paren = -1
        ds.text_cursor_blink_time = time.time()
        click_pos = _xy_to_char_index(text, io.mouse_pos.x, io.mouse_pos.y,
                                      origin_x, origin_y, line_px, vcols=_get_vcols())

        now = time.time()
        within_window = (now - ds.text_double_click_time < 0.3
                         and abs(click_pos - ds.text_last_click_pos) <= 1)
        ds.text_click_count = ds.text_click_count + 1 if within_window else 1
        ds.text_double_click_time = now
        ds.text_last_click_pos = click_pos

        if ds.text_click_count == 2:
            # Double-click word-selects. (Usage jump lives on Ctrl+B - see the
            # standalone handler under the click block.)
            ds.text_drag_mode = 'word'
            ds.text_selection_start = _select_unit_left(text, click_pos)
            ds.text_selection_end = _select_unit_right(text, click_pos)
            ds.text_cursor_pos = ds.text_selection_end
            ds.text_drag_anchor_lo = ds.text_selection_start
            ds.text_drag_anchor_hi = ds.text_selection_end       
        elif ds.text_click_count >= 3:
            ds.text_drag_mode = 'line'
            line_start = _get_line_start(text, click_pos)
            line_end = _get_line_end(text, click_pos)
            if line_end < len(text):
                line_end += 1  # include trailing newline so delete deletes the line
            ds.text_selection_start = line_start
            ds.text_selection_end = line_end
            ds.text_cursor_pos = ds.text_selection_end
            ds.text_drag_anchor_lo = line_start
            ds.text_drag_anchor_hi = line_end
        else:
            ds.text_drag_mode = 'char'
            ds.text_cursor_pos = click_pos
            if io.key_shift:
                ds.text_selection_end = click_pos
            else:
                ds.text_selection_start = click_pos
                ds.text_selection_end = click_pos
            ds.text_drag_anchor_lo = ds.text_selection_start
            ds.text_drag_anchor_hi = ds.text_selection_end

    # Extend the selection on cursor motion, and also every frame the button is
    # held (left_mouse_held) once a drag is underway - so holding the cursor
    # past the top/bottom edge keeps auto-scrolling and selecting more text,
    # not just while the mouse is moving.
    # Gestures that started inside a plain token widget never extend a text
    # selection - the drag drives the widget's value adjustment (see the press
    # handler above). Cleared on release in the token-view loop below.
    if left_mouse_drag and getattr(ds, '_plain_tv_gesture', False):
        left_mouse_drag = None
    if left_mouse_drag:
        mx = left_mouse_drag.x if left_mouse_drag else io.mouse_pos.x
        my = left_mouse_drag.y if left_mouse_drag else io.mouse_pos.y
        # Auto-scroll when the cursor hs/passes the view's top or bottom edge
        # so the selection can reach text outside the viewport. No-ops when the
        # cursor is comfortably inside.
        _scroll_into_view(ds, my, my)
        drag_pos = _xy_to_char_index(text, mx, my,
                                     origin_x, origin_y, line_px, vcols=_get_vcols())
        anchor_lo = ds.text_drag_anchor_lo
        anchor_hi = ds.text_drag_anchor_hi
        if ds.text_drag_mode in ('word', 'line') and (anchor_lo != anchor_hi):
            # Snap the moving end to the word/line boundary under the mouse,
            # then merge with the anchor span so the originally-selected
            # word/line stays fully highlighted while dragging either way.
            if ds.text_drag_mode == 'word':
                edge_lo = _select_unit_left(text, drag_pos)
                edge_hi = _select_unit_right(text, drag_pos)
            else:
                edge_lo = _get_line_start(text, drag_pos)
                edge_hi = _get_line_end(text, drag_pos)
                if edge_hi < len(text):
                    edge_hi += 1
            if drag_pos < anchor_lo:
                # Extending left: anchor's far (right) edge is the fixed end.
                ds.text_selection_start = anchor_hi
                ds.text_selection_end = edge_lo
                ds.text_cursor_pos = edge_lo
            else:
                # At/right of the anchor: anchor's left edge is fixed.
                ds.text_selection_start = anchor_lo
                ds.text_selection_end = max(anchor_hi, edge_hi)
                ds.text_cursor_pos = ds.text_selection_end
        else:
            ds.text_selection_end = drag_pos
            ds.text_cursor_pos = drag_pos
        ds.text_cursor_blink_time = time.time()
        # The latched drag keeps arriving even after the cursor leaves this
        # view, but this handler only runs on frames the (use_cache=True)
        # editor body actually re-renders - and once the cursor is off-view
        # the view's hover-driven per-frame invalidation stops, so the
        # selection/auto-scroll froze at the view edge. Keep the vie
        # re-rendering while the drag is held (similar pattern mirrors
        # draw_overlay_titlebar and the number-token drag sustain).
        ds.invalidate()
        request_render()

    # Ctrl+B - IntelliJ-style "go to declaration" at the CARET, no mouse
    # involved. (This flag used to be read only inside the click handler above,
    # so the shortcut silently required a simultaneous mouse press.) The event
    # is global-routed, so it reaches the editor under the pointer; gate on
    # focus so a stale caret in some other merely-hovered editor can't jump.
    if ctrl_b_down and not single_line and not is_search_box:
        _uj_log(f"ctrl_b {ds.name!r} focused={is_focused} "
                f"(focus_owner={getattr(Melty.text_focused_ds, 'name', None)!r}) "
                f"uj_open={getattr(ds, '_uj_open', False)} "
                f"caret={ds.text_cursor_pos} text_len={len(text)}")
    # Ctrl+Shift+B - open the CARET's line in the external editor (IntelliJ).
    # Same routing/focus gates as Ctrl+B; the display line maps through the
    # fold remap to the full buffer, plus the view's file offset gives the
    # 1-based source line. The launch runs off the render thread - the GUI
    # command blocks until the running instance answers.
    if (ctrl_shift_b_down and is_focused and not single_line
            and not is_search_box):
        _xp = getattr(jump_to, 'path', None) if jump_to is not None else None
        if _xp is not None:
            _xpos = min(ds.text_cursor_pos, max(len(text) - 1, 0))
            _xdl = text.count('\n', 0, _xpos)
            _xfl = (_fold_d2b[_xdl] if _fold_d2b is not None
                    and _xdl < len(_fold_d2b) else _xdl)
            _xline = _usage_off + _xfl + 1
            import threading
            from meltygui.utils.jump_to_code import open_in_intellij
            threading.Thread(target=open_in_intellij, args=(str(_xp), _xline),
                             daemon=True, name="open_in_intellij").start()
        else:
            from meltygui.notifications import notify
            notify("No file path for this buffer — can't open it externally.",
                   tint=(1.0, 0.65, 0.4, 1.0), tag="external_editor")

    if (ctrl_b_down and is_focused and not single_line and not is_search_box
            and not getattr(ds, '_uj_open', False)):
        _cb_pos = min(ds.text_cursor_pos, max(len(text) - 1, 0))
        if not _try_usage_jump(_cb_pos):
            # No jump target here - flash the word under the caret red so the
            # shortcut has answers instead of silently doing nothing.
            _w0 = _select_unit_left(text, _cb_pos)
            _w1 = _select_unit_right(text, _cb_pos)
            _vc = _get_vcols()
            _fx0, _fy0 = _char_pos_to_xy(text, _w0, origin_x, origin_y,
                                         line_px, vcols=_vc)
            _fx1, _ = _char_pos_to_xy(text, max(_w1, _w0 + 1), origin_x,
                                      origin_y, line_px, vcols=_vc)
            if _fx1 <= _fx0:   # word wrapped onto the next line - fall back
                _fx1 = _fx0 + imgui.calc_text_size(text[_w0:_w1] or " ").x
            Melty.emphasize(f"jump_fail {ds.name}",
                            (_fx0 - 3, _fy0 - 1, _fx1 + 3, _fy0 + line_px + 1),
                            tint=(0.9, 0.28, 0.22))
            request_render()

    _pf("mouse")
    # --- Keyboard handling ---
    if is_focused:
        shift = io.key_shift
        ctrl = io.key_ctrl

        # The caret can outlive the buffer it was placed in: a jump consume
        # (Ctrl+B) or a persisted draw_state stamps text_cursor_pos against
        # one text, and the content is then cut shorter underneath it
        # (buffer reload / merge adopt / external change). Every handler below
        # indexes text[cursor], so clamp ONCE here instead of per-site.
        if (ds.text_cursor_pos or 0) > len(text):
            ds.text_cursor_pos = len(text)
        if (getattr(ds, 'text_selection_start', 0) or 0) > len(text):
            ds.text_selection_start = len(text)
        if (getattr(ds, 'text_selection_end', 0) or 0) > len(text):
            ds.text_selection_end = len(text)

        # --- Code-suggestion popup: navigation & accept ---
        # Real editors don't suggest in the find box or inline single-line
        # value fields, so gate that out. (ac_state was set up at the top.)
        # Exception: a single-line box that has its own `completion_source`
        # (the context-aware Eval REPL) can autocomplete -- it drives candidates
        # off the live scope cache instead of the parsed code_tree.
        # `autocomplete=False` opts a field out entirely (text-code fields -
        # e.g. the params panel's string boxes render prose, not code) - an
        # explicit completion_source always wins, same as the single_line
        # exception (the Eval REPL asks for candidates on purpose).
        ac_enabled = (not is_search_box
                      and (not single_line or completion_source is not None)
                      and (autocomplete or completion_source is not None))
        if not ac_enabled:
            ds._ac_open = False
        # These run BEFORE the normal Arrow/Enter/Tab handlers and eat their
        # keys (discard from `_fired`) when the popup is open, so the same press
        # controls the suggestion list instead of moving the caret / inserting a
        # newline. Driven off LAST frame's open state + candidate list, i.e. the
        # popup the user is actually looking at this keypress.
        if ac_enabled and getattr(ds, '_ac_open', False):
            _ac_cands = getattr(ds, '_ac_candidates', None) or []
            _ac_idx = (_completion_selection(_ac_cands, getattr(ds, '_ac_index', 0), ac_state)
                       if _ac_cands else 0)
            ds._ac_index = _ac_idx
            if pressed(glfw.KEY_ESCAPE):
                # Dismiss and remember this site so it doesn't re-open
                # while the caret stays put (cleared once the caret moves on).
                ds._ac_open = False
                ds._ac_suppress_anchor = getattr(ds, '_ac_anchor', -1)
                ds._ac_request_anchor = -1
                _fired.discard(glfw.KEY_ESCAPE)




            elif (pressed(glfw.KEY_UP) or pressed(glfw.KEY_DOWN)) and _ac_cands:
                step = 1 if pressed(glfw.KEY_DOWN) else -1
                _ac_idx = (_ac_idx + step) % len(_ac_cands)
                ds._ac_index = _ac_idx
                ac_state._kbd_mode = True
                ac_state.cursor_path = (_ac_cands[_ac_idx],)
                # Keep the selection cursor visible: nudge the popup window to
                # scroll the minimal amount (no-op while the row is in view).
                from meltygui.core.dropdown_core import _dd_scroll_cursor_into_view
                _dd_scroll_cursor_into_view(
                    Melty.cache.key_to_draw_state.get(getattr(ds, '_ac_menu_tile', None)),
                    _ac_idx)
                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)
                  or (pressed(glfw.KEY_TAB) and not shift)) and _ac_cands and not ctrl:
                # A visible popup owns Tab; otherwise Tab accepts the AI ghost
                # or indents. Never accept two competing suggestions at once.
                chosen = _ac_cands[min(_ac_idx, len(_ac_cands) - 1)]
                anchor = getattr(ds, '_ac_anchor', ds.text_cursor_pos)
                # Both keyboard acceptance keys replace the existing word.
                _replace_to = _completion_replace_end(
                    text, ds.text_cursor_pos, chosen, getattr(ds, '_ac_snips', None))

                _ins, _coff, _extra = _ac_pick_insert(ds, chosen,
                                                      following=text[_replace_to:_replace_to + 64],
                                                      preceding=text[max(0, anchor - 64):anchor],
                                                      replaced=text[anchor:_replace_to],
                                                      line_prefix=text[_get_line_start(text, anchor):anchor])
                text = text[:anchor] + _ins + text[_replace_to:]
                ds.text_cursor_pos = anchor + _coff
                # Remaining $N stops: END-relative so fill-in typing at an
                # earlier stop never shifts them (see the Tab-stop handler).
                ds._ac_tabstops = [len(text) - (anchor + s) for s in _extra] or None
                text = _ac_apply_auto_import(ds, chosen, jump_to, text)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                ds.text_cursor_blink_time = time.time()
                ds._ac_open = False
                ds._ac_request_anchor = -1
                # Disarm the snippet site: the trigger char (if kept by the
                # overtype deducer, e.g. '(') still sits at its recorded spot
                # and the inserted text can match the trigger as a substring,
                # so the stale site would keep the popup alive. A FRESH trigger
                # ending at the new caret (a 0 landing right after '(') still
                # re-arms next frame - deliberate chaining.
                ds._ac_snip_site = None
                changed = True
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)
                _fired.discard(glfw.KEY_TAB)

        # --- FIM ghost text: accept / dismiss (fim_state) --- reads LAST frame's
        # ghost (what the user is looking at). Runs after the suggestion popup's
        # handlers and before the indent / caret handlers, consuming its keys
        # the same way. Tab = the visible chunk (Ctrl+Tab = everything
        # buffered), Ctrl+Right = one word, Esc = drop the buffer (not
        # consumed so Esc keeps its other jobs). A visible popup owns acceptance;
        # hidden ghost text must never splice into a later navigation action.
        _fim_ghost_prev = getattr(ds, '_fim_ghost', None)
        if (fim_state is not None and _fim_ghost_prev is not None and _fim_ghost_prev.text
                and not getattr(ds, '_ac_open', False)
                and not is_search_box and not single_line):
            if any(key == glfw.KEY_ESCAPE for key, _ in _frame_keys):
                fim_state.dismiss()
                ds._fim_ghost = None
            else:
                _fim_mode = None
                if pressed(glfw.KEY_TAB) and not shift:
                    _fim_mode = "all" if ctrl else "chunk"
                elif pressed(glfw.KEY_RIGHT) and ctrl and not shift:
                    _fim_mode = "word"
                if _fim_mode is not None:
                    _ins = fim_state.accept(_fim_mode)
                    if _ins:
                        _pos = ds.text_cursor_pos
                        text = text[:_pos] + _ins + text[_pos:]
                        ds.text_cursor_pos = _pos + len(_ins)
                        ds.text_selection_start = ds.text_cursor_pos
                        ds.text_selection_end = ds.text_cursor_pos
                        ds.text_cursor_blink_time = time.time()
                        changed = True
                        _fired.discard(glfw.KEY_TAB)
                        _fired.discard(glfw.KEY_RIGHT)

        # --- Usage-jump picker: navigation & accept --- same key model as the
        # suggestion popup above: while open, Esc/arrows/Enter drive the picker
        # and are consumed before the caret handlers see them.
        if getattr(ds, '_uj_open', False):
            _uj_rows = uj_model.rows
            if pressed(glfw.KEY_ESCAPE):
                ds._uj_open = False
                _fired.discard(glfw.KEY_ESCAPE)
            elif (pressed(glfw.KEY_UP) or pressed(glfw.KEY_DOWN)) and _uj_rows:
                step = 1 if pressed(glfw.KEY_DOWN) else -1
                uj_model.index = (uj_model.index + step) % len(_uj_rows)
                uj_model.kbd_mode = True
                from meltygui.editor.usage_picker import scroll_row_into_view
                scroll_row_into_view(
                    Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None)),
                    uj_model.index)
                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)) and _uj_rows and not ctrl:
                _row = uj_model.current()
                if _row is not None:
                    _pick_usage_row(_row)
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)

        # --- Import quick-fix (Alt+Enter) --- same key model as the popups
        # above. While the chooser is open, Esc/arrows/Enter drive it (keys
        # consumed before the caret handlers). Otherwise Alt+Enter with the
        # caret on a missing-import site applies the single fix directly, or
        # opens the chooser when several imports could bind the name.
        _alt = getattr(io, 'key_alt', False)
        _qf_enter = pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)
        if getattr(ds, '_qf_open', False):
            _qf_opts = getattr(ds, '_qf_options', None) or []
            _qf_idx = getattr(ds, '_qf_index', 0)
            if pressed(glfw.KEY_ESCAPE):
                ds._qf_open = False
                _fired.discard(glfw.KEY_ESCAPE)
            elif (pressed(glfw.KEY_UP) or pressed(glfw.KEY_DOWN)) and _qf_opts:
                step = 1 if pressed(glfw.KEY_DOWN) else -1
                _qf_idx = (_qf_idx + step) % len(_qf_opts)
                ds._qf_index = _qf_idx
                qf_state._kbd_mode = True
                qf_state.cursor_path = (_qf_opts[_qf_idx],)
                from meltygui.core.dropdown_core import _dd_scroll_cursor_into_view
                _dd_scroll_cursor_into_view(
                    Melty.cache.key_to_draw_state.get(getattr(ds, '_qf_menu_tile', None)),
                    _qf_idx)

                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif _qf_enter and _qf_opts and not ctrl:
                _stmt = _qf_opts[min(_qf_idx, len(_qf_opts) - 1)]
                _fx_changed, _fx_text = _apply_import_fix(_stmt, jump_to, text)
                from meltygui.code.chain_converters import _import_bound_name
                ds._qf_applied.add(_import_bound_name(_stmt))
                if _fx_changed:
                    ds.text_cursor_pos += len(_fx_text) - len(text)
                    text = _fx_text
                    changed = True
                ds._qf_open = False
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)
                request_render()
        elif _alt and _qf_enter and not ctrl and not is_search_box:
            _caret_ln = text.count('\n', 0, ds.text_cursor_pos) + 1
            _qf_opts = _qf_fixes.get(_caret_ln) or []
            if len(_qf_opts) == 1:
                _fx_changed, _fx_text = _apply_import_fix(_qf_opts[0], jump_to, text)
                from meltygui.code.chain_converters import _import_bound_name
                ds._qf_applied.add(_import_bound_name(_qf_opts[0]))
                if _fx_changed:
                    ds.text_cursor_pos += len(_fx_text) - len(text)
                    text = _fx_text
                    changed = True
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)
                request_render()
            elif len(_qf_opts) > 1:
                ds._qf_options = list(_qf_opts)
                ds._qf_index = 0
                ds._qf_anchor = ds.text_cursor_pos
                ds._qf_open = True
                qf_state._kbd_mode = True
                qf_state.cursor_path = (_qf_opts[0],)
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)
                request_render()
        # --- Typed characters --- drained in order, using each key event's own
        # modifiers so fast shift-typing across a slow frame stays shifted.
        typed_dot_this_frame = False
        typed_word_char_this_frame = False

        for _fk, _fmods in _frame_keys:
            if _fmods & glfw.MOD_CONTROL:
                continue
            _cm = _KEY_CHAR_MAP.get(_fk)
            if _cm is None:
                continue
            ds.text_cursor_blink_time = time.time()
            ch = _cm[1] if (_fmods & glfw.MOD_SHIFT) else _cm[0]
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
            text = text[:ds.text_cursor_pos] + ch + text[ds.text_cursor_pos:]
            ds.text_cursor_pos += len(ch)
            ds.text_selection_start = ds.text_cursor_pos
            ds.text_selection_end = ds.text_cursor_pos
            # Both a typed dot (member access) and a plain identifier char are
            # popup triggers (IDE model) - the visibility block below decides
            # whether this site actually qualifies (comment/string/def-site gates).
            if ch == '.':
                typed_dot_this_frame = True
            elif ch.isalnum() or ch == '_':
                typed_word_char_this_frame = True
            changed = True




        # --- Snippet tabstops: Tab hops to the next $N of the last accepted
        # template. Stops are stored END-relative (len(text) - pos): fill-in
        # typing at an earlier stop shifts everything after the caret equally,
        # so a later stop's distance from the new END is invariant. Esc or
        # a click abandons the remaining stops (handlers elsewhere).
        if (pressed(glfw.KEY_ESCAPE) and getattr(ds, '_ac_tabstops', None)):
            ds._ac_tabstops = None      # not consumed - Esc has its other handlers
        if (pressed(glfw.KEY_TAB) and not ctrl and not shift
                and getattr(ds, '_ac_tabstops', None)):
            _endrel = ds._ac_tabstops.pop(0)
            if not ds._ac_tabstops:
                ds._ac_tabstops = None
            _pos = max(0, min(len(text), len(text) - _endrel))
            ds.text_cursor_pos = _pos
            ds.text_selection_start = _pos
            ds.text_selection_end = _pos
            ds.text_cursor_blink_time = time.time()
            _fired.discard(glfw.KEY_TAB)

        def _bracket_ctx(p):
            """(text, pos) for the bracket-cue queries below
            (_open_bracket_indent / _unclosed_opener): the FULL buffer with
            `p` mapped through the collapsed fold segments. On the display
            text a collapsed def whose signature spans lines ends its
            visible header in an unclosed '(' — the ')' lives in the hidden
            body — so every Enter/Tab below it read a phantom continuation
            cue and indented to the signature column. Falls back to the
            display text once this frame has already edited it (the segment
            anchors would be stale)."""
            if not _fold_segments or text is not original_input:
                return text, p
            return _fold_full, p + sum(len(_h) for _a, _h, *_ in _fold_segments
                                       if _a < p)

        # --- Tab / Shift+Tab ---
        # Never in a search box: indent is irrelevant there, and the global
        # search window uses Tab/Shift+Tab to switch result highlighted.
        if pressed(glfw.KEY_TAB) and not ctrl and not is_search_box:
            ds.text_cursor_blink_time = time.time()
            # Bracket-aware align (same ([{ cue as Enter): when adjusting a single
            # line's own indent (no selection, caret in the leading whitespace)
            # and the line is a bracket continuation, Tab pulls an under-indented
            # line UP to the cue and Shift+Tab pulls an over-indented line DOWN to
            # it - e.g. a stray `show_name=False,` snaps under the `@renderable(`.
            _ls = _get_line_start(text, ds.text_cursor_pos)
            _cur = _get_indent(text, _ls)
            _target = _open_bracket_indent(*_bracket_ctx(_ls))
            _align = (_target is not None and not _has_selection(ds)
                      and not text[_ls:ds.text_cursor_pos].strip()
                      and ((_cur < _target) if not shift else (_cur > _target)))
            if _align:
                text = text[:_ls] + ' ' * _target + text[_ls + _cur:]
                ds.text_cursor_pos = _ls + _target
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
            elif shift or _has_selection(ds):
                if _has_selection(ds):
                    lo, hi = _sel_range(ds)
                else:
                    lo = hi = ds.text_cursor_pos
                text, new_lo, new_hi = _indent_lines(text, lo, hi, dedent=shift)
                ds.text_selection_start = new_lo
                ds.text_selection_end = new_hi
                ds.text_cursor_pos = new_hi
            else:
                insert = '    '
                text = text[:ds.text_cursor_pos] + insert + text[ds.text_cursor_pos:]
                ds.text_cursor_pos += len(insert)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
            changed = True

        # --- Enter / Shift+Enter --- (skipped for single-line fields like the
        # search box, where Enter is reserved for find-next / Shift+Enter
        # find-prev).
        # Ctrl+Enter is reserved for recompile (general_go_to_address), so we
        # don't insert a newline when Ctrl is held.
        if (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)) and not single_line and not ctrl:
            ds.text_cursor_blink_time = time.time()
            if shift:
                # Shift+Enter: start a new line BELOW without splitting the
                # current one - the caret escapes trailing closers like `)]`
                # instead of dragging them along. Indent is computed at the
                # line END with the same bracket cue as plain Enter, so a
                # still-open ([{ on this line gets the scope indent and a
                # balanced line keeps its own indentation.
                eol = text.find('\n', ds.text_cursor_pos)
                pos = eol if eol != -1 else len(text)
                _bt, _bp = _bracket_ctx(pos)
                indent = _open_bracket_indent(_bt, _bp)
                if indent is None:
                    opener = _unclosed_opener(_bt, _get_line_start(_bt, _bp))
                    indent = _get_indent(_bt, opener) if opener is not None \
                        else _get_indent(_bt, _bp) + _block_open_extra(_bt, _bp)
                text = text[:pos] + '\n' + ' ' * indent + text[pos:]
                ds.text_cursor_pos = pos + 1 + indent
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True
            else:
                if _has_selection(ds):
                    text, ds.text_cursor_pos = _delete_selection(text, ds)
                pos = ds.text_cursor_pos
                # Bracket-aware auto-indent. Inside an unclosed (, [, or { align to
                # that bracket's scope (everything past the opener, or a fixed indent
                # when nothing follows it) so multi-line signatures / lists / dicts
                # line up instead of snapping to the line's own indent. If the caret
                # is instead on a continuation line whose bracket already CLOSED on
                # this line, dedent back to the statement's opening-line indent
                # (e.g. after `...)` in a multi-line decorator → back to col 0).
                # Otherwise keep the current line's indentation.
                _bt, _bp = _bracket_ctx(pos)
                indent = _open_bracket_indent(_bt, _bp)
                if indent is None:
                    opener = _unclosed_opener(_bt, _get_line_start(_bt, _bp))
                    # Block opener (`def f():` / `class C:` / `if x:`) →
                    # one level deeper, see _block_open_extra.
                    indent = _get_indent(_bt, opener) if opener is not None \
                        else _get_indent(_bt, _bp) + _block_open_extra(_bt, _bp)
                # The remainder of the current line moves down to the new line. Strip
                # ITS leading spaces (only up to the line's end - never the next
                # line's indent) so they don't stack on top of the indent we insert.
                # Without this, whitespace sitting after the caret compounds on every
                # Enter: the new line ends up `indent + trailing` wide, the caret
                # sits mid-whitespace, and the next Enter measures that larger indent
                # - marching the caret ever rightward instead of keeping the line's
                # indentation.
                tail = pos
                line_end = text.find('\n', pos)
                stop = line_end if line_end != -1 else len(text)
                while tail < stop and text[tail] == ' ':
                    tail += 1
                # Splitting a comment mid-prose: the moved-down remainder would
                # land as bare whitespace and instantly re-lex as code. Continue the
                # comment instead - the new line re-opens with the '#' run (plus
                # its trailing space) at the '#'s own column, so a trailing
                # comment after code re-anchors under its '#' rather than the
                # statement's indent. Only when real content moves down
                # (tail < stop), Enter at a comment's end starts a fresh line.
                cont = ''
                _split = None
                if syntax_highlight and tail < stop:
                    _offs, _lopen = _ac_lex_state(ds, text)
                    # Splitting a single-quoted string literal: close it,
                    # reopen on the next line (implicit concatenation, parens
                    # added when not already bracketed) so the buffer stays
                    # valid Python - see _string_split.
                    if Toggles.TextEditor.enter_splits_strings:
                        _split = _string_split(text, pos, stop, _offs, _lopen)
                    if _split is None:
                        cc = _comment_continuation(text, pos, stop, _offs,
                                                   _lopen)
                        if cc is not None:
                            indent, cont = cc
                if _split is not None:
                    text, ds.text_cursor_pos = _split
                else:
                    text = text[:pos] + '\n' + ' ' * indent + cont + text[tail:]
                    ds.text_cursor_pos = pos + 1 + indent + len(cont)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True

        # --- Backspace ---
        if pressed(glfw.KEY_BACKSPACE):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True
            elif ds.text_cursor_pos > 0:
                if ctrl:
                    # Same granular behaviour as ctrl-click selection so a
                    # ctrl-backspace stops at a bracket/operator instead of
                    # eating a whole run of '{(' etc. Anchored on the char LEFT of
                    # the caret so it deletes the unit behind the caret (not the
                    # one under it, which left it a no-op before a bracket).
                    new_pos = _unit_left_of(text, ds.text_cursor_pos)
                    text = text[:new_pos] + text[ds.text_cursor_pos:]
                    ds.text_cursor_pos = new_pos
                else:
                    # Indent-aware backspace. When the caret is in a line's
                    # whitespace section (everything to its left on the line is
                    # spaces), snap back to the previous 4-col tab stop instead
                    # of removing a fixed 4 / a single char. A misaligned indent
                    # (e.g. 6 spaces) collapses to the nearest stop (4) rather
                    # than deleting 4 and leaving 2 stray spaces; an aligned
                    # full space deletes a whole tab; a lone stray space snaps
                    # to its own. Outside the indent it's a plain char delete.
                    line_start = _get_line_start(text, ds.text_cursor_pos)
                    col = ds.text_cursor_pos - line_start
                    in_indent = col > 0 and not text[line_start:ds.text_cursor_pos].strip(' ')
                    if in_indent:
                        new_pos = line_start + ((col - 1) // 4) * 4
                    else:
                        new_pos = ds.text_cursor_pos - 1
                    text = text[:new_pos] + text[ds.text_cursor_pos:]
                    ds.text_cursor_pos = new_pos
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True
        
        # --- Delete ---
        if pressed(glfw.KEY_DELETE):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True
            elif ds.text_cursor_pos < len(text):
                if ctrl:
                    new_pos = _select_unit_right(text, ds.text_cursor_pos)
                    text = text[:ds.text_cursor_pos] + text[new_pos:]
                else:
                    text = text[:ds.text_cursor_pos] + text[ds.text_cursor_pos + 1:]
                changed = True

        # --- Left ---
        # Ctrl+Shift+Left/Right belongs to navigation undo/redo (the root
        # NavUndo hotkeys, which fire even while text is focused) - the editor
        # ignores the chord, giving up extend-selection-by-word for it.
        if pressed(glfw.KEY_LEFT) and not (ctrl and shift):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = _word_boundary_left(text, ds.text_cursor_pos)
            elif _has_selection(ds) and not shift:
                ds.text_cursor_pos = min(ds.text_selection_start, ds.text_selection_end)
            elif ds.text_cursor_pos > 0:
                ds.text_cursor_pos -= 1
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos


        # --- Right ---
        if pressed(glfw.KEY_RIGHT) and not (ctrl and shift):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = _word_boundary_right(text, ds.text_cursor_pos)
            elif _has_selection(ds) and not shift:
                ds.text_cursor_pos = max(ds.text_selection_start, ds.text_selection_end)
            elif ds.text_cursor_pos < len(text):
                ds.text_cursor_pos += 1
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Up ---
        # Search box: Up/Down belong to the results list (global search moves
        # its highlight) and the caret snapping to 0/len was pure noise.
        if pressed(glfw.KEY_UP) and not is_search_box:
            _dbg = getattr(Melty, '_ac_debug', None)
            if _dbg:
                _dbg[-1]['cursor_moved'] = True
            ds.text_cursor_blink_time = time.time()
            line, col = _index_to_line_col(text, ds.text_cursor_pos)
            if line > 0:
                ds.text_cursor_pos = _line_col_to_index(text, line - 1, col)
            else:
                ds.text_cursor_pos = 0
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos


        # --- Down ---
        if pressed(glfw.KEY_DOWN) and not is_search_box:
            _dbg = getattr(Melty, '_ac_debug', None)
            if _dbg:
                _dbg[-1]['cursor_moved'] = True
            ds.text_cursor_blink_time = time.time()
            line, col = _index_to_line_col(text, ds.text_cursor_pos)
            total_lines = len(_line_starts(text)) - 1
            if line < total_lines:
                ds.text_cursor_pos = _line_col_to_index(text, line + 1, col)
            else:
                ds.text_cursor_pos = len(text)
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos



        # --- Home ---
        if pressed(glfw.KEY_HOME):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = 0
            else:
                ds.text_cursor_pos = _get_line_start(text, ds.text_cursor_pos)
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- End ---
        if pressed(glfw.KEY_END):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = len(text)
            else:
                ds.text_cursor_pos = _get_line_end(text, ds.text_cursor_pos)
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Ctrl+A ---
        if ctrl and pressed(glfw.KEY_A):
            ds.text_selection_start = 0
            ds.text_selection_end = len(text)
            ds.text_cursor_pos = len(text)

        # --- Ctrl+C ---
        if ctrl and pressed(glfw.KEY_C):
            if _has_selection(ds):
                lo, hi = _sel_range(ds)
                imgui.set_clipboard_text(text[lo:hi])

        # --- Ctrl+X ---
        if ctrl and pressed(glfw.KEY_X):
            if _has_selection(ds):
                lo, hi = _sel_range(ds)
                imgui.set_clipboard_text(text[lo:hi])
                text, ds.text_cursor_pos = _delete_selection(text, ds)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True

        # --- Ctrl+V ---
        if ctrl and pressed(glfw.KEY_V):
            ds.text_cursor_blink_time = time.time()
            clipboard = imgui.get_clipboard_text()
            if clipboard:

                if _has_selection(ds):
                    text, ds.text_cursor_pos = _delete_selection(text, ds)
                # Smart reindent on paste. A copied indented line block is dropped
                # at the caret's own indentation, preserving the block's RELATIVE
                # indentation, instead of pushing the block indent on top of the
                # line's (double-indenting). Only when the caret is on a line's
                # leading whitespace (the "paste onto a fresh indented line" case)
                # and the text is multi-line or carries leading spaces; plain
                # inline pastes (a token mid-statement) are left untouched.
                line_start = _get_line_start(text, ds.text_cursor_pos)
                prefix = text[line_start:ds.text_cursor_pos]
                reindent = (not prefix.strip()
                            and ('\n' in clipboard or clipboard[:1].isspace()))
                insert = _reindent_paste(clipboard, prefix) if reindent else clipboard
                text = text[:ds.text_cursor_pos] + insert + text[ds.text_cursor_pos:]
                ds.text_cursor_pos += len(insert)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True

        # --- Ctrl+/ (toggle line comment) ---
        if ctrl and pressed(glfw.KEY_SLASH):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                lo, hi = _sel_range(ds)
            else:
                lo = hi = ds.text_cursor_pos
            text, new_lo, new_hi = _toggle_comment(text, lo, hi)
            ds.text_selection_start = new_lo
            ds.text_selection_end = new_hi
            ds.text_cursor_pos = new_hi
            changed = True

        # --- Ctrl+I (insert Font Awesome icon glyph) ---
        # Inserts a placeholder glyph at the caret; the "icon" token_views renderer
        # immediately dresses it as the inline icon-picker dropdown, so this is
        # the keyboard entry point into icon picking.
        if ctrl and pressed(glfw.KEY_I):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
            text = text[:ds.text_cursor_pos] + GENERIC_ICON + text[ds.text_cursor_pos:]
            ds.text_cursor_pos += len(GENERIC_ICON)
            ds.text_selection_start = ds.text_cursor_pos
            ds.text_selection_end = ds.text_cursor_pos
            changed = True


        # Any buffer edit dismisses the usage-jump picker - its spans (and the
        # anchor it hangs off) are stale the moment the text shifts.
        if changed and getattr(ds, '_uj_open', False):
            ds._uj_open = False

        _pf("kbd:keys")
        # --- Code-suggestion popup: toggle visibility + rebuild candidates ---
        # Runs after every text-mutating key so the prefix reflects the final
        # buffer. Produces the list THIS frame's render draws and next frame's
        # nav reads. `_ac_anchor` is the span an accepted pick overwrites.
        if ac_enabled:
            prefix, anchor, dot_trigger = _completion_context(text, ds.text_cursor_pos)
            # Snippet shortcut site (Toggles.TextEditor.AC_SNIPPETS), opened by
            # typing a trigger like '#['. Computed before the suppress reset so
            # an Esc at the SNIPPET anchor isn't instantly forgotten (the
            # completion anchor is a different position).
            _snip = _snippet_context(ds, text, ds.text_cursor_pos, changed)
            sup = getattr(ds, '_ac_suppress_anchor', -1)
            if (sup != -1 and sup != anchor
                    and not (_snip is not None and sup == _snip[0])):
                ds._ac_suppress_anchor = sup = -1  # caret moved on; allow reopen
            if _snip is not None and sup == _snip[0]:
                _snip = None                       # Esc'd at the snippet site
            # IDE trigger model (IntelliJ-style): the popup opens as you TYPE -
            # a '.' (member access), an identifier char (scope completion), or
            # inside an import line (module completion) - or explicitly on
            # Ctrl+Space (Ctrl+P asks for the HO HINT instead - see the
            # signature-help block below). The trigger sets the completion site
            # (`_ac_request_anchor`); the popup stays up there — re-filtering as
            # the prefix grows/shrinks - until the caret leaves that site, Esc,
            # or an accepted pick. A bare caret move (e.g. clicking right after
            # an existing '.') never opens it. Typed triggers stay out inside
            # comments/strings (checked against the same incremental lexer
            # state the viewport tokenizer maintains) and right after a
            # name-DEFINING keyword (`def f`, `for x` - a name being referenced
            # has no members); an explicit ask bypasses both gates and also
            # overrides a prior Esc at this site, which typed triggers respect.
            # A `completion_source` box (the Eval REPL) is a one-line eval - no
            # comments, no parse tree - so every typed char re-triggers as-is.
            import_ctx = _import_line_context(text, anchor)
            typed_trigger = False
            if typed_dot_this_frame or typed_word_char_this_frame:
                if completion_source is not None:
                    typed_trigger = True
                elif syntax_highlight and (dot_trigger or import_ctx or prefix):
                    _offs, _lopen = _ac_lex_state(ds, text)
                    typed_trigger = (
                        not _pos_in_string_or_comment(
                            text, anchor - 1 if dot_trigger else anchor,
                            _offs, _lopen)
                        and (dot_trigger or import_ctx
                             or not _defining_keyword_before(text, anchor)))
            if ctrl and pressed(glfw.KEY_SPACE):
                text_editor_state._completion_explicit = True
                ds._ac_request_anchor = anchor
                ds._ac_suppress_anchor = sup = -1  # explicit ask overrides a prior Esc
            elif typed_trigger and sup != anchor:
                text_editor_state._completion_explicit = False
                ds._ac_request_anchor = anchor
            req = getattr(ds, '_ac_request_anchor', -1)
            if req != -1 and req != anchor:
                ds._ac_request_anchor = req = -1  # caret left the trigger site
            elif (req != -1 and changed and not prefix and not dot_trigger
                    and _snip is None and not (ctrl and pressed(glfw.KEY_SPACE))):
                # Deleted back to a blank prefix - the site is empty again, so
                # drop the popup instead of showing the unfiltered pool. Dot/
                # import sites keep their empty-prefix popups; typing (or
                # Ctrl+Space) re-triggers as usual.
                ds._ac_request_anchor = req = -1
            suppressed = sup != -1 and sup == anchor
            was_open = getattr(ds, '_ac_open', False)
            want = req != -1 and req == anchor and not suppressed
            # Popularity + tint ranking inputs for _filter_completions: usage-
            # site counts from the buffer's symbol graph (cached per parse
            # identity) and the tinted-name set (buffer map + the member-file
            # map, both already computed for the popup's row colors). Only
            # built while the popup is actually wanted.
            _ac_users = _usage_user_counts(ds, _usage_tree) if want else None
            _ac_tinted = None
            if want:
                _dtc = getattr(ds, '_def_tints', None)
                _ntc = _dtc[3] if _dtc is not None and len(_dtc) == 4 else None
                _mtc = getattr(ds, '_ac_member_tints', None)
                if _ntc or _mtc:
                    _ac_tinted = set(_ntc or ()) | set(_mtc or ())
            if _snip is not None and _snip[1]:
                # Word-like triggers ('t', 'in') are prefixes of real
                # identifiers. Once the typed filter matches NO snippet row
                # (label or insert text), the user is typing an identifier
                # ('token_views'), not asking for a snippet - disarm the site
                # so this same keystroke re-triggers scope completion instead
                # of the exclusive-but-empty snippet popup.
                _p = _snip[1].lower()
                if not any(_p in s.label.lower()
                           or _p in s.insert.replace("$0", "").lower()
                           for s in _snip[2]):
                    ds._ac_snip_site = None
                    _snip = None
            if _snip is None:
                ds._ac_snips = None    # accept must not treat identifiers as snippets
            ds._ac_import_stmts = None  # only the bare-identifier branch sets it
            if _snip is not None:
                # Snippet popup - only at its site: triggers like '#['
                # have no identifier completions, and the accept path replaces
                # the WHOLE snippet with the template. anchor/prefix are
                # remapped to the snippet site so the shared candidate-set
                # block below (and Esc suppression) work there.
                anchor, prefix = _snip[0], _snip[1]
                ds._ac_snips = {s.label: s for s in _snip[2]}
                # A snippet stays offered while the typed filter fits its
                # LABEL or its INSERT text — so typing the expansion itself
                # ('tint=(') keeps the row alive (accept replaces the whole
                # [anchor, caret) span, so nothing duplicates). Map order is
                # the ranking; these lists are hand-authored and tiny.
                _p = prefix.lower()
                cands = []
                for s in _snip[2]:
                    _ins_l = s.insert.replace("$0", "").lower()
                    # Fully typed = the filter is the TAIL of the insert:
                    # nothing left for accept to replace, so stop suggesting.
                    if ((_p in s.label.lower() or _p in _ins_l)
                            and not (_p and _ins_l.endswith(_p))):
                        cands.append((s.label, "snip"))
                ds._ac_member_tints = None
            elif want and completion_source is not None:
                # Eval REPL path - candidates come from the live scope cache
                # (FuncsMetadata), not the parsed code tree/jedi. Synchronous: the
                # source resolves member access via the recorded type's dict
                # (vs a live module's getattr) and bare names from the scope, both
                # with EXACT type tags. We still filter by the half-typed prefix.
                try:
                    raw = completion_source(text, anchor, prefix, dot_trigger) or []
                except Exception:
                    raw = []
                cands = _filter_completions(raw, prefix, users=_ac_users, tints=_ac_tinted,
                                            keep_exact=getattr(text_editor_state, '_completion_explicit', False))
                ds._ac_member_tints = None
            elif want and (dot_trigger or import_ctx):
                # Member access (`imgui.`, `foo.bar`) or an import line - the
                # live module namespace answers instantly when it can; otherwise
                # jedi resolves the receiver's REAL type / all importable
                # modules (async, off-thread, full-file context). Until that
                # lands, keep the list closed (scoped names aren't helpful) and
                # keep the body repainting so the future gets polled.
                members, pending = _ensure_member_completions(ds, text, anchor, jump_to)
                if members is not None:
                    cands = _filter_completions(members, prefix,
                                                users=_ac_users, tints=_ac_tinted,
                                                keep_exact=getattr(text_editor_state, '_completion_explicit', False))
                else:
                    cands = []   # jedi still resolving; its done-callback wakes us once
            elif want:
                # Bare identifier: scope-aware names from the parsed tree. The
                # POOL depends on the tree + the caret's line (its scope), NOT the
                # prefix, so cache it + only rebuild when those change. The body
                # re-runs every frame while the popup is open (keep-alive
                # invalidate); without this we'd re-walk the parse and build the
                # LineMap each frame just to filter by prefix.
                # The tree is _usage_tree, NOT raw code_tree: parsers can
                # deliver the parse as code_dict (code_tree=None) or shadow it
                # with the syntax-error marker - raw code_tree there dropped
                # every scope/position filter and the pool fell back to an
                # unfiltered whole-buffer list. _usage_tree already resolves
                # that. Its node spans are BUFFER-relative (only usage SITES
                # are file-absolute), so the buffer caret line is the right
                # coordinate to pass - no _usage_off here.
                _ac_line = _index_to_line_col(text, ds.text_cursor_pos)[0]
                _pool_key = (id(_usage_tree), _ac_line, id(text))
                if getattr(ds, '_ac_pool_key', None) != _pool_key:
                    # Extending the current identifier changes the prefix,
                    # not its scope. Build the pool until another part of the
                    # buffer changes; pin the buffer to make identity safe.
                    previous = getattr(text_editor_state, '_completion_buffer', None)
                    previous_key = getattr(ds, '_ac_pool_key', None)
                    reuse = False
                    if (previous is not None and previous_key is not None
                            and previous_key[:2] == _pool_key[:2]
                            and getattr(text_editor_state, '_completion_anchor', None) == anchor):
                        edit = _text_splice(previous, text)
                        reuse = (edit is None or
                                 (edit[0] >= anchor and edit[1] + edit[2] <= ds.text_cursor_pos
                                  and not edit[3]
                                  and all(c.isalnum() or c == '_'
                                          for c in previous[edit[0]:edit[1]] +
                                          text[edit[0]:edit[1] + edit[2]])))
                    if not reuse:
                        _pool_func = _ac_live_context(ds, text, jump_to)[1]
                        ds._ac_pool = _completion_pool(_usage_tree, text, _ac_line, _pool_func)
                    ds._ac_pool_key = _pool_key
                    text_editor_state._completion_buffer = text
                    text_editor_state._completion_anchor = anchor
                cands = _filter_completions(ds._ac_pool, prefix,
                                            users=_ac_users, tints=_ac_tinted,
                                            keep_exact=getattr(text_editor_state, '_completion_explicit', False))
                # Statement-only keywords are not useful after `=`, `return`,
                # or `if`. Scan only the current line, not the full buffer.
                line_prefix = text[_get_line_start(text, anchor):anchor].strip()
                if line_prefix:
                    expression_keywords = {"True", "False", "None", "not", "lambda", "await"}
                    cands = [(name, kind) for name, kind in cands
                             if kind != "kw" or name in expression_keywords]
                # Import shortcuts: global classes/modules the buffer doesn't
                # know yet - accepting one also inserts the import statement.
                cands = _ac_import_rows(ds, cands, prefix, jump_to,
                                        explicit=getattr(text_editor_state, '_completion_explicit', False))
                ds._ac_member_tints = None   # scope names - member map would mislabel
            else:
                cands = []
            if cands:
                # cands is [(name, kind)]. Names drive nav/scroll/highlight; the
                # kind becomes a dim per-row tag (func/class/var/...) via kind_tags.
                names = [n for n, _ in cands]
                if prefix != getattr(ds, '_ac_prefix', None) or not was_open:
                    ds._ac_index = 0  # list changed shape, restart at the top match
                    # Snap the (latched) popup back to the top so the restarted
                    # selection is visible - the popup keeps its scroll_offset
                    # across reshapes and reopens otherwise.
                    from meltygui.core.dropdown_core import _dd_scroll_cursor_into_view
                    _dd_scroll_cursor_into_view(
                        Melty.cache.key_to_draw_state.get(getattr(ds, '_ac_menu_tile', None)), 0)
                    # Assert keyboard-select mode so the top match is highlighted
                    # immediately (the dropdown only paints the cursor_path row
                    # when _kbd_mode is set; otherwise it waits for hover). A mouse
                    # press flips back to hover (handled in the popup render).
                    ac_state._kbd_mode = True
                    if not was_open:
                        request_render()
                ds._ac_index = min(getattr(ds, '_ac_index', 0), len(names) - 1)
                ds._ac_open = True
                ds._ac_anchor = anchor
                ds._ac_prefix = prefix
                ds._ac_candidates = names
                ds._ac_kinds = {n: _kind_tag(k) for n, k in cands}
                if _snip is not None:
                    # Snippet labels preview their expansion dim (detail wins).
                    # Leading space: the suffix draws flush after the label
                    # (right for noparams, wrong for a label).
                    ds._ac_params = {s.label: " " + (s.detail or s.insert)
                                     for s in _snip[2]}
                else:
                    # Dim '(param, param2)' suffixes for callable rows —
                    # resolved live, memoized per callable in _SIG_CACHE.
                    ds._ac_params = _ac_param_suffixes(ds, text, cands, anchor,
                                                       dot_trigger, jump_to)
                ac_state.cursor_path = (names[ds._ac_index],)
                ac_state.open_path = ()
            else:
                ds._ac_open = False

        # --- Function call parameter hints (signature help) ---
        # Shown while EDITING a call's parameters, never on a bare caret move -
        # a click into existing parens stays quiet (the click handler above
        # disarms), and Ctrl+P asks explicitly. A buffer edit made with the
        # caret inside call parens arms the hint at that call - keyed on the
        # '(' index, so edits after it won't shift - and it stays up (the
        # active arg recomputed locally each frame, jedi re-queried only when
        # the callee changes) until the caret leaves that call's parens.
        # `_ac_sig_show` gates the render below. Skipped in the Eval REPL box
        # (completion_source): jedi can't see its runtime-typed locals, and we
        # don't want a subprocess completion job fired per keystroke in a
        # one-liner.
        _pf("kbd:ac")
        # --- FIM ghost text: reconcile the buffer against this frame's final
        # edit, schedule/continue requests, and stash what the render below
        # (and next frame's accept handler) should show.
        if (fim_state is not None and not is_search_box and not single_line
                and not is_diff and completion_source is None):
            # "typed" = the buffer changed by CONTENT typing. Backspace /
            # Delete / Tab / Enter edit the buffer too but never start a
            # generation (they only reconcile or abort a showing ghost).
            # Read from the raw frame keys - handlers discard consumed keys
            # from `_fired` (an accepted Tab is gone by now).
            _fim_typed = changed and not any(k in _FIM_NON_TRIGGER_KEYS for k, _m in _frame_keys)
            ds._fim_ghost = _fim_poll(ds, fim_state, text, jump_to, fim, typed=_fim_typed)
        else:
            ds._fim_ghost = None
        _pf("kbd:fim")
        if ac_enabled and completion_source is None:
            _open_paren, _arg_index = _call_context(text, ds.text_cursor_pos)
            if any(key == glfw.KEY_ESCAPE for key, _ in _frame_keys):
                text_editor_state._signature_dismissed = _open_paren
            elif ctrl and pressed(glfw.KEY_P):
                text_editor_state._signature_dismissed = None
            elif _open_paren != getattr(text_editor_state, '_signature_dismissed', None):
                text_editor_state._signature_dismissed = None
            _sig_req = getattr(ds, '_ac_sig_request_paren', -1)
            if (_open_paren is None
                    or _open_paren == getattr(text_editor_state, '_signature_dismissed', None)):
                _sig_req = -1
            elif changed or (ctrl and pressed(glfw.KEY_P)):
                _sig_req = _open_paren
            elif _sig_req != _open_paren:
                _sig_req = -1   # caret move into a call it wasn't armed for
            ds._ac_sig_request_paren = _sig_req
            if _sig_req != -1 and _ensure_signature_help(
                    ds, text, _open_paren, ds.text_cursor_pos, jump_to) is not None:
                ds._ac_sig_active = _arg_index
                ds._ac_sig_open_paren = _open_paren   # lets the hint align under the call name
                ds._ac_sig_show = True
            else:
                ds._ac_sig_show = False

    # Clamp
    ds.text_cursor_pos = max(0, min(ds.text_cursor_pos, len(text)))
    ds.text_selection_start = max(0, min(ds.text_selection_start, len(text)))
    ds.text_selection_end = max(0, min(ds.text_selection_end, len(text)))

    # Display edit splice (fold-collapsed edit frames only): with a collapsed
    # fold, tint/usage resolves against the frame-start FULL buffer and the
    # fold remap is built on the frame-start layout, so their output lags this
    # body run's edit by exactly the typed/deleted characters for one frame.
    # Diff the frame-start display text against the edited one so the washes
    # can be arithmetically shifted by the splice below.
    if _fold_segments and text is not original_input:
        _disp_sp = _display_edit_splice(original_input, text)

    # Visual-column map over the render region (text is final now). `_colx(idx)`
    # gives the line-relative visual x (px) of a source index, honouring all
    # token-view widths; with no views it's just the plain character column.
    vcols = _get_vcols()
    def _colx(idx, line_start=None):
        # Line-relative visual x (px) of source `idx`. With inline views in this
        # window, read the window vcols; otherwise (idx off-window - those aren't
        # drawn) it's just the character column, O(1) if line_start is known.
        if vcols is not None:
            cell = vcols.cell(idx)
            if cell is not None:
                return cell * char_w
        col = (idx - line_start) if line_start is not None else _index_to_line_col(text, idx)[1]
        return col * char_w

    # Hide the parse-error display while it's STALE: the buffer has been edited
    # since this error/code_tree was parsed (the reparse runs in the body),
    # so its line numbers are out of date or it may already be fixed. The stale
    # flag is set + cleared at the end of the body (see "Parse-error staleness").
    # Also hide while a completion/signature popup is up (code mid-edit).
    # A fresh fast-path marker (see the block up top) is exempt from the stale
    # hide - it was computed against this very text - but still yields to the
    # popup suppression like every other marker.
    if is_focused and (getattr(ds, '_ac_open', False)
                       or getattr(ds, '_ac_sig_show', False)):
        _err_markers = []
        _err_msg = None
    elif getattr(ds, '_err_stale', False) and not _fast_fresh_err:
        _err_markers = list(_caller_markers)      # the caller's marker is never stale
        _err_msg = None

    _pf("keyboard")
    # --- Find-in-text search ---
    # The term arrives either forwarded from an ancestor search owner (as a
    # SearchTerm carrying the shared cross-view session) or, when this editor
    # hosts the find UI itself, on ds.search_text with a session it pushed.
    # We search locally, register our matches to the session so every view
    # combines into one global set, and scroll to the global-current match
    # when it lands in this view.
    # The find UI's search input box (is_search_box) never matches: its text IS
    # the query, so the term an ancestor owner forwards down would highlight
    # every character typed into it as a hit.
    search_term = ("" if is_search_box
                   else search_text or (ds.search_text if ds.search_active else ""))
    # Match against the FULL buffer, not the fold display text: results
    # inside collapsed folds must be found and counted, and the fold section
    # already auto-expanded when it hides the CURRENT match. Cached
    # by (text identity, term) - shared with the fold section's lookup.
    # Late-bound on edit frames (same pattern as _view_usage_spans): with no
    # collapsed fold the full buffer IS `text`, the buffer the glyph pass
    # draws this frame - keyboard handling above reassigns it, while _fold_full
    # stays the frame-start buffer. Matching _fold_full left every highlight
    # after the caret off by the typed/deleted characters for the edit frame.
    # With a fold collapsed the match stays frame-start (_fold_off projects
    # against the frame-start layout) and the resulting display coords are
    # splice-shifted across this run's edit below.
    _match_base = _fold_full if _fold_segments else text
    _smc = getattr(ds, '_search_match_cache', None)
    if (_smc is not None and _smc[0] is _match_base
            and _smc[1] == str(search_term)):
        search_matches = _smc[2]
    else:
        search_matches = _find_matches(_match_base, search_term)
        ds._search_match_cache = (_match_base, str(search_term), search_matches)
    # Display-coord projection for highlight/scroll; a match still hidden
    # inside a collapsed fold projects to None and just isn't drawn. Slot
    # alignment with search_matches is kept (current_local indexes into it).
    if _fold_segments and search_matches:
        _sm_disp = []
        for _sm_s, _sm_e in search_matches:
            _p0 = _fold_off(_sm_s)
            if _p0 is None:
                _sm_disp.append(None)
            else:
                _p1 = _fold_off(_sm_e)
                _sm_disp.append((_p0, _p1 if _p1 is not None
                                 else _p0 + (_sm_e - _sm_s)))
        if _disp_sp is not None:
            # Edit frame with a fold collapsed: the projections above are
            # frame-start - shift them across this run's splice so the
            # highlights track the glyphs (same remap as the usage washes,
            # done by hand here because _display_splice_shift drops entries
            # and the None slots must survive for index alignment).
            _sp_p, _sp_oe, _sp_d = _disp_sp[0], _disp_sp[1], _disp_sp[2]
            for _i, _m in enumerate(_sm_disp):
                if _m is None:
                    continue
                if _m[0] >= _sp_oe:
                    _sm_disp[_i] = (_m[0] + _sp_d, _m[1] + _sp_d)
                elif _m[1] > _sp_p:
                    # Touches the edited region: the text under it changed -
                    # hide for this frame; next frame the rescan re-finds it.
                    _sm_disp[_i] = None
    else:
        _sm_disp = search_matches

    # Which local match (if any) is the global-current one is decided by a
    # search owner's pre-body tree walk (search_walk), not by claiming here:
    # the walk is the single source for both the count and the selection, so
    # off-screen views the render skips can't shift the indices. We just read
    # the local index the walk stashed on us and highlight/scroll to it.
    if isinstance(search_term, SearchTerm):
        session = search_term
    elif ds.search_active and ds._search_session is not None:
        session = ds._search_session
    else:
        session = None

    local_count = len(search_matches)
    if session is not None:
        current_local = ds._search_active_local
        if current_local is not None and current_local >= local_count:
            current_local = None
        # Scroll to it on a full-search frame (term change / nav). An empty
        # search box never moves the view: no term means nothing to reveal.
        should_scroll = (bool(str(search_term))
                         and current_local is not None and session.scroll_to)
    else:
        current_local = None
        should_scroll = False

    # Stash a matcher so a search owner can recount this editor's matches by
    # walking the live draw_state tree (DrawState.descendants / search_walk)
    # without re-rendering it - the key to counting off-screen editors. Set
    # every render (capturing the current text) so an editor that has since
    # scrolled out still contributes its count. The find UI's own input box
    # (is_search_box) must never self-count, so it clears any matcher - its text
    # IS the query, so a matcher inside would always self-match (phantom +1).
    if not is_search_box:
        # _match_base: the full buffer (hidden-in-fold matches count too),
        # post-shift when this body run edited it - see the match section above.
        ds._search_matcher = (
            lambda term, sess, _t=_match_base: sess.claim(len(_find_matches(_t, term))))
    else:
        ds._search_matcher = None

    if should_scroll and _sm_disp[current_local] is not None:
        ms, me = _sm_disp[current_local]
        line, _col = _index_to_line_col(text, ms)
        # Vertical: scroll the editor (or its scroll parent) so the match
        # is fully on screen. Pass the line's full vertical band [top, bottom] in
        # screen space - _scroll_into_view takes (top_abs, bottom_abs), so a
        # match ABOVE the viewport scrolls up and one BELOW scrolls down.
        # Anchor on origin_y (the actual rendered content top, == abs_top minus
        # the editor's own vertical scroll) - the SAME origin the highlight is
        # drawn at below (origin_y + m_line * line_px). Using ds.abs_top here
        # would ignore the editor's self-scroll, so the computed origin always
        # sat at-or-below the true one: the view only ever scrolled down (never
        # up) and the match landed off-screen whenever the editor owned its
        # scrollbar.
        match_top_abs = origin_y + line * line_px
        _scroll_into_view(ds, match_top_abs, match_top_abs + line_px, center=True)

        # Horizontal: default back to the line start (h_scroll 0) while paging
        # through results, scrolling to only when the match wouldn't fit.
        # For a multi-line match only the first line drives the horizontal
        # scroll - _colx(me) on a later line would be meaningless here.
        _nl = text.find('\n', ms, me)
        match_x = _colx(ms)
        match_x_end = _colx(me if _nl == -1 else _nl)
        edge_padding = 20.0
        if text_visible_width > 0:
            if match_x_end <= text_visible_width - edge_padding:
                ds.text_h_scroll = 0.0
            else:
                # Pin the match's end to the right edge so we scroll the least
                # amount needed to reveal it, instead of dragging it to the left.
                ds.text_h_scroll = max(0.0, match_x_end - text_visible_width + edge_padding)
        request_render()
        # Flash the match the search LANDED on (the familiar browser's search
        # feel): a Melty.emphasize on its cells, only when the landing match
        # moves - a keystroke that keeps the same first match (like on
        # from "op" to "open") stays quiet, a nav step or a term that lands
        # elsewhere flashes. Keyed by the match start in this frame.
        _flash_at = (ms, id(_match_base))
        if getattr(ds, '_search_flash_at', None) != _flash_at:
            ds._search_flash_at = _flash_at
            _fl_line_start = text.rfind('\n', 0, ms) + 1
            _fl_end = me if _nl == -1 else _nl
            _fl_cols = (ms - _fl_line_start, max(_fl_end - _fl_line_start, ms - _fl_line_start + 1))
            _fl_line1 = line if _nl == -1 else _index_to_line_col(text, me)[0]

            def _search_flash_rect(ds=ds, li0=line, li1=_fl_line1,
                                   cols=_fl_cols if _nl == -1 else None):
                lp = getattr(ds, '_diff_line_px', None) or 16
                inset = getattr(ds, '_diff_top_inset', 0)
                y0 = ds.abs_top + inset + li0 * lp - ds.scroll_offset[1]
                y1 = ds.abs_top + inset + (li1 + 1) * lp - ds.scroll_offset[1]
                vt, vb = ds.abs_top, ds.abs_top + (ds.height or 0)
                if y1 < vt or y0 > vb:
                    return None
                x0, x1 = ds.abs_left, ds.abs_left + (ds.width or 0)
                cw = getattr(ds, '_diff_char_w', None)
                ox = getattr(ds, '_diff_origin_x_off', None)
                if cols is not None and cw and ox is not None:
                    x0 = max(x0, ds.abs_left + ox + cols[0] * cw - 4)
                    x1 = min(x1, ds.abs_left + ox + cols[1] * cw + 4)
                    if x1 <= x0:
                        return None
                return (x0, max(y0, vt) - 2, x1, min(y1, vb) + 2)

            Melty.emphasize(f"search_landing {ds.name}", _search_flash_rect,
                            fade_frames=36, rounding=4.0)
    _pf("find_search")


    # --- Horizontal auto-scroll ---
    # Only kicks in when the cursor moved this frame, so middle-drag pans
    # are not snapped back. Brings the cursor into view on a single line.
    visible_width = text_visible_width
    if (ds.text_cursor_pos != ds.text_prev_cursor_pos and visible_width > 0
            and not restore_active):
        cursor_logical_x = _colx(ds.text_cursor_pos)
        edge_padding = 20.0
        if cursor_logical_x - ds.text_h_scroll < edge_padding:
            ds.text_h_scroll = max(0.0, cursor_logical_x - edge_padding)
        elif cursor_logical_x - ds.text_h_scroll > visible_width - edge_padding:
            ds.text_h_scroll = cursor_logical_x - visible_width + edge_padding

    # --- Undo/redo landing ---
    # The wrapper tail stamped `_undo_landing` = (restored text, start, end,
    # frame) when Ctrl+Z / Ctrl+Shift+Z restored this view's value (see the
    # undo interception in pre_render). Once the restored buffer has flowed
    # back in as parent input, scroll the changed range into view (centered when
    # it sits off-screen; untouched when already visible) and flash it - the
    # same yellow emphasis a usage jump gets - so an undo of an edit made
    # far from the viewport is never a silent, invisible change. Consumed at
    # most once; dropped after a few frames if the restored text never shows
    # up (the parent rejected the write).
    _ul = getattr(ds, '_undo_landing', None)
    if _ul is not None and line_px and not is_search_box:
        _ul_text, _ul_a, _ul_b, _ul_frame = _ul
        _ul_stale = Melty.frame_count - _ul_frame > 8
        if _ul_stale:
            ds._undo_landing = None
        elif _fold_full == _ul_text:
            ds._undo_landing = None
            _n = len(_fold_full)
            _ua = min(max(_ul_a, 0), _n)
            _ub = min(max(_ul_b, _ua), _n)
            _uli0 = _fold_full.count('\n', 0, _ua)
            _uli1 = _fold_full.count('\n', 0, _ub)
            # Columns for a single-line change (multi-line, identical in full and
            # white space); a pure delete still gets a 1-cell marker.
            _ucols = None
            if _uli0 == _uli1:
                _uls = _fold_full.rfind('\n', 0, _ua) + 1
                _ucols = (_ua - _uls, max(_ub - _uls, _ua - _uls + 1))
            # Fold projection: expand any collapsed fold hiding the change and
            # map both ends into visible lines (see fold_project_jump).
            _, _uli0 = fold_project_jump(ds, _fold_full, _ua, _uli0)
            if _ub != _ua:
                _, _uli1 = fold_project_jump(ds, _fold_full, _ub, _uli1)
            else:
                _uli1 = _uli0
            # Live-origin compensation, same as the caret-follow below: an
            # earlier fold jump in this body leaves origin_y stale.
            _uty0 = (origin_y + (_origin_sy - ds.scroll_offset[1])
                     + _uli0 * line_px)
            _uty1 = (origin_y + (_origin_sy - ds.scroll_offset[1])
                     + (_uli1 + 1) * line_px)
            _scroll_into_view(ds, _uty0, min(_uty1, _uty0 + 6 * line_px),
                              center=True)

            def _undo_flash_rect(ds=ds, li0=_uli0, li1=_uli1, cols=_ucols):
                lp = getattr(ds, '_diff_line_px', None) or 16
                inset = getattr(ds, '_diff_top_inset', 0)
                y0 = ds.abs_top + inset + li0 * lp - ds.scroll_offset[1]
                y1 = ds.abs_top + inset + (li1 + 1) * lp - ds.scroll_offset[1]
                vt, vb = ds.abs_top, ds.abs_top + (ds.height or 0)
                if y1 < vt or y0 > vb:
                    return None
                x0, x1 = ds.abs_left, ds.abs_left + (ds.width or 0)
                cw = getattr(ds, '_diff_char_w', None)
                ox = getattr(ds, '_diff_origin_x_off', None)
                if cols is not None and cw and ox is not None:
                    x0 = max(x0, ds.abs_left + ox + cols[0] * cw - 3)
                    x1 = min(x1, ds.abs_left + ox + cols[1] * cw + 3)
                    if x1 <= x0:
                        return None
                return (x0, max(y0, vt) - 1, x1, min(y1, vb) + 1)

            Melty.emphasize(f"undo_landing {ds.name}", _undo_flash_rect)
            ds.invalidate()
            request_render()

    # --- Vertical auto-scroll ---
    # Vertical counterpart of the horizontal follow above: when the caret moves
    # to a line off the top/bottom of the viewport (typing past the last visible
    # line, wheeling/paging the cursor away, pasting a multi-line block), scroll
    # the editor - or its scroll container - so the caret's line comes back into
    # view. Same cursor-moved test so wheel/middle-drag pans that leave the caret
    # put are not snapped back. Anchors on origin_y and hands _scroll_into_view
    # the caret line's full vertical band exactly like the search scroll above.
    # Never for the find box: it's pinned to the host view's clip (bottom-left),
    # so _scroll_into_view walks up to the HOST editor's scroll container and
    # nudges it by the box's bottom-height overflow - and since the pin leaves
    # the box put, the same overflow reapplies every keystroke, creeping the
    # host view up a line per typed character.
    if (ds.text_cursor_pos != ds.text_prev_cursor_pos and line_px
            and not is_search_box and not restore_active):
        cursor_line, _ = _index_to_line_col(text, ds.text_cursor_pos)
        # LIVE origin, not the body-start origin_y: the Ctrl+B usage jump runs
        # EARLIER in this editor body (the picker jump is later, which is why
        # only Ctrl+B failed) - it moves the caret AND writes the centered
        # scroll_offset, so origin_y is stale by the jump's scroll time. The
        # follow then computed the caret ~100k px off-screen and slammed its
        # fresh scroll to the clamp (top of file): "jumps to some other line".
        cursor_top_abs = (origin_y + (_origin_sy - ds.scroll_offset[1])
                          + cursor_line * line_px)
        _scroll_into_view(ds, cursor_top_abs, cursor_top_abs + line_px)

    # --- Bring-to-front on caret/selection edits ---
    # Moving the caret or changing the selection in an editor whose window
    # sits behind another raises that window - the keyboard counterpart of
    # click-to-raise (which only fires on clicks). Gated on this editor
    # being text focus so programmatic caret writes into an unfocused
    # editor (external jumps, session restores) don't steal z-order, and on
    # the owning root window not already being front so plain typing in
    # the front editor won't queue a move every keystroke.
    _sel_now = (ds.text_selection_start, ds.text_selection_end)
    if ((ds.text_cursor_pos != ds.text_prev_cursor_pos
            or _sel_now != getattr(ds, '_prev_sel_state', _sel_now))
            and Melty.text_focused_ds is ds):
        _root, _n = ds, 0
        while (_root._tile_id not in Melty.registered_windows
               and _root.parent_window is not None
               and _root.parent_window is not _root and _n < 64):
            _root = _root.parent_window
            _n += 1
        if (_root._tile_id in Melty.registered_windows
                and next(reversed(Melty.registered_windows), None) != _root._tile_id):
            Melty.move_window_to_front(ds)
    ds._prev_sel_state = _sel_now

    ds.text_prev_cursor_pos = ds.text_cursor_pos

    # Clamp h_scroll to content bounds - the widest line drives the limit. Uses
    # plain character count (vcols now covers only the visible window, not the
    # whole buffer); inline widgets widen a line by a couple of cells, so the
    # h-scroll limit can be a hair short on widget-heavy lines - harmless.
    # Memoized by text IDENTITY: the buffer object is stable across
    # selection / caret / scroll frames, so the O(N) scan runs only
    # when the content actually changes.
    _mll = getattr(ds, '_max_line_len', None)
    if _mll is None or _mll[0] is not text:
        _mll = (text, max(map(len, text.split('\n')), default=0))
        ds._max_line_len = _mll
    max_line_width = _mll[1] * char_w
    max_h_scroll = max(0.0, max_line_width - visible_width + 50.0)
    ds.text_h_scroll = max(0.0, min(ds.text_h_scroll, max_h_scroll))
    origin_x = left + gutter_w + gutter_margin - ds.text_h_scroll

    _pf("autoscroll")
    # --- Drawing ---
    draw_list = imgui.get_window_draw_list()
    # Text content is clipped to start after the gutter, so highlights never
    # bleed under the line numbers when scrolled horizontally.
    rect_min_x = left + gutter_w + gutter_margin
    # Clip the text body to start below the floating jump-to bar so scrolled code
    # never appears over it (the bar is drawn above, before the body).
    rect_min_y = draw_state.abs_clip_rect[1] + bar_height
    rect_max_x = left + draw_state.content_width
    # content_width reserves up scroll_bar_width + margin for the scroll bar.
    # While a click-drag is in flight the wrapper clip removes that reserve,
    # so widen the body clip to match and let glyphs run under the bar.
    # freeze_resize panes never gave anything up: content_width already
    # caps the right edge and blit_offscreen draws the bar over it.
    if Melty.on_drag and not getattr(draw_state, "freeze_resize", False):
        rect_max_x += scroll_bar_width
    rect_max_y = draw_state.abs_clip_rect[3]

    # The text ROWS: from the gutter's right edge and the first drawn line's
    # top to the bottom of the last one (with edges once the text scrolls
    # past them). The selection subscription lives here and nowhere else - a
    # left_mouse_drag on the gutter or in the empty space above the first / below
    # the last line is left unsubscribed (event_rect), so it falls through to
    # the enclosing window's move handle and drags the WINDOW.
    # left_mouse_down remains view-wide: a press on the gutter still toggles
    # folds / opens the usage picker / places the caret, and a press below
    # the last line puts the caret at the end.
    text_rows_rect = (rect_min_x, max(rect_min_y, origin_y), rect_max_x,
                      min(rect_max_y, origin_y + len(_line_starts(text)) * line_px))
    draw_state.event_rect(("left_mouse_drag", "left_mouse_held"), text_rows_rect)

    # Native I-beam over the text rows (gutter, jump bar, scrollbar and the
    # window-drag space above the text keep the arrow). Cursor-only
    # subscription - no events - so it ignores the click subs' z-order /
    # blocker rules and sticks through a selection drag.
    # priority_delta=3 lands this AT the view's wrapper registration
    # (core_render registers event params at `priority - 3`): the blocker
    # pass keeps only entries at/above the enclosing closable window's own
    # `priority - 3`, and a delta-0 entry from a wrapper child sits below
    # that and would be pruned by its own window.
    draw_state.on_action([], view_id="text_cursor", rect=text_rows_rect,
                         priority_delta=3, cursor=mouse_cursor.TEXT)

    draw_list.push_clip_rect(rect_min_x, rect_min_y, rect_max_x, rect_max_y, True)

    # Definition tints (drawn FIRST, under everything): a block wash behind
    # every tinted class/def/etc in this buffer - top-left corner at the def
    # keyword's first character, bottom at the last line before the dedent,
    # right edge wrapping the block's widest line - plus a small wash behind every occurrence
    # of a symbol whose definition (here or in another file) carries a tint,
    # in that definition's color. Ties usages to their definitions at a glance.
    _dt_blocks = _dt_spans = _dt_lines = _dt_comments = ()
    # Tints off (or in search box): no block tint under any pill.
    if ds.__dict__.get("_lv_tint_blocks"):
        object.__setattr__(ds, "_lv_tint_blocks", ())
    # Open this body run's glow group: live cache lets glows from the last
    # run drop unless re-emitted below (so toggling tints off or scrolling the
    # bands away really clears them), while cache-skipped frames never reach
    # this point at all. HOLD the group (skip the clear) when tints are
    # ON but the code tree is transiently unreadable - _def_tints returns
    # empty from a non-dict tree BYPASSING its last-good machinery, and a
    # hover-coincident body run during initial parse churn would read
    # that as "tints removed" and drop the retained glow at random.
    _dt_on = Toggles.TextEditor.definition_tints and not is_search_box
    # Roster tints don't read the parse tree - no parse-churn hold needed.
    if not (_dt_on and not Toggles.TextEditor.roster_def_tints
            and not isinstance(_usage_tree, dict)):
        clear_glows(ds)
    if _dt_on:
        _t_dt = time.perf_counter()


        _k_dt = getattr(ds, "_def_tints_key", None)
        # Resolve target: the buffer whose glyph positions will draw THIS frame.
        # With no collapsed folds that is `text` - which key handling above
        # may have just edited - not _fold_full (frame-start buffer). The
        # anchor resolve re-finds displaced lines by name per call, but
        # locks onto the target it's handed; without it the pre-edit buffer
        # drew every wash/glow below an inserted newline one line off for
        # exactly the first frame (the snap-back flicker). With a collapsed
        # fold the display remap below is built on the frame-start layout,
        # so _fold_full stays the consistent (one-frame-stale) target.
        _dt_full = _fold_full if _fold_segments else text
        # Visible band in FULL-buffer lines for the roster's chunked view
        # (display lines via the fold map when a fold is collapsed).
        _dt_vis = None
        if Toggles.TextEditor.roster_def_tints and line_px:
            try:
                _clip = draw_state.abs_clip_rect
                _v0 = max(0, int((_clip[1] + bar_height - top) / line_px) - 3)
                _v1 = max(_v0, int((_clip[3] - top) / line_px) + 3)
                if _fold_d2b is not None and _fold_d2b:
                    _v0 = _fold_d2b[min(_v0, len(_fold_d2b) - 1)]
                    _v1 = _fold_d2b[min(_v1, len(_fold_d2b) - 1)]
                _dt_vis = (_v0, _v1)
            except Exception:
                _dt_vis = None
        # The file the buffer belongs to: the jump_to Address, else - only
        # for a world / detached pane, which never holds the buffer - the
        # wrapper's file_key memo.
        _dt_path = getattr(jump_to, 'path', None) if jump_to is not None else None
        if (_dt_path is None and (roster_world is not None or roster_table is not None)
                and isinstance(getattr(ds, '_file_meta', None), str)):
            _dt_path = ds._file_meta
        _pf("dt:pre")
        _dt_blocks, _dt_spans, _dt_lines, _ = _def_tints(
            ds, _dt_full, _usage_tree, _usage_off, _dt_path,
            vis=_dt_vis, hold_live=roster_live_hold, world=roster_world,
            table=roster_table)
        _pf("dt:def_tints")
        # Fold remap: def tints resolve against the FULL buffer (keeps the
        # last-good/anchor caches fold-independent); project the back into
        # display coords. Blocks whose head line is visible keep their wash,
        # with the extent clamped to the last visible line (a collapsed class
        # still washes its header row); entries living entirely on hidden
        # lines drop.
        if _fold_bl is not None:
            # Memoized like _fold_remap_spans (inputs + fold are held by
            # reference so id reuse can't alias): the remap itself is cheap,
            # but downstream memos (_b_lvls, _scope_surface) key on the
            # OUTPUT lists' identity, so they must be stable frame to frame.
            _dtm = getattr(ds, '_fold_dt_memo', None)
            if (_dtm is not None and _dtm[0] is _dt_blocks
                    and _dtm[1] is _dt_lines and _dtm[2] is _fold_built):
                _dt_blocks, _dt_lines = _dtm[3], _dtm[4]
            else:
                _rb = []
                for _b_ln, _b_ix, _b_end, _b_tt in _dt_blocks:
                    _dl = _fold_bl(_b_ln)
                    if _fold_d2b[_dl] != _b_ln:
                        continue
                    _dix = _fold_off(_b_ix)
                    if _dix is None:
                        continue
                    _rb.append((_dl, _dix, _fold_bl(_b_end), _b_tt))
                _rl = []
                for _l_ln, _l_rgb, _l_sc, _l_si, _l_ei in _dt_lines:
                    _dl = _fold_bl(_l_ln)
                    if _fold_d2b[_dl] != _l_ln:
                        continue
                    _dsi, _dei = _fold_off(_l_si), _fold_off(_l_ei)
                    if _dsi is None:
                        continue
                    _rl.append((_dl, _l_rgb, _l_sc, _dsi,
                                _dei if _dei is not None else _dsi + (_l_ei - _l_si)))
                ds._fold_dt_memo = (_dt_blocks, _dt_lines, _fold_built,
                                    tuple(_rb), tuple(_rl))
                _dt_blocks, _dt_lines = ds._fold_dt_memo[3], ds._fold_dt_memo[4]
            _dt_spans = _fold_remap_spans(_dt_spans, 'dt')
        # The DISPLAY-coordinate block list for the live-value pills
        # (see live_views._pill_tint: a pill wears the tint of the block
        # under its cursor). A tuple ref - the pill memo keys on its identity.
        object.__setattr__(ds, "_lv_tint_blocks", _dt_blocks)
        # Comment-text tints come from a direct scan of the buffer text - no
        # code_tree, no debounce, so a tint comment colors as it's typed
        # instead of waiting on the cst-dict round trip. Scans over the
        # FULL buffer (keeps the cache fold-independent, and a multi-line
        # override truncated at a fold seam wouldn't parse), then projected
        # into display coords: a collapsed run's visible header line keeps
        # its paint, clamped to that line so the color can't run past the
        # seam onto whatever follows the fold badge.
        _pf("dt:fold_remap")
        _dt_comments = _comment_tints(ds, _dt_full)
        _pf("dt:comment_tints")
        if _fold_bl is not None and _dt_comments:
            # Memoized like _fold_remap_spans (inputs + fold layout by
            # identity): the glyph pass's per-token surface memo keys on
            # the tuple's identity, and a fresh tuple every frame missed
            # it on every frame with a fold collapsed.
            _cm = getattr(ds, '_fold_comment_memo', None)
            if (_cm is not None and _cm[0] is _dt_comments
                    and _cm[1] is _fold_built):
                _dt_comments = _cm[2]
            else:
                _rc = []
                for _c_si, _c_ei, _c_rgb in _dt_comments:
                    _dsi = _fold_off(_c_si)
                    if _dsi is None:
                        continue
                    _dei = _fold_off(_c_ei)
                    if _dei is None:      # tail hidden - clamp to the header line
                        _dei = text.find('\n', _dsi)
                        if _dei == -1:
                            _dei = len(text)
                    _rc.append((_dsi, _dei, _c_rgb))
                ds._fold_comment_memo = (_dt_comments, _fold_built, tuple(_rc))
                _dt_comments = ds._fold_comment_memo[2]
        if _disp_sp is not None:
            # Edit frame with a fold collapsed: everything above (resolve +
            # fold remap) is frame-start; shift all four overlay families
            # across this frame's edit splice so the washes/glows track the
            # glyphs instead of lagging by the typed/deleted characters
            # (see _display_splice_shift).
            _dt_blocks, _dt_lines, _dt_spans, _dt_comments = (
                _display_splice_shift(_disp_sp, text, _dt_blocks, _dt_lines,
                                      _dt_spans, _dt_comments))
        _pf("dt:comment_remap+splice")
        _pf_info['dt_call_ms'] = round((time.perf_counter() - _t_dt) * 1000.0, 1)
        _pf_info['dt_miss'] = _k_dt is not getattr(ds, "_def_tints_key", None)
        _pf_info['dt_n'] = (len(_dt_blocks), len(_dt_lines), len(_dt_spans))
        # ALL def-tint washes paint on the UNDER-text channel (same idiom as
        # the cursor-token highlight below): translucent rects must never be
        # able to land over glyphs - the tile pipeline composites re-renders
        # over prior content, so transparent-over-text accumulates copies and
        # clouds the final color with tile count.
        # single_line micro-buffers (global search's code rows) stay on the
        # TEXT channel instead: their single tiles fully repaint on every
        # re-render (no partial-recomposite accumulation to guard against),
        # and the under channel loses to the enclosing window's composite
        # there so washes only showed while typing forced live re-renders. The
        # washes draw before the glyphs, so same-channel command order still
        # keeps them underneath.
        if Melty.channels_split:
            draw_list.channels_set_current(
                Core.melty.get_channel() - (0 if single_line else 1))
        # Per-row color adjustment (hsv shift and brightness clamp) - see
        # _bg_adjust. Blocks, line bands, and symbol washes each get their
        # own saturation/value pair; the brightness clamp is shared.
        _min_b = Toggles.TextEditor.bg_min_brightness
        _max_b = Toggles.TextEditor.bg_max_brightness
        _bg_f = (Toggles.TextEditor.bg_tint_saturation,
                 Toggles.TextEditor.bg_tint_value, _min_b, _max_b)
        _sym_f = (Toggles.TextEditor.symbol_tint_saturation,
                  Toggles.TextEditor.symbol_tint_value, _min_b, _max_b)
        _line_f = (Toggles.TextEditor.line_tint_saturation,
                   Toggles.TextEditor.line_tint_value, _min_b, _max_b)
        _dt_block_a = Toggles.TextEditor.def_block_alpha
        _dt_outline_a = Toggles.TextEditor.def_outline_alpha
        _dt_outline_t = Toggles.TextEditor.def_outline_thickness
        _dt_outline_b = Toggles.TextEditor.def_outline_brightness
        _dt_sym_ol_a = Toggles.TextEditor.def_symbol_outline_alpha
        _dt_sym_ol_t = Toggles.TextEditor.def_symbol_outline_thickness
        _dt_sym_ol_b = Toggles.TextEditor.def_symbol_outline_brightness
        # Compositor shadows under the washes (add_shadow depth marks; 0
        # disables). All marks clip to the visible text rect - partially
        # scrolled rows still draw here.
        _dt_block_sh = Toggles.TextEditor.def_block_shadow_offset
        _dt_sym_sh = Toggles.TextEditor.def_symbol_shadow_offset
        _sh_clip = (rect_min_x, rect_min_y, rect_max_x, rect_max_y)
        # Scope-aware depth: each block's shadow base is its NESTING level
        # (how many other blocks contain it) × the block offset, so a
        # method's wash sits above its class's, and the class above the
        # page surface. Per block the mark then PEELS: top corners at the
        # base (stuck flush to the enclosing scope - no shadow at the top
        # edge), bottom corners one step up, easing down (the peel).
        # Levels are also needed (shadow or not) to keep ROOT blocks apart
        # when root washes are off: the caller's show_root_backgrounds=False
        # (global-search embeds) is honoured only while
        # Toggles.TextEditor.root_symbol_tints is False.
        show_root_backgrounds = (show_root_backgrounds
                                 or Toggles.TextEditor.root_symbol_tints)
        _b_list = list(_dt_blocks) if (_dt_block_sh or not show_root_backgrounds) else []
        # O(blocks²), so memoized by the block tuple's identity (the ref in
        # the memo guards id change) - recomputing every frame was a real
        # render-thread cost on buffers with hundreds of defs.
        _blm = getattr(ds, '_dt_blvl_memo', None)
        _blm_key = (bool(_dt_block_sh), bool(show_root_backgrounds))
        if (_blm is not None and _blm[0] is _dt_blocks
                and _blm[1] == _blm_key):
            _b_lvls = _blm[2]
        else:
            _b_lvls = []
            for _l0, _i0, _e0, _t0 in _b_list:
                _b_lvls.append(sum(
                    1 for _l1, _i1, _e1, _t1 in _b_list
                    if (_l1 <= _l0 and _e0 <= _e1
                        and (_l1, _e1) != (_l0, _e0))))
            ds._dt_blvl_memo = (_dt_blocks, _blm_key, _b_lvls)

        # Per-line result cache for _scope_surface: each call scans every
        # block, and the wash/symbol-shadow passes call it per visible def
        # line per frame, it depends only on (block list, offset knob).
        _ssm = getattr(ds, '_dt_surf_memo', None)
        if (_ssm is None or _ssm[0] is not _dt_blocks
                or _ssm[1] != _dt_block_sh):
            _ssm = ds._dt_surf_memo = (_dt_blocks, _dt_block_sh, {})
        _surf_cache = _ssm[2]

        def _scope_surface(line):
            # Depth of the enclosing-t's surface at `line`: the innermost
            # containing block's base + its peel, interpolated with the same
            # smoothstep the gradient shader applies, so chips ride a
            # constant lift above the surface beneath them.
            best = _surf_cache.get(line)
            if best is not None:
                return best
            best, best_lvl = 0.0, -1
            for _sbi, (_l0, _i0, _e0, _t0) in enumerate(_b_list):
                if _l0 <= line <= _e0 and _b_lvls[_sbi] > best_lvl:
                    best_lvl = _b_lvls[_sbi]
                    t = (line - _l0) / max(1, _e0 - _l0)
                    t = t * t * (3.0 - 2.0 * t)
                    best = _dt_block_sh * (_b_lvls[_sbi] + t)
            _surf_cache[line] = best
            return best

        def _ol_rgb(c, b=None):
            # Outline color: the wash color pushed BRIGHTER than the bg
            # clamp allows - a 1-2px edge needs far more luminance than a
            # translucent fill to pop against the editor background.
            b = _dt_outline_b if b is None else b
            return (min(1.0, c[0] * b),
                    min(1.0, c[1] * b),
                    min(1.0, c[2] * b))
        # Per-line content lengths (rstripped chars) cached on the
        # draw_state per text identity: the block washes below wrap to the
        # widest line in their span instead of running to the view edge.
        _ll = getattr(ds, "_dt_line_lens", None)
        if _dt_blocks and (_ll is None
                           or getattr(ds, "_dt_line_lens_text", None) is not text):
            previous = getattr(ds, '_dt_line_lens_text', None)
            _ll = ds._dt_line_lens = (_update_line_widths(previous, text, _ll)
                                     if previous is not None and _ll is not None else
                                     [len(row.rstrip()) for row in text.split('\n')])
            ds._dt_line_lens_text = text
        _pf("w:pre_blocks")
        for _bi, (_b_line, _b_idx, _b_end, _b_tint) in enumerate(_dt_blocks):
            if not show_root_backgrounds and _b_lvls and _b_lvls[_bi] == 0:
                continue  # the embed paints non-symbol backgrounds itself
            sy = origin_y + _b_line * line_px
            ey = origin_y + (_b_end + 1) * line_px
            if ey < rect_min_y or sy > rect_max_y:
                continue
            sx = origin_x + _colx(_b_idx)
            # Wrap to the block's content: right edge at its widest line
            # plus a character of air - the block's TRUE extent, never
            # clamped to the view edge (a clamp put the rounded corners at
            # the clip instead of the content; the draw-list clip cuts an
            # overflowing wash with a sharp edge, as it should). Never
            # narrower than a stub when the span is blank/stale mid-scroll.
            _e0, _e1 = min(_b_line, len(_ll)), min(_b_end + 1, len(_ll))
            _bx1 = origin_x + (max(_ll[_e0:_e1] or (0,)) + 1) * char_w
            _bx1 = max(_bx1, sx + 2 * char_w)
            _b_rgb = _bg_adjust(tuple(_b_tint[:3]), _bg_f)
            _b_col = pack_color(_b_rgb[0], _b_rgb[1], _b_rgb[2], _dt_block_a)
            if _dt_block_sh:
                # The peel: top corners sit AT the enclosing scope's surface
                # (base = nesting level × block offset - flat, no shadow at
                # the top edge), bottom corners one step above it, depth
                # easing down the block.
                _base = _dt_block_sh * _b_lvls[_bi]
                _peel = _base + _dt_block_sh
                add_shadow((sx, sy, _bx1 - sx, ey - sy),
                           offset=(_base, _base, _peel, _peel),
                           corner_radius=4.0, clip=_sh_clip,
                           draw_state=ds)
            draw_list.add_rect_filled(sx, sy, _bx1, ey, _b_col, 4.0)
            if _dt_outline_a > 0:
                _b_ol = _ol_rgb(_b_rgb)
                draw_list.add_rect(sx, sy, _bx1, ey,
                                   pack_color(
                                       _b_ol[0], _b_ol[1], _b_ol[2],
                                       _dt_outline_a), 4.0,
                                   thickness=_dt_outline_t)
        # Line tint (the subtlest layer, over blocks, under the symbol
        # washes): one plain wash fitting the line's TEXT extent (indent →
        # last non-ws char), in the line's color - its explicit comment tint
        # if the definition has one, else a mix of its symbol tints.
        # Full-width (def_line_full_width) and feathered (def_line_blur +
        # def_line_blur_radius) variants are toggleable; blurred bands skip
        # the outline since a crisp outline would defeat the feather.
        _dt_line_a = Toggles.TextEditor.def_line_alpha
        if _dt_line_a > 0:
            _dt_line_full = Toggles.TextEditor.def_line_full_width
            _dt_line_blur = Toggles.TextEditor.def_line_blur
            _dt_line_blur_r = Toggles.TextEditor.def_line_blur_radius
            _dt_line_blur_a = Toggles.TextEditor.def_line_blur_alpha

            _dt_line_blur_k = Toggles.TextEditor.def_line_blur_falloff
            _dt_line_blur_n = Toggles.TextEditor.def_line_blur_samples
            _dt_line_blur_minv = Toggles.TextEditor.def_line_blur_min_value
            _dt_line_blur_maxv = Toggles.TextEditor.def_line_blur_max_value
            # GL glow path: the band goes through add_glow to the low-res
            # light buffer and onto the screen via the shadow composite as
            # a real light source (brightens neighbors, pushes back shadows)
            # - one quad instead of the draw-list feather stack below.
            _dt_line_glow = (Toggles.TextEditor.def_line_glow
                             and Toggles.glow and Toggles.filters
                             and not Toggles.draw_legacy)
            _dt_line_glow_i = Toggles.TextEditor.def_line_glow_intensity

            def _glow_rect(x0, y0, x1, y1, rgb, alpha, rounding, line):
                add_glow((x0, y0, x1 - x0, y1 - y0), rgb,
                         intensity=alpha * _dt_line_blur_a * _dt_line_glow_i,
                         radius=float(_dt_line_blur_r),
                         falloff=max(0.0, _dt_line_blur_k),
                         offset=_scope_surface(line),
                         corner_radius=rounding, clip=_sh_clip,
                         draw_state=ds)

            def _blur_rect(x0, y0, x1, y1, rgb, alpha, rounding):
                # Feathered band with an INVERSE-SQUARE profile - a hot
                # core that drops off fast, then a long faint tail, so the
                # band registers as emitted light rather than uniform fog.
                # Each expanding layer draws the DIFFERENCE in the profile
                # at its inner/outer radius, alpha, so cumulative
                # alpha at distance t from the edge is alpha * P(t), where
                # P(t) = inverse-square normalized to 1 at the edge and 0
                # at the blur radius. Cheap draw-list glow; no shader.
                alpha = min(1.0, alpha * _dt_line_blur_a)
                # Sample count from the setting, still capped by the radius
                # (more layers than pixels of radius is pure overdraw).
                steps = max(2, min(int(_dt_line_blur_n),
                                   int(_dt_line_blur_r) + 2))
                k = max(0.0, _dt_line_blur_k)
                floor = 1.0 / (1.0 + k) ** 2
                prev = 1.0
                for _i in range(steps):
                    t = (_i + 1) / steps
                    cur = ((1.0 / (1.0 + k * t) ** 2) - floor) / (1.0 - floor) \
                        if k > 0 else 1.0 - t
                    _c = pack_color(rgb[0], rgb[1], rgb[2],
                                                  alpha * (prev - cur))
                    prev = cur
                    e = _dt_line_blur_r * t
                    draw_list.add_rect_filled(x0 - e, y0 - e, x1 + e, y1 + e,
                                              _c, rounding + e)
            for _l_line, _l_rgb, _l_sc, _l_s, _l_e in _dt_lines:
                sy = origin_y + _l_line * line_px
                ey = sy + line_px
                if ey < rect_min_y or sy > rect_max_y:
                    continue
                _la = _bg_adjust(tuple(_l_rgb[:3]), _line_f)
                # The blurred band's own brightness clamp, on top of the
                # line_tint_* adjustment above - the feathered glow reads
                # differently from the hard rect at the same value.
                _lb = _brightness_clamp(_la[0], _la[1], _la[2],
                                        _dt_line_blur_minv, _dt_line_blur_maxv) \
                    if (_dt_line_blur and _dt_line_blur_r > 0) else _la
                _l_col = pack_color(_la[0], _la[1], _la[2],
                                                  _dt_line_a * _l_sc)
                if _dt_line_full:
                    if _dt_line_blur and _dt_line_blur_r > 0:
                        if _dt_line_glow:
                            _glow_rect(rect_min_x, sy, rect_max_x, ey,
                                       _lb, _dt_line_a * _l_sc, 0.0, _l_line)
                        else:
                            _blur_rect(rect_min_x, sy, rect_max_x, ey,
                                       _lb, _dt_line_a * _l_sc, 0.0)
                    else:
                        draw_list.add_rect_filled(rect_min_x, sy, rect_max_x,
                                                  ey, _l_col, 0.0)
                else:
                    sx = origin_x + _colx(_l_s)
                    ex = origin_x + _colx(_l_e)
                    if _dt_line_blur and _dt_line_blur_r > 0:
                        if _dt_line_glow:
                            _glow_rect(sx - 3, sy, ex + 3, ey,
                                       _lb, _dt_line_a * _l_sc, 3.0, _l_line)
                        else:
                            _blur_rect(sx - 3, sy, ex + 3, ey,
                                       _lb, _dt_line_a * _l_sc, 3.0)
                    elif _dt_outline_a > 0:
                        draw_list.add_rect_filled(sx - 3, sy, ex + 3, ey,
                                                  _l_col, 3.0)
                        _l_ol = _ol_rgb(_la)
                        draw_list.add_rect(sx - 3, sy, ex + 3, ey,
                                           pack_color(
                                               _l_ol[0], _l_ol[1], _l_ol[2],
                                               _dt_outline_a * _l_sc), 3.0,
                                           thickness=_dt_outline_t)
                    else:
                        draw_list.add_rect_filled(sx - 3, sy, ex + 3, ey,
                                                  _l_col, 3.0)
        _dt_sym_a = Toggles.TextEditor.def_symbol_alpha
        # GLow path for the TOKEN chips: each symbol-occurrence wash rect
        # gets its own light emitter (same pipeline as the line bands above),
        # so the light reads as reflecting off the individual token background's
        # surface rather than the whole line's.
        _dt_sym_glow = (Toggles.TextEditor.def_symbol_glow
                        and Toggles.glow and Toggles.filters
                        and not Toggles.draw_legacy)
        _dt_sym_glow_i = Toggles.TextEditor.def_symbol_glow_intensity
        _dt_sym_glow_r = Toggles.TextEditor.def_symbol_glow_radius

        for _s_start, _s_end, _s_tint, _s_scale in _dt_spans:
            _s_line, _ = _index_to_line_col(text, _s_start)
            sy = origin_y + _s_line * line_px
            ey = sy + line_px
            if ey < rect_min_y or sy > rect_max_y:
                continue
            sx = origin_x + _colx(_s_start)
            ex = origin_x + _colx(_s_end)
            # _s_scale < 1 indicates a PROPAGATED tint (reference flow) - same
            # color family, fainter wash per hop from the tinted definition.
            _sa = _bg_adjust(tuple(_s_tint[:3]), _sym_f)
            _s_col = pack_color(_sa[0], _sa[1], _sa[2],
                                              _dt_sym_a * _s_scale)
            if _dt_sym_sh:
                # Ride the scope surface: the wash's lift is the peeling
                # block surface at its line plus the symbol offset, so a
                # chip deep in a nested method casts off THAT wash, not
                # the entire background's flat depth.
                add_shadow((sx - 1, sy + 1, ex - sx + 2, ey - sy - 2),
                           offset=_scope_surface(_s_line) + _dt_sym_sh,
                           corner_radius=3.0, clip=_sh_clip,
                           draw_state=ds)
            if _dt_sym_glow:
                add_glow((sx - 1, sy + 1, ex - sx + 2, ey - sy - 2), _sa,
                         intensity=_dt_sym_a * _s_scale * _dt_sym_glow_i,
                         radius=float(_dt_sym_glow_r),
                         falloff=max(
                             0.0, Toggles.TextEditor.def_line_blur_falloff),
                         offset=_scope_surface(_s_line) + _dt_sym_sh,
                         corner_radius=3.0, clip=_sh_clip, draw_state=ds)
                
            draw_list.add_rect_filled(sx - 1, sy + 1, ex + 1, ey - 1, _s_col, 3.0)
            if _dt_sym_ol_a > 0:
                _s_ol = _ol_rgb(_sa, _dt_sym_ol_b)
                draw_list.add_rect(sx - 1, sy + 1, ex + 1, ey - 1,
                                   pack_color(
                                       _s_ol[0], _s_ol[1], _s_ol[2],
                                       _dt_sym_ol_a * _s_scale), 3.0,
                                   thickness=_dt_sym_ol_t)
        _pf("w:lines+spans")
        # Back to the body's text channel for everything after the washes.
        if Melty.channels_split:
            draw_list.channels_set_current(Core.melty.get_channel() + 1)
    _pf("w:channel")

    # Scope guides: a thin vertical line down the head column of every
    # indented block (IntelliJ-style), from the top of the block to
    # the block's last line. Colour is PROGRESSIVE: a tinted def/class
    # block's guide wears its definition tint, a guide with no tint of its
    # own wears its nearest enclosing tinted block's, and outside any
    # tinted block it wears the file's tint, else the neutral
    # Tint.scope_guide. All guide colours go through _bg_adjust
    # with the scope_guide_* hsv knobs. Segments are scanned once per
    # DISPLAY text identity (collapsed folds hide their body, so no guide
    # spans a fold seam); the per-frame work is the lineing only.
    if (Toggles.TextEditor.scope_guides and syntax_highlight
            and not single_line and not is_search_box and line_px):
        _sg_memo = getattr(ds, '_scope_guide_memo', None)
        if _sg_memo is None or _sg_memo[0] is not text:
            # The ROOT scope is one big guide at column 0 spanning the
            # whole file - head -1 so it starts on line 0, and it REPLACES
            # the column-0 block guides (a root-level def's own guide would
            # only retrace it).
            segments = (_sg_memo[1] if _sg_memo is not None
                        and _same_guide_shape(_sg_memo[0], text) else
                        [(-1, text.count('\n'), 0)] +
                        [segment for segment in _scope_guide_segments(text) if segment[2] > 0])
            _sg_memo = ds._scope_guide_memo = (text, segments)
        _sg_segments = _sg_memo[1]
        if _sg_segments:
            _sg_factors = (Toggles.TextEditor.scope_guide_saturation,
                           Toggles.TextEditor.scope_guide_value,
                           Toggles.TextEditor.scope_guide_min_value,
                           Toggles.TextEditor.scope_guide_max_value)
            _sg_alpha = Toggles.TextEditor.scope_guide_alpha
            _sg_thick = Toggles.TextEditor.scope_guide_thickness
            # Colour per segment, PROGRESSIVE and by the washes' own rule:
            # a segment wears the tint of its INNERMOST definition block
            # (_dt_blocks - the very rects painted above, decorator-run
            # start to body end) whose extent contains its head line; no
            # containing block, base (the file/neutral base). Resolved
            # once per (segments, definition tints) identity pair - one
            # merged sweep over the two sorted lists - never per frame.
            _sg_cmemo = getattr(ds, '_scope_guide_color_memo', None)
            _sg_blocks = _dt_blocks if _dt_on else ()
            if (_sg_cmemo is None or _sg_cmemo[0] is not _sg_segments
                    or _sg_cmemo[1] is not _sg_blocks):
                _sg_tints = _scope_guide_tints(_sg_segments, _sg_blocks)
                _sg_cmemo = ds._scope_guide_color_memo = (
                    _sg_segments, _sg_blocks, _sg_tints)
            _sg_tinted = _sg_cmemo[2]
            _sg_path = (getattr(jump_to, 'path', None) if jump_to is not None
                        else getattr(ds, '_file_meta', None))
            _sg_file_rgb = (_uj_file_tint(_sg_path) if _sg_path is not None
                            else None)
            _sg_base = tuple((_sg_file_rgb or Tint.scope_guide())[:3])
            _sg_base_col = None
            # The guide the caret sits ON - its line inside the block's span
            # AND its column equals the guide's column (this display focused) -
            # draws brighter: the scope_guide_active_* knobs replace alpha
            # and value for it. Merely being inside the block is not enough
            # (Lukas 08-28): the caret has to touch the line.
            _sg_active = None
            if (Melty.text_focused_ds is ds and ds.text_cursor_pos is not None):
                _sg_cpos = min(ds.text_cursor_pos, len(text))
                _sg_cline = bisect.bisect_right(_line_starts(text), _sg_cpos) - 1
                _sg_ccol = _sg_cpos - (text.rfind('\n', 0, _sg_cpos) + 1)
                for _sg_seg in _sg_segments:
                    if _sg_seg[0] > _sg_cline:
                        break
                    if (_sg_seg[0] < _sg_cline <= _sg_seg[1]
                            and _sg_seg[2] == _sg_ccol
                            and _sg_seg[0] >= 0):        # never the root guide
                        _sg_active = _sg_seg
                        break
            _sg_active_factors = (_sg_factors[0],
                                  Toggles.TextEditor.scope_guide_active_value,
                                  _sg_factors[2],
                                  Toggles.TextEditor.scope_guide_active_max_value)
            _sg_active_alpha = Toggles.TextEditor.scope_guide_active_alpha
            _sg_v0 = int((rect_min_y - origin_y) / line_px) - 1
            _sg_v1 = int((rect_max_y - origin_y) / line_px) + 1
            # Segments sorted by head line; the ones on screen are those whose
            # span meets the visible band.
            for _sg_head, _sg_end, _sg_colc in _sg_segments:
                if _sg_end < _sg_v0:
                    continue
                if _sg_head > _sg_v1:
                    break
                # Three pixels left of the column so the line never touches
                # the glyphs in it (+0.5 centres the 1 px stroke).
                _sg_x = origin_x + _sg_colc * char_w - 3.0 + 0.5
                # Scrolled under the gutter → hidden (the root line at
                # column 0 sits LEFT of the text inset, so minus that much).
                if _sg_x < rect_min_x - 3.0:
                    continue
                _sg_y0 = max(origin_y + (_sg_head + 1) * line_px, rect_min_y)
                # Bottom end pulled up 2 px so the line stops short of the
                # next row's glyphs.
                _sg_y1 = min(origin_y + (_sg_end + 1) * line_px - 2.0, rect_max_y)
                if _sg_y1 <= _sg_y0:
                    continue
                _sg_tint = _sg_tinted.get((_sg_head, _sg_end, _sg_colc))
                if (_sg_head, _sg_end, _sg_colc) == _sg_active:
                    _sg_rgb = _bg_adjust(
                        _sg_tint if _sg_tint is not None else _sg_base,
                        _sg_active_factors)
                    _sg_col = pack_color(
                        _sg_rgb[0], _sg_rgb[1], _sg_rgb[2], _sg_active_alpha)
                elif _sg_tint is not None:
                    _sg_rgb = _bg_adjust(_sg_tint, _sg_factors)
                    _sg_col = pack_color(
                        _sg_rgb[0], _sg_rgb[1], _sg_rgb[2], _sg_alpha)
                else:
                    if _sg_base_col is None:
                        _sg_rgb = _bg_adjust(_sg_base, _sg_factors)
                        _sg_base_col = pack_color(
                            _sg_rgb[0], _sg_rgb[1], _sg_rgb[2], _sg_alpha)
                    _sg_col = _sg_base_col
                if _sg_x < rect_min_x:
                    # The root line sits left of the body clip pushed at
                    # the top of the draw pass: pop it, push one left for
                    # the offset (intersected with the OUTER clip, so the
                    # tile bounds still hold), stroke, restore. Once per
                    # frame - only the root guide gets here.
                    draw_list.pop_clip_rect()
                    draw_list.push_clip_rect(rect_min_x - 3.0, rect_min_y,
                                             rect_max_x, rect_max_y, True)
                    draw_list.add_line(_sg_x, _sg_y0, _sg_x, _sg_y1,
                                       _sg_col, _sg_thick)
                    draw_list.pop_clip_rect()
                    draw_list.push_clip_rect(rect_min_x, rect_min_y,
                                             rect_max_x, rect_max_y, True)
                else:
                    draw_list.add_line(_sg_x, _sg_y0, _sg_x, _sg_y1,
                                       _sg_col, _sg_thick)

    _pf("body:washes")
    # Selection
    if _has_selection(ds):
        sel_color = (*Tint.text_selection()[:3], 0.4)
        lo, hi = _sel_range(ds)
        # Only the lines the selection touches AND the visible band - the
        # buffer-wide range+enumerate this replaced cost ~1.5ms a second on a
        # 12k-line file for every selection drag (memoized line starts →
        # two bisects, then a range over the visible rows).
        _starts = _line_starts(text)
        _n_lines = len(_starts)
        _first = max(bisect.bisect_right(_starts, lo) - 1,
                     int((rect_min_y - origin_y) // line_px) - 1, 0)
        _last = min(bisect.bisect_right(_starts, hi) - 1,
                    int((rect_max_y - origin_y) // line_px) + 1, _n_lines - 1)
        sel_u32 = pack_color(*sel_color)
        for line_idx in range(_first, _last + 1):
            line_abs_start = _starts[line_idx]
            line_abs_end = (_starts[line_idx + 1] - 1
                            if line_idx + 1 < _n_lines else len(text))
            sy = origin_y + line_idx * line_px
            if (line_abs_end >= lo and line_abs_start <= hi
                    and sy + line_px >= rect_min_y and sy <= rect_max_y):
                sel_start_in_line = max(0, lo - line_abs_start)
                sel_end_in_line = min(line_abs_end - line_abs_start,
                                      hi - line_abs_start)
                sx = origin_x + _colx(line_abs_start + sel_start_in_line, line_start=line_abs_start)
                ex = origin_x + _colx(line_abs_start + sel_end_in_line, line_start=line_abs_start)
                if hi > line_abs_end and line_abs_end >= lo:
                    # selection runs past the newline → extend one cell past EOL
                    ex = origin_x + _colx(line_abs_end, line_start=line_abs_start) + char_w
                draw_list.add_rect_filled(sx, sy, ex, sy + line_px, sel_u32)

    _pf("body:selection")
    # Token-occurrence highlight: when the caret rests on an identifier that
    # appears more than once, wash a subtle background behind every place that
    # exact token shows up - INCLUDING the one under the caret. A dumb,
    # identifier-bounded character match (see _word_match_ranges) - no CST /
    # symbol-usage index involved - so it works in any text, even mid-edit or
    # unparseable. A unique identifier (its own occurrence and no other) lights
    # nothing up. Drawn under the usage washes / search glow / glyphs.
    if (is_focused and not is_search_box and highlight_token_matches
            and Toggles.TextEditor.highlight_token_matches):
        _tok = _word_under_cursor(text, ds.text_cursor_pos)
        # Only IDENTIFIERS light up: keywords (`if`, `None`, `del`), number
        # literals and a caret inside a string / comment wash nothing, and a
        # token that sits inside a string or comment is dropped - the lexer
        # state comes from the same incremental line_open the tokenizer keeps,
        # so each check scans only its own line (see _line_lex_at).
        if _tok is not None and _is_highlightable_word(_tok[2]):
            _lo_offs, _lo_open = _ac_lex_state(ds, text)
            if _pos_in_string_or_comment(text, _tok[0], _lo_offs, _lo_open):
                _tok = None
        else:
            _tok = None
        if _tok is not None:
            _t_start, _t_end, _t_word = _tok
            matches = getattr(text_editor_state, '_token_matches', None)
            if matches is None or matches[0] is not text or matches[1] != _t_word:
                _ranges = [r for r in _word_match_ranges(text, _t_word)
                           if not _pos_in_string_or_comment(text, r[0], _lo_offs, _lo_open)]
                if text_editor_state is not None:
                    text_editor_state._token_matches = (text, _t_word, _ranges)
            else:
                _ranges = matches[2]
            # Only when the token recurs (its own occurrence plus at least one
            # other) - so the caret's own occurrence is washed too.
            if len(_ranges) > 1:
                _tm_color = pack_color(*Toggles.TextEditor.token_match_tint)
                for _ms, _me in _ranges:
                    _m_line, _ = _index_to_line_col(text, _ms)
                    sy = origin_y + _m_line * line_px
                    ey = sy + line_px
                  
                    if ey < rect_min_y - 10.0 or sy > rect_max_y + 10.0:
                        continue
                        
                    sx = origin_x + _colx(_ms)
                    ex = origin_x + _colx(_me)
                    
                    # Just the click to highlight is disabled with a flag
                    if highlight_token_matches:
                        draw_list.add_rect_filled(sx - 1, sy + 1, ex + 1, ey - 1, _tm_color, 3.0)

    _pf("body:tok_match")
    # Symbol-usage heat, PER LINE: instead of washing each symbol occurrence
    # inline, the jump-target counts (_usage_target_count - the same list
    # _try_usage_jump would show) of every usage span on a line are SUMMED and
    # the total boxes that line's gutter number and the usage heat color (the
    # blue→orange ramp - more references on the line, hotter number). The
    # per-symbol breakdown deliberately collapses to a per-line summary; Ctrl+B
    # on a symbol still resolves per-span. Counts are gathered here (spans are
    # buffer-indexed) and drawn in the gutter pass below.
    _u_vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
    _t_us = time.perf_counter()
    _uspans = _view_usage_spans(_u_vpath)
    _pf_info['us_call_ms'] = round((time.perf_counter() - _t_us) * 1000.0, 1)
    _pf_info['us_n'] = len(_uspans)
    _usage_line_heat = {}
    if _uspans and Toggles.TextEditor.usage_heat_gutter:
        _u_vspan = (_usage_off + 1, _usage_off + _fold_full.count('\n') + 1)
        # Visible band only: _uspans is sorted by start index
        # (_collect_usage_spans sorts), so bisect the on-screen character
        # range instead of walking every span in the file - the old loop
        # paid a _index_to_line_col per span before its own cull.
        _uls = _line_starts(text)
        _ul0 = max(0, min(len(_uls) - 1,
                          int((rect_min_y - origin_y) // line_px)))
        _ul1 = max(0, min(len(_uls) - 1,
                          int((rect_max_y - origin_y) // line_px) + 2))
        _ui0 = bisect.bisect_left(_uspans, (_uls[_ul0],))
        _ui1 = bisect.bisect_right(_uspans, (_uls[_ul1],))
        # Heat memo: the summed counts only change when the span SET or or
        # visible band moves - an idle repaint re-walked a few hundred spans
        # (bisect + memo-dict hits, but still 3-5ms of Python loop) for the
        # identical dict every frame. Memo on the span tuple's identity + band
        # indices; keeping original ref in the memo guards id reuse. A keystroke
        # replaces the tuple (no remap), so edits still recompute.
        _uh = getattr(ds, "_uh_memo", None)
        if (_uh is not None and _uh[1] is _uspans
                and _uh[0] == (id(_uspans), _ui0, _ui1)):
            _usage_line_heat = _uh[2]
        else:
            for _us, _ue, _su, _at_def in _uspans[_ui0:_ui1]:
                u_line, _ = _index_to_line_col(text, _us)
                sy = origin_y + u_line * line_px
                if sy + line_px < rect_min_y or sy > rect_max_y:
                    continue
                n = _usage_target_count(ds, _su, _at_def, _u_vpath, _u_vspan)
                if n:
                    _usage_line_heat[u_line] = _usage_line_heat.get(u_line, 0) + n
            ds._uh_memo = ((id(_uspans), _ui0, _ui1), _uspans, _usage_line_heat)
            _pf_info['uh_n'] = _ui1 - _ui0

    _pf("body:usage_heat")
    # Search match highlights (drawn under the text so glyphs stay readable).
    # The current match radiates a circular gradient glow with its rect cut out
    # so the matched text stays visible; the rest get a thin border. Look is
    # tunable via Toggles.SearchSettings (see search_glow.draw_search_highlight).
    if _sm_disp:
        for m_idx, _sm_m in enumerate(_sm_disp):
            if _sm_m is None:
                continue     # hidden inside a collapsed fold
            ms, me = _sm_m
            m_line, _ = _index_to_line_col(text, ms)
            # A match may span lines (multi-line search terms): collect one
            # rect per covered line, like the selection wash above. Segments
            # that run through a newline extend one cell past EOL to read as
            # continuing onto the next line. The whole match draws as one
            # glow around the segments' bounding box (per-line glows overlap
            # into a blob) with each segment outlined separately.
            segs = []
            seg_start = ms
            seg_line = m_line
            while True:
                nl = text.find('\n', seg_start, me)
                seg_end = me if nl == -1 else nl
                sy = origin_y + seg_line * line_px
                sx = origin_x + _colx(seg_start)
                ex = origin_x + _colx(seg_end)
                if nl != -1:
                    ex += char_w
                segs.append((sx, sy, ex, sy + line_px))
                if nl == -1:
                    break
                seg_start = nl + 1
                seg_line += 1
            if segs[-1][3] >= rect_min_y and segs[0][1] <= rect_max_y:
                draw_search_highlight_multi(draw_list, segs,
                                            current=(m_idx == current_local))

    _pf("body:search_hl")



    # Parse/semantic messages: each marker is a red button in the GUTTER over
    # its line number (see the gutter pass - `_err_by_line`), clamped to the
    # view's top / bottom with an arrow when the line is off screen. Only the
    # CLICKED marker (`ds._err_open_line`) washes its line red - under the
    # glyphs, with its message box beside it - so errors stay out of the way
    # while scrolling.
    _err_open_ln = getattr(ds, '_err_open_line', None)
    if _err_markers and _err_open_ln is not None:
        # [tint=(0.95, 0.25, 0.25)]
        error_line_wash = (0.824, 0.157, 0.157, 0.431)
        if any(err_line - 1 == _err_open_ln for err_line, _msg in _err_markers):
            ey0 = origin_y + _err_open_ln * line_px
            ey1 = ey0 + line_px
            if not (ey1 < rect_min_y or ey0 > rect_max_y):
                error_line = _diagnostic_line_text(text, _err_open_ln)
                draw_list.add_rect_filled(origin_x - 4, ey0, origin_x + min(visible_width, max(char_w, len(error_line) * char_w)), ey1,
                                          pack_color(*error_line_wash))
    # Import quick-fix affordance: every symbol an import would bind wears a
    # translucent yellow underline, and the floating Alt+Enter hint appears at
    # the end of the line only when the mouse is over one of those underlined
    # symbols. Deliberately independent of the error markers (the suggestions
    # travel on their own lines, and the parser error may sit on a DIFFERENT
    # line than the half-typed `json.`). Alt+Enter still stays caret-driven -
    # a caret anywhere on the line applies the fix / opens the chooser (see
    # the keyboard block / the _qf draw_dd_menu below); hovering only reveals
    # the hint. No hover invalidation needed here: the wrapper auto-repaints
    # a bounding boxable tile every frame and once on the leave edge.
    if _qf_fixes:
        _po_hover_ln = None
        _ul_col = pack_color(0.92, 0.80, 0.18, 0.95)
        for _ul_ln in _qf_fixes:
            _ul_y = origin_y + (_ul_ln - 1) * line_px
            if _ul_y + line_px < rect_min_y or _ul_y > rect_max_y:
                continue
            _ul_names = _qf_names.get(_ul_ln)
            if not _ul_names:
                continue
            _ul_ls = 0
            for _ in range(_ul_ln - 1):
                _nl = text.find('\n', _ul_ls)
                if _nl == -1:
                    break
                _ul_ls = _nl + 1
            _ul_le = text.find('\n', _ul_ls)
            _ul_line_text = text[_ul_ls:] if _ul_le == -1 else text[_ul_ls:_ul_le]
            for _um in re.finditer(r'[A-Za-z_][A-Za-z0-9_]*', _ul_line_text):
                if _um.group(0) not in _ul_names:
                    continue
                # Attribute accesses (`foo.json`) never miss an import - only
                # base identifiers do (mirrors the suggestion scan's rule).
                if _ul_line_text[:_um.start()].rstrip().endswith('.'):
                    continue
                _ux0, _uy = _char_pos_to_xy(text, _ul_ls + _um.start(),
                                            origin_x, origin_y, line_px, vcols=vcols)
                _ux1, _ = _char_pos_to_xy(text, _ul_ls + _um.end(),
                                          origin_x, origin_y, line_px, vcols=vcols)
                draw_list.add_line(_ux0, _uy + line_px - 2, _ux1, _uy + line_px - 2,
                                   _ul_col, 2.0)
                if (_ux0 <= io.mouse_pos.x < _ux1
                        and _uy <= io.mouse_pos.y < _uy + line_px):
                    _po_hover_ln = _ul_ln
        if _po_hover_ln is not None and not getattr(ds, '_qf_open', False):
            _po_ln = _po_hover_ln
            _po_opts = _qf_fixes[_po_ln]
            _po_ls = 0
            for _ in range(_po_ln - 1):
                _po_ls = text.find('\n', _po_ls) + 1
            _po_le = text.find('\n', _po_ls)
            _po_line_text = text[_po_ls:] if _po_le == -1 else text[_po_ls:_po_le]
            _po_label = (f"Alt+Enter  {_po_opts[0]}" if len(_po_opts) == 1
                         else f"Alt+Enter  {len(_po_opts)} imports…")
            _po_w, _po_h = imgui.calc_text_size(_po_label)
            _po_x = origin_x + imgui.calc_text_size(_po_line_text).x + 28
            _po_y = origin_y + (_po_ln - 1) * line_px
            if rect_min_y <= _po_y <= rect_max_y:
                draw_list.add_rect_filled(
                    _po_x - 8, _po_y - 2, _po_x + _po_w + 8, _po_y + _po_h + 4,
                    pack_color(0.13, 0.16, 0.24, 0.96), 5.0)
                draw_list.add_rect(
                    _po_x - 8, _po_y - 2, _po_x + _po_w + 8, _po_y + _po_h + 4,
                    pack_color(0.45, 0.60, 0.90, 0.55), 5.0)
                draw_list.add_text(_po_x, _po_y,
                                   pack_color(0.72, 0.82, 1.0, 1.0),
                                   _po_label)

    # Side-by-side diff anchors: enough for an OUTSIDE ribbon pass (the code
    # editor's compare tile) to map this view's buffer lines onto live screen
    # coords without this body running. Both stored RELATIVE to the view's
    # pane corner - an absolute value goes stale the moment the window moves
    # while this body is a cached blit - so the overlay re-derives
    # origin_y = live abs_top + inset - scroll, origin_x = live abs_left +
    # offset. origin_x is the text start (right of the line-number gutter).
    ds._diff_top_inset = (origin_y + ds.scroll_offset[1]) - ds.abs_top
    ds._diff_line_px = line_px
    ds._diff_origin_x_off = origin_x - ds.abs_left
    ds._diff_char_w = char_w
    # Fold layout bridge: display line -> buffer line (None = identity, no
    # collapsed fold). The ribbon pass projects change buffer-line blocks
    # through this so a change hidden in a collapsed fold snaps its
    # swoosh to the collapse (header) line instead of a linear-extrapolated
    # position inside it.
    ds._diff_d2b = _fold_d2b
    # The text band clip (top inset, BOTTOM inset) relative to the pane
    # box - insets are height-stable, so the ribbon pass can project the
    # band onto the pane's LIVE height. Stashing the BOTTOM EDGE offset
    # froze it at the stash-time height: a freeze_resize pane mid
    # resize-drag serves its blit without re-running this body, and the
    # ribbons clamped to the pre-drag bottom until release. (The pane
    # corner itself is tracked live via ds._abs_left()/_abs_top() - see
    # _pane_pos in open_files.py.)
    ds._diff_clip_off = (rect_min_y - ds.abs_top,
                         (ds.abs_top + (ds.height or 0)) - rect_max_y)

    # Diff washes: in is_diff mode each line's leading marker (the +/- left over
    # from the unified diff, with the ---/+++/@@ headers already stripped by the
    # caller) drives a full-width background — added lines green, deleted lines
    # red, drawn under the glyphs so the code stays readable.
    if is_diff:
        add_bg = (0.157, 0.627, 0.157, 0.353)  # translucent green
        del_bg = (0.824, 0.235, 0.235, 0.353)  # translucent red
        for line_idx, line_text in enumerate(text.split('\n')):
            c = line_text[:1]
            bg = add_bg if c == '+' else del_bg if c == '-' else None
            if bg is None:
                continue
            dy0 = origin_y + line_idx * line_px
            dy1 = dy0 + line_px
            if dy1 < rect_min_y or dy0 > rect_max_y:
                continue
            draw_list.add_rect_filled(origin_x - 4, dy0, origin_x + visible_width, dy1, pack_color(*bg))
    _pf("body:err_diff")
    # Syntax-highlighted text - only the visible window is tokenized (see
    # `_window`), so this is O(visible) not O(buffer). The loop starts at the
    # window's first line and source offset; tokens above it (the merge-context
    # lookback) are processed but viewport-culled. Each token is drawn one
    # line-segment at a time with a single add_text call rather than per glyph.
    win_line, win_off, tokens, _ = _window()
    # Def-block tint per def line (display coords, like the token loop's
    # line counter) - the debug run buttons wear their function's tint.
    _fn_tint_lines = ({b[0]: b[3] for b in _dt_blocks}
                      if _dt_blocks else {})
    # Glyph tinting: glyphs under a definition-tint wash lean ever so
    # slightly toward the wash color (syntax color stays the base), so text
    # reads as part of its panel - the same treatment used app-wide. Token
    # granularity: a symbol is per identifier token, so per-token is exact.
    # Cost: one bisect + ≤4-span overlap walk per visible token; the mixed
    # packed color is memoized in _GLYPH_MIX_CACHE.
    _dt_mix = (Toggles.TextEditor.def_text_tint_mix
               if _dt_spans else 0.0)
    _dt_starts = [s[0] for s in _dt_spans] if _dt_mix > 0 else None
    # The mix TARGET gets its own sat/value factors (independent of the
    # wash's bg_tint_* pair) but a shared brightness clamp - same _bg_adjust
    # machinery, different factor tuple.
    _tx_f = (Toggles.TextEditor.text_tint_saturation,
             Toggles.TextEditor.text_tint_value,
             Toggles.TextEditor.bg_min_brightness,
             Toggles.TextEditor.bg_max_brightness)
    # Override comments carrying tint=(...) draw their TEXT in that color -
    # the comment names a definition, so it wears it (text-only, no background).
    # The tint factors join the memo key so any toggle tweaks repaint.
    _ct_starts = [c[0] for c in _dt_comments] if _dt_comments else None
    _ct_factors = (Toggles.TextEditor.comment_tint_saturation,
                   Toggles.TextEditor.comment_tint_value,
                   Toggles.TextEditor.comment_min_brightness,
                   Toggles.TextEditor.bg_max_brightness)
    # Presentation mode: every glyph on a line WITHOUT a def-tint line band
    # lerps toward black (through _mix_packed, sharing the wash-mix memo) -
    # lines carrying a line tint keep full brightness and read like the
    # highlighted content. _dt_lines line numbers are buffer-based, the same
    # space as win_line / the loop's line counter below.
    _pres_lines = None
    _pres_k = 0.0
    if Toggles.presentation_mode and not is_search_box:
        _pres_k = 1.0 - Toggles.TextEditor.presentation_text_brightness
        if _pres_k > 0.0:
            _pres_lines = {l[0] for l in _dt_lines}
    # Diff-gap PREVIEW rows: a collapsed diff piece's header sits
    # Toggles.TextEditor.diff_preview_lines_below below the change's last
    # context line (open_files._diff_gap_folds slides it down), so the
    # lines from there up to and including the header are hidden-gap
    # content kept visible as a peek; likewise a piece's hidden range
    # ends diff_preview_lines_above short of the next change's context,
    # so the lines right after it are the peek from that side. Both are
    # painted with their alpha set by diff_preview_alpha. Display line
    # set, like _pres_lines; any lines a scope fold hides are skipped
    # (exact mapping, never the covering header). O(collapsed pieces ×
    # preview) per body run - never a walk of the buffer.
    # Memoized on the fold LAYOUT's identity: ds._fold_cache is rebuilt
    # exactly when the union ranges or a collapse set change (its key
    # carries both), and _diff_rngs is the memoized gap list - so an idle
    # repaint / scroll / live-edit tick requires one tuple compare, not a
    # walk of every gap (0.55 ms a frame at 322 gaps, measured 09-01).
    # Stand-in frames replay a snapshot's faded previews (_restore_preview,
    # gutter replay below); the diff layer is off, so the gate below
    # never overwrites them.
    _preview_lines = _restore_preview
    # Height of the separator band under a collapsed diff gap's header
    # row; the gap's chevron centers on it (gutter pass + badge pass).
    # [tint=(0.36, 0.62, 0.85)]
    _diff_band_h = 1.5
    # Diff-gap styling color: the file's tint (the same FileMeta color the
    # editor tab wears) for the band under the chevrons and the "N lines"
    # labels. Toggles.TextEditor.diff_fold_tint only in a buffer with no
    # file. Resolved once per body run - two dict reads.
    _dsep_path = (getattr(jump_to, 'path', None) if jump_to is not None
                  else getattr(ds, '_file_meta', None))
    _dsep_rgb = (_uj_file_tint(_dsep_path) if _dsep_path is not None
                 else None) or Toggles.TextEditor.diff_fold_tint
    _preview_alpha = Toggles.TextEditor.diff_preview_alpha
    _preview_n = (Toggles.TextEditor.diff_preview_lines_below,
                  Toggles.TextEditor.diff_preview_lines_above)
    _diff_col_prev = getattr(ds, '_diff_fold_collapsed', None)
    if (_diff_rng_set and _diff_col_prev and max(_preview_n) > 0
            and _preview_alpha < 1.0):
        _pv_layout = ds.__dict__.get('_fold_cache')
        _pv_layout = _pv_layout[2] if _pv_layout is not None else None
        _pv_memo = ds.__dict__.get('_diff_preview_memo')
        if (_pv_memo is not None and _pv_memo[0] is _diff_rngs
                and _pv_memo[1] is _pv_layout and _pv_memo[2] == _preview_n):
            _preview_lines = _pv_memo[3]
        else:
            _preview_lines = set()
            for _prng in _diff_rngs:
                if _prng not in _diff_col_prev:
                    continue
                # BELOW the change above the gap: the rows ending at the
                # header; ABOVE the change below it: the rows right after
                # the hidden range (_n_lines from the diff-range setup
                # above - set whenever _diff_rng_set is). Buffer lines,
                # mapped to display lines.
                for _pv_lo, _pv_hi in (
                        (max(_prng[0] - _preview_n[0] + 1, 0), _prng[0] + 1),
                        (_prng[1] + 1, min(_prng[1] + _preview_n[1] + 1, _n_lines))):
                    for _pb in range(_pv_lo, _pv_hi):
                        if _fold_d2b is None:
                            _preview_lines.add(_pb)
                        else:
                            _pi = bisect.bisect_right(_fold_d2b, _pb) - 1
                            if _pi >= 0 and _fold_d2b[_pi] == _pb:
                                _preview_lines.add(_pi)
            if not _preview_lines:
                _preview_lines = None
            ds._diff_preview_memo = (_diff_rngs, _pv_layout, _preview_n,
                                     _preview_lines)
    ds._diff_preview_lines = _preview_lines     # display coords; tests / overlays
    _pf("body:preview_rows")

    x = origin_x
    y = origin_y + win_line * line_px   # window's first line (lookback above the clip)
    _cur_ln = win_line                  # line number of the glyph being drawn
    src_i = win_off    # ABSOLUTE source index at the start of the current token
    _tv_idx = 0        # Nth inline view drawn this frame - its STABLE name. Render
                       # order stays stable frame-to-frame (so each view keeps its
                       # state), unlike source/line position which shifts on edits.
    _tv_edit = None    # (src_index, src_len, new_value) from an inline view that changed
    _tv_click = None   # (src_index, src_len, right_half) - press landed on a whole-token widget
    # Screen rects of PLAIN owns inline widgets (no render_func, so no event
    # subscription latching drags away from the editor). The press/drag
    # handlers ABOVE read last body-run's list - reset here if the body ran -
    # and skip caret/selection for gestures starting inside one, so a value
    # drag doesn't grow a text selection. Scroll/edit invalidations re-run the
    # body, so the rects track the screen pixels the user actually sees.
    ds._plain_tv_rects = []
    # Gutter widgets recorded by this body run (display line -> (spec, token,
    # name, kwargs)), consumed by the gutter pass below. Frame-local.
    _gutter_views = {}
    # Auto-exec edit watch for defs whose widget is scrolled out of view:
    # one identity test per render (see _fnrun_auto_exec_scan).
    _fnrun_auto_exec_scan(ds, text_editor_state, text,
                          code_dict if code_dict is not None else code_tree)
    # Glyph pass. ~900 tokens a frame: the plain single-line token is the
    # common case and gets the fast path below: no segment loop, and
    # consecutive same-colour ASCII tokens on a line merge to ONE add_text
    # (monospace: imgui's advance per glyph == char_w, so a merged run lands
    # every glyph exactly where separate draws did - the harness in the
    # perf notes asserts calc_text_size(run) == len(run) * char_w). Runs are
    # flushed before any other draw path so screen order is unchanged.
    _inline_by_key = ({k: (v, v.get("char_width") is not None)
                       for k, v in token_views.items() if isinstance(k, str)}
                      if token_views else {})
    _run_parts = None       # pending merged run: [tokens], start x/y, color, end x
    _run_x = _run_y = _run_end = 0.0
    _run_col = 0
    # Per-token packed colours memoized in the draw_state: the comment-tint /
    # def-tint resolution below (a bisect + span walk + hsv mix per token)
    # depends only on the cached token window and the cached tint tables,
    # which are the same objects frame after frame during a drag / hover
    # session - so a hit replaces all of it with a packed value per token.
    # Optional tint for plain embedded text; syntax palettes stay unchanged.
    _plain_color = (pack_color(*text_tint[:3], 1.0)
                    if text_tint is not None and not syntax_highlight else None)
    _tc_key = (tokens, win_off, _dt_spans, _dt_comments, _dt_mix, _ct_factors, _tx_f, _plain_color)
    _tc_memo = getattr(ds, '_tok_color_memo', None)
    _tok_colors = None
    if (_tc_memo is not None and _tc_memo[0][0] is tokens and _tc_memo[0][1] == win_off
            and _tc_memo[0][2] is _dt_spans and _tc_memo[0][3] is _dt_comments
            and _tc_memo[0][4:] == _tc_key[4:]):
        _tok_colors = _tc_memo[1]
    _tok_colors_new = [] if _tok_colors is None else None
    # Draw runs (built with the colour memo): consecutive PLAIN tokens on a
    # line with the same resolved colour become one run - {first token
    # index: (n_tokens, text, num_chars, colour)} - so a memo hit draws a run
    # with one add_text and skips its member tokens with a counter instead
    # of pushing ~900 tokens through the per-token path every frame.
    _runs = _tc_memo[2] if (_tok_colors is not None and len(_tc_memo) > 2) else None
    _plain_rec = [] if _tok_colors_new is not None else None
    # Positional live-usage gaps (absolute source index → width): stamped by
    # _window() beside the vcols trails. Tokens are pre-split so a gap
    # always STARTS a token - one dict probe per token, all draw paths
    # (runs, widgets, segments) use the shifted x.
    _lv_gaps = ds.__dict__.get('_lv_gap_map') or None
    _at_gap = False
    _skip_n = 0
    _ti = -1
    for token, color_key in tokens:
        _ti += 1
        if _skip_n:
            _skip_n -= 1
            continue
        _at_gap = False
        if _lv_gaps is not None:
            _gp = _lv_gaps.get(src_i)
            if _gp:
                x += _gp * char_w
                _at_gap = True
        if _runs is not None:
            _run = _runs.get(_ti)
            if _run is not None:
                _rn, _rtext, _rchars, _rcol = _run
                if _rtext and y + line_px >= rect_min_y and y <= rect_max_y:
                    _seg_col = (_rcol if _pres_lines is None
                                or _cur_ln in _pres_lines
                                else _mix_packed(_rcol, (0.0, 0.0, 0.0), _pres_k))
                    if _preview_lines is not None and _cur_ln in _preview_lines:
                        _seg_col = _fade_packed(_seg_col, _preview_alpha)
                    draw_list.add_text(x, y, _seg_col, _rtext)
                x += _rchars * char_w
                src_i += _rchars
                _skip_n = _rn - 1
                continue
        if color_key == 'clipped':
            # Off-screen stretch on a visible line (see _window_tokens band):
            # never contains a newline, never draws - just advance.
            if _run_parts is not None:
                draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
                _run_parts = None
            if _tok_colors_new is not None:
                _tok_colors_new.append(0)
            x += len(token) * char_w
            src_i += len(token)
            continue
        if _tok_colors is not None:
            color = _tok_colors[_ti]
        else:
            color = _plain_color if _plain_color is not None else COLORS[color_key]
            # Inside a color-carrying override comment, the comment text AND the
            # merged color-tuple token (color3 - the token's `(r, g, b)` text)
            # wear the comment's adjusted color; other value types (numbers,
            # bools) keep their own token colors.
            if _ct_starts is not None and color_key in ('comment', 'color3'):
                _ci = bisect.bisect_right(_ct_starts, src_i) - 1
                if _ci >= 0 and src_i < _dt_comments[_ci][1]:
                    _cc = _dt_comments[_ci][2]

                    _ck = (_cc, _ct_factors)
                    _pk = _COMMENT_TINT_CACHE.get(_ck)
                    if _pk is None:
                        _cr, _cg, _cb = _comment_tint_color(_cc)
                        _pk = pack_color(_cr, _cg, _cb, 1.0)
                        if len(_COMMENT_TINT_CACHE) > 1024:
                            _COMMENT_TINT_CACHE.clear()
                        _COMMENT_TINT_CACHE[_ck] = _pk
                    color = _pk
            elif _dt_mix > 0:
                # Spans sort (start, -width): at a given start the SHORTEST comes
                # last, so bisect lands on the base symbol for the base token; the
                # short backward walk finds the chain span still covering a
                # member token past the base's end.
                _si = bisect.bisect_right(_dt_starts, src_i) - 1
                for _k in range(_si, max(-1, _si - 4), -1):
                    _sp = _dt_spans[_k]
                    if _sp[1] <= src_i:
                        continue
                    if _sp[0] <= src_i:
                        # Mix toward the tint through the TEXT factor pair
                        # (text_tint_saturation/value + shared brightness clamp).
                        color = _mix_packed(color, _bg_adjust(tuple(_sp[2][:3]), _tx_f),
                                            _dt_mix * _sp[3])
                    break
            # Inline token view: a str-keyed token_views entry with a char_width draws
            # a widget INSTEAD of this token's text, occupying char_width cells (see
            # the token-views note above). type-keyed entries are handled by the
            # gutter pass after the body.
            _tok_colors_new.append(color)
        _vi = _inline_by_key.get(color_key)
        if _vi is None:
            _view, _inline = None, False
        else:
            _view, _inline = _vi
        # Whole-token inline view: one widget for the entire token (e.g. a
        # clickable "True" word, a drag for "3.14") rather than one per char.
        # These tokens never contain '\n', so no segment loop is needed. Two
        # layouts, picked by the spec's lead_cells:
        #  - REPLACE (lead_cells absent): the widget IS the token - exactly
        #    len(token) cells, identity vcols, sits in the grid like the
        #    literal it replaces.
        #  - ACCESSORY (lead_cells=N): the editor draws the token TEXT itself,
        #    normally (same color/grid, fully editable as text), shifted right
        #    by N cells; the widget gets only the N-cell lead area to its left
        #    (e.g. a color swatch). vcols reflects the shift for caret/click.
        # Either way a changed return splices the whole token.
        if _inline and _view.get("whole_token") and token and '\n' not in token:
            if _run_parts is not None:
                draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
                _run_parts = None
            # Presentation dim for the whole-token paths below (caret-in text,
            # with lead text) - safe to overwrite as this branch continues.
            if _pres_lines is not None and _cur_ln not in _pres_lines:
                color = _mix_packed(color, (0.0, 0.0, 0.0), _pres_k)
            if _preview_lines is not None and _cur_ln in _preview_lines:
                color = _fade_packed(color, _preview_alpha)
            # GUTTER (`gutter`): the token is plain text only; the widget is
            # recorded for the gutter pass (drawn on this line in place of
            # the line number) - no lead / trail cells, no caret hiding.
            _gutter = bool(_view.get("gutter"))
            _lead = 0 if _gutter else _view.get("lead_cells", 0)
            # TRAILING (`trail_cells`): the accessory's mirror - the token
            # text draws in place, the widget gets the trail cells right
            # after it (the rest of the line shifts; vcols carries it).
            _trail = 0 if _gutter else _view.get("trail_cells", 0)
            _cells = _lead + len(token) + _trail
            _wx = x + (_lead + len(token)) * char_w if _trail else x
            # While the editor caret sits on TOP a REPLACE token, the widget
            # gets out of the way entirely: the token rides as plain text, so
            # caret, selection and typing behave like any other code, and the
            # widget returns when the caret leaves. (The widget view
            # composites ABOVE the editor tile, so a caret under it would be
            # invisible anyway.) _tv_idx is still consumed so the OTHER
            # visible widgets keep their render-order names (and state).
            _caret_in = (not _lead and not _trail and not _gutter
                         and Melty.text_focused_ds is ds
                         and src_i <= ds.text_cursor_pos <= src_i + len(token))
            if _caret_in and y + line_px >= rect_min_y and y <= rect_max_y:
                _tv_idx += 1
                draw_list.add_text(x, y, color, token)
                # Pass-through widgets (bool) rely on clicks reaching the
                # editor, so the FIRST click of a double-click places the
                # caret in the token and lands us here - the hidden widget
                # can't see the second click. Honor the double-click toggle
                # for it: flip the literal exactly as draw_bool_token would.
                if (color_key == 'bool' and token in ('True', 'False')
                        and x <= io.mouse_pos.x < x + len(token) * char_w
                        and y <= io.mouse_pos.y < y + line_px
                        and imgui.is_mouse_double_clicked(0)):
                    _tv_edit = (src_i, len(token),
                                'False' if token == 'True' else 'True', False)
            elif y + line_px >= rect_min_y and y <= rect_max_y:
                _name = f"{ds.name}_tv{_tv_idx}"
                _tv_idx += 1
                _save_cur = imgui.get_cursor_screen_pos()
                # pad_px widens a REPLACE widget's view - and so its clip rect -
                # a few px past the token cells on both sides, giving the frame
                # breathing room around the glyphs. (Expanding inside the
                # renderer doesn't work: drawing clips at the view boundary.)
                # The cells the token reserves in the grid stay exact.
                _pad = 0 if (_lead or _trail) else _view.get("pad_px", 0)
                imgui.set_cursor_screen_pos((_wx - _pad, y))
                if _trail:
                    _w = _trail * char_w
                elif _lead:
                    _w = _lead * char_w
                else:
                    _w = len(token) * char_w + 2 * _pad
                # Inside a tint-carrying override comment, bool/number
                # widgets adopt the comment's (adjusted) color for clutter
                # reduction; the same widgets in code keep their own color.
                _extra = {}
                if color_key in ('bool', 'number'):
                    _wc = None
                    if _ct_starts is not None:
                        _wci = bisect.bisect_right(_ct_starts, src_i) - 1
                        if _wci >= 0 and src_i < _dt_comments[_wci][1]:
                            _wc = _comment_tint_color(_dt_comments[_wci][2])
                    # Presentation dim: a widget in ANY `# [...]` override
                    # comment fades with its line - its comment tint when it
                    # has one, else its own default token color. The widget
                    # draws its own text via text_tint (float rgb, not the
                    # packed `color` dimmed at the branch entry), so scale
                    # here - boosted by presentation_widget_boost to visually
                    # match the comment background (the widget hsv path reads
                    # darker).
                    if (_pres_lines is not None and _cur_ln not in _pres_lines
                            and (_wc is not None
                                 or _in_comment_override(text, src_i))):
                        _w_tinted = _wc is not None
                        if _wc is None:
                            # Untinted widgets fade to the comment GREY -
                            # their native token blue reads as live code,
                            # not comment, at presentation dim.
                            _dc = COLORS['comment']
                            _wc = unpack_color(_dc)[:3]
                        _pb = min(1.0, (1.0 - _pres_k)
                                  * Toggles.TextEditor.presentation_widget_boost)
                        _wc = (_wc[0] * _pb, _wc[1] * _pb, _wc[2] * _pb)
                        # Background chip a step brighter than the text so
                        # the widget still stands like a block on a dim line.
                        _bb = Toggles.TextEditor.presentation_widget_bg_boost
                        if color_key == 'number':
                            # The number chip paints brighter than the bool's
                            # smaller one at the same intensity (depth-ramped bg +
                            # dragable fill stack) - extra dim for its bg,
                            # and lower the legibility cap to match.
                            _bb *= Toggles.TextEditor.presentation_number_bg_dim
                            _extra['max_bg_value'] = (
                                0.25 * Toggles.TextEditor.presentation_number_bg_dim)
                        _extra['tint'] = (min(1.0, _wc[0] * _bb),
                                          min(1.0, _wc[1] * _bb),
                                          min(1.0, _wc[2] * _bb))
                        # COLORED comment widgets get a transparency cut on
                        # top of the dim - saturated colors read brighter than
                        # the grey at equal value (4th component = text
                        # tint, honored by the token renderers).
                        if _w_tinted:
                            _wc = _wc + (Toggles.TextEditor.presentation_widget_alpha,)
                    if _wc is not None:
                        _extra['text_tint'] = _wc
                        if color_key == 'bool':
                            _extra.setdefault('tint', _wc)   # the bool's bg box too
                if color_key == 'number':
                    # Number chip adopts the EDITOR's tint (ds.tint) instead
                    # of its own dark constant so it blends with the
                    # surrounding view; draw_bg's bg_offset=-1 step plus the
                    # max_bg_value cap keeps it reading as a chip. setdefault:
                    # presentation mode's dimmed tint above still wins.
                    _extra.setdefault('tint', getattr(ds, 'tint', None)
                                      or _view.get("tint"))
                elif _view.get("tint") is not None:
                    _extra.setdefault('tint', _view["tint"])
                if color_key == 'def_name':
                    # Run-button context: which function this token heads -
                    # resolved by absolute file line (display line from y,
                    # projected through the fold layout to a buffer line,
                    # then _usage_off maps buffer → file, same offset the
                    # live-view overlays work with) plus the def's name -
                    # the token itself.
                    _dl = int((y - origin_y) / line_px + 0.5)
                    _bl = (_fold_d2b[_dl]
                           if _fold_d2b is not None and 0 <= _dl < len(_fold_d2b)
                           else _dl)
                    _fn_root = code_dict if code_dict is not None else code_tree
                    _fp = (getattr(_fn_root, 'file_path', None)
                           or getattr(getattr(_fn_root, 'address', None), 'path', None)
                           or getattr(jump_to, 'path', None))
                    _extra['file_path'] = str(_fp) if _fp else None
                    _extra['def_line'] = _usage_off + _bl + 1
                    _extra['def_name'] = token
                    # Params for above: hand the widget the ROOTED TREE
                    # plus coordinates - never the resolved def node. The
                    # code-host now serves Bubbling proxies whose identity
                    # doesn't survive re-access, so any per-token memo keyed
                    # on the tree misses every frame and the node lookup
                    # walked the whole parse per def token per frame. The
                    # widget resolves lazily (on click / while its panel is
                    # open), memoized against the buffer text identity.
                    _extra['code_root'] = _fn_root
                    _extra['def_buf_line'] = _bl + 1
                    _extra['tv_text'] = text
                    _extra['def_disp_line'] = _dl
                    _extra['editor_state'] = text_editor_state
                    _extra['fn_tint'] = _fn_tint_lines.get(_cur_ln)
                # Plain (wrapper-less) renderers need the editor's draw_state:
                # they have no tile of their own, so gesture liveness requires
                # invalidating the EDITOR tile (see draw_number_token_plain).
                if getattr(_view["renderer"], "_plain_tv", False):
                    _extra['editor_ds'] = ds
                    # ACCESSORY plain widgets register too: _w is just the
                    # lead area there, so the token text keeps normal clicks
                    # while a press on the swatch doesn't move the caret
                    # (the wrapped version's left_mouse_down latch did this).
                    if _view.get("owns_mouse") and not _gutter:
                        ds._plain_tv_rects.append((_wx - _pad, y, _wx - _pad + _w, y + line_px))
                if _gutter:
                    # Deferred to the gutter pass: it draws the widget on
                    # this display line (the gutter owns the cell geometry and
                    # the owns_mouse rect) - here the token is plain text.
                    _gutter_views[_cur_ln] = (_view, token, _name, _extra)
                    _res = None
                else:
                    try:
                        _res = _view["renderer"](token, width=_w, height=line_px,
                                                 name=_name, **_extra)
                    except Exception:                    _res = None
                imgui.set_cursor_screen_pos(_save_cur)
                if _lead or _trail or _gutter:
                    draw_list.add_text(x + _lead * char_w, y, color, token)
                if (isinstance(_res, tuple) and len(_res) >= 2 and _res[0]
                        and isinstance(_res[1], str) and _res[1] != token):
                    # owns_mouse REPLACE edits come from a value DRAG - the
                    # caret must not be stamped into the token by the splice
                    # (a caret in the token hides the widget, killing the
                    # drag on its first value change); see the splice below.
                    _tv_edit = (src_i, len(token), _res[1],
                                bool(_view.get("owns_mouse")) and not _lead)
                # owns_mouse TV widgets consume the meltygui mouse events, so
                # a press on them never reaches the editor's click handling -
                # read the raw mouse and place the caret at the column under
                # it, exactly like a text click (the widget's cells are
                # identity vcols, one cell per source char). Fires on RELEASE
                # without drag, not on press: a click-and-drag is a value
                # adjustment and must keep the widget alive (placing a caret
                # hides it - see _caret_in above), while a plain click hands
                # the token over to text editing. get_mouse_drag_delta stays
                # (0,0) until the drag threshold is ever exceeded, so a drag
                # that circles back to its origin still counts as a drag.
                # Pass-through widgets (bool) and ACCESSORY widgets skip this:
                # their text takes normal editor clicks, and a press on an
                # accessory (opening its popover) shouldn't move the caret.
                if (_view.get("owns_mouse") and not _lead and not _trail
                        and not _gutter
                        and x <= io.mouse_pos.x < x + _cells * char_w
                        and y <= io.mouse_pos.y < y + line_px):
                    if imgui.is_mouse_clicked(0):
                        ds._tv_press_time = time.time()
                    if imgui.is_mouse_released(0):
                        # A click must also be SHORT: press-and-hold is an
                        # (abandoned) drag, and releasing it in-place must not
                        # move the caret. pyimgui doesn't expose imgui's
                        # mouse_click_duration, so the press time on our own.
                        _dd = imgui.get_mouse_drag_delta(0)
                        if (_dd.x == 0 and _dd.y == 0
                                and time.time() - getattr(ds, '_tv_press_time', 0) < 0.33):
                            _col = int((io.mouse_pos.x - x) / char_w + 0.5)
                            _tv_click = (src_i, len(token),
                                         min(len(token), max(0, _col)))
            x += _cells * char_w
            src_i += len(token)
            continue
        if not _inline and color_key != 'icon' and '\n' not in token:
            # Plain single-line token (the common case): no segment loop.
            # A gap-starting token never records as mergeable: the memoized
            # run replay draws whole runs and skips members, so a gap
            # swallowed into it would lose its x shift (the live merge below
            # breaks this - the shifted x fails the _run_end == x
            # join test).
            if _plain_rec is not None:
                _plain_rec.append((_ti, color,
                                   token.isascii() and '\t' not in token
                                   and not _at_gap, token))
            if token and y + line_px >= rect_min_y and y <= rect_max_y:
                _seg_col = (color if _pres_lines is None
                            or _cur_ln in _pres_lines
                            else _mix_packed(color, (0.0, 0.0, 0.0), _pres_k))
                if _preview_lines is not None and _cur_ln in _preview_lines:
                    _seg_col = _fade_packed(_seg_col, _preview_alpha)
                _mergeable = token.isascii() and '\t' not in token
                if (_run_parts is not None and _mergeable and _run_y == y
                        and _run_col == _seg_col and _run_end == x):
                    _run_parts.append(token)
                    _run_end = x + len(token) * char_w
                else:
                    if _run_parts is not None:
                        draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
                        _run_parts = None
                    if _mergeable:
                        _run_parts = [token]
                        _run_x, _run_y, _run_col = x, y, _seg_col
                        _run_end = x + len(token) * char_w
                    else:
                        draw_list.add_text(x, y, _seg_col, token)
            x += len(token) * char_w
            src_i += len(token)
            continue
        if _run_parts is not None:
            draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
            _run_parts = None
        start = 0

        while True:
            nl = token.find('\n', start)
            seg = token[start:nl] if nl != -1 else token[start:]
            if seg and y + line_px >= rect_min_y and y <= rect_max_y:
                _seg_col = (color if _pres_lines is None
                            or _cur_ln in _pres_lines
                            else _mix_packed(color, (0.0, 0.0, 0.0), _pres_k))
                if _preview_lines is not None and _cur_ln in _preview_lines:
                    _seg_col = _fade_packed(_seg_col, _preview_alpha)
                if _inline:
                    # Inline view: a render_func drawn char-by-source-char, each in
                    # a char_width cell (source stays one char per glyph, matching
                    # the vcols map). Called like any widget - (input_value)→
                    # (changed, new_value) - positioned into the cell via the cursor;
                    # a changed result splices the new value into the source below.
                    _cw = _view["char_width"]
                    _ix = x
                    for _ci, _ch in enumerate(seg):
                        _src = src_i + start + _ci         # source pos (for the edit splice)
                        _name = f"{ds.name}_tv{_tv_idx}"   # render-order index (stable name)
                        _tv_idx += 1
                        _save_cur = imgui.get_cursor_screen_pos()
                        imgui.set_cursor_screen_pos((_ix, y))
                        # Plain (wrapper-less) renderers get the editor's
                        # draw_state and, with owns_mouse, a caret-suppression
                        # rect; the same plumbing the whole-token path has.
                        _extra = ({'tint': _view["tint"]}
                                  if _view.get("tint") is not None else {})
                        if getattr(_view["renderer"], "_plain_tv", False):
                            _extra['editor_ds'] = ds
                            if _view.get("owns_mouse"):
                                ds._plain_tv_rects.append(
                                    (_ix, y, _ix + _cw * char_w, y + line_px))
                        try:
                            _res = _view["renderer"](_ch, width=_cw * char_w, height=line_px, name=_name,
                                                     **_extra)
                        except Exception:
                            _res = None
                        imgui.set_cursor_screen_pos(_save_cur)
                        if (isinstance(_res, tuple) and len(_res) >= 2 and _res[0]
                                and isinstance(_res[1], str) and _res[1] != _ch):
                            _tv_edit = (_src, 1, _res[1], False)
                        _ix += _cw * char_w
                elif color_key == 'icon':
                    # Font Awesome glyphs aren't monospaced - their natural width
                    # differs from char_w. Draw each in its own standard-width cell
                    # (so surrounding code stays grid-aligned) and nudge it 1px left
                    # to sit better in the cell.
                    ix = x
                    for ch in seg:
                        draw_list.add_text(ix - 1, y, _seg_col, ch)
                        ix += char_w
                else:
                    draw_list.add_text(x, y, _seg_col, seg)
            if nl == -1:
                # Inline views: each char occupies char_width cells; else 1 cell.
                x += len(seg) * (_view["char_width"] if _inline else 1) * char_w
                break
            x = origin_x
            y += line_px
            _cur_ln += 1

            start = nl + 1
        src_i += len(token)
    if _run_parts is not None:
        draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
        _run_parts = None
    if _tok_colors_new is not None and len(_tok_colors_new) == len(tokens):
        # Fold the plain tokens into runs (see _plain above).
        _new_runs = {}
        _r_start = _r_n = _r_chars = 0
        _r_parts = []
        _r_col = None
        _r_merge = False
        _prev_ti = -2
        for _pti, _pcol, _pmerge, _ptok in _plain_rec:
            if (_r_n and _pti == _prev_ti + 1 and _pmerge and _r_merge
                    and _pcol == _r_col):
                _r_parts.append(_ptok)
                _r_n += 1
                _r_chars += len(_ptok)
            else:
                if _r_n:
                    _new_runs[_r_start] = (_r_n, ''.join(_r_parts), _r_chars, _r_col)
                _r_start, _r_n, _r_chars = _pti, 1, len(_ptok)
                _r_parts = [_ptok]
                _r_col, _r_merge = _pcol, _pmerge
            _prev_ti = _pti
        if _r_n:
            _new_runs[_r_start] = (_r_n, ''.join(_r_parts), _r_chars, _r_col)
        ds._tok_color_memo = (_tc_key, _tok_colors_new, _new_runs)

    _pf("body:glyphs")
    # An inline view (e.g. the icon dropdown) changed its value - splice the new
    # text in for the view's source char and report the edit, so the framework
    # reparses/saves exactly as if it were typed.
    # Params-panel splices (PARTIAL CODE INSERTION): the def widget's
    # new values replace each changed default's expression inside the
    # parenthesis - applied bottom-up so earlier splices never shift later
    # ones, through the same text path as token-widget edits, so they
    # save/undo like keystrokes.
    _pp_splices = ds.__dict__.pop('_fnrun_splices', None)
    if _pp_splices:
        _pp_splices = _fnrun_resolve_splices(text, _pp_splices)
    if _pp_splices:
        for _ps, _pl, _pv in sorted(_pp_splices, reverse=True):
            _ptrace("editor params-splice", name=ds.name, at=_ps,
                    old=repr(text[_ps:_ps + _pl][:24]), new=repr(_pv[:24]))
            text = text[:_ps] + _pv + text[_ps + _pl:]
            _d = len(_pv) - _pl
            if _d:
                for _attr in ('text_cursor_pos', 'text_selection_start',
                              'text_selection_end'):
                    _v = getattr(ds, _attr)
                    if _v >= _ps + _pl:
                        setattr(ds, _attr, _v + _d)
        changed = True
    if _tv_edit is not None:
        _es, _el, _ev, _keep_caret = _tv_edit
        # Timeline: every token-widget splice, with old→new content. A splice
        # with NO mouse gesture is the echo-storm signature - this line names
        # the token (and so the widget) that fired.
        _ptrace("editor token-splice", name=ds.name, at=_es,
                old=repr(text[_es:_es + _el][:24]), new=repr(_ev[:24]))
        text = text[:_es] + _ev + text[_es + _el:]
        if _keep_caret:
            # Widget widget edit: leave the caret where it is (stamping it into
            # the token would hide the cursor mid-drag - see _caret_in). Only
            # shift positions sitting at/after the splice, when the token's
            # length changed, so the selection stays on the same line.
            # Also latch "this mouse gesture edited a value" - the release
            # handler below must NOT place the caret after a value drag, and
            # short drags (1-6px: enough to edit, under imgui's drag
            # threshold) are indistinguishable from clicks by mouse motion
            # alone. Cleared on every mouse release.
            ds._tv_gesture_edited = True
            _d = len(_ev) - _el
            if _d:
                for _attr in ('text_cursor_pos', 'text_selection_start',
                              'text_selection_end'):
                    _v = getattr(ds, _attr)
                    if _v >= _es + _el:
                        setattr(ds, _attr, _v + _d)
        else:
            ds.text_cursor_pos = _es + len(_ev)
            ds.text_selection_start = ds.text_selection_end = ds.text_cursor_pos
        if not _ev:
            # The widget deleted ITSELF (e.g. the number input's buffer was
            # emptied and backspace pressed again) - hand the keyboard back to
            # the editor at the literal's position so deletion keeps feeling
            # like normal text editing.
            Melty.text_focused_ds = ds
            ds.text_cursor_blink_time = time.time()
        changed = True
    # A press on a whole-token widget also places the editor caret INSIDE the
    # literal at the clicked column - and focuses the editor - so the literal
    # feels like any other text; the widget's only extra behavior is the drag.
    # Applied after the splice so it overrides its caret-at-end default; if the
    # same press changed the value, clamp to the NEW token's length.
    if _tv_click is not None and not getattr(ds, '_tv_gesture_edited', False):
        _cs, _cl, _col = _tv_click
        if _tv_edit is not None and _tv_edit[0] == _cs:
            _cl = len(_tv_edit[2])
        Melty.text_focused_ds = ds
        ds.text_cursor_pos = _cs + min(_col, _cl)
        ds.text_selection_start = ds.text_selection_end = ds.text_cursor_pos
        ds.text_cursor_blink_time = time.time()
    # The gesture-edited latch lives for exactly one mouse gesture: every
    # release ends it (checked above too, so the release that ENDS a value
    # drag is still suppressed).
    if getattr(ds, '_tv_gesture_edited', False) and imgui.is_mouse_released(0):
        ds._tv_gesture_edited = False
    # End of a plain-widget gesture: selection suppression lifts. (Also
    # re-evaluated on every fresh press, so a missed release event can't
    # leave it latched across frames.)
    if getattr(ds, '_plain_tv_gesture', False) and imgui.is_mouse_released(0):
        ds._plain_tv_gesture = False

    # Token views keyed by code_tree node TYPE (e.g. Conditional) — overlay pass,
    # positioned by each node's span. Runs after the inline text so widgets paint
    # on top of the code they annotate. The PARSE arrives as code_tree in the
    # chain routes but as code_dict on the code-host-cache route (where
    # code_tree carries only the error dict - see draw_text_editor_code_cache),
    # so prefer code_dict when both are present; it's the node tree with spans.
    _tv_tree = code_dict if code_dict is not None else code_tree
    if token_views and _tv_tree is not None:
        # Same-frame edit delta for the overlays (see display_shift in
        # _draw_cst_token_views): built from the display edit _window
        # memoized for this frame's trail remap (`_lv_trail_splice`), or
        # computed here when that path didn't run.
        _tv_disp_shift = None
        if text is not original_input and isinstance(original_input, str):
            _tvm = getattr(ds, '_lv_trail_splice', None)
            if (_tvm is None or _tvm[0] is not original_input
                    or _tvm[1] is not text):
                _tvm = (original_input, text,
                        _display_edit_splice(original_input, text),
                        _line_offsets_cached(original_input))
                ds._lv_trail_splice = _tvm
            _tv_sp, _tv_offs_prev = _tvm[2], _tvm[3]
            if _tv_sp is not None:
                def _tv_disp_shift(ln, _sp=_tv_sp, _offs=_tv_offs_prev):
                    # 1-based display line of the frame-start text → this
                    # frame's line. Lines past the edit shift by its line
                    # delta; the edited line keeps its number when the
                    # deletion happened at its start or end (Enter at a line
                    # end is an insertion at the NEXT line's start, so
                    # that line shifts whole); a mid-line split re-anchors
                    # the line (None).
                    _p, _oe, _d, _dl, _el, _oel = _sp
                    i0 = ln - 1
                    if i0 > _oel:
                        return ln + _dl
                    if i0 < _el:
                        return ln
                    if _el == _oel and i0 < len(_offs):
                        _ls = _offs[i0]
                        _le = (_offs[i0 + 1] - 1 if i0 + 1 < len(_offs)
                               else None)
                        if _p <= _ls:
                            return ln + _dl
                        if _le is not None and _p >= _le and _oe >= _le:
                            return ln
                    return None
                _tv_disp_shift._sp = _tv_sp
        # Span cols are in the PARSE's coords (dedented on the code-host
        # route); shift the origin by the indent delta so node overlays land
        # on the glyphs, which use the buffer's file-indented chars.
        _tv_shift = _parse_col_shift(text, getattr(_tv_tree, 'source', '') or '')
        # Caret position in buffer space (1-indexed line, non-shifted col) for
        # the live_view overlays - with hover preview off, a marker whose symbol
        # the caret sits on shows its value window instead.
        # Value widgets show their window only while SELECTED: hand the
        # focused editor's non-empty selection down as (line, col) bounds
        # (1-indexed lines, shift-corrected cols); a blank editor passes None.
        # The caret (the selection's MOVING end) rides along too: of the
        # widgets inside the selection only the one nearest it - the last
        # one selected - shows its window (live_view_views.flush_selected()).
        # Caret's 0-based DISPLAY line while this editor owns text focus -
        # the live-view inline pills hide on that line so the real code is
        # editable under the caret. `text` is display space here (widgets token
        # spliced), same space as the pills' fold to lines. O(caret offset)
        # once per focused repaint.
        _tv_caret_line = None
        if Melty.text_focused_ds is ds:
            _tv_caret_line, _ = _index_to_line_col(text, ds.text_cursor_pos)
        _sel_lo = _sel_hi = _sel_caret = None
        if Melty.text_focused_ds is ds and _has_selection(ds):
            _s0, _s1 = _sel_range(ds)
            _l0, _c0 = _index_to_line_col(text, _s0)
            _l1, _c1 = _index_to_line_col(text, _s1)
            _sel_lo = (_l0 + 1, _c0 - _tv_shift)
            _sel_hi = (_l1 + 1, _c1 - _tv_shift)
            _lc, _cc = _index_to_line_col(text, ds.text_cursor_pos)
            _sel_caret = (_lc + 1, _cc - _tv_shift)
        # Folds collapsed? pass the FULL buffer to the parse→buffer diff
        # bridge (the display text has one deletion per collapsed fold, and
        # the single-region diff maps everything between the first and last
        # fold to None - live-view markers on visible lines vanished) and
        # map buffer→display exactly via the fold layout.
        _tv_fold_lm = None
        _tv_buf = text
        if _fold_bl is not None:
            _tv_buf = _fold_full

            def _tv_fold_lm(line, _d2b=_fold_d2b):
                # 1-indexed full-buffer line → 1-indexed display line;
                # None while hidden inside a collapsed region.
                b = line - 1
                i = bisect.bisect_right(_d2b, b) - 1
                if i < 0 or _d2b[i] != b:
                    return None
                return i + 1
            # The layout the closure projects through, for the snapshot
            # view's idle-pass memo (live_view_views): the closure is
            # fresh every frame, the layout list only changes with a fold
            # toggle - so the memo keys are frame and the display lines match.
            _tv_fold_lm._d2b = _fold_d2b
        _draw_cst_token_views(_tv_tree, token_views, origin_x + _tv_shift * char_w,
                              origin_y, line_px, char_w, ds,
                              line_offset=_usage_off, jump_to=jump_to,
                              buffer_text=_tv_buf, sel_lo=_sel_lo,
                              sel_hi=_sel_hi, fold_line_map=_tv_fold_lm,
                              sel_caret=_sel_caret, fold_d2b=_fold_d2b,
                              caret_line=_tv_caret_line, live_store=live_store,
                              col_shift=_tv_shift, display_shift=_tv_disp_shift)

    # Live-usage trailing gaps: drop labels from defs whose overlay didn't
    # re-stamp THIS frame (scrolled out, store cleared, live view toggled
    # off) so their reserved gaps close on the next layout. The usage pass
    # stamps (frame, span) per def - see live_view_views._draw_usage_labels.
    _tv_trails = getattr(ds, '_lv_trail_views', None)
    if _tv_trails:
        _tv_now = Melty.frame_count
        _tv_stale = [k for k, (f, _s) in _tv_trails.items() if f != _tv_now]
        for k in _tv_stale:
            del _tv_trails[k]
        if _tv_stale:
            ds._lv_trail_gen = getattr(ds, '_lv_trail_gen', 0) + 1
            ds.invalidate()
            request_render()

    _pf("body:tv_overlay")
    # --- Spell-check squiggles -------------------------------------------------
    # Red wavy lines under unknown words. Gated behind the global toggle and
    # only recomputed when the buffer text changes (cached on the draw_state), so
    # scrolling / cursor-blink repaints never re-scan. Drawn after the glyphs and
    # inside the text clip rect so the squiggles scroll with the code.
    #
    # TODO(symbol-aware): this currently spell-checks every alphabetic word in the
    # buffer (find_misspellings(text)). THIS is the integration point - when the
    # libcst parsing work lands, drive the span list off the routed `code_tree`
    # instead: only check tokens belonging to comment / string / docstring /
    # identifier symbols, splitting identifiers on camelCase / snake_case. Do NOT
    # reuse this view's syntax `tokenize()` for that - the libcst symbol tree is
    # the source of truth. Replace the find_misspellings(text) call below with a
    # tree-driven list of (start, end, word) spans; the rendering stays the same.
    if Toggles.TextEditor.enable_spell_check:
        if getattr(ds, '_spell_cache_text', None) != text:
            import meltygui.editor.spell_check as spell_check
            ds._spell_cache_text = text
            ds._spell_errors = spell_check.find_misspellings(text)
        spell_color = 0xFF0000FF  # red (ABGR)
        period = 4.0   # px per complete zig-zag
        amp = 1.6      # px above/below the baseline
        for ws, we, _word in ds._spell_errors:
            e_line, _ = _index_to_line_col(text, ws)
            sx = origin_x + _colx(ws)
            ex = origin_x + _colx(we)
            base_y = origin_y + e_line * line_px + line_px - 2.0
            if base_y < rect_min_y or base_y > rect_max_y:
                continue
            # Triangle-wave squiggle from short segments (see the add_line
            # idiom is here; no reliance on add_polyline).
            px, py = sx, base_y
            up = True
            cx = sx
            while cx < ex:
                nx = min(cx + period / 2.0, ex)
                ny = base_y - amp if up else base_y + amp
                draw_list.add_line(px, py, nx, ny, spell_color, 1.0)
                px, py = nx, ny
                cx = nx
                up = not up


    # Cursor. Drawn at the caret even while a selection exists, so the active
    # (moving) edge of a drag or shift-selection shows where delete and arrow
    # keys will act from - text_cursor_pos already tracks that location.
    blink_cursor = False
    if is_focused:
        if not blink_cursor or (time.time() - ds.text_cursor_blink_time) % 1.0 < 0.5:
            cx, cy = _char_pos_to_xy(text, ds.text_cursor_pos, origin_x, origin_y, line_px, vcols=vcols)
            current_line_rect = (int(origin_x), int(cy + 1), int(origin_x + visible_width), int(cy + line_px + 1))
            line_highlight_color = pack_color(*Tint.cursor_line_tint()[:3], Toggles.TextEditor.cursor_line_alpha)
            draw_list.channels_set_current(Core.melty.get_channel() - 1)  # draw under the text
            draw_list.add_rect_filled(*current_line_rect, line_highlight_color)
            draw_list.channels_set_current(Core.melty.get_channel() + 1)  # draw under the text

            imgui_color = pack_color(*Tint.cursor_tint()[:3], 1.0)
            draw_list.add_line(cx, cy, cx, cy + line_px, imgui_color, 2.0)
            # Highlight selection

    # Function call parameter hint, floated over the code (within the body clip so
    # it never rides up onto the header). Only for the focused editor.
    if Melty.text_focused_ds is ds:
        _draw_signature_hint(ds, draw_state, text, origin_x, origin_y, line_px, vcols=vcols)

    draw_list.pop_clip_rect()
    _pf("squiggles+hint")
    # --- Line-number gutter ---
    # Drawn after the text body in its own clip column (left to → gutter_w) so
    # the numbers stay fixed while code scrolls horizontally under them. Numbers
    # ride origin_y, so they scroll vertically in lockstep with their lines. The
    # cursor's line is brightened for emphasis.
    # Fold headers swap the line number for the fold chevron (both states) -
    # lookup: display line -> (range, collapsed?). Badge rects reset HERE,
    # before subsequent passes that append to them (gutter chevrons below, collapsed
    # "N lines" labels in the folding pass above the body).
    ds._fold_badge_rects = []
    _fh_c = getattr(ds, '_fold_hdr_cache', None)
    if _fold_folds and _fh_c is not None and _fh_c[0] is _fold_folds:
        _fold_hdr = _fh_c[1]     # same fold layout as last frame
    else:
        _fold_hdr = _fold_header_map(_fold_folds) if _fold_folds else {}
        ds._fold_hdr_cache = (_fold_folds, _fold_hdr)
    if _restore_hdr:
        # Stand-in frames: chevrons replayed throughout the gutter (the fold
        # layer sits restore frames below, so _fold_folds is empty). Range is
        # None - draw-only, no badge rect, nothing to toggle.
        _fold_hdr = _restore_hdr
    if show_gutter and gutter_w > 0:
        gutter_bg = (*Tint.line_number_bg()[:3], 1.0)  # dark tinted gray
        num_color = pack_color(*Tint.line_number_tint()[:3], 1.0)
        cur_color = pack_color(*Tint.cursor_tint()[:3], 1.0)
        cur_line = _index_to_line_col(text, ds.text_cursor_pos)[0] if is_focused else -1
        # Clamp the column's top to the text body (origin_y) so the fill doesn't
        # ride up over the header bar above it; rect_min_y still works once the
        # body has scrolled up past the clip top.
        gutter_top = max(rect_min_y, origin_y)
        _gut_sh = Toggles.TextEditor.gutter_shadow_offset
        _uh_sh = Toggles.TextEditor.usage_heat_shadow_offset
        _uh_sh_max = Toggles.TextEditor.usage_heat_shadow_max
        _gut_clip = (left, gutter_top, left + gutter_w, rect_max_y)
        if _gut_sh:
            # Recessed strip (negative shadow): the code surface casts into
            # the gutter along its edge. The rect IS the exact strip (the
            # shadow sits outside _sh_clip's text-body bounds), so no clip.
            add_shadow((left, gutter_top, gutter_w, rect_max_y - gutter_top),
                       offset=_gut_sh, corner_radius=0.0, clip=False,
                       draw_state=ds)
        # gutter_indent: the fold chevrons move out of the number strip
        # into the indent band to its right (the 4-column inset through
        # column 0), right-aligned there — so the numbers keep their column
        # and the arrow sits where the root-level indent guide would start.
        # The clip widens to cover the band; the fill stays the strip.
        _chev_in_indent = bool(gutter_indent)
        _gut_clip_r = left + gutter_w + (gutter_margin if _chev_in_indent else 0.0)
        draw_list.push_clip_rect(left, gutter_top, _gut_clip_r, rect_max_y, True)
        draw_list.add_rect_filled(left, gutter_top, left + gutter_w, rect_max_y, pack_color(*gutter_bg))
        # Line-tint lookup for the heat wash below: a line with a definition
        # tint draws its number with THAT color instead of the usage heat ramp.
        _dt_line_map = {l[0]: l for l in _dt_lines} if _dt_lines else {}
        # Live-marker open/close buttons (see _lv_btn_w above): raw draw-list
        # widgets - definitely NOT meltygui buttons; a render_func per line
        # would dominate the gutter pass. Click toggles every marker on the
        # line via set_marker_open and invalidates this tile so their marker
        # bodies re-run and create/hide their value windows.
        _lv_marks = (getattr(ds, "_lv_gutter_markers", None) or {}) if _lv_btn_w else {}
        total_lines = len(_line_starts(text))
        # Visible band only - the old range(total_lines) walked every line of
        # the file per frame and culled inside the loop; the number/heat-box
        # work only ever applies to on-screen lines, so calculate the band once
        # and iterate just those.
        _gl0 = max(0, int((gutter_top - origin_y) // line_px))
        _gl1 = min(total_lines, int((rect_max_y - origin_y) // line_px) + 2)

        # Error buttons: one per marker line, red flat_button with the
        # warning glyph over the number. Off-screen markers still show —
        # clamped to the strip's top / bottom row with an arrow toward the
        # line (see _draw_error_button). A click toggles the message box
        # (`ds._err_open_line`); the box also closes when its marker goes.
        # [tint=(0.95, 0.25, 0.25)]
        error_button_color = (0.85, 0.12, 0.14)
        # Bg brightness knobs: flat_button's theme mix + clamp mute a bg to
        # 0.25 brightness by default - lifted here so the chip reads RED.
        error_button_value = 0.45
        error_button_max_brightness = 0.6
        # [tint=(1.0, 0.75, 0.72)]
        error_icon_color = (1.0, 0.80, 0.78, 1.0)
        error_icon = "\uf071"
        error_up_icon = "\uf077"
        error_down_icon = "\uf078"
        _err_by_line = {}
        for _el, _em in _err_markers:
            _err_by_line.setdefault(_el - 1, _em)
        if (getattr(ds, '_err_open_line', None) is not None
                and ds._err_open_line not in _err_by_line):
            ds._err_open_line = None

        def _draw_error_button(line_idx, ly, x1, arrow=None):
            """The marker's gutter button at row `ly` — its own line, or the
            clamped top / bottom row with `arrow` = up / down glyph, where
            the arrow is its OWN button (left) that scrolls the error line
            into view (centered), next to the error button proper."""
            from meltygui.view.header_view import flat_button
            _eb_x0 = left + _lv_btn_w + 2.0
            _eb_h = max(6.0, line_px - 4.0)
            _eb_save = imgui.get_cursor_screen_pos()
            if arrow is not None:
                _ar_w = max(10.0, (x1 - _eb_x0) * 0.4)
                imgui.set_cursor_screen_pos((_eb_x0, ly + (line_px - _eb_h) * 0.5))
                ds._plain_tv_rects.append((_eb_x0, ly, _eb_x0 + _ar_w, ly + line_px))
                if flat_button(f"{arrow}##{ds.name}errgo{line_idx}", ds,
                               f"errgo::{line_idx}", width=_ar_w, height=_eb_h,
                               color=error_button_color, corner_radius=4.0,
                               shadow=True, text_pad=1,
                               tint_value=error_button_value,
                               max_bg_brightness=error_button_max_brightness,
                               text_color=error_icon_color):
                    # Center the error line (the Ctrl+B goto's math: an
                    # offset in tile coords, capped at the max scroll).
                    _target = (line_idx * line_px
                               - max(0.0, (ds.height or 0) - line_px) * 0.5)
                    _mx = getattr(ds, '_max_scroll_y', None)
                    if _mx is not None:
                        _target = min(_target, _mx)
                    ds.scroll_offset = (ds.scroll_offset[0], max(0.0, _target))
                    ds.invalidate()
                    request_render()
                _eb_x0 += _ar_w + 2.0
            _eb_w = max(12.0, x1 - _eb_x0)
            _eb_label = error_icon
            imgui.set_cursor_screen_pos((_eb_x0, ly + (line_px - _eb_h) * 0.5))
            ds._plain_tv_rects.append((_eb_x0, ly, _eb_x0 + _eb_w, ly + line_px))
            _eb_hit = flat_button(f"{_eb_label}##{ds.name}err{line_idx}", ds,
                           f"err::{line_idx}", width=_eb_w, height=_eb_h,
                           color=error_button_color, corner_radius=4.0,
                           shadow=True, text_pad=2, factor=0.1,
                           tint_value=error_button_value,
                           max_bg_brightness=error_button_max_brightness,
                           text_color=error_icon_color)
            imgui.set_cursor_screen_pos(_eb_save)
            if _eb_hit:
                ds._err_open_line = (None if getattr(ds, '_err_open_line', None) == line_idx
                                     else line_idx)
                ds.invalidate()
                request_render()
                
        def _draw_gutter_widget(line_idx, ly, x1):
            """Draw the line's gutter widget in the number cell, from the
            live-marker button column to `x1`: an ERROR button when the line
            carries a parse/compile marker, else a `gutter: True` token view
            (the def run buttons). True when drawn; False (draw the number
            instead) when the line has none or the cell is too narrow."""
            if line_idx in _err_by_line:
                _draw_error_button(line_idx, ly, x1)
                return True
            _gv = _gutter_views.get(line_idx)
            if _gv is None:
                return False
            _gv_spec, _gv_tok, _gv_name, _gv_extra = _gv
            _gv_x0 = left + _lv_btn_w + 2.0
            if x1 - _gv_x0 < 12.0:
                return False
            _gv_save = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((_gv_x0, ly))
            if _gv_spec.get("owns_mouse"):
                ds._plain_tv_rects.append((_gv_x0, ly, x1, ly + line_px))
            try:
                _gv_spec["renderer"](_gv_tok, width=x1 - _gv_x0,
                                     height=line_px, name=_gv_name,
                                     **_gv_extra)
            except Exception:
                pass
            imgui.set_cursor_screen_pos(_gv_save)
            return True

        # Per-line number string + x memo, keyed on the numbers table's
        # identity, the offset and the gutter geometry (~60 visible lines a
        # frame built `str(num)` and a float expression each; the visible band
        # only changes on scroll, so this fills lazily and then hits).
        _gn_key = (line_numbers, line_offset, char_w, left + gutter_w)
        _gn_memo = getattr(ds, '_gutter_num_memo', None)
        if (_gn_memo is None or _gn_memo[0][0] is not line_numbers
                or _gn_memo[0][1:] != _gn_key[1:]):
            _gn_memo = (_gn_key, {})
            ds._gutter_num_memo = _gn_memo
        _gn = _gn_memo[1]
        _gn_base = left + gutter_w - 6.0
        for line_idx in range(_gl0, _gl1):
            ly = origin_y + line_idx * line_px
            if ly + line_px < gutter_top or ly > rect_max_y:
                continue
            _gne = _gn.get(line_idx)
            if _gne is None:
                if line_numbers is not None:
                    # Trailing empty line (the text ends in \n) has no number; so do
                    # any line whose number was explicitly None.
                    num = line_numbers[line_idx] if line_idx < len(line_numbers) else None
                    if num is None:
                        _gn[line_idx] = False
                        continue
                    num_str = str(num)
                else:
                    num_str = str(line_offset + line_idx + 1)
                _gne = _gn[line_idx] = (num_str, _gn_base - len(num_str) * char_w)
            elif _gne is False:
                continue
            num_str, nx = _gne
            # Usage heat box (see the aggregation pass above): a rounded wash
            # around the number, summed over every usage token on the line -
            # colored by the line's definition tint when it has one, so the
            # gutter mark matches the line's light.
            heat = _usage_line_heat.get(line_idx)
            if heat:
                _lt = _dt_line_map.get(line_idx)
                if _lt is not None:
                    _ga = _bg_adjust(tuple(_lt[1][:3]), _bg_f)
                    _hb = pack_color(_ga[0], _ga[1], _ga[2],
                                                   0.55 * _lt[2])
                else:
                    _hb = _usage_wash_color(heat)
                _hx0, _hx1 = nx - 3.0, left + gutter_w - 3.0
                _hy0, _hy1 = ly + 1, ly + line_px - 1
                if line_idx in _fold_hdr and not _chev_in_indent:
                    # Fold header: no number for the box, and the fold
                    # arrow shares the cell - shrink to a half-size strip
                    # (right-aligned, vertically centered) so the arrow gets
                    # breathing room to its left.
                    _hx0 = (_hx0 + _hx1) * 0.5
                    _hy0 = ly + line_px * 0.25
                    _hy1 = ly + line_px * 0.75
                if _uh_sh:
                    # Heat-scaled lift: the box's depth is the usage count
                    # times the per-usage offset, magnitude-capped so a
                    # hub line doesn't cast across the whole strip.
                    _uh_off = _uh_sh * heat
                    _uh_off = max(-_uh_sh_max, min(_uh_sh_max, _uh_off))
                    add_shadow((_hx0, _hy0, _hx1 - _hx0, _hy1 - _hy0),
                               offset=_uh_off, corner_radius=3.0,
                               clip=_gut_clip, draw_state=ds)
                draw_list.add_rect_filled(_hx0, _hy0, _hx1, _hy1, _hb, 3.0)
            _fh = _fold_hdr.get(line_idx)
            if (_fh is not None and _hide_expanded_diff and not _fh[1]
                    and _fh[0] in _diff_rng_set):
                _fh = None      # expanded diff span after expand-all: no chevron
            if _fh is not None:
                # Fold header line: chevron in place of the number; the
                # toggle handler at the top of the body reads these on
                # next frame.
                _rng_g, _col_g = _fh
                if _chev_in_indent:
                    # Indent band: arrow right-aligned in the band, number
                    # keeps its place in the strip; the hit zone is the band.
                    _band_l = left + gutter_w
                    _gcx = _gut_clip_r - 4.0 - char_w * 0.5
                    _gr = (_band_l, ly, _gut_clip_r, ly + line_px)
                    if not _draw_gutter_widget(line_idx, ly, left + gutter_w - 3.0):
                        draw_list.add_text(nx, ly, cur_color if line_idx == cur_line
                                           else num_color, num_str)
                elif heat:
                    # Heat chip exists (right half of the cell): arrow sits
                    # left of it with breathing room, and the hit region stops
                    # at the chip so clicking it still opens the usage box
                    # instead of toggling the fold.
                    _chip_l = ((nx - 3.0) + (left + gutter_w - 3.0)) * 0.5
                    _gcx = max(_chip_l - 8.0, left + _lv_btn_w + 5.0)
                    _gr = (left + _lv_btn_w, ly, _chip_l - 2.0, ly + line_px)
                else:
                    # No chip: right-align the arrow with the line numbers;
                    # a gutter widget takes the cell left of the arrow.
                    _gcx = left + gutter_w - 6.0 - char_w
                    _gr = (left + _lv_btn_w, ly, left + gutter_w, ly + line_px)
                    if _draw_gutter_widget(line_idx, ly, _gcx - 8.0):
                        _gr = (_gcx - 6.0, ly, left + gutter_w, ly + line_px)
                _ghov = (_gr[0] <= io.mouse_pos.x < _gr[2]
                         and _gr[1] <= io.mouse_pos.y < _gr[3])
                # Diff folds wear their own tint so the two fold kinds read
                # apart in the strip (scope folds stay the neutral grey).
                # A snapshot-replayed row (range=None) is a diff header when
                # the stand-in's diff rows say so.
                _is_diff_g = (_rng_g in _diff_rng_set if _rng_g is not None
                              else (_restore_diff is not None
                                    and line_idx in _restore_diff))
                if _is_diff_g:
                    _dft = Toggles.TextEditor.diff_fold_tint
                    _gcc = pack_color(
                        *_dsep_rgb[:3], min(1.0, _dft[3] + (0.35 if _ghov else 0.0)))
                else:
                    _gcc = pack_color(
                        0.9, 0.9, 0.9, 0.55 if _ghov else 0.31)
                _gcy = ly + line_px * 0.5
                if _col_g and _is_diff_g:
                    # Collapsed diff gap: the chevron sits ON the separator
                    # band under the header row (the badge pass below it at
                    # the row's bottom edge), not at the row's middle.
                    _gcy = ly + line_px - _diff_band_h * 0.5
                if _col_g:
                    # right-pointing chevron: click to expand
                    draw_list.add_triangle_filled(_gcx - 2.5, _gcy - 4.0,
                                                  _gcx - 2.5, _gcy + 4.0,
                                                  _gcx + 3.5, _gcy, _gcc)
                else:
                    # down-pointing chevron: click to collapse
                    draw_list.add_triangle_filled(_gcx - 4.0, _gcy - 2.5,
                                                  _gcx + 4.0, _gcy - 2.5,
                                                  _gcx, _gcy + 3.5, _gcc)
                if _rng_g is not None:   # None = no replay, paint-only
                    ds._fold_badge_rects.append((_gr, _rng_g))
            elif ((line_idx in _err_by_line or line_idx in _gutter_views)
                  and _draw_gutter_widget(line_idx, ly, left + gutter_w - 3.0)):
                pass    # a gutter widget took the number cell
            else:
                # Plain line: the number (a gutter widget takes its cell -
                # def lines are fold headers, so they mostly land above).
                # A diff-preview row fades its number with its glyphs.
                _num_col = cur_color if line_idx == cur_line else num_color
                if _preview_lines is not None and line_idx in _preview_lines:
                    _num_col = _fade_packed(_num_col, _preview_alpha)
                draw_list.add_text(nx, ly, _num_col, num_str)
            _mlist = _lv_marks.get(line_idx)
            if _mlist:
                _open = any(getattr(m, "_lv_open", False) for m in _mlist)
                # Every marker on the line renders its value INLINE (simple
                # builtins - see draw_live_view_marker's inline=True): no
                # window to open or close, so the cell shows an inert info
                # glyph instead of the magnifier toggle.
                _inline_all = all(getattr(m, "_lv_inline", False)
                                  for m in _mlist)
                # Hit zone: the FULL button cell (whole column width x whole
                # line height), not just the glyph - the icon itself is tiny.
                _bhov = (not _inline_all
                         and left <= io.mouse_pos.x < left + _lv_btn_w
                         and ly <= io.mouse_pos.y < ly + line_px)
                # Icon tint defaults to the LINE's color: the definition tint when
                # the line has one (same adjust as the heat box), else the
                # number color this line is drawn with.
                _blt = _dt_line_map.get(line_idx)
                if _blt is not None:
                    _bga = _bg_adjust(tuple(_blt[1][:3]), _bg_f)
                    _bc = pack_color(_bga[0], _bga[1], _bga[2], 1.0)
                else:
                    _bc = cur_color if line_idx == cur_line else num_color
                _bcx = left + _lv_btn_w * 0.5
                _bcy = ly + line_px * 0.5
                if _bhov:
                    draw_list.add_rect_filled(
                        left + 1.0, ly + 1.0, left + _lv_btn_w - 1.0,
                        ly + line_px - 1.0,
                        pack_color(1.0, 1.0, 1.0, 0.10), 3.0)
                if _inline_all:
                    # info icon: circle + dot + stem - the value is already
                    # shown inline, nothing to open.
                    _br = 3.6
                    draw_list.add_circle(_bcx, _bcy, _br, _bc, 12, 1.2)
                    draw_list.add_circle_filled(_bcx, _bcy - 1.6, 0.8, _bc)
                    draw_list.add_line(_bcx, _bcy - 0.1,
                                       _bcx, _bcy + 2.0, _bc, 1.2)
                elif _open:
                    # × close button
                    _br = 3.5
                    draw_list.add_line(_bcx - _br, _bcy - _br,
                                       _bcx + _br, _bcy + _br, _bc, 1.6)
                    draw_list.add_line(_bcx - _br, _bcy + _br,
                                       _bcx + _br, _bcy - _br, _bc, 1.6)
                else:
                    # inspect icon: magnifier (circle + handle)
                    _br = 2.8
                    _bgx, _bgy = _bcx - 1.2, _bcy - 1.2
                    draw_list.add_circle(_bgx, _bgy, _br, _bc, 12, 1.4)
                    _bhx = _br * 0.707
                    draw_list.add_line(_bgx + _bhx, _bgy + _bhx,
                                       _bgx + _br + 2.6, _bgy + _br + 2.6,
                                       _bc, 1.4)
                if getattr(ds, "_lv_btn_pressed_line", None) == line_idx:
                    ds._lv_btn_pressed_line = None
                    if not _inline_all:
                        from meltygui.editor.live_view_views import set_marker_open
                        for m in _mlist:
                            set_marker_open(m, not _open)
                        ds.invalidate()
                        request_render()
                    
                
        # An unconsumed press stash dies with the pass - a press on a line
        # whose marker disappeared must not fire on a later frame's layout.
        ds._lv_btn_pressed_line = None
        # Off-screen markers: clamp their buttons to the strip's top /
        # bottom row (still on the gutter) with an arrow toward the line, so
        # an error anywhere in the file is one click away.
        if _err_by_line:
            _eb_x1 = left + gutter_w - 3.0
            _eb_up = [l for l in _err_by_line if origin_y + (l + 1) * line_px < gutter_top]
            _eb_dn = [l for l in _err_by_line if origin_y + l * line_px > rect_max_y]
            if _eb_up:
                _draw_error_button(max(_eb_up), gutter_top + 2.0, _eb_x1,
                                   arrow=error_up_icon)
            if _eb_dn:
                _draw_error_button(min(_eb_dn), rect_max_y - line_px - 2.0, _eb_x1,
                                   arrow=error_down_icon)
        draw_list.pop_clip_rect()

    # --- Fold labels ---------------------------------------------------------
    # The fold chevrons live in the GUTTER (in place of the header's line
    # number - see the gutter pass above); here a collapsed fold keeps its
    # "N lines" label at the end of the header (also a click target), and a
    # gutterless editor falls back to end-of-line chevrons so folds stay
    # reachable. Rects are stashed for NEXT frame's toggle handler at the
    # top of the body (raw draw-list widgets, same tech as the live-view
    # gutter markers - a render_func per fold would be overkill); the list
    # was created above during gutter pass.
    if _fold_folds:
        draw_list.push_clip_rect(left + gutter_w, rect_min_y,
                                 left + ds.content_width, rect_max_y, True)
        _fm_y = (line_px - imgui.get_text_line_height()) * 0.5
        _need_chev = gutter_w <= 0.0    # no gutter: chevrons fall back here
        # Collapsed diff gap separator band (color resolved beside
        # _diff_band_h at the preview-rows block).
        _dsep_col = pack_color(*_dsep_rgb[:3], 0.35)
        # A def fold header is widened by the run buttons trailing the def's
        # name (the def_name token view's trail_cells); the badge - placed
        # from the header's CHAR length - shifts with them.
        _def_tv = token_views.get('def_name') if token_views else None
        _trail_px = (_def_tv.get("trail_cells", 0) * char_w
                     if isinstance(_def_tv, dict) else 0.0)
        # Folds are in display-line order; bisect to the visible band rather
        # than the every fold in the file (2.5k in a 12k-line file).
        _fdl_c = getattr(ds, '_fold_dl_cache', None)
        if _fdl_c is None or _fdl_c[0] is not _fold_folds:
            _fdl_c = (_fold_folds, [_f[1] for _f in _fold_folds])
            ds._fold_dl_cache = _fdl_c
        _fv0 = int((rect_min_y - origin_y) // line_px) - 2
        _fv1 = int((rect_max_y - origin_y) // line_px) + 1
        for _rng, _dl, _fcol, _nh, _hlen, _fa, _fhl in _fold_folds[
                bisect.bisect_left(_fdl_c[1], _fv0):
                bisect.bisect_right(_fdl_c[1], _fv1)]:
            _fy = origin_y + _dl * line_px
            if _fy > rect_max_y or _fy + 2 * line_px < rect_min_y:
                continue
            if _hide_expanded_diff and not _fcol and _rng in _diff_rng_set:
                continue    # expanded diff span under expand-all: no badge
            _bx = origin_x + _hlen * char_w + char_w
            if _trail_px:
                # Header line text = the _hlen chars ending at the anchor.
                _hl_s = text[max(0, _fa - _hlen):_fa].lstrip()
                if _hl_s.startswith('def ') or _hl_s.startswith('async def '):
                    _bx += _trail_px

            _is_diff_fold = _rng in (getattr(ds, '_diff_rng_set', None) or ())
            if _fcol and not _need_chev and not _is_diff_fold:
                # Gutter owns the chevron and the "N lines" label is not
                # drawn (see below): no badge rect here - an INVISIBLE
                # badge after the header text meant that placing the caret
                # at the end of the line toggled the fold.
                continue
            if _fcol:
                # [tint=(0.656, 0.044, 0.615), show_tint=True]
                _lbl = f"{_nh} lines"
                _bw = (16.0 if _need_chev else 4.0) + len(_lbl) * char_w + 8.0
            elif _need_chev:
                _lbl = None
                _bw = 16.0
            else:
                continue    # expanded + no chevron: nothing on the line
            _fr = (_bx, _fy + 1.0, _bx + _bw, _fy + line_px - 1.0)
            _fhov = (_fr[0] <= io.mouse_pos.x < _fr[2]
                     and _fr[1] <= io.mouse_pos.y < _fr[3])

            if _is_diff_fold:
                _dft = Toggles.TextEditor.diff_fold_tint
                _fcc = pack_color(
                    *_dsep_rgb[:3], min(1.0, _dft[3] + (0.3 if _fhov else 0.0)))
                if _fcol:
                    # Collapsed diff gap: a thin separator line across the
                    # row under the header - hidden UNCHANGED code, visually
                    # distinct from a folded scope - in the file's tint.
                    _dby = _fy + line_px - _diff_band_h
                    draw_list.add_rect_filled(
                        left + gutter_w, _dby, left + ds.content_width,
                        _dby + _diff_band_h, _dsep_col)
            else:
                _fcc = pack_color(
                    0.9, 0.9, 0.9, 0.4 if _fhov else 0.31)
            _fcx, _fcy = _fr[0] + 8.0, (_fr[1] + _fr[3]) * 0.5
            if _is_diff_fold and _fcol:
                # The chevron sits ON the separator band (see the gutter
                # pass for the same rule).
                _fcy = _fy + line_px - _diff_band_h * 0.5
            if _need_chev:
                if _fcol:
                    # right-pointing chevron: click to expand
                    draw_list.add_triangle_filled(_fcx - 2.5, _fcy - 4.0,
                                                  _fcx - 2.5, _fcy + 4.0,
                                                  _fcx + 3.5, _fcy, _fcc)
                else:
                    # down-pointing chevron: click to collapse
                    draw_list.add_triangle_filled(_fcx - 4.0, _fcy - 2.5,
                                                  _fcx + 4.0, _fcy - 2.5,
                                                  _fcx, _fcy + 3.5, _fcc)
            # Add this if you want a display of the number of collapsed lines
            # if _fcol:
            # #     draw_list.add_text(_fr[0] + (16.0 if _need_chev else 4.
            #                        _fy + _fm_y, _fcc, _lbl)
            # Collapsed diff gaps DO display their count - "N lines" in the
            # diff tint beside the badge, part of the fold styling.
            if _is_diff_fold and _fcol:
                draw_list.add_text(_fr[0] + (16.0 if _need_chev else 4.0),
                                   _fy + _fm_y, _fcc, _lbl)
            ds._fold_badge_rects.append((_fr, _rng))
        draw_list.pop_clip_rect()
    elif _restore_diff:
        # Stand-in frames: the fold layer is off, so replay the snapshot of
        # diff-gap chrome - the separator band + "N lines" - for every
        # COLLAPSED gap header in the visible band (same geometry and order
        # as the live pass above; the gutter pass painted the chevron on
        # the band). Paint-only: no badge rects, nothing to toggle. The
        # header's length comes from the stand-in's own line (the band text
        # IS the display text), so the label lands where the live pass will.
        draw_list.push_clip_rect(left + gutter_w, rect_min_y,
                                 left + ds.content_width, rect_max_y, True)
        _fm_y = (line_px - imgui.get_text_line_height()) * 0.5
        _need_chev = gutter_w <= 0.0
        _dsep_col = pack_color(*_dsep_rgb[:3], 0.35)
        _fcc = pack_color(
            *_dsep_rgb[:3], Toggles.TextEditor.diff_fold_tint[3])
        _rd_offs = _line_starts(text)
        _fv0 = int((rect_min_y - origin_y) // line_px) - 2
        _fv1 = int((rect_max_y - origin_y) // line_px) + 1
        for _rd_line in sorted(_restore_diff):
            if _rd_line < _fv0 or _rd_line > _fv1 or _rd_line >= len(_rd_offs):
                continue
            _rd_n = _restore_diff[_rd_line]
            if not _rd_n:
                continue    # expanded gap: gutter chevron only
            _fy = origin_y + _rd_line * line_px
            _dby = _fy + line_px - _diff_band_h
            draw_list.add_rect_filled(
                left + gutter_w, _dby, left + ds.content_width,
                _dby + _diff_band_h, _dsep_col)
            _rd_end = (_rd_offs[_rd_line + 1] - 1
                       if _rd_line + 1 < len(_rd_offs) else len(text))
            _hlen = _rd_end - _rd_offs[_rd_line]
            _bx = origin_x + _hlen * char_w + char_w
            if _need_chev:
                _fcy = _fy + line_px - _diff_band_h * 0.5
                draw_list.add_triangle_filled(_bx + 5.5, _fcy - 4.0,
                                              _bx + 5.5, _fcy + 4.0,
                                              _bx + 11.5, _fcy, _fcc)
            draw_list.add_text(_bx + (16.0 if _need_chev else 4.0),
                               _fy + _fm_y, _fcc, f"{_rd_n} lines")
        draw_list.pop_clip_rect()

    if changed:
        text_height = len(_line_starts(text)) * line_px + 2
    else:
        text_height = len(_line_starts(input_value)) * line_px + 2

    # text_width = max(vcols) if vcols else max((len(l) for l in text.split('\n')), default=0) * char_w

    _pf("gutter")
    # Icon-picker orphan close: the picker popover is latched by its icon
    # picker's body (draw_icon_selector_plain) - if that widget stopped
    # rendering this frame (token scrolled out, edited away, sort-order name
    # shifted) the menu popover would keep its last closed=False stamp forever
    # and float on as a ghost. The editor owns the latch state, so close it
    # here whenever the open widget wasn't seen this frame.
    _io_name = getattr(ds, '_icon_open_name', None)
    if _io_name is not None:
        _io_seen = getattr(ds, '_icon_seen', None)
        if not (_io_seen and _io_seen[0] == _io_name
                and _io_seen[1] == Melty.frame_count):
            _io_menu = (getattr(ds, '_icon_menus', None) or {}).get(_io_name)
            if _io_menu is not None:
                _io_menu.closed = True
                if Melty.popover_focused_ds is _io_menu:
                    Melty.popover_focused_ds = None
            _io_root = getattr(ds, '_icon_dd_root', None)
            if _io_root is not None:
                from meltygui.core.dropdown_core import _dd_close
                _dd_close(_io_root)   # collapse paths / release the box's text focus
            ds._icon_open_name = None
            request_render()

    # --- Code-suggest popup (dropdown menu anchored to the caret) ---
    # Rendered after the body (and after the monospace font is popped, so its
    # rows use the normal UI font) so it floats above the code. We reuse the
    # dropdown's menu render with its own search box suppressed - the editor
    # owns text focus and the half-typed identifier IS the filter. A flat
    # name->name dict makes each leaf return the chosen identifier; a mouse click
    # bubbles back as (changed, pick) and we splice it in like the keyboard accept.
    # Mode.WINDOW menus are LATCHED - once drawn they persist until explicitly
    # closed, so we must call draw_dd_menu EVERY frame and toggle `closed=` rather
    # than gating the call (a gated call would leave the last-open frame painted).
    # Only the open state feeds real items / drives the keep-alive repaint.
    from meltygui.view.dropdown_view import draw_dd_menu
    _ac_show = (Melty.text_focused_ds is draw_state and getattr(ds, '_ac_open', False)
                and bool(getattr(ds, '_ac_candidates', None)))
    _ac_cands = ds._ac_candidates if _ac_show else []
    _ac_items = {n: n for n in _ac_cands}
    # Row colors from the definition-tint pass: a candidate whose symbol
    # carries a tint renders its row in that color, matching the editor's
    # washes. The name→int map rides the cached _def_tints result already
    # computed this frame (len guard: a differently-shaped tuple may linger on
    # a pre-hotswap draw_state); resolving here is one dict lookup per row.
    _dt = getattr(ds, '_def_tints', None)
    _nt = _dt[3] if _ac_show and _dt is not None and len(_dt) == 4 else None
    _mt = getattr(ds, '_ac_member_tints', None) if _ac_show else None
    # Snippet rows carry their own author-set tint (Snippet.tint) - it wins
    # over the symbol maps (a snippet label isn't a symbol).
    _sn = getattr(ds, '_ac_snips', None) if _ac_show else None
    _ac_tints = None
    if _nt or _mt or _sn:
        _ac_tints = {}
        for n in _ac_cands:
            # The member map (receiver's defining file) wins - it's exact for
            # the receiver, while the namespace map's dotted-name last-segment
            # lookup is only a guess for bare member names.
            t = None
            if _sn:
                _s = _sn.get(n)
                t = tuple(_s.tint[:3]) if _s is not None and getattr(_s, 'tint', None) else None
            t = t or (_mt.get(n) if _mt else None) or (_nt.get(n) if _nt else None)
            if t is not None:
                _ac_tints[n] = t
        _ac_tints = _ac_tints or None
    # Draw FIM ghost text (fim.py): the visible chunk under the caret, extra
    # lines in an anchor below, drawn while the mono font is still pushed.
    _fim_ghost = getattr(ds, '_fim_ghost', None) if is_focused else None
    if (_fim_ghost is not None and (_fim_ghost.text or _fim_ghost.pending)
            and not getattr(ds, '_ac_open', False)):
        _draw_fim_ghost(ds, _fim_ghost, text, origin_x, origin_y, line_px, vcols)

    _ac_anchor = getattr(ds, '_ac_anchor', ds.text_cursor_pos)

    _ac_x, _ac_y = _char_pos_to_xy(text, _ac_anchor, origin_x, origin_y, line_px, vcols=vcols)
    popup_x, popup_y, popup_height = _completion_popup_rect(
        _ac_x, _ac_y, line_px, draw_state.abs_clip_rect,
        Toggles.TextEditor.completion_max_height)
    if _ac_show:
        # Keyboard-vs-hover highlight. The menu paints the keyboard cursor only in
        # _kbd_mode, else the hovered row - so we keep _kbd_mode True while the
        # mouse is NOT over the popup (selection always shown, never goes
        # blank) and ONLY drop to hover if the mouse actually MOVES over it. A
        # resting pointer never drives the highlight, so arrow nav keeps working
        # even with the mouse parked over the popup.
        _mp = imgui.get_mouse_pos()
        _lm = getattr(ac_state, '_last_mouse', None)
        _pop_x0, _pop_y0 = popup_x, popup_y
        _pop_ds = Melty.cache.key_to_draw_state.get(getattr(ds, '_ac_menu_tile', None))
        if _pop_ds is not None and _pop_ds.width and _pop_ds.height:
            # REAL window rect (live abs pos - cached abs lags a frame during a
            # parent-window drag). Survives a user resize; a hardcoded estimate
            # here left a narrow band where _kbd_mode was re-forced every frame,
            # so rows outside it never hover-highlighted.
            _px0, _py0 = _pop_ds._abs_left(), _pop_ds._abs_top()
            _over = (_px0 - 4 <= _mp[0] <= _px0 + _pop_ds.width + 4
                     and _py0 - 2 <= _mp[1] <= _py0 + _pop_ds.height)
        else:
            # First-open-frame fallback before the popup's tile id is known.
            _pop_h = min(len(_ac_cands) * 24 + 10, popup_height)
            _over = (_pop_x0 - 4 <= _mp[0] <= _pop_x0 + 400
                     and _pop_y0 - 2 <= _mp[1] <= _pop_y0 + _pop_h)
        _moved = _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5)
        if not _over:
            ac_state._kbd_mode = True       # mouse away → keyboard selection shown
        elif _moved:
            ac_state._kbd_mode = False      # actively moving over it → hover drives
        # over + resting → leave as-is (so an arrow's _kbd_mode=True persists)
        ac_state._last_mouse = (_mp[0], _mp[1])

    imgui.dummy(draw_state.content_width, max(draw_state._kwargs.get("min_height", 0), text_height))

    # draw_dd_menu is a LATCHED window: called every frame with closed=not _ac_show
    # so it persists when this (cached) body is skipped. Hover/keys wake the loop;
    # background results wake it via the future's done-callback (_wake_on_future).
    # [tint=(0.867, 0.255, 0.255), show_tint=True]
    ac_changed, ac_pick, _ac_menu_ds = draw_dd_menu(
        _ac_items, name=f"{ds.name}_ac_menu", view_offset=False,
        temp=True, show_search=False, swoosh=False, closed=not _ac_show, max_height=popup_height,
        window_pos=(popup_x - draw_state.abs_left, popup_y - draw_state.abs_top), text_align="left",
        row_tags=(getattr(ds, '_ac_kinds', None) if _ac_show else None),
        row_tints=(_ac_tints or None),
        row_suffixes=(getattr(ds, '_ac_params', None) if _ac_show else None),
        parent_window=draw_state, root_state=ac_state, path_prefix=(),
        return_extras=True)


    # Latch the popup's exact tile id from the call itself (return_extras hands
    # back its draw_state on every wrapper path, including closed/deferred). The
    # old name-prefix scan of cache._tiles mis-latched ANOTHER editor's popup
    # whenever they share a prefix - every RenderHost editor is named "value" -
    # and since the wrong popup stayed live the latch never healed: that editor's
    # popup only repainted while hovered. Restamped every call, so a rebuilt
    # tile or renamed editor re-latches automatically.
    if _ac_menu_ds is not None:
        ds._ac_menu_tile = _ac_menu_ds._tile_id
    # Hover may have moved the menu's cursor (when the mouse is over it); mirror
    # that back into our selection index so Enter/arrows continue from the hovered row.
    if _ac_show and not ac_state._kbd_mode:
        _cp = ac_state.cursor_path
        if isinstance(_cp, tuple) and len(_cp) == 1 and _cp[0] in _ac_cands:
            ds._ac_index = _ac_cands.index(_cp[0])
    # The popup is a CACHED latched window with no kwargs cache key - repaints
    # happen only via explicit invalidation. Fire it ONLY on a real change edge:
    # the candidate list / kind tags (typing, jedi landing), the keyboard row,
    # or the highlight mode. Hover repaints need nothing here (a currently-
    # hovered tile re-renders every frame), so a resting pointer or held key
    # costs zero invalidates. Must be invalidate_up - it cascades to child
    # tiles (a plain invalidate leaves inner collections due to blit-skip).
    # It lands before the parent window dispatch on end_frame, so the menu
    # repaints the same frame; request_render backstops bad orderings.
    if _ac_show:
        _sig = (ds._ac_candidates, ds._ac_kinds, ds._ac_index, _ac_tints,
                bool(getattr(ac_state, '_kbd_mode', True)))
        if _sig != getattr(ds, '_ac_menu_sig', None):
            ds._ac_menu_sig = _sig
            _mt = getattr(ds, '_ac_menu_tile', None)
            if _mt is not None:
                Melty.cache.invalidate_up(_mt, force=True)
                request_render()
    else:
        ds._ac_menu_sig = None   # force one repaint on the next open
    if ac_changed and isinstance(ac_pick, str):
        anchor = ds._ac_anchor
        replace_end = _completion_replace_end(
            text, ds.text_cursor_pos, ac_pick, getattr(ds, '_ac_snips', None))
        _ins, _coff, _extra = _ac_pick_insert(
            ds, ac_pick, following=text[replace_end:replace_end + 64],
            preceding=text[max(0, anchor - 64):anchor],
            replaced=text[anchor:ds.text_cursor_pos],
            line_prefix=text[_get_line_start(text, anchor):anchor])
        text = text[:anchor] + _ins + text[replace_end:]
        ds.text_cursor_pos = anchor + _coff
        ds._ac_tabstops = [len(text) - (anchor + s) for s in _extra] or None
        text = _ac_apply_auto_import(ds, ac_pick, jump_to, text)
        ds.text_selection_start = ds.text_cursor_pos
        ds.text_selection_end = ds.text_cursor_pos
        ds._ac_open = False
        ds._ac_request_anchor = -1
        ds._ac_snip_site = None   # same disarm as a keyboard accept
        changed = True

    _pf("ac_popup")
    # --- Usage-jump picker (multi-user symbols) ---
    # Same latched-window contract as the suggestion popup above: the picker
    # window (usage_picker.draw_usage_picker, GlobalSearch's Usage tab as a
    # popover) is called every frame with closed= toggled. Rows are file →
    # scope chain → usage line; a pick - mouse (model.picked) or Enter
    # (handled in the key_event) - lands through _pick_usage_row.
    _uj_show = ((Melty.text_focused_ds is draw_state
                 or _focus_in_context_menu_over(draw_state))
                and getattr(ds, '_uj_open', False)
                and bool(uj_model.rows))
    # Edge-log the "flagged open but not shown" state - an invisible-but-open
    # picker still gates Ctrl+B off (its `not _uj_open` check), which looks
    # exactly like "the shortcut is broken".
    _uj_hidden = getattr(ds, '_uj_open', False) and not _uj_show
    if _uj_hidden != getattr(ds, '_uj_hidden_prev', False):
        ds._uj_hidden_prev = _uj_hidden
        if _uj_hidden:
            _uj_log(f"picker OPEN-BUT-HIDDEN {ds.name!r} "
                    f"focus_owner={getattr(Melty.text_focused_ds, 'name', None)!r} "
                    f"rows={len(uj_model.rows)}")
    _uj_anchor = getattr(ds, '_uj_anchor', ds.text_cursor_pos)
    _uj_gut = getattr(ds, '_uj_anchor_gutter', None)
    if _uj_gut is not None and show_gutter and gutter_w > 0:
        # Gutter-opened picker docks beside the clicked heat box: right of
        # the gutter column, top aligned to the line. Downstream the
        # window_pos adds line_px to _uj_y (the under-the-symbol
        # convention), so aim one line above.
        _uj_x = left + gutter_w + 4.0
        _uj_y = origin_y + (_uj_gut - 1) * line_px
    else:
        _uj_x, _uj_y = _char_pos_to_xy(text, _uj_anchor, origin_x, origin_y, line_px, vcols=vcols)
    from meltygui.view.code_view import draw_usage_picker
    from meltygui.editor.usage_picker import picker_fit
    # ── Popover size: fitted to the rows ONCE on open, then free ─ use the
    # dropdown's plumbing (auto_resize=False hands the window its resize
    # handle) without its every-frame stamp: on the opening frames the size
    # is the content fit (picker_fit - every row, height-stamped), the
    # width the remembered drag width (text_editor_state.usage_picker_width,
    # below) when there is one; afterwards the handle resizes freely,
    # bounded only by the content height (picker_content_height, passed as the
    # wrapper's max_height - the wrapper enforces it mid-drag: the corner
    # resize and the frame-edge solve cap at draw_state.max_height), and only
    # only the handle width is remembered. Re-stamping every frame fought
    # the drag (Lukas 09-04).
    from meltygui.editor.usage_picker import picker_content_height
    _uj_width = (getattr(text_editor_state, 'usage_picker_width', None)
                 if text_editor_state is not None else None)
    _uj_pop = Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None))
    _uj_fitting = Melty.frame_count - getattr(ds, '_uj_open_frame', -99) <= 1
    if _uj_show and _uj_pop is not None:
        if _uj_fitting:
            if _uj_width:
                _uj_pop.width = _uj_width
            # The latched window's tile may be clean from its last open -
            # force the body to run so the fit measures THESE rows.
            if _uj_pop._tile_id is not None:
                Melty.cache.invalidate_up(_uj_pop._tile_id, force=True, bypass_clip=True)
        if _uj_pop.width is None or _uj_pop.width < 5:
            _uj_pop.width = 680
    _uj_max_h = picker_content_height(_uj_pop, uj_model)
    # [tint=(0.071, 0.354, 0.511), show_tint=True]
    _uj_res = draw_usage_picker(
        uj_model, name=f"{ds.name}_uj_menu", view_offset=False, show_bg=True,
        temp=True, swoosh=False, closed=not _uj_show, bg_offset=0, max_height=_uj_max_h,
        window_pos=(_uj_x - draw_state.abs_left, _uj_y - draw_state.abs_top + line_px),
        parent_window=draw_state, tint=(0.06, 0.08277813, 0.13),
        return_extras=True)
    _uj_menu_ds = _uj_res[2] if len(_uj_res) > 2 else None
    # Exact tile id from the call above - the old name-prefix scan mis-landed
    # across same-named editors (see the AC popup note above).
    if _uj_menu_ds is not None:
        ds._uj_menu_tile = _uj_menu_ds._tile_id
    if _uj_show and _uj_menu_ds is not None:
        _cur_w = _uj_menu_ds.width
        _last_w = getattr(ds, '_uj_menu_fit_w', None)
        if _uj_fitting:
            _fit = picker_fit(_uj_menu_ds, uj_model)
            _target = (_uj_width or _fit[0], _fit[1])
            if _target != (_uj_menu_ds.width, _uj_menu_ds.height):
                _uj_menu_ds.width, _uj_menu_ds.height = _target
                request_render()
        else:
            if _last_w is not None and _cur_w and _cur_w != _last_w:
                # The handle moved it: the width is the user's from now on.
                if text_editor_state is not None:
                    text_editor_state.usage_picker_width = _cur_w
        ds._uj_menu_fit_w = _uj_menu_ds.width
    # Change-gated repaint - one invalidate per real change edge (rows swap,
    # arrow nav, highlight row flip, hover row), zero on parked-pointer
    # frames. Same design as the AC popup block above.
    if _uj_show:
        _sig = uj_model.signature()
        if _sig != getattr(ds, '_uj_menu_sig', None):
            ds._uj_menu_sig = _sig
            _mt = getattr(ds, '_uj_menu_tile', None)
            if _mt is not None:
                Melty.cache.invalidate_up(_mt, force=True)
                request_render()
    else:
        ds._uj_menu_sig = None   # force one repaint on the next open
    # Ignore a mouse pick landing on the picker's very open frame(s): the
    # window is LATCHED, so on a re-open it can draw one frame at its stale
    # previous position/contents - a click there replayed the LAST session's
    # row (seen as "gutter click instantly jumps to the previously-jumped
    # file"). Real picks always come ≥2 frames after the open.
    _uj_pick = uj_model.picked
    if _uj_pick is not None:
        uj_model.picked = None
        _uj_log(f"pick mouse {_uj_pick.kind} {getattr(_uj_pick, 'path', None)}:"
                f"{getattr(_uj_pick, 'line', None)} "
                f"open_age={Melty.frame_count - getattr(ds, '_uj_open_frame', -99)}")
        if _uj_show and Melty.frame_count - getattr(ds, '_uj_open_frame', -99) > 1:
            _pick_usage_row(_uj_pick)

    # --- Import quick-fix chooser --- same latched-window contract as the two
    # popups above: draw_dd_menu called EVERY frame with closed= toggled. Rows
    # are the candidate import statements for the caret line's missing name;
    # a pick - mouse or Enter (handled in the key block) - applies the fix.
    _qf_show = (Melty.text_focused_ds is draw_state
                and getattr(ds, '_qf_open', False)
                and bool(getattr(ds, '_qf_options', None)))
    _qf_items = {s: s for s in (ds._qf_options if _qf_show else [])}
    _qf_anchor = getattr(ds, '_qf_anchor', ds.text_cursor_pos)
    _qf_x, _qf_y = _char_pos_to_xy(text, _qf_anchor, origin_x, origin_y, line_px, vcols=vcols)
    qf_changed, qf_pick, _qf_menu_ds = draw_dd_menu(
        _qf_items, name=f"{ds.name}_qf_menu", view_offset=False,
        temp=True, show_search=False, swoosh=False, closed=not _qf_show, max_height=400,
        window_pos=(_qf_x - draw_state.abs_left, _qf_y - draw_state.abs_top + line_px),
        text_align="left", parent_window=draw_state, root_state=qf_state,
        path_prefix=(), return_extras=True)
    if _qf_menu_ds is not None:
        ds._qf_menu_tile = _qf_menu_ds._tile_id
    # Change-gated repaint - one invalidate per real change edge (options swap,
    # arrow nav); same design as the popups above.
    if _qf_show:
        _sig = (tuple(ds._qf_options), getattr(ds, '_qf_index', 0))
        if _sig != getattr(ds, '_qf_menu_sig', None):
            ds._qf_menu_sig = _sig
            _mt = getattr(ds, '_qf_menu_tile', None)
            # if _mt is not None:
                # Melty.cache.invalidate_up(_mt, force=True)
                # request_render()
    else:
        ds._qf_menu_sig = None      # force one repaint on the next open
    if qf_changed and isinstance(qf_pick, str):
        _fx_changed, _fx_text = _apply_import_fix(qf_pick, jump_to, text)
        from meltygui.code.chain_converters import _import_bound_name
        if getattr(ds, '_qf_applied', None) is None:
            ds._qf_applied = set()
        ds._qf_applied.add(_import_bound_name(qf_pick))
        if _fx_changed:
            ds.text_cursor_pos += len(_fx_text) - len(text)
            text = _fx_text
            changed = True
        ds._qf_open = False
        request_render()

    if _font_pushed:
        imgui.pop_font()

    _pf("uj_picker")
    # --- Floating error box pinned to the bottom of the view ---
    # The error popover owns its hit region, including below a one-line file.
    # Background work keeps polling after it closes; completion uses the shared
    # import statement path and the original file's selected environment.
    # Only the gutter error button opens or closes the diagnostic popup.
    _err_open_line = getattr(ds, '_err_open_line', None)
    _err_open_msg = (dict((l - 1, m) for l, m in _err_markers).get(_err_open_line)
                     if _err_markers and _err_open_line is not None else None)
    from meltygui.extensions import call
    dependency_statement = call('source_diagnostic_view', source_tools, jump_to, _err_open_msg,
                                _err_open_line, draw_state, origin_x, origin_y, line_px)
    if dependency_statement:
        import_changed, imported_text = _apply_import_fix(dependency_statement, jump_to, text)
        from meltygui.code.chain_converters import _import_bound_name
        draw_state._qf_applied.add(_import_bound_name(dependency_statement))
        if import_changed:
            draw_state.text_cursor_pos += len(imported_text) - len(text)
            text = imported_text
            changed = True
    # window_pos is an offset from the parent window's absolute origin. The menu
    # window carries an intrinsic ~one-row top offset (draw_dropdown back-compensates
    # the same way), so anchor at the caret's line top minus a line to sit it snug
    # under the insertion site instead of a line too low.

    # --- Parse-error staleness tracking
    # The error messages come from a BACKGROUND reparse, so the moment the buffer
    # changes they describe an OLD buffer - wrong line numbers (esp. after adding
    # / removing lines) or an error that's already been fixed. Mark them stale the
    # moment the editable text changes, and keep them stale until a FRESH parse
    # result arrives - detected as a new `error` / `code_tree` object pair
    # (the chain hands back the same cached object until it reparses). This holds
    # the message off for exactly the reparse gap, with no timing guess, and the
    # text-compare catches every edit including pure newline insertions.
    # Reassemble the FULL text once for everything downstream: the fast
    # syntax/import checks must never compile the fold-spliced display text
    # (a collapsed def has no body - a guaranteed false syntax error), and
    # the changed return hands the caller the full text. With no collapsed
    # fold this is `text` unchanged, so the section below operates exactly as
    # before.
    if _fold_segments:
        if changed:
            # The reassemble works on the layout's UNION collapse set; the
            # moved map replays each dnl shift onto the diff collapse, so
            # scope and diff state stay separate.
            _full_now, _union_after, _fold_dropped, _fold_moved = \
                _fold_reassemble(original_input, text, _fold_segments,
                                 _fold_union_col)
            if not _fold_dropped and not _fold_moved:
                updated = _fold_update_inline(_fold_full, _full_now, _fold_built)
                if updated is not None:
                    # The old pass just tokenized this exact display string.
                    # Carry its results into the next pass, instead of
                    # reconstructing an equal string and invalidating every
                    # lexer, width, tint and token-window cache a second time.
                    ds._fold_cache = (_full_now, fold_layout_key, (text, *updated[1:]))
            ds._fold_collapsed = {_fold_moved.get(r, r)
                                  for r in ds._fold_collapsed
                                  if r not in _fold_dropped}
            if getattr(ds, '_diff_fold_collapsed', None):
                ds._diff_fold_collapsed = {
                    _fold_moved.get(r, r) for r in ds._diff_fold_collapsed
                    if r not in _fold_dropped}
            # A seam-edited fold force-expanded: its KEY must drop too, or
            # the key->range projection next frame will re-collapse it.
            # (The dnl-shifted tuples change nothing - keys are line-only.)
            if (_fold_dropped and _fold_key_of is not None
                    and getattr(ds, '_fold_keys', None) is not None):
                ds._fold_keys -= {_fold_key_of[r] for r in _fold_dropped
                                  if r in _fold_key_of}
        else:
            _full_now = _fold_full
    else:
        _full_now = text
    _parse_pair = (error, code_tree)
    _prev_text = getattr(ds, '_err_prev_text', None)
    if _prev_text is None:
        ds._err_prev_text = _full_now             # baseline on first render
    elif _full_now != _prev_text:
        ds._err_prev_text = _full_now
        if not getattr(ds, '_err_stale', False):
            ds._err_stale = True
            ds._err_stale_pair = _parse_pair       # this parse is now outdated
        # Fast-path syntax check (consumed by the marker block up top): re-check
        # the edited buffer inline so the NEXT frame shows/clears a red marker
        # immediately instead of waiting out the background reparse debounce.
        # Only for code buffers already in the error path (syntax_highlight
        # + a resolved file - never plain-text fields), size-capped
        # (fast_check_max_chars, another toggle) because compile() is O(buffer)
        # on the render thread. _compile_check dedents and handles in-function
        # errors, so span buffers check clean.
        _fast_ok = (Toggles.TextEditor.check_syntax_errors
                    and Toggles.TextEditor.fast_syntax_check
                    and syntax_highlight and jump_to is not None
                    and not single_line)
        if _fast_ok and len(_full_now) <= Toggles.TextEditor.fast_check_max_chars:
            from meltygui.code.new_converters import _compile_check
            _fe = _compile_check(_full_now)
            ds._fast_err_state = (_full_now, _fe)
            ds._fast_err_extra = ((_full_now, _compile_check_more(_full_now, _fe))
                                  if _fe is not None else None)
            # Import-suggestion fast path (consumed by the quick-fix block up
            # top): a warm incremental scan is O(changed region) per keystroke
            # (~0.4ms). Gated on has_scan_state - a path's first scan is
            # O(buffer tokenize + module-binds parse), ~1s max, and must be
            # to the background workers; they warm the incremental fast-path state
            # and this path takes over from the next keystroke on. The current
            # background payload rides along so the top block can tell when a
            # landed relint/reparse superseded this scan.
            try:
                from meltygui.code.code_checks import collect_import_suggestions
                from meltygui.code.code_checks import has_scan_state
                _fi_path = getattr(jump_to, 'path', None)
                if has_scan_state(_fi_path):
                    _fi_scan = collect_import_suggestions(_full_now, path=_fi_path)
                    ds._fast_imports_state = (_full_now, _fi_scan or {}, import_fixes)
                else:
                    ds._fast_imports_state = None
            except Exception:
                ds._fast_imports_state = None
        elif (_fast_ok and _prev_text is not None
                and Toggles.TextEditor.check_changed_region):
            # Over-cap buffer: compile only the changed top-level span, diffed
            # against the pre-edit text (_prev_text - still the OLD buffer
            # here; ds._err_prev_text was already advanced above). The
            # heuristic in _region_compile_check means a region cut mid-
            # string/bracket reports "ambiguous", not a false error. Held-
            # error rules: a fresh failure over a previously-clean region is
            # real (show it); both-fail keeps an already-held error alive
            # (same-function still being typed, fresh line mapping); a clean
            # region clears a held error only if it LIVES IN that region -
            # one found in a DIFFERENT region stays across the edit, its
            # line shifted by the edit's delta when the edit sat above it, so
            # big-file errors never wait out the background debounce to
            # reappear. "skip" (huge paste) drops the held state - the buffer
            # changed structurally and the background parse re-flags.
            from meltygui.code.new_converters import _region_compile_check
            _r_status, _r_err, _r_span = _region_compile_check(
                _prev_text, _full_now, Toggles.TextEditor.fast_check_max_chars)
            _held = getattr(ds, '_fast_err_state', None)
            _held_err = _held[1] if _held is not None else None
            # A held error OUTSIDE the edited span survives every outcome:
            # above the edit its line is unchanged, below it shifts by the
            # edit's delta. Inside the span its fate depends on the status
            # (or stays None): clean clears it, error/ambiguous re-map it
            # to the fresh compile, skip defers it to the background parse.
            _keep, _inside = None, False
            _ln = getattr(_held_err, 'lineno', None) if _held_err else None
            if _ln is not None and _r_span is not None:
                _s, _e_old, _dlt = _r_span
                _inside = _s < _ln <= _e_old
                if not _inside:
                    if _ln > _e_old:
                        _held_err.lineno = _ln + _dlt
                    _keep = _held_err
            if _r_status == "error":
                ds._fast_err_state = (_full_now, _r_err)
            elif _r_status == "ambiguous" and _held_err is not None:
                # Same error re-type gets a fresh line mapping; a held
                # error from ANOTHER region keeps the extraction artifact.
                ds._fast_err_state = (_full_now, _r_err if _inside else _keep)
            else:
                ds._fast_err_state = (_full_now, _keep)
            # Import-suggestion fast path for over-cap buffers too: the
            # scanner's warm incremental step is O(changed region) regardless
            # of buffer size - only its full fallback (first scan, big paste,
            # tokenizer trouble) is O(buffer). incremental_only refuses
            # exactly that fallback: a None result leaves the fast state
            # unset and the debounced background channel authoritative (its
            # next scan re-warms the incremental state).
            try:
                from meltygui.code.code_checks import collect_import_suggestions
                from meltygui.code.code_checks import has_scan_state
                _fi_path = getattr(jump_to, 'path', None)
                _fi_scan = (collect_import_suggestions(_full_now, path=_fi_path,
                                                       incremental_only=True)
                            if has_scan_state(_fi_path) else None)
                ds._fast_imports_state = (None if _fi_scan is None
                                          else (_full_now, _fi_scan, import_fixes))
            except Exception:
                ds._fast_imports_state = None
        else:
            ds._fast_err_state = None
            ds._fast_imports_state = None
        if not (_fast_ok and len(_full_now) <= Toggles.TextEditor.fast_check_max_chars):
            ds._fast_err_extra = None   # over-cap / off: only the first error
    elif getattr(ds, '_err_stale', False):
        _sp = getattr(ds, '_err_stale_pair', (None, None))
        if not (error is _sp[0] and code_tree is _sp[1]):
            ds._err_stale = False                  # a fresh parse landed

    # ── Viewport snapshot (for instant-restore source) ────────────────────
    # Capture what this editor SHOWED: the visible display-line band and the
    # text, persisted by text_editor_state (@exclude'd so these per-scroll
    # writes must never invalidate). Band math mirrors _window()'s. Display
    # space on purpose: with a fold collapsed the snapshot is what was ON
    # SCREEN, and the restored stand-in reproduces the look, not the folds
    # (they re-derive when the real buffer lands). Skipped when WE are the
    # stand-in, and for one-line boxes.
    if (text_editor_state is not None and not restore_active
            and not single_line and not is_search_box and line_px):
        try:
            _clip = draw_state.abs_clip_rect
            _sn_n = len(_line_starts(text))
            _sv0 = max(0, min(int((_clip[1] + bar_height - top) / line_px) - 3,
                              _sn_n - 1))
            _sv1 = max(_sv0, min(int((_clip[3] - top) / line_px) + 3, _sn_n - 1))
            _soffs = _line_offsets_cached(text)
            _ss = _soffs[_sv0]
            _se = (_soffs[_sv1 + 1] - 1) if _sv1 + 1 < len(_soffs) else len(text)
            text_editor_state.restore_first_line = _sv0
            text_editor_state.restore_total_lines = _sn_n
            text_editor_state.restore_text = text[_ss:_se]
            # Gutter shape (digits decide the number column's X) and the fold
            # keys (line-independent collapse identities - the ONLY place
            # fold state survives a session; ds._fold_keys itself never
            # serializes). None keys = fold layer disabled for this buffer.
            text_editor_state.restore_gutter_digits = (
                int(gutter_digits) if gutter_w > 0 else 0)
            text_editor_state.restore_line_offset = int(line_offset)
            # The band's gutter EXACTLY as painted this frame: number per
            # row (line_numbers is already fold-remapped display-space),
            # -1/-2 for a fold-header chevron (expanded/collapsed), None
            # for a numberless row. The stand-in replays these so the
            # gutter doesn't change when the real buffer lands.
            if gutter_w > 0:
                _sgr = []
                for _ri in range(_sv0, _sv1 + 1):
                    _sfh = _fold_hdr.get(_ri)
                    if (_sfh is not None and _hide_expanded_diff
                            and not _sfh[1] and _sfh[0] in _diff_rng_set):
                        _sfh = None   # expand-all: the gutter painted its number
                    if _sfh is not None:
                        _sgr.append(-2 if _sfh[1] else -1)
                    elif line_numbers is not None:
                        _sn = (line_numbers[_ri]
                               if _ri < len(line_numbers) else None)
                        _sgr.append(int(_sn) if _sn is not None else None)
                    else:
                        _sgr.append(int(line_offset) + _ri + 1)
                text_editor_state.restore_gutter_rows = _sgr
            else:
                text_editor_state.restore_gutter_rows = None
            # The band's DIFF-gap headers ({band row: hidden count}, 0 =
            # expanded) and its preview-fade rows, so the stand-in wears
            # the collapsed compare split's look - separator color, "N
            # lines" labels, tinted chevrons, the fade - from frame 1
            # (the diff layer itself needs the real buffer). Bisect over the
            # fold list (display-line sorted, the badge pass's cache) and
            # one membership test per band row: O(band), never a walk.
            _sdr = {}
            _fdl_s = getattr(ds, '_fold_dl_cache', None)
            if (_diff_rng_set and _fold_folds and _fdl_s is not None
                    and _fdl_s[0] is _fold_folds):
                for _sf in _fold_folds[bisect.bisect_left(_fdl_s[1], _sv0):
                                       bisect.bisect_right(_fdl_s[1], _sv1)]:
                    if _sf[0] not in _diff_rng_set:
                        continue
                    if _hide_expanded_diff and not _sf[2]:
                        continue   # expand-all: no chevron, no badge
                    _sdr[_sf[1] - _sv0] = int(_sf[3]) if _sf[2] else 0
            text_editor_state.restore_diff_rows = _sdr
            text_editor_state.restore_preview_rows = (
                [_ri - _sv0 for _ri in range(_sv0, _sv1 + 1)
                 if _ri in _preview_lines]
                if _preview_lines else None)
            _sfk = getattr(ds, '_fold_keys', None)
            text_editor_state.restore_fold_keys = (
                None if _sfk is None else list(_sfk))
            # The diff layer's collapse history, while the layer is active
            # (turn off leaves the last capture in place - the file may
            # come back into a compare next session). Change-edge test via
            # a ds-side copy: the set is a few hundred tuples at most.
            _sdc = getattr(ds, '_diff_fold_collapsed', None)
            if _sdc is not None and getattr(ds, '_diff_persist_memo', None) != _sdc:
                ds._diff_persist_memo = set(_sdc)
                text_editor_state.restore_diff_collapsed = sorted(_sdc)
        except Exception:
            pass   # a snapshot failure cannot take down the editor
    if restore_active:
        changed = False   # stand-in edits are discarded, never propagated
        # Caret state frozen across the stand-in (see the restore branch):
        # the body's clamp against the short stand-in is undone, and prev is
        # synced so the first REAL-text frame sees no caret "move" - the
        # follow must not yank the restored scroll toward the caret.
        if _restore_caret is not None:
            (ds.text_cursor_pos, ds.text_selection_start,
             ds.text_selection_end) = _restore_caret
            ds.text_prev_cursor_pos = ds.text_cursor_pos

    _pf("errbox+tail")
    # Emit the per-section breakdown for every edited frame (typing latency is the
    # target) plus any anomalous slow frame, so idle repaints are silent.
    _pf_total_ms = (time.perf_counter() - _pf_t0) * 1000.0
    if changed or _pf_total_ms >= 8.0:
        _prev_t = _pf_t0
        _parts = []
        for _lbl, _tm in _pf_marks:
            _ms = (_tm - _prev_t) * 1000.0
            _prev_t = _tm
            if _ms >= 0.05:
                _parts.append((_lbl, _ms))
        _parts.sort(key=lambda p: -p[1])
        _bd = " ".join(f"{_l}={_m:.1f}" for _l, _m in _parts)
        if _pf_tok[1]:
            _bd += f" (tokenize_miss={_pf_tok[0] * 1000.0:.1f}x{_pf_tok[1]})"
        _ptrace("draw_text perf", name=ds.name, total_ms=round(_pf_total_ms, 1),
                cpu_ms=round((time.thread_time() - _pf_cpu0) * 1000.0, 1),
                changed=changed, lines=len(_line_starts(text)), breakdown=_bd,
                **_pf_info)

    if changed:
        # Timeline: WHAT changed. Chunked common-prefix scan: equal 4KB slices
        # skip at C speed, per-char refinement only inside the first differing
        # chunk. The old per-char zip walk was O(edit position) of work per
        # keystroke (~9ms measured at 119k chars in) - and it ran after
        # the _pf summary above, so no breakdown section ever showed it.
        _old = original_input if isinstance(original_input, str) else ""
        _m = min(len(_old), len(text))
        _di = 0
        while _di < _m:
            _step = min(4096, _m - _di)
            if _old[_di:_di + _step] == text[_di:_di + _step]:
                _di += _step
                continue
            _e = _di + _step
            while _di < _e and _old[_di] == text[_di]:
                _di += 1
            break
        _ptrace("editor CHANGED", name=ds.name, old_len=len(_old), new_len=len(text),
                diff_at=_di, old=repr(_old[_di:_di + 24]), new=repr(text[_di:_di + 24]))
        return True, _full_now
    # Unchanged: hand back the FULL buffer, never the fold-spliced display
    # text (original_input IS the display text while a fold is collapsed -
    # returning it would drop every fold line if the wrapper propagates
    # the unchanged).
    return False, (_fold_full if _fold_segments else original_input)
