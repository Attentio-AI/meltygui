"""File Watch Debug window — what the watcher and symbol index can see, now.

One row per known file, grouped by watched directory: whether its text is
baselined in Melty.code_cache (an external edit to an UNCACHED file is
invisible — nothing to diff), how many views watch it, whether it's
project-tracked (watch_project_files), whether external drift is currently
recorded for it, and the usage-symbol index status — the warmer's per-file
refs cache (fresh/stale vs disk mtime) plus how many cached span-usage
results are fresh under the current (mtime, pending-gen, index-gen)
signature. The rescan button re-runs the project walk to pick up files/dirs
created since startup.
"""

import os
import sys
from collections import defaultdict
from pathlib import Path

from meltygui.rendering.render_funcs import RenderFuncs
from meltygui.rendering.core_render import render_func
from meltygui.rendering.decorators.window_decoration import window


def _symbol_index_view():
    """Merged read-only view of the symbol index's per-file state across BOTH
    module identities (src.lsd.… / lsd.… — the span/mtime stores are
    sys-anchored and shared, but _index_refs_cache is module-level, so the
    warmer may be filling either twin's dict)."""
    refs, spans, snap, gen = {}, {}, {}, 0
    for name in ("meltygui.code.libcst_conversion",
                 "lsd.gl_gui.view.core_conversion.libcst_conversion"):
        m = sys.modules.get(name)
        if m is None:
            continue
        refs.update(getattr(m, "_index_refs_cache", {}))
        spans.update(getattr(m, "_symbol_usage_cache", {}))
        snap.update(getattr(m, "_mtime_snapshot", {}))
        gen = max(gen, getattr(m, "_index_generation", 0))
    return refs, spans, snap, gen


@window(disable_scroll=False, tint=(0.16296297311782837, 0.2611111, 0.24118687212467194))
@render_func()
def file_watch_debug(draw_state=None):
    from meltygui.melty import Melty
    from meltygui.melty import FileWatch
    from meltygui.editor.external_changes import ExternalChanges

    RenderFuncs.draw_function(FileWatch.watch_project_files, icon="",
                              tint=(0, 0, 0, 1), show_bg=False, run_in_thread=True,
                              result_fade_frames=30)

    from meltygui.editor.pending_save import PendingSave

    view_paths = {p: len(dss) for p, dss in FileWatch.path_to_draw_states.items()}
    tracked = FileWatch.project_tracked
    all_paths = sorted(set(view_paths) | set(tracked))

    refs_cache, span_cache, snap, index_gen = _symbol_index_view()
    spans_by_file = defaultdict(list)
    for (p, start, end), (sig, _usages) in list(span_cache.items()):
        spans_by_file[p].append(sig)

    _mtimes = {}

    def _mtime(p):
        if p not in _mtimes:
            try:
                _mtimes[p] = os.stat(p).st_mtime
            except OSError:
                _mtimes[p] = None
        return _mtimes[p]

    def _sym_marks(p):
        """Usage-symbol status for one file: warmer refs cache + span usages."""
        out = []
        entry = refs_cache.get(p)
        if entry is not None:
            # entry[0] is (mtime, pending_gen); disk-freshness is the mtime
            # half. A pre-hotswap twin module can still hold old-format bare
            # float entries - treat those as the mtime too.
            _sig = entry[0]
            fresh = (_sig[0] if isinstance(_sig, tuple) else _sig) == _mtime(p)
            out.append(f"refs {len(entry[1])}" if fresh else "refs STALE")
        elif p in snap:
            out.append("refs evicted")     # counted by the warmer, but cold
        else:
            out.append("no idx")           # never indexed (no live module)
        sigs = spans_by_file.get(p)
        if sigs:
            try:
                pg = PendingSave.pending_gen_for(Path(p))
                ok = sum(1 for s in sigs
                         if s and s[0] == _mtime(p) and s[1] == pg
                         and (s[2] or s[3] == index_gen))
            except Exception:
                ok = "?"
            out.append(f"usages {ok}/{len(sigs)} fresh")
        return out

    refs_fresh = sum(1 for p, entry in refs_cache.items()
                     if entry is not None and entry[0] == _mtime(p))
    cached = sum(1 for p in all_paths if p in Melty.code_cache)
    lines = [
        f"watched dirs: {len(FileWatch._watched_dirs)}   "
        f"files known: {len(all_paths)} (project-tracked {len(tracked)}, "
        f"view-watched {len(view_paths)})",
        f"baselined in code_cache: {cached}/{len(all_paths)}   "
        f"external drift tracked: {len(ExternalChanges.originals)}",
        f"symbol index: gen {index_gen}   refs cached {len(refs_cache)} file(s) "
        f"({refs_fresh} fresh)   span usages {len(span_cache)} "
        f"across {len(spans_by_file)} file(s)",
        "",
    ]

    by_dir = defaultdict(list)
    for p in all_paths:
        by_dir[str(Path(p).parent)].append(p)

    # Dirs watched but currently holding no tracked file still matter (events
    # within them can auto-track new .py files) - show them at the end.
    empty_dirs = sorted(d for d in FileWatch._watched_dirs if d not in by_dir)

    for d in sorted(by_dir):
        watched = "" if d in FileWatch._watched_dirs else "   [dir NOT scheduled!]"
        lines.append(f"{d}{watched}")
        for p in by_dir[d]:
            marks = []
            if p in Melty.code_cache:
                marks.append("cached")
            else:
                marks.append("UNCACHED — edits invisible")
            n = view_paths.get(p)
            if n:
                marks.append(f"{n} view{'s' if n > 1 else ''}")
            if p in tracked:
                marks.append("tracked")
            if p in ExternalChanges.originals:
                marks.append("DRIFT")
            marks.extend(_sym_marks(p))
            lines.append(f"    {Path(p).name}  [{', '.join(marks)}]")
    if empty_dirs:
        lines.append("")
        lines.append(f"watched dirs with no known files ({len(empty_dirs)}):")
        lines.extend(f"    {d}" for d in empty_dirs)

    RenderFuncs.draw_text("\n".join(lines), show_name=True, name="watch status")
