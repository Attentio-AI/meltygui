"""A real terminal: an interactive shell running in a pseudo-terminal (PTY), with
its byte stream fed through a pyte VT100 emulator and the resulting screen grid
rendered each frame. Because the child sees a true tty (isatty() == True), full
interactive TUIs — Claude Code, vim, REPLs — launch and render here, not just
line-oriented `--print` tools.

Pipeline:
    keystrokes ── encode ──► os.write(master)
    os.read(master) ── pyte.ByteStream.feed ──► pyte.Screen grid ──► imgui draw

The shell owns line-editing, history, cd, prompts, job control — so this file is
just a PTY pump + a screen renderer + a key encoder, plus selection/copy on top.
"""
import fcntl
import os
import re
import select
import shlex
import struct
import subprocess
import termios
import threading
import time

import glfw
import imgui

from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
# Reuse the editor's GLFW-key -> character map (covers letters, digits, punctuation
# with shift pairs) to turn key events into the bytes the shell expects.
from src.lsd.gl_gui.view.core_views.text_editor import _KEY_CHAR_MAP
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


_COL_ERR = (1.0, 0.45, 0.40)

# Wait this long after the cursor size change before actually resizing the PTY. A
# window drag sweeps through many sizes; each TIOCSWINSZ sends SIGWINCH and the
# shell reprints its prompt, so resizing per-frame stacks duplicate prompts. We
# coalesce to a single resize once the size settles.
_RESIZE_SETTLE = 0.12


def _mouse_held():
    """True while either mouse button is physically down — a window-resize drag in
    progress (left = corner handle, right = right-drag resize). Read straight from
    GLFW rather than imgui.io, which isn't reliably populated under Melty's input."""
    w = Melty.glfw_window
    if w is None:
        return False
    return (glfw.get_mouse_button(w, glfw.MOUSE_BUTTON_LEFT) == glfw.PRESS
            or glfw.get_mouse_button(w, glfw.MOUSE_BUTTON_RIGHT) == glfw.PRESS)

# 16-colour ANSI palette (pyte hands us names for the basic colours, 6-hex strings
# for 256/true-colour). Tuned to read well on the dark window background.
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
_SEL_COLOR = (102 << 24) | (204 << 16) | (102 << 8) | 51   # translucent blue wash

# Special keys -> the escape sequences an xterm-based terminal sends for them.
_PTY_KEYS = {
    glfw.KEY_ENTER: b"\r", glfw.KEY_KP_ENTER: b"\r",
    glfw.KEY_BACKSPACE: b"\x7f", glfw.KEY_TAB: b"\t", glfw.KEY_ESCAPE: b"\x1b",
    glfw.KEY_UP: b"\x1b[A", glfw.KEY_DOWN: b"\x1b[B",
    glfw.KEY_RIGHT: b"\x1b[C", glfw.KEY_LEFT: b"\x1b[D",
    glfw.KEY_HOME: b"\x1b[H", glfw.KEY_END: b"\x1b[F",
    glfw.KEY_DELETE: b"\x1b[3~", glfw.KEY_INSERT: b"\x1b[2~",
    glfw.KEY_PAGE_UP: b"\x1b[5~", glfw.KEY_PAGE_DOWN: b"\x1b[6~",
}


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass




def _preexec():
    # New session + make the pty slave (fd 0) the controlling terminal, so the
    # child has a real session leader with job control - what an interactive shell
    # and the programs it launches expect.
    os.setsid()
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


_TERM_SCREEN_CLS = None


