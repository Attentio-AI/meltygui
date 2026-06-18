import difflib
import re
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

    @classmethod
    def mark_load(cls, address, data, **kwargs):
        cls.originals[address] = data

    @classmethod
    def queue_save(cls, address, codec, **kwargs):
        cls.pending_saves[address] = codec, kwargs

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
                print_stack_trace()

        cls.pending_saves.clear()
        cls.pending_saves.update(survivors)


@window(disable_scroll=False)
@render_func()
def draw_pending_saves():
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
    RenderFuncs.draw_type(PendingSave, name="Pending Saves")


    for address, (codec, kwargs) in PendingSave.pending_saves.items():
        if address in PendingSave.originals:
            original_data = PendingSave.originals[address]
            # get code diff using codec library (DO NOT USE CODEC) code.diff does not exist.
            # code_diff = codec.diff(address=address, **kwargs) ### WRONG
            new_data = kwargs.get("data")
            old_data = original_data
            new_lines = str(new_data).splitlines(keepends=True)
            old_lines = str(old_data).splitlines(keepends=True)
            diff = difflib.unified_diff(
                fromfile=str(address.path), tofile=str(address.path),
                a=old_lines, b=new_lines, n=3,
            )
            # Trim the unified-diff scaffolding (---, +++ headers, @@ hunk
            # ranges, "\ No newline" markers) down to the +/- and context lines,
            # and compute each line's TRUE file number. The diff runs over the
            # snippet (a span of the file starting at address.start), so the @@
            # numbers are snippet-relative - shifting by address.start lands them
            # on the file's logical lines. draw_text(is_diff=True) colors the +/-
            # lines; line_numbers feeds the gutter.
            content_lines, line_numbers = _diff_lines_with_numbers(diff, address.start or 0)
            diff_str = "".join(content_lines)

            file_name = address.path.name
            line_range = f"({address.start}:{address.end})"
            name = f"{file_name} {line_range}"
            RenderFuncs.draw_text(diff_str, show_name=True, name=name,
                                  is_diff=True, line_numbers=line_numbers)
        else:
            PendingSave.originals[address] = codec.load(address=address, **kwargs)
            imgui.text("No original data to compare against for address: {}".format(address))
