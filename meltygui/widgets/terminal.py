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
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid

import glfw
import imgui
from src.lsd.gl_gui.hdr_color import pack_color

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
_SEL_COLOR = pack_color(51 / 255, 102 / 255, 204 / 255, 102 / 255)   # pale blue wash

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

# Keys that should auto-repeat while held: every typeable char plus the special PTY
# keys (arrows, backspace, enter, ...). Used to synthesize repeats from imgui since
# GLFW doesn't emit REPEAT actions on its own - same approach as draw_text.
_TERM_REPEATABLE_KEYS = set(_KEY_CHAR_MAP) | set(_PTY_KEYS)

# Navigation keys that take the xterm "modified" form when ctrl/alt/shift is held:
# plain `\x1b[<final>`, modified `\x1b[1;<mod><final>` (mod from _xterm_mod). This is
# how a terminal distinguishes Ctrl+Right (word-forward) from a bare Right arrow.
_CSI_FINAL = {
    glfw.KEY_UP: "A", glfw.KEY_DOWN: "B", glfw.KEY_RIGHT: "C", glfw.KEY_LEFT: "D",
    glfw.KEY_HOME: "H", glfw.KEY_END: "F",
}
# Editing/paging keys in the tilde form: plain `\x1b[<n>~`, modified `\x1b[<n>;<mod>~`.
_CSI_TILDE = {
    glfw.KEY_INSERT: 2, glfw.KEY_DELETE: 3,
    glfw.KEY_PAGE_UP: 5, glfw.KEY_PAGE_DOWN: 6,
}


def _xterm_mod(shift, alt, ctrl):
    """The xterm modifier parameter: 1 + a bitmask (shift=1, alt=2, ctrl=4). Returns 0
    when no modifier is held, signalling the caller to emit the plain sequence instead
    of the `;<mod>` parameterized one. E.g. Ctrl+Right -> mod 5 -> `\\x1b[1;5C`."""
    bits = (1 if shift else 0) | (2 if alt else 0) | (4 if ctrl else 0)
    return bits + 1 if bits else 0


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass




def _inheritable_fds():
    """Every fd >= 3 that would survive an exec (not FD_CLOEXEC). Python-created fds are
    CLOEXEC by default (PEP 446); this catches the C libraries' (CUDA / GL / inotify)."""
    fds = []
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        if fd < 3:
            continue
        try:
            if not (fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC):
                fds.append(fd)
        except OSError:
            pass          # closed between the listdir and the fcntl
    return fds


def _spawn_in_pty(argv, slave, env):
    """Run `argv` on the pty `slave` as a session leader with the pty as its controlling
    terminal, and return the child's pid. NEVER fork()s: `os.posix_spawn` is glibc's
    vfork/CLONE_VM path — the child shares our address space until it execs, so there is
    no page-table copy of the studio's huge CUDA/GL/torch mappings and NO at-fork handler
    runs. The old `subprocess.Popen(..., preexec_fn=setsid+TIOCSCTTY)` had to fork() the
    whole process (preexec_fn rules out posix_spawn) and the single-threaded child
    deadlocked in an inherited lock before it ever reached exec — Popen then blocked on
    the exec-errpipe read forever and the terminal sat on "starting…" (08-27).

    Controlling tty without TIOCSCTTY: the file actions run AFTER the child's setsid, so
    OPENing the slave's path (no O_NOCTTY) as the new session leader makes it the
    controlling terminal — the same rule login programs rely on. The CLOSE actions are
    Popen's close_fds=True: glibc ignores EBADF on an in-range fd, so an fd another
    thread closed in the meantime is harmless. Signals match Popen's restore_signals."""
    executable = shutil.which(argv[0]) or argv[0]       # posix_spawn wants a path
    actions = [(os.POSIX_SPAWN_CLOSE, fd) for fd in _inheritable_fds()]
    actions += [(os.POSIX_SPAWN_OPEN, 0, os.ttyname(slave), os.O_RDWR, 0),
                (os.POSIX_SPAWN_DUP2, 0, 1),
                (os.POSIX_SPAWN_DUP2, 0, 2)]
    return os.posix_spawn(executable, argv, env, file_actions=actions, setsid=True,
                          setsigmask=(), setsigdef=(signal.SIGPIPE, signal.SIGXFSZ))


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



