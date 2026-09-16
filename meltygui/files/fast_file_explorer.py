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

import meltygui.window_api as glfw
import meltygui_imgui as imgui

from meltygui.hdr_color import pack_color
from meltygui.melty import Melty
from meltygui.melty import FileWatch
from meltygui.state.dict_conversion import DictConversion
from meltygui.models.file_meta import FileMeta
from meltygui.models.file_meta import file_meta_store
from meltygui.extensions import source_folders as project_roots
from meltygui.toggles import Toggles
from meltygui.utils.glfw_utils import request_render
from meltygui.code.new_codecs import extension_to_codec
from meltygui.views.blit_offscreen import add_shadow
from meltygui.views.blit_offscreen import clear_glows
from meltygui.views.columns import ColumnLayout
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.core_decoration import no_save
from meltygui.views.drag_drop import DragDrop
from meltygui.views.headers import _brightness_clamp_fn


@no_save("_listing", "_last_dir", "_watched", "_search", "_search_for")
class FileExplorerState(DictConversion):
    """The listing's injected state (`explorer_state: FileExplorerState`).
    Persists the selection, the scroll position per directory visited
    (going back lands where you left) and the hidden-files switch."""
    _owner_ds = None

    def __init__(self):
        super().__init__()
        self.selected = None        # str path of the single-clicked row
        self.scroll_by_dir = {}     # str dir -> scroll y
        self.show_hidden = False    # dotfiles
        self._listing = None        # (dir, mtime_ns, show_hidden, rows) memo
        self._last_dir = None       # the dir of the last run: navigation detection
        self._watched = None        # the dir the view's FileWatch emitter is on
        self._search = ""           # the type-to-search query
        self._search_for = None     # the query the current selection was found for


class ShortcutState(DictConversion):
    """Persist sidebar order independently of directory listing order."""

    def __init__(self):
        super().__init__()
        self.order = []


# ── the directory watch ─────────────────────────────────────────────────────
# directory (str) -> the listing draw_states showing it. One FileWatch emitter per
# dir in this map; a listing moves its emitter on navigation (watch_directory)
# and the observer-thread listener posts an invalidate to the render thread.
_WATCHERS = globals().get("_WATCHERS", {})


def _on_file_event(src_path):
    """FileWatch global listener (observer thread): an entry of a watched
    directory changed — created, modified, moved, deleted — repaint the
    listings showing that directory. Bumps nothing else; the listing's
    mtime memo notices what changed."""
    directory = os.path.dirname(src_path)
    watchers = _WATCHERS.get(directory) or _WATCHERS.get(src_path)
    if not watchers:
        return

    def repaint(draw_states=tuple(watchers)):
        for draw_state in draw_states:
            draw_state.invalidate()
    Melty.post_to_render(repaint)


def watch_directory(draw_state, state, dir_key):
    """Point this listing's emitter at `dir_key`: the previous directory's
    emitter is retired when no other listing shows it (the inotify instance
    cap is per user), the new one scheduled through FileWatch.watch_dir."""
    if state._watched == dir_key:
        return
    if _on_file_event not in FileWatch.global_listeners:
        # Hotswap-safe: an older copy of this function is replaced by itself.
        FileWatch.global_listeners[:] = [f for f in FileWatch.global_listeners
                                         if getattr(f, "__name__", "") != "_on_file_event"]
        FileWatch.global_listeners.append(_on_file_event)
    FileWatch.start()
    previous = state._watched
    if previous is not None:
        holders = _WATCHERS.get(previous)
        if holders is not None:
            holders.discard(draw_state)
            if not holders:
                del _WATCHERS[previous]
                FileWatch.unwatch_dir(previous)
    _WATCHERS.setdefault(dir_key, set()).add(draw_state)
    FileWatch.watch_dir(dir_key)
    state._watched = dir_key


