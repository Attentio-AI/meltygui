"""Trace view functions and supporting definitions."""
from meltygui.core.melty import Melty
from meltygui.model.trace_model import ALL_APPS
from meltygui.model.trace_model import CrashReportStore
from meltygui.model.trace_model import SavedTrace
from meltygui.core.core_render import render_func
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.state.trace_state import CrashReportsPanelState
from meltygui.state.trace_state import StackTraceState
from meltygui.core.runtime.toggles import Tint
from meltygui.core.runtime.toggles import Toggles
import meltygui_imgui as imgui
import os
import time
import types
from meltygui.hdr_color import pack_color
from pathlib import Path
import colorsys


@render_func(is_default_for=(types.TracebackType, BaseException, SavedTrace),use_cache=True, tint=(0.9, 0.35, 0.28))
def draw_stack_trace(input_value: types.TracebackType | BaseException | SavedTrace,
                     draw_state=None, trace_state: StackTraceState = None,
                     max_span_lines=80, context_lines=8, freeze_resize=True,
                     project_only=True, max_frames=40, indent_views=True,
                     hide_dispatch=False, placeholder_lines=None, file_headers=False,
                     file_header_indent=18, cull_offscreen=True, crumb_headers=False,
                     crumb_height=24.0):
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
    `file_header_indent` px. `crumb_headers=True` (the context menu's Code
    tab) puts the file browser's crumb strip (`draw_breadcrumbs`,
    `crumb_height` tall: every path segment a dropdown over its directory,
    the crumbs in their painted file-meta tints) above each pane instead;
    a picked file opens in the code editor. `cull_offscreen=False` turns the viewport
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
    from meltygui.code.fileref import Address
    from meltygui.code.fileref import _PROJECT_ROOT
    from meltygui.code.project_code import project_code
    from meltygui.core.styling.fonts import Font
    from meltygui.core.diagnostics.trace_core import _Pane
    from meltygui.core.diagnostics.trace_core import _RaisingLine
    from meltygui.core.diagnostics.trace_core import _crop_folds
    from meltygui.core.diagnostics.trace_core import _ensure_store
    from meltygui.core.diagnostics.trace_core import _indent_of
    from meltygui.core.diagnostics.trace_core import _pane_parse
    from meltygui.core.diagnostics.trace_core import _pane_view
    from meltygui.core.diagnostics.trace_core import _reindent_rows
    from meltygui.core.diagnostics.trace_core import _resolve_pane
    from meltygui.core.diagnostics.trace_core import _shift_panes
    from meltygui.core.diagnostics.trace_core import chain_shift
    from meltygui.core.diagnostics.trace_core import stack_frames

    from meltygui.core.melty import Melty
    # [tint=(0.9, 0.35, 0.28)]
    project_prefix = str(_PROJECT_ROOT)
    # File header line: `File "path", line N, in func` - the path in the
    # file's painted tint, the rest in the subtle-text colour.
    header_height = imgui.get_text_line_height() + Melty.px(6) if file_headers else 0.0
    if crumb_headers:
        # The crumb strip above each pane: its own height, no card / inset.
        header_height = Melty.px(crumb_height)
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
            from meltygui.code.chain_converters import _is_dispatch_frame
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
                from meltygui.code.new_converters import LOADING
                from meltygui.code.new_converters import UNSET
                from meltygui.code.new_converters import run_in_background
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
        elif crumb_headers:
            # The pane's file as the crumb strip, drawn at the cursor into
            # the trace's tile (a plain function; one strip per pane index).
            from meltygui.view.file_view import draw_breadcrumbs
            imgui.set_cursor_screen_pos((cursor_x, cursor_y))
            picked, target = draw_breadcrumbs(
                pane.path, draw_state, width=available_width,
                crumb_height=crumb_height, name=f"stack crumbs {index}")
            if picked and not Path(target).is_dir():
                from meltygui.core.runtime.extensions import open_source as open_in_editor
                open_in_editor(target)
            cursor_y += header_height
            imgui.set_cursor_screen_pos((cursor_x, cursor_y))
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


