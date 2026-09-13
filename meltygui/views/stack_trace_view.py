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

import imgui
from src.lsd.gl_gui.hdr_color import pack_color

from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Tint
from src.lsd.gl_gui.view.core_conversion.address import Address, _PROJECT_ROOT
from src.lsd.gl_gui.view.core_conversion.project_code import project_code
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save


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


@no_save("panes", "trace_obj", "trace_sig", "index_armed",
         "scroll_bottom_pending")
class StackTraceState(DictConversion):
    """Injected per-view state (`trace_state: StackTraceState = None`). All
    fields are derived from the input trace and hold live objects (frame
    locals, LocalValueStores, parses), so nothing here persists — a fresh
    session re-resolves from the input."""

    def __init__(self):
        super().__init__()
        self.panes = None              # [_Pane | None(elision)] per frame
        self.trace_obj = None          # the input the panes were built from
        self.trace_sig = None          # (project_only, max_frames)
        self.index_armed = None        # paths whose span_index runner armed
        # Fresh capture → open at the BOTTOM (the view function the trace
        # bottoms out in); pinned until the bottom pane has really rendered.
        self.scroll_bottom_pending = False


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
        from src.lsd.gl_gui.toggles import Toggles
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
            cst_module_to_dict)
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
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _enclosing_function)
    from src.lsd.gl_gui.view.core_conversion.live_view import (
        frame_value_store)
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
    from src.lsd.gl_gui.view.core_conversion.new_converters import (
        run_in_background, _chain_in_debounce_ms)
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


