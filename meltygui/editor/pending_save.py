import difflib
from src.lsd.gl_gui.notifications import lag_traced

import re
import types
from collections import defaultdict
from typing import Any

import imgui

from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.utils.glfw_utils import print_stack_trace
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_lines_with_numbers(diff, base):
    """Walk a unified diff and return (content_lines, numbers): the +/- and
    context lines with the --- / +++ / @@ / "\\ No newline" scaffolding removed,
    plus the true file line number for each. @@ hunk numbers are 1-based and
    snippet-relative; `base` (the address's 0-based start line) shifts them onto
    the file. Added/context lines take the new-side number, deleted lines the
    old-side number — a replacement shows the same number on both."""
    content_lines, numbers = [], []
    old_ln = new_ln = 0
    for line in diff:
        if line.startswith(("---", "+++")):
            continue
        m = _HUNK_RE.match(line)
        if m:
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            continue
        if line.startswith("\\"):           # "\\ No newline at end of file"
            continue
        if line.startswith("+"):
            content_lines.append(line); numbers.append(base + new_ln); new_ln += 1
        elif line.startswith("-"):
            content_lines.append(line); numbers.append(base + old_ln); old_ln += 1
        else:                                # context line
            content_lines.append(line); numbers.append(base + new_ln)
            old_ln += 1; new_ln += 1
    return content_lines, numbers


# draw_pending_saves' per-entry rendered-diff cache: address →
# (id(original), id(pending), (pending text, blocks), diff_str, numbers).
# Keyed on object identities (content-free) - the diff/render recomputes
# once per actual edit: typing edges splice incrementally off the previous
# blocks instead of re-matching the whole entry.
_pending_diff_memo = {}


def _render_diff_blocks(old_l, new_l, blocks, base, n=3):
    """(content_lines, numbers) for draw_text(is_diff=True) straight from
    change blocks — unified-diff-shaped output without re-running the
    matcher: blocks whose ±n context windows touch group into one hunk;
    each block renders its '-' old lines then '+' new lines, with ' '
    context between and around. Numbers are true file lines (base = the
    address's 0-based start): '+'/context take the new side, '-' the old —
    the same convention as _diff_lines_with_numbers."""
    content, numbers = [], []
    if not blocks:
        return content, numbers
    groups, cur = [], [blocks[0]]
    for blk in blocks[1:]:
        if blk[0] - cur[-1][1] <= 2 * n:
            cur.append(blk)
        else:
            groups.append(cur)
            cur = [blk]
    groups.append(cur)
    for group in groups:
        for k in range(max(0, group[0][0] - n), group[0][0]):
            content.append(" " + new_l[k] + "\n")
            numbers.append(base + k + 1)
        for gi, (n0, n1, b0, b1, _t) in enumerate(group):
            for k in range(b0, b1):
                content.append("-" + old_l[k] + "\n")
                numbers.append(base + k + 1)
            for k in range(n0, n1):
                content.append("+" + new_l[k] + "\n")
                numbers.append(base + k + 1)
            stop = (group[gi + 1][0] if gi + 1 < len(group)
                    else min(len(new_l), n1 + n))
            for k in range(n1, stop):
                content.append(" " + new_l[k] + "\n")
                numbers.append(base + k + 1)
    return content, numbers


