"""Editor-side live_view widgets: marker token + anchored value window.

The type-keyed token_views overlay in draw_text calls `draw_live_view_overlay`
for every CallParse node; it bails unless the call is live_view, then resolves
the SAME (store_obj, key_path) the capture side publishes under
(live_view.site_for_line — one resolution code path, no drift) and draws a
small marker dot just past the call token. Clicking the marker toggles the
value window: a closable nested window (latched into Melty.root_draw_states,
so it persists and re-draws every frame even while the editor tile is
blit-cached) whose swoosh connector anchors back to the marker. The window
re-reads the store each render and registers itself as a watcher, so the
publishing thread invalidates exactly this window per publish — never the
editor tile, never per frame.

Only explicit live_view() call tokens auto-open their window the first time a
marker renders while a value exists — you typed the call, so the value shows
without a click. Snapshot/param markers (every captured assignment from an
instrumented run) start closed and open on dot click, so a run doesn't bury
the code under one window per local. Sites the dict conversion can't surface
(while/with/match bodies — line-keyed fallback stores) have no CallParse token
to anchor and get no marker yet.
"""

import inspect
import weakref

import imgui

from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_conversion.live_view import (
    live_values_for, site_for_line, watch, install_builtin)
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window

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
    not the (ever-changing) value."""
    __slots__ = ("store_obj", "key_path")

    def __init__(self, store_obj, key_path):
        self.store_obj = store_obj
        self.key_path = key_path

    def __hash__(self):
        return hash((self.store_obj, self.key_path))

    def __eq__(self, other):
        return (isinstance(other, LiveHandle)
                and self.store_obj is other.store_obj
                and self.key_path == other.key_path)

    def __repr__(self):
        return f"LiveHandle({'/'.join(self.key_path)})"


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
    imgui.set_cursor_screen_pos((x + token_cells * char_w + 6,
                                 y + max(0.0, (line_px - 16) / 2)))
    draw_live_view_marker(LiveHandle(store_obj, key_path),
                          name=f"lvm::{draw_state.id}::{'/'.join(key_path)}",
                          editor_ds=draw_state)


@render_func(use_cache=True, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, disable_scroll=True, wrap=True,
             z_offset=3, max_height=24)
def draw_live_view_marker(input_value, draw_state=None, editor_ds=None,
                          auto_open=True, child_kwargs=None,
                          left_mouse_down=False, left_mouse_held=False,
                          **kwargs):
    """The anchor dot beside a live_view call: green when a value has been
    captured, dim when the site hasn't run yet. Click toggles the value
    window. `auto_open=False` (the snapshot/param markers) keeps the window
    closed until the dot is clicked — only explicit live_view() tokens pop
    their value unprompted. left_mouse_* are declared (never read) so a press
    on the dot latches here instead of moving the editor caret — see
    draw_icon_selector."""
    handle = input_value
    ds = draw_state
    if child_kwargs is None:
        child_kwargs = {}
    # First-publish only: a dim dot whose code then runs gets invalidated
    # on the key's FIRST value, flips green (and auto-opens below, if this
    # marker auto-opens) - one editor re-render per new key, nothing per
    # steady-state publish.
    watch(handle.store_obj, handle.key_path, ds, first_only=True)
    lv = live_values_for(handle.store_obj).get(handle.key_path)

    if getattr(ds, "_lv_open", None) is None and lv is not None and auto_open:
        ds._lv_open = True  # first value seen → show it without a click
    x, y = imgui.get_cursor_screen_pos()
    size = 18.7
    io = imgui.get_io()
    hovered = x <= io.mouse_pos.x < x + size and y <= io.mouse_pos.y < y + size
    open_now = bool(getattr(ds, "_lv_open", False))
    if lv is not None:
        base = (0.36, 0.85, 0.46) if open_now else (0.26, 0.62, 0.34)
    else:
        base = (0.45, 0.45, 0.45)
    if hovered:
        base = tuple(min(1.0, c + 0.18) for c in base)
    dl = imgui.get_window_draw_list()
    cx, cy, r = x + size / 2, y + size / 2, 3.9
    dl.add_circle_filled(cx, cy, r, imgui.get_color_u32_rgba(*base, 1.0))
    if open_now:
        dl.add_circle(cx, cy, r + 2.5,
                      imgui.get_color_u32_rgba(*base, 0.55), thickness=1.2)
    imgui.dummy(size, size)

    # The user X-ing the window directly must beat our open flag. Detect it
    # BEFORE this render passes closed= (which overwrites the very flag we're
    # reading): an X is "closed became True while we last asked for open".
    win_ds = getattr(ds, "_lv_window_ds", None)
    if (open_now and win_ds is not None
            and getattr(ds, "_lv_passed_closed", None) is False
            and (win_ds.closed or win_ds.abs_closed)):
        ds._lv_open = open_now = False

    if hovered and imgui.is_mouse_clicked(0):
        ds._lv_open = not open_now
        ds.invalidate()

    # Latched-window visibility pattern (the color-picker pattern,
    # draw_editor ~750): once a window exists, CALL IT EVERY MARKER RENDER
    # with closed= toggled - a latched window stays in root_draw_states until
    # its closed flag is set, so merely not-calling it does nothing.
    if getattr(ds, "_lv_open", None) is not None and (
            getattr(ds, "_lv_open", False) or win_ds is not None):
        label = (lv.name if lv is not None and lv.name
                 else handle.key_path[-1])
        # Anchor the latch from INSIDE the marker's bounds: a cursor sitting
        # outside the clip (e.g. after a dummy that overflows max_height) makes
        # the window's FIRST measure degenerate - and a closable window never
        # remeasures (fixed_size), leaving it 0×0 and invisible visible.
        imgui.set_cursor_screen_pos((x, y))
        _c, _v, win_ds = draw_live_value_window(
            handle, name=f"{label}##lv{ds.id}", closable=True,
            closed=not ds._lv_open, return_extras=True, child_kwargs=child_kwargs, **child_kwargs)
        ds._lv_passed_closed = not ds._lv_open
        if ds._lv_open and win_ds is not None and not win_ds.width:
            # The closable self-sizing branch (core_render ~1358) silently
            # fails in some nested layouts (wrap inheritance, multi_line
            # siblings) and a 0-width first measure never recovers - seed it;
            # auto sizing takes over from there. Volumes get a viewport-
            # sized seed: the voxel renderer derives its render size from the
            # owning window, so a 120×40 seed would orbit in a keyhole.
            if lv is not None and is_volume(lv.value):
                win_ds.width = 320
                win_ds.height = max(win_ds.height or 0, 340)
            else:
                win_ds.width = 120
                win_ds.height = max(win_ds.height or 0, 40)
            # First latch only: a marker anchored below/right of the display
            # anchors its window OFF-SCREEN ("not seeing the live view") -
            # clamp the fresh window into the viewport; the whoosh still
            # points back at the (off-screen) marker. Window drags persist
            # afterwards because this only runs on the 0-width first measure.
            from src.lsd.gl_gui.melty import Melty
            display = imgui.get_io().display_size
            cx = min(max(x, 10), max(10, display.x - win_ds.width - 20))
            cy = min(max(y, 10), max(10, display.y - win_ds.height - 30))
            if (cx, cy) != (x, y):
                Melty.summon_window(win_ds, cx, cy)
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
    # Store this registration BEFORE any values exist: the first instrumented
    # run's brand-new keys invalidate the screen, the overlay re-runs, and
    # the markers materialize (closed - these auto_open=False dots wait for a
    # click). Without it the first run stays invisible until an unrelated
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
        rel_line, end_col = anchor
        if end_col is None:
            # No span (line:N tail) - anchor at the end of the line's text.
            if 1 <= rel_line <= len(source_lines):
                end_col = len(source_lines[rel_line - 1])
            else:
                continue
        imgui.set_cursor_screen_pos(
            (origin_x + (end_col + 1.5) * char_w,
             origin_y + (rel_line - 1) * line_px
             + max(0.0, (line_px - 16) / 2)))
        # Auto-open the VOLUMES (3-D data → orbiting voxel window: the
        # point of the lab) so a run pops them unprompted; scalars/configs
        # stay quiet click-to-open dots so 19 locals don't hide the code.
        overrides = {}
        if 'locals' in node and '__overrides__' in node['locals']:
            override_path = f"__{key_path[-1]}__"
            if override_path in node['locals']["__overrides__"]:
                overrides = dict(node['locals']["__overrides__"][override_path])
        draw_live_view_marker(
            LiveHandle(fn, key_path),
            name=f"lvs::{draw_state.id}::{fn.__qualname__}::"
                 f"{'/'.join(key_path)}",
            editor_ds=draw_state, auto_open=is_volume(lv.value), child_kwargs=overrides, **overrides)


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
    """(buffer-relative line, end col | None) a snapshot key anchors at:
    descend the scope's locals by the key path to the leaf's span; a `line:N`
    tail IS the (absolute) anchor, with no column information."""
    tail = key_path[-1]
    if tail.startswith("line:"):
        try:
            return int(tail[5:]) - line_offset, None
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
    return sp.start_line, getattr(sp, "end_col", None)


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


@window
@render_func(tint=(0.09, 0.03, 0.32), auto_resize=True)
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
                       source_mode=None, column_edges=None, **kwargs):
    """draw_function with transparent instrumentation, source side by side:
    the instrumented twin (live_instrument) runs AUTOMATICALLY — on first
    view, on every hotswap (id(fn.__code__) is the auto_run token, so a save
    in the editor compiles AND runs in one go), and on param edits — every
    assignment publishes one snapshot to the ORIGINAL function's store, and
    the editor column shows the ORIGINAL code with the snapshot overlay
    anchoring each captured value at its line.

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
    top_y = draw_state.abs_top + 32
    imgui.set_cursor_screen_pos((draw_state.abs_left, top_y))
    cols = ColumnLayout(draw_state, 2, column_edges=column_edges,
                        column_widths=[244])
    clip = cols.clip if cols.clip is not None else draw_state.abs_clip_rect
    avail = (max(MIN_ROW_HEIGHT, clip[3] - cols.top) if clip is not None
             else 400.0)
    inner_h = avail - 2 * cols.padding
    with cols.cell(0, height=avail) as col_w:
        draw_function(_run_proxy(fn), height=inner_h, width=col_w,
                      name=f"{fn.__name__} runner")
    with cols.cell(1, height=avail) as col_w:
        draw_any(fn, mode=source_mode or Mode.FILE_TREE, height=inner_h,
                 width=col_w, name=f"{fn.__name__} live source",
                 left_edge=cols.edges[1], right_edge=cols.edges[2])
    cols.finish()
    return False, input_value


