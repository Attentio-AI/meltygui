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
without a click. SIMPLE builtin values (int/float/str/bool/tuple of ≤4
scalars/enum members, never None) skip the window entirely and render as an
inline pill drawn directly over the symbol in the editor's font
(_inline_value_text + the inline block in draw_live_view_marker) — for
explicit live_view() tokens AND snapshot markers alike. An inline marker has
no value window at all (no auto-open, preview, or double-click; the gutter
shows an inert info glyph instead of the magnifier). Snapshot/param markers (every captured assignment from an
instrumented run) start closed and open on box click, so a run doesn't bury
the code under one window per local. Sites the dict conversion can't surface
(while/with/match bodies — line-keyed fallback stores) have no CallParse token
to anchor and get no marker yet.
"""

import bisect
import enum
import inspect
import re
import sys
import time
import weakref

import imgui
from imgui.core import _DrawList

from src.lsd.gl_gui.model.core_model.draw_state import Anchor, Pin
from src.lsd.gl_gui.modes import Modes
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_conversion.live_view import (
    live_values_for, label_for, site_for_line, watch, install_builtin,
    auto_dim_names_for, RerunHint)
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow
from src.lsd.gl_gui.view.core_views import live_usage

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


_NO_VALUE = object()     # "window has received no value yet" sentinel


def _display_key(key):
    """Human title for one key-path element: a `line:N#name` key (frame
    snapshots / twin_snap stamps) reads as its token name, not the line
    number. Display only — stable IDs keep the raw key."""
    key = str(key)
    if key.startswith("line:") and "#" in key:
        return key.split("#", 1)[1]
    return key


def _split_line_key(seg):
    """(label, line) for a `line:N#name` segment, (None, None) otherwise."""
    seg = str(seg)
    if not seg.startswith("line:"):
        return None, None
    head, _, label = seg.partition("#")
    try:
        return label, int(head[5:])
    except ValueError:
        return label, 0



def _stable_key_name(key_path, all_keys=None):
    """Identity form of a store key path for VIEW NAMES — the LINE NUMBER is
    STRIPPED: `line:12#r` reads as `r`. The name is hashed into the render
    unique ID, so a raw line-keyed name gives the same code line a NEW view
    identity every time a rerun/edit re-stamps its line — leaking the old
    marker/window draw_states and colliding IDs across runs. The label IS the
    code line's stable id; repeat sites of the same label stay sibling-
    distinct via their ordinal among same-label keys in `all_keys` (the
    store's key set), ranked by stamped line — the ORDER survives the line
    shifts the raw numbers don't. Bare `line:N` keys rank under the empty
    label the same way."""
    parts = []
    for i, seg in enumerate(key_path):
        label, line = _split_line_key(seg)
        if label is None:
            parts.append(str(seg))
            continue
        ordinal = 0
        if all_keys is not None:
            lines = []
            for k in all_keys:
                if len(k) > i:
                    lb, ln = _split_line_key(k[i])
                    if lb == label and ln is not None:
                        lines.append(ln)
            if line in lines:
                ordinal = sorted(lines).index(line)
        parts.append(label if ordinal == 0 else f"{label}~{ordinal}")
    return "/".join(parts)


def _stable_key_names(all_keys):
    """{key_path: line-free name} for a WHOLE store snapshot in one pass.
    The per-key _stable_key_name ordinal scan is O(store), which made naming
    O(store²) per overlay pass on frame-snapshot stores (thousands of keys —
    the draw_text slowdown). Grouping ordinals per (position, label) once
    produces identical names at O(n log n); callers cache the map on their
    existing invalidation signals (the anchor index / a per-store memo), so
    steady-state naming is a dict hit."""
    groups = {}
    for k in all_keys:
        for i, seg in enumerate(k):
            label, line = _split_line_key(seg)
            if label is not None:
                groups.setdefault((i, label), []).append(line)
    ranks = {}
    for (i, label), lines in groups.items():
        for o, ln in enumerate(sorted(lines)):
            ranks.setdefault((i, label, ln), o)   # dup lines keep same rank
    out = {}
    for k in all_keys:
        parts = []
        for i, seg in enumerate(k):
            label, line = _split_line_key(seg)
            if label is None:
                parts.append(str(seg))
            else:
                o = ranks.get((i, label, line), 0)
                parts.append(label if o == 0 else f"{label}~{o}")
        out[k] = "/".join(parts)
    return out


def _inline_value_text(value, max_chars=48):
    """Format a simple builtin value for the marker's INLINE label, or None
    when the value isn't simple enough (those keep the popover window).
    Simple: bool, int, float, str, enum members, and tuples of up to 4 such
    scalars. `max_chars` caps a string's printed length (ellipsis past it) —
    the label floats over code, so it must stay short."""
    if value is None:
        return None     # None is NOT on the supported list - no label
    if isinstance(value, RerunHint):
        # The parked 'Rerun to visualize ...' placeholder is an affordance,
        # not a captured value - it keeps the popover, not a label.
        return None
    # bool before int (bool IS-A int), Enum before int (IntEnum members).
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, str):
        text = repr(value)
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return text
    if isinstance(value, tuple) and len(value) <= 4:
        parts = []
        for item in value:
            part = (None if isinstance(item, tuple)
                    else _inline_value_text(item, max_chars))
            if part is None:
                return None
            parts.append(part)
        tail = "," if len(value) == 1 else ""
        return "(" + ", ".join(parts) + tail + ")"
    return None


def _paint_value_pill(inline_text, span_x, text_y, span_width=None,
                      allow_overflow=False, fill=False, outline_rect=None):
    """The shared inline-value pill on the window draw list, in the CURRENT
    (editor) font, over the code span [span_x, span_x + span_width).

    Placement rules: a value narrower than the span RIGHT-ALIGNS on it, so
    the span's FIRST characters peek through beside it; a wider value
    either grows right past the span (`allow_overflow` — the span is the
    last code on its line, nothing there to cover) or is elided with an
    ellipsis to the span's width. `fill=True` stretches the card over the
    whole span regardless of the value's width (a live_view() call token is
    instrumentation, not code worth peeking at). span_width=None is the
    simple left-anchored unlimited pill (no span geometry known).

    `outline_rect` (x0, y0, x1, y1) rings the ASSOCIATED TOKEN — the symbol
    the value belongs to — in the label's own green, tying the two together
    visually (the pill can sit at the RHS end, far from its symbol).

    Fixed design colours, dark FOREST green on purpose — a live value must
    read as data the run produced, not as more source code."""
    # [tint=(0.36, 0.85, 0.46)] pad_x = 3.0
    pad_x = 3.0
    pad_y = 1.0
    shadow_offset = 2.0
    corner_radius = 4.0
    text_size = imgui.calc_text_size(inline_text)
    if (span_width is not None and text_size.x > span_width
            and not allow_overflow):
        while inline_text and imgui.calc_text_size(
                inline_text + "…").x > span_width:
            inline_text = inline_text[:-1]
        if not inline_text:
            return              # not even one character fits - draw nothing
        inline_text += "…"
        text_size = imgui.calc_text_size(inline_text)
    if span_width is not None and text_size.x <= span_width:
        text_x = span_x + span_width - text_size.x      # right-aligned
    else:
        text_x = span_x
    card_left = span_x if (fill or text_x == span_x) else text_x
    card_right = text_x + text_size.x
    if fill and span_width is not None:
        card_right = max(card_right, span_x + span_width)
    box_x = card_left - pad_x
    box_y = text_y - pad_y
    box_width = (card_right - card_left) + 2 * pad_x
    box_height = text_size.y + 2 * pad_y
    add_shadow((box_x, box_y, box_width, box_height),
               offset=shadow_offset, corner_radius=corner_radius)
    draw_list: _DrawList = imgui.get_window_draw_list()
    draw_list.add_rect_filled(
        box_x, box_y, box_x + box_width, box_y + box_height,
        imgui.get_color_u32_rgba(0.085, 0.145, 0.055, 0.96),
        rounding=corner_radius)
    draw_list.add_text(text_x, text_y,
                       imgui.get_color_u32_rgba(0.58, 0.78, 0.44, 0.95),
                       inline_text)
    if outline_rect is not None:
        draw_list.add_rect(outline_rect[0], outline_rect[1],
                           outline_rect[2], outline_rect[3],
                           imgui.get_color_u32_rgba(0.58, 0.78, 0.44, 0.6),
                           rounding=corner_radius)


# Assignment operators the inline binding pill replaces the right side of:
# augmented forms first (so `+=` doesn't read as a bare `=`), walrus, then a
# bare `=` that is neither ==/<=/>=/!= nor the tail of an augmented form.
_ASSIGN_OP_RE = re.compile(
    r"\*\*=|//=|>>=|<<=|[+\-*/%&|^@]=|:=|(?<![=<>!+\-*/%&|^@:])=(?!=)")


def _code_end_col(line_text):
    """Column where the line's CODE ends: before an inline # comment
    (quote-aware scan, so a '#' inside a string literal doesn't count) and
    before trailing whitespace."""
    quote = None
    i = 0
    n = len(line_text)
    while i < n:
        ch = line_text[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#":
            return len(line_text[:i].rstrip())
        i += 1
    return len(line_text.rstrip())


def _rhs_span(line_text, from_col):
    """(start_col, end_col) of the assignment's right-hand side on
    `line_text`, searching for the assignment operator from `from_col` (the
    boxed symbol's end) — so `seq_len = input_ids.shape[1]` pills over the
    RHS and reads `seq_len = 301`. None when the line has no assignment
    after the symbol (bare live_view() calls, expression statements) or the
    RHS continues on the next line with nothing on this one."""
    match = _ASSIGN_OP_RE.search(line_text, from_col or 0)
    if match is None:
        return None
    start = match.end()
    while start < len(line_text) and line_text[start] == " ":
        start += 1
    end = _code_end_col(line_text)
    if end <= start:
        return None
    return start, end


def _stacked_list_value(value, ds):
    """Display-side stacking: a LIST of ≥2 same-shape tensors/ndarrays
    renders as ONE stacked tensor (leading dim = list index) — covering a
    raw captured list (`hiddens = output.hidden_states`), an accumulator
    that fell back to its list path, and stores built by older code. Dtype/
    device drift is coerced to the first element's. Anything else (ragged,
    mixed kinds, non-tensor lists) passes through untouched. Memoized on
    the marker's draw_state — keyed on the list's identity, length, and
    first/last element identity, so a rollover overwrite or an append
    rebuilds while steady-state re-renders are a tuple compare."""
    if not (isinstance(value, list) and len(value) >= 2):
        return value
    kind = type(value[0]).__name__
    if kind not in ("Tensor", "ndarray"):
        return value
    first = value[0]
    if not all(type(v).__name__ == kind
               and tuple(v.shape) == tuple(first.shape) for v in value):
        return value
    key = (id(value), len(value), id(value[0]), id(value[-1]))
    cached = getattr(ds, "_lv_stack_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        if kind == "Tensor":
            import torch
            stacked = torch.stack([v.detach().to(first.device, first.dtype)
                                   for v in value])
        else:
            import numpy as np
            stacked = np.stack(value).astype(first.dtype, copy=False)
    except Exception:
        return value
    ds._lv_stack_cache = (key, stacked)
    return stacked


def _merged_dim_names(auto_dims, user_dims):
    """Combined dim names for an accumulated loop-site value: the auto loop
    names (leading stacked dims) followed by the site's own `# [dim_names=…]`
    (the per-iteration value's dims) — ('l_idx', 'head', 'query', 'key').
    Round-trip stable: a user list that ALREADY starts with the auto names
    (e.g. a merged list written back into the comment by a panel edit) is
    returned as-is instead of gaining a second copy of the loop dims."""
    user = ([user_dims] if isinstance(user_dims, str)
            else [str(d) for d in (user_dims or ())])
    auto = [str(d) for d in auto_dims]
    if user[:len(auto)] == auto:
        return user
    return auto + user


def _padded_dim_names(user_dims, ndim):
    """dim_names padded to the value's dim count: a list with too few names
    (or none at all) gains positional `dim<i>` entries for the unnamed
    trailing axes, so every axis still gets a picker tab and an edge label.
    `i` is the actual axis index — matching the `dim{i}` fallbacks the voxel
    view already uses for out-of-range axes. Returns None when the names
    already cover ndim (no change needed)."""
    names = ([user_dims] if isinstance(user_dims, str)
             else [str(d) for d in (user_dims or ())])
    if not ndim or len(names) >= ndim:
        return None
    return names + [f"dim{i}" for i in range(len(names), ndim)]


def _override_owner(scope_node, lookup_key):
    """The dict that OWNS statement `lookup_key` — the node whose
    __overrides__ carries the site's `# [...]` comment — searched
    breadth-first through surfaced BLOCK children (for/if/try branches, via
    libcst_conversion's _is_block_key), so a loop-body site links its
    comment exactly like a top-level one: the marker reads comment kwargs
    from it, and live_root points set_anywhere's lazy entry at the same
    level the save patcher writes back. Never descends into non-block dicts
    (nested defs, CallParse args — their names live in other scopes).
    Returns scope_node itself when the key isn't surfaced anywhere (sites
    in with/while bodies — the conversion has no node to hang a comment
    on)."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
        _is_block_key)
    queue = [scope_node]
    for node in queue:
        if not isinstance(node, dict):
            continue
        ovs = node.get("__overrides__")
        if (lookup_key in node
                or (isinstance(ovs, dict) and f"__{lookup_key}__" in ovs)):
            return node
        for k, v in node.items():
            if isinstance(v, dict) and _is_block_key(k):
                queue.append(v)
    return scope_node


def _is_funcdef_node(node):
    """A def's parse from EITHER parser: a FunctionParse (core_syntax stamps no
    __cst__) or a libcst FunctionDef-backed dict. isinstance, never the class
    NAME: the code host's held tree is reclassed in place to
    `Bubbling_FunctionParse` (bubbling.py), which is what the overlay walk
    hands this function — a name check returned False for every def and the
    snapshot overlay never drew a single live view (08-25)."""
    if not isinstance(node, dict):
        return False
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import FunctionParse
    if isinstance(node, FunctionParse):
        return True
    return type(node.get("__cst__")).__name__ == "FunctionDef"


def _def_name(node):
    """The def's name: `def_name` (both parsers stamp it) or the libcst node's."""
    name = getattr(node, "def_name", None)
    if name is not None:
        return name
    cst_node = node.get("__cst__") if isinstance(node, dict) else None
    return getattr(getattr(cst_node, "name", None), "value", None)


def _is_def_parse(node, def_name):
    return _is_funcdef_node(node) and _def_name(node) == def_name


def _find_def_node(tree, def_name, near_line):
    """The def parse named `def_name` in `tree` (a module or span parse).
    Module-level defs are a direct key lookup; otherwise (methods, nested
    defs) a plain dict walk, and when the name repeats (same-named methods
    on two classes) the one whose span starts nearest `near_line`. The
    caller memoizes per tree."""
    direct = tree.get(def_name) if isinstance(tree, dict) else None
    if _is_def_parse(direct, def_name):
        return direct
    best, best_d = None, None
    stack, seen = [tree], set()
    while stack:
        node = stack.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        if _is_def_parse(node, def_name):
            sp = getattr(node, "span", None)
            d = abs((getattr(sp, "start_line", 0) or 0) - (near_line or 0))
            if best is None or d < best_d:
                best, best_d = node, d
        for v in node.values():
            if isinstance(v, dict):
                stack.append(v)
    return best


def _host_held_tree(node):
    """The tree the owning code host CURRENTLY holds — the one its chain_out
    serializes — reached from ANY generation of the site's dict: every
    parse dict keeps `_bubble_root` (the dict RenderHost) after a reparse
    orphans it, and the host's `_held()` is the live tree. None when the
    node isn't host-backed (tests, plain parses)."""
    root = getattr(node, "_bubble_root", None)
    held = getattr(root, "_held", None)
    if callable(held):
        try:
            cur = held()
        except Exception:
            return None
        if isinstance(cur, dict):
            return cur
    return None


def current_live_root(ds):
    """The dict that owns a live site's `# [...]` comment, resolved against
    the tree the code host CURRENTLY holds — for the marker's own comment
    splat, set_anywhere's `# [<key>]` source row, and the replayed window's
    comment re-splat in Melty.draw.

    Why not the stamped `live_root` (or the editor's code_dict kwarg): every
    reparse (incremental merges included) REPLACES the def dict, its block
    dicts and their __overrides__. The marker re-stamps only when it renders
    (an open window whose def is scrolled off-viewport is pruned whole by
    the overlay walk), and the editor's code_dict is whatever the cached
    tabs body last captured from the host — both lag the host's held tree
    by an arbitrary number of frames. A panel edit then wrote into an
    orphan: the replay re-splatted the value from it (the UI moved), the
    host serialized its OWN tree without it, and the next fresh render
    handed the window the real tree — the value snapped back and
    anywhere_value's cross-check fired ("settled at 1, not the TensorDim(3)
    that was set"), most often when one write's round trip overlapped the
    next write. Resolving through the host puts the write where the save
    reads, whatever the editor happens to be showing.

    Anchor: the stamped dict's `_bubble_root` → host → `_held()`; the
    editor's code_dict/code_tree is the fallback when the site isn't
    host-backed, then the stamp itself. Memoized per tree on the ds (tree
    identity + its `source` str — the content-free change signal) and kept
    across renders, so the def lookup runs once per reparse per ds."""
    d = ds.__dict__
    stamped = d.get("live_root")
    loc = d.get("_lv_locator")
    if loc is None:
        return stamped
    editor_ds, def_name, def_line = loc
    tree = _host_held_tree(stamped)
    if tree is None and editor_ds is not None:
        # draw_text params (auto-state markers): the spanned node tree
        # rides code_dict on the non-host route (code_tree rides the error
        # dict case) and code_tree on all chain routes.
        ed = editor_ds.__dict__
        tree = ed.get("code_dict")
        if not isinstance(tree, dict):
            tree = ed.get("code_tree")
    if not isinstance(tree, dict):
        return stamped
    src = getattr(tree, "source", None)
    cache = d.get("_lv_owner_cache")
    if (cache is not None and cache[0] is tree
            and (src is None or cache[1] is src)):
        return cache[2]
    node = _find_def_node(tree, def_name, def_line)
    scope = node.get("locals") if isinstance(node, dict) else None
    key = d.get("live_key")
    owner = stamped
    if isinstance(scope, dict) and isinstance(key, str):
        owner = _override_owner(scope, key)
        if owner is not stamped:
            # Keep the stamp fresh for anything still reading it raw.
            object.__setattr__(ds, "live_root", owner)
    object.__setattr__(ds, "_lv_owner_cache", (tree, src, owner))
    return owner


# First-spawn size estimates for the display clamps below: the real size
# only exists after the window's first render (the spawn must pass it).
# Height: voxel/value windows both land in this size. Width: matches
# LIVE_WINDOW's initial width in view.py, which IS the first-spawn width.
_SPAWN_EST_HEIGHT = 380.0
_SPAWN_EST_WIDTH = 400.0


def _left_of_window_pos(anchor_left, marker_x, marker_y=None, win_h=None,
                        win_w=None, gap=50.0, screen_margin=10.0):
    """Parent-relative window_pos that opens a spawned live-value window just
    to the LEFT of the draw text, vertically level with the marker — instead
    of on top of the code the marker sits in.

    `anchor_left` is the absolute x of the draw text's left edge (the editor
    draw_state's abs_left — NOT the enclosing window's edge, which can sit
    far left of the text in multi-pane layouts). The value window's RIGHT
    edge sits `gap` left of it, so its left edge needs `win_w` (the live
    width on reopen, the LIVE_WINDOW initial width on first spawn).

    window_pos is relative to the spawned window's parent (the same editor
    window), and a marker renders at the cursor (abs_left == marker_x when
    window_pos is 0), so the parent-origin x offset is exactly marker_x:
    subtract it from the target absolute left edge to land there. Keeping
    window_pos parent-relative means the value window then tracks the
    editor window as it moves. Returns None when there's nothing to anchor
    to (caller falls back to the default on-cursor placement).

    `marker_y` enables the display-bottom clamp: a marker near the screen
    bottom would otherwise spawn its window mostly below the display (the
    pinned-anchor bound in _pinned_base_y clamps to the EDITOR window's box,
    which can itself reach the display bottom, and window_pos is
    deliberately outside that bound). The y offset lifts the window just
    enough that `win_h` (the live height on reopen, an estimate on first
    spawn) fits above the display bottom, floored so the top never leaves
    the screen.

    The x offset gets the same treatment against the display's LEFT edge:
    a draw text flush against it would spawn the value window entirely
    off-screen. Floored so the left edge stays on screen — when the display
    can't fit both, keeping the left edge visible wins."""
    if anchor_left is None:
        return None
    disp = Core.melty.display_size
    est_w = win_w or _SPAWN_EST_WIDTH
    x_off = anchor_left - gap - est_w - marker_x
    if disp:
        x_off = min(x_off, disp[0] - screen_margin - est_w - marker_x)
        x_off = max(x_off, screen_margin - marker_x)  # left edge on screen
    y_off = 0.0
    if marker_y is not None and disp:
        est = win_h or _SPAWN_EST_HEIGHT
        y_off = min(0.0, disp[1] - screen_margin - est - marker_y)
        y_off = max(y_off, -marker_y)          # keep the title bar on screen
    return (x_off, y_off)


def _token_in_selection(line, start_col, end_col, sel_lo, sel_hi):
    """True when the editor selection is EXACTLY the token on buffer `line`
    spanning [start_col, end_col) ((line, col) tuples, None when the editor
    has no selection / isn't focused). Exact on purpose: only selecting the
    symbol and JUST the symbol (a double-click select) previews its value
    window — a sweep that happens to contain instrumented tokens must not
    pop windows over the text being selected."""
    if line is None or sel_lo is None or sel_hi is None:
        return False
    return sel_lo == (line, start_col) and sel_hi == (line, end_col)


def _line_in_selection(line, sel_lo, sel_hi):
    """True when buffer `line` (1-based) intersects the editor selection's
    line range — inline value pills hide there so the text being selected
    stays readable."""
    return (line is not None and sel_lo is not None and sel_hi is not None
            and sel_lo[0] <= line <= sel_hi[0])


def _draw_marker_at(editor_ds, pos, cursor_inside, token_span, token,
                    **marker_kwargs):
    """Draw a live-view marker at screen `pos` — unless its token lies inside
    the editor selection, in which case it is QUEUED on the editor and drawn
    by `flush_selected_markers` after the overlay walk. Only ONE selected
    token previews its value window at a time (the last one selected), and
    which one can only be decided once every selected token of the frame is
    known — deferring the calls keeps that decision lag-free (no frame where
    two previews show) and keeps all marker state inside the marker body.
    `token_span` = (buffer line, start_col, end_col) in the selection's
    coordinates; `token` is the marker's positional input_value."""
    if cursor_inside and editor_ds is not None:
        fc = Core.melty.frame_count
        if getattr(editor_ds, "_lv_sel_frame", None) != fc:
            editor_ds._lv_sel_frame = fc
            editor_ds._lv_sel_pending = []
        editor_ds._lv_sel_pending.append(
            (token_span, pos, token, marker_kwargs))
        return
    imgui.set_cursor_screen_pos(pos)
    draw_live_view_marker(token, cursor_inside=cursor_inside,
                          editor_ds=editor_ds, **marker_kwargs)


def _last_selected(pending, sel_caret):
    """Index into `pending` of the LAST-selected token: the one nearest the
    caret end of the selection. The caret is the moving end of every
    selection gesture (shift+arrows, drag, shift+click), so the token it
    sits nearest is the one the selection most recently grew over — and
    when the selection shrinks back off a token, the remaining nearest one
    takes over. Stateless, so it needs no memory of entry order. "Nearest" is
    in DOCUMENT order: on the caret's line the closer edge, on a line above
    the later token, on a line below the earlier one (a column gap only
    means something on the caret's own line). A missing caret falls to the
    last token in document order."""
    if sel_caret is None:
        return max(range(len(pending)),
                   key=lambda i: pending[i][0][:2])
    cl, cc = sel_caret

    def _key(i):
        line, c0, c1 = pending[i][0]
        if line == cl:
            return (0, min(abs(c0 - cc), abs(c1 - cc)))
        if line < cl:
            return (cl - line, -c1)     # above the caret: later = nearer
        return (line - cl, c0)          # below the caret: earlier = nearer
    return min(range(len(pending)), key=_key)


def flush_selected_markers(draw_state=None, sel_caret=None, **kwargs):
    """text_editor calls this once after the overlay walk (inside its
    cursor-neutral bracket): draw the tokens `_draw_marker_at`
    queued for this frame, passing cursor_inside=True to the elected one only
    — the others render exactly as if the caret had left them, closing any
    preview they were showing. The queue is dropped here whatever happens:
    its entries carry the captured values (tensors), so a lingering queue
    would pin a run's generation."""
    pending = getattr(draw_state, "_lv_sel_pending", None) if draw_state else None
    if draw_state is not None:
        draw_state._lv_sel_pending = None
    if not pending:
        return
    winner = _last_selected(pending, sel_caret)
    for i, (_span, pos, token, mk) in enumerate(pending):
        imgui.set_cursor_screen_pos(pos)
        draw_live_view_marker(token, cursor_inside=(i == winner),
                              editor_ds=draw_state, **mk)


def _marker_idle_skip(editor_ds, name, x, y, w, h, captured, cursor_inside,
                      store_obj, key_path, buffer_line, auto_open):
    """True when marker `name` is provably a NO-OP this frame, letting the
    overlay skip its ~90µs @render_func call entirely. An idle marker (not
    hovered, no caret inside, no open or pending value window, no hover/
    cursor edge left to clear) draws nothing — its only per-frame work is the
    gutter registration and the first-publish watch, both replicated here
    raw. The marker's draw_state comes from editor_ds._lv_marker_ds (stamped
    by the body), so the FIRST render of each marker always takes the full
    path to create it; steady state is a dict hit + a rect test."""
    reg = getattr(editor_ds, "_lv_marker_ds", None) if editor_ds else None
    mds = reg.get(name) if reg else None
    if mds is None:
        return False
    io = imgui.get_io()
    if x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h:
        return False
    if (cursor_inside or getattr(mds, "_lv_hovered", False)
            or getattr(mds, "_lv_cursor_in", False)):
        return False        # interaction, or cursor edge → body must observe
    wds = getattr(mds, "_lv_window_ds", None)
    if getattr(mds, "_lv_open", False) or (wds is not None and not wds.closed):
        return False        # open window streams values through the body
    if captured and auto_open and getattr(mds, "_lv_open", None) is None:
        return False        # first value seen → body must auto-open
    # Idle - replicate the body's cheap registrations and bail.
    # Dismissed-latch reset (the body does this while focused with the caret
    # outside the symbol - skipping every such frame would leave the cursor
    # preview permanently suppressed after one X-close).
    if (editor_ds is not None and Core.melty.text_focused_ds is editor_ds
            and getattr(mds, "_lv_cursor_dismissed", False)):
        mds._lv_cursor_dismissed = False
    if editor_ds is not None and buffer_line is not None:
        if getattr(editor_ds, "_lv_gutter_frame", None) != Core.melty.frame_count:
            editor_ds._lv_gutter_frame = Core.melty.frame_count
            editor_ds._lv_gutter_markers = {}
        editor_ds._lv_gutter_markers.setdefault(buffer_line, []).append(mds)
    watch(store_obj, key_path, mds, first_only=True)
    return True


def draw_live_view_overlay(x=0, y=0, w=0, h=0, draw_state=None, char_w=8.0,
                           line_px=20.0, node=None, span=None, root=None,
                           line_offset=0, jump_to=None, sel_lo=None,
                           sel_hi=None, **kwargs):
    """token_views overlay callback for CallParse nodes (plain function — the
    overlay pass calls it with raw screen coords, no render_func wrapper)."""
    if getattr(node, "func_name", None) != "live_view":
        return
    from src.lsd.gl_gui.toggles import Toggles
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
    # Per-store name-map memo: the snapshot scan is O(store), and this runs
    # per visible call token per frame - a frame-snapshot store made that
    # O(store²) per pass. Keyed by store identity (weakref-validated; a store
    # id could recycle) + key count; the map only shifts when the KEY set
    # does, and this is the same tolerance the anchor caches use.
    _memos = draw_state.__dict__.setdefault("_lvm_name_memos", {})
    _me = _memos.get(id(store_obj))
    if _me is None or _me[0]() is not store_obj or _me[1] != len(snap):
        if len(_memos) > 64:
            _memos.clear()
        _me = (weakref.ref(store_obj), len(snap), _stable_key_names(snap))
        _memos[id(store_obj)] = _me
    _mname = (f"lvm::{_store_name(store_obj)}"
              f"::{_me[2].get(key_path) or _stable_key_name(key_path)}")
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


def _ds_in_window(ds, win_ds, max_hops=64):
    """True when `ds` sits inside `win_ds`'s subtree. Walks BOTH up-links —
    the render-tree `_parent` chain and `parent_window` — because a deferred
    satellite (e.g. the voxel controls panel) parents to its window via
    parent_window while its `_parent` chain, stamped at queue time, isn't
    guaranteed to pass through the window after a root_draw_states
    re-dispatch. Identity-set + hop cap bound the walk (chains can
    self-parent at their root)."""
    seen, stack = set(), [ds]
    while stack and len(seen) < max_hops:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        if node is win_ds:
            return True
        seen.add(id(node))
        if node._parent is not node:
            stack.append(node._parent)
        stack.append(getattr(node, "parent_window", None))
    return False


def _mouse_in_window_tree(win_ds, mx, my):
    """True when (mx, my) is inside `win_ds`'s rect or any open window
    parented into its subtree — the value window's satellites (the voxel
    controls panel, a context menu) are separate root windows positioned
    OUTSIDE the window's own rect, so a press on them must count as
    engaging with the window."""
    def _hit(w):
        try:
            x, y = w.abs_left, w.abs_top
            return (x <= mx < x + (w.width or 0)
                    and y <= my < y + (w.height or 0))
        except Exception:
            return False
    if _hit(win_ds):
        return True
    for lst in Core.melty.root_draw_states.values():
        for w in lst:
            if (w is not win_ds and not w.closed and _hit(w)
                    and _ds_in_window(w, win_ds)):
                return True
    return False


# auto_state=False: every named param would otherwise be MIRRORED onto the
# draw_state (draw_state.value / .store_obj + the _auto_baseline copy) - so a
# marker that stops rendering (culled off-viewport, key pruned) would keep
# pinning its last tensor AND its last store-owning function (whose
# __live_values__ may hold gigabytes of a superseded run) through those
# mirrors. The marker writes none of its params, so it needs no auto-state.
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
        live_root = _override_owner(code_tree_node, lookup_key)
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
    if _merge_auto:
        _dlog = ("merge", tuple(_merge_auto), tuple(comment_args["dim_names"]))
    else:
        _dlog = ("skip", _auto_dims, _vkind,
                 tuple(comment_args.get("dim_names") or ()))
    if getattr(ds, "_lv_dims_log", None) != _dlog:
        ds._lv_dims_log = _dlog
        _shape = (tuple(getattr(value, "shape", ()))
                  if _vkind in ("Tensor", "ndarray") else ())
        if _dlog[0] == "merge":
            print(f"live_view dims: marker {key_path} shape={_shape} "
                  f"auto={_auto_dims} comment={_user_dims} "
                  f"-> window dim_names={comment_args['dim_names']}",
                  file=sys.stderr)
        else:
            print(f"live_view dims: marker {key_path} shape={_shape} "
                  f"kind={_vkind} auto={_auto_dims} -> MERGE SKIPPED "
                  f"(window gets dim_names={comment_args.get('dim_names')})",
                  file=sys.stderr)

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

    auto_open = comment_args.get("auto_open", auto_open)

    # ── INLINE VALUE: a simple builtin (int/float/str/bool/shortlist/enum)
    # renders as a text label overlapping the top of its code line instead
    # of a separate window. Both modes pass inline_values=True (explicit
    # live_view() tokens and instrumented-run snapshot markers alike):
    # every boxed symbol with a simple value shows up in place.
    inline_text = (_inline_value_text(value)
                   if captured and inline_values else None)
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
        from src.lsd.gl_gui.utils.glfw_utils import request_render
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
    from src.lsd.gl_gui.toggles import Toggles
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
    # Outline only under the mouse — the boxes read as clutter when every
    # instrumented symbol is permanently framed; hover reveals the
    # affordance. An inline marker skips it: its token wears the pill's
    # green ring instead (painted with the pill below), and there's no
    # window gesture for hover to advertise.
    if hovered and inline_text is None:
        dl: _DrawList = imgui.get_window_draw_list()
        dl.add_rect(x, y + 2, x + w, y + h - 3,
                    imgui.get_color_u32_rgba(*base, 0.9 if open_now else 0.6),
                    rounding=corner_radius)
    imgui.dummy(w, h)

    if inline_text is not None:
        # The label must repaint on every publish - the first_only watch
        # above fires once; this full watch invalidates per value.
        watch(store_obj, key_path, ds)
        # Caret on this line, or the line inside the text selection: show
        # the real code, nothing painted - typing or deleting under a pill
        # would be blind. The pill comes back when the caret/selection
        # leave (the kwarg change re-renders this).
        if ((caret_line is None or caret_line != buffer_line)
                and not in_selection):
            # Drawn IN PLACE in the editor's code font (the current font -
            # no push). For an assignment site the overlay hands inline_dx
            # + inline_span_w: the span is the RHS expression and the value
            # RIGHT-ALIGNS on it (`seq_len = input_ids.sha…[301]`, first
            # RHS characters to fall through). Without them (live_view()
            # call tokens, no `=` on the line) it sits on the boxed token
            # itself. The marker rect wraps the token with a 2 px pad, so
            # the token's text starts at x + 2.
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
            # The ring goes around the marker's entire TOKEN (the symbol) -
            # same rect the hover outline uses - so an inline pill far down
            # the RHS still points back at its symbol.
            _paint_value_pill(inline_text, text_x, text_y,
                              span_width=inline_span_w,
                              allow_overflow=inline_overflow,
                              fill=inline_fill,
                              outline_rect=(x, y + 2, x + w, y + h - 3))
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
        if open_now:
            _auto_run_on_user_open(editor_ds, store_obj)
        if open_now and win_ds is not None:
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
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
        # Named by the code line's STABLE name - the marker's own name (raw,
        # line number stripped), never the full line-keyed path and never
        # draw_state.line: the name hashes into the unique ID, so a line
        # number here re-identified the window every time a rerun/edit
        # re-stamped the site's line.
        win_kwargs = dict(
            name=f"{'/'.join(map(_display_key, key_path))}"
                 f"##lv::{ds.name}",
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
        # First creation: anchor the window to the LEFT of the editor's
        # window, always on top of the code, lifted clear of the editor
        # bottom (estimated height - the real one doesn't exist yet).
        # window_pos persists on the spawned window's draw_state
        # (parent-relative, so it tracks the editor window) - set once; user
        # drags it preserved after.
        if win_ds is None:
            pos = _left_of_window_pos(
                editor_ds.abs_left if editor_ds is not None else None,
                x, marker_y=y)
            if pos is not None:
                win_kwargs["window_pos"] = pos
        else:
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
        # kwargs (melty.py, "Live-view comment re-splat"), which would clobber
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
                from src.lsd.gl_gui.view.core_conversion.live_view import park_rerun_hint
                park_rerun_hint(store_obj, key_path)
            except Exception as e:
                print(f"live_view: park hint for {key_path} failed: {e!r}")
            try:
                from src.lsd.gl_gui.gc_manager import release_cuda_cache_soon
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


_VALUE_ATTRS = ("_raw_input_value", "_input_value", "_original_input_ref")
_VALUE_KWARGS = ("input_value", "value")


def _sever_value_pins(window_ds):
    """Under `window_ds`, cut every GLState-cached TORCH reference to the
    displayed value while keeping the cache entry's metadata: the
    cuda_march path's `cuda_view` (a CudaVolumeView over a strided view of
    the source) has its `.view` nulled and its key cleared, so the previous
    generation can die, yet draw_voxels' hold-last-frame path can still
    read source_shape/mapping off it to keep the slice sliders up. The FBO
    / last image / GL-path textures are display-GPU objects and stay."""
    from src.lsd.gl_gui.gl_state import GLState
    for state in GLState.states_under(window_ds):
        cv = state.peek("cuda_view")
        if cv is not None:
            try:
                cv.view = None
                cv._vol_key = None      # never a cache hit again
            except Exception:
                state.drop("cuda_view")


def release_live_value(ds, gl=True, keep_image=False):
    """Drop every reference a live-value VIEW holds to its captured value, so
    a tensor the store no longer serves can actually die.

    Draw_states persist (that's the framework contract — a closed window
    keeps its size/position/params and lazily re-renders), and the wrapper
    stamps the last-rendered value onto them (`_kwargs['input_value']`,
    `_raw_input_value`, ...). For the live lab that is the leak: each run
    publishes NEW tensors (the per-layer accumulators are hundreds of MB),
    so a marker whose key was pruned (a renamed/removed assignment — every
    few keystrokes while typing) or a value window the user X-closed kept
    its last tensor — AND its GLState volume texture — pinned for the rest
    of the session, one generation per orphan. VRAM climbed with every
    edit; torch.cuda.empty_cache() can't help while the refs live.

    Called when a key is pruned (live_view._prune_keys, for the marker and
    its window) and by the marker whenever its window is closed. The marker
    re-supplies the value on the next show (draw_any(value, draw_state=win)),
    so nothing is lost: only the STALE copy goes. GL resources under the
    window are released through the same path a deleted window takes
    (GLState.on_window_deleted — queued deletes, drained on the GL thread);
    the window lazily re-uploads when reopened. Safe from any thread (attr
    writes, queued GL deletes) and idempotent. `keep_image=True` (the
    fresh-run release) only severs the cached torch refs (_sever_value_pins)
    and leaves the FBO / last image, so the window holds its last frame
    until the new value arrives."""
    if ds is None:
        return
    targets = [ds]
    try:
        targets.extend(ds.descendants(max_depth=8))
    except Exception:
        pass
    from src.lsd.gl_gui.view.core_views.core_render import release_input_refs
    for d in targets:
        # The wrapper owns more refs than the obvious two: the offscreen
        # blit stamps `_input_value_cache` (mark_start_offscreen) and the
        # change detector keeps `_input_cache["external_state"]` - a closed
        # value window kept a 13 GB stack alive via exactly those.
        # release_input_refs is the wrapper's own complete list.
        try:
            release_input_refs(d)
        except Exception:
            pass
        for attr in _VALUE_ATTRS:
            if getattr(d, attr, None) is not None:
                try:
                    setattr(d, attr, None)
                except Exception:
                    pass
        kw = getattr(d, "_kwargs", None)
        if isinstance(kw, dict):
            for k in _VALUE_KWARGS:
                if kw.get(k) is not None:
                    kw[k] = None
    if gl:
        try:
            from src.lsd.gl_gui.gl_state import GLState
            if keep_image:
                _sever_value_pins(ds)
            else:
                GLState.on_window_deleted(ds)
        except Exception:
            pass


def _drop_captured_value(store_obj, key_path):
    """Forget one captured value's DATA on X-close: the loop accumulator is
    dropped, the store entry is swapped for the rerun hint, and every
    marker/window pin + GL texture is severed (live_view.park_rerun_hint).
    The KEY stays — the widget must keep rendering as captured so it can be
    reopened; the next run with it open refills it."""
    if store_obj is None or key_path is None:
        return
    try:
        from src.lsd.gl_gui.view.core_conversion.live_view import park_rerun_hint
        park_rerun_hint(store_obj, key_path)
    except Exception as e:
        print(f"live_view: park hint for {key_path} failed: {e!r}")
    # The tensor is unreferenced now; hand its blocks back to the system so
    # the VRAM actually drops (allocator cache → empty_cache), off-thread.
    try:
        from src.lsd.gl_gui.gc_manager import release_cuda_cache_soon
        release_cuda_cache_soon(label="live view close")
    except Exception:
        pass


def _auto_run_on_user_open(editor_ds, store_obj):
    """User opened a value window: with Auto Execute on for the def, the
    def widget recompiles + runs so the window fills (text_editor.
    fnrun_auto_run_on_open — coalesced there). Never raises."""
    try:
        from src.lsd.gl_gui.view.core_views.text_editor import (
            fnrun_auto_run_on_open)
        fnrun_auto_run_on_open(editor_ds, store_obj)
    except Exception as e:
        print(f"live_view: auto-run on open failed: {e!r}")


def set_marker_open(marker_ds, open_):
    """Gutter-button entry point: latch a marker's value window open/closed
    from OUTSIDE the marker body (raw draw-list button, no render_func).
    Mirrors the double-click toggle: flipping open snaps an existing window
    back to the left of the editor window (it may have been dragged onto
    the code). The marker ds is invalidated so its body re-runs and
    creates/hides the window on the next editor render; the CALLER must
    invalidate the editor tile itself (the marker only renders inside the
    editor's overlay pass)."""
    open_ = bool(open_)
    if bool(getattr(marker_ds, "_lv_open", False)) == open_:
        return
    marker_ds._lv_open = open_
    win_ds = getattr(marker_ds, "_lv_window_ds", None)
    if open_:
        _auto_run_on_user_open(getattr(marker_ds, "_lv_editor_ds", None),
                               (getattr(marker_ds, "_kwargs", None) or {}).get("store_obj"))
    if open_ and win_ds is not None:
        # Clear the window's stale closed flag NOW: this latch is set from
        # OUTSIDE the marker body (the gutter runs after a close left
        # closed=True), and the body's own "closed via the header X" check
        # (win_ds.closed and _lv_open) runs before it repaints the window -
        # without this reset it reads the previous close as a fresh X click
        # and cancels the reopen on the spot.
        win_ds.closed = False
        _ed = getattr(marker_ds, "_lv_editor_ds", None)
        pos = _left_of_window_pos(_ed.abs_left if _ed is not None else None,
                                  marker_ds.abs_left,
                                  marker_y=marker_ds.abs_top,
                                  win_h=win_ds.height,
                                  win_w=win_ds.width)
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
    if not _is_funcdef_node(node) or span is None:
        return
    from src.lsd.gl_gui.toggles import Toggles
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
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
            parse_def_name)
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
            imgui.get_color_u32_rgba(*_ml_col))
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
                imgui.get_color_u32_rgba(0.275, 0.118, 0.157, 0.922), 4.0)
            _rdl.add_rect(
                _bx0, _by0, _bx1, _by1,
                imgui.get_color_u32_rgba(0.588, 0.235, 0.275, 1.0), 4.0)
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
            or len(_ie) < 6 or _ie[4]() is not fn):
        # Append + ONE sort (C-speed): the first version insort-ed each key
        # (list.insert, O(n) memmove → O(n²) per rebuild), and a publish
        # storm - a stack-trace snapshot landing thousands of keys with
        # renders interleaved - meant a rebuild per render. That froze
        # the editor the moment a stack was published.
        _pairs = []
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
            _pairs.append((_rl, key_path))
        _pairs.sort(key=lambda p: p[0])
        # Stable hash-free cache for the whole snapshot, built WITH the index
        # (same invalidation) - per-key naming was O(store) hash → O(store²)
        # per pass on frame-snapshot stores.
        _ie = (_src, _ik, [p[0] for p in _pairs], [p[1] for p in _pairs],
               weakref.ref(fn), _stable_key_names(_snap_vals))
        object.__setattr__(draw_state, "_lv_key_index", _ie)
    _ilines, _ikeys, _skey_names = _ie[2], _ie[3], _ie[5]
    if (_clip is not None
            and getattr(draw_state, "_lv_full_overlay_until", 0)
            <= Core.melty.frame_count):
        _blo = int((_clip[1] - origin_y) / line_px) - 64
        _bhi = int((_clip[3] - origin_y) / line_px) + 65
        _i0 = bisect.bisect_left(_ilines, _blo)
        _i1 = bisect.bisect_right(_ilines, _bhi)
        _cand = _ikeys[_i0:_i1]
        _soc[1] = len(_ikeys) - len(_cand)
    else:
        # Post-run forward pass renders EVERY key in the loop - the per-key
        # cull below still drops off-viewport keys unless their marker has
        # an OPEN window (rendered at the frozen anchor, which forwards the
        # actual value into the window's draw_any).
        _cand = _ikeys

    for key_path in _cand:
        value = _snap_vals.get(key_path)
        anchor = _key_anchor(node, key_path, line_offset)
        if anchor is None:
            continue
        rel_line, start_col, end_col = anchor
        if end_col is None:
            # line:N keys carry a RUN-TIME line stamp - edits since the run
            # shift the code out from under line. The labeled SYMBOL is the
            # real anchor - if the stamped line no longer shows the label,
            # snap to the nearest line in this def that does (memoized above
            # during the index build, so this costs a dict hit).
            tail = key_path[-1]
            _mk = ("label", tail)
            _snapped = _snap_memo.get(_mk)
            if _snapped is not None:
                rel_line = _snapped
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
                    f"::{_skey_names.get(key_path) or _stable_key_name(key_path)}"
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
                    from src.lsd.gl_gui.perf_trace import trace as _ptr
                    _mreg2 = getattr(draw_state, "_lv_marker_ds", None) or {}
                    _mk2 = (f"lvs::{fn.__qualname__}::"
                            f"{_skey_names.get(key_path) or _stable_key_name(key_path)}")
                    _ptr("lv full-pass skip", key=_mk2,
                         have_marker=_mk2 in _mreg2,
                         reg_keys=len(_mreg2))
                _soc[1] += 1
                continue    # off-viewport - don't draw a marker for it
            _frozen_pos = (_fmds.abs_left, _fmds.abs_top)
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
        # Selection containment: _ml is in buffer-space, cols are the
        # boxed symbol span - same test the call-token overlay does.
        cursor_inside = _token_in_selection(
            _ml, start_col, end_col, kwargs.get("sel_lo"), kwargs.get("sel_hi"))
        _snm = (f"lvs::{fn.__qualname__}"
                f"::{_skey_names.get(key_path) or _stable_key_name(key_path)}")
        _sao = (bool(Toggles.TextEditor.live_auto_open_volumes)
                and is_volume(value) and key_path not in
                (getattr(fn, "__frame_snapshot_keys__", None) or ()))
        # An inline-labeled marker (simple builtin value) draws every buffer
        # repaint - the label bakes into the tile - so it never idle-skips.
        if (_frozen_pos is None
                and _inline_value_text(value) is None
                and _marker_idle_skip(
                    draw_state, _snm,
                    origin_x + start_col * char_w - pad, _my - pad,
                    max(1, end_col - start_col) * char_w + 2 * pad,
                    line_px + 2 * pad, True, cursor_inside,
                    fn, key_path, _ml - 1, _sao)):
            _soc[2] += 1
            continue
        _soc[3] += 1
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
        # comment source from it and splats it 1:1 onto the popup window's
        # draw_state (not onto the marker's own wrapper - show_bg=True with a
        # dark tint would draw an opaque bg over the very symbol it boxes).
        # Assignment sites pill on the RHS - the symbol stays visible and
        # the value RIGHT-ALIGNS on the RHS span (which runs to the line's
        # code end by construction), so the line reads
        # `seq_len = input_ids.sha…[301]` with the RHS's first characters
        # peeking through. The pill offset is a pixel delta from the boxed
        # symbol's start. A non-assignment site's pill right-aligns on its
        # token span the same way; either kind grows past its span only
        # when the marker ends the line's code.
        _rhs = _rhs_span(text, end_col)
        if _rhs is not None:
            _span_w = max(1, _rhs[1] - _rhs[0]) * char_w
            _overflow = True
        else:
            _span_w = max(1, end_col - start_col) * char_w
            _overflow = end_col >= _code_end_col(text)
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
            inline_dx=((_rhs[0] - start_col) * char_w if _rhs else 0.0),
            inline_span_w=_span_w, inline_overflow=_overflow,
            in_selection=_line_in_selection(_ml, kwargs.get("sel_lo"),
                                            kwargs.get("sel_hi")),
            caret_line=kwargs.get("caret_line"), def_node=node)
    # Inline USAGE labels: `seq_len=384` inserted after every later
    # occurrence of a captured symbol - the code text is SHIFTED to make
    # room (display-time only - see live_usage + the positional-trail
    # machinery in draw_text's _window/_build_vcols).
    try:
        _draw_usage_labels(draw_state, fn, node, span, source_lines,
                           _snap_vals, _ilines, _ikeys, origin_x, origin_y,
                           char_w, line_px, _lmap, _clip,
                           kwargs.get("col_shift", 0))
    except Exception as e:
        print(f"live_view: usage labels failed: {e!r}", file=sys.stderr)
    # TEMP perf: one line per slow enough pass (keys=store size for this
    # def, culled=off-viewport, idle=fast-id skips, drawn=full wrapper calls).
    _soms = (time.perf_counter() - _sot0) * 1000.0
    if _soms >= 2.0:
        from src.lsd.gl_gui.perf_trace import trace as _sotrace
        _sotrace("snapshot_overlay", fn=getattr(fn, "__qualname__", "?"),
                 ms=round(_soms, 1), keys=_soc[0], culled=_soc[1],
                 idle=_soc[2], drawn=_soc[3])