def _draw_file_header(pane, x, y, width, height, text_x=None, draw_state=None, index=0):
    """One printed-trace line above a pane: `File "<path>", line N, in
    <func>`, on a card that WRAPS the pane — a rect from the header's top
    down past the pane's bottom, painted through the pane's OWN background
    recipe (`BlitCache.draw_freeze_bg(pane.ds, …, live=False)` replays the
    depth / bg-stack / style-tint the pane's wrapper captured), so card and
    pane are the same colour by construction; the pane then draws over it.
    Nothing is painted until the pane has a draw_state (its first frame).
    Text: the editor tabs' colour — the file's painted FileMeta tint (the
    tabs' default tint when unpainted) scaled by the active-tab knobs, so
    it is tinted toward the card, never grey."""
    from src.lsd.gl_gui.melty import Melty
    from src.lsd.gl_gui.model.open_files import FileMeta
    from src.lsd.gl_gui.toggles import Toggles
    from src.lsd.gl_gui.view.core_views.new_core_view import _file_meta_tint
    from src.lsd.gl_gui.view.playground.open_files import _tab_text_color
    text_pad_x = Melty.px(8)
    text_pad_y = Melty.px(3)
    # The card's colour is the pane's own bg (below) pushed darker and more
    # saturated — change the header/pane contrast here.
    # [tint=(0.9, 0.35, 0.28)]
    card_saturation = 1.6
    card_value = 0.75
    draw_list = imgui.get_window_draw_list()
    # The card: a plain draw-list rect from the pane's OWN background
    # colour — `bg_color`, stamped on its draw_state by the wrapper
    # (compute_bg_color with the file tint pushed) — so card and pane share
    # a hue by construction. Square, straight to the list.
    # Painted two channels BELOW the body's: the panes' tint washes and
    # live-value pills sit a channel under their text, and a card on the
    # body's own channel fought them.
    # [tint=(0.9, 0.35, 0.28)]
    card_channel_drop = 2
    bg_color = getattr(pane.ds, "bg_color", None) if pane.ds is not None else None
    if bg_color is not None:
        hue, sat, val = colorsys.rgb_to_hsv(*bg_color[:3])
        card = colorsys.hsv_to_rgb(hue, min(1.0, sat * card_saturation), val * card_value)
        if Melty.channels_split:
            draw_list.channels_set_current(max(0, Melty.get_channel() - card_channel_drop))
        draw_list.add_rect_filled(x, y, x + width, y + height,
                                  pack_color(card[0], card[1], card[2], 1.0))
        if Melty.channels_split:
            draw_list.channels_set_current(Melty.get_channel())
    tint = _file_meta_tint(pane.path) or FileMeta.tint
    rgb = _tab_text_color(tint, Toggles.CodeEditor.tab_active_text_brightness,
                          Toggles.CodeEditor.tab_active_text_saturation,
                          Toggles.CodeEditor.tab_active_text_min_brightness)
    name_u32 = pack_color(rgb[0], rgb[1], rgb[2], 1.0)
    rest_u32 = pack_color(rgb[0], rgb[1], rgb[2], 0.7)
    try:
        shown = str(Path(pane.path).resolve().relative_to(_PROJECT_ROOT))
    except (OSError, ValueError):
        shown = pane.path
    pen = (text_x if text_x is not None else x) + text_pad_x
    text_y = y + text_pad_y
    line_height = imgui.get_text_line_height()
    jump_rects = {}
    for key, text, color in (("", 'File "', rest_u32), ("file", shown, name_u32),
                             ("", f'", line {pane.lineno}, in ', rest_u32),
                             ("func", pane.qualname, name_u32)):
        draw_list.add_text(pen, text_y, color, text)
        text_w = imgui.calc_text_size(text)[0]
        if key:
            jump_rects[key] = (pen, text_y, pen + text_w, text_y + line_height)
        pen += text_w
    # Ctrl+B anywhere on the header line jumps to the code editor at that
    # frame's line; on the FUNCTION name it also lands the caret on the
    # def (its rect registers one level above the name's). on_action
    # rects on the trace's draw_state, replayed on cache hits like every
    # body action. The whole line, not the name's glyph box: the 18 px
    # target a few px above a pane's def row was too easy to miss.
    if draw_state is not None:
        from src.lsd.gl_gui.view.playground.open_files import open_in_editor
        # At least the text's extent - a view whose width is not measured
        # yet (first frames, the render harness) would give a zero rect.
        line_rect = (x, y, max(x + width, pen), y + text_pad_y + line_height + text_pad_y)
        fired = None
        if draw_state.on_action("ctrl_b_down", view_id=f"trace_jump_func_{index}",
                                rect=jump_rects["func"], priority_delta=5) is not None:
            fired = "func"
        elif draw_state.on_action("ctrl_b_down", view_id=f"trace_jump_file_{index}",
                                  rect=line_rect, priority_delta=4) is not None:
            fired = "file"
        if fired:
            token = pane.qualname.rsplit(".", 1)[-1] if fired == "func" else None
            open_in_editor(pane.path, pane.lineno, token=token)


