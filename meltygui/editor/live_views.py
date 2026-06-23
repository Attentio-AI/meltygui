"""Editor-side live_view widgets: marker token + anchored value window.

The type-keyed token_views overlay in draw_text calls `draw_live_view_overlay`
for every CallParse node; it bails unless the call is live_view, then resolves
the SAME (store_obj, key_path) the capture side publishes under
(live_view.site_for_line — one resolution code path, no drift) and draws a
box outline around the symbol being visualized. Clicking the box toggles the
value window: a closable nested window (latched into Melty.root_draw_states,
so it persists and re-draws every frame even while the editor tile is
blit-cached) whose swoosh connector anchors back to the marker. The window
re-reads the store each render and registers itself as a watcher, so the
publishing thread invalidates exactly this window per publish — never the
editor tile, never per frame.

Only explicit live_view() call tokens auto-open their window the first time a
marker renders while a value exists — you typed the call, so the value shows
without a click. Snapshot/param markers (every captured assignment from an
instrumented run) start closed and open on box click, so a run doesn't bury
the code under one window per local. Sites the dict conversion can't surface
(while/with/match bodies — line-keyed fallback stores) have no CallParse token
to anchor and get no marker yet.
"""

import inspect
import re
import weakref

import imgui
from imgui.core import _DrawList

from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_conversion.live_view import (
    live_values_for, site_for_line, watch, install_builtin)
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header

# The seamless path: any app can call live_view() with no import (like
# breakpoint()). Installed when the editor side loads - i.e. every studio
# session - so "type the call, hotswap, run" needs no source changes beyond
# the call itself.
install_builtin()


def install_token_views(token_views):
    """Merge the live_view overlays into a token_views dict (called by
    text_editor right after DEFAULT_TOKEN_VIEWS is defined). Order matters:
    CallParse IS-A GeneralParse and the walk takes the first matching type, so
    the call-token entry must precede the root snapshot entry."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
        CallParse, GeneralParse)
    token_views[CallParse] = {"renderer": draw_live_view_overlay,
                              "char_width": None}
    token_views[GeneralParse] = {"renderer": draw_snapshot_overlay,
                                 "char_width": None}


class LiveHandle:
    """Hashable (store_obj, key_path) pair — the input_value of the marker and
    window render_funcs, so draw_state identity and caching key off the site,
    not the (ever-changing) value. `box_w`/`box_h` (the symbol box's pixel
    size) ride along as plain attrs excluded from hash/eq, re-stamped by the
    overlay every call: per-site geometry must NOT travel as render_func
    kwargs — caching/auto-state replays another marker's values there (every
    box rendered at the last call's width)."""
    __slots__ = ("store_obj", "key_path", "box_w", "box_h")

    def __init__(self, store_obj, key_path, box_w=None, box_h=None):
        self.store_obj = store_obj
        self.key_path = key_path
        self.box_w = box_w
        self.box_h = box_h

    def __hash__(self):
        return hash((self.store_obj, self.key_path))

    def __eq__(self, other):
        return (isinstance(other, LiveHandle)
                and self.store_obj is other.store_obj
                and self.key_path == other.key_path)

    def __repr__(self):
        return f"LiveHandle({'/'.join(self.key_path)})"


def _right_of_window_pos(parent_win, marker_x, gap=10.0):
    """Parent-relative window_pos that opens a spawned live-value window just
    to the RIGHT of the editor's enclosing window, vertically level with the
    marker — instead of on top of the code the marker sits in.

    window_pos is relative to the spawned window's parent (the same editor
    window), and a marker renders at the cursor (abs_left == marker_x when
    window_pos is 0), so the parent-origin x offset is exactly marker_x:
    subtract it from the window's absolute right edge to land there. Keeping
    window_pos parent-relative means the value window then tracks the editor
    window as it moves. Returns None when there's no enclosing window to
    anchor to (caller falls back to the default on-cursor placement)."""
    if parent_win is None:
        return None
    return (parent_win.abs_left + parent_win.width + gap - marker_x, 0)


def draw_live_view_overlay(x=0, y=0, w=0, h=0, draw_state=None, char_w=8.0,
                           line_px=20.0, node=None, span=None, root=None,
                           line_offset=0, jump_to=None, **kwargs):
    """token_views overlay callback for CallParse nodes (plain function — the
    overlay pass calls it with raw screen coords, no render_func wrapper)."""
    if getattr(node, "func_name", None) != "live_view":
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
    imgui.set_cursor_screen_pos((x - pad, y - pad))
    draw_live_view_marker(LiveHandle(store_obj, key_path,
                                     box_w=token_cells * char_w + 2 * pad,
                                     box_h=line_px + 2 * pad),
                          name=f"lvm::{draw_state.id}::{'/'.join(key_path)}",
                          editor_ds=draw_state)


