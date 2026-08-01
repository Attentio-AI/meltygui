"""External Changes window — the outside-in twin of draw_pending_saves.

PendingSave tracks edits made IN the studio that haven't reached disk yet;
ExternalChanges tracks edits made OUTSIDE the studio that have. FileWatch
already sees every fs event in the watched dirs — _on_event hands each one
here together with the pre-change code_cache text, which becomes the diff
baseline. Current text is never stored: the watcher pops code_cache on every
change, so Melty.read_code at render time is both fresh and cheap.
"""

import difflib
from pathlib import Path

from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.pending_save import _diff_lines_with_numbers


class ExternalChanges:
    # resolved path (str) → file text BEFORE the first recorded change
    # This is setdefault-only: later events on the same file keep the same
    # baseline, so the window shows the accumulated drift since the studio
    # last opened the file - not just the latest write.
    originals = {}
    _window_ds = None   # draw_external_changes' draw_state - the wake target

    @classmethod
    def on_file_event(cls, src_path, old_text):
        """Called from FileWatch._on_event (observer thread) for every fs
        event, with the code_cache text as it was BEFORE the event popped it.
        old_text=None means the studio never read the file — nothing to
        baseline against, so it isn't tracked."""
        from src.lsd.gl_gui.melty import FileWatch, Melty
        if FileWatch.is_self_write(src_path):
            return              # an in-process save, not an outside program
        # An external write obsoletes any cached NO-OP pending edits for this
        # file (data == load-time original): they'd shadow the new disk
        # content on every span reload. Before the old_text gate - the queue
        # must clear even for a file the studio never cached. Hopped to the
        # render thread; frame time invalidates the queue.
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        Melty.post_to_render(lambda p=src_path: PendingSave.drop_noop_entries_for(p))
        if old_text is None:
            return
        try:
            resolved = str(Path(src_path).resolve())
        except OSError:
            return
        cls.originals.setdefault(resolved, old_text)
        # Same wake mechanism as FileWatch.dispatch_event_for: the flag makes
        # core_render skip the blit cache for the window's next pass, so the
        # new diff actually renders instead of replaying a stale tile.
        ds = cls._window_ds
        if ds is not None:
            ds._external_change = True
        # The conflict window shares the same edge: external drift may now
        # overlap a pending span. Lazy import (merge_files imports this module).
        try:
            from src.lsd.gl_gui.view.core_views.merge_files import MergeFiles
            MergeFiles.wake()
        except Exception:
            pass
        request_render()

    @classmethod
    def dismiss_all(cls):
        cls.originals.clear()

    @classmethod
    def recompile_all(cls):
        """Hotswap every tracked externally-changed file into the running
        process from its on-disk contents — the same whole-module in-place
        reload the MCP hotswap tool uses (hotswap-guard rollback included; runs
        on the button's worker thread, the established off-render path).
        Entries are NOT dismissed — recompiling absorbs the change into the
        live process, but the tracker keeps showing what drifted from the
        baseline until the user dismisses it (same model as draw_pending_saves,
        whose recompile leaves the save queue intact)."""
        from pathlib import Path as _P
        from src.lsd.gl_gui.mcp_hotswap import hotswap_file
        ok_names, failures = [], []
        for path in list(cls.originals):
            try:
                status = hotswap_file(path)
            except Exception as e:
                status = f"{type(e).__name__}: {e}"
            if str(status).startswith("hotswapped"):
                ok_names.append(_P(path).name)
            else:
                failures.append(f"{_P(path).name}: {status}")
        # Wake the window so it re-renders with the fresh diffs (same
        # wake as on_file_event - this runs on a worker thread).
        ds = cls._window_ds
        if ds is not None:
            ds._external_change = True
        request_render()
        if not (ok_names or failures):
            summary = "Nothing to recompile — no tracked external changes."
        else:
            lines = [f"Recompiled {len(ok_names)}: {', '.join(ok_names)}"] if ok_names else []
            for failure in failures:
                lines.append(f"FAILED {failure}")
            summary = "\n".join(lines)
        cls._surface_result(summary)
        return summary

    @classmethod
    def _surface_result(cls, result):
        """Stamp `result` onto the window's recompile_all runner draw_state so
        the fading check mark + summary show exactly as a button click's would
        (same stamp draw_main's Ctrl+Enter does for the Pending Saves window).
        Redundant-but-idempotent for the button path (its run_in_thread runner
        writes the same result on completion); it's what makes an MCP-driven
        recompile visible in the window. No-op if the window never rendered —
        the caller's returned summary / toast covers that."""
        try:
            from src.lsd.gl_gui.melty import Melty
            win = Melty.find_window("draw_external_changes")
            if win is None:
                return
            for ds in win.descendants(max_depth=8):
                if str(getattr(ds, 'name', '')).startswith("recompile_all"):
                    ds.result = result
                    ds.misc["_result_frame"] = Melty.frame_count
                    Melty.cache.invalidate_up(ds._tile_id, force=True, max_depth=6)
                    break
        except Exception:
            pass


# Hotswap / dual-identity guard. A hotswap re-executes this file - under the
# BARE 'lsd....' namespace, while melty's _on_file_event uses the 'src.lsd....' one -
# so without this the window body and the event feed end up on two different
# ExternalChanges classes (split-brain: events land in a dict the window never
# reads). Alias the mutable state from whichever twin is already loaded, so
# every identity shares ONE originals dict and a hotswap never wipes the queue.
import sys as _sys
for _n in ("src.lsd.gl_gui.view.core_views.external_changes",
           "lsd.gl_gui.view.core_views.external_changes"):
    _twin = getattr(_sys.modules.get(_n), "ExternalChanges", None)
    if _twin is not None and _twin is not ExternalChanges:
        ExternalChanges.originals = _twin.originals
        ExternalChanges._window_ds = _twin._window_ds
        break