def _draw_usage_labels(draw_state, fn, node, span, source_lines, snap_vals,
                       anchor_lines, anchor_keys, origin_x, origin_y,
                       char_w, line_px, lmap, clip, col_shift=0):
    """Every later USAGE of a captured symbol reads `seq_len=384` — the
    value is INSERTED right after the symbol and the rest of the line
    shifts to make room, so name and value show together (the same
    grid-bending draw_text uses for inline color swatches).

    Two halves, one frame apart: this pass STAMPS the wanted gaps on the
    editor ds (`_lv_trail_views`: def start → (frame, {(display line0,
    buffer col): cells}); a change bumps `_lv_trail_gen` + invalidates, and
    draw_text's _window folds them into _build_vcols as positional trails
    on its next layout, publishing each gap's start CELL back in
    `_lv_trail_cells`) — and PAINTS the value pill into every gap already
    laid out. Raw draw-list paint: no draw_states, no hit rects (clicks
    fall through; caret/click math stays exact through vcols), no captures
    — each pill reads the binding's single store entry at draw time (see
    live_usage's module docstring). The occurrence index is memoized per
    (source, def, store size); the per-repaint work is one cheap loop over
    the def's occurrences. Stale defs' stamps are pruned by draw_text after
    the overlay pass. No caret/selection suppression here: the code text
    stays fully visible and closing the gap under an active caret would
    shift the line mid-edit."""
    from src.lsd.gl_gui.toggles import Toggles
    if not Toggles.TextEditor.live_inline_usages or not snap_vals:
        return
    memo = draw_state.__dict__.get("_lv_usage_memo")
    if memo is None or memo[0] is not source_lines:
        memo = (source_lines, {})
        object.__setattr__(draw_state, "_lv_usage_memo", memo)
    entry = memo[1].get(span.start_line)
    if entry is None or entry[0]() is not fn or entry[1] != len(snap_vals):
        labels = vars(fn).get("__live_labels__") or {}
        # Binding sites extracted from the store's reverse index: name →
        # (all binding lines, their store keys). The store IS the
        # binding registry - a usage can only ever show a captured value.
        bindings = {}
        for line, key in zip(anchor_lines, anchor_keys):
            name = live_usage.binding_name(key, labels)
            if name is None:
                continue
            binding_lines, binding_keys = bindings.setdefault(name, ([], []))
            binding_lines.append(line)
            binding_keys.append(key)
        # Nested defs: an occurrence inside a closure's body is that
        # scope's own name (or a closure read at a DIFFERENT time), never a
        # plain read of an outer binding - keep out.
        exclude = []
        _locals = node.get("locals") if isinstance(node, dict) else None
        if isinstance(_locals, dict):
            for child in _locals.values():
                if isinstance(child, dict) and _is_funcdef_node(child):
                    child_span = getattr(child, "span", None)
                    if child_span is not None:
                        exclude.append((child_span.start_line + 1,
                                        child_span.end_line))
        def_text = "\n".join(source_lines[span.start_line - 1:span.end_line])
        occurrences = live_usage.build_usage_index(
            def_text, span.start_line,
            {n: b[0] for n, b in bindings.items()}, tuple(exclude))
        entry = (weakref.ref(fn), len(snap_vals), occurrences, bindings)
        memo[1][span.start_line] = entry
    occurrences, bindings = entry[2], entry[3]
    frame = Core.melty.frame_count
    trails = draw_state.__dict__.setdefault("_lv_trail_views", {})
    publish_seq = vars(fn).get("__live_pub_seq__") or {}
    sub = {}          # (display line0, buffer boundary col) → gap cells
    paints = []       # visible pills, paint after the stamp below
    for line, col, name, _last in occurrences:
        _ml = lmap(line) if lmap else line
        if _ml is None:
            continue          # inside the mid-edit region - skip a wash
        binding_lines, binding_keys = bindings[name]
        key = live_usage.governing_key(binding_lines, binding_keys, line,
                                       publish_seq)
        if key is None or key not in snap_vals:
            continue
        pill_text = _inline_value_text(snap_vals.get(key))
        if pill_text is None:
            continue
        pill_text = "=" + pill_text     # reads as `seq_len=384`
        # A publish to the governing key must repaint usage pills even when
        # its binding marker sits off-viewport (culled, so its own full
        # watch never registered). Idempotent WeakSet add.
        watch(fn, key, draw_state)
        cells = len(pill_text) + 1      # one breathing cell around the value
        boundary_col = col + len(name) + col_shift
        sub[(_ml - 1, boundary_col)] = cells
        pill_y = origin_y + (_ml - 1) * line_px
        if clip is not None and (pill_y + line_px < clip[1]
                                 or pill_y > clip[3]):
            continue
        paints.append((_ml - 1, boundary_col, pill_text, len(name), pill_y))
    previous = trails.get(span.start_line)
    trails[span.start_line] = (frame, sub)
    if previous is None or previous[1] != sub:
        # New/changed gaps - relayout next frame (draw_text's _window clears
        # its cache on the stamp).
        draw_state._lv_trail_gen = getattr(draw_state, "_lv_trail_gen", 0) + 1
        draw_state.invalidate()
        from src.lsd.gl_gui.utils.glfw_utils import request_render
        request_render()
    # Paint into the gaps the CURRENT layout reserved (stamped back by
    # draw_text's _window). A gap not laid out yet - first frame after a
    # value appeared - skips painting; it opens on the very next layout.
    gap_cells = draw_state.__dict__.get("_lv_trail_cells") or {}
    base_x = origin_x - col_shift * char_w      # buffer cell 0 in px
    for line0, boundary_col, pill_text, name_len, pill_y in paints:
        gap_cell = gap_cells.get((line0, boundary_col))
        if gap_cell is None:
            continue
        gap_x = base_x + gap_cell * char_w
        # Ring around the SYMBOL (which sits right before the gap, one cell
        # per char), tying the inserted value to its name.
        _paint_value_pill(pill_text, gap_x + 3.0, pill_y,
                          outline_rect=(gap_x - name_len * char_w - 2.0,
                                        pill_y, gap_x + 1.0,
                                        pill_y + line_px - 1.0))


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


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _label_line_index(source_lines, lo, hi):
    """{identifier: sorted [1-based lines]} for every identifier appearing in
    the def's [lo, hi] span — one linear pass, built once per source version
    (memoized by the caller in the _src-keyed snap memo). Turns the per-key
    label relocation from an O(def lines) regex scan into a lookup."""
    idx = {}
    for ln in range(max(1, lo), min(len(source_lines), hi) + 1):
        for m in _IDENT_RE.finditer(source_lines[ln - 1]):
            idx.setdefault(m.group(), []).append(ln)
    return idx


