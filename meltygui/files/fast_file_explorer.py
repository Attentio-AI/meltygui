"""draw_fast_file_explorer — a shortcuts column beside a flat directory
listing, immediate mode.

    changed, picked = draw_fast_file_explorer(current_dir, name="files")
    if changed:
        current_dir = picked if Path(picked).is_dir() else current_dir   # a file: open it

`input_value` is the directory shown. The view returns ``(True, path)`` the
frame the user NAVIGATES — a double-click on a folder, Ctrl+Up to the
parent, a crumb of the path strip, a shortcut, Enter on a selected row — or
double-clicks a FILE (the caller opens it however it likes); the caller
writes a directory back and passes it in next frame. Nothing of the listing
is kept beyond a scan memoized on the directory's mtime, refreshed by a
FileWatch emitter on the directory (one at a time, retired on navigation).

Two views: `draw_file_listing` is the path strip + rows (the fast_dock /
file_tree model: names straight to the window draw list, no per-row widgets
or draw_states, events as parameters, the blit cache while idle, its own
scroll); `draw_fast_file_explorer` puts it in a ColumnLayout (columns.py,
shared draggable edge, persisted) next to the shortcuts — the XDG user
directories, home and the root. Each row wears the file's OWN tint from the
shared file-meta store (`FileMeta.painted_tint`, what the studio's tabs and
trees paint) on its name and, stronger, on its icon — no row background;
the selection and hover washes use the tint through the editor tab's colour recipe —
and its icon from the meta entry, else the codec's, else the folder / file
glyph. Every row — the shortcuts too — leads with its tint control: a
painted row shows its `draw_tuple_fast` chip (click = the colour-picker
popover, the studio's tab-bar chip), an unpainted one a faint paint-brush
button (shown only on the selected row / the current shortcut) whose click
stamps `default_tint` in and opens the picker. The store is only
WRITTEN for a row the user paints (a meta entry per browsed file would
bloat ~/.melty/file_meta.pkl); clearing the colour in the picker drops the
tint again, and the entry with it when nothing else is attached.

Rows drag to reorder (the code editor's tab bar model, `DragDrop.on_drag`
per row + one `on_drop`): a landed drop stamps every row of the directory
with an `order` in the meta store — the studio's folder-tree convention
(folder_files._collect_meta), so the two agree on a folder's order — and
the listing sorts by those stamps (unstamped rows keep the natural
folders-first order after the stamped ones). A folder that is itself
painted washes the whole listing in its tint (`folder_bg_boost`), the rows
on top of it. `context_menu={label: callable}` is the wrapper's right-click
menu (the hdr-viewer's), except that the explorer's callables receive ONE
argument: the path of the row under the right-click (a right-press
selects it), or the directory when the click landed on no row.

Type to search (`type_to_search`, on by default): while nothing else owns
the keyboard — no text editor, find box, menu or popover — the listing holds
meltygui's text-focus slot (the menu bar's trick), so every keystroke reaches it
without the pointer having to hover it, and typing searches the directory
shown. The keys come from the GLFW callback queue (Melty.frame_key_events, the
editor's source: nothing is dropped on a slow frame). Each keystroke re-ranks
the rows against the query — a name prefix, then a word start, a substring,
finally the letters in order — selects the best, scrolls it into view (centred
when it was out of sight) and flashes it with Melty.emphasize; every other
match shows the matched letters highlighted and the rest of the listing dims
so the matches stand out. A pill at the bottom right shows the query and
"n of m". Up / Down (and Tab / Shift+Tab) step through the matches, Enter
opens the selected one, Backspace edits, Ctrl+Backspace clears, Ctrl+V pastes,
Esc clears the search (a second Esc the selection). A navigation clears it.
"""
import os
import re
from pathlib import Path

import meltygui.core.windowing.window_api as glfw
import meltygui_imgui as imgui

from meltygui.hdr_color import pack_color
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.code.codec_registry import extension_to_codec
from meltygui.core.cache.tile_marks import add_shadow
from meltygui.core.cache.tile_marks import clear_glows
from meltygui.core.layout.header_runtime import _brightness_clamp_fn


