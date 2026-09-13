import difflib
import inspect
import time
import threading
import traceback
import types
import zlib
from collections import defaultdict
from copy import copy
from enum import Enum
from functools import wraps
from math import ceil
from typing import Any, get_type_hints

import glfw
import imgui
from src.lsd.gl_gui.hdr_color import pack_color
from imgui.core import _DrawList

from src.lsd.gl_gui import mouse_cursor
from src.lsd.gl_gui.background import Background, Pending
from src.lsd.gl_gui.events.input_handler import ALL_ACTIONS
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Counters, Toggles, Tint, SwooshMode
from src.lsd.gl_gui.mode_defaults import ModeDefaults
from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
from src.lsd.gl_gui.view.core_conversion.address import to_address, Address
from src.lsd.gl_gui.view.core_conversion.path_finder import PendingState
from src.lsd.gl_gui.model.core_model.draw_state import DrawState, Hotkey, DragMode, Anchor, Pin, TileMode, AttrDict, TOP_ANCHORS, LEFT_ANCHORS, ExpandMode
from src.lsd.gl_gui.model.core_model.core_enums import PendingAction
from src.lsd.gl_gui.shaped import Shaped
from src.lsd.gl_gui.utils.custom_views import push_style_var, pop_style_var
from src.lsd.gl_gui.utils.glfw_utils import request_render, print_stack_trace, trace_group, get_live_frames
from src.lsd.gl_gui.melty import Melty, apply_collection_action, MeltyState, SearchTerm, search_walk
from src.lsd.gl_gui.view.core_views.basic_view_utils import same_line
from src.lsd.gl_gui.view.core_views.blit_offscreen import snap_int, add_shadow, clear_shadows
from src.lsd.gl_gui.view.core_views.core_meta import AnnotationOverride
from src.lsd.gl_gui.view.core_views.core_undo import UndoManager, handle_undo
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
# Imported as a module (not `from ... import DragDrop`) so hotswapping
# drag_drop.py rebinds through the module object on the next call.
from src.lsd.gl_gui.view.core_views import drag_drop as _drag_drop
from src.lsd.gl_gui.view.invalidation_tracker import Note


# Resolved lazily: core_undo imports new_core_view (via its view_func), which
# calls render_func from this module - importing core_undo at top level would
# close that cycle. handle_undo is only ever called at render time, by which
# point every module is fully loaded, so we bind it on first use and cache it.

melty_state_registry = {}
static_melty = MeltyState()

id_stack = []
stack_holder = {}

# Off-screen window rescue (first-frame clamp on the closable-window scroll
# block): how many pixels of a window must stay grabbable inside the display,
# and how far each successive rescued window is nudged apart so a pile of lost
# windows fans out instead of covering each other.
RESCUE_EDGE_MARGIN = 40
RESCUE_CASCADE_STEP = 30
_offscreen_rescue = {"count": 0}

channels_split_stack = False
child_stack_holder = {}

#2322
def _run_convert_chain(value=None, chain=None, **extra_kwargs):
    """Run a list of converter functions in sequence.

    Each function is called with _converter_mode=True, which triggers
    render_func's lightweight path (no imgui, automatic load_data
    handling and parameter injection).

    Plain functions (not @render_func) are called with manual kwarg
    matching as a fallback.

    Designed to run on a background thread.
    Callable by Background.run (accepts value/0000
    chain as kwargs).

    Returns the final converted value (or Pending).
    """
    if chain is None or value is None or value is UNSET_VALUE:
        return value

    for conv_fn in chain:
        # If it's a render_func, use converter mode - render_func
        # handles load_data and parameter injection internally
        if hasattr(conv_fn, '__wrapped__'):
            result = conv_fn(value, _converter_mode=True, **extra_kwargs)
        else:
            # Plain function fallback - manual kwarg matching
            fn_load_data = getattr(conv_fn, '_load_data', None)
            injectable = dict(extra_kwargs)
            if fn_load_data is not None:
                ref = to_address(value)
                if ref is not None:
                    injectable['data'] = fn_load_data(ref)
                    injectable['ref'] = ref

            sig = inspect.signature(conv_fn)
            call_kwargs = {}
            for p in sig.parameters:
                if p == 'input_value':
                    continue
                if p in injectable:
                    call_kwargs[p] = injectable[p]
            result = conv_fn(value, **call_kwargs)

        if isinstance(result, tuple) and len(result) == 2:
            pending_or_flag, value = result
            if isinstance(pending_or_flag, Pending):
                return pending_or_flag
        else:
            value = result

    return value


# Width of the overlay scrollbar grab. Views override per-call with the
# scroll_bar_width kwarg (declare it as a signature param to change a view's
# default, e.g. draw_text).
SCROLL_BAR_WIDTH_DEFAULT = 4.0

# Multiplier on the bar's alpha (idle and hovered, clamped to 1.0). Views
# override per-call with the scroll_bar_brightness kwarg, same as
# scroll_bar_width.
SCROLL_BAR_BRIGHTNESS_DEFAULT = 1.0

# Track margins around the bar in draw_overlay_scrollbar. The horizontal space
# content region gives up to scroll-capable children - so content doesn't render
# underneath the bar - is scroll_bar_width + this.
SCROLLBAR_MARGIN = 6.0

# Retention group of the scrollbar grab's depth mark (add_shadow `group=`).
# The grab is drawn by the WRAPPER (draw_overlay_scrollbar, and
# BlitCache.draw_freeze_scrollbar for cached freeze_resize views), not by
# the view's body, so its retained mark lives in its own group: a body's
# clear_glows never touches it, and every bar-drawing path clears this group
# (clear_shadows) before deciding whether to draw - a bar that stops drawing
# drops its silhouette instead of re-stacking it under the view.
SCROLLBAR_SHADOW_GROUP = "scrollbar"


def column_boundary(content_width, n_cols, offsets, c):
    """Left-edge x of column ``c`` relative to the content origin.

    Columns start out evenly sized (``content_width / n_cols``). Each boundary
    ``c`` (1..n_cols) is then nudged by ``offsets[c-1]`` pixels when that offset
    exists, applied left-to-right. Offsets are never clamped, so a boundary may
    cross its neighbour or push past ``content_width``. Only the left edge
    (c == 0) is fixed; the right edge (c == n_cols) can move, which is what lets
    the last column take an explicit width. A column's width is the gap between
    its own boundary and the next one.
    """
    base = snap_int(content_width / n_cols * c)
    if offsets and 1 <= c <= n_cols and c - 1 < len(offsets):
        base += offsets[c - 1]
    return base


def set_column_width(column_parent, n_cols, column, column_width):
    """Pin ``column`` to ``column_width`` px on its parent by nudging the
    column's right divider, writing into ``column_parent._column_offsets``.

    The column's left edge is read as-is (so widths set left-to-right compose
    predictably) and the right divider's offset is solved for the requested
    width. The list is padded with zeros as needed and never length-capped; the
    last writer in a frame wins.
    """
    content_width = column_parent.content_width
    offsets = column_parent.column_offsets
    left_boundary = column_boundary(content_width, n_cols, offsets, column)
    base_right = snap_int(content_width / n_cols * (column + 1))
    if len(offsets) <= column:
        offsets.extend([0] * (column + 1 - len(offsets)))
    offsets[column] = left_boundary + column_width - base_right


def column_max_height(column_parent):
    """Max height a column child may occupy: its parent's column viewport.

    Single source of truth for the bound — applied both before the body runs
    (pre-render clamp) and after the end-of-render measurement. The two sites
    MUST agree: the pre-render clamp runs on every call (including cache-served
    frames) while the post-render measure only runs on cache misses, so any
    disagreement makes the height alternate between the two values on every
    invalidation.
    """
    if column_parent.clip_size is None:
        return 1e9
    columns_top = column_parent._columns_top or 0
    return column_parent.clip_size[1] - columns_top + 10



def draw_overlay_scrollbar(draw_state, max_scroll_y, clip_height,
                           bar_width=SCROLL_BAR_WIDTH_DEFAULT,
                           bar_brightness=SCROLL_BAR_BRIGHTNESS_DEFAULT,
                           overlay=False):
    """Draw an interactive vertical scrollbar on top of the view's content.

    Hover and drag are routed through ``draw_state.on_action`` against the
    grab's screen-space rect, so the scrollbar competes for the cursor like any
    other view. Dragging the grab mutates ``draw_state.scroll_offset`` in place;
    the wheel-scroll path in the wrapper still owns wheel input.

    overlay=False (default): the window draw list, a few channels up — the
    normal z-order, and captured into the view's tile with the rest of its
    pixels. Called from the wrapper body (draw_inner_main) for ordinary
    views (the content gives up a gutter for it, ``SCROLLBAR_MARGIN``) and
    from ``BlitCache.draw_freeze_scrollbar`` for cached freeze_resize views
    at rest.

    overlay=True: the BACKGROUND draw list, clipped to the view's clip rect
    — deliberately UNDER the whole UI. blit_offscreen routes exactly one
    frame here: a freeze_resize drag's clean-capture frame, where the bar
    must stay out of the tile capture. The freshly rendered body covers it,
    so the capture takes the body's clean pixels while the grab's on_action
    subs and shadow mark still run. Every other freeze frame uses the
    normal window-list path above — mid-drag nothing re-captures the frozen
    tile, so drawing it "regularly" there is both un-baked and correctly
    colored (the pre-filter brightness / shadow-composite chain; the
    foreground list, tried first, renders after the filters and always
    read slightly darker).

    Either way the grab stamps an add_shadow depth mark
    (``Toggles.ScrollSettings.scrollbar_shadow_offset``): its own raised
    silhouette in the depth map, so the composite lights it as a thing of
    its own instead of as part of the view's edge (specular rim, neighbours'
    shadows) — that is what kept the window-list bar looking lighter / more
    shadowed than the overlay one. Inside a recording body the mark bakes
    into the tile's cached mask like the bar itself; from blit_offscreen it
    is re-issued every frame.
    """
    # This call owns the grab's retained depth mark for the frame: clear
    # its group FIRST (before any early return), and the add_shadow below
    # re-retains it only if the bar actually draws. Without this the
    # last grab mark kept re-stamping every frame after the bar hid -
    # content shrunk to fit, pane grown past its content.
    clear_shadows(draw_state, SCROLLBAR_SHADOW_GROUP)
    if max_scroll_y <= 0 or clip_height <= 0:
        return

    content_height = draw_state.abs_content_height
    if content_height <= 0:
        return

    # Viewport in screen space - the scroll region starts under the header.
    # The overlay bar reads the LIVE position: the cached abs_left/abs_top
    # lag a blit-served drag by a frame and the bar would clip the view.
    if overlay:
        view_left = draw_state._abs_left()
        view_top = draw_state._abs_top() + draw_state.header_height
    else:
        view_left = draw_state.abs_left
        view_top = draw_state.abs_top + draw_state.header_height
    view_width = draw_state.width
    bar_offset = -1.0

    # Track geometry, glued to the right edge of the viewport.
    margin = 2.0
    track_x2 = view_left + view_width - margin + bar_offset
    track_x1 = track_x2 - bar_width + bar_offset
    track_y1 = view_top + margin
    track_y2 = view_top + clip_height - margin - draw_state.header_height
    track_h = track_y2 - track_y1
    if track_h <= 0.0:
        return

    # Proportional grab with a floor; travel is how far the grab can slide.
    ratio = clip_height / content_height
    grab_h = max(20.0, min(track_h, ratio * track_h))
    travel = max(0.0, track_h - grab_h)

    scroll_y = draw_state.scroll_offset[1]
    t = max(0.0, min(1.0, scroll_y / max_scroll_y))
    grab_y1 = track_y1 + travel * t
    grab_y2 = grab_y1 + grab_h

    # Interaction: hit-test the grab rect, support hover + left-drag on it. A
    # large priority_delta lets the narrow grab win the cursor over the content
    # views nested beneath it. The drag is latched by the event handler's drag
    # capture (set on mousedown in the view), so on_action keeps feeding it
    # even after the cursor leaves the grab/view - no hover bookkeeping here.
    grab_rect = (track_x1, grab_y1, track_x2, grab_y2)
    hovered = draw_state.on_action("cursor_hover", view_id="scrollbar_grab",
                                   rect=grab_rect, priority_delta=15) is not None
    held = draw_state.on_action("left_mouse_held", view_id="scrollbar_grab",
                                rect=grab_rect, priority_delta=30)
    drag = draw_state.on_action("left_mouse_down", view_id="scrollbar_grab",
                                rect=grab_rect, priority_delta=30)
    drag = draw_state.on_action("left_mouse_clicked", view_id="scrollbar_grab",
                                rect=grab_rect, priority_delta=30)
    drag = draw_state.on_action("left_mouse_drag", view_id="scrollbar_grab",
                                rect=grab_rect, priority_delta=30)
    active = drag is not None
    if active and travel > 0.0 and Melty.frame_count > 2:
        # Map grab pixel motion back into scroll-offset motion.
        scroll_per_px = max_scroll_y / travel
        new_y = max(0.0, min(scroll_y + drag.dy * scroll_per_px, max_scroll_y))
        draw_state.scroll_offset = (draw_state.scroll_offset[0], new_y)
        Melty.selected = {draw_state}
        # Re-place the grab so it tracks the cursor on the same frame.
        t = max(0.0, min(1.0, new_y / max_scroll_y))
        grab_y1 = track_y1 + travel * t
        grab_y2 = grab_y1 + grab_h

    # Depth mark under the grab - see the docstring. Clip to the view's clip
    # rect (a parent scroll / window edge), not the live clip snapshot.
    shadow_offset = Toggles.ScrollSettings.scrollbar_shadow_offset
    if shadow_offset and grab_y2 > grab_y1:
        add_shadow((track_x1, grab_y1, track_x2 - track_x1, grab_y2 - grab_y1),
                   offset=shadow_offset, corner_radius=3.0,
                   clip=draw_state.abs_clip_rect or True, draw_state=draw_state,
                   group=SCROLLBAR_SHADOW_GROUP)

    if active:
        # The latched drag is delivered even off-view, but the latch only runs
        # while the scroll node re-renders - and the node otherwise only redraws
        # when hovered or when the scroll changes. Moving the cursor off-view
        # (or holding it still past the track end) stops both, so the node goes
        # idle and stops consuming the drag. Keep it re-rendering while the drag
        # is held so the latch is read every frame regardless of cursor pos.
        Melty.cache.invalidate(draw_state._tile_id)
        request_render()

    # Paint to the overlay list so the bar floats above clipped content.
    tint = draw_state.current_tint
    grab_alpha = min(1.0, (0.9 if (hovered or active) else 0.5) * bar_brightness)
    if tint is not None:
        col_grab = pack_color(*tint[:3], grab_alpha)
    else:
        col_grab = pack_color(1, 1, 1, grab_alpha)
    col_border = imgui.get_color_u32(imgui.COLOR_BORDER)

    overlay_clip = None
    if overlay:
        dl = imgui.get_background_draw_list()
        # The background list has no window clip of its own; scissor the bar
        # to what the view actually shows (a parent scroll / window edge).
        overlay_clip = draw_state.abs_clip_rect
        if overlay_clip is not None:
            dl.push_clip_rect(*overlay_clip, True)
    else:
        dl = imgui.get_window_draw_list()
        # Channels above the view's body the grab paints on. The bar must
        # sit over EVERY piece of window-list chrome drawn across a view
        # from a shallower depth: the code editor's compare ribbons draw at
        # get_channel() + 10 and its take arrows at + 11 (take_files /
        # merge_files) from the editor WINDOW's depth, and the pane the bar
        # belongs to is never shallower than that window, so + 12 clears
        # them at any nesting. (Blit's served-frame bar already landed on
        # top - it paints after the tile image - so this only matched the
        # live view to date.) Bump this if a new chrome offset outgrows it.
        channel_lift = 12
        # Only while the window list is actually split (the split is lazy —
        # Melty.channels_split; blit's draw_freeze_scrollbar can run when it
        # is not), and clamped into the split's channel count: get_channel()
        # caps at max_depth - 1, so the lift can run past the last channel
        # at deep nesting and imgui asserts on an out-of-range channel.
        if Melty.channels_split:
            dl.channels_set_current(max(0, min(Melty.get_channel() + channel_lift,
                                               Melty.max_depth - 1)))
    # dl.add_rect(track_x1, track_y1, track_x2, track_y2, col_border, rounding=3.0)
    if grab_y2 > grab_y1:
        dl.add_rect_filled(track_x1, grab_y1, track_x2, grab_y2, col_grab, rounding=3.0)
        # dl.add_rect(track_x1, grab_y1, track_x2, grab_y2, col_border, rounding=3.0)
    if overlay and overlay_clip is not None:
        dl.pop_clip_rect()



# ── Auto draw_state params ──────────────────────────────────────────────────
# Every named render_func parameter (minus the exclusions below) is mirrored
# on the view's draw_state as a plain attribute: draw_state.<param>. This
# automates the old manual pattern of declaring a param AND a duplicate
# DrawState field.
#
# Per-call resolution (the "kwargs gauntlet", high → low priority):
#   caller-passed kwarg / Mode override          (explicit_param_keys)
#   draw_state.auto_params[<name>]              (diverged internal state)
#   type / attrib / annotation / codec default  (Melty default maps)
#   decorator o_kwargs → signature default
#
# draw_state.auto_params holds DIVERGED values only: when view code writes
# draw_state.speed = x, the next frame's divergence scan (x no longer
# matches the _auto_baseline mirror of the last resolved value) promotes it
# into auto_params, where it overrides the default layers until cleared
# (del draw_state.auto_params['speed']) - but an explicitly passed kwarg or
# Mode override still wins the resolution. Params that were never written
# stay out of auto_params, so Mode / class-annotation / codec defaults keep
# flowing live frame to frame. auto_params is a public (non-underscore)
# DrawState field, so it serializes whenever non-empty - diverged state
# persists across saves; at-default params save nothing.
#
# Params whose name is an existing DrawState field/property are SKIPPED - they
# keep their legacy manual handling. To migrate one, delete the field from
# DrawState.__init__ (and its no_init/exclude entries): the name leaves the
# reserved set and auto-state takes over. Opt a param out entirely with
# @render_func(auto_state=False).

_AUTO_PARAM_EXCLUDE = {
    # injected by set_default / the wrapper itself
    'input_value', 'draw_state', 'name', 'unique', 'suffix', 'window_stack',
    'func', 'render_func', 'style_manager', 'vis', 'view_func', 'outer_func',
    # signature plumbing
    'kwargs', 'args', 'o_kwargs', 'next_kwargs', 'changed',
    # per-call context / converter args, never view state
    'key', 'collection', 'meta', 'mode', 'current_mode', 'real_type',
    'data', 'ref', 'chain', 'initial', 'auto_state',
}

# Object-attached params: a VALUE OBJECT can set one of these as a
# plain attribute (e.g. auroras.tint) and the wrapper injects it as a render
# kwarg - the LOWEST-priority source (setdefault: explicit kwargs, mode,
# comment args, decorator defaults, this, same as the old hardcoded
# hasattr(input_value, "tint") elif this replaces). Whitelisted so arbitrary
# object attrs never leak into kwargs; separate from the set-anywhere
# whitelist (anywhere.SET_ANYWHERE_PARAMS).
OBJ_ATTR_PARAMS = ("tint",)

_EVENT_SUFFIXES = tuple(f"_{a}" for a in ALL_ACTIONS)


def _is_event_param_name(name):
    """True when `name` is an input-event subscription per the event-param
    auto-subscribe convention (enter_key_pressed, inverted_ctrl_f_down, ...).
    Event values are injected per-frame from Melty.events — persisting one on
    the draw_state would replay the event on every following frame."""
    n = name[3:] if name.startswith('on_') else name
    return n in ALL_ACTIONS or n.endswith(_EVENT_SUFFIXES)


def _values_differ(a, b):
    """Divergence test for the auto-state scan. Identity first (the common
    no-write case: the attr still holds the exact object the mirror wrote),
    then equality so re-writing an equal scalar/tuple doesn't stick the param.
    Values whose == doesn't reduce to a bool (numpy arrays, tensors) count as
    diverged — identity is the only sane check for those."""
    if a is b:
        return False
    try:
        return bool(a != b)
    except Exception:
        return True


_ds_reserved_cache = None
_ds_reserved_for_cls = None



def _draw_state_reserved_names():
    """Every name the current DrawState already owns (init fields, properties,
    methods). Auto-state must not touch these — a param sharing one of these
    names keeps its legacy manual handling. Returns None while DrawState isn't
    constructible yet (Core.melty not up); callers just skip auto-state for
    that call and retry next time. Recomputed when the class object changes
    (hotswap of draw_state.py)."""
    global _ds_reserved_cache, _ds_reserved_for_cls
    if _ds_reserved_for_cls is not DrawState:
        try:
            fields = set(vars(DrawState()))
        except Exception:
            return None
        _ds_reserved_cache = fields | set(dir(DrawState))
        _ds_reserved_for_cls = DrawState
    return _ds_reserved_cache


# type -> codec class (or None), resolved once per type via the codec
# registry's MRO walk. Codec identity is hotswap-stable (classes patch in
# place), and render_kwargs is read per-call with getattr, so editing a
# codec's tint takes effect without busting this cache.
_codec_by_type_cache = {}


def _codec_for_type(value_type):
    """The codec class registered for `value_type` (MRO walk, like
    code_file_io's codec resolution), or None. Drives both the codec's
    render_kwargs base layer and the Melty.codec_stack data-source context."""
    codec = _codec_by_type_cache.get(value_type, _codec_by_type_cache)  # sentinel: self
    if codec is _codec_by_type_cache:
        codec = None
        try:
            from src.lsd.gl_gui.view.core_conversion.new_codecs import type_to_codec
            for base in value_type.__mro__:
                codec = type_to_codec.get(base)
                if codec is not None:
                    break
        except Exception:
            codec = None
        _codec_by_type_cache[value_type] = codec
    return codec


def _codec_render_kwargs(value_type):
    """The render_kwargs dict of `value_type`'s codec — {} when no codec
    matches or the codec declares nothing."""
    codec = _codec_for_type(value_type)
    if codec is None:
        return {}
    return getattr(codec, "render_kwargs", None) or {}


def _owning_window(ds):
    """The nearest window that owns `ds`: the draw_state itself when it IS a
    window (closable, or a top-level root), else its parent_window. This is
    NOT root_window — nested windows all share a root, and Ctrl+F routing
    must distinguish sibling nested windows from each other."""
    if ds is None or ds.closable or ds.parent_window is None:
        return ds
    return ds.parent_window


def _window_descends_from(win, ancestor):
    """True when window `win` is a STRICT descendant of window `ancestor`
    (its parent_window chain reaches `ancestor`; a window is not its own
    descendant). Bounded walk, self-loop guarded — used by the Ctrl+F
    routing to keep parent windows from stealing the key from searchables
    inside their nested children."""
    if win is None or ancestor is None or win is ancestor:
        return False
    node = win.parent_window
    for _ in range(32):
        if node is None:
            return False
        if node is ancestor:
            return True
        nxt = node.parent_window
        if nxt is node:
            return False
        node = nxt
    return False


def _selection_for_search(owner_ds):
    """Selected text to prefill the find box with on Ctrl+F, or None. The
    selection lives on the text-focused editor's draw_state
    (text_selection_start/end against its _raw_input_value buffer). Only
    honored when that editor is the searchable view itself or one of its
    descendants, so a selection in some other window never leaks into this
    view's search. The find box itself is never a source."""
    ds = Melty.text_focused_ds
    if ds is None or getattr(ds, "is_search_box", False):
        return None
    node = ds
    while node is not owner_ds:
        parent = node._parent
        if parent is node or parent is None:
            return None
        node = parent
    lo, hi = ds.text_selection_start, ds.text_selection_end
    if lo > hi:
        lo, hi = hi, lo
    text = ds._raw_input_value
    # Selection offsets index the editor's DISPLAY text - the buffer with
    # every collapsed fold's hidden lines spliced out (_fold_build) - not
    # the raw buffer. Slice the text the offsets were set against, else
    # a collapsed region above the selection shifts the slice and the
    # find box seeds the wrong text. _fold_cache = (source, depth, built),
    # built = (display_text, segments, spans, disp_to_buf); disp_to_buf
    # None means identity (nothing collapsed), where raw slicing is fine.
    fold_cache = getattr(ds, "_fold_cache", None)
    if (fold_cache is not None and fold_cache[0] is text
            and fold_cache[2][3] is not None):
        text = fold_cache[2][0]
    if lo == hi or not isinstance(text, str):
        return None
    # Trim surrounding whitespace so a line-swipe selection (which picks up
    # both neighboring newlines) seeds the bare line. Interior newlines are
    # kept so a multi-line selection becomes a multi-line search term.
    sel = text[lo:hi].strip()
    if not sel:
        return None
    return sel

def _str_change_span(old, new):
    """(start, end) of the region of `new` that differs from `old` — common
    prefix / common suffix trimmed (suffix bounded so it never overlaps the
    prefix). end == start when the change was a pure deletion in `new`."""
    n_old, n_new = len(old), len(new)
    lim = min(n_old, n_new)
    p = 0
    while p < lim and old[p] == new[p]:
        p += 1
    s = 0
    while s < lim - p and old[n_old - 1 - s] == new[n_new - 1 - s]:
        s += 1
    return p, n_new - s


def _rebase_resize_baselines(draw_state, drag, from_top_left):
    """Re-latch the corner-resize baselines so the running formulas are
    continuous at the CURRENT size / position / drag total:

        size = init_size + sign * total          (sign = -1 top-left, +1 bottom-right)
        pos  = init_pos  + (init_size - size)    (top-left: the far corner stays put)
        pos  = init_pos                          (bottom-right: sticky re-anchors hold it)

    Used when the corner flips mid-drag (left button pressed / released
    during a right-drag) and when a resize (re)activates with a non-zero
    total (press and first drag frame compressed together after a stall).
    At a normal drag start total ≈ 0 and this is a no-op."""
    sign = -1 if from_top_left else 1
    draw_state._initial_window_size = (draw_state.width - sign * drag.total_dx,
                                       draw_state.height - sign * drag.total_dy)
    if from_top_left:
        draw_state._initial_window_pos_resize = (draw_state.window_pos[0] - drag.total_dx,
                                                 draw_state.window_pos[1] - drag.total_dy)
    else:
        draw_state._initial_window_pos_resize = (draw_state.window_pos[0], draw_state.window_pos[1])


