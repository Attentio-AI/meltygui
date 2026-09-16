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
import logging
import re
import sys
import threading
import time
import traceback
from pathlib import Path

# Project root: src/lsd/gl_gui/mcp_server.py -> parents[3] == latent-descent/
from meltygui.core.runtime.paths import cache_root
_ROOT = cache_root()
STATE_DIR = _ROOT
LOG_PATH = STATE_DIR / "console.log"

import os

from meltygui.core.rendering.window_decoration import window

PORT = int(os.environ.get("MELTY_MCP_PORT", "8787"))
HOST = "127.0.0.1"

_tee_installed = False
_mcp_started = False

# The uvicorn server + its event loop, captured in _run so the Melty lifecycle
# handlers can reach in and drop connections without stopping the listener (the
# launcher can stay reachable across studio sessions, e.g. for `launch`/`status`
# while idle).
_uvicorn_server = None
_uvicorn_loop = None
# Set while Melty is tearing down: the gate closes during the drop so a fresh
# request can't re-hang the launcher. Auto-clears shortly after the drop (see
# notify_melty_shutdown), so the launcher is reachable again while idle.
_draining = threading.Event()
# Drop requests until Melty has painted this many frames - see _serving_ready.
WARMUP_FRAMES = 3

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


class _HttpRequestLine(logging.Handler):
    """Prints httpx's per-response INFO record as ONE short console line:

        [http] 20:26:36 GET localhost:11434/api/tags 200

    httpx logs `'HTTP Request: %s %s "%s %d %s"'` with args (method, url,
    http_version, status, reason); the scheme is dropped and the reason kept
    only for non-2xx/3xx answers (`… 429 Too Many Requests`). Any other record
    on the logger falls back to its plain message. Goes through print() so
    the log tee mirrors it into console.log like every other line.
    """

    def emit(self, record):
        try:
            args = record.args if isinstance(record.args, tuple) else ()
            if len(args) == 5 and str(record.msg).startswith("HTTP Request:"):
                method, url, _version, status, reason = args
                url = re.sub(r"^https?://", "", str(url))
                status = int(status)
                tail = f"{status}" if status < 400 else f"{status} {reason}"
                text = f"{method} {url} {tail}"
            else:
                text = record.getMessage()
            print(f"[http] {time.strftime('%H:%M:%S')} {text}")
        except Exception:
            pass


_http_logging_installed = False


def install_concise_http_logging():
    """Route the `httpx` logger to _HttpRequestLine and stop it propagating,
    so the request lines never reach whatever handler sits on the root.
    Idempotent."""
    global _http_logging_installed
    if _http_logging_installed:
        return
    _http_logging_installed = True
    http_logger = logging.getLogger("httpx")
    http_logger.setLevel(logging.INFO)
    http_logger.propagate = False
    http_logger.addHandler(_HttpRequestLine())


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


# --- MCP activity toasts ----------------------------------------------------
# Every tool call pops a toast in the studio (tag "MCP") so Claude and
# the live process is visible. Color-coded per tool, red on error. notify() fires
# UNCONDITIONALLY on every call: it's thread-safe, no-op in its render wake when
# no window exists, and an idle-time call just appends to the bounded deque
# (maxlen 20) and shows on the next session - no studio gate.
MCP_TOOL_TINTS = {
    "get_logs":         (0.55, 0.70, 0.95, 1.0),
    "status":           (0.45, 0.80, 1.00, 1.0),
    "last_error":       (1.00, 0.65, 0.30, 1.0),
    "launch":           (0.40, 1.00, 0.55, 1.0),
    "restart":          (1.00, 0.80, 0.35, 1.0),
    "restart_launcher": (1.00, 0.55, 0.40, 1.0),
    "hotswap":          (0.55, 0.90, 1.00, 1.0),
    "recompile_external_changes": (0.55, 1.00, 0.75, 1.0),
    "screenshot":       (0.80, 0.60, 1.00, 1.0),
    "list_windows":     (0.70, 0.75, 0.85, 1.0),
    "eval_python":      (0.45, 0.95, 0.80, 1.0),
    "find_views":       (0.60, 0.85, 0.95, 1.0),
    "describe_view":    (0.60, 0.85, 0.95, 1.0),
    "hit_test":         (0.95, 0.75, 0.55, 1.0),
    "param_sources":    (0.85, 0.70, 0.95, 1.0),
    "tile_cache":       (0.70, 0.95, 0.60, 1.0),
}
MCP_DEFAULT_TINT = (0.70, 0.72, 0.82, 1.0)
MCP_ERROR_TINT = (1.00, 0.35, 0.35, 1.0)