def _make_history_screen(pyte, cols, rows):
    """A pyte HistoryScreen that also implements SU/SD (CSI Ps S / CSI Ps T), which
    pyte omits entirely. tmux clears the screen (and redraws on scroll) by setting a
    scroll region and SCROLLING UP — `\\x1b[<n>S` — not `\\x1b[2J`. Without a handler
    pyte silently drops it, so the pane never clears; tmux's later writes assume a
    blank pane and overprint the stale rows (mangled output) until a resize forces a
    full redraw. We map S/T to index()/reverse_index() (scroll one line within the
    margins, which is exactly SU/SD).

    The CSI dispatch is compiled once from `pyte.Stream.csi` at stream construction, so
    S/T must be registered BEFORE the ByteStream is built (start() does so right after
    this call)."""
    global _TERM_SCREEN_CLS
    if _TERM_SCREEN_CLS is None:
        from pyte.screens import Margins

        class _TermScreen(pyte.HistoryScreen):
            def scroll_up(self, count=1):           # CSI Ps S
                count = count or 1
                sy, sx = self.cursor.y, self.cursor.x
                _, bottom = self.margins or Margins(0, self.lines - 1)
                self.cursor.y = bottom
                for _ in range(count):
                    self.index()                    # scrolls the region up by one
                self.cursor.y, self.cursor.x = sy, sx

            def scroll_down(self, count=1):          # CSI Ps T
                count = count or 1
                sy, sx = self.cursor.y, self.cursor.x
                top, _ = self.margins or Margins(0, self.lines - 1)
                self.cursor.y = top
                for _ in range(count):
                    self.reverse_index()            # scrolls the region down by one
                self.cursor.y, self.cursor.x = sy, sx

        if "S" not in pyte.Stream.csi:
            pyte.Stream.csi = {**pyte.Stream.csi, "S": "scroll_up", "T": "scroll_down"}
        _TERM_SCREEN_CLS = _TermScreen
    return _TERM_SCREEN_CLS(cols, rows, history=4000, ratio=0.5)