def render_func(*args, **o_kwargs):
    func = args[0] if args else None
    if not callable(func):
        def class_wrapper(the_func):
            return render_func(the_func, *args, **o_kwargs)

        return class_wrapper

    # Session-teardown hook (see run_cleanup_callbacks): decorator-only, never
    # a render kwarg, so kick it out before o_kwargs merge into call kwargs.
    on_cleanup = o_kwargs.pop("on_cleanup", None)

    sig = inspect.signature(func)
    params = sig.parameters
    param_types = [params[p].annotation for p in params]
    # A module under `from __future__ import annotations` (PEP 563) hands us
    # STRINGS here, and the injected-state path (`set_default`'s misc type:
    # `panel_state: PanelState = None`) needs the class to instantiate - so
    # resolve string annotations against the function's globals. Only the
    # string ones: a real class passes through untouched, and a name that
    # doesn't resolve (lazy import) stays a string for now.
    if any(isinstance(annotation, str) for annotation in param_types):
        try:
            resolved_hints = get_type_hints(func)
        except Exception:
            resolved_hints = {}
        param_types = [resolved_hints.get(p, params[p].annotation)
                       if isinstance(params[p].annotation, str) else params[p].annotation
                       for p in params]
    name_to_param_type = {}
    for idx, param_name in enumerate(params):
        name_to_param_type[param_name] = param_types[idx]

    wanted_params = list(params.keys())
    wanted_params.remove("args") if "args" in wanted_params else None
    wanted_params.remove("o_kwargs") if "o_kwargs" in wanted_params else None
    # Decorator kwargs - tint included - are the SourcePriority.DECORATION
    # layer: merged UNDER the per-call kwargs below (`merge_o_kwargs | kwargs`),
    # so a `@window(tint=...)` (draw_main passes the registration's kwargs as
    # caller kwargs), a Mode tint, a caller or a `# [tint=...]` comment all
    # outrank the `@render_func(tint=...)` tint, which only paints a view when
    # nothing above it supplies one. (It was stripped here for a while so an
    # invocation never took its func's tint; re-added 09-08 at this rank.)
    # The render_func tint here also lets a parsed-class @defaults tint -
    # AT_DEFAULT_CODE_TYPE, above DECORATION - beat it, see _tint_is_decoration.
    merge_o_kwargs = o_kwargs
    header_defaults = merge_o_kwargs
    _decoration_tint = o_kwargs.get("tint")

    def _tint_is_decoration(kwargs):
        """True when the tint in kwargs is still the decorator's own object —
        no higher layer (caller / @window / mode / comment / instance attr)
        replaced it — so a source ranked above DECORATION in SourcePriority
        but applied later in the chain may take over."""
        return _decoration_tint is not None and kwargs.get("tint") is _decoration_tint
    param_defaults = {p: params[p].default for p in params if params[p].default is not inspect.Parameter.empty}
    # Decoration-time plan for the per-call "get the params the caller
    # didn't pass" loop: (param, wanted_type) with the pass-through names
    # already dropped and `Parameter.empty` already mapped to None, so the
    # per-call loop is one membership test per param (90 wrapper calls a
    # frame × ~50 params on the largest views).
    _default_plan = tuple(
        (p, (None if name_to_param_type.get(p) is inspect.Parameter.empty
             else name_to_param_type.get(p)))
        for p in wanted_params
        if p not in ("kwargs", "args", "o_kwargs", "next_kwargs"))

    # Auto-state param names for this func, resolved lazily (DrawState must be
    # constructible to know what names its real fields reserve) and cached
    # until the reserved set changes (DrawState hotswap).
    _auto_params_cache = ()
    _auto_params_for = None

    def _auto_state_params():
        nonlocal _auto_params_cache, _auto_params_for
        reserved = _draw_state_reserved_names()
        if reserved is None:
            return ()
        if reserved is not _auto_params_for:
            out = []
            for p in wanted_params:
                if p in _AUTO_PARAM_EXCLUDE or p in reserved or _is_event_param_name(p):
                    continue
                ann = name_to_param_type.get(p)
                if (ann is not inspect.Parameter.empty and inspect.isclass(ann)
                        and param_defaults.get(p) is None):
                    # Injected-state param (CodeObject / GLState / ...): owned
                    # by auto_default's misc type path, which also handles
                    # type-mismatch discard across hotswap. Keep it there.
                    continue
                out.append(p)
            _auto_params_cache = tuple(out)
            _auto_params_for = reserved
        return _auto_params_cache


    """
    Decorator for render functions.
    - Computes stable UI ID (unique) from callstack+meta.
    - Provides a per-widget viewstate object (with .unique).
    - Injects meta/viewstate only if the function signature wants them.
    - Pushes/pops ImGui ID scope automatically.
    """

    # Auto-expose load_data / save_data as attributes for auto discovery
    _rf_load_data = o_kwargs.get('load_data', None)
    _rf_save_data = o_kwargs.get('save_data', None)


    @wraps(func)
    def wrapper(input_value=None, **kwargs):
        _wt0 = time.perf_counter()   # TEMP DEBUG: wrapper prologue/inner split
        _ves_pushed = False          # this call entered the exclusive-time cache

        # ── imgui availability ──────────────────────────────────────
        # When called from a background thread (_converter_mode=True),
        # the full render_func runs but all imgui calls are skipped.
        # The converter still gets: unique ID, draw_state, caching,
        # parameter injection, load_data/save_data handling.
        _has_imgui = not kwargs.pop('_converter_mode', False)

        if _has_imgui and Melty.in_annotation_mode():
            # The function is being evaluated as a field annotation / class
            # decorator (e.g. `alpha: as_float(min_value=15.0)`), not actually
            # rendering - at startup (global annotation_mode) or inside a
            # recompile's exec (thread-local annotation_scope). Hand the
            # positional value and 1the call's kwargs to annotation_track,
            # which returns a carrier the metain data into Melty's
            # default kwargs. Never falls through to a real render.
            return annotation_track(input_value, wrapper=wrapper, call_kwargs=kwargs)

        modes = kwargs.get("mode", None)
        if not isinstance(modes, tuple):
            modes = (modes,) if modes is not None else None

        drives = kwargs.pop("drives", None)
        lens_func = None
        if drives is not None:
            if isinstance(drives, tuple):
                driven_value = drives[0]
                lens_func = drives[1]
            else:
                driven_value = drives
                # Shape-reflection (Shaper) lenses first, then the real-type
                # registry - same order as view routing.
                lens_func = Melty.get_default_lens_function(driven_value)

            if lens_func is not None:
                kwargs['input_value'] = input_value
                changed, value = lens_func.__wrapped__(driven_value, view_func=wrapper, lens_func=lens_func, child_kwargs=kwargs,
                                           _converter_mode=True)
                if changed:
                    Core.melty.cache.invalidate_up_by_obj(driven_value)
                return changed, value

        mode_stacked = False
        # if modes is None:
        #     default_mode = ModeDefaults.default_mode_from_type.get(type(input_value), None)
        #     if default_mode is not None:
        #         modes = [default_mode]

        if modes is not None:
            mode_config = modes[0].value.get(type(input_value), None)
            if mode_config is not None and mode_config.recursive:
                Melty.mode_stack.append(modes[0])
                mode_stacked = True

            not_recursive = []
            for mode in modes:
                if mode is not None:
                    mode_config = mode.get_config_for(input_value)
                    if mode_config is not None and mode_config.kwargs is not None:
                        override_kwargs = mode_config.kwargs.copy()
                        kwargs = kwargs | override_kwargs
                        kwargs['current_mode'] = mode
                        if not mode_config.recursive:
                            not_recursive.append(mode)
                        else:
                            kwargs['mode'] = mode

            for mode in not_recursive:
                if mode in kwargs and kwargs['mode'] == mode:
                    kwargs.pop('mode', None)


        # Auto-state: the keys explicitly provided for THIS call - by the
        # decorator or a Mode override - captured before any default layer merges
        # in.  The auto-state value yields to these, but overrides every
        # default layer (type/attrib defaults, codec render_kwargs, decorator
        # o_kwargs, signature default).
        explicit_param_keys = set(kwargs)
        kwargs.pop('mode', None)

        kwargs = Melty.default_kwargs_by_type[kwargs.get("real_type", type(input_value))] | kwargs
        as_window = kwargs.get("as_window", False)

        if as_window:
            kwargs['show_bg'] = True
            kwargs['z_offset'] = 0
            kwargs['selectable'] = False
            kwargs['use_cache'] = True
            kwargs['melty_window'] = True
            kwargs['closable'] = True
            kwargs['auto_resize'] = False
            kwargs['draggable'] = True
            kwargs['show_tint'] = True
            kwargs['show_header'] = True
            kwargs['disable_scroll'] = False

            from src.lsd.gl_gui.view.core_views.headers import draw_header_end
            from src.lsd.gl_gui.view.core_views.headers import draw_header
            # A caller-passed header wins (SourcePriority: caller kwargs beat
            # defaults) - the window chrome fills in the empty slots, so a
            # custom header like fast_dock.dock_header isn't clobbered here.
            if kwargs.get('with_header_end') is None:
                kwargs['with_header_end'] = draw_header_end
            if kwargs.get('with_header') is None:
                kwargs['with_header'] = draw_header


        # kwargs = Melty.default_kwargs_by_attrib_type[kwargs.get("type_collection", type(collection))][key] | kwargs

        # A view that cannot size itself (its @render_func has
        # determines_height=False or fill_height - draw_text, draw_texture,
        # layouts) drawn at root level inside an OS-window surface fills the
        # window (Melty.root_fill is stamped by Surface.frame): the first such
        # view of the frame gets the remaining height, all get the width,
        # headerless (the OS window has the title bar). Self-sizing views
        # (buttons, fields) lay out naturally - so a body can put a row of
        # widgets above an editor.
        _fill = Melty.root_fill
        if (_fill is not None and not Melty.melty_window_stack
                and (merge_o_kwargs.get('determines_height', True) is False
                     or merge_o_kwargs.get('fill_height') is not None)
                and not kwargs.get('closable') and not kwargs.get('glfw_window')
                and kwargs.get('parent_window') is None):
            if kwargs.get('width') is None:
                kwargs['width'] = _fill[0]
            if kwargs.get('height') is None and not Melty.root_fill_used:
                _y = imgui.get_cursor_screen_pos()[1] if _has_imgui else 0.0
                kwargs['height'] = max(40.0, _fill[1] - max(0.0, _y - _fill[2]))
                Melty.root_fill_used = True
            kwargs.setdefault('auto_resize', False)
            kwargs.setdefault('show_header', False)

        passed_width = kwargs.get('width', None)
        passed_height = kwargs.get('height', None)

        if kwargs.get("horizontal", False):
            kwargs['disable_scroll'] = True

        Melty.silence_invalidate = True
        start_time = time.time()
        if kwargs.get("bypass", False):
            kwargs.pop("bypass", None)
            return func(**kwargs)
        return_extras = kwargs.get('return_extras', False)
        name = kwargs.get("name", "")
        active_layer = kwargs.get("active_layer", None)

        return_value = None
        # Captured from the header render (e.g. an in-header tint edit) so it can
        # be folded into the view's own changed/value result downstream.
        header_changed = False
        header_return = None
        is_root = Melty.depth == 0
        input_value = kwargs.get("input_value", input_value)


        content_margin = ((len(Melty.bg_stack)) * 2.0)

        kwargs = merge_o_kwargs | kwargs

        if header_defaults is not None:
            kwargs = header_defaults | kwargs

        # Read AFTER the o_kwargs merge so decorator-level initial={...} is
        # seen; a caller-passed initial still wins (kwargs takes precedence).
        initial_values = kwargs.get("initial", {})

        # Object-attached params (OBJ_ATTR_PARAMS): a value object's own
        # whitelisted attrs (Loras.tint) feed in as ordinary kwargs. This
        # lives HERE, in the always-run prologue, NEVER in the cache-gated
        # render path: a kwarg injected only on full renders makes
        # draw_state._kwargs oscillate between cached and uncached frames
        # (the tint tab read that as a value ↔ + flicker). setdefault
        # semantics: every caller/mode/decorator source wins. Dict values carry
        # their config in __overrides__ instead (fed later, render path).
        if input_value is not None and not isinstance(input_value, dict):
            for _oa in OBJ_ATTR_PARAMS:
                if _oa not in kwargs:
                    _oav = getattr(input_value, _oa, None)
                    if _oav is not None:
                        kwargs[_oa] = _oav

        # __overrides__ dict args feed HERE too - same lesson as the
        # OBJ_ATTR injection above: feeding only in the cache-gated render
        # path makes draw_state._kwargs oscillate between full and replay
        # frames (a comment-tinted row read tint=None on replay - the header
        # swatch and the tint tab showed different values for one ds). The
        # render-path block still covers convert_in-produced dicts; feeding
        # twice is idempotent (same values, same keys).
        _pcol = kwargs.get("collection")
        if isinstance(_pcol, dict) and "key" in kwargs:
            _povs = _pcol.get("__overrides__")
            if isinstance(_povs, dict):
                _pfov = _povs.get(f"__{kwargs['key']}__")
                if isinstance(_pfov, dict):
                    for _ok, _ov in _pfov.items():
                        if not (isinstance(_ok, str) and _ok.startswith("__")):
                            kwargs[_ok] = _ov
        elif isinstance(input_value, dict):
            _povs = input_value.get("__overrides__")
            if isinstance(_povs, dict):
                for _ok, _ov in _povs.items():
                    if not (isinstance(_ok, str) and _ok.startswith("__")):
                        kwargs[_ok] = _ov

        # ExpandMode.MANUAL: the view func always runs (gate in draw_inner_main)
        # and owns its collapsed rendering, so every wrapper shortcut keyed to
        # "collapsed -> no content" must back off for it: sizes keep flowing,
        # bg isn't forced, collapse/expand rects aren't juggled, convert_in
        # keeps running. AUTO (default) keeps the legacy skip-the-func path.
        manual_expand = kwargs.get("expanded_mode", None) is ExpandMode.MANUAL
        _wtA = time.perf_counter()   # TEMP perf: end of kwargs/mode merge

        if _has_imgui and not Melty.channels_split:
            draw_list = imgui.get_window_draw_list()
            draw_list.channels_split(Melty.max_depth)
            Melty.channels_split = True

        if name == "" and is_root:
            kwargs["name"] = "Unnamed" + func.__name__ + kwargs.get("collection", None).__class__.__name__
            name = kwargs["name"]

        name_func = kwargs.get("name_func", None)
        if name_func is not None:
            try:
                name = name_func(input_value)
                if not isinstance(name, str):
                    name = str(name)
            except Exception as e:
                name = str(f"{e}")

        key = kwargs.get("key", "")
        if name == "" and not is_root:
            if isinstance(input_value, (int, float, str, bool)):
                str_input = str(input_value)
                short = str_input[:10] + "..." if len(str_input) > 10 else str_input
                sanitize = short.replace(" ", "_").replace("\n", "_").replace("%", "_").replace("/", "_")
                name = sanitize + kwargs.get("collection", None).__class__.__name__
            else:
                name = str(key) + kwargs.get("collection", None).__class__.__name__

        if Melty.depth > Melty.max_depth:
            if return_extras:
                return False, None, None
            return False, None

        # ----- Unique computation BEFORE pushing ID scope (avoid divergence) -----
        old_suffix = kwargs.get("suffix", None)
        unique_name = kwargs.get("unique_name", name)

        index = key if isinstance(key, int) else 0
        suffix = Melty.unique_stack[-1] if len(Melty.unique_stack) > 0 else (name or "")
        column = str(kwargs.get("column", ""))

        # Keep original behavior of always appending name (even if empty)

        suffix = f"{old_suffix}_{suffix}_{unique_name}_{key}"

        # A deferred re-entry from Melty.draw (the layer pass) brings the
        # window's own unique, and parks the cursor at its ABS position.
        deferred_entry = "layer_unique" in kwargs
        if deferred_entry:
            unique = kwargs.pop("layer_unique")
        else:
            if len(Melty.melty_window_stack) > 0:
                _rw = Melty.melty_window_stack[-1]
                # The floating dragged item renders as its own window, so its
                # descendants would hash the item's name here instead of the
                # home window's - fresh uniques, fresh draw_states, expanded/
                # scroll state gone every pickup. Keep the inline identity:
                # while the item floats, its subtree hashes the home window.
                if (_drag_drop.DragDrop.active
                        and _rw is _drag_drop.DragDrop.item_ds
                        and _rw.parent_window is not None):
                    root_window_name = _rw.parent_window.name
                else:
                    root_window_name = _rw.name
            else:
                root_window_name = "Root"
            if is_root:
                unique = ui_id(suffix=name + unique_name + str(key) + func.__name__)
                suffix = f"{unique_name}_{func.__name__}_{unique}_{key}"
            else:
                unique = ui_id(suffix=suffix + unique_name +
                                      name + root_window_name +
                                      str(key) + func.__name__, idx=index)

        draw_state: DrawState = kwargs.get("draw_state", get_draw_state(unique))
        closable = kwargs.get("closable", False)
        detached = kwargs.get("detached", False)
        draw_state._view_func = func
        draw_state._wrapper = wrapper

        # glfw_window=True requests a CHILD GLFW WINDOW of the active surface
        # (surface.py). Like closable=True the view renders nothing here -
        # app.py opens a Surface whose body draws this view (Melty.
        # draw_surface_root) and the result comes back a frame later
        # through returned_values. The child surface's draw call carries no
        # glfw_window, so it renders normally there.
        if kwargs.get('glfw_window') and not deferred_entry:
            _tile = f"{name}##{strhash(str(unique) + str(draw_state.id))}"
            draw_state._tile_id = _tile
            Melty.surface_window_request(_tile, name, input_value, kwargs, draw_state)
            return_value = Melty.returned_values.pop(_tile, None) or (False, None)
            if return_extras:
                return return_value[0], return_value[1], draw_state
            return return_value

        if _drag_drop.DragDrop.active and draw_state is _drag_drop.DragDrop.item_ds:
            # The floating dragged item anchors available_width on ITSELF
            # (closable -> fixed_size, x_offset 0), where inline the parent
            # clip clamp absorbed the depth margin into the content width
            # (content_width == width). Zero the margin so that stays true
            # while floating. Descendants need no correction: Melty.draw
            # restores the window's full inline bg snapshot, so their
            # absolute depths - including margins - match inline exactly.
            content_margin = 0.0

        if closable:
            kwargs['use_cache'] = True

        auto_apply = kwargs.get("auto_apply", ())

        tile_id = f"{name}##{strhash(str(unique) + str(draw_state.id))}"
        draw_state._tile_id = tile_id
        _wtB = time.perf_counter()   # TEMP perf: unique/draw_state computation

        if "closed" in kwargs:
            draw_state.closed = kwargs["closed"]

        # An explicit parent_window= makes the view render even with no melty
        # window on the stack: a host that draws its contents straight to the
        # root imgui window (the hdr-viewer) still gets popovers - the
        # context_menu= below - placed relative to their spawner (09-10).
        # Never on a deferred re-entry: Melty.draw parks the cursor at the
        # window's abs position, which already carries window_pos, so
        # capturing the offset from it here pulls window_pos in once more
        # per cached frame: the colour-picker popover (window_pos y = the
        # exposure-band lift) walked up the screen until the display cap
        # held it, and jittered whenever its spawner's body ran (09-10).
        explicit_parent = kwargs.get("parent_window") is not None
        if _has_imgui and not deferred_entry and (len(Melty.melty_window_stack) > 0 or explicit_parent):
            draw_state.parent_window = kwargs.get("parent_window", None)
            if draw_state.parent_window is None:
                draw_state.parent_window = Melty.melty_window_stack[-1]

            # draw_state._cursor_start_pos = imgui.get_cursor_screen_pos()
            # if draw_state._parent is draw_state or draw_state._parent._cursor_start_pos is None:
            #     draw_state.top_offset_true = draw_state.header_height
            # else:
            #     parent_cursor = draw_state._parent._cursor_start_pos[1]
            #     this_cursor = imgui.get_cursor_screen_pos()[1]
            #     # if kwargs.get("column", None) is not None and draw_state._parent.final_max_column > 1:
            #     #     this_cursor = draw_state._parent._column_cursor[kwargs.get("column", 0)][1]
            #
            #     parent_scroll = draw_state._parent.scroll_offset[1] if draw_state._parent.scroll_offset is not None else 0
            #     draw_state.top_offset_true = (this_cursor - parent_cursor + parent_scroll + 1)

            # Store left/top_offset as the UNSCROLLED position relative to
            # parent_window's content (cursor pos already reflects ancestor
            # scroll, so add it back). _abs_left subtracts the live ancestor
            # scroll, making abs_left react to mid-frame scroll deltas instead
            # of waiting for this view to re-render with a new cursor pos.
            anc_sx, anc_sy = draw_state._ancestor_scroll()
            if kwargs.get("view_offset", True):
                left = kwargs.get("left", imgui.get_cursor_screen_pos()[0])
                draw_state.left_offset, draw_state.top_offset = (
                    left - draw_state.parent_window.abs_left + anc_sx,
                    imgui.get_cursor_screen_pos()[1] - draw_state.parent_window.abs_top + anc_sy)
            else:
                draw_state.left_offset, draw_state.top_offset = (kwargs.get("left", 0),0)


        if closable:
            if draw_state.parent_window is None and not kwargs.get("unmanaged", False):
                # Read BEFORE the defaultdict inserts the entry below. A new
                # key first reuses the hollow entry saved under this NAME
                # (or ID of a previous draw_state - see reclaim_window_slot),
                # keeping its z-order slot instead of leaving a stale twin.
                newly_registered = tile_id not in Melty.registered_windows
                _entry = Melty.registered_windows.get(tile_id)
                if _entry is None or _entry.draw_state is None:
                    # First registration of this key this run (a loaded
                    # session is hollow until now) - once per window.
                    if Melty.reclaim_window_slot(name, tile_id):
                        newly_registered = False
                Melty.registered_windows[tile_id].input_value = input_value
                Melty.registered_windows[tile_id].draw_state = draw_state
                Melty.registered_windows[tile_id].window_args = kwargs
                Melty.registered_windows[tile_id].name = name
                # First-seen windows feed the dock's "Recently added" section.
                Melty.note_window_seen(name)
                # Mirror the resolved tint onto the draw_state, open or
                # closed. The dock reads a row's tint through locate_tint:
                # draw_state._kwargs first, else draw_state.tint - and a
                # CLOSED window returns below BEFORE _restamp_kwargs, while
                # the render path only seeds draw_state.tint if it is None
                # (never None since DrawState defaults it). So a window's
                # `@window(tint=...)` / instance / decoration tint reached the
                # dock only while the window was open, and every closed
                # window read the default grey after a reboot. Stamped here
                # it persists with the draw_state and re-stamps on the first
                # frame of every boot (registered_windows is hollow then, so
                # the wrapper runs once per closed window before the
                # dispatch loop's skip takes over).
                _window_tint = kwargs.get("tint")
                if _window_tint is not None and draw_state.tint != _window_tint:
                    draw_state.tint = _window_tint

                if newly_registered and Melty.frame_count > 2:
                    Melty.cache.invalidate_by_obj(Melty.registered_windows)

            if draw_state.closed and not id(input_value) == id(Melty.registered_windows):
                # The table reads a row's icon from draw_state._kwargs (_row_icon),
                # but a closed window returns here BEFORE _restamp_kwargs, so a
                # call-site icon= never reached the draw_state and the Important
                # Button dropped the row. Stamp just the icon, never the full
                # kwargs: that would pin input values on a closed window.
                icon = kwargs.get("icon")
                if icon:
                    if type(draw_state._kwargs) is dict:
                        if draw_state._kwargs.get("icon") != icon:
                            draw_state._kwargs["icon"] = icon
                    else:
                        draw_state._kwargs = {"icon": icon}

                if draw_state._is_nested:
                    draw_state.dlt_count = 0
                if closable:
                    Melty.root_draw_states[draw_state.id] = []
                if return_extras:
                    return False, None, draw_state

                return False, None
            elif draw_state.closed and input_value == Melty.registered_windows:
                draw_state.closed = False

        _restamp_kwargs(draw_state, kwargs)
        # One filtered copy instead of copy() + 20 pops (each runs for every
        # window call; the heavy-arg views have 60+ keys).
        ds_kwargs = {k: v for k, v in kwargs.items() if k not in _DS_KWARGS_EXCLUDE}

        draw_state.unique = unique
        draw_state._collection = Melty.collection_stack[-1] if len(Melty.collection_stack) > 0 else None
        draw_state.name = name
        original_type = type(input_value)
        converted_input = False

        draw_state.closable = closable
        draw_state.behind = kwargs.get("behind", draw_state.behind)
        draw_state.tile_mode = kwargs.get("tile_mode", draw_state.tile_mode)

        window_key = tile_id
        draw_state.persistent = kwargs.get("persistent", True)
        if kwargs.get("temp", False):
            draw_state.dlt_count = 0

        computed_unique = unique

        # -------------------------------------------------------------------------

        # After you compute `new_unique` for `obj` in the render loop:
        ds_registry = Melty.draw_state_registry
        # pending = Melty.move_draw_state_pending
        #
        # if pending:  # any remaps waiting?
        #     ds = pending.pop(id(input_value), None)  # was this object moved?
        #     if ds is not None:
        #         # If the draw state tracks its own unique, retire the old entry
        #         old_u = getattr(ds, "unique", None)
        #         if old_u is not None:
        #             ds_registry.pop(old_u, None)
        #         ds.unique = unique  # keep the DS in sync
        #
        #         # Install under the new unique (overwrite if needed)
        #         ds_registry[unique] = ds.deepcopy()
        #
        #         # Optional: clean up empty dict to avoid pointless checks later
        #         if not pending:
        #             # FIX: ensure we reset the same container we read from
        #             Melty.move_draw_state_pending = {}

        if len(Melty.draw_state_stack) > 0:
            parent = Melty.draw_state_stack[-1]
            draw_state._parent = parent

            # Self-register into the parent's child index. draw_collection used
            # to be the only view with children (keyed by collection idx),
            # so every other container had an empty _children and
            # children_in_clip found nothing. Doing it here - at the one place
            # the render-tree parent is assigned - populates it for every view.
            # Keyed by id(): draw_states are reused from the registry, so a
            # view's id is stable across frames, and a re-render always overwrites
            # its own entry. Stale _parented entries are removed at read time
            # (children_in_clip drops any whose _parent is no longer this DS).
        if draw_state._parent is not None:
            if draw_state._parent.id != draw_state.id:
                if id(draw_state) not in draw_state._parent._view_children:
                    draw_state._parent._view_children[id(draw_state)] = draw_state

        original_width_b = draw_state.width
        original_height_b = draw_state.height
        style_manager = Melty.style_manager
        collection = kwargs.get("collection", None)


        draw_state._raw_input_value = input_value
        if draw_state._input_cache["external_state"][0] is UNSET_VALUE:
            if not isinstance(input_value, Pending):
                input_hash = Background.simple_hash(input_value)
                draw_state._input_cache["external_state"] = (input_value, Melty.frame_count, input_hash)

        draw_state._wrapper = wrapper
        draw_state._bg_stack = copy(Melty.bg_stack)
        draw_state._bg_depth = Melty.bg_depth


        #######################################
        #### Wrapper
        with_wrapper = kwargs.get("with_wrapper", None)
        if with_wrapper is not None:
            kwargs['view_func'] = func
            return with_wrapper(**kwargs)


        #############################################
        ###### Layer rendering delay
        original_active_layer = Melty.active_layer
        original_paint_rank = Melty.paint_rank
        draw_state._start_z_pos = min(Melty.z_pos, 3)
        if Melty.cache is not None:
            draw_state._parent_ctx = Melty.cache.get_current_parent()

        # ── Auto draw_state params (see module comment above) ──
        # auto_params holds DIVERGED values only (view code wrote
        # draw_state.<param>, not a deserialized save) - those inject into the
        # kwargs gauntlet above the other layers. _auto_baseline holds last
        # frame's instance value per param; an instance attr that no longer
        # matches its baseline is a view-code write, and gets promoted into
        # auto_params. Both lazily created: pre-existing draw_states
        # (deserialized / pre-hotswap) lack the fields.
        auto_state_params = _auto_state_params() if kwargs.get("auto_state", True) else ()
        auto_state_values = draw_state.__dict__.get('auto_params')
        if auto_state_values is None:
            auto_state_values = {}
            draw_state.auto_params = auto_state_values
        auto_state_baseline = draw_state.__dict__.get('_auto_baseline')
        if auto_state_baseline is None:
            auto_state_baseline = {}
            draw_state._auto_baseline = auto_state_baseline

        # Divergence check: promote view-code writes since the last frame
        # into the persisted dict. Untouched params stay out of auto_params,
        # so defaults (Mode / class annotations / codec) keep as live.
        if auto_state_params and auto_state_baseline:
            ds_attrs = draw_state.__dict__
            for p in auto_state_params:
                if p in auto_state_baseline and p in ds_attrs:
                    cur = ds_attrs[p]
                    if _values_differ(cur, auto_state_baseline[p]):
                        auto_state_values[p] = cur

        # Set default values from initial on the first frame (before any potential mutation)
        if draw_state.frame_count < 3 or draw_state.closed:
            approved_kwargs = ['expanded', 'closed']
            for item_name, initial_value in initial_values.items():
                # An auto-state param receives `kwargs` defaults only before its
                # first resolution (and never over a deserialized value).
                if (item_name in auto_state_params
                        and item_name not in auto_state_values
                        and item_name not in auto_state_baseline):
                    auto_state_values[item_name] = initial_value
                    continue

                if isinstance(getattr(draw_state, item_name, None), int):
                    if getattr(draw_state, item_name) == 0 or kwargs.get("force_initial", False):
                        setattr(draw_state, item_name, initial_value)
                else:

                    if hasattr(draw_state, item_name) and (getattr(draw_state, item_name) is None or kwargs.get(
                            "force_initial", False)):
                        initial_values["forced"] = initial_values.get("forced", "") + item_name + f"{draw_state.frame_count}"
                        draw_state._kwargs["initial"] = initial_values
                        setattr(draw_state, item_name, initial_value)

                if item_name in approved_kwargs:
                    kwargs[item_name] = initial_value

        if active_layer is None and _has_imgui:
            # Call-site capture for the caller-arg lens / jump-to-caller. This is
            # the INLINE pass (active_layer is None), invoked from the user's own
            # render code - so the live stack still holds the draw_text(...) call,
            # unlike the Melty.draw re-dispatch pass. Captured once per widget the
            # first frame its context menu is open (one frame later than the menu
            # appearing - that frame simply has no frames yet, which is fine), then
            # left alone until app restart. inspect.stack is expensive, so the
            # context_menu_open gate keeps it off the steady-state hot path.
            #
            # Resolve the (filename, lineno) HERE, from this frame's stack, and
            # cache just that tuple - never the raw frames. The lens/jump button
            # must NOT re-walk the live stack again: a mouse drag re-renders with
            # parents first (an optimization), which changes the stack and would
            # make the walk land elsewhere, breaking the edit mid-drag.
            # Also fire when a descendant's menu walked UP to this view (it has no
            # menu of its own, but context_menu_open is False) - see _call_site_requested.
            if ((draw_state.context_menu_open or draw_state._call_site_requested)
                    and not draw_state._call_site_captured):
                draw_state._call_site_captured = True
                draw_state._call_site_requested = False
                from src.lsd.gl_gui.view.core_conversion.chain_converters import (
                    caller_site, call_stack_frames)
                # Grab the WHOLE stack here (once, from this frame's stack), UNfiltered
                # - the menu renders all of it and filters per-frame at draw time.
                # _call_site stays the filtered head for the lens. Resolving now and
                # caching tuples - never re-walking the live stack later - is
                # essential: a drag re-renders with parents skipped, which changes the
                # stack and would change every site.
                frames = get_live_frames(skip_count=0)
                draw_state._call_stack = call_stack_frames(frames)
                draw_state._call_site = caller_site(frames)
                # The stack stash for the context menu's Code tab (the stack
                # trace view): (path, lineno, func_name, locals) outermost
                # first, holding f_locals only for project frames - library
                # frames render as code without values, and their kwargs
                # would otherwise stay pinned on this draw_state. Entirely
                # separate from the publish below: the Code tab renders from
                # this list alone (frame_value_store), touching no global
                # store.
                from src.lsd.gl_gui.view.core_conversion.address import (
                    is_editable_source)
                draw_state._call_stack_frames = [
                    (e[0], e[1], e[2],
                     e[4] if is_editable_source(e[0]) else None)
                    for e in frames]
                # The trace ends in this wrapper - the TARGET's body hasn't
                # run yet, so its frame isn't on the stack. Append it as a
                # terminal entry: lineno = def def region (co_firstlineno -
                # the stack view renders the whole function when the lineno
                # IS the def), scope = the resolved kwargs with the aliases
                # a user sees (the same scope the publish path serializes).
                # next_kwargs is the self-cyclic plumbing dict; not a
                # value worth showing, and holding it would pin the whole
                # kwargs chain on this draw_state.
                try:
                    _target_fn = inspect.unwrap(draw_state._view_func)
                    _target_code = getattr(_target_fn, "__code__", None)
                    if (_target_code is not None
                            and is_editable_source(_target_code.co_filename)):
                        _target_scope = dict(kwargs)
                        _target_scope.pop("next_kwargs", None)
                        _target_scope.update({
                            "input_value": input_value, "value": input_value,
                            "draw_state": draw_state, "ds": draw_state})
                        draw_state._call_stack_frames.append(
                            (_target_code.co_filename,
                             _target_code.co_firstlineno,
                             getattr(_target_fn, "__qualname__",
                                     _target_fn.__name__),
                             _target_scope))
                        # One-shot: this invocation's body runs under
                        # call_with_body_capture (sys.monitoring, local
                        # events on the target code only), and its FULL
                        # locals replace the terminal entry's signature
                        # scope at return (see the call site). Armed
                        # regardless of the live-view toggle - the Code
                        # tab's adoption is local; the capture's GLOBAL
                        # publish half stays toggle-gated inside _finish.
                        # The capture only fires when the body actually
                        # RUNS - a blit-cache hit skips it and the tab
                        # must sit on signature values - so force this
                        # view's next frame past the cache.
                        draw_state._lv_capture_body = True
                        draw_state.invalidate()
                        request_render()
                except Exception:
                    pass
                # Scope-snapshot publish: the frames still hold each frame's
                # f_locals; the TARGET's own locals is not among them (we're in
                # its wrapper - its body hasn't run), so its scope is rebuilt
                # from the resolved kwargs. ONE async pass (live_view worker)
                # turns it into completion types (FuncsMetadata) and
                # live-value markers/windows. Runs inside this one-shot
                # menu-open edge case - never steady-state. (The
                # publish_with_body_capture profiler that used to ride along here
                # stays removed: profiling a heavy view body took seconds.)
                if Toggles.TextEditor.enable_live_view:
                    try:
                        from src.lsd.gl_gui.view.core_conversion.live_view import (
                            publish_stack_locals)
                        _target_scope = dict(kwargs)
                        _target_scope.update({
                            "input_value": input_value, "value": input_value,
                            "draw_state": draw_state, "ds": draw_state})
                        publish_stack_locals(frames, extra_snapshots=[
                            (draw_state._view_func, _target_scope, None)])
                        # (The body-locals one-shot - _lv_capture_body - is
                        # armed in the except block above, outside this
                        # toggle: the Code tab's local adoption always runs;
                        # only the capture's PUBLISH half is toggle-gated,
                        # inside call_with_body_capture._finish.)
                    except Exception:
                        pass

            if closable:
                if _drag_drop.DragDrop.is_dragged_item(draw_state):
                    # The floating dragged item rides the very top layer -
                    # above its parent window (which, being focused, sits at
                    # len(registered_windows) + top_layer_boost) and above
                    # every other window for the duration of the drag.
                    window_z_pos = len(Melty.layers) - 1
                elif draw_state is not None and draw_state.parent_window is not None:
                    # Nested window: same z-band mapping as
                    # draw_state.abs_layer, so the first registration frame
                    # buckets where steady-state dispatch will.
                    window_z_pos = Melty.nested_window_layer(
                        draw_state.parent_window.layer or 0,
                        kwargs.get("layer_offset", 4),
                        ds=draw_state)
                else:
                    # Managed windows are not nested, so we check the registry directly to find their layer
                    # Indicates this is not a nested window
                    window_z_pos = list(Melty.registered_windows.keys()).index(window_key) \
                        if window_key in Melty.registered_windows else None
                    if window_z_pos is not None:
                        window_z_pos = max(window_z_pos, Melty.active_layer)
                    if kwargs.get("always_on_top", False):
                        # Pinned above the nested-window band (which sits above
                        # every root window), below only the dragged item.
                        window_z_pos = Melty.always_on_top_layer

                kwargs['layer'] = window_z_pos

            if kwargs.get("layer", None) is not None and len(Melty.layers) > 0:
                layer = kwargs.pop("layer", None)

                if layer == len(Melty.registered_windows) - 1 and not "z_absolute" in kwargs:
                    layer = len(Melty.registered_windows) + Melty.top_layer_boost

                if closable and (len(Melty.melty_window_stack) > 0 or explicit_parent):
                    if kwargs.get("inline", False):
                        if kwargs.get("with_header", None) is not None:
                            imgui.push_id(tile_id + "_inline")

                            pre_header_cursor = imgui.get_cursor_screen_pos()
                            kwargs['style_manager'] = style_manager
                            kwargs['draw_state'] = draw_state
                            kwargs["with_header"](**draw_state._kwargs)
                            left = kwargs.get("left", imgui.get_cursor_screen_pos()[0])

                            imgui.pop_id()
                            draw_state.left_offset, draw_state.top_offset = (
                                left - draw_state._parent.abs_left,
                                imgui.get_cursor_screen_pos()[1] - draw_state._parent.abs_top)
                            if not draw_state.expanded and not manual_expand:
                                return_value = (False, None)
                                # TEMP perf: bill this early-out (collapsed
                                # inline window) - otherwise it hides in the
                                # parent's exclusive time.
                                _vm = getattr(Melty, "_frame_view_ms", None)
                                if _vm is not None:
                                    _nm = getattr(func, "__name__", "?") + "~w"
                                    _t = time.perf_counter() - _wt0
                                    _e = _vm.get(_nm)
                                    _vm[_nm] = ((_e[0] + _t, _e[1] + 1)
                                                if _e else (_t, 1))
                                if return_extras:
                                    if len(return_value) == 3:
                                        return return_value
                                    else:
                                        return *return_value, draw_state
                                return return_value

                        else:
                            imgui.text(f"{name}")

                    draw_state.is_nested = True
                    parent_ds = draw_state.parent_window
                    if not detached:
                        draw_state._detached_for = 0
                    if draw_state not in set(Melty.root_draw_states[parent_ds.id]):
                        if not detached:
                            Melty.root_draw_states[parent_ds.id].append(draw_state)
                            layer = layer + (len(Melty.root_draw_states[parent_ds.id]))
                        # A detached (dragged) window may already be queued this
                        # frame by DragDrop._keep_alive, which is into the
                        # layer loop and ran just when this body was about to run
                        # its defered call - never queue it twice, or the
                        # loop draws the same draw_state twice in one frame.
                        if not any(draw_state in l for l in Melty.layers):
                            Melty.layers[min(layer, len(Melty.layers) - 1)].append(draw_state)
                else:
                    Melty.layers[min(layer, len(Melty.layers) - 1)].append(draw_state)

                kwargs["active_layer"] = layer
                return_value = (False, None)
                if draw_state._tile_id in Melty.returned_values:
                    return_value = Melty.returned_values.pop(draw_state._tile_id)



                Melty.cache.mark_uncached(name, input_value, collection, tile_id, draw_state)

                # This view is being DEFERRED to a layer (drawn at end of frame).
                # Capture the QUEUE-TIME stack NOW: we're still in the inline pass,
                # so the live stack holds the full caller chain that led here. At
                # end-of-frame dispatch that chain is gone (just the layer-loop
                # dispatch), so a descendant rendered inside this layer has a
                # _call_stack that bottoms out at the dispatch. draw_context_menu
                # appends this preemptively-captured stack to complete the picture.
                # Mark the view, and capture lazily - not in the inline pass
                # so get_live_frames stays off the hot path: only when this view's
                # own menu is open, or a descendant requested it.
                draw_state._is_deferred_layer = True
                # EDGE-gated, not level: context_menu_open stays True for the
                # menu's whole lifetime, and this branch used to re-walk the
                # live stack AND republish frame snapshots every single frame
                # the menu was open. The latch fires the capture once per
                # ON-OPEN (releasing when the menu closes), plus once per
                # explicit descendant request.
                if not draw_state.context_menu_open:
                    draw_state._deferred_stack_published = False
                if ((draw_state.context_menu_open
                     and not getattr(draw_state, '_deferred_stack_published', False))
                        or draw_state._deferred_stack_requested):
                    draw_state._deferred_stack_published = True
                    draw_state._deferred_stack_requested = False
                    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
                        call_stack_frames)
                    deferred_frames = get_live_frames(skip_count=0)
                    draw_state._deferred_call_stack = call_stack_frames(deferred_frames)
                    # The Code-tab copy (same shape as _call_stack_frames:
                    # locals kept for project frames only). A descendant's
                    # menu splices its own dispatch-bottomed frames onto this
                    # (_merged_call_stack_frames in new_core_view), so the
                    # trace reads as if the layer had been drawn inline.
                    from src.lsd.gl_gui.view.core_conversion.address import (
                        is_editable_source)
                    draw_state._deferred_call_stack_frames = [
                        (e[0], e[1], e[2],
                         e[4] if is_editable_source(e[0]) else None)
                        for e in deferred_frames]
                    # Same tap as the inline capture: the async live_view
                    # batch records the queue-time callers' local types AND
                    # publishes their values as frame metadata.
                    if Toggles.TextEditor.enable_live_view:
                        try:
                            from src.lsd.gl_gui.view.core_conversion.live_view import (
                                publish_stack_locals)
                            publish_stack_locals(deferred_frames)
                        except Exception:
                            pass

                if Toggles.layer_stack_trace:
                    get_stack = get_live_frames(skip_count=1)
                    draw_state._layer_stack_trace = get_stack

                # TEMP perf: bill this early exit (deferred/closed window
                # path) - otherwise it counted in the parent's exclusive time.
                _vm = getattr(Melty, "_frame_view_ms", None)
                if _vm is not None:
                    _nm = getattr(func, "__name__", "?") + "~w"
                    _t = time.perf_counter() - _wt0
                    _e = _vm.get(_nm)
                    _vm[_nm] = ((_e[0] + _t, _e[1] + 1) if _e else (_t, 1))
                if return_extras:
                    if len(return_value) == 3:
                        return return_value
                    else:
                        return *return_value, draw_state
                return return_value
        else:
            Melty.active_layer = active_layer if active_layer is not None else 4
            if active_layer is None:
                # Inline pass outside any dispatched window: the dispatch loop
                # has no paint rank for this view, so its z rank is the same
                # fallback layer. Inside a window the loop's rank takes.
                Melty.paint_rank = 4

        # Handle untracked object invalidation
        if not hasattr(input_value, "__melty__"):
            if Melty.frame_count > 2 and draw_state.frame_count > 2:
                if isinstance(input_value, (type(None), int, float, str, bool, tuple, set)):
                    if draw_state._raw_input_value != input_value:
                        if kwargs.get("collection", None) is not None:
                            note = Note(name=f"Untracked value changed: {input_value}", draw_state=draw_state, tint=(0.1, 0.1, 0.4))
                            Melty.cache.invalidate_up_by_obj(collection, name=name, max_depth=5, note=note)
                            Melty.last_attr = draw_state.name
                            request_render()
                        else:
                            Melty.cache.invalidate(draw_state._tile_id)

        kwargs['return_extras'] = False

        if draw_state._is_nested:
            Counters.nested_window_count += 1

        start_shadow_depth = Melty.shadow_depth
        if unique in Melty.seen_unique:
            if _has_imgui and 'draw_state' in wanted_params:
                overlay_list: _DrawList = imgui.get_overlay_draw_list()
                overlay_list.add_text(*imgui.get_cursor_screen_pos(),
                                      pack_color(1.0, 0.0, 0.0, 1.0),
                                      f"ID {draw_state.name}")
                kwargs['use_cache'] = False
                print(f"Duplicate unique detected: {unique} for {draw_state.name} {input_value.__class__.__name__} {func.__name__}. Forcing re-render.")
                # return False, None

        Melty.seen_unique.add(unique)

        if "expanded" in kwargs:
            draw_state.expanded = kwargs["expanded"]
            if not draw_state.expanded:
                kwargs['is_tree'] = False

        if not draw_state.expanded and not manual_expand:
            kwargs.pop("width", None)
            kwargs.pop("height", None)

        has_collection = collection is not None and not isinstance(collection, tuple)
        if has_collection:
            Melty.collection_stack.append(collection)

        if not draw_state.expanded and not manual_expand:
            passed_width = None
            passed_height = None

        fixed_size = not draw_state.auto_resize or (kwargs.get("height", None) and not "column" in kwargs) or closable or (
                    kwargs.get("fill_height", None) is not None and not "column" in kwargs)

        auto_resize = kwargs.get("auto_resize", True if not closable else False) or not draw_state.expanded
        draw_state.auto_resize = auto_resize and not fixed_size

        # Restore expanded =================
        if draw_state._last_expanded is not None and draw_state._last_expanded != draw_state.expanded and draw_state.frame_count > 2:
            if not manual_expand:
                if draw_state._last_expanded:
                    draw_state.expanded_rect = (
                        draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.height)
                else:
                    draw_state._collapsed_rect = (
                        draw_state.abs_left, draw_state.abs_top, draw_state.width, draw_state.header_height)

                if draw_state.expanded:
                    # Restore the rect saved at collapse time - but only a real
                    # one. A view that STARTED collapsed has only the zeroed
                    # default here; restoring it wiped width/height, so leave
                    # them for the auto-resize writers to measure this frame.
                    _exp_rect = draw_state.expanded_rect
                    if _exp_rect and _exp_rect[2] > 5 and _exp_rect[3] > 5:
                        draw_state.left, draw_state.right, draw_state.width, draw_state.height = _exp_rect
                        draw_state._source["height"] = "expanded_rect"
                    draw_state.expanded_rect = (0, 0, 0, 0)
                else:
                    _col_rect = draw_state._collapsed_rect
                    if _col_rect and _col_rect[2] > 5 and _col_rect[3] > 5:
                        draw_state.left, draw_state.right, draw_state.width, draw_state.height = _col_rect
                    else:
                        # First-ever collapse: nothing saved yet. Keep the current
                        # width (a header-only row spans the same width) and drop
                        # height to the header band. Restoring the zeroed default
                        # made the rect degenerate - bvh_sync skipped the box and
                        # the view vanished from hover and drag-and-drop.
                        draw_state.height = max(draw_state.header_height or 0, 18)
                    draw_state._source["height"] = "collapsed_rect"

            if draw_state._collection_draw_state is not None:
                draw_state._collection_draw_state.invalid_content_height = True
            if draw_state._parent is not None:
                draw_state._parent.invalid_content_height = True

        if draw_state.expanded:
            draw_state.expanded_rect = (0, 0, 0, 0)

        draw_state._last_expanded = draw_state.expanded
        # End restore expanded ================

        if _has_imgui:
            Melty.draw_state_stack.append(draw_state)
        draw_state._has_popup = kwargs.get("has_popup", False)
        if passed_width is not None:
            draw_state.width = snap_int(passed_width)
        if passed_height is not None:
            draw_state.height = snap_int(passed_height)
            draw_state._source["height"] = "passed height"

        Melty.input_value_stack.append(input_value)
        inc_depth = False

        if draw_state.tint is None:
            draw_state.tint = kwargs.get("tint", draw_state.tint)

        melty_window = kwargs.get("melty_window", False)
        melty_window_header = kwargs.get("melty_window", False)
        draw_state.melty_window = melty_window_header
        previous_tint = None

        _pushed_search = False
        if _has_imgui and closable and draw_state._is_nested and draw_state.current_tint is not None:
            style_manager.set_imgui_tint(*draw_state.current_tint)
            kwargs["tint"] = draw_state.current_tint
        try:

            def set_default(key, default_value, type=None):
                # if key in vars(meta) and vars(meta)[key] is not None:
                #     default_value = vars(meta)[key]
                draw_state_misc = draw_state.misc
                # Custom draw state object to be dynamically created for unmatched params
                if key in draw_state.misc and type is not None:
                    default_value = draw_state_misc[key]
                    draw_state.misc_used.add(key)

                    if not default_value.__class__.__name__ == type.__name__:
                        default_value = None
                        draw_state_misc.pop(key, None)

                if default_value is None:
                    default_value = (param_defaults or {}).get(key, default_value)

                    # Create a new instance of the custom draw state object.
                    # Enum subclasses can't be constructed with no args - skip
                    # them and let the param's signature default (often None)
                    # pass through.
                    if (type is not None and default_value is None
                            and not (inspect.isclass(type) and issubclass(type, Enum))):
                        draw_state_misc[key] = type()
                        draw_state.misc_used.add(key)
                        default_value = draw_state_misc[key]
                        # Injected custom objects that declare an `_owner_ds`
                        # slot (e.g. GLState) learn which draw_state owns
                        # them - lifecycle hooks key off the owner's
                        # parent_window chain (GL release on window delete).
                        if hasattr(default_value, "_owner_ds"):
                            default_value._owner_ds = draw_state

                kwargs.setdefault(key, default_value)

            kwargs["style_manager"] = Melty.style_manager
            # Same injection contract as style_manager: any render_func that
            # declares `vis` in its signature receives the studio automatically
            # (explicitly passed vis, e.g. draw_main's, wins via setdefault).
            kwargs.setdefault("vis", Melty.vis)


            kwargs = Melty.default_kwargs_by_type[kwargs.get("real_type", type(input_value))] | kwargs
            kwargs = Melty.default_kwargs_by_attrib_type[kwargs.get("type_collection", type(collection))][key] | kwargs
            # Codec-provided base kwargs + active-codec marker. The value's
            # data-source codec (function / call-site / class / decorations)
            # contributes its render_kwargs as the LOWEST priority layer -
            # every explicit/default kwarg above overrides them. `tint` is the
            # ONE EXCEPTION: it deliberately does NOT propagate from here (it
            # would wash codec-typed views app-wide), and is stored on the
            # codec as the effective color-code, consumed only by views
            # that surface this info (draw_param_matrix in the context menu).
            # The codec also pushes onto Melty.codec_stack (popped in the
            # cleanup next to mode_stack), and every draw_state stashes the
            # ACTIVE codec as ds._codec - its own, or inherited from the
            # enclosing subtree - so any view can ask which data source it
            # renders under.
            _codec = _codec_for_type(kwargs.get("real_type", type(input_value)))
            _codec_pushed = False
            # Explicit file context: a caller rendering codec-loaded content
            # outside any codec subtree (the open-files editor drawing
            # host["value"]) passes file_key=<path str> to seed the per-file
            # layer directly. More robust than value provenance alone: a
            # buffer with pending edits comes back as a PLAIN str (TypeCodec.
            # load's pending path never stamps), so the value can't always
            # carry its codec.
            _fk = kwargs.get("file_key", None)
            _wtD = time.perf_counter()   # TEMP perf: codec/file-key prologue
            if _fk is not None and not isinstance(getattr(draw_state, "_file_meta", None), str):
                draw_state._file_meta = str(_fk)
            # The ACTIVE codec: this value's own, or inherited from the
            # enclosing subtree (codec_stack) - a draw_text rendering a file's
            # path has no codec of its own, but renders UNDER the file's.
            _active_codec = _codec or (Melty.codec_stack[-1] if Melty.codec_stack else None)
            if _active_codec is None:
                # Value-carried provenance: a codec-loaded value (DiskSpanText)
                # rendered OUTSIDE its loader's subtree - the open-files editor
                # drawing host["value"] directly - still knows its codec and
                # file to adopt them (the span's realpath seeds the ds file_meta
                # memo Codec.file_meta_key reads).
                _active_codec = getattr(input_value, "_codec", None)
                if _active_codec is not None:
                    _span = getattr(input_value, "_disk_span", None)
                    if _span and not isinstance(getattr(draw_state, "_file_meta", None), str):
                        draw_state._file_meta = _span[0]
                elif isinstance(getattr(draw_state, "_file_meta", None), str):
                    # An edit decays DiskSpanText to plain str, but the ds
                    # memo survives - keep the per-file layer alive through
                    # the edited, instead of flickering it off.
                    from src.lsd.gl_gui.view.core_conversion.new_codecs import Codec as _active_codec
            # Per-FILE codec attributes: any file this element resolves to
            # (its own Address, or the parent ds's memo - O(1), no walk)
            # carries its own persisted params - AppModel.file_meta_collection, the
            # dict mirroring the file tree; Codec.update_file_meta writes it.
            # Merged BEFORE the codec's class render_kwargs so a file-attached
            # value overrides the codec-wide default, and beneath everything
            # explicit above - so tinting a file "just works" on every view
            # rendered under it, draw_text included, with no per-view wiring.
            if _active_codec is not None:
                _fm = _active_codec.file_meta_entry(draw_state)
                if _fm:
                    # An alpha-0 tint (FileMeta's unpainted default) is
                    # dropped here for the same reason as below: it must not
                    # shadow the draw_state fallback.
                    _fm = {k: v for k, v in _fm.items()
                           if k != "order" and not (isinstance(k, str) and k.startswith("__"))
                           and not (k == "tint" and isinstance(v, (tuple, list))
                                    and len(v) >= 4 and not v[3])}
                    if _fm:
                        kwargs = _fm | kwargs
            if _codec is not None:
                _ck = getattr(_codec, "render_kwargs", None)
                if _ck:
                    # An ALPHA-0 tint is a codec's opt-OUT (FunctionCodec: the
                    # green is provenance color, not a wash) - letting it
                    # into kwargs drives nothing and yet shadows the
                    # draw_state fallback and claims the anywhere pick. Real-
                    # alpha codec tints (caller color, mode override) keep
                    # washing their views.
                    _ck = {k: v for k, v in _ck.items()
                           if not (k == "tint" and isinstance(v, (tuple, list))
                                   and len(v) >= 4 and not v[3])}
                    if _ck:
                        kwargs = _ck | kwargs
                Melty.codec_stack.append(_codec)
                _codec_pushed = True
            draw_state._codec = _active_codec

            set_default("input_value", input_value)
            set_default("draw_state", draw_state)
            set_default("name", name)
            set_default("unique", unique)
            set_default("suffix", suffix)
            set_default("window_stack", Melty.window_stack)
            set_default("func", func)
            set_default("render_func", wrapper)

            ########## New event handler system ##########
            unique_events = Melty.events.get(tile_id, {})
            kwargs = kwargs | unique_events
            ##############################################

            # Auto-state pre-pass: inject each diverged value into the kwargs
            # gauntlet. By this point every DEFAULT layer (type/attrib
            # defaults, codec render_kwargs, decorator o_kwargs) has merged
            # into kwargs, so the stored value overwrites those - but yields
            # to keys the caller or a Mode override'd explicitly.
            if auto_state_values:
                for p in auto_state_params:
                    if p in auto_state_values and p not in explicit_param_keys:
                        kwargs[p] = auto_state_values[p]

            for param, wanted_type in _default_plan:
                if param not in kwargs:
                    set_default(param, None, wanted_type)

            # Auto-state post-pass: mirror every resolved param onto the
            # draw_state under its own name (so view code reads and writes
            # draw_state.<param>) and record it as the baseline the next
            # frame's divergence logic compares against.
            if auto_state_params:
                ds_attrs = draw_state.__dict__
                for p in auto_state_params:
                    if p in kwargs:
                        v = kwargs[p]
                        ds_attrs[p] = v
                        auto_state_baseline[p] = v

            _restamp_kwargs(draw_state, kwargs)

            # if draw_state.kwargs is None:
            #     draw_state.kwargs = AttrDict(kwargs)
            # else:
            #     draw_state.kwargs.rebind(kwargs)

            draw_state.just_shadow = kwargs.get("just_shadow", False)

            inc_depth = True
            Melty.unique_stack.append(computed_unique)

            last_draw_state = Melty.last_draw_state[Melty.depth][0]
            if last_draw_state is not None:
                new_index_in_parent = Melty.collection_index_stack[-1] if len(Melty.collection_index_stack) > 0 else 0
                if draw_state.index_in_parent != new_index_in_parent:
                    draw_state.previous = last_draw_state
                    last_draw_state.next = draw_state
                    draw_state.index_in_parent = new_index_in_parent
            Melty.last_draw_state[Melty.depth] = (draw_state, kwargs.get("collection", None))


            passed_z_offset = kwargs.get("z_offset", 0)
            ds_z_offset = draw_state.z_offset
            # if draw_state.pressed:
            #     if kwargs.get("shadow", False):
            #         if draw_state.selected:
            #             internal_z_offset = -3.0
            #         else:
            #             internal_z_offset = 1
            #     else:
            #         if draw_state.selected:
            #             internal_z_offset = -1
            #         else:
            #             internal_z_offset = 0

            # if draw_state.selected:
            #     if kwargs.get("shadow", False):
            #         internal_z_offset = -2.0
            #     else:
            #         internal_z_offset = 0
            #
            # else:
            if kwargs.get("shadow", False):
                internal_z_offset = 1
            else:
                internal_z_offset = 0

            total_z_offset = ds_z_offset + passed_z_offset + internal_z_offset

            draw_state.total_z_offset
            Melty.depth = Melty.depth + 1
            draw_state._cursor_screen_pos = imgui.get_cursor_screen_pos()
            draw_state.depth = Melty.depth
            draw_state.layer = Melty.active_layer

            # Paint-order rank, not the bucket (Melty.paint_rank): a window
            # drawn later must rank higher otherwise its hidden children lose input.
            Melty.z_pos = (Melty.paint_rank * Melty.max_depth) + Melty.depth
            draw_state.z_pos = Melty.z_pos

            kwargs['depth'] = Melty.depth
            draw_state._draggable = kwargs.get("draggable", False)

            # Wrapping
            parent_wrap = Melty.wrap_stack[-1] if len(Melty.wrap_stack) > 0 else False
            this_wrap = kwargs.get("wrap", False)
            _wtE = time.perf_counter()   # TEMP perf: injected/auto-state params
            if not auto_resize:
                parent_wrap = False
                this_wrap = False

            # This view's effective wrap state - True if it sets wrap=True or
            # inherits it from an ancestor. Captured here because wrap_stack is
            # popped before the final width assignment, and a view wrapped only
            # by inheritance (wrap kwarg False) must still take the wrap sizing
            # path; otherwise its width is never set and flickers as None.
            Melty.wrap_stack.append(this_wrap or parent_wrap)

            # ── Converter path (no imgui) ───────────────────────────
            if not _has_imgui:
                # Handle load_data: resolve Address, load, inject
                if _rf_load_data is not None and kwargs.get('data') is None:
                    # Prefer a ref passed from the parent (avoids stale
                    # inspect.getsourcelines after hotswap + file rewrite)
                    _ref = kwargs.pop('ref', None) or to_address(input_value)
                    if _ref is not None:
                        kwargs['data'] = _rf_load_data(_ref)
                        kwargs['ref'] = _ref
                        draw_state._address = _ref
                        draw_state._original_input_ref = input_value
                    else:
                        # Can't resolve source (e.g. builtin type) so skip conversion
                        return input_value

                # Call inner function with wanted params
                clean_args = {p: kwargs[p] for p in wanted_params
                              if p in kwargs and p != 'args' and p != 'o_kwargs'}
                return_value = func(**clean_args)
                # Stack cleanup handled by the finally block below
                return return_value

            ############################# WINDOW SETUP #####################################################
            ############ HANDLE WINDOW DRAGGING ########################
            imgui_active = Melty.imgui_active or Melty.imgui_popup_open

            if closable:
                # melty_hovered = draw_state.on_action("on_hover", view_id="window_hover", priority_delta=1)
                # Commit last frame's accumulated max header width so headers
                # padded this width read a stable value, then clear the
                # accumulator for the headers about to render into this window.
                Melty.melty_window_stack.append(draw_state)

                if draw_state.window_pos is None:
                    draw_state.window_pos = (0, 0)

                if Melty.frame_count < 2:
                    if draw_state.width is None or draw_state.width < 5:
                        draw_state.width = 200

                if draw_state.height is None or draw_state.height < 5:
                    # Placeholder for the first render only: replaced by the
                    # measured content height once the body has drawn (see the
                    # _height_from_content commit after the render below).
                    # Kept at the 20px floor, not larger: children of a
                    # fixed-size window pad/fill against the window border, so
                    # any placeholder taller than the floor reads back as the
                    # "measured" height of content shorter than it. Content
                    # taller than 20px overflows the placeholder and measures
                    # its true height (item_rect is the unclipped group rect).
                    draw_state.height = 20
                    draw_state._source["height"] = "first-frame placeholder"
                    # Re-arm the content measure only on the window's FIRST
                    # frame: on subsequent frames an invalid height means a user
                    # drag-resize below 5px (modes without min height), which
                    # should clamp to the floor - not snap back to content.
                    if passed_height is None and draw_state.frame_count == 0:
                        draw_state._height_from_content = True

                if 'window_pos' in kwargs:
                    draw_state.window_pos = kwargs.get('window_pos', draw_state.window_pos)

                # First-frame off-screen rescue: window positions persist (and
                # move between machines via git), so on a smaller display a
                # saved window may load entirely outside the visible area -
                # invisible and ungrabbable. On the window's first frame, pull
                # it back just enough that a RESCUE_EDGE_MARGIN sliver stays
                # inside the display; the saved position is otherwise kept, so
                # the window hangs off the edge ready to be dragged in. The
                # top clamps to 0 (not a sliver) because the header IS the
                # drag handle. Rescued windows cascade - each lands a bit
                # higher than the last - so several lost windows pile up
                # visibly along the edge instead of covering each other.
                # window_pos shifts by the delta in ABS coordinates (same as
                # summon_window) so anchor offsets ride along. An `unmanaged`
                # window is placed by code, never saved or dragged (the
                # RenderHost's envelopes, parked off the display on
                # purpose), so it is never rescued; the rescue would land it
                # on screen for its first frame and waste a cascade slot.
                if (draw_state.frame_count == 0 and draw_state.parent_window is None
                        and not kwargs.get("unmanaged", False)):
                    disp_w, disp_h = imgui.get_io().display_size
                    if disp_w > RESCUE_EDGE_MARGIN * 2 and disp_h > RESCUE_EDGE_MARGIN * 2:
                        w = draw_state.width if draw_state.width else 200
                        left = draw_state.abs_left or 0
                        top = draw_state.abs_top or 0
                        new_left = max(min(left, disp_w - RESCUE_EDGE_MARGIN),
                                       RESCUE_EDGE_MARGIN - w)
                        new_top = max(min(top, disp_h - RESCUE_EDGE_MARGIN), 0)
                        if (new_left, new_top) != (left, top):
                            idx = _offscreen_rescue["count"]
                            _offscreen_rescue["count"] += 1
                            new_top = max(0, new_top - idx * RESCUE_CASCADE_STEP)
                            wp = draw_state.window_pos
                            draw_state.window_pos = (wp[0] + new_left - left,
                                                     wp[1] + new_top - top)

                # Bottom-of-display placement: a window appearing for the first
                # time must not land with its bottom below the bottom of the
                # display - lift it by the overflow, never past the display edge
                # (a window taller than the display keeps its header, and so
                # its drag handle, reachable). Runs on the window's FIRST TWO
                # rendered frames, not just frame 0: a brand-new window has no
                # height until it's drawn once, so frame 1 is the earliest
                # its bounding box is knowable. frame_count only increments on frames
                # the window actually renders, so this is "the first frames it
                # is drawn" - re-opening a closed window doesn't re-trigger it.
                # Later frames are untouched, so a window you drag low yourself
                # stays there.
                if (draw_state.frame_count <= 1 and draw_state.parent_window is None
                        and draw_state.height):
                    disp_h = imgui.get_io().display_size[1]
                    edge_margin = Toggles.WindowSettings.edge_margin
                    win_top = draw_state.abs_top or 0
                    below = (win_top + draw_state.height) - (disp_h - edge_margin)
                    if below > 0:
                        wp = draw_state.window_pos
                        draw_state.window_pos = (wp[0], wp[1] - min(below, max(0, win_top)))

                # Drag-and-drop: the dragged item renders as a closable window;
                # glue it under the cursor here - at render/dispatch time, on its
                # draw_state - so it tracks the live mouse position on frames when
                # the source collection's body (and thus its kwargs) isn't re-run.
                if _drag_drop.DragDrop.is_dragged_item(draw_state):
                    _drag_drop.DragDrop.glue_window_to_cursor(draw_state)
            else:
                draw_state.window_pos = (0, 0)

            # The right-drag (corner_drag) drives the resize block below: a
            # plain right-drag drags the BOTTOM-RIGHT corner, a DOUBLE
            # right-drag (corner_double: press-press-drag in the handler's
            # right_mouse_double_dragged) drags the TOP-LEFT corner instead -
            # decided at the press for the whole gesture (the left+right
            # chord it replaced switched corners mid-drag, Lukas dropped it
            # 08-25). Initialised to None so the
            # window-move press latch further down can still check whether a
            # right-drag resize is in flight (auto-resize windows have no
            # resize handle, so corner_drag stays None for them).
            corner_drag = None
            if not auto_resize and (passed_width is None or passed_height is None):
                # Resolved through the module each call so columns.py hotswaps
                # keep reaching the width-retargeting below (and to avoid a
                # circular import at module load).
                from src.lsd.gl_gui.view.core_views import columns as _columns
                corner_rect = get_resize_handle(draw_state)
                handle_drag = draw_state.on_action("left_mouse_drag", view_id="window_resize",
                                                   rect=corner_rect, priority_delta=1,
                                                   cursor=mouse_cursor.RESIZE_SE)

                corner_drag = draw_state.on_action("right_mouse_drag", view_id="corner_drag", priority_delta=-1)
                # DOUBLE right-drag (the 2nd press of a double right-click,
                # held and dragged) = the TOP-LEFT corner. The handler hands
                # a double press to DOUBLE_DRAGGED subscribers first, so this
                # fires INSTEAD of the plain corner drag: same event shape,
                # the mode is decided at the press for the whole gesture.
                corner_double = draw_state.on_action("double_right_mouse_drag",
                                                     view_id="corner_drag_double", priority_delta=-1)
                if corner_drag is None and corner_double is not None:
                    corner_drag = corner_double
                # A right-drag always resizes; the held LEFT button only
                # selects the corner (read per frame in the drag block below
                # - pressing/releasing left mid-drag flips corners live, with
                # a baseline rebase there keeping the handoff seamless).
                if handle_drag is None and corner_drag is not None:
                    handle_drag = corner_drag

                # Press-anchored resize baselines (same reasoning as the
                # window-move press latch below): the first-drag-frame rebase
                # discards any delta accumulated during a UI stall between the
                # press and the first delivered drag frame, leaving the resize
                # handle many pixels behind the cursor. non_blocking at better
                # priority observes the press without stealing it from the
                # window-move press and right-press raise chain; if a child
                # wins the press chain first, we simply fall back to the
                # rebase (old behavior).
                handle_press = draw_state.on_action("non_blocking_left_mouse_down", view_id="window_resize",
                                                    rect=corner_rect, priority_delta=2)
                corner_press = draw_state.on_action("non_blocking_right_mouse_down", view_id="corner_drag",
                                                    priority_delta=1)


                if handle_press or corner_press:
                    # A press marks a NEW gesture boundary - drop every piece of
                    # drag-lifecycle state here. At low framerates the previous
                    # gesture's last drag frame and this press land in ADJACENT
                    # frames (or this press and its first drag event on the
                    # SAME frame), so the "press on the first idle frame"
                    # branch below never runs and the stale baseline /
                    # edge-target from the old gesture made the new one leap
                    # (old_baseline + new_total, and total_dx - stale_x0).
                    #
                    # Also the freeze_resize clean-capture trigger: a press
                    # on a resize handle, before any drag has started -
                    # freeze views snap their clean pre-drag capture this
                    # frame (mark_start_offscreen).
                    Melty.resize_press_frame = Melty.frame_count
                    draw_state._resize_target_edge = None
                    draw_state._resize_target_edge_x0 = None
                    draw_state._resize_target_row = None
                    draw_state._resize_target_row_y0 = None
                    draw_state._resize_from_top_left = None
                    if handle_drag is None and draw_state.window_pos is not None:
                        draw_state._initial_window_size = (draw_state.width, draw_state.height)
                        draw_state._initial_window_pos_resize = (draw_state.window_pos[0],
                                                                 draw_state.window_pos[1])
                    else:
                        # Press and first drag frame compressed together - let
                        # the rebase in the drag branch re-latch from the
                        # gesture's own total (continuous, no leap; only the
                        # press-anchored stall catch-up is lost).
                        draw_state._initial_window_size = None
                        draw_state._initial_window_pos_resize = None

                if handle_drag and not auto_resize:
                    # Which corner this drag drags: a right-drag (and the
                    # corner handle) the BOTTOM-RIGHT corner; a DOUBLE
                    # right-drag the TOP-LEFT corner - left column edge +
                    # window top, same collision rules. Fixed for the
                    # gesture (a double press can't become single mid-drag),
                    # but both modes always state size/pos as baseline ±
                    # total_d and the rebase-on-flip below is live, so a
                    # mode change between frames hands off with zero jump;
                    # the column edge is re-latched for the new side against
                    # the CURRENT cursor (the gesture-start latch below uses
                    # the press point).
                    top_left_now = bool(handle_drag is corner_drag and corner_double is not None)
                    # The corner's own directional shape (bottom-right ↘ vs
                    # top-left ↖). Immediate: the right-drag has no grab rect
                    # to hang a subscription cursor on, and this block runs
                    # every frame of the drag - request() is per-frame.
                    mouse_cursor.request(mouse_cursor.RESIZE_NW if top_left_now
                                         else mouse_cursor.RESIZE_SE)
                    from_top_left = getattr(draw_state, "_resize_from_top_left", None)
                    if from_top_left is None:
                        from_top_left = top_left_now
                        draw_state._resize_from_top_left = top_left_now
                    elif top_left_now != from_top_left:
                        from_top_left = top_left_now
                        draw_state._resize_from_top_left = top_left_now
                        # The two modes hand off with zero jump: every
                        # baseline is rebased around the current
                        # size/pos/total (see _rebase_resize_baselines).
                        _rebase_resize_baselines(draw_state, handle_drag, from_top_left)
                        # Re-latch the width edge for the new side at the
                        # CURRENT cursor (the drag-start point may sit in a
                        # different column by now); x0 = total so the
                        # incremental queue restarts from zero.
                        try:
                            draw_state._resize_target_edge = _columns.edge_under_cursor(
                                draw_state, handle_drag.x - draw_state.abs_left,
                                handle_drag.y, left=from_top_left)
                        except Exception:
                            draw_state._resize_target_edge = None
                        draw_state._resize_target_edge_x0 = handle_drag.total_dx
                        # And the row edge for the new side (above the
                        # cursor in top-left mode, below it otherwise).
                        try:
                            draw_state._resize_target_row = _columns.row_edge_under_cursor(
                                draw_state, handle_drag.y - draw_state.abs_top,
                                handle_drag.x, above=from_top_left)
                        except Exception:
                            draw_state._resize_target_row = None
                        draw_state._resize_target_row_y0 = handle_drag.total_dy
                    if (draw_state._initial_window_size is None
                            or draw_state._initial_window_pos_resize is None):
                        # Resize (re-)activating mid-drag (press and first drag
                        # frame compressed together after a stall): rebase so
                        # the formulas are continuous from here.
                        _rebase_resize_baselines(draw_state, handle_drag, from_top_left)

                    draw_state.expanded = True
                    resize_sign = -1 if from_top_left else 1
                    size_w = draw_state._initial_window_size[0] + resize_sign * handle_drag.total_dx
                    size_h = draw_state._initial_window_size[1] + resize_sign * handle_drag.total_dy
                    # True while this right-drag drives a column edge through
                    # the shared collision solver (set in the width block
                    # below). Hoisted so the sticky-x re-anchor further down
                    # can see it even when passed_width short-circuits the
                    # width block.
                    queued = False
                    # The row-axis twin: True while this right-drag drives
                    # a ROW edge (the window's own top/bottom frame edges
                    # included) through the y-axis solve.
                    queued_rows = False
                    if passed_height is None:
                        # A right-drag (corner_drag) retargets the height to
                        # a ROW edge latched once at drag start - the row
                        # edge BELOW the cursor (plain drag), ABOVE it in
                        # top-left mode - queued incrementally as a
                        # cursor-driven drag on the window's y-axis solve
                        # (window_edge_pass, below, same frame). The
                        # window's own top/bottom frame edges register like
                        # any other edge, so a window without rows resizes
                        # exactly as before, and a drag past the row pile
                        # pushes the far frame edge and slides or grows the
                        # window like an interior divider. The width block
                        # below carries the full rationale (incremental
                        # queue survives the rebase; any columns hiccup
                        # falls back to a plain resize).
                        if handle_drag is corner_drag:
                            try:
                                if getattr(draw_state, "_resize_target_row", None) is None:
                                    start_x_abs = handle_drag.x - handle_drag.total_dx
                                    start_y = (handle_drag.y - handle_drag.total_dy) - draw_state.abs_top
                                    draw_state._resize_target_row = _columns.row_edge_under_cursor(
                                        draw_state, start_y, start_x_abs, above=from_top_left)
                                    draw_state._resize_target_row_y0 = handle_drag.total_dy
                                row_edge = draw_state._resize_target_row
                                if row_edge is not None:
                                    inc = handle_drag.total_dy - draw_state._resize_target_row_y0
                                    draw_state._resize_target_row_y0 = handle_drag.total_dy
                                    if inc:
                                        _columns._ensure_window_state(draw_state)
                                        draw_state._pending_row_drags.append(
                                            (row_edge, row_edge["y"] + inc, True))
                                    queued_rows = True
                            except Exception:
                                queued_rows = False
                        if not queued_rows:
                            new_h = snap_int(max(size_h, draw_state.min_height))
                            if draw_state.max_height:
                                new_h = min(new_h, snap_int(draw_state.max_height))
                            draw_state.height = new_h
                            draw_state._source["height"] = "initial window size"
                            if from_top_left:
                                # TOP edge follows the cursor: the bottom
                                # stays fixed, so the window slides down by
                                # exactly what the height gave up (min_height
                                # clamp included - once the height floors,
                                # the top stops with it).
                                draw_state.window_pos = (
                                    draw_state.window_pos[0],
                                    draw_state._initial_window_pos_resize[1]
                                    + (draw_state._initial_window_size[1] - new_h))

                    if passed_width is None:
                        # A right-drag (corner_drag) retargets the width to a
                        # COLUMN edge latched once at drag start: plain drag
                        # takes the edge to the cursor's RIGHT, left held
                        # (top-left-d) the edge to its LEFT. The bottom-right corner
                        # HANDLE (left-drag) always resizes the window frame
                        # directly. The latched edge - typically the window's
                        # own frame edge - is queued onto the window's
                        # pending drags and solved by window_edge_pass (below,
                        # same frame), so dragging past the pile pushes the
                        # far frame edge and slides the window exactly like an
                        # interior divider push. The third tuple slot marks
                        # the edge cursor-driven, exempting it from the
                        # frame-edge walls that guard the foreign-width
                        # invariant  in _solve_collisions. Defensive: any
                        # columns hiccup bverts to a plain width resize.
                        #
                        # The queue is INCREMENTAL (edge["x"] + this frame's dx),
                        # exactly like ColumnLayout's own edge handles, NOT an
                        # absolute x0+total_dx. window_edge_pass rebases every
                        # edge when the left frame edge moves (it slides the
                        # window and shifts all edges to hold their screen
                        # position); an absolute baseline doesn't rebase, so it
                        # falls behind the window, pushes the left edge again, and
                        # the window flies off screen. Reading edge["x"] live each
                        # frame survives the rebase. _resize_target_edge_x0 holds
                        # the previous total_dx so the per-frame delta is exact.
                        if handle_drag is corner_drag:
                            if not closable:
                                draw_state.invalidate()
                            try:
                                if draw_state._resize_target_edge is None:
                                    sx = (handle_drag.x - handle_drag.total_dx) - draw_state.abs_left
                                    sy = handle_drag.y - handle_drag.total_dy
                                    draw_state._resize_target_edge = _columns.edge_under_cursor(
                                        draw_state, sx, sy, left=from_top_left)
                                    draw_state._resize_target_edge_x0 = handle_drag.total_dx
                                edge = draw_state._resize_target_edge
                                if edge is not None:
                                    inc = handle_drag.total_dx - draw_state._resize_target_edge_x0
                                    draw_state._resize_target_edge_x0 = handle_drag.total_dx
                                    if inc:
                                        _columns._ensure_window_state(draw_state)
                                        draw_state._pending_drags.append(
                                            (edge, edge["x"] + inc, True))
                                    queued = True
                            except Exception:
                                queued = False
                        if not queued:
                            new_w = snap_int(max(size_w, draw_state.min_width))
                            draw_state.width = new_w
                            if from_top_left:
                                # Direct LEFT-edge fallback (no edge exists in
                                # this window): the right edge stays fixed, so
                                # the window slides right by exactly what the
                                # width gave up (min_width clamp included).
                                draw_state.window_pos = (
                                    draw_state._initial_window_pos_resize[0]
                                    + (draw_state._initial_window_size[0] - new_w),
                                    draw_state.window_pos[1])

                    # Pointer shape for what the right-drag actually moves:
                    # a latched INTERIOR column edge moves sideways (<->, the
                    # shape its own left-drag handle carries), an interior
                    # row edge up/down, both at once the four-way arrow -
                    # all overriding the corner shape requested above. The
                    # window's OWN frame edges are the corner resize itself
                    # and keep the corner shape.
                    if handle_drag is corner_drag:
                        frame_edges = getattr(draw_state, "_frame_edges", None) or ()
                        frame_rows = getattr(draw_state, "_frame_rows", None) or ()
                        column_edge = (getattr(draw_state, "_resize_target_edge", None)
                                       if queued else None)
                        row_edge = (getattr(draw_state, "_resize_target_row", None)
                                    if queued_rows else None)
                        column_interior = (column_edge is not None and not any(
                            column_edge is frame_edge for frame_edge in frame_edges))
                        row_interior = (row_edge is not None and not any(
                            row_edge is frame_row for frame_row in frame_rows))
                        if column_interior and row_interior:
                            mouse_cursor.request(mouse_cursor.RESIZE_ALL)
                        elif column_interior:
                            mouse_cursor.request(mouse_cursor.RESIZE_EW)
                        elif row_interior:
                            mouse_cursor.request(mouse_cursor.RESIZE_NS)

                    # Anchor-managed resize positioning is stated in
                    # bottom-right corner semantics; top-left mode derives its
                    # own position from the fixed bottom-right corner above.
                    if draw_state.anchor_pos is not None and not from_top_left:
                        anchor_pos = draw_state.anchor_pos
                        if anchor_pos == Anchor.TOP_LEFT:
                            pass
                        elif anchor_pos == Anchor.TOP_RIGHT:
                            draw_state.window_pos = (
                                draw_state._initial_window_pos_resize[0] + max(0, handle_drag.total_dx),
                                draw_state.window_pos[1])
                        elif anchor_pos == Anchor.BOTTOM_LEFT:
                            draw_state.window_pos = (draw_state.window_pos[0],
                                                     draw_state._initial_window_pos_resize[1] + handle_drag.total_dy)
                        elif anchor_pos == Anchor.BOTTOM_RIGHT:
                            draw_state.window_pos = (
                                draw_state._initial_window_pos_resize[0] + max(0, handle_drag.total_dx),
                                draw_state._initial_window_pos_resize[1] + max(0, handle_drag.total_dy))
                        elif anchor_pos == Anchor.TOP_CENTER:
                            draw_state.window_pos = (
                                snap_int(draw_state._initial_window_pos_resize[0] + max(0, handle_drag.total_dx) / 2),
                                draw_state.window_pos[1])
                        elif anchor_pos == Anchor.BOTTOM_CENTER:
                            draw_state.window_pos = (
                                snap_int(draw_state._initial_window_pos_resize[0] + max(0, handle_drag.total_dx) / 2),
                                draw_state._initial_window_pos_resize[1] + max(0, handle_drag.total_dy))
                        elif anchor_pos == Anchor.CENTER_LEFT:
                            draw_state.window_pos = (
                                draw_state.window_pos[0],
                                snap_int(draw_state._initial_window_pos_resize[1] + max(0, handle_drag.total_dy) / 2))
                        elif anchor_pos == Anchor.CENTER_RIGHT:
                            draw_state.window_pos = (
                                draw_state._initial_window_pos_resize[0] + max(0, handle_drag.total_dx),
                                snap_int(draw_state._initial_window_pos_resize[1] + max(0, handle_drag.total_dy) / 2))
                        elif anchor_pos == Anchor.CENTER:
                            draw_state.window_pos = (
                                snap_int(draw_state._initial_window_pos_resize[0] + max(0, handle_drag.total_dx) / 2),
                                snap_int(draw_state._initial_window_pos_resize[1] + max(0, handle_drag.total_dy) / 2))

                    # Sticky resize: re-anchor the top to the drag-start
                    # position each frame so the bottom-on-display clamp below
                    # is the ONLY thing that displaces the window. As the window
                    # shrinks the displacement unwinds and it returns to where
                    # the drag began, rather than keeping whatever raised
                    # position an earlier overflow left it at. Only needed for
                    # anchors that don't already re-derive y from the start pos
                    # each frame (None / TOP_*); bottom/center anchors revert on
                    # their own. Preserves the (layout-managed) x.
                    # Skipped in top-left mode: there the y IS the drag, and
                    # bottom-fixed from the baseline each frame above.
                    # NOT while the right-drag drives a row edge
                    # (`queued_rows`) - the y-axis twin of the x rule below:
                    # a row pushed into the TOP frame edge makes
                    # window_edge_pass slide window_pos and grow height, and
                    # re-anchoring y here would undo the slide each frame.
                    if (Toggles.WindowSettings.sticky_drag and not from_top_left
                            and not queued_rows
                            and (draw_state.anchor_pos is None
                                 or draw_state.anchor_pos in TOP_ANCHORS)):
                        draw_state.window_pos = (draw_state.window_pos[0],
                                                 draw_state._initial_window_pos_resize[1])
                    # Similar sticky re-anchor for x, so the right-on-display
                    # clamp below is the only thing that displaces the window
                    # horizontally and the displacement unwinds as the window
                    # shrinks back. Left/None anchors don't re-derive x each
                    # frame; right/center anchors revert on their own.
                    # NOT while the right-drag holds an interior column edge
                    # (`queued`): a divider right into the LEFT frame edge
                    # makes window_edge_pass slide window_pos and grow width
                    # (slide+grow contact) - re-anchoring x here undoes the
                    # slide each frame while the width growth sticks, so the
                    # window ratchets wider without ever moving and the
                    # divider stops tracking the cursor.
                    # (Also skipped in top-left mode: x is either edge-solve
                    # owned or derived right-fixed from the baseline above.)
                    if (Toggles.WindowSettings.sticky_drag and not queued
                            and not from_top_left
                            and (draw_state.anchor_pos is None
                                 or draw_state.anchor_pos in LEFT_ANCHORS)):
                        draw_state.window_pos = (draw_state._initial_window_pos_resize[0],
                                                 draw_state.window_pos[1])

                    # The display edges: a window whose frame edges ride the
                    # edge solve (`queued` / `queued_rows`) collides with the
                    # OS window's edges THERE (columns._frame_pass → os_frame:
                    # the OS window grows / moves, the edge is the wall, a
                    # blocked edge grows the window from the other side). Only
                    # the DIRECT path - no edge latches, a plain size write -
                    # keeps growing in-place pin-and-slide here: the far edge
                    # pins at the display edge and the near edge gives.
                    from src.lsd.gl_gui import os_frame
                    if from_top_left and not queued_rows:
                        abs_top_tl = draw_state._abs_top()
                        if abs_top_tl < 0:
                            if passed_height is None:
                                draw_state.height = snap_int(draw_state.height + abs_top_tl)
                            draw_state.window_pos = (draw_state.window_pos[0],
                                                     snap_int(draw_state.window_pos[1] - abs_top_tl))
                    display_h = imgui.get_io().display_size[1]
                    abs_top = draw_state._abs_top()
                    if abs_top + draw_state.height > display_h and not queued_rows:
                        new_h, slide = os_frame.clamp_far_edge(
                            abs_top, draw_state.height, display_h, cap_size=passed_height is None)
                        if new_h != draw_state.height:
                            draw_state.height = snap_int(new_h)
                        if slide > 0:
                            draw_state.window_pos = (draw_state.window_pos[0],
                                                     snap_int(draw_state.window_pos[1] - slide))
                    display_w = imgui.get_io().display_size[0]
                    abs_left = draw_state._abs_left()
                    if abs_left + draw_state.width > display_w and not queued:
                        new_w, slide = os_frame.clamp_far_edge(
                            abs_left, draw_state.width, display_w, cap_size=passed_width is None)
                        if new_w != draw_state.width:
                            draw_state.width = snap_int(new_w)
                        if slide > 0:
                            draw_state.window_pos = (snap_int(draw_state.window_pos[0] - slide),
                                                     draw_state.window_pos[1])
                elif not (handle_press or corner_press):
                    # Not on the press branch itself - that would wipe the
                    # press-anchored baselines latched just above before the
                    # drag's first event arrives.
                    draw_state._initial_window_size = None
                    draw_state._initial_window_pos_resize = None
                    draw_state._resize_target_edge = None
                    draw_state._resize_target_edge_x0 = None
                    draw_state._resize_target_row = None
                    draw_state._resize_target_row_y0 = None
                    draw_state._resize_from_top_left = None

            # Click-away focus clearing: non_blocking + high priority sees every
            # left press over this window without taking it from interactive
            # children. Protect the press hit-stack's ancestor closure (bvh_query
            # at the press point) so a click inside a popover / find box keeps
            # its owner focused, while any outside click dismisses popovers and
            # releases text focus. Window activation runs once at input time
            # (Melty.raise_pressed_window), independently of these subscriptions.
            clear_press = draw_state.on_action("non_blocking_left_mouse_down", "clear_focus", priority_delta=512)
            if clear_press:
                # Resolve against the press position the event captured, not the
                # live cursor - a press dispatched a frame ago (start of a drag)
                # would otherwise read a cursor that has already moved off the
                # press point and protect/clear the wrong views.
                ds_under_mouse = Melty.bvh_query(clear_press.x, clear_press.y)
                Melty.clear_focus(not_this=(*ds_under_mouse, draw_state))

            # Universal drag-and-drop: any view rendered as an item of a
            # dict/list collection offers its window as a drag handle. The
            # gesture itself is owned by DragDrop.frame_update (Melty.end_frame).
            _drag_drop.DragDrop.register_item(draw_state)
            _wtF = time.perf_counter()   # TEMP perf: size/resize time
            if draw_state.window_pos is not None and closable:
                _explicit_window_pos = kwargs.get("window_pos", None) is not None
                if not _explicit_window_pos:
                    on_held = draw_state.on_action("left_mouse_held", "window_move", priority_delta=-2)
                    # The move handle shows the MOVE pointer only where a
                    # press would hand IT the drag (cursor_gate): any child
                    # that takes left drags (resize rows, sliders, dnd
                    # headers, the corner handle) keeps its own shape.
                    on_drag = draw_state.on_action(
                        "left_mouse_drag", "window_move",
                        cursor=mouse_cursor.MOVE if Toggles.Melty.window_move_cursor else None,
                        cursor_gate="left_mouse_dragged")
                    left_mouse_down = draw_state.on_action("left_mouse_down", "window_move", priority_delta=-1)

                    if left_mouse_down:
                        # Press-anchored move baseline. The first-drag-frame
                        # rebase below aligns the cursor - right for mid-drag
                        # (re)adoption, but it permanently discards any delta
                        # accumulated while the UI wasn't rendering (a stall
                        # between the press and the first delivered drag frame
                        # left the window that many pixels behind the cursor).
                        # Latching at the press makes pos = press_pos + total_d
                        # catch up in sync. Skip while a drag is already
                        # driving, so a stray left press during an active
                        # right-drag resize can't latch a stale move baseline.
                        if on_drag is None and corner_drag is None:
                            draw_state._initial_window_pos = (draw_state.window_pos[0],
                                                              draw_state.window_pos[1])
                            # Nav-undo origin for this move gesture: unlike the
                            # baseline it is NEVER rebased mid-drag, so the
                            # recorded step covers the whole hand move.
                            draw_state._move_undo_origin = draw_state._initial_window_pos
                        else:
                            # Press with a drag already delivering this frame -
                            # low-fps combination of release+press+drag, or a
                            # stray left press during an active right-drag
                            # resize. Drop any stale baseline so the drag
                            # branch's rebase re-latches for the CURRENT
                            # gesture instead of leaping from the old one's.
                            draw_state._initial_window_pos = None

                    # Window moves are left-drag only (the right-drags, single
                    # and double, are the resize block above). A left press
                    # while a right-drag resize is in flight is a chord the
                    # handler swallows (input_handler.feed_down), so on_drag
                    # can't consistently arrive here at the same time as
                    # corner_drag; guard anyway so a lost release can't make
                    # the two fight over window_pos.
                    move_drag = on_drag if corner_drag is None else None

                    if move_drag and not imgui_active:
                        if draw_state._initial_window_pos is None:
                            # First frame of this move segment. Rebase the
                            # baseline by the drag delta so far so pos =
                            # baseline + total_d is continuous when the move
                            # (re)activates mid-drag - e.g. resuming after an
                            # imgui interruption. At a normal drag start
                            # total_d≈0 so it's a no-op.
                            draw_state._initial_window_pos = (draw_state.window_pos[0] - move_drag.total_dx,
                                                              draw_state.window_pos[1] - move_drag.total_dy)
                            if getattr(draw_state, "_move_undo_origin", None) is None:
                                # Move (re)activating mid-drag with no press
                                # latch (imgui interruption / stall): the
                                # current position is the best gesture origin.
                                draw_state._move_undo_origin = (draw_state.window_pos[0],
                                                                draw_state.window_pos[1])

                        pos_x = draw_state._initial_window_pos[0] + move_drag.total_dx
                        pos_y = draw_state._initial_window_pos[1] + move_drag.total_dy
                        draw_state.window_pos = (pos_x, pos_y)
                        # A hand move: the edge pass below (window_edge_pass →
                        # os_frame) pushes the OS window's edges out of the
                        # window's way, and nothing clamps the move - a window
                        # may be dragged partly off the display (Lukas 08-27;
                        # the "never above the display top" clamp that lived
                        # here is gone: it pinned the window at the content's
                        # top before the top edge could ever push the OS edge)
                        # - except the DISPLAY's top, clamped in _frame_pass
                        # AFTER the move (Toggles.Melty.window_top_hard_limit).
                        draw_state._hand_move_frame = Melty.frame_count
                    elif imgui_active or (left_mouse_down is None and on_held is None):
                        # Keep the press-anchored baseline alive through the
                        # pre-activation held frames (button down, or below
                        # threshold) so it's still there when the drag's first
                        # event lands after a stall. Clear when the button is
                        # up, the press never entered this window, or imgui
                        # owns the gesture (resuming after an imgui
                        # interruption must rebase for continuity, as before).
                        # End of the move gesture: a window that actually
                        # moved since its origin latched gets one nav-undo
                        # step (Ctrl+Shift+Left walks it back and the
                        # Orchestrator cues and restores offscreen). A plain
                        # click leaves origin == pos and records nothing.
                        _move_origin = getattr(draw_state, "_move_undo_origin", None)
                        if _move_origin is not None:
                            if (_move_origin[0], _move_origin[1]) != (draw_state.window_pos[0],
                                                                      draw_state.window_pos[1]):
                                from src.lsd.gl_gui.view.core_views.core_undo import NavUndo
                                NavUndo.record_window_move(draw_state, _move_origin,
                                                           draw_state.window_pos)
                            draw_state._move_undo_origin = None
                        draw_state._initial_window_pos = None

                # Anchor / pin stamping applies to explicitly-positioned windows
                # too: the find bar (and the Save/Load pending dialogs) pass
                # window_pos= together with anchor= and pin_to_clip=. These were
                # previously only stamped for draggable (no window_pos kwarg)
                # windows, so the pin was silently ignored and the find bar rode
                # with scrolled content instead of pinning to the visible top.
                anchor_pos = kwargs.get("anchor", Anchor.TOP_LEFT)
                draw_state.anchor_pos = anchor_pos
                draw_state.parent_anchor_pos = kwargs.get("parent_anchor", Anchor.TOP_LEFT)
                # Only the Pin enum is supported. Store the mode; the actual
                # view (parent / grandparent / root, or clip) is resolved up
                # from the tree at compute time (see draw_state.pin_rect), so
                # nothing is snapshotted here and the float tracks its target as
                # it scrolls. Anything else means "not pinned".
                pin = kwargs.get("pin_to_clip", None)
                draw_state.pin_to_clip = pin if isinstance(pin, Pin) else None

                # Draggable windows always render at their own abs box; an
                # explicitly-positioned window keeps the caller's cursor but
                # a clip overrides its position (abs box ignores the captured
                # offsets, so the cursor must follow the pin).
                if not _explicit_window_pos or draw_state.pin_to_clip is not None:
                    imgui.set_cursor_screen_pos((snap_int(draw_state.abs_left), snap_int(draw_state.abs_top)))

                # Draggable window frame edges (left/right) use the shared
                # column-edge collision system (columns.window_edge_pass).
                # Resolved through the module each call so columns.py
                # hotswaps before reaching here.
                from src.lsd.gl_gui.view.core_views import columns as _columns
                try:
                    _columns.window_edge_pass(draw_state)
                except Exception as _edge_err:
                    if getattr(draw_state, "_edge_pass_error", None) != repr(_edge_err):
                        draw_state._edge_pass_error = repr(_edge_err)
                        print(f"[window_edge_pass] {draw_state.name}: {_edge_err!r}")

            kwargs['melty_window'] = False
            Melty.size_stack.append((draw_state.width, draw_state.height))

            if len(Melty.melty_window_stack) > 0:
                Melty.is_melty_window = True
            else:
                Melty.is_melty_window = False



            ######################## ERROR HANDLING FOR TYPES ########################
            cursor_pos = imgui.get_cursor_pos()
            imgui.set_cursor_pos((snap_int(cursor_pos[0]), snap_int(cursor_pos[1])))


            if "changed" in wanted_params:
                draw_state._external_change |= kwargs.get("changed", False)
                kwargs['changed'] |= draw_state._external_change
                if draw_state.frame_count < 1:
                    kwargs['changed'] = True

                draw_state._pending |= draw_state._external_change
                kwargs['pending'] = draw_state._pending | kwargs.get("changed", False)
                if kwargs['pending']:
                    pass
                # kwargs['external_change'] = draw_state._external_change
                # if draw_state._external_change:
                #     draw_state._input_value_cache = input_value
                # elif draw_state._input_value_cache is not UNSET_VALUE:
                    # input_value = draw_state._input_value_cache
                    # draw_state._input_value = input_value
                    # draw_state._raw_input_value = input_value
            # else:
                # draw_state._input_value_cache = input_value

            # if draw_state._output_value_cache is UNSET_VALUE and not old_convert_path:
            #     draw_state._external_change = True

            # _external_change (git watcher wake, FileWatch, MergeFiles.wake,
            # ExternalChanges, PendingSave) does not switch the cache off: it
            # refuses the cache HIT inside mark_start_offscreen, so the body
            # renders live THROUGH the tile path and the window still marks its
            # depth (mask_mark_view in mark_end_offscreen) and the fresh
            # render is captured. With use_cache off for the frame the window
            # skipped its tile context entirely: no depth mark (its shadow
            # dropped and its cached children cast onto rank-0 background
            # above it, the mangled merge-window / code-editor shadows
            # after an external edit) and no capture (the next frame shows
            # the stale tile).
            use_cache = kwargs.get("use_cache", False) and Melty.cache.enabled
            draw_state._bypass_cache = kwargs.get("draw", False)
            draw_state.use_cache = use_cache
            # Opt-in (off by default): during window mouse-dragging the view's
            # stale cache is blitted instead of live re-rendering every frame.
            # Read by mark_start_offscreen via getattr (corner_radius pattern).
            draw_state.freeze_resize = kwargs.get("freeze_resize", False)
            # The effective corner radius, stamped for EVERY view (the blit
            # rounds a cached tile by it) - not only in the show_bg path
            # below, so a bg-less view's corner_radius=0 never reached the
            # blit and its square content got clipped round.
            draw_state.corner_radius = kwargs.get("corner_radius", 5.0)
            if draw_state.freeze_resize:
                # Blit owns all bg rendering for freeze_resize views
                # (Melty.cache.draw_freeze_bg, called from the show_bg block
                # below on live frames and by the frozen blit mid-drag) -
                # stamp the recipe it draws with. update(), not overwrite: the
                # live call adds a capture of current color globals
                # (bg_depth/bg_stack/tint) that must survive frozen frames'
                # re-stamps.
                if getattr(draw_state, "_frozen_bg_kwargs", None) is None:
                    draw_state._frozen_bg_kwargs = {}
                draw_state._frozen_bg_kwargs.update({
                    "show_bg": kwargs.get("show_bg", False),
                    "bg_offset": kwargs.get("bg_offset", 0),
                    "max_bg_depth": kwargs.get("max_bg_depth", None),
                    "max_bg_value": kwargs.get("max_bg_value", None),
                    "saturation": kwargs.get("saturation", 1.0),
                    "nested_bg": not closable and kwargs.get("bg_offset", 0) >= 0,
                })
            # if (draw_state.parent_window is not None and draw_state.window_pos is not None and
            #         draw_state.parent_window.window_pos is not None and closable):
            draw_state.left = draw_state.abs_left
            draw_state.top = draw_state.abs_top

            if fixed_size:
                Melty.fixed_size_stack.append(draw_state)

            if (fixed_size and auto_resize and closable and draw_state.multi_line
                    and not Melty.is_wrapped() and kwargs.get("fill_height", None) is None):
                draw_state.width = 100

            header_width = draw_state.header_width + draw_state.header_end_width
            if len(Melty.fixed_size_stack) > 0:
                fixed_size_draw_state = Melty.fixed_size_stack[-1]
                parent_wrap_width = fixed_size_draw_state.width
                parent_wrap_height = fixed_size_draw_state.height - fixed_size_draw_state.header_height - fixed_size_draw_state.footer_height
                parent_wrap_left = fixed_size_draw_state.abs_left
                parent_wrap_top = fixed_size_draw_state.abs_top
                # A pushed Melty clip is a HARD width limit: we can't
                # use space past it, so when the live clip ends before the
                # wrap frame does, wrap at the clip's right edge instead
                # (draw_columns pushes exactly this clip around each cell).
                # For everything else the innermost clip is the enclosing
                # view's own clip == the wrap frame, and this is a no-op.
                _live_clip = Melty.get_clip_rect()
                if (_live_clip is not None and parent_wrap_width is not None
                        and _live_clip[2] < parent_wrap_left + parent_wrap_width):
                    parent_wrap_width = max(0, _live_clip[2] - parent_wrap_left)
            elif draw_state.clip_rect is not None:
                clip_size = draw_state.clip_size
                clip_rect = draw_state.abs_clip_rect
                if clip_size is not None:
                    parent_wrap_width = clip_size[0]
                    parent_wrap_left = clip_rect[0]
                    parent_wrap_top = clip_rect[1]
                    parent_wrap_height = clip_size[1]
                else:
                    parent_wrap_width = imgui.get_io().display_size[0]
                    parent_wrap_left = 0
                    parent_wrap_top = 0
                    parent_wrap_height = imgui.get_io().display_size[1]
            else:
                clip_size = draw_state.clip_size
                clip_rect = draw_state.abs_clip_rect
                if clip_size is not None:
                    parent_wrap_width = clip_size[0]
                    parent_wrap_left = clip_rect[0]
                    parent_wrap_top = clip_rect[1]
                    parent_wrap_height = clip_size[1]
                else:
                    parent_wrap_width = imgui.get_io().display_size[0]
                    parent_wrap_left = 0
                    parent_wrap_top = 0
                    parent_wrap_height = imgui.get_io().display_size[1]

            column_parent = draw_state._parent if len(Melty.fixed_size_stack) > 1 else draw_state._parent

            if draw_state.frame_count > 1:
                draw_state.final_max_column = draw_state._current_max_column

            if draw_state.final_max_column > 0 and column is None and column_parent is not None:
                column = kwargs.get("column", 0)
                kwargs['column'] = column
            else:
                column = kwargs.get("column", None)

            draw_state._columns_top = None
            if column is not None and column_parent._columns_top is None:
                column_parent._columns_top = imgui.get_cursor_screen_pos()[1] - column_parent.abs_top

            x_offset = draw_state.abs_left - parent_wrap_left
            indent_x = kwargs.get("indent_size", 0)
            # if column_parent._current_max_column == 0:
            #     column_parent._column_cursor = imgui.get_cursor_screen_pos()[1]

            if column is not None and column_parent is not None:
                column_parent._current_max_column = max(column_parent._current_max_column, column)
                n_cols = column_parent.final_max_column + 1
                # A child may pin its column's width: column_text(column=1,
                # column_width=100) writes the matching divider offset on the
                # parent. Last setter in the frame wins.
                column_width_override = kwargs.get("column_width", None)
                if column_width_override is not None:
                    set_column_width(column_parent, n_cols, column, column_width_override)
                offsets = column_parent.column_offsets
                if n_cols > 1:
                    left_boundary = column_boundary(column_parent.content_width, n_cols, offsets, column)
                    right_boundary = column_boundary(column_parent.content_width, n_cols, offsets, column + 1)
                else:
                    # Only one column this frame: span the full content width,
                    # ignoring any pinned divider offsets left over from a
                    # previous multi-column frame.
                    left_boundary = 0
                    right_boundary = column_parent.content_width
                parent_wrap_width = right_boundary - left_boundary
                column_parent._column_width = parent_wrap_width


                parent_wrap_left = draw_state.abs_left + snap_int(left_boundary)
                available_width = int(parent_wrap_width - indent_x - 5)
            else:
                # content_margin is a RIGHT-side depth inset (2 px per stacked
                # bg) that keeps nested content short of the enclosing bg
                # edge. freeze_resize views run edge to edge — the scrollbar
                # is drawn outside them by blit_offscreen - so they take
                # none of it (a code-editor pane in a column cell otherwise
                # clipped its glyphs 4-6 px before its own bg edge).
                _depth_margin = 0.0 if draw_state.freeze_resize else content_margin
                available_width = (parent_wrap_width - x_offset - _depth_margin)

            if len(Melty.fixed_size_stack) > 0:
                if (draw_state.auto_resize and not closable and not
                Melty.is_wrapped() and passed_width is None) and (kwargs.get("fill_height", None) is None):
                    draw_state.width = available_width

            if column is not None and column_parent is not None:
                max_height = column_max_height(column_parent)
                if not closable:
                    draw_state.height = snap_int(min(draw_state.height, max_height))
                    draw_state._source["height"] = "column not closable, item_rect[1]"

            if kwargs.get("show_bg", False):
                outline_margin = 3
            else:
                outline_margin = 0

            if kwargs.get("fill_height", None) is not None and passed_height is None and auto_resize and not closable:
                # Is fill height callable?
                if callable(kwargs.get("fill_height")):
                    fill_height_result = kwargs.get("fill_height")(draw_state)
                    draw_state._source["height"] = "fill height callback"
                    if len(Melty.fixed_size_stack) > 1:
                        fixed_size_draw_state = Melty.fixed_size_stack[-2]
                        parent_wrap_width = fixed_size_draw_state.width
                        draw_state.width = snap_int(parent_wrap_width)
                    draw_state.height = snap_int(fill_height_result) - content_margin
                    parent_wrap_width = fixed_size_draw_state.width
                    available_width = snap_int(parent_wrap_width)

                else:
                    if len(Melty.fixed_size_stack) > 2:
                        fixed_size_draw_state = Melty.fixed_size_stack[-2]
                    else:
                        fixed_size_draw_state = Melty.melty_window_stack[-1] if len(Melty.melty_window_stack) > 0 else draw_state
                    parent_wrap_width = fixed_size_draw_state.width
                    available_width = snap_int(parent_wrap_width)
                    draw_state._source["height"] = "fill height"
                    draw_state.width = snap_int(available_width)

                    view_top = draw_state.abs_top
                    if draw_state.parent_window is not None:
                        delta_from_top = view_top - draw_state.parent_window.abs_top
                        if draw_state.expanded:
                            fill_height = min(kwargs.get("max_height", 1e9), draw_state.parent_window.height - delta_from_top)

                        else:
                            fill_height = 20
                        draw_state.height = fill_height
                        parent_wrap_height = fill_height
                    # fixed_size_draw_state = Melty.fixed_size_stack[-2]
                    # fixed_size_draw_state_p = Melty.fixed_size_stack[-1]
                    #
                    # parent_offset = fixed_size_draw_state_p.abs_top - fixed_size_draw_state.abs_top
                    #
                    # # Fix this eventually
                    # current_y = imgui.get_cursor_screen_pos()[1]
                    # parent_wrap_height = (
                    #             fixed_size_draw_state.height - draw_state.header_height - draw_state.footer_height -
                    #             fixed_size_draw_state.footer_height - parent_offset)
                    #
                    #
                    # available_width = snap_int(parent_wrap_width)
                    # draw_state.height = snap_int(parent_wrap_height)
                    # draw_state._source["height"] = "fill height"
            single_line_avail = available_width - header_width - 10


            # Auto expand (go multi-line) when the content can't fit on a
            # single line at its min_width. Falls back to 150 when no
            # min_width is set so widgets without one keep prior behavior.
            single_line_trigger = kwargs.get("min_width", draw_state.min_width) or 30
            header_same_line = kwargs.get("header_same_line", False)
            if "content_width" in kwargs:
                # draw_state.content_width = kwargs["content_width"]
                draw_state._source["content_width"] = "explicit content_width"
                draw_state.multi_line = True

            else:

                if ((single_line_avail < single_line_trigger or (
                        draw_state.height is not None and draw_state.height - draw_state.footer_height > 50))
                        and not header_same_line):
                    draw_state.multi_line = True
                    draw_state.content_width = available_width
                    draw_state._source["content_width"] = "available_width"
                else:
                    draw_state.multi_line = False
                    draw_state.content_width = single_line_avail
                    draw_state._source["content_width"] = "single_line_avail"

                # Always reserve room for the scrollbar on any view that could
                # scroll (height externally limited, scrolling not disabled),
                # whether or not the bar is currently showing - content width
                # must not shift when the bar appears/disappears.
                can_scroll = (not kwargs.get("disable_scroll", False)
                              and (not draw_state.auto_resize
                                   or kwargs.get("max_height", None) is not None))
                # freeze_resize views give up nothing: their content runs to
                # the right edge and the bar is drawn over it by
                # blit_offscreen (BlitCache.draw_freeze_scrollbar, after the
                # tile image on each frame).
                if can_scroll and not draw_state.freeze_resize:
                    _sb_reserve = (kwargs.get("scroll_bar_width", SCROLL_BAR_WIDTH_DEFAULT)
                                   + SCROLLBAR_MARGIN)
                    draw_state.content_width = max(0, draw_state.content_width - _sb_reserve)
                    draw_state._source["content_width"] += " - scrollbar"

            if draw_state.expanded:
                draw_state.min_width = kwargs.get("min_width", draw_state.min_width)
                draw_state.min_height = kwargs.get("min_height", draw_state.min_height)
                # `max_height` alone bounds only what the wrapper SIZES (the
                # auto-resize measure, a closable window's first-render
                # content adopt - Mode.LIVE_WINDOW relies on the handle being
                # free under it); `enforce_max_height=True` makes it a hard
                # cap the resize paths hold in realtime (draw_state.max_height).
                draw_state.max_height = (kwargs.get("max_height", None)
                                         if kwargs.get("enforce_max_height", False) else None)

                if draw_state.width is not None and draw_state.min_width is not None:
                    draw_state.width = max(draw_state.width, draw_state.min_width)
                    draw_state.content_width = max(draw_state.content_width, draw_state.min_width)

                if draw_state.height is not None and draw_state.min_height is not None:
                    if draw_state.height < draw_state.min_height:
                        draw_state._source["height"] = "initial window size"

                    draw_state.height = max(draw_state.height, draw_state.min_height)
                # A closable (user-resized) window never exceeds its
                # max_height - the cap the resize paths enforce mid-drag,
                # re-applied here so a size inherited from elsewhere (a
                # default size, a shrunk window) lands under it too.
                if closable and draw_state.max_height and draw_state.height:
                    if draw_state.height > draw_state.max_height:
                        draw_state.height = snap_int(draw_state.max_height)
                        draw_state._source["height"] = "max_height cap"

            ################# Columns
            if column is not None and column_parent is not None and draw_state.parent_window is not None:
                column_cursor_y = column_parent._column_cursor[column][1] + column_parent._columns_top
                imgui.set_cursor_screen_pos((snap_int(parent_wrap_left + indent_x),
                                             snap_int(parent_wrap_top + column_cursor_y)))

                # See the main-entry capture at :393 - store unscrolled position
                # in parent_window's content so _abs_left can react to mid-frame
                # ancestor-scrolls without waiting for re-render.
                anc_sx, anc_sy = draw_state._ancestor_scroll()
                draw_state.left_offset, draw_state.top_offset = (
                    imgui.get_cursor_screen_pos()[0] - draw_state.parent_window.abs_left + anc_sx,
                    imgui.get_cursor_screen_pos()[1] - draw_state.parent_window.abs_top + anc_sy)

                draw_state.left = draw_state.abs_left
                draw_state.top = draw_state.abs_top
                # Collisions.check(column_parent)

                Melty.fixed_size_stack.append(draw_state)

            #
            # if draw_state.final_max_column > 0:
            #     n_cols = draw_state.final_max_column + 1
            #     offsets = draw_state._column_offsets
            #     draw_list: _DrawList = imgui.get_window_draw_list()
            #     table_top = 0
            #
            #
            #     for i in range(column_parent.final_max_column + 1):
            #         column_height = column_parent._column_cursor[c][1]
            #         if column_height > table_top:
            #             table_top = column_height + 2
            #     for c in range(1, draw_state.final_max_column + 1):
            #         # Draw divider lines, we are the parent now
            #         boundary_x = column_boundary(draw_state.content_width, n_cols, offsets, c)
            #         columns_top = draw_state._columns_top if draw_state._columns_top is not None else draw_state.footer_t
            #         draw_list.add_line(draw_state.left + snap_int(boundary_x), draw_state.abs_top  + snap_int(columns_top) + 30,
            #                            draw_state.left + snap_int(boundary_x),
            #                            draw_state.abs_top + snap_int(draw_state.height),
            #                            pack_color(0.0, 0.0, 0.0, 0.3), 1)


            ##########################
            if kwargs.get("live", False):
                draw_state.live = True
                fa_live_icon = "\uf0e7  Live"
                draw_list: _DrawList = imgui.get_window_draw_list()
                draw_list.add_text(draw_state.left + 5, draw_state.top - 20,
                                   pack_color(1.0, 0.0,
                                                            0.0, 1.0), fa_live_icon)
                Melty.cache.invalidate(tile_id, note=Note(name="Live view", reason="live=True", tint=(1, 0.5, 0.5)))
            kwargs.pop("live", None)
            if not kwargs.get('visible_in_ui', True):
                if return_extras:
                    return False, None, draw_state
                return False, None

            last_bounding_hovered = draw_state._bounding_hovered
            new_bounding_hovered = draw_state.is_bounding_hovered()

            # Suppress hover if a closable window with higher z-order covers this view.
            # Not while this view's imgui item is ACTIVE (a drag_float mid-drag): an
            # active item follows the pointer wherever it goes - imgui semantics - but
            # the press that initiated it in a BEHIND window leaves the window behind
            # until the deferred raise (apply_move_to_front waits for imgui_active to
            # clear). Suppressed here, the drag crossing the front window unhovered
            # the item, its tile fell back to the blit pile, the item stopped being
            # submitted, imgui dropped it, and the window's captured move handle
            # took the rest of the gesture (Loras flew off-screen, 09-01).
            if new_bounding_hovered and not draw_state._imgui_is_active:
                mouse_x, mouse_y = imgui.get_mouse_pos()
                hits = Melty.bvh_query(mouse_x, mouse_y)
                my_depth = draw_state.shadow_depth
                for ds in hits:
                    parent_window = draw_state.parent_window if draw_state.parent_window is not None else None
                    if ds.closable and ds is not draw_state and ds is not parent_window and ds.shadow_depth > my_depth:
                        new_bounding_hovered = False
                        break

            hover_changed = last_bounding_hovered != new_bounding_hovered
            draw_state._bounding_hovered = new_bounding_hovered
            _wtG = time.perf_counter()   # TEMP perf: hover/bounding hovered
            if (draw_state.width is None or draw_state.height is None or hover_changed or
                    draw_state._bounding_hovered or draw_state._imgui_popover_open):
                someone_elses_scroll = Melty.on_scroll and not draw_state.scroll_visible
                if (not Melty.on_drag and not imgui.is_mouse_dragging(2) and not imgui.is_mouse_dragging(1)) and not someone_elses_scroll:
                    if not draw_state.just_shadow:
                        note = "hover_change"
                        if draw_state.width is None:
                            note = "hover_change + width=None"
                        Melty.cache.invalidate(tile_id, force=True, note=Note(name=note, tint=(1,1,0, 0.1),
                                                                                              reason="unhovered" if not draw_state._bounding_hovered else "hovered",
                                                                                              frame=Melty.frame_count,
                                                                                              draw_state=draw_state))

            if draw_state.width > 0 and draw_state.height > 0:
                inside_clip = Melty.fully_inside_clip(rect=(draw_state.abs_left, draw_state.abs_top,
                                                            draw_state.width, draw_state.height))
                needs_invalidate = False
                if inside_clip != draw_state.fully_clipped and inside_clip:
                    needs_invalidate = True
                draw_state.fully_clipped = inside_clip

                inside_clip = Melty.inside_clip(rect=(draw_state.abs_left, draw_state.abs_top + draw_state.header_height,
                                                      draw_state.width, draw_state.height))
                if inside_clip != draw_state.inside_clip and inside_clip:
                    needs_invalidate = True
                draw_state.inside_clip = inside_clip

                # if not draw_state._one_full_draw:
                #     if needs_invalidate and not Melty.window_drag and not imgui.is_mouse_dragging(
                #             1) and not imgui.is_mouse_dragging(2):
                #
                #         Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=5, force=True, note=Note(name="Clip change",
                #                                                       draw_state=draw_state,
                #                                                       reason="",
                #                                                       tint=(0.5, 0.5, 1)))

            if kwargs.get("shadow", False):
                Melty.shadow_depth = Melty.shadow_depth + 1 + total_z_offset
            else:
                Melty.shadow_depth = Melty.shadow_depth + total_z_offset

            if draw_state.tile_mode == TileMode.MIN:
                draw_state.shadow_margin = 0
            else:
                draw_state.shadow_margin = 0

            push_id(unique)

            if closable:
                Melty.push_clip((draw_state.abs_left, draw_state.abs_top,
                                 draw_state.abs_left + draw_state.width,
                                 draw_state.abs_top + draw_state.height))
            clip_rect = Melty.get_clip_rect()
            if clip_rect is not None:
                draw_state.clip_rect = clip_rect
                # Anchor the captured (absolute) clip to the parent window's
                # position now, so abs_clip_rect can shift it by the window's
                # later movement (a window drag) and stay current without a
                # re-render. None when this view has no enclosing window.
                _pw = draw_state.parent_window
                if _pw is not None and _pw is not draw_state:
                    draw_state._clip_win_anchor = (_pw._abs_left(), _pw._abs_top())
                else:
                    draw_state._clip_win_anchor = None
                left_clipped_by = max(0, clip_rect[0] - draw_state.abs_left)
                top_clipped_by = max(0, clip_rect[1] - draw_state.abs_top)
                right_clipped_by = max(0, (draw_state.abs_left + draw_state.width) - (clip_rect[2]))
                bottom_clipped_by = max(0, (draw_state.abs_top + draw_state.height) - (clip_rect[3]))
                draw_state.clipped_by_rect = (left_clipped_by, top_clipped_by, right_clipped_by, bottom_clipped_by)


            # Filter out originated functions that have an error pending state
            # no point auto-applying a converter that will just error out.
            _error_originated = set()
            for _p in draw_state._all_pending.values():
                if isinstance(_p, Pending) and _p.state == PendingState.ERROR:
                    _error_originated.add(_p.originated)

            _active_auto_apply = tuple(f for f in auto_apply if f not in _error_originated)

            if draw_state._save_pending_obj is not None and draw_state._save_pending_obj.originated in _active_auto_apply:
                if draw_state._save_pending_obj.originated not in _error_originated:
                    draw_state._apply_save = draw_state._save_pending_obj.originated
                    Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                    request_render()

            if draw_state._internal_pending is not None and draw_state._internal_pending.originated in _active_auto_apply:
                # Don't auto-load if there's a pending save, let the user decide
                if not draw_state._show_save:
                    draw_state._apply_load = draw_state._internal_pending.originated
                    Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                    request_render()

            if o_kwargs.get("searchable", False) or kwargs.get("searchable", False):
                # Ctrl+F opens this view's search. It's a modifier-qualified
                # action, so Ctrl+Shift+F (draw_main's global search) lands in a
                # different bucket and never reaches here.
                search_requested = draw_state.on_action("inverted_ctrl_f_down")

                if search_requested:
                    # Inverted dispatch delivers Ctrl+F to the BACK-most hovered
                    # subscriber, and hover eligibility ignores window occlusion
                    # - so a searchable inside a nested window layered BEHIND
                    # the hovered one receives the press. Act only when this
                    # view lives in the window actually under the cursor - every
                    # other case (occluded sibling, floating window in front) is
                    # resolved by draw_main's root fallback, which ranks hits by
                    # window and always runs (non_blocking).
                    _hits = Melty.bvh_query(*imgui.get_mouse_pos())
                    _my_win = _owning_window(draw_state)
                    if _hits and _owning_window(_hits[0]) is not _my_win:
                        search_requested = False
                    elif _hits:
                        # hits[0] alone can't be trusted for the window test: a
                        # higher z_pos on a nested-window subtree sorts a PARENT-
                        # window hit to the front, so the parent passes the
                        # check above and steals Ctrl+F from the nested child.
                        # Scan every hit: a searchable living in a DESCENDANT
                        # window under the cursor owns the key over this view -
                        # yield (its own subscription or the root fallback
                        # opens its bar instead).
                        for _h in _hits:
                            _hw = _owning_window(_h)
                            if _hw is _my_win or not _window_descends_from(_hw, _my_win):
                                continue
                            _hkw = getattr(_h, '_kwargs', None) or {}
                            if (_hkw.get('searchable')
                                    or getattr(_hkw.get('render_func'), '_searchable', False)):
                                search_requested = False
                                break

                if len(Melty.search_stack) > 0:
                    kwargs["search_text"] = Melty.search_stack[-1]

                if search_requested:
                    # Prefill the find box from the current text selection -
                    # also when the search is already open, replacing the query.
                    _sel = _selection_for_search(draw_state)
                    if _sel is not None and _sel != draw_state.search_text:
                        draw_state.search_text = _sel
                        # New query: recompute matches across the subtree, same
                        # as an edit typed into the find box (render_search).
                        Melty.cache.invalidate_up(draw_state._tile_id,
                                                  force=True, max_depth=12)
                    # Multiple find bars may stay open at once: opening a view's
                    # search no longer closes whichever view's search was open.
                    draw_state.search_active = True
                    # Reset so render_search re-requests focus, and release
                    # the view's own text focus, so the search box takes
                    # priority even when the view was already focused.
                    draw_state._search_was_active = False
                    # # One-shot: force the find box to claim focus on the next
                    # # render, if the editor re-grabs melty text focus before
                    # # the box renders (clearing text_focused_ds here alone isn't
                    # # enough - the next searchable view did reclaim it, which
                    # # left the box un-focused after Ctrl+F).
                    # draw_state._search_focus_pending = True
                    # Keep the open popover (color picker / dropdown) alive:
                    # it isn't in this view's ancestor tree, so a bare
                    # clear_focus(not_this=draw_state) close it on every
                    # Ctrl+F. Popovers close on outside click / Esc / pick only.
                    Melty.clear_focus(not_this=(draw_state, Melty.popover_focused_ds))
                    Melty.focused_ds = draw_state
                    request_render()

                if draw_state.search_active:
                    # Stay live while searching so the find UI (inline or the
                    # floating draw_search window) keeps rendering even if the
                    # view would otherwise be served from cache.
                    # draw_state._external_change = True
                    esc_key = draw_state.on_action("escape_key_down_inverted")
                    if esc_key:
                        Melty.focused_ds = None
                        draw_state.search_active = False
                        # Keep search_text so Find restores the last query.
                        draw_state._search_was_active = False
                        if Toggles.TextEditor.text_focus_stack_trace:
                            print_stack_trace()
                        request_render()

            # Push search term to stack so child views can apply search converters

            draw_state.depth_and_layer = (Melty.shadow_depth, Melty.paint_rank)
            _pushed_search = False
            draw_state._melty_cursor = (0, 0)  # column -> (x, y)

            _wtC = time.perf_counter()   # TEMP perf: pre-cache is done
            _render_body = Melty.cache.mark_start_offscreen(draw_state=draw_state)
            if not _render_body and _has_imgui:
                # Blit-cache hit: the body below is skipped, and with it every
                # on_action it would have made (the text editor's I-beam, tab
                # ctrl+enter, ...). Re-issue last render's record so a cached
                # tile keeps its subscriptions - same fix as the pre-gate
                # register_hovered above, for body-level subs.
                draw_state.replay_body_actions()
            if _render_body:
                # Fresh record for this render's body-level on_action calls.
                draw_state._body_actions = (Melty.frame_count, [])
                # Rect-scoped event params (draw_state.event_rect): take the
                # LAST body run's declarations for this run's registration
                # lookup and clear the slot, so the body re-declares each run
                # and a run that stops declaring lifts the scope.
                _event_rects = draw_state._event_rects
                draw_state._event_rects = None
                draw_state._melty_content_height = 0

                if style_manager is not None:
                    draw_state.current_tint = style_manager.get_tint()

                draw_state._column_cursor = defaultdict(lambda: [0, 0])  # column -> (x, y)

                draw_state._outside_column_height = 0

                draw_state._current_max_column = 0

                if closable:
                    Melty.root_draw_states[draw_state.id]
                from src.lsd.gl_gui.view.core_views.new_core_view import pending_window

                from src.lsd.gl_gui.view.mode import Mode

                # Floating find bar: searchable views that have no header can't
                # show the inline search box, so float the shared render_search
                # UI in a window anchored to this view's top-right.
                _has_header = kwargs.get("show_header", True)
                # if draw_state.search_active and not _has_header and kwargs.get("searchable", False):
                from src.lsd.gl_gui.view.core_views.new_core_view import draw_search
                # WINDOW_CLEAN (not WINDOW) - it auto-resizes to the find bar
                # and drops the tree arrow/tint, matching pending_window.
                # manual_search=True: the view's own body renders the find UI
                # (e.g. draw_text's inline search box) - skip the floating Find
                # window here but keep the session push/recount below, which is
                # what drives match highlighting and the count either way.
                _manual_search = (o_kwargs.get("manual_search", False)
                                  or kwargs.get("manual_search", False))
                if len(Melty.search_stack) == 0 and kwargs.get("searchable", False) and draw_state.search_active:
                    if _manual_search:
                        # Focus retry, manual flavor: the body's own search box
                        # claims focus, so keep THIS tile re-rendering until it
                        # stamps _search_was_active (self-limiting, one frame).
                        if (not draw_state._search_was_active
                                or draw_state._search_focus_pending):
                            Melty.cache.invalidate_up(draw_state._tile_id, force=True)
                            request_render()
                    else:
                        extras = draw_search(input_value=draw_state,
                            closed=False,
                            auto_resize=True,
                            swoosh=False,
                            tint=(1.0, 1.0, 1.0),
                            mode=Mode.WINDOW_CLEAN,
                            # Pin live to this view's VISIBLE box: Pin.CLIP
                            # intersects the view's rect with the window, so the
                            # bar rides the view's top-left corner while that is
                            # on-screen and stops at the window edge once the
                            # view scrolls under it (Pin.PARENT followed the raw
                            # view top, which scrolled the bar away).
                            pin_to_clip=Pin.CLIP,
                            window_pos=(0, 0),
                            width=300,

                            initial={"height": 30},
                            anchor=Anchor.BOTTOM_LEFT,
                            name=f"Find{unique}",
                            return_extras=True)
                        search_ds = extras[2]
                        if search_ds.last_seen is None:
                            search_ds.window_pos = (0, 0)

                        # Focus retry: render_search (inside this Find window) is
                        # what claims text focus for the box, and it stamps
                        # _search_was_active=True when it actually runs. While the
                        # claim is pending (Ctrl+F just flipped _search_was_active
                        # False) a CACHED Find tile would blit-skip and never run
                        # render_search - so a repeated Ctrl+F couldn't re-focus
                        # the box. Invalidate the Find subtree until the claim
                        # lands (one frame in practice, self-limiting).
                        if (not draw_state._search_was_active
                                or draw_state._search_focus_pending):
                            Melty.cache.invalidate_up(search_ds._tile_id, force=True)
                            request_render()

                    # Build this frame's cross-view aggregation session. A new
                    # term resets the global selection to the first match; nav
                    # from the find UI flags a scroll. Children register their
                    # matches into this SearchTerm as they render; its .total is
                    # read back into text_search_count after the body (below).
                    term_str = draw_state.search_text or ""
                    # full_search drives the recount-current-match walk below;
                    # scroll additionally yanks the view to the selected match.
                    # A term change (typing) always recounts, but only scrolls
                    # when scroll_while_typing allows it; explicit navigation
                    # (Enter / arrows → _search_nav_pending) always scrolls.
                    scroll = False
                    full_search = False
                    if draw_state._search_last_term != term_str:
                        draw_state._search_last_term = term_str
                        draw_state.text_search_current = 0
                        full_search = True
                        scroll = Toggles.SearchSettings.scroll_while_typing
                    if draw_state._search_nav_pending:
                        draw_state._search_nav_pending = False
                        full_search = True
                        scroll = True
                    session = SearchTerm(term_str, current=draw_state.text_search_current,
                                         scroll_to=scroll)
                    draw_state._search_session = session
                    Melty.search_stack.append(session)
                    _pushed_search = True

                    # Single source of truth for the find UI. BEFORE the body
                    # renders, walk the full draw_state tree to (1) count each
                    # match via each view's _search_matcher and (2) mark which
                    # match is current, stashing its local index on the owning
                    # node (_search_active_local). Each view reads that mark
                    # while drawing and highlights/scrolls to it, so the count
                    # and the selection always come from this one walk - no
                    # render-time claims, and off-screen rows can't join.
                    # Only on a new-search frame (term change / nav); otherwise
                    # the marks from the last such frame stand.
                    # Recompute the count + current-match marks on a full-search
                    # frame (term change / nav), AND whenever the term currently
                    # has zero results. The zero case self-heals a fresh load from
                    # disk: from_dict rebuilds the searched subtree as brand-new
                    # draw_states with no matchers yet, under the SAME search
                    # term, so the term-change walk on the load frame counts 0;
                    # re-counting when the total sits at 0 recovers it as soon as
                    # the rebuilt views have rendered their matchers - no new state,
                    # so no scroll yank (the scroll/invalidate is gated to a
                    # full-search frame below).
                    _recount = (full_search
                                or (term_str and draw_state.text_search_count == 0))
                    if _recount:
                        _q = str(session)
                        _tally = SearchTerm(_q)
                        search_walk(draw_state, _q, _tally)
                        _total = _tally.total
                        draw_state.text_search_count = _total
                        _cur = (draw_state.text_search_current % _total) if _total > 0 else 0
                        draw_state.text_search_current = _cur
                        _current_node = search_walk(
                            draw_state, _q, SearchTerm(_q, current=_cur))
                        # Remember the current match's node so Ctrl+Enter in the
                        # find UI can fake a mouse-down on it (or its current
                        # child) to "click" the selected result.
                        _prev_search_node = Melty.search_current_node
                        Melty.search_current_node = _current_node
                        # The node the selection LEFT must re-render too: the
                        # text editor re-collapses its search-expanded view in
                        # its body, which a cached node would never run.
                        if (_prev_search_node is not None
                                and _prev_search_node is not _current_node
                                and _prev_search_node is not draw_state
                                and _prev_search_node._tile_id is not None):
                            Melty.cache.invalidate_up(_prev_search_node._tile_id,
                                                      force=True, max_depth=12)
                        # Force the current match's view (and ancestors) to
                        # re-render so an off-screen row scrolls into view - only
                        # on a real full-search frame, never on a passive recount.
                        if (session.scroll_to
                                and _current_node is not None and _current_node is not draw_state
                                and _current_node._tile_id is not None):
                            Melty.cache.invalidate_up(_current_node._tile_id,
                                                      force=True, max_depth=12)

                # Pre-discover load_data for revert actions
                _chain_load_data_early = None
                _convert_in_early = o_kwargs.get("convert_in", None) or kwargs.get("convert_in", None)
                if _convert_in_early is not None:
                    for _ci_fn in _convert_in_early:
                        _ld = getattr(_ci_fn, '_load_data', None)
                        if _ld is not None:
                            _chain_load_data_early = _ld
                            break

                _has_save_pending = (draw_state._show_save
                                     and draw_state._save_pending_obj is not None
                                     and draw_state._save_pending_obj.originated not in auto_apply)
                _has_load_pending = (draw_state._show_load
                                     and draw_state._internal_pending is not None
                                     and draw_state._internal_pending.originated not in auto_apply)

                if _has_save_pending:
                    # Show save dialog with contextual buttons:
                    # - Save: write changes to disk
                    # - Revert: restore to the originally loaded value (what the diff shows)
                    # - Load (when file also changed): reload from disk, discard edits
                    save_result = pending_window(
                        input_value=f"save", return_extras=True, min_width=300,
                        closed=False, tint=draw_state.tint, window_pos=(0, 0), auto_resize=True, wrap=True,
                        button_name="Save", name=f"Save", anchor=Anchor.BOTTOM_LEFT,
                        pending=draw_state._save_pending_obj,
                        show_revert=True, show_load=_has_load_pending,
                        mode=Mode.WINDOW_CLEAN)
                    if save_result[0]:
                        action = save_result[1]
                        if action == PendingAction.REVERT:
                            # Revert: re-parse from _original_load_data (not disk).
                            # Run the convert_in chain with the cached original data
                            # injected directly, bypassing load_data.
                            if _convert_in_early is not None and draw_state._original_load_data is not None:
                                reverted = _run_convert_chain(
                                    value=draw_state._raw_input_value,
                                    chain=_convert_in_early,
                                    data=draw_state._original_load_data,
                                    ref=draw_state._address)
                                if not isinstance(reverted, Pending):
                                    draw_state._input_cache["internal_state"] = reverted, Melty.frame_count
                                    draw_state._input_value = reverted
                            draw_state._show_save = False
                            draw_state._save_pending_obj = None
                            draw_state._all_pending['save_pending'] = None
                            # Clear Background.run cache for convert_out so it
                            # re-runs with the reverted value instead of using
                            # the cached dirty result
                            _out_uid = str(unique) + " | convert_out"
                            # Background._user_cache.pop(_out_uid, None)
                            Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                            Melty.cache.invalidate_up(draw_state._tile_id, force=True)
                            request_render()
                        elif action == PendingAction.LOAD:
                            # Load: reload from disk, discard edits AND pick up external changes
                            if _chain_load_data_early is not None and draw_state._address is not None:
                                fresh_data = _chain_load_data_early(draw_state._address)
                                draw_state._original_load_data = fresh_data
                                draw_state.mark_file_current()
                            draw_state._apply_load = _chain_load_data_early
                            draw_state._show_save = False
                            draw_state._show_load = False
                            draw_state._save_pending_obj = None
                            draw_state._internal_pending = None
                            draw_state._all_pending['save_pending'] = None
                            draw_state._all_pending['load_pending'] = None
                            Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                            Melty.cache.invalidate_up(draw_state._tile_id, force=True)
                            request_render()
                        else:
                            # Save: apply the pending changes
                            draw_state._apply_save = draw_state._save_pending_obj.originated
                            draw_state._pending_convert = True
                            # Clear load pending - save takes precedence
                            draw_state._show_load = False
                            draw_state._internal_pending = None
                            draw_state._all_pending['load_pending'] = None
                            Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                            Melty.cache.invalidate_up(draw_state._tile_id, force=True)
                            request_render()

                elif _has_load_pending:
                    # Show load dialog only when there's no competing save pending
                    if pending_window(input_value=f"load",
                                      closed=False, window_pos=(0, 0), auto_resize=True,
                                      pending=draw_state._internal_pending, min_width=300,
                                      button_name="Load", name=f"Load", anchor=Anchor.BOTTOM_LEFT, tint=draw_state.tint,
                                      mode=Mode.WINDOW_CLEAN)[0]:
                        draw_state._apply_load = draw_state._internal_pending.originated
                        Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=5, force=True)
                        Melty.cache.invalidate_up(draw_state._tile_id, max_depth=5, force=True)
                        draw_state._pending_convert = True
                        request_render()

                begin_group(unique)

                draw_state.left = snap_int(draw_state.left)
                draw_state.top = snap_int(draw_state.top)
                # expected_type = param_types[0] if len(param_types) > 0 else None
                # --- convert_in / convert_out (explicit function chains) ---
                # Check decorator kwargs first, then runtime kwargs (from Mode).
                # Skip decorator convert_in if already converted by a parent.
                _convert_in = o_kwargs.get("convert_in", None)
                _convert_out = o_kwargs.get("convert_out", None)
                if _convert_in is None:
                    _convert_in = kwargs.get("convert_in", None)
                    # Skip if input is already of target type (parent converted)
                    if _convert_in is not None:
                        last_fn = _convert_in[-1]
                        ret_ann = inspect.signature(last_fn).return_annotation
                        if ret_ann is not inspect.Parameter.empty and isinstance(input_value, ret_ann):
                            _convert_in = None
                if _convert_out is None:
                    _convert_out = kwargs.get("convert_out", None)

                _convert_in_done = False

                # Discover load_data / save_data from converter function chains
                _chain_load_data = None
                _chain_save_data = None
                if _convert_in is not None:
                    for _ci_fn in _convert_in:
                        _ld = getattr(_ci_fn, '_load_data', None)
                        if _ld is not None:
                            _chain_load_data = _ld
                            break
                if _convert_out is not None:
                    for _co_fn in _convert_out:
                        _sd = getattr(_co_fn, '_save_data', None)
                        if _sd is not None:
                            _chain_save_data = _sd
                            break

                thead_launch_frame = 0
                _file_stale = False

                if _convert_in is not None:
                    # Build hash from all converter fn parameters, not just value.
                    # This ensures changes to injected params (like search_text)
                    # are detected as input changes.
                    _hash_parts = [Background.simple_hash(draw_state._raw_input_value)]
                    for _ci_fn in _convert_in:
                        _unwrapped = getattr(_ci_fn, '__wrapped__', _ci_fn)
                        for _p in inspect.signature(_unwrapped).parameters:
                            if _p in ('input_value', 'value', 'data', 'ref', 'draw_state'):
                                continue
                            if _p in kwargs:
                                _hash_parts.append(Background.simple_hash(kwargs[_p]))
                    input_hash = "|".join(_hash_parts)
                    cached_hash = draw_state._input_cache["external_state"][2] if len(
                        draw_state._input_cache["external_state"]) > 2 else None
                    input_changed = input_hash != cached_hash

                    if input_changed:
                        note = Note(name=f"Input changed: {input_value}", draw_state=draw_state,
                                    tint=(0.1, 0.1, 0.4))

                        Melty.cache.invalidate_up(draw_state._tile_id, note=note)
                        Melty.cache.invalidate_up(draw_state._parent._tile_id, note=note)
                        request_render()

                    if draw_state._apply_load is not None or draw_state._pending_convert:
                        input_changed = True

                    # Check file staleness on main thread (cheap stat call).
                    # When the file changes on disk, return Pending (like the
                    # old load wrapper) so the UI shows a load-pending dialog.
                    # Skip on first load (_original_load_data is None) — first
                    # load should always succeed without a Pending.
                    # Resolve Address on first encounter
                    if _chain_load_data is not None and draw_state._address is None:
                        ref = to_address(input_value)
                        if ref is not None:
                            draw_state._address = ref
                            draw_state._original_input_ref = input_value

                    # Check file staleness (skip on first load - no baseline yet).
                    # When stale, return Pending like the old load wrapper.
                    _file_stale = False
                    if (_chain_load_data is not None and not input_changed
                            and draw_state._original_load_data is not None
                            and draw_state.is_file_stale()):
                        _file_stale = True
                        # Refresh address from cache - another view may have
                        # saved with a different line count, so our local
                        # draw_state._address could have a stale count.
                        _fresh_ref = to_address(input_value)
                        if _fresh_ref is not None:
                            draw_state._address = _fresh_ref

                    if (draw_state._raw_input_value == UNSET_VALUE or
                            (draw_state._raw_input_value is None) or
                            (not draw_state.expanded and not manual_expand)):
                        internal_value, thead_launch_frame = draw_state._input_cache["internal_state"]
                    else:
                        start_frame = Melty.frame_count
                        if ((not Melty.on_drag and not imgui.is_mouse_down(2) and not imgui.is_mouse_down(1)) or
                                draw_state._input_cache["internal_state"][0] == UNSET_VALUE
                                and draw_state._save_pending is None):

                            # Gate loading behind apply (like the old load wrapper).
                            # Return Pending on: first load, file stale,
                            # unless apply_load matches the load function.
                            _needs_apply = (_file_stale or draw_state._original_load_data is None)
                            if _needs_apply and draw_state._apply_load != _chain_load_data:
                                internal_value = Pending(
                                    originated=_chain_load_data,
                                    wrapped=draw_state._input_cache["internal_state"][0],
                                    state=PendingState.CONFIRM,
                                    status="file changed on disk" if _file_stale else "load")
                                draw_state._internal_pending = internal_value
                                if not draw_state._show_load:
                                    if Melty.cache is not None:
                                        Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=5, force=True)
                                    request_render()
                                draw_state._show_load = True
                            else:
                                # Bypass cache when input changed (file stale,
                                # apply_load set, or input hash changed).
                                # Normal steady-state frames hit the cache.
                                no_cache = input_changed

                                _fk = {k: v for k, v in kwargs.items()
                                       if k not in ('convert_in', 'convert_out', 'auto_apply',
                                                    'value', 'chain', 'input_value')}
                                _fk["value"] = draw_state._raw_input_value
                                _fk["chain"] = _convert_in
                                # pass known-good address so converters don't
                                # re-resolve via inspect.getsourcelines
                                if draw_state._address is not None:
                                    _fk["ref"] = draw_state._address
                                internal_value = Background.run(
                                    _run_convert_chain,
                                    user_id=str(draw_state.unique) + " | convert_in",
                                    func_kwargs=_fk,
                                    invalidate_id=draw_state._parent._tile_id,
                                    on_frame=start_frame, no_cache=no_cache,
                                    stateful=True)

                            if isinstance(internal_value, tuple):
                                internal_value, thead_launch_frame = internal_value

                            if not isinstance(internal_value, Pending):
                                thead_launch_frame = Melty.frame_count
                                was_apply_load = draw_state._apply_load is not None
                                if was_apply_load:
                                    Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=5, force=True)
                                if input_changed:
                                    request_render()
                                draw_state._apply_load = None
                                draw_state._pending_convert = False
                                draw_state._internal_pending = None
                                draw_state._show_load = False
                                draw_state._all_pending['load_pending'] = None
                                # Run deferred work (e.g. cross-file usage
                                # lookups) on a background thread.
                                _deferred_fn = getattr(internal_value, '_deferred', None)
                                if callable(_deferred_fn):
                                    Background.run(
                                        _deferred_fn,
                                        user_id=str(unique) + " | deferred",
                                        invalidate_id=draw_state._parent._tile_id,
                                        on_frame=Melty.frame_count)
                                    internal_value._deferred = None
                                # Update file stale state + cache original data on main thread
                                if _chain_load_data is not None:
                                    draw_state.mark_file_current()
                                    if draw_state._original_load_data is None or input_changed or _file_stale:
                                        ref = draw_state._address
                                        if ref is not None:
                                            new_data = _chain_load_data(ref)
                                            pass  # debug removed
                                            draw_state._original_load_data = new_data
                            else:
                                if internal_value.state != PendingState.BACKGROUND:
                                    draw_state._all_pending['load_pending'] = internal_value
                        else:
                            internal_value = draw_state._input_cache["internal_state"][0]

                        if isinstance(internal_value, tuple):
                            internal_value, thead_launch_frame = internal_value

                        if not isinstance(internal_value, Pending):
                            if draw_state._apply_load is not None:
                                thead_launch_frame = Melty.frame_count
                                Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                                request_render()

                    if isinstance(internal_value, tuple):
                        internal_value, thead_launch_frame = internal_value

                    if input_changed or draw_state._input_cache["internal_state"][0] == UNSET_VALUE:
                        if isinstance(internal_value, Pending):
                            if internal_value.state == PendingState.CONFIRM:
                                draw_state._internal_pending = internal_value
                                if not draw_state._show_load:
                                    Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=5, force=True)
                                    request_render()
                                draw_state._show_load = True
                                if type(internal_value.wrapped) != type(draw_state._input_cache["internal_state"][0]):
                                    internal_value = draw_state._input_cache["internal_state"][0]
                                else:
                                    internal_value = internal_value.wrapped

                        if isinstance(internal_value, Pending):
                            draw_state._load_pending = True
                            draw_state._load_pending_for += 1
                            if draw_state._load_pending_for > 2:
                                draw_pending_status(draw_state, internal_value)
                        else:
                            draw_state._load_pending = False
                            draw_state._load_pending_for = 0
                            prev_internal_hash = Background.simple_hash(
                                draw_state._input_cache["internal_state"][0])

                            if thead_launch_frame >= draw_state._input_cache["internal_state"][1]:
                                if draw_state._apply_load is not None or draw_state._apply_save is None:
                                    draw_state._input_cache["internal_state"] = internal_value, thead_launch_frame
                                    draw_state._input_value = internal_value
                                new_internal_hash = Background.simple_hash(
                                    draw_state._input_cache["internal_state"][0])

                                if prev_internal_hash != new_internal_hash:
                                    Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
                                    Melty.cache.invalidate(draw_state._tile_id, force=True)
                                    request_render()

                    converted_input = True
                    draw_state._input_value = draw_state._input_cache["internal_state"][0]
                    kwargs["input_value"] = draw_state._input_cache["internal_state"][0]
                    # Sync external cache with composite hash so input_changed
                    # doesn't fire every frame
                    draw_state._input_cache["external_state"] = (
                        input_value, Melty.frame_count, input_hash)
                    draw_state._raw_input_value = input_value
                    _convert_in_done = True

                if not _convert_in_done:
                    draw_state._input_value = input_value
                    kwargs["input_value"] = draw_state._input_value

                # __overrides__: comment-arg render kwargs (`# [tint=(...),
                # bg_offset=5]`), applied here so the values feed show_bg/tint
                # and clean_args. Two sources, parent slot first so a dict
                # child's OWN comment wins:
                #
                # 1. A primitive leaf's comment overrides on its PARENT dict
                #    under __<key>__ (the leaf has no dict of its own to carry
                #    it). Any call that identifies its slot with collection= +
                #    key= gets that slot's overrides - draw_collection children
                #    pass those already, and e.g. the live-view debug window
                #    names its code-dict slot the same way. No view-side
                #    routing: naming the slot IS the routing.
                if isinstance(collection, dict) and "key" in kwargs:
                    _slot_ov = collection.get("__overrides__")
                    if isinstance(_slot_ov, dict):
                        _field_ov = _slot_ov.get(f"__{kwargs['key']}__")
                        if isinstance(_field_ov, dict):
                            for _ok, _ov in _field_ov.items():
                                if not (isinstance(_ok, str) and _ok.startswith("__")):
                                    kwargs[_ok] = _ov

                # 2. A dict value has its own store. Read the finalized
                # kwargs["input_value"] (set above for both the positional and
                # convert_in paths) so nested and converted dicts are covered.
                _ov_collection = kwargs.get("input_value")
                if isinstance(_ov_collection, dict):
                    _overrides = _ov_collection.get("__overrides__")
                    if isinstance(_overrides, dict):
                        for _ok, _ov in _overrides.items():
                            if not (isinstance(_ok, str) and _ok.startswith("__")):
                                kwargs[_ok] = _ov

                highlight = False

                show_bg = kwargs.get("show_bg", False) or (
                        highlight and draw_state.height < 60) or (
                        not draw_state.expanded and not manual_expand)

                # Input_value is indexable. isinstance on the VALUE too: a
                # generic data dict can legitimately carry a 'decorators' KEY
                # (the inputter's jedi completion map maps name->name, so a
                # completion literally named "decorators" put the STRING
                # there - .items() crashed on it).
                # Decorator tint is COLLECTED first but applied below kwargs
                # tint: a tintless decorator (bare @defaults) used to take
                # this branch as an if/elif and swallow the node's comment
                # tint entirely, and in Source Code the `# [tint=...]`
                # comment (fed into kwargs by the __overrides__ merges)
                # outranks @defaults anyway.
                _deco_tint = None
                if isinstance(input_value, dict) and isinstance(
                        input_value.get("decorators"), dict):
                    for decorator_name, decorator_value in input_value["decorators"].items():
                        # A decorator targeting a specific attribute (e.g.
                        # @defaults(attrib="x", tint=...)) carries that child's
                        # overrides - its tint belongs to that child, not the
                        # whole class node - so don't tint the node from it.
                        if isinstance(decorator_value, dict) and (
                                decorator_value.get("attr") or decorator_value.get("attrib")):
                            continue
                        if isinstance(decorator_value, dict) and \
                                isinstance(decorator_value.get("tint"), (tuple, list)) and \
                                len(decorator_value["tint"]) >= 3:
                            _deco_tint = decorator_value["tint"]
                # A tint that is only the @defaults_func decoration's
                # (DECORATION) yields to a parsed-class @defaults tint
                # (AT_DEFAULT_CODE_TYPE, ranked above it) - anything higher
                # already replaced the decoration's object in kwargs.
                if _deco_tint is not None and _tint_is_decoration(kwargs):
                    previous_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*_deco_tint)

                elif "tint" in kwargs and kwargs.get("tint", None) is not None:
                    previous_tint = style_manager.get_tint()
                    new_tint = kwargs.get("tint")
                    if isinstance(new_tint, (tuple, list)):
                        # Forward the full tint, incl. a 4th alpha channel:
                        # set_imgui_tint blends it against the previous tint so the
                        # background bleed accumulates down the tint stack.
                        if len(new_tint) >= 3:
                            style_manager.set_imgui_tint(*new_tint[:4])

                elif _deco_tint is not None:
                    previous_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*_deco_tint)

                elif hasattr(collection, "__tint__") and getattr(collection, "__tint__"):
                    if name in collection.__tint__:
                        previous_tint = style_manager.get_tint()
                        style_manager.set_imgui_tint(*collection.__tint__[name])

                elif draw_state.tint is not None and kwargs.get("show_bg", False) and kwargs.get("show_tint",
                                                                                                 False):
                    previous_tint = style_manager.get_tint()
                    style_manager.set_imgui_tint(*draw_state.tint)

                if previous_tint is not None:
                    kwargs["tint"] = style_manager.get_tint()

                nested_bg = not closable and kwargs.get("bg_offset", 0) >= 0
                from src.lsd.gl_gui.view.core_views.new_core_view import compute_bg_color
                # max_bg_depth pins the ceiling on the effective depth
                # (Melty.bg_depth + bg_offset) the palette is read at, so a
                # deeply nested view stops getting darker past that step;
                # max_bg_value caps the color that ramp resolves at.
                draw_state.bg_color = compute_bg_color(bg_offset=kwargs.get("bg_offset", None), nested_bg=True,
                                                       max_bg_depth=kwargs.get("max_bg_depth", None),
                                                       max_bg_value=kwargs.get("max_bg_value", None))

                if converted_input:
                    # reformat icon wrench
                    converted_icon_text = f"\uf0ad"
                    overlay_list: _DrawList = imgui.get_window_draw_list()
                    overlay_list.add_text(*(draw_state.left + draw_state.header_width + 5, draw_state.top + 5),
                                          pack_color(0.5, 0.0, 0.0, 1.0),
                                          f"{converted_icon_text}")

                if show_bg:
                    if Melty.channels_split:
                        offscreen_depth = Melty.get_channel()
                        draw_list = imgui.get_window_draw_list()
                        draw_list.channels_set_current(
                            max(0, min(offscreen_depth + passed_z_offset - 2,
                                       Melty.max_depth - 1)))

                    # Resolved through the kwargs gauntlet (caller / parent /
                    # auto_params / type defaults), so any show_bg view can be
                    # rounded with corner_radius=...; 5.0 is the legacy stamp.
                    # Stamped onto the draw_state so framework painters (melty
                    # highlights, blurr mask) read this view's effective radius.
                    draw_state.corner_radius = kwargs.get("corner_radius", 5.0)
                    from src.lsd.gl_gui.view.core_views.new_core_view import draw_bg
                    style_manager = Melty.global_attrs['style_manager']

                    bg_color = (0, 0, 0, 0)
                    if draw_state.width > 5 and draw_state.height > 5:
                        nested_bg = not closable and kwargs.get("bg_offset", 0) >= 0
                        if getattr(draw_state, "freeze_resize", False):
                            # Blit owns ALL bg rendering for freeze_resize
                            # views: the same draw_freeze_bg runs here (live,
                            # capturing the color globals), and during frozen
                            # mid-drag frames (reusing them), so the two views
                            # never drift apart. The view itself draws no bg.
                            bg_return = Melty.cache.draw_freeze_bg(
                                draw_state, draw_state.abs_left, draw_state.abs_top,
                                draw_state.width, draw_state.height, live=True)
                        else:
                            # bg_outline=False: no outer stroke, the fill IS the
                            # view's edge (the frameless OS window's root: its
                            # rounded fill, mask rim and corner alpha cut share
                            # one geometry, in draw_melty_windows).
                            bg_return = draw_bg(bypass=True, left=draw_state.abs_left, top=draw_state.abs_top,
                                                width=draw_state.width, height=draw_state.height,
                                                rounding=draw_state.corner_radius, bg_offset=kwargs.get("bg_offset", 0),
                                                outline=kwargs.get("bg_outline", True),
                                                max_bg_depth=kwargs.get("max_bg_depth", None),
                                                max_bg_value=kwargs.get("max_bg_value", None),
                                                depth=Melty.shadow_depth, selected=False,
                                                opacity=1.0 if show_bg else 0.0,
                                                saturation=kwargs.get("saturation", 1.0),
                                                pressed=False,
                                                style_manager=style_manager, nested_bg=nested_bg)
                        # draw_bg paints into this view's tile rather than owning
                        # one, so register it against this view's key for invalidate_by_func.
                        Melty.cache.register_func_key(draw_bg, draw_state._tile_id)
                        if bg_return is not None:
                            bg_color = bg_return[1]


                    Melty.bg_color_stack.append(bg_color)


                ########################

                if Melty.channels_split:
                    offscreen_depth = Melty.get_channel()
                    draw_list = imgui.get_window_draw_list()
                    draw_list.channels_set_current(
                        max(0, min(offscreen_depth + passed_z_offset + ds_z_offset, Melty.max_depth - 1)))

                if kwargs.get("selectable", True):
                    left_mouse_down_press = draw_state.on_action("left_mouse_held", "press", priority_delta=2)
                    draw_state.pressed = True if left_mouse_down_press else False
                    # click = draw_state.on_action("left_mouse_click", priority_delta=2)
                    click=False
                    middle_down = draw_state.on_action("non_blocking_middle_mouse_down", priority_delta=0)

                    if middle_down:
                        Melty.selected = set()
                        Melty.selected.add(draw_state)
                        Melty.last_selected = draw_state
                        Melty.clear_focus(not_this=draw_state)

                    elif click and not Melty.imgui_active:
                        Melty.clear_focus(not_this=draw_state)
                        Melty.previous_select = copy(Melty.selected)
                        if not click.modifiers:
                            if len(Melty.selected) == 1 and draw_state in Melty.selected:
                                Melty.selected.remove(draw_state)
                            else:
                                Melty.selected = set()
                                Melty.selected.add(draw_state)
                                Melty.last_selected = draw_state
                        elif click.modifiers == glfw.MOD_CONTROL:
                            if draw_state in Melty.selected:
                                Melty.selected.remove(draw_state)
                            else:
                                Melty.selected.add(draw_state)

                        elif click.modifiers == glfw.MOD_SHIFT:
                            if draw_state in Melty.selected:
                                Melty.selected.remove(draw_state)
                                adding = False
                            else:
                                Melty.selected.add(draw_state)
                                adding = True

                            if Melty.last_selected is not None:
                                # Check if both have the same parent
                                it_count = 0
                                max_iter = 1000
                                seen = set()
                                items_to_select = []

                                ds_index = draw_state.index_in_parent
                                last_index = Melty.last_selected.index_in_parent
                                go_back = ds_index > last_index
                                if go_back:
                                    next_ds = draw_state.previous
                                else:
                                    next_ds = draw_state.next

                                while (id(next_ds) not in seen and
                                       id(next_ds) != id(Melty.last_selected) and
                                       next_ds is not None
                                       and it_count < max_iter):
                                    seen.add(id(next_ds))
                                    items_to_select.append(next_ds)

                                    if go_back:
                                        next_ds = next_ds.previous
                                    else:
                                        next_ds = next_ds.next
                                    it_count += 1

                                if id(next_ds) == id(Melty.last_selected):
                                    for item in items_to_select:
                                        if adding:
                                            if item not in Melty.selected:
                                                Melty.selected.add(item)
                                        else:
                                            if item in Melty.selected:
                                                Melty.selected.remove(item)

                                        # Melty.cache.invalidate(item._tile_id)

                            Melty.last_selected = draw_state

                    draw_state.selected = draw_state in Melty.selected

                ########### CONTEXT MENU HANDLING ############
                from src.lsd.gl_gui.view.core_views.new_core_view import (
                    draw_context_menu, draw_context_menu_items, INSPECT)
                # context_menu={label: callable}: a right-click opens the labels as
                # a dropdown menu at the origin (draw_context_menu_items) instead
                # of the inspector; an Inspect row opens the inspector. No dict:
                # the right-click opens the inspector directly, as it always has.
                menu_items = kwargs.get("context_menu")
                # Gate on the occlusion-aware bounding hover so a right-click
                # only opens the topmost view's menu - not views sitting
                # behind a closable window under the cursor. (on_action's
                # priority resolution could pick the topmost subscriber, but
                # this guards the case where the front window isn't itself a
                # right-click subscriber and so doesn't consume the event.)
                is_root_view = draw_state.parent_window is None and not draw_state.closable

                # A root view gets no inspector on right-click, but an explicit
                # context_menu= dict is an opt-in: the hdr-viewer draws
                # draw_texture straight into its root window and wants its
                # menu there (09-10).
                if not is_root_view or menu_items is not None:
                    # The menu opens on the right button's event, judged a click
                    # by the travel the UP event covers - not on the double_clicked:
                    # CLICKED is held for the double-click window (250 ms) whenever
                    # a double subscriber is hovered, and the corner double
                    # right-drag registers it on every window, so the menu
                    # always came up a quarter second late (09-10).
                    from src.lsd.gl_gui.events.input_handler import CLICK_MAX_DISTANCE
                    release = draw_state.on_action("right_mouse_up")
                    right_click = (release is not None and draw_state._bounding_hovered
                                   and (release.total_dx ** 2 + release.total_dy ** 2)
                                   <= CLICK_MAX_DISTANCE ** 2)
                    open_inspector = False
                    if menu_items is not None:
                        picked = draw_context_menu_items(draw_state, menu_items, right_click, name, unique)
                        open_inspector = picked is INSPECT
                        right_click = False   # the items menu took it; Inspect is the inspector's way in
                    if right_click or open_inspector:
                        draw_state.context_menu_open = True if open_inspector else not draw_state.context_menu_open
                        if draw_state.context_menu_ds is not None:
                            draw_state.context_menu_ds.closed = not draw_state.context_menu_open
                            if draw_state.context_menu_open:
                                # Reopen at the pin anchor. window_pos is
                                # also where a drag of the menu ended, and
                                # the draw_state outlives the open, so a
                                # stale value would otherwise reopen the
                                # menu that far from its spawner.
                                draw_state.context_menu_ds.window_pos = (0, 0)
                    if draw_state.context_menu_open:
                        # (Call-site frames are captured in the inline pass - see the
                        # `active_layer is None` block - not here in the full-render
                        # pass, where the frame is Melty.draw's re-dispatch.)
                        tint = style_manager.get_tint()

                        mixed_color = style_manager.make_color_rgb(tint[0], tint[1], tint[2],
                                                                   value=0.03, factor=0.2,
                                                                   saturation_scale=0.5,
                                                                   alpha=1.0)
                        returned_val = draw_context_menu(input_value=draw_state, mode=Mode.WINDOW_NO_HEADER, func=func,
                                                         tint=mixed_color, show_tint=False, show_add_delete=False,
                                                         min_width=100, min_height=100, pin_to_clip=Pin.PARENT,
                                                         persistent=False, anchor=Anchor.TOP_LEFT, parent_anchor=Anchor.TOP_RIGHT,
                                                         bg_offset=Tint.context_menu_bg_offset, swoosh_mode=SwooshMode.LINE,
                                                         with_footer=None, use_cache=True,
                                                         name=f"{name}##context_menu_{unique}", auto_resize=False,
                                                         return_extras=True)

                        ctx_ds = returned_val[2]
                        ctx_ds.tint = tint
                        draw_state.context_menu_ds = ctx_ds
                        if Melty.frame_count > 2:
                            if ctx_ds.last_seen is None:
                                ctx_ds.closed = False
                                # No scroll compensation here: the pin
                                # anchors to the target's raw box top, which
                                # sits far above the visible clip when the
                                # view is scrolled, but DrawState._pinned_base_y
                                # now bounds that anchor to the parent window
                                # so the menu stays adjacent to it. Adding
                                # (clipped_top - abs_top) on top of that bound
                                # over-compensated and opened the menu a
                                # scroll-height BELOW its spawner.
                                ctx_ds.window_pos = (0, 0)

                        if ctx_ds.closed:
                            draw_state.context_menu_open = False

                if kwargs.get("show_bg", False):
                    outline_margin = 3
                else:
                    outline_margin = 0

                header_start = imgui.get_cursor_screen_pos()

                #
                # if "with_footer" in kwargs and kwargs.get("with_footer", None) is not None:
                #     imgui.set_cursor_screen_pos(
                #         (draw_state.left, draw_state.top + draw_state.height - draw_state.footer_height))
                #     from src.lsd.gl_gui.view.core_views.new_core_view import empty

                # if "with_footer" in kwargs and kwargs.get("with_footer", None) is not None:
                #     if closable and draw_state.expanded:
                #         imgui.set_cursor_screen_pos(
                #             (draw_state.abs_left, draw_state.abs_clip_rect[3] - draw_state.footer_height))
                #
                #         RenderFuncs.empty(name=f"empty_shadow{unique}", z_offset=0,
                #               tile_mode=TileMode.MAX, width=draw_state.width - 2,
                #               height=draw_state.footer_height)
                #     imgui.set_cursor_screen_pos(header_start)


                # if draw_state.closable:
                #
                #     imgui.set_cursor_screen_pos(header_start)
                #     from src.lsd.gl_gui.view.core_views.new_core_view import empty
                #     empty(input_value=input_value, name=f"shadow{unique}", shadow=True, width=draw_state.width - 2,
                #           show_bg=False, height=draw_state.header_height + 2, z_offset=-2)
                if "with_header" in kwargs and kwargs.get("with_header", None) is not None and kwargs.get("show_header",
                                                                                                          True):
                    next_kwargs = kwargs.get('next_kwargs', {})
                    next_kwargs['func'] = func
                    next_kwargs['outer_func'] = wrapper
                    next_kwargs['show_bg'] = kwargs.get("show_bg", True)
                    draw_header = kwargs.get("with_header", None)
                    imgui.begin_group()
                    header_start_cursor = imgui.get_cursor_screen_pos()

                    # header_indent: px the header starts to the right of the
                    # view's left edge, past chrome that owns that space (an
                    # OS window's left-side controls - surface.root_view_kwargs
                    # ← titlebar.chrome_insets). Nothing else reads it.
                    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0] + outline_margin
                                                 + float(kwargs.get("header_indent", 0.0) or 0.0),
                                                 imgui.get_cursor_screen_pos()[1] + outline_margin))

                    # Clip the main header so it doesn't draw over the end header.
                    # The end header is right-aligned to the window edge, so its left
                    # edge sits at clip_size[0] - header_end_width (header_end_width is
                    # the prior frame's measurement but good enough to clip against).
                    # Only when expanded: collapsed views place the end header right
                    # after the header, so there's no overlap to clip.
                    _hdr_clip = draw_state.clip_size
                    if _hdr_clip is None and (not auto_resize or closable):
                        _hdr_clip = (draw_state.width, draw_state.height)
                    _hdr_clipped = False
                    if _hdr_clip is not None and draw_state.expanded and draw_state.header_end_width:
                        _hdr_right = draw_state.abs_left + _hdr_clip[0] - draw_state.header_end_width
                        Melty.push_clip((draw_state.abs_left, draw_state.abs_top,
                                         _hdr_right, draw_state.abs_top + draw_state.height))
                        _hdr_clipped = True

                    _header_ret = draw_header(**kwargs)
                    if _hdr_clipped:
                        Melty.pop_clip()
                    if isinstance(_header_ret, tuple) and len(_header_ret) >= 1 and _header_ret[0]:
                        header_changed = True
                        if len(_header_ret) >= 2:
                            header_return = _header_ret[1]

                    imgui.set_cursor_screen_pos(header_start_cursor)
                    end_group()
                    if imgui.is_item_active() or imgui.is_item_activated():
                        Melty.report_imgui_active()
                    draw_state.header_left = header_start_cursor[0]
                    draw_state.header_top = header_start_cursor[1]
                    header_rect = imgui.get_item_rect_size()
                    draw_state.header_width = header_rect[0]
                    draw_state.header_height = header_rect[1]

                    # Fold this header's natural width into the parent window's
                    # running max so sibling headers align to the widest one.
                    if draw_state.parent_window is not None and not draw_state.multi_line:
                        draw_state.parent_window.max_header_width = min(Toggles.Collection.max_preferred_header_width, max(
                            draw_state.parent_window.max_header_width,
                            draw_state.header_natural_width))

                    if not draw_state.multi_line:
                        same_line(spacing=0.0)

                else:
                    draw_state.header_left = draw_state.left
                    draw_state.header_top = draw_state.top
                    draw_state.header_width = 0
                    draw_state.header_height = 0
                    imgui.begin_group()
                    end_group()

                if "with_header_end" in kwargs and kwargs.get("with_header_end", None) is not None and kwargs.get(
                        "show_header",
                        True):
                    next_kwargs = kwargs.get('next_kwargs', {})
                    next_kwargs['func'] = func
                    next_kwargs['outer_func'] = wrapper
                    next_kwargs['show_bg'] = kwargs.get("show_bg", True)
                    draw_header_end = kwargs.get("with_header_end", None)

                    clip_size = draw_state.clip_size
                    imgui.same_line()

                    if clip_size is None and (not auto_resize or closable):
                        clip_size = (draw_state.width, draw_state.height)
                    current_cursor = imgui.get_cursor_screen_pos()
                    if closable:
                        margin = 0
                    else:
                        margin = 0

                    if clip_size is not None:
                        # Right-align the end header to the window's right edge. We
                        # deliberately don't clamp it past the main header (i.e. no
                        # `max(end_x, abs_left + header_width + 10)`): when space is
                        # tight the end header overlaps/draws on top of the header
                        # rather than clipping off the right edge of the window.
                        end_x = max(draw_state.abs_left,
                                    draw_state.abs_left + clip_size[0] - draw_state.header_end_width - margin)
                        # Collapsed windows shrink to the header, so the close
                        # button trails the header. Collection rows keep the
                        # right-aligned alignment even when collapsed because their clip
                        # still spans the parent's content width.
                        if not draw_state.expanded and closable:
                            end_x = draw_state.abs_left + draw_state.header_width + 50

                        imgui.set_cursor_screen_pos((end_x,
                                                     imgui.get_cursor_screen_pos()[1] + outline_margin))

                    # if Melty.channels_split:
                    #     draw_list = imgui.get_window_draw_list()
                    #     draw_list.channels_set_current(Melty.get_channel() + kwargs.get("channel_offset", 0))

                    imgui.begin_group()
                    start_c = imgui.get_cursor_screen_pos()
                    draw_header_end(**kwargs)
                    imgui.set_cursor_screen_pos(start_c)
                    imgui.same_line(spacing=0)
                    imgui.dummy(outline_margin, 0)
                    end_group()
                    if imgui.is_item_active() or imgui.is_item_activated():
                        Melty.report_imgui_active()

                    end_header_rect = imgui.get_item_rect_size()
                    draw_state.header_end_width = end_header_rect[0]

                    if not draw_state.multi_line:
                        imgui.same_line()
                        imgui.set_cursor_screen_pos(current_cursor)

                else:
                    draw_state.header_end_width = 0

                # header_end_cursor = imgui.get_cursor_screen_pos()
                # if draw_state.closable:
                #     imgui.set_cursor_screen_pos(header_start)
                #     from src.lsd.gl_gui.view.core_views.new_core_view import empty
                #     empty(input_value=input_value, name=f"empty_{unique}", width=draw_state.width,
                #           show_bg=False, height=draw_state.header_height + 2, z_offset=-1)
                #     imgui.set_cursor_screen_pos(header_end_cursor)
                ####################################################################################
                #### with callback header

                if draw_state.header_left is not None and draw_state.left is not None:
                    draw_state.header_left_delta = draw_state.left - draw_state.header_left
                    draw_state.header_top_delta = draw_state.top - draw_state.header_top

                if indent_x > 0:
                    sc = imgui.get_cursor_screen_pos()
                    imgui.set_cursor_screen_pos((sc[0] + indent_x,
                                                 sc[1]))

                imgui.begin_group()

                is_hovered = draw_state.on_action("cursor_hover", view_id="hover", priority_delta=0) is not None
                draw_state._hovered = is_hovered

                if draw_state._bounding_hovered:
                    draw_state._parent.child_selected = draw_state

                if draw_state.child_selected is not None:
                    if kwargs.get("show_mouse_over", False):
                        selected = draw_state.child_selected
                        draw_list: _DrawList = imgui.get_overlay_draw_list()

                        if selected.parent_window is None or selected.parent_window._bounding_hovered:
                            if Melty.channels_split:
                                draw_list.channels_set_current(draw_state.window_index + 10)
                            parent_tint = draw_state.current_tint or (draw_state._kwargs.get("tint", (1, 1, 1))[:3], 1.0)
                            highlight_rgb = (1, 0, 0)
                            bg_col = pack_color(*highlight_rgb, Tint.highlight_bg_alpha)

                            # Parent view: faint fill + matching tint outline,
                            # clipped to the parent's own clip rect so the highlight
                            # doesn't bleed past where the parent is scrolled/clipped.

                            clip_rect = selected.abs_clip_rect
                            draw_list.push_clip_rect(*clip_rect)
                            draw_list.add_rect_filled(selected.abs_left, selected.abs_top,
                                                      selected.abs_left + selected.width,
                                                      selected.abs_top + selected.height,
                                                      bg_col, rounding=getattr(selected, 'corner_radius', 6))
                            draw_list.pop_clip_rect()


                if Melty.inside_clip(draw_state=draw_state):
                    hover_eligible = draw_state.hover_eligible(rect=draw_state.get_content_rect()) and draw_state.hover_reported
                else:
                    hover_eligible = False

                if hover_eligible:
                    if closable:
                        Melty.any_window_hovered_pending = True
                    max_layer_depth = Melty.max_depth * Melty.max_depth + Melty.max_depth
                    priority = max_layer_depth - draw_state.z_pos
                    event_names = copy(wanted_params)

                    # Remove event names from wanted params that aren't in kwargs
                    event_names = [e for e in event_names if e in kwargs]
                    if _event_rects:
                        # A param the body scoped with event_rect registers only
                        # while the pointer is within one of its rects (stored
                        # relative to the view creation to re-anchor to the LIVE
                        # window, as replay_body_actions needs); elsewhere it is
                        # left unsubscribed so the click/drag falls through to
                        # the view behind (the window's move button, ...).
                        _er_left, _er_top = draw_state._abs_left(), draw_state._abs_top()
                        event_names = [
                            e for e in event_names
                            if e not in _event_rects
                            or any(draw_state.hover_eligible((_er_left + r[0], _er_top + r[1],
                                                              _er_left + r[2], _er_top + r[3]))
                                   for r in _event_rects[e])]
                    _content_cursor = kwargs.get("mouse_cursor")
                    Melty.event_handler.register_hovered(tile_id, event_names, priority - 3, tile_id,
                                                         selected=draw_state.selected,
                                                         blocker=closable,
                                                         cursor=_content_cursor,
                                                         cursor_rect=(draw_state.get_content_rect()
                                                                      if _content_cursor is not None else None))

                ###########################################################
                kwargs['next_kwargs'] = kwargs
                if "height" in wanted_params and "height" not in kwargs:
                    kwargs["height"] = draw_state.height
                if "width" in wanted_params and "width" not in kwargs:
                    kwargs["width"] = draw_state.width

                if 'kwargs' in wanted_params:
                    clean_args = kwargs
                else:
                    clean_args = {k: kwargs[k] for k in wanted_params if k in kwargs}
                ###########################################################
                draw_state.channel = Melty.get_channel()

                if not auto_resize:
                    draw_list = imgui.get_window_draw_list()
                    if Melty.channels_split:
                        draw_list.channels_set_current((Melty.max_depth - 1))

                    if passed_height is None or passed_width is None:
                        draw_resize_handle(draw_state)
                    if Melty.channels_split:
                        draw_list.channels_set_current(Melty.get_channel() + kwargs.get("channel_offset", 0))

                ##### Register With event handler #########################

                ##########################################################

                if hasattr(input_value, 'pending_upload') and callable(getattr(input_value, 'pending_upload')):
                    try:
                        pending = input_value.pending_upload()
                        # request_render()
                    except Exception as e:
                        print(f"Error checking pending upload: {e}")

                ############# HANDLE SELECTION
                top = draw_state.top
                left = draw_state.left
                width = draw_state.width
                height = draw_state.height

                top = snap_int(top)
                left = snap_int(left)
                width = snap_int(width)
                height = snap_int(height)

                #### MAIN CALL #######################
                Melty.push_clip((left, top,
                                 left + width,
                                 top + height - draw_state.footer_height))

                # if (draw_state.left is not None and draw_state.top is not None and
                #     draw_state.width is not None and draw_state.height is not None) and closable:
                #     if (draw_state.width > 0 and draw_state.height > 0):
                #         reset_to = imgui.get_cursor_screen_pos()
                #
                #         # imgui.invisible_button(str(unique) + "window_blocker", width=draw_state.width,
                #         #                        height=draw_state.height)
                #         imgui.set_cursor_screen_pos(reset_to)
                #         imgui.set_item_allow_overlap()

                if show_bg:
                    Melty.bg_depth += 1
                    Melty.bg_stack.append(style_manager.get_tint())

                Melty.bg_depth += kwargs.get("bg_offset", 0)

                if not closable and show_bg:
                    imgui.dummy(outline_margin / 2, outline_margin / 2)

                # Snapshot the focused input's caret/selection BEFORE the body
                # runs, so an edit this frame records the pre-edit UI state. Only
                # the focused text draw_state can produce a text edit, so this is
                # one cheap copy per frame rather than one per draw_state.
                if Melty.text_focused_ds is draw_state:
                    draw_state._undo_pre = draw_state.capture_undo_state()

                _wt1 = time.perf_counter()
                # TEMP perf: exclusive view stack entry; popped in the return
                # block below, where this call's total minus its nested
                # wrapper calls is credited to this wrapper (see `frame views`).
                _ves = getattr(Melty, "_view_excl_stack", None)
                if _ves is not None:
                    _ves.append(0.0)
                    _ves_pushed = True
                return_value = draw_inner_main(clean_args, draw_state,
                                               input_value, unique, kwargs)
                _wt2 = time.perf_counter()
                # TEMP perf: the wrapper split is logged at the RETURN block
                # below (with epi_ms), so the epilogue - footer/content-rect,
                # changed handling, convert_out, dnd - is attributed too; the
                # measured draw and edited-frame gap (call ≫ body) lived
                # exactly there, invisible to a split that stopped here.

                if show_bg and show_bg:
                    Melty.bg_depth -= 1
                    Melty.bg_stack.pop()
                    Melty.bg_color_stack.pop()

                Melty.bg_depth -= kwargs.get("bg_offset", 0)

                draw_state._imgui_is_hovered = draw_state._imgui_is_item_hovered and is_hovered

                if imgui.is_item_active() or imgui.is_item_activated():
                    Melty.report_imgui_active()
                #######################


                end_group()
                Melty.pop_clip()

                content_rect = imgui.get_item_rect_size()
                #
                if draw_state.final_max_column > 0:
                    max_height = 0
                    max_column_index = 0
                    for i in range(draw_state.final_max_column + 1):
                        column_height = draw_state._column_cursor[i][1]
                        if column_height > max_height:
                            max_height = column_height
                            max_column_index = i
                    draw_state._max_column_height = max_height
                    draw_state._max_column_index = max_column_index

                    imgui.set_cursor_screen_pos((draw_state.abs_left + draw_state._column_width * max_column_index,
                                                    draw_state.abs_top + draw_state._header_height + max_height))
                    # draw_state._content_rect = (draw_state._column_width, content_rect[1])
                draw_state._content_rect = content_rect
                if ("with_footer" in kwargs and kwargs.get("with_footer", None) is not None and
                        draw_state.expanded):

                    next_kwargs = kwargs.get('next_kwargs', {})
                    next_kwargs['func'] = func
                    next_kwargs['outer_func'] = wrapper
                    next_kwargs['show_bg'] = kwargs.get("show_bg", True)
                    draw_footer = kwargs.get("with_footer", None)

                    current_cursor = imgui.get_cursor_screen_pos()
                    imgui.set_cursor_screen_pos((current_cursor[0] + outline_margin,
                                                 min(draw_state.abs_top + draw_state.height - draw_state.footer_height,
                                                     draw_state.abs_top + draw_state.observed_content_height)))

                    push_id(str(unique) + "footer")
                    imgui.begin_group()

                    foot_start = imgui.get_cursor_screen_pos()

                    draw_footer(**kwargs)
                    imgui.set_cursor_screen_pos(foot_start)

                    if imgui.is_item_active() or imgui.is_item_activated():
                        Melty.report_imgui_active()
                    push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))


                    end_group()
                    pop_style_var(2)
                    footer_rect = imgui.get_item_rect_size()
                    draw_state.footer_height = footer_rect[1]
                    draw_state.footer_width = footer_rect[0]
                    pop_id()

                else:
                    draw_state.footer_height = 0
                    draw_state.footer_width = 0

                if not kwargs.get("imgui_padding", True):
                    push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
                    end_group()
                    pop_style_var(2)
                else:
                    push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
                    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
                    end_group()
                    pop_style_var(2)

                if previous_tint is not None:
                    style_manager.set_imgui_tint(*previous_tint)



            draw_state._imgui_is_edited = imgui.is_item_edited()
            draw_state._imgui_is_activated = imgui.is_item_activated()
            draw_state._imgui_is_active = imgui.is_item_active()
            draw_state._imgui_is_focused = imgui.is_item_focused()
            draw_state._imgui_is_item_hovered = imgui.is_item_hovered()
            item_rect = imgui.get_item_rect_size()
            if passed_height is not None:
                item_rect = (item_rect[0], passed_height)
            if passed_width is not None:
                item_rect = (passed_width, item_rect[1])
            if return_value is not None and len(return_value) >= 2:
                child_changed = return_value[0] or header_changed
                new_value_child = return_value[1]
                if header_changed and not return_value[0] and header_return is not None:
                    new_value_child = header_return
                report_value = new_value_child
                report_changed = False

                if draw_state._apply_save:
                    converted_input = True
                thead_launch_frame = 0
                if (converted_input or draw_state._apply_save) and not draw_state._read_only:
                    # Clear ERROR pending if the user edits the code -
                    # the new code might be valid, so allow auto_apply to retry.
                    if child_changed:
                        _cur_sp = draw_state._all_pending.get("save_pending")
                        if isinstance(_cur_sp, Pending) and _cur_sp.state == PendingState.ERROR:
                            # print(f"Clearing ERROR pending for {draw_state.name} since user edited the value. Error pending status: {_cur_sp.status}")
                            draw_state._all_pending["save_pending"] = None
                    if child_changed:
                        # # draw_state._input_value_cache["external_state"] = ...
                        # if isinstance(new_value_child, Pending):
                        #     raise Exception("Pending needs to be handled before saving to cache")
                        draw_state._input_cache["internal_state"] = new_value_child, Melty.frame_count + 1
                        # Melty.cache.invalidate(draw_state._parent._tile_id, force=True)
                        # Melty.cache.invalidate(draw_state._tile_id, force=True)
                        # request_reload()j
                    else:
                        new_value_child = draw_state._input_cache["internal_state"][0]

                    if _convert_in_done and _convert_out is not None and (not _file_stale or child_changed):
                        ####################################### CONVERT_OUT SAVE HANDLER
                        # Bypass cache when data changes (user edit, reload, or save apply)
                        no_cache = child_changed or input_changed or draw_state._apply_save is not None

                        _fk_out = {k: v for k, v in kwargs.items()
                                   if k not in ('convert_in', 'convert_out', 'auto_apply',
                                                'value', 'chain', 'input_value')}
                        _fk_out["value"] = new_value_child
                        _fk_out["chain"] = _convert_out
                        external_value = Background.run(
                            _run_convert_chain,
                            user_id=str(unique) + " | convert_out",
                            func_kwargs=_fk_out,
                            invalidate_id=draw_state._parent._tile_id,
                            on_frame=Melty.frame_count, no_cache=no_cache,
                            stateful=True)

                        if isinstance(external_value, tuple):
                            external_value, thead_launch_frame = external_value

                        if not isinstance(external_value, Pending):
                            # Dirty detection against original load data
                            if _chain_save_data is not None and draw_state._original_load_data is not None:
                                is_dirty = (external_value != draw_state._original_load_data)
                                if is_dirty:
                                    pass  # debug removed

                                if is_dirty and draw_state._apply_save is not None:
                                    ref = draw_state._address
                                    try:
                                        _orig_ref = draw_state._original_input_ref
                                        # Forward any extra kwargs the save_data
                                        # function accepts (e.g. hotswap_instances
                                        # injected by Mode).
                                        _save_extra = {}
                                        _save_inner = getattr(_chain_save_data, '__wrapped__', _chain_save_data)
                                        for _sp in inspect.signature(_save_inner).parameters:
                                            if _sp in kwargs and _sp not in (
                                                    'input_value', 'ref', 'function_ref',
                                                    'module_ref', 'class_ref'):
                                                _save_extra[_sp] = kwargs[_sp]
                                        save_result = _chain_save_data(
                                            external_value,
                                            _converter_mode=True,
                                            ref=ref,
                                            function_ref=_orig_ref,
                                            module_ref=_orig_ref,
                                            class_ref=_orig_ref,
                                            **_save_extra)
                                        # save_data render_func returns (pending, value)
                                        if isinstance(save_result, tuple) and len(save_result) == 2:
                                            save_pending, updated_ref = save_result
                                        else:
                                            save_pending, updated_ref = None, save_result
                                        if isinstance(save_pending, Pending):
                                            draw_state._all_pending['save_pending'] = save_pending
                                            draw_state._show_save = True
                                            report_changed = False
                                        else:
                                            if isinstance(updated_ref, Address):
                                                draw_state._address = updated_ref
                                            draw_state._original_load_data = external_value
                                            draw_state.mark_file_current()
                                            draw_state._show_save = False
                                            draw_state._apply_save = None
                                            draw_state._save_pending_obj = None
                                            draw_state._all_pending['save_pending'] = None
                                            report_changed = True
                                            report_value = draw_state._original_input_ref or input_value
                                            # Update the cache so input_changed doesn't
                                            # fire every frame after a successful save
                                            draw_state._input_cache["external_state"] = (
                                                report_value, Melty.frame_count,
                                                Background.simple_hash(report_value))
                                            # if Melty.cache is not None:
                                            #     Melty.cache.invalidate_up(draw_state._parent._tile_id, max_depth=4,
                                            #                               force=True)
                                            request_render()
                                    except Exception as e:
                                        draw_state._all_pending['save_pending'] = Pending(
                                            originated=_chain_save_data, status=str(e),
                                            state=PendingState.ERROR)
                                        report_changed = False
                                elif is_dirty:
                                    try:
                                        lines1 = str(draw_state._original_load_data).splitlines(keepends=True)
                                        lines2 = str(external_value).splitlines(keepends=True)
                                        print("+!!!!!!!!!!!!!!!!!!!!!!! SLOW")
                                        diff = "".join(difflib.unified_diff(
                                            lines1, lines2,
                                            fromfile="original.py", tofile="modified.py"))
                                    except Exception:
                                        diff = "dirty"
                                    save_pending = Pending(
                                        originated=_chain_save_data, wrapped=external_value,
                                        status=diff)
                                    draw_state._all_pending['save_pending'] = save_pending
                                    draw_state._save_pending_obj = save_pending
                                    # if not draw_state._show_save:
                                    #     Melty.cache.invalidate_up(draw_state._parent._tile_id, force=True)
                                    #     request_render()
                                    draw_state._show_save = True
                                    report_changed = False
                                else:
                                    draw_state._show_save = False
                                    draw_state._all_pending['save_pending'] = None
                                    report_changed = False
                            else:
                                report_changed = child_changed
                                report_value = external_value

                            if draw_state._show_save or draw_state._apply_save is not None:
                                draw_state._apply_save = None
                        else:
                            if external_value.state == PendingState.ERROR:
                                draw_state._apply_save = None
                                draw_state._save_pending_obj = None
                                draw_state._show_save = False
                            if external_value.state != PendingState.BACKGROUND:
                                _cur_sp = draw_state._all_pending.get("save_pending")
                                if not (isinstance(_cur_sp, Pending) and _cur_sp.state == PendingState.ERROR
                                        and external_value.state != PendingState.ERROR):
                                    draw_state._all_pending["save_pending"] = external_value
                            report_changed = False

                        return_value = (report_changed, report_value, *return_value[2:])

                else:
                    # Synchronous

                    report_changed = child_changed
                    report_value = new_value_child

                # if isinstance(report_value, Pending):
                #     raise Exception("Pending needs to be handled before saving to cache")
                return_value = (report_changed, report_value, *return_value[2:])

                if auto_resize:
                    # Post-pending guard: while ANY view rendered a pending
                    # placeholder this frame, refuse to SHRINK a persisted
                    # content_height - the collapsed measure reflects the
                    # placeholder, not the content, and committing it wipes the
                    # restored geometry (the view then visibly re-settles when
                    # its value lands). See Melty.pending_placeholder_frame.
                    _measured_h = draw_state._content_rect[1]
                    if not (Melty.pending_placeholder_frame == Melty.frame_count
                            and _measured_h < draw_state.content_height):
                        draw_state.content_height = _measured_h

                draw_state._source["content_height"] = "draw_state._content_rect[1]"

            if use_cache:
                # Cached freeze_resize views get their scrollbar drawn in
                # here, after the body / the blitted tile frame
                # (BlitCache.draw_freeze_scrollbar).
                Melty.cache.mark_end_offscreen()

            if _has_imgui:
                if closable:
                    Melty.pop_clip()

                if fixed_size:
                    Melty.fixed_size_stack.pop()

                if column is not None and column_parent is not None and draw_state.parent_window is not None:
                    fixed_sized_ds = Melty.fixed_size_stack[-1] if len(Melty.fixed_size_stack) > 0 else None
                    fixed_sized_bottom = fixed_sized_ds.top + fixed_sized_ds.height if fixed_sized_ds is not None else 0
                    Melty.fixed_size_stack.pop()
                    #
                    max_height = 0
                    for i in range(column_parent.final_max_column + 1):
                        column_height = column_parent._column_cursor[i][1]
                        if column_height > max_height:
                            max_height = column_height + 2

                    column_top = column_parent._columns_top if column_parent._columns_top is not None else 0
                    imgui.set_cursor_screen_pos((imgui.get_cursor_screen_pos()[0], column_parent.abs_top +draw_state.header_height + column_top + max_height))

                if draw_state._has_popup:
                    is_popup_open = Melty.imgui_popup_open
                    if is_popup_open != draw_state._imgui_popover_open and not is_popup_open:
                        note = Note(name=f"popover close {draw_state.name}", tint=(0,1,1))
                        Melty.cache.invalidate_up_by_obj(input_value, max_depth=4, note=note)
                    if is_popup_open:
                        Melty.report_imgui_active()
                    draw_state._imgui_popover_open = Melty.imgui_popup_open

            if _has_imgui:
                pop_id()
                if closable:
                    Melty.melty_window_stack.pop()
                Melty.size_stack.pop()
            if has_collection:
                Melty.collection_stack.pop()
            Melty.input_value_stack.pop()
            Melty.wrap_stack.pop()

            # if draw_state.frame_count < 3 and Melty.frame_count > 5:
            #     print(f"new view! {draw_state.id}")
            #     # New view created on the fly, let everything settle.
            #     draw_state._parent.invalidate_up(max_depth=6, frame_delta=1)
            #     request_render(for_frames=2)

            if melty_window and draw_state.width < 30:
                draw_state.width = 30
            if melty_window and draw_state.height < 30:
                draw_state.height = 30
                draw_state._source["height"] = "min 30"

            if auto_resize:

                if kwargs.get("wrap", False) or closable:
                    if passed_width is None:
                        max_width = kwargs.get("max_width", 1e9)
                        min_width = kwargs.get("min_width", 20)
                        draw_state.width = snap_int(max(min(item_rect[0], max_width), min_width))

                if passed_height is None and kwargs.get("fill_height", None) is None:
                    max_height = kwargs.get("max_height", 1e9)
                    if column is not None and column_parent is not None:
                        # item_rect is the UNCLIPPED group rect, so a scrolled
                        # group would measure its full content here and undo
                        # the pre-rendering clamp - bound it the same way.
                        max_height = min(max_height, column_max_height(column_parent))
                    if not closable:
                        draw_state.height = min(snap_int(item_rect[1]), max_height)
                        draw_state._source["height"] = "not closable, item_rect[1]"
                    else:
                        display_height = imgui.get_io().display_size[1]
                        draw_state.height = snap_int(min(item_rect[1], min(display_height, max_height)))
                        draw_state._source["height"] = "closable, item_rect[1]"

            elif (closable and passed_height is None
                  and getattr(draw_state, "_height_from_content", False)):
                # Fixed-size closable window that opened with the first-frame
                # placeholder height: adopt the measured content height once,
                # then leave the height to the user's resize handle. Skip
                # pending-placeholder frames - the measure reflects the
                # placeholder, not the content (same guard as fill_height).
                if (item_rect[1] > 1
                        and Melty.pending_placeholder_frame != Melty.frame_count):
                    display_height = imgui.get_io().display_size[1]
                    max_height = kwargs.get("max_height", 1e9)
                    draw_state.height = snap_int(
                        max(min(item_rect[1], display_height, max_height), 20))
                    draw_state._source["height"] = "initial content height"
                    draw_state._height_from_content = False


            if (draw_state.width != original_width_b or
                    draw_state.height != original_height_b):
                if (draw_state._collection_draw_state is not None and not Melty.on_drag and not
                imgui.is_mouse_down(1) and not imgui.is_mouse_down(2)):
                    draw_state._collection_draw_state.invalid_content_height = True

                if draw_state._parent is not None:
                    draw_state._parent.invalid_content_height = True

            if draw_state.width > 10000:
                draw_state.width = 10000

            if draw_state.height > 70000:
                draw_state.height = 70000
                draw_state._source["height"] = "70000 max height"

            if draw_state.expanded:
                draw_state.min_width = kwargs.get("min_width", draw_state.min_width)
                draw_state.min_height = kwargs.get("min_height", draw_state.min_height)
                # `max_height` alone bounds only what the wrapper SIZES (the
                # auto-resize measure, a closable window's first-render
                # content adopt - Mode.LIVE_WINDOW relies on the handle being
                # free of it). `enforce_max_height=True` makes it a hard
                # cap the resize paths hold in realtime (draw_state.max_height).
                draw_state.max_height = (kwargs.get("max_height", None)
                                         if kwargs.get("enforce_max_height", False) else None)

                if draw_state.width is not None and draw_state.min_width is not None:
                    draw_state.width = max(draw_state.width, draw_state.min_width)
                    draw_state.content_width = max(draw_state.content_width, draw_state.min_width)

                if draw_state.height is not None and draw_state.min_height is not None:
                    if draw_state.height < draw_state.min_height:
                        draw_state._source["height"] = "initial window size"

                    draw_state.height = max(draw_state.height, draw_state.min_height)
                # A closable (user-resized) window never exceeds its
                # max_height - the cap the resize paths enforce mid-drag,
                # re-applied here so a size written from elsewhere (a
                # persisted size, a shrunk content) lands under it too.
                if closable and draw_state.max_height and draw_state.height:
                    if draw_state.height > draw_state.max_height:
                        draw_state.height = snap_int(draw_state.max_height)
                        draw_state._source["height"] = "max_height cap"


            column = kwargs.get("column", None)

            # The parent's flow must advance past this view's BOX
            # (draw_state.height is what the bg, borders, BVH and the dummy
            # skip-advance all use), not just past what the body managed to
            # draw. When a floor (min_height / passed height) lifts height
            # above the measured flow, live-rendered rows pack tighter than
            # skip-advanced ones and everything below jumps as the view
            # crosses a scroll viewport edge. The group ended above
            # (item_rect is already captured), so this dummy extends only the
            # PARENT's flow - it cannot feed back into this view's measure.
            if (not closable and draw_state.expanded and column is None
                    and not header_same_line and draw_state.height is not None):
                _box_bottom = draw_state.abs_top + draw_state.height
                _short_by = _box_bottom - imgui.get_cursor_screen_pos()[1]
                if _short_by > 2:
                    imgui.dummy(0, _short_by)

            current_cursor = imgui.get_cursor_screen_pos()
            delta_x = current_cursor[0] - draw_state.abs_left
            delta_y = current_cursor[1] - draw_state.abs_top

            if column_parent is not None:
                if column is not None:
                    clip_bottom = draw_state.abs_clip_rect[3]
                    height = clip_bottom - draw_state.abs_top
                    column_parent._column_cursor[column][1] += draw_state.height

            if not closable:
                clip_rect = draw_state._parent.abs_clip_rect

                clipped_bottom = min(clip_rect[3], draw_state.abs_top + draw_state.height)
                clipped_top = max(clip_rect[1], draw_state.abs_top)
                clipped_height = clipped_bottom - clipped_top
                draw_state._parent._melty_content_height += clipped_height



                # else:
                #     if draw_state._parent.id != draw_state.id:
                #         draw_state._parent._outside_column_height += draw_state.height
            # elif column is None and column_parent is not None and column_parent._current_max_column == 0:
            #     draw_state_bottom = draw_state.abs_top + draw_state.height
            #     column_parent._inner_cursor = draw_state_bottom - column_parent.abs_top  # This will lag behind a frame, but keeping code tidy instead

            # if column_parent is not None:
            #     for i in range(column_parent.final_max_column):
            #         column_parent._column_cursor[i][1] += draw_state.height


            draw_state.pos_changed()

            draw_state._hovered = False
            draw_state.hotkey_receiver = False
            draw_state.last_seen = Melty.frame_count


        except Exception as e:
            # A hotswapped function/class/module that compiled clean can still
            # throw when its new code actually RUNS here. Ask the rollback guard to
            # match this traceback to a recent hotswap; if it owns it, the live
            # object is reverted to its previous good state (app stays up) and the
            # error is recorded for the user to surface. A rolled-back swap won't
            # throw again next frame, so the loop recovers.
            from src.lsd.gl_gui.view.core_conversion import hotswap_guard
            if hotswap_guard.handle_exception(e):
                request_render()

            # Check if previous stack trace is the same as the current one to avoid flooding logs with the same error
            is_same_exception = False
            if draw_state is not None and draw_state._stack_trace is not None:
                previous_exception = draw_state._stack_trace
                if type(e) == type(previous_exception):
                    is_same_exception = True
            if draw_state is not None:
                draw_state._stack_trace = e

            if not is_same_exception:
                # The fancy printer resolves watches against live objects and
                # can itself throw. An escape here would land in the finally
                # below, whose return DISCARDS the in-flight exception and the
                # original trace would vanish without a sound. Fall back to a
                # plain traceback, which cannot fail.
                try:
                    with trace_group(f"Drawing {func.__name__} {draw_state.name}", hash=draw_state.unique) as g:
                        watch = ["draw_state.name", "input_value", "convert_path", "clean_args.input_value", "func.__name__",
                                 "mode"]
                        print_stack_trace(frames=get_live_frames(), section="UI Thread",
                                          group=g, watch=watch)
                        print_stack_trace(exception=e, section="Exception",
                                          group=g, watch=watch)
                        print_stack_trace(exception=e, ignore_functions=[])
                except Exception as report_err:
                    print(f"print_stack_trace failed, plain traceback for {func.__name__}:")
                    traceback.print_exception(type(e), e, e.__traceback__)
                    print("--- reporter's own failure ---")
                    traceback.print_exception(type(report_err), report_err,
                                              report_err.__traceback__)
            else:
                print(f"Exception in {func.__name__}: {e}")


        finally:

            if is_root:
                style = imgui.get_style()
                style.item_spacing = Melty.original_spacing
                style.window_padding = Melty.original_window_padding
                style.frame_padding = Melty.original_frame_padding

                if Melty.channels_split:
                    draw_list = imgui.get_window_draw_list()
                    Melty.channels_split = False
                    draw_list.channels_merge()

            Melty.active_layer = original_active_layer
            Melty.paint_rank = original_paint_rank
            Melty.shadow_depth = start_shadow_depth

            if mode_stacked:
                Melty.mode_stack.pop()
            if _codec_pushed:
                Melty.codec_stack.pop()
            if _pushed_search:
                # Count + current-match selection were computed by the pre-body
                # search_walk (the single source for truth); nothing to read back
                # here, just unwind the stack.
                Melty.search_stack.pop()

            draw_state.frame_count += 1
            if Melty.imgui_crashed:
                if return_extras:
                    return False, None, draw_state
                return False, None
            if inc_depth:
                if _has_imgui:
                    Melty.draw_state_stack.pop()

                Melty.depth = Melty.depth - 1
                if len( Melty.unique_stack) > 0:
                    Melty.unique_stack.pop()

            use_cache = kwargs.get("use_cache", False) and Melty.cache is not None and Melty.cache.enabled
            if not use_cache and Melty.cache is not None:
                Melty.cache.mark_uncached(draw_state.name, input_value, collection, tile_id, draw_state)

            if return_value is None:
                return_value = draw_state._return_value

            return_draw_state = draw_state
            if return_value is None:
                child_changed, new_value = False, None
            elif isinstance(return_value, tuple) and len(return_value) == 3:
                child_changed, new_value, return_draw_state = return_value
            elif isinstance(return_value, tuple) and len(return_value) == 2:
                child_changed, new_value = return_value
            elif isinstance(return_value, bool):
                child_changed, new_value = return_value, input_value
            else:
                if _has_imgui:
                    imgui.text("Unsupported return from render_func")
                child_changed, new_value = False, None

            end_time = time.time()
            draw_state.render_time = end_time - start_time

            if _has_imgui:
                style = imgui.get_style()

                if is_root:
                    style.item_spacing = Melty.original_spacing
                    style.window_padding = Melty.original_window_padding
                    style.frame_padding = Melty.original_frame_padding

                    if Melty.channels_split:
                        draw_list = imgui.get_window_draw_list()
                        Melty.channels_split = False
                        draw_list.channels_merge()

            draw_state._external_change = False
            if child_changed:
                draw_state._pending = False

            # Undo/redo interception: if ctrl+z / ctrl+shift+z registered a request
            # for this draw state, override its output with the requested value so
            # the parent writes it back into the model, and restore the caret that
            # rode along. Skip recording this restore as a new change - otherwise
            # undo/redo would itself be logged and the timeline would toggle.
            is_undo = draw_state in Melty.undo_requests
            if is_undo:
                _requested, target_ui = Melty.undo_requests.pop(draw_state)
                if getattr(_requested, "__collection_mutation__", False):
                    # Mutation-based undo entry (drag-drop reorder): the stack
                    # contains "insert x at key a"-style ops, never dict values.
                    # Apply the requested side to the LIVE collection and let
                    # the mutated result flow out as this view's changed value.
                    _live = new_value if child_changed else input_value
                    try:
                        _, new_value, _ = _requested.apply(_live)
                        # Rows moved/appeared; no SIZE changed, so nothing
                        # else triggered the height changed - yet every cached
                        # relative_pos now describes the pre-mutation layout
                        # and a skip-advance render would commit it as truth.
                        draw_state.invalid_content_height = True
                    except Exception as _mut_err:
                        print(f"undo mutation apply failed: {_mut_err}")
                        new_value = _live
                else:
                    # Text undo/redo entry: stamp the changed span (in the
                    # RESTORED version's coordinates) so the parent view can
                    # scroll the change into view and flash it next body run
                    # (draw_text consumes `_undo_landing`). Restores go
                    # through the same wrapper-tail path for every value type,
                    # so this is the one place that knows both sides.
                    _live = new_value if child_changed else input_value
                    if isinstance(_requested, str) and isinstance(_live, str):
                        _a, _b = _str_change_span(_live, _requested)
                        draw_state._undo_landing = (_requested, _a, _b,
                                                    Melty.frame_count)
                    new_value = _requested
                child_changed = True
                draw_state.apply_undo_state(target_ui)

            if _has_imgui and not is_undo:
                handle_undo(child_changed, input_value, new_value, draw_state)

            # Drag-and-drop interception: a drop registered a CollectionMutation
            # for this view's draw_state (see drag_drop.py). Apply it to
            # the LIVE collection and report changed=True so the parent writes
            # it back into the model - the same path an undo restore takes -
            # and record (inverse, mutation) so the drop is one undo step.
            if Melty.dnd_requests and draw_state in Melty.dnd_requests:
                _dnd_op = Melty.dnd_requests.pop(draw_state)
                try:
                    _dnd_changed, _dnd_value, _dnd_inverse = _dnd_op.apply(
                        new_value if child_changed else input_value)
                except Exception as _dnd_err:
                    print(f"drag-drop apply failed: {_dnd_err}")
                    _dnd_changed, _dnd_value, _dnd_inverse = False, None, None
                if _dnd_changed:
                    child_changed, new_value = True, _dnd_value
                    # A reorder/insert changed no row SIZES, so the size-change
                    # detector never flags the height - but every cached
                    # relative_pos now describes the pre-drop layout, and a
                    # skip-advance render would measure and COMMIT that stale
                    # layout (the locked 949-vs-164 content_height on Loras
                    # View Three.) Force a full measure on the next render.
                    draw_state.invalid_content_height = True
                    if _dnd_inverse is not None:
                        UndoManager.record(draw_state, _dnd_inverse, _dnd_op)


            # TEMP perf: any view whose wrapper burns >30ms - split the
            # prologue (kwargs gauntlet, scroll/cache setup: entry→inner), the
            # inner dispatch (draw_inner_main; the body's own time is on the
            # perf line), and the epilogue (inner→here: footer/content-rect,
            # changed handling, convert_out, dirty detection, dnd). Guarded:
            # cache-hit paths return before _wt1/_wt2 exist.
            try:
                _wt3 = time.perf_counter()
                _tot = _wt3 - _wt0
                _vm = getattr(Melty, "_frame_view_ms", None)
                if _ves_pushed:
                    # Exclusive time attribution (FULL renders - this call
                    # pushed around draw_inner_main): total - what nested
                    # wrapper calls already consumed. A frame-start reset
                    # (lsd_studio) rebalances after an exception unwind.
                    _ves = getattr(Melty, "_view_excl_stack", None)
                    if _ves:
                        _child = _ves.pop()
                        if _ves:
                            _ves[-1] += _tot
                        if _vm is not None:
                            _nm = getattr(func, "__name__", "?")
                            _e = _vm.get(_nm)
                            _vm[_nm] = ((_e[0] + _tot - _child, _e[1] + 1)
                                        if _e else (_tot - _child, 1))
                            if _nm == "button":
                                _bl = getattr(Melty, "_frame_button_names", None)
                                if _bl is not None and len(_bl) < 24:
                                    _bl.append(str(getattr(draw_state, "name", "?"))[:40])
                elif _vm is not None:
                    # Tile REPLAY (mark_start found the cached version; no
                    # push happened): the call still paid the full prologue +
                    # the shared epilogue. Billed to its own `name~r` row -
                    # NOT popped from the stack (the earlier version popped
                    # the PARENT's entry time, corrupting attribution: an
                    # inflated draw_func() times). All time still remains
                    # inside the nearest full-render ancestor's exclusive;
                    # the ~r rows exist to decompose exactly that.
                    _nm = getattr(func, "__name__", "?") + "~r"
                    _e = _vm.get(_nm)
                    _vm[_nm] = ((_e[0] + _tot, _e[1] + 1) if _e else (_tot, 1))
                if _ves_pushed and _tot > 0.0008:
                    from src.lsd.gl_gui.perf_trace import trace as _wtr
                    _b0 = getattr(draw_state, "_wt_body0", _wt2)
                    _b1 = getattr(draw_state, "_wt_body1", _wt2)
                    _ms = lambda a, b: f"{(b - a) * 1000.0:.2f}"
                    _wtr("wrapper split",
                         view=getattr(func, "__name__", "?"),
                         merge=_ms(_wt0, _wtA), resolve=_ms(_wtA, _wtB),
                         chk1=_ms(_wtB, _wtD), state=_ms(_wtD, _wtE),
                         size=_ms(_wtE, _wtF), pos=_ms(_wtF, _wtG),
                         hover=_ms(_wtG, _wtC), setup=_ms(_wtC, _wt1),
                         pre=_ms(_wt1, _b0), body=_ms(_b0, _b1),
                         post=_ms(_b1, _wt2), epi=_ms(_wt2, _wt3))
            except Exception:
                pass

            # Normal return path
            if kwargs.get("convert_out", None) is not None or kwargs.get("convert_in", None) is not None:
                if return_extras:

                    return child_changed, new_value, return_draw_state
                return child_changed, new_value

            if isinstance(new_value, Pending):
                draw_state._loading = new_value
            else:
                draw_state._loading = False

            if return_extras:
                return child_changed, new_value, return_draw_state
            return child_changed, new_value

    def draw_pending_status(draw_state, pending_obj):
        load_icon = f"\uf110"
        draw_list: _DrawList = imgui.get_window_draw_list()
        if pending_obj.state == PendingState.ERROR:
            draw_list.add_text(
                *(draw_state.left + 2,
                  draw_state.top + draw_state.height - draw_state.footer_height - 20),
                pack_color(1, 0, 0, 1.0),
                f"{load_icon} {pending_obj.status}")
        else:
            draw_list.add_text(
                *(draw_state.left + 2, draw_state.top + draw_state.height - draw_state.footer_height - 20),
                pack_color(1, 1, 1, 0.5),
                f"{load_icon} {pending_obj.status}")

    def draw_inner_main(clean_args, draw_state, input_value, unique, kwargs):
        return_value = None
        expected_type = param_types[0] if len(param_types) > 0 else None
        annotation_empty = expected_type == inspect.Parameter.empty
        input_value = clean_args.get('input_value', input_value)

        if not annotation_empty:
            if expected_type is not Any and isinstance(expected_type, type):
                if not isinstance(input_value, expected_type):
                    yellow = (1.0, 1.0, 0.0, 1.0)
                    if imgui.button(f"Fix Type##{unique}"):
                        if expected_type == tuple:
                            return True, (0, 0, 0)
                        return True, expected_type()
                    same_line()
                    type_class_path = f"{expected_type.__module__}.{expected_type.__name__}"
                    actual_type_class_path = f"{type(input_value).__module__}.{type(input_value).__name__}"
                    imgui.text_colored(f"Type mismatch in {func.__name__}\n"
                                       f"Expected {type_class_path}, "
                                       f"got {actual_type_class_path}", *yellow)
                    return False, None

        use_cache = kwargs.get("use_cache", False) and Melty.cache.enabled
        draw_state.use_cache = use_cache
        kwargs.pop("use_cache", None)

        # A view that auto-sizes to its content can never overflow ITSELF: its
        # height tracks the content, so abs_content_height ~= height and the
        # comparison below becomes a perpetual near-tie that flutters True for
        # single frames during scroll. Only a view whose height is externally
        # bounded (a window/closable, a passed or fill height, or a clamping
        # max_height) can actually overflow and thus needs its own scrollbar.
        # (The scrollbar reserve is subtracted from content_width unconditionally
        # for scroll-capable views in the renderer - scroll_visible only
        # controls whether the bar is drawn, never the width.)
   
        _max_h = kwargs.get("max_height", None)
        _height_bounded = (not draw_state.auto_resize
                           or (_max_h is not None and draw_state.abs_content_height > _max_h))
        needs_scroll = (_height_bounded and draw_state.multi_line
                        and draw_state.abs_content_height > draw_state.height
                        + draw_state.footer_height + draw_state.header_height)

        if not kwargs.get("disable_scroll", True) and Toggles.ScrollSettings.debug_scroll:
            draw_list = imgui.get_overlay_draw_list()
            draw_list.channels_set_current(Melty.max_layer - 1)

            red = pack_color(1, 0, 0, 1.0)
            yellow = pack_color(1, 1, 0, 1.0)
            green = pack_color(0, 1, 0, 1.0)
            draw_list.add_text(draw_state.abs_left, draw_state.abs_top - 20, red,
                                "abs_left, abs_top, width, height")
            draw_list.add_rect(draw_state.abs_left, draw_state.abs_top, draw_state.abs_left + draw_state.width,
                                 draw_state.abs_top + draw_state.height, red)

            draw_list.add_text(draw_state.abs_left, draw_state.abs_top, yellow,
                               "abs_left, abs_top, width, draw_state.abs_clipped_height")
            draw_list.add_rect(draw_state.abs_left, draw_state.abs_top, draw_state.abs_left + draw_state.width,
                               draw_state.abs_top + draw_state.abs_clipped_height, yellow)


            # draw_list.add_line(draw_state.abs_left, draw_state.abs_top + draw_state.header_height,
            #                  draw_state.abs_left + draw_state.width, draw_state.abs_top + draw_state.header_height, yellow)


        if draw_state.just_shadow or kwargs.get("disable_scroll", False):
            needs_scroll = False

        draw_state.scroll_visible = needs_scroll
        # The bar's retained depth mark is the wrapper's (SCROLLBAR_SHADOW_GROUP),
        # so the wrapper resets it on every body run the bar doesn't draw on
        # (needs_scroll off or closed): draw_overlay_scrollbar re-retains it
        # below when it does draw. Cached freeze_resize views are skipped:
        # the bar was already cleared this frame from mark_end_offscreen
        # (BlitCache.draw_freeze_scrollbar clears the group itself) and a
        # premature here would drop that emission.
        if not (draw_state.freeze_resize and draw_state.use_cache):
            clear_shadows(draw_state, SCROLLBAR_SHADOW_GROUP)
        # Only zero the offset when the content GENUINELY fits - never while the
        # content height is still unmeasured (invalid_content_height). On the
        # first frame(s) after a view loads from a saved state, its children
        # haven't registered yet, so abs_content_height (and thus needs_scroll)
        # reads stale/0; zeroing then would wipe the restored scroll_offset
        # before the content kicks in. invalid_content_height isn't persisted, so a
        # freshly loaded view starts True and turns False once draw_collection
        # has actually measured the content.
        if not needs_scroll and draw_state.frame_count > 3:
            if (draw_state.scroll_offset[1] > 1
                    and Melty.frame_count - getattr(draw_state, "_jump_dbg", -9999) < 120):
                try:  # jump-scroll debug (see _uj_log in text_editor.py)
                    with open("/tmp/uj_debug.log", "a") as _jf:
                        _jf.write(f"[f{Melty.frame_count}] WIPE {draw_state.name!r} "
                                  f"sy={draw_state.scroll_offset[1]:.0f}->0 "
                                  f"content_h={draw_state.abs_content_height:.0f} "
                                  f"h={draw_state.height} ds_frames={draw_state.frame_count}\n")
                except OSError:
                    pass
            draw_state.scroll_offset = (0, 0)
        scroll_y_changed = None
        if needs_scroll:
            scroll_y_changed = draw_state.on_action("scroll_y_changed", view_id="view_scroll", priority_delta=10)
            scroll_delta = 0

            if scroll_y_changed is not None:
                scroll_delta = scroll_y_changed.value
                Melty.selected = set()
                Melty.selected.add(draw_state)

            scroll_offset = draw_state.scroll_offset
            current_x = scroll_offset[0]
            current_y = scroll_offset[1]
            direction = -1
            # Scale the scroll speed to the window: never let a single wheel
            # tick jump more than max_increment_fraction of the visible (clipped)
            # height, so small views don't overshoot. Larger views fall back to
            # the constant increment.
            time_since_scroll = time.time() - Melty.last_scroll_time
            if time_since_scroll > Toggles.ScrollSettings.acceleration_threshold:

                scroll_speed = min(
                    Toggles.ScrollSettings.scroll_speed,
                    Toggles.ScrollSettings.max_increment_fraction *
                    draw_state.abs_clipped_height)
            else:
                scroll_speed = Toggles.ScrollSettings.scroll_speed
            new_offset_y = ceil(current_y + scroll_delta * direction * scroll_speed)

            if scroll_y_changed:
                Melty.last_scroll_time = time.time()

            min_scroll_y = 0
            max_scroll_y = max(0, draw_state.abs_content_height -
                               draw_state.abs_clipped_height + 1)
            # Publish the authoritative max for descendants (text editor's
            # drag-auto-scroll). Recomputing the value there captured
            # content_height at a different time, so the two clamps disagreed by
            # a few px and fought each frame - a flicker at the bottom.
            draw_state._max_scroll_y = max_scroll_y

            # Give views time to settle
            if Melty.frame_count > 2:
                # Apply the wheel delta only when not dragging/panning (those set
                # scroll_offset directly), but ALWAYS clamp to range - even
                # on-drag. The text editor's drag-auto-scroll writes
                # scroll_offset during content render (after this runs), pushing
                # it past EOF; this captured value is then used just below to
                # offset the content cursor. A clamping here that overshoot
                # positions the content over-scrolled, leaving the cursor in an
                # inconsistent spot for the next view - the bottom flicker.
                if not Melty.on_drag and not imgui.is_mouse_down(1) and not imgui.is_mouse_down(2):
                    target_y = new_offset_y
                else:
                    target_y = current_y
                _clamped_y = max(min_scroll_y, min(target_y, max_scroll_y))
                if (abs(_clamped_y - current_y) > 1
                        and Melty.frame_count - getattr(draw_state, "_jump_dbg", -9999) < 120):
                    try:  # jump-scroll debug (see _uj_log in text_editor.py)
                        with open("/tmp/uj_debug.log", "a") as _jf:
                            _jf.write(f"[f{Melty.frame_count}] CLAMP {draw_state.name!r} "
                                      f"sy={current_y:.0f}->{_clamped_y:.0f} "
                                      f"max={max_scroll_y:.0f} "
                                      f"content_h={draw_state.abs_content_height:.0f} "
                                      f"clip_h={draw_state.abs_clipped_height:.0f}\n")
                    except OSError:
                        pass
                draw_state.scroll_offset = (current_x, _clamped_y)
                # Change ledger: a wheel scroll that MOVED the view is an
                # observable effect (the orchestrator's "scroll" precondition
                # fix is discovered from a recorded one - see change_value).
                if (scroll_y_changed is not None and _clamped_y != current_y
                        and Melty.effect_hook is not None):
                    try:
                        Melty.effect_hook("scroll", str(draw_state.name).split("##")[0],
                                          draw_state)
                    except Exception:
                        pass

            if scroll_y_changed is not None:
                note=Note(name=f"scroll change {draw_state.name}", tint=(1, 0, 1))
                # Don't cascade the scroll invalidation into nested windows -
                # a window owns its own scrollbar and doesn't scroll with us,
                # so re-invalidating that subtree is wasted work.
                Melty.cache.invalidate_up(draw_state._tile_id, max_depth=3, note=note,
                                          include_windows=False)
                Melty.cache.invalidate_scrolled_in(draw_state, on_change=False)

            # Cached freeze_resize views: blit_offscreen owns the scrollbar and
            # draws it in mark_end_offscreen, after the body on live frames
            # and after the clipped tile image on served/frozen frames
            # (BlitCache.draw_freeze_scrollbar). With caching off there is
            # no blit, so the body path draws it like any other view.
            if not draw_state.closed and not (draw_state.freeze_resize
                                              and draw_state.use_cache):
                draw_overlay_scrollbar(draw_state, max_scroll_y, draw_state.height - draw_state.footer_height,
                                       bar_width=kwargs.get("scroll_bar_width", SCROLL_BAR_WIDTH_DEFAULT),
                                       bar_brightness=kwargs.get("scroll_bar_brightness",
                                                                 SCROLL_BAR_BRIGHTNESS_DEFAULT))

        do_scroll = needs_scroll
        scroll_offset = draw_state.scroll_offset if do_scroll else (0, 0)

        # if draw_state.abs_content_height > draw_state.height + draw_state.header_height + draw_state.footer_height or draw_state.closable:
        #     current_cursor = imgui.get_cursor_screen_pos()
        #     imgui.set_cursor_screen_pos((draw_state.abs_left, draw_state.abs_top))
        #     from src.lsd.gl_gui.view.core_views.new_core_view import empty
        #     #
        #     # empty(name=f"header space{unique}", z_offset=0.0,
        #     #       tile_mode=TileMode.MAX, width=draw_state.width,
        #     #       height=draw_state.header_height)
        #     imgui.set_cursor_screen_pos(current_cursor)

        if do_scroll:
            header_height = draw_state.header_height
            # Reserve space at the right edge for the overlay scrollbar (grab
            # area + margins - see draw_overlay_scrollbar) so the content
            # wraps/clips before the bar instead of going under it.
            # freeze_resize views: no reserve - content runs to the edge and
            # the bar is drawn over it by blit_offscreen.
            if draw_state.freeze_resize:
                scrollbar_reserve = 0
            else:
                scrollbar_reserve = (kwargs.get("scroll_bar_width", SCROLL_BAR_WIDTH_DEFAULT)
                                     + SCROLLBAR_MARGIN)

            Melty.push_clip((draw_state.abs_left, draw_state.abs_top + header_height,
                             draw_state.abs_left + draw_state.width - scrollbar_reserve,
                             draw_state.abs_top + header_height + draw_state.height + 2))
            start_cursor = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((start_cursor[0],
                                         start_cursor[1] - scroll_offset[1]))


        # If we are using the new callback header, gate rendering behind expanded.
        # ExpandMode.MANUAL opts out of the gate: the func always runs and reads
        # draw_state.expanded to gate its own collapsed rendering.
        Melty.silence_invalidate = False
        if draw_state.expanded or kwargs.get("expanded_mode", None) is ExpandMode.MANUAL:
            is_primitive = input_value is None or isinstance(input_value,
                                                             (int, float, str, bool, tuple)) and not hasattr(
                input_value, '__dict__')

            # seen_values holds id()s (appended below) - count the id, not the
            # object: `count(obj)` compares obj == id with list `==`, which on
            # a torch.Tensor input_value is elementwise → "Boolean value of
            # Tensor is ambiguous" in the render thread (locking the studio down
            # whenever a tensor was rendered nested).
            if not is_primitive and (id(input_value) in Melty.seen_values and
                                     Melty.seen_values.count(id(input_value)) > 1 or Melty.depth > 15):
                imgui.text("Recursive reference detected: " + str(input_value))
            else:
                if not is_primitive:
                    Melty.seen_values.append(id(input_value))
                #################################################################################################

                if kwargs.get("background", False) and draw_state.frame_count > 3:
                    # Run the full wrapper with _converter_mode=True in the
                    # background call. This gives the func all the render_func
                    # machinery (draw_state, caching, parameter injection) but
                    # skips the imgui call via the _has_imgui guards.
                    # Dear ImGui's current context is a global (not thread-local),
                    # so we can't create a headless context on worker threads.
                    return_value = Background.run(func, func_kwargs=clean_args, invalidate_id=draw_state._tile_id,
                                                  user_id=str(unique) + "async", no_cache=kwargs.get("changed", False),
                                                  on_frame=Melty.frame_count)
                    if isinstance(return_value, tuple) and len(return_value) == 2 and isinstance(return_value[1], int):
                        return_value = return_value[0]

                    if isinstance(return_value, Pending):
                        draw_list = imgui.get_window_draw_list()
                        draw_list.add_text(
                            *(draw_state.left + 2,
                              draw_state.top + draw_state.height - draw_state.footer_height - 20),
                            pack_color(1, 1, 1, 0.5),
                            f"\uf110 {return_value.status}")
                        return_value.originated = wrapper
                        return_value = False, return_value
                else:

                    Melty.silence_invalidate = False

                    # Eval harness hook (used by the context menu's eval tab).
                    # We run the snippet HERE, right before the view function, so
                    # it sees that function's real call-time locals: clean_args is
                    # exactly the arg set about to be bound as the func's params.
                    if getattr(draw_state, '_eval_pending', False):
                        draw_state._eval_pending = False
                        try:
                            from src.lsd.gl_gui.view.core_views.new_core_view import run_scoped_eval
                            draw_state._eval_result = run_scoped_eval(
                                getattr(draw_state, '_eval_code', '') or '',
                                func, draw_state, clean_args)
                        except Exception as _eval_err:
                            draw_state._eval_result = f"eval harness error: {_eval_err}"
                        draw_state._eval_generation = getattr(draw_state, '_eval_generation', 0) + 1
                        request_render()

                    start_cursor = imgui.get_cursor_screen_pos()

                    # drives = kwargs.pop("drives", None)
                    lens_func = None
                    # if drives is not None:
                    #     imgui.text("driving")
                    #     print(f"drives: {drives}")
                    #
                    #     if isinstance(drives, tuple):
                    #         driven_value = drives[0]
                    #         lens_func = drives[1]
                    #     else:
                    #         driven_value = drives
                    #         if type(driven_value) in Melty.default_lenses_by_type:
                    #             lens_func = Melty.default_lenses_by_type[type(driven_value)]
                    #
                    #     if lens_func is not None:
                    #         changed, value = lens_func(driven_value, view_func=func, lens_func=lens_func,
                    #                                    child_kwargs=clean_args, _converter_mode=True)
                    #
                    #         if changed:
                    #             Core.melty.cache.invalidate_up_by_obj(driven_value)
                    #
                    #         return_value = changed, value
                    #
                    #     else:
                    #         imgui.text_colored(f"No lens for type {type(driven_value).__name__}", 1, 0.5, 0.5)
                    # else:

                    if getattr(draw_state, "_lv_capture_body", False):
                        # One-shot from a menu-open capture: monitor this func
                        # call's entry/exit (sys.monitoring, local events on
                        # the target code only) and its locals + source line
                        # show as frame-snapshot markers at return - then
                        # the LOCAL stack copy (the context menu's Code tab)
                        # swaps its terminal entry's signature scope for the
                        # body's full locals.
                        draw_state._lv_capture_body = False
                        from src.lsd.gl_gui.view.core_conversion.live_view import (
                            call_with_body_capture)
                        _bc_code = getattr(inspect.unwrap(func), "__code__",
                                           None)

                        def _adopt_body_locals(scope, _ds=draw_state,
                                               _code=_bc_code):
                            # Replace IN A NEW list - the Code tab keys its
                            # pane rebuild on the stack list's identity, so
                            # mutating in place would never re-resolve. Only
                            # when the tail really is this function (the
                            # terminal append is skipped on non-project
                            # source).
                            stack = getattr(_ds, "_call_stack_frames", None)
                            if not stack or _code is None:
                                return
                            tail = stack[-1]
                            if (tail[0] != _code.co_filename
                                    or tail[1] != _code.co_firstlineno):
                                return
                            _ds._call_stack_frames = stack[:-1] + [
                                (tail[0], tail[1], tail[2], scope)]
                            # The Code tab retains the OLD list object -
                            # find its tiles by that object and re-render
                            # them so the fresh locals show without waiting
                            # for an unrelated repaint.
                            try:
                                Melty.cache.invalidate_by_obj(stack)
                                request_render()
                            except Exception:
                                pass

                        return_value = call_with_body_capture(
                            func, clean_args, on_captured=_adopt_body_locals)
                    else:
                        draw_state._wt_body0 = time.perf_counter()   # TEMP perf
                        return_value = func(**clean_args)
                        draw_state._wt_body1 = time.perf_counter()   # TEMP perf


                    # Stack cleanup handled by the finally block below

                    draw_state._return_value = return_value
                    end_cursor = imgui.get_cursor_screen_pos()
                    draw_state.observed_content_height = int(end_cursor[1] - start_cursor[1])

                    Melty.silence_invalidate = True


                ################################################################################################
                imgui.set_item_allow_overlap()
                if not is_primitive:
                    Melty.seen_values.pop()


        # if scroll_y_changed is not None:
        #     scroll_delta = scroll_y_changed.value
        #     Melty.selected = set()
        #     Melty.selected.add(draw_state)
        #     Melty.cache.invalidate_scrolled_in(draw_state, on_change=False)

            # Melty.cache._last_scroll_change_frame[draw_state._tile_id] = Melty.frame_count
            # BVH sweep: any view inside this scroll view's clip rect with a
            # dirty bbox gets invalidated. Runs at the scroll-event site so
            # each scroll view manages its own descendants, not per-child.
            # Melty.cache.invalidate_scrolled_in(draw_state, on_change=True)
        # else:
        #     # Catchup one frame later: views that registered their updated
        #     # bbox during their own render (after the scroll-event sweep
        #     # already ran last frame) weren't visible to the BVH yet, so
        #     # repeat the sweep once with the now-current index.
        #     last_change = Melty.cache._last_scroll_change_frame.get(draw_state._tile_id, -10)
        #     if Melty.cache._frame_id - last_change < 2:
        #         Melty.cache.invalidate_scrolled_in(draw_state, on_change=False)


        if do_scroll:
            Melty.pop_clip()
            start_cursor = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((start_cursor[0],
                                         start_cursor[1] + scroll_offset[1]))

        return return_value

    def add_default(register_type):
        o_kwargs.pop('is_default_for', None)
        if isinstance(register_type, Shaped):
            # Shape-refined entry, keyed by the (hashable) Shaped itself so
            # the hotswap reconcile treats it like any other key → wrapper.
            Melty.default_funcs_by_shape[register_type] = wrapper
        elif not isinstance((register_type), str):
            Melty.default_funcs_by_type[register_type] = wrapper
            Melty.default_funcs_by_name[register_type.__name__] = wrapper
        else:
            Melty.default_funcs_by_name[register_type] = wrapper

    is_default_for = o_kwargs.get('is_default_for', None)
    if isinstance(is_default_for, (tuple, list)):
        for a_type in is_default_for:
            add_default(a_type)
    elif isinstance(is_default_for, (type, str, Shaped)):
        add_default(is_default_for)

    def add_lens(register_type):
        if isinstance(register_type, Shaped):
            Melty.default_lenses_by_shape[register_type] = wrapper
        else:
            Melty.default_lenses_by_type[register_type] = wrapper

    is_lens_for = o_kwargs.pop('is_lens_for', None)
    if isinstance(is_lens_for, (tuple, list)):
        for a_type in is_lens_for:
            add_lens(a_type)
    else:
        add_lens(is_lens_for)

    interrupt_type = o_kwargs.pop('interrupt_source_for', None)
    if interrupt_type is not None:
        Melty.type_interrupts[interrupt_type] = wrapper

    ##++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
    ## Converter render_func frankenstein to handle both normal render functions and annotation-based render functions
    ##____________________________________________________________________________________________________________
    load_data = o_kwargs.get("load_data", None)
    save_data = o_kwargs.get("save_data", None)
    registry = o_kwargs.get("registry", None)
    from_type = o_kwargs.get("from_type", None)
    to_type = o_kwargs.get("to_type", None)

    if registry is not None:
        if not hasattr(registry, '_converters') or not isinstance(registry._converters, dict):
            setattr(registry, '_converters', {})

        func_signature = inspect.signature(func)
        fn_params = func_signature.parameters

        inferred_to = func_signature.return_annotation
        if inferred_to is inspect.Signature.empty:
            inferred_to = None
        if inferred_to is not None and hasattr(inferred_to, "__args__"):
            inferred_to = inferred_to.__args__[0]

        inferred_from = None
        # for param_name in ("value", "data"):
        #     param = fn_params.get(param_name)
        #     if param is not None and param.annotation is not inspect.Parameter.empty:
        #         inferred_from = param.annotation
        #         break

        actual_from = from_type or inferred_from
        actual_to = to_type or inferred_to
        if actual_from is not None and actual_to is not None:
            registry._converters[(actual_from, actual_to)] = wrapper

        if hasattr(registry, "_converter_to_type") and isinstance(registry._converter_to_type, dict):
            registry._converter_to_type[wrapper] = (actual_from, actual_to)

        # ── Store flags ─────────────────────────────────────────────────

        if hasattr(registry, 'converter_flags_by_type') and isinstance(registry.converter_flags_by_type, dict):
            if o_kwargs:
                registry.converter_flags_by_type[(actual_from, actual_to)] = o_kwargs

        if hasattr(registry, 'converter_flags') and isinstance(registry.converter_flags, dict):
            if o_kwargs:
                registry.converter_flags[wrapper] = o_kwargs
                if "inverse_of" in o_kwargs:
                    inverse_fn = o_kwargs["inverse_of"]
                    if inverse_fn not in registry.converter_flags:
                        registry.converter_flags[inverse_fn] = {}
                    registry.converter_flags[inverse_fn]["inverse_of"] = wrapper

        if load_data is not None:
            if hasattr(registry, 'converter_flags') and isinstance(registry.converter_flags, dict):
                if wrapper not in registry.converter_flags:
                    registry.converter_flags[wrapper] = {}
                registry.converter_flags[wrapper]["stateful"] = True

    # Expose load_data/save_data for chain discovery
    if _rf_load_data is not None:
        wrapper._load_data = _rf_load_data
    if _rf_save_data is not None:
        wrapper._save_data = _rf_save_data

    # Expose searchable flag for draw_any
    if o_kwargs.get("searchable", False):
        wrapper._searchable = True

    wrapper.__render_func__ = True
    wrapper.__header_defaults__ = header_defaults
    wrapper.__params__ = params
    # on_cleanup(draw_state): called once per draw_state of this view at
    # Melty teardown (run_cleanup_callbacks). Stamped on the RAW func too -
    # draw_state._view_func is the raw function, which is how the teardown
    # walk finds the hook from a draw_state.
    wrapper.on_cleanup = on_cleanup
    try:
        func.__on_cleanup__ = on_cleanup
    except (AttributeError, TypeError):
        pass                      # non-function callables: no raw-side stamp
    # Callable (lazy - DrawState must be constructible) the param names this
    # view auto-mirrors onto its draw_state. For introspection/docs/tests.
    wrapper.__auto_state_params__ = _auto_state_params

    # Auto-register by name so RenderFuncs.<name> can resolve this lazily
    # without anyone importing the module that defines it (avoids import cycles).
    Melty.render_funcs_by_name[wrapper.__name__] = wrapper

    # Decorator-order independence: `@window` / `@defaults` return their
    # argument unchanged, so written before @render_func they ran on the RAW
    # function and registered IT. draw_main's window loop then saw an object
    # without __render_func__ (a code_file_io editor of the source instead of
    # the view), and hotswap's registry reconcile couldn't recognise the
    # throwaway raw a recompile re-registered. Adopt any registration made on
    # the raw onto the wrapper now, so every order lands on the same object.
    _adopt_raw_registrations(func, wrapper)

    return wrapper