def _snap_line_to_label(label, rel_line, source_lines, lo, hi, lidx=None):
    """Buffer line a line-keyed marker should anchor on: the stamped line if
    it still contains `label` (the common case — one regex on one line), else
    the NEAREST line in the def's [lo, hi] span that does. Pure string ops on
    the already-split source — the symbol reference is recovered from the
    line stamp without any reparse, so line-keyed captures follow their
    symbols through edits instead of staying pinned where the last run left
    them. No match anywhere (symbol renamed/removed — the value is stale and
    the next run prunes it) keeps the stamp unchanged.

    `lidx` (from _label_line_index) replaces the def-wide scan with a
    candidate lookup on the label's final identifier segment; candidates are
    tried nearest-first and verified with _label_box_span (dotted labels
    need their prefix checked)."""
    if not label:
        return rel_line
    if (1 <= rel_line <= len(source_lines)
            and _label_box_span(label, source_lines[rel_line - 1]) is not None):
        return rel_line
    if lidx is not None:
        cand = lidx.get(label.rsplit(".", 1)[-1].split("[", 1)[0])
        if not cand:
            return rel_line
        i = bisect.bisect_left(cand, rel_line)
        lo_i, hi_i = i - 1, i
        while lo_i >= 0 or hi_i < len(cand):
            _below = cand[lo_i] if lo_i >= 0 else None
            _above = cand[hi_i] if hi_i < len(cand) else None
            if _above is None or (_below is not None
                                  and rel_line - _below <= _above - rel_line):
                ln, lo_i = _below, lo_i - 1
            else:
                ln, hi_i = _above, hi_i + 1
            if _label_box_span(label, source_lines[ln - 1]) is not None:
                return ln
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


