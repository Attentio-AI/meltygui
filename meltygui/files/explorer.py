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
trees paint) as its background, run through the editor tab's colour recipe,
and its icon from the meta entry, else the codec's, else the folder / file
glyph. Every row — the shortcuts too — leads with its tint control: a
painted row shows its `draw_tuple_fast` chip (click = the colour-picker
popover, the studio's tab-bar chip), an unpainted one a faint paint-brush
button (shown only on the selected row / the current shortcut) whose click
stamps `default_tint` in and opens the picker. The store is only
WRITTEN for a row the user paints (a meta entry per browsed file would
bloat ~/.melty/file_meta.pkl); clearing the colour in the picker drops the
tint again, and the entry with it when nothing else is attached.
"""
import os
import re
from pathlib import Path

import imgui

from src.lsd.gl_gui.hdr_color import pack_color
from src.lsd.gl_gui.melty import Melty, FileWatch
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.model.file_meta import FileMeta, file_meta_store
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.new_codecs import extension_to_codec
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.columns import ColumnLayout
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save
from src.lsd.gl_gui.view.core_views.headers import _brightness_clamp_fn


@no_save("_listing", "_last_dir", "_watched")
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
                 swatch=None, show_brush=True):
    """One row's tint control at (x, y): the `draw_tuple_fast` chip when
    `tint` is painted, else the paint-brush button. `key` is the store
    path; `hovered` says the pointer is on the row (the brush brightens
    under it). One view_id for chip and brush, so the brush's click can
    hand the popover to the chip that replaces it next frame;
    priority_delta=4 outranks the row's own left_mouse_down param
    (registered at 3). `swatch` is the colour the chip paints (see
    `chip_swatch`); None paints the tint itself. `show_brush` False draws
    (and registers) no brush for an unpainted row — the listing shows it
    only on the selected row. Returns True when the store was written."""
    # [tint=(0.55, 0.72, 0.95)]
    brush_icon = f"\uf1fc"
    brush_col = pack_color(1.0, 1.0, 1.0, 0.22)
    brush_hover_col = pack_color(1.0, 1.0, 1.0, 0.9)
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_tuple_fast

    view_id = f"tint_{key}"
    if tint:
        cursor = imgui.get_cursor_screen_pos()
        changed, new_tint = draw_tuple_fast(
            tint, draw_state, view_id=view_id, x=x, y=y, size=size, priority_delta=4,
            setter=lambda value, _k=key: set_row_tint(_k, value), swatch=swatch)
        imgui.set_cursor_screen_pos(cursor)
        if changed:
            set_row_tint(key, new_tint if isinstance(new_tint, tuple) else None)
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
    set_row_tint(key, default_tint)
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


# ── the listing ─────────────────────────────────────────────────────────────
@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=False,
             show_add_delete=False, is_tree=False, show_bg=False, shadow=False)
def draw_file_listing(input_value: str, draw_state, explorer_state: FileExplorerState,
                      left_mouse_down=False, left_mouse_double_clicked=False,
                      ctrl_up_key_pressed=False, up_key_pressed=False, down_key_pressed=False,
                      enter_key_pressed=False, escape_key_pressed=False,
                      row_height=20.0, left_pad=6.0, glyph_width=18.0, crumb_height=24.0,
                      show_tint_chips=True, chip_size=17.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                      select_boost=0.22, plain_select_boost=0.06, select_shadow=2.0,
                      select_rounding=3.0, chip_mix=0.55, hover_boost=0.08, text_mix=0.3,
                      **kwargs):
    """The path strip + rows of one directory (see the module docstring).
    Returns ``(True, path)`` on navigation / a file double-click, else
    ``(False, input_value)``. `show_tint_chips` puts the tint chip / brush
    before each row's icon; `default_tint` is what the brush stamps. The
    selected row is its own tint brightened by `select_boost` (an unpainted
    row: `default_tint` by the smaller `plain_select_boost`, so it stays
    close to the background), lifted off the list by an add_shadow of
    `select_shadow` depth (0 disables it). `chip_mix` pulls the tint chip's
    colour toward its row background (0 = the raw tint). Hover is the same
    tint brightened by `hover_boost` (on top of the selection's), and a
    tinted row's text is mixed `text_mix` toward its tint."""
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

    state = explorer_state
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
        draw_state.scroll_offset = (0.0, state.scroll_by_dir.get(dir_key, 0.0))
        draw_state.invalidate()
    watch_directory(draw_state, state, dir_key)

    # ── listing, memoized by the directory's mtime (renames / new files bump it) ──
    mtime = _dir_mtime_ns(directory)
    listing = state._listing
    if listing is None or listing[:3] != (dir_key, mtime, state.show_hidden):
        listing = state._listing = (dir_key, mtime, state.show_hidden,
                                    list_directory(directory, state.show_hidden))
    rows = listing[3]

    # ── the path strip: every segment a crumb; click = jump there ──
    x0, y0 = imgui.get_cursor_screen_pos()
    crumbs = []                              # (x_left, x_right, target Path)
    parts = directory.parts
    cx = x0 + pad
    crumb_y = y0 + (crumb_h - imgui.get_font_size()) * 0.5
    for i, part in enumerate(parts):
        label = part if part != os.sep else os.sep
        width = imgui.calc_text_size(label).x
        target = Path(*parts[:i + 1])
        crumbs.append((cx, cx + width, target))
        hovered = hover_ok and cx <= mouse_x < cx + width and y0 <= mouse_y < y0 + crumb_h
        if hovered:
            draw_list.add_rect_filled(cx - px(3), y0 + px(2), cx + width + px(3), y0 + crumb_h - px(2),
                                      crumb_hover_col, rounding=px(3))
        last = i == len(parts) - 1
        draw_list.add_text(cx, crumb_y, text_col if last else dim_col, label)
        cx += width
        if not last and part != os.sep:       # the root "/" is its own separator
            draw_list.add_text(cx, crumb_y, dim_col, crumb_separator)
            cx += imgui.calc_text_size(crumb_separator).x
    imgui.dummy(content_w, crumb_h)
    if click is not None and y0 <= click[1] < y0 + crumb_h:
        for left, right, target in crumbs:
            if left <= click[0] < right and target != directory:
                return navigate(target)

    # ── content height: rows + the top inset (the fast_dock rule) ──
    rows_x, rows_y = imgui.get_cursor_screen_pos()
    top_inset = (rows_y + draw_state.scroll_offset[1]) - draw_state.abs_top
    imgui.dummy(content_w, max(1.0, len(rows) * row_h + max(0.0, top_inset)))

    # ── input on rows ──
    # The tint control's widget (chip / brush) at the row's left edge is
    # the chip's own click, never the row's - a double-click there must
    # not navigate.
    chip_x = rows_x + pad

    def row_at(point):
        if point is None or not (rows_x <= point[0] <= rows_x + content_w):
            return None
        if show_tint_chips and point[0] < chip_x + chip + px(2):
            return None
        index = int((point[1] - rows_y) // row_h)
        return index if 0 <= index < len(rows) else None

    selected_index = None
    if state.selected is not None:
        for i, (path, _is_dir) in enumerate(rows):
            if str(path) == state.selected:
                selected_index = i
                break

    hit = row_at(double_click)
    if hit is not None:
        return navigate(rows[hit][0])
    hit = row_at(click)
    if hit is not None:
        state.selected = str(rows[hit][0])
        selected_index = hit
        request_render()
    if ctrl_up_key_pressed and directory.parent != directory:
        return navigate(directory.parent)
    if enter_key_pressed and selected_index is not None:
        return navigate(rows[selected_index][0])
    if escape_key_pressed and state.selected is not None:
        state.selected = None
        request_render()
    step = (1 if down_key_pressed else 0) - (1 if up_key_pressed else 0)
    if step and rows:
        selected_index = (0 if selected_index is None and step > 0
                          else len(rows) - 1 if selected_index is None
                          else max(0, min(len(rows) - 1, selected_index + step)))
        state.selected = str(rows[selected_index][0])
        _scroll_row_into_view(draw_state, selected_index, row_h, rows_y - draw_state.abs_top
                              + draw_state.scroll_offset[1])
        request_render()

    # ── rows: viewport-culled, straight to the draw list ──
    clip = getattr(draw_state, "abs_clip_rect", None)
    meta = file_meta_store()
    row_bg = row_tint_bg()

    text_y_pad = (row_h - imgui.get_font_size()) * 0.5
    for i, (path, is_dir) in enumerate(rows):
        ry0 = rows_y + i * row_h
        ry1 = ry0 + row_h
        if clip is not None and (ry1 < clip[1] or ry0 > clip[3]):
            continue
        key = str(path)
        entry = meta.get(key) if meta is not None else None
        tint = FileMeta.painted_tint(entry)
        if tint:
            draw_list.add_rect_filled(rows_x, ry0, rows_x + content_w, ry1, row_bg(tint))
        row_hovered = hover_ok and rows_x <= mouse_x <= rows_x + content_w and ry0 <= mouse_y < ry1
        # Selection / hover are brighter steps of the row's own tint
        # (the default tint when unpainted); they stack.
        boost = ((select_boost if tint else plain_select_boost) if i == selected_index else 0.0) \
            + (hover_boost if row_hovered else 0.0)
        if i == selected_index and select_shadow:
            add_shadow((rows_x, ry0, content_w, row_h), offset=select_shadow,
                       corner_radius=px(select_rounding), clip=clip, draw_state=draw_state)
        if boost:
            draw_list.add_rect_filled(rows_x, ry0, rows_x + content_w, ry1,
                                      row_bg(tint or default_tint, boost),
                                      rounding=px(select_rounding))
        icon = row_icon(path, is_dir, entry, folder_icon, file_icon)
        if tint:
            name_col = tinted_text(folder_rgba if is_dir else text_rgba, tint, text_mix)
            icon_col = tinted_text(text_rgba, tint, text_mix)
        else:
            name_col = icon_col = folder_col if is_dir else text_col
        draw_list.add_text(rows_x + text_x, ry0 + text_y_pad, icon_col, icon)
        draw_list.add_text(rows_x + text_x + glyph_w, ry0 + text_y_pad, name_col, path.name)
        if show_tint_chips:
            swatch = None
            if tint:
                swatch = chip_swatch(tint, row_bg.rgb(tint, boost), chip_mix)
            tint_control(draw_state, key, tint, chip_x, ry0 + (row_h - chip) * 0.5, chip,
                         ry0 + text_y_pad, row_hovered, default_tint, swatch=swatch,
                         show_brush=i == selected_index)

    return False, input_value


def _scroll_row_into_view(draw_state, index, row_h, rows_top):
    """Nudge the window's scroll the minimal amount so row `index` (at
    `rows_top` + index × row_h in content coordinates) is fully visible."""
    view_h = draw_state.abs_clipped_height - draw_state.header_height - draw_state.footer_height
    if view_h <= 0:
        return
    sx, sy = draw_state.scroll_offset
    row_top = rows_top + index * row_h
    row_bottom = row_top + row_h
    new_sy = sy
    if row_bottom > new_sy + view_h:
        new_sy = row_bottom - view_h
    if row_top < new_sy:
        new_sy = row_top
    new_sy = max(0.0, new_sy)
    if new_sy != sy:
        draw_state.scroll_offset = (sx, new_sy)
        draw_state.invalidate()


# ── the explorer: shortcuts + listing ───────────────────────────────────────
@render_func(tint=(0.32, 0.42, 0.54), selectable=False, disable_scroll=True,
             show_add_delete=False, is_tree=False, show_bg=False, shadow=False)
def draw_fast_file_explorer(input_value: str, draw_state, column_edges=None,
                            left_mouse_down=False, ctrl_up_key_pressed=False,
                            shortcuts_width=190.0, shortcut_row_height=22.0, column_gap=6.0,
                            show_tint_chips=True, chip_size=17.0, default_tint=(0.32, 0.42, 0.54, 1.0),
                            **kwargs):
    """A ColumnLayout with two cells: the shortcuts (draw-list rows, a click
    navigates) and `draw_file_listing`, sharing one draggable edge
    (`column_edges`, persisted by auto-state). Returns what the listing
    returns; Ctrl+Up works from anywhere over the explorer. Shortcut rows
    wear their directory's tint like the listing's, with the same leading
    tint control (`show_tint_chips`, `chip_size`, `default_tint`)."""
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
    hover_boost = 0.08
    # [tint=(0.55, 0.72, 0.95)]
    text_mix = 0.3

    px = Melty.px
    directory = Path(input_value if input_value else Path.home()).expanduser()
    draw_list = imgui.get_window_draw_list()
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    click = ((left_mouse_down.x, left_mouse_down.y)
             if (left_mouse_down and hasattr(left_mouse_down, "x")) else None)
    chip = px(chip_size) if show_tint_chips else 0.0
    text_x = px(left_pad) + (chip + px(6) if show_tint_chips else 0.0)
    meta = file_meta_store()
    row_bg = row_tint_bg()

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
        x, y = imgui.get_cursor_screen_pos()
        row_h = px(shortcut_row_height)
        shortcuts = shortcut_directories()
        # The shortcut that holds the current directory: the deepest one.
        current = None
        for label, path in shortcuts:
            if path == directory or path in directory.parents:
                if current is None or len(path.parts) > len(current.parts):
                    current = path
        text_y_pad = (row_h - imgui.get_font_size()) * 0.5
        chip_x = x + px(left_pad)
        # The tint control's column is the chip's own width, never the row's.
        click_left = chip_x + chip + px(2) if show_tint_chips else x
        for i, (label, path) in enumerate(shortcuts):
            ry0 = y + i * row_h
            ry1 = ry0 + row_h
            key = str(path)
            tint = FileMeta.painted_tint(meta.get(key)) if meta is not None else None
            if tint:
                draw_list.add_rect_filled(x, ry0, x + width, ry1, row_bg(tint), rounding=px(4))
            row_hovered = hover_ok and x <= mouse_x < x + width and ry0 <= mouse_y < ry1
            # The current shortcut: its tint (default when unpainted),
            # brighter, lifted off the row by a small shadow; hover is a
            # further brightening of the same tint.
            boost = ((0.22 if tint else 0.06) if path == current else 0.0) \
                + (hover_boost if row_hovered else 0.0)
            if path == current:
                add_shadow((x, ry0, width, row_h), offset=2.0, corner_radius=px(4),
                           clip=getattr(draw_state, "abs_clip_rect", None), draw_state=draw_state)
            if boost:
                draw_list.add_rect_filled(x, ry0, x + width, ry1,
                                          row_bg(tint or default_tint, boost), rounding=px(4))
            icon = computer_icon if path == Path(os.sep) else folder_icon
            label_col = tinted_text(text_rgba, tint, text_mix) if tint else text_col
            draw_list.add_text(x + text_x, ry0 + text_y_pad, label_col, icon)
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
        imgui.dummy(width, len(shortcuts) * row_h)
    with columns.cell(1, height=body_height) as width:
        changed, value = draw_file_listing(str(directory), name="listing", width=width,
                                           height=body_height, disable_scroll=False)
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