# auto_resize=True is REQUIRED here even though closable makes the box
# fixed_size: the closable self-sizing branch (core_render ~1358,
# `fixed_size and auto_resize and closable and multi_line and not is_wrapped`)
# is what assigns the window its initial width - with auto_resize=False (or a
# wrap-inheriting caller) the condition fails, the first measure stays 0×0,
# and a closable window never remeasures, so it latches invisible.
# Header ON: closable windows get their X from draw_header (headers.py ~757),
# which flips draw_state.closed - the same flag the marker's toggle protocol
# uses, so the X and the dot stay in sync. The window name ("loss##lv...")
# shows its label; the ## suffix stays hidden.
def _draw_close_x(ds):
    """An explicit top-right close glyph drawn OVER the content — volume
    values fill the whole window and bury the header's X, so the window
    carries its own."""
    size = 15.0
    x1 = ds.abs_left + (ds.width or 0) - size - 5
    y1 = ds.abs_top + 5
    io = imgui.get_io()
    hovered = x1 <= io.mouse_pos.x < x1 + size and y1 <= io.mouse_pos.y < y1 + size
    dl = imgui.get_window_draw_list()
    col = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.9 if hovered else 0.45)
    pad = 4.0
    dl.add_line(x1 + pad, y1 + pad, x1 + size - pad, y1 + size - pad, col, 1.6)
    dl.add_line(x1 + size - pad, y1 + pad, x1 + pad, y1 + size - pad, col, 1.6)
    if hovered and imgui.is_mouse_clicked(0):
        ds.closed = True   # a USER close: the marker treats it as an X
        ds.invalidate()
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()


