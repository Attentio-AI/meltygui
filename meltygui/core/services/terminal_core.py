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
import os
import shlex
import shutil
import signal
import struct
import subprocess
import threading
import time
import uuid

if os.name != "nt":          # the pseudo-terminal pump is POSIX; Terminal reports that on Windows
    import fcntl
    import termios

import meltygui.core.windowing.window_api as glfw
import meltygui_imgui as imgui

from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.rendering.window_decoration import window
# Reuse the editor's GLFW-key -> character map (covers letters, digits, punctuation
# with shift pairs) to turn key events into the bytes the shell expects.
from meltygui.editor.text_editor import _KEY_CHAR_MAP


# 16-colour ANSI palette (pyte hands us names for the basic colours, 6-hex strings
# for 256/true-colour). Tuned to read well on the dark window background.

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
    meltygui-close ↔ gnome-close stay in sync. Non-blocking (Popen). `new-session -A` (not
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


from meltygui.model.terminal_model import Terminal


# --------------------------------------------------------------------------- #
# Color helpers
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# View
# --------------------------------------------------------------------------- #


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


def _mouse_seq(modes, btn, col0, row0):
    """Encode a mouse event for the program. SGR (1006) when offered — it has no
    coordinate limit — else the legacy X10 form (clamped to a byte)."""
    c, r = col0 + 1, row0 + 1
    if _SGR_MOUSE in modes:
        return ("\x1b[<%d;%d;%dM" % (btn, c, r)).encode()
    return b"\x1b[M" + bytes((32 + btn, 32 + min(c, 223), 32 + min(r, 223)))


# --- clickable file:line links (jump to IDE, like draw_text's jump-to button) ---
from meltygui.core.runtime.paths import application_root
_PROJECT_ROOT = str(application_root())
# Python traceback `File "path", line N`, or an absolute/~/./relative `path.ext:line`.


def _resolve_path(p):
    p = os.path.expanduser(p.strip())
    if os.path.isabs(p):
        return p
    cand = os.path.join(_PROJECT_ROOT, p)   # resolve relatives against the project
    return cand if os.path.exists(cand) else p


from meltygui.view.terminal_view import draw_terminal_screen


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
    from meltygui.state.terminal_state import _norm
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
    # get_content_rect() is the body below the window header (and above any footer);
    # abs_window_rect starts at the very top, so using it would paint under the title.
    left, top, right, bottom = ds.get_content_rect()
    imgui.set_cursor_screen_pos((left, top))
    draw_terminal_screen(term, name=name,
                         width=max(1.0, right - left), height=max(1.0, bottom - top))
    return False, term


# Output subscribers and declared input events invalidate the affected screens.
from meltygui.view.terminal_view import draw_terminal
draw_terminal = window(tint=(0.05, 0.06, 0.07), bg_offset=-1, max_bg_value=0.08, disable_scroll=True, input_value=terminal_instance)(draw_terminal)


from meltygui.view.terminal_view import draw_session_terminal
draw_session_terminal = window(tint=(0.09, 0.07, 0.077), input_value=session_instance)(draw_session_terminal)