@render_func(use_cache=False, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, disable_scroll=True, wrap=True,
             z_offset=4, max_height=32)
def draw_live_view_marker(input_value, draw_state=None, editor_ds=None,
                          auto_open=True, child_kwargs=None, corner_radius=4.0,
                          left_mouse_down=False, left_mouse_held=False, unique=0,
                          **kwargs):
    """The anchor box around the symbol being visualized: a plain rect
    over the live_view call / captured assignment — green when a value has
    been captured, dim when the site hasn't run yet. Click toggles the value
    window. The box's pixel size rides on the handle (`handle.box_w/h`, set
    by the overlays along with the cursor at the box's top-left) — NOT as
    kwargs: use_cache replays stale kwargs on watcher-driven re-renders, so
    every box would render at some other marker's size.
    `auto_open=False` (the snapshot/param markers) keeps the window closed
    until the box is clicked — only explicit live_view() tokens pop their
    value unprompted. left_mouse_* are declared (never read) so a press on
    the box latches here instead of moving the editor caret — see
    draw_icon_selector."""
    handle = input_value
    ds = draw_state
    if child_kwargs is None:
        child_kwargs = {}
    # First only only: a gray box whose code then runs gets invalidated
    # on the key's FIRST value, flips green (and auto-opens below, when this
    # marker auto-opens) — one editor re-render per new key, nothing per
    # steady-state publish.
    watch(handle.store_obj, handle.key_path, ds, first_only=True)
    lv = live_values_for(handle.store_obj).get(handle.key_path)

    if getattr(ds, "_lv_open", None) is None and lv is not None and auto_open:
        ds._lv_open = True  # first value seen → show it without a click
    x, y = imgui.get_cursor_screen_pos()
    w = getattr(handle, "box_w", None) or 18.7
    h = getattr(handle, "box_h", None) or 18.7
    io = imgui.get_io()
    hovered = x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h
    win_ds = getattr(ds, "_lv_window_ds", None)
    if win_ds is not None:
        open_now = not win_ds.closed
    else:
        open_now = False

    if lv is not None:
        base = (0.36, 0.85, 0.46) if open_now else (0.26, 0.62, 0.34)
    else:
        base = (0.45, 0.45, 0.45)
    if hovered:
        base = tuple(min(1.0, c + 0.18) for c in base)
    dl: _DrawList = imgui.get_window_draw_list()
    dl.add_rect(x, y + 2, x + w, y + h - 3,
                imgui.get_color_u32_rgba(*base, 0.9 if open_now else 0.6), rounding=corner_radius)
    imgui.dummy(w, h)

    if hovered and imgui.is_mouse_clicked(0):
        ds._lv_open = not open_now
        if win_ds is not None:
            win_ds.closed = not ds._lv_open
            if ds._lv_open:
                # Reopening a latched window: snap it back to the right of the
                # editor window (it may have been dragged onto the code).
                pos = _right_of_window_pos(ds.parent_window, x)
                if pos is not None:
                    win_ds.window_pos = pos
        ds.invalidate()

    # Latched-window visibility pattern (the color-picker pattern,
    # draw_editor ~750): once a window exists, CALL IT EVERY MARKER RENDER
    # with closed= toggled - a latched window stays in root_draw_states until
    # its closed flag is set, so merely not-calling it does nothing.
    if getattr(ds, "_lv_open", None) is not None and (
            getattr(ds, "_lv_open", False) or win_ds is not None):
        # Full (per-publish) watch now the window is open so the value
        # streams in - the first_only watch above just flips the box green.
        # Re-uses the lv resolved at the top: no value → nothing to anchor.
        watch(handle.store_obj, handle.key_path, ds)
        if lv is None:
            return False, None
        label = lv.name if lv.name else handle.key_path[-1]
        key = '/'.join(handle.key_path)
        # Anchor the latch from INSIDE the marker's bounds: a cursor sitting
        # outside the clip (e.g. after a dummy that overflows max_height) makes
        # the window's FIRST measure degenerate - and a closable window never
        # remeasures (fixed_size), leaving it 0×0 and invisible visible.
        imgui.set_cursor_screen_pos((x, y))
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_any

        # The value renders through its own normal view as a closable window
        # (Mode.WINDOW) - volumes route to draw_voxels, scalars/dicts to their
        # renderers. No dedicated draw_live_value_window wrapper needed.
        child_kwargs.pop("mode", None)
        # First creation: open the value window to the RIGHT of the editor's
        # window rather than on top of the code. window_pos persists on the
        # spawned window's draw_state (and stays parent-relative, so it tracks
        # the editor window), so this is set once - later frames and user drags
        # of the value window are preserved.
        if win_ds is None and "window_pos" not in child_kwargs:
            pos = _right_of_window_pos(ds.parent_window, x)
            if pos is not None:
                child_kwargs["window_pos"] = pos
        _c, _v, win_ds = draw_any(
            lv.value, name=f"{label}##lv{ds.id}{key}", mode=Modes.WINDOW,
            with_header=draw_header, selectable=False, min_height=32,
            min_width=9, disable_scroll=True, return_extras=True, **child_kwargs)

        if win_ds.closed:
            ds._lv_open = False
        ds._lv_window_ds = win_ds

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
    cst_node = node.get("__cst__") if isinstance(node, dict) else None
    if type(cst_node).__name__ != "FunctionDef" or span is None:
        return
    filename = (getattr(root, "file_path", None)
                or getattr(getattr(root, "address", None), "path", None)
                or getattr(jump_to, "path", None))
    if filename is None:
        return
    fn = _scope_function(str(filename), span.start_line + line_offset)
    if fn is None:
        return
    # Store-level registration BEFORE any values exist: the first instrumented
    # run's brand-new keys invalidate this editor, the overlay re-runs, and
    # the markers materialize (closed - these auto_open=False boxes wait for
    # a click). Without it the first run stays invisible until an unrelated
    # repaint.
    watch(fn, None, draw_state)

    origin_y = y - (span.start_line - 1) * line_px
    origin_x = x - getattr(span, "start_col", 0) * char_w
    source_lines = (getattr(root, "source", "") or "").split("\n")
    for key_path, lv in live_values_for(fn).items():
        if not key_path or not isinstance(key_path[-1], str):
            continue
        if key_path[-1].split("#", 1)[0] == "live_view()":
            continue  # anchored by its own call-token marker
        anchor = _key_anchor(node, key_path, line_offset)
        if anchor is None:
            continue
        rel_line, start_col, end_col = anchor
        if end_col is None:
            # No span (line:N key) - box the line's text, first non-space
            # to end ("line highlight").
            if 1 <= rel_line <= len(source_lines):
                text = source_lines[rel_line - 1]
                if not text.strip():
                    continue
                start_col = len(text) - len(text.lstrip())
                end_col = len(text.rstrip())
            else:
                continue
        else:
            # The leaf span covers the assignment (or sometimes just its
            # RHS, depending on the parse) - box the SYMBOL itself so the
            # highlight (and its click latch) doesn't swallow the line: the
            # first word-boundary occurrence of the target name on the line
            # (the assignment target precedes any RHS use of the name).
            name = key_path[-1].split("#", 1)[0]
            text = (source_lines[rel_line - 1]
                    if 1 <= rel_line <= len(source_lines) else "")
            m = re.search(rf"\b{re.escape(name)}\b", text)
            if m is not None:
                start_col, end_col = m.start(), m.end()
            else:
                end_col = start_col + max(1, len(name))
        pad = 2.0
        imgui.set_cursor_screen_pos(
            (origin_x + start_col * char_w - pad,
             origin_y + (rel_line - 1) * line_px - pad))
        # Auto-open the VOLUMES (3-D data → orbiting voxel window: the
        # point of the lab) so a run pops them unprompted; scalars/configs
        # stay quiet click-to-open boxes so 19 locals don't bury the def.
        overrides = {}
        if 'locals' in node and '__overrides__' in node['locals']:
            override_path = f"__{key_path[-1]}__"
            if override_path in node['locals']["__overrides__"]:
                overrides = dict(node['locals']["__overrides__"][override_path])
        # Overrides go ONLY in child_kwargs (for the value window's view) -
        # never splat them onto the marker itself: they're view params
        # (tint/show_bg/dim_names/nf_format), and show_bg=True in a dark theme
        # makes the marker's wrapper paint an opaque bg over the very symbol
        # it boxes (the symbol inside the rect disappears).
        draw_live_view_marker(
            LiveHandle(fn, key_path,
                       box_w=max(1, end_col - start_col) * char_w + 2 * pad,
                       box_h=line_px + 2 * pad),
            name=f"lvs::{draw_state.id}::{fn.__qualname__}::"
                 f"{'/'.join(key_path)}",
            editor_ds=draw_state, auto_open=is_volume(lv.value),
            child_kwargs=overrides)


