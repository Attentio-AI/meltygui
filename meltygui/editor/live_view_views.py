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
import collections
import colorsys
import enum
import inspect
import re
import sys
import time
import weakref

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
from meltygui_imgui.core import _DrawList

from meltygui.state.new_core_model import Anchor
from meltygui.state.new_core_model import Pin
from meltygui.core.styling.fonts import Font
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
from meltygui.code.live_view import live_values_for
from meltygui.code.live_view import label_for
from meltygui.code.live_view import site_for_line
from meltygui.code.live_view import watch
from meltygui.code.live_view import install_builtin
from meltygui.code.live_view import auto_dim_names_for
from meltygui.code.live_view import RerunHint
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.window_decoration import window
from meltygui.core.cache.tile_cache import add_shadow
import meltygui.editor.live_usage as live_usage

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
    from meltygui.code.libcst_conversion import CallParse
    from meltygui.code.libcst_conversion import GeneralParse
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
    label the same way.

    A plain STATEMENT key with the same name at the same position —
    `('x',)` beside `('line:N#x',)`, a dict-visible assignment plus a
    line-keyed with/while-body site of the same local — claims ordinal 0
    and shifts every line-keyed ordinal up: the two used to collapse to
    ONE name, and colliding view names shared their marker/window
    draw_states (the draw_text garbled-overlay bug)."""
    parts = []
    for i, seg in enumerate(key_path):
        label, line = _split_line_key(seg)
        if label is None:
            parts.append(str(seg))
            continue
        ordinal = 0
        if all_keys is not None:
            lines = []
            statement_twin = False
            for k in all_keys:
                if len(k) > i:
                    lb, ln = _split_line_key(k[i])
                    if lb == label and ln is not None:
                        lines.append(ln)
                    elif lb is None and str(k[i]) == label:
                        statement_twin = True
            if line in lines:
                ordinal = sorted(lines).index(line)
            if statement_twin:
                ordinal += 1
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
    statement_twins = set()      # (position, literal name) of non-line segs
    for k in all_keys:
        for i, seg in enumerate(k):
            label, line = _split_line_key(seg)
            if label is not None:
                groups.setdefault((i, label), []).append(line)
            else:
                statement_twins.add((i, str(seg)))
    ranks = {}
    for (i, label), lines in groups.items():
        # A statement key with this literal name at this position claims
        # ordinal 0 (see _stable_key_name's docstring) - every line-keyed
        # sibling shifts up so no two key paths share a name.
        base = 1 if (i, label) in statement_twins else 0
        for o, ln in enumerate(sorted(lines)):
            ranks.setdefault((i, label, ln), o + base)  # dups keep first rank
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


# One name map per store - see _store_key_names.
_NAME_MAPS = weakref.WeakKeyDictionary()


def _store_key_names(store_obj):
    """The store's {key_path: view name} map, memoized per KEY-SET
    generation (`__live_keys_gen__`, bumped by live_view on first publish /
    re-key / prune, with len as a backstop). Ordinal names depend on the
    WHOLE key set, so every consumer — snapshot overlay, call-token
    overlay, idle paths — must read the SAME map: consumers holding
    differently-stale private memos minted one name for two different keys
    (duplicate marker/window IDs, the draw_text garbled overlays)."""
    try:
        d = vars(store_obj)
    except TypeError:
        return {}
    store = d.get("__live_values__") or {}
    gen = d.get("__live_keys_gen__", 0)
    try:
        ent = _NAME_MAPS.get(store_obj)
        if ent is None or ent[0] != gen or ent[1] != len(store):
            ent = (gen, len(store), _stable_key_names(list(store.keys())))
            _NAME_MAPS[store_obj] = ent
        return ent[2]
    except TypeError:
        return _stable_key_names(list(store.keys()))


def _inline_value_text(value, max_chars=20):
    """Format a simple builtin value for the marker's INLINE label, or None
    when the value isn't simple enough (those keep the popover window).
    Simple: bool, int, float, str, enum members, and tuples of up to 4 such
    scalars. `max_chars` caps a string's printed length (ellipsis past it) —
    the label floats over code, so it must stay short. Colors (see
    _inline_swatch_rgba) keep their text; the pill adds the swatch."""
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
        # repr ONLY a max_chars prefix: a frame snapshot's locals include
        # whole file texts (megabytes), and repr(value) on one is ~1 ms
        # per call — 90 markers a frame put the stack-trace pane at 110 ms
        # (cProfile 09-01: 2.13 s of 2.38 s in builtins.repr). Quotes and
        # quotes only lengthen a repr, so the prefix's repr already runs
        # past max_chars whenever the full one would, and the label cut
        # from it is the same text.
        if len(value) > max_chars:
            return repr(value[:max_chars])[:max_chars] + "…"
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


_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
# Two blank cells the pill text reserves for the swatch - the width math and
# the left-gap cells below need no special case for it.
_SWATCH_HOLE = "  "


def _inline_swatch_rgba(value):
    """(r, g, b, a) in 0..1 when a captured value READS as a color — a 3/4
    tuple of numbers all within 0..1 (or all ints within 0..255 with one
    past 1, scaled down), or a hex color string (`'#8888c6'`, `'#fff'`,
    RGBA `'#8888c680'`) — else None. Same shapes the editor's color3 /
    colorhex token widgets swatch."""
    if isinstance(value, str):
        if len(value) > 9 or not _HEX_COLOR_RE.match(value):
            return None
        hex_digits = value[1:]
        if len(hex_digits) == 3:
            hex_digits = "".join(c + c for c in hex_digits)
        channels = [int(hex_digits[i:i + 2], 16) / 255.0
                    for i in range(0, len(hex_digits), 2)]
        return (channels[0], channels[1], channels[2],
                channels[3] if len(channels) == 4 else 1.0)
    if not isinstance(value, tuple) or len(value) not in (3, 4):
        return None
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        if not 0 <= item <= 255:
            return None
    if all(item <= 1 for item in value):
        channels = tuple(float(c) for c in value)
    elif all(isinstance(item, int) for item in value):
        channels = tuple(c / 255.0 for c in value)
    else:
        return None
    return channels if len(channels) == 4 else channels + (1.0,)


def _paint_value_pill(inline_text, span_x, text_y, span_width=None,
                      allow_overflow=False, fill=False, tint=None,
                      swatch=None):
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

    `tint` is the rgb the pill wears — the background tint under the token
    (see _pill_tint: token → enclosing def/class block → file), so the
    value reads as part of the scope it was captured in; None keeps the
    default forest green. Fill and text derive from it with the SAME
    factors as the green, so an untinted pill looks exactly as before.

    `swatch` = ((r, g, b, a), hole_index): a color chip painted into the
    _SWATCH_HOLE the text carries at character `hole_index` — the pill's
    layout treats the hole as text, so nothing else changes. The chip is
    split like the editor's color widgets: left half opaque, right half at
    the real alpha over the card."""
    # [tint=(0.36, 0.85, 0.46)] pad_x = 3.0
    pad_x = 3.0
    # [tint=(0.30, 0.52, 0.20)] default_tint = (0.30, 0.52, 0.20)
    default_tint = (0.30, 0.52, 0.20)
    # Card and text are the tint re-saturated at fixed brightness (value),
    # so every pill reads as the same kind of thing whatever hue it wears:
    # a deep, vivid card with equally vivid text - deliberately contrasting the
    # muted code text around it. Raise *_value to lighten, *_saturation
    # toward 1.0 to make the hue purer.
    fill_saturation = 0.92
    fill_value = 0.14
    text_saturation = 0.78
    text_value = 0.88
    # The label's own face: a step below the editor's (JetBrains Mono 18.5)
    # so the value never passes for code. None while the face is still
    # baking (first get() queues it) - the current font stands in.
    label_font = Font.JETBRAINS_MONO_16
    pad_y = 1.0
    shadow_offset = 2.0
    corner_radius = 4.0
    line_text_height = imgui.get_text_line_height()      # in editor face
    _font_mgr = Core.melty.font_mgr
    _font_handle = _font_mgr.get(label_font) if _font_mgr is not None else None
    if _font_handle is not None:
        imgui.push_font(_font_handle)
    try:
        _paint_value_pill_body(inline_text, span_x, text_y, span_width,
                               allow_overflow, fill, tint, swatch, pad_x,
                               pad_y, shadow_offset, corner_radius,
                               default_tint, fill_saturation, fill_value,
                               text_saturation, text_value, line_text_height)
    finally:
        if _font_handle is not None:
            imgui.pop_font()