def _mcp_toast_text(name, call_args):
    """Compact one-line summary of a tool call for the notification toast:
    the tool name plus its args, each value collapsed to a single line and
    truncated so a big `source`/`code` payload can't blow up the toast."""
    def short(v):
        s = " ".join(str(v).split())
        return s if len(s) <= 40 else s[:40] + "..."
    detail = ", ".join(f"{k}={short(v)}" for k, v in call_args.items())
    return f"{name}({detail})" if detail else f"{name}()"


def start_launcher_mcp(model_server, host=HOST, port=PORT):
    """Start the launcher MCP server in a daemon thread. Idempotent."""
    global _mcp_started
    if _mcp_started:
        return
    _mcp_started = True

    install_log_tee()

    try:
        import asyncio
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

    # FastMCP's constructor calls logging.basicConfig(level=INFO) with a rich
    # handler on the ROOT logger, which then rendered every library INFO
    # record - httpx's `HTTP Request: GET http://... "HTTP/1.1 200 OK"` for each
    # Ollama probe / Anthropic call - as a wide rich line with file-link
    # escapes, or one character per line when it misjudged the tee's width.
    # Snapshot the root logger before the constructor and set it back, then
    # give httpx its own one-line handler (install_concise_http_logging).
    root_logger = logging.getLogger()
    root_handlers, root_level = list(root_logger.handlers), root_logger.level
    mcp = FastMCP("meltygui", host=host, port=port)
    for handler in list(root_logger.handlers):
        if handler not in root_handlers:
            root_logger.removeHandler(handler)
    root_logger.setLevel(root_level)
    install_concise_http_logging()

    def logged_tool():
        """Like ``mcp.tool()`` but records each call to ``MCPServerLog.logs``.

        ``functools.wraps`` copies ``__wrapped__`` so FastMCP's
        ``inspect.signature`` still resolves the original signature, name, and
        docstring — the tool schema is unchanged.
        """

        def deco(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                # Resolve notify/MCPServerLog PER CALL, not at registration. The
                # tool wrappers are registered once at launcher boot; a later
                # hotswap of notifications.py / monitor.py re-imports the module and
                # makes a NEW NotificationCenter class (with fresh deques) that the
                # render loop reads. A registered-level `import` would keep
                # appending to the OLD, pre-hotswap class - toasts land in an
                # orphaned deque nothing draws (the post-hotswap silent-toast bug).
                # Re-importing here always hits the live sys.modules entry.
                from meltygui.core.diagnostics.monitor_core import MCPServerLog
                from meltygui.core.diagnostics.notifications import notify
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
                    notify(_mcp_toast_text(fn.__name__, call_args) + " (error)",
                           tint=MCP_ERROR_TINT, tag="MCP")
                    raise
                MCPServerLog.record(fn.__name__, call_args, result=result)
                notify(_mcp_toast_text(fn.__name__, call_args),
                       tint=MCP_TOOL_TINTS.get(fn.__name__, MCP_DEFAULT_TINT),
                       tag="MCP")
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
        """Report the launcher PID, whether a studio session is running, and —
        if it isn't — why the last session stopped (user_quit / restart / crash).

        Use the reason to tell an expected exit (the user closed or restarted the
        window) from a crash worth investigating, instead of treating every idle
        studio as a bug.
        """
        import meltygui.core.diagnostics.session_status as session_status
        return f"{model_server.mcp_status()}; {session_status.summary()}"

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
    def hotswap(path: str, source: str = "") -> str:
        """Recompile a project source file and hotswap it into the running studio
        process — apply code changes live, no full restart.

        path:   absolute or project-relative path to a .py file whose module is
                already imported in the running process.
        source: optional full new file contents. Omit it to reload the file's
                current on-disk contents (e.g. after editing it on disk). If
                given, it is hotswapped first and written to disk only on a clean
                compile, so a syntax error never leaves broken code on disk.

        The whole module is reloaded: every function/class in the file is patched
        in place, so imported names and live instances keep working. A swap that
        compiles but throws at runtime is auto-reverted by the editor's hotswap
        guard. Library/stdlib paths are refused. Returns a status line.
        """
        from meltygui.core.diagnostics.notifications import notify
        notify(f"hotswap requested: {path}", tint=MCP_TOOL_TINTS.get("hotswap", MCP_DEFAULT_TINT), tag="MCP")
        import meltygui.core.automation.mcp_hotswap as mcp_hotswap
        return mcp_hotswap.hotswap_file(path, source or None)

    @logged_tool()
    def recompile_external_changes() -> str:
        """Recompile everything the studio has queued — pending edits AND
        tracked external changes — the SAME code path as clicking the Pending
        Saves window's recompile button (PendingSave.recompile_all_ui drives
        the button's own runner draw_state: busy spinner while running, then
        the fading check mark + summary), so the button and this tool always
        behave identically. The window is revealed so the result is visible.

        External changes are first decomposed into per-span pending entries
        and merged with any overlapping pending edits — rebase / per-span
        3-way merge / adopt (MERGED / ADOPTED / CONFLICT lines land in the
        window's persistent merge display); the external window keeps showing
        absorbed drift until the user dismisses it. Each entry then hotswaps
        in place with hotswap-guard rollback. Returns the same summary string
        the button shows.
        """
        from meltygui.core.diagnostics.notifications import notify
        notify("recompile requested",
               tint=MCP_TOOL_TINTS.get("recompile_external_changes", MCP_DEFAULT_TINT), tag="MCP")
        from meltygui.editor.pending_save import PendingSave
        return PendingSave.recompile_all_ui()

    @logged_tool()
    def screenshot(window: str):
        """Capture one Melty studio window by name and return it as a PNG image.

        Pass the window's title (exact, else case-insensitive substring). Use
        list_windows to see what's open. Captures a single window rather than
        the whole display, which may span an ultra-wide monitor.
        """
        from meltygui.core.diagnostics.notifications import notify
        notify(f"screenshot requested: {window}", tint=MCP_TOOL_TINTS.get("screenshot", MCP_DEFAULT_TINT), tag="MCP")
        if not model_server._studio_running():
            return "no studio session running — call launch first"
        from meltygui.core.graphics.screenshot import request_capture
        path, error = request_capture(window)
        if error:
            return f"screenshot failed: {error}"
        return Image(path=path)

    @logged_tool()
    def list_windows() -> str:
        """List the names of currently-open Melty studio windows."""
        if not model_server._studio_running():
            return "no studio session running — call launch first"
        from meltygui.core.graphics.screenshot import list_window_names
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
        from meltygui.core.automation.mcp_eval import request_eval
        return request_eval(code, model_server)

    # --- Typed state queries (mcp_query.py): JSON read on the render thread ---

    def _query(collect):
        if not model_server._studio_running():
            return "no studio session running — call launch first"
        from meltygui.core.automation.mcp_query import run_query
        return run_query(collect, model_server)

    @logged_tool()
    def find_views(func: str = "", name: str = "", window: str = "",
                   include_closed: bool = False, limit: int = 50) -> str:
        """Find live Melty views (draw_states) by case-insensitive substring:
        `func` on the render function's qualname, `name` on the view name /
        tile_id, `window` on the ROOT window title (see list_windows). Rows
        are front-most first with rect, clip, closed / hidden / hovered flags,
        layer, z_pos, parent window and the input value's type. Use the
        returned `tile_id` with describe_view / param_sources / tile_cache."""
        from meltygui.core.automation.mcp_query import collect_find_views
        return _query(lambda: collect_find_views(func, name, window, include_closed, limit))

    @logged_tool()
    def describe_view(view: str, children_depth: int = 1) -> str:
        """One view in full: summary, resolved kwargs, diverged auto_params,
        event_rect scopes, this frame's event subscriptions and cursor
        registration, its tile-cache entry (dirty, last clean / invalidated
        frame, last bump reason), window_pos / content size / scroll, the
        render-tree ancestors and window chain, and children to
        `children_depth`. `view` = a tile_id (exact or unique substring) or
        a draw_state id prefix."""
        from meltygui.core.automation.mcp_query import collect_describe_view
        return _query(lambda: collect_describe_view(view, children_depth))

    @logged_tool()
    def hit_test(x: float, y: float) -> str:
        """The BVH stack at screen point (x, y), front to back, each view
        with its z_pos / priority and the event subscriptions + cursor shape
        registered for it. Subscriptions exist only for views under the REAL
        pointer (`pointer`, `pointer_matches_point`); elsewhere the stack is
        exact but subscriptions are empty. Also: Melty.hovered_ds, the
        resolved cursor shape, drag capture and blocker views."""
        from meltygui.core.automation.mcp_query import collect_hit_test
        return _query(lambda: collect_hit_test(x, y))

    @logged_tool()
    def param_sources(view: str, param: str = "") -> str:
        """The context menu's inputs tab as data: for each parameter of the
        view (or just `param`) the value it reads, the DRIVING source (the
        SourcePriority pick) and every source that sets it in priority order
        (kind, writable, value). `sources` lists the sources with file:line."""
        from meltygui.core.automation.mcp_query import collect_param_sources
        return _query(lambda: collect_param_sources(view, param))

    @logged_tool()
    def tile_cache(view: str = "", history_frames: int = 0, limit: int = 100) -> str:
        """Blit tile-cache state. With `view`: that tile (dirty, clean /
        invalidated cache frames, last bump, blit_served_frame, tracker note)
        and its invalidations over the last `history_frames` frames. Without:
        tile totals, per-frame body_runs / cache_hits / captures (last 10
        frames, or `history_frames`), the latest `limit` invalidations and the
        top invalidators over the window — a per-frame invalidator shows up
        here with a count near the frame count."""
        from meltygui.core.automation.mcp_query import collect_tile_cache
        return _query(lambda: collect_tile_cache(view, history_frames, limit))

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
        global _uvicorn_server, _uvicorn_loop
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            app = _make_gate(mcp.streamable_http_app())
            # timeout_graceful_shutdown=0: if the server ever does stop, never
            # wait on connections (we drop them explicitly via notify_melty_shutdown).
            config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                                    timeout_graceful_shutdown=0)
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None  # off the main thread
            _uvicorn_server = server
            _uvicorn_loop = loop
            loop.run_until_complete(server.serve())
        except Exception as e:
            print(f"[mcp] server thread crashed: {e}")

    threading.Thread(target=_run, daemon=True, name="launcher-mcp").start()
    print(f"[mcp] launcher MCP listening on http://{host}:{port}/mcp")