class Terminal:
    """A PTY-backed terminal session plus its pyte screen.

    `launch_cmd` is the argv spawned in the PTY — by default a `tmux attach`/create,
    so the processes live in a durable tmux server (they survive studio restarts and
    are shared with any external `tmux attach`). The reader thread feeds bytes into
    pyte under `lock`; the render thread reads the screen grid under the same `lock`."""

    def __init__(self, launch_cmd=None, tmux_session=None):
        self.launch_cmd = launch_cmd or [os.environ.get("SHELL", "/bin/bash"), "-i"]
        self.tmux_session = tmux_session   # session ID (if tmux-backed), for reference
        self.master_fd = None
        self.proc = None
        self.screen = None
        self.stream = None
        self.lock = threading.Lock()
        self.started = False
        self.size = (0, 0)         # (cols, rows) currently applied to the PTY
        self.pending_size = None   # last requested size, applied when it settles
        self.pending_at = 0.0
        self.error = None
        self._ds = None            # this terminal's window draw_state (set by render)
                                   # so the reader thread can invalidate it on new output

    def start(self, cols, rows):
        if self.started:
            return
        self.started = True
        self.size = (cols, rows)
        try:
            import pyte
        except ImportError:
            self.error = "pyte not installed — run: pip install pyte"
            return
        try:
            self.screen = _make_history_screen(pyte, cols, rows)
            self.stream = pyte.ByteStream(self.screen)
            master, slave = os.openpty()
            self.master_fd = master
            _set_winsize(master, rows, cols)
            # Unset TMUX so a `tmux attach` child can launch even when the studio is
            # itself launched inside tmux (otherwise tmux refuses to nest).
            env = dict(os.environ, TERM="xterm-256color",
                       COLUMNS=str(cols), LINES=str(rows))
            env.pop("TMUX", None)
            self.proc = subprocess.Popen(
                self.launch_cmd, stdin=slave, stdout=slave, stderr=slave,
                cwd=os.getcwd(), env=env, preexec_fn=_preexec, close_fds=True)
            os.close(slave)
            threading.Thread(target=self._read_loop, daemon=True).start()
        except Exception as e:  # surface a spawn failure in the render
            self.error = f"pty start failed: {e}"

    def _read_loop(self):
        fd = self.master_fd
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
                if fd not in r:
                    continue
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            with self.lock:
                self.stream.feed(data)
            # Coalesce a burst into ONE re-render. A flood of logs arrives as many
            # 64KB chunks; invalidating per chunk re-renders + re-blits the whole
            # window each time (the real cost; the pyte feed itself is cheap). So
            # keep feeding whatever was ALREADY queued before invalidating once. The
            # time/byte budget keeps continuous output updating (~100Hz) instead of
            # the drain ever starving the render. Each chunk feeds under its own lock
            # so the render can still interleave.
            ended = False
            deadline = time.monotonic() + 0.008
            read_total = len(data)
            while read_total < (1 << 20) and time.monotonic() < deadline:
                try:
                    r, _, _ = select.select([fd], [], [], 0)
                    if fd not in r:
                        break
                    more = os.read(fd, 65536)
                except OSError:
                    ended = True
                    break
                if not more:
                    ended = True
                    break
                with self.lock:
                    self.stream.feed(more)
                read_total += len(more)
            # New output -> mark the window dirty so it re-renders this/next frame.
            # invalidate() just flips a tile flag (safe to call off the render thread,
            # like request_render); this replaces the now with live=True.
            ds = self._ds
            if ds is not None:
                ds.invalidate()
            request_render()
            if ended:
                break

    def write(self, data):
        fd = self.master_fd
        if fd is None or not data:
            return
        try:
            os.write(fd, data)
        except OSError:
            pass

    def request_resize(self, cols, rows):
        """Note a desired size; the actual resize is deferred (see _RESIZE_SETTLE)."""
        if cols < 2 or rows < 2 or (cols, rows) == self.size:
            self.pending_size = None
            return
        if (cols, rows) != self.pending_size:
            self.pending_size = (cols, rows)
            self.pending_at = time.monotonic()

    def apply_pending_resize(self, defer=False):
        """Apply a pending resize once it settles. `defer` (a mouse button held =
        resize drag in progress) holds it off until release; the settle timer is the
        fallback. This is only an optimization to cut churn — _resize_screen makes
        every individual resize correct, so it doesn't matter if a few fire mid-drag."""
        if self.pending_size is None:
            return
        if defer or time.monotonic() - self.pending_at < _RESIZE_SETTLE:
            return
        cols, rows = self.pending_size
        self.pending_size = None
        self.size = (cols, rows)
        with self.lock:
            if self.screen is not None:
                self._resize_screen(rows, cols)
        if self.master_fd is not None:
            _set_winsize(self.master_fd, rows, cols)

    def _resize_screen(self, new_rows, new_cols):
        """Resize the emulated screen so the result is correct no matter how often
        this fires (a drag can call it many times). pyte's own resize is unfit for a
        scrolling shell: on shrink it drops the top rows outright (lost), and on grow
        it pads blanks at the BOTTOM, leaving content stuck at the top — so a later
        shrink then pushes that content (prompt included) into history and the shell
        reprints a fresh prompt → stacked prompts with window-sized gaps.

        Instead we rebuild around the cursor (the prompt) line: take scrollback +
        everything down to the cursor, drop the stale blanks below it, and re-lay it
        BOTTOM-anchored (cursor on the last row, overflow back into history). The
        shell's SIGWINCH redraw then always lands on the same bottom row and nothing
        accumulates. Short content stays top-anchored, like a real terminal."""
        screen = self.screen
        old_rows = screen.lines
        # A full-screen program (vim, less, Claude) owns the alt-screen grid and
        # redraws itself on SIGWINCH; don't second-guess its layout.
        if _ALT_SCREEN_MODES & set(screen.mode):
            screen.resize(new_rows, new_cols)
            return

        cur_y = min(screen.cursor.y, old_rows - 1)
        above = list(screen.history.top) + [screen.buffer[y] for y in range(cur_y + 1)]
        screen.resize(new_rows, new_cols)

        if len(above) >= new_rows:               # content fills the screen -> bottom-anchor
            rows_content = above[-new_rows:]
            overflow = above[:len(above) - new_rows]
            cursor_row = new_rows - 1
        else:                                    # little content -> keep it at the top
            rows_content = above
            overflow = []
            cursor_row = len(above) - 1

        screen.history.top.clear()
        screen.history.top.extend(overflow)
        for y in range(new_rows):
            screen.buffer[y] = rows_content[y] if y < len(rows_content) else screen.buffer.default_factory()
        screen.cursor.y = max(0, cursor_row)
        screen.cursor.x = min(screen.cursor.x, new_cols - 1)
        screen.dirty.update(range(new_rows))


# --------------------------------------------------------------------------- #
# Color helpers
# --------------------------------------------------------------------------- #

def _pack(rgb, alpha=255):
    r, g, b = (max(0, min(255, int(c * 255))) for c in rgb)
    return (alpha << 24) | (b << 16) | (g << 8) | r


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


# --------------------------------------------------------------------------- #
# View
# --------------------------------------------------------------------------- #

class TerminalScreenState:
    """Ephemeral per-view state: scrollback offset and grid selection endpoints."""

    def __init__(self):
        self.scroll = 0          # lines scrolled up from the live buffer; 0 == bottom
        self.last_total = None   # composed line count last frame (to restore position)
        self.sel_anchor = None
        self.sel_active = None


def _push_mono():
    handle = Melty.font_mgr.get(Font.JETBRAINS_MONO_19) if Melty.font_mgr else None
    if handle is not None:
        imgui.push_font(handle)
    return handle is not None