def _pill_rgb(base, saturation, value):
    """`base` re-saturated at a fixed brightness — the pill's card / text
    colour for a tint. Hue is all that survives of the base."""
    hue, sat, _val = colorsys.rgb_to_hsv(base[0], base[1], base[2])
    # A grey base has no hue to keep - let it stay grey at the target value.
    return colorsys.hsv_to_rgb(hue, saturation if sat > 0.05 else sat, value)


def _paint_value_pill_body(inline_text, span_x, text_y, span_width,
                           allow_overflow, fill, tint, swatch, pad_x, pad_y,
                           shadow_offset, corner_radius, default_tint,
                           fill_saturation, fill_value, text_saturation,
                           text_value, line_text_height):
    """_paint_value_pill's layout + paint, run with the label font pushed
    (every measurement here is in that face). The pill's vertical centre
    stays on the code line: text_y is the LINE's text top, and the smaller
    face is centred within the line's glyph height."""
    text_size = imgui.calc_text_size(inline_text)
    text_y += max(0.0, (line_text_height - text_size.y) * 0.5)
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
    base = tint if tint is not None else default_tint
    fill_rgb = _pill_rgb(base, fill_saturation, fill_value)
    text_rgb = _pill_rgb(base, text_saturation, text_value)
    draw_list: _DrawList = imgui.get_window_draw_list()
    draw_list.add_rect_filled(
        box_x, box_y, box_x + box_width, box_y + box_height,
        pack_color(fill_rgb[0], fill_rgb[1], fill_rgb[2], 0.97),
        rounding=corner_radius)
    draw_list.add_text(text_x, text_y,
                       pack_color(text_rgb[0], text_rgb[1],
                                                text_rgb[2], 0.97),
                       inline_text)
    if swatch is not None and len(inline_text) >= swatch[1] + len(_SWATCH_HOLE):
        rgba, hole_index = swatch
        hole_x = text_x + imgui.calc_text_size(inline_text[:hole_index]).x
        hole_width = imgui.calc_text_size(_SWATCH_HOLE).x
        side = max(4.0, min(hole_width - 2.0, text_size.y - 2.0))
        chip_x = hole_x + (hole_width - side) * 0.5
        chip_y = text_y + (text_size.y - side) * 0.5
        chip_mid = chip_x + side * 0.5
        draw_list.add_rect_filled(
            chip_x, chip_y, chip_mid, chip_y + side,
            pack_color(rgba[0], rgba[1], rgba[2], 1.0),
            rounding=2.0, flags=imgui.DRAW_ROUND_CORNERS_LEFT)
        draw_list.add_rect_filled(
            chip_mid, chip_y, chip_x + side, chip_y + side,
            pack_color(rgba[0], rgba[1], rgba[2], rgba[3]),
            rounding=2.0, flags=imgui.DRAW_ROUND_CORNERS_RIGHT)


