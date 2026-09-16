"""Stack trace view — the CODE behind a stack trace, not the usual file:line
text dump. `draw_any(exception)` / `draw_any(traceback)` routes here: every
frame draws just its span — the function's def line down to the call into the
next frame — as a small draw_text, stacked outermost-first, so the whole
trace reads top-to-bottom like one continuous file that starts at main and
ends on the line that raised.

Data access goes through `project_code` (core_conversion/project_code.py):
each pane's text is a LineRange over the file's PENDING truth (same caches
and queue as code_file_io — disk is never read when a cache can answer), so
panes are editable and stay in sync with the code editor; an edit writes back
through the range proxy (one queued whole-file entry, the editor-keystroke
shape) and _shift_panes moves the other panes' spans by the line delta.

Everything renders from LOCAL state — nothing is published anywhere global:
frame locals become per-pane `LocalValueStore`s (live_view.frame_value_store)
handed to draw_text as `live_store=`, and each pane's `code_dict` is its own
span parse (cached on the pane, keyed by the file text's identity), so the
live-value pills/markers/windows draw over the code exactly as in the editor
while two traces of the same function coexist untouched. Span bounds resolve
once per capture from the pending-text ast (live_view._ast_for — mtime +
pending-generation cached).
"""

import colorsys
import types
from pathlib import Path

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color

from meltygui.core.styling.fonts import Font
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.runtime.toggles import Tint
from meltygui.code.fileref import Address
from meltygui.code.fileref import _PROJECT_ROOT
from meltygui.code.project_code import project_code
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import no_save


from meltygui.model.trace_model import SavedTrace


class _RaisingLine(Exception):
    """The raising line as draw_text's `error=` marker (see
    `_exception_errors`: `.lineno` = 1-based BUFFER line, `.msg` = the
    message) — the editor's own red gutter chip, click for the wash."""

    def __init__(self, lineno, msg):
        super().__init__(msg)
        self.lineno = lineno
        self.msg = msg


def stack_frames(value):
    """Normalize `value` into [(path, lineno, function_name, scope), ...]
    with the OUTERMOST frame (main) first and the raising frame last. `scope`
    is a shallow copy of the frame's f_locals when a live frame is available
    (traceback / frame input), else None (FrameSummary / bare tuples).

    Accepts a live traceback, an exception (its __traceback__), a frame
    object, or an iterable of traceback.FrameSummary / (path, lineno[, name])
    tuples. A traceback only starts at the frame that RAISED under the catch
    site, so the frames above it (catch site up to main) are recovered from
    the outermost traceback frame's f_back chain."""
    if isinstance(value, SavedTrace):
        value = value.frames
    elif isinstance(value, BaseException):
        value = value.__traceback__
    frames = []
    if isinstance(value, types.TracebackType):
        above = []
        outer = value.tb_frame.f_back
        while outer is not None:
            above.append((outer.f_code.co_filename, outer.f_lineno,
                          outer.f_code.co_qualname, dict(outer.f_locals)))
            outer = outer.f_back
        frames.extend(reversed(above))
        current = value
        while current is not None:
            frame = current.tb_frame
            frames.append((frame.f_code.co_filename, current.tb_lineno,
                           frame.f_code.co_qualname, dict(frame.f_locals)))
            current = current.tb_next
    elif isinstance(value, types.FrameType):
        walker = value
        while walker is not None:
            frames.append((walker.f_code.co_filename, walker.f_lineno,
                           walker.f_code.co_qualname, dict(walker.f_locals)))
            walker = walker.f_back
        frames.reverse()
    elif value is not None:
        for entry in value:
            if hasattr(entry, "filename"):        # traceback.FrameSummary
                frames.append((entry.filename, entry.lineno,
                               getattr(entry, "name", "") or "", None))
            else:                       # (path, lineno[, name[, scope]])
                path, lineno = entry[0], entry[1]
                scope = entry[3] if len(entry) > 3 \
                    and isinstance(entry[3], dict) else None
                frames.append((str(path), int(lineno),
                               entry[2] if len(entry) > 2 else "", scope))
    return frames


