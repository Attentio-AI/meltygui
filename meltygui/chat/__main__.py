#!/usr/bin/env python3
"""melty-claude — a Claude Code client built on meltygui.

    python -m meltygui.chat [PROJECT]   the window. With
                                PROJECT (a directory) every new conversation
                                runs there; without, a new conversation runs
                                in the selected one's project (the cwd when
                                none is selected)

The window is `meltygui.chat.draw_claude_chat` (the chat interface over the
detached chat service) as an OS window: a
source bar of tabs (Claude Code; the other registered kinds show too;
Shift+click shows several at once), a sidebar of conversations grouped by
project (age filter chips above it: 1h / 2h / Day / 2 days / All; a running
conversation shows a live dot), the transcript (pictures inline, HDR when
the desktop is), the composer, and approval prompts when Claude wants to
run a tool. The conversations are Claude Code's own sessions
(~/.claude/projects), read and driven through the Claude Agent SDK by the
backend in `meltygui.chat.claude_code` (install `meltygui[claude]`); `claude` on PATH is the process behind a turn.

Keys: Ctrl+Enter sends the draft (the Send button does too); Escape stops
the running turn; Ctrl+Q closes the UI.

Conversations run in a detached chat service, independent of this UI process.
Closing the window, Ctrl+Q, or killing the UI leaves accepted turns running.
Reopening reconnects to their live state, including pending approvals.
The service must remain alive; rebooting or killing the service stops its work.

"""
import os
import pathlib
import socket
import sys
import threading

import meltygui
from meltygui import glfw_window, pressed, window_api
from meltygui.core.melty import Melty
from meltygui.core.core_render import render_func
from meltygui.view.header_view import draw_header
from meltygui.chat import draw_claude_chat, stop_running, disconnect_chats

# The app id keeps melty-claude's saved session and single-instance socket.
APP_ID = 'melty-claude'


def _instrument():
    """MELTY_FRAMETIME=1: time the frame's stages and the chat window's parts
    from the app side: one line per frame on stdout (ms). The budget for
    120 fps is 8.3 ms; a 297-message transcript measures ~6.5 ms median."""
    import time
    from meltygui.view import chat_view as ui
    marks = {}

    def timed(label, fn):
        def run(*args, **kwargs):
            t = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                marks[label] = marks.get(label, 0.0) + (time.perf_counter() - t) * 1000
        return run
    from meltygui.view import chat_sidebar_view
    chat_sidebar_view.draw_chat_sidebar = timed('sidebar', chat_sidebar_view.draw_chat_sidebar)
    ui.draw_messages = timed('messages', ui.draw_messages)
    from meltygui.view import text_view
    text_view.draw_text = timed('composer+editors', text_view.draw_text)
    end_frame, post_frame = Melty.end_frame, Melty.post_frame
    Melty.end_frame = classmethod(lambda cls, *a, **k: timed('end_frame', end_frame)(*a, **k))

    def post(cls, *a, **k):
        t = time.perf_counter()
        try:
            return post_frame(*a, **k)
        finally:
            marks['post_frame'] = (time.perf_counter() - t) * 1000
            print('melty-claude stages: ' + ' '.join(f'{k}={v:.1f}' for k, v in marks.items()), flush=True)
            marks.clear()
    Melty.post_frame = classmethod(post)


if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] in ('-h', '--help')):
    print(__doc__.strip())
    sys.exit(2)
project = pathlib.Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) == 2 else None
if project is not None and not project.is_dir():
    sys.exit(f'melty-claude: not a directory: {project}')

state = dict(new_project=str(project) if project else None, default_project=str(pathlib.Path.cwd()))

# One instance per user; MELTY_CLAUDE_INSTANCE=name runs a separate one (tests, a second desktop).
SOCKET = (pathlib.Path(os.environ.get('XDG_RUNTIME_DIR') or '/tmp')
          / f"{APP_ID}-{os.getuid()}{('-' + os.environ['MELTY_CLAUDE_INSTANCE']) if os.environ.get('MELTY_CLAUDE_INSTANCE') else ''}.sock")


def hand_over_to_running_instance():
    """If a UI is already running, bring its window forward and exit."""
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1.0)
            client.connect(str(SOCKET))
            client.sendall(b'show\n')
            client.recv(16)
        return True
    except OSError:
        return False


def serve_instance():
    """Answer later launches: 'show' brings the window back."""
    try:
        SOCKET.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(SOCKET))
    server.listen(2)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    if conn.recv(64).strip().startswith(b'show'):
                        show_window()
                    conn.sendall(b'ok\n')
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True, name='melty-claude-instance').start()


def show_window():
    from meltygui.core.windowing.glfw_utils import request_render

    def show():
        window = Melty.glfw_window
        if window is not None:
            window_api.show_window(window)
            # GLFW can also ask the compositor for focus; meltygui's native
            # Wayland backend has no activation request yet.
            if not window_api.is_native_window(window):
                window_api.focus_window(window)
    Melty.post_to_render(show)
    request_render()


if hand_over_to_running_instance():
    sys.exit(0)
serve_instance()


# name is the window's name and its OS title, width / height its content size
# (meltygui's universal names, as on @window and every view). The melty header
# in the chrome row shows the name and the tint chip; the body lays out
# under it, sized from get_content_rect().
def close_requested(surface):
    """The detached chat service owns ongoing work; closing only ends this UI."""
    return True


@glfw_window(name='Claude Code', width=1180, height=820, app_id=APP_ID, on_close=close_requested,
             with_header=draw_header, show_name=True, tint=(0.3455185649779721, 0.37840735777949847, 0.44))
@render_func()
def claude(_, draw_state):
    left, top, right, bottom = draw_state.get_content_rect()
    if os.environ.get('MELTY_FRAMETIME') and not state.get('instrumented'):
        state['instrumented'] = True
        _instrument()            # after boot: the chat views are imported by now
    draw_claude_chat(None, new_project=state['new_project'], default_project=state['default_project'],
                     width=right - left, height=bottom - top)
    # pressed() sees every key event of the frame, typed text included, so
    # our keys are chords a text field never types.
    if pressed('escape'):
        stop_running()
    elif pressed('ctrl+q'):
        window_api.set_window_should_close(Melty.glfw_window, True)
    return False, None


meltygui.run()
disconnect_chats()