_FILE_TINT_FN = None


def _pill_tint(editor_ds, line0, symbol=None, token_tint=None):
    """RGB a value pill at DISPLAY line `line0` wears: the background tint
    under its token. Highest first: the token's own tint (its `# [tint=…]`
    comment → `token_tint`; else a tinted definition named `symbol`, one
    dict read off the editor's cached def-tint name map), the innermost
    tinted class/def block containing the line (draw_text stamps its
    fold-remapped block list as `_lv_tint_blocks`), the file's FileMeta
    tint, else None (the pill's default green). Cheap by construction: the
    block scan is memoized per line against the block tuple's identity
    (rebuilt only when the def-tint pass rebuilds), everything else is a
    handful of dict reads — this runs once per visible pill per repaint."""
    global _FILE_TINT_FN
    if token_tint is not None:
        return tuple(token_tint[:3])
    if editor_ds is None:
        return None
    _d = editor_ds.__dict__
    if symbol is not None:
        _dt = _d.get("_def_tints")
        if _dt is not None and len(_dt) == 4:
            _t = _dt[3].get(symbol)
            if _t is not None:
                return _t
    blocks = _d.get("_lv_tint_blocks") or ()
    memo = _d.get("_lv_pill_tint_memo")
    if memo is None or memo[0] is not blocks:
        memo = (blocks, {})
        object.__setattr__(editor_ds, "_lv_pill_tint_memo", memo)
    cache = memo[1]
    got = cache.get(line0, _NO_VALUE)
    if got is _NO_VALUE:
        got, best = None, -1
        for _l0, _i0, _e0, _t0 in blocks:
            # Innermost = the containing block that starts LAST.
            if _l0 <= line0 <= _e0 and _l0 > best:
                best, got = _l0, tuple(_t0[:3])
        cache[line0] = got
    if got is not None:
        return got
    # File tint: the same FileMeta color the editor tab wears. The path
    # comes from the editor's jump_to Address, else the editor's file_key.
    _jt = _d.get("jump_to")
    path = getattr(_jt, "path", None) if _jt is not None else None
    if path is None:
        path = _d.get("_file_meta")
        if not isinstance(path, str):
            return None
    if _FILE_TINT_FN is None:
        from meltygui.editor.text_editor import _uj_file_tint
        _FILE_TINT_FN = _uj_file_tint
    return _FILE_TINT_FN(path)


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