class _Pane:
    """One frame's resolved render plan. All line fields are 1-based
    PENDING-truth file lines: `first` = def line (or context-window start),
    `last` = end of the call statement, `def_last` = end of the whole def
    (the parse extends to it so a mid-block cut can't break the parse),
    `lineno` = the call line itself."""
    __slots__ = ("path", "lineno", "qualname", "scope", "first", "last",
                 "def_last", "store", "resolved", "expand_diff",
                 "fold_gen_seen", "view", "address", "file_code",
                 "render_memo", "last_height", "ds", "has_def",
                 "store_tried", "parse_memo", "parse_armed", "error_marker",
                 "error_opened", "seen_height")

    def __init__(self, path, lineno, qualname, scope):
        self.path = path
        self.lineno = lineno
        self.qualname = qualname
        self.scope = scope
        self.first = None
        self.last = None
        self.def_last = None
        self.store = None
        self.resolved = False
        # Middle-fold switch: starts open; the first manual badge toggle
        # hands the state to individual tracking (None) - the owner-switch
        # dance draw_code_editor's diff folds use.
        self.expand_diff = False
        self.fold_gen_seen = 0
        self.view = None       # (file_text_obj, bounds, buffer, parse, indent)
        self.address = None    # (bounds, Address) = per only, rebuilt on shift
        self.file_code = None  # project_code FileCode (memoized: the lookup
                               # resolves the path - syscalls - per call)
        # (bounds, max_span_lines, line_numbers, folds): draw_text kwargs
        # must keep a STABLE IDENTITY frame to frame - two of its
        # staleness checks compare by identity (the merge window memoizes
        # its line-number lists for the same reason), so not one list per
        # frame re-rendered every pane every frame: constant GC, and the
        # baked marker band flickering with every scroll.
        self.render_memo = None
        # Measured layout advance of the last real draw - what the
        # off-screen skip claims with a cursor move instead of rendering.
        self.last_height = None
        self.ds = None         # this pane's draw_state, for the skip's evs
        self.has_def = False   # bounds found an enclosing def
        self.store_tried = False   # lazy store: one build attempt per capture
        self.parse_memo = None     # (span_text_obj, cst dict | None)
        self.parse_armed = None    # span_text the background parse ran for
        self.error_marker = None   # the raising pane's _RaisingMarker (stable identity)
        self.error_opened = False  # its context box opened once on first sight
        self.seen_height = None    # advance measured while the pane was in view

    def bounds(self):
        return (self.first, self.last, self.def_last)


from meltygui.state.trace_state import StackTraceState


def _span_bounds(span_index, lineno):
    """(def_first, def_last, stmt_last) in 1-based lines from a FileCode
    span_index (one ast walk per text version — never a walk per query):
    the innermost def containing `lineno`, and the end of the statement
    STARTING at `lineno` (the call into the next frame — a multi-line call
    spans to its closing paren). Containment includes the def's DECORATOR
    lines (a decorated function's co_firstlineno — the terminal
    render-function frame — points at the first decorator, above the def
    itself); `def_first` is always the def line. def bounds are None for a
    module-level frame."""
    defs, stmt_ends = span_index
    best = None
    for span_start, def_line, end in defs:
        if span_start <= lineno <= end and (best is None
                                            or def_line > best[0]):
            best = (def_line, end)
    stmt_last = max(lineno, stmt_ends.get(lineno, lineno))
    if best is None:
        return None, None, stmt_last
    return best[0], best[1], stmt_last


def chain_shift(def_indent, call_indent, parent_call_col, indent_step=4):
    """How many COLUMNS to slide a frame's whole view right so the callee's
    BODY lines up with the call line it replaces: the call `self.render()`
    and the lines inside `def render` are the same thing, so they share an
    indentation level — which puts the def line one level ABOVE the call,
    aligned with the call's enclosing block header. Returns
    (shift_columns, call_col): this frame's slide beyond its own def indent,
    and its own call line's DISPLAYED column for the next frame.

    `parent_call_col` is None for the first frame (no shift). The shift
    never goes negative (clamped at 0): a def already deeper than its target
    — a closure called from a shallow frame — keeps its own indentation."""
    shift = 0
    if parent_call_col is not None:
        shift = max(0, parent_call_col - indent_step - def_indent)
    return shift, shift + call_indent


def _indent_of(row):
    return len(row) - len(row.lstrip())


def _dedent_rows(rows, margin):
    """Strip exactly `margin` leading spaces from every row that carries
    them (blank/shallower rows pass through) — the buffer draw_text sees;
    the view-slide adds the indentation back visually."""
    if margin <= 0:
        return list(rows)
    prefix = " " * margin
    return [row[margin:] if row.startswith(prefix) else row for row in rows]


def _reindent_rows(rows, margin):
    """The inverse of _dedent_rows for writing an edited buffer back into
    file columns: non-blank rows regain the margin."""
    if margin <= 0:
        return list(rows)
    prefix = " " * margin
    return [prefix + row if row.strip() else row for row in rows]


def _span_parse(text):
    """cst dict of a (dedented, syntactically whole) span text, through the
    same converter the code hosts run. None when it doesn't parse — the pane
    then renders without live-value anchors until the next clean text."""
    try:
        from meltygui.core.runtime.toggles import Toggles
        from meltygui.code.libcst_conversion import cst_module_to_dict
        if Toggles.TextEditor.melty_syntax:
            return cst_module_to_dict(text)
        import libcst as cst
        return cst_module_to_dict(cst.parse_module(text))
    except Exception:
        return None


