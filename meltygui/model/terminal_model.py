"""Terminal model functions and supporting definitions."""
from meltygui.core.core_decoration import defaults
import os
import select
import threading
import time


@defaults(tint=(0.31, 0.37, 0.66))
class Terminal:
    """A PTY-backed terminal session plus its pyte screen.
    `launch_cmd` is the argv spawned in the PTY — by default a `tmux attach`/create,
    so the processes live in a durable tmux server (they survive studio restarts and
    are shared with any external `tmux attach`). The reader thread feeds bytes into
    pyte under `lock`; the render thread reads the screen grid under the same `lock`."""

    def __init__(self, launch_cmd=None, tmux_session=None):
        # No args (the "+" add button calls Terminal()) -> create a brand-new OWNED
        # terminal: the in-app PTY itself creates the tmux session (new-session -A) when
        # it starts, then hands it off to its own gnome-terminal window (claude-d style).
        # No blocking `tmux new-session -d` on the UI thread first. With a launch_cmd/
        # session given (main/lsd/discovered claude-d) we just attach, as before.
        from meltygui.core.terminal_core import _new_owned_session_name
        from meltygui.core.terminal_core import _owned_launch_argv

        owned = launch_cmd is None and tmux_session is None
        if owned:
            tmux_session = _new_owned_session_name()
            launch_cmd = _owned_launch_argv(tmux_session)
        self.launch_cmd = launch_cmd or [os.environ.get("SHELL", "/bin/bash"), "-i"]
        self.tmux_session = tmux_session   # session name (if tmux attached)
        # add_to_collection keys a dict by `.id`, so a "+"-added terminal lands in the
        # store UNDER its session name; the studio poller then finds it already there
        # and doesn't create a duplicate.
        self.id = tmux_session
        self.master_fd = None
        self.pid = None            # the PTY child (a bash → tmux client); reaped by the reader
        self.screen = None
        self.stream = None
        self.lock = threading.Lock()
        self.started = False
        self.size = (0, 0)         # (cols, rows) currently applied to the PTY
        self.error = None
        self._ds = None            # this terminal's window draw_state (set by render)
                                   # so the reader thread can invalidate it on new output
        self._last_sig = None      # content signature at the last render - the reader
                                   # only invalidates/wakes when this actually changes
        self._reader_alive = False  # True while the reader thread runs; is_dead() reads it
        # For a brand-new OWNED terminal: the session the PTY creates, handed off to its
        # own gnome-terminal window once the PTY is up (in start()). None = attach-only.
        self._owned_session = tmux_session if owned else None
        self._appeared = False     # set text focus once, on the terminal's first render

    def is_dead(self):
        """True once the reader thread has exited — the PTY (and thus its tmux session)
        ended. The studio io drops a terminal on THIS (immediate) rather than waiting
        for the ~1s poller snapshot, so a just-created terminal isn't briefly dropped."""
        return self.started and not self._reader_alive

    def start(self, cols, rows):
        if self.started:
            return
        self.started = True
        self.size = (cols, rows)
        # Spawn the PTY + tmux attach OFF the main thread. Launching the studio with many
        # discovered terminals would spawn a the processes (bash → tmux attach) plus
        # openpty on a SINGLE render frame - a big launch hitch that scales with the
        # number of sessions. Claim _reader_alive now so is_dead() can't fire in the gap
        # before the bg thread runs; the thread clears it on exit / spawn failure. screen
        # stays None until ready, so draw_terminal_screen shows a placeholder meanwhile.
        self._reader_alive = True
        threading.Thread(target=self._start_and_read, args=(cols, rows), daemon=True).start()

    def _start_and_read(self, cols, rows):
        from meltygui.core.terminal_core import _OWNED_SESSION_PREFIX
        from meltygui.core.terminal_core import _disable_mouse
        from meltygui.core.terminal_core import _handoff_to_gnome
        from meltygui.core.terminal_core import _make_history_screen
        from meltygui.core.terminal_core import _set_winsize
        from meltygui.core.terminal_core import _spawn_in_pty

        try:
            import pyte
        except ImportError:
            self.error = "pyte not installed — run: pip install pyte"
            self._reader_alive = False
            return
        try:
            screen = _make_history_screen(pyte, cols, rows)
            stream = pyte.ByteStream(screen)
            master, slave = os.openpty()
            _set_winsize(master, rows, cols)
            # Unset TMUX so a `tmux attach` child can launch even when the studio is
            # itself launched inside tmux (otherwise tmux refuses to nest).
            env = dict(os.environ, TERM="xterm-256color",
                       COLUMNS=str(cols), LINES=str(rows))
            env.pop("TMUX", None)
            # posix_spawn, not fork - see _spawn_in_pty. The child inherits our cwd.
            # The slave stays open here until the spawn returns (the child has opened
            # its own by then - posix_spawn resumes here only after the exec).
            pid = _spawn_in_pty(self.launch_cmd, slave, env)
            os.close(slave)
            # Publish the live objects under the lock so the render thread either sees a
            # fully-wired terminal or still-None (placeholder), never a half-built one.
            with self.lock:
                self.screen = screen
                self.stream = stream
                self.master_fd = master
                self.pid = pid
            # The PTY has forked (owned sessions: created via new-session -A): hand off to
            # its own gnome-terminal window (a second client that owns the lifetime), and
            # disable tmux mouse for claude-d sessions so normal text selection works.
            if self._owned_session is not None:
                _handoff_to_gnome(self._owned_session)
            if self.tmux_session and self.tmux_session.startswith(_OWNED_SESSION_PREFIX):
                _disable_mouse(self.tmux_session)
        except Exception as e:  # surface a spawn failure in the render
            self.error = f"pty start failed: {e}"
            self._reader_alive = False
            return
        self._read_loop()

    def _screen_signature(self):
        """A hash of everything we DRAW — every cell (text + colors), the cursor, and
        the scrollback depth. Lets _read_loop invalidate/wake ONLY when a read actually
        changed what's on screen: a terminal that emits bytes with no visible effect
        (a query/response, a cursor report, reprinted-identical output) must not force a
        re-render. pyte's Char is a hashable namedtuple, so we hash the rows directly."""
        sc = self.screen
        if sc is None:
            return None
        cols = sc.columns
        buf = sc.buffer
        rows = tuple(tuple(buf[y][x] for x in range(cols)) for y in range(sc.lines))
        return hash((rows, sc.cursor.x, sc.cursor.y, sc.cursor.hidden, len(sc.history.top)))

    def _read_loop(self):
        from meltygui.core.glfw_utils import request_render

        fd = self.master_fd
        # Force a first paint. Guarded: this runs BEFORE the try below, so a None _ds
        # (a brand-new terminal whose screen hasn't rendered yet) would raise here and
        # kill the thread without the finally clearing _reader_alive, leaving a frozen,
        # never-drawn terminal. draw_terminal_screen sets _ds before start(), so it's
        # normally set, but stay defensive.
        if self._ds is not None:
            self._ds.invalidate()

        try:
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
            # Only invalidate + wake the loop if this read actually CHANGED what we
            # draw. The session pane (the studio's own console) dribbles bytes that
            # don't alter the visible output; invalidating per read re-rendered the app
            # every frame. The signature is computed once per read burst, so it's
            # cheap, and it's the only thing that calls request_render here - no data
            # change, no wake. (invalidate is the tile cache; request_render wakes
            # the render thread; both gated on a real change.)
            with self.lock:
                sig = self._screen_signature()
            if sig != self._last_sig:
                ds = self._ds
                if ds is not None:
                    ds.invalidate()
                try:
                    request_render()
                    self._last_sig = sig          # commit only after a successful wake
                except Exception:
                    # GLFW not initialized yet (a reader can fire during studio startup,
                    # before the window exists). request_render()'s get_current_context()
                    # raises then, which would otherwise kill this thread → frozen
                    # terminal. Swallow here and leave _last_sig stale so the next read
                    # retries once GLFW is up.
                    pass
            if ended:
                break
        finally:
            self._reap()
            self._reader_alive = False   # reader exited -> is_dead = True -> io drops us

    def _reap(self):
        """Collect the PTY child's exit status so it doesn't linger as a zombie. The
        master hit EOF/EIO, which normally means the child is gone; a child that merely
        closed its tty and lives on is left alone after a short bounded wait."""
        pid, self.pid = self.pid, None
        if pid is None:
            return
        for _ in range(20):
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return
            except ChildProcessError:
                return
            time.sleep(0.05)

    def write(self, data):
        fd = self.master_fd
        if fd is None or not data:
            return
        try:
            os.write(fd, data)
        except OSError:
            pass

    def resize(self, cols, rows):
        """Resize the PTY + emulated screen immediately — no coalescing, so every size
        the window sweeps through during a drag is applied as fast as it changes.
        _resize_screen keeps each individual resize correct."""
        from meltygui.core.terminal_core import _set_winsize

        if cols < 2 or rows < 2 or (cols, rows) == self.size:
            return
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
        from meltygui.core.terminal_core import _ALT_SCREEN_MODES

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
