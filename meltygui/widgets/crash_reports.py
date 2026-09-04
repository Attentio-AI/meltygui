"""Crash Reports — the traces print_stack_trace saved under
`Toggles.CrashReports.directory` (~/.lsd/crash_reports by default), one file
per trace (glfw_utils.save_crash_report), listed newest first.

Modelled on fast_dock: rows are plain draw-list rects/text with manual
hit-testing; hover boosts and clicks resolve inside the body while the view
is hovered (the wrapper repaints every frame then), and the idle tile is a
cached blit that a new report repaints via `reports_changed()` (called by
save_crash_report from whatever thread printed the trace).

Model: `reports` (CrashReportStore, a dict path → entry dict with the
report's header fields: time / thread / error). Clicking a row expands it
and draws it through draw_stack_trace (the code behind each frame, from
the report's saved (path, lineno, function) frames — an older report
without a frames header has them parsed out of its printed text); the
trash button on a hovered row deletes that file, the toolbar's Clear all
deletes every file.
Which rows are expanded persists in CrashReportsPanelState.
"""
from __future__ import annotations

import ast
import json
import os
import re
import threading
import time
from pathlib import Path

import imgui

from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.utils.glfw_utils import crash_reports_dir, request_render
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import flat_button
from src.lsd.gl_gui.view.core_views.stack_trace_view import SavedTrace, draw_stack_trace

# Bumped by reports_changed(); the store refreshes when it sees a new value.
_generation = 0
_generation_lock = threading.Lock()


def reports_changed():
    """A report file was written or removed outside the window (any
    thread): mark the store stale and repaint the window's cached tile."""
    global _generation
    with _generation_lock:
        _generation += 1
    try:
        Melty.cache.invalidate_up_by_obj(reports, force=True)
        request_render()
    except Exception:
        pass                                      # no studio (tests, launcher): nothing to repaint


# ──────────────────────────────────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────────────────────────────────

class CrashReportStore(dict):
    """path (str) → {"name", "time", "thread", "error", "commit", "mtime", "size"},
    newest first. The trace bodies load lazily (`text`) and are cached by
    the file's (mtime, size) so a rewritten file re-reads."""
    # save_crash_report's header: `key: value` lines up to the first blank
    # line - time / thread / error, and `frames` (JSON) when the trace saved
    # frames. Parsed by _read_header; nothing here counts lines.
    HEADER_KEYS = ("time", "thread", "error", "commit", "frames", "locals")

    def __init__(self):
        super().__init__()
        self.loaded = False
        self.error = ""
        self._seen_generation = -1
        self._dir_mtime = None
        self._texts = {}                          # path → (signature, lines, SavedTrace)

    def directory(self) -> Path:
        return crash_reports_dir()

    def refresh_if_stale(self):
        """Reload the listing when a report was saved (`reports_changed`)
        or the directory's mtime moved (a file deleted or added by hand).
        One stat per call — content-free, so it is fine per frame."""
        directory = self.directory()
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
            entry = {"name": path.stem, "time": "", "thread": "", "error": "", "commit": "",
                     "mtime": stat.st_mtime, "size": stat.st_size}
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    header, _ = self._read_header(handle)
            except OSError:
                header = {}
            for key in ("time", "thread", "error", "commit"):
                entry[key] = header.get(key, "")
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
        try:
            os.unlink(path)
        except OSError as exc:
            self.error = str(exc)
        self.pop(path, None)
        self._texts.pop(path, None)
        reports_changed()

    def remove_all(self):
        for path in list(self.keys()):
            try:
                os.unlink(path)
            except OSError as exc:
                self.error = str(exc)
        self.clear()
        self._texts.clear()
        reports_changed()


# `File "path", line num, in func` - the printed trace's frame line, the
# fallback source of frames for a report file without a frames header.
_FRAME_LINE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')


def _frames_from_text(lines):
    return [(match.group(1), int(match.group(2)), match.group(3), None)
            for line in lines for match in [_FRAME_LINE.search(line)] if match]


def _restore_value(text):
    """A saved local back to a plain value where its display string is a
    literal (numbers, strings, None, small containers) so the marker shows
    `5`, not `'5'`; anything else (`<Tensor (3, 4) float32>`, an object
    repr) stays the string it was saved as."""
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return text


def _restore_scope(scope):
    if not isinstance(scope, dict):
        return None
    return {str(name): _restore_value(value) for name, value in scope.items()}


reports = CrashReportStore()