def _adopt_raw_registrations(raw, wrapper):
    """Re-point registrations that `@window` / `@defaults` made on `raw` (they
    ran BEFORE @render_func wrapped it) onto `wrapper`, the object the app
    calls and the hotswap reconcile tracks. No-op when nothing registered."""
    if raw is wrapper:
        return
    # A @window that ran before melty installed its registrar sits in the
    # decoration module's pending list - retarget it there too.
    try:
        from src.lsd.gl_gui.view.core_views.decoration import window_decoration as _wd
        _wd._pending[:] = [((wrapper if c is raw else c), kw) for c, kw in _wd._pending]
    except Exception:
        pass
    wins = getattr(Melty, "annotated_window_classes", None)
    if isinstance(wins, dict):
        for key, entry in list(wins.items()):
            if isinstance(entry, tuple) and len(entry) == 2 and entry[0] is raw:
                wins[key] = (wrapper, entry[1])
    for reg_name in ("default_kwargs_by_type", "default_kwargs_by_attrib_type",
                     "default_funcs_by_name_type"):
        reg = getattr(Melty, reg_name, None)
        # Plain `in` - these are defaultdicts, a [] would create an entry.
        if isinstance(reg, dict) and raw in reg:
            reg[wrapper] = reg.pop(raw)


# Keys the wrapper consumes for control CONVERSION/PLUMBING, not appearance -
# excluded from render_func_kwarg_names. Tune freely; everything else the
# scan finds (width/height/tint/shadow/mode/layer/...) counts as an input that
# can realistically change how the view looks.
_RF_KWARG_EXCLUDE = frozenset({
    "_converter_mode", "next_kwargs", "draw_state", "input_value",
    "melty_window", "style_manager", "depth", "changed", "collection",
    "data", "ref", "registry", "from_type", "to_type", "load_data",
    "save_data", "is_default_for", "is_lens_for", "interrupt_source_for",
    "inverse_of", "real_type", "key", "return_extras", "name_func",
    "current_mode", "auto_apply", "convert_in", "convert_out", "pending",
    "type_collection", "drives", "view_func",
})
  