def row_icon(path, is_dir, entry, folder_icon, file_icon):
    """The glyph before a row's name: the file-meta icon, else the codec
    registered for the extension (no content sniff — that reads file heads,
    one per row), else the folder / file glyph."""
    icon = entry.get("icon") if isinstance(entry, dict) else None
    if icon:
        return icon
    if is_dir:
        return folder_icon
    codec = extension_to_codec.get(path.suffix.lower())
    codec_icon = getattr(codec, "icon", None) if codec is not None else None
    return codec_icon or file_icon


def row_tint_bg():
    """A memoized `(tint, boost) -> packed row background` through the
    editor tab's colour recipe (style-manager mix under
    Toggles.CodeEditor.tab_active_bg_* + the brightness clamp), so a bright
    tint still leaves the name readable. `boost` adds to the mix value AND
    the clamp ceiling: the selected row is the same tint, brighter."""
    # [tint=(0.55, 0.72, 0.95)]
    bg_theme_factor = 0.1
    style_manager = Melty.style_manager
    brightness_clamp = _brightness_clamp_fn()
    bg_memo = {}

    rgb_memo = {}

    def row_rgb(tint, boost=0.0):
        """The row background as an (r, g, b) tuple."""
        tint = tuple(tint[:3])
        memo_key = (tint, boost)
        rgb = rgb_memo.get(memo_key)
        if rgb is None:
            mixed = style_manager.make_color_rgb(
                tint[0], tint[1], tint[2],
                value=Toggles.CodeEditor.tab_active_bg_brightness + boost,
                factor=bg_theme_factor,
                saturation_scale=Toggles.CodeEditor.tab_active_bg_saturation,
                alpha=1.0)
            mixed = brightness_clamp(mixed[0], mixed[1], mixed[2], 0.0,
                                     Toggles.CodeEditor.tab_active_bg_max_brightness + boost)
            rgb = rgb_memo[memo_key] = (mixed[0], mixed[1], mixed[2])
        return rgb

    def row_bg(tint, boost=0.0):
        memo_key = (tuple(tint[:3]), boost)
        bg = bg_memo.get(memo_key)
        if bg is None:
            bg = bg_memo[memo_key] = pack_color(*row_rgb(tint, boost), 1.0)
        return bg
    row_bg.rgb = row_rgb
    return row_bg


_TEXT_TINT_MEMO = globals().get("_TEXT_TINT_MEMO", {})


def tinted_text(base, tint, mix=0.3):
    """The row's text colour: `base` (r, g, b, a) pulled `mix` of the way
    toward `tint`'s hue at the base's brightness, so the name reads in the
    row's colour without losing contrast. Memoized (packed) per pair."""
    key = (base, tuple(tint[:3]), mix)
    col = _TEXT_TINT_MEMO.get(key)
    if col is None:
        r, g, b = float(tint[0]), float(tint[1]), float(tint[2])
        # Lift the tint to the base's brightness before mixing, so a dark
        # tint colours the name rather than dimming it.
        base_lum = max(base[0], base[1], base[2])
        tint_lum = max(r, g, b, 1e-6)
        scale = base_lum / tint_lum
        r, g, b = min(1.0, r * scale), min(1.0, g * scale), min(1.0, b * scale)
        col = _TEXT_TINT_MEMO[key] = pack_color(
            base[0] + (r - base[0]) * mix, base[1] + (g - base[1]) * mix,
            base[2] + (b - base[2]) * mix, base[3])
    return col


def chip_swatch(tint, bg_rgb, mix=0.55):
    """The tint chip's painted colour: `tint` pulled `mix` of the way toward
    the row background it sits on, so the chip reads as a subtle marker
    rather than a saturated block (the picker still edits the real tint)."""
    r, g, b = float(tint[0]), float(tint[1]), float(tint[2])
    return (r + (bg_rgb[0] - r) * mix, g + (bg_rgb[1] - g) * mix, b + (bg_rgb[2] - b) * mix,
            float(tint[3]) if len(tint) == 4 else 1.0)


