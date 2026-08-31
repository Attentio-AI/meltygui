"""project_code — dict-proxy access to the studio's code, at PENDING truth.

    from src.lsd.gl_gui.view.core_conversion.project_code import project_code
    span = project_code[path].get_lines(first, last)   # 0-based, end-EXCLUSIVE
    text = span["value"]          # read: pending overlay over the code cache
    span["value"] = new_text      # write: queues into PendingSave, never disk

The lightweight sibling of the code-host RenderHosts: same ground truth, same
caches, no view machinery. Reads compose `PendingSave.current_file_text`
(disk via `Melty.code_cache` + every queued span/whole-file edit spliced in —
pending changes are the truth, disk is stale mid-session) and are memoized on
content-free signals only (`live_instrument._pending_gen` + the cached disk
text's identity — FileWatch pops the cache on external writes, so those show
up too; nothing is ever hashed). Writes splice the range into the pending
truth and queue ONE whole-file entry through `PendingSave.queue_save` — the
exact entry shape the code editor's own keystrokes queue — so every editor,
host and the merge window see the edit through the established cross-view
sync, the shutdown flush writes it with the same conflict guard, and no
span-coordinate drift can exist between this proxy and the queue.

A LineRange tracks its OWN writes (`end` moves with the line-count delta);
edits from elsewhere (an editor typing above the range) do not re-anchor it —
the caller owns cross-range bookkeeping, as the stack trace view does with
_sync_spans.
"""

from pathlib import Path

from src.lsd.gl_gui.view.core_conversion.address import Address


def _pending_generation(path):
    from src.lsd.gl_gui.view.core_conversion.live_instrument import _pending_gen
    return _pending_gen(str(path))


class LineRange(dict):
    """A line span of one file, read and written like a dict (the RenderHost
    convention: `range[range.value_key]`). `start`/`end` are 0-based,
    end-exclusive PENDING-truth line indices — the same slice convention as
    `Address(path, start, end)` and the pending queue's span entries."""

    value_key = "value"

    def __init__(self, file_code, start, end):
        super().__init__()
        self._file = file_code
        self.start = start
        self.end = end

    @property
    def path(self):
        return self._file.path

    def __getitem__(self, key):
        if key == self.value_key:
            return self.text()
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        if key == self.value_key:
            text = self.text()
            return text if text is not None else default
        return dict.get(self, key, default)

    def __setitem__(self, key, value):
        if key == self.value_key:
            self.write(value)
            return
        dict.__setitem__(self, key, value)

    def text(self):
        """The range's current text — pending truth, cache-served. Memoized
        on the file text's identity so a steady-state frame pays two dict
        hits and nothing O(file). None when the file is unreadable."""
        whole = self._file.text()
        if whole is None:
            return None
        memo = getattr(self, "_slice_memo", None)
        if (memo is not None and memo[0] is whole
                and memo[1] == (self.start, self.end)):
            return memo[2]
        sliced = "\n".join(self._file.lines()[self.start:self.end])
        self._slice_memo = (whole, (self.start, self.end), sliced)
        return sliced

    def line_count(self):
        return max(0, self.end - self.start)

    def write(self, new_text):
        """Replace the range with `new_text` in the file's PENDING truth:
        splice into the composed file text and queue one whole-file entry —
        exactly what an editor keystroke queues — then move `end` by the
        line-count delta so this proxy keeps addressing the same code. A
        no-op write queues nothing."""
        if not isinstance(new_text, str):
            return False
        whole = self._file.text()
        if whole is None:
            return False
        lines = whole.split("\n")
        new_lines = new_text.split("\n")
        if lines[self.start:self.end] == new_lines:
            return False
        lines[self.start:self.end] = new_lines
        queued = self._file.write_text("\n".join(lines))
        if queued:
            self.end = self.start + len(new_lines)
        return queued


class FileCode:
    """One file's proxy: whole-text access plus `get_lines`. Composed text is
    memoized on (pending generation, cached disk text identity) — recomputed
    only when an edit queues or FileWatch pops the disk cache."""

    def __init__(self, path):
        self.path = Path(path)
        self._memo = None          # (gen, disk_text_obj, composed_text)
        self._lines_memo = None    # (composed_text_obj, split lines)

    def get_lines(self, start, end):
        """A LineRange over 0-based, end-exclusive line indices (the Address
        slice convention). A fresh proxy each call — ranges are cheap and a
        cached one would go stale when other edits shift the file."""
        return LineRange(self, start, end)

    def text(self):
        """The whole file as the studio sees it: disk (code cache) with every
        queued pending edit spliced in. None when unreadable."""
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        gen = _pending_generation(self.path)
        disk = Melty.read_code(self.path)
        memo = self._memo
        if memo is not None and memo[0] == gen and memo[1] is disk:
            return memo[2]
        composed = PendingSave.current_file_text(self.path)
        self._memo = (gen, disk, composed)
        return composed

    def lines(self):
        """The composed text's line list, memoized on the text's identity —
        every LineRange of this file slices the same split."""
        text = self.text()
        if text is None:
            return []
        memo = self._lines_memo
        if memo is None or memo[0] is not text:
            memo = self._lines_memo = (text, text.split("\n"))
        return memo[1]

    def line_count(self):
        text = self.text()
        return 0 if text is None else text.count("\n") + 1

    def write_text(self, new_text):
        """Queue `new_text` as the file's pending truth (whole-file entry,
        the editor-keystroke shape). Records the disk baseline on the first
        queue (no-op detection + merge base) and arms the flush-time conflict
        fingerprint, both exactly as a load through code_file_io would."""
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.view.core_conversion.address import is_writable_file
        from src.lsd.gl_gui.view.core_conversion.new_codecs import (
            codec_for_path, _span_fingerprint)
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        codec = codec_for_path(self.path)
        if codec is None or not codec.editable:
            print(f"[project_code] refusing write: no editable codec for "
                  f"{self.path}")
            return False
        address = Address(self.path)
        if is_writable_file(address.path):
            address._allow_write = True   # same way TextFileCodec stamps
        if PendingSave.originals.get(address) is None:
            disk = Melty.read_code(self.path)
            if disk is not None:
                PendingSave.mark_load(address, disk)
                address._span_fp = _span_fingerprint(disk.split("\n"))
        PendingSave.queue_save(address, codec, data=new_text)
        self._memo = None
        return True


class _ProjectCode:
    """`project_code[path]` → the file's FileCode, memoized per resolved
    path so every consumer shares one composed-text cache per file."""

    def __init__(self):
        self._files = {}

    def _key(self, path):
        try:
            return str(Path(path).resolve())
        except OSError:
            return str(path)

    def __getitem__(self, path):
        key = self._key(path)
        file_code = self._files.get(key)
        if file_code is None:
            file_code = self._files[key] = FileCode(key)
        return file_code

    def __contains__(self, path):
        return Path(self._key(path)).is_file()


project_code = _ProjectCode()