_rf_kwarg_names_cache = None

@window
@render_func
def draw_kwargs_names():
    global _rf_kwarg_names_cache
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
    draw_any(_rf_kwarg_names_cache, name="Names cache")

def render_func_kwarg_names():
    """The kwarg names the @render_func machinery itself consumes — width,
    height, tint, shadow, the flag zoo — i.e. the inputs every render func
    shares on top of its own signature. Built ONCE per process by AST-scanning
    render_func's own source for string-key access on kwargs / o_kwargs /
    header_defaults (`.get/.pop/.setdefault("x")`, `["x"]`, `"x" in kwargs`),
    minus the conversion-plumbing keys in _RF_KWARG_EXCLUDE. Self-maintaining:
    a new `kwargs.get("new_flag")` in the wrapper shows up on next launch (the
    cache resets when this module re-execs on hotswap)."""
    global _rf_kwarg_names_cache
    if _rf_kwarg_names_cache is None:
        import ast
        import textwrap
        targets = {"kwargs", "o_kwargs", "header_defaults"}
        names = set()
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(render_func)))
        except (OSError, TypeError, SyntaxError):
            return []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (isinstance(node.func.value, ast.Name) and node.func.value.id in targets
                        and node.func.attr in ("get", "pop", "setdefault")
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    names.add(node.args[0].value)
            elif isinstance(node, ast.Subscript):
                if (isinstance(node.value, ast.Name) and node.value.id in targets
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)):
                    names.add(node.slice.value)
            elif isinstance(node, ast.Compare):
                if (len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
                        and isinstance(node.left, ast.Constant)
                        and isinstance(node.left.value, str)
                        and any(isinstance(c, ast.Name) and c.id in targets
                                for c in node.comparators)):
                    names.add(node.left.value)
        _rf_kwarg_names_cache = sorted(names - _RF_KWARG_EXCLUDE)
    return _rf_kwarg_names_cache