def tint_control(draw_state, key, tint, x, y, size, text_y, hovered, default_tint,
                 swatch=None, show_brush=True, setter=None, brush_color=None):
    """One row's tint control at (x, y): the `draw_tuple_fast` chip when
    `tint` is painted, else the paint-brush button. `key` is the store
    path; `hovered` says the pointer is on the row (the brush brightens
    under it). One view_id for chip and brush, so the brush's click can
    hand the popover to the chip that replaces it next frame;
    priority_delta=4 outranks the row's own left_mouse_down param
    (registered at 3). `swatch` is the colour the chip paints (see
    `chip_swatch`); None paints the tint itself. `show_brush` False draws
    (and registers) no brush for an unpainted row — the listing shows it
    only on the selected row. ``setter(value)`` writes the tint somewhere
    other than the file-meta store (the chat window's conversations); None
    clears. `brush_color` optionally supplies the exact icon RGB (the chat
    sidebar matches its expand arrow). Returns True when the store was written."""
    if setter is None:
        raise TypeError("tint_control requires a supplied value setter")
    write = setter
    # [tint=(0.55, 0.72, 0.95)]
    brush_icon = f"\uf1fc"
    brush_col = pack_color(*brush_color, 1.0) if brush_color is not None else pack_color(1.0, 1.0, 1.0, 0.22)
    brush_hover_col = brush_col if brush_color is not None else pack_color(1.0, 1.0, 1.0, 0.9)
    from meltygui.view.collection_view import draw_tuple_fast

    view_id = f"tint_{key}"
    if tint:
        cursor = imgui.get_cursor_screen_pos()
        changed, new_tint = draw_tuple_fast(
            tint, draw_state, view_id=view_id, x=x, y=y, size=size, priority_delta=4,
            setter=write, swatch=swatch)
        imgui.set_cursor_screen_pos(cursor)
        if changed:
            write(new_tint if isinstance(new_tint, tuple) else None)
            if not isinstance(new_tint, tuple):
                # The picker's delete button: the chip that opened the popover
                # is a brush next frame and never runs draw_tuple_fast
                # again, so let go of the popover here or it stays open.
                if Melty.popover_focused_ds is not None:
                    Melty.popover_focused_ds = None
                draw_state._tint_edit_key = None
            draw_state.invalidate()
            request_render()
        return changed
    if not show_brush:
        return False
    mouse_x, mouse_y = imgui.get_mouse_pos()
    brush_hovered = hovered and x <= mouse_x < x + size and y <= mouse_y < y + size
    brush_w = imgui.calc_text_size(brush_icon).x
    imgui.get_window_draw_list().add_text(x + (size - brush_w) * 0.5, text_y,
                                          brush_hover_col if brush_hovered else brush_col,
                                          brush_icon)
    if draw_state.on_action("left_mouse_down", view_id=view_id, rect=(x, y, x + size, y + size),
                            priority_delta=4) is None:
        return False
    # Stamp the brush in (the first write as a browsed file) and open the
    # picker to the chip that appears next frame by the same slot handoff
    # draw_tuple_fast's own click does - the opening click is graced, and
    # the chip, seeing itself owner with the slot on the host, draws the
    # picker and moves the slot to the pop window.
    write(default_tint)
    Melty.popover_focused_ds = draw_state
    draw_state._tint_edit_key = view_id
    Melty._popover_open_frame = Melty.frame_count
    draw_state.invalidate()
    request_render()
    return True


_XDG_LINE = re.compile(r'^\s*XDG_(\w+)_DIR\s*=\s*"?(.*?)"?\s*$')


