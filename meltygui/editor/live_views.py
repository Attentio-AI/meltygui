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
    live_values_for, label_for, site_for_line, watch, install_builtin)
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



def _store_name(obj):
    """Stable display/name key for a store object (function qualname or
    module name) — window/marker names key on this + the cst key path,
    never on draw_state ids (not stable across sessions/editors)."""
    return getattr(obj, "__qualname__", None) or getattr(obj, "__name__", "?")


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
    snap = live_values_for(store_obj)
    draw_live_view_marker(snap.get(key_path),
                          captured=key_path in snap,
                          store_obj=store_obj, key_path=key_path,
                          width=token_cells * char_w + 2 * pad,
                          height=line_px + 2 * pad,
                          name=f"lvm::{_store_name(store_obj)}::"
                               f"{'/'.join(key_path)}")


@render_func(use_cache=False, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, disable_scroll=True, wrap=True,
             z_offset=4, max_height=32)
def draw_live_view_marker(input_value=None, draw_state=None,
                          store_obj=None, key_path=None, captured=False,
                          code_tree_node=None, auto_open=True,
                          corner_radius=4.0,
                          left_mouse_down=False, left_mouse_held=False,
                          unique=0, **kwargs):
    """The live-view token widget — draw_bool_token's pattern plus one extra
    call, draw_any(value, mode=WINDOW). input_value IS the captured value
    (None + captured=False while the site hasn't run); the window call just
    forwards it and the framework routes it by type. draw_text positioned
    this view inline over the symbol and the draw_state carries its size —
    no rect plumbing.

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
    until the box is clicked — only explicit live_view() tokens pop their
    value unprompted. left_mouse_* are declared (never read) so a press on
    the box latches here instead of moving the editor caret — see
    draw_icon_selector."""
    ds = draw_state
    # First only only: a gray box whose code then runs gets invalidated
    # on the key's FIRST value, flips green (and auto-opens below, when this
    # marker auto-opens) — one editor re-render per new key, nothing per
    # steady-state publish. The invalidation re-runs the overlay, which boxes
    # the marker and passes the new value in.
    watch(store_obj, key_path, ds, first_only=True)

    # The comment data, already a dict from the code tree.
    comment_args = {}
    if isinstance(code_tree_node, dict) and key_path:
        _ca = code_tree_node.get("__overrides__", {}).get(
            f"__{key_path[-1]}__")
        if isinstance(_ca, dict):
            comment_args = {k: v for k, v in _ca.items()
                            if not (isinstance(k, str) and k.startswith("__"))}

    # Input-tab lookup: the context menu resolves this site's inputs off the
    # draw_state graph (draw_input_tab's live_root branch), so attach the same
    # scope dict the comment-args splat reads - restamp the render like
    # everything else per-site.
    ds.live_root = code_tree_node
    ds.live_key = key_path[-1] if key_path else None

    auto_open = comment_args.get("auto_open", auto_open)

    # Manual window-ds tracking (see docstring).
    win_ds = getattr(ds, "_lv_window_ds", None)
    if getattr(ds, "_lv_open", None) is None and captured and auto_open:
        ds._lv_open = True  # first value seen → show it without a click
    elif win_ds is not None and win_ds.closed and getattr(ds, "_lv_open", False):
        ds._lv_open = False  # user closed the window via its own header X
    open_now = bool(getattr(ds, "_lv_open", False))

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
    # Outline only under the mouse - the boxes read as clutter when every
    # instrumented site is permanently framed. hover reveals the affordance.
    if hovered:
        dl: _DrawList = imgui.get_window_draw_list()
        dl.add_rect(x, y + 2, x + w, y + h - 3,
                    imgui.get_color_u32_rgba(*base, 0.9 if open_now else 0.6),
                    rounding=corner_radius)
    imgui.dummy(w, h)

    if hovered and imgui.is_mouse_clicked(0):
        open_now = not open_now
        ds._lv_open = open_now
        if open_now and win_ds is not None:
            # Reopening: snap the window back to the right of the editor
            # window (it may have been dragged onto the code).
            pos = _right_of_window_pos(ds.parent_window, x)
            if pos is not None:
                win_ds.window_pos = pos
        ds.invalidate()

    if captured and (open_now or win_ds is not None):
        # Full (per-publish) watch once a window exists so the value streams
        # in - the first_only call above only flips the box green.
        watch(store_obj, key_path, ds)
        label = label_for(store_obj, key_path) or key_path[-1]
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
        # Named by the cst dict's own stable identity - never draw_state ids.
        win_kwargs = dict(
            name=f"{label}##lv::{_store_name(store_obj)}::"
                 f"{'/'.join(key_path)}",
            mode=Modes.LIVE_WINDOW, closed=not open_now,
            with_header=draw_header, disable_scroll=True, return_extras=True)
        # First creation: place the window to the RIGHT of the editor's
        # window rather than on top of the code. window_pos persists on the
        # spawned window's draw_state (parent-relative, so it tracks the
        # editor window) - set once; user drags are preserved after.
        if win_ds is None:
            pos = _right_of_window_pos(ds.parent_window, x)
            if pos is not None:
                win_kwargs["window_pos"] = pos
        _c, _v, win_ds = draw_any(input_value, **(win_kwargs | comment_args))
        ds._lv_window_ds = win_ds
        # The menu usually opens on the WINDOW - stamp the site context there
        # too (the _parent chain isn't guaranteed to pass through this marker
        # after a root_draw_states re-dispatch).
        win_ds.live_root = code_tree_node
        win_ds.live_key = ds.live_key

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
    for key_path, value in live_values_for(fn).items():
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
        # code_tree_node carries the scope dict: the marker reads the site's
        # comment source from it and splats it 1:1 onto the popup window's
        # draw_state (not onto the marker's own wrapper - show_bg=True with a
        # dark tint would draw an opaque bg over the very symbol it boxes).
        draw_live_view_marker(
            value, captured=True, store_obj=fn, key_path=key_path,
            width=max(1, end_col - start_col) * char_w + 2 * pad,
            height=line_px + 2 * pad,
            code_tree_node=node.get("locals") if isinstance(node, dict) else None,
            name=f"lvs::{fn.__qualname__}::{'/'.join(key_path)}",
            auto_open=is_volume(value))


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
@render_func(tint=(0.11, 0.11, 0.26, 1.00), auto_resize=False)
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