def _resolve_pane(pane, context_lines):
    """Fill a pane's span bounds — CHEAP: the file's span index (one ast
    walk per text version, shared by every pane of the file) plus lookups.
    Runs once per capture. The heavy halves — the value store (a file parse
    for anchors) and the span parse (libcst of the whole def) — build
    lazily at the pane's first VISIBLE draw (_ensure_store / _pane_view),
    so off-screen panes cost nothing at load. Bounds resolve against the
    SAME text the pane renders — the project_code proxy (studio truth:
    sync frame + pending), never a different cache's view."""
    if pane.file_code is None:
        pane.file_code = project_code[pane.path]
    span_index = pane.file_code.span_index()
    if span_index is None:
        pane.resolved = True       # unreadable/unparseable: context window
        pane.first = max(1, pane.lineno - context_lines)
        pane.last = pane.def_last = pane.lineno
        return
    def_first, def_last, stmt_last = _span_bounds(span_index, pane.lineno)
    if def_first is None:                   # module-level frame
        pane.first = max(1, pane.lineno - context_lines)
        pane.def_last = stmt_last
    elif pane.lineno <= def_first:
        # TERMINAL pane: whose lineno IS the def region (co_firstlineno - the
        # render function the trace bottoms out in, whose body wasn't on the
        # stack). No call to cut at - render the whole function.
        pane.first = def_first
        pane.lineno = def_first
        pane.def_last = def_last
        stmt_last = def_last
    else:
        pane.first = def_first
        pane.def_last = def_last
    pane.last = max(stmt_last, pane.first)
    pane.def_last = max(pane.def_last, pane.last)
    pane.has_def = def_first is not None
    pane.resolved = True


def _ensure_store(pane):
    """Build the pane's LocalValueStore on FIRST NEED (its first visible
    draw): the anchor computation resolves the live function and parses the
    file (live_view._ast_for) — too heavy to pay per pane at tab open, and
    an off-screen pane may never need it. One attempt per capture."""
    if (pane.store is not None or pane.store_tried or not pane.scope
            or not pane.has_def):
        return
    pane.store_tried = True
    from meltygui.code.chain_converters import _enclosing_function
    from meltygui.code.live_view import frame_value_store
    fn = _enclosing_function(pane.path, pane.lineno)
    if fn is not None:
        # Reuse the proxy's ast - the very tree the bounds resolved on -
        # instead of _ast_for's second whole-file parse; anchors land in
        # the rendered text's own coordinates.
        pane.store = frame_value_store(
            fn, pane.scope,
            tree=pane.file_code.ast_tree() if pane.file_code else None)


# Defs up to this many lines parse INLINE at their FIRST visible draw (a few
# ms; the pane shows its live-value anchors on the frame it appears); longer
# ones parse in the BACKGROUND and the pane renders without anchors until the
# parse lands — a 6.5k-line def's parse was the old tab-open stall. Every
# RE-parse (the span's text changed: a keystroke here, an editor typing in
# the same file) is debounced + backgrounded regardless of size, exactly as
# code_file_io's chain_in — a keystroke used to re-parse inline, per pane of
# the file, per frame it landed on (09-01).
# [tint=(0.9, 0.35, 0.28)]
PARSE_INLINE_LINES = 300


def _pane_parse(pane, span_text, index):
    """The pane's span cst dict, memoized on the span text's identity.

    Parses run through run_in_background — the same runner and typing
    debounce (`_chain_in_debounce_ms`, Toggles.TextEditor.parse_debounce_ms /
    small_file_debounce_ms) as the code editor's chain_in, so a burst of
    keystrokes coalesces into ONE parse when the input goes quiet. The first
    parse of a small def is `inline_first` (runs synchronously, anchors on
    the pane's first frame); a giant def's first parse and every re-parse go
    to the worker. While a re-parse is pending the pane keeps its previous
    parse (stale anchors label-snap) or renders plain code. Only panes with
    a value store parse at all.

    The runner idle-returns its LAST result — inside the debounce window
    that is the previous text's parse — so a result is adopted only when it
    is a NEW object (identity against the memo) or arrives on the report
    edge; stamping the old parse against the new text would have memoized
    it for good and the landed re-parse would never have been read."""
    if pane.store is None:
        return None
    memo = pane.parse_memo
    if memo is not None and memo[0] is span_text:
        return memo[1]
    from meltygui.code.new_converters import run_in_background
    from meltygui.code.new_converters import _chain_in_debounce_ms
    arm = pane.parse_armed is not span_text
    if arm:
        pane.parse_armed = span_text
    small = span_text.count("\n") + 1 <= PARSE_INLINE_LINES
    landed, result = run_in_background(
        _span_parse, child_kwargs={"text": span_text},
        name=f"stack span parse {pane.qualname}##{index}", start=arm,
        inline_first=small, debounce_ms=_chain_in_debounce_ms(span_text))
    previous = memo[1] if memo is not None else None
    if landed or (isinstance(result, dict) and result is not previous):
        pane.parse_memo = (span_text, result if isinstance(result, dict)
                           else None)
        return pane.parse_memo[1]
    return previous