def _serving_ready():
    """True when the server should accept requests.

    Rejects during a Melty teardown drain, and until Melty has painted
    WARMUP_FRAMES frames (Melty.init_complete() == frame_count > 2) so clients
    can't poke a process whose GUI hasn't initialized. Fails OPEN if Melty isn't
    importable yet — the launcher tools (status/launch) must stay reachable to
    bring the studio up.
    """
    if _draining.is_set():
        return False
    try:
        from meltygui.core.melty import Melty
        return Melty.init_complete()
    except Exception:
        return True


def _make_gate(app):
    """ASGI wrapper rejecting HTTP requests with 503 until _serving_ready().

    Non-HTTP scopes (lifespan) pass through untouched so uvicorn startup/shutdown
    events still fire.
    """
    async def gate(scope, receive, send):
        if scope.get("type") == "http" and not _serving_ready():
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"connection", b"close")],
            })
            await send({"type": "http.response.body", "body": b"meltygui not ready"})
            return
        await app(scope, receive, send)

    return gate


def notify_melty_shutdown():
    """Drop all active MCP connections immediately, so a hanging client can't

    block Melty's teardown. Keeps the listener bound (the launcher stays
    reachable for the next session / idle `launch`), and clears the drain shortly
    after so new requests are served again. Safe to call if the server never
    started.
    """
    server, loop = _uvicorn_server, _uvicorn_loop
    if server is None or loop is None or loop.is_closed():
        return
    _draining.set()

    def _drop():
        try:
            # Same primitives uvicorn's own Server.shutdown uses: ask every live
            # connection to close, and cancel any in-flight request task that
            # would otherwise keep the teardown waiting.
            for conn in list(getattr(server.server_state, "connections", ())):
                conn.shutdown()
            for task in list(getattr(server.server_state, "tasks", ())):
                task.cancel()
        except Exception as e:
            print(f"[mcp] error dropping connections on meltygui shutdown: {e}")
        finally:
            loop.call_later(1.0, _draining.clear)

    try:
        loop.call_soon_threadsafe(_drop)
    except RuntimeError:
        # Loop already gone - nothing to drop.
        _draining.clear()
