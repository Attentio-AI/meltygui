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
import threading

import meltygui
from meltygui import glfw_window, pressed, window_api
from meltygui.core.melty import Melty
from meltygui.core.core_render import render_func
from meltygui.core.runtime.app import _draw_root
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


def hand_over_to_running_instance(socket_path):
    """If a UI is already running, bring its window forward and exit."""
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1.0)
            client.connect(str(socket_path))
            client.sendall(b'show\n')
            client.recv(16)
        return True
    except OSError:
        return False


def serve_instance(socket_path):
    """Answer later launches: 'show' brings the window back."""
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(socket_path))
    server.listen(2)
    server.settimeout(0.2)
    stopped = threading.Event()

    def loop():
        while not stopped.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                try:
                    if conn.recv(64).strip().startswith(b'show'):
                        show_window()
                    conn.sendall(b'ok\n')
                except OSError:
                    pass
    worker = threading.Thread(target=loop, daemon=True, name='melty-claude-instance')
    worker.start()

    def close():
        stopped.set()
        server.close()
        worker.join(timeout=1.5)
        socket_path.unlink(missing_ok=True)

    return close


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


@render_func()
def draw_chat_window(input_value: dict, draw_state=None):
    """Lay out the reusable chat within the standalone window's content area."""
    left, top, right, bottom = draw_state.get_content_rect()
    draw_claude_chat(None, new_project=input_value['new_project'],
                     default_project=input_value['default_project'],
                     width=right - left, height=bottom - top)
    return False, input_value


def main(argv=None):
    """Run the standalone chat; importing this module never starts the UI."""
    import argparse

    parser = argparse.ArgumentParser(prog='melty-claude', description=__doc__)
    parser.add_argument('project', nargs='?', type=pathlib.Path)
    args = parser.parse_args(argv)
    project = args.project.expanduser().resolve() if args.project else None
    if project is not None and not project.is_dir():
        parser.error(f'not a directory: {project}')
    state = dict(new_project=str(project) if project else None,
                 default_project=str(pathlib.Path.cwd()))
    instance = os.environ.get('MELTY_CLAUDE_INSTANCE')
    socket_path = (pathlib.Path(os.environ.get('XDG_RUNTIME_DIR') or '/tmp')
                   / f"{APP_ID}-{os.getuid()}{('-' + instance) if instance else ''}.sock")
    if hand_over_to_running_instance(socket_path):
        return 0
    close_instance = serve_instance(socket_path)
    try:
        # Window registration and runtime state belong to this invocation.
        @glfw_window(name='Claude Code', width=1180, height=820, app_id=APP_ID,
                     with_header=draw_header, show_name=True,
                     tint=(0.3455185649779721, 0.37840735777949847, 0.44))
        def claude():
            if os.environ.get('MELTY_FRAMETIME') and not state.get('instrumented'):
                state['instrumented'] = True
                _instrument()
            _draw_root(draw_chat_window, 'Claude Code', value=state,
                       with_header=draw_header, show_name=True,
                       tint=(0.3455185649779721, 0.37840735777949847, 0.44))
            # This is the uncached native-window callback, outside the cached view.
            if pressed('escape'):
                stop_running()
            elif pressed('ctrl+q'):
                window_api.set_window_should_close(Melty.glfw_window, True)

        meltygui.run()
    finally:
        try:
            close_instance()
        finally:
            disconnect_chats()
    return 0