class CrashReportsPanelState(DictConversion):
    """Which reports are expanded — file name → True — persisted with the
    window's draw_state (injected as `panel_state: CrashReportsPanelState`,
    the TabState pattern) so the window reopens the way it was left."""

    def __init__(self):
        super().__init__()
        self.open = {}


def date_bucket(epoch, now=None):
    """The section a report files under by its day, local time: "Today",
    "Yesterday", else the date ("Mon Sep 2, 2026")."""
    when = time.localtime(epoch)
    today = time.localtime(time.time() if now is None else now)
    day = (when.tm_year, when.tm_yday)
    if day == (today.tm_year, today.tm_yday):
        return "Today"
    yesterday = time.localtime(time.mktime(today) - 86400)
    if day == (yesterday.tm_year, yesterday.tm_yday):
        return "Yesterday"
    return time.strftime("%a %b ", when) + f"{when.tm_mday}, {when.tm_year}"


def _mix(style_manager, tint, value, factor, saturation):
    return style_manager.make_color_rgb(tint[0], tint[1], tint[2], value=value,
                                        factor=factor, saturation_scale=saturation)


def _color_u32(color, alpha=1.0):
    return imgui.get_color_u32_rgba(color[0], color[1], color[2], alpha)


def _ellipsize(text, max_width):
    if max_width <= 0:
        return ""
    if imgui.calc_text_size(text)[0] <= max_width:
        return text
    ellipsis = "…"
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if imgui.calc_text_size(text[:mid] + ellipsis)[0] <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low] + ellipsis if low else ellipsis


# disable_scroll=False: the @window draw path defaults it to True (most
# windows lay out their own scrolling); this one is a scrolling list.
@window(input_value=reports, tint=(0.23, 0.20, 0.20), icon=f"",
        display_name="Crash Reports", initial={"width": 720, "height": 480},
        disable_scroll=False)
@render_func(use_cache=True, selectable=False, show_add_delete=False,
             is_tree=False, show_name=True, shadow=True,
             is_default_for="CrashReportStore", tint=(0.86, 0.24, 0.2))