def list_directory(directory, show_hidden=False):
    """The rows of `directory`: [(Path, is_dir)] — folders first, then files,
    each case-insensitive by name. An unreadable directory lists empty."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    folders, files = [], []
    for entry in entries:
        if not show_hidden and entry.name.startswith("."):
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        (folders if is_dir else files).append(entry.name)
    key = str.casefold
    return ([(Path(directory) / n, True) for n in sorted(folders, key=key)]
            + [(Path(directory) / n, False) for n in sorted(files, key=key)])


def _dir_mtime_ns(directory):
    try:
        return os.stat(directory).st_mtime_ns
    except OSError:
        return -1


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


def set_row_tint(path, value):
    """Write `value` (an rgb(a) tuple) as the tint of `path` in the shared
    file-meta store, creating the entry only now — the listing never
    setdefault()s entries for the files it merely shows. None (the picker's
    clear) removes the tint, and the entry too when it holds nothing else,
    so an unpainted file leaves no trace in file_meta.pkl."""
    meta = file_meta_store()
    key = str(path)
    entry = meta.get(key)
    if value is None:
        if isinstance(entry, dict) and dict.__contains__(entry, "tint"):
            del entry["tint"]
            if dict.__len__(entry) == 0:
                del meta[key]
        return
    if not isinstance(entry, dict):
        entry = meta[key] = FileMeta()
    entry["tint"] = tuple(value)


def ordered_rows(rows, meta):
    """`rows` ([(Path, is_dir)], natural order) sorted by the `order` stamps
    in the meta store, the studio's rule (folder_files._apply_meta):
    stamped rows first by their number, unstamped ones after in natural
    order. No stamps at all: `rows` itself."""
    if meta is None:
        return rows
    orders = {}
    for i, (path, _is_dir) in enumerate(rows):
        entry = meta.get(str(path))
        if isinstance(entry, dict):
            order = entry.get("order")
            if isinstance(order, (int, float)):
                orders[i] = order
    if not orders:
        return rows
    indexed = sorted(range(len(rows)), key=lambda i: (orders.get(i, float("inf")), i))
    return [rows[i] for i in indexed]


def set_row_order(paths):
    """Stamp `order` = position into the meta entry of every path of a
    directory (created for the rows that have none — a reorder is the
    user's explicit edit of the folder, like painting it)."""
    meta = file_meta_store()
    for i, path in enumerate(paths):
        key = str(path)
        entry = meta.get(key)
        if not isinstance(entry, dict):
            entry = meta[key] = FileMeta()
        if entry.get("order") != i:
            entry["order"] = i


def apply_row_drop(rows, drag_keys, first_visible, drop):
    """The directory's new order after `drop` (a DropEvent from on_drop, or
    None): `drag_keys` are the rows that registered a drag handle this run
    — the visible ones, a contiguous slice of `rows` starting at
    `first_visible` — and the event's indices count in that slice. Returns
    the complete [Path] order to stamp, or None when nothing moved (no
    drop, a drop back in place, a cross-collection kind: rows only reorder
    here)."""
    if drop is None or drop.kind != "reorder" or not drag_keys:
        return None
    keys = list(drag_keys)
    if not drop.apply(keys):
        return None
    paths = [path for path, _is_dir in rows]
    return paths[:first_visible] + keys + paths[first_visible + len(drag_keys):]


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
    write = setter or (lambda value, _k=key: set_row_tint(_k, value))
    # [tint=(0.55, 0.72, 0.95)]
    brush_icon = f"\uf1fc"
    brush_col = pack_color(*brush_color, 1.0) if brush_color is not None else pack_color(1.0, 1.0, 1.0, 0.22)
    brush_hover_col = brush_col if brush_color is not None else pack_color(1.0, 1.0, 1.0, 0.9)
    from meltygui.views.new_core_view import draw_tuple_fast

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
@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=False,
             show_add_delete=False, is_tree=False, show_bg=False, shadow=False)
