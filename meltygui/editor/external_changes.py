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
    # path → (id(baseline), id(disk text)) stamped when a recompile absorbed
    # this exact drift into the pending queue (PendingSave.resolve_external).
    # Absorbing must not hide the entry - the window keeps showing what an
    # external program changed until the user dismisses it. The marker only
    # stops re-processing: absorb skips the file while the ids still match,
    # and find_conflicts won't call the absorbed drift a conflict. Both ids
    # are content-free change signals (baseline is setdefault-once; the
    # watcher pops code_cache on every write, so a NEW external edit yields a
    # new disk object and the marker naturally expires).
    absorbed = {}
    # path → disk text the last absorb was computed against (the SYNC frame).
    # `originals` is display-only accumulation (setdefault-once, kept until the
    # user dismisses); merging must diff from the last text the pending queue
    # and live co_firstlineno's were rebased to - after an absorb that is the
    # absorbed disk, not the original baseline. Diffing new drift from the old
    # baseline would re-apply already-absorbed hunks (false lineno shifts,
    # phantom conflicts). Falls back to `originals` before the first absorb.
    synced = {}

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
        cls.absorbed.clear()
        cls.synced.clear()

    @classmethod
    def untrack(cls, path):
        """Drop every record for `path` — dismissals and drift-back heals.
        The next external event re-baselines from whatever the cache holds
        then, so a stale sync frame must never outlive its originals entry."""
        cls.originals.pop(path, None)
        cls.absorbed.pop(path, None)
        cls.synced.pop(path, None)

    @classmethod
    def mark_absorbed(cls, path, disk_text):
        """Record that the CURRENT drift of `path` (baseline → `disk_text`,
        the held read_code object) has been folded into the pending queue."""
        baseline = cls.originals.get(path)
        if baseline is not None:
            cls.absorbed[path] = (id(baseline), id(disk_text))
        # The sync point moved with this absorb: the merge window's original
        # world (symbol_roster.World over synced/originals) re-keys on it.
        try:
            from src.lsd.gl_gui.view.core_conversion.symbol_roster import (
                bump_disk_generation)
            bump_disk_generation()
        except Exception:
            pass

    @classmethod
    def is_absorbed(cls, path, disk_text):
        """True while `path`'s tracked drift is exactly the one a recompile
        already absorbed — expires the moment either side changes object."""
        baseline = cls.originals.get(path)
        return (baseline is not None
                and cls.absorbed.get(path) == (id(baseline), id(disk_text)))

    @classmethod
    def recompile_all(cls):
        """External changes are no longer hotswapped directly from here (the
        raw whole-module reload bypassed the pending machinery and caused
        stale-tile invalidation issues). Delegate to the Pending Saves window:
        PendingSave.recompile_all hotswaps the pending queue through the
        established per-entry path — external drift is NOT absorbed; it is
        merged manually via the merge window. recompile_all_ui drives
        the Pending Saves button's own runner draw_state (busy spinner,
        fading check mark + summary), so any caller of this alias gets the
        exact button-click UI. Kept for backward compatibility — the MCP
        recompile tool now calls PendingSave.recompile_all_ui directly."""
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        return PendingSave.recompile_all_ui()


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
        ExternalChanges.absorbed = getattr(_twin, "absorbed", ExternalChanges.absorbed)
        ExternalChanges.synced = getattr(_twin, "synced", ExternalChanges.synced)
        break


@window(disable_scroll=False, tint=(0.16296297311782837, 0.21243055828288198, 0.2611111), icon=None)
@render_func()
def draw_external_changes(draw_state=None):
    from src.lsd.gl_gui.melty import Melty, FileWatch
    ExternalChanges._window_ds = draw_state
    RenderFuncs.draw_function(ExternalChanges.dismiss_all, tint=(0, 0, 0, 1), show_bg=False, shadow=False, icon=None)

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
            ExternalChanges.untrack(path)
            continue
        current = Melty.read_code(path)
        if current is not None and \
                current.splitlines(keepends=True) == str(original).splitlines(keepends=True):
            # Drifted back to the baseline (e.g. an outside edit was undone).
            # Only heal when no absorb moved the SYNC frame past the baseline:
            # after an absorb, live code is the absorbed state, so a disk
            # change is a sync→disk drift the next recompile must absorb -
            # untracking here would leave live and disk silently diverged.
            _synced = ExternalChanges.synced.get(path)
            if _synced is None or str(_synced).splitlines(keepends=True) \
                    == current.splitlines(keepends=True):
                with open("/tmp/ext_changes_debug.log", "a") as _f:
                    _f.write(f"pop drift_back {path}\n")
                ExternalChanges.untrack(path)
                continue
        entries.append((path, original, current))

    # Stale-blit guard. The event frame renders with the cache BYPASSED (the
    # _external_change flag), so its output is never captured into the blit
    # tiles - without this, the next frame replays the pre-event tile and the
    # fresh diff flickers away. When the rendered diff actually changes,
    # force-invalidate this window's own subtree so the tiles re-capture.
    # id(current) is a content-free change signal: code_cache holds each text
    # until the watcher pops it, and every re-read is a new str object. The
    # absorption marker joins the sig so the "absorbed" annotation appearing
    # (same texts, new baseline) still re-captures the tiles.
    sig = tuple((p, id(c), ExternalChanges.absorbed.get(p)) for p, _, c in entries)
    if draw_state.misc.get("_ext_sig") != sig:
        draw_state.misc["_ext_sig"] = sig
        Melty.cache.invalidate_up(draw_state._tile_id, force=True, max_depth=6)

    for path, original, current in entries:
        file_name = Path(path).name
        if current is None:
            if RenderFuncs.button(f" Dismiss##{path}", name=f"dismiss {path}",
                                  tint=(0.12, 0.002037035, 0.002037035, 0.4))[0]:
                ExternalChanges.untrack(path)
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
            ExternalChanges.untrack(path)
        if ExternalChanges.is_absorbed(path, current):
            RenderFuncs.draw_text("absorbed into pending queue (recompiled) — "
                                  "Dismiss to clear",
                                  name=f"absorbed {path}", tint=(0.45, 0.75, 0.45),
                                  show_bg=False)
        RenderFuncs.draw_text(diff_str, show_name=True, name=f"{file_name}##{path}",
                              is_diff=True, line_numbers=line_numbers)