# Warm the cache off the launch-critical path: inspect.getsource on the
# ~4700-line wrapper plus the AST walk is ~a third of the whole project-import
# cascade when run inline here - and an immediate thread just timeshares the
# GIL with the remaining imports (measured: no help). The 1s delay lets the
# cascade finish first; this scan then lands long before any human can open a
# context menu. It is idempotent and only sets the module global, so a
# consumer that arrives first simply computes inline - never wrong, never
# blocked. Hotswap reexec arms a fresh timer; the recompute is by design
# (the cache resets so code edits show up).
_rf_warm_timer = threading.Timer(1.0, render_func_kwarg_names)
_rf_warm_timer.daemon = True
_rf_warm_timer.start()


def begin_window(unique_id, *args, **kwargs):
    return_val = imgui.begin(unique_id, *args, **kwargs)
    draw_list = imgui.get_window_draw_list()
    draw_list.channels_split(Melty.max_depth)
    Melty.channels_split = True
    return return_val


def begin_group(unique_id=0, *args, **kwargs):
    return imgui.begin_group()


def end_group():
    imgui.end_group()


def end_window(unique_id):
    imgui.get_window_draw_list().channels_merge()
    Melty.channels_split = False
    imgui.end()


def push_id(unique_id):
    global id_stack
    if Melty.imgui_crashed:
        return
    imgui.push_id(str(unique_id))
    id_stack.append(unique_id)