def draw_file_listing(input_value: str, draw_state, explorer_state: FileExplorerState,
                      left_mouse_down=False, left_mouse_double_clicked=False,
                      right_mouse_down=False,
                      ctrl_up_key_pressed=False, up_key_pressed=False, down_key_pressed=False,
                      enter_key_pressed=False, escape_key_pressed=False,
                      row_height=20.0, left_pad=6.0, glyph_width=18.0, crumb_height=24.0,
                      show_tint_chips=True, chip_size=17.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                      select_boost=0.22, plain_select_boost=0.06, select_shadow=2.0,
                      select_rounding=3.0, chip_mix=0.55, hover_boost=0.06, hover_alpha=0.05, text_mix=0.5, icon_mix=0.9,
                      folder_bg_boost=-0.12, folder_bg_rounding=0.0, drag_rows=True, menu_target=None,
                      type_to_search=True, search_tint=(1.0, 0.82, 0.3), search_dim=0.45,
                      search_flash_frames=36, show_crumbs=True, show_hidden=None,
                      **kwargs):
    """The path strip + rows of one directory (see the module docstring).
    `show_crumbs=False` leaves the strip out (a host drawing the crumbs in
    its own toolbar, where they stay put while the rows scroll).
    Returns ``(True, path)`` on navigation / a file double-click, else
    ``(False, input_value)``. `show_tint_chips` puts the tint chip / brush
    before each row's icon; `default_tint` is what the brush stamps. The
    selected row is its own tint brightened by `select_boost` (an unpainted
    row: `default_tint` by the smaller `plain_select_boost`, so it stays
    close to the background), lifted off the list by an add_shadow of
    `select_shadow` depth (0 disables it). `chip_mix` pulls the tint chip's
    colour toward its row background (0 = the raw tint). Hover is the same
    tint brightened by `hover_boost` on the selected row; any other hovered
    row gets only a faint white wash of `hover_alpha`. A painted
    row wears its tint on its text (mixed `text_mix` toward the tint) and
    its icon (`icon_mix`, stronger), not as a row background. `type_to_search`
    is the keyboard search of the module docstring: `search_tint` colours
    the matched letters and the pill, the non-matching rows' text fades to
    `search_dim` of its alpha while there are matches, and the flash on the
    row a keystroke lands on fades over `search_flash_frames` frames."""
    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    file_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    crumb_separator = "  /  "
    text_rgba = (0.92, 0.92, 0.92, 1.0)
    text_col = pack_color(*text_rgba)
    dim_col = pack_color(0.6, 0.63, 0.68, 1.0)
    folder_rgba = (0.78, 0.84, 0.92, 1.0)
    folder_col = pack_color(*folder_rgba)
    crumb_hover_col = pack_color(1.0, 1.0, 1.0, 0.12)
    search_wash = pack_color(search_tint[0], search_tint[1], search_tint[2], 0.30)
    search_col = pack_color(search_tint[0], search_tint[1], search_tint[2], 1.0)
    no_match_col = pack_color(0.95, 0.55, 0.5, 1.0)
    hover_wash = pack_color(1.0, 1.0, 1.0, hover_alpha)

    state = explorer_state
    # The selected row's add_shadow is RETAINED under this draw_state until
    # the caller opens its group again: a selection that vanishes (the file
    # trashed, Esc, a new directory) would otherwise keep its shadow.
    clear_glows(draw_state)
    px = Melty.px
    row_h, pad, glyph_w, crumb_h = px(row_height), px(left_pad), px(glyph_width), px(crumb_height)
    chip = px(chip_size) if show_tint_chips else 0.0
    # The tint control leads the row; icon & name shift right past it.
    chip_x = 0.0
    text_x = pad + (chip + px(6) if show_tint_chips else 0.0)
    directory = Path(input_value if input_value else Path.home()).expanduser()
    dir_key = str(directory)
    draw_list = imgui.get_window_draw_list()
    content_w = draw_state.content_width or (draw_state.width or 240)
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_down.x, left_mouse_down.y)
             if (left_mouse_down and hasattr(left_mouse_down, "x")) else None)
    double_click = ((left_mouse_double_clicked.x, left_mouse_double_clicked.y)
                    if (left_mouse_double_clicked and hasattr(left_mouse_double_clicked, "x")) else None)
    right_press = ((right_mouse_down.x, right_mouse_down.y)
                   if (right_mouse_down and hasattr(right_mouse_down, "x")) else None)
    meta = file_meta_store()
    row_bg = row_tint_bg()

    def navigate(target):
        """Leave `dir_key` for `target`: remember where this listing was
        scrolled so a return lands there, and hand the path to the caller."""
        state.scroll_by_dir[dir_key] = draw_state.scroll_offset[1]
        request_render()
        return True, str(target)

    # ── a new directory: restore the scroll, drop the selection, move the watch ──
    if state._last_dir != dir_key:
        state._last_dir = dir_key
        state.selected = None
        state._search, state._search_for = "", None
        draw_state.scroll_offset = (0.0, state.scroll_by_dir.get(dir_key, 0.0))
        draw_state.invalidate()
    watch_directory(draw_state, state, dir_key)

    # ── listing, memoized by the directory's mtime (renames / new files bump it) ──
    mtime = _dir_mtime_ns(directory)
    listing = state._listing
    if show_hidden is not None:
        state.show_hidden = show_hidden
    if listing is None or listing[:3] != (dir_key, mtime, state.show_hidden):
        listing = state._listing = (dir_key, mtime, state.show_hidden,
                                    list_directory(directory, state.show_hidden))
    rows = ordered_rows(listing[3], meta)

    # ── the path strip: every segment a crumb; click = jump there ──
    x0, y0 = imgui.get_cursor_screen_pos()
    crumbs = []                              # (x_left, x_right, target Path)
    parts = directory.parts if show_crumbs else ()
    cx = x0 + pad
    crumb_y = y0 + (crumb_h - imgui.get_font_size()) * 0.5
    for i, part in enumerate(parts):
        label = part if part != os.sep else os.sep
        width = imgui.calc_text_size(label).x
        target = Path(*parts[:i + 1])
        crumbs.append((cx - px(3), cx + width + px(3), target))
        hovered = hover_ok and cx - px(3) <= mouse_x < cx + width + px(3) and y0 <= mouse_y < y0 + crumb_h
        crumb_tint = FileMeta.painted_tint(meta.get(str(target))) if meta is not None else None
        if crumb_tint:
            draw_list.add_rect_filled(cx - px(3), y0 + px(2), cx + width + px(3), y0 + crumb_h - px(2),
                                      row_bg(crumb_tint, folder_bg_boost or 0.0), rounding=px(3))
        if hovered:
            draw_list.add_rect_filled(cx - px(3), y0 + px(2), cx + width + px(3), y0 + crumb_h - px(2),
                                      crumb_hover_col, rounding=px(3))
        last = i == len(parts) - 1
        crumb_col = (tinted_text(text_rgba, crumb_tint, text_mix) if crumb_tint
                     else text_col if last else dim_col)
        draw_list.add_text(cx, crumb_y, crumb_col, label)
        cx += width
        if not last and part == os.sep:
            cx += px(8)  # Keep the root chip separate from the first folder.
        elif not last:
            draw_list.add_text(cx, crumb_y, dim_col, crumb_separator)
            cx += imgui.calc_text_size(crumb_separator).x
    if show_crumbs:
        imgui.dummy(content_w, crumb_h)
    if click is not None and crumbs and y0 <= click[1] < y0 + crumb_h:
        for left, right, target in crumbs:
            if left <= click[0] < right and target != directory:
                return navigate(target)

    # ── content height: rows + the top inset (the fast_dock rule) ──
    rows_x, rows_y = imgui.get_cursor_screen_pos()
    # ── a painted directory: a tint wash below the breadcrumb strip ──
    dir_tint = FileMeta.painted_tint(meta.get(dir_key)) if meta is not None else None
    if dir_tint and folder_bg_boost is not None:
        wash = getattr(draw_state, "abs_clip_rect", None)
        if wash is None:
            wash = (draw_state.abs_left, draw_state.abs_top,
                    draw_state.abs_left + (draw_state.width or 0),
                    draw_state.abs_top + (draw_state.height or 0))
        wash_top = max(wash[1], rows_y)
        if wash_top < wash[3]:
            draw_list.add_rect_filled(wash[0], wash_top, wash[2], wash[3],
                                      row_bg(dir_tint, folder_bg_boost), rounding=px(folder_bg_rounding))

    top_inset = (rows_y + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(content_w, max(1.0, len(rows) * row_h + max(0.0, top_inset)))

    # ── input on rows ──
    # The tint control's widget (chip / brush) at the row's left edge is
    # the chip's own click, never the row's - a double-click there must
    # not navigate.
    chip_x = rows_x + pad
    clip = getattr(draw_state, "abs_clip_rect", None)

    drag_left = chip_x + chip + px(2) if show_tint_chips else rows_x

    def row_at(point, chips=False):
        if point is None or not (rows_x <= point[0] <= rows_x + content_w):
            return None
        if not chips and show_tint_chips and point[0] < drag_left:
            return None
        index = int((point[1] - rows_y) // row_h)
        return index if 0 <= index < len(rows) else None

    selected_index = None
    if state.selected is not None:
        for i, (path, _is_dir) in enumerate(rows):
            if str(path) == state.selected:
                selected_index = i
                break

    # ── the keyboard: the type-to-search queue when this listing owns it,
    # else the hover-routed key params (the find box has the keyboard, say) ──
    owns_keys = type_to_search and claim_keyboard(draw_state)
    query = state._search if type_to_search else ""
    step = (1 if down_key_pressed else 0) - (1 if up_key_pressed else 0)
    if owns_keys:
        keys = search_keys()
        query, step, enter_key_pressed, ctrl_up_key_pressed, escape_key_pressed = \
            search_typed(query, keys)
        if keys:
            draw_state.invalidate()
            request_render()
    if query != state._search:
        state._search = query
    rows_top = rows_y - draw_state.abs_top + draw_state.scroll_offset[1]
    view_rect = clip if clip is not None else (
        draw_state.abs_left, draw_state.abs_top,
        draw_state.abs_left + (draw_state.width or 0), draw_state.abs_top + (draw_state.height or 0))

    def flash_row(index):
        """Melty.emphasize on row `index`: a fire-and-forget flash whose rect
        follows the scroll, scissored to the listing (keyed by the row's
        path, so a new target starts a fresh fade)."""
        top = rows_top + index * row_h

        def rect(ds=draw_state, top=top, h=row_h, x0=rows_x, w=content_w):
            y = ds.abs_top + top - ds.scroll_offset[1]
            return (x0, y, x0 + w, y + h)

        def clip_rect(ds=draw_state, fallback=view_rect):
            return getattr(ds, "abs_clip_rect", None) or fallback

        Melty.emphasize(f"explorer search {draw_state._tile_id} {rows[index][0]}", rect,
                        clip=clip_rect, rounding=px(select_rounding),
                        fade_frames=search_flash_frames)

    def jump_to_row(index):
        """Select row `index` and bring it into view; the flash only when the
        landing row CHANGES (a keystroke that keeps the same best match, or
        the same step target, is quiet)."""
        moved = index != selected_index
        state.selected = str(rows[index][0])
        _scroll_row_into_view(draw_state, index, row_h, rows_top, centre=True,
                              content_h=len(rows) * row_h + max(0.0, top_inset))
        if moved:
            flash_row(index)
        request_render()
        return index

    hit = row_at(double_click)
    if hit is not None:
        return navigate(rows[hit][0])
    hit = row_at(click)
    if hit is not None:
        state.selected = str(rows[hit][0])
        selected_index = hit
        request_render()
    # A right-press selects the row under it (the chip column included): the
    # wrapper opens the context menu on the release, on that row.
    hit = row_at(right_press, chips=True)
    if hit is not None:
        state.selected = str(rows[hit][0])
        selected_index = hit
        draw_state.invalidate()
        request_render()
    elif right_press is not None and state.selected is not None:
        # Empty space: the menu acts on the dir, not a stale selection.
        state.selected = None
        selected_index = None
        draw_state.invalidate()
        request_render()
    if menu_target is not None:
        menu_target["path"] = state.selected if selected_index is not None else dir_key
    if ctrl_up_key_pressed and directory.parent != directory:
        return navigate(directory.parent)
    if enter_key_pressed and selected_index is not None:
        return navigate(rows[selected_index][0])
    if escape_key_pressed and state.selected is not None:
        state.selected = None
        draw_state.invalidate()
        request_render()

    # ── the search: rank the rows, land on the best, step through the rest ──
    # A query change (or a selection that left no matches: a click in the
    # directory changed under the watch) lands on the best match; Up / Down
    # / Tab walk the matches in listing order, wrapping. Without a query the
    # steps walk the whole listing as before. No match: the selection stays
    # where it was and the listing says so.
    hits, best = search_hits(rows, query) if query else ([], None)
    hit_rows = {index: spans for index, _rank, spans in hits}
    if query and hits:
        positions = [index for index, _rank, _spans in hits]
        if state._search_for != query or selected_index not in hit_rows:
            selected_index = jump_to_row(positions[best])
        elif step:
            at = positions.index(selected_index)
            selected_index = jump_to_row(positions[(at + step) % len(positions)])
    elif step and rows and not query:
        selected_index = (0 if selected_index is None and step > 0
                          else len(rows) - 1 if selected_index is None
                          else max(0, min(len(rows) - 1, selected_index + step)))
        state.selected = str(rows[selected_index][0])
        _scroll_row_into_view(draw_state, selected_index, row_h, rows_top)
        request_render()
    state._search_for = query if query else None
    if query and not hits and step:
        request_render()
    dimmed = bool(query and hits)

    # ── rows: viewport-culled, straight to the draw list ──
    text_y_pad = (row_h - imgui.get_font_size()) * 0.5
    ghost_alpha = 0.9
    drag_keys = []          # the visible rows' paths, in on_drag call order
    first_visible = None    # index into `rows` of drag_keys[0]
    for i, (path, is_dir) in enumerate(rows):
        ry0 = rows_y + i * row_h
        ry1 = ry0 + row_h
        if clip is not None and (ry1 < clip[1] or ry0 > clip[3]):
            continue
        key = str(path)
        entry = meta.get(key) if meta is not None else None
        tint = FileMeta.painted_tint(entry)
        icon = row_icon(path, is_dir, entry, folder_icon, file_icon)
        spans = hit_rows.get(i) if dimmed else None
        name_rgba, glyph_rgba = (folder_rgba if is_dir else text_rgba), text_rgba
        if dimmed and spans is None:
            name_rgba = name_rgba[:3] + (name_rgba[3] * search_dim,)
            glyph_rgba = glyph_rgba[:3] + (glyph_rgba[3] * search_dim,)
        if tint:
            name_col = tinted_text(name_rgba, tint, text_mix)
            icon_col = tinted_text(glyph_rgba, tint, icon_mix)
        elif dimmed and spans is None:
            name_col = pack_color(*name_rgba)
            icon_col = pack_color(*glyph_rgba)
        else:
            name_col = icon_col = folder_col if is_dir else text_col
        if drag_rows:
            # The tab bar's immediate-mode DragDrop: the row (past the chip
            # column) is its own drag handle. While THIS row is the drag,
            # on_drag has parked the cursor at the ghost: paint the row
            # there on the overlay, but leave the inline row alone
            # (DragDrop treats it as the home / drop target).
            if first_visible is None:
                first_visible = i
            drag_keys.append(path)
            drag = DragDrop.on_drag((drag_left, ry0, rows_x + content_w, ry1), key=key,
                                    draw_state=draw_state)
            if drag:
                ghost = drag.draw_list
                ghost.add_rect_filled(drag.x, drag.y, drag.x + drag.w, drag.y + drag.h,
                                      pack_color(*row_bg.rgb(tint or default_tint, select_boost),
                                                 ghost_alpha),
                                      rounding=px(select_rounding))
                ghost.add_text(drag.x + px(4), drag.y + text_y_pad, icon_col, icon)
                ghost.add_text(drag.x + px(4) + glyph_w, drag.y + text_y_pad, name_col, path.name)
                DragDrop.end_drag()
                continue
        row_hovered = hover_ok and rows_x <= mouse_x <= rows_x + content_w and ry0 <= mouse_y < ry1
        # Selection / hover are brighter steps of the row's own tint
        # (the default tint when unpainted); they stack.
        boost = (select_boost if tint else plain_select_boost) if i == selected_index else 0.0
        if i == selected_index and select_shadow:
            add_shadow((rows_x, ry0, content_w, row_h), offset=select_shadow,
                       corner_radius=px(select_rounding), clip=clip, draw_state=draw_state)
        if boost:
            draw_list.add_rect_filled(rows_x, ry0, rows_x + content_w, ry1,
                                      row_bg(tint or default_tint,
                                             boost + (hover_boost if row_hovered else 0.0)),
                                      rounding=px(select_rounding))
        elif row_hovered:
            draw_list.add_rect_filled(rows_x, ry0, rows_x + content_w, ry1, hover_wash,
                                      rounding=px(select_rounding))
        draw_list.add_text(rows_x + text_x, ry0 + text_y_pad, icon_col, icon)
        name_x = rows_x + text_x + glyph_w
        if spans:
            # The matched letters: a wash of the search tint over them.
            name = path.name
            for start, end in spans:
                sx0 = name_x + imgui.calc_text_size(name[:start]).x
                sx1 = sx0 + imgui.calc_text_size(name[start:end]).x
                draw_list.add_rect_filled(sx0 - px(1), ry0 + px(2), sx1 + px(1), ry1 - px(2),
                                          search_wash, rounding=px(2))
        draw_list.add_text(name_x, ry0 + text_y_pad, name_col, path.name)
        if show_tint_chips:
            swatch = None
            if tint:
                swatch = chip_swatch(tint, row_bg.rgb(tint, boost), chip_mix)
            tint_control(draw_state, key, tint, chip_x, ry0 + (row_h - chip) * 0.5, chip,
                         ry0 + text_y_pad, row_hovered, default_tint, swatch=swatch,
                         show_brush=i == selected_index)

    # ── the search pill: the query and "n of m", bottom right of the view ──
    if query:
        icon_search = "\uf002"
        count = (f"{positions.index(selected_index) + 1} of {len(hits)}"
                 if hits and selected_index in hit_rows else "no match")
        pill_h = px(26)
        pad_x, gap = px(11), px(12)
        query_w = imgui.calc_text_size(query).x
        icon_w = imgui.calc_text_size(icon_search).x
        count_w = imgui.calc_text_size(count).x
        pill_w = pad_x + icon_w + px(8) + query_w + gap + count_w + pad_x
        px1 = view_rect[2] - px(14)
        py1 = view_rect[3] - px(12)
        px0 = max(view_rect[0] + px(6), px1 - pill_w)
        py0 = py1 - pill_h
        draw_list.add_rect_filled(px0 + px(1), py0 + px(2), px1 + px(1), py1 + px(2),
                                  pack_color(0.0, 0.0, 0.0, 0.35), rounding=pill_h * 0.5)
        draw_list.add_rect_filled(px0, py0, px1, py1,
                                  pack_color(*row_bg.rgb(default_tint, -0.02), 0.97),
                                  rounding=pill_h * 0.5)
        draw_list.add_rect(px0, py0, px1, py1, pack_color(1.0, 1.0, 1.0, 0.14),
                           rounding=pill_h * 0.5)
        pill_ty = py0 + (pill_h - imgui.get_font_size()) * 0.5
        draw_list.add_text(px0 + pad_x, pill_ty, search_col, icon_search)
        draw_list.add_text(px0 + pad_x + icon_w + px(8), pill_ty, text_col, query)
        draw_list.add_text(px1 - pad_x - count_w, pill_ty,
                           dim_col if hits else no_match_col, count)

    # ── close the drag body: paint the between-row slots, apply a drop ──
    # A reorder lands in on_drag call order = the visible rows, a contiguous
    # slice of `rows`; splice the new slice in and stamp the whole
    # directory's order (folders and files together - the stamps are the order
    # from now on). Cross-collection kinds ("insert" / "remove") are ignored:
    # rows only reorder here.
    if drag_rows:
        drop = DragDrop.on_drop(draw_state=draw_state)
        order = apply_row_drop(rows, drag_keys, first_visible, drop)
        if order is not None:
            set_row_order(order)
            draw_state.invalidate()
            request_render()

    return False, input_value


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
@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=True,
             show_add_delete=False, is_tree=False, show_bg=False, shadow=False)
def draw_shortcuts(input_value: str, draw_state, left_mouse_clicked=False,
                   shortcut_state: ShortcutState = None, row_height=22.0,
                   show_tint_chips=True, chip_size=17.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                   show_projects=True, **kwargs):
    """The shortcuts column on its own: home, the XDG user directories and
    the root as draw-list rows, then a **Projects** section — every folder
    marked with meltygui.mark_project (the shared file-meta store's flag) —
    and a click returns ``(True, path)`` once. `input_value` is the
    directory the host shows (or None): the deepest shortcut / project
    holding it is the CURRENT row (brighter, lifted by a shadow, its brush
    showing). Every row leads with its folder's tint control (the same
    file-meta tint the listings wear). Shortcuts drag to reorder, the order
    persisting in `shortcut_state`; projects keep the store's path order.
    The explorer draws this in its first cell; the code editor draws it as
    a leading column (`show_shortcuts=True`), a pick selecting the project
    in its injected `EditorProjectState`."""
    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    computer_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    left_pad = 8.0
    # [tint=(0.55, 0.72, 0.95)]
    glyph_width = 20.0
    text_rgba = (0.85, 0.88, 0.92, 1.0)
    text_col = pack_color(*text_rgba)
    # [tint=(0.55, 0.72, 0.95)]
    hover_boost = 0.06
    # [tint=(0.55, 0.72, 0.95)]
    hover_alpha = 0.05
    # [tint=(0.55, 0.72, 0.95)]
    text_mix = 0.5
    # [tint=(0.55, 0.72, 0.95)]
    icon_mix = 0.9
    hover_wash = pack_color(1.0, 1.0, 1.0, hover_alpha)

    px = Melty.px
    clear_glows(draw_state)      # the current row's retained shadow
    directory = Path(input_value).expanduser() if input_value else None
    draw_list = imgui.get_window_draw_list()
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_clicked.x, left_mouse_clicked.y)
             if (left_mouse_clicked and hasattr(left_mouse_clicked, "x")) else None)
    chip = px(chip_size) if show_tint_chips else 0.0
    text_x = px(left_pad) + (chip + px(6) if show_tint_chips else 0.0)
    meta = file_meta_store()
    row_bg = row_tint_bg()
    x, y = imgui.get_cursor_screen_pos()
    width = float(draw_state.content_width or draw_state.width or px(190))
    row_h = px(row_height)
    shortcuts = shortcut_directories()
    ranks = {key: i for i, key in enumerate(shortcut_state.order)}
    shortcuts.sort(key=lambda item: ranks.get(str(item[1]), len(ranks)))
    # The second section: every folder marked as a project - an alphabetical
    # list of the projects, the same rows in path order, no drag-reorder
    # (the set is the store's, not this column's).
    projects = ([(root.name or str(root), root) for root in project_roots()]
                if show_projects else [])
    # The shortcut / project that holds the current directory: the deepest one.
    current = None
    if directory is not None:
        for label, path in shortcuts + projects:
            if path == directory or path in directory.parents:
                if current is None or len(path.parts) > len(current.parts):
                    current = path
    text_y_pad = (row_h - imgui.get_font_size()) * 0.5
    chip_x = x + px(left_pad)
    # The tint control's column: the chip's own click, never the row's.
    click_left = chip_x + chip + px(2) if show_tint_chips else x
    section_gap = row_h * 0.5

    def draw_rows(rows, y, draggable):
        """One section of rows from `y` down; returns the picked path."""
        picked = None
        for i, (label, path) in enumerate(rows):
            ry0 = y + i * row_h
            ry1 = ry0 + row_h
            key = str(path)
            tint = FileMeta.painted_tint(meta.get(key)) if meta is not None else None
            icon = computer_icon if path == Path(os.sep) else folder_icon
            label_col = tinted_text(text_rgba, tint, text_mix) if tint else text_col
            icon_col = tinted_text(text_rgba, tint, icon_mix) if tint else text_col
            drag = DragDrop.on_drag((click_left, ry0, x + width, ry1), key=key,
                                    draw_state=draw_state) if draggable else None
            if drag:
                ghost = drag.draw_list
                ghost.add_rect_filled(drag.x, drag.y, drag.x + drag.w, drag.y + drag.h,
                                      row_bg(tint or default_tint, 0.22), rounding=px(4))
                ghost.add_text(drag.x + px(4), drag.y + text_y_pad, icon_col, icon)
                ghost.add_text(drag.x + px(4) + px(glyph_width), drag.y + text_y_pad,
                               label_col, label)
                DragDrop.end_drag()
                continue
            row_hovered = hover_ok and x <= mouse_x < x + width and ry0 <= mouse_y < ry1
            # The current row: its tint (default when unpainted), brighter,
            # lifted above the column by a soft shadow; hover is a smaller
            # brightening of the same tint.
            boost = (0.22 if tint else 0.06) if path == current else 0.0
            if path == current:
                add_shadow((x, ry0, width, row_h), offset=2.0, corner_radius=px(4),
                           clip=getattr(draw_state, "abs_clip_rect", None), draw_state=draw_state)
            if boost:
                draw_list.add_rect_filled(x, ry0, x + width, ry1,
                                          row_bg(tint or default_tint,
                                                 boost + (hover_boost if row_hovered else 0.0)),
                                          rounding=px(4))
            elif row_hovered:
                draw_list.add_rect_filled(x, ry0, x + width, ry1, hover_wash, rounding=px(4))
            draw_list.add_text(x + text_x, ry0 + text_y_pad, icon_col, icon)
            draw_list.add_text(x + text_x + px(glyph_width), ry0 + text_y_pad, label_col, label)
            if (click is not None and click_left <= click[0] < x + width and ry0 <= click[1] < ry1
                    and path != directory):
                picked = path
            if show_tint_chips:
                swatch = None
                if tint:
                    swatch = chip_swatch(tint, row_bg.rgb(tint, boost))
                tint_control(draw_state, key, tint, chip_x, ry0 + (row_h - chip) * 0.5, chip,
                             ry0 + text_y_pad, row_hovered, default_tint, swatch=swatch,
                             show_brush=path == current)
        return picked

    picked = draw_rows(shortcuts, y, draggable=True)
    total_h = len(shortcuts) * row_h
    if projects:
        # A dim "Projects" heading half a row below the shortcuts.
        head_y = y + total_h + section_gap
        draw_list.add_text(x + text_x, head_y + text_y_pad,
                           imgui.get_color_u32_rgba(*text_rgba[:3], text_rgba[3] * 0.55),
                           "Projects")
        picked = draw_rows(projects, head_y + row_h, draggable=False) or picked
        total_h += section_gap + row_h + len(projects) * row_h
    drop = DragDrop.on_drop(draw_state=draw_state)
    if drop is not None and drop.kind == "reorder" and drop.apply(shortcuts):
        shortcut_state.order = [str(path) for _label, path in shortcuts]
        picked = None
    imgui.dummy(width, total_h)
    if picked is not None:
        request_render()
        return True, str(picked)
    return False, input_value


