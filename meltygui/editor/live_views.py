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

from src.lsd.gl_gui.model.core_model.draw_state import Anchor, Pin
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


# First-spawn height estimate for the display-bottom clamp below: the real
# height only exists after the window's first render (the reopen path passes
# it), and live-view windows commonly land in this range.
_SPAWN_EST_HEIGHT = 380.0


def _right_of_window_pos(parent_win, marker_x, marker_y=None, win_h=None,
                         gap=10.0):
    """Parent-relative window_pos that opens a spawned live-value window just
    to the RIGHT of the editor's enclosing window, vertically level with the
    marker — instead of on top of the code the marker sits in.

    window_pos is relative to the spawned window's parent (the same editor
    window), and a marker renders at the cursor (abs_left == marker_x when
    window_pos is 0), so the parent-origin x offset is exactly marker_x:
    subtract it from the window's absolute right edge to land there. Keeping
    window_pos parent-relative means the value window then tracks the editor
    window as it moves. Returns None when there's no enclosing window to
    anchor to (caller falls back to the default on-cursor placement).

    `marker_y` enables the display-bottom clamp: a marker near the screen
    bottom would otherwise spawn its window mostly below the display (the
    pinned-anchor bound in _pinned_base_y clamps to the EDITOR window's box,
    which can itself reach the display bottom, and window_pos is
    deliberately outside that bound). The y offset lifts the window just
    enough that `win_h` (the live height on reopen, an estimate on first
    spawn) fits above the display bottom, floored so the top never leaves
    the screen."""
    if parent_win is None:
        return None
    y_off = 0.0
    if marker_y is not None:
        disp = Core.melty.display_size
        if disp:
            est = win_h or _SPAWN_EST_HEIGHT
            y_off = min(0.0, disp[1] - gap - est - marker_y)
            y_off = max(y_off, -marker_y)      # keep the title bar on screen
    return (parent_win.abs_left + parent_win.width + gap - marker_x, y_off)


def draw_live_view_overlay(x=0, y=0, w=0, h=0, draw_state=None, char_w=8.0,
                           line_px=20.0, node=None, span=None, root=None,
                           line_offset=0, jump_to=None, cursor_line=None,
                           cursor_col=None, **kwargs):
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
    # Viewport cull: the parse walk visits every node in the buffer, not just
    # the visible ones - an off-screen marker is a full render_func call for
    # nothing (its latched value window persists via root_draw_states either
    # way, exactly as when its def scope fades out).
    clip = getattr(draw_state, "abs_clip_rect", None)
    if clip is not None and (y + line_px < clip[1] or y > clip[3]):
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
    # Caret containment in buffer space: span lines are parse-relative, so map
    # through the parse→buffer bridge before comparing with the caret line.
    _lm = kwargs.get("line_map")
    _sl = _lm(span.start_line) if _lm else span.start_line
    cursor_inside = (_sl is not None and cursor_line == _sl
                     and cursor_col is not None
                     and span.start_col <= cursor_col
                     < span.start_col + token_cells)
    imgui.set_cursor_screen_pos((x - pad, y - pad))
    snap = live_values_for(store_obj)
    draw_live_view_marker("/".join(map(str, key_path)),
                          value=snap.get(key_path),
                          captured=key_path in snap,
                          store_obj=store_obj, key_path=key_path,
                          width=token_cells * char_w + 2 * pad,
                          height=line_px + 2 * pad,
                          cursor_inside=cursor_inside,
                          editor_ds=draw_state,
                          buffer_line=(_sl or span.start_line) - 1,
                          name=f"lvm::{_store_name(store_obj)}::"
                               f"{'/'.join(key_path)}")


@render_func(use_cache=False, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, disable_scroll=True, wrap=True,
             z_offset=4, max_height=32)