def _norm(a, b):
    if a is None or b is None:
        return None, None
    return (a, b) if a <= b else (b, a)


# pyte reports private DEC modes shifted left by 5; the alternate-screen modes
# (47 / 1047 / 1049) tell us a full-screen program (vim, Claude) owns the grid.
_ALT_SCREEN_MODES = {47 << 5, 1047 << 5, 1049 << 5}
# Mouse-tracking modes (1000 off / 1002 button / 1003 any) mean the program (e.g.
# tmux with `mouse on`) wants mouse events forwarded; 1006 is the SGR form.
_MOUSE_MODES = {1000 << 5, 1002 << 5, 1003 << 5}
_SGR_MOUSE = 1006 << 5
# tmux's copy-mode WheelUpPane scrolls this many lines per forwarded wheel escape
# (its `bind-keys -X -N 5 scroll-up` default), with a smaller line count between escapes.
_TMUX_WHEEL_LINES = 5


def _wheel_lines(visible_px, line_px):
    """Lines to scroll per wheel tick, from the global Toggles scroll settings — the
    same pixel model core_render uses (`scroll_speed` px, capped to a fraction of the
    visible height so small views don't overshoot), converted to lines."""
    s = Toggles.ScrollSettings
    px = min(s.scroll_speed, s.max_increment_fraction * max(1.0, visible_px))
    return max(1, int(round(px / max(1.0, line_px))))


def _row_blank(line, ncols):
    return all(line[x].data in (' ', '', '\x00') for x in range(ncols))


def _mouse_seq(modes, btn, col0, row0):
    """Encode a mouse event for the program. SGR (1006) when offered — it has no
    coordinate limit — else the legacy X10 form (clamped to a byte)."""
    c, r = col0 + 1, row0 + 1
    if _SGR_MOUSE in modes:
        return ("\x1b[<%d;%d;%dM" % (btn, c, r)).encode()
    return b"\x1b[M" + bytes((32 + btn, 32 + min(c, 223), 32 + min(r, 223)))


# --- clickable file:line links (jump to IDE, like draw_text's jump-to button) ---
_PROJECT_ROOT = "/home/lukas/Desktop/latent-descent"
# Python traceback `File "path", line N`, or an absolute/~/./relative `path.ext:line`.
_LINK_RE = re.compile(
    r'File "(?P<p1>[^"\n]+)", line (?P<l1>\d+)'
    r'|(?<![\w./~-])(?P<p2>(?:/|~/|\./|\.\./)[^\s:"\'\)\],]+\.[A-Za-z0-9_]+):(?P<l2>\d+)')
_LINK_COLOR = (200 << 24) | (255 << 16) | (180 << 8) | 110   # ABGR color (cyan-blue)
_LINK_HOVER_COLOR = (255 << 24) | (255 << 16) | (235 << 8) | 170  # brighter on hover


def _resolve_path(p):
    p = os.path.expanduser(p.strip())
    if os.path.isabs(p):
        return p
    cand = os.path.join(_PROJECT_ROOT, p)   # resolve relatives against the project
    return cand if os.path.exists(cand) else p


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


@render_func(show_bg=False, show_header=False, show_name=False, is_tree=False,
             selectable=False, disable_scroll=True)