def draw_crash_reports(
        # [tint=(0.85, 0.75, 0.05)]
        input_value: CrashReportStore,
        draw_state, panel_state: CrashReportsPanelState = None, style_manager=None,
        left_mouse_down=False, **kwargs):
    store = input_value
    store.refresh_if_stale()
    open_rows = panel_state.open if panel_state is not None else {}

    # ---- styling (fast_dock colours) ----
    row_bg_value, row_text_value = 0.06, 0.95         # a collapsed row
    open_bg_value, open_text_value = 0.14, 1.25       # an expanded row
    factor, saturation = 0.90, 1.0
    hover_bg_boost, hover_text_boost = 0.05, 0.5
    text_saturation = 0.8
    section_text_value = 0.75                          # the Today / Yesterday / date headers
    # Fixed design colours (rule 18): the error text, the dim time/thread/commit
    # label, and the delete buttons (the editor's error-chip red).
    error_color = (0.95, 0.45, 0.4)
    meta_color = (0.62, 0.65, 0.72)
    delete_color = (0.85, 0.12, 0.14)

    # ---- icons (glyph literals - the editor renders them as a picker) ----
    trash_icon = f""                                 # delete this report / clear all
    section_icon = f""                               # calendar, on the date section headers
    open_icon = f""                                  # chevron on an expanded row
    closed_icon = f""                                # chevron on a collapsed row

    # ---- geometry, authored at ui_scale 1.0 and evaluated once per frame ----
    px = Melty.px
    # [tint=(0.939, 0.453, 0.245)]
    row_height = px(30.0)
    row_gap = px(4.0)
    pad_x = px(10.0)
    corner = px(6.0)
    toolbar_height = px(30.0)
    section_height = px(24.0)                         # a date section header row
    section_gap = px(6.0)                             # gap after a section header
    button_height = px(24.0)
    button_pad_x = px(10.0)
    trace_gap = px(4.0)                               # gap between a row and its stack trace view
    # [tint=(0.35, 0.85, 0.94)]
    chevron_inset = px(10.0)                          # chevron x inside the row
    text_inset = px(28.0)                             # error text x inset (after the chevron)
    meta_width = px(260.0)                            # the right-aligned "14:03 · thread · commit" column
    text_nudge_y = px(-1.0)

    if style_manager is None:
        style_manager = Melty.style_manager
    draw_list = imgui.get_window_draw_list()
    origin_x, origin_y = imgui.get_cursor_screen_pos()
    content_width = draw_state.content_width or (draw_state.width or 300)
    mouse_x, mouse_y = imgui.get_mouse_pos()
    hover_ok = draw_state._bounding_hovered
    press = left_mouse_down
    # [tint=(0.62, 0.47, 0.95)]
    click = (press.x, press.y) if (press and hasattr(press, "x")) else None
    clip = getattr(draw_state, "abs_clip_rect", None)
    line_height = imgui.get_text_line_height()
    row_left, row_right = origin_x + pad_x, origin_x + content_width - pad_x
    tint = draw_state.locate_tint
    # Flipped whenever this frame mutated the store (a delete) or the panel
    # state — it is the view's `changed` return (style guide rule 10).
    changed = False

    def visible(top, bottom):
        return clip is None or not (bottom < clip[1] or top > clip[3])

    def hovered(left, top, right, bottom):
        return hover_ok and left <= mouse_x <= right and top <= mouse_y <= bottom

    def clicked(left, top, right, bottom):
        return click is not None and left <= click[0] <= right and top <= click[1] <= bottom

    def draw_button(label, view_id, right, top):
        """A right-aligned flat_button ending at `right`; returns (left,
        pressed). Placed (pos=, layout=False): the row flow is manual here.
        The click is claimed through this view's on_action rect, so it
        works on blit-cache hits too."""
        width = imgui.calc_text_size(label)[0] + 2 * button_pad_x
        left = right - width
        pressed = flat_button(label, draw_state, view_id, width=width, height=button_height,
                              pos=(left, top), layout=False, color=delete_color,
                              tint_value=0.45, max_bg_brightness=0.6,
                              text_color=(1.0, 0.80, 0.78, 1.0), corner_radius=corner)
        return left, bool(pressed)

    # ---- toolbar: count + directory, Clear all ----
    toolbar_top = origin_y
    entries = list(store.items())
    clear_left, clear_pressed = row_right, False
    if entries:
        clear_left, clear_pressed = draw_button(f"{trash_icon} Clear all", "crash_clear_all", row_right,
                                                toolbar_top + (toolbar_height - button_height) / 2.0)
    if clear_pressed:
        store.remove_all()
        entries = []
        changed = True
    count_note = (f"{len(entries)} report{'s' if len(entries) != 1 else ''}   ·   "
                  f"{store.directory()}")
    if store.error:
        count_note += f"   ·   {store.error}"
    draw_list.add_text(row_left, toolbar_top + (toolbar_height - line_height) / 2.0,
                       _color_u32(meta_color, 0.8), _ellipsize(count_note, clear_left - px(10) - row_left))

    # ---- rows, under Today / Yesterday / date section headers ----
    row_top = toolbar_top + toolbar_height + row_gap
    section_color = _mix(style_manager, tint, section_text_value, factor, text_saturation)
    now = time.time()
    current_section = None
    for path, entry in entries:
        section = date_bucket(entry["mtime"], now)
        if section != current_section:
            if current_section is not None:
                row_top += section_gap                 # breathing room between sections
            current_section = section
            if visible(row_top, row_top + section_height):
                section_y = row_top + (section_height - line_height) / 2.0 + text_nudge_y
                draw_list.add_text(row_left, section_y, _color_u32(section_color), section_icon)
                draw_list.add_text(row_left + px(20), section_y, _color_u32(section_color), section)
            row_top += section_height + row_gap
        row_bottom = row_top + row_height
        is_open = bool(open_rows.get(entry["name"]))
        row_hovered = hovered(row_left, row_top, row_right, row_bottom)
        # The button: on the row's right while the pointer's on the
        # row (the cached tile re-renders when at hover edges, like the
        # fast dock's summon button).
        trash_left = row_right
        trash_pressed = False
        if row_hovered:
            trash_left, trash_pressed = draw_button(trash_icon, f"crash_delete_{entry['name']}",
                                                    row_right - px(4),
                                                    row_top + (row_height - button_height) / 2.0)
            trash_left -= px(6)

        if visible(row_top, row_bottom):
            bg_value, text_value = (open_bg_value, open_text_value) if is_open \
                else (row_bg_value, row_text_value)
            bg = _mix(style_manager, tint, bg_value + (hover_bg_boost if row_hovered else 0.0),
                      factor, saturation)
            fg = _mix(style_manager, tint, text_value + (hover_text_boost if row_hovered else 0.0),
                      factor, text_saturation)
            if is_open:
                add_shadow((row_left, row_top, row_right - row_left, row_height),
                           offset=11, corner_radius=corner, clip=clip)
            draw_list.add_rect_filled(row_left, row_top, row_right, row_bottom,
                                      _color_u32(bg), rounding=corner)
            text_y = row_top + (row_height - line_height) / 2.0 + text_nudge_y
            draw_list.add_text(row_left + chevron_inset, text_y, _color_u32(fg),
                               open_icon if is_open else closed_icon)
            # Time of day only - the section header carries the date.
            meta = time.strftime("%H:%M:%S", time.localtime(entry["mtime"]))
            if entry["thread"] and entry["thread"] != "MainThread":
                meta += f"  ·  {entry['thread']}"
            if entry["commit"]:
                sha, _, branch = entry["commit"].partition(" ")
                meta += f"  ·  {sha[:8]}{(' ' + branch) if branch else ''}"
            meta_right = trash_left - px(6)
            meta_fit = _ellipsize(meta, min(meta_width, meta_right - (row_left + text_inset)))
            meta_size = imgui.calc_text_size(meta_fit)
            draw_list.add_text(meta_right - meta_size[0], text_y, _color_u32(meta_color, 0.9), meta_fit)
            error_fit = _ellipsize(entry["error"] or entry["name"],
                                   meta_right - meta_size[0] - px(12) - (row_left + text_inset))
            draw_list.add_text(row_left + text_inset, text_y, _color_u32(error_color), error_fit)

        # ---- clicks ----
        if trash_pressed:
            store.remove(path)
            open_rows.pop(entry["name"], None)
            changed = True
        elif clicked(row_left, row_top, trash_left, row_bottom):
            if is_open:
                open_rows.pop(entry["name"], None)
            else:
                open_rows[entry["name"]] = True
            is_open = not is_open
            if panel_state is not None:
                panel_state.open = dict(open_rows)          # @live setattr: repaints the tile
            changed = True
            request_render()

        row_top = row_bottom + row_gap
        if trash_pressed or not is_open:
            continue

        # ---- stack trace under each expanded row ----
        # The stack trace VIEW (the code behind each frame, editable,
        # pending truth) - a nested render_func in the manual row flow:
        # park the cursor where the row ends, let the wrapper lay it out,
        # pull the cursor back for the next row. Locals aren't saved, so
        # the panes inherit no live values.
        trace_top = row_top - row_gap + trace_gap
        imgui.set_cursor_screen_pos((row_left, trace_top))
        # cull_offscreen=False: every pane lays out at its real height, in
        # view or not - this window scrolls by the trace's height, so that
        # height must not change under the scroll.
        _changed, _trace, trace_ds = draw_stack_trace(
            store.trace(path), name=f"crash_report_{entry['name']}",
            indent_views=False, file_headers=True, cull_offscreen=False, return_extras=True)
        # MANUAL height: the nested view's draw_state.height, not the cursor
        # it left behind - the wrapper advances the cursor by the live
        # layout on the first run and by the tile on a cache hit, and the two
        # differ by a few px, so a cursor-read total jittered every time a
        # pane re-rendered under the pointer. `height` is stamped from the
        # measured content and only moves when the content really does.
        trace_height = (trace_ds.height if trace_ds is not None and trace_ds.height
                        else imgui.get_cursor_screen_pos()[1] - trace_top)
        row_top = trace_top + trace_height + row_gap
        imgui.set_cursor_screen_pos((origin_x, row_top))

    if not entries and visible(row_top, row_top + row_height):
        draw_list.add_text(row_left, row_top + (row_height - line_height) / 2.0,
                           _color_u32(meta_color, 0.6), "No crash reports.")
        row_top += row_height

    # MANUAL content height: the wrapper's content() reads the cursor the
    # body leaves behind (observed content_height), so the body pins it at
    # its own total - rows + sections + each expanded trace's draw_state
    # height (above) - instead of whatever the nested view's layout left.
    # The dummy claims the width for the content rect; the setCursor after
    # it fixes the exact bottom (a dummy alone adds item spacing). The
    # top_inset term is fast_dock's: the clip covers the whole window,
    # header included, while rows start below the header.
    top_inset = (origin_y + draw_state.scroll_offset[1]) - draw_state.abs_top
    total_height = max(1.0, (row_top - origin_y) + max(0.0, top_inset))
    imgui.set_cursor_screen_pos((origin_x, origin_y))
    imgui.dummy(content_width, total_height)
    imgui.set_cursor_screen_pos((origin_x, origin_y + total_height))

    return changed, input_value