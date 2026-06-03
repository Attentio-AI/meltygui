"""In-process MCP server for the launcher (model_server).

The launcher process is the long-lived supervisor: it stays up across studio
sessions (and is itself kept alive by the run loop). Hosting the MCP server here
— rather than in the short-lived Melty studio session — means it's reachable
whenever the launcher is, with no process killing and no race with the run loop.

Two entry points, both called from ``model_server.py``'s ``__main__``:

* ``install_log_tee()`` — tee stdout/stderr to a logfile so ``get_logs`` can
  read the console across studio open/close.
* ``start_launcher_mcp(model_server)`` — run a FastMCP streamable-http server in
  a daemon thread, exposing tools that drive the launcher in-place:
  ``get_logs``, ``status``, ``launch``, ``restart``.

Tools call thread-safe methods on the ModelServer (``mcp_launch`` etc.), which
queue work on the existing task queue — the same path as the Ctrl+Enter re-run.
"""

import functools
import re
import sys
import threading
import traceback
from pathlib import Path

# Project root: src/lsd/gl_gui/mcp_server.py -> parents[3] == latent-descent/
_ROOT = Path(__file__).resolve().parents[3]
STATE_DIR = _ROOT / ".melty"
LOG_PATH = STATE_DIR / "console.log"

import os

from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

PORT = int(os.environ.get("MELTY_MCP_PORT", "8787"))
HOST = "127.0.0.1"

_tee_installed = False
_mcp_started = False

# Cap stored result/error text so a chatty tool (get_logs returning 200 lines)
# can't balloon the in-memory log.
_LOG_RESULT_CAP = 2000

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Markers that begin an error region, in this codebase's three formats:
#  - "Exception Trace": print_stack_trace(exception=...) - a ─-bar-delimited
#    block with frames, variable-watch tables, and a final "ExcType: msg".
#  - "Traceback (most recent call last):": standard (thread handlers, log_exc).
#  - "Error Caught": the older print_colored_traceback banner.
_PST_TITLE = "Exception Trace"
_FALLBACK_MARKERS = ("Traceback (most recent call last):", "Error Caught")


def _is_bar(line):
    s = line.strip()
    return len(s) >= 20 and set(s) == {"─"}


def _extract_last_error(text, cap=200):
    """Return the most recent error block from `text` (ANSI stripped), or None.

    Picks whichever error format appears latest in the log.
    """
    lines = [_ANSI.sub("", l) for l in text.splitlines()]

    # Latest marker of any format.
    pst = std = None
    for i in range(len(lines) - 1, -1, -1):
        if pst is None and _PST_TITLE in lines[i]:
            pst = i
        if std is None and any(mk in lines[i] for mk in _FALLBACK_MARKERS):
            std = i
        if pst is not None and std is not None:
            break
    if pst is None and std is None:
        return None

    # print_stack_trace block: from the ─-bar above the title to the closing bar.
    if pst is not None and (std is None or pst > std):
        bars = [i for i, l in enumerate(lines) if _is_bar(l)]
        before = [b for b in bars if b < pst]
        after = [b for b in bars if b > pst]
        b0 = before[-1] if before else pst
        # after[0] is the bar right after the title; after[1] is the closing bar.
        b2 = after[1] if len(after) >= 2 else (after[0] if after else len(lines) - 1)
        block = lines[b0:b2 + 1]
        return "\n".join(block[:cap])

    # Fallback: cut from the marker to its exception message line.
    block, seen_frame, j = [], False, std
    while j < len(lines) and len(block) < cap:
        l = lines[j]
        s = l.strip()
        is_marker = any(mk in l for mk in _FALLBACK_MARKERS)
        is_file = s.startswith('File "')
        is_indented = l[:1] in (" ", "\t")
        if is_marker or is_file or is_indented or s == "":
            block.append(l)
            if is_file or is_indented:
                seen_frame = True
            j += 1
            continue
        block.append(l)  # the message line
        if seen_frame:
            break
        j += 1
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block) if block else None


class _Tee:
    """Write to the real stream and the logfile at once. Thread-safe.

    Falls back gracefully if either sink raises so logging can never take the
    process down.
    """

    def __init__(self, stream, fh, lock):
        self._stream = stream
        self._fh = fh
        self._lock = lock

    def write(self, data):
        with self._lock:
            try:
                self._stream.write(data)
            except Exception:
                pass
            try:
                self._fh.write(data)
                self._fh.flush()
            except Exception:
                pass

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass
        try:
            self._fh.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_log_tee():
    """Mirror stdout/stderr into LOG_PATH (fresh per process). Idempotent."""
    global _tee_installed
    if _tee_installed:
        return
    _tee_installed = True
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(LOG_PATH, "w", buffering=1)
        lock = threading.Lock()
        sys.stdout = _Tee(sys.__stdout__, fh, lock)
        sys.stderr = _Tee(sys.__stderr__, fh, lock)
    except Exception as e:
        print(f"[mcp] could not install log tee: {e}")


