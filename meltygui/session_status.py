"""Shared studio session-status file, written by the studio and read by the MCP server.

The launcher's ``status`` tool can only tell whether a studio *thread* is alive
(running vs idle). It can't tell *why* a session ended: a clean user quit looks
the same as a crash. That ambiguity means a transient "studio idle" reads like a
bug to chase when it's usually the user closing or restarting the window.

This module is the bridge. The studio stamps a small JSON file on startup
(``state: "running"``) and again on shutdown (``state: "stopped"`` + a ``reason``
classifying the exit). The MCP server reads that file in its ``status`` tool, so
a caller can distinguish "the user quit" / "the user restarted" from "it crashed".

Reasons:
  user_quit   — the window close button (clean, expected)
  restart     — interrupt propagated into the render loop (KeyboardInterrupt/SystemExit)
  stopped     — programmatic close: Ctrl+Enter re-run (via cleanup_all) or launcher
                shutdown — not a crash
  crash       — an exception propagated out of the render loop (a bug worth chasing)

Only `crash` indicates a bug; the rest are expected, environmental exits.
"""

import json
import os
import time
from pathlib import Path

# session_status.py -> src/lsd/gl_gui/session_status.py -> parents[3] -> latent-descent/
# Same depth/root as mcp_server.py so both agree on the .melty location.
_ROOT = Path(__file__).resolve().parents[3]
STATE_DIR = _ROOT / ".melty"
STATUS_PATH = STATE_DIR / "session_status.json"


def _write(payload):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Stamp atomically so a concurrent reader never sees a half-written file.
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, STATUS_PATH)
    except Exception as e:
        # Status bookkeeping must never take down the studio.
        print(f"session_status: failed to write {STATUS_PATH}: {e}")


def mark_running(pid=None):
    """Record that a studio session has started its render loop."""
    _write({
        "state": "running",
        "pid": pid if pid is not None else os.getpid(),
        "started_at": time.time(),
    })


def mark_stopped(reason, error=None):
    """Record that the studio render loop has exited, and why.

    `reason` is one of user_quit / restart / crash / unknown. `error` is an
    optional short string (e.g. the exception message) for crashes.
    """
    payload = read() or {}
    payload.update({
        "state": "stopped",
        "reason": reason,
        "stopped_at": time.time(),
        "pid": os.getpid(),
    })
    if error is not None:
        payload["error"] = str(error)[:1000]
    _write(payload)


def read():
    """Return the parsed status dict, or None if missing/unreadable."""
    try:
        if not STATUS_PATH.exists():
            return None
        return json.loads(STATUS_PATH.read_text())
    except Exception:
        return None


def summary():
    """One-line human/agent-readable summary of the last recorded session state."""
    data = read()
    if not data:
        return "no session status recorded yet"
    state = data.get("state", "unknown")
    if state == "running":
        started = data.get("started_at")
        ago = f" ({int(time.time() - started)}s ago)" if started else ""
        return f"studio session running (pid {data.get('pid')}, started{ago})"
    reason = data.get("reason", "unknown")
    msg = f"studio session stopped — reason: {reason}"
    if data.get("error"):
        msg += f" — {data['error']}"
    return msg
