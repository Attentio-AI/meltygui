"""Code view functions and supporting definitions."""
from inspect import Parameter
from meltygui.code.fileref import Address
from meltygui.code.libcst_conversion import Comment
from meltygui.code.libcst_conversion import SymbolUsage
from meltygui.code.libcst_conversion import UsageRef
from meltygui.core.styling.fonts import Font
from meltygui.hdr_color import pack_color
from meltygui.core.melty import Melty
from meltygui.model.code_model import UsagePickerModel
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.state.code_state import SourcePreviewState
from meltygui.state.new_core_model import Anchor
from meltygui.state.new_core_model import DrawState
from meltygui.state.new_core_model import ExpandMode
from meltygui.state.new_core_model import Pin
from meltygui.state.new_core_model import TabState
from meltygui.core.runtime.toggles import Toggles
from meltygui.view.header_view import draw_footer
from meltygui.view.header_view import draw_header
from meltygui_imgui.core import _DrawList
from pathlib import Path
import bisect
import collections
import inspect
import meltygui_imgui as imgui
import sys
import threading
import time
import types
import weakref


@render_func(use_cache=True, selectable=False)
def run_button(input_value: any, with_kwargs=None, draw_state=None, clicked=False):
    from meltygui.core.conversion.path_finder import Pending

    is_render_func = hasattr(input_value, "__render_func__")
    if not is_render_func:
        imgui.text_colored(f"Value of type {type(input_value).__name__} needs @render_func",
                           1.0, 0.5, 0.0)
        return False, None
    if with_kwargs is None:
        with_kwargs = {}

    if hasattr(input_value, "__header_defaults__"):
        run_in_background = input_value.__header_defaults__.get("background", False)
    else:
        run_in_background = True

    running = draw_state._running is input_value if run_in_background else False

    fa_run_arrow = ""
    from meltygui.view.control_view import button
    if clicked or running or button(f"{fa_run_arrow} {input_value.__name__}##{draw_state.unique}",
                                    height=30, draw=True, value=0.4, saturation=1.5,
                                    name=f"{input_value.__name__}{draw_state.unique}_run")[0]:
        with_kwargs['changed'] = True
        changed, value = input_value(**with_kwargs)
        if isinstance(value, Pending):
            draw_state._running = input_value
            return False, None

        draw_state._running = False
        return True, (changed, value)

    return False, None


@render_func(use_cache=True, show_bg=False, selectable=False, disable_scroll=True,
             shadow=False, indent_size=0, with_footer=None, fill_height=False)