def start_launcher_mcp(model_server, host=HOST, port=PORT):
    """Start the launcher MCP server in a daemon thread. Idempotent."""
    global _mcp_started
    if _mcp_started:
        return
    _mcp_started = True

    install_log_tee()

    try:
        import asyncio
        import logging
        import uvicorn
        from mcp.server.fastmcp import FastMCP, Image
    except Exception as e:
        print(f"[mcp] not starting — dependencies unavailable: {e}")
        return

    # Keep MCP/uvicorn/etc logging out of the launcher console (and the
    # tee'd logfile that get_logs reads).
    for _name in ("uvicorn", "uvicorn.error", "uvicorn.access",
                  "mcp", "mcp.server", "sse_starlette"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    mcp = FastMCP("latent-descent-launcher", host=host, port=port)

    def logged_tool():
        """Like ``mcp.tool()`` but records each call to ``MCPServerLog.logs``.

        ``functools.wraps`` copies ``__wrapped__`` so FastMCP's
        ``inspect.signature`` still resolves the original signature, name, and
        docstring — the tool schema is unchanged.
        """

        def deco(fn):
            from src.lsd.gl_gui.view.core_views.monitor import MCPServerLog

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                # Bind positional args to names so the log is self-describing.
                call_args = dict(kwargs)
                names = list(fn.__code__.co_varnames[:fn.__code__.co_argcount])
                for name, val in zip(names, args):
                    call_args[name] = val
                try:
                    result = fn(*args, **kwargs)
                except Exception:
                    MCPServerLog.record(fn.__name__, call_args,
                                        error=traceback.format_exc())
                    raise
                MCPServerLog.record(fn.__name__, call_args, result=result)
                return result

            return mcp.tool()(wrapper)

        return deco

    @logged_tool()
    def get_logs(lines: int = 200) -> str:
        """Return the last `lines` lines of the launcher's console output.

        Captures stdout + stderr for the launcher and any studio session it
        runs. Use this to read tracebacks, training progress, or warnings.
        """
        if not LOG_PATH.exists():
            return "(no log file yet)"
        rows = LOG_PATH.read_text(errors="replace").splitlines()
        if not rows:
            return "(log is empty)"
        return "\n".join(rows[-max(1, lines):])

    @logged_tool()
    def status() -> str:
        """Report the launcher PID and whether a studio session is running."""
        return model_server.mcp_status()

    @logged_tool()
    def last_error() -> str:
        """Return the most recent traceback from the console log, or a note that
        the run looks clean. Faster than scanning get_logs after a crash."""
        if not LOG_PATH.exists():
            return "(no log file yet)"
        block = _extract_last_error(LOG_PATH.read_text(errors="replace"))
        if block is None:
            return "no traceback found in the current run's log (looks clean)"
        return block

    @logged_tool()
    def launch() -> str:
        """Open the latent-descent studio by replaying the last run.

        No-op (with a message) if a studio session is already running.
        """
        return model_server.mcp_launch()

    @logged_tool()
    def restart() -> str:
        """Restart the studio session in place: interrupt the running session
        (if any) and replay the last run. The launcher process stays up, so the
        MCP connection survives — just call get_logs afterward.
        """
        return model_server.mcp_restart()

    @logged_tool()
    def screenshot(window: str):
        """Capture one Melty studio window by name and return it as a PNG image.

        Pass the window's title (exact, else case-insensitive substring). Use
        list_windows to see what's open. Captures a single window rather than
        the whole display, which may span an ultra-wide monitor.
        """
        if not model_server._studio_running():
            return "no studio session running — call launch first"
        from src.lsd.gl_gui.screenshot import request_capture
        path, error = request_capture(window)
        if error:
            return f"screenshot failed: {error}"
        return Image(path=path)

    @logged_tool()
    def list_windows() -> str:
        """List the names of currently-open Melty studio windows."""
        if not model_server._studio_running():
            return "no studio session running — call launch first"
        from src.lsd.gl_gui.screenshot import list_window_names
        names = list_window_names()
        return "\n".join(sorted(set(names))) if names else "(no named windows open)"

    @logged_tool()
    def eval_python(code: str) -> str:
        """Execute Python in the live launcher process; returns stdout + result.

        Runs on the studio render thread when a session is running (safe to
        read/poke Melty + imgui state), else inline on the launcher thread.
        In scope: `Melty`, `server`/`model_server`, and (when a studio is up)
        `vis` (the studio) and `app`/`root` (the root AppModel). Import anything
        else. A trailing expression's repr is returned. Arbitrary in-process
        code — for inspecting/poking live state while iterating.
        """
        from src.lsd.gl_gui.mcp_eval import request_eval
        return request_eval(code, model_server)

    @logged_tool()
    def restart_launcher() -> str:
        """Fully restart the launcher process (not just the studio session).

        Use this to pick up newly-added MCP tools or changes to launcher/MCP
        startup code — an in-place `restart` only replays the studio and can't
        register new tools. The run loop relaunches the process; reconnect after
        ~15-20s. For ordinary rendering/converter code edits, prefer `restart`.
        """
        return model_server.mcp_restart_launcher()

    def _run():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            app = mcp.streamable_http_app()
            config = uvicorn.Config(app, host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None  # off the main thread
            loop.run_until_complete(server.serve())
        except Exception as e:
            print(f"[mcp] server thread crashed: {e}")

    threading.Thread(target=_run, daemon=True, name="launcher-mcp").start()
    print(f"[mcp] launcher MCP listening on http://{host}:{port}/mcp")