@window(disable_scroll=False, tint=(0.16296297311782837, 0.21243055828288198, 0.2611111))
@render_func()
def draw_external_changes(draw_state=None):
    from src.lsd.gl_gui.melty import Melty, FileWatch
    ExternalChanges._window_ds = draw_state
    RenderFuncs.draw_function(ExternalChanges.dismiss_all, icon="",
                              tint=(0, 0, 0, 1), show_bg=False, shadow=False)
    # Mirrors draw_pending_saves' recompile_all: run_in_thread keeps the
    # hotswaps off the render loop; result_fade_frames shows the fading
    # "Recompiled ..." summary instead of keeping there forever.
    RenderFuncs.draw_function(ExternalChanges.recompile_all, name="recompile_all", icon="",
                              tint=(0, 0, 0, 1), show_bg=False, shadow=False, run_in_thread=True,
                              result_fade_frames=30)

    # DEBUG: what the tracker will actually see. A file only gets a baseline if
    # its text sat in Melty.code_cache when the fs event fired (old_lines is the
    # popped cache entry), so per watched file show whether it's cached NOW -
    # "no" means an external edit to it would be skipped silently.
    watched = sorted(FileWatch.path_to_draw_states.keys())
    lines = [f"tracked entries: {len(ExternalChanges.originals)}"]
    for p, orig in sorted(ExternalChanges.originals.items()):
        lines.append(f"  {p}  (baseline {len(str(orig).splitlines())} lines)")
    lines.append(f"watched dirs: {len(FileWatch._watched_dirs)}   "
                 f"code_cache entries: {len(Melty.code_cache)}")
    lines.append(f"watched files ({len(watched)}):")
    for p in watched:
        cached = "cached" if p in Melty.code_cache else "NOT in code_cache"
        lines.append(f"  {p}  [{len(FileWatch.path_to_draw_states[p])} views, {cached}]")
    RenderFuncs.draw_text("\n".join(lines), show_name=True, name="watch debug")

    entries = []
    for path, original in list(ExternalChanges.originals.items()):
        # Once the studio itself writes the file, disk equals in-process truth
        # again - the external change is resolved, drop the entry. This
        # also self-heals the truncate race: a mid-write fs event can slip
        # past the event-time is_self_write check (disk hash not final yet)
        # and record a bogus entry, but by render time the flush has landed.
        if FileWatch.is_self_write(path):
            with open("/tmp/ext_changes_debug.log", "a") as _f:
                _f.write(f"pop self_write {path} recorded={FileWatch._self_write_hashes.get(path)} "
                         f"disk={FileWatch._get_hash(path)}\n")
            ExternalChanges.originals.pop(path, None)
            continue
        current = Melty.read_code(path)
        if current is not None and \
                current.splitlines(keepends=True) == str(original).splitlines(keepends=True):
            # Drifted back to the baseline (e.g. an outside edit was undone).
            with open("/tmp/ext_changes_debug.log", "a") as _f:
                _f.write(f"pop drift_back {path}\n")
            ExternalChanges.originals.pop(path, None)
            continue
        entries.append((path, original, current))

    # Stale-blit guard. The event frame renders with the cache BYPASSED (the
    # _external_change flag), so its output is never captured into the blit
    # tiles - without this, the next frame replays the pre-event tile and the
    # fresh diff flickers away. When the rendered diff actually changes,
    # force-invalidate this window's own subtree so the tiles re-capture.
    # id(current) is a reliable content change signal: code_cache holds each text
    # until the watcher pops it, and every re-read is a new str instance.
    sig = tuple((p, id(c)) for p, _, c in entries)
    if draw_state.misc.get("_ext_sig") != sig:
        draw_state.misc["_ext_sig"] = sig
        Melty.cache.invalidate_up(draw_state._tile_id, force=True, max_depth=6)

    for path, original, current in entries:
        file_name = Path(path).name
        if current is None:
            if RenderFuncs.button(f" Dismiss##{path}", name=f"dismiss {path}",
                                  tint=(0.12, 0.002037035, 0.002037035, 0.4))[0]:
                ExternalChanges.originals.pop(path, None)
            RenderFuncs.draw_text(f"{path}: deleted or unreadable", name=f"{file_name}##{path}")
            continue
        new_lines = current.splitlines(keepends=True)
        old_lines = str(original).splitlines(keepends=True)

        diff = difflib.unified_diff(
            fromfile=path, tofile=path,
            a=old_lines, b=new_lines, n=3,
        )
        # Whole-file diffs, so the @@ hunk numbers ARE the file's line numbers
        # - base 0 (see _diff_lines_with_numbers; pending_save spans shift by
        # their address.start, a whole file starts at 0).
        content_lines, line_numbers = _diff_lines_with_numbers(diff, 0)
        diff_str = "".join(content_lines)

        if RenderFuncs.button(f" Dismiss##{path}", name=f"dismiss {path}",
                              tint=(0.12, 0.002037035, 0.002037035, 0.4))[0]:
            # Dismiss = accept the on-disk state as the new baseline: the
            # entry drops, and the next external edit re-baselines from
            # whatever the cache holds then.
            ExternalChanges.originals.pop(path, None)
        RenderFuncs.draw_text(diff_str, show_name=True, name=f"{file_name}##{path}",
                              is_diff=True, line_numbers=line_numbers)
