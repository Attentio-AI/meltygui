"""Trace model functions and supporting definitions."""
from pathlib import Path
import json
import os


class SavedTrace:
    """EXTERNAL frames for draw_stack_trace — a trace captured or saved
    elsewhere (a crash report file, a queued capture) rather than a live
    exception: `frames` = [(path, lineno, function_name[, scope]), ...]
    outermost first, `scope` a {name: value} dict the view shows as the
    frame's live values (None = none), `error` the raising line's message
    for draw_text's error marker ("" = no marker). draw_stack_trace is the
    default view for it, so `draw_any(SavedTrace(...))` works. Keep ONE
    object per trace: the view rebuilds its panes whenever the input's
    identity changes."""
    __slots__ = ("frames", "error", "seen_height")

    def __init__(self, frames, error=""):
        self.frames = list(frames or ())
        self.error = error or ""
        # The view's height last measured while IN VIEW (a scrolling host
        # stamps it - see crash_reports): the honest height to lay out even
        # while the view sits off-screen, where the wrapper's group measure
        # runs to the clip edge.
        self.seen_height = None

    def __repr__(self):
        return f"SavedTrace({len(self.frames)} frames, {self.error!r})"


# The app filter's choices beside the app IDs themselves: every report, and the
# reports saved before the header carried an `app` line.
ALL_APPS = "All apps"
UNKNOWN_APP = "unknown"


class CrashReportStore(dict):
    """path (str) → {"name", "time", "thread", "pid", "app", "error", "commit",
    "mtime", "size"}, every Melty process's reports in one list, newest first. The trace bodies load lazily (`text`) and are cached by
    the file's (mtime, size) so a rewritten file re-reads."""
    # save_crash_report's header: `key: value` lines up to the first blank
    # line - time / thread / error, and `frames` (JSON) when the trace saved
    # frames. Parsed by _read_header; nothing here counts lines.
    HEADER_KEYS = ("time", "thread", "pid", "app", "error", "commit", "frames", "locals")

    def __init__(self):
        super().__init__()
        self.loaded = False
        self.error = ""
        self._seen_generation = -1
        self._dir_mtime = None
        self._texts = {}                          # path → (signature, lines, SavedTrace)

    def directory(self) -> Path:
        from meltygui.core.windowing.glfw_utils import crash_reports_dir

        return crash_reports_dir()

    def refresh_if_stale(self):
        """Reload the listing when a report was saved (`reports_changed`)
        or the directory's mtime moved (a file deleted or added by hand).
        One stat per call — content-free, so it is fine per frame."""
        from meltygui.model.trace_report_model import _generation
        from meltygui.model.trace_report_model import watch_reports

        directory = self.directory()
        watch_reports(directory)
        try:
            dir_mtime = os.stat(directory).st_mtime_ns
        except OSError:
            dir_mtime = None
        if (self.loaded and self._seen_generation == _generation
                and dir_mtime == self._dir_mtime):
            return
        self._seen_generation = _generation
        self._dir_mtime = dir_mtime
        self.load()

    def load(self):
        self.clear()
        self.error = ""
        directory = self.directory()
        try:
            files = sorted(directory.glob("*.txt"), reverse=True)   # names sort by time
        except OSError as exc:
            files = []
            self.error = str(exc)
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            entry = {"name": path.stem, "time": "", "thread": "", "pid": "", "app": "",
                     "error": "", "commit": "", "mtime": stat.st_mtime, "size": stat.st_size}
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    header, _ = self._read_header(handle)
            except OSError:
                header = {}
            for key in ("time", "thread", "pid", "app", "error", "commit"):
                entry[key] = header.get(key, "")
            entry["app"] = entry["app"] or UNKNOWN_APP
            self[str(path)] = entry
        self.loaded = True
        live = set(self.keys())
        for stale in [key for key in self._texts if key not in live]:
            del self._texts[stale]

    @classmethod
    def _read_header(cls, handle):
        """(header dict, rest of the file) from an open report: `key: value`
        lines up to the first blank line; an unknown first line means an
        older file with no header (everything is body)."""
        header = {}
        first = handle.readline()
        key, _, value = first.rstrip("\n").partition(": ")
        if key not in cls.HEADER_KEYS:
            return header, first + handle.read()
        header[key] = value
        while True:
            line = handle.readline()
            if not line or not line.strip():
                break
            key, _, value = line.rstrip("\n").partition(": ")
            if key in cls.HEADER_KEYS:
                header[key] = value
        return header, handle.read()

    def _load_body(self, path):
        """(trace lines, SavedTrace) of one report, cached per file version.
        The trace's frames are the header's JSON list as (path, lineno,
        name, scope) tuples; `scope` is the frame's saved locals as
        {name: value}, display strings restored to plain literals where
        they parse (`_restore_value`), None when the report saved none;
        its `error` the header's error line. ONE SavedTrace object per
        file version, which draw_stack_trace needs: it rebuilds its panes
        whenever the input's identity changes."""
        from meltygui.model.trace_model import SavedTrace
        from meltygui.model.trace_report_model import _frames_from_text
        from meltygui.model.trace_report_model import _restore_scope

        entry = self.get(path)
        signature = (entry["mtime"], entry["size"]) if entry else None
        cached = self._texts.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1], cached[2]
        frames = None
        header = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                header, body_text = self._read_header(handle)
            if header.get("frames"):
                rows = json.loads(header["frames"])
                scopes = json.loads(header["locals"]) if header.get("locals") else []
                scopes = list(scopes) + [None] * (len(rows) - len(scopes))
                frames = [(str(row[0]), int(row[1]), str(row[2]), _restore_scope(scope))
                          for row, scope in zip(rows, scopes)]
        except (OSError, ValueError, TypeError, IndexError) as exc:
            header, body_text = {}, f"<could not read {path}: {exc}>"
        lines = body_text.split("\n")
        while lines and not lines[0].strip():
            lines = lines[1:]
        while lines and not lines[-1].strip():
            lines = lines[:-1]
        if frames is None:
            frames = _frames_from_text(lines)
        trace = SavedTrace(frames, header.get("error") or (entry["error"] if entry else ""))
        self._texts[path] = (signature, lines, trace)
        return lines, trace

    def text(self, path):
        """The report's printed trace lines (header stripped)."""
        return self._load_body(path)[0]

    def trace(self, path):
        """The report as a SavedTrace (draw_stack_trace's external-frames
        input): the header's frames, else the ones its printed text names
        (an older report) — empty when the text has none either."""
        return self._load_body(path)[1]

    def frames(self, path):
        """The report's (path, lineno, name, scope) frames."""
        return self.trace(path).frames

    def remove(self, path):
        from meltygui.model.trace_report_model import reports_changed

        try:
            os.unlink(path)
        except OSError as exc:
            self.error = str(exc)
        self.pop(path, None)
        self._texts.pop(path, None)
        reports_changed()

    def apps(self):
        """The app IDs that have a report, sorted: the filter's choices."""
        return sorted({entry["app"] for entry in self.values()})

    def shown(self, app=ALL_APPS):
        """[(path, entry)] newest first, of one app ID or of every app."""
        return [(path, entry) for path, entry in self.items()
                if app in (ALL_APPS, "", None) or entry["app"] == app]

    def remove_all(self, app=ALL_APPS):
        """Delete every report, or only the ones of `app`."""
        from meltygui.model.trace_report_model import reports_changed

        for path, _entry in self.shown(app):
            try:
                os.unlink(path)
            except OSError as exc:
                self.error = str(exc)
            self.pop(path, None)
            self._texts.pop(path, None)
        reports_changed()
