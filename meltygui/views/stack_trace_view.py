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

import ast
import types
from pathlib import Path

import imgui

from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.model.dict_conversion import DictConversion
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Tint
from src.lsd.gl_gui.view.core_conversion.address import Address, _PROJECT_ROOT
from src.lsd.gl_gui.view.core_conversion.project_code import project_code
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import no_save


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
    if isinstance(value, BaseException):
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
            else:                                  # (path, lineno[, name])
                path, lineno = entry[0], entry[1]
                frames.append((str(path), int(lineno),
                               entry[2] if len(entry) > 2 else "", None))
    return frames


class _Pane:
    """One frame's resolved render plan. All line fields are 1-based
    PENDING-truth file lines: `first` = def line (or context-window start),
    `last` = end of the call statement, `def_last` = end of the whole def
    (the parse extends to it so a mid-block cut can't break the parse),
    `lineno` = the call line itself."""
    __slots__ = ("path", "lineno", "qualname", "scope", "first", "last",
                 "def_last", "store", "resolved", "expand_diff",
                 "fold_gen_seen", "view", "address")

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

    def bounds(self):
        return (self.first, self.last, self.def_last)


@no_save("panes", "trace_obj", "trace_sig")
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


def _span_bounds(tree, lineno):
    """(def_first, def_last, stmt_last) in 1-based lines from the pending
    text's ast: the innermost def containing `lineno`, and the end of the
    statement STARTING at `lineno` (the call into the next frame — a
    multi-line call spans to its closing paren). def bounds are None for a
    module-level frame."""
    best = None
    stmt_last = lineno
    for node in ast.walk(tree):
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if start is None or end is None:
            continue
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and start <= lineno <= end
                and (best is None or start > best[0])):
            best = (start, end)
        if isinstance(node, ast.stmt) and start == lineno and end > stmt_last:
            stmt_last = end
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
    """Fill a pane's span bounds from the pending-text ast (cached — see
    live_view._ast_for) and build its local value store. Runs once per
    capture."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _enclosing_function)
    from src.lsd.gl_gui.view.core_conversion.live_view import (
        _ast_for, frame_value_store)
    path = Path(pane.path)
    try:
        tree, _text, _sig = _ast_for(path, path.stat().st_mtime)
    except (OSError, SyntaxError, ValueError):
        pane.resolved = True       # unparseable file: context window only
        pane.first = max(1, pane.lineno - context_lines)
        pane.last = pane.def_last = pane.lineno
        return
    def_first, def_last, stmt_last = _span_bounds(tree, pane.lineno)
    if def_first is None:                   # module-level frame
        pane.first = max(1, pane.lineno - context_lines)
        pane.def_last = stmt_last
    else:
        pane.first = def_first
        pane.def_last = def_last
    pane.last = max(stmt_last, pane.first)
    pane.def_last = max(pane.def_last, pane.last)
    if pane.scope and def_first is not None:
        fn = _enclosing_function(pane.path, pane.lineno)
        if fn is not None:
            pane.store = frame_value_store(fn, pane.scope)
    pane.resolved = True


# Defs longer than this reparse only when their BOUNDS change, not per
# keystroke — a 5k-line def's libcst parse per edit frame is the old lag.
# [tint=(0.9, 0.35, 0.28)]
PARSE_LINE_CAP = 1500


def _pane_view(pane, file_code):
    """(buffer, parse, def_indent) for a pane, memoized on the file text's
    identity + the pane's bounds. `buffer` is the DEDENTED def→call slice
    (draw_text sees dedented text, like every span editor; the view-slide
    re-adds the indentation). `parse` covers the WHOLE def (always valid
    Python — the def→call cut can end mid-block), so against the buffer it
    is one trailing deletion, which the overlay's parse→buffer line bridge
    maps exactly."""
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
    parse = None
    if pane.store is not None:
        span_line_count = def_last - first + 1
        if (view is not None and view[3] is not None
                and view[1] == bounds and span_line_count > PARSE_LINE_CAP):
            parse = view[3]                 # giant def: keep the last parse
        else:
            parse = _span_parse("\n".join(span_rows))
    pane.view = (whole, bounds, buffer, parse, def_indent)
    return buffer, parse, def_indent


def _crop_folds(span_rows, call_row, max_span_lines):
    """The middle fold for a span over `max_span_lines`, in 0-based BUFFER
    rows ((start, end): row `start` stays visible as the fold header, rows
    start+1..end hide): keep a head below the def and a tail above the call.
    None when the span fits."""
    if span_rows <= max_span_lines:
        return None
    keep = max(2, max_span_lines // 2)
    fold_start = keep - 1
    fold_end = call_row - keep
    if fold_end <= fold_start:
        return None
    return [(fold_start, fold_end)]


def _shift_panes(panes, edited_pane, path, edit_line, delta):
    """After an edit in `edited_pane` changed its file's line count by
    `delta` at 1-based `edit_line`, move every OTHER pane of that file so
    the spans keep addressing the same code."""
    if not delta:
        return
    for pane in panes or ():
        if pane is None or pane is edited_pane or pane.path != path \
                or not pane.resolved:
            continue
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


@render_func(is_default_for=(types.TracebackType, BaseException),
             show_bg=True, use_cache=True, tint=(0.9, 0.35, 0.28))
def draw_stack_trace(input_value: types.TracebackType | BaseException,
                     draw_state=None, trace_state: StackTraceState = None,
                     max_span_lines=80, context_lines=8,
                     project_only=True, max_frames=40):
    """One draw_text per frame, outermost (main) first — each pane is a
    LineRange over the frame's file (project_code — pending truth, editable)
    from the def line to the call into the next frame, with the frame's
    locals drawn over it as live-value markers from a pane-local store.
    Knobs: `max_span_lines` folds the middle of a giant span; `project_only`
    hides stdlib / site-packages frames (turned off automatically when it
    would hide everything); `max_frames` caps very deep / recursive stacks,
    keeping the ends and eliding the middle."""
    from src.lsd.gl_gui.melty import Melty
    # [tint=(0.9, 0.35, 0.28)]
    project_prefix = str(_PROJECT_ROOT)

    # (Re)build the render plan only when the INPUT changes - steady-state
    # frames never re-walk the stack or re-copy locals.
    sig = (project_only, max_frames)
    if trace_state.trace_obj is not input_value or trace_state.trace_sig != sig:
        frames = [f for f in stack_frames(input_value)
                  if f[0] and not f[0].startswith("<")]
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
    editor_font_handle = Melty.font_mgr.get(Font.FONTAWESOME_MONO_19) \
        if Melty.font_mgr is not None else None
    if editor_font_handle is not None:
        imgui.push_font(editor_font_handle)
        char_width = imgui.calc_text_size("0").x
        imgui.pop_font()
    else:
        char_width = imgui.calc_text_size("0").x
    available_width = draw_state.content_width

    parent_call_col = None
    for index, pane in enumerate(panes):
        if pane is None:
            RenderFuncs.draw_text("… frames elided …",
                                  name="stack trace elision", single_line=True,
                                  show_bg=False, editable=False,
                                  syntax_highlight=False,
                                  tint=Tint.subtle_text())
            continue
        if not pane.resolved:
            _resolve_pane(pane, context_lines)
        file_code = project_code[pane.path]
        view = _pane_view(pane, file_code)
        if view is None:
            RenderFuncs.draw_text(f"(unreadable: {pane.path})",
                                  name=f"stack frame unreadable##{index}",
                                  single_line=True, show_bg=False,
                                  editable=False, syntax_highlight=False,
                                  tint=Tint.subtle_text())
            continue
        buffer, parse, def_indent = view
        file_lines = file_code.lines()
        call_row = pane.lineno - pane.first
        shift_columns, parent_call_col = chain_shift(
            def_indent, _indent_of(file_lines[pane.lineno - 1])
            if pane.lineno <= len(file_lines) else 0, parent_call_col)
        slide_columns = def_indent + shift_columns
        span_kwargs = {}
        if slide_columns:
            slide_px = slide_columns * char_width
            cursor_x, cursor_y = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((cursor_x + slide_px, cursor_y))
            # Keep the main view's right edge inside the content rect.
            if available_width > slide_px + 200:
                span_kwargs["width"] = available_width - slide_px
        if parse is not None:
            span_kwargs["code_dict"] = parse
            span_kwargs["live_store"] = pane.store
            bounds = pane.bounds()
            if pane.address is None or pane.address[0] != bounds:
                pane.address = (bounds,
                                Address(pane.path, pane.first - 1, pane.last))
            span_kwargs["jump_to"] = pane.address[1]
        folds = _crop_folds(pane.last - pane.first + 1, call_row,
                            max_span_lines)
        if folds is not None:
            span_kwargs["diff_fold_ranges"] = folds
            span_kwargs["expand_diff"] = pane.expand_diff

        edited, new_text, pane_ds = RenderFuncs.draw_text(
            buffer, name=f"stack frame {pane.qualname}##{index}",
            file_key=pane.path,
            line_numbers=list(range(pane.first, pane.last + 1)),
            show_header=False, show_file_header=False, show_jump_bar=False,
            gutter_indent=True, shadow=False, bg_offset=-1,
            use_cache=True, return_extras=True, **span_kwargs)
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
                         pane.first + edit_row, delta)
    return False, input_value
