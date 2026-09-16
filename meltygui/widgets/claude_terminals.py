"""
Claude terminals — discovered tmux sessions rendered through a RenderHost.

`claude-d` launches Claude Code in a tmux session named `claude-d-<pid>`. This
module surfaces every such session as a live terminal in the studio, using the
RenderHost mechanism exactly as `modifies_playground` does:

    discover()  →  view_func(dict)  →  apply()              (claude_terminals_io)

`claude_terminals_io` is the plain stateful wrapper — it knows nothing about
RenderHost. It reconciles a persistent `{session: Terminal}` dict against the live
`claude-d-*` tmux sessions, hands that dict to its `view_func`, and (hypothetically)
applies any edits the view made back to the sessions on the way out. `RenderHost`
wraps it so the program just sees "a dict full of terminal windows"; the host owns
the Melty window and calls the wrapper each frame it re-renders.

A background poller re-runs discovery when the set of sessions changes (the
wrapper's body is blit-cached, so it wouldn't otherwise notice a session that
appeared/vanished with no terminal output to invalidate it).
"""

import itertools
import os
import shlex
import shutil
import subprocess
import threading

from meltygui.lifecycle import module_is_live
import time
from pathlib import Path

import meltygui_imgui as imgui

from meltygui.modes import Modes
from meltygui.rendering.render_funcs import RenderFuncs
from meltygui.utils.glfw_utils import request_render
from meltygui.code.render_host import RenderHost
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.core_decoration import Core
from meltygui.rendering.decorators.window_decoration import window
from meltygui.widgets.terminal_playground import Terminal
from meltygui.widgets.terminal_playground import draw_terminal_screen

_SESSION_PREFIX = "claude-d-"

# claude-d runs on a DEDICATED tmux server (`-L claude-d`) started with `-f /dev/null`
# so it ignores ~/.tmux.conf (which sets `mouse on` + rebinds the arrows). This isolation
# is what lets the gnome window scroll/copy like a plain terminal without touching the
# tm's `main`/`lsd` server - only bash/claude-d. EVERY tmux call here must target that
# same server or it would look at the wrong (default) server and find nothing.
_TMUX = "tmux -f /dev/null -L claude-d"
# List form for subprocess argv. ABSOLUTE binary path so (with close_fds=False)
# CPython spawns via posix_spawn, not fork - fork from this process copies the
# huge engine/GL/torch address space with the GIL held and blocks the render thread.
_TMUX_BIN = shutil.which("tmux") or "/usr/bin/tmux"
_TMUX_ARGS = [_TMUX_BIN, "-f", "/dev/null", "-L", "claude-d"]
# Reapply the plain terminal config from any client (idempotent global `set -g`); needed
# in case THIS client cold-starts the server before gnome/claude-d does.
_TMUX_SETUP = (
    f"{_TMUX} set -g mouse off 2>/dev/null; "
    f"{_TMUX} set -g status off 2>/dev/null; "
    f"{_TMUX} set -g window-size latest 2>/dev/null; "
    f"{_TMUX} set -g terminal-overrides ',*:smcup@:rmcup@' 2>/dev/null; ")


