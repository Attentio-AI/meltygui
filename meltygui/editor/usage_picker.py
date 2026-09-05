"""The Ctrl+B usage-jump picker — GlobalSearch's Code tab, as a popover.

Rows are a TREE, exactly the Code tab's: the file (depth 0, icon + name,
FileMeta tint), then every enclosing class / def of a hit as its own row
(the def line, painted like the editor paints it), then the usage line
itself. EVERY row is selectable — a scope row jumps to that definition, the
file row opens the file — and the keyboard cursor lands on the BEST match
(the first target the resolver returned) rather than row 0, so Enter still
does what the flat list did.

Rows are built ONCE when the picker opens (`build_usage_rows`: roster
tables for the scope chains, `CodeLineTints` for the washes) and painted
every frame with `draw_code_line_fast` — a few draw-list calls per row, no
per-row editor body. The window is `draw_usage_picker`, a latched popover
with the same shell as `draw_dd_menu` (called every frame with `closed=`
toggled by draw_text, which owns the keys and the focus gate).
"""
import os
import re
from pathlib import Path

import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import UsageRef
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.code_line_fast import (
    CodeLineTints, draw_code_line_fast, pop_code_font, push_code_font, shift_spans)
from src.lsd.gl_gui.view.core_views.core_render import render_func

# Row pitch shared with the scroll-into-view helper — the Code tab's 24 + 2.
ROW_H = 24.0
ROW_GAP = 2.0
ROW_PITCH = ROW_H + ROW_GAP


class UsageRow:
    """One picker row. `kind` is "file" / "scope" / "site"; `ref` is the
    UsageRef a pick jumps to (None for the file row — it opens the file),
    `token` the symbol spelling the caret lands on; `text` is the display
    line (stripped), `block_tint` / `spans` its washes in display columns;
    `tint` the ROW's own tint (file: FileMeta, scope: the definition's)."""
    __slots__ = ("kind", "depth", "path", "line", "text", "tint", "ref", "token",
                 "block_tint", "spans", "label", "emphasis")

    def __init__(self, kind, depth, path, line, text, tint=None, ref=None,
                 token=None, block_tint=None, spans=(), label="", emphasis=None):
        self.kind = kind
        self.depth = depth
        self.path = path
        self.line = line
        self.text = text
        self.tint = tint
        self.ref = ref
        self.token = token
        self.block_tint = block_tint
        self.spans = spans
        self.label = label
        # Display columns of the searched symbol on a site row - painted at
        # full brightness while the rest of the line is dimmed; () dims the
        # whole line (a def scope row), None dims nothing.
        self.emphasis = emphasis


class UsagePickerModel:
    """What draw_text hands the picker window: the rows, the keyboard cursor
    (`index`), the hover-vs-keyboard highlight mode, and the row a mouse
    click picked (`picked`, consumed by draw_text). Plain object — draw_text
    change-gates the popover's repaint on (rows, index, kbd_mode)."""
    __slots__ = ("rows", "all_rows", "index", "kbd_mode", "picked", "_last_mouse",
                 "hover", "measured", "chrome_h")

    def __init__(self):
        self.rows = []
        self.all_rows = []
        self.index = 0
        self.kbd_mode = True
        self.picked = None
        self._last_mouse = None
        self.hover = None
        # (frame, row height) the body laid out) - the sizing reads the
        # wrapper's content rect only when it was measured this frame.
        self.measured = None
        # Window chrome above + below the rows (content rect - rows height),
        # learned from a window measure; sizes the rows without one.
        self.chrome_h = 8.0

    def set_rows(self, rows, best_index=0, max_rows=None):
        """Install `rows`; past `max_rows` the list is cut (never above the
        best row) and ends in a selectable "+ N more" row — `expand()`."""
        self.all_rows = rows
        if max_rows is not None and max_rows > 0 and len(rows) > max_rows:
            keep = max(max_rows, best_index + 1)
            if keep < len(rows):
                hidden = len(rows) - keep
                rows = rows[:keep] + [UsageRow("more", 0, None, 0,
                                               f"+ {hidden} more", label=str(hidden))]
        self.rows = rows
        self.index = max(0, min(best_index, len(rows) - 1)) if rows else 0
        self.kbd_mode = True
        self.picked = None
        self.hover = None

    def expand(self):
        """List every row (the "+ N more" row was picked); the cursor stays
        on the first newly listed row."""
        if self.rows is self.all_rows:
            return
        cut = len(self.rows) - 1
        self.rows = self.all_rows
        self.index = max(0, min(cut, len(self.rows) - 1))
        self.kbd_mode = True
        self.picked = None

    def signature(self):
        return (id(self.rows), self.index, self.kbd_mode, self.hover)

    def current(self):
        rows = self.rows
        return rows[self.index] if 0 <= self.index < len(rows) else None