# ── the explorer: shortcuts + listing ───────────────────────────────────────
@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=True,
             show_add_delete=False, is_tree=False, show_bg=False, shadow=False)
def draw_fast_file_explorer(input_value: str, draw_state, column_edges=None,
                            left_mouse_clicked=False, ctrl_up_key_pressed=False,
                            shortcuts_width=190.0, shortcut_row_height=22.0, column_gap=6.0,
                            show_tint_chips=True, chip_size=17.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                            context_menu=None, drag_rows=True, folder_bg_boost=-0.12,
                            folder_bg_rounding=0.0, type_to_search=True, show_crumbs=True,
                            layout_out=None, show_hidden=None, **kwargs):
    """A ColumnLayout with two cells: `draw_shortcuts` (a click navigates)
    and `draw_file_listing`, sharing one draggable edge
    (`column_edges`, persisted by auto-state). Returns what the listing
    returns; Ctrl+Up works from anywhere over the explorer. Shortcut rows
    wear their directory's tint like the listing's, with the same leading
    tint control (`show_tint_chips`, `chip_size`, `default_tint`).
    `context_menu` = {label: callable(path)} is the listing's right-click
    menu; each callable gets the path of the right-clicked row, or of the
    directory (see the module docstring). `drag_rows` / `folder_bg_boost`
    go to the listing, as does `type_to_search` (the keyboard search of the
    module docstring; False leaves the keyboard alone), and `show_crumbs`
    (False drops the listing's path strip for a host that draws its own).
    `layout_out`, a dict, receives ``listing_left``: the absolute x where
    the listing column's content starts, so a host toolbar can line up with
    it (the frame's value lands after this call; a host drawing above the
    explorer reads last frame's). Shortcuts drag to reorder, with their own
    persisted order."""
    # [tint=(0.55, 0.72, 0.95)]
    folder_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    computer_icon = f""
    # [tint=(0.55, 0.72, 0.95)]
    left_pad = 8.0
    # [tint=(0.55, 0.72, 0.95)]
    glyph_width = 20.0
    text_rgba = (0.85, 0.88, 0.92, 1.0)
    text_col = pack_color(*text_rgba)
    # [tint=(0.55, 0.72, 0.95)]
    hover_boost = 0.06
    # [tint=(0.55, 0.72, 0.95)]
    hover_alpha = 0.05
    # [tint=(0.55, 0.72, 0.95)]
    text_mix = 0.5
    # [tint=(0.55, 0.72, 0.95)]
    icon_mix = 0.9
    hover_wash = pack_color(1.0, 1.0, 1.0, hover_alpha)

    px = Melty.px
    clear_glows(draw_state)      # The current shortcut's retained shadow (see the listing)
    directory = Path(input_value if input_value else Path.home()).expanduser()
    draw_list = imgui.get_window_draw_list()
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_clicked.x, left_mouse_clicked.y)
             if (left_mouse_clicked and hasattr(left_mouse_clicked, "x")) else None)
    chip = px(chip_size) if show_tint_chips else 0.0
    text_x = px(left_pad) + (chip + px(6) if show_tint_chips else 0.0)
    meta = file_meta_store()
    row_bg = row_tint_bg()
    # The listing writes what the right-click landed on here each run; the
    # menu item callables (built here, run by the wrapper's items menu with no
    # label) read it when picked.
    menu_target = {"path": str(directory)}
    menu_items = None
    if context_menu:
        menu_items = {label: (lambda _action=action: _action(menu_target["path"]))
                      for label, action in context_menu.items()}

    # The band: from the flow cursor to the bottom of the view.
    body_top = imgui.get_cursor_screen_pos()[1]
    body_bottom = draw_state.abs_top + (draw_state.height or px(600))
    body_height = max(px(120), body_bottom - body_top)
    columns = ColumnLayout(draw_state, 2, column_edges=column_edges,
                           column_widths=[shortcuts_width, None], column_mins=[110, 200],
                           padding=px(column_gap), padding_y=0, border_color=None)

    result = (False, input_value)
    picked = None
    with columns.cell(0, height=body_height) as width:
        changed_s, picked_s = draw_shortcuts(
            str(directory), name="shortcuts", width=width, height=body_height,
            row_height=shortcut_row_height, show_tint_chips=show_tint_chips,
            chip_size=chip_size, default_tint=default_tint, disable_scroll=True,
            left_mouse_clicked=left_mouse_clicked)
        picked = Path(picked_s) if changed_s else None
    with columns.cell(1, height=body_height) as width:
        if layout_out is not None:
            layout_out["listing_left"] = imgui.get_cursor_screen_pos()[0]
        changed, value = draw_file_listing(str(directory), name="listing", width=width,
                                           height=body_height, disable_scroll=False,
                                           context_menu=menu_items, menu_target=menu_target,
                                           drag_rows=drag_rows, folder_bg_boost=folder_bg_boost,
                                           folder_bg_rounding=folder_bg_rounding,
                                           show_tint_chips=show_tint_chips, chip_size=chip_size,
                                           default_tint=default_tint, type_to_search=type_to_search,
                                           show_crumbs=show_crumbs, show_hidden=show_hidden)
        if changed:
            result = (True, value)
    columns.finish()

    if result[0]:
        return result
    if picked is not None:
        request_render()
        return True, str(picked)
    if ctrl_up_key_pressed and directory.parent != directory:
        request_render()
        return True, str(directory.parent)
    return False, input_value