def _scope_function(filename, def_line):
    """The live function object for a def at an absolute file line — the same
    resolver capture uses, so the store read here is the store written to."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _enclosing_function)
    try:
        return _enclosing_function(filename, def_line + 1)
    except Exception:
        return None


def _key_anchor(scope_node, key_path, line_offset):
    """(buffer-relative line, start col | None, end col | None) the symbol
    box for a snapshot key: descend the scope's locals by the key path to the
    leaf's span; a `line:N` tail IS the (absolute) anchor, with no column
    information (the caller boxes the whole line's text)."""
    tail = key_path[-1]
    if tail.startswith("line:"):
        try:
            return int(tail[5:]) - line_offset, None, None
        except ValueError:
            return None
    node = scope_node.get("locals")
    if not isinstance(node, dict):
        node = scope_node
    for seg in key_path[:-1]:
        node = node.get(seg) if isinstance(node, dict) else None
        if node is None:
            return None
    spans = getattr(node, "_child_spans", None) or {}
    sp = spans.get(tail)
    if sp is None:
        child = node.get(tail) if isinstance(node, dict) else None
        sp = getattr(child, "span", None)
    if sp is None:
        return None
    end_col = getattr(sp, "end_col", None)
    return (sp.start_line,
            getattr(sp, "start_col", 0) if end_col is not None else None,
            end_col)


# ── Run-once snapshot view: draw_function + instrumentation + source ──

_proxies = weakref.WeakKeyDictionary()


def _run_proxy(fn):
    """A stable callable twin-runner for draw_function: same name/signature as
    `fn` (so the param UI builds identically), but the button runs the
    INSTRUMENTED twin. Cached per function object — identity survives hotswap,
    and run_instrumented re-twins per code version underneath."""
    proxy = _proxies.get(fn)
    if proxy is None:
        def proxy(**kw):
            from src.lsd.gl_gui.view.core_conversion.live_instrument import (
                run_instrumented)
            return run_instrumented(fn, **kw)
        proxy.__name__ = fn.__name__
        proxy.__qualname__ = fn.__qualname__
        proxy.__signature__ = inspect.signature(fn)
        _proxies[fn] = proxy
    return proxy


def run_forward_pass(use_gen_pass=True):
    """Trigger the model's full forward pass. As a live lab this gets the
    whole streamlined loop: opening the window runs it, saving an edit (to
    THIS function or, more usefully, to any model code you've dropped
    live_view() calls into) re-runs it, and every captured value lands in an
    anchored window. No-op until a forward pass + prompt are selected."""
    from src.lsd.train.lsd_train import LSD
    LSD.full_forward_pass(root=Core.melty.vis.root, vis=Core.melty.vis,
                          use_gen_pass=use_gen_pass)


@window(initial={"width": 350, "height": 540})
@render_func(tint=(0.026, 0.055, 0.089), auto_resize=False)
def live_view_forward(input_value=None, draw_state=None, **kwargs):
    from src.lsd.train.lsd_train import LSD
    from src.lsd.gl_gui.view.mode import Mode
    # NEW_CODE = the full two-pane code_file_io display: the draw_collection
    # structured (code_dict) pane AND the live-overlay text pane side by side.
    draw_function_live(LSD.full_forward_pass_live, name="run_forward_pass runner",
                       source_mode=Mode.NEW_CODE)


@render_func(use_cache=True, show_bg=False, shadow=False, selectable=False,
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
    the same time (live_view_forward uses it)."""
    fn = input_value
    try:
        fn = inspect.unwrap(fn)
    except Exception:
        pass
    if not callable(fn) or getattr(fn, "__code__", None) is None:
        imgui.text("draw_function_live: needs a plain function")
        return False, input_value
    from src.lsd.gl_gui.view.core_views.new_core_view import (
        draw_function, draw_any)
    from src.lsd.gl_gui.view.core_views.columns import (
        ColumnLayout, MIN_ROW_HEIGHT)
    from src.lsd.gl_gui.view.mode import Mode
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
                      name=f"{fn.__name__} runner", run_in_thread=run_in_thread)
    with cols.cell(1, height=avail) as col_w:
        draw_any(fn, mode=source_mode or Mode.FILE_TREE, height=inner_h,
                 width=col_w, name=f"{fn.__name__} live source",
                 left_edge=cols.edges[1], right_edge=cols.edges[2])
    cols.finish()
    return False, input_value


def is_volume(value):
    """True for values the voxel renderer should own: 3-D+ numeric tensors or
    ndarrays. 2-D and below keep their existing views; name-based checks so
    torch/numpy never import for scalar traffic."""
    cls = type(value).__name__
    try:
        if cls == "Tensor":
            return value.dim() >= 3
        if cls == "ndarray":
            return value.ndim >= 3 and value.dtype.kind in "fiu"
    except Exception:
        pass
    return False