# ── row builder ─────────────────────────────────────────────────────────────

class _Node:
    __slots__ = ("entry", "children", "sites")

    def __init__(self, entry):
        self.entry = entry
        self.children = {}   # qualname -> _Node, first-appearance order
        self.sites = []      # UsageRefs, first-appearance order


def _display_name(path):
    """The file's name relative to the project's src tree ("core_views/
    text_editor.py"), the bare name when the path isn't under it."""
    p = str(path)
    marker = f"{os.sep}src{os.sep}"
    i = p.rfind(marker)
    return p[i + len(marker):] if i != -1 else os.path.basename(p)


def _symbol_columns(disp, token, column=None):
    """Column spans of `token` (an identifier) in the display line `disp`:
    the occurrence at `column` when one starts there, else every whole-word
    occurrence. Empty when the token isn't on the line."""
    if not token:
        return []
    if column is not None and 0 <= column < len(disp) and disp.startswith(token, column):
        before = disp[column - 1] if column > 0 else ""
        after = disp[column + len(token)] if column + len(token) < len(disp) else ""
        if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
            return [(column, column + len(token))]
    pat = re.compile(r"(?<![\w])" + re.escape(token) + r"(?![\w])")
    return [(m.start(), m.end()) for m in pat.finditer(disp)]


def build_usage_rows(targets, names, tints=None):
    """Rows for `targets` (UsageRefs, best first) grouped file → scope chain
    → usage line, in first-appearance order. `names` maps ref → the symbol
    spelling for the caret. Returns (rows, best_index) — best_index is the
    row of targets[0]."""
    from src.lsd.gl_gui.view.core_conversion import symbol_roster as roster
    from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    from src.lsd.gl_gui.view.core_views.text_editor import _uj_file_tint
    tints = tints if tints is not None else CodeLineTints()
    files = {}   # normalized path -> (path_str, text, lines, table, root _Node)
    for ref in targets:
        p = getattr(ref, "path", None)
        if p is None:
            continue
        key = roster._norm(str(p))
        ent = files.get(key)
        if ent is None:
            try:
                text = PendingSave.current_file_text(Path(str(p)))
            except Exception:
                text = None
            text = text if isinstance(text, str) else ""
            try:
                table = roster.table_for(key)
            except Exception:
                table = None
            ent = files[key] = (str(p), text, text.split("\n"), table, _Node(None))
        _path, _text, _lines, table, root = ent
        node = root
        if table is not None:
            chain = []
            scope = table.scope_at(ref.line)
            while scope is not None:
                chain.append(scope)
                scope = (table.by_qualname.get(scope.parent)
                         if scope.parent else None)
            for entry in reversed(chain):
                child = node.children.get(entry.qualname)
                if child is None:
                    child = node.children[entry.qualname] = _Node(entry)
                node = child
        node.sites.append(ref)

    rows, best = [], 0
    first = targets[0] if targets else None

    def line_row(kind, depth, path, text, lines, line, ref, token, tint=None):
        raw = lines[line - 1] if 0 < line <= len(lines) else ""
        disp = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        block, spans = tints.tints(path, text, line)
        spans = shift_spans(spans, indent)
        # A site row lights the caret symbol; a scope row is context.
        if kind == "site":
            col = getattr(ref, "column", None)
            emphasis = _symbol_columns(disp, token,
                                       None if col is None else col - indent)
        else:
            emphasis = ()
        return UsageRow(kind, depth, path, line, disp, tint=tint, ref=ref,
                        token=token, block_tint=block, spans=spans, emphasis=emphasis)

    def emit(node, depth, path, text, lines):
        nonlocal best
        entry = node.entry
        site_lines = {getattr(r, "line", None) for r in node.sites}
        if entry is not None:
            # A target ON the def line IS this scope: one row, the site's ref.
            own = next((r for r in node.sites if r.line == entry.line), None)
            ref = own or UsageRef(Path(path), entry.line, entry.col,
                                  scope=entry.parent or "<module>")
            token = names.get(own) if own is not None else entry.name
            if own is not None and own is first:
                best = len(rows)
            rows.append(line_row("site" if own is not None else "scope", depth,
                                 path, text, lines, entry.line, ref,
                                 token or entry.name, tint=entry.tint))
            depth += 1
        for ref in node.sites:
            if entry is not None and ref.line == entry.line:
                continue
            if ref is first:
                best = len(rows)
            rows.append(line_row("site", depth, path, text, lines, ref.line,
                                 ref, names.get(ref)))
        for child in node.children.values():
            emit(child, depth, path, text, lines)

    for _key, (path, text, lines, _table, root) in files.items():
        rows.append(UsageRow("file", 0, path, 1, _display_name(path),
                             tint=_uj_file_tint(path), label=path))
        emit(root, 1, path, text, lines)
    return rows, best