def _build_owner_index(scope_node):
    """{surfaced key → owning dict} for a scope, the same breadth-first
    walk _override_owner does, done ONCE: a marker's first render used to
    BFS the whole scope per marker (5.5k markers × a 5.6k-line def = 15 s
    on a diff expand, 09-01). First node in BFS order wins, as before."""
    from meltygui.code.libcst_conversion import _is_block_key
    index = {}
    queue = [scope_node]
    for node in queue:
        if not isinstance(node, dict):
            continue
        ovs = node.get("__overrides__")
        for k in node.keys():
            if isinstance(k, str) and k not in index:
                index[k] = node
        if isinstance(ovs, dict):
            for ok in ovs.keys():
                if (isinstance(ok, str) and len(ok) > 4 and ok.startswith("__")
                        and ok.endswith("__") and ok[2:-2] not in index):
                    index[ok[2:-2]] = node
        for k, v in node.items():
            if isinstance(v, dict) and _is_block_key(k):
                queue.append(v)
    return index


def _owner_index(host_ds, scope_node, def_key, src):
    """The owner index of a def scope, memoized on `host_ds` (the editor)
    per source version: `src` is the parse's source string (identity = the
    content-free change signal), `def_key` = (def name, def line)."""
    d = host_ds.__dict__
    memo = d.get("_lv_owner_indexes")
    if memo is None or memo[0] is not src:
        memo = (src, {})
        object.__setattr__(host_ds, "_lv_owner_indexes", memo)
    index = memo[1].get(def_key)
    if index is None:
        index = memo[1][def_key] = _build_owner_index(scope_node)
    return index


def _override_owner(scope_node, lookup_key, index_host=None, def_key=None,
                    src=None):
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
    if index_host is not None and def_key is not None and src is not None:
        return _owner_index(index_host, scope_node, def_key, src).get(
            lookup_key, scope_node)
    from meltygui.code.libcst_conversion import _is_block_key
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
    from meltygui.code.libcst_conversion import FunctionParse
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
    # Def lookup + owner BFS memoized on the EDITOR ds per (tree, source):
    # per marker ds they ran once each — 5.5k first renders on a diff
    # expand walked the def 11k times (09-01).
    if editor_ds is not None:
        _dmemo = editor_ds.__dict__.get("_lv_def_nodes")
        if _dmemo is None or _dmemo[0] is not tree or _dmemo[1] is not src:
            _dmemo = (tree, src, {})
            object.__setattr__(editor_ds, "_lv_def_nodes", _dmemo)
        node = _dmemo[2].get((def_name, def_line), _NO_VALUE)
        if node is _NO_VALUE:
            node = _dmemo[2][(def_name, def_line)] = _find_def_node(
                tree, def_name, def_line)
    else:
        node = _find_def_node(tree, def_name, def_line)
    scope = node.get("locals") if isinstance(node, dict) else None
    key = d.get("live_key")
    owner = stamped
    if isinstance(scope, dict) and isinstance(key, str):
        owner = _override_owner(scope, key, index_host=editor_ds,
                                def_key=(def_name, def_line),
                                src=src if editor_ds is not None else None)
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