@render_func(is_default_for=(types.TracebackType, BaseException, SavedTrace),use_cache=True, tint=(0.9, 0.35, 0.28))
def draw_stack_trace(input_value: types.TracebackType | BaseException | SavedTrace,
                     draw_state=None, trace_state: StackTraceState = None,
                     max_span_lines=80, context_lines=8, freeze_resize=True,
                     project_only=True, max_frames=40, indent_views=True,
                     hide_dispatch=False, placeholder_lines=None, file_headers=False,
                     file_header_indent=18, cull_offscreen=True):
    """One draw_text per frame, outermost (main) first — each pane is a
    LineRange over the frame's file (project_code — pending truth, editable)
    from the def line to the call into the next frame, with the frame's
    locals drawn over it as live-value markers from a pane-local store.
    Knobs: `max_span_lines` folds the middle of a giant span; `project_only`
    hides stdlib / site-packages frames (turned off automatically when it
    would hide everything); `max_frames` caps very deep / recursive stacks,
    keeping the ends and eliding the middle; `indent_views=False` (the
    context menu's Code tab) keeps every pane flush left — buffers are
    dedented, so panes read like stacked defs — instead of the inlined
    call-chain slide. An exception input, or a SavedTrace with an `error`
    message, marks the RAISING line — the last frame's line — with
    draw_text's own error marker (`error=`: the red gutter chip, click for
    the line wash and message); a bare captured call stack (the context
    menu's Code tab, whose last frame is the view being drawn) marks
    nothing. `file_headers=True` reads like a printed trace: each pane sits
    under a `File "path", line N, in func` line (the path in the file's
    painted FileMeta tint — the editor tabs' colour) and is indented by
    `file_header_indent` px. `cull_offscreen=False` turns the viewport
    culling OFF: every pane resolves its bounds synchronously (the file's
    span index inline — the one-time parse the background path defers)
    and lays out at its real height whether or not it is in view, so the
    view's height is right from its first frame and never moves as the
    user scrolls. For a trace nested in a scrolling LIST (the Crash
    Reports window): a parent that scrolls by the child's height can't
    have that height change with the scroll — placeholders that resolved
    to different heights as panes scrolled in made the list jitter.
    `placeholder_lines` is the height, in lines, an UNRESOLVED pane
    below the viewport holds before its bounds are known (default
    max_span_lines + 2, sized for a self-scrolling view); a view nested in
    a scrolling LIST (the Crash Reports window) passes a small number, or
    every collapsed-away trace inflates the list by ~1900 px of empty
    scroll that snaps shorter as panes resolve.

    Panes fully outside the view's clip skip their draw_text call entirely:
    the cursor advances by the pane's last measured height, so the scroll
    geometry holds while only visible panes pay a render."""
    from src.lsd.gl_gui.melty import Melty
    # [tint=(0.9, 0.35, 0.28)]
    project_prefix = str(_PROJECT_ROOT)
    # File header line: `File "path", line N, in func` - the path in the
    # file's painted tint, the rest in the subtle-text colour.
    header_height = imgui.get_text_line_height() + Melty.px(6) if file_headers else 0.0
    header_indent = Melty.px(file_header_indent) if file_headers else 0.0
    card_pad_bottom = Melty.px(6) if file_headers else 0.0    # the card drops this far past the pane
    # The raising line's message - the marker's text; "" = no marker.
    if isinstance(input_value, SavedTrace):
        error_message = input_value.error
    elif isinstance(input_value, BaseException):
        error_message = f"{type(input_value).__name__}: {input_value}"
    else:
        error_message = ""

    # (Re)build the render plan only when the INPUT changes — steady-state
    # frames never re-walk the trace or re-copy files. The knobs join the
    # sig so a live toggle flip rebuilds.
    sig = (project_only, max_frames, hide_dispatch)
    if trace_state.trace_obj is not input_value or trace_state.trace_sig != sig:
        frames = [f for f in stack_frames(input_value)
                  if f[0] and not f[0].startswith("<")]
        if hide_dispatch:
            # Renderfunc dispatch machinery (the render_func wrapper /
            # draw_inner_main, draw_any re-dispatch) - the same filter the
            # func-stack labels apply per frame (_is_dispatch_frame).
            from src.lsd.gl_gui.view.core_conversion.chain_converters import (
                _is_dispatch_frame)
            frames = [f for f in frames
                      if not _is_dispatch_frame(f[0], f[2])]
        if project_only:
            kept = [f for f in frames if f[0].startswith(project_prefix)]
            if kept:
                frames = kept
        if len(frames) > max_frames:
            half = max_frames // 2
            frames = frames[:half] + [None] + frames[-half:]
        trace_state.panes = [
            _Pane(*f) if f is not None else None for f in frames]
        trace_state.trace_obj = input_value
        trace_state.trace_sig = sig
        trace_state.scroll_bottom_pending = True

    panes = trace_state.panes or []
    if not panes:
        RenderFuncs.draw_text("(no readable frames in this stack trace)",
                              name="stack trace empty", show_bg=False,
                              editable=False, tint=Tint.subtle_text())
        return False, input_value

    # Each callee's whole VIEW (gutter and all) slides right so its body
    # lines up with the call line it replaces; the shift is columns of the
    # editor's monospace font, converted to pixels here. Buffers are
    # dedented, so the slide carries the def's own indentation too.
    char_width = 0.0
    if indent_views:
        editor_font_handle = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) \
            if Melty.font_mgr is not None else None
        if editor_font_handle is not None:
            imgui.push_font(editor_font_handle)
            char_width = imgui.calc_text_size("0").x
            imgui.pop_font()
        else:
            char_width = imgui.calc_text_size("0").x
    available_width = draw_state.content_width
    view_clip = draw_state.abs_clip_rect

    parent_call_col = None
    index_polled = set()   # one span-index runner poll per FILE per frame
    # file_headers: every header (and its pane) sits this far in from the
    # card's left edge, and every pane stops this far short of the card's
    # right edge — the card is the full width, the text views narrower.
    # [tint=(0.9, 0.35, 0.28)]
    file_inset = Melty.px(2) if file_headers else 0.0
    # The cards span the view's FULL rect (abs_left / width - not the
    # relative content rect the cursor starts in), edge to edge.
    trace_left = draw_state.abs_left if file_headers else imgui.get_cursor_screen_pos()[0]
    card_width = draw_state.width or available_width
    error_pane = next((p for p in reversed(panes) if p is not None), None) \
        if error_message else None
    for index, pane in enumerate(panes):
        if pane is None:
            RenderFuncs.draw_text("… frames elided …",
                                  name="stack trace elision", single_line=True,
                                  show_bg=False, editable=False, freeze_resize=True,
                                  syntax_highlight=False,
                                  tint=Tint.subtle_text())
            continue
        # [tint=(0.9, 0.35, 0.28)]
        offscreen_margin = 300.0
        estimated_line_px = 23.0
        cursor_x, cursor_y = imgui.get_cursor_screen_pos()
        if not pane.resolved and not cull_offscreen:
            # No culling: bounds resolve, inline (span_index parses the file
            # synchronously when its memo holds it) - the height is real
            # from this frame on.
            if pane.file_code is None:
                pane.file_code = project_code[pane.path]
            _resolve_pane(pane, context_lines)
        if not pane.resolved:
            placeholder_height = pane.last_height \
                if pane.last_height is not None \
                else ((placeholder_lines if placeholder_lines is not None
                       else max_span_lines + 2) * estimated_line_px)
            # Below-viewport pre-skip, BEFORE resolving: bounds need the
            # FILE's ast (span_index - ~100 ms for the biggest files), so a
            # frame-1 pane far below the clip holds its place with a FIXED
            # estimate instead; it renders the frame the scroll brings it
            # near. (The indent chain doesn't advance for this - the Code tab
            # runs indent_views=False anyway, and the playground's chain
            # corrects when the pane really renders.)
            if (view_clip is not None
                    and cursor_y > view_clip[3] + offscreen_margin):
                imgui.set_cursor_screen_pos(
                    (cursor_x, cursor_y + header_height + placeholder_height))
                imgui.dummy(0, 0)
                continue
            # VISIBLE but unresolved: build the file's span index in the
            # background (its ast parse is the tab-open stall) and hold the
            # pane's place until the worker's result lands; then resolve is a
            # memo lookup. One runner poll per FILE per frame (a second
            # same-name call trips the duplicate-unique guard); an
            # unparseable file completes with a None index - span_index_ready
            # stays False, but the runner's completed edge resolves through
            # the context-window logic.
            if pane.file_code is None:
                pane.file_code = project_code[pane.path]
            if pane.file_code.span_index_ready():
                _resolve_pane(pane, context_lines)
            else:
                from src.lsd.gl_gui.view.core_conversion.new_converters import (
                    LOADING, UNSET, run_in_background)
                if trace_state.index_armed is None:
                    trace_state.index_armed = set()
                arm = pane.path not in trace_state.index_armed
                if arm:
                    trace_state.index_armed.add(pane.path)
                result = LOADING
                if pane.path not in index_polled:
                    index_polled.add(pane.path)
                    _changed, result = run_in_background(
                        pane.file_code.span_index, child_kwargs={},
                        name=f"stack span index {pane.path}", start=arm)
                if result is LOADING or result is UNSET:
                    imgui.set_cursor_screen_pos(
                        (cursor_x, cursor_y + header_height + placeholder_height))
                    imgui.dummy(0, 0)
                    continue
                _resolve_pane(pane, context_lines)
        if pane.file_code is None:
            pane.file_code = project_code[pane.path]
        file_code = pane.file_code
        file_lines = file_code.lines()
        if not file_lines:
            RenderFuncs.draw_text(f"(unreadable: {pane.path})",
                                  name=f"stack frame unreadable##{index}",
                                  single_line=True, show_bg=False, freeze_resize=True,
                                  editable=False, syntax_highlight=False,
                                  tint=Tint.subtle_text())
            continue
        def_indent = _indent_of(file_lines[pane.first - 1]) \
            if pane.first <= len(file_lines) else 0
        call_row = pane.lineno - pane.first

        # ── Off-screen skip - BEFORE the heavy lazy builds (store, libcst
        # span parse, dedent): a pane fully outside the view's clip pays
        # nothing but the bounds lookups above. A never-measured pane skips
        # on an ESTIMATED height (visible rows × a nominal line height) so
        # the first frame already renders only what's in view - no tab-open
        # lag of every pane parsing at once; the estimate corrects to the
        # measured height when the pane scrolls in. The body re-runs on
        # scroll, so panes crossing the edge render the frame they matter.
        span_rows = pane.last - pane.first + 1
        skip_height = pane.last_height
        if skip_height is None:
            visible_rows = min(span_rows, placeholder_lines if placeholder_lines is not None
                               else max_span_lines + 2)
            skip_height = visible_rows * estimated_line_px + 12.0
        skip_height += header_height
        cursor_x, cursor_y = imgui.get_cursor_screen_pos()
        if (cull_offscreen and view_clip is not None
                and (cursor_y > view_clip[3] + offscreen_margin
                     or cursor_y + skip_height
                     < view_clip[1] - offscreen_margin)):
            if indent_views:
                _, parent_call_col = chain_shift(
                    def_indent, _indent_of(file_lines[pane.lineno - 1])
                    if pane.lineno <= len(file_lines) else 0,
                    parent_call_col)
            # A skipped pane is DISCARDED, not closed - neither bvh_sync
            # (render-only) nor bvh_query's lazy evict would ever drop its
            # hit boxes, which remain at the on-screen positions in the
            # input of whatever scrolled in under them. Evict once on the
            # rendered→skipped transition; the next real render re-indexes.
            if pane.ds is not None:
                Melty.bvh_evict_window(pane.ds)
                pane.ds = None
            imgui.set_cursor_screen_pos(
                (cursor_x, cursor_y + skip_height))
            imgui.dummy(0, 0)
            continue

        # Lazy heavy halves, first visible draw only: the live store (a
        # file parse for anchors), then the span parse (libcst of the def).
        _ensure_store(pane)
        view = _pane_view(pane, file_code)
        if view is None:
            RenderFuncs.draw_text(f"(unreadable: {pane.path})",
                                  name=f"stack frame unreadable##{index}",
                                  single_line=True, show_bg=False, freeze_resize=True,
                                  editable=False, syntax_highlight=False,
                                  tint=Tint.subtle_text())
            continue
        buffer, span_text, def_indent = view
        parse = _pane_parse(pane, span_text, index)

        span_kwargs = {}
        # This pane's header start: the card's left edge plus file_inset -
        # the same for every pane; the pane's own indent never carries over.
        base_x = trace_left + file_inset if file_headers else cursor_x
        cursor_x = base_x
        if file_headers:
            # The card (full trace width, from the card's left edge) is
            # header + the pane's last measured advance tall (a fresh pane
            # has no draw_state yet and paints no card).
            pane_advance = pane.seen_height if pane.seen_height is not None else \
                (pane.last_height - header_height if pane.last_height else 0.0)
            _draw_file_header(pane, trace_left, cursor_y, card_width,
                              header_height + pane_advance + card_pad_bottom,
                              text_x=base_x, draw_state=draw_state, index=index)
            cursor_y += header_height
            cursor_x += header_indent
            imgui.set_cursor_screen_pos((cursor_x, cursor_y))
            # Even: the pane stops as far short of the card's right edge as
            # it starts in from the card's left.
            pane_width = card_width - 2 * (cursor_x - trace_left)
            if pane_width > 200:
                span_kwargs["width"] = pane_width
        if indent_views:
            shift_columns, parent_call_col = chain_shift(
                def_indent, _indent_of(file_lines[pane.lineno - 1])
                if pane.lineno <= len(file_lines) else 0, parent_call_col)
            slide_columns = def_indent + shift_columns
            if slide_columns:
                slide_px = slide_columns * char_width
                imgui.set_cursor_screen_pos((cursor_x + slide_px, cursor_y))
                # Keep the slid view's right edge inside the client rect.
                if available_width > slide_px + 200:
                    span_kwargs["width"] = available_width - slide_px
        marks_error = pane is error_pane and not (call_row <= 0 and pane.has_def)
        if pane.has_def and not marks_error:
            # Panes open COMPACT: the root def (buffer row 0 - spans are
            # dedented def→call slices) starts collapsed on the pane's first
            # sight; expanding sets the badge, and the user's fold state owns
            # it from then on. Module-frame panes have no def at row 0. The
            # RAISING pane opens expanded: collapsed, the raising line and
            # its marker sat hidden inside the fold.
            span_kwargs["default_collapsed_lines"] = (0,)
        # jump_to (the buffer's file-line offset) rides on every draw, not
        # only once the span parse lands: the editor's floating error box
        # - the raising line's message - is gated on it, and a pane with
        # no value store never parses at all.
        bounds = pane.bounds()
        if pane.address is None or pane.address[0] != bounds:
            pane.address = (bounds, Address(pane.path, pane.first - 1, pane.last))
        span_kwargs["jump_to"] = pane.address[1]
        if parse is not None:
            span_kwargs["code_dict"] = parse
            span_kwargs["live_store"] = pane.store
        # Stable-identity render kwargs, rebuilt only when the span moves
        # (see the render_memo comment on _Pane).
        memo = pane.render_memo
        if memo is None or memo[0] != pane.bounds() \
                or memo[1] != max_span_lines:
            memo = pane.render_memo = (
                pane.bounds(), max_span_lines,
                list(range(pane.first, pane.last + 1)),
                _crop_folds(pane.last - pane.first + 1, call_row,
                            max_span_lines))
        folds = memo[3]
        if folds is not None:
            span_kwargs["diff_fold_ranges"] = folds
            span_kwargs["expand_diff"] = pane.expand_diff
        # The raising line's marker (a terminal whole-function pane -
        # call_row 0 with a def - has no raising line). Memoized beside the
        # render kwargs: draw_text compares its kwargs by identity.
        if marks_error:
            marker = pane.error_marker
            if marker is None or marker.lineno != call_row + 1 \
                    or marker.msg != error_message:
                marker = pane.error_marker = _RaisingLine(call_row + 1, error_message)
            span_kwargs["error"] = marker

        edited, new_text, pane_ds = RenderFuncs.draw_text(
            buffer, name=f"stack frame {pane.qualname}##{index}",
            file_key=pane.path,
            line_numbers=memo[2], freeze_resize=True,
            show_header=False, show_file_header=False, show_jump_bar=False,
            gutter_indent=True, shadow=False, bg_offset=-1,
            # Cached: all panes are siblings in one window tile, so with
            # use_cache=False a selection drag in one pane re-ran every
            # span on every frame (10 draw_text bodies, 15ms - 09-01).
            use_cache=True, return_extras=True, **span_kwargs)
        # Measured layout advance (this item + its spacing) — what the
        # off-screen skip reproduces with a cursor move. The wrapper sizes
        # the pane by its whole imgui GROUP, and a pane lying entirely
        # in the fold has an item in draw_text's tail land at the clip
        # edge, so the group (and the cursor the wrapper advances) runs
        # from the pane's top down to the clip bottom - thousands of px
        # for a collapsed def, and different on every scroll. The
        # wrapper's `observed_content_height` is the honest cursor advance
        # of the body itself; when the advance overshoots it by more than
        # a line, lay out by the honest height and put the cursor there.
        advance = max(0.0, imgui.get_cursor_screen_pos()[1] - cursor_y)
        honest = getattr(pane_ds, "observed_content_height", 0) if pane_ds is not None else 0
        if honest > 0 and advance > honest + estimated_line_px:
            # Off-screen: the in-view measurement if there was one (the
            # off-screen honest delta itself wobbles by a px), else the
            # honest delta.
            advance = pane.seen_height if pane.seen_height is not None else float(honest)
            imgui.set_cursor_screen_pos((base_x, cursor_y + advance))
        else:
            pane.seen_height = advance
        pane.last_height = advance + header_height + card_pad_bottom
        if file_headers:
            imgui.set_cursor_screen_pos((base_x, cursor_y + advance + card_pad_bottom))
        # The raising pane shows its message box from the start - the
        # marker's open state (`_err_open_line`, the 0-based buffer row)
        # is stamped once on first sight; the user can close it from there.
        if marks_error and not pane.error_opened and pane_ds is not None:
            from src.lsd.gl_gui.view.playground.open_files import _diff_disp_span
            pane.error_opened = True
            # Markers live in DISPLAY rows (fold-mapped by draw_text); the
            # pane's fold map is stamped by the body that just ran.
            pane_ds._err_open_line = _diff_disp_span(pane_ds, call_row, call_row + 1)[0]
            pane_ds.invalidate()
        if file_headers and pane.ds is None and pane_ds is not None:
            # First sight: the card (drawn from pane.ds's captured bg
            # recipe) could not paint this frame - repaint next frame.
            draw_state.invalidate()
        pane.ds = pane_ds
        # A manual fold-badge toggle hands this pane's middle back to
        # automatic tracking (expand_diff=False would re-collapse it).
        manual_gen = getattr(pane_ds, "_diff_manual_gen", 0)
        if manual_gen != pane.fold_gen_seen:
            pane.fold_gen_seen = manual_gen
            pane.expand_diff = None
        # ── Edit write-through: through the range proxy into the file's PENDING
        # truth (the same queue the editor's keystrokes land in), then shift
        # this pane's call line and every affected pane's span by the delta.
        if edited and isinstance(new_text, str) and new_text != buffer:
            old_rows = buffer.split("\n")
            new_rows = new_text.split("\n")
            edit_row = 0                    # first differing 0-based row
            for row_index in range(min(len(old_rows), len(new_rows))):
                if old_rows[row_index] != new_rows[row_index]:
                    edit_row = row_index
                    break
            else:
                edit_row = min(len(old_rows), len(new_rows))
            span_range = file_code.get_lines(pane.first - 1, pane.last)
            edited_span = (pane.first, pane.last)
            span_range["value"] = "\n".join(
                _reindent_rows(new_rows, def_indent))
            delta = (span_range.end + 1) - (pane.last + 1)
            new_last = span_range.end       # 0-based end == 1-based last
            if edit_row <= call_row:
                pane.lineno += delta
            if pane.def_last is not None:
                pane.def_last += delta
            pane.last = new_last
            _shift_panes(panes, pane, pane.path,
                         pane.first + edit_row, delta,
                         edited_span=edited_span, file_code=file_code)

    # ── Open at the BOTTOM: the trace bottoms out at the view's, so
    # a new capture lands there. Write past the end and let the wrapper's
    # scroll clamp (max_scroll_y, next render) settle it; keep pinning
    # while the async resolves are still reshaping the content height, and
    # hand the scroll back to the user once the bottom pane has actually
    # rendered and been measured.
    # An auto-resizing view (nested in a scrolling parent) has no scroll of
    # its own to clamp - writing 10**9 there just leaves a junk offset.
    if trace_state.scroll_bottom_pending and draw_state.auto_resize:
        trace_state.scroll_bottom_pending = False
    if trace_state.scroll_bottom_pending:
        draw_state.scroll_offset = (draw_state.scroll_offset[0], 10 ** 9)
        bottom_pane = next((p for p in reversed(panes) if p is not None), None)
        if bottom_pane is None or bottom_pane.last_height is not None:
            trace_state.scroll_bottom_pending = False
    return False, input_value