# A new "owned" terminal names its session claude-d-<pid>-<n> so the studio's
# claude_terminals poller discovers + manages it just like an externally-launched
# claude-d session. The suffix is a random uuid, NOT pid+counter: the studio restarts
# IN-PLACE (same pid) and a module reload would reset any module-level counter to 1, so
# pid+counter handed out `claude-d-<samepid>-1` after every restart → the new window
# collided with the previous run's draw_state (stale `closed` etc.). A uuid is unique
# across restarts and immune to pid reuse.
_OWNED_SESSION_PREFIX = "claude-d-"

# Full path to tmux so subprocess can use posix_spawn (vfork) instead of fork()ing the
# studio's huge CUDA/GL/torch address space - a fork stalls the render thread (kernel
# mmap_lock + GIL held across the fork). Full path + close_fds=False is what triggers the
# posix_spawn path on this Python. See claude_terminals._list_claude_sessions.
_TMUX = shutil.which("tmux") or "/usr/bin/tmux"
_GNOME_TERMINAL = shutil.which("gnome-terminal") or "/usr/bin/gnome-terminal"

# OWNED claude-d sessions run on a DEDICATED tmux server (`-L claude-d`) started with
# `-f /dev/null` so it ignores ~/.tmux.conf (which sets `mouse on` + rebinds the arrows).
# That isolation lets the handed-off gnome window scroll/copy like a plain terminal
# without disturbing the user's `main`/`lsd` sessions - see bin/claude-d. `main`/`lsd`
# themselves run on the DEFAULT server (see _tmux_launch). `_CD_TMUX` is the common prefix
# for any claude-d tmux call; `_CD_SETUP` reapplies the plain default config (idempotent
# global `set -g`) in case the client cold-started the server.
_CD_TMUX = "tmux -f /dev/null -L claude-d"
_CD_SETUP = (
    f"{_CD_TMUX} set -g mouse off 2>/dev/null; "
    f"{_CD_TMUX} set -g status off 2>/dev/null; "
    f"{_CD_TMUX} set -g window-size latest 2>/dev/null; "
    f"{_CD_TMUX} set -g terminal-overrides ',*:smcup@:rmcup@' 2>/dev/null; ")


def _attach_argv(session):
    """Argv that attaches an in-app PTY to an existing tmux session (a shared client).
    `window-size latest` makes this view the active client so output wraps to it."""
    return ["bash", "-c",
            "tmux set -g window-size latest 2>/dev/null; "
            "exec tmux attach-session -t " + shlex.quote(session)]


def _new_owned_session_name():
    """Just the NAME for a brand-new owned session. Nothing is created here — the
    in-app PTY creates the session itself when it starts (see _owned_launch_argv), so
    a "+" click never blocks the UI thread on a synchronous `tmux new-session`."""
    return _OWNED_SESSION_PREFIX + uuid.uuid4().hex[:8]


def _owned_launch_argv(session):
    """Argv for the in-app PTY of a brand-new OWNED terminal: it CREATES the session
    (`new-session -A` = create-or-attach) directly in the PTY fork. This is the speed
    win — no separate blocking `tmux new-session -d` round-trip on the UI thread first;
    the terminal launches the process itself and the session exists as soon as the PTY
    is up. The gnome window is handed the session afterwards (see _handoff_to_gnome)."""
    return ["bash", "-c",
            _CD_SETUP +
            "exec " + _CD_TMUX + " new-session -A -s " + shlex.quote(session)]


def _handoff_to_gnome(session):
    """Open a gnome-terminal that attaches to an OWNED session the in-app PTY already
    created, and OWNS its lifetime: its trap kills the session on window close, so
    melty-close ↔ gnome-close stay in sync. Non-blocking (Popen). `new-session -A` (not
    plain attach) is race-safe — whoever loses the create just attaches — though the PTY
    forks first so it normally wins. No `exec`, or the trap is skipped (see claude-d).
    `env -u TMUX` so it attaches even if the studio itself was launched inside tmux."""
    q = shlex.quote(session)
    inner = (_CD_SETUP +
             'trap "' + _CD_TMUX + ' kill-session -t ' + q + ' 2>/dev/null" EXIT HUP TERM INT; '
             'env -u TMUX ' + _CD_TMUX + ' new-session -A -s ' + q)
    try:
        # Full path + close_fds=False → posix_spawn, never a fork of the studio (see _TMUX).
        subprocess.Popen([_GNOME_TERMINAL, "--", "bash", "-c", inner], close_fds=False)
    except Exception:
        pass