from meltygui.view.code_view import draw_live_view_overlay


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
from meltygui.view.code_view import draw_live_view_marker


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
    from meltygui.core.graphics.gl_state import GLState
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
    from meltygui.core.core_render import release_input_refs
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
            from meltygui.core.graphics.gl_state import GLState
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
        from meltygui.code.live_view import park_rerun_hint
        park_rerun_hint(store_obj, key_path)
    except Exception as e:
        print(f"live_view: park hint for {key_path} failed: {e!r}")
    # The tensor is unreferenced now; hand its blocks back to the system so
    # the VRAM actually drops (allocator cache → empty_cache), off-thread.
    try:
        from meltygui.core.runtime.gc_manager import release_cuda_cache_soon
        release_cuda_cache_soon(label="live view close")
    except Exception:
        pass


def _auto_run_on_user_open(editor_ds, store_obj):
    """User opened a value window: with Auto Execute on for the def, the
    def widget recompiles + runs so the window fills (text_editor.
    fnrun_auto_run_on_open — coalesced there). Never raises."""
    try:
        from meltygui.editor.text_editor import fnrun_auto_run_on_open
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
    from meltygui.core.windowing.window_visibility import marker_user_visibility
    marker_user_visibility(marker_ds, not open_)
    win_ds = getattr(marker_ds, "_lv_window_ds", None)
    if open_:
        _auto_run_on_user_open(getattr(marker_ds, "_lv_editor_ds", None),
                               (getattr(marker_ds, "_kwargs", None) or {}).get("store_obj"))
    if (open_ and win_ds is not None
            and "window_pos" not in (win_ds._kwargs or {})):
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


from meltygui.view.code_view import draw_snapshot_overlay


def _draw_usage_labels(draw_state, fn, node, span, source_lines, snap_vals,
                       anchor_lines, anchor_keys, origin_x, origin_y,
                       char_w, line_px, lmap, clip, col_shift=0,
                       binding_pills=None):
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
    from meltygui.core.runtime.toggles import Toggles
    usages_on = bool(Toggles.TextEditor.live_inline_usages)
    if not snap_vals or (not usages_on and not binding_pills):
        return
    frame = Core.melty.frame_count
    if not usages_on:
        # Binding pills only - no occurrence index needed.
        return _stamp_and_paint(draw_state, fn, span, (), {}, snap_vals,
                                origin_x, origin_y, char_w, line_px, lmap,
                                clip, col_shift, frame, binding_pills)
    memo = draw_state.__dict__.get("_lv_usage_memo")
    if memo is None or memo[0] is not source_lines:
        memo = (source_lines, {})
        object.__setattr__(draw_state, "_lv_usage_memo", memo)
    entry = memo[1].get(span.start_line)
    _fresh_owner = (entry is not None and len(entry) >= 6
                    and entry[0]() is fn)
    # Key-count changes DEBOUNCE (30 frames): a run adds hundreds of
    # keys over frames, and rebuilding (a whole-def tokenize) per repaint
    # during the burst is O(def) per frame. New bindings' pills appear afte
    # the burst settles; a source/def change rebuilds ASAP.
    if not _fresh_owner or (entry[1] != len(snap_vals)
                            and frame - entry[5] > 30):
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
        entry = (weakref.ref(fn), len(snap_vals), occurrences, bindings,
                 [o[0] for o in occurrences], frame)
        memo[1][span.start_line] = entry
    occurrences, bindings = entry[2], entry[3]
    # VISIBLE BAND ONLY: draw_text renders 20k+ line files and a ne
    # snapshot binds every local, so the full occurrence list is huge;
    # per-frame work must stay O(visible). Same approach as the anchor
    # index: occurrences are line-sorted, bisect the band (display band
    # ±64 lines of slack for in-flight edit shifts, matching _build_key_index)
    # and only those need the _lmap / resolve / stamp loop.
    if clip is not None and line_px:
        occ_lines = entry[4]
        band_lo, band_hi = _parse_line_band(lmap, clip, origin_y, line_px)
        i0 = bisect.bisect_left(occ_lines, band_lo)
        i1 = bisect.bisect_right(occ_lines, band_hi)
        occurrences = occurrences[i0:i1]
    return _stamp_and_paint(draw_state, fn, span, occurrences, bindings,
                            snap_vals, origin_x, origin_y, char_w, line_px,
                            lmap, clip, col_shift, frame, binding_pills)


