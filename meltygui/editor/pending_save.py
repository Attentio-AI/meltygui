import difflib
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


@window(view_func=RenderFuncs.draw_type, disable_scroll=False)
class PendingSave:
    pending_saves = defaultdict(Any)
    originals = defaultdict(Any)
    # Monotonic per-file edit counter, bumped on every queue_save. A cheap,
    # content-free cache-invalidation signal (see CLAUDE.md - never hash files):
    # readers of current_file_text key on this instead of hashing the text.
    _pending_gen = defaultdict(int)

    @classmethod
    def mark_load(cls, address, data, **kwargs):
        cls.originals[address] = data

    @classmethod
    def pending_gen_for(cls, path):
        """Edit generation for `path` — bumps on every queue_save for it. Keyed by
        the same `address.path` value queue_save writes (matches pending_text_for's
        no-resolve convention)."""
        return cls._pending_gen.get(path, 0)

    @classmethod
    def queue_save(cls, address, codec, **kwargs):
        prev = cls.pending_saves.get(address)
        cls.pending_saves[address] = codec, kwargs
        cls._pending_gen[address.path] += 1
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
        # if prev is None or prev[1].get("data") != kwargs.get("data"):
        #     cls._wake_file_watchers(address.path)

    @classmethod
    def _wake_file_watchers(cls, path):
        if path is None:
            return
        from src.lsd.gl_gui.melty import FileWatch
        try:
            resolved = str(path.resolve())
        except OSError:
            return
        for ds in list(FileWatch.path_to_draw_states.get(resolved, ())):
            FileWatch.dispatch_event_for(ds)

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
        for addr, (codec, kwargs) in cls.pending_saves.items():
            data = kwargs.get("data")
            if not isinstance(data, str):
                continue
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
        for addr, (codec, kwargs) in cls.pending_saves.items():
            if (addr.path == address.path and addr.start == address.start
                    and addr.end == address.end):
                return kwargs.get("data")
        return None


    @classmethod
    def apply_all_saves(cls):
        from src.lsd.gl_gui.view.core_conversion.new_codecs import SaveConflict
        from src.lsd.gl_gui.notifications import notify

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
    def recompile_all(cls):
        """Hotswap every changed pending edit into the running process — no
        disk write; the queue stays intact for apply_all_saves at shutdown.

        Rides the editor Run button's worker (recompile_source): each queued
        span recompiles its OWN live object in place (class / function /
        module / decorator block / call site), so line-number conventions
        match a per-editor Run exactly and the hotswap guard arms as usual.
        Entries whose text still equals their load-time original are skipped
        — the same no-op filter the diff view uses."""
        from src.lsd.gl_gui.view.core_conversion.new_converters import recompile_source
        from src.lsd.gl_gui.view.core_conversion.new_codecs import CallSite, Decorations
        from src.lsd.gl_gui.view.core_conversion.chain_converters import record_compile

        compiled, failures, skipped = [], [], 0
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
                skipped += 1   # pure text / no live object - nothing to hotswap
                continue
            try:
                err = recompile_source(source, data, address.path, address=address)
            except Exception as e:
                err = e
            if err is None:
                record_compile(address)
                compiled.append(label)
            else:
                failures.append(f"{label}: {type(err).__name__}: {err}")

        if not (compiled or failures or skipped):
            return "Nothing to recompile — no changed pending edits."
        lines = [f"Recompiled {len(compiled)}: {', '.join(compiled)}"] if compiled else []
        if skipped:
            lines.append(f"Skipped {skipped} non-code edit(s)")
        for failure in failures:
            lines.append(f"FAILED {failure}")
        return "\n".join(lines)


@window(disable_scroll=False, tint=(0.18712963163852692, 0.2611111, 0.19945986568927765))
@render_func()
def draw_pending_saves():
    pass
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
    RenderFuncs.draw_function(PendingSave.apply_all_saves, icon="", tint=(0,0,0,1), show_bg=False, shadow=False)
    # name= keeps its draw_state distinct from apply_all_saves' (both calls
    # would otherwise derive the same file-name identity); run_in_thread so
    # the hotswaps run outside the render loop like every other recompile.
    # result_fade_frames: the check mark + "Recompiled ..." summary hold
    # briefly, then fade themselves (same fade model as code_file_io's
    # recompile_status) instead of parking forever.
    RenderFuncs.draw_function(PendingSave.recompile_all, name="recompile_all", icon="",
                              tint=(0,0,0,1), show_bg=False, shadow=False, run_in_thread=True,
                              result_fade_frames=30)

    for address, (codec, kwargs) in list(PendingSave.pending_saves.items()):
        if address in PendingSave.originals:
            original_data = PendingSave.originals[address]
            # generate code diff using external library (DO NOT USE CODEC) code.diff does not exist.
            # code_diff = codec.diff(address=address, **kwargs) ### WRONG
            new_data = kwargs.get("data")
            old_data = original_data
            new_lines = str(new_data).splitlines(keepends=True)
            old_lines = str(old_data).splitlines(keepends=True)
            if new_lines == old_lines:
                continue

            diff = difflib.unified_diff(
                fromfile=str(address.path), tofile=str(address.path),
                a=old_lines, b=new_lines, n=3,
            )
            # Strip the unified-diff scaffolding (--- / +++ headers, @@ hunk
            # ranges, "\ No newline" markers) down to the +/- and context lines,
            # and compute each line's TRUE file number. The diff runs over the
            # snippet (the slice of the file starting at address.start), so the @@
            # numbers are snippet-relative - shifting by address.start lands them
            # on the file's real lines. draw_text(is_diff=True) colors the +/-
            # lines; line_numbers feeds the gutter.
            content_lines, line_numbers = _diff_lines_with_numbers(diff, address.start or 0)
            diff_str = "".join(content_lines)

            file_name = address.path.name
            line_range = f"({address.start}:{address.end})"
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
            PendingSave.originals[address] = codec.load(address=address, **kwargs)
            imgui.text("No original data to compare against for address: {}".format(address))