def pop_id():
    global id_stack
    if Melty.imgui_crashed:
        return

    imgui.pop_id()
    id_stack.pop()


def tmp_undo_stack(undo_point_id):
    """Context manager to temporarily clear the ID stack."""
    global id_stack
    global stack_holder
    stack_holder[undo_point_id] = id_stack[:]
    for _ in stack_holder[undo_point_id]:
        imgui.pop_id()
    id_stack = []


def redo_stack(undo_point_id):
    """Restore the ID stack to a previously saved state."""
    global id_stack
    global stack_holder
    if undo_point_id in stack_holder:
        saved_stack = stack_holder.pop(undo_point_id)
        for uid in saved_stack:
            imgui.push_id(str(uid))
        id_stack = saved_stack


def get_melty_state():
    global melty_state_registry, static_melty
    return static_melty


def get_draw_state(unique: int) -> DrawState:
    """Get or create a ViewState object for a widget ID."""
    registry = None
    if Melty.vis is not None:
        registry = Melty.vis.root.draw_state_registry
    elif Melty.draw_state_registry is not None:
        registry = Melty.draw_state_registry

    if registry is None:
        # Headless / background thread - use a module-level fallback
        registry = _headless_draw_state_registry
        if Melty.draw_state_registry is None:
            Melty.draw_state_registry = registry

    if unique not in registry or registry[unique] is None:
        registry[unique] = DrawState()
        registry[unique].unique = unique

    registry[unique].dlt_count = Melty.save_draw_state_for
    return registry[unique]