# ── picker window ───────────────────────────────────────────────────────────────

def _same_tint(a, b):
    """True when two rgb tints are the same colour (None never matches)."""
    if a is None or b is None:
        return False
    return all(abs(float(x) - float(y)) < 0.005 for x, y in zip(a[:3], b[:3]))


# Popover size: fitted to the rows ONCE, on open (width = the remembered
# drag width, TextEditorState.usage_picker_width, when there is one); after
# that the handle resizes it freely, constrained only by the content height
# (every row + the "+ N more" row) measured in draw_text's call.
PICKER_MIN_W = 680
PICKER_MIN_H = 33


def _fresh_rect(menu_ds, model):
    """The wrapper's content rect, only when the body measured it THIS frame
    — a latched popover keeps the rect of its last open (a longer list
    sized a 4-row picker to 500 px, 09-04). A fresh rect also teaches the
    model its chrome height."""
    if menu_ds is None or model is None or model.measured is None:
        return None
    frame, rows_h = model.measured
    if frame != Melty.frame_count:
        return None
    rect = getattr(menu_ds, "_content_rect", None)
    if not rect or rect[0] <= 0 or rect[1] <= 0:
        return None
    if rect[1] >= rows_h:
        model.chrome_h = rect[1] - rows_h
    return rect


def picker_content_height(menu_ds, model):
    """The popover's whole content: every row (the "+ N more" row included)
    plus the window chrome — the ceiling of its height."""
    rect = _fresh_rect(menu_ds, model)
    if rect is not None:
        return max(PICKER_MIN_H, rect[1])
    n = len(model.rows) if model is not None else 0
    chrome = model.chrome_h if model is not None else 8.0
    return max(PICKER_MIN_H, n * ROW_PITCH + chrome)


def picker_fit(menu_ds, model):
    """The popover size that fits its rows, display-clamped: the fresh
    content rect's width (else the minimum) and the content height."""
    from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int
    display_w, display_h = imgui.get_io().display_size
    rect = _fresh_rect(menu_ds, model)
    fit_w = snap_int(max(min(rect[0] if rect else PICKER_MIN_W, display_w), PICKER_MIN_W))
    fit_h = snap_int(max(min(picker_content_height(menu_ds, model), display_h),
                         PICKER_MIN_H))
    return (fit_w, fit_h)


def scroll_row_into_view(menu_ds, row_index):
    """Minimal scroll of the picker window so `row_index` is fully visible
    (the dd-menu helper at this module's row pitch)."""
    from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
    _dd_scroll_cursor_into_view(menu_ds, row_index, pitch=ROW_PITCH)


@render_func(use_cache=True, show_bg=True, shadow=True, selectable=False, temp=True,
             closable=True, melty_window=False, auto_resize=False, with_header=None,
             min_width=PICKER_MIN_W, swoosh=False, min_height=PICKER_MIN_H,
             enforce_max_height=True,   # the content height cap holds mid-drag
             is_default_for=UsagePickerModel, tint=(0.071, 0.354, 0.511))