def draw_terminal_screen(input_value: Terminal, draw_state, view_state: TerminalScreenState,
                         left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False,
                         left_mouse_clicked=False):
    term, ds, vs = input_value, draw_state, view_state
    left, top, right, bottom = ds.abs_clip_rect
    pad = 4.0

    pushed = _push_mono()
    try:
        char_w, line_px = max(1.0, imgui.calc_text_size("0").x), imgui.get_text_line_height() * 1.25
        x0, y0 = left + pad, top + pad
        cols = max(2, int((right - left - 2 * pad) / char_w))
        rows = max(2, int((bottom - top - 2 * pad) / line_px))

        term.start(cols, rows)
        term.request_resize(cols, rows)
        # Prefer to coalesce the resize to frame-end, but _resize_screen keeps every
        # dimension correct, so this is just churn reduction, not correctness.
        term.apply_pending_resize(defer=_mouse_held())
        if term.error:
            imgui.set_cursor_screen_pos((x0, y0))
            imgui.text_colored(term.error, _COL_ERR[0], _COL_ERR[1], _COL_ERR[2], 1.0)
            return False, term

        is_focused = Melty.text_focused_ds is ds
        io = imgui.get_io()

        # --- snapshot scrollback + live screen under the lock (reader feeds pyte) ---
        with term.lock:
            screen = term.screen
            srows, scols = screen.lines, screen.columns
            cur_x, cur_y, cur_hidden = screen.cursor.x, screen.cursor.y, screen.cursor.hidden
            modes = set(screen.mode)
            hist = list(screen.history.top)               # lines scrolled off the top
            buf_rows = [screen.buffer[y] for y in range(srows)]

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
        over = left <= io.mouse_pos.x <= right and top <= io.mouse_pos.y <= bottom
        if over and io.mouse_wheel:
            lines = _wheel_lines(bottom - top, line_px)   # speed = Toggles.ScrollSettings
            if _MOUSE_MODES & modes:
                # The program wants the mouse (e.g. tmux/less/vim) - forward the wheel
                # so it scrolls ITS scrollback instead of our (empty, only-the-screen)
                # pyte history. Button 64 = wheel up, 65 = wheel down. tmux scrolls
                # _TMUX_WHEEL_LINES per escape, so send enough escapes to hit `lines`.
                col = max(0, min(int((io.mouse_pos.x - x0) / char_w), scols - 1))
                row = max(0, min(int((io.mouse_pos.y - y0) / line_px), srows - 1))
                btn = 64 if io.mouse_wheel > 0 else 65
                ticks = abs(int(round(io.mouse_wheel))) or 1
                for _ in range(ticks * max(1, round(lines / _TMUX_WHEEL_LINES))):
                    term.write(_mouse_seq(modes, btn, col, row))
            else:
                vs.scroll += int(round(io.mouse_wheel * lines))
                if term._ds is not None:   # our own scrollback moved - re-render it
                    term._ds.invalidate()
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
            hc = int((io.mouse_pos.x - x0) / char_w)
            hr = int((io.mouse_pos.y - y0) / line_px)
            for i, (_p, _l, segs) in enumerate(links):
                if any(r == hr and lo_c <= hc < hi_c for (r, lo_c, hi_c) in segs):
                    hovered_link = i
                    break
            if links and term._ds is not None:
                term._ds.invalidate()   # re-render so the hover highlight tracks the mouse


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
            Melty.text_focused_ds = ds
            is_focused = True
            vs.sel_anchor = vs.sel_active = xy_to_rc(io.mouse_pos.x, io.mouse_pos.y)
        if (left_mouse_drag or left_mouse_held) and vs.sel_anchor is not None:
            mx = left_mouse_drag.x if left_mouse_drag else io.mouse_pos.x
            my = left_mouse_drag.y if left_mouse_drag else io.mouse_pos.y
            vs.sel_active = xy_to_rc(mx, my)

        # A plain click (no drag) on a link -> jump to it in the IDE, off-thread like
        # draw_text's jump button. `left_mouse_clicked` fires only on click, not drag,
        # so this never fights with selection.
        if left_mouse_clicked:
            cc = int((io.mouse_pos.x - x0) / char_w)
            cr = int((io.mouse_pos.y - y0) / line_px)
            for path, line, segments in links:
                if any(r == cr and lo <= cc < hi for (r, lo, hi) in segments):
                    from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
                    threading.Thread(target=open_in_intellij, args=(_resolve_path(path),),
                                     kwargs={"line_number": line}, daemon=True).start()
                    break

        if is_focused:
            _forward_keys(term, vs)
            # Keep re-rendering THIS window while focused so the cursor blinks and
            # typed input shows promptly (the echo also arrives via the reader). Only
            # the focused terminal pays this; the rest stay idle until they get focus.
            if term._ds is not None:
                term._ds.invalidate()

        # --- render: per-row style runs, selection highlight, block cursor ---
        # Clip to the body rect: while a resize drag is deferred the PTY is still at
        # the old (possibly larger) size, so its grid can overrun the shrinking
        # window - the clip keeps it from painting over neighbouring windows.
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

        if is_focused and at_bottom and not cur_hidden and 0 <= cursor_vy < srows and int(time.time() * 2) % 2 == 0:
            cx, cy = x0 + cur_x * char_w, y0 + cursor_vy * line_px
            dl.add_rect_filled(cx, cy, cx + char_w, cy + line_px, 0x88FFFFFF)
        dl.pop_clip_rect()
    finally:
        if pushed:
            imgui.pop_font()

    return False, term