def _parse_line_band(lmap, clip, origin_y, line_px, slack=64):
    """The visible band as PARSE lines (1-based, `_lv_key_index` /
    occurrence-list space): the clip's rows are DISPLAY lines, and with
    folds collapsed a display line sits at a larger buffer line — a band
    taken straight from the rows bisected the buffer-sorted anchor lists
    short of every marker past ~64 folded lines (the live views vanished
    towards the end of a file with its `# [` comments hidden, 09-02). The
    line map carries the fold layout (`_d2b`: buffer line per display
    line); rows past its end extend at the same rate. `slack` rows either
    side cover in-flight edit shifts (the bridge)."""
    lo = int((clip[1] - origin_y) / line_px)
    hi = int((clip[3] - origin_y) / line_px)
    d2b = getattr(lmap, "_d2b", None) if lmap is not None else None
    if d2b:
        n = len(d2b)

        def _to_buf(dl):
            if dl < 0:
                return dl
            if dl < n:
                return d2b[dl]
            return d2b[-1] + (dl - n + 1)
        lo, hi = _to_buf(lo), _to_buf(hi)
    return lo - slack, hi + slack + 1


def _stamp_and_paint(draw_state, fn, span, occurrences, bindings, snap_vals,
                     origin_x, origin_y, char_w, line_px, lmap, clip,
                     col_shift, frame, binding_pills):
    """The trailing-gap stamp + paint shared by usage labels and binding
    pills (see _draw_usage_labels)."""
    trails = draw_state.__dict__.setdefault("_lv_trail_views", {})
    _fvars = vars(fn)
    publish_seq = _fvars.get("__live_pub_seq__") or {}
    # governing_key is O(bindings above the line) - a name rebound dozens
    # of times in a big def (`x`, `_ti`) with ~100 visible occurrences
    # was a per-frame scan in the thousands. Its answer only changes when
    # the bindings index rebuilds or a publish re-orders the sequence
    # (`__live_pub_gen__` bumped by add_view beside __live_pub_seq__).
    _pub_gen = _fvars.get("__live_pub_gen__", 0)
    _gov_memo = draw_state.__dict__.get("_lv_gov_memo")
    if (_gov_memo is None or _gov_memo[0] is not bindings
            or _gov_memo[1] != _pub_gen):
        _gov_memo = (bindings, _pub_gen, {})
        object.__setattr__(draw_state, "_lv_gov_memo", _gov_memo)
    _gov = _gov_memo[2]
    # Occurrence loop memo (a no-clip pane case hands EVERY occurrence of
    # the def here, ~5k in draw_text, every frame): its two outputs only
    # change with the occurrence index, a publish, the fold layout (the
    # fresh per-frame index keys its layout as `_d2b`, see draw_text's
    # _tv_fold_lm) or the geometry. An empty layout (no `_d2b`) means a
    # merge in flight - no memo. Watches were registered on the pass that
    # built the entry; they persist.
    _lm_key = getattr(lmap, "_lv_key", None) if lmap is not None else None
    _ok_key = (clip is None and (lmap is None or _lm_key is not None)
               and id(occurrences)) or None
    # The binding-pill LIST rides in the key by identity: the snapshot
    # overlay's full-pass memo hands the SAME list object frame after
    # frame while no key changes state (and pins it in its memo), so a
    # hit here covers the pills merge below as well.
    _ok = (_ok_key, _pub_gen, _lm_key, origin_y, line_px, col_shift,
           id(binding_pills) if binding_pills else 0)
    _om = draw_state.__dict__.get("_lv_stamp_memo")
    _hit = bool(_ok_key) and _om is not None and _om[0] == _ok
    if _hit:
        sub = _om[1]            # shared: never mutated past this point
        paints = _om[2]         # line-sorted, pills merged
        occurrences = ()
        binding_pills = None
    else:
        sub = {}          # (display line0, buffer boundary col) → gap cells
        paints = []       # visible pills, painted after the stamp below
    for line, col, name, _last in occurrences:
        _ml = lmap(line) if lmap else line
        if _ml is None:
            continue          # inside the mid-edit region - skip a wash
        _gk = (line, name)
        key = _gov.get(_gk, _NO_VALUE)
        if key is _NO_VALUE:
            binding_lines, binding_keys = bindings[name]
            key = live_usage.governing_key(binding_lines, binding_keys, line,
                                           publish_seq)
            _gov[_gk] = key
        if key is None or key not in snap_vals:
            continue
        _uval = snap_vals.get(key)
        pill_text = _inline_value_text(_uval)
        if pill_text is None:
            continue
        # Reads as `seq_len=384`; a color value carries the swatch hole
        # right after the `=`.
        _urgba = _inline_swatch_rgba(_uval)
        swatch = (_urgba, 1) if _urgba is not None else None
        pill_text = "=" + (_SWATCH_HOLE if _urgba is not None else "") + pill_text
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
        paints.append((_ml - 1, boundary_col, pill_text, len(name), pill_y,
                       name, swatch or ()))
    # Binding pills: the captured value of an assignment/param target,
    # inserted right after ITS symbol exactly like a usage label -
    # `edited=False, new_text='...' = draw_text(...)`. A usage gap already at
    # the same boundary wins (same captured value).
    if binding_pills:
        for line0, bcol, pill_text, sym_len, swatch in binding_pills:
            if (line0, bcol) in sub:
                continue
            sub[(line0, bcol)] = len(pill_text) + 1
            pill_y = origin_y + line0 * line_px
            if clip is not None and (pill_y + line_px < clip[1]
                                     or pill_y > clip[3]):
                continue
            paints.append((line0, bcol, pill_text, sym_len, pill_y, "",
                           swatch or ()))
    if _ok_key and not _hit:
        # `_layout`, `occurrences` and the pill list ride along so the ids
        # in the key stay pinned. Paints are stored line-sorted so a hit
        # can bisect the laid-out window instead of walking every
        # occurrence.
        paints.sort()
        object.__setattr__(draw_state, "_lv_stamp_memo",
                           (_ok, sub, paints, lmap, occurrences,
                            binding_pills))
    previous = trails.get(span.start_line)
    trails[span.start_line] = (frame, sub)
    if previous is None or previous[1] != sub:
        # New/changed gaps - relayout next frame (draw_text's _window clears
        # its cache on the stamp).
        draw_state._lv_trail_gen = getattr(draw_state, "_lv_trail_gen", 0) + 1
        draw_state.invalidate()
        from meltygui.core.windowing.glfw_utils import request_render
        request_render()
    # Paint into the gaps the CURRENT layout reserved (stamped back by
    # draw_text's _window). A gap not laid out yet - first frame after a
    # value appeared - skips painting; it opens on the very next layout.
    gap_cells = draw_state.__dict__.get("_lv_trail_cells") or {}
    base_x = origin_x - col_shift * char_w      # buffer cell 0 in px
    if not gap_cells:
        paints = ()
    elif _hit and len(paints) > 256:
        # Memo hit: `paints` is line-sorted - only the lines draw_text has
        # gaps open for (the tokenized window) can paint, so bisect to them.
        _glo = min(k[0] for k in gap_cells)
        _ghi = max(k[0] for k in gap_cells)
        paints = paints[bisect.bisect_left(paints, (_glo,)):
                        bisect.bisect_right(paints, (_ghi + 1,))]
    for line0, boundary_col, pill_text, name_len, pill_y, name, swatch in paints:
        gap_cell = gap_cells.get((line0, boundary_col))
        if gap_cell is None:
            continue
        gap_x = base_x + gap_cell * char_w
        # The pill wears the marker tint under its symbol (a tinted
        # definition of the name, else the enclosing function, else the
        # file) - one memoized lookup per pill.
        _paint_value_pill(pill_text, gap_x + 3.0, pill_y,
                          tint=_pill_tint(draw_state, line0, name or None),
                          swatch=swatch or None)


