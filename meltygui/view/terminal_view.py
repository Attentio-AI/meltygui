"""Terminal view functions and supporting definitions."""
from meltygui.core.services.terminal_runtime import TerminalRuntime
from meltygui.model.terminal_model import Terminal
from meltygui.core.core_render import render_func
from meltygui.state.terminal_state import TerminalScreenState
from meltygui.core.runtime.toggles import Toggles
import meltygui_imgui as imgui
import re
from meltygui.hdr_color import pack_color
from meltygui.core.styling.fonts import Font
from meltygui.state.terminal_state import _norm


_COL_ERR = (1.0, 0.45, 0.40)


_PALETTE = {
    'black': (0.0, 0.0, 0.0), 'red': (0.86, 0.30, 0.30), 'green': (0.34, 0.78, 0.40),
    'brown': (0.80, 0.66, 0.28), 'blue': (0.38, 0.56, 0.94), 'magenta': (0.83, 0.46, 0.83),
    'cyan': (0.33, 0.80, 0.86), 'white': (0.85, 0.85, 0.85),
    'brightblack': (0.46, 0.48, 0.52), 'brightred': (1.0, 0.46, 0.43),
    'brightgreen': (0.52, 0.92, 0.54), 'brightbrown': (0.96, 0.86, 0.42),
    'brightblue': (0.56, 0.70, 1.0), 'brightmagenta': (0.96, 0.62, 0.96),
    'brightcyan': (0.56, 0.93, 0.96), 'brightwhite': (1.0, 1.0, 1.0),
}


_DEFAULT_FG = (0.85, 0.85, 0.85)


_DEFAULT_BG = None  # None == transparent, let the window background show through


_SEL_COLOR = pack_color(51 / 255, 102 / 255, 204 / 255, 102 / 255)   # pale blue wash


def _pack(rgb, alpha=255):
    return pack_color(rgb[0], rgb[1], rgb[2], alpha / 255.0)


def _resolve(name, default, bold=False):
    if name in (None, 'default'):
        rgb = default
    elif name in _PALETTE:
        rgb = _PALETTE[name]
    elif isinstance(name, str) and len(name) == 6:
        try:
            rgb = (int(name[0:2], 16) / 255, int(name[2:4], 16) / 255, int(name[4:6], 16) / 255)
        except ValueError:
            rgb = default
    else:
        rgb = default
    if bold and rgb is not None:
        rgb = tuple(min(1.0, c * 1.25 + 0.08) for c in rgb)
    return rgb


def _wheel_lines(visible_px, line_px):
    """Lines to scroll per wheel tick, from the global Toggles scroll settings — the
    same pixel model core_render uses (`scroll_speed` px, capped to a fraction of the
    visible height so small views don't overshoot), converted to lines."""
    px = min(Toggles.ScrollSettings.scroll_speed,
             Toggles.ScrollSettings.max_increment_fraction
             * max(1.0, visible_px))
    return max(1, int(round(px / max(1.0, line_px))))


def _row_blank(line, ncols):
    return all(line[x].data in (' ', '', '\x00') for x in range(ncols))


_LINK_RE = re.compile(
    r'File "(?P<p1>[^"\n]+)", line (?P<l1>\d+)'
    r'|(?<![\w./~-])(?P<p2>(?:/|~/|\./|\.\./)[^\s:"\'\)\],]+\.[A-Za-z0-9_]+):(?P<l2>\d+)')


_LINK_COLOR = pack_color(110 / 255, 180 / 255, 1.0, 200 / 255)      # underline (cyan-blue)


_LINK_HOVER_COLOR = pack_color(170 / 255, 235 / 255, 1.0, 1.0)      # brighter on hover