@render_func(use_cache=True, selectable=False, show_add_delete=False,
             is_tree=False, show_name=True, shadow=True, bg_offset=-2,
             is_default_for="CrashReportStore", tint=(0.86, 0.24, 0.2))
def draw_crash_reports(
        # [tint=(0.85, 0.75, 0.05)]
        input_value: CrashReportStore,
        draw_state, panel_state: CrashReportsPanelState = None, style_manager=None,
        left_mouse_down=False, **kwargs):
    from meltygui.editor.source_ui import _file_meta_tint
    from meltygui.editor.source_ui import _tab_text_color
    from meltygui.editor.text_editor import COLORS
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.view.header_view import flat_button
    from meltygui.core.cache.tile_marks import add_shadow
    from meltygui.core.layout.header_runtime import _brightness_clamp_fn
    from meltygui.model.trace_report_model import _color_u32
    from meltygui.model.trace_report_model import _ellipsize
    from meltygui.model.trace_report_model import _mix
    from meltygui.model.trace_report_model import date_bucket

    store = input_value
    store.refresh_if_stale()
    open_rows = panel_state.open if panel_state is not None else {}

    # ---- styling (fast_dock colours) ----
    # Rows take the editor tabs' colour knobs (toggles/EditorColor_*);
    # these mix the section headers and the toolbar text.
    factor = 0.90
    hover_bg_boost = 0.05
    # The row / card background is the tab colour scaled by this: a list of fifty
    # saturated error tints glares where a strip of tabs does not. 1.0 = the
    # tabs' own brightness; lower = darker rows.
    row_bg_dim = 0.5
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
    app_filter_width = px(190.0)                      # the toolbar's app ID dropdown
    trace_gap = px(4.0)                               # gap between a row and its stack trace view
    card_pad_bottom = px(6.0)                         # the row's card runs this far past its trace
    footer_height = px(22.0)                          # thread · commit line under an expanded trace
    # [tint=(0.35, 0.85, 0.94)]
    chevron_inset = px(10.0)                          # chevron x inside the row
    text_inset = px(28.0)                             # error text x inset (after the chevron)
    # The right-aligned "5:02PM + 20s · thread · commit" column takes what it
    # needs (a thread name is never clipped for a fixed width); the ERROR
    # text is what gives way, down to this much room.
    error_min_width = px(90.0)
    func_pad_x = px(6.0)                             # the function pill's inset
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

    # ---- toolbar: count + directory, the app filter, Clear all ----
    # Every Melty process saves into this one folder; the list is all of
    # them newest first, narrowed to one app ID by the dropdown.
    toolbar_top = origin_y
    app_choices = [ALL_APPS, *store.apps()]
    shown_app = panel_state.app if panel_state is not None else ALL_APPS
    if shown_app not in app_choices:
        shown_app = ALL_APPS                           # that app's last report was deleted
    entries = store.shown(shown_app)
    # flat_button, placed by the imgui cursor with layout=True so the click
    # is claimed through this view's on_action rect (layout=False is
    # draw-only - no subscription at all). event="left_mouse_down": the
    # body's own view-wide left_mouse_down param would otherwise take the
    # click; the button's registration sits 4 above it.
    clear_left, clear_pressed = row_right, False
    if entries:
        clear_label = f"{trash_icon} Clear {'all' if shown_app == ALL_APPS else shown_app}"
        clear_width = imgui.calc_text_size(clear_label)[0] + 2 * button_pad_x
        clear_left = row_right - clear_width
        imgui.set_cursor_screen_pos((clear_left, toolbar_top + (toolbar_height - button_height) / 2.0))
        clear_pressed = bool(flat_button(
            clear_label, draw_state, "crash_clear_all", width=clear_width, height=button_height,
            event="left_mouse_down", color=delete_color, tint_value=0.45, max_bg_brightness=0.6,
            text_color=(1.0, 0.80, 0.78, 1.0), corner_radius=corner))
    if clear_pressed:
        store.remove_all(shown_app)
        entries = []
        changed = True
    filter_left = clear_left - px(8) - app_filter_width
    imgui.set_cursor_screen_pos((filter_left, toolbar_top + (toolbar_height - button_height) / 2.0))
    # display_label: the state's app is the selection of record (the
    # dropdown's own remembered label lags a change made elsewhere).
    app_picked, picked_app = RenderFuncs.draw_dropdown(
        shown_app, collection=app_choices, name="crash_app_filter", display_label=shown_app,
        show_header=False, width=app_filter_width, trigger_height=button_height,
        shadow=False, tint=tint, z_offset=2)
    if app_picked and picked_app != shown_app and panel_state is not None:
        panel_state.app = picked_app                   # @live setattr: repaints the tile
        changed = True
        request_render()
    count_note = (f"{len(entries)} report{'s' if len(entries) != 1 else ''}   ·   "
                  f"{store.directory()}")
    if store.error:
        count_note += f"   ·   {store.error}"
    draw_list.add_text(row_left, toolbar_top + (toolbar_height - line_height) / 2.0,
                       _color_u32(meta_color, 0.8), _ellipsize(count_note, filter_left - px(10) - row_left))

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
        # Trash button lives on the row's right while the pointer is on the
        # row (the cached tile re-renders while hovered, like the fast
        # toggle's summon button). Its LEFT edge is reserved here so the meta
        # text stops short of it; the button itself paints on the row's
        # background below - drawn first, the row rect masks it.
        trash_left = row_right
        trash_pressed = False
        trash_width = imgui.calc_text_size(trash_icon)[0] + 2 * button_pad_x
        if row_hovered:
            trash_left = row_right - px(4) - trash_width - px(6)

        # The row wears the colour of the file that RAISED (the trace's last
        # frame; a painted FileMeta tint - the editor tabs' source), and
        # names that file after the error. An unpainted file keeps the
        # window's tint.
        saved_trace = store.trace(path)
        raising_frame = saved_trace.frames[-1] if saved_trace.frames else None
        file_tint = _file_meta_tint(raising_frame[0]) if raising_frame else None
        row_tint = file_tint or tint
        file_label = ""
        if raising_frame:
            file_label = f"{os.path.basename(raising_frame[0])}:{raising_frame[1]}"

        # An expanded row's card WRAPS its whole trace: the rect runs from
        # the row's top past the trace's bottom (the trace's height as last
        # measured - its first frame draws a row-sized card and the
        # re-measure repaints). The trace draws over it. The card is
        # visibility-tested on ITS OWN rect, not the row's: with the row
        # scrolled off the top the card must still paint behind the trace.
        card_bottom = row_bottom
        if is_open and saved_trace.seen_height is not None:
            card_bottom = (row_bottom + trace_gap + saved_trace.seen_height
                           + footer_height + card_pad_bottom)
        if visible(row_top, card_bottom):
            # The editor tabs' colour pipeline (open_files' tab flat_button):
            # make_color_rgb at factor 0.1 - the raw file tint with a sliver
            # of theme - at the tab_*_bg knobs, brightness-clamped, and the
            # tabs' text colour. Expanded = active tab, collapsed = inactive.
            # tabs' text colour (below). Collapsed or expanded, the same styling.
            bg = style_manager.make_color_rgb(
                row_tint[0], row_tint[1], row_tint[2],
                value=Toggles.CodeEditor.tab_active_bg_brightness * row_bg_dim
                + (hover_bg_boost if row_hovered else 0.0),
                factor=0.1, saturation_scale=Toggles.CodeEditor.tab_active_bg_saturation, alpha=1.0)
            bg = _brightness_clamp_fn()(bg[0], bg[1], bg[2], 0.0,
                                        Toggles.CodeEditor.tab_active_bg_max_brightness * row_bg_dim
                                        + (hover_bg_boost if row_hovered else 0.0))
            add_shadow((row_left, row_top, row_right - row_left, card_bottom - row_top),
                       offset=11, corner_radius=corner, clip=clip)
            # One channel DOWN for the card: the trace's panes render on this
            # body's channel, so a card on this paints over their text
            # (columns.py's cell-bg pattern).
            if Melty.channels_split:
                draw_list.channels_set_current(max(0, Melty.get_channel() - 1))
            # Expanded, only the top corners round - the file cards inside
            # are square and flush, when poked out of a rounded bottom.
            draw_list.add_rect_filled(row_left, row_top, row_right, card_bottom,
                                      _color_u32(bg), rounding=corner,
                                      flags=(imgui.DRAW_ROUND_CORNERS_TOP if is_open
                                             else imgui.DRAW_ROUND_CORNERS_ALL))
            if Melty.channels_split:
                draw_list.channels_set_current(Melty.get_channel())
        if visible(row_top, row_bottom):
            fg = _tab_text_color(row_tint, Toggles.CodeEditor.tab_active_text_brightness,
                                 Toggles.CodeEditor.tab_active_text_saturation,
                                 Toggles.CodeEditor.tab_active_text_min_brightness)
            text_y = row_top + (row_height - line_height) / 2.0 + text_nudge_y
            draw_list.add_text(row_left + chevron_inset, text_y, _color_u32(fg),
                               open_icon if is_open else closed_icon)
            # ── right: time of day (the section header carries the date),
            # thread, commit - right-aligned, never clipped by the thread ──
            when = time.localtime(entry["mtime"])
            meta = (f"{entry['app']}  ·  {when.tm_hour % 12 or 12}:{when.tm_min:02d}"
                    f"{'AM' if when.tm_hour < 12 else 'PM'} + {when.tm_sec}s")
            meta_right = trash_left - px(6)
            # ── left: the raising FILE first, then its FUNCTION on a pill in
            # the function's own definition tint (or roster's, where the def
            # carries one), then the error ──
            x = row_left + text_inset
            func_rect = None
            if file_label:
                draw_list.add_text(x, text_y, _color_u32(fg), file_label)
                x += imgui.calc_text_size(file_label)[0] + px(10)
            func_name = (raising_frame[2] or "") if raising_frame else ""
            if func_name and func_name != "<module>":
                def_w = imgui.calc_text_size("def ")[0]
                pill_w = def_w + imgui.calc_text_size(func_name)[0] + 2 * func_pad_x
                # The editor's own syntax colours: `def` in the keyword
                # orange, the name in the def-name blue (code_editor.COLORS).
                draw_list.add_text(x + func_pad_x, text_y, COLORS["def"], "def ")
                draw_list.add_text(x + func_pad_x + def_w, text_y, COLORS["def_name"], func_name)
                func_rect = (x, row_top, x + pill_w, row_bottom)
                x += pill_w + px(10)
            # Ctrl+B on the row jumps to the code editor at the raising
            # file's line; on the function name it also lands the caret on
            # the name (its rect registers one level above the row's) -
            # the same targets as the trace's file headers.
            if raising_frame:
                fired = None
                if func_rect is not None and draw_state.on_action(
                        "ctrl_b_down", view_id=f"crash_jump_func_{entry['name']}",
                        rect=func_rect, priority_delta=5) is not None:
                    fired = "func"
                elif draw_state.on_action("ctrl_b_down", view_id=f"crash_jump_file_{entry['name']}",
                                          rect=(row_left, row_top, row_right, row_bottom),
                                          priority_delta=4) is not None:
                    fired = "file"
                if fired:
                    from meltygui.core.runtime.extensions import open_source as open_in_editor
                    open_in_editor(raising_frame[0], raising_frame[1],
                                   token=(func_name.rsplit(".", 1)[-1] if fired == "func" else None))
            # the meta takes what it needs, the error gets the rest (floored)
            meta_room = max(px(40), meta_right - x - error_min_width - px(12))
            meta_fit = _ellipsize(meta, meta_room)
            meta_size = imgui.calc_text_size(meta_fit)
            draw_list.add_text(meta_right - meta_size[0], text_y, _color_u32(meta_color, 0.9), meta_fit)
            error_fit = _ellipsize(entry["error"] or entry["name"], meta_right - meta_size[0] - px(12) - x)
            if error_fit:
                draw_list.add_text(x, text_y, _color_u32(error_color), error_fit)

        if row_hovered:
            imgui.set_cursor_screen_pos((row_right - px(4) - trash_width,
                                         row_top + (row_height - button_height) / 2.0))
            trash_pressed = bool(flat_button(
                trash_icon, draw_state, f"crash_delete_{entry['name']}",
                width=trash_width, height=button_height, event="left_mouse_down",
                color=delete_color, tint_value=0.45, max_bg_brightness=0.6,
                text_color=(1.0, 0.80, 0.78, 1.0), corner_radius=corner))

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
            indent_views=False, file_headers=True, cull_offscreen=False, return_extras=True,
            width=row_right - row_left, corner_radius=0,
            # The row's card is the background and the shadow caster; the
            # trace's own bg / shadow mark are rounded (radius 5) and showed
            # as a corner rect where its square file headers poked over them.
            show_bg=False, shadow=False)
        # MANUAL height: the nested view's draw_state.height, not the cursor
        # it left behind - the wrapper advances the cursor by the live
        # layout on the first run and by the tile on a cache hit, and the two
        # differ by a few px, so a cursor-read total jittered every time a
        # pane re-rendered under the pointer. `height` is stamped from the
        # measured content and only moves when the content really does.
        trace_height = (trace_ds.height if trace_ds is not None and trace_ds.height
                        else imgui.get_cursor_screen_pos()[1] - trace_top)
        # ... and the wrapper's `height` is the whole imgui GROUP in which a
        # pane lying above the viewport stretches to the clip edge (see
        # draw_stack_trace's advance fix); `observed_content_height` is
        # the honest cursor delta of the trace body. Prefer it whenever
        # the group overshoots it by more than a row.
        honest = getattr(trace_ds, "observed_content_height", 0) if trace_ds is not None else 0
        if honest > 0 and trace_height > honest + line_height:
            trace_height = (saved_trace.seen_height if saved_trace.seen_height is not None
                            else float(honest))
        else:
            if saved_trace.seen_height != trace_height:
                request_render()                       # the card bg was sized off the old value
                changed = True
            saved_trace.seen_height = trace_height
        # ---- footer: thread - commit, right-aligned inside the card ----
        footer_top = trace_top + trace_height
        footer = entry["thread"] or ""
        if entry["pid"]:
            footer += f"{'  ·  ' if footer else ''}pid {entry['pid']}"
        if entry["commit"]:
            sha, _, branch = entry["commit"].partition(" ")
            footer += f"{'  ·  ' if footer else ''}{sha[:8]}{(' ' + branch) if branch else ''}"
        if footer and visible(footer_top, footer_top + footer_height):
            footer_fit = _ellipsize(footer, row_right - row_left - 2 * px(8))
            footer_size = imgui.calc_text_size(footer_fit)
            draw_list.add_text(row_right - px(8) - footer_size[0],
                               footer_top + (footer_height - line_height) / 2.0 + text_nudge_y,
                               _color_u32(meta_color, 0.9), footer_fit)
        # The card runs card_pad_bottom past the footer; the next row follows it.
        row_top = footer_top + footer_height + card_pad_bottom + row_gap
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
    from meltygui.code.fileref import _PROJECT_ROOT

    from meltygui.core.melty import Melty
    from meltygui.models.file_meta import FileMeta
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.editor.source_ui import _file_meta_tint
    from meltygui.editor.source_ui import _tab_text_color
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
        from meltygui.core.runtime.extensions import open_source as open_in_editor
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