def draw_usage_picker(input_value: UsagePickerModel, draw_state, **kwargs):
    """Paint the picker rows Code-tab style. Hover moves the highlight only
    while the pointer MOVES over the window (a resting pointer never steals
    the keyboard cursor); a click on a row sets `model.picked` for draw_text
    to consume. Returns (True, model) on a pick."""
    from src.lsd.gl_gui.view.core_views.new_core_view import _dd_row_width
    # [tint=(0.9, 0.6, 0.2)] layout knobs — the Code tab's numbers
    ICON_COL = 22.0
    ICON_X = 4.0
    TREE_INDENT = 16.0
    GROUP_INSET = ICON_COL - 4.0
    file_icon = f""
    icon_alpha = 0.55
    suffix_alpha = 0.55
    row_bg_value, row_bg_hot = 0.045, 0.10
    group_bg_value, group_bg_step = 0.028, 0.012
    file_text_value, file_text_hot = 0.9, 1.5
    more_tint = (0.55, 0.72, 0.85)       # the Code tab's default blue
    more_text_value, more_text_hot = 0.5, 1.0
    text_saturation = 0.8
    sel_color = imgui.get_color_u32_rgba(0.88, 0.93, 1.0, 1.0)
    sel_thickness = 1.5
    hot_bg_untinted = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.07)
    suffix_color = imgui.get_color_u32_rgba(0.52, 0.55, 0.6, suffix_alpha)

    model = input_value
    rows = model.rows if model is not None else []
    sm = Melty.style_manager

    def _mix(tint, value, factor=0.8, sat=1.0, alpha=1.0):
        c = tint if (isinstance(tint, tuple) and len(tint) >= 3) else (0.5, 0.5, 0.5)
        col = sm.make_color_rgb(c[0], c[1], c[2], value=value, factor=factor,
                                saturation_scale=sat)
        return imgui.get_color_u32_rgba(col[0], col[1], col[2], alpha)

    draw_list = imgui.get_window_draw_list()
    origin = imgui.get_cursor_screen_pos()
    x0, y0 = origin[0], origin[1]
    width = _dd_row_width(draw_state)
    n = len(rows)
    total_h = n * ROW_PITCH
    if model is not None:
        model.measured = (Melty.frame_count, total_h if n else ROW_H)
    if n == 0:
        imgui.dummy(width, ROW_H)
        return False, model

    # Visible band of the (scrolling) window - rows outside it keep their
    # geometry and skip their paint.
    band_top = draw_state._abs_top()
    band_bot = band_top + (draw_state.height or 0)

    # ── hover vs keyboard highlight ──
    mouse = imgui.get_mouse_pos()
    over = (getattr(draw_state, "_bounding_hovered", False)
            and x0 <= mouse[0] < x0 + width and band_top <= mouse[1] < band_bot)
    last = model._last_mouse
    moved = last is not None and (abs(mouse[0] - last[0]) > 0.5
                                  or abs(mouse[1] - last[1]) > 0.5)
    model._last_mouse = (mouse[0], mouse[1])
    hover = None
    if over:
        hi = int((mouse[1] - y0) // ROW_PITCH)
        if 0 <= hi < n and (mouse[1] - y0) - hi * ROW_PITCH < ROW_H:
            hover = hi
    if not over:
        model.kbd_mode = True
    elif moved and hover is not None:
        model.kbd_mode = False
    model.hover = hover if not model.kbd_mode else None
    if not model.kbd_mode and hover is not None:
        model.index = hover   # Enter switches from the hovered row

    pushed, char_w, line_h = push_code_font()
    try:
        row_y = [y0 + i * ROW_PITCH for i in range(n)]
        depths = [r.depth for r in rows]

        # ── group background: a tinted scope's rows float in its wash ──
        for i, row in enumerate(rows):
            d = depths[i]
            j = i + 1
            while j < n and depths[j] > d:
                j += 1
            if j == i + 1 or row.tint is None:
                continue
            gy0, gy1 = row_y[i], row_y[j - 1] + ROW_H
            if gy1 < band_top or gy0 > band_bot:
                continue
            gx0 = x0 + d * TREE_INDENT + GROUP_INSET
            add_shadow((gx0, gy0, x0 + width - gx0, gy1 - gy0),
                       offset=2.0 + 2.0 * d, corner_radius=4.0)
            draw_list.add_rect_filled(gx0, gy0, x0 + width, gy1,
                                      _mix(row.tint, group_bg_value + d * group_bg_step),
                                      rounding=4.0)

        picked = None
        # The tint a row's background ALREADY wears: its own, else the
        # nearest tinted ancestor's (its group block or under the row).
        # A code line's block wash and symbol washes in that same colour
        # would just double it — they are dropped below (Lukas 09-04).
        painted_stack = []   # (depth, painted tint)
        for i, row in enumerate(rows):
            ry = row_y[i]
            while painted_stack and painted_stack[-1][0] >= depths[i]:
                painted_stack.pop()
            painted = row.tint if row.tint is not None else (
                painted_stack[-1][1] if painted_stack else None)
            painted_stack.append((depths[i], painted))
            if ry + ROW_H < band_top or ry > band_bot:
                continue
            ind = depths[i] * TREE_INDENT
            hot = (i == model.index) if model.kbd_mode else (i == hover)
            own = row.tint is not None
            is_file = row.kind == "file"
            rx = x0 + ind + (ICON_COL if is_file else GROUP_INSET)
            # Offset the row's shadow by depth depth so nested rows stack.
            if hot and not own:
                draw_list.add_rect_filled(rx, ry, x0 + width, ry + ROW_H,
                                          hot_bg_untinted, rounding=4.0)
            elif hot or own:
                if own and not is_file:
                    add_shadow((rx, ry, x0 + width - rx, ROW_H),
                               offset=2.0 + 2.0 * depths[i], corner_radius=4.0)
                draw_list.add_rect_filled(rx, ry, x0 + width, ry + ROW_H,
                                          _mix(row.tint, row_bg_hot if hot else row_bg_value),
                                          rounding=4.0)
            text_y = ry + (ROW_H - line_h) * 0.5
            text_x = rx + 8.0
            if row.kind == "more":
                # The "+ N more" row: Code-tab style, selectable like any row.
                if hot:
                    draw_list.add_rect_filled(rx, ry, x0 + width, ry + ROW_H,
                                              hot_bg_untinted, rounding=4.0)
                draw_list.add_text(x0 + ICON_COL + 8.0, text_y,
                                   _mix(more_tint, more_text_hot if hot else more_text_value),
                                   row.text)
            elif is_file:
                tv = file_text_hot if hot else file_text_value
                draw_list.add_text(x0 + ICON_X + ind, text_y,
                                   _mix(row.tint, tv, sat=text_saturation, alpha=icon_alpha),
                                   file_icon)
                draw_list.add_text(text_x, text_y, _mix(row.tint, tv, sat=text_saturation),
                                   row.text)
            else:
                suffix = str(row.line)
                suffix_w = imgui.calc_text_size(suffix)[0]
                code_w = max(60.0, x0 + width - 8.0 - suffix_w - 12.0 - text_x)
                block = None if _same_tint(row.block_tint, painted) else row.block_tint
                spans = [sp for sp in row.spans
                         if not (_same_tint(sp[2], painted) or _same_tint(sp[2], block))]
                # The focus symbol pops: full brightness on it, the rest of
                # the line (and whole context rows) faded like the diff
                # collapse preview; the hot line reads in full.
                draw_code_line_fast(draw_list, text_x, text_y, row.text, char_w, line_h,
                                    max_width=code_w, block_tint=block, spans=spans,
                                    emphasis=None if hot else row.emphasis)
                draw_list.add_text(x0 + width - 8.0 - suffix_w, text_y, suffix_color, suffix)
            if i == model.index:
                draw_list.add_rect(rx, ry, x0 + width, ry + ROW_H, sel_color,
                                   rounding=4.0, thickness=sel_thickness)
            # Click to pick: on_action keeps the honest z-order (a window in
            # front blocks clicks) and replays on multiple hits.
            if draw_state.on_action("left_mouse_down", view_id=f"uj_row_{i}",
                                    rect=(x0, ry, x0 + width, ry + ROW_H),
                                    priority_delta=4) is not None:
                picked = row
    finally:
        pop_code_font(pushed)
    imgui.set_cursor_screen_pos((x0, y0))
    imgui.dummy(width, total_h)
    if picked is not None:
        model.picked = picked
        model.index = rows.index(picked)
        return True, model
    return False, model
