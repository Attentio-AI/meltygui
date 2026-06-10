"""Hotswap a project source file into the running process, for the MCP server.

Lets an MCP caller apply code edits to a live studio without a full restart. It
reuses the editor's hotswap machinery rather than re-implementing it:

* ``_recompile_module`` (file_converters.py) compiles the new source and patches
  every function/class in the module **in place** — existing references (imported
  names, live instances) keep working because their ``__code__`` is overwritten
  rather than the objects being replaced.
* It registers a rollback with ``hotswap_guard``, so a swap that compiles cleanly
  but throws at runtime is auto-reverted by the render loop's except hook.
* ``is_editable_source`` is the same fail-closed gate the editor uses — only paths
  inside the project tree are ever compiled or written.

The launcher (which hosts the MCP server) runs the studio in-process, so the
studio's modules live in this process's ``sys.modules`` and can be patched from
the MCP server thread — the same off-render-thread path the editor's background
recompile already uses.
"""

import sys
from pathlib import Path


def _resolve_module(path):
    """Find the live module object loaded from `path` (resolved), or None."""
    target = Path(path).resolve()
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            if Path(f).resolve() == target:
                return mod
        except (OSError, ValueError):
            continue
    return None


def hotswap_file(path, source=None):
    """Recompile `path`'s module into the running process and return a status line.

    If `source` is None, the file's current on-disk contents are reloaded. If
    `source` is given, it is hotswapped first and persisted to disk only after a
    clean compile, so a syntax error never leaves broken code on disk.
    """
    # Imported lazily: this module is loaded by the MCP server, which must not
    # drag in the (imgui-heavy) editor stack unless a hotswap is actually asked for.
    from src.lsd.gl_gui.view.core_conversion.file_converters import _recompile_module
    from src.lsd.gl_gui.view.core_conversion.address import is_editable_source
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import invalidate_usage_cache

    p = Path(path).resolve()
    if not is_editable_source(p):
        return f"refusing to hotswap source outside the project tree: {p}"
    if source is None and not p.exists():
        return f"no such file: {p}"

    module = _resolve_module(p)
    if module is None:
        return (f"no live module is loaded from {p} — nothing to hotswap. "
                f"(The file must already be imported in the running process.)")

    provided = source is not None
    if not provided:
        try:
            source = p.read_text(encoding="utf-8")
        except Exception as e:
            return f"could not read {p}: {e}"

    # Swap the live module from the in-memory source first; only touch disk on
    # success. _recompile_module RAISES on a syntax error (compile() in its
    # try) and RETURNS the exception on an exec-time error - handle both.
    try:
        err = _recompile_module(module, source, str(p))
    except Exception as e:
        err = e
    if err is not None:
        return (f"hotswap FAILED for {module.__name__} ({p.name}): "
                f"{type(err).__name__}: {err} — live module unchanged, disk unchanged")

    if provided:
        try:
            p.write_text(source, encoding="utf-8")
        except Exception as e:
            return (f"hotswapped {module.__name__} live, but FAILED to persist to disk: {e} "
                    f"— change will be lost on restart")

    # Refresh the line caches in BOTH paths - a from-disk reload means the file
    # was just changed by an external editor, so linecache may still hold the
    # pre-save content. Stale lines make getsourcelines-based span resolution
    # (and thus the in-process editor's next save) splice at wrong offsets.
    invalidate_usage_cache(p)
    import linecache
    linecache.checkcache(str(p))

    # Wake the render loop so the new code runs on the next frame.
    try:
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()
    except Exception:
        pass

    verb = "applied + saved to disk" if provided else "reloaded from disk"
    return (f"hotswapped {module.__name__} ({p.name}) — {verb}. "
            f"Every function/class in the file was patched in place; a runtime "
            f"error will auto-revert it via the hotswap guard.")