_headless_draw_state_registry = {}

# kwargs the wrapper never copies onto ds_kwargs (see the wrapper's
# `ds_kwargs` build) - a frozenset so the per-call check is one pass.
_DS_KWARGS_EXCLUDE = frozenset((
    "input_value", "wanted_params", "depth", "shadow_depth",
    "name", "z_offset", "use_cache", "active_layer", "auto_resize",
    "unique", "suffix", "collection", "expanded_rect", "z_pos", "bg_offset",
    "max_bg_depth", "max_bg_value",
    "meta", "next_kwargs", "param_types"))


def _restamp_kwargs(draw_state, kwargs):
    """draw_state._kwargs = kwargs, breaking the PREVIOUS dict's self-cycle
    first. Every wrapper call sets kwargs['next_kwargs'] = kwargs (the
    header/child plumbing below), which makes each superseded kwargs dict
    — holding input_value, i.e. a whole tensor generation for a live value
    window — unreachable-but-cyclic garbage the moment the next render
    replaces it. The studio runs with gen2 collection effectively off
    (gc_manager), so those dicts lived for the session: 31 GB of retired
    generations reclaimed by a single gc.collect() when this was found.
    Popping the self-reference lets refcounting free the old dict at once."""
    prev = draw_state.__dict__.get("_kwargs")
    if prev is not None and prev is not kwargs and type(prev) is dict:
        prev.pop("next_kwargs", None)
    draw_state._kwargs = kwargs