def shortcut_directories(home=None):
    """The shortcuts column: home, the XDG user directories that exist
    (`~/.config/user-dirs.dirs`, the conventional names when the file is
    missing) and the filesystem root. [(label, Path)], home first."""
    home = Path(home) if home is not None else Path.home()
    names = ["DESKTOP", "DOCUMENTS", "DOWNLOAD", "PICTURES", "MUSIC", "VIDEOS"]
    fallback = {"DESKTOP": "Desktop", "DOCUMENTS": "Documents", "DOWNLOAD": "Downloads",
                "PICTURES": "Pictures", "MUSIC": "Music", "VIDEOS": "Videos"}
    dirs = {name: home / fallback[name] for name in names}
    config = home / ".config" / "user-dirs.dirs"
    try:
        for line in config.read_text().splitlines():
            match = _XDG_LINE.match(line)
            if match and match.group(1) in dirs:
                dirs[match.group(1)] = Path(match.group(2).replace("$HOME", str(home)))
    except OSError:
        pass
    out = [("Home", home)]
    for name in names:
        path = dirs[name]
        if path.is_dir() and path != home:
            out.append((path.name, path))
    out.append(("Computer", Path(os.sep)))
    return out


# ── type-to-search ──────────────────────────────────────────────────────────
_SEARCH_SEPARATORS = " _-.,()[]{}+&@'\""
# Keys that keep repeating while held (imgui's synthesized auto-repeat):
# GLFW's REPEAT events are noisy or absent on Wayland.
_SEARCH_REPEAT_KEYS = (glfw.KEY_BACKSPACE, glfw.KEY_UP, glfw.KEY_DOWN, glfw.KEY_TAB)


def search_match(name, query):
    """How `name` matches `query`, case-insensitively: ``(rank, spans)`` —
    rank 0 a prefix of the name, 1 the start of a word inside it (after a
    space, dot, dash, underscore ...), 2 a substring, 3 a subsequence (the
    query's characters in order, anything between) — or None. `spans` are
    the [start, end) character ranges of `name` the query landed on, what
    the listing highlights."""
    if not query:
        return None
    n, q = name.lower(), query.lower()
    at = n.find(q)
    if at == 0:
        return 0, [(0, len(q))]
    if at > 0:
        word_at = at if n[at - 1] in _SEARCH_SEPARATORS else -1
        pos = at
        while word_at < 0:
            pos = n.find(q, pos + 1)
            if pos < 0:
                break
            if n[pos - 1] in _SEARCH_SEPARATORS:
                word_at = pos
        if word_at >= 0:
            return 1, [(word_at, word_at + len(q))]
        return 2, [(at, at + len(q))]
    spans, pos = [], 0
    for ch in q:
        pos = n.find(ch, pos)
        if pos < 0:
            return None
        if spans and spans[-1][1] == pos:
            spans[-1] = (spans[-1][0], pos + 1)
        else:
            spans.append((pos, pos + 1))
        pos += 1
    return 3, spans


def search_hits(rows, query):
    """The rows of `rows` ([(Path, is_dir)]) matching `query`:
    ``([(row index, rank, spans)] in listing order, position of the best)``
    — the best is the lowest rank, the earliest in the listing among equals
    (folders lead it, so a folder beats a file at the same rank). No match:
    ``([], None)``."""
    hits = []
    for i, (path, _is_dir) in enumerate(rows):
        match = search_match(path.name, query)
        if match is not None:
            hits.append((i, match[0], match[1]))
    if not hits:
        return hits, None
    return hits, min(range(len(hits)), key=lambda k: (hits[k][1], k))


def search_keys():
    """This frame's keystrokes, in typed order, for a view that owns the
    keyboard: the GLFW callback queue (Melty.frame_key_events, every press
    and repeat since the last frame) plus imgui's synthesized auto-repeat
    for the keys that should keep firing while held. Keeps frames coming
    while one of those is down so the repeat cadence is sampled."""
    keys = list(Melty.frame_key_events)
    seen = {k for k, _m in keys}
    io = imgui.get_io()
    mods = ((glfw.MOD_SHIFT if io.key_shift else 0)
            | (glfw.MOD_CONTROL if io.key_ctrl else 0)
            | (glfw.MOD_ALT if getattr(io, "key_alt", False) else 0))
    for key in _SEARCH_REPEAT_KEYS:
        if imgui.is_key_down(key):
            request_render()
        if key not in seen and imgui.is_key_pressed(key, repeat=True):
            keys.append((key, mods))
    return keys