def draw_live_view_marker(input_value=None, draw_state=None,
                          store_obj=None, key_path=None, captured=False,
                          code_tree_node=None, auto_open=True,
                          corner_radius=4.0, value=None,
                          left_mouse_double_clicked=False,
                          cursor_inside=False, editor_ds=None,
                          buffer_line=None, unique=0, **kwargs):
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
    # flowing into the value windows after the line-key migration).
    comment_args = {}
    lookup_key = None
    if key_path:
        tail = str(key_path[-1])
        lookup_key = (tail.split("#", 1)[1]
                      if tail.startswith("line:") and "#" in tail else tail)
    if isinstance(code_tree_node, dict) and lookup_key:
        _ca = code_tree_node.get("__overrides__", {}).get(
            f"__{lookup_key}__")
        if isinstance(_ca, dict):
            comment_args = {k: v for k, v in _ca.items()
                            if not (isinstance(k, str) and k.startswith("__"))}

    # Input-tab lookup: the context menu resolves this site's inputs off the
    # draw_state graph (draw_input_tab's live_root branch), so attach the same
    # scope dict the comment-args splat reads - restamp the render like
    # everything else per-site. Same name-normalized key as the comment
    # lookup, so line-keyed sites find their statement entry too.
    ds.live_root = code_tree_node
    ds.live_key = lookup_key

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
    from src.lsd.gl_gui.toggles import Toggles
    hover_mode = bool(getattr(Toggles.TextEditor, "live_hover_preview", False))
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
    preview_show = (captured and not open_now
                    and ((hovered and hover_mode) or cursor_inside))
    if preview_show and hover_mode:
        from src.lsd.gl_gui.utils.glfw_utils import request_render
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
    # Outline only under the mouse - the boxes read as clutter when every
    # instrumented site is permanently framed. hover reveals the affordance.
    if hovered:
        dl: _DrawList = imgui.get_window_draw_list()
        dl.add_rect(x, y + 2, x + w, y + h - 3,
                    imgui.get_color_u32_rgba(*base, 0.9 if open_now else 0.6),
                    rounding=corner_radius)
    imgui.dummy(w, h)

    # Double-click toggles the value window. Read RAW imgui here (the same
    # split the single-click version used): the declared event param is the
    # subscription/wake half - its delivery invalidates the tile so the body
    # renders on the very frame is_mouse_double_clicked is true - while the raw
    # read is the single trigger, so the two halves can never toggle twice
    # for one gesture.
    if hovered and imgui.is_mouse_double_clicked(0):
        open_now = not open_now
        ds._lv_open = open_now
        if open_now and win_ds is not None:
            # Reopening: snap the window back to the right of the editor
            # window (it may have been dragged onto the code). The window's
            # real height is known here, so the bottom clamp is exact.
            pos = _right_of_window_pos(ds.parent_window, x, marker_y=y,
                                       win_h=win_ds.height)
            if pos is not None:
                win_ds.window_pos = pos
        ds.invalidate()

    if captured and (open_now or preview_show or win_ds is not None):
        # Full (per-publish) watch once a window exists so the value streams
        # in - the first_only call above only flips the box green.
        watch(store_obj, key_path, ds)
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
        # Named by the cst dict's own stable identity - never draw_state ids.
        win_kwargs = dict(
            name=f"{'/'.join(key_path)}##lv::{_store_name(store_obj)}::"
                 f"{'/'.join(key_path)}",
            mode=Modes.LIVE_WINDOW, closed=not (open_now or preview_show),
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
        # First creation: place the window to the RIGHT of the editor's
        # window, always on top of the code, lifted clear of the editor
        # bottom (estimated height - the real one doesn't exist yet).
        # window_pos persists on the spawned window's draw_state
        # (parent-relative, so it tracks the editor window) - set once; user
        # drags it preserved after.
        if win_ds is None:
            pos = _right_of_window_pos(ds.parent_window, x, marker_y=y)
            if pos is not None:
                win_kwargs["window_pos"] = pos
        _c, _v, win_ds = draw_any(value, **(win_kwargs | comment_args))
        ds._lv_window_ds = win_ds
        # The menu usually opens on the WINDOW - stamp the site context there
        # too (the _parent chain isn't guaranteed to pass through this marker
        # after a root_draw_states re-dispatch).
        win_ds.live_root = code_tree_node
        win_ds.live_key = ds.live_key
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

    # Stamp whether THIS frame's window visibility is caret-held - the X-close
    # should only fire for a window the cursor preview was in.
    ds._lv_cursor_preview_shown = bool(preview_show and cursor_inside)

    return False, None


def set_marker_open(marker_ds, open_):
    """Gutter-button entry point: latch a marker's value window open/closed
    from OUTSIDE the marker body (raw draw-list button, no render_func).
    Mirrors the double-click toggle: flipping open snaps an existing window
    back to the right of the editor window (it may have been dragged onto
    the code). The marker ds is invalidated so its body re-runs and
    creates/hides the window on the next editor render; the CALLER must
    invalidate the editor tile itself (the marker only renders inside the
    editor's overlay pass)."""
    open_ = bool(open_)
    if bool(getattr(marker_ds, "_lv_open", False)) == open_:
        return
    marker_ds._lv_open = open_
    win_ds = getattr(marker_ds, "_lv_window_ds", None)
    if open_ and win_ds is not None:
        # Clear the window's stale closed flag NOW: this latch is set from
        # OUTSIDE the marker body (the gutter runs after a close left
        # closed=True), and the body's own "closed via the header X" check
        # (win_ds.closed and _lv_open) runs before it repaints the window -
        # without this reset it reads the previous close as a fresh X click
        # and cancels the reopen on the spot.
        win_ds.closed = False
        pos = _right_of_window_pos(marker_ds.parent_window,
                                   marker_ds.abs_left,
                                   marker_y=marker_ds.abs_top,
                                   win_h=win_ds.height)
        if pos is not None:
            win_ds.window_pos = pos
    marker_ds.invalidate()


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
    source_lines = _src.split("\n")
    # Viewport cull bounds: the store can have a marker per binding in the
    # def (frame snapshots publish the whole scope), and the walk visits the
    # scope regardless of scroll - every off-screen marker skipped here is a
    # full render_func call saved per frame. Latched value windows persist
    # via root_draw_states without a marker, same as when the whole editor
    # scrolls away.
    _clip = getattr(draw_state, "abs_clip_rect", None)
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
    _memo_ent = draw_state.__dict__.get("_lv_snap_memo")
    if _memo_ent is None or _memo_ent[0] is not _src:
        _memo_ent = (_src, {})
        object.__setattr__(draw_state, "_lv_snap_memo", _memo_ent)
    _snap_memo = _memo_ent[1]
    # Exit-line washes: where the last instrumented run CAME OUT.
    # __live_return_line__ (stamped by live_view.twin_ret / the body-capture
    # profile hook) washes green; __live_error_line__ ((line, msg, text),
    # stamped
    # by live_instrument._stamp_error_line when the run raised) washes red
    # with the message right-aligned on the line - the per-run twin of the
    # editor's routed error markers. Both are absolute file coords, mapped
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
            imgui.get_color_u32_rgba(*_ml_col))
        if _ml_msg:
            _tw = imgui.calc_text_size(_ml_msg).x
            _rdl.add_text(origin_x + _cw - _tw - 8.0, _ry + 2.0,
                          imgui.get_color_u32_rgba(1.0, 0.55, 0.55, 0.95),
                          _ml_msg)
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
            # line:N keys carry a RUN-TIME line stamp - edits since the run
            # shift the code out from under line. The labeled SYMBOL is the
            # real anchor - if the stamped line no longer shows the label,
            # snap to the nearest line in this def that does.
            tail = key_path[-1]
            _mk = ("label", tail)
            _snapped = _snap_memo.get(_mk)
            if _snapped is None:
                label = ((getattr(fn, "__live_labels__", None) or {})
                         .get(key_path)
                         or (tail.split("#", 1)[1] if "#" in tail else None))
                _snapped = _snap_line_to_label(
                    label, rel_line, source_lines,
                    span.start_line, span.end_line)
                _snap_memo[_mk] = _snapped
            rel_line = _snapped
        _ml = _lmap(rel_line) if _lmap else rel_line
        if _ml is None:
            continue        # anchor inside the mid-edit region - skip a frame
        _my = origin_y + (_ml - 1) * line_px
        if _clip is not None and (_my + line_px < _clip[1] or _my > _clip[3]):
            continue        # off-viewport - don't render a marker for it
        if end_col is None:
            # No span (line:N keys) — box the LABELED SYMBOL on the line when
            # the store's label names one (frame-snapshot params and twin
            # with-body keys both carry the local's name; attribute keys
            # box just the FINAL segment - see _label_box_span), falling
            # back to the whole line's text. Full-line boxes stack into an
            # unreadable double-wide strip when several line-keyed values
            # land on adjacent lines (a captured signature), and their click
            # latches swallow the lines.
            if 1 <= rel_line <= len(source_lines):
                text = source_lines[rel_line - 1]
                if not text.strip():
                    continue
                label = (getattr(fn, "__live_labels__", None) or {}).get(key_path)
                span_cols = _label_box_span(label, text)
                if span_cols is not None:
                    start_col, end_col = span_cols
                else:
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
        # Caret containment: _ml is already buffer-space, as are the cursor
        # box's - same test the call-token overlay does.
        _cl, _cc = kwargs.get("cursor_line"), kwargs.get("cursor_col")
        cursor_inside = (_cl == _ml and _cc is not None
                         and start_col <= _cc < end_col)
        imgui.set_cursor_screen_pos(
            (origin_x + start_col * char_w - pad, _my - pad))
        # Auto-open the VOLUMES (3-D data → orbiting voxel window: the
        # point of the lab) so a run pops them unprompted; scalars/configs
        # stay quiet click-to-open boxes so 19 locals don't bury the def.
        # Frame-snapshot keys (context-menu capture - tracked in
        # __frame_snapshot_keys__) never auto-open: opening the menu on a
        # widget must not spawn a window per local tensor.
        # code_tree_node carries the scope dict: the marker reads the site's
        # comment source from it and splats it 1:1 onto the popup window's
        # draw_state (not onto the marker's own wrapper - show_bg=True with a
        # dark tint would draw an opaque bg over the very symbol it boxes).
        draw_live_view_marker(
            "/".join(map(str, key_path)), value=value,
            captured=True, store_obj=fn, key_path=key_path,
            width=max(1, end_col - start_col) * char_w + 2 * pad,
            height=line_px + 2 * pad,
            code_tree_node=node.get("locals") if isinstance(node, dict) else None,
            cursor_inside=cursor_inside, editor_ds=draw_state,
            buffer_line=_ml - 1,
            name=f"lvs::{fn.__qualname__}::{'/'.join(key_path)}",
            auto_open=(is_volume(value) and key_path not in
                       (getattr(fn, "__frame_snapshot_keys__", None) or ())))