def release_input_refs(draw_state):
    """Drop every wrapper-owned reference a draw_state holds to the value it
    rendered: the input slots the render_func wrapper fills per call
    (_input_value / _raw_input_value / _input_cache / _input_value_cache)
    and the dirty-detection baseline (_original_load_data). For views whose
    value is a GPU tensor these are the references that keep the tensor —
    and its VRAM — alive after the view is gone; on_cleanup hooks call this
    so torch.cuda.empty_cache() has something to return. The draw_state
    stays valid: a later render simply refills the slots."""
    draw_state._input_value = UNSET_VALUE
    draw_state._raw_input_value = UNSET_VALUE
    draw_state._input_value_cache = UNSET_VALUE
    draw_state._input_cache = {"external_state": (UNSET_VALUE, 0, -1),
                               "internal_state": (UNSET_VALUE, 0)}
    draw_state._original_load_data = None
    kw = draw_state.__dict__.get("_kwargs")
    if type(kw) is dict:
        kw.pop("next_kwargs", None)      # the self-cycle (see _restamp_kwargs)


def run_cleanup_callbacks():
    """Melty.cleanup: call every @render_func(on_cleanup=fn) hook once per
    draw_state that last rendered through that view. Runs BEFORE the GL /
    GLState teardown so a hook can still release GL-backed resources through
    the normal queue, and before torch.cuda.empty_cache() so severed tensor
    references actually free VRAM. A hook that throws is reported and skipped
    — teardown never stops on one view. Returns the number of hooks run."""
    registries = []
    try:
        if Melty.vis is not None and getattr(Melty.vis, "root", None) is not None:
            registries.append(Melty.vis.root.draw_state_registry)
    except Exception:
        pass
    if Melty.draw_state_registry is not None:
        registries.append(Melty.draw_state_registry)
    registries.append(_headless_draw_state_registry)
    seen = set()
    ran = 0
    for reg in registries:
        if not isinstance(reg, dict):
            continue
        for ds in list(reg.values()):
            if ds is None or id(ds) in seen:
                continue
            seen.add(id(ds))
            fn = getattr(getattr(ds, "_view_func", None), "__on_cleanup__", None)
            if fn is None:
                continue
            try:
                fn(ds)
                ran += 1
            except Exception as e:
                print(f"[melty] on_cleanup {getattr(fn, '__qualname__', fn)} failed: {e!r}")
    return ran


def strhash(s: str) -> int:
    """Stable 32-bit hash of a string."""
    return zlib.crc32(s.encode("utf-8")) & 0xffffffff


def combine(h: int, s: str) -> int:
    """Order-sensitive, stable combine (FNV-style)."""
    return ((h * 16777619) ^ strhash(s)) & 0xffffffff


def ui_id(suffix=None, idx=0) -> int:
    """
    Generate a stable UI ID from the call stack + optional metadata.

    - meta: optional Meta object to fold in attribute name/type
    - max_depth: limit to avoid walking the whole interpreter stack
    """
    # Pure function of (suffix, idx) - memoized: every wrapper call builds
    # its suffix string and hashed it twice (crc32 + str round trip).
    memo_key = (suffix, idx)
    hit = _UI_ID_MEMO.get(memo_key)
    if hit is not None:
        return hit
    h = 0
    # frame = sys._getframe(2)  # skip ui_id itself
    code = None
    func_name = ""
    cls_name = ""

    scope = f"{cls_name}.{func_name}" if cls_name else func_name
    h = combine(h, scope)
    # datatype = datatype if datatype is not None else Any
    # h = combine(h, str(datatype))
    suffix_int = strhash(str(suffix))

    unique = h if suffix is None else (((h * 16777619) ^ suffix_int) + (idx + 1))
    unique = strhash(str(unique))

    if len(_UI_ID_MEMO) > 65536:
        _UI_ID_MEMO.clear()     # bounded: dynamic names (table-row ids) churn
    _UI_ID_MEMO[memo_key] = unique
    return unique


_UI_ID_MEMO = globals().get("_UI_ID_MEMO", {})    # (suffix, idx) → stable id


class WrapType(Enum):
    CHILD = 1
    ID = 2
    GROUP = 3
    WINDOW = 4


# Hotkey decorator
def listens_for(hotkey):
    def decorator(func):
        Melty.hotkey_registry[func] = Melty.hotkey_registry.get(func, {})
        if isinstance(hotkey, (list, tuple)):
            for hk in hotkey:
                Melty.hotkey_registry[func][hk.name] = hk
        elif isinstance(hotkey, Hotkey):
            Melty.hotkey_registry[func][hotkey.name] = hotkey
        return func

    return decorator


def annotation_track(first_arg=None, *, wrapper, call_kwargs=None):
    """Capture a ``@render_func`` view function that is being used as an
    annotation (rather than rendered) at class-definition time.

    Three shapes are handled:
      * ``@view(for_type=T)`` on a class — deferred decorator, registers a type
        default once the class arrives.
      * ``@view`` on a class — registers that class as its own type default.
      * ``field: view`` / ``field: view(**kwargs)`` — returns an
        ``AnnotationOverride`` carrier; ``FieldMeta`` registers it per-attribute.
    """
    if not Melty.in_annotation_mode():
        return None

    call_kwargs = dict(call_kwargs or {})

    # @view(for_type=T) used as a class decorator: the class hasn't arrived yet.
    if 'for_type' in call_kwargs and not isinstance(first_arg, type):
        def class_wrapper(cls):
            return annotation_track(cls, wrapper=wrapper, call_kwargs=call_kwargs)

        return class_wrapper

    # @view / @view(for_type=T) decorating a class: register a type default,
    # the same maps is_default_for writes to.
    if isinstance(first_arg, type):
        for_type = call_kwargs.get('for_type', first_arg)
        Melty.default_funcs_by_type[for_type] = wrapper
        if isinstance(for_type, type):
            Melty.default_funcs_by_name[for_type.__name__] = wrapper
        elif isinstance(for_type, str):
            Melty.default_funcs_by_name[for_type] = wrapper
        return first_arg

    # View function used as a field annotation (bare or called with kwargs).
    # The call-time kwargs are considered per-field overrides.
    return AnnotationOverride(wrapper, call_kwargs)


def apply_drag_and_drop():
    ######## -------- apply drag & drop -----------
    did_apply = False
    while len(Melty.actions_to_apply) > 0:
        action = Melty.actions_to_apply.pop(0)
        result = apply_collection_action(action)
        if action.source_collection == action.target_collection:
            Melty.cache.invalidate_up_by_obj(action.source_collection, max_depth=8)
        else:
            Melty.cache.invalidate_up_by_obj(action.source_collection, max_depth=8)
            Melty.cache.invalidate_up_by_obj(action.target_collection, max_depth=8)
        did_apply = True

    Melty.actions_to_apply = []
    if did_apply:
        request_render()


def get_resize_handle(a_ds):
    if a_ds.left is None or a_ds.height is None or a_ds.width is None or a_ds.top is None:
        return (0, 0, 0, 0)
    left = a_ds.left
    top = a_ds.top
    right = left + a_ds.width
    bottom = top + a_ds.height

    right, bottom = Melty.apply_clip((right, bottom))

    margin = 34
    left, top = (right - margin, bottom - margin)
    # Never let the grab triangle climb into the header row: on a short window
    # it would sit over the close button (and win the press, being a
    # priority_delta=1 drag sub plus an invisible button), leaving close
    # unclickable. The visible triangle is only 13px, so a clamped rect
    # still covers it.
    header_bottom = a_ds.top + (a_ds.header_height or 0)
    top = max(top, min(header_bottom, bottom))
    return left, top, right, bottom


def draw_resize_handle(a_ds):
    if a_ds.width is None:
        return
    if a_ds.height is None:
        return
    if a_ds.left is None:
        return

    rect_br = get_resize_handle(a_ds)
    draw_list = imgui.get_window_draw_list()
    width = rect_br[2] - rect_br[0]
    height = rect_br[3] - rect_br[1]
    right, bottom = (rect_br[2], rect_br[3])
    left, top = (right - width, bottom - height)

    if width <= 0 or height <= 0:
        return

    current_cursor = imgui.get_cursor_screen_pos()
    if a_ds.expanded:
        imgui.set_cursor_screen_pos((left, top))
        imgui.invisible_button(str(a_ds.unique) + "resize_btn", width, height)

    alpha = 0.0
    if imgui.is_mouse_hovering_rect(left, top, right, bottom):
        Melty.blocker_hovered = True
        alpha = 0.5

    # Resizeable corner drag
    arrow_size = 13
    margin = 1
    draw_list.add_triangle_filled(
        right - margin - 1, bottom - arrow_size - margin,
        right - margin - 1, bottom - margin,
        right - margin - 1 - arrow_size, bottom - margin,
        pack_color(1, 1, 1, alpha)
    )
    # Bottom corner
    if alpha > 0.0:
        Melty.cache.mask_mark_rect(a_ds, Melty.max_depth - 1, a_ds.shadow_depth,
                                   right - arrow_size - margin - 1,
                                   bottom - margin - arrow_size, arrow_size, arrow_size,
                                   key=str(a_ds.unique) + "resize")
    if a_ds.expanded:
        imgui.set_cursor_screen_pos(current_cursor)

    return (left, top, right, bottom)


def jet_color(val: float):
    # jet color function
    four_value = 4.0 * val
    r = min(four_value - 1.5, -four_value + 4.5)
    g = min(four_value - 0.5, -four_value + 3.5)
    b = min(four_value + 0.5, -four_value + 2.5)
    return max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, b)), 1.0