@render_func(use_cache=True, show_bg=True, shadow=True, auto_resize=False,
             selectable=False)
def draw_live_value_window(input_value, draw_state=None, child_kwargs=None, **kwargs):
    """The anchored value window: re-reads the store each render, registers as
    a watcher so the PUBLISHING thread invalidates it per publish (throttled
    wake — the editor tile is never touched), and routes the value through
    draw_any so every type renders with its normal view. Volume-shaped values
    (3-D+ tensors/ndarrays) route through the voxel pipeline instead: upload
    via voxel_io (gl_state/axes auto-injected per draw_state, CUDA tensors go
    device-to-device, re-upload keyed on identity/_version so each publish
    streams in) and draw_any(tex) lands on draw_voxels — a live, orbiting
    volume anchored to the code that produced it."""
    if child_kwargs is None:
        child_kwargs = {}
    handle = input_value
    watch(handle.store_obj, handle.key_path, draw_state)
    lv = live_values_for(handle.store_obj).get(handle.key_path)
    if lv is None:
        imgui.text_colored("no value yet — run the code", 0.65, 0.65, 0.65, 1.0)
        _draw_close_x(draw_state)
        return False, None
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
    key = '/'.join(handle.key_path)
    if is_volume(lv.value):
        from src.lsd.gl_gui.view.playground.voxel_playground import voxel_io
        _c, tex = voxel_io(lv.value, name=f"lvvox::{key}")
        if type(tex).__name__ == "GLTexture":
            draw_any(tex, name=f"lvtex::{key}", **child_kwargs)
        else:
            draw_any(lv.value, name=f"lvv::{key}", **child_kwargs)
    else:
        draw_any(lv.value, name=f"lvv::{key}")
    imgui.text_colored(f"{lv.name or ''}  gen {lv.generation}",
                       0.55, 0.55, 0.55, 1.0)
    # Drawn LAST = on top - volume values fill the window and bury the
    # header's X, so the window carries its own close glyph.
    _draw_close_x(draw_state)
    return False, None


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
