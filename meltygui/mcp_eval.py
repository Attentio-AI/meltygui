"""Evaluate Python in the live launcher process via MCP.

When a studio session is running the snippet runs on the render thread (GL
context current, safe to read/poke Melty + imgui state) via a post_frame
hand-off, mirroring screenshot.py; otherwise it runs inline on the caller's
thread (fine for pure-Python inspection of launcher/server state).

`Melty` and `server`/`model_server` are pre-bound; import anything else you
need. A trailing expression's repr is returned alongside captured stdout/stderr.

This is arbitrary in-process code execution — intended for inspecting and
poking live state while iterating.
"""

import threading

_pending = []
_lock = threading.Lock()


def _build_namespace(extra=None):
    ns = {"__builtins__": __builtins__}
    try:
        from meltygui.runtime import Melty
        ns["Melty"] = Melty
        # Conveniences reachable from Melty: the studio (vis) and the root
        # AppModel (vis.root). Everything here derives from Melty.
        vis = getattr(Melty, "vis", None)
        if vis is not None:
            ns["vis"] = vis
            root = getattr(vis, "root", None)
            if root is not None:
                ns["app"] = root
                ns["root"] = root
    except Exception:
        pass
    if extra:
        ns.update(extra)
    return ns


def _run_code(code, extra=None):
    """Execute `code`, returning (stdout_text, result_repr_or_None, error_or_None).

    Tries to evaluate it as a single expression first (to capture a value);
    falls back to exec for statements.
    """
    import io
    import contextlib
    import traceback

    ns = _build_namespace(extra)
    buf = io.StringIO()
    result = None
    error = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            try:
                compiled = compile(code, "<eval_python>", "eval")
            except SyntaxError:
                compiled = None
            if compiled is not None:
                value = eval(compiled, ns)
                if value is not None:
                    result = repr(value)
            else:
                exec(compile(code, "<eval_python>", "exec"), ns)
        except Exception:
            error = traceback.format_exc()
    return buf.getvalue(), result, error


def request_eval(code, model_server, timeout=10.0):
    """Run `code` and return formatted text (where it ran + stdout + result/error)."""
    extra = {"server": model_server, "model_server": model_server}

    studio_running = False
    try:
        studio_running = model_server._studio_running()
    except Exception:
        pass

    if not studio_running:
        out, result, error = _run_code(code, extra)
        return _format(out, result, error, where="launcher thread (no studio)")

    req = {"code": code, "extra": extra, "event": threading.Event(),
           "out": "", "result": None, "error": None}
    with _lock:
        _pending.append(req)
    try:
        from meltygui.utils.glfw_utils import request_render
        request_render()  # force a frame so the request is served soon
    except Exception:
        pass

    if not req["event"].wait(timeout):
        with _lock:
            if req in _pending:
                _pending.remove(req)
        return f"timed out after {timeout:.0f}s waiting for the render thread"
    return _format(req["out"], req["result"], req["error"], where="render thread")


def request_call(fn, model_server, timeout=10.0):
    """Run the no-arg callable `fn` on the render thread (inline when no
    studio is running) and return `(result, error_text)`. The typed MCP
    query tools (mcp_query.py) ride this instead of eval_python's source
    string: same queue, same post_frame drain, a Python value back."""
    import traceback
    studio_running = False
    try:
        studio_running = model_server._studio_running()
    except Exception:
        pass
    if not studio_running:
        try:
            return fn(), None
        except Exception:
            return None, traceback.format_exc()

    req = {"fn": fn, "event": threading.Event(), "result": None, "error": None}
    with _lock:
        _pending.append(req)
    try:
        from meltygui.utils.glfw_utils import request_render
        request_render()
    except Exception:
        pass
    if not req["event"].wait(timeout):
        with _lock:
            if req in _pending:
                _pending.remove(req)
        return None, f"timed out after {timeout:.0f}s waiting for the render thread"
    return req["result"], req["error"]


def process_evals():
    """Run pending eval requests on the render thread. Call from post_frame."""
    import traceback
    with _lock:
        if not _pending:
            return
        reqs = _pending[:]
        _pending.clear()
    for req in reqs:
        if "fn" in req:
            try:
                req["result"] = req["fn"]()
            except Exception:
                req["error"] = traceback.format_exc()
        else:
            req["out"], req["result"], req["error"] = _run_code(req["code"], req["extra"])
        req["event"].set()


def _format(out, result, error, where):
    parts = [f"[ran on {where}]"]
    if out.strip():
        parts.append("stdout:\n" + out.rstrip())
    if result is not None:
        parts.append("result: " + result)
    if error:
        parts.append("error:\n" + error.rstrip())
    if len(parts) == 1:
        parts.append("(no output)")
    return "\n".join(parts)