def _symbol_cols(anchor, rel_line, key_path, source_lines, labels, memo=None):
    """(start_col, end_col) of the symbol a snapshot key boxes, or None to
    drop the key (a line-keyed anchor on a blank / out-of-range line).
    Resolved ONCE per source version into the anchor index: this is a
    regex per key, and running it per key per FRAME was 45 of the 70 ms a
    5k-key def (draw_text in the context menu's Code pane) cost per
    selection frame (09-01)."""
    _rl, start_col, end_col = anchor
    if memo is not None:
        # Anchor index rebuilds (every reparse while typing): the answer
        # depends only on the line's text + the key, and nearly every line
        # is unchanged, so the regex runs once per (line text, key).
        text = (source_lines[rel_line - 1]
                if 1 <= rel_line <= len(source_lines) else None)
        _mk = (text, key_path[-1], end_col is None, start_col)
        _hit = memo.get(_mk, _NO_VALUE)
        if _hit is not _NO_VALUE:
            return _hit
        _res = _symbol_cols(anchor, rel_line, key_path, source_lines, labels)
        memo[_mk] = _res
        return _res
    if end_col is None:
        # No span (line-only keys) - box the LABELED SYMBOL on the line when
        # the store's key names one (frame-snapshot params and twin
        # while-body keys always carry their a's name; attribute keys
        # box just their FINAL segment - see _label_box_span), falling
        # back to the whole line's text. Full-line boxes stack into an
        # unreadable double-washed region when several line-keyed values
        # land on adjacent lines (a captured signature), and their click
        # latches swallow the lines.
        if not (1 <= rel_line <= len(source_lines)):
            return None
        text = source_lines[rel_line - 1]
        if not text.strip():
            return None
        span_cols = _label_box_span(labels.get(key_path), text)
        if span_cols is not None:
            return span_cols
        return len(text) - len(text.lstrip()), len(text.rstrip())
    # The leaf span covers the assignment (or maybe just its target,
    # depending on the key) - box the target itself so the highlight
    # (and its click latch) doesn't swallow the line: the first
    # word-boundary occurrence of the target name on the line (the
    # assignment target precedes any RHS use of the name).
    name = key_path[-1].split("#", 1)[0]
    text = (source_lines[rel_line - 1]
            if 1 <= rel_line <= len(source_lines) else "")
    m = re.search(rf"\b{re.escape(name)}\b", text)
    if m is not None:
        return m.start(), m.end()
    return start_col, start_col + max(1, len(name))


