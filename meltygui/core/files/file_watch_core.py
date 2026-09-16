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

from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window


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


from meltygui.view.file_view import file_watch_debug
file_watch_debug = window(disable_scroll=False, tint=(0.16296297311782837, 0.2611111, 0.24118687212467194))(file_watch_debug)