def claim_keyboard(draw_state):
    """Take meltygui's text-focus slot for `draw_state` when it is free (or held
    by an earlier draw_state of the same tile — a cache rebuild), the way the
    menu bar does while a menu is open: begin_frame then re-runs this view on
    every key event, hovered or not, and the bare-key global hotkeys (E, the
    invalidate tracker) stay muted. Returns True when the view has the
    keyboard this frame; an open popover (a colour picker, the context menu)
    keeps it off so the two never read the same arrows."""
    holder = Melty.text_focused_ds
    if holder is None or (holder is not draw_state
                          and getattr(holder, "_tile_id", None) == draw_state._tile_id):
        Melty.text_focused_ds = draw_state
        Melty._text_focus_grant_frame = Melty.frame_count
        holder = draw_state
    return holder is draw_state and Melty.popover_focused_ds is None


def search_typed(query, keys):
    """Apply this frame's `keys` ([(glfw key, mods)]) to the search `query`.
    Returns ``(query, step, activate, parent, escape)``: the edited query,
    the Up / Down / Tab steps (net, + is down), Enter, Ctrl+Up and a bare
    Esc that landed on an EMPTY query (the caller's "clear the selection").
    Alt / Super chords and Ctrl chords other than Backspace (clear) and V
    (paste) are left alone — they are shortcuts, not typing."""
    from meltygui.editor.text_editor import _KEY_CHAR_MAP
    step, activate, parent, escape = 0, False, False, False
    for key, mods in keys:
        if mods & (glfw.MOD_ALT | glfw.MOD_SUPER):
            continue
        ctrl, shift = bool(mods & glfw.MOD_CONTROL), bool(mods & glfw.MOD_SHIFT)
        if key == glfw.KEY_ESCAPE:
            if query:
                query = ""
            else:
                escape = True
        elif key == glfw.KEY_BACKSPACE:
            query = "" if ctrl else query[:-1]
        elif key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER):
            activate = True
        elif key == glfw.KEY_UP:
            if ctrl:
                parent = True
            else:
                step -= 1
        elif key == glfw.KEY_DOWN:
            if not ctrl:
                step += 1
        elif key == glfw.KEY_TAB:
            if query and not ctrl:
                step += -1 if shift else 1
        elif ctrl:
            if key == glfw.KEY_V:
                lines = (imgui.get_clipboard_text() or "").strip().splitlines()
                query += lines[0].strip() if lines else ""
        else:
            pair = _KEY_CHAR_MAP.get(key)
            if pair is not None:
                query += pair[1] if shift else pair[0]
    return query, step, activate, parent, escape


# ── the listing ─────────────────────────────────────────────────────────────


def _scroll_row_into_view(draw_state, index, row_h, rows_top, centre=False, content_h=None):
    """Nudge the window's scroll the minimal amount so row `index` (at
    `rows_top` + index × row_h in content coordinates) is fully visible.
    `centre`: a row that is out of sight lands in the middle of the view
    instead of at its edge (a search jump), the scroll clamped to
    `content_h` when given; a row already in view is left alone."""
    view_h = draw_state.abs_clipped_height - draw_state.header_height - draw_state.footer_height
    if view_h <= 0:
        return
    sx, sy = draw_state.scroll_offset
    row_top = rows_top + index * row_h
    row_bottom = row_top + row_h
    new_sy = sy
    if centre and (row_bottom > sy + view_h or row_top < sy):
        new_sy = row_top - (view_h - row_h) * 0.5
        if content_h is not None:
            new_sy = min(new_sy, max(0.0, content_h - view_h))
    else:
        if row_bottom > new_sy + view_h:
            new_sy = row_bottom - view_h
        if row_top < new_sy:
            new_sy = row_top
    new_sy = max(0.0, new_sy)
    if new_sy != sy:
        draw_state.scroll_offset = (sx, new_sy)
        draw_state.invalidate()


# ── the shortcuts column ────────────────────────────────────────────────────


# ── the explorer: shortcuts + listing ───────────────────────────────────────