def _node_owns_function(node, fn, span, line_offset=0):
    """True when the def node at `span` IS the definition of `fn` — the name
    matches and fn's first line (its top decorator, so it may sit a few
    lines ABOVE the span's def line) lies inside the span's line range. A
    closure's node fails this for the enclosing function it resolved to."""
    from meltygui.code.libcst_conversion import parse_def_name
    try:
        inner = inspect.unwrap(fn)
    except Exception:
        inner = fn
    if parse_def_name(node) != getattr(inner, "__name__", None):
        return False
    code = getattr(inner, "__code__", None)
    first = getattr(code, "co_firstlineno", None)
    if first is None:
        return True
    decorator_slack = 16
    start = span.start_line + line_offset
    end = getattr(span, "end_line", span.start_line) + line_offset
    return start - decorator_slack <= first <= end


def _scope_function(filename, def_line):
    """The live function object for a def at an absolute file line — the same
    resolver capture uses, so the store read here is the store written to."""
    from meltygui.code.chain_converters import _enclosing_function
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
            from meltygui.code.live_instrument import run_instrumented
            return run_instrumented(fn, **kw)
        proxy.__name__ = fn.__name__
        proxy.__qualname__ = fn.__qualname__
        proxy.__signature__ = inspect.signature(fn)
        _proxies[fn] = proxy
    return proxy








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
    from meltygui.core.windowing.glfw_utils import request_render
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