def _list_claude_sessions():
    """The live tmux sessions started by `claude-d` (by name prefix). [] on any
    failure (no tmux server, none running)."""
    try:
        out = subprocess.run(_TMUX_ARGS + ["list-sessions", "-F", "#{session_name}"],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return []
    return sorted(s for s in out.split() if s.startswith(_SESSION_PREFIX))


def _attach_cmd(session):
    """Argv that attaches the in-app PTY to an existing claude-d session (a second,
    shared client — the session was created by `claude-d`, we just view/drive it).
    `window-size latest` makes our view the active client so output wraps to us."""
    return ["bash", "-c",
            _TMUX_SETUP +
            "exec " + _TMUX + " attach-session -t " + shlex.quote(session)]

def _kill_session(session):
    """Kill a claude-d tmux session off-thread. This ends the `claude-d` process
    running it (its EXIT/HUP trap fires) → the gnome window closes, and the session
    leaves the tmux server so the poller drops it from `_live_sessions`."""

    def go():
        try:
            subprocess.run(_TMUX_ARGS + ["kill-session", "-t", session],
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=2)
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()


from meltygui.paths import application_root
_REPO_ROOT = application_root()
_studio_session_counter = itertools.count(1)


def launch_claude_session(prompt_text=None):
    """Start a NEW detached claude-d tmux session running Claude Code and, once
    its input box is up, TYPE `prompt_text` into it — literally, no Enter, so
    the user reviews/extends the prompt before sending. The ~1s poller discovers
    the session like any external `claude-d` one and the studio window grows a
    terminal for it. Unlike `bin/claude-d` there's no gnome window: the session
    lives on the dedicated socket only, and closing the in-app window kills it
    via the normal `_kill_session` path. Returns the session name immediately;
    everything runs off-thread (subprocess uses the absolute tmux path +
    close_fds=False → posix_spawn, see _TMUX_BIN)."""
    session = f"{_SESSION_PREFIX}studio-{os.getpid()}-{next(_studio_session_counter)}"

    def run(args, **kw):
        kw.setdefault("close_fds", False)
        kw.setdefault("timeout", 10)
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
        return subprocess.run(args, **kw)

    def go():
        # `TMUX` unset so this works even when the studio itself was launched
        # from inside a tmux session (tmux refuses to nest otherwise).
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        try:
            run(_TMUX_ARGS + ["new-session", "-d", "-s", session, "-c", str(_REPO_ROOT),
                              "claude --dangerously-skip-permissions"], env=env)
        except Exception as e:
            print(f"launch_claude_session: failed to start {session}: {e}")
            return
        # Plain-terminal config, same as bin/claude-d (idempotent `set -g`) - in
        # case this cold-starts the dedicated server before bin/claude-d does.
        for opt in (["set", "-g", "mouse", "off"],
                    ["set", "-g", "status", "off"],
                    ["set", "-g", "window-size", "latest"],
                    ["set", "-g", "terminal-overrides", ",*:smcup@:rmcup@"]):
            try:
                run(_TMUX_ARGS + opt)
            except Exception:
                pass
        if not prompt_text:
            return
            
        # Wait for Claude Code's input box before typing, so the text lands in
        # the prompt rather than the boot screen. The box's prompt chrome is
        # "" (U+276F); accept ASCII ">" too in case the glyph changes.
        deadline = time.time() + 30.0
        while time.time() < deadline:
            try:
                out = run(_TMUX_ARGS + ["capture-pane", "-p", "-t", session],
                          stdout=subprocess.PIPE, text=True).stdout or ""
            except Exception:
                out = ""
            if "❯" in out or ">" in out:
                break
            time.sleep(0.5)
        time.sleep(0.5)  # let the TUI finish wiring its key handling
        try:
            # -l = literal keys: the prompt is TYPED, not sent (no Enter).
            run(_TMUX_ARGS + ["send-keys", "-t", session, "-l", prompt_text])
        except Exception as e:
            print(f"launch_claude_session: failed to type prompt into {session}: {e}")

    threading.Thread(target=go, daemon=True, name="claude-session-launch").start()
    return session


def open_claude_terminals_window():
    """Open + front the studio's Claude Terminals window (render thread only —
    same open pattern as screenshot.process_captures)."""
    from meltygui.melty import Melty
    from meltygui.screenshot import _find_window
    mw = _find_window("draw_claude_terminals")
    if mw is None:
        return
    mw.draw_state.closed = False
    if isinstance(mw.window_args, dict):
        mw.window_args["closed"] = False
    Melty.move_window_to_front(mw.draw_state)


# ── the stateful wrapper: discover -> view_func(dict) -> apply ──────────────────
# Shaped like code_file_io: stateful work, ONE nested view_func call, stateful work.
# This does not know about RenderHost - the host hands it `view_func` and consumes
# whatever dict it passes through.
@render_func(use_cache=True, selectable=False, show_bg=False)
def claude_terminals_io(input_value, draw_state, view_func=None, external_change=False, **kwargs):
    # ── IN: reconcile the HELD dict IN PLACE against the live sessions. Reconcile
    # `input_value` (the host's held {session: Terminal} it hands us) - NOT a separate
    # draw_state store - so adds/drops happen in the SAME object the @window reads. A
    # separate store leaves the host's materialized ["value"] a stale COPY: if the
    # first materialize runs before the poller fills _live_sessions, ["value"] is stuck
    # empty forever and NO claude windows ever show. First frame the host is empty
    # (input_value None) → start fresh; view_func materializes it as the held value.
    store = input_value if isinstance(input_value, dict) else {}
    # Sessions we've seen in the store at least once. Distinguishes a brand-NEW session
    # (add it) from one the user just CLOSED - its window X deleted the key from the held
    # dict (draw_collection, by reference), but its tmux session is still live. Marking
    # EVERY current store key covers "+"-created terminals too (they add themselves).
    seen = getattr(draw_state, "_seen_sessions", None)
    if seen is None:
        seen = draw_state._seen_sessions = set()
    # Sessions we've already DEALT WITH (PTY ended on its own, or we just killed one) and
    # are now waiting for the ~1s poller to drop from `live`. The live loop skips these so
    # a session lingering in a stale snapshot is neither re-killed every frame nor
    # re-attached as "new" - both spin a loop, visible (since the relauncher restarts a
    # killed session under a new pid → new name) and as windows "added over and over".
    dead = getattr(draw_state, "_dead_sessions", None)
    if dead is None:
        dead = draw_state._dead_sessions = set()
        

    # ── Drop a Terminal when its PTY actually ENDED (reader EOF) - immediate, not tied to
    # the poller snapshot, so a just-created "+" terminal isn't briefly dropped. A PTY
    # that ended on its OWN is not a user event: forget it in `seen` (so the live loop
    # below does NOT mistake it for a closed window and kill it) and park it in `dead`.
    for k, term in list(store.items()):
        if getattr(term, "is_dead", None) and term.is_dead():
            store.pop(k)
            seen.discard(k)
            dead.add(k)

    for k in store:
    
        seen.add(k)
    live = _live_sessions
    for s in live:
        if s in store or s in dead:
            continue            # held, or already dealt with - wait for the poller drop
        if s in seen:
            # OUT (change by reference): the user closed this terminal's window, so it's
            # missing from the held dict but its tmux session is still alive. Kill the session
            # (ends claude-d → closes the gnome window) and park it in `dead` so we don't
            # re-fire the kill every frame until the poller drops it from `live`.
            _kill_session(s)
            dead.add(s)
        else:
            # New external session -> add a Terminal (writer/reader start on render).
            store[s] = Terminal(_attach_cmd(s), tmux_session=s)
            seen.add(s)
    live_set = set(live)
    seen &= live_set | set(store)   # forget sessions that are neither live nor still held
    dead &= live_set                # forget dealt-with sessions once the poller drops them

    # ── VIEW: hand the dict to the host's view_func (it materializes + renders) ──
    edited, value = view_func(input_value=store, external_change=False, **kwargs)

    if store is not None:
        imgui.text(f"Found {len(store)} terminals")
    return edited, value


# ── the proxy: RenderHost wraps the wrapper; standalone -> renders in its own
# window via draw_main. To the program it's just a dict whose value is the
# {session: Terminal} map the wrapper materialized.
claude_proxy = RenderHost(io_function=claude_terminals_io, input_value=None,
                          name="Claude Terminals")


terminal_loop_running = False

# ── the renderer: draw each discovered terminal stacked in the host window ──────
@window(input_value=claude_proxy, tint=(0.112029,0.04592592,0.267), width=300, height=100)
@render_func(show_bg=False, use_cache=True, selectable=False)
def draw_claude_terminals(input_value, draw_state,  **kwargs):
    global terminal_loop_running
    if not terminal_loop_running:
        threading.Thread(target=_poll_loop, daemon=True, name="claude-sessions-poller").start()
        terminal_loop_running = True

    imgui.dummy(1, 20)


    # input_value is the proxy. The {session: Terminal} dict is held ONE LEVEL DOWN
    # under value_key ("value") - draw_collection on the proxy itself would only see
    # the single {"value": ...} key (and render that _BubblingDict, not the
    # Terminals). Pull the held dict out and draw its terminals.
    windows = input_value.get("value") if isinstance(input_value, dict) else {}
    if windows is None:
        windows = {}

    # Each Terminal mutates in place (stable identity), so the wrapper's cache won't
    # see new output on its own - the terminal's reader thread must invalidate THIS
    # window. Point them at our draw_state, and stash it so the poller can wake us
    # when a session appears/vanishes. See [[project_live_views_stable_identity]].

    global _window_ds
    _window_ds = draw_state
    for term in windows.values():
        term._ds = draw_state


    # draw_collection makes each terminal a value-key draw_state; view_func renders each
    # VALUE with draw_terminal_screen (the inline screen renderer). We pass view_func
    # rather than relying on is_default_for=Terminal → draw_terminal, because the
    # default is the @window bound to terminal_screen with a hardcoded screen name so
    # every terminal would collide on one draw_state and show the wrong content.
    dict_changed, new_val, draw_state = RenderFuncs.draw_collection(windows, return_extras=True, show_add_delete=True,
                                                                    close_triggers_delete=True, is_tree=False, wrap=True,
                                                                    name="New Terminal", disable_scroll=True,
                                                                    new_item_type=Terminal, temp=True, shadow=False,
                                                                    child_kwargs={"mode": Modes.TERMINAL_WINDOW, "swoosh":False,
                                                                                  "disable_scroll": True})
    
    imgui.text(f"{len(windows)} windows")
    
    # for window_ds in draw_state._children.values():
    #     imgui.text(f"{window_ds.name} {window_ds.closed} {window_ds._kwargs.get('initial', {})}")


    # for window_ds in draw_state._children.values():
    #     imgui.text(f"{window_ds.name} {window_ds.closed} {window_ds._kwargs.get('initial', {})}")

    return False, None


# ── discovery poller: the wrapper's body is blit-cached, so it wouldn't notice a
# session that appeared/vanished with no output to invalidate it. Poll tmux and,
# on a CHANGE to the session set, re-run the io_function (discovery) AND re-render
# the @window so a new/removed terminal shows up.
_live_sessions = []
_window_ds = None  # draw_claude_terminals' draw_state, stashed each render


def _poll_loop():
    global _live_sessions
    last = None
    while module_is_live(globals()):   # exits when a restart purges this module
        if Core.melty.frame_count < 4:
            time.sleep(2)
        # Resilient: this thread starts at startup - BEFORE GLFW is initialized - so an
        # early request_render() raises "GLFW library is not initialized" and (without
        # this guard) the whole poller thread exits, locking _live_sessions so new
        # sessions are never discovered. Swallow per-iteration errors and keep polling;
        # once GLFW is up, request_render works.
        try:
            cur = _list_claude_sessions()
            if cur != last:
                last = cur
                _live_sessions = []
                for ds in (getattr(claude_proxy, "_wrapper_draw_state", None),
                           getattr(claude_proxy, "_draw_state", None), _window_ds):
                    if ds is not None:
                        ds.invalidate()
                request_render()
        except Exception:
            pass
        time.sleep(1.0)