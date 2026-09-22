"""Crash Reports — the traces print_stack_trace saved under
`Toggles.CrashReports.directory` (~/.lsd/crash_reports by default), one file
per trace (glfw_utils.save_crash_report), listed newest first.

Every Melty process saves into the one folder, so the list is all of their
crashes in one chronological run; each report's `app` header (the saving
process's app ID) is what the toolbar's dropdown filters by. FileWatch events
on the folder (`watch_reports`) repaint the list for a report another process
saved.

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

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

from meltygui.core.melty import Melty
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.windowing.glfw_utils import crash_reports_dir
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.cache.tile_marks import add_shadow
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window
from meltygui.core.layout.header_runtime import _brightness_clamp_fn
from meltygui.editor.source_ui import _tab_text_color
from meltygui.editor.text_editor import COLORS
from meltygui.editor.source_ui import _file_meta_tint

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


# The folder FileWatch reports on (resolved str); None until the list first shows.
_watched_directory = None


def _on_report_file_event(src_path):
    """FileWatch global listener (the watchdog OBSERVER thread): a report
    another Melty process saved, or one deleted by hand. The idle window is a
    cached tile whose body never runs, so nothing else would show it."""
    if isinstance(src_path, str) and _watched_directory and src_path.startswith(_watched_directory):
        reports_changed()


def watch_reports(directory):
    """Have the reports folder's file events repaint the list. Idempotent per
    folder; a changed Toggles.CrashReports.directory moves the watch."""
    global _watched_directory
    from meltygui.core.melty import FileWatch
    directory = str(Path(directory).resolve())
    if directory == _watched_directory and _on_report_file_event in FileWatch.global_listeners:
        return
    # Hotswap-safe: an older copy of the listener is replaced by this one.
    FileWatch.global_listeners[:] = [listener for listener in FileWatch.global_listeners
                                     if getattr(listener, "__name__", "") != "_on_report_file_event"]
    FileWatch.global_listeners.append(_on_report_file_event)
    FileWatch.start()
    if _watched_directory is not None:
        FileWatch.unwatch_dir(_watched_directory)
    Path(directory).mkdir(parents=True, exist_ok=True)
    _watched_directory = directory if FileWatch.watch_dir(directory) else None


# ──────────────────────────────────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────────────────────────────────

from meltygui.model.trace_model import CrashReportStore


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


from meltygui.state.trace_state import CrashReportsPanelState


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
    return pack_color(color[0], color[1], color[2], alpha)


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
from meltygui.view.trace_view import draw_crash_reports
draw_crash_reports = window(input_value=reports, tint=(0.25, 0.23, 0.23), icon=f'\uf188', display_name='Crash Reports', initial={'width': 720, 'height': 480}, disable_scroll=False)(draw_crash_reports)