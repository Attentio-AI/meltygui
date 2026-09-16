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

from meltygui.rendering.render_funcs import RenderFuncs
from meltygui.utils.glfw_utils import request_render
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.window_decoration import window
from meltygui.editor.pending_save import _diff_lines_with_numbers


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
        from meltygui.melty import FileWatch
        from meltygui.melty import Melty
        if FileWatch.is_self_write(src_path):
            return              # an in-process save, not an outside program
        # An external write obsoletes any cached NO-OP pending edits for this
        # file (data == load-time original): they'd shadow the new disk
        # content on every span reload. Before the old_text gate - the queue
        # must clear even for a file the studio never cached. Hopped to the
        # render thread; frame time invalidates the queue.
        from meltygui.editor.pending_save import PendingSave
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
            from meltygui.extensions import call
            call('conflicts_changed')
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
            from meltygui.code.symbol_roster import bump_disk_generation
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
        from meltygui.editor.pending_save import PendingSave
        return PendingSave.recompile_all_ui()


# Hotswap / dual-identity guard. A hotswap re-executes this module — under the
# BARE 'lsd....' name, while meltygui's _on_event hook imports the 'src.lsd....' one -
# so without this the window body and the event feed end up on two different
# ExternalChanges classes (split-brain: events land in a dict the window never
# reads). Alias the mutable state from whichever twin is already loaded, so
# every identity shares ONE originals dict and a hotswap never wipes the queue.
import sys as _sys
for _n in ("meltygui.editor.external_changes",
           "lsd.gl_gui.view.core_views.external_changes"):
    _twin = getattr(_sys.modules.get(_n), "ExternalChanges", None)
    if _twin is not None and _twin is not ExternalChanges:
        ExternalChanges.originals = _twin.originals
        ExternalChanges._window_ds = _twin._window_ds
        ExternalChanges.absorbed = getattr(_twin, "absorbed", ExternalChanges.absorbed)
        ExternalChanges.synced = getattr(_twin, "synced", ExternalChanges.synced)
        break


from meltygui.view.file_view import draw_external_changes
draw_external_changes = window(disable_scroll=False, tint=(0.16296297311782837, 0.212430558282882, 0.2611111), icon=None)(draw_external_changes)