def _pane_view(pane, file_code):
    """(buffer, span_text, def_indent) for a pane, memoized on the file
    text's identity + the pane's bounds. `buffer` is the DEDENTED def→call
    slice (draw_text sees dedented text, like every span editor; the
    view-slide re-adds the indentation); `span_text` the dedented WHOLE def
    (always valid Python — the def→call cut can end mid-block), the parse
    input: against the buffer it is one trailing deletion, which the
    overlay's parse→buffer line bridge maps exactly. Parsing itself is the
    CALLER's job (inline for small defs, background for giant ones)."""
    whole = file_code.text()
    if whole is None:
        return None
    bounds = pane.bounds()
    view = pane.view
    if view is not None and view[0] is whole and view[1] == bounds:
        return view[2], view[3], view[4]
    lines = file_code.lines()
    first, last, def_last = bounds
    last = min(last, len(lines))
    def_last = min(def_last, len(lines))
    def_indent = _indent_of(lines[first - 1]) if first <= len(lines) else 0
    span_rows = _dedent_rows(lines[first - 1:def_last], def_indent)
    buffer = "\n".join(span_rows[:last - first + 1])
    span_text = "\n".join(span_rows)
    pane.view = (whole, bounds, buffer, span_text, def_indent)
    return buffer, span_text, def_indent


def _crop_folds(span_rows, call_row, max_span_lines):
    """The fold for a span over `max_span_lines`, in 0-based BUFFER rows
    ((start, end): row `start` stays visible as the fold header, rows
    start+1..end hide). A call mid-span folds the MIDDLE (head below the
    def, tail above the call); a terminal pane (call_row 0 — the whole
    render function) folds the TAIL, keeping the first max_span_lines rows.
    None when the span fits."""
    if span_rows <= max_span_lines:
        return None
    if call_row <= 0:
        return [(max_span_lines - 1, span_rows - 1)]
    keep = max(2, max_span_lines // 2)
    fold_start = keep - 1
    fold_end = call_row - keep
    if fold_end <= fold_start:
        return None
    return [(fold_start, fold_end)]


def _shift_panes(panes, edited_pane, path, edit_line, delta,
                 edited_span=None, file_code=None):
    """After an edit in `edited_pane` changed its file's line count by
    `delta` at 1-based `edit_line`, move every OTHER pane of that file so
    the spans keep addressing the same code.

    With `edited_span` (the edited pane's PRE-edit (first, last)) and the
    file's `file_code`, a sibling whose def the edit did not touch also
    CARRIES its view memo onto the new file text: its dedented rows are the
    same rows, so re-keying the memo (new text object, shifted bounds) keeps
    its `span_text` identity and the parse memo behind it — nothing to
    re-dedent, nothing to re-parse. Content-free: the edit's own span
    decides; a sibling whose def overlaps it (recursion, a closure frame
    inside the edited def) drops its view and rebuilds. Without this every
    pane of the file re-parsed on every keystroke in any one of them."""
    if delta or edited_span is not None:
        new_whole = file_code.text() if file_code is not None else None
        edit_first, edit_last = edited_span or (edit_line, edit_line)
    for pane in panes or ():
        if pane is None or pane is edited_pane or pane.path != path \
                or not pane.resolved:
            continue
        untouched = (edited_span is not None and new_whole is not None
                     and pane.first is not None and pane.def_last is not None
                     and (pane.def_last < edit_first or pane.first > edit_last))
        if delta:
            if pane.first is not None and pane.first > edit_line:
                pane.first += delta
            if pane.last is not None and pane.last >= edit_line:
                pane.last += delta
            if pane.def_last is not None and pane.def_last >= edit_line:
                pane.def_last += delta
            if pane.lineno >= edit_line:
                pane.lineno += delta
            store = pane.store
            if store is not None:
                def_line = getattr(store, "__def_line__", None)
                if def_line is not None and def_line > edit_line:
                    store.__def_line__ = def_line + delta
        if edited_span is None:
            continue
        view = pane.view
        if untouched and view is not None:
            pane.view = (new_whole, pane.bounds()) + view[2:]
        else:
            pane.view = None


from meltygui.view.trace_view import _draw_file_header


from meltygui.view.trace_view import draw_stack_trace