def draw_with_view_funcs(input_value, view_funcs, route, routed, route_to_kwargs,
                         tab_state: TabState, unique, column_widths=None,
                         draw=False, draw_state=None, **kwargs):
    """Tab strip + columns half of the old draw_modes.

    Shows a tab per entry in `view_funcs` and renders the selected ones side by
    side. The shared `routed` payload (built by convert_in_and_out) decides each
    column's input: a view with a `route` entry is fed routed[route[view]] (e.g.
    draw_collection <- the parsed dict); a view with no entry edits the raw text
    (draw_text <- input_value). Every routed value also rides in as a kwarg, so a
    view picks up the extras it wants (draw_text <- jump_to / error / code_tree)
    WITHOUT convert_in_and_out hand-threading them — that's the routing.

    Two edit channels flow back to convert_in_and_out:
      * a RAW text edit is returned directly as (changed, value);
      * a CONVERTED edit (a routed/structured view) is stashed on the shared
        `routed` dict under 'converted_edit', because it must go back through
        chain_out before it becomes text. It's written AFTER the loop so the
        columns in this same frame never see it as an input kwarg."""
    from meltygui.code.new_converters import UNSET

    # Drop entries that didn't survive (de)serialization, then default to the
    # first two views (text | structured) like the old draw_modes did.
    tab_state.selected_tabs = [t for t in tab_state.selected_tabs if t is not None]
    if not tab_state.selected_tabs:
        tab_state.selected_tabs = view_funcs[:2] or view_funcs[:1]

    imgui.dummy(0, 5)
    names = [getattr(vf, '__name__', str(vf)) for vf in view_funcs]
    tab_changed, new_tabs = RenderFuncs.draw_tab_bar(input_value=tab_state.selected_tabs,
                                                     tab_height=30, show_bg=False, bg_offset=1,
                                                     name=f"tab_bar{unique}", names=names,
                                                     collection=view_funcs, as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    imgui.dummy(0, 2)
    raw_changed, raw_value = False, input_value
    converted_edit = UNSET
    for idx, view_func in enumerate(tab_state.selected_tabs):
        if column_widths is not None and len(column_widths) > idx:
            column_width = column_widths[idx]
        else:
            column_width = None

        # A view with a route entry consumes a routed (converted) value; one
        # without edits the raw text. Falls back to the last-good routed value
        # (already merged into `routed`), or UNSET if it never parsed.
        uses_converted = route is not None and view_func in route
        if uses_converted:
            view_input = routed.get(route[view_func], UNSET)
        else:
            view_input = input_value

        # route_to_kwargs_this = route_to_kwargs.get(view_func, {})
        # for k in route_to_kwargs_this:
        #     arg_name = route_to_kwargs_this[k]
        #     if arg_name in routed:
        #         routed[k] = routed[arg_name]

        # routed = {**routed, **{k: routed.get(k) for k in route_to_kwargs_this}}

        # `draw=` (the external trigger flag) bypasses the view's cache for one
        # redraw WITHOUT setting any sticky edit flags - never pass it as
        # `changed=`, or a forced redraw comes back reported as an edit and, with
        # auto_save, spins an endless reload -> redraw -> save.
        if len(tab_state.selected_tabs) == 1:
            column = None
        m_changed, m_out = view_func(input_value=view_input, excluded=["__cst__", "__origin__"],
                                     show_system=False, draw=draw, max_width=draw_state.content_width - 10,
                                     disable_scroll=False, show_header=False,
                                     column=idx, column_width=column_width,
                                     show_add_delete=False, name=f"{view_func.__name__}##{unique}",
                                     selectable=False, **routed)
        if not m_changed:
            continue
        if draw_state is not None:
            draw_state.invalidate_up(max_depth=3)
        if uses_converted:
            # Keep only the latest converted edit so a continuous drag collapses
            # into one chain_out call when it settles.
            converted_edit = m_out
        else:
            raw_changed, raw_value = True, m_out

    if converted_edit is not UNSET:
        routed['converted_edit'] = converted_edit
    return raw_changed, raw_value


def draw_text_from_code_cache(input_value=None, root_input=None, error=None,
                              run_jedi=False, **kwargs):
    """FILE_TREE's editor view: plain draw_text fed from the shared code-host
    cache instead of an inline chain. code_hosts_for(path) owns the parse —
    str_host watches the file, dict_host re-parses on change — and we pull its
    held cst_dict each frame, so usage links and syntax-error highlighting
    arrive as code_dict / code_tree exactly as in the NEW_CODE routes. Pull-
    based by design: an edit here auto-saves to disk, the cache's str_host
    reloads from the file, and the editor picks up the fresh parse a beat
    later — the leaf and the cache only ever talk through the file."""
    from meltygui.code.new_converters import ModesState
    from meltygui.code.new_converters import _ensure_symbol_index
    from meltygui.code.new_converters import _error_markers
    from meltygui.code.new_converters import _host_label
    from meltygui.code.new_converters import _host_relint_and_fixes
    from meltygui.code.new_converters import code_hosts_for
    from meltygui.core.diagnostics.perf_trace import once as _ponce
    from meltygui.core.diagnostics.perf_trace import trace as _ptrace
    from meltygui.core.diagnostics.perf_trace import trace_rl as _ptrace_rl
    from meltygui.core.windowing.glfw_utils import request_render

    code_dict, cache_error, dict_host = None, None, None
    # Scope-up auto-select (contextual func tab): a file-ABSOLUTE line whose
    # statement should be selected in this editor - consumed here (popped so it
    # never leaks into draw_text as a stray kwarg) and applied one-shot below.
    # select_seq is the arrow-press generation: it keys the one-shot, so every
    # click re-selects even when the same line repeats in a persisted ds.
    select_line = kwargs.pop("select_line", None)
    select_seq = kwargs.pop("select_seq", 0)
    _t_editor0 = time.monotonic()
    if root_input is not None:
        _str_host, dict_host = code_hosts_for(root_input)
        code_dict = dict_host._held()
        # First-parse arrival transition (once per host): the gap between
        # "waiting" and "visible" is what the user sees as load time.
        if isinstance(code_dict, dict):
            if _ponce(("parse-visible", _host_label(dict_host))):
                _ptrace("editor: first parse visible", host=_host_label(dict_host))
        elif _ponce(("parse-wait", _host_label(dict_host))):
            _ptrace("editor: waiting for first parse", host=_host_label(dict_host))
        # Background auto-index: keep the held parse's symbol usages current
        # without the manual Index click (no-op when already indexed).

        _ensure_symbol_index(dict_host, _str_host, code_dict, kwargs.get("jump_to"))
        # The chain's parse error lives on the wrapper's injected ModesState.
        # Normalize it to the ParseError-dict shape _code_tree_errors reads
        # ({'__error__','__line__'}) and pass it as code_tree: the raw exception
        # could be a cst.ParserSyntaxError, whose line lives in `raw_line` - a
        # name neither _code_tree_errors nor _exception_handler knows. code_dict
        # keeps the last GOOD parse alongside, so the editor highlights the
        # offending line without losing its structure.
        wds = getattr(dict_host, "_wrapper_draw_state", None)
        import_fixes = _host_relint_and_fixes(dict_host, _str_host, wds)
        for v in (getattr(wds, "misc", None) or {}).values():
            if isinstance(v, ModesState):
                err = v.last_error
                lint = getattr(v, "last_lint", None) or None
                if err is not None or lint:
                    # ONE dict per underlying (exception, lint) pair, not one
                    # per frame: draw_text's parse-error staleness check
                    # compares code_tree by IDENTITY to detect "a fresh parse
                    # landed", so a dict rebuilt every frame would re-hide a
                    # stale highlight one frame after an edit, pinned to the
                    # old line. The memo keeps identity stable until the
                    # background reparse actually replaces last_error /
                    # last_lint (both swapped per completed parse, never
                    # mutated in place).
                    memo = getattr(dict_host, "_err_view_memo", None)
                    if memo is not None and memo[0] is err and memo[1] is lint:
                        cache_error = memo[2]
                    else:
                        # The parse/compile error, then the lint findings -
                        # all in __errors__, with the first mirrored into the
                        # single __error__/__line__ keys the editors use.
                        markers = _error_markers(err, lint)
                        cache_error = {"__error__": markers[0][1], "__line__": markers[0][0],
                                       "__errors__": markers}
                        dict_host._err_view_memo = (err, lint, cache_error)
        # The Index button's pulse rides to the cache's chain_in (one-shot:
        # cleared again on the next un-pulsed frame). cst_module_to_dict only
        # runs jedi when it ALSO has the resolved address (jump_to) - the leaf's
        # code_file_io hands us the whole-file Address, so forward it alongside.
        # _pending_external makes the host re-run chain_in (its start gate is
        # external_change); invalidating alone would just replay the blit.
        if run_jedi:
            dict_host.child_kwargs["run_jedi"] = True
            if kwargs.get("jump_to") is not None:
                dict_host.child_kwargs["jump_to"] = kwargs["jump_to"]
            dict_host._pending_external = True
            for hds in (wds, getattr(dict_host, "_draw_state", None)):
                if hds is not None:
                    hds.invalidate()
            request_render()
        elif (dict_host.child_kwargs.get("run_jedi")
              and not dict_host._pending_external):
            # One-shot clear - but only after the host actually consumed the
            # pulse (_pending_external drops when its chain_in is handed the
            # kwargs). Popping earlier loses a click whenever this editor
            # re-renders between the pulse frame and the host's next draw.
            dict_host.child_kwargs.pop("run_jedi", None)

        held_values = list(_str_host.values())
        from_host = len(held_values) > 0
        # Short-circuit the first-parse wait: the code_dict pair materializes
        # through the full cst→dict chain, and until it lands this editor
        # rendered NOTHING - that gap IS the perceived load time of a code
        # buffer ("editor: waiting for first parse" above). The codec-loaded
        # buffer (input_value) is available the frame load_file lands, so draw
        # it immediately: the colors come from the tokenizer and def-hints
        # from the text mode roster, both text-based; code_dict extras
        # (usage links) join when the parse arrives and dict_host's
        # notify_on_change repaints this editor. Edits during this brief window
        # are discarded - the host isn't there to receive them, the same
        # contract as a read-only code-diff tab.
        buffer_text = (held_values[0] if from_host
                       else input_value if isinstance(input_value, str) else None)
        if buffer_text is not None:
            _t_dt0 = time.monotonic()
            changed, value, ds = RenderFuncs.draw_text(buffer_text, code_dict=code_dict,
                                                       code_tree=cache_error, error=error,
                                                       import_fixes=import_fixes,
                                                       return_extras=True,
                                                       **{"gutter_indent": True, **kwargs, "is_tree": False})
            _t_dt1 = time.monotonic()
            # Every frame's editor draws: mark as a LIVE user so the idle sweep
            # keeps the host registered (and repaint it when a background parse
            # lands). Not gated on `changed` - an open-but-unedited editor still
            # owns the host, and the sweep would otherwise immediately-register it.
            dict_host.notify_on_change(ds)
            # Apply a pending start-up auto-select: map the file-absolute line
            # into this span buffer via jump_to.start (the 0-based file line of
            # buffer line #0) and select that line's code - same shape as the
            # mouse-click line-select and draw_code_editor's jump_to_line
            # consumption (caret placement + focus grant; the editor's own
            # cursor-follow scroll brings it into view on the next body frame).
            # One-shot per (arrow press, line) - stamped on ds.misc so re-renders
            # must not keep clamping the selection while the user edits the
            # editor, but a new press (select_seq bump) re-applies it.
            if (select_line is not None and ds is not None
                    and isinstance(buffer_text, str)
                    and ds.misc.get("_applied_select_line") != (select_seq, select_line)):
                ds.misc["_applied_select_line"] = (select_seq, select_line)
                _start0 = getattr(kwargs.get("jump_to"), "start", 0) or 0
                _lines = buffer_text.split("\n")
                _li = max(0, min(int(select_line) - 1 - _start0, len(_lines) - 1))
                _line_start = sum(len(l) + 1 for l in _lines[:_li])
                _indent = len(_lines[_li]) - len(_lines[_li].lstrip())
                _sel_len = len(_lines[_li]) - _indent
                # Fold projection: these are FULL-buffer coords; the editor
                # lays out fold-spliced display text. Expands any collapsed
                # fold at the line, then shifts the selection start - the
                # end rides the same line, so it shifts by the same delta.
                from meltygui.editor.text_editor import fold_project_jump
                _sel_s, _ = fold_project_jump(
                    ds, buffer_text, _line_start + _indent, _li)
                ds.text_selection_start = _sel_s
                ds.text_selection_end = _sel_s + _sel_len
                ds.text_cursor_pos = ds.text_selection_end
                Melty.text_focused_ds = ds
                Melty._text_focus_grant_frame = Melty.frame_count
                ds.invalidate()
                request_render()
            if changed and from_host:
                _key0 = list(_str_host.keys())[0]
                _held0 = _str_host[_key0]
                if _held0 == value:
                    # Echo breaker: a changed=True with byte-identical text must
                    # not dirty the str host - that write is what feeds the
                    # startup parse storm (dirty → chain_out → queue_save →
                    # pending_gen bump → full reparse + usage recompute, per
                    # frame, for a no-op). Python == short-circuits by length
                    # and first differing char, so real edits pay ~nothing;
                    # the full-length compare only matters in the spurious
                    # case, where it replaces a multi-frame pipeline.
                    _ptrace("editor changed with IDENTICAL text — host write suppressed",
                            host=_host_label(dict_host))
                else:
                    _t_w0 = time.monotonic()
                    _str_host[_key0] = value
                    _t_w1 = time.monotonic()
                    # TEMP perf: split the editor frame's tail - the draw_text
                    # CALL (body + render_func wrapper epilogue; body time is
                    # the top "draw_text took" line) vs the host WRITE
                    # (install_bubbling + notify_on_changed + invalidations).
                    if (_t_w1 - _t_dt0) * 1000.0 >= 30.0:
                        _ptrace("editor tail split",
                                call_ms=round((_t_dt1 - _t_dt0) * 1000.0, 1),
                                write_ms=round((_t_w1 - _t_w0) * 1000.0, 1))
    # # Re-render this editor when a background parse lands: its cached
    # # subtree is outside the host's own draw loop, so without registering it
    # # the fresh cst_dict sits invisible until an unrelated invalidation.
    # if dict_host is not None and ds is not None:
    #     dict_host.notify_on_change(ds)
    _dt_editor = (time.monotonic() - _t_editor0) * 1000.0
    if _dt_editor >= 20.0 and dict_host is not None:
        # Render-thread stall inside the editor's loop (draw_text + pulls).
        _ptrace_rl(("editor-slow", id(dict_host)),
                   f"editor frame took {_dt_editor:.0f}ms",
                   host=_host_label(dict_host))
    return False, None


@render_func(use_cache=True, show_bg=False, selectable=False, disable_scroll=True,
             shadow=False, indent_size=0, with_footer=None, bg_offset=0)
def draw_code_tabs_from_cache(input_value=None, root_input=None, tab_state: TabState = None,
                              unique=None, draw_state: DrawState = None, column_widths=None,
                              column_edges=None, draw=False, error=None,
                              run_jedi=False, **kwargs):
    """NEW_CODE's text|structured tabs on the code-host-cache route — no inline
    chain_in/chain_out (the legacy convert_in_and_out path this replaces).

      • draw_text — delegates to draw_text_from_code_cache; a text edit returns
        to the enclosing code_file_io, which saves the span (its normal path).
      • draw_collection — renders the shared dict_host's HELD GeneralParse
        (code_hosts_for). An edit mutates the bubbling-wrapped parse in place,
        marking the host dirty; the host chain_outs to source and saves through
        the cache's own str_host, so nothing returns upward from this column.
        An edit ALSO drives the live source immediately (live_apply_edits:
        class vars, function param defaults + constant locals, module
        globals) — the same responsive preview the chain route's
        general_parse_to_address gives, ahead of any recompile.

    The two code_file_io instances (this window's and the cache's str_host)
    only ever talk through the FILE: a dict edit saves via the cache and this
    window's auto_load_edits picks it up; a text edit saves here and the
    cache's file watch re-parses."""
    from meltygui.code.chain_converters import live_apply_edits
    from meltygui.code.new_converters import _ensure_symbol_index
    from meltygui.code.new_converters import _host_code_tree_error
    from meltygui.code.new_converters import _host_label
    from meltygui.code.new_converters import _host_relint_and_fixes
    from meltygui.code.new_converters import code_hosts_for
    from meltygui.core.diagnostics.notifications import notify
    from meltygui.core.diagnostics.perf_trace import once as _ponce
    from meltygui.core.diagnostics.perf_trace import trace as _ptrace

    _t_tabs0 = time.monotonic()
    view_funcs = [RenderFuncs.draw_collection_as_tabs, RenderFuncs.draw_text]
    # Drop entries that didn't survive (de)serialization, then default to two
    # tabs (structured | text), matching draw_with_view_funcs.
    tab_state.selected_tabs = [t for t in tab_state.selected_tabs if t is not None]
    if not tab_state.selected_tabs:
        tab_state.selected_tabs = view_funcs[:2]

    imgui.dummy(0, 5)
    names = [getattr(vf, '__name__', str(vf)) for vf in view_funcs]
    tab_changed, new_tabs = RenderFuncs.draw_tab_bar(input_value=tab_state.selected_tabs,
                                                     tab_height=30, show_bg=False, bg_offset=0,
                                                     name=f"tab_bar{unique}", names=names,
                                                     collection=view_funcs, as_toggles=False)
    if tab_changed:
        tab_state.selected_tabs = new_tabs

    imgui.dummy(0, 2)
    dict_host = None
    if root_input is not None:
        _str_host, dict_host = code_hosts_for(root_input)
        # Repaint this subtree when the background parse lands - it reads the
        # host's value from outside the host's own draw loop (same registration
        # draw_text_from_code_cache makes for its error/code_dict pull).
        dict_host.notify_on_change(draw_state)
        # Auto-index here too (idempotent with the draw_text delegate's call):
        # a structured-only window never runs draw_text_from_code_cache, and
        # its usage links should stay live all the same.
        _ensure_symbol_index(dict_host, _str_host, dict_host._held(),
                             kwargs.get("jump_to"))

        # New columnLayout (shared edge system): the panes line up with other
        # objects drawn on the root window - the divider between the
        # structured and text panes is a draggable line in the same collision
        # region as every other window edge. Lazy import (new_core_view sits
        # between this module and columns.py). left_edge/right_edge: when this
        # view renders inside another row's cell, the host passes the cell's edge
        # dicts (through code_file_io's child_kwargs) and they become this row's
        # far edges by reference - same adoption draw_columns gives nested
        # Columns. Absent (the usual standalone window), ColumnLayout falls back
        # to the window frame edges.
        from meltygui.core.layout.column_core import ColumnLayout
        from meltygui.core.layout.column_core import MIN_ROW_HEIGHT

        cols = ColumnLayout(draw_state, len(tab_state.selected_tabs),
                            column_edges=column_edges, column_widths=column_widths,
                            left_edge=kwargs.get("left_edge"),
                            right_edge=kwargs.get("right_edge"))
        # Pin each pane to the visible viewport (the legacy column_max_height
        # path this replaces) - long sources scroll inside their pane.
        avail_h = None
        size_kwargs = {}
        clip = cols.clip if cols.clip is not None else draw_state.abs_clip_rect
        if clip is not None:
            # Exactly match the frame band: the pane then ends one pixel
            # above the band's bottom edge, so the black reads even all around.
            avail_h = max(MIN_ROW_HEIGHT, clip[3] - cols.top)
            # The pane content is inset by the fixed padding on every side.
            size_kwargs = {"height": avail_h - 2 * cols.padding}

        # Grab the parse + its normalized error off the MANAGED dict_host once, before
        # the tab loop. Both tabs read them: the structured tab renders `gp` directly,
        # the text tab injects code_dict/code_tree/error into its draw_text leaf via
        # child_kwargs. Computing here also drops the old order dependency (the text tab
        # read `gp` before the structured branch defined it).
        gp = dict_host._held() if dict_host is not None else None
        if isinstance(gp, dict):
            if _ponce(("parse-visible", _host_label(dict_host))):
                _ptrace("tabs: first parse visible", host=_host_label(dict_host))
        elif dict_host is not None and _ponce(("parse-wait", _host_label(dict_host))):
            _ptrace("tabs: waiting for first parse", host=_host_label(dict_host))
        # Relint machinery + the import-suggestions pull run here too - this
        # route's text pane wires draw_text directly (below), NOT through
        # draw_text_from_code_cache, so without this the Alt-Enter quick-fix
        # data never reached it (the live_view_forward NEW_CODE path).
        import_fixes = _host_relint_and_fixes(
            dict_host, _str_host, getattr(dict_host, "_wrapper_draw_state", None))
        cache_error = _host_code_tree_error(dict_host)

        raw_changed, raw_value = False, input_value
        _pane_ms = {}
        for idx, view_func in enumerate(tab_state.selected_tabs):
            _t_pane0 = time.monotonic()
            with cols.cell(idx, height=avail_h) as col_width:
                if getattr(view_func, "__name__", "") == "draw_text":
                    # The host's parse + errors flow to the draw_text leaf through
                    # draw_collection's child_kwargs: code_dict → token views / symbol
                    # usages, code_tree → the syntax/lint error highlight, error → the
                    # recompile/runtime highlight (the same trio draw_text_from_code_cache
                    # hands draw_text, now via the parent _str_host).
                    m_changed, m_out = RenderFuncs.draw_collection(
                        input_value=_str_host,
                        child_kwargs={"error": error, "view_func": RenderFuncs.draw_text, "is_tree": False,
                                      "code_dict": gp, "code_tree": cache_error, "child_kwargs": {"is_tree": False},
                                      "import_fixes": import_fixes,
                                      "run_jedi": run_jedi, "jump_to": kwargs.get("jump_to")},
                        show_header=False, show_name=False,
                        width=col_width, **size_kwargs,
                        name=f"draw_text##{unique}")
                    if m_changed:
                        notify("text changed", tag="save bug", tint=(1, 1, 0.5))
                        raw_changed, raw_value = True, m_out
                        # draw_state.invalidate_up(max_depth=2)
                else:
                    if not isinstance(gp, dict):
                        # Placeholder frame: keep every ancestor's persisted
                        # content_height (see Melty.pending_placeholder_frame) -
                        # this one-line stand-in doesn't become the measure.
                        Melty.pending_placeholder_frame = Melty.frame_count
                        imgui.text_colored("Parsing…" if dict_host is not None
                                           else "No parse for this source", 0.6, 0.6, 0.6, 1.0)
                        continue
                    # No draw= forwarding: code_file_io passes draw=True the frame
                    # after EVERY keystroke (its reconvert trigger), and the one-shot
                    # cache bypass rebuilt this whole structured pane per key
                    # (~13-23ms for Toggles' ~110 rows). The pane's content (gp)
                    # only changes when a chain_in parse lands, and that landing
                    # already force-invalidates this subtree (dict_host's
                    # _notify_callers) - so the pane refreshes once per debounced
                    # parse instead of per keystroke.
                    # DEBUG (dict-pane keystroke cost): snapshot the pane's tile
                    # state BEFORE the call - was it dirty (someone invalidated
                    # it) or clean (cache gate re-ran the pane anyway)?
                    _dbg_key = getattr(draw_state, "_dbg_dict_key", None)
                    _dbg_t = (Melty.cache._tiles.get(_dbg_key)
                              if _dbg_key and Melty.cache is not None else None)
                    _pane_ms["dict_pre"] = (
                        f"dirty={_dbg_t.dirty}/inv=f{_dbg_t.last_invalidated_frame}"
                        f"/clean=f{_dbg_t.last_clean_frame}/now=f{Melty.frame_count}"
                        if _dbg_t is not None else "tile=?")
                    m_changed, m_out = RenderFuncs.draw_collection(
                        gp, excluded=["__cst__", "__origin__"],
                        child_kwargs={"show_bg": False, "shadow": False, "folder_type":(dict), "use_cache": True, "z_offset": 0, "view_func":RenderFuncs.draw_collection_as_tabs},
                        show_system=False,
                        disable_scroll=False, show_header=False, show_add_delete=False,
                        width=col_width, **size_kwargs, show_parent_add_delete=False,
                        name=f"draw_collection##{unique}", selectable=False)
                    if _dbg_key is None and Melty.cache is not None:
                        # One-shot: resolve the pane's tile key by name and arm the
                        # tile's invalidation stack-print on its draw_state.
                        _nm = f"draw_collection##{unique}"
                        for _k, _ds2 in Melty.cache.key_to_draw_state.items():
                            if _ds2 is not None and getattr(_ds2, "name", None) == _nm:
                                draw_state._dbg_dict_key = _k
                                # Arm the tile-bump tracer (see draw_offscreen's
                                # _bump_note): every code path that dirties this
                                # pane's tile names itself in the perf log.
                                _ds2._bump_trace_armed = True
                                break
                    if m_changed:
                        notify("dict changed", tag="save bug", tint=(1, 1, 0.5))
                        # A rebuilt top-level dict (reorder / add / delete) replaces the
                        # host value; an in-place value edit already bubbled the host
                        # dirty. Either way the host chain_outs + updates on its own draw.
                        if m_out is not gp and isinstance(m_out, dict):
                            dict_host[dict_host.value_key] = m_out
                            gp = m_out
                        live_apply_edits(root_input, gp)
                        draw_state.invalidate_up(max_depth=2)
            _pane_ms[getattr(view_func, "__name__", str(idx))] = \
                (time.monotonic() - _t_pane0) * 1000.0

        cols.finish()
    _dt_tabs = (time.monotonic() - _t_tabs0) * 1000.0
    if dict_host is not None and (
            _dt_tabs >= 10.0
            or (isinstance(_pane_ms.get("draw_collection"), float)
                and _pane_ms["draw_collection"] >= 3.0)):
        # Render time time inside the tabs subtree this frame - per-pane
        # split so a slow frame shows the pane (text editor vs structured
        # collection) instead of one opaque total. UNratelimited while the
        # dict pane takes time, so a typing burst shows every occurrence
        # (bounded by keystroke rate; dict_pre shows the pane tile's dirty
        # state going in).
        _panes = " ".join(f"{k}={v:.0f}ms" if isinstance(v, float) else f"{k}={v}"
                          for k, v in _pane_ms.items())
        _ptrace(f"tabs frame took {_dt_tabs:.0f}ms [{_panes}]",
                host=_host_label(dict_host))
    return False, None


def draw_jump_to(input_value: Address, unique, width=30, error_msg=None,
                 draw_state=None):
    file_name = input_value.path.name if input_value.path is not None else "Unknown file"
    line_number = input_value.start + 1 if input_value.start is not None else None
    # Unicode escape (not a literal string) for the Font Awesome folder icon - a
    # pasted PUA char gets stripped to empty on save, which is why it vanished.
    folder_icon = ""  # FA folder

    # Label with the enclosing function name + line number. Prefer the function
    # already attached to the address (.source); otherwise retrieve it from the
    # line via the cached _enclosing_function helper.
    fn = input_value.source if isinstance(input_value.source, types.FunctionType) else None
    if fn is None and line_number is not None and input_value.path is not None:
        from meltygui.code.chain_converters import _enclosing_function
        fn = _enclosing_function(str(input_value.path), line_number)

    label = f"{file_name}:{line_number}" if line_number is not None else file_name
    if fn is not None:
        label = f"{fn.__name__}  ({label})"

    # File-header bar: a rounded filled rect spanning the content width, drawn
    # behind the label + jump button. Packed ABGR colors per the codebase idiom.
    # When there's an error, the bar grows a second row to hold the message, and
    # both the fill and the outline tint red so the header reads as "this file has
    # a problem".
    draw_list = imgui.get_window_draw_list()
    x0, y0 = imgui.get_cursor_screen_pos()
    pad_x, pad_y = 8, 3
    row_h = imgui.get_frame_height() + pad_y * 2
    msg = str(error_msg).split('\n', 1)[0] if error_msg else None
    msg_row_h = (imgui.get_text_line_height() + 4) if msg else 0
    x1, y1 = x0 + width, y0 + row_h + msg_row_h
    # Round only the top two corners so the bar reads as a box sitting flush
    # on top of the body below it.
    rounding = 4.0
    top_corners = imgui.DRAW_ROUND_CORNERS_TOP
    if msg:
        fill_col = pack_color(70 / 255, 30 / 255, 40 / 255, 235 / 255)    # dark red-tinted fill
        line_col = pack_color(150 / 255, 60 / 255, 70 / 255, 1.0)         # red outline
    else:
        fill_col = pack_color(44 / 255, 52 / 255, 62 / 255, 230 / 255)
        line_col = pack_color(66 / 255, 78 / 255, 90 / 255, 1.0)
    draw_list.add_rect_filled(x0, y0, x1, y1, fill_col, rounding, top_corners)
    draw_list.add_rect(x0, y0, x1, y1, line_col, rounding, top_corners)

    # Row 1: label (vertically centered against the button frame) + icon jump
    # button. flat_button (draw-list + on_action through the EDITOR's
    # draw_state - this bar is drawn inside draw_text's body), not a
    # @render_func button: the old widget re-rendered its full wrapper every
    # editor frame. The measured rect is stashed in the editor's state so its
    # selection pass can null the PRESS inside it (the old button's own
    # draw_state used to claim that press; without the null, clicking Open
    # would also place the caret in the document under the floating bar).
    from meltygui.view.header_view import flat_button
    from meltygui.core.melty import Melty
    imgui.set_cursor_screen_pos((x0 + pad_x, y0 + pad_y))
    _open_label = f"{folder_icon} Open"
    _bw = imgui.calc_text_size(_open_label).x + Melty.px(15)
    _bh = Melty.px(18.0)
    _bx, _by = imgui.get_cursor_screen_pos()
    if draw_state is not None:
        draw_state._jump_btn_rect = (_bx, _by, _bx + _bw, _by + _bh)
    if flat_button(f"{_open_label}##jump_to{unique}", draw_state,
                   view_id=f"jump_open{unique}", width=_bw, height=_bh):
        from meltygui.core.runtime.extensions import open_source as open_in_editor
        open_in_editor(str(input_value.path), line_number=line_number,
                       token=fn.__name__ if fn is not None else None)

    imgui.same_line()
    imgui.align_text_to_frame_padding()
    imgui.text(label)

    # Row 2: the full error message, in red, spanning the bar. Truncated to the
    # bar width so a long message can't overflow.
    if msg:
        avail = max(0, width - 2 * pad_x)
        if imgui.calc_text_size(msg).x > avail:
            ch_w = max(1.0, imgui.calc_text_size("x").x)
            keep = max(3, int(avail / ch_w) - 1)
            msg = msg[:keep] + "…"
        imgui.set_cursor_screen_pos((x0 + pad_x, y0 + row_h - 2))
        imgui.text_colored(msg, 1.0, 0.5, 0.46, 1.0)

    # Reserve the bar's full height so following content doesn't overlap it.
    imgui.set_cursor_screen_pos((x0, y1 + 2))


def draw_code_line_fast(draw_list, x, y, text, char_w, line_h, max_width=None,
                        block_tint=None, spans=(), line_open=None, alpha=1.0,
                        block_rounding=4.0, emphasis=None, dim_alpha=None):
    """Paint `text` (one line, no newline) at (x, y) with the editor's syntax
    colours. `char_w` / `line_h` come from `push_code_font` (the font must
    be pushed). `max_width` truncates by whole cells. `block_tint` (rgb)
    paints the definition block wash under the line — the enclosing tinted
    class/def — and `spans` = [(col_start, col_end, rgb, scale)] the
    occurrence washes, LINE-relative columns (see `CodeLineTints`).
    `line_open` is the lexer state at the line's start (None = code,
    'comment', or the (quote, kind) pair of a string the line begins
    inside). `alpha` fades everything (a context row). `emphasis` =
    [(col_start, col_end)] keeps those glyphs at full brightness and fades
    every other glyph by `dim_alpha` (default the diff-collapse preview
    fade, `Toggles.TextEditor.diff_preview_alpha`) — an EMPTY list dims the
    whole line, None dims nothing. Washes never dim. Returns the painted
    width in pixels."""
    from meltygui.editor.code_line_fast import _split_emphasis
    from meltygui.editor.code_line_fast import _u32
    from meltygui.editor.code_line_fast import _wash_factors

    from meltygui.editor.text_editor import COLORS
    from meltygui.editor.text_editor import _tokenize_from
    from meltygui.editor.text_editor import _bg_adjust
    from meltygui.editor.text_editor import _mix_packed
    from meltygui.editor.text_editor import _fade_packed
    if max_width is not None:
        cells = max(0, int(max_width // char_w))
        if len(text) > cells:
            text = text[:cells]
    width = len(text) * char_w
    block_f, sym_f, text_f = _wash_factors()

    # ── block wash: the tinted definition body this line belongs to ──
    if block_tint is not None:
        rgb = _bg_adjust(tuple(block_tint[:3]), block_f)
        a = max(0.0, min(1.0, Toggles.TextEditor.def_block_alpha)) * alpha
        draw_list.add_rect_filled(x, y, x + max(len(text) + 1, 2) * char_w,
                                  y + line_h, _u32(rgb, a), block_rounding)

    # ── occurrence washes (+ their outline), same rects as the editor ──
    sym_a = Toggles.TextEditor.def_symbol_alpha * alpha
    ol_a = Toggles.TextEditor.def_symbol_outline_alpha * alpha
    ol_b = Toggles.TextEditor.def_symbol_outline_brightness
    ol_t = Toggles.TextEditor.def_symbol_outline_thickness
    mixes = []   # (col_start, col_end, rgb_adjusted_for_text, scale)
    for c0, c1, rgb, scale in spans:
        c0, c1 = max(0, c0), min(len(text), c1)
        if c1 <= c0:
            continue
        sa = _bg_adjust(tuple(rgb[:3]), sym_f)
        sx, ex = x + c0 * char_w, x + c1 * char_w
        draw_list.add_rect_filled(sx - 1, y + 1, ex + 1, y + line_h - 1,
                                  _u32(sa, sym_a * scale), 3.0)
        if ol_a > 0:
            ol = (min(1.0, sa[0] * ol_b), min(1.0, sa[1] * ol_b), min(1.0, sa[2] * ol_b))
            draw_list.add_rect(sx - 1, y + 1, ex + 1, y + line_h - 1,
                               _u32(ol, ol_a * scale), 3.0, thickness=ol_t)
        mixes.append((c0, c1, _bg_adjust(tuple(rgb[:3]), text_f), scale))

    # ── glyphs: tokens → palette, mixed by the wash under them ──
    mix_k = Toggles.TextEditor.def_text_tint_mix
    default = COLORS["default"]
    try:
        tokens = _tokenize_from(text, line_open)
    except Exception:
        tokens = [(text, "default")]
    if emphasis is not None and dim_alpha is None:
        dim_alpha = Toggles.TextEditor.diff_preview_alpha
    col = 0
    run_parts, run_x, run_col = None, 0.0, 0
    pieces = []   # (col, text, kind) - tokens cut at emphasis boundaries
    for token, kind in tokens:
        if not token:
            continue
        nl = token.find("\n")
        if nl != -1:
            token = token[:nl]
            if not token:
                break
        if kind == "clipped":
            col += len(token)
            continue
        if emphasis is not None:
            for pc, pt, bright in _split_emphasis(col, token, emphasis):
                pieces.append((pc, pt, kind, bright))
        else:
            pieces.append((col, token, kind, True))
        col += len(token)
        if nl != -1:
            break
    for col, token, kind, bright in pieces:
        color = COLORS.get(kind, default)
        for c0, c1, rgb, scale in mixes:
            if c0 <= col < c1:
                color = _mix_packed(color, rgb, mix_k * scale)
                break
        if alpha < 1.0:
            color = _fade_packed(color, alpha)
        if not bright:
            color = _fade_packed(color, dim_alpha)
        tx = x + col * char_w
        if kind == "icon":
            if run_parts is not None:
                draw_list.add_text(run_x, y, run_col, "".join(run_parts))
                run_parts = None
            # Icons aren't monospaced: one per standard cell, nudged 1px left.
            ix = tx
            for ch in token:
                draw_list.add_text(ix - 1, y, color, ch)
                ix += char_w
        elif token.isascii() and "\t" not in token:
            if run_parts is not None and run_col == color:
                run_parts.append(token)
            else:
                if run_parts is not None:
                    draw_list.add_text(run_x, y, run_col, "".join(run_parts))
                run_parts, run_x, run_col = [token], tx, color
        else:
            if run_parts is not None:
                draw_list.add_text(run_x, y, run_col, "".join(run_parts))
                run_parts = None
            draw_list.add_text(tx, y, color, token)
    if run_parts is not None:
        draw_list.add_text(run_x, y, run_col, "".join(run_parts))
    return width


def draw_live_view_overlay(x=0, y=0, w=0, h=0, draw_state=None, char_w=8.0,
                           line_px=20.0, node=None, span=None, root=None,
                           line_offset=0, jump_to=None, sel_lo=None,
                           sel_hi=None, **kwargs):
    """token_views overlay callback for CallParse nodes (plain function — the
    overlay pass calls it with raw screen coords, no render_func wrapper)."""
    from meltygui.code.live_view import live_values_for
    from meltygui.code.live_view import site_for_line
    from meltygui.editor.live_view_views import _code_end_col
    from meltygui.editor.live_view_views import _draw_marker_at
    from meltygui.editor.live_view_views import _inline_value_text
    from meltygui.editor.live_view_views import _line_in_selection
    from meltygui.editor.live_view_views import _marker_idle_skip
    from meltygui.editor.live_view_views import _stable_key_name
    from meltygui.editor.live_view_views import _store_key_names
    from meltygui.editor.live_view_views import _store_name
    from meltygui.editor.live_view_views import _token_in_selection

    if getattr(node, "func_name", None) != "live_view":
        return
    from meltygui.core.runtime.toggles import Toggles
    if not Toggles.TextEditor.enable_live_view:
        return
    # Viewport cull FIRST: the parse walk visits every node in the buffer, not
    # just the visible ones - each off-screen marker is a full render_func call
    # for nothing (its latched value will propagate via root_draw_states
    # either way, exactly as when its liveosh scrolls in). Culling before
    # the store lookup also keeps site_for_line (span parse + linemap, per
    # node per frame) off every out-of-view live_view node.
    clip = getattr(draw_state, "abs_clip_rect", None)
    _off_view = clip is not None and (y + line_px < clip[1] or y > clip[3])
    if (_off_view and getattr(draw_state, "_lv_full_overlay_until", 0)
            <= Core.melty.frame_count):
        # _lv_full_overlay_until: non-instrumented-run forward pass - an
        # off-viewport marker with an OPEN window still renders once (with a
        # FROZEN anchor, below) so the next value flows into the window.
        return
    filename = (getattr(root, "file_path", None)
                or getattr(getattr(root, "address", None), "path", None)
                or getattr(jump_to, "path", None))
    if filename is None:
        return
    # span lines are 1-indexed relative to the editor buffer; line_offset is
    # the 0-based file line of buffer line 0 → absolute 1-indexed file line.
    store_obj, key_path = site_for_line(str(filename),
                                        line_offset + span.start_line)
    if store_obj is None:
        return
    token_cells = max(1, span.end_col - span.start_col)
    if getattr(span, "end_line", span.start_line) != span.start_line:
        # Multi-line call: box just the first line, from the token start to
        # the end of that line's text.
        src_lines = (getattr(root, "source", "") or "").split("\n")
        if 1 <= span.start_line <= len(src_lines):
            token_cells = max(1, len(src_lines[span.start_line - 1].rstrip())
                              - span.start_col)
    pad = 2.0
    # Selection predicate in buffer space: span lines are parse-relative,
    # so map through the parse→buffer bridge before comparing with the
    # editor's (line, col) selection bounds. The widget counts as "inside"
    # only when its whole token lies within the selection - a bare caret
    # (no selection) never shows the window.
    _lm = kwargs.get("line_map")
    _sl = _lm(span.start_line) if _lm else span.start_line
    cursor_inside = _token_in_selection(
        _sl, span.start_col, span.start_col + token_cells, sel_lo, sel_hi)
    snap = live_values_for(store_obj)
    # The SHARED per-store name map (generation-keyed) - never a private
    # copy: a stale private map minted names another consumer's fresh map
    # gave to different keys (duplicate view IDs). The fallback name the
    # key_path too, so a not-yet-mapped key still ranks against its twins.
    _me = _store_key_names(store_obj)
    _mname = (f"lvm::{_store_name(store_obj)}"
              f"::{_me.get(key_path) or _stable_key_name(key_path, snap)}")
    _frozen_pos = None
    if _off_view:
        # Forward pass for a culled marker: only proceed when its window is
        # open, and draw at the marker's current absolute position - anchoring
        # the pinned window at the true off-screen anchor parks it at
        # _pinned_base = an editor-bottom clamp (the window "disappears").
        _mreg = getattr(draw_state, "_lv_marker_ds", None)
        _mds = _mreg.get(_mname) if _mreg else None
        _w = getattr(_mds, "_lv_window_ds", None) if _mds else None
        if _mds is None or _w is None or _w.closed:
            return
        _frozen_pos = (_mds.abs_left, _mds.abs_top)
    _value = snap.get(key_path)
    # A marker with an inline value draws every editor repaint (the value
    # bakes into the tile), so it can never take the idle skip.
    if (_frozen_pos is None
            and _inline_value_text(_value) is None
            and _marker_idle_skip(
                draw_state, _mname, x - pad, y - pad,
                token_cells * char_w + 2 * pad, line_px + 2 * pad,
                key_path in snap, cursor_inside, store_obj, key_path,
                (_sl or span.start_line) - 1, True)):
        return
    # Inline pill over a live_view() call: the call is instrumentation, not
    # code worth peeking at, so the card FILLS the entire call span; the
    # value may grow past it only when the span ends its line's code
    # (source split shared with the snapshot overlay's memo, same memo
    # invalidation).
    _memo_ent = draw_state.__dict__.get("_lv_snap_memo")
    _rsrc = getattr(root, "source", "") or ""
    if _memo_ent is None or _memo_ent[0] is not _rsrc or len(_memo_ent) < 3:
        _memo_ent = (_rsrc, {}, _rsrc.split("\n"))
        object.__setattr__(draw_state, "_lv_snap_memo", _memo_ent)
    _lines = _memo_ent[2]
    _ltext = (_lines[span.start_line - 1]
              if 1 <= span.start_line <= len(_lines) else "")
    _tok_end = span.start_col + token_cells
    _overflow = _tok_end >= _code_end_col(_ltext)
    _draw_marker_at(draw_state,
                    _frozen_pos if _frozen_pos is not None
                    else (x - pad, y - pad),
                    cursor_inside,
                    (_sl, span.start_col, span.start_col + token_cells),
                    "/".join(map(str, key_path)),
                    value=_value,
                    captured=key_path in snap,
                    store_obj=store_obj, key_path=key_path,
                    inline_values=True,
                    inline_span_w=token_cells * char_w,
                    inline_overflow=_overflow, inline_fill=True,
                    in_selection=_line_in_selection(_sl, sel_lo, sel_hi),
                    caret_line=kwargs.get("caret_line"),
                    width=token_cells * char_w + 2 * pad,
                    height=line_px + 2 * pad,
                    buffer_line=(_sl or span.start_line) - 1,
                    name=_mname)


@render_func(use_cache=False, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, disable_scroll=True, wrap=True,
             z_offset=4, max_height=32, auto_state=False)
def draw_live_view_marker(input_value=None, draw_state=None,
                          store_obj=None, key_path=None, captured=False,
                          code_tree_node=None, auto_open=True,
                          inline_values=False, inline_dx=0.0,
                          inline_span_w=None, inline_overflow=False,
                          inline_fill=False,
                          in_selection=False, caret_line=None,
                          corner_radius=4.0, value=None,
                          left_mouse_double_clicked=False,
                          cursor_inside=False, editor_ds=None,
                          buffer_line=None, def_node=None, unique=0, **kwargs):
    """The live-view token widget — draw_bool_token's pattern plus one extra
    call, draw_any(value, mode=WINDOW). `value` is the captured value (None +
    captured=False while the site hasn't run); the window call just forwards
    it and the framework routes it by type. input_value is a cheap STABLE
    TOKEN (the key string), deliberately NOT the value: the wrapper's
    recursion guard tracks non-primitive input_values by id, and a captured
    object that also sits in the render ancestry (draw_state/ds aliases, the
    menu's own target) tripped it — painting "Recursive reference detected"
    over the code — while big values also paid wrapper bookkeeping per
    marker. draw_text positioned this view inline over the symbol and the
    draw_state carries its size — no rect plumbing.

    (A bare-function version — draw_number_token_plain's pattern — was tried
    and REVERTED: the swoosh connector anchors the value window back to the
    marker's draw_state, so the marker must stay a render_func. Off-viewport
    markers are culled by the overlays, which bounds the wrapper cost to
    visible markers.)

    `code_tree_node` is the owning scope's dict from draw_text's parse; the
    site's `# [...]` comment is already formatted as a dict there
    (__overrides__['__<key>__']) and IS, 1:1, the window call's **kwargs.
    The box wears its `tint`.

    Window close: the one place a Modes.WINDOW child differs from a direct
    call — the framework can't see when a parent STOPS calling draw_any (it
    approximates liveness with abs_closed) — so the window ds is tracked by
    hand and, once it exists, called every render with visibility driven
    through the closed= kwarg. (Maybe the framework can own this someday.)

    `auto_open=False` (the snapshot/param markers) keeps the window closed
    until the box is double-clicked — only explicit live_view() tokens pop
    their value unprompted.

    Interaction follows the number-widget convention: a single press/click
    passes straight through to the editor (caret placement, selection —
    plain text editing; nothing is declared to latch it away), and the
    widget's own gesture is separate — DOUBLE-click toggles the value
    window. left_mouse_double_clicked is declared (never read) as the
    subscription half: hover-routed — the marker's higher z outranks the
    editor's word-select for the doubled press — and its delivery
    invalidates the tile so the body renders on the frame the raw
    is_mouse_double_clicked read below is true."""
    from meltygui.code.live_view import auto_dim_names_for
    from meltygui.code.live_view import watch
    from meltygui.editor.live_view_views import _NO_VALUE
    from meltygui.editor.live_view_views import _SWATCH_HOLE
    from meltygui.editor.live_view_views import _auto_run_on_user_open
    from meltygui.editor.live_view_views import _def_name
    from meltygui.editor.live_view_views import _display_key
    from meltygui.editor.live_view_views import _drop_captured_value
    from meltygui.editor.live_view_views import _ds_in_window
    from meltygui.editor.live_view_views import _inline_swatch_rgba
    from meltygui.editor.live_view_views import _inline_value_text
    from meltygui.editor.live_view_views import _left_of_window_pos
    from meltygui.editor.live_view_views import _merged_dim_names
    from meltygui.editor.live_view_views import _mouse_in_window_tree
    from meltygui.editor.live_view_views import _override_owner
    from meltygui.editor.live_view_views import _padded_dim_names
    from meltygui.editor.live_view_views import _paint_value_pill
    from meltygui.editor.live_view_views import _pill_tint
    from meltygui.editor.live_view_views import _stacked_list_value
    from meltygui.editor.live_view_views import current_live_root
    from meltygui.editor.live_view_views import release_live_value
    from meltygui.view.header_view import draw_header

    ds = draw_state
    # Gutter registry: tell the editor which buffer lines carry a live marker
    # so its line-number gutter can draw a raw open/close button per line
    # (see the gutter pass in text_editor / set_marker_open below). Rebuilt
    # from scratch each frame the overlays render - frame-stamped so stale
    # entries from a previous parse never linger on the editor ds.
    if editor_ds is not None and buffer_line is not None:
        if getattr(editor_ds, "_lv_gutter_frame", None) != Core.melty.frame_count:
            editor_ds._lv_gutter_frame = Core.melty.frame_count
            editor_ds._lv_gutter_markers = {}
        editor_ds._lv_gutter_markers.setdefault(buffer_line, []).append(ds)
    # Marker-ds registry for the overlays' idle fast path (_marker_idle_skip):
    # lets them consult this site's state without paying the wrapper.
    if editor_ds is not None:
        reg = getattr(editor_ds, "_lv_marker_ds", None)
        if reg is None:
            reg = editor_ds._lv_marker_ds = {}
        reg[ds.name] = ds
        # Backlink for set_marker_open (the gutter pass only has the marker
        # ds): the value window anchors to the draw TEXT's left edge, which
        # only the editor ds knows.
        ds._lv_editor_ds = editor_ds
    # First only only: a gray box whose code then runs gets invalidated
    # on the key's FIRST value, flips green (and auto-opens below, when this
    # marker auto-opens) — one editor re-render per new key, nothing per
    # steady-state publish. The invalidation re-runs the overlay, which boxes
    # the marker and passes the new value in.
    watch(store_obj, key_path, ds, first_only=True)

    # The comment data, already a dict in the code tree. Statement keys ARE
    # the symbol name; a `line:N#name` tail (frame snapshots, twin-site
    # params) carries the name behind the hash - the `# [...]` comment is
    # stamped in __overrides__ under the STATEMENT key, so resolve by it
    # either way (this is what keeps comment overrides like tint/cam_zoom
    # flowing into the value windows after the line-key migration). The
    # owning dict is found by _override_owner - a loop/if/try-body site's
    # comment lives in ITS block's nested dict, not the top of the scope.
    comment_args = {}
    lookup_key = None
    live_root = code_tree_node
    _locator = None
    if key_path:
        tail = str(key_path[-1])
        lookup_key = (tail.split("#", 1)[1]
                      if tail.startswith("line:") and "#" in tail else tail)
    if isinstance(code_tree_node, dict) and lookup_key:
        # Locator for current_live_root (def name + start line, the editor
        # ds only as a fallback during readers), stamped BEFORE the comment
        # read so this render's own splat also resolves through the code
        # host's held tree - `code_tree_node` is whatever the cached tabs
        # body last captured and can lag a reparse by frames.
        _dname = _def_name(def_node)
        _locator = None
        if _dname:
            _dspan = getattr(def_node, "span", None)
            _locator = (editor_ds, _dname, getattr(_dspan, "start_line", 0) or 0)
        # Owner through the editor's per-def index (one BFS per def per
        # source version), not a BFS per marker; the raw walk stays the
        # fallback when there's no editor / def to key on.
        _osrc = None
        if editor_ds is not None and _locator is not None:
            _ecd = editor_ds.__dict__.get("code_dict")
            if not isinstance(_ecd, dict):
                _ecd = editor_ds.__dict__.get("code_tree")
            _osrc = getattr(_ecd, "source", None) if isinstance(_ecd, dict) else None
        live_root = _override_owner(
            code_tree_node, lookup_key,
            index_host=editor_ds if _osrc is not None else None,
            def_key=(_locator[1], _locator[2]) if _locator else None,
            src=_osrc)
        object.__setattr__(ds, "live_root", live_root)
        object.__setattr__(ds, "live_key", lookup_key)
        object.__setattr__(ds, "_lv_locator", _locator)
        _resolved = current_live_root(ds)
        if isinstance(_resolved, dict):
            live_root = _resolved
        _ov = live_root.get("__overrides__")
        _ca = _ov.get(f"__{lookup_key}__") if isinstance(_ov, dict) else None
        if isinstance(_ca, dict):
            comment_args = {k: v for k, v in _ca.items()
                            if not (isinstance(k, str) and k.startswith("__"))}

    # A list of same-shape tensors renders as ONE stacked tensor (leading
    # dim = list index) - the accumulator normally stacks at capture time,
    # but a raw captured list, its ragged-shape fast path, or a store built
    # by older code all arrive here as lists; healing at display time makes
    # "stacked" unconditional.
    value = _stacked_list_value(value, ds)

    # dim_names is a SPECIAL input for loop sites: an accumulation value
    # carries auto-named leading dims (one per enclosing loop - stamped in
    # live_view._stack), and the site's own `# [dim_names=...]` names the
    # per-iteration value's dims. Prepend auto to user before the window
    # renders, so `for l_idx ...` over a (head, query, key) accumulator reads
    # ('l_idx', 'head', 'query', 'key') in the voxel tab. Tensor-shaped
    # values only - a list accumulator has no dims to name.
    _auto_dims = auto_dim_names_for(store_obj, key_path)
    _vkind = type(value).__name__
    _user_dims = comment_args.get("dim_names")
    _merge_auto = (_auto_dims if _auto_dims
                   and _vkind in ("Tensor", "ndarray") else None)
    if _merge_auto:
        comment_args = dict(comment_args)
        comment_args["dim_names"] = _merged_dim_names(_merge_auto, _user_dims)
    # Too few names for the value's dims (or none at all)? pad with
    # positional dim<i> entries - AFTER the auto merge, so the pad covers
    # whatever the merged list still leaves out.
    _ndim = (len(getattr(value, "shape", ()))
             if _vkind in ("Tensor", "ndarray") else 0)
    _pad = _padded_dim_names(comment_args.get("dim_names"), _ndim)
    if _pad:
        comment_args = dict(comment_args)
        comment_args["dim_names"] = _pad

    # Input-tab lookup: the context menu resolves this site's inputs off the
    # draw_state graph (draw_input_tab's live_root branch), so attach the same
    # OWNING dict the comment-args splat reads - the nested block dict for a
    # loop-body site - restamped every render like everything else per-site.
    # set_anywhere's lazy `# []` entry then materializes at the level the
    # save patch then writes back (a top-of-scope entry for a loop site
    # would never reach the site). Same name-normalized key as the comment
    # lookup, so line-keyed sites find their statement entry too.
    ds.live_root = live_root
    ds.live_key = lookup_key
    # Locator for current_live_root (stamped first, before the comment
    # read): readers that run while this marker ISN'T rendering (replayed
    # window, set_anywhere from its panel) resolve the owner dict through
    # the code host's held tree instead of the stamp, which every reparse
    # orwrites. The owner memo (_lv_owner_dict) is keyed on tree identity
    # and therefore survives renders - the def lookup runs once per
    # reparse, not once per frame.
    object.__setattr__(ds, "_lv_locator", _locator)

    from meltygui.core.windowing.window_visibility import sync_marker_visibility
    sync_marker_visibility(ds, comment_args)
    auto_open = comment_args.get("auto_open", auto_open)

    # ── INLINE VALUE: a simple builtin (int/float/str/bool/shortlist/enum)
    # renders as a text label overlapping the top of its code line instead
    # of a separate window. Both modes pass inline_values=True (explicit
    # live_view() tokens and instrumented-run snapshot markers alike):
    # every boxed symbol with a simple value shows up in place.
    inline_text = (_inline_value_text(value)
                   if captured and inline_values else None)
    inline_swatch = None
    if inline_text is not None:
        _rgba = _inline_swatch_rgba(value)
        if _rgba is not None:
            inline_text = _SWATCH_HOLE + inline_text
            inline_swatch = (_rgba, 0)
    # An inline value has NO value window at all - no auto-open, no
    # preview, no double-click toggle. A window still open (persisted state,
    # or the value just turned simple) closes through the ordinary closing
    # draw_any call below. _lv_open resets to None, so a value that later
    # turns complex (str → tensor between runs) auto-opens again.
    if inline_text is not None and getattr(ds, "_lv_open", False):
        ds._lv_open = None
    # The gutter swaps the magnifier for an info glyph on inline markers
    # (no open/close left to toggle) - after the _lv_open pass in text.py.
    if getattr(ds, "_lv_inline", None) != (inline_text is not None):
        ds._lv_inline = inline_text is not None

    # Manual window-ds tracking (see docstring).
    win_ds = getattr(ds, "_lv_window_ds", None)
    if not captured and win_ds is not None and not win_ds.closed:
        # The captured value vanished (store dropped) while the window was
        # open: with captured False the draw_any block below never runs, so
        # nothing else would stamp closed= and the window would linger
        # orphaned in root_draw_states. Stamp it directly; _lv_open resets to
        # None so the next value auto-opens it.
        win_ds.closed = True
        ds._lv_open = None
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
    if (getattr(ds, "_lv_open", None) is None and captured and auto_open
            and inline_text is None):
        ds._lv_open = True  # first value seen → show it without a click
    elif win_ds is not None and win_ds.closed and getattr(ds, "_lv_open", False):
        ds._lv_open = False  # user closed the window via its own header X
        # Arm the cursor-dismissed latch too: with the caret still inside the
        # symbol, preview_show would otherwise re-show the window on the very
        # next frame - the X X pins first (it's a press inside the window
        # rect), then closes, and an unarmed latch made the close a no-op.
        # The latch resets itself once the focused caret leaves the symbol.
        ds._lv_cursor_dismissed = True
        # Closing a live view forgets its DATA (the loop accumulator + the
        # tensor, often hundreds of MB) - the store keeps the marker with a
        # rerun hint so the widget stays, and the next run refills it.
        _drop_captured_value(store_obj, key_path)
    open_now = bool(getattr(ds, "_lv_open", False))

    # ── EDIT-PIN: engaging with a preview window's own UI latches it open.
    # A cursor-held preview closes the moment the editor loses editor focus -
    # but the click that starts a param edit (a field in the window's
    # controls, or a context menu) IS such a focus change, so editing the
    # view's input params yanked the window (and the panel mid-edit) away.
    # The PRESS is the trigger, not focus: click processing clears the
    # editor's focus at frame start, this marker then sees and would stamp
    # the close, and only at end-of-frame dispatch would the clicked field
    # render and grab focus - by which point a closed window's panel never
    # renders at all. So on any engaged mouse press, rect-test the mouse
    # against the window and its controls and act exactly as if the marker
    # had been double-clicked; the focus check remains as the late-signal
    # fallback (e.g. focus handed over without a press). The header X still
    # unpins via the win_ds.closed branch above.
    if (captured and not open_now and inline_text is None
            and win_ds is not None and not win_ds.closed):
        _m = Core.melty
        pin = any(f is not None and _ds_in_window(f, win_ds)
                  for f in (_m.focused_ds, _m.text_focused_ds,
                            _m.popover_focused_ds))
        if not pin and (imgui.is_mouse_down(0) or imgui.is_mouse_clicked(0)
                        or imgui.is_mouse_released(0)
                        or imgui.is_mouse_down(1)
                        or imgui.is_mouse_clicked(1)):
            _io = imgui.get_io()
            pin = _mouse_in_window_tree(win_ds, _io.mouse_pos.x,
                                        _io.mouse_pos.y)
        if pin:
            ds._lv_open = True
            open_now = True
            ds.invalidate()

    x, y = imgui.get_cursor_screen_pos()
    w = max(1.0, ds.width)
    h = max(1.0, ds.height)
    io = imgui.get_io()
    hovered = x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h
    # Hover-edge invalidation (enter/leave only, never per-frame): the
    # outline is never-stated, so a cached tile must repaint exactly when
    # visibility changes.
    if getattr(ds, "_lv_hovered", None) != hovered:
        ds._lv_hovered = hovered
        ds.invalidate()

    # PREVIEW: temporarily show the captured marker's live value - same
    # window, same placement - with double-click below still latching it
    # open permanently. Two trigger modes on Toggles.TextEditor.
    # live_hover_preview: ON → mousing over the marker previews and
    # mouse-leave closes; OFF → the editor TEXT CURSOR coming inside the
    # symbol previews and caret-leave closes. The hover mode needs a
    # per-frame keep-alive while previewing (the leave edge can only be
    # SEEN by a running body - a cached tile never re-tests hover); the
    # cursor mode doesn't: the caret only moves on frames the editor
    # renders, and the cursor_inside edge below invalidates the tile.
    from meltygui.core.runtime.toggles import Toggles
    hover_mode = bool(Toggles.TextEditor.live_hover_preview)
    _raw_ci = bool(cursor_inside) and not hover_mode
    # Dismissed latch: an X-closed cursor preview stays dismissed until the
    # FOCUSED caret genuinely leaves the symbol once. clear_focus alone was
    # not enough - the close's focus drop raced re-grants, and any regained
    # focus with the caret still in the symbol instantly re-showed the
    # window. The latch is positional, so it holds through editor churn;
    # while the editor is unfocused the overlay passes null cursor coords
    # (_raw_ci False), so the reset below keys off actual editor focus.
    _ed_focused = (editor_ds is not None
                   and Core.melty.text_focused_ds is editor_ds)
    if _ed_focused and not _raw_ci:
        ds._lv_cursor_dismissed = False
    cursor_inside = _raw_ci and not getattr(ds, "_lv_cursor_dismissed", False)
    # Close observed one frame late (window rendered from root_draw_states
    # while this editor tile was cached): mark dismissal as the last
    # detection after draw_any below.
    if (cursor_inside and not open_now and win_ds is not None
            and win_ds.closed
            and getattr(ds, "_lv_cursor_preview_shown", False)):
        Core.melty.clear_focus()
        ds._lv_cursor_dismissed = True
        ds._lv_cursor_preview_shown = False
        cursor_inside = False
    if getattr(ds, "_lv_cursor_in", None) != cursor_inside:
        ds._lv_cursor_in = cursor_inside
        ds.invalidate()
    preview_show = (captured and not open_now and inline_text is None
                    and ((hovered and hover_mode) or cursor_inside))
    if preview_show and hover_mode:
        from meltygui.core.windowing.glfw_utils import request_render
        ds.invalidate()
        request_render()

    tint = comment_args.get("tint")
    if captured:
        if tint is not None:
            base = tuple(min(1.0, c + (0.15 if open_now else 0.0))
                         for c in tint[:3])
        else:
            base = (0.36, 0.85, 0.46) if open_now else (0.26, 0.62, 0.34)
    else:
        base = (0.45, 0.45, 0.45)
    if hovered:
        base = tuple(min(1.0, c + 0.18) for c in base)
    # Outline only under the mouse — the boxes read as clutter when every
    # instrumented symbol is permanently framed; hover reveals the
    # affordance. An inline marker skips it: its pill wears the token's
    # background tint (painted below), and there's no window gesture for
    # hover to advertise.
    if hovered and inline_text is None:
        dl: _DrawList = imgui.get_window_draw_list()
        dl.add_rect(x, y + 2, x + w, y + h - 3,
                    pack_color(*base, 0.9 if open_now else 0.6),
                    rounding=corner_radius)
    imgui.dummy(w, h)

    if inline_text is not None:
        # The label must repaint on every publish - the first_only watch
        # above fires once; this full watch invalidates per value.
        watch(store_obj, key_path, ds)
        # Only the FILL pill (a live_viewed call token - instrumentation
        # worth painting whole) paints here; comment/snapshot binding
        # markers paint through the trailing-gap pass instead (see
        # _draw_usage_labels - the code is repositioned, never covered).
        # Caret on this line, or the line inside the text selection: show
        # the real code, nothing painted - typing or deleting under a pill
        # would be blind. The pill comes back when the caret/selection
        # leave (the kwarg change re-renders this).
        if (inline_fill
                and (caret_line is None or caret_line != buffer_line)
                and not in_selection):
            # Drawn IN PLACE in the editor's code font (the current font -
            # no push), covering the instrumentation call token. The marker rect
            # wraps the token with a 2 px pad, so the token's text starts
            # at x + 2.
            text_x = x + 2.0 + inline_dx
            text_y = y + 2.0
            # The editor clipped this body to the token's own rect; the
            # pill is value-sized and can sit past that (the RHS). Pop out
            # to the ENCLOSING clip (the editor window) through Melty's own
            # stack and re-push the SAME rect after: push_clip intersects
            # with its parent, so pushing the saved (already-intersected)
            # rect restores it exactly, and the clip bookkeeping the tile
            # engine reads stays coherent.
            saved_clip = (Core.melty.clip_stack[-1]
                          if Core.melty.clip_stack else None)
            if saved_clip is not None:
                Core.melty.pop_clip()
            # The pill wears the token's background tint (the comment
            # tint, else the enclosing def/class, else the file) so an RHS
            # pill far down the line is colored as this scope's value.
            _paint_value_pill(inline_text, text_x, text_y,
                              span_width=inline_span_w,
                              allow_overflow=inline_overflow,
                              fill=inline_fill,
                              tint=_pill_tint(editor_ds, buffer_line,
                                              str(key_path[-1]) if key_path
                                              else None, tint),
                              swatch=inline_swatch)
            if saved_clip is not None:
                Core.melty.push_clip(saved_clip)

    # Double-click toggles the value window. Read RAW imgui here (the same
    # split the single-click version used): the declared event param is the
    # subscription/wake half - its delivery invalidates the tile so the body
    # renders on the very frame is_mouse_double_clicked is true - while the raw
    # read is the single trigger, so the two halves can never toggle twice
    # for one gesture.
    if hovered and inline_text is None and imgui.is_mouse_double_clicked(0):
        open_now = not open_now
        ds._lv_open = open_now
        from meltygui.core.windowing.window_visibility import marker_user_visibility
        marker_user_visibility(ds, not open_now)
        if open_now:
            _auto_run_on_user_open(editor_ds, store_obj)
        if open_now and win_ds is not None and "window_pos" not in comment_args:
            # Reopening: snap the window back to the LEFT of the editor
            # window (it may have been dragged onto the code). The window's
            # real size is known here, so the display clamps are exact.
            pos = _left_of_window_pos(
                editor_ds.abs_left if editor_ds is not None else None,
                x, marker_y=y, win_h=win_ds.height, win_w=win_ds.width)
            if pos is not None:
                win_ds.window_pos = pos
        ds.invalidate()

    # Draw the value window only while it shows - plus ONE closing call when
    # it just stopped showing (closed=True must be stamped on the ds so the
    # deferred dispatch discards it; skipping that call would leave an orphan
    # window rendering from root_draw_states). A window the user X-closed is
    # already stamped, so a closed marker costs zero draw_any calls per
    # render - and the full per-publish watch below stops too, leaving only
    # the cheap first_only watch above.
    _show = open_now or preview_show
    if captured and (_show or (win_ds is not None and not win_ds.closed)):
        # Full (per-publish) watch once a window exists so the value streams
        # in - the first_only call above only flips the box green.
        watch(store_obj, key_path, ds)
        from meltygui.core.rendering.render_dispatch import draw_any
        # Named by the code line's STABLE name - the marker's own name (raw,
        # line number stripped), never the full line-keyed path and never
        # draw_state.line: the name hashes into the unique ID, so a line
        # number here re-identified the window every time a rerun/edit
        # re-stamped the site's line.
        win_kwargs = dict(
            name=f"{'/'.join(map(_display_key, key_path))}"
                 f"##lv::{ds.name}",
            mode=Modes.LIVE_WINDOW, closed=not (open_now or preview_show),
            open_requested=bool(preview_show),
            with_header=draw_header, disable_scroll=True, return_extras=True,
            # Anchor like a context menu: pinned to the marker, so the window
            # tracks it live and takes the pinned base's clamp - it rides the
            # code only as far as the editor window's edges instead of
            # chasing the marker off screen. (Swoosh style, alone: these
            # get the ribbon.) Both anchors are TOP_LEFT so the pinned base is
            # the marker's top-left - exactly what the window_pos offsets below
            # are measured from. hide_offscreen=False keeps it drawn once the
            # marker itself scrolls away: the window is what keeps it on screen.
            pin_to_clip=Pin.PARENT, anchor=Anchor.TOP_LEFT,
            parent_anchor=Anchor.TOP_LEFT, hide_offscreen=False,
            # Edits made anywhere in this window's subtree (params panel,
            # popup menus) should land on this site's `# [...]` comment -
            # set_anywhere reads the flag off the window's kwargs (walking
            # up from nested windows) and creates the binding there if the
            # comment hasn't set the param yet.
            preferred_source="code comment")
        # First creation: anchor the window to the LEFT of the editor's
        # window, always on top of the code, lifted clear of the editor
        # bottom (estimated height - the real one doesn't exist yet).
        # window_pos persists on the spawned window's draw_state
        # (parent-relative, so it tracks the editor window) - set once; user
        # drags it preserved after.
        if win_ds is None and "window_pos" not in comment_args:
            pos = _left_of_window_pos(
                editor_ds.abs_left if editor_ds is not None else None,
                x, marker_y=y)
            if pos is not None:
                win_kwargs["window_pos"] = pos
        elif win_ds is not None:
            # REUSE the tracked window draw_state: draw_any routes by VALUE
            # type and folds the render func into the unique, so a value
            # whose type changed between runs (None→tensor, int→str...) would
            # otherwise mint a fresh same-named ds - position/size/open
            # state gone, the old window orphaned in root_draw_states as a
            # duplicate-ID phantom. Pinning the ds keeps the window's
            # identity; the wrapper restamps _view_func per call, so the
            # body still re-routes by the value type.
            win_kwargs["draw_state"] = win_ds
            # NEW VALUE → repaint, exactly once per publish: the window is a
            # deferred nested root, so this call only restamps its kwargs;
            # the publish's own invalidation (_notify_watchers, during the
            # run) will arrive BEFORE this marker re-renders; the window
            # repaints on the new input, and a restamped kwargs is
            # not itself a dirty signal: the window then held its last
            # frame (voxels) or the run-start None ("No view for type")
            # until something else triggered it. Gate on identity: an
            # unchanged value never invalidates.
            if (getattr(win_ds, "_raw_input_value", _NO_VALUE) is not value
                    and win_ds._tile_id is not None):
                Core.melty.cache.invalidate_up(win_ds._tile_id, force=True,
                                              max_depth=8)
        _c, _v, win_ds = draw_any(value, **(win_kwargs | comment_args))
        # Showstop: if the framework still handed back a different ds
        # (a path that ignores the pinned draw_state), close the replaced
        # one on the spot so it can never orphan.
        _prev_win = getattr(ds, "_lv_window_ds", None)
        if (_prev_win is not None and _prev_win is not win_ds
                and not _prev_win.closed):
            _prev_win.closed = True
        ds._lv_window_ds = win_ds
        # The menu usually opens on the WINDOW - stamp the site context there
        # too (the _parent chain isn't guaranteed to pass through this marker
        # after a root_draw_states re-dispatch).
        win_ds.live_root = live_root
        win_ds.live_key = ds.live_key
        # Same contract as the marker's (see current_live_root): the window
        # outlives this render - its replay re-splat and its panel's
        # set_anywhere must not trust a stamped tree a reparse may have
        # replaced since; they resolve through the host's held tree (memo
        # keyed by host identity, so it self-refreshes on every reparse).
        object.__setattr__(win_ds, "_lv_locator", _locator)
        from meltygui.core.windowing.window_visibility import override_state, user_window_closed
        _window_state = override_state(ds)
        if _window_state.pending_marker_closed is not None:
            user_window_closed(win_ds, _window_state.pending_marker_closed)
            _window_state.pending_marker_closed = None
        # The window's dispatch (Melty.draw, root_draw_states) re-reads
        # the CURRENT value for this key from the store, so a publish while
        # this marker is culled off-viewport still swaps the window's tensor
        # (fresh display, and the previous generation is released on the
        # spot instead of riding the stale kwargs until the marker next
        # renders).
        win_ds._lv_store_obj = store_obj
        win_ds._lv_key_path = key_path
        # Auto loop dims for the REPLAY path: the deferred root_draw_states
        # dispatch re-splats the site's raw `# [...]` comment over the stored
        # kwargs (meltygui.py, "Live-view comment re-splat"), and would clobber
        # the merged dim_names above with the comment's un-merged list. Stamp
        # the raw names and the value's dim count so the replay can redo the
        # same loop + dim<i> padding.
        win_ds._lv_auto_dims = _merge_auto
        win_ds._lv_ndim = _ndim
        # Window X-close detection: the header's close button runs DURING the
        # draw_any call above, so a caret-held preview closed this instant
        # shows as closed=True right after a window it passed closed=False.
        # Dismiss immediately - deterministic, no dependence on which order
        # the window and the editor re-render in adjacent frames.
        if (preview_show and cursor_inside and not open_now
                and win_ds.closed):
            Core.melty.clear_focus()
            ds._lv_cursor_dismissed = True
            ds._lv_cursor_in = False
            cursor_inside = False
            ds.invalidate()
            # The X on a caret-held preview parks the hint too (same
            # contract as the latched window above).
            _drop_captured_value(store_obj, key_path)

    # Stamp whether THIS frame's window visibility is caret-held - the X-close
    # should only fire for a window the cursor preview was in.
    ds._lv_cursor_preview_shown = bool(preview_show and cursor_inside)

    # A closed value window must not keep its last tensor (+ GL texture)
    # alive while the store streams on - release once per close (the flag
    # re-arms on the next show, when draw_any re-supplies the value).
    if win_ds is not None and win_ds.closed and not _show:
        if not getattr(win_ds, "_lv_released", False):
            win_ds._lv_released = True
            release_live_value(win_ds)
            # A closed window no longer earns its stack: drop the key's
            # accumulator and park the rerun hint in the store (the key
            # stays, the marker stays green), severing the marker/window
            # pins so nothing references the value any more.
            try:
                from meltygui.code.live_view import park_rerun_hint
                park_rerun_hint(store_obj, key_path)
            except Exception as e:
                print(f"live_view: park hint for {key_path} failed: {e!r}")
            try:
                from meltygui.core.runtime.gc_manager import release_cuda_cache_soon
                release_cuda_cache_soon(label="live window close")
            except Exception:
                pass
    elif win_ds is not None and getattr(win_ds, "_lv_released", False):
        win_ds._lv_released = False

    # The marker itself must not outlive the value it was handed: the
    # framework stores this call's kwargs on the ds (_kwargs), and a marker
    # that isn't rendered again (scrolled off; culled; key pruned) would pin
    # the tensor until the next time it draws. The value was only ever
    # needed inside this body (the window got its own copy via draw_any).
    _kw = ds.__dict__.get("_kwargs")
    if isinstance(_kw, dict) and _kw.get("value") is not None:
        _kw["value"] = None

    return False, None


def draw_snapshot_overlay(x=0, y=0, w=0, h=0, draw_state=None, char_w=8.0,
                          line_px=20.0, node=None, span=None, root=None,
                          line_offset=0, jump_to=None, **kwargs):
    """Per-FUNCTION-scope overlay: anchor every captured value that has NO
    live_view token to anchor to — assignment keys published by an
    instrumented twin (live_instrument) and line-keyed sites in while/with
    bodies — with the same marker/window/watcher stack as the call tokens.

    Registered for GeneralParse, so the walk calls it for many nodes; it acts
    only on def scopes (__cst__ FunctionDef). Deliberately NO identity checks
    against `root`: the code-host route hands the walk Bubbling proxy wrappers
    whose identities don't survive re-access, which is exactly how the first
    root-guarded version of this overlay silently never ran."""
    from meltygui.code.live_view import live_values_for
    from meltygui.code.live_view import watch
    from meltygui.editor.live_view_views import _NO_VALUE
    from meltygui.editor.live_view_views import _SWATCH_HOLE
    from meltygui.editor.live_view_views import _draw_marker_at
    from meltygui.editor.live_view_views import _draw_usage_labels
    from meltygui.editor.live_view_views import _inline_swatch_rgba
    from meltygui.editor.live_view_views import _inline_value_text
    from meltygui.editor.live_view_views import _is_funcdef_node
    from meltygui.editor.live_view_views import _key_anchor
    from meltygui.editor.live_view_views import _label_line_index
    from meltygui.editor.live_view_views import _line_in_selection
    from meltygui.editor.live_view_views import _marker_idle_skip
    from meltygui.editor.live_view_views import _node_owns_function
    from meltygui.editor.live_view_views import _parse_line_band
    from meltygui.editor.live_view_views import _scope_function
    from meltygui.editor.live_view_views import _snap_line_to_label
    from meltygui.editor.live_view_views import _snap_line_to_text
    from meltygui.editor.live_view_views import _stable_key_name
    from meltygui.editor.live_view_views import _store_key_names
    from meltygui.editor.live_view_views import _symbol_cols
    from meltygui.editor.live_view_views import _token_in_selection
    from meltygui.editor.live_view_views import is_volume

    if not _is_funcdef_node(node) or span is None:
        return
    from meltygui.core.runtime.toggles import Toggles
    live_store = kwargs.get("live_store")
    if live_store is None and not Toggles.TextEditor.enable_live_view:
        return
    if live_store is not None:
        # PASSED-IN store (draw_text's live_store=, e.g. the stack trace
        # window): no global resolution at all - the caller computed the
        # values locally (live_view.frame_value_store), and this overlay
        # reads only what it was handed. Act on exactly the def the store
        # was built for: name match (rules out enclosing defs, whose spans
        # also contain the target's lines) + the store's def line inside
        # this node's span (rules out unrelated same-named defs).
        from meltygui.code.libcst_conversion import parse_def_name
        _def_line = getattr(live_store, "__def_line__", None)
        if (_def_line is None
                or parse_def_name(node) != getattr(live_store, "__name__", None)
                or not (span.start_line + line_offset <= _def_line
                        <= getattr(span, "end_line", span.start_line)
                        + line_offset)):
            return
        fn = live_store
    else:
        filename = (getattr(root, "file_path", None)
                    or getattr(getattr(root, "address", None), "path", None)
                    or getattr(jump_to, "path", None))
        if filename is None:
            return
        fn = _scope_function(str(filename), span.start_line + line_offset)
        if fn is None:
            return
        # A NESTED def resolves to its ENCLOSING function (closures attach
        # their captures to the outer store and are no module var, so the
        # nearest module-level def at or above the line wins). Its keys are
        # line-keyed and _key_anchor reads the line alone, so this node's
        # overlay would repaint every visible key of the outer store with
        # the SAME view as the outer node's overlay already used - the
        # red "ID ..." duplicate label over the outer body (09-02). Only the
        # node that IS the resolved function's own def draws it.
        if not _node_owns_function(node, fn, span, line_offset):
            return
        # Store-level registration before any markers exist: the first
        # instrumented run's brand-new keys invalidate this editor, the
        # overlay re-runs, and the markers materialize (closed - these
        # auto_open=False boxes wait for a click). Without it the first run
        # stays invisible until an instrument happens. (A passed-in store is
        # a static snapshot - nothing will ever publish to it, so no watch.)
        watch(fn, None, draw_state)

    # Parse→buffer line bridge (dispatch passes it while a merge is in
    # flight): the given y is already mapped, so deriving origin from the
    # MAPPED start keeps origin == buffer line 1; each anchor then maps
    # individually - lines above an edit stay, lines below shift, lines
    # within the changed region skip the frame. Content lookups keep the
    # PARSE-space line: outside the changed region both texts hold the
    # identical line, by construction of the diff.
    _lmap = kwargs.get("line_map")
    _sl = _lmap(span.start_line) if _lmap else span.start_line
    if _sl is None:
        return
    origin_y = y - (_sl - 1) * line_px
    origin_x = x - getattr(span, "start_col", 0) * char_w
    _src = getattr(root, "source", "") or ""
    # Viewport cull bounds: the store can have a marker per binding in the
    # def (frame snapshots publish the whole scope), and the walk visits the
    # scope regardless of scroll - every off-screen marker skipped here is a
    # full render_func call saved per frame. Latched value windows persist
    # via root_draw_states without a marker, same as when the whole editor
    # scrolls away.
    _clip = getattr(draw_state, "abs_clip_rect", None)
    if live_store is not None:
        # PASSED-IN store (the stack trace view's panes): the pane is a
        # BOUNDED span whose tile bakes once and then blitted while the
        # PARENT scrolls - its own body doesn't re-run per scroll frame,
        # so a clip cull here baked only the then-visible band's markers
        # and they popped in/out as the scroll crossed rows (08-31;
        # re-confirmed 09-01: with the cull, draw_text's 5.6k-line pane
        # showed gutter markers only for the first ~180 lines). Render
        # every marker - so the per-key scan below must stay cheap: for
        # that pane it runs 5k+ regex scans every selection frame.
        _clip = None
    # Snap memo: the label/content relocation scans are O(def lines) per
    # STALE stamp - with frame snapshots holding a key per occurrence that's
    # hundreds of the scans per repaint if run hot. Snap results only
    # change when the text changes, so memoize per (stamp, label) against
    # the source OBJECT - identity is the content-free change signal (a
    # reparse builds a new string; edits-in-flight are the _lmap's job).
    # Per-EDITOR (this draw_state) because the same scope can be overlaid from
    # several editors at once (same def in two tiles), each with its own
    # source object - a shared memo would ping-pong between their sources
    # and rescan every stamp every frame. Raw-written like the other editor
    # memo caches (_anc_scroll_cache): @live's __setattr__ would run a
    # value != original_value compare on full value-carrying tuples.
    # The tuple also carries the module source's line split: splitting the
    # WHOLE file per FunctionDef per frame is ~2/3 of draw_text in profiling
    # - same invalidation (source object identity), so it rides the memo.
    _memo_ent = draw_state.__dict__.get("_lv_snap_memo")
    if _memo_ent is None or _memo_ent[0] is not _src or len(_memo_ent) < 3:
        _memo_ent = (_src, {}, _src.split("\n"))
        object.__setattr__(draw_state, "_lv_snap_memo", _memo_ent)
    _snap_memo = _memo_ent[1]
    source_lines = _memo_ent[2]
    # Exit-line washes: where the last instrumented run CAME OUT.
    # __live_return_line__ (stamped by live_view.twin_ret / the body-capture
    # profile hook) washes green; __live_error_line__ ((line, msg, text),
    # stamped
    # by live_instrument._stamp_error_line when the run raised) washes red
    # with the message in a wrapped box flush above the line, right-aligned -
    # the live-run twin of the editor's routed error markers. Both are absolute file coords, mapped
    # through the same parse→buffer bridge as the anchors; both cleared at
    # run start, so a rerun never shows the previous run's exit. A couple of
    # attribute accesses + at most two rects per frame.
    _exit_marks = []
    _ret_mark = getattr(fn, "__live_return_line__", None)
    if _ret_mark:
        _rline, _rtext = (_ret_mark if isinstance(_ret_mark, tuple)
                          else (_ret_mark, None))
        _exit_marks.append((_rline, None, _rtext,
                            (0.157, 0.824, 0.31, 0.16)))
    _err_mark = getattr(fn, "__live_error_line__", None)
    if _err_mark:
        _exit_marks.append((_err_mark[0], _err_mark[1],
                            _err_mark[2] if len(_err_mark) > 2 else None,
                            (0.824, 0.157, 0.157, 0.22)))
    for _ml_line, _ml_msg, _ml_text, _ml_col in _exit_marks:
        # Same follow-the-code snap as the markers: the stamp is run-time
        # coordinates, so if edits moved the statement, re-find it by its
        # stamped CONTENT inside the def before mapping to buffer space.
        _mk = ("exit", _ml_line, _ml_text)
        _rl = _snap_memo.get(_mk)
        if _rl is None:
            _rl = _snap_line_to_text(_ml_text, _ml_line - line_offset,
                                     source_lines,
                                     span.start_line, span.end_line)
            _snap_memo[_mk] = _rl
        _rlm = _lmap(_rl) if _lmap else _rl
        if _rlm is None or _rlm < 1:
            continue
        _ry = origin_y + (_rlm - 1) * line_px
        if _clip is not None and (_ry + line_px < _clip[1]
                                  or _ry > _clip[3]):
            continue
        _rdl = imgui.get_window_draw_list()
        _cw = getattr(draw_state, "content_width", 800.0)
        _rdl.add_rect_filled(
            origin_x - 4.0, _ry, origin_x + _cw, _ry + line_px,
            pack_color(*_ml_col))
        if _ml_msg:
            # Same treatment as the editor's parse-error box (text_editor's
            # draw_text): a wrapped, capped-width box sitting flush ABOVE the
            # line, right-aligned, so the box never covers the code.
            _pad_x, _pad_y, _margin = 6, 4, 6
            _bx1 = (_clip[2] if _clip is not None
                    else origin_x + _cw) - _margin
            _max_w = min(420.0, max(80.0, (_bx1 - origin_x) - 2 * _pad_x))
            _ts = imgui.calc_text_size(_ml_msg, False, _max_w)
            _bx0 = _bx1 - (_ts.x + 2 * _pad_x)
            _by1 = _ry
            _by0 = _by1 - (_ts.y + 2 * _pad_y)
            if _clip is not None and _by0 < _clip[1] + _margin:
                _by0 = _ry + line_px          # no room above - box below
                _by1 = _by0 + _ts.y + 2 * _pad_y
            _rdl.add_rect_filled(
                _bx0, _by0, _bx1, _by1,
                pack_color(0.275, 0.118, 0.157, 0.922), 4.0)
            _rdl.add_rect(
                _bx0, _by0, _bx1, _by1,
                pack_color(0.588, 0.235, 0.275, 1.0), 4.0)
            _save_cursor = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((_bx0 + _pad_x, _by0 + _pad_y))
            imgui.push_text_wrap_pos(imgui.get_cursor_pos_x() + _max_w)
            imgui.text_colored(_ml_msg, 1.0, 0.72, 0.68, 1.0)
            imgui.pop_text_wrap_pos()
            imgui.set_cursor_screen_pos(_save_cursor)
    _sot0 = time.perf_counter()
    _soc = [0, 0, 0, 0]   # keys seen, culled(index+clip), idle-skipped, drawn
    _snap_vals = live_values_for(fn)
    _soc[0] = len(_snap_vals)

    # Line-bucketed anchor index: resolving an anchor per key per frame is
    # O(store) - a frame-snapshotted big def holds THOUSANDS of keys, nearly
    # all off-viewport, and during an edit burst each also paid an _lmap
    # call (this was the measured 12-16ms/frame). Anchors only move when the
    # source or the key set changes, so resolve them ONCE into a sorted
    # (rel_line, key) list and bisect the visible band per frame. rel_line
    # is pre-_lmap (parse space); the 64-line slack on the band covers any
    # plausible in-buffer region shift until the reparse rebuilds _src (which
    # rebuilds the index - same identity signal as _snap_memo).
    _ik = (len(_snap_vals), line_offset)
    _ie = draw_state.__dict__.get("_lv_key_index")
    # The function is part of the identity; a def view's exec-fn runs
    # publish to a FRESH function object per run, so a stale index would
    # iterate the PREVIOUS run's key paths against the new values - every
    # lookup None, rendered as captured markers (the phantom "second set"),
    # whose None-routed windows then collide with the real ones. Weakref so
    # the index never pins a replaced run's function (id() of a dead object
    # could recycle).
    if (_ie is None or _ie[0] is not _src or _ie[1] != _ik
            or len(_ie) < 8 or _ie[4]() is not fn):
        # Append + ONE sort (C-speed): the first version insort-ed each key
        # (list.insert, O(n) memmove → O(n²) per rebuild), and a publish
        # storm - a stack-trace snapshot landing thousands of keys with
        # renders interleaved - meant a rebuild per render. That froze
        # the editor the moment a stack was published.
        _pairs = []
        _ilabels = getattr(fn, "__live_labels__", None) or {}
        _cols_memo = draw_state.__dict__.get("_lv_cols_memo")
        if _cols_memo is None or len(_cols_memo) > 200000:
            _cols_memo = {}
            object.__setattr__(draw_state, "_lv_cols_memo", _cols_memo)
        for key_path in _snap_vals:
            if not key_path or not isinstance(key_path[-1], str):
                continue
            if key_path[-1].split("#", 1)[0] == "live_view()":
                continue  # anchored by its own call-token marker
            _a = _key_anchor(node, key_path, line_offset)
            if _a is None:
                continue
            _rl = _a[0]
            if _a[2] is None:
                tail = key_path[-1]
                _mk = ("label", tail)
                _snapped = _snap_memo.get(_mk)
                if _snapped is None:
                    # Label→line index, built ONCE per source version: after
                    # an edit shifts lines, EVERY stored label mismatches at
                    # the next reparse, and the old per-key def-wide regex
                    # scan cost keys × def-lines (2640 × 2900 ≈ 9.6 SECONDS,
                    # the post-edit baseline). With the index each key is a
                    # dict hit + nearest-line bisect.
                    _lidx = _snap_memo.get(("lidx",))
                    if _lidx is None:
                        _lidx = _label_line_index(
                            source_lines, span.start_line, span.end_line)
                        _snap_memo[("lidx",)] = _lidx
                    label = ((getattr(fn, "__live_labels__", None) or {})
                             .get(key_path)
                             or (tail.split("#", 1)[1] if "#" in tail else None))
                    _snapped = _snap_line_to_label(
                        label, _rl, source_lines,
                        span.start_line, span.end_line, lidx=_lidx)
                    _snap_memo[_mk] = _snapped
                _rl = _snapped
            _cols = _symbol_cols(_a, _rl, key_path, source_lines, _ilabels,
                                 memo=_cols_memo)
            if _cols is None:
                continue
            _pairs.append((_rl, key_path, (_rl, _cols[0], _cols[1])))
        _pairs.sort(key=lambda p: p[0])
        # Stable hash-free cache for the whole snapshot, built WITH the index
        # (same invalidation) - per-key naming was O(store) hash → O(store²)
        # per pass on frame-snapshot stores.
        # The resolved anchor rides with its key: _key_anchor is a
        # locals-walk per key, and re-running it per line per frame
        # was O(visible band) of dict descents for every repaint.
        _ie = (_src, _ik, [p[0] for p in _pairs], [p[1] for p in _pairs],
               weakref.ref(fn), None, [p[2] for p in _pairs],
               {p[1]: i for i, p in enumerate(_pairs)})
        object.__setattr__(draw_state, "_lv_key_index", _ie)
    _ilines, _ikeys, _ianchors = _ie[2], _ie[3], _ie[6]
    # Names from the SHARED generation-keyed store map - never the anchor
    # index's own cache; differently-stale name maps across consumers
    # minted duplicate value IDs (see _stable_key_names).
    _skey_names = _store_key_names(fn)
    if (_clip is not None
            and getattr(draw_state, "_lv_full_overlay_until", 0)
            <= Core.melty.frame_count):
        _blo, _bhi = _parse_line_band(_lmap, _clip, origin_y, line_px)
        _i0 = bisect.bisect_left(_ilines, _blo)
        _i1 = bisect.bisect_right(_ilines, _bhi)
        _cand = _ikeys[_i0:_i1]
        _cand_anchors = _ianchors[_i0:_i1]
        _soc[1] = len(_ikeys) - len(_cand)
    else:
        # Post-run forward pass renders EVERY key in the loop - the per-key
        # cull below still drops off-viewport keys unless their marker has
        # an OPEN window (rendered at the frozen anchor, which forwards the
        # actual value into the window's draw_any).
        _cand = _ikeys
        _cand_anchors = _ianchors

    # Binding-value pills, built by the marker loop and painted by the
    # trailing-gap pass: (display 0, boundary buffer col, "=value",
    # symbol length) - usage-label pairs, one per captured target.
    #
    # IDLE-PASS MEMO (the no-clip pane below: every key, every frame). An
    # idle key's whole per-frame outcome is its gutter registration and
    # its pill, and both only change with the index, a publish, the
    # geometry or the focus - all in `_pk`. On a memo hit only the ACTIVE
    # keys run the per-key logic: keys non-idle last pass (hovered, open,
    # caret-inside - they must observe their state), keys on the mouse's
    # line (for hover could begin), keys on an exact single-line selection
    # (`_token_in_selection`); everything else replays. draw_text's
    # context-menu pane: 5,145 keys → 70 ms a selection frame before.
    _sel_lo, _sel_hi = kwargs.get("sel_lo"), kwargs.get("sel_hi")
    _col_shift = kwargs.get("col_shift", 0)
    _fc = Core.melty.frame_count
    _full_pass = getattr(draw_state, "_lv_full_overlay_until", 0) > _fc
    try:
        _pub_gen = vars(fn).get("__live_pub_gen__", 0)
    except TypeError:
        _pub_gen = 0
    # The line map is either None, the fold projection (a fresh lambda per
    # frame carrying the layout list in `_d2b` - see draw_text's
    # _get_fold_lm; the list only changes on a fold toggle, so it is the
    # identity the memo keys on) or an edit bridge (a change in flight - no
    # memo, every frame is a potential change).
    _layout = getattr(_lmap, "_d2b", None) if _lmap is not None else None
    _lm_key = getattr(_lmap, "_lv_key", None) if _lmap is not None else None
    _pk = (id(_ie), _pub_gen, origin_x, origin_y, line_px, char_w,
           _col_shift, Core.melty.text_focused_ds is draw_state,
           _lm_key)
    _record = (_clip is None and not _full_pass
               and (_lmap is None or _lm_key is not None))
    _pm = draw_state.__dict__.get("_lv_pass_memo")
    _replay = (_record and _pm is not None and len(_pm) >= 7
               and _pm[0] == _pk)
    _bind_pills = []
    if _replay:
        _idle_gutter, _pills, _nonidle = _pm[1], _pm[2], _pm[3]
        _static_gutter, _static_pills = _pm[5], _pm[6]
        _active = set(_nonidle)

        def _mark_display_line(dl):
            # display line (1-based) → parse line: identity without folds,
            # else through the fold layout (0-based parse line per
            # display line); hidden / out-of-range lines have no keys.
            if _layout is None:
                rel = dl
            elif 0 <= dl - 1 < len(_layout):
                rel = _layout[dl - 1] + 1
            else:
                return
            _active.update(_ikeys[bisect.bisect_left(_ilines, rel):
                                  bisect.bisect_right(_ilines, rel)])
        if line_px:
            _mdl = int((imgui.get_io().mouse_pos.y - origin_y) / line_px) + 1
            for _dl in (_mdl - 1, _mdl, _mdl + 1):
                _mark_display_line(_dl)
        if (_sel_lo is not None and _sel_hi is not None
                and _sel_lo[0] == _sel_hi[0]):
            _mark_display_line(_sel_lo[0])
        # Replay the idle keys' gutter registration (same per-frame reset
        # the loop body / idle skip do) and their pills - from the memo's
        # STATIC structures, not a rebuild (5k entries a frame): the
        # gutter map is a ChainMap over a fresh front dict (the active
        # keys' markers get a filtered copy there so their own registration
        # can't duplicate the static entry); the pill list is the memo's
        # own object (copied only when an active key changes it - its
        # identity keys _stamp_and_paint's merge memo).
        _pos = _ie[7]
        if getattr(draw_state, "_lv_gutter_frame", None) != _fc:
            draw_state._lv_gutter_frame = _fc
            _front = {}
            for _k in _active:
                _ig = _idle_gutter.get(_k)
                if _ig is not None and _ig[0] not in _front:
                    _front[_ig[0]] = [m for m in _static_gutter.get(_ig[0], ())
                                      if m is not _ig[1]]
            draw_state._lv_gutter_markers = collections.ChainMap(
                _front, _static_gutter)
        else:
            _gm = draw_state._lv_gutter_markers   # another frame for first
            for _k, (_gl, _gmds) in _idle_gutter.items():
                if _k not in _active:
                    _gm.setdefault(_gl, []).append(_gmds)
        _bind_pills = _static_pills
        for _k in _active:
            _p = _pills.get(_k)
            # A key non-idle when the memo was taken has no static pill
            # (it re-adds its own below); an idle one's is pulled so its
            # re-add can't duplicate it.
            if _p is not None and _k not in _nonidle:
                if _bind_pills is _static_pills:
                    _bind_pills = list(_static_pills)
                try:
                    _bind_pills.remove(_p)
                except ValueError:
                    pass
        _loop = [(k, _ianchors[_pos[k]]) for k in _active if k in _pos]
        _soc[2] = len(_ikeys) - len(_loop)
    else:
        _idle_gutter, _pills = {}, {}
        _static_pills = None
        _loop = zip(_cand, _cand_anchors)
    _new_nonidle = set()
    _mutated = not _replay
    _created = 0
    _budget = int(Toggles.TextEditor.live_marker_create_budget)
    for key_path, anchor in _loop:
        value = _snap_vals.get(key_path)
        rel_line, start_col, end_col = anchor
        _ml = _lmap(rel_line) if _lmap else rel_line
        if _ml is None:
            continue        # anchor inside the mid-edit region - skip a frame
        _my = origin_y + (_ml - 1) * line_px
        _frozen_pos = None
        if _clip is not None and (_my + line_px < _clip[1] or _my > _clip[3]):
            # Post-run forward pass (_lv_full_overlay_until): an off-viewport
            # key whose marker has an OPEN value window still renders once -
            # at the marker's LAST stamped location, NOT its true off-screen
            # spot (the pinned window would park at _pinned_base_y's screen-
            # bottom clamp and "disappear"). Everything else stays culled.
            _fmds = None
            if (getattr(draw_state, "_lv_full_overlay_until", 0)
                    > Core.melty.frame_count):
                _mreg = getattr(draw_state, "_lv_marker_ds", None)
                _fmds = _mreg.get(
                    f"lvs::{fn.__qualname__}"
                    f"::{_skey_names.get(key_path) or _stable_key_name(key_path, _snap_vals)}"
                ) if _mreg else None
                _fw = getattr(_fmds, "_lv_window_ds", None) if _fmds else None
                if _fw is None or _fw.closed:
                    _fmds = None
            if _fmds is None:
                # TEMP diag: an off-viewport key skipped DURING an active
                # full pass means its open window didn't reflect the run's
                # value - name why (no marker ds for the expected key, or
                # its window closed/missing).
                if (getattr(draw_state, "_lv_full_overlay_until", 0)
                        > Core.melty.frame_count):
                    from meltygui.core.diagnostics.perf_trace import trace as _ptr
                    _mreg2 = getattr(draw_state, "_lv_marker_ds", None) or {}
                    _mk2 = (f"lvs::{fn.__qualname__}::"
                            f"{_skey_names.get(key_path) or _stable_key_name(key_path, _snap_vals)}")
                    _ptr("lv full-pass skip", key=_mk2,
                         have_marker=_mk2 in _mreg2,
                         reg_keys=len(_mreg2))
                _soc[1] += 1
                continue    # off-viewport - don't draw a marker for it
            _frozen_pos = (_fmds.abs_left, _fmds.abs_top)
        pad = 2.0
        # Selection containment: _ml is in buffer-space, cols are the
        # boxed symbol span - same test the call-token overlay does.
        cursor_inside = _token_in_selection(
            _ml, start_col, end_col, _sel_lo, _sel_hi)
        _snm = (f"lvs::{fn.__qualname__}"
                f"::{_skey_names.get(key_path) or _stable_key_name(key_path, _snap_vals)}")
        _sao = (bool(Toggles.TextEditor.live_auto_open_volumes)
                and is_volume(value) and key_path not in
                (getattr(fn, "__frame_snapshot_keys__", None) or ()))
        # An inline-labeled marker (simple builtin value) paints NOTHING
        # itself: its pill is tracked here (_bind_pills) and painted raw
        # by _stamp_and_paint after the gap draw_text laid out. So an idle
        # one skips its @render_wrapper call like any other marker - once the
        # body has run at least once with the inline flag (the gutter glyph
        # and _lv_open button happen there; `_lv_inline` tracks it) - with
        # the body's full per-key watch replicated so a publish still
        # repaints the pill. Before this every visible captured binding of
        # a frame snapshot paid a full wrapper call per line: a big def
        # could draw_text (one binding on most lines) froze the editor.
        _btext = _inline_value_text(value)
        _pill = None
        if _btext is not None:
            _brgba = _inline_swatch_rgba(value)
            _pill = (_ml - 1, end_col + _col_shift,
                     "=" + (_SWATCH_HOLE if _brgba is not None else "") + _btext,
                     max(1, end_col - start_col),
                     (_brgba, 1) if _brgba is not None else None)
        _mreg0 = draw_state.__dict__.get("_lv_marker_ds")
        _mds0 = _mreg0.get(_snm) if _mreg0 else None
        if (_frozen_pos is None
                and (_btext is None
                     or (_mds0 is not None
                         and getattr(_mds0, "_lv_inline", None) is True))
                and _marker_idle_skip(
                    draw_state, _snm,
                    origin_x + start_col * char_w - pad, _my - pad,
                    max(1, end_col - start_col) * char_w + 2 * pad,
                    line_px + 2 * pad, True, cursor_inside,
                    fn, key_path, _ml - 1, _sao)):
            if _pill is not None:
                watch(fn, key_path, _mds0)
                if _bind_pills is _static_pills:
                    _bind_pills = list(_static_pills)
                _bind_pills.append(_pill)
            if _record:
                if (_idle_gutter.get(key_path) != (_ml - 1, _mds0)
                        or _pills.get(key_path, _NO_VALUE) != _pill):
                    _mutated = True
                _idle_gutter[key_path] = (_ml - 1, _mds0)
                _pills[key_path] = _pill
            _soc[2] += 1
            continue
        if _mds0 is None and _frozen_pos is None and _budget > 0:
            # FIRST render of this marker (no draw_state yet) - a wrapper
            # call + DrawState + comment resolve each. A diff expand can
            # reveal thousands at once (15 s in one case, 09-01), so at
            # most `_budget` are created per pass; the rest stay pending
            # (nonidle state → active next pass) and the pills show now.
            if _created >= _budget:
                _new_nonidle.add(key_path)
                if _pill is not None:
                    if _bind_pills is _static_pills:
                        _bind_pills = list(_static_pills)
                    _bind_pills.append(_pill)
                if _record:
                    _mutated = True
                    _pills[key_path] = _pill
                if _created == _budget:
                    _created += 1
                    from meltygui.core.windowing.glfw_utils import request_render
                    draw_state.invalidate()
                    request_render()
                continue
            _created += 1
        _soc[3] += 1
        _new_nonidle.add(key_path)
        if _record:
            if key_path in _idle_gutter or _pills.get(key_path, _NO_VALUE) != _pill:
                _mutated = True
            _idle_gutter.pop(key_path, None)
            _pills[key_path] = _pill
        # Frozen-anchor path: the marker draws at its previous position so
        # its pinned window doesn't chase the true off-screen coords.
        # Auto-open the VOLUMES (3-D tensors → orbiting voxel windows) only
        # when live_auto_open_volumes is enabled - off by default: with loop
        # accumulation stacking per-layer tensors into volumes, a run would
        # pop one window per captured tensor. Scalars/configs always stay
        # as click-to-open boxes so 19 locals don't bury the code.
        # Frame-snapshot keys (context-menu capture — tracked in
        # __frame_snapshot_keys__) never auto-open: opening a menu on a
        # widget must not spawn a window per captured tensor.
        # code_tree_node carries the scope dict: the marker reads the site's
        # comment dict from it and splats it 1:1 onto the value window's
        # draw_any (never onto the marker's own wrapper — show_bg=True with a
        # dark tint would paint an opaque bg over the very symbol it boxes).
        # The simple captured value renders EXACTLY like a usage label: the
        # gap opens right after the boxed target symbol and the line reads
        # `edited=False, new_text='...' = get_text(...)` - code repositions,
        # nothing is covered. Collected here, stamped + painted by
        # _draw_usage_labels below (sym_len drives the gap-derived ring).
        if _pill is not None:
            if _bind_pills is _static_pills:
                _bind_pills = list(_static_pills)
            _bind_pills.append(_pill)
        _draw_marker_at(
            draw_state,
            _frozen_pos if _frozen_pos is not None
            else (origin_x + start_col * char_w - pad, _my - pad),
            cursor_inside, (_ml, start_col, end_col),
            "/".join(map(str, key_path)), value=value,
            captured=True, store_obj=fn, key_path=key_path,
            width=max(1, end_col - start_col) * char_w + 2 * pad,
            height=line_px + 2 * pad,
            code_tree_node=node.get("locals") if isinstance(node, dict) else None,
            buffer_line=_ml - 1, name=_snm, auto_open=_sao,
            inline_values=True,
            in_selection=_line_in_selection(_ml, _sel_lo, _sel_hi),
            caret_line=kwargs.get("caret_line"), def_node=node)
    if _record:
        if _mutated or _replay and _new_nonidle != _nonidle:
            # Rebuild the static replay structures (a key changed state).
            _static_gutter = {}
            for _k, (_gl, _gmds) in _idle_gutter.items():
                _static_gutter.setdefault(_gl, []).append(_gmds)
            _static_pills = [p for _k, p in _pills.items()
                             if p is not None and _k not in _new_nonidle]
        # `_lmap` (and what it was built from) rides along so the ids in
        # _pk stay pinned.
        object.__setattr__(draw_state, "_lv_pass_memo",
                           (_pk, _idle_gutter, _pills, _new_nonidle, _lmap,
                            _static_gutter, _static_pills))
    # Inline USAGE labels: `seq_len=384` inserted after every later
    # occurrence of a captured symbol - the code text is SHIFTED to make
    # room (display-time only - see live_usage + the positional-trail
    # machinery in draw_text's _window/_build_vcols).
    try:
        _draw_usage_labels(draw_state, fn, node, span, source_lines,
                           _snap_vals, _ilines, _ikeys, origin_x, origin_y,
                           char_w, line_px, _lmap, _clip,
                           kwargs.get("col_shift", 0),
                           binding_pills=_bind_pills)
    except Exception as e:
        print(f"live_view: usage labels failed: {e!r}", file=sys.stderr)
    # TEMP perf: one line per slow enough pass (keys=store size for this
    # def, culled=off-viewport, idle=fast-id skips, drawn=full wrapper calls).
    _soms = (time.perf_counter() - _sot0) * 1000.0
    if _soms >= 2.0:
        from meltygui.core.diagnostics.perf_trace import trace as _sotrace
        _sotrace("snapshot_overlay", fn=getattr(fn, "__qualname__", "?"),
                 ms=round(_soms, 1), keys=_soc[0], culled=_soc[1],
                 idle=_soc[2], drawn=_soc[3])


@render_func(use_cache=False, show_bg=False, shadow=False, selectable=False,
             with_header=None, show_name=False)
def draw_function_live(input_value, draw_state=None, unique=None,
                       source_mode=None, column_edges=None, run_in_thread=True,
                       **kwargs):
    """draw_function with transparent instrumentation, source side by side:
    the instrumented twin (live_instrument) runs AUTOMATICALLY — on first
    view, on every hotswap (id(fn.__code__) is the auto_run token, so a save
    in the editor compiles AND runs in one go), and on param edits — every
    assignment publishes one snapshot to the ORIGINAL function's store, and
    the editor column shows the ORIGINAL code with the snapshot overlay
    anchoring each captured value at its line.

    `run_in_thread=True` (the default) runs the twin on a worker so a long
    pass (live_view_forward's full forward pass) never blocks the render
    loop. The live views need no special handling for this: each publis
    invalidates its watcher draw_states from the worker and wakes the loop
    (the terminal-reader pattern in live_view._notify_watchers), and all
    window rendering / voxel uploads happen on the GL thread next frame.

    `source_mode` picks the source column's route: FILE_TREE (the default)
    is the text-only editor; NEW_CODE is the full code_file_io display —
    the draw_collection structured pane and the live-overlay text pane at
    the same time (live_view_forward uses it).

    Ctrl+Enter over the lab presses the Run button (request_run) —
    overriding the global Ctrl+Enter's Pending-Saves-window flow while the
    mouse is here. The run itself already executes the latest source: the
    twin compiles from the pending in-memory text."""
    from meltygui.editor.live_view_views import _run_proxy
    from meltygui.editor.live_view_views import request_run

    fn = input_value
    try:
        fn = inspect.unwrap(fn)
    except Exception:
        pass
        
    if not callable(fn) or getattr(fn, "__code__", None) is None:
        imgui.text("draw_function_live: needs a plain function")
        return False, input_value

    # Ctrl+Enter over the lab: press the Run button. registered BLOCKING
    # with a priority well above draw_main's non_blocking root handler
    # (512 - but any on-screen depth is < 512, so this always sorts first),
    # which stops the event chain at this view: the global re-save-all
    # window flow never fires while the mouse is here. This hook only
    # covers frames where the body renders; the blit-cached half is
    # draw_main's root BVH fallback → request_run (dual dispatch).
    if draw_state.on_action("ctrl_enter_down", priority_delta=1024):
        request_run(draw_state)

    from meltygui.view.code_view import draw_function
    from meltygui.core.rendering.render_dispatch import draw_any
    from meltygui.core.layout.column_core import ColumnLayout
    from meltygui.core.layout.column_core import MIN_ROW_HEIGHT
    from meltygui.core.rendering.mode import Mode
    # Runner | source: a shared edge system (ColumnLayout): the divider is
    # a draggable line in the window's flat collision solve, and each column
    # manages its own height - no _columns_top capture to race with
    # cache-skipped siblings. Columns pin to the visible viewport so long
    # functions clip inside their cell instead of growing past the window
    # bottom. A NEW_CODE source column nests its own structured+text
    # ColumnLayout inside cell 1; the cell's edge dicts pass down with the
    # call (left_edge/right_edge, by reference - code_file_io forwards them
    # like jump_to), so the nested row's far edges ARE this row's divider and
    # right edge and can never drift apart from them.
    top_y = draw_state.abs_top + 0
    imgui.set_cursor_screen_pos((draw_state.abs_left, top_y))
    cols = ColumnLayout(draw_state, 2, column_edges=column_edges,
                        column_widths=[244])
    clip = cols.clip if cols.clip is not None else draw_state.abs_clip_rect
    avail = (max(MIN_ROW_HEIGHT, clip[3] - cols.top) if clip is not None
             else 400.0)
    inner_h = avail - 2 * cols.padding
    with cols.cell(0, height=avail) as col_w:
        draw_function(_run_proxy(fn), height=inner_h, width=col_w, temp=True,
                      name=f"{fn.__name__} runner", run_in_thread=run_in_thread, rounding=None)
    with cols.cell(1, height=avail) as col_w:
        draw_any(fn, mode=source_mode or Mode.FILE_TREE, height=inner_h,
                 width=col_w, name=f"{fn.__name__} live source",
                 left_edge=cols.edges[1], right_edge=cols.edges[2])
    cols.finish()
    return False, input_value


@render_func
def draw_source_preview(input_value=None, draw_state=None, preview: SourcePreviewState = None):
    from meltygui.view.text_view import draw_text
    from meltygui.core.melty import Melty
    from meltygui.code.new_converters import code_hosts_for
    if input_value is not None:
        preview.path, preview.line, preview.token = input_value
    if preview.path is None:
        return False, input_value
    path = Path(preview.path)
    text = Melty.read_code(path)
    if text is None:
        text = ''
    code_dict, dict_host = None, None
    if path.suffix == '.py':
        _, dict_host = code_hosts_for(path)
        code_dict = dict_host._held()
    from meltygui.editor.source_ui import _RowSpan
    _, _, pane = draw_text(text, name='source', editable=False, code_dict=code_dict, return_extras=True,
              jump_to=_RowSpan(0, path), show_header=False, width=draw_state.width,
              height=draw_state.height, show_widgets=True)
    if preview.line is not None and pane is not None and text:
        from meltygui.editor.text_editor import fold_project_jump
        lines = text.split('\n')
        row = max(0, min(int(preview.line) - 1, len(lines) - 1))
        offset = sum(len(line) + 1 for line in lines[:row])
        if preview.token and preview.token in lines[row]:
            offset += lines[row].index(preview.token)
        offset, row = fold_project_jump(pane, text, offset, row)
        pane.text_cursor_pos = offset
        pane.text_selection_start = pane.text_selection_end = offset
        pane.invalidate()
        preview.line = None
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
    return False, input_value


def draw_pending_preview():
    import meltygui.editor.source_preview

    request, meltygui.editor.source_preview._pending = meltygui.editor.source_preview._pending, None
    draw_source_preview(request, name='Source preview', closable=True,
                        open_requested=request is not None, width=900, height=650)


@render_func(use_cache=True, show_bg=True, shadow=True, selectable=False, temp=True,
             closable=True, melty_window=False, auto_resize=False, with_header=None,
             min_width=Toggles.UsagePicker.min_width, swoosh=False, min_height=Toggles.UsagePicker.min_height,
             enforce_max_height=True,   # the content height cap holds mid-drag
             is_default_for=UsagePickerModel, tint=(0.071, 0.354, 0.511))
def draw_usage_picker(input_value: UsagePickerModel, draw_state,
                      row_height=Toggles.UsagePicker.row_height, row_gap=Toggles.UsagePicker.row_gap, tree_indent=16.0,
                      code_font=Font.FONTAWESOME_MONO_19, collapsible=False,
                      group_tint=None, change_kinds=None, **kwargs):
    """Paint the picker rows Code-tab style. Hover moves the highlight only
    while the pointer MOVES over the window (a resting pointer never steals
    the keyboard cursor); a click on a row sets `model.picked` for draw_text
    to consume. Returns (True, model) on a pick."""
    from meltygui.editor.usage_picker import paint_usage_rows

    return paint_usage_rows(
        input_value, draw_state, row_height=row_height, row_gap=row_gap,
        tree_indent=tree_indent, code_font=code_font, collapsible=collapsible,
        group_tint=group_tint, change_kinds=change_kinds)


@render_func(is_default_for=types.ModuleType, use_cache=True,
             show_bg=True, with_header=draw_header, with_footer=draw_footer)
def draw_module(input_value: types.ModuleType, draw_state, **kwargs):
    imgui.text(f"Module: {input_value.__name__}")


@render_func(is_default_for=(type), tint=(0.928, 0.836, 0.655, 0.308), use_cache=True,
             header_single_line=True, show_name=True, temp=True, is_tree=False, shadow=False,
             show_bg=True, with_header=draw_header)
def draw_type_name(input_value, **kwargs):
    try:
        if isinstance(input_value, str):
            imgui.text(f"{input_value}")

        else:
            imgui.text(f"{input_value.__name__}")
    except Exception as e:
        imgui.text(f"Error displaying type: {e}")


@render_func(use_cache=True, is_default_for=SymbolUsage)
def draw_symbol_usage(input_value):
    imgui.text(str(input_value))


@render_func(is_default_for=(property))
def draw_property(input_value: property, draw_state, **kwargs):
    imgui.text_colored(f"Property: {input_value.fget.__name__}", 1.0, 0.5, 0.0, 1.0)


@render_func(show_bg=True, align_header=False, use_cache=True, shadow=False,
             with_header=draw_header)
def draw_type(input_value: type, **kwargs):
    from meltygui.view.collection_view import draw_collection

    try:
        class_vars = {**{k: getattr(input_value, k) for k in vars(input_value)}}

        changed, new_dict = draw_collection(class_vars, real_type=input_value, disable_scroll=True,
                                            name=f"Class: {input_value.__name__}")

        if changed:
            for k, v in new_dict.items():
                if k.startswith("_"):
                    continue
                try:
                    imgui.text(f"Setting attribute {k} to value {v} on class {input_value.__name__}")
                    setattr(input_value, k, v)
                except Exception as e:
                    imgui.text(f"Error setting attribute {k} on class {input_value.__name__}: {e}")
    except Exception as e:
        imgui.text(f"Error rendering type {input_value}: {e}")


@render_func(is_default_for=UsageRef, use_cache=True, shadow=True, z_offset=2, show_bg=True, with_header=draw_header,
             is_tree=True, tint=(0.11, 0.1, 0.16))
def draw_usage(input_value: UsageRef):
    imgui.text(
        f"{input_value.path} {input_value.line}:{input_value.column} {input_value.scope} {input_value.module_name}")

    return False, input_value


@render_func(is_default_for=(Comment), shadow=False, header_same_line=True, initial={"expanded": False}, icon="",
             is_tree=True, show_name=False, indent_size=8, selectable=False, use_cache=False,
             tint=(0.137, 0.683, 0.299, 0.708),
             show_bg=False, with_header=draw_header, temp=False, expanded_mode=ExpandMode.MANUAL)
def draw_comment(input_value: Comment, draw_state, style_manager, cursor_hover=False, font=Font.JETBRAINS_MONO_13):
    changed, value = False, input_value

    imgui.dummy(0, 0)
    depth = max(0.3, Core.melty.bg_depth)
    depth_scale = 0.047

    # [tint=(0.883, 0.712, 0.206, 0.34)]
    name_style = {
        'value': -0.420, 'saturation': 1.06,
        'alpha': 0.047, 'max_value': 0.704,
        'depth_factor': 0.34
    }
    depth_intensity = float(depth) * depth_scale
    name_style['value'] = depth_intensity * name_style['depth_factor'] + name_style['value']

    # [tint=(0.767, 0.379, 0.379)]
    alpha = 1.02

    sat_depth_factor = 0.0
    sat_depth_offset = 0.188
    sat_shift = float(depth + sat_depth_offset) * sat_depth_factor
    name_style['saturation'] = name_style['saturation'] + sat_shift

    name_color = style_manager.make_color_style_value(input=name_style)

    # A grouped multi-line comment is a '\n'-joined run of '# ' lines; strip the
    # '#'/'# ' prefix from EACH line so it displays as clean prose, not just the
    # first (str[2:] would leave a stray '#' on every continuation line).
    def _strip_hash(ln):
        return ln[2:] if ln.startswith("# ") else (ln[1:] if ln.startswith("#") else ln)

    display = "\n".join(_strip_hash(ln) for ln in str(input_value).split("\n"))

    # The framework header (is_tree=True) owns the expand/collapse button.
    # ExpandMode.MANUAL keeps this body visible while collapsed, with the
    # first line standing in for the whole comment.
    if "\n" in display and not draw_state.expanded:
        # Collapsed: one line truncated to the available width - never let it
        # spill onto a second row.
        flat = " ".join(display.split("\n"))
        avail = draw_state.abs_left + draw_state.width - imgui.get_cursor_screen_pos().x
        if imgui.calc_text_size(flat).x > avail:
            lo, hi = 0, len(flat)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if imgui.calc_text_size(flat[:mid]).x <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            flat = flat[:lo].rstrip()
        imgui.push_style_color(imgui.COLOR_TEXT, *name_color[:3], alpha)
        imgui.text(flat)
        imgui.pop_style_color()
    else:
        imgui.push_text_wrap_pos(draw_state.abs_left + draw_state.width)
        imgui.push_style_color(imgui.COLOR_TEXT, *name_color[:3], alpha)
        imgui.text_wrapped(display)
        imgui.pop_style_color()
        imgui.pop_text_wrap_pos()

    if changed:
        return True, value
    return False, input_value


@render_func(is_default_for=(Parameter), wraps=render_func, with_header=draw_header)
def draw_parameter(input_value):
    from meltygui.core.rendering.render_dispatch import draw_any

    parameter_default = input_value.default
    if parameter_default is inspect.Parameter.empty:
        imgui.same_line()
        imgui.text("<No Default>")
    else:
        return draw_any(parameter_default, show_name=False, show_add_delete=False)


@render_func(is_default_for=(types.FunctionType, types.MethodType), z_offset=0, use_cache=True,
             show_add_delete=False, selectable=False, show_bg=True,
             parent_show_add_delete=False, is_tree=False, show_name=False, with_header=draw_header)
def draw_function(input_value, name, draw_state, unique, auto_run=None, wrap=False,
                  show_run_button=True, run_in_thread=False, result_fade_frames=None,
                  **kwargs):
    """`auto_run`: opt-in compile-and-run — pass any comparable version token
    (e.g. id(fn.__code__)); the function runs whenever the token CHANGES or a
    parameter is edited, no button click. The token is stored before running
    so a throwing function doesn't retry every frame. `show_run_button=False`
    drops the named run button (the streamlined live-lab look).


    `run_in_thread=True` runs the function on a daemon worker instead of
    blocking the render loop (long model passes). Single-flight: a click or
    auto_run while a run is in flight is skipped — but the auto_run token is
    only latched when a run actually starts, so a hotswap landing mid-run
    re-fires on completion instead of being lost. The worker only writes
    draw_state attrs and uses the cross-thread invalidation path (the
    Background.run completion pattern); all rendering stays on the GL thread."""
    from meltygui.core.windowing.glfw_utils import request_render
    from meltygui.utils.render_utils import print_colored_traceback
    from meltygui.view.collection_view import draw_collection
    from meltygui.view.control_view import button
    from meltygui.core.rendering.render_dispatch import _RUN_RESULT_HOLDERS
    from meltygui.core.rendering.render_dispatch import _format_run_error
    from meltygui.core.rendering.render_dispatch import _respond_to_cuda_oom
    from meltygui.core.rendering.render_dispatch import draw_any
    from meltygui.core.rendering.render_dispatch import is_run_busy
    from meltygui.core.rendering.render_dispatch import run_busy_begin
    from meltygui.core.rendering.render_dispatch import run_busy_end

    if not callable(input_value):
        imgui.text("Not a callable function")
        return False, input_value
    params_edited = False
    try:
        signature = inspect.signature(input_value)
        params = signature.parameters
        if len(draw_state.params) != len(params):
            param_dict = {}

            for name, param in params.items():
                if name == 'kwargs':
                    continue
                if param.default is not inspect.Parameter.empty:
                    param_dict[name] = param.default
                else:
                    param_type = param.annotation
                    default_value = param.default
                    if default_value is not inspect.Parameter.empty:
                        param_dict[name] = default_value
                    else:
                        if name in Core.melty.global_attrs:
                            param_dict[name] = Core.melty.global_attrs[name]

            draw_state.params = param_dict
        if len(draw_state.params) > 0:
            from meltygui.core.rendering.mode import Mode
            changed, new_val = draw_collection(draw_state.params, name="Parameters", initial={"expanded": True},
                                               use_cache=True,
                                               mode=Mode.FUNCTION_PARAMS,
                                               show_add_delete=False, shadow=True, z_offset=1,
                                               parent_show_add_delete=False,
                                               horizontal=False, wrap=wrap,
                                               child_kwargs={"max_width": 397, "shadow": False,
                                                             "show_bg": False, "use_cache": True, "z_offset": 0.0
                                                             }, tint=(0.34, 0.40, 0.45))
            if changed:
                draw_state.params = new_val
                params_edited = True
    except Exception as e:
        imgui.text(f"Error inspecting function parameters: {e}")
        draw_state.params = {}

        sees_this = 0

    # A pkl written while the old misc-latch was active reloads as busy -
    # scrub it; the latch is _RUN_BUSY now (never persisted).
    draw_state.misc.pop("_run_busy", None)

    def _run():
        if run_in_thread:
            if not run_busy_begin(draw_state):
                return  # single-flight: one run per runner at a time
            # Snapshot params so a mid-run edit can't give the worker a
            # half-updated dict; never run a @render_func WRAPPER off-thread
            # (it mutates process-global Melty stacks - see run_in_background),
            # otherwise take the bare function.
            params = dict(draw_state.params)
            fn = getattr(input_value, "__wrapped__", input_value)

            def _worker():
                try:
                    draw_state.result = fn(**params)
                    _RUN_RESULT_HOLDERS.add(draw_state)
                    draw_state.misc["_result_frame"] = Melty.frame_count
                    draw_state.misc.pop("_run_error", None)
                except Exception as e:
                    draw_state.misc["_run_error"] = _format_run_error(e)
                    print(f"Error calling function '{input_value.__name__}': {e}")
                    print_colored_traceback(*sys.exc_info())
                    _respond_to_cuda_oom(e, input_value.__name__)
                finally:
                    run_busy_end(draw_state)
                    # invalidate_up_current reads the live render stack - only
                    # valid mid-render on the GL thread. Off-thread completion
                    # marks the runner's subtree by tile id (force: the result
                    # pane is a cached descendant) and wakes the loop; the
                    # validation itself happens on the render thread.
                    from meltygui.core.cache.invalidation_tracker import Note
                    Melty.cache.invalidate_up(
                        draw_state._tile_id, force=True,
                        note=Note(name="draw_function run complete",
                                  reason=f"func={input_value.__name__}",
                                  tint=(0, 0, 1)))
                    request_render()

            threading.Thread(target=_worker, daemon=True,
                             name=f"draw_function:{input_value.__name__}").start()
            Core.melty.cache.invalidate_up_current(force=True)  # show spinner now
            request_render()
            return
        try:
            draw_state.result = input_value(**draw_state.params)
            _RUN_RESULT_HOLDERS.add(draw_state)
            draw_state.misc["_result_frame"] = Melty.frame_count
            draw_state.misc.pop("_run_error", None)
            Core.melty.cache.invalidate_up_current(force=True)
        except Exception as e:
            # Surfaced in the UI (red text where the result goes), anchored at
            # the deepest frame in PROJECT code - the line the user can fix.
            draw_state.misc["_run_error"] = _format_run_error(e)
            Core.melty.cache.invalidate_up_current(force=True)
            print(f"Error calling function '{input_value.__name__}': {e}")
            print_colored_traceback(*sys.exc_info())
            _respond_to_cuda_oom(e, input_value.__name__)

    busy = run_in_thread and is_run_busy(draw_state)
    # One-offed run request (draw_function_live's Ctrl+Enter - the
    # hotkey IS the Run button): always popped, so it can't replay on later
    # frames; dropped while busy, matching a click during a threaded run.
    if draw_state.misc.pop("_run_requested", None) and not busy:
        _run()
    if auto_run is not None and not busy and (
            params_edited or draw_state.misc.get("_auto_run_ver") != auto_run):
        draw_state.misc["_auto_run_ver"] = auto_run
        _run()

    imgui.new_line()
    if show_run_button and button(f"Run {input_value.__name__}()##{unique}", icon=kwargs.get("icon", ""), height=35,
                                  bg_offset=0, tint=(0.499, 0.844, 0.488, 0.32), shadow=True, rounding=None)[0]:
        _run()

    # Fading result (result_fade_frames): the check mark + result text hold,
    # then fade out and clear - modeled on code_file_io's recompile_status
    # (frame-based fade, invalidate + request_render pump while fading).
    # None (default) keeps the persistent result pane.
    result_fade = 1.0
    if result_fade_frames and draw_state.result is not None:
        shown_for = float(Melty.frame_count
                          - draw_state.misc.get("_result_frame", Melty.frame_count))
        result_fade = min(1.0, max(0.0, 2.0 - shown_for / float(result_fade_frames)))
        if result_fade > 0.01:
            draw_state.invalidate()
            request_render()
        else:
            draw_state.result = None
            draw_state.misc.pop("_result_frame", None)

    if run_in_thread and is_run_busy(draw_state):
        imgui.same_line(spacing=10)
        imgui.text_colored("", 0.55, 0.75, 1.0, 1.0)
        imgui.new_line()
    elif draw_state.result is not None:
        imgui.same_line(spacing=10)
        imgui.text_colored("", 0.55, 0.75, 1.0, result_fade)
        if result_fade_frames and isinstance(draw_state.result, str):
            # Fading summary rides the button row inline, so hiding it never
            # reflows the content below (the row holds its height).
            imgui.same_line(spacing=8)
            imgui.text_colored(draw_state.result, 0.55, 0.75, 1.0, result_fade)
        imgui.new_line()
    else:
        imgui.same_line()
        imgui.text_colored(" ", 0.55, 0.75, 1.0, 1.0)
        imgui.new_line()

    run_error = draw_state.misc.get("_run_error")
    if run_error:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_colored(run_error, 1.0, 0.45, 0.40, 1.0)
        imgui.pop_text_wrap_pos()

    if draw_state.result is not None and not (result_fade_frames
                                              and isinstance(draw_state.result, str)):
        imgui.text_colored(" Result", *(1.0, 1.0, 1.0, 0.5))
        imgui.set_cursor_pos_y(imgui.get_cursor_pos_y() - 15)
        draw_any(draw_state.result, name="Result", header_same_line=True, show_header=False,
                 show_add_delete=False)

    # pop_style_var(3)

    return False, input_value