def _disable_mouse(session):
    """Belt-and-suspenders `mouse off` for an owned session. The dedicated `-L claude-d`
    server already runs with mouse off globally (it skips ~/.tmux.conf via `-f /dev/null`
    and _CD_SETUP reasserts it), so this is mostly redundant now — but it cheaply covers
    any ordering race before _CD_SETUP lands. With mouse mode ON, a click-drag in the
    gnome window is grabbed by tmux's copy-mode and the selection is cleared the instant
    you release — you can't select/copy text. Off, gnome-terminal does its own native
    selection. Run off-thread with a short retry: an owned session is created by the
    in-app PTY's `new-session -A` and may not exist the instant we ask."""
    def go():
        for _ in range(20):
            try:
                # Full path + close_fds=False → posix_spawn, not fork (see _TMUX). The
                # `-f /dev/null -L claude-d` flags target the dedicated server (see _CD_TMUX).
                r = subprocess.run([_TMUX, "-f", "/dev/null", "-L", "claude-d",
                                    "set", "-t", session, "mouse", "off"],
                                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   timeout=2, close_fds=False)
                if r.returncode == 0:
                    return
            except Exception:
                pass
            time.sleep(0.1)
    threading.Thread(target=go, daemon=True).start()


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
    px = min(Toggles.ScrollSettings.scroll_speed,
             Toggles.ScrollSettings.max_increment_fraction
             * max(1.0, visible_px))
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
_LINK_COLOR = pack_color(110 / 255, 180 / 255, 1.0, 200 / 255)      # underline (cyan-blue)
_LINK_HOVER_COLOR = pack_color(170 / 255, 235 / 255, 1.0, 1.0)      # brighter on hover


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


@render_func(is_default_for=(Terminal), show_bg=False, show_header=False, show_name=False, is_tree=False,
             selectable=False, disable_scroll=True, initial={"closed": False})
def draw_terminal_screen(input_value: Terminal, draw_state, view_state: TerminalScreenState,
                         left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False, 
                         left_mouse_clicked=False):
    term, ds, vs = input_value, draw_state, view_state
    # Point the reader thread's invalidator at the tile that actually re-renders this
    # terminal. In the claude-terminals path this screen IS the blit-cached child window
    # (TERMINAL_WINDOW mode → closable=True), so THIS draw_state is what must be
    # invalidated on new output - set it BEFORE term.start() so the reader (launched
    # inside start()) always has the right target. In the main/lsd path
    # draw_terminal_screen is nested inside draw_terminal's @window (not closable) and
    # _draw_terminal_window already pointed term._ds at that window tile - don't clobber
    # that with this non-cached text screen. Without this the claude reader invalidated
    # the PARENT window while each child's cached blit was unchanged (terminals stopped
    # refreshing on "+" or on characters typed in a gnome-side client).
    if ds.closable:
        term._ds = ds
        # Grab text focus the first time this terminal appears, so a freshly-opened
        # ("+" or just-discovered) terminal is typeable immediately without a click.
        # Scoped to the closable (claude) tiles so the main/lsd ones don't steal
        # focus on studio startup. Same mechanism as the click-to-focus below.
        if not term._appeared:
            term._appeared = True
            Melty.text_focused_ds = ds
    left, top, right, bottom = ds.abs_left, ds.abs_top, ds.abs_left + ds.width, ds.abs_top+ ds.height
    pad = 4.0

    pushed = _push_mono()
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

        term.start(cols, rows)
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
        term.resize(cols, rows)   # immediate - applied every time the drag changes size

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
        over = left <= io.mouse_pos.x <= right and top <= io.mouse_pos.y <= bottom
        if over and io.mouse_wheel:
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


