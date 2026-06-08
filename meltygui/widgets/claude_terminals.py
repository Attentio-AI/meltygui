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

import shlex
import subprocess
import threading
import time

import imgui

from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.render_host import RenderHost
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.playground.terminal_playground import Terminal, draw_terminal_screen

_SESSION_PREFIX = "claude-d-"


def _list_claude_sessions():
    """The live tmux sessions started by `claude-d` (by name prefix). [] on any
    failure (no tmux server, none running)."""
    try:
        out = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return []
    return sorted(s for s in out.split() if s.startswith(_SESSION_PREFIX))


def _attach_cmd(session):
    """Argv that attaches the in-app PTY to an existing claude-d session (a second,
    shared client — the session was created by `claude-d`, we just view/drive it).
    `window-size latest` makes our view the active client so output wraps to us."""
    return ["bash", "-c",
            "tmux set -g window-size latest 2>/dev/null; "
            "exec tmux attach-session -t " + shlex.quote(session)]


def _kill_session(session):
    """Kill a claude-d tmux session off-thread. This ends the `claude-d` process
    running it (its EXIT/HUP trap fires) → the gnome window closes, and the session
    leaves the tmux server so the poller drops it from `_live_sessions`."""

    def go():
        try:
            subprocess.run(["tmux", "kill-session", "-t", session],
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=2)
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()


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
    for k in store:
        seen.add(k)
    live = _live_sessions
    for s in live:
        if s not in store:
            if s in seen:
                # OUT (change by reference): the user closed this terminal's window, so
                # it's gone from the held dict but its tmux session is still alive. Kill
                # the session (ends claude-d → closes the gnome window); the poller then
                # drops it from `live`, so we do NOT re-add the store here.
                _kill_session(s)
            else:
                # New external session -> add a Terminal (PTY/reader start on render).
                store[s] = Terminal(_attach_cmd(s), tmux_session=s)
                seen.add(s)
    # Drop the terminal when its PTY has ENDED (reader EOF) - immediate, and not tied
    # to the ~1s poller loop, so a just-created "+" terminal whose session the poller
    # hasn't scanned yet isn't briefly dropped (the flicker). `is_dead` is False until
    # the reader has started and then exited.
    for k, term in list(store.items()):
        if getattr(term, "is_dead", None) and term.is_dead():
            store.pop(k)
    seen &= set(live) | set(store)  # drop sessions that are neither live nor still held

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
@window(input_value=claude_proxy, tint=(0.306977,0.09673172,0.04568956))
@render_func(show_bg=False, use_cache=True, selectable=False)
def draw_claude_terminals(input_value, draw_state, **kwargs):
    global terminal_loop_running
    if not terminal_loop_running:
        threading.Thread(target=_poll_loop, daemon=True, name="claude-sessions-poller").start()
        terminal_loop_running = True


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

    imgui.text(f"Terminal Windows: {len(windows)}")

    # draw_collection makes each terminal a value-key draw_state; view_func renders each
    # VALUE with draw_terminal_screen (the inline screen renderer). We pass view_func
    # rather than relying on is_default_for=Terminal → draw_terminal, because the
    # default is the @window bound to terminal_screen with a hardcoded screen name so
    # every terminal would collide on one draw_state and show the wrong content.
    dict_changed, new_val, draw_state = RenderFuncs.draw_collection(windows, return_extras=True, show_add_delete=True,
                                                                    close_triggers_delete=True,
                                                                    name="Claude Sessions", disable_scroll=True,
                                                                    new_item_type=Terminal, temp=True,
                                                                    child_kwargs={"mode": Modes.TERMINAL_WINDOW,
                                                                                  "disable_scroll": True})

    for window_ds in draw_state._children.values():
        imgui.text(f"{window_ds.name} {window_ds.closed} {window_ds._kwargs.get('initial', {})}")

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
    while True:
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
                _live_sessions = cur
                for ds in (getattr(claude_proxy, "_wrapper_draw_state", None),
                           getattr(claude_proxy, "_draw_state", None), _window_ds):
                    if ds is not None:
                        ds.invalidate()
                request_render()
        except Exception:
            pass
        time.sleep(1.0)