@window(initial={"width": 350, "height": 540}, tint=(0.16, 0.173, 0.19))
@render_func(tint=(0.40, 0.53, 0.78), auto_resize=False)
def live_view_forward(input_value=None, draw_state=None, **kwargs):
    from src.lsd.train.lsd_train import LSD
    from src.lsd.gl_gui.view.mode import Mode
    # NEW_CODE = the full two-pane code_file_io display: the draw_collection
    # structured (code_dict) pane AND the live-overlay text pane side by side.
    draw_function_live(LSD.full_forward_pass_live, name="run_forward_pass runner",
                       source_mode=Mode.NEW_CODE)


@window(initial={"width": 350, "height": 540}, tint=(0.132, 0.159, 0.18))
@render_func(tint=(0.36, 0.62, 0.66), auto_resize=False)
def attention_walkthrough(input_value=None, draw_state=None, **kwargs):
    """The minimal real forward pass over the selected model (see
    src/lsd/train/attention_walkthrough.py) as a live lab: Run executes the
    instrumented twin, and the per-layer loop accumulates every tensor into
    a leading `l_idx` stack — `scores` is the (l_idx, head, query, key)
    attention volume this window exists for."""
    from src.lsd.train.attention_walkthrough import attention_walkthrough_pass
    from src.lsd.gl_gui.view.mode import Mode
    draw_function_live(attention_walkthrough_pass,
                       name="attention_walkthrough runner",
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
                      name=f"{fn.__name__} runner", run_in_thread=run_in_thread, rounding=None)
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