def _forward_keys(term, vs):
    # Keystrokes come from the loop's event queue (Melty.frame_key_events: ordered
    # (glfw_key, mods) per PRESS/REPEAT this frame). GLFW doesn't send REPEAT actions
    # on every platform, so - like draw_text - supplement the queue with imgui's
    # synthesized auto-repeat (io.key_repeat_delay/rate) for any held key, skipping
    # those GLFW already reported this frame so a held key never double-inputs. While a
    # repeatable key is down, keep the loop rendering so imgui's repeat cadence (which
    # is once per frame) keeps firing instead of stalling on wait_events.
    frame_keys = list(Melty.frame_key_events)
    io = imgui.get_io()
    glfw_this_frame = {k for k, _m in frame_keys}
    repeat_mods = ((glfw.MOD_SHIFT if io.key_shift else 0)
                   | (glfw.MOD_CONTROL if io.key_ctrl else 0)
                   | (glfw.MOD_ALT if getattr(io, 'key_alt', False) else 0))
    any_down = False
    for rk in _TERM_REPEATABLE_KEYS:
        if imgui.is_key_down(rk):
            any_down = True
        if rk not in glfw_this_frame and imgui.is_key_pressed(rk, repeat=True):
            frame_keys.append((rk, repeat_mods))
    if any_down:
        request_render()
    if not frame_keys:
        return

    def send(data):
        vs.scroll = 0      # any input to the program snaps the view back to the bottom
        term.write(data)

    for fk, fmods in frame_keys:
        ctrl = fmods & glfw.MOD_CONTROL
        shift = fmods & glfw.MOD_SHIFT
        alt = fmods & glfw.MOD_ALT
        # Ctrl+Shift+C / V are copy / paste (Ctrl+C alone is SIGINT, sent as ^C below).
        if ctrl and shift and fk == glfw.KEY_C:
            _copy_selection(term, vs)
            continue
        if ctrl and shift and fk == glfw.KEY_V:
            clip = imgui.get_clipboard_text()
            if clip:
                send(clip.encode())
            continue
        # Navigation keys carry their modifiers in the xterm "modified" CSI form, so the
        # program sees Ctrl+Arrow (word jump), Alt+Arrow, Shift+Arrow as distinct from a
        # bare arrow; mod==0 (no modifier) falls back to the plain sequence.
        mod = _xterm_mod(shift, alt, ctrl)
        if fk in _CSI_FINAL:
            final = _CSI_FINAL[fk]
            send((f"\x1b[1;{mod}{final}" if mod else f"\x1b[{final}").encode())
            continue
        if fk in _CSI_TILDE:
            n = _CSI_TILDE[fk]
            send((f"\x1b[{n};{mod}~" if mod else f"\x1b[{n}~").encode())
            continue
        if fk in _PTY_KEYS:   # enter, tab, backspace, escape: Alt adds ESC (meta),
            seq = _PTY_KEYS[fk]   # so e.g. Alt+Backspace = `\x1b\x7f` (delete word).
            send(b"\x1b" + seq if alt else seq)
            continue
        cm = _KEY_CHAR_MAP.get(fk)
        if cm is None:
            continue
        ch = cm[1] if shift else cm[0]
        if ctrl:
            o = ord(ch.upper())
            if 64 <= o <= 95:        # Ctrl+@..Ctrl+_ -> control byte (Ctrl+C=^C, Ctrl+D=^D, ...)
                b = bytes([o - 64])
            elif ch == ' ':
                b = b"\x00"
            else:
                continue
            send(b"\x1b" + b if alt else b)   # Ctrl+Alt+key -> ESC + control byte
            continue
        if alt:                              # Alt+key -> ESC prefix (meta), e.g. Alt+f /
            send(b"\x1b" + ch.encode())      # Alt+b for readline-like navigation.
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
@window(tint=(0.05, 0.06, 0.07), bg_offset=-1,max_bg_value=0.08, disable_scroll=True, input_value=terminal_instance)
@render_func(is_default_for=Terminal)
def draw_terminal(input_value: Terminal, draw_state):
    return _draw_terminal_window(input_value, draw_state, "terminal_screen")


@window(tint=(0.09, 0.07, 0.077), input_value=session_instance)
@render_func
def draw_session_terminal(input_value: Terminal, draw_state, max_bg_value=0.064):
    return _draw_terminal_window(input_value, draw_state, "session_screen")