def _find_links(grid, scols):
    """Scan the visible grid for file:line references, joining wrapped rows so a path
    split across the terminal width still matches. Returns [(path, line, segments)]
    where segments is [(row, col_start, col_end)] (a link can span wrapped rows)."""
    if not grid:
        return []
    # Flatten visible rows into one string + a char ->-(row,col) origin map, NOT
    # inserting a newline where a row wrapped (pyte fills the next row when it wraps).
    parts, origins = [], []
    for r, row in enumerate(grid):
        text = "".join((c[0] or ' ') for c in row)
        end = len(text.rstrip())
        for c in range(end):
            parts.append(text[c])
            origins.append((r, c))
        wrapped = scols > 0 and row[scols - 1][0] not in (' ', '', '\x00')
        if not wrapped:
            parts.append('\n')
            origins.append((r, end))
    text = "".join(parts)

    links = []
    for m in _LINK_RE.finditer(text):
        if m.group('p1') is not None:
            path, line = m.group('p1'), int(m.group('l1'))
        else:
            path, line = m.group('p2'), int(m.group('l2'))
        by_row = {}
        for (r, c) in origins[m.start():m.end()]:
            lo, hi = by_row.get(r, (c, c))
            by_row[r] = (min(lo, c), max(hi, c))
        segments = [(r, lo, hi + 1) for r, (lo, hi) in sorted(by_row.items())]
        links.append((path, line, segments))
    return links



@render_func(is_default_for=(Terminal), show_bg=False, show_header=False, show_name=False, is_tree=False,
             selectable=False, disable_scroll=True, initial={"closed": False})
def draw_terminal_screen(input_value: Terminal, draw_state, view_state: TerminalScreenState,
                         left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False,
                         left_mouse_clicked=False, cursor_moved=None,
                         cursor_hover_exit=None, scroll_y_changed=None,
                         terminal_runtime: TerminalRuntime = None, font_manager=None):
    from meltygui.core.services.terminal_core import _ALT_SCREEN_MODES
    from meltygui.core.services.terminal_core import _MOUSE_MODES
    from meltygui.core.services.terminal_core import _TMUX_WHEEL_LINES
    from meltygui.core.services.terminal_core import _mouse_seq

    term, ds, vs = input_value, draw_state, view_state
    left, top, right, bottom = ds.abs_left, ds.abs_top, ds.abs_left + ds.width, ds.abs_top+ ds.height
    pad = 4.0

    font = font_manager.get(Font.JETBRAINS_MONO_19) if font_manager else None
    pushed = font is not None
    if pushed:
        imgui.push_font(font)
    try:
        char_w, line_px = max(1.0, imgui.calc_text_size("0").x), imgui.get_text_line_height() * 1.25
        x0, y0 = left + pad, top + pad
        # Logical terminal size is the available body, but never SMALLER than the
        # configured minimum (Toggles.TerminalSettings, in px). Below that floor the PTY
        # grid stays frozen at the minimum - line wrapping stops re-flowing - and the
        # clip is pushed before the draw call crops any overflow instead of rewrapping
        # to an unusably narrow grid.
        avail_w = max(right - left - 2 * pad,
                      Toggles.TerminalSettings.min_width)
        avail_h = max(bottom - top - 2 * pad,
                      Toggles.TerminalSettings.min_height)
        cols = max(2, int(avail_w / char_w))
        rows = max(2, int(avail_h / line_px))

        terminal_runtime.prepare(term, cols, rows)
        if term.error:
            imgui.set_cursor_screen_pos((x0, y0))
            imgui.text_colored(term.error, _COL_ERR[0], _COL_ERR[1], _COL_ERR[2], 1.0)
            return False, term
        # start() now spawns the PTY on a bg thread, so screen is None until it's wired.
        # Show a placeholder and bail (the reader invalidates this tile when ready).
        if term.screen is None:
            imgui.set_cursor_screen_pos((x0, y0))
            imgui.text_colored("starting…", 0.55, 0.55, 0.55, 1.0)
            return False, term
        is_focused = terminal_runtime.focused
        for event in (cursor_moved, scroll_y_changed, left_mouse_down,
                      left_mouse_drag, left_mouse_held, left_mouse_clicked):
            if event:
                vs.pointer = (event.x, event.y)
        if cursor_hover_exit:
            vs.pointer = None
        mouse_x, mouse_y = vs.pointer if vs.pointer is not None else (-1e9, -1e9)
        wheel = scroll_y_changed.value if scroll_y_changed else 0

        srows, scols, cursor, modes, hist, buf_rows = term.snapshot()
        cur_x, cur_y, cur_hidden = cursor

        # When the grid is taller than the window body (window dragged below min_height,
        # so `rows` are frozen), crop from the TOP rather than the BOTTOM: shift the
        # origin up so the last grid rows - the live prompt + cursor - stay pinned to the
        # window's bottom edge, and the blank rows above slide out of the clip. No
        # overflow -> the min() keeps the normal top-anchored origin.
        y0 = min(top + pad, (bottom - pad) - srows * line_px)

        # Compose history-above + the live screen. We read history.top directly rather
        # than driving pyte's pop_page/next_page, which rewrites the live screen and
        # corrupts both scrollback and the program (the "mangled off-screen text").
        all_lines = hist + buf_rows
        total = len(all_lines)

        # Growing the window makes pyte pad blank rows at the BOTTOM of the buffer,
        # leaving the prompt floating above a big empty block. Outside the alternate
        # screen, anchor the view's bottom to the last content row and pull scrollback
        # up to fill it, so the prompt stays pinned to the bottom like a real terminal.
        # In the alternate screen (vim, Claude) the program owns the grid - leave it.
        trailing = 0
        if not (_ALT_SCREEN_MODES & modes):
            for line in reversed(buf_rows):
                if _row_blank(line, scols):
                    trailing += 1
                else:
                    break
        content_bottom = total - 1 - trailing
        max_scroll = max(0, content_bottom - srows + 1)

        # Hold the viewport steady when output grows while the user is scrolled up.
        if vs.last_total is not None and total > vs.last_total and vs.scroll > 0:
            vs.scroll += total - vs.last_total
        vs.last_total = total
        over = left <= mouse_x <= right and top <= mouse_y <= bottom
        if over and wheel:
            lines = _wheel_lines(bottom - top, line_px)   # speed = Toggles.ScrollSettings
            if (_MOUSE_MODES & modes) and (_ALT_SCREEN_MODES & modes):
                # Forward the wheel to the program ONLY when it's an ALT-SCREEN program
                # that owns the grid (vim/Claude/emacs) - it has its own scrollback and
                # our pyte history is unused there. Crucially NOT for a plain shell under
                # tmux `mouse on`: forwarding a wheel-up there makes tmux enter copy-mode
                # (`[0/0]` when there's no content selection) and swallow keystrokes on copy-mode
                # nav - "can't copy" when you press q. A plain shell falls to the else
                # branch and scrolls OUR pyte history instead (clipped, so a no-log
                # terminal simply doesn't scroll). Button 64 = wheel up, 65 = wheel down;
                # tmux scrolls _TMUX_WHEEL_LINES per escape, so send enough to hit `lines`.
                col = max(0, min(int((mouse_x - x0) / char_w), scols - 1))
                row = max(0, min(int((mouse_y - y0) / line_px), srows - 1))
                btn = 64 if wheel > 0 else 65
                ticks = abs(int(round(wheel))) or 1
                for _ in range(ticks * max(1, round(lines / _TMUX_WHEEL_LINES))):
                    term.write(_mouse_seq(modes, btn, col, row))
            else:
                vs.scroll += int(round(wheel * lines))
        vs.scroll = max(0, min(vs.scroll, max_scroll))
        at_bottom = vs.scroll == 0

        start = max(0, content_bottom - vs.scroll - srows + 1)
        visible = all_lines[start:start + srows]
        cursor_vy = (len(hist) + cur_y) - start   # cursor's row within the visible window
        grid = [[(line[x].data, line[x].fg, line[x].bg, line[x].bold, line[x].reverse)
                 for x in range(scols)] for line in visible]

        # Clickable file:line links in the visible text (underlined, click to jump).
        links = _find_links(grid, scols)
        hovered_link = None
        if over:
            hc = int((mouse_x - x0) / char_w)
            hr = int((mouse_y - y0) / line_px)
            for i, (_p, _l, segs) in enumerate(links):
                if any(r == hr and lo_c <= hc < hi_c for (r, lo_c, hi_c) in segs):
                    hovered_link = i
                    break


        # Per-row printed width (columns up to the last non-blank cell) and the last
        # row with any content. Selection clamps to these so a drag can't run off
        # the prompt into the empty area below, or past the text on a line.
        row_len = [next((i + 1 for i in range(len(row) - 1, -1, -1)
                         if row[i][0] not in (' ', '', '\x00')), 0)
                   for row in grid]
        last_row = max((y for y in range(len(grid)) if row_len[y] > 0), default=0)

        def xy_to_rc(px, py):
            r = max(0, min(int((py - y0) / line_px), last_row))
            c = max(0, min(int((px - x0) / char_w), row_len[r] if r < len(row_len) else 0))
            return (r, c)

        # --- mouse: focus + drag select (selection is ours, not sent to the program) ---
        if left_mouse_down:
            terminal_runtime.focus()
            is_focused = True
            vs.sel_anchor = vs.sel_active = xy_to_rc(mouse_x, mouse_y)
        if (left_mouse_drag or left_mouse_held) and vs.sel_anchor is not None:
            mx = left_mouse_drag.x if left_mouse_drag else mouse_x
            my = left_mouse_drag.y if left_mouse_drag else mouse_y
            vs.sel_active = xy_to_rc(mx, my)

        # A plain click (no drag) on a link -> jump to it in the IDE, off-thread like
        # draw_text's jump button. `left_mouse_clicked` fires only on click, not drag,
        # so this never fights with selection.
        if left_mouse_clicked:
            cc = int((mouse_x - x0) / char_w)
            cr = int((mouse_y - y0) / line_px)
            for path, line, segments in links:
                if any(r == cr and lo <= cc < hi for (r, lo, hi) in segments):
                    terminal_runtime.open_link(path, line)
                    break

        terminal_runtime.forward_keys(term, vs)

        # --- render: per-row style runs, selection highlight, block cursor ---
        # Clip to the body rect: when the grid is frozen at the min-size (window dragged
        # below min_width/min_height) the PTY is larger than the window, so the grid can
        # overrun the shrinking window - the clip keeps it from painting over neighbouring
        # windows.
        lo, hi = _norm(vs.sel_anchor, vs.sel_active)
        dl = imgui.get_window_draw_list()
        dl.push_clip_rect(left, top, right, bottom, True)
        for y, row in enumerate(grid):
            ry = y0 + y * line_px
            # selection highlight, clamped to this row's printed width so trailing
            # blanks (and empty rows) don't get washed.
            if lo is not None and lo != hi and lo[0] <= y <= hi[0]:
                cs = lo[1] if y == lo[0] else 0
                ce = hi[1] if y == hi[0] else row_len[y]
                ce = min(ce, row_len[y])
                if ce > cs:
                    dl.add_rect_filled(x0 + cs * char_w, ry, x0 + ce * char_w, ry + line_px, _SEL_COLOR)
            x = 0
            while x < len(row):
                style = row[x][1:]
                j = x + 1
                while j < len(row) and row[j][1:] == style:
                    j += 1
                data, fg, bg, bold, rev = row[x]
                if rev:
                    fg, bg = bg, fg
                bgc = _resolve(bg, _DEFAULT_BG)
                if bgc is not None:
                    dl.add_rect_filled(x0 + x * char_w, ry, x0 + j * char_w, ry + line_px, _pack(bgc))
                seg = "".join(c[0] for c in row[x:j])
                if seg.strip():
                    dl.add_text(x0 + x * char_w, ry, _pack(_resolve(fg, _DEFAULT_FG, bold)), seg)
                x = j

        # Underline clickable file:line links so they read as actionable; the link
        # under the mouse gets a brighter, thicker underline.
        for i, (_p, _l, segments) in enumerate(links):
            hot = i == hovered_link
            lc, th = (_LINK_HOVER_COLOR, 2.0) if hot else (_LINK_COLOR, 1.0)
            for (r, lo_c, hi_c) in segments:
                uy = y0 + r * line_px + line_px - 1.0
                dl.add_line(x0 + lo_c * char_w, uy, x0 + hi_c * char_w, uy, lc, th)

        if is_focused and at_bottom and not cur_hidden and 0 <= cursor_vy < srows:
            cx, cy = x0 + cur_x * char_w, y0 + cursor_vy * line_px
            dl.add_rect_filled(cx, cy, cx + char_w, cy + line_px, 0x88FFFFFF)
        dl.pop_clip_rect()
    finally:
        if pushed:
            imgui.pop_font()

    return False, term


@render_func(is_default_for=Terminal)
def draw_terminal(input_value: Terminal, draw_state):
    from meltygui.core.services.terminal_core import _draw_terminal_window

    return _draw_terminal_window(input_value, draw_state, "terminal_screen")


@render_func
def draw_session_terminal(input_value: Terminal, draw_state, max_bg_value=0.064):
    from meltygui.core.services.terminal_core import _draw_terminal_window

    return _draw_terminal_window(input_value, draw_state, "session_screen")