def _scope_function(filename, def_line):
    """The live function object for a def at an absolute file line — the same
    resolver capture uses, so the store read here is the store written to."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _enclosing_function)
    try:
        return _enclosing_function(filename, def_line + 1)
    except Exception:
        return None


def _label_box_span(label, text):
    """(start_col, end_col) of the symbol a line-keyed marker should box on
    `text`, or None (caller falls back to the whole line). A plain
    identifier boxes its first word-boundary occurrence. A DOTTED label
    (attribute keys: `draw_state.some_val`) boxes only its FINAL segment,
    matched right after its dotted prefix — so the base name's own marker
    (boxing `draw_state`) and the attribute's (boxing `some_val`) never
    overlap, and each toggles its own value window."""
    if not isinstance(label, str) or not label:
        return None
    if label.isidentifier():
        m = re.search(rf"\b{re.escape(label)}\b", text)
        return m.span() if m is not None else None
    if "." in label:
        prefix, last = label.rsplit(".", 1)
        if not last.isidentifier():
            return None
        m = re.search(rf"\b{re.escape(prefix)}\s*\.\s*({re.escape(last)})\b",
                      text)
        return m.span(1) if m is not None else None
    return None


def _snap_line_to_text(text, rel_line, source_lines, lo, hi):
    """Buffer line an exit-line wash should sit on: the stamped line if its
    current content still equals the stamped run-time `text` (stripped), else
    the NEAREST line in the def's [lo, hi] span with that content — so the
    return/error washes follow their statement through edits the same way
    line-keyed markers follow their symbols. No stamped text (old-shape
    stamp) or no match keeps the stamp unchanged."""
    want = text.strip() if isinstance(text, str) else ""
    if not want:
        return rel_line
    if (1 <= rel_line <= len(source_lines)
            and source_lines[rel_line - 1].strip() == want):
        return rel_line
    best = None
    for ln in range(max(1, lo), min(len(source_lines), hi) + 1):
        if best is not None and abs(ln - rel_line) >= abs(best - rel_line):
            continue
        if source_lines[ln - 1].strip() == want:
            best = ln
    return best if best is not None else rel_line


def _snap_line_to_label(label, rel_line, source_lines, lo, hi):
    """Buffer line a line-keyed marker should anchor on: the stamped line if
    it still contains `label` (the common case — one regex on one line), else
    the NEAREST line in the def's [lo, hi] span that does. Pure string ops on
    the already-split source — the symbol reference is recovered from the
    line stamp without any reparse, so line-keyed captures follow their
    symbols through edits instead of staying pinned where the last run left
    them. No match anywhere (symbol renamed/removed — the value is stale and
    the next run prunes it) keeps the stamp unchanged."""
    if not label:
        return rel_line
    if (1 <= rel_line <= len(source_lines)
            and _label_box_span(label, source_lines[rel_line - 1]) is not None):
        return rel_line
    best = None
    for ln in range(max(1, lo), min(len(source_lines), hi) + 1):
        if best is not None and abs(ln - rel_line) >= abs(best - rel_line):
            continue
        if _label_box_span(label, source_lines[ln - 1]) is not None:
            best = ln
    return best if best is not None else rel_line


def _key_anchor(scope_node, key_path, line_offset):
    """(buffer-relative line, start col | None, end col | None) the symbol
    box for a snapshot key: descend the scope's locals by the key path to the
    leaf's span; a `line:N` tail IS the (absolute) anchor, with no column
    information (the caller boxes the whole line's text)."""
    tail = key_path[-1]
    if tail.startswith("line:"):
        # A '#name' suffix is the per-param qualifier frame snapshots append
        # so several params on one def signature line keep distinct keys.
        try:
            return int(tail[5:].split("#", 1)[0]) - line_offset, None, None
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


@window(initial={"width": 350, "height": 540}, tint=(0.114, 0.1324, 0.16))
@render_func(tint=(0.40, 0.53, 0.78), auto_resize=False)
def live_view_forward(input_value=None, draw_state=None, **kwargs):
    from src.lsd.train.lsd_train import LSD
    from src.lsd.gl_gui.view.mode import Mode
    # NEW_CODE = the full two-pane code_file_io display: the draw_collection
    # structured (code_dict) pane AND the live-overlay text pane side by side.
    draw_function_live(LSD.full_forward_pass_live, name="run_forward_pass runner",
                       source_mode=Mode.NEW_CODE)


def request_run(lab_ds):
    """Ctrl+Enter = the Run button, nothing more: find the lab's
    draw_function runner draw_state and stamp a one-shot run request;
    draw_function pops it on its next render and calls the same _run() a
    button click does (same single-flight _run_busy guard, same twin path —
    the twin already compiles from the pending in-memory source, so running
    IS the latest code). Ctrl+Enter's DUAL dispatch calls this from both
    halves (the Ctrl+F rule: the behavior must live in BOTH places): the
    lab body's blocking on_action while the subtree renders, and draw_main's
    root ctrl_enter fallback when the lab is blit-cached (per-frame
    subscriptions lapse under a cached ancestor, so the root re-routes via
    BVH). At most one half fires per press — the body's blocking sub stops
    the chain before the root's."""
    from src.lsd.gl_gui.utils.glfw_utils import request_render
    for d in lab_ds.descendants(max_depth=8):
        if str(getattr(d, 'name', '')).endswith(" runner"):
            d.misc["_run_requested"] = True
            d.invalidate()          # cached runner must re-render to consume
            request_render()
            return


# use_cache=False: the body must run every frame so its Ctrl+Enter on_action
# re-registers - subscriptions are per-frame, and a blit-cached body would
# drop the action, letting draw_main's global handler win. The two column
# children keep their own tile caches, so the shell itself is all this
# dispatch about.
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