def three_way_merge(base, mine, theirs):
    """Line-level 3-way merge. Returns the merged text, or None when the two
    sides' edits overlap — a direct conflict that needs a human.

    Both sides diff against `base` (SequenceMatcher, no autojunk); an edit is
    a replaced base-line range plus its replacement lines. An edit both sides
    made identically collapses into one. Overlap is checked on
    insertion-expanded ranges (a pure insert claims the line it lands before),
    so an insert INSIDE the other side's edit conflicts, while edits that
    merely touch end-to-start still splice cleanly. Within one side opcodes
    are separated by at least one equal line, so expansion never makes a side
    self-overlap."""
    base_l = base.splitlines(keepends=True)
    edits = []
    for side, text in ((0, mine), (1, theirs)):
        other_l = text.splitlines(keepends=True)
        sm = difflib.SequenceMatcher(None, base_l, other_l, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                edits.append((i1, i2, tuple(other_l[j1:j2]), side))
    deduped, seen = [], set()
    for i1, i2, repl, side in edits:
        if (i1, i2, repl) in seen:
            continue                    # both sides made this exact change
        seen.add((i1, i2, repl))
        deduped.append((i1, i2, repl, side))
    # Sweep sorted-by-start edit spans; an overlap with an earlier
    # opposite-side span shows as start < that side's running max end.
    max_end = {0: -1, 1: -1}
    for s, e, side in sorted((i1, max(i2, i1 + 1), side)
                             for i1, i2, _, side in deduped):
        if s < max_end[1 - side]:
            return None
        max_end[side] = max(max_end[side], e)
    merged = list(base_l)
    for i1, i2, repl, _ in sorted(deduped, reverse=True):
        merged[i1:i2] = repl
    return "".join(merged)


@window(view_func=RenderFuncs.draw_type, disable_scroll=False)
class PendingSave:
    pending_saves = defaultdict(Any)
    originals = defaultdict(Any)
    # Result lines from manual merge actions (MERGED / ADOPTED / KEPT OURS /
    # TOOK THEIRS / CONFLICT per file - resolve_external via the merge
    # window's / commit banner's buttons). Shown by draw_pending_saves until
    # dismissed - a merge rewrites the queue, so its outcome must stay
    # inspectable.
    merge_results = []
    # Monotonic per-file edit counter, bumped on every queue_save. A cheap,
    # content-free cache-invalidation signal (see CLAUDE.md - never hash files):
    # readers of current_file_text key on this instead of hashing the text.
    _pending_gen = defaultdict(int)
    # Tint-relevant twin of _pending_gen: bumps only when a queued edit could
    # change definition-tint washes in editors viewing OTHER files - its line
    # count moved (shifting every def line below the span) or a tint-carrying
    # line changed. A #[...] param drag rewriting `#[speed=3.1]` per frame
    # bumps _pending_gen every time but leaves this unchanged, so the
    # _def_tints memos across the app stop recomputing (and mid-edit dropping
    # propagated washes) after each drag frame.
    _tint_gen = defaultdict(int)

    @classmethod
    def mark_load(cls, address, data, **kwargs):
        # A load answered from the pending overlay (codec.load returns the
        # pending edit, not disk) must NOT re-baseline: basing the result as
        # the "original" turns a real pending edit into a no-op (data ==
        # original) — drop_noop_entries_for would then discard it on the next
        # external write, and the merge base would be wrong.
        if isinstance(data, str) and cls.pending_text_for(address) == data:
            return
        cls.originals[address] = data

    @classmethod
    def entry_for(cls, address):
        """The queued entry whose span is `address`: exact (path, start, end)
        value match first (Address hashes by location), else by the address's
        `source` — an external write shifts the file, so a freshly resolved
        address no longer matches the coordinates the edit was queued under,
        but both still point at the same live object / call site. Returns
        (queued_address, codec, kwargs) or None."""
        hit = cls.pending_saves.get(address)
        if hit is not None:
            return address, hit[0], hit[1]
        src = getattr(address, "source", None)
        if src is None:
            return None
        for addr, (codec, kwargs) in list(cls.pending_saves.items()):
            try:
                if addr.path == address.path and getattr(addr, "source", None) == src:
                    return addr, codec, kwargs
            except Exception:
                continue
        return None

    @classmethod
    def original_for(cls, address):
        """(matched_address, load-time original text) for `address`, matched
        like entry_for — the 3-way-merge base. None when this span was never
        loaded through load_file."""
        hit = cls.originals.get(address)
        if hit is not None:
            return address, hit
        src = getattr(address, "source", None)
        if src is None:
            return None
        for addr, data in list(cls.originals.items()):
            try:
                if addr.path == address.path and getattr(addr, "source", None) == src:
                    return addr, data
            except Exception:
                continue
        return None

    @classmethod
    def rebase_entry(cls, old_address, new_address, codec, data, original, **kwargs):
        """Move a queued edit onto a freshly resolved span: drop the
        stale-coordinate entry and its load-time original, re-baseline the
        original to `original` (the CURRENT disk span), and queue `data` under
        the new address. The manual merge path calls this after splicing an
        external change into a pending edit, so apply_all_saves later splices
        at coordinates that match the rewritten file."""
        if old_address != new_address:
            cls.pending_saves.pop(old_address, None)
            cls.originals.pop(old_address, None)
        cls.originals[new_address] = original
        cls.queue_save(new_address, codec, data=data, **kwargs)

    @classmethod
    def discard_entry_for(cls, address):
        """Drop the queued edit (and its baseline) matching `address` — the
        user chose "Load theirs" on an unmergeable conflict; without this the
        pending overlay would keep answering loads with the discarded edit."""
        hit = cls.entry_for(address)
        if hit is not None:
            cls.pending_saves.pop(hit[0], None)
            cls.originals.pop(hit[0], None)
        cls.originals.pop(address, None)

    @classmethod
    def pending_gen_for(cls, path):
        """Edit generation for `path` — bumps on every queue_save for it. Keyed by
        the same `address.path` value queue_save writes (matches pending_text_for's
        no-resolve convention)."""
        return cls._pending_gen.get(path, 0)

    @staticmethod
    def _tint_relevant_change(prev_data, data):
        """Could replacing `prev_data` with `data` change def-tint washes in
        another file's editor? True when the line count moved or any line
        mentioning 'tint' differs; unknown shapes (non-str, first sight with
        no baseline) bump conservatively. The split runs only when the span
        mentions tint at all — a plain param edit stays on two C-level
        checks, and the data is already in memory (this is not a file hash)."""
        if prev_data is data:
            return False
        if not isinstance(prev_data, str) or not isinstance(data, str):
            return True
        if prev_data.count("\n") != data.count("\n"):
            return True
        p_has, n_has = "tint" in prev_data, "tint" in data
        if p_has != n_has:
            return True
        if not p_has:
            return False
        return ([l for l in prev_data.split("\n") if "tint" in l]
                != [l for l in data.split("\n") if "tint" in l])

    @classmethod
    @lag_traced("queue_save", 30)
    def queue_save(cls, address, codec, wake=True, **kwargs):
        prev = cls.pending_saves.get(address)
        cls.pending_saves[address] = codec, kwargs
        cls._pending_gen[address.path] += 1
        # Compare against what this span last queued (or its load-time
        # original on first queue) for the delta this edit actually introduces.
        prev_data = (prev[1].get("data") if prev is not None
                     else cls.originals.get(address))
        if cls._tint_relevant_change(prev_data, kwargs.get("data")):
            cls._tint_gen[address.path] += 1
        # Debug timeline: who bumped this file's pending generation (a bump is
        # what invalidates the symbol-usage cache sig → forces a recompute).
        try:
            from src.lsd.gl_gui.perf_trace import trace as _ptrace
            import sys as _sys
            trail = []
            f = _sys._getframe(1)
            for _ in range(4):
                if f is None:
                    break
                trail.append(f"{f.f_code.co_filename.rsplit('/', 1)[-1]}:{f.f_lineno} {f.f_code.co_name}")
                f = f.f_back
            _ptrace(f"queue_save gen={cls._pending_gen[address.path]} <- {' <- '.join(trail)}",
                    file=getattr(address.path, 'name', address.path))
        except Exception:
            pass
        # Deferred saves never write disk, so the file watcher never needs to update
        # SIBLING views of this file (a structured/cst/dict view, another editor).
        # When the queued text content changes, wake them so they re-render and
        # pull the new edit from this cache (code_file_io's cross-view-sync branch).
        # Reuses FileWatch's per-path watcher set + dispatch; the editing view that
        # produced the entry is guarded there (its own buffer already matches).
        # `wake=False` is the focused-editor exception: the typing editor's
        # per-keystroke auto-save should not re-render every sibling view of the
        # file on each keystroke (save_file passes it if the text focus sits
        # inside the saving view's own subtree). Programmatic edits - param
        # panels, lenses, live preview comment edits - keep the wake, which is
        # what lands their edit in the visible editor promptly.
        if wake and (prev is None or prev[1].get("data") != kwargs.get("data")):
            cls._wake_file_watchers(address.path)

        # The merge/conflict window watches this edge too: a fresh pending edit
        # may now have tracked external drift. wake() no-ops while that
        # window is closed, so this hot path (queue_save can fire per edit
        # frame) pays one attr fetch. Lazy import - merge_files imports us.
        try:
            from src.lsd.gl_gui.view.core_views.merge_files import MergeFiles
            MergeFiles.wake()
        except Exception:
            pass

        # Re-lint the file's code-host: what the missing-import checker reports
        # depends on the file's PENDING text (code_checks._module_level_binds),
        # so any queued edit - an import added orremoved in some other view,
        # a revert - may change the right answer for every span of this file
        # without touching their addresses. Cheap flag + rate-limited consumer
        # wake per host (see _kick_relint).
        try:
            from src.lsd.gl_gui.view.core_conversion.new_converters import _kick_relint
            _kick_relint(address.path)
        except Exception:
            pass

    @classmethod
    def _wake_file_watchers(cls, path):
        if path is None:
            return
        from src.lsd.gl_gui.melty import FileWatch, Melty
        from src.lsd.gl_gui.view.invalidation_tracker import Note
        try:
            resolved = str(path.resolve())
        except OSError:
            return
        watchers = list(FileWatch.path_to_draw_states.get(resolved, ()))
        try:
            from src.lsd.gl_gui.perf_trace import trace as _ptrace
            _ptrace(f"wake_file_watchers n={len(watchers)} "
                    f"[{', '.join(getattr(d, 'name', '?') or '?' for d in watchers)}]",
                    file=resolved.rsplit('/', 1)[-1])
        except Exception:
            pass
        for ds in watchers:
            FileWatch.dispatch_event_for(ds)
            # dispatch only flags ds._external_change - that bypasses the VALUE
            # cache, but only once the body runs, and a blit-cached editor tile
            # replays its texture without even running the body. Dirty the tile
            # too (force + depth, same shape as the host's notify) so
            # code_file_io actually re-executes and pulls the queued edit.
            tid = getattr(ds, "_tile_id", None)
            if tid is not None:
                Melty.cache.invalidate_up(
                    tid, force=True, max_depth=8,
                    note=Note(name="queue_save wake", tint=(1, 0.8, 0.2),
                              draw_state=ds))

    @classmethod
    def current_file_text(cls, path):
        """Disk text of `path` with every queued (unsaved) span edit spliced in —
        the file as it WOULD be on disk if the deferred saves flushed right now.

        Readers that re-read disk see pre-edit content (saves defer to shutdown);
        this is the in-memory truth for whole-file consumers like the symbol-usage
        index, which otherwise resolve references against the stale on-disk file.
        Splices bottom-up (highest start first), matching apply_all_saves /
        codec.save, so an applied span never shifts a not-yet-applied span above
        it. Newline-normalized to '\\n' (callers here only need line/col, which is
        newline-agnostic). A queued whole-file edit (start is None) IS the text.
        Returns None if the file can't be read."""
        from src.lsd.gl_gui.melty import Melty
        from pathlib import Path as _P
        disk = Melty.read_code(path)
        if disk is None:
            return None
        try:
            rp = _P(path).resolve()
        except OSError:
            return disk
        edits = []
        for addr, (codec, kwargs) in list(cls.pending_saves.items()):
            data = kwargs.get("data")
            if not isinstance(data, str):
                continue
            if data == cls.originals.get(addr):
                continue        # no-op entry - must not splice stale text over
                                # an externally-changed disk (see pending_text_for)
            try:
                if _P(addr.path).resolve() != rp:
                    continue
            except Exception:
                continue
            edits.append((addr.start, addr.end, data))
        if not edits:
            return disk
        whole = [d for (s, e, d) in edits if s is None]
        if whole:
            return whole[-1].replace("\r\n", "\n").replace("\r", "\n")
        lines = disk.split("\n")
        for start, end, data in sorted((e for e in edits if e[0] is not None),
                                       key=lambda e: -e[0]):
            d = data.replace("\r\n", "\n").replace("\r", "\n")
            if d.endswith("\n"):
                d = d[:-1]
            lines[start:end] = d.split("\n")
        return "\n".join(lines)

    @classmethod
    def pending_text_for(cls, address):
        """The unsaved in-memory span text queued for `address`, or None.

        Saves are buffered here and only flushed to disk at shutdown
        (apply_all_saves), so during a session the file on disk is stale. A load
        that re-read disk — a sibling editor, a code-host, the structured view, a
        staleness reload — would resurrect the pre-edit content, and a recompile
        off it would compile the stale version. Matched by (path, start, end),
        NOT identity: every consumer resolves its OWN Address from the (stable,
        since unwritten) disk to the same coords, so the value match lets a
        sibling see the editor's live edit. None when nothing is queued there."""
        for addr, (codec, kwargs) in list(cls.pending_saves.items()):
            if (addr.path == address.path and addr.start == address.start
                    and addr.end == address.end):
                data = kwargs.get("data")
                # A no-op entry (data == its load-time original) answers with
                # text identical to what disk held at load - worthless as an
                # overlay, and actively wrong the moment an EXTERNAL write
                # changes the file: it would shadow the new disk content on
                # every reload. drop_noop_entries_for skips such entries on
                # the external-event path, but the drop is a posted render
                # task and a load triggered by the same event can run FIRST - so
                # the skip must live here, at the consumption point.
                if data == cls.originals.get(addr):
                    return None
                return data
        # A whole-file pending entry (a manual merge queues one per merged
        # external change) holds span edits that are NOT on disk - a span
        # consumer reloading from disk would lose them. Serve the slice from
        # the merged text. The coords are valid against it: resolve_external
        # resyncs live linenos to the merged source (the same coords the
        # consumer resolved from).
        if address.start is not None:
            for addr, (codec, kwargs) in list(cls.pending_saves.items()):
                if addr.start is not None or addr.path != address.path:
                    continue
                data = kwargs.get("data")
                if not isinstance(data, str) or data == cls.originals.get(addr):
                    return None
                lines = data.split("\n")
                if address.end is not None and address.end > len(lines):
                    return None     # bad coords - never serve a short slice
                return "\n".join(lines[address.start:address.end])
        return None


    @classmethod
    def drop_noop_entries_for(cls, path):
        """Remove queued entries for `path` whose data still equals their
        load-time original — no-op entries (a value toggled and toggled back,
        or a Revert). Called when an EXTERNAL write lands on the file: a no-op
        entry has nothing left to preserve, but left queued it SHADOWS the new
        disk content — pending_text_for keeps answering with the old span
        text, so every reload of that span (a code host, a sibling editor, the
        symbol index's current_file_text splice) resurrects the pre-edit file
        — and at shutdown apply_all_saves would write the stale span back over
        the external edit. Real pending edits (data != original) stay queued
        and surface as the editor's changed-on-disk conflict, as before.
        Render-thread only (callers hop via Melty.post_to_render): the queue
        is iterated by frame code."""
        from pathlib import Path as _P
        try:
            rp = _P(path).resolve()
        except OSError:
            return
        for addr in [a for a, (codec, kw) in cls.pending_saves.items()
                     if a.path == rp and kw.get("data") == cls.originals.get(a)]:
            del cls.pending_saves[addr]

    @classmethod
    @lag_traced("apply_all_saves", 50)
    def apply_all_saves(cls):
        from src.lsd.gl_gui.view.core_conversion.new_codecs import SaveConflict
        from src.lsd.gl_gui.notifications import notify

        # No merge-on-save: external drift is resolved MANUALLY (merge window /
        # editor banner) before the flush - the main-window close guard
        # (needs_merge?) blocks a shutdown that would land here with pending
        # edits on a changed file. If a changed span does reach codec.save, its
        # _span_fp fingerprint refuses the write (SaveConflict) and the entry
        # defers below instead of splicing at stale offsets.

        # Apply same-file saves bottom-up (highest start line first). A splice
        # only shifts the lines BELOW its span, so saving the lowest span last
        # means an applied edit never invalidates a still-lying span above it.
        # This matters because the addresses are snapshots taken at resolve time
        # and NOTHING re-resolves them between these batched saves - codec.save
        # shifts live co_firstlineno's, but the Address objects sitting here keep
        # their initial .start/.end. (start=None is a whole-file save; sort it last
        # so it can't clobber span coords mid-batch.)
        def _order(item):
            addr = item[0]
            start = addr.start
            return (str(addr.path), -start if start is not None else float("-inf"))

        # Keep refused saves PENDING instead of dropping them: a SaveConflict
        # means the on-disk span changed under a stale address (e.g. an earlier
        # save in this batch grew the file above it). The owning editor re-resolves
        # its address on the next mtime-driven render, so a later apply lands it
        # correctly. A plain False is success OR a hard refusal (library/binary/
        # read-only) - neither is retryable, so don't re-queue it.
        survivors = {}
        for address, (codec, kwargs) in sorted(cls.pending_saves.items(), key=_order):
            result = codec.save(address=address, **kwargs)
            if isinstance(result, SaveConflict):
                survivors[address] = (codec, kwargs)
                notify(f"Save deferred: {address.path.name} changed under it",
                       tint=(1.0, 0.8, 0.3))

        cls.pending_saves.clear()
        cls.pending_saves.update(survivors)

    @classmethod
    def studio_text_for(cls, path):
        """The studio's current view of `path`: the sync-frame text (the last
        disk state the pending queue was rebased to — ExternalChanges.synced,
        falling back to the drift baseline, falling back to disk) with this
        file's real pending span edits spliced in bottom-up. This is the BASE
        side of the manual merge diff: pending is treated as current, and the
        external disk change is the incoming side diffed against it.

        NOT current_file_text — that splices pending into the CURRENT disk
        text, which already contains the external edits, so diffing it against
        disk would hide the incoming side. Newline-normalized to '\\n'.
        Returns None when no text is available at all."""
        from pathlib import Path as _P
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.view.core_views.external_changes import ExternalChanges
        _norm = cls._norm_text
        key = str(path)
        base = ExternalChanges.synced.get(key,
                                          ExternalChanges.originals.get(key))
        if base is None:
            base = Melty.read_code(path)
        if base is None:
            return None
        base_n = _norm(base)
        try:
            rp = _P(path).resolve()
        except OSError:
            return base_n
        edits = []
        for addr, (codec, kwargs) in list(cls.pending_saves.items()):
            data = kwargs.get("data")
            if not isinstance(data, str):
                continue
            # This no-op skip is for SPAN entries only (data == load-time
            # original - nothing at stake). A WHOLE-FILE entry compares
            # against a different reference point (its original is the
            # sync frame at QUEUE time, the studio base here is CURRENT
            # sync frame): after a resolve advanced the base, a "no-op"
            # whole-file is still the flushable pending truth - skipping
            # it reported DISK's text as the studio's.
            if addr.start is not None and data == cls.originals.get(addr):
                continue
            try:
                if _P(addr.path).resolve() != rp:
                    continue
            except Exception:
                continue
            edits.append((addr.start, addr.end, data))
        if not edits:
            return base_n
        whole = [d for (s, e, d) in edits if s is None]
        if whole:
            return _norm(whole[-1])
        lines = base_n.split("\n")
        for start, end, data in sorted((e for e in edits if e[0] is not None),
                                       key=lambda e: -e[0]):
            d = _norm(data)
            if d.endswith("\n"):
                d = d[:-1]
            lines[start:end] = d.split("\n")
        return "\n".join(lines)

    # path → ((id(sync), id(disk)), unmerged: bool). Identity-keyed memo for
    # unmerged_drift_paths: both texts are held objects (synced/originals are
    # setdefault/assign-once per event; the watcher pops code_cache on disk
    # write), so the O(file) normalize+compare runs once per actual change,
    # not per render/frame (editor banner and merge window call this hot).
    _drift_memo = {}

    @classmethod
    def unmerged_drift_paths(cls):
        """Paths (resolved strs, ExternalChanges keys) whose tracked external
        drift has NOT been merged into pending yet: normalized sync-frame text
        differs from current disk. Comparison is memoized by object identity —
        never a per-call content pass over unchanged texts."""
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.view.core_views.external_changes import ExternalChanges
        _norm = cls._norm_text
        out = []
        for path, baseline in list(ExternalChanges.originals.items()):
            disk = Melty.read_code(path)
            if disk is None:
                continue
            sync = ExternalChanges.synced.get(path, baseline)
            key = (id(sync), id(disk))
            hit = cls._drift_memo.get(path)
            if hit is None or hit[0] != key:
                hit = (key, _norm(sync) != _norm(disk))
                cls._drift_memo[path] = hit
            if hit[1]:
                out.append(path)
        return out

    @classmethod
    def needs_merge(cls):
        """True iff some file has BOTH a real pending edit (data differs from
        its load-time original) and unmerged external drift — the state where
        exiting would lose or clobber pending. Drives the main-window close
        guard. Pure read over existing state."""
        from pathlib import Path as _P
        drifted = set()
        for p in cls.unmerged_drift_paths():
            try:
                drifted.add(_P(p).resolve())
            except OSError:
                continue
        if not drifted:
            return False
        for addr, (codec, kwargs) in list(cls.pending_saves.items()):
            data = kwargs.get("data")
            if not isinstance(data, str) or data == cls.originals.get(addr):
                continue
            try:
                if _P(addr.path).resolve() in drifted:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _norm_text(t):
        return str(t).replace("\r\n", "\n").replace("\r", "\n")

    @classmethod
    def resolve_external(cls, path, prefer=None, allow_merge=None,
                         whole_text=None):
        """Resolve ONE tracked external change into per-span pending entries
        and return its result line (None when there's nothing to do —
        untracked path, or drift that healed back to the baseline).

        whole_text: the caller's WHOLE-FILE pending buffer (the merge
        window's pending pane — the studio's current text of the file with
        every accepted change spliced in). When given, the file resolves on
        the whole-file path with that text as "mine" (prefer='mine' queues
        it verbatim as the result; 'theirs' takes disk; None + allow_merge
        3-way merges it against the sync frame), and the per-span entries
        it already contains are dropped for the one whole-file entry.

        allow_merge gates the per-span 3-way merge for OVERLAPPING spans:
        only an explicit merge action (the Merge buttons) passes True;
        the default (None/False) forces overlaps to CONFLICT. Rebase/adopt
        of non-overlapping work never depends on it.

        The disk drift (sync frame → current disk, see ExternalChanges.synced)
        is decomposed into the SAME shape as in-studio edits — span pending
        entries anchored on live objects — and merged with the queue:

        * pending span entries untouched by the drift are REBASED to the new
          disk coordinates (their span shifted through the drift's hunks);
        * entries the drift overlaps are 3-way merged PER SPAN (base = the
          sync-frame slice, mine = the pending data, theirs = the disk slice);
        * drift hunks inside a live top-level def/class with no pending entry
          are ADOPTED as new pending entries (data = the disk span, original =
          the sync-frame span, so recompile_all hotswaps them; a successful
          hotswap re-baselines them into no-ops since disk already holds them);
        * brand-new defs/classes and changed import lines are exec'd into the
          live module; other module-level changes are reported as
          restart-needed.

        Live co_firstlineno's are shifted from the sync frame to the disk
        frame (resync_file_linenos) so the disk-coordinate invariant holds —
        the whole-module hotswap from non-disk text this used to do is what
        moved live code into coordinates no file had, corrupting every span
        resolution afterwards.

        prefer=None runs the auto merge; an overlap that cannot merge returns
        the CONFLICT line and mutates NOTHING. The Merge window's accept
        buttons force a side per overlapping entry instead: prefer='mine'
        keeps the pending data (drift under it is overwritten on
        recompile/shutdown-save); prefer='theirs' drops the overlapping
        entries and takes disk. Non-overlapping entries and drift are always
        rebased/adopted regardless of prefer.

        The ExternalChanges entry is marked absorbed — NOT popped: the
        external window keeps showing accumulated drift until the user
        dismisses it. ExternalChanges.synced records the disk this absorb was
        computed against, so the next drift diffs from HERE, not from the
        display baseline."""
        from pathlib import Path as _P
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.view.core_views.external_changes import ExternalChanges
        from src.lsd.gl_gui.mcp_hotswap import _resolve_module

        if allow_merge is None:
            allow_merge = False     # merging only allowed on explicit request

        _norm = cls._norm_text
        baseline = ExternalChanges.originals.get(path)
        if baseline is None:
            return None
        name = _P(path).name
        disk = Melty.read_code(path)
        if disk is None:
            return f"SKIPPED {name}: deleted or unreadable"
        if prefer is None and ExternalChanges.is_absorbed(path, disk):
            return None         # this exact drift is already in the queue
        base_n, disk_n = _norm(baseline), _norm(disk)
        sync_n = _norm(ExternalChanges.synced.get(path, baseline))
        if sync_n == disk_n:
            # Nothing new since the last absorb. Fully healed (disk also back
            # at the display baseline) → drop tracking; else just arm the
            # absorbed marker.
            if base_n == disk_n:
                ExternalChanges.untrack(path)
            else:
                ExternalChanges.mark_absorbed(path, disk)
            return None
        # NOTE: disk == display baseline is NOT "nothing to do" - after an
        # absorb, a disk revert back to the baseline is REAL sync→disk drift
        # (live code holds the absorbed state) and must absorb like any other
        # change, or live and disk silently diverge (found the hard way:
        # reverting a hotswapped smoke edit left the old text live).
        try:
            rp = _P(path).resolve()
        except OSError:
            return f"SKIPPED {name}: unresolvable path"

        # The file's real pending span entries (data differs from load-time
        # original), and any legacy whole-file entry.
        entries, whole = [], []
        for addr, (codec, kwargs) in list(cls.pending_saves.items()):
            data = kwargs.get("data")
            if not isinstance(data, str):
                continue
            if data == cls.originals.get(addr):
                continue                    # no-op entry - nothing at stake
            try:
                if _P(addr.path).resolve() != rp:
                    continue
            except Exception:
                continue
            (whole if addr.start is None else entries).append(
                (addr, codec, kwargs, data))

        module = _resolve_module(path)
        if module is None or whole or whole_text is not None:
            # Plain files (no live module → no span hotswap, whole-file is
            # available), whole-file entries (the code editor's buffer, the merge
            # window's pane) and an explicit whole_text take the whole-file
            # merge. The base is the SYNC frame - the text pending derived
            # from - so a second merge after an absorb doesn't re-apply
            # already-absorbed hunks.
            return cls._resolve_external_wholefile(
                rp, name, path, module, sync_n, disk_n, disk,
                entries + whole, prefer, allow_merge, mine=whole_text)

        return cls._resolve_external_spans(
            rp, name, path, module, sync_n, disk_n, disk, entries, prefer,
            allow_merge)

    @classmethod
    def _resolve_external_wholefile(cls, rp, name, path, module, base_n,
                                    disk_n, disk, absorbed, prefer,
                                    allow_merge, mine=None):
        """The whole-file merge: plain (non-module) files, whole-file entries
        (the code editor's buffer) and the merge window's pane (`mine`, the
        caller's whole-file text). base/mine/theirs are whole texts; the
        result is ONE whole-file pending entry fingerprinted against the
        disk it merged with."""
        from src.lsd.gl_gui.view.core_views.external_changes import ExternalChanges
        from src.lsd.gl_gui.view.core_conversion.new_codecs import (
            ModuleCodec, TextFileCodec, _span_fingerprint)
        from src.lsd.gl_gui.view.core_conversion.address import Address

        _norm = cls._norm_text
        whole = [d for a, c, k, d in absorbed if a.start is None]
        if mine is not None:
            mine = _norm(mine)
        elif whole:
            mine = _norm(whole[-1])
        elif absorbed:
            # Splice bottom-up (highest start first) so an applied span
            # never overlap a not-yet-applied span above it - same order as
            # current_file_text / apply_all_saves.
            lines = base_n.split("\n")
            for addr, codec, kwargs, data in sorted(
                    (e for e in absorbed if e[0].start is not None),
                    key=lambda e: -e[0].start):
                d = _norm(data)
                if d.endswith("\n"):
                    d = d[:-1]
                lines[addr.start:addr.end] = d.split("\n")
            mine = "\n".join(lines)
        else:
            mine = base_n

        if prefer == "mine":
            merged = mine
        elif prefer == "theirs":
            merged = disk_n
        else:
            # 3-way merge gated by allow_merge (True only on an explicit Merge
            # action); off → overlapping pending edits report as a conflict
            # for the manual merge window instead of merging silently.
            if mine == base_n:
                merged = disk_n
            elif allow_merge:
                merged = three_way_merge(base_n, mine, disk_n)
            else:
                merged = None
            if merged is None:
                return (f"CONFLICT {name}: {len(absorbed)} pending edit(s) "
                        f"overlap the external change — see Merge window")

        address = Address(rp, source=module if module is not None else str(rp))
        merge_codec = ModuleCodec if module is not None else TextFileCodec
        if module is None:
            address._allow_write = True     # plain-file gate, see codec.save
        for addr, codec, kwargs, data in absorbed:
            cls.pending_saves.pop(addr, None)
            cls.originals.pop(addr, None)
        cls.originals[address] = base_n
        # Fingerprint the DISK this merge was computed against, arming
        # codec.save's changed-on-disk refusal (SaveConflict); like every
        # codec.load-stamped entry; drifts after this merge defers
        # the flush instead of being silently overwritten.
        address._span_fp = _span_fingerprint(disk_n.split("\n"))
        cls.queue_save(address, merge_codec, data=merged)
        ExternalChanges.synced[path] = disk
        ExternalChanges.mark_absorbed(path, disk)
        if prefer == "mine":
            return (f"KEPT OURS {name}: pending version queued — the external "
                    f"change will be overwritten")
        if prefer == "theirs":
            return (f"TOOK THEIRS {name}: disk version queued, dropped "
                    f"{len(absorbed)} pending edit(s)")
        if absorbed:
            return f"MERGED {name}: external change + {len(absorbed)} pending edit(s)"
        return f"ADOPTED {name}: external change is now a pending edit"

    @classmethod
    def _resolve_external_spans(cls, rp, name, path, module, sync_n, disk_n,
                                disk, entries, prefer, allow_merge):
        """Span-level absorption for a live Python module (see
        resolve_external). Two-phase: PLAN everything against the sync→disk
        diff first — any unresolvable overlap returns the CONFLICT line with
        NOTHING mutated — then commit: shift live linenos, rebase/merge/adopt
        entries, exec new imports/defs, advance the sync frame."""
        import ast
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.view.core_views.external_changes import ExternalChanges
        from src.lsd.gl_gui.view.core_conversion.address import (
            Address, _evict_linecache)
        from src.lsd.gl_gui.view.core_conversion.new_codecs import (
            TypeCodec, FunctionCodec, _span_fingerprint, resync_file_linenos)

        sync_lines = sync_n.split("\n")
        disk_lines = disk_n.split("\n")
        ops = [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in
               difflib.SequenceMatcher(None, sync_lines, disk_lines,
                                       autojunk=False).get_opcodes()
               if tag != "equal"]
        if not ops:
            ExternalChanges.synced[path] = disk
            ExternalChanges.mark_absorbed(path, disk)
            return None

        def _exp(i1, i2):
            # Insertion-expanded old-side range: a pure insert claims the line
            # it lands before (same convention as three_way_merge /
            # merge_files._changed_old_ranges), so an insert INSIDE a span
            # overlaps it while an insert AT its end belongs below.
            return i1, max(i2, i1 + 1)

        _norm = cls._norm_text
        rebases, merges, drops = [], [], []
        merged_ct = kept_ct = took_ct = 0
        # Sync-frame spans whose entry SURVIVES an overlap (merged / kept):
        # their hunks are settled by the entry. A dropped entry ('theirs')
        # leaves its hunks unclaimed so the disk version adopts + hotswaps;
        # a non-overlapped span contains no hunks, so claiming is moot.
        claimed = []
        for addr, codec, kwargs, data in entries:
            s = addr.start
            orig = cls.originals.get(addr)
            if addr.end is not None:
                e = addr.end
            elif isinstance(orig, str):
                e = s + len(_norm(orig).split("\n"))
            else:
                e = s + 1
            d_above = d_inside = 0
            overlap = straddle = False
            for (i1, i2, j1, j2) in ops:
                e1, e2 = _exp(i1, i2)
                if e2 <= s:
                    d_above += (j2 - j1) - (i2 - i1)
                elif e1 < e and e2 > s:
                    overlap = True
                    if i1 < s or i2 > e:
                        straddle = True
                    else:
                        d_inside += (j2 - j1) - (i2 - i1)
            ns = s + d_above
            if not overlap:
                rebases.append((addr, codec, kwargs, _norm(data),
                                ns, e + d_above, orig))
                continue
            if straddle and prefer != "theirs":
                # The external change crosses this span's boundary - no clean
                # theirs-slice exists, and splicing "mine" over part of it
                # would tear the hunk. Human call either way.
                return (f"CONFLICT {name}: an external change crosses a "
                        f"pending span boundary — see Merge window")
            ne = e + d_above + d_inside
            theirs_txt = "\n".join(disk_lines[ns:ne])
            if prefer == "mine":
                kept_ct += 1
                claimed.append((s, e))
                merges.append((addr, codec, kwargs, _norm(data),
                               ns, ne, theirs_txt))
            elif prefer == "theirs":
                took_ct += 1
                drops.append(addr)
            else:
                # base = the sync-frame slice: the common ancestor both the
                # pending edit and the disk drift derived from. Gated by
                # allow_merge (allow only on an explicit Merge action).
                if not allow_merge:
                    return (f"CONFLICT {name}: external change overlaps a "
                            f"pending edit — see Merge window")
                base_txt = "\n".join(sync_lines[s:e])
                m = three_way_merge(base_txt, _norm(data), theirs_txt)
                if m is None:
                    return (f"CONFLICT {name}: external change overlaps a "
                            f"pending edit — see Merge window")
                merged_ct += 1
                claimed.append((s, e))
                merges.append((addr, codec, kwargs, m, ns, ne, theirs_txt))

        # Drift hunks no pending entry claims → adopt / exec / restart-note.
        leftover = [op for op in ops
                    if not any(_exp(op[0], op[1])[0] < e
                               and _exp(op[0], op[1])[1] > s
                               for s, e in claimed)]
        adopts, new_defs, exec_fails = [], [], []
        restart_needed = 0
        if leftover:
            try:
                sync_tree = ast.parse(sync_n)
                disk_tree = ast.parse(disk_n)
            except SyntaxError as ex:
                return (f"SKIPPED {name}: does not parse "
                        f"({ex.msg}, line {ex.lineno}) — fix and recompile again")

            def _spans(tree):
                out = {}
                for node in tree.body:
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                        first = node.lineno
                        if node.decorator_list:
                            first = min(first, node.decorator_list[0].lineno)
                        out[node.name] = (first - 1, node.end_lineno)
                return out

            sspans, dspans = _spans(sync_tree), _spans(disk_tree)
            adopt_names, newdef_names = set(), set()
            for (i1, i2, j1, j2) in leftover:
                e1, e2 = _exp(i1, i2)
                owner = next((nm for nm, (s0, e0) in sspans.items()
                              if s0 <= e1 and e2 <= e0), None)
                if owner is not None:
                    obj = module.__dict__.get(owner)
                    if (isinstance(obj, (type, types.FunctionType))
                            and owner in dspans):
                        adopt_names.add(owner)
                    else:
                        restart_needed += 1
                    continue
                # Not inside any sync-frame object - brand-new disk-frame
                # functions/classes exec live (intersection, not containment: the
                # hunk usually drags the blank lines around a new def along).
                # Leftover import lines are handled by _exec_file_imports
                # below; anything else non-blank needs a restart.
                de1, de2 = j1, max(j2, j1 + 1)
                hits = [nm for nm, (s0, e0) in dspans.items()
                        if nm not in sspans and s0 < de2 and de1 < e0]
                newdef_names.update(hits)
                covered = [dspans[nm] for nm in hits]
                for idx in range(j1, min(j2, len(disk_lines))):
                    t = disk_lines[idx].strip()
                    if (t and not t.startswith(("import ", "from ", "#"))
                            and not any(s0 <= idx < e0 for s0, e0 in covered)):
                        restart_needed += 1
                        break
            for nm in sorted(adopt_names):
                s0, e0 = sspans[nm]
                ds0, de0 = dspans[nm]
                adopts.append((nm, module.__dict__[nm], ds0, de0,
                               "\n".join(disk_lines[ds0:de0]),
                               "\n".join(sync_lines[s0:e0])))
            new_defs = [(nm,) + dspans[nm] for nm in sorted(newdef_names)]

        # ── Apply ───────────────────────────────────────────────────────────
        # Live co_firstlineno's move from the sync frame to the disk frame
        # FIRST: recompile_all's per-span hotswaps uses each function's
        # live lineno, so it must already have the disk one (disk-coordinate
        # invariant), and every later span resolution hits disk.
        resync_file_linenos(rp, sync_lines, disk_lines)
        _evict_linecache(str(rp))

        for addr in drops:
            cls.pending_saves.pop(addr, None)
            cls.originals.pop(addr, None)

        # New imports/defs exec BEFORE the adopted spans queue: an adopted
        # function that calls a new helper must find it live when it hotswaps.
        if leftover:
            try:
                from src.lsd.gl_gui.view.core_conversion.file_converters import (
                    _exec_file_imports)
                _exec_file_imports(str(rp), module.__dict__)
            except Exception as ex:
                exec_fails.append(f"imports: {type(ex).__name__}: {ex}")
        for nm, ds0, de0 in new_defs:
            # Pad so the new code object lands with the true disk lineno.
            src = "\n" * ds0 + "\n".join(disk_lines[ds0:de0])
            try:
                with Melty.annotation_scope():
                    exec(compile(src, str(rp), "exec"), module.__dict__)
            except Exception as ex:
                exec_fails.append(f"new def {nm}: {type(ex).__name__}: {ex}")

        # Adopted entries queue before the rebases: both may touch the same
        # object (a sub-span entry like Decorations inside an adopted class),
        # and recompile_all runs in queue order - the whole-object adoption
        # must hotswap first so the narrower pending edit re-applies on top.
        for nm, obj, ds0, de0, dtxt, otxt in adopts:
            na = Address(rp, ds0, de0, source=obj)
            na._span_fp = _span_fingerprint(disk_lines[ds0:de0])
            # Marks the entry for recompile_all: once the hotswap lands, the
            # entry re-baselines to a no-op (its text is already on disk).
            na._ext_adopt = True
            cls.originals[na] = otxt
            cls.queue_save(na, FunctionCodec if isinstance(obj, types.FunctionType)
                           else TypeCodec, data=dtxt)

        for addr, codec, kwargs, data, ns, ne, orig in rebases:
            na = Address(rp, ns, ne, source=addr.source,
                         watcher_ds=addr._watcher_ds)
            for attr in ("_allow_write", "_shift_source"):
                if hasattr(addr, attr):
                    setattr(na, attr, getattr(addr, attr))
            na._span_fp = _span_fingerprint(disk_lines[ns:ne])
            cls.rebase_entry(addr, na, codec, data=data,
                             original=(orig if isinstance(orig, str)
                                       else "\n".join(disk_lines[ns:ne])),
                             **{k: v for k, v in kwargs.items() if k != "data"})
        for addr, codec, kwargs, data, ns, ne, theirs_txt in merges:
            na = Address(rp, ns, ne, source=addr.source,
                         watcher_ds=addr._watcher_ds)
            for attr in ("_allow_write", "_shift_source"):
                if hasattr(addr, attr):
                    setattr(na, attr, getattr(addr, attr))
            na._span_fp = _span_fingerprint(disk_lines[ns:ne])
            # original = the disk slice: the entry stays "real" (data differs)
            # so it recompiles and flushes at shutdown, and the next drift
            # 3-way merges against the frame it actually diverged from.
            cls.rebase_entry(addr, na, codec, data=data, original=theirs_txt,
                             **{k: v for k, v in kwargs.items() if k != "data"})

        ExternalChanges.synced[path] = disk
        ExternalChanges.mark_absorbed(path, disk)

        parts = []
        if merged_ct:
            parts.append(f"merged {merged_ct} overlapping pending edit(s)")
        if kept_ct:
            parts.append(f"kept {kept_ct} pending edit(s) over the external change")
        if took_ct:
            parts.append(f"dropped {took_ct} pending edit(s) for disk")
        if adopts:
            parts.append(f"adopted {len(adopts)} changed def(s)")
        if new_defs:
            parts.append(f"exec'd {len(new_defs)} new def(s)")
        if rebases:
            parts.append(f"rebased {len(rebases)} pending edit(s)")
        if restart_needed:
            parts.append(f"{restart_needed} module-level change(s) apply on restart")
        parts.extend(f"FAILED {f}" for f in exec_fails)
        tag = ("KEPT OURS" if prefer == "mine"
               else "TOOK THEIRS" if prefer == "theirs"
               else "MERGED" if merged_ct else "ADOPTED")
        return f"{tag} {name}: " + "; ".join(parts)

    @classmethod
    def _wake_windows(cls):
        """Worker-thread wake after absorb rewrites the queue: force both
        windows' subtrees to re-capture (the pending window shows new
        entries + merge results; the external window gained absorbed markers —
        its _external_change flag is the established cache bypass)."""
        try:
            from src.lsd.gl_gui.melty import Melty
            from src.lsd.gl_gui.utils.glfw_utils import request_render
            from src.lsd.gl_gui.view.core_views.external_changes import ExternalChanges
            win = Melty.find_window("draw_pending_saves")
            if win is not None:
                Melty.cache.invalidate_up(win._tile_id, force=True, max_depth=8)
            ds = ExternalChanges._window_ds
            if ds is not None:
                ds._external_change = True
            request_render()
        except Exception:
            pass

    @classmethod
    def recompile_all(cls):
        """Hotswap every changed pending edit into the running process — no
        disk write; the queue stays intact for apply_all_saves at shutdown.
        External disk drift is IGNORED here: pending is the current state,
        and drift is merged in manually (merge window / editor banner), never
        as a recompile side effect.

        Rides the editor Run button's worker (recompile_source): each queued
        span recompiles its OWN live object in place (class / function /
        module / decorator block / call site), so line-number conventions
        match a per-editor Run exactly and the hotswap guard arms as usual.
        Entries whose text still equals their load-time original are skipped
        — the same no-op filter the diff view uses."""
        from src.lsd.gl_gui.view.core_conversion.new_converters import recompile_source
        from src.lsd.gl_gui.view.core_conversion.new_codecs import CallSite, Decorations
        from src.lsd.gl_gui.view.core_conversion.chain_converters import record_compile
        from src.lsd.gl_gui.view.core_conversion.file_converters import module_for_path

        # Pick up project files/dirs created since startup (idempotent, only
        # uncached files are read) so their NEXT pending edit is tracked -
        # this runs on the recompile worker, never the render thread.
        try:
            from src.lsd.gl_gui.melty import FileWatch
            FileWatch.watch_project_files()
        except Exception:
            pass

        compiled, failures = [], []
        # Snapshot: this runs in a worker thread (draw_function run_in_thread)
        # while the render thread may still queue edits mid-iteration.
        for address, (codec, kwargs) in list(cls.pending_saves.items()):
            data = kwargs.get("data")
            if not isinstance(data, str):
                continue
            if data == cls.originals.get(address):   # .get - the factory raises
                continue
            label = address.path.name if address.path is not None else "?"
            if address.start is not None:
                label += f"({address.start}:{address.end})"
            source = getattr(address, "source", None)
            if not isinstance(source, (type, types.FunctionType,
                                       types.ModuleType, CallSite, Decorations)):
                # Whole-file text entries (TextFileCodec) carry the PATH as
                # source - a spanless .py entry with a live module still
                # hotswaps (recompile_source resolves the module). Anything
                # else is plain text / no live object - nothing to hotswap;
                # the entry simply flushes to disk at save. Not worth reporting.
                if not (address.start is None and address.path is not None
                        and address.path.suffix.lower() == ".py"
                        and module_for_path(address.path) is not None):
                    continue
            # Whole-file module entries hotswap from their PENDING text - the
            # same whole-module path the per-editor Run button uses on the
            # live buffer (_recompile_module). The old refusal of non-disk
            # text guarded the absorb-era merged entries, which matched
            # neither the editors nor disk; a whole-file entry now IS the
            # editor's buffer truth, and span consumers read that same text
            # through the pending overlay (pending_text_for's whole-file
            # filtering), so live linenos and the served source are coherent.
            try:
                err = recompile_source(source, data, address.path, address=address)
            except Exception as e:
                err = e
            if err is None:
                record_compile(address)
                compiled.append(label)
                if getattr(address, "_ext_adopt", False):
                    # An adopted external span is live now and already on
                    # disk - re-baseline it into a no-op so it drops from the
                    # diff view and never re-merges as a "pending edit".
                    cls.originals[address] = data
            else:
                failures.append(f"{label}: {type(err).__name__}: {err}")

        if not (compiled or failures):
            return "Nothing to recompile — no changed pending edits."
        lines = []
        if compiled:
            lines.append(f"Recompiled {len(compiled)}: {', '.join(compiled)}")
        for failure in failures:
            lines.append(f"FAILED {failure}")
        return "\n".join(lines)

    @classmethod
    def recompile_all_ui(cls):
        """Run recompile_all exactly as a CLICK on the Pending Saves window's
        recompile button does: same runner draw_state, same lifecycle — the
        _run_busy spinner while running, then result + _result_frame stamped
        for the fading check mark + summary (draw_function's run_in_thread
        worker protocol, including _run_error on an exception). The recompile
        itself runs on the CALLING thread (MCP handler / hotkey worker — the
        established off-render path) while the render thread paints the busy
        state. Callers: the MCP recompile tool and draw_main's Ctrl+Enter.

        Reveals the window first (posted to the render thread) so the summary
        is actually seen; if the window has never rendered, the runner ds
        appears on that reveal frame and is picked up by a short retry —
        worst case the summary is only the returned string. Single-flight via
        the button's own _run_busy latch."""
        import time
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        from src.lsd.gl_gui.view.core_views.new_core_view import (
            is_run_busy, run_busy_begin, run_busy_end)

        def _find_runner():
            try:
                win = Melty.find_window("draw_pending_saves")
                if win is None:
                    return None
                for d in win.descendants(max_depth=8):
                    if str(getattr(d, 'name', '')).startswith("recompile_all"):
                        return d
            except Exception:
                return None
            return None

        def _reveal():
            try:
                from src.lsd.gl_gui.view.core_views.new_core_view import Core
                Core.melty.open_window("draw_pending_saves")
            except Exception:
                pass

        try:
            Melty.post_to_render(_reveal)
        except Exception:
            pass
        request_render()
        ds = _find_runner()
        if ds is None:                      # never-rendered window: the reveal
            for _ in range(10):             # frame creates the runner ds
                time.sleep(0.05)
                ds = _find_runner()
                if ds is not None:
                    break
        # The busy latch is draw_function's long-lifetime _RUN_BUSY (not
        # serialized - the misc flag once persisted into ds.pkl and reloaded
        # every session as "True"). Armed INSIDE the try so nothing between
        # arming and the finally (the invalidate below once sat outside it)
        # can leave the runner busy for good.
        if ds is not None and is_run_busy(ds):
            return "recompile already running — try again shortly"
        try:
            if ds is not None:
                run_busy_begin(ds)
                Melty.cache.invalidate_up(ds._tile_id, force=True, max_depth=6)
                request_render()
            try:
                summary = cls.recompile_all()
            except Exception as e:
                summary = f"recompile FAILED: {type(e).__name__}: {e}"
                if ds is not None:
                    ds.misc["_run_error"] = summary
            else:
                if ds is not None:
                    ds.result = summary
                    ds.misc["_result_frame"] = Melty.frame_count
                    ds.misc.pop("_run_error", None)
        finally:
            if ds is not None:
                run_busy_end(ds)
                try:
                    Melty.cache.invalidate_up(ds._tile_id, force=True, max_depth=6)
                except Exception as e:
                    print(f"recompile_all_ui: invalidate after run failed: {e!r}")
            request_render()
        return summary


@window(disable_scroll=False, z_offset=0, tint=(0.11, 0.17, 0.133))
@render_func()
def draw_pending_saves():
    pass
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
    RenderFuncs.draw_function(PendingSave.apply_all_saves, icon="", tint=(0,0,0,1), show_bg=False)
    # name= keeps its draw_state distinct from apply_all_saves' (both calls
    # would otherwise derive the same file-name identity); run_in_thread so
    # the hotswaps run outside the render loop like every other recompile.
    # result_fade_frames: the check mark + "Recompiled ..." summary hold
    # briefly, then fade themselves (same fade model as code_file_io's
    # recompile_status) instead of parking forever.
    RenderFuncs.draw_function(PendingSave.recompile_all, name="recompile_all", icon="",
                              tint=(0,0,0,1), show_bg=False, run_in_thread=True,
                              result_fade_frames=30, temp=True)

    # Result of the last external-change absorb (recompile_all's merge pass).
    # Persistent, no fading if a save rewrote the file - the per-file
    # MERGED / ADOPTED / CONFLICT outcome stays visible until dismissed.
    if PendingSave.merge_results:
        if RenderFuncs.button(" Dismiss##merge_results", name="dismiss merge_results",
                              tint=(0.12, 0.002037035, 0.002037035, 0.4))[0]:
            PendingSave.merge_results = []
        RenderFuncs.draw_text("\n".join(PendingSave.merge_results), show_name=True,
                              name="external merge results")

    # Drop memo entries whose queue entry is gone (applied/re reverted).
    for _k in list(_pending_diff_memo):
        if _k not in PendingSave.pending_saves:
            _pending_diff_memo.pop(_k, None)
    for address, (codec, kwargs) in list(PendingSave.pending_saves.items()):
        if address in PendingSave.originals:
            original_data = PendingSave.originals[address]
            new_data = kwargs.get("data")
            old_data = original_data
            # Identity-memoized diff (content-free keys - all texts are
            # fresh objects on change): recompute only on real edges. A
            # typing edge (same original, new pending text) splices through
            # the incremental differ off the previous blocks; anything else
            # runs the full matcher once. The +/-/context text and the
            # and line numbers (base = address.start) render straight from
            # the blocks - no unified_diff pass.
            m = _pending_diff_memo.get(address)
            if m is None or m[0] != id(old_data) or m[1] != id(new_data):
                from src.lsd.gl_gui.view.playground.open_files import (
                    _diff_blocks, _incremental_diff_blocks)
                old_s, new_s = str(old_data), str(new_data)
                blocks = None
                if m is not None and m[0] == id(old_data) and m[2] is not None:
                    prev_new, prev_blocks = m[2]
                    blocks = _incremental_diff_blocks(old_s, prev_new, new_s,
                                                      prev_blocks)
                if blocks is None:
                    blocks = _diff_blocks(old_s, new_s)
                content_lines, line_numbers = _render_diff_blocks(
                    old_s.split("\n"), new_s.split("\n"), blocks,
                    address.start or 0)
                m = (id(old_data), id(new_data), (new_s, blocks),
                     "".join(content_lines), line_numbers)
                _pending_diff_memo[address] = m
            diff_str, line_numbers = m[3], m[4]
            if not diff_str:
                continue               # no-op edit (pending == original)

            file_name = address.path.name
            line_range = (f"({address.start}:{address.end})"
                          if address.start is not None else "(whole file)")
            name = f"{file_name} {line_range}"
            if RenderFuncs.button(f" Revert##{name}", name=f"revert {name}",
                                  tint=(0.12, 0.002037035, 0.002037035, 0.4))[0]:
                # Revert: queue the load-time text as a fresh pending edit.
                # The entry stays in the queue (so sibling views still resolve
                # their text through pending_data_for and pick up the revert),
                # but data == original makes it a no-op for the diff view,
                # recompile_all, and the eventual disk write.
                PendingSave.queue_save(address, codec, **{**kwargs, "data": old_data})
                # Deferred saves never touch disk, so no watcher wakes on its
                # own - dispatch the file event so every view of this file
                # reloads and picks the reverted text up from the pending cache.
                PendingSave._wake_file_watchers(address.path)
            RenderFuncs.draw_text(diff_str, show_name=True, name=name,
                                  is_diff=True, line_numbers=line_numbers)
        else:
            # Baseline against DISK, never the pending cache: a plain
            # codec.load answers with the queued edit itself, and stamping the
            # edit as its own "original" reclassifies the entry as a no-op
            # (dropped on the next disk write, invisible in this diff) and
            # poisons the merge base. source_text pins the load to disk.
            from src.lsd.gl_gui.melty import Melty
            disk_text = Melty.read_code(address.path) if address.path is not None else None
            PendingSave.originals[address] = codec.load(
                address=address, **{**kwargs, "source_text": disk_text})
            imgui.text("No original data to compare against for address: {}".format(address))