def _forward_keys(term, vs):
    frame_keys = list(Melty.frame_key_events)
    if not frame_keys:
        return

    def send(data):
        vs.scroll = 0      # any input to the program snaps the view back to the bottom
        term.write(data)

    for fk, fmods in frame_keys:
        ctrl = fmods & glfw.MOD_CONTROL
        shift = fmods & glfw.MOD_SHIFT
        # Ctrl+Shift+C / V are copy / paste (Ctrl+C alone is SIGINT, sent as ^C below).
        if ctrl and shift and fk == glfw.KEY_C:
            _copy_selection(term, vs)
            continue
        if ctrl and shift and fk == glfw.KEY_V:
            clip = imgui.get_clipboard_text()
            if clip:
                send(clip.encode())
            continue
        if fk in _PTY_KEYS:
            send(_PTY_KEYS[fk])
            continue
        cm = _KEY_CHAR_MAP.get(fk)
        if cm is None:
            continue
        ch = cm[1] if shift else cm[0]
        if ctrl:
            o = ord(ch.upper())
            if 64 <= o <= 95:        # Ctrl+@..Ctrl+_ -> control byte (Ctrl+C=^C, Ctrl+D=^D, ...)
                send(bytes([o - 64]))
            elif ch == ' ':
                send(b"\x00")
            continue
        send(ch.encode())


def _copy_selection(term, vs):
    lo, hi = _norm(vs.sel_anchor, vs.sel_active)
    if lo is None or lo == hi:
        return
    with term.lock:
        disp = list(term.screen.display)
    lines = []
    for r in range(lo[0], hi[0] + 1):
        if 0 <= r < len(disp):
            s = lo[1] if r == lo[0] else 0
            e = hi[1] if r == hi[0] else len(disp[r])
            lines.append(disp[r][s:e].rstrip())
    if lines:
        imgui.set_clipboard_text("\n".join(lines))


def _tmux_launch(session):
    """Argv that attaches to (creating if needed) a durable tmux session. The shells
    and processes live in the tmux server, so they survive studio restarts and are
    shared with any external `tmux attach -t <session>`.

    `window-size latest` (tmux's default) sizes the pane to the ACTIVE client. With
    `largest`, a wider/stale client makes the pane wider than this view, so long lines
    don't wrap here — they clip at our right edge and clickable links lose their
    `:line`. `latest` means typing/scrolling here makes this the active client, so
    output wraps to our width and links stay whole."""
    return ["bash", "-c",
            "tmux set -g window-size latest 2>/dev/null; "
            "exec tmux new-session -A -s " + shlex.quote(session)]


# Two durable, attachable terminals. `main` is the app's shell; `lsd` is reserved
# for the studio/launcher session (will hold the app's console once the app is
# wrapped in tmux). test_instance kept as an alias so old tooling still works.
terminal_instance = Terminal(_tmux_launch("main"), tmux_session="main")
session_instance = Terminal(_tmux_launch("lsd"), tmux_session="lsd")
test_instance = terminal_instance


def _draw_terminal_window(term, ds, name):
    # Stash the window draw_state so the reader thread (and the focused-blink path)
    # can invalidate() THIS tile on demand, replacing the old blanket live=True.
    # draw_terminal_screen has use_cache=False, so re-running the window re-runs it.
    term._ds = ds
    # get_content_rect() is the body below the window header (and above any footer);
    # abs_window_rect starts at the very top, so using it would paint under the title.
    left, top, right, bottom = ds.get_content_rect()
    imgui.set_cursor_screen_pos((left, top))
    draw_terminal_screen(term, name=name,
                         width=max(1.0, right - left), height=max(1.0, bottom - top))
    return False, term


# No live=True: the terminal re-renders only when it has something new. The reader
# thread invalidate()s the window on PTY output; the render below invalidate()s whe
# focused (for the blinking cursor + input responsiveness). Idle/unfocused terminals
# cost nothing.
@window(tint=(0.032, 0.098, 0.2), bg_offset=-1, input_value=terminal_instance)
@render_func(is_default_for=Terminal)
def draw_terminal(input_value: Terminal, draw_state):
    return _draw_terminal_window(input_value, draw_state, "terminal_screen")


@window(tint=(0.10, 0.0, 0.02), input_value=session_instance)
@render_func
def draw_session_terminal(input_value: Terminal, draw_state):
    return _draw_terminal_window(input_value, draw_state, "session_screen")
