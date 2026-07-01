import bisect
import keyword
import re
import time

import glfw
import imgui

from src.lsd.gl_gui.model.core_model.draw_state import Anchor, Pin, DropDownState
from src.lsd.gl_gui.toggles import Tint
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import CodeLine
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header, draw_footer
from src.lsd.gl_gui.view.core_views.search_glow import draw_search_highlight
from src.lsd.gl_gui.melty import Melty, SearchTerm
from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.jump_to import draw_jump_to
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults, Core


def _hex(h):
    """Convert '#rrggbb' to imgui packed u32 color (ABGR format)."""
    r = int(h[1:3], 16)
    g = int(h[3:5], 16)
    b = int(h[5:7], 16)
    a = 255
    # ImGui uses ABGR packing for color u32
    return (a << 24) | (b << 16) | (g << 8) | r


COLORS = {
    'default': _hex('#a9b7c6'),  # Token (from Darcula)
    'keyword': _hex('#cc7832'),  # Keyword
    'keyword_const': _hex('#cc7832'),  # Keyword.Constant (None)
    'bool': _hex('#cc7832'),  # True/False - own key so color highlighting can target them
    'operator_word': _hex('#cc7832'),  # Operator.Word (and, or, not, in, is)
    'builtin_pseudo': _hex('#94558d'),  # Name.Builtin.Pseudo (self, cls)
    'decorator': _hex('#bbb529'),  # Name.Decorator
    'string': _hex('#6a8759'),  # String
    'string_doc': _hex('#629755'),  # String.Doc (docstrings)
    'comment': _hex('#808080'),  # Comment
    'number': _hex('#6897bb'),  # Number
    'color3': _hex('#6897bb'),  # merged float 3-tuple `(r, g, b)` (for text color)
    'icon': _hex('#56b6c2'),  # Font Awesome / Pango glyph (cyan, distinct from strings)
}

KEYWORDS = {'def', 'class', 'if', 'else', 'elif', 'for', 'while',
            'return', 'import', 'from', 'with', 'as', 'try', 'except',
            'finally', 'raise', 'yield', 'pass', 'break', 'continue',
            'lambda', 'global', 'nonlocal', 'del', 'assert', 'async', 'await'}

KEYWORD_CONSTS = {'True', 'False', 'None'}

OPERATOR_WORDS = {'and', 'or', 'not', 'in', 'is'}

BUILTIN_PSEUDO = {'self', 'cls'}

WORD_DELIMITERS = ' \t\n\r,.;:!?()[]{}\'\"=+-*/<>@#$%^&|~`\\'

# --- Code-suggestion (autocomplete) --------------------------------------------
# Drives the dropdown popup (draw_dd_menu) anchored at the caret. The actual
# symbol intelligence lives in libcst_conversion.completions_at - it reads the
# routed code_tree (the digest + parsed dict + span/line index + libcst tree +
# jedi symbol data) and returns scope-aware, kind-tagged candidates ranked
# best-first. Here we just thread the caret context, supplement with a plain
# buffer scan (covers freshly-typed locals the parse hasn't caught up to yet),
# and filter by the half-typed prefix.
#
# Still NOT type-aware: after `somevar.` we can't resolve what `somevar` IS, so
# the dot-trigger offers the same scoped name pool as bare-identifier typing.
# Resolving attribute members (via jedi on the fly) is the next step.

_IDENT_RE = re.compile(r'[A-Za-z_]\w*')
# Identifier immediately to the left of a position - the half-typed word the
# popup filters by (and the span an accepted suggestion replaces).
_PREFIX_RE = re.compile(r'[A-Za-z_]\w*$')
_PY_KEYWORDS = frozenset(keyword.kwlist)
_AC_MAX_ROWS = 40  # cap so a huge file can't render a million-row popup


def _completion_context(text, cursor):
    """The completion site at `cursor`: the identifier `prefix` being typed, the
    `anchor` index where it starts (== cursor when there's no prefix yet), and
    whether the char just before the prefix is a `.` (attribute access). The
    anchor is the span an accepted suggestion overwrites."""
    left = text[:cursor]
    m = _PREFIX_RE.search(left)
    prefix = m.group(0) if m else ""
    anchor = cursor - len(prefix)
    if anchor - 1 >= 0 and len(text) > anchor - 1:
        dot_trigger = anchor > 0 and text[anchor - 1] == "."
        return prefix, anchor, dot_trigger
    else:
        return prefix, anchor, False


def _completion_pool(code_tree, text, line):
    """Ordered (name, kind) candidate pool for a caret on 0-indexed `line`,
    best-first. The scope-aware names from the parsed `code_tree` lead (params,
    locals, members, module, imports, jedi symbols — see `completions_at`); a
    plain identifier scan of the live buffer is appended at low priority so
    just-typed locals that haven't round-tripped through libcst yet still show.
    De-duplicated keeping the first (highest-ranked) occurrence of each name."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import completions_at
    pool, seen = [], set()

    def add(name, kind):
        if name and name not in seen and len(name) > 1 and name not in _PY_KEYWORDS:
            seen.add(name)
            pool.append((name, kind))

    if code_tree is not None:
        try:
            for name, kind in completions_at(code_tree, line):
                add(name, kind)
        except Exception:
            pass  # never let a parse hiccup kill typing
    for name in _IDENT_RE.findall(text):
        add(name, "name")
    return pool


# Internal completion `kind` → short display tag shown dim on the right of each
# row. "name" (a bare buffer identifier we couldn't classify) maps to "" so no
# tag is drawn for it.
_KIND_TAGS = {"param": "param", "local": "local", "var": "var", "func": "fn",
              "class": "class", "member": "attr", "module": "mod",
              "import": "import", "symbol": "sym", "instance": "var",
              "kw": "kw", "path": "path", "name": ""}


def _kind_tag(kind):
    return _KIND_TAGS.get(kind, kind)


def _filter_completions(pool, prefix):
    """Filter the ordered (name, kind) `pool` by `prefix`, returning the matching
    (name, kind) rows. Prefix matches (case-insensitive) come before looser
    substring matches; the pool's own scope ranking is preserved within each
    group. Empty prefix (right after a `.`) keeps the pool order. The exact word
    already fully typed is dropped so we never suggest what's on screen."""
    rows = [(n, k) for (n, k) in pool if n != prefix]
    if not prefix:
        # Empty prefix only happens right after a '.', where a pile of dunders is
        # noise - hide them (typing a leading '_' brings them back via the else).
        ranked = [(n, k) for n, k in rows if not n.startswith("_")]
    else:
        p = prefix.lower()
        starts = [(n, k) for n, k in rows if n.lower().startswith(p)]
        contains = [(n, k) for n, k in rows if p in n.lower() and not n.lower().startswith(p)]
        ranked = starts + contains
    return ranked[:_AC_MAX_ROWS]


# jedi completion `.type` → our kind mapping.
_JEDI_KIND = {"module": "module", "class": "class", "function": "func",
              "instance": "var", "statement": "var", "param": "param",
              "property": "member", "keyword": "kw", "path": "path"}


def _wake_on_future(fut, ds):
    """Wake the render loop ONCE when an async jedi `fut` resolves on its worker
    thread. The worker can't drive a render itself (`request_render` no-ops off
    the main thread — no GL context), and the loop is parked in `glfw.wait_events`
    until an event arrives, so without this the popup stays blank until an
    unrelated event (a click) happens to wake it. We invalidate the editor tile
    exactly once and post a GLFW event (both thread-safe) — no per-frame polling
    while the job is in flight. Returns `fut` for call-site chaining."""
    if fut is None:
        return None
    tile = getattr(ds, '_tile_id', None)

    def _cb(_f, tile=tile):
        try:
            from src.lsd.gl_gui.melty import Melty
            if tile is not None:
                Melty.cache.invalidate(tile)
        except Exception:
            pass
        try:
            from src.lsd.gl_gui.utils import glfw_utils
            glfw_utils._needs_render.set()   # survive the training-branch render gate
        except Exception:
            pass
        try:
            glfw.post_empty_event()          # wake glfw.wait_events from any thread
        except Exception:
            pass

    fut.add_done_callback(_cb)
    return fut


def _ensure_member_completions(ds, text, anchor):
    """Type-aware member candidates for the dotted receiver ending at `anchor`
    (index just past the '.'), via jedi (async). Submits ONE job per receiver
    context to the background pool, polls it without blocking, and returns
    (members, pending): `members` is an ordered [(name, kind)] once ready (else
    None); `pending` is True while a job is in flight, so the caller keeps the
    body repainting to poll it. The receiver key is anchored at the '.', so it's
    stable while the user types the member stem — one jedi call, local filtering."""
    line0, col = _index_to_line_col(text, anchor)
    line_start = _get_line_start(text, anchor)
    key = (line0, text[line_start:anchor])   # the receiver expression on this line

    if getattr(ds, '_ac_jedi_done_key', None) == key:
        return ds._ac_jedi_members, False

    if getattr(ds, '_ac_jedi_req_key', None) != key:
        # Receiver changed - request new completions (drops any stale future).
        # The done-callback wakes us once when it lands; no per-frame polling.
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import submit_member_completion
        ds._ac_jedi_future = _wake_on_future(submit_member_completion(text, line0, col), ds)
        ds._ac_jedi_req_key = key

    fut = getattr(ds, '_ac_jedi_future', None)
    if fut is None:
        return None, False        # pool e
    if not fut.done():
        return None, True         # still computing - the done-callback will wake us
    try:
        raw = fut.result()
    except Exception:
        raw = []
    ds._ac_jedi_members = [(name, _JEDI_KIND.get(jtype, jtype)) for name, jtype in raw]
    ds._ac_jedi_done_key = key
    ds._ac_jedi_future = None

    ds.invalidate_up()
    return ds._ac_jedi_members, False


def _call_context(text, cursor):
    """For the innermost call whose parens enclose `cursor`, return
    (open_paren_index, arg_index); else (None, 0). arg_index counts top-level
    commas between the '(' and the caret. Bracket/brace literals at the cursor's
    level (a list/dict, not a call) return None. Scan is bounded for big buffers."""
    depth = 0
    commas = 0
    i = cursor - 1
    limit = max(0, cursor - 4000)
    if i >= len(text):
        return (None, 0)
    while i >= limit:
        c = text[i]
        if c in ")]}":
            depth += 1
        elif c in "([{":
            if depth == 0:
                return (i, commas) if c == "(" else (None, 0)
            depth -= 1
        elif c == "," and depth == 0:
            commas += 1
        i -= 1
    return (None, 0)


def _callee_at(text, open_paren):
    """The call expression directly before `open_paren` (e.g. 'imgui.text'),
    or '' if a non-identifier precedes the paren (a grouping paren, not a call)."""
    k = open_paren - 1
    while k >= 0 and (text[k].isalnum() or text[k] in "_."):
        k -= 1
    return text[k + 1:open_paren]


def _param_name(s):
    """Reduce a jedi param string to just its name, across the two formats jedi
    emits: python 'name: ann=default' / '*args' and C-style (pyimgui) 'type name'
    / 'Type name=default'. We show names only, so strip annotations/defaults."""
    s = s.split("=", 1)[0].strip()
    if ":" in s:                       # python: 'name: annotation'
        return s.split(":", 1)[0].strip().lstrip("*")
    parts = s.split()                  # C-style 'type name' (or a bare name)
    return (parts[-1] if parts else s).lstrip("*")


def _param_type(s):
    """The type annotation from a jedi param string, or '' if none. Mirror of
    _param_name for the two formats: python 'name: ann' → ann; C-style 'type
    name' → type; bare 'name' → ''."""
    s = s.split("=", 1)[0].strip()
    if ":" in s:                       # python: 'name: annotation'
        return s.split(":", 1)[1].strip()
    parts = s.split()                  # C-style 'type name'
    return " ".join(parts[:-1]) if len(parts) > 1 else ""


def _ensure_signature_help(ds, text, open_paren, cursor):
    """Signature of the call whose '(' is at `open_paren`, via jedi (async, same
    pool/synthetic-module trick as completion). Submits ONE job per callee
    (keyed at the paren, stable while typing args), polls without blocking, and
    returns (name, [param_names]) once ready, else None."""
    callee = _callee_at(text, open_paren)
    if not callee:
        return None
    key = (open_paren, callee)
    if getattr(ds, '_ac_sig_done_key', None) == key:
        return ds._ac_sig_data
    if getattr(ds, '_ac_sig_req_key', None) != key:
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import submit_signature_help
        line0, col = _index_to_line_col(text, cursor)
        ds._ac_sig_future = _wake_on_future(submit_signature_help(text, line0, col), ds)
        ds._ac_sig_req_key = key
    fut = getattr(ds, '_ac_sig_future', None)
    if fut is None:
        return None
    if not fut.done():
        return None           # still computing - the done-callback will wake us
    try:
        raw = fut.result()
    except Exception:
        raw = []
    # Names for every param (compact), plus the parallel type list so the hint
    # can annotate just the ACTIVE param with its type. The job hands back
    # richer 'type name=default' strings; we split them here.
    if raw:
        _ps = raw[0][1]
        ds._ac_sig_data = (raw[0][0], [_param_name(p) for p in _ps],
                           [_param_type(p) for p in _ps])
    else:
        ds._ac_sig_data = None
    ds._ac_sig_done_key = key
    ds._ac_sig_future = None
    return ds._ac_sig_data


def _draw_signature_hint(ds, draw_state, text, origin_x, origin_y, line_px, vcols=None):
    """Float the active call's signature (name + comma-separated param names) just
    above the call line, with the current argument highlighted (and its type).
    The hint's function name is aligned horizontally with the call's function name
    in the code, so the parameters line up over the call and it's obvious at a
    glance which argument you're on. Overflow clips on the right (name stays put);
    no room above → drop below. Non-interactive; mono font assumed active."""
    if not getattr(ds, '_ac_sig_show', False):
        return
    sig = getattr(ds, '_ac_sig_data', None)
    if not sig:
        return
    name, params, types = sig
    active = getattr(ds, '_ac_sig_active', 0)
    if params:
        active = max(0, min(active, len(params) - 1))   # extra args ride the last (*args)

    name_col = imgui.get_color_u32_rgba(0.55, 0.78, 1.0, 1.0)
    dim = imgui.get_color_u32_rgba(0.72, 0.76, 0.84, 1.0)
    acc = imgui.get_color_u32_rgba(1.0, 0.84, 0.42, 1.0)
    type_col = imgui.get_color_u32_rgba(0.55, 0.72, 0.55, 1.0)   # muted green for the type

    # Lay out out with their x-offsets (so we can clip to the active one).
    segs = []          # (string, color, x_offset, is_active)
    x = 0.0
    def add(s, col, is_active=False):
        nonlocal x
        segs.append((s, col, x, is_active))
        x += imgui.calc_text_size(s).x
    add(name + "(", name_col)
    for i, p in enumerate(params):
        if i:
            add(", ", dim)
        add(p, acc if i == active else dim, i == active)
        # Annotate ONLY the active arg with its type (when jedi knew one).
        if i == active and i < len(types) and types[i]:
            add(": " + types[i], type_col)
    add(")", dim)
    total = x

    th = imgui.get_text_line_height()
    clip = draw_state.abs_clip_rect          # (left, top, right, bottom)
    cx, cy = _char_pos_to_xy(text, ds.text_cursor_pos, origin_x, origin_y, line_px, vcols=vcols)

    # Align the hint's function name with the call's function name in the code:
    # walk back from the '(' over the trailing identifier (the displayed name) and
    # anchor there, so the param list lines up over the call.
    op = getattr(ds, '_ac_sig_open_paren', None)
    if op is None or op > len(text):
        name_start = ds.text_cursor_pos
    else:
        k = op
        while k > 0 and (text[k - 1].isalnum() or text[k - 1] == '_'):
            k -= 1
        name_start = k
    nx, ny = _char_pos_to_xy(text, name_start, origin_x, origin_y, line_px, vcols=vcols)
    base_x = max(origin_x, nx)               # never slide under the gutter

    # Sit one line above the call's line (drop below if that's clipped at the top).
    hy = ny - line_px - 5
    if hy < clip[1] + 2:
        hy = ny + line_px + 4

    pad = 7
    x0 = base_x - pad
    x1 = min(clip[2] - 2, base_x + total + pad)   # overflow clips on the right
    bg = imgui.get_color_u32_rgba(0.11, 0.12, 0.15, 0.97)
    border = imgui.get_color_u32_rgba(0.30, 0.33, 0.42, 0.9)
    dl = imgui.get_window_draw_list()
    dl.add_rect_filled(x0, hy - 3, x1, hy + th + 3, bg, rounding=4)
    dl.add_rect(x0, hy - 3, x1, hy + th + 3, border, rounding=4)

    dl.push_clip_rect(x0, hy - 3, x1, hy + th + 3, True)
    for s, col, off, _a in segs:
        dl.add_text(base_x + off, hy, col, s)
    dl.pop_clip_rect()


# GLFW key to character mappings (unshifted, shifted)
_KEY_CHAR_MAP = {
    glfw.KEY_SPACE: (' ', ' '),
    glfw.KEY_APOSTROPHE: ("'", '"'),
    glfw.KEY_COMMA: (',', '<'),
    glfw.KEY_MINUS: ('-', '_'),
    glfw.KEY_PERIOD: ('.', '>'),
    glfw.KEY_SLASH: ('/', '?'),
    glfw.KEY_0: ('0', ')'),
    glfw.KEY_1: ('1', '!'),
    glfw.KEY_2: ('2', '@'),
    glfw.KEY_3: ('3', '#'),
    glfw.KEY_4: ('4', '$'),
    glfw.KEY_5: ('5', '%'),
    glfw.KEY_6: ('6', '^'),
    glfw.KEY_7: ('7', '&'),
    glfw.KEY_8: ('8', '*'),
    glfw.KEY_9: ('9', '('),
    glfw.KEY_SEMICOLON: (';', ':'),
    glfw.KEY_EQUAL: ('=', '+'),
    glfw.KEY_LEFT_BRACKET: ('[', '{'),
    glfw.KEY_BACKSLASH: ('\\', '|'),
    glfw.KEY_RIGHT_BRACKET: (']', '}'),
    glfw.KEY_GRAVE_ACCENT: ('`', '~'),
}

# Add letter keys A-Z
for _i in range(26):
    _key = glfw.KEY_A + _i
    _ch = chr(ord('a') + _i)
    _KEY_CHAR_MAP[_key] = (_ch, _ch.upper())

# Keys the editor repeats when held: every typed char plus certain navigation/edit
# keys. Used to supplement frame_key_events with imgui's synthesized auto-repeat
# (see draw_text) so held keys repeat even when the platform's GLFW backend
# doesn't generate REPEAT actions.
_REPEATABLE_KEYS = set(_KEY_CHAR_MAP) | {
    glfw.KEY_BACKSPACE, glfw.KEY_DELETE, glfw.KEY_ENTER, glfw.KEY_KP_ENTER,
    glfw.KEY_TAB, glfw.KEY_LEFT, glfw.KEY_RIGHT, glfw.KEY_UP, glfw.KEY_DOWN,
    glfw.KEY_HOME, glfw.KEY_END,
}


# Global fallback for `draw_text(token_views=...)`: when a caller passes no
# token_views, the editor uses this DEFAULT SET of callback widgets. A per-call
# token_views always wins; set this to None to disable widgets everywhere.
# The real value is assigned just below draw_icon_selector (it references that
# renderer, which is defined later in the file) - keep this forward-declaration so
# anything importing the module before then sees the value.
DEFAULT_TOKEN_VIEWS = None


# --- Token views: draw widgets in place of (or above) tokenized code ----------
# `draw_text(..., token_views=...)` maps a token kind to a renderer that draws a
# widget instead of / on top of the code. Two kinds of key, dispatched by type:
#
#   token_views = {
#       "icon":      {"renderer": draw_icon_selector,   "char_width": 4},
#       Conditional: {"renderer": draw_floating_window, "char_width": None},
#   }
#
#  - str key  → matched against the syntax tokenizer's color_key ("icon",
#    "string", "keyword", ...). INLINE + EDITABLE: each matched source char is
#    replaced by a render_func drawn in `char_width` cells. The renderer is called
#    like any other - `renderer(input_value) -> (changed, new_value)` - positioned
#    at the cell (the editor moves the cursor first; pass width=/height=/name=).
#    When it returns changed=True, the editor splices new_value in for that source
#    char and reports the edit, so it round-trips/saves like a keystroke. The
#    visual-column map (vcols) keeps it ONE source character for caret/click even
#    though it spans char_width cells.
#    With `"whole_token": True` the renderer is called ONCE per matched token
#    (e.g. "True", "3.14") instead of per char - input_value is the full token
#    text and a changed return splices the whole token. Two layouts:
#      REPLACE (default): the widget IS the token - exactly token-width
#      (len(token) cells, 1 cell per source char), it sits in the text grid
#      like the text it replaces; vcols stays identity. char_width is then
#      only the inline flag.
#      ACCESSORY (`"lead_cells": N`): the editor draws the token by itself,
#      normally - fully editable as text - shifted N cells right, and the
#      renderer gets only the N-cell lead area to the text's LEFT (e.g. the
#      color3 swatch). The line grows by N cells; vcols carries the shift so
#      caret/click/selection stay exact. This is THE pattern for widgets that
#      ride beside the text instead of replacing it.
#  - type key → matched (isinstance) against nodes in the routed code_tree
#    (Conditional/Loop/...); positioned by the node's `.span`. With `char_width=None`
#    it's a non-inline OVERLAY drawing callback (floats over/by the code, doesn't
#    edit text): `renderer(x, y, w, h, draw_state=, char_w=, line_px=, node=, span=)`.
#
# `draw_icon_selector` (below) is the reference inline renderer: an icon picker.

# The full Font Awesome set: {icon-name: glyph} read from the bundled font's cmap
# (see fa_icons.py - generated, do not hand-edit). The dropdown lists the NAMES
# (searchable, e.g. type "arrow") and picks the glyph value. FA_GLYPH_SET gives an
# O(1) "is this a known glyph?" check for the current-value fallback below.
from src.lsd.gl_gui.view.core_views.fa_icons import FA_ICONS, FA_GLYPH_SET
ICON_COLLECTION = FA_ICONS
GENERIC_ICON = "\uf005"  # star - the placeholder Ctrl+I inserts; pick the real one from the dropdown


@render_func(use_cache=True, show_bg=True, shadow=True, tint=(0.77,0.66,0.20,1.00), z_offset=2, bg_offset=4, with_header=None, disable_scroll=True,
             show_name=False, selectable=False, max_height=30)
def draw_icon_selector(input_value, draw_state=None,
                       left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False,
                       **kwargs):
    """Inline Font Awesome icon picker — the reference token_views inline renderer.

    Shaped like every other renderer: `(input_value) -> (changed, icon_str)`. It
    draws an inline dropdown (core_view's `draw_dropdown`) whose trigger shows the
    current glyph; picking a different glyph returns (True, new_glyph). Wire it via
    `token_views={"icon": {"renderer": draw_icon_selector, "char_width": N}}` and
    draw_text splices the chosen glyph back into the source on change.

    The left_mouse_* params are declared but never read: declaring them subscribes
    this view to those events, so a press that starts on the widget resolves to IT
    (topmost subscriber, blocking) and the InputHandler latches the whole drag
    here — the surrounding editor never sees the gesture, so it won't move the
    caret or grow a selection. imgui drives the actual widget from raw input."""
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_dropdown
    cur = input_value if isinstance(input_value, str) else ""
    # Always include the current glyph so the dropdown can display/round-trip it
    # even if it isn't one of the defaults.
    coll = ICON_COLLECTION if (not cur or cur in FA_GLYPH_SET) else {cur: cur, **ICON_COLLECTION}
    name = f"{getattr(draw_state, 'name', 'icon')}_dropdown"
    changed, picked = draw_dropdown(cur, collection=coll, name=name+"drop_down", show_header=False, text_align="center",
                                    width=max(21, draw_state.width), max_height=22, tint=draw_state.tint)
    return (True, picked) if (changed and isinstance(picked, str)) else (False, cur)


@render_func(use_cache=True, show_bg=True, shadow=True, with_header=None, tint=(0.911, 0.305, 0.0),
             show_name=False, selectable=False, z_offset=3, bg_offset=2)
def draw_bool_token(input_value, draw_state=None, **kwargs):
    """Inline True/False word — whole-token token_views renderer for 'bool'
    tokens. Renders the literal exactly as the editor would (same font, grid
    position and keyword color) so it reads as code. Deliberately NO imgui item
    and NO left_mouse_* subscription: single clicks and drags fall through to
    the editor, so the caret lands anywhere inside the word and selections
    sweep it like plain text. A DOUBLE-click flips the literal — hover shows an
    underline as the hint. (Single-click toggling proved too easy to trip.)"""
    word = input_value if input_value in ("True", "False") else "True"
    x, y = imgui.get_cursor_screen_pos()
    w = max(1.0, draw_state.width)
    h = max(1.0, draw_state.height)
    io = imgui.get_io()
    hovered = x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h
    draw_list = imgui.get_window_draw_list()
    color = COLORS['bool']
    # if hovered:
    #      draw_list.add_line(x, y + h - 1.5, x + w, y + h - 1.5, color, 0.0)
    draw_list.add_text(x, y, color, word)
    if hovered and imgui.is_mouse_double_clicked(0):
        return True, ("False" if word == "True" else "True")
    return False, input_value


def _parse_number_token(s):
    """Classify a numeric literal token. Returns (kind, value, fmt_back, disp)
    where kind is 'int' | 'float' | None (unparseable — render as plain text),
    fmt_back turns the dragged value back into source text matching the
    literal's original shape, and disp is the drag widget's printf format:
    - hex/oct/bin ints keep their base
    - e-notation floats round-trip through '%g' to keep the exponent form
    - plain floats keep the original number of decimal places (min 1, max 6)
    The token may carry a merged unary sign (`-5`, `+0.5` — see tokenize), so
    base-prefix detection looks past it; int()/float()/hex() all take signs."""
    low = s.lower()
    body = low[1:] if low[:1] in '+-' else low
    try:
        if body.startswith(('0x', '0o', '0b')):
            return 'int', int(s, 0), {'0x': hex, '0o': oct, '0b': bin}[body[:2]], '%d'
        if 'e' in body:
            return 'float', float(s), lambda v: '%g' % v, '%g'
        if '.' in s:
            prec = min(6, max(1, len(s.split('.', 1)[1])))
            return 'float', float(s), lambda v, p=prec: f"{v:.{p}f}", f'%.{prec}f'
        return 'int', int(s), str, '%d'
    except (ValueError, KeyError):
        return None, None, None, None


@render_func(use_cache=True, show_bg=True, shadow=True, with_header=None, z_offset=4,
             show_name=False, selectable=False, bg_offset=3, tint=(0.043, 0.068, 0.094), wrap=True)
def draw_number_token(input_value, draw_state=None,
                      left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False,
                      **kwargs):
    """Inline drag widget for a numeric literal — whole-token token_views renderer
    
    for 'number' tokens. Ints get drag_int, floats drag_float (unbounded: min=max=0);
    the dragged value is formatted back preserving the literal's shape (base,
    e-notation, decimal places) and spliced into the source like a keystroke.
    A unary sign is merged into the token by tokenize(), so the widget owns it
    and a drag crosses zero in one gesture. In binary-minus contexts (`a - 5`)
    the widget sees only the magnitude; dragging it negative splices `a - -1`,
    which is still valid Python.
    Typing mode (ctrl+click / double-click) is OURS, not imgui's: the drag is
    drawn with SLIDER_FLAGS_NO_INPUT and we swap in an input_text whose buffer
    lives on the draw_state. imgui's built-in temp input keeps its buffer
    private (and sets NoMarkEdited), so "the buffer is empty" is unobservable
    from outside — owning the buffer is what lets backspace/delete on an
    already-empty buffer delete the literal itself (splice '', widget gone),
    matching what the text caret would do.
    left_mouse_* are declared (never read) to win the event latch over the editor —
    a drag that starts on the widget latches here, so the editor doesn't grow a
    text selection while a value is being dragged. See draw_icon_selector."""
    from src.lsd.gl_gui.utils.custom_views import (push_style_var, pop_style_var,
                                                   push_style_color, pop_style_color)
    s = input_value if isinstance(input_value, str) else str(input_value)
    kind, val, fmt_back, disp = _parse_number_token(s)
    if kind is None:
        imgui.text(s)
        return False, s

    # The call site hands us pad_px of slack per side (the view - or its clip -
    # is that much wider than the token cells), so the frame fills the digits.
    # Editor-look colors: number-blue lettering on a dark frame, like the bool
    # word, with only a subtle hover/active lift instead of imgui's bright blue.
    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 1))
    push_style_color(imgui.COLOR_TEXT, 0.41, 0.59, 0.73)              # number blue
    def _pop_styles():
        pop_style_color(1)
        pop_style_var()

    imgui.set_next_item_width(draw_state.width)
    if getattr(draw_state, '_num_edit', False):
        # --- Typing mode -----------------------------------------------------
        # _num_edit_buf is LAST frame's buffer (buffer at the start of this
        # frame's input processing), so the keystroke that empties the buffer
        # doesn't itself fire the delete - only the next backspace/delete does.
        prev_buf = getattr(draw_state, '_num_edit_buf', s)
        _del_keys = (glfw.KEY_BACKSPACE, glfw.KEY_DELETE)
        del_pressed = (any(k in _del_keys for k, _ in Melty.frame_key_events)
                       or any(imgui.is_key_pressed(k, repeat=True) for k in _del_keys))
        if del_pressed and not prev_buf.strip():
            draw_state._num_edit = False
            _pop_styles()
            return True, ''
        if not getattr(draw_state, '_num_edit_started', False):
            imgui.set_keyboard_focus_here()
        _, buf = imgui.input_text("##num_tv_edit", prev_buf,
                                  flags=imgui.INPUT_TEXT_AUTO_SELECT_ALL)
        draw_state._num_edit_buf = buf
        if imgui.is_item_active():
            draw_state._num_edit_started = True
        elif getattr(draw_state, '_num_edit_started', False):
            draw_state._num_edit = False   # Enter / Esc / click-away ends this
        # The editor's tile is cached; keep it re-rendering while we hold the
        # input so the caret blinks and keystrokes land the frame they occur.
        Melty.cache.invalidate_up(draw_state._tile_id, max_depth=4, force=True)
        request_render()
        _pop_styles()
        # Live-apply parseable edits like imgui's temp input did: a same-kind
        # value keeps the literal's shape via fmt_back; a kind change (int text
        # typed over a float) splices the typed text verbatim.
        typed = buf.strip()
        nk, nv, _nf, _nd = _parse_number_token(typed) if typed else (None, None, None, None)
        if nk is not None:
            out = fmt_back(nv) if nk == kind else typed
            if out != s:
                return True, out
        return False, s

    if kind == 'int':
        speed = max(0.2, abs(val) * 0.01)
        try:
            changed, new = imgui.drag_int("##num_tv", val, change_speed=speed,
                                          min_value=0, max_value=0,
                                          flags=imgui.SLIDER_FLAGS_NO_INPUT)
        except Exception:
            _pop_styles()
            return False, s
    else:
        speed = max(0.01, abs(val) * 0.005)
        changed, new = imgui.drag_float("##num_tv", val, change_speed=speed,
                                        min_value=0, max_value=0, format=disp,
                                        flags=imgui.SLIDER_FLAGS_NO_INPUT)
    # Ctrl+click / double-click enters typing mode (imgui's own temp input is
    # disabled above): seed the buffer with the literal; the editor draws - and
    # grabs keyboard focus - next frame in this widget's place.
    if (imgui.is_item_hovered()
            and (imgui.is_mouse_double_clicked(0)
                 or (imgui.is_mouse_clicked(0) and imgui.get_io().key_ctrl))):
        draw_state._num_edit = True
        draw_state._num_edit_buf = s
        draw_state._num_edit_started = False
        request_render()
    _pop_styles()
    if changed and new != val:
        return True, fmt_back(new)
    return False, s


def _fmt_color_channel(v):
    """Format a 0..1 channel back into source keeping it a FLOAT literal (a bare
    `1` would break the all-floats tuple pattern and unmerge the token)."""
    s = f"{max(0.0, min(1.0, v)):.3f}".rstrip('0')
    return s + '0' if s.endswith('.') else s


@render_func(use_cache=True, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, z_offset=3)
def draw_color3_token(input_value, draw_state=None,
                      left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False,
                      **kwargs):
    """Inline color swatch for a float 3-tuple — ACCESSORY (lead_cells) renderer
    for 'color3' tokens (`(1.0, 0.5, 0.2)` merged by tokenize). The editor draws
    the tuple TEXT itself, normally — fully editable, caret/selection like any
    code — and this widget only gets the lead area to its LEFT, where it draws a
    swatch. Clicking the swatch opens the MELTY color-picker popover (same
    pattern as draw_tuple's swatch: popover_focused_ds identity is the open
    state, the picker window is latched — drawn every frame with closed=
    toggled — anchored under the swatch, dismissed by outside click / Esc).
    NEVER imgui's built-in popup: melty windowing has diverged (shadows,
    z-order, cached render tiles) and they don't compose. Edits splice the
    reformatted tuple back; channels stay float literals so the token re-merges.
    Draws nothing if the tuple doesn't parse (the text is still there).
    left_mouse_* declared (never read) for the event latch — see draw_icon_selector."""
    from src.lsd.gl_gui.view.mode import Mode
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_color_picker
    s = input_value if isinstance(input_value, str) else str(input_value)
    try:
        r, g, b = (float(p) for p in s.strip('()').split(','))
    except ValueError:
        return False, s

    # Square-ish swatch inset in the lead area, vertically centered on the line;
    # the extra cell width to its right is the gap before the text.
    _sw = max(6.0, min(draw_state.width - 3, draw_state.height - 4))
    _cx, _cy = imgui.get_cursor_screen_pos()
    imgui.set_cursor_screen_pos((_cx, _cy + (draw_state.height - _sw) * 0.5))
    is_open = Melty.popover_focused_ds is draw_state
    if imgui.color_button("##color3_tv", r, g, b, 1.0,
                          flags=imgui.COLOR_EDIT_NO_TOOLTIP,
                          width=_sw, height=_sw):
        Melty.popover_focused_ds = None if is_open else draw_state
        if not is_open:
            Melty._popover_open_frame = Melty.frame_count  # grace the opening click
        request_render()
    is_open = Melty.popover_focused_ds is draw_state  # reflect the close this frame

    # Fixed-size popover (closable windows can't auto-resize, the picker body is
    # raw imgui the framework can't measure): SV square + 3 channel rows + title.
    picker_h = 180 + 14 + 3 * 26 + 26
    color_changed, new_color = draw_color_picker(
        (r, g, b), name=f"{draw_state.name}_picker", closed=not is_open,
        window_pos=(0, 10), parent_window=draw_state, width=216, height=picker_h,
        mode=Mode.POPOVER)
    if is_open:
        if any(k == glfw.KEY_ESCAPE for k, _ in Melty.frame_key_events):
            Melty.popover_focused_ds = None
            request_render()
        # Keep re-rendering while a picker slider/square is being dragged so the
        # live imgui interaction updates each frame despite the editor's cache.
        if Melty.imgui_any_item_active or imgui.is_mouse_down(0):
            Melty.cache.invalidate_up(draw_state._tile_id, max_depth=10, force=True)
            request_render()
        if color_changed:
            return True, (f"({_fmt_color_channel(new_color[0])}, "
                          f"{_fmt_color_channel(new_color[1])}, "
                          f"{_fmt_color_channel(new_color[2])})")
    return False, s


# The default callback-widget set: when draw_text is called with no token_views,
# Font Awesome glyphs ("icon" tokens) become inline icon-picker dropdowns,
# True/False become double-click-to-toggle words, numeric literals become drag
# widgets, and float 3-tuples become color swatches. Add more entries here to
# make other token kinds interactive by default.
# For whole_token entries char_width is also the "this is inline" flag - the
# widget REPLACES the text at exactly token-width (len(token) cells), unless
# lead_cells=N makes it an ACCESSORY: the text draws normally (shifted N cells
# right) and the widget gets only the N-cell lead area beside it. owns_mouse
# marks widgets that consume clicks (they subscribe to the left_mouse_*
# events); for REPLACE widgets draw_text then emulates the caret placement a
# text click would have given. pad_px widens a REPLACE widget's view N px per
# side past the token cells (visual breathing room; the grid stays exact).
DEFAULT_TOKEN_VIEWS = {
    "icon": {"renderer": draw_icon_selector, "char_width": 3},
    "bool": {"renderer": draw_bool_token, "char_width": 1, "whole_token": True},
    "number": {"renderer": draw_number_token, "char_width": 1, "whole_token": True,
               "owns_mouse": True, "pad_px": 2},
    "color3": {"renderer": draw_color3_token, "char_width": 1, "whole_token": True,
               "owns_mouse": True, "lead_cells": 2},
}

# live_view() call sites get an anchor marker + nested value window (the first
# type-keyed overlay entry). Import from its own module so the widgets and
# their live_view imports stay out of this file.
from src.lsd.gl_gui.view.core_views.live_view_views import install_token_views as _install_live_view_tv
_install_live_view_tv(DEFAULT_TOKEN_VIEWS)


def _parse_col_shift(buffer_text, parse_source):
    """Uniform column delta between the rendered buffer and the parse's own
    source: the code-host route parses function spans DEDENTED while the
    buffer keeps the file's indent, so every span col sits that many cells
    left of its glyph. Compared on the first line that's non-blank in both;
    0 when the sources agree (whole-file editors)."""
    if not parse_source or parse_source is buffer_text:
        return 0
    for a, b in zip(buffer_text.split('\n', 50), parse_source.split('\n', 50)):
        if a.strip() and b.strip():
            return (len(a) - len(a.lstrip())) - (len(b) - len(b.lstrip()))
    return 0


def _draw_cst_token_views(code_tree, token_views, origin_x, origin_y, line_px, char_w, ds,
                          line_offset=0, jump_to=None):
    """Overlay pass for the TYPE-keyed entries of `token_views`: walk the code_tree
    for nodes matching a key type and call its renderer positioned at the node's
    span. Lines are 1-indexed relative to the editor's source (== code_tree.source),
    so span line 1 sits at origin_y. Runs after the text body. `root`/`line_offset`/
    `jump_to` ride along so a renderer can resolve file-absolute context (the
    live_view overlay maps its node span back to a file line)."""
    if not token_views or not isinstance(code_tree, dict):
        return
    type_specs = [(k, v) for k, v in token_views.items() if isinstance(k, type)]
    if not type_specs:
        return
    seen = set()

    def walk(node, depth=0):
        if not isinstance(node, dict) or depth > 64 or id(node) in seen:
            return
        seen.add(id(node))
        span = getattr(node, 'span', None)
        if span is not None:
            for ktype, spec in type_specs:
                if isinstance(node, ktype):
                    y = origin_y + (span.start_line - 1) * line_px
                    h = (span.end_line - span.start_line + 1) * line_px
                    x = origin_x + getattr(span, 'start_col', 0) * char_w
                    try:
                        spec["renderer"](x=x, y=y, w=max(0.0, ds.content_width - (x - origin_x)),
                                         h=h, draw_state=ds, char_w=char_w, line_px=line_px,
                                         node=node, span=span, root=code_tree,
                                         line_offset=line_offset, jump_to=jump_to)
                    except Exception:
                        pass
                    break
        for v in node.values():
            walk(v, depth + 1)

    # Cursor aware, like the inline token views: renderers position
    # themselves with set_cursor_screen_pos (the live_view markers), so the
    # caller's size-decaring area must measure from wherever the body left
    # the cursor - just below the last marker drawn.
    _save_cur = imgui.get_cursor_screen_pos()
    walk(code_tree)
    imgui.set_cursor_screen_pos(_save_cur)


# --- Symbol usages: highlight + double-click jump-to-caller -------------------
# The parse pipeline attaches {name: SymbolUsage} maps to GeneralParse nodes
# under "__symbol_usages__" (see libcst_conversion.populate_symbol_usages /
# _distribute_by_name). Each SymbolUsage carries `sites` — file-absolute
# (line, col) occurrences of the symbol within this view's source — and
# `callers` — cross-project UsageRefs. The editor washes a slight background
# behind each site whose symbol HAS callers, its color climbing a blue→orange
# heat ramp with the user count; a double-click jumps via IntelliJ (the same opener as the jump-to
# button) - toward the callers if a definition is in a view, back to the
# definition from a usage site. A single target jumps straight there, more
# open the usage-jump popup (the same latched dropdown as the code-suggestion
# popup) listing every user.

def _collect_usage_spans(code_tree, text, line_offset=0, view_path=None):
    """[(start_index, end_index, SymbolUsage, at_def)] — buffer-index spans for
    every occurrence of a symbol that has callers, from the code_tree's nested
    __symbol_usages__ maps. Sites are file-absolute; `line_offset` (the file
    line, 0-based, of buffer line 0 — usually jump_to.start) maps them into the
    buffer. A site whose text no longer matches the symbol name (buffer edited
    since the background usage pass ran) is dropped rather than highlighting
    the wrong characters.

    `at_def` flags the occurrence that IS the symbol's declaration (the one site
    on `su.definition.line` when the definition lives in THIS file, `view_path`).
    The click/wash logic is per-SITE, not per-symbol: AT the declaration you jump
    to its usages; at a usage you jump back to the declaration. Without this a
    symbol defined in-view — every LOCAL variable, since its def is always in
    scope — showed its whole usage list at every occurrence. realpath runs at most
    once per symbol here (per keystroke), keeping the per-frame wash realpath-free."""
    spans = []
    seen_nodes, seen_sites = set(), set()
    _def_in_file = {}        # id(su) -> is su declaration in THIS view file

    def _su_def_in_file(su):
        k = id(su)
        if k not in _def_in_file:
            import os
            d = getattr(su, 'definition', None)
            dp = getattr(d, 'path', None) if d is not None else None
            if dp is None or view_path is None:
                _def_in_file[k] = False
            else:
                try:
                    _def_in_file[k] = (os.path.realpath(str(dp))
                                       == os.path.realpath(str(view_path)))
                except OSError:
                    _def_in_file[k] = False
        return _def_in_file[k]

    def walk(node, depth=0):
        if not isinstance(node, dict) or depth > 64 or id(node) in seen_nodes:
            return
        seen_nodes.add(id(node))
        su_map = node.get("__symbol_usages__")
        if isinstance(su_map, dict):
            for key, su in su_map.items():
                if not getattr(su, 'callers', None):
                    continue
                # Key may be edit-proof, not the spelling (for local keys on
                # scope+name+line); the highlighted token is the SymbolUsage's name.
                name = getattr(su, 'name', key) or key
                d = getattr(su, 'definition', None)
                def_line = getattr(d, 'line', None) if d is not None else None
                def_col = getattr(d, 'column', None) if d is not None else None
                # Local-variable entries (key = scope\x1fname\x1fline) record an
                # ACCURATE binding column, so the declaration is matched by (line,
                # col) - needed to single out the binding among several occurrences
                # on one line (`[t for t in xs]`). Module/member symbols record a
                # PLACEHOLDER def column 0, so they match by line only (their
                # declaration is the lone occurrence on its def line; col-matching
                # would mark NONE of them, self-linking every in-view definition).
                is_local = isinstance(key, str) and "\x1f" in key
                for site in getattr(su, 'sites', None) or ():
                    ln, col = site
                    # Key includes the name: a bare-name target (`Window`) and a
                    # dot target (`Mode.WINDOW`) share the same (line, col).
                    skey = (ln, col, name)
                    if skey in seen_sites:
                        continue
                    seen_sites.add(skey)
                    buf_line = ln - 1 - line_offset
                    if buf_line < 0:
                        continue
                    idx = _line_col_to_index(text, buf_line, col)
                    end = idx + len(name)
                    if text[idx:end] != name:
                        # The fast import-index records the STATEMENT start col
                        # (e.g. the `class` keyword), not the symbol's own col -
                        # and the buffer may have drifted since the pass ran.
                        # Recover by finding the name (word-bounded) in the
                        # site's line; give up on that site if it's not there.
                        ls = _get_line_start(text, min(idx, len(text)))
                        le = _get_line_end(text, ls)
                        p = text.find(name, ls, le)
                        while p != -1:
                            b_ok = p == 0 or not (text[p - 1].isalnum() or text[p - 1] == '_')
                            a = p + len(name)
                            a_ok = a >= len(text) or not (text[a].isalnum() or text[a] == '_')
                            if b_ok and a_ok:
                                break
                            p = text.find(name, p + 1, le)
                        if p == -1:
                            continue
                        idx, end = p, p + len(name)
                    # This occurrence is the declaration when its file line (and,
                    # for locals, col) is the definition's AND the definition lives
                    # in this file.
                    at_def = (def_line is not None and ln == def_line
                              and (col == def_col if is_local else True)
                              and _su_def_in_file(su))
                    spans.append((idx, end, su, at_def))
        for k, v in node.items():
            if k not in ("__cst__", "__symbol_usages__"):
                walk(v, depth + 1)

    walk(code_tree)
    spans.sort(key=lambda s: s[0])
    return spans


def _usage_spans(ds, text, code_tree, line_offset=0, view_path=None):
    """Cached-per-(code_tree, text) wrapper around _collect_usage_spans. The
    top-level __symbol_usages__ map's identity rides in the key: the background
    usage pass fills it in-place on an already-rendered code_tree (fresh dict
    per compute), so its arrival must bust the cache even though the tree and
    text are unchanged."""
    if code_tree is None:
        return ()
    su_top = code_tree.get("__symbol_usages__") if isinstance(code_tree, dict) else None
    key = (id(code_tree), id(su_top), line_offset, text, str(view_path))
    if getattr(ds, '_usage_spans_key', None) != key:
        try:
            ds._usage_spans = _collect_usage_spans(code_tree, text, line_offset, view_path)
        except Exception:
            ds._usage_spans = ()
        ds._usage_spans_key = key
        ds._usage_tc = {}     # per-(su, at_def) jump-target counts; dies with span set
    return ds._usage_spans


def _usage_target_count(ds, su, at_def, view_path, view_span):
    """len() of the EXACT list the usage-jump dropdown would show for THIS
    occurrence of `su` — the wash color keys on this, so hue ≡ dropdown size.
    Raw caller count is the wrong signal: a USAGE occurrence jumps to exactly one
    place (the declaration) however many callers exist project-wide, so it must
    read cool, while the DECLARATION (`at_def`) reads hot with its full usage
    list. Memoized per (su, at_def) on the draw_state — at_def is precomputed by
    _collect_usage_spans, so this is realpath-free; the memo dies with the span
    set."""
    tc = getattr(ds, '_usage_tc', None)
    if tc is None:
        tc = ds._usage_tc = {}
    mk = (id(su), at_def)
    n = tc.get(mk)
    if n is None:
        n = len(_usage_jump_targets(su, view_path=view_path, view_span=view_span,
                                    at_def=at_def))
        tc[mk] = n
    return n


def _usage_jump_targets(su, view_path=None, view_span=None, at_def=None):
    """Ordered jump candidates (UsageRefs) for a symbol-usage click. Direction is
    per-OCCURRENCE (`at_def`, from _collect_usage_spans):
      • at_def True  → we're ON the declaration: candidates are its callers
        ("who uses this?").
      • at_def False → we're at a USAGE: the declaration (falling back to the
        callers when none was found).
    One candidate → jump straight there; several → open the usage-jump picker.
    `at_def=None` falls back to the legacy symbol-level test (is the definition
    anywhere in view_span) for callers that don't pass a per-site flag."""
    d = getattr(su, 'definition', None)
    if d is not None and getattr(d, 'path', None) is None:
        d = None
    callers = [c for c in (getattr(su, 'callers', None) or ())
               if getattr(c, 'path', None) is not None]

    if at_def is None:
        at_def = False
        if d is not None and view_path is not None and view_span:
            try:
                import os
                at_def = (os.path.realpath(str(d.path)) == os.path.realpath(str(view_path))
                          and view_span[0] <= (getattr(d, 'line', 0) or 0) <= view_span[1])
            except OSError:
                at_def = False

    if at_def:
        return callers
    return [d] if d is not None else callers


def _open_usage_ref(ref):
    """Open one UsageRef in IntelliJ — the same opener the jump-to header
    button uses. Async (daemon thread) so a slow IDE never stalls the loop."""
    import threading
    from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
    threading.Thread(target=open_in_intellij,
                     args=(str(ref.path),),
                     kwargs={"line_number": getattr(ref, 'line', None)},
                     daemon=True).start()


def _usage_ref_items(targets):
    """({label: UsageRef}, {UsageRef: tag}) rows for the usage-jump picker: the
    label is the user's enclosing scope, the dim right-aligned tag its
    file:line. Duplicate scope labels get a numeric suffix (dict keys feed
    draw_dd_menu, so they must be unique)."""
    items, tags = {}, {}
    for ref in targets:
        scope = (getattr(ref, 'scope', '') or getattr(ref, 'module_name', '')
                 or '<module>')
        label, n = scope, 2
        while label in items:
            label = f"{scope} ({n})"
            n += 1
        items[label] = ref
        p = getattr(ref, 'path', None)
        tags[ref] = f"{p.name}:{ref.line}" if p is not None else f":{ref.line}"
    return items, tags


def _usage_wash_color(n_targets):
    """Packed-ABGR wash for a usage span. The look — color + opacity vs the
    jump-target count `n_targets` (see _usage_target_count) — lives in the
    live-editable Toggles.TextEditor.usage_tint; this just clamps + packs its
    (r, g, b, a) into the int the draw list wants. Read fresh every call so a
    tweak to usage_tint shows immediately."""
    from src.lsd.gl_gui.toggles import Toggles
    r, g, b, a = Toggles.TextEditor.usage_tint(n_targets)
    pr, pg, pb, pa = (min(255, max(0, int(c * 255))) for c in (r, g, b, a))
    return (pa << 24) | (pb << 16) | (pg << 8) | pr


def _is_icon_char(c):
    """True for a Font Awesome / Private Use Area glyph (BMP PUA, U+E000–U+F8FF).
    These are the icon code points the UI embeds in strings (e.g. "\\uf054")."""
    return '\ue000' <= c <= '\uf8ff'


def _split_icons(s, base):
    """Split `s` into (substr, color_key) runs so PUA icon glyphs paint as 'icon'
    while the surrounding text keeps `base` — lets an icon embedded in a string
    token (the common case) stand out without recolouring the whole literal."""
    start, run_icon = 0, None
    for j, c in enumerate(s):
        ic = _is_icon_char(c)
        if run_icon is None:
            run_icon = ic
        elif ic != run_icon:
            yield s[start:j], ('icon' if run_icon else base)
            start, run_icon = j, ic
    if start < len(s):
        yield s[start:], ('icon' if run_icon else base)


def _tokenize_raw(text):
    """Yields (text, color_key) tuples with Darcula-style token categories.
    Raw pass — see tokenize() below for the unary-sign merge."""
    i = 0
    n = len(text)

    while i < n:
        # --- Comments ---
        if text[i] == '#':
            end = text.find('\n', i)
            end = end if end != -1 else n
            yield text[i:end], 'comment'
            i = end

        # --- Decorators ---
        elif text[i] == '@' and (i == 0 or text[i - 1] in '\n '):
            end = i + 1
            while end < n and (text[end].isalnum() or text[end] in '_.'):
                end += 1
            yield text[i:end], 'decorator'
            i = end

        # --- Triple-quoted strings ---
        elif i + 2 < n and text[i:i + 3] in ('"""', "'''"):
            quote = text[i:i + 3]
            end = text.find(quote, i + 3)
            end = end + 3 if end != -1 else n
            yield from _split_icons(text[i:end], 'string_doc')
            i = end

        # --- String prefixes (f", r", b", rb", etc.) ---
        elif (text[i] in 'fFrRbBuU'
              and i + 1 < n
              and (text[i + 1] in ('"', "'")
                   or (i + 2 < n and text[i + 1] in 'fFrRbBuU' and text[i + 2] in ('"', "'")))):
            prefix_end = i + 1
            if prefix_end < n and text[prefix_end] in 'fFrRbBuU':
                prefix_end += 1
            quote = text[prefix_end]
            if prefix_end + 2 < n and text[prefix_end:prefix_end + 3] in ('"""', "'''"):
                triple = text[prefix_end:prefix_end + 3]
                end = text.find(triple, prefix_end + 3)
                end = end + 3 if end != -1 else n
            else:
                end = prefix_end + 1
                escaped = False
                while end < n:
                    if escaped:
                        escaped = False
                        end += 1
                        continue
                    if text[end] == '\\':
                        escaped = True
                        end += 1
                        continue
                    if text[end] == quote:
                        end += 1
                        break
                    end += 1
            yield from _split_icons(text[i:end], 'string')
            i = end

        # --- Strings ---
        elif text[i] in ('"', "'"):
            quote = text[i]
            end = i + 1
            escaped = False
            while end < n:
                if escaped:
                    escaped = False
                    end += 1
                    continue
                if text[end] == '\\':
                    escaped = True
                    end += 1
                    continue
                if text[end] == quote:
                    end += 1
                    break
                end += 1
            yield from _split_icons(text[i:end], 'string')
            i = end

        # --- Words ---
        elif text[i].isalpha() or text[i] == '_':
            end = i
            while end < n and (text[end].isalnum() or text[end] == '_'):
                end += 1
            word = text[i:end]

            if word in KEYWORD_CONSTS:
                yield word, 'bool' if word in ('True', 'False') else 'keyword_const'
            elif word in OPERATOR_WORDS:
                yield word, 'operator_word'
            elif word in KEYWORDS:
                yield word, 'keyword'
            elif word in BUILTIN_PSEUDO:
                yield word, 'builtin_pseudo'
            else:
                yield word, 'default'
            i = end

        # --- Numbers ---
        elif text[i].isdigit() or (text[i] == '.' and i + 1 < n and text[i + 1].isdigit()):
            end = i
            if end + 1 < n and text[end] == '0' and text[end + 1] in 'xXoObB':
                end += 2
                while end < n and text[end] in '0123456789abcdefABCDEF_':
                    end += 1
            else:
                has_dot = False
                while end < n and (text[end].isdigit() or text[end] in '._eE'):
                    if text[end] == '.':
                        if has_dot:
                            break
                        has_dot = True
                    if text[end] in 'eE' and end + 1 < n and text[end + 1] in '+-':
                        end += 1
                    end += 1
            yield text[i:end], 'number'
            i = end

        # --- Everything else (operators, punctuation, whitespace) ---
        # A bare PUA glyph (an icon not inside a string literal) lands here too -
        # colour it as an icon rather than default.
        else:
            yield text[i], 'icon' if _is_icon_char(text[i]) else 'default'
            i += 1


def _unary_sign_context(prev):
    """True when a `+`/`-` in front of a number literal reads as a SIGN rather
    than binary arithmetic, judged by the last significant token: nothing yet,
    a keyword (`return -5`), a word operator (`x and -5`), or a single
    operator/punctuation char (`= ( [ { , : ;` …). Identifiers, literals and
    closing brackets mean binary (`a - 5`, `5 - 3`, `f() - 5`)."""
    if prev is None:
        return True
    tok, kind = prev
    if kind in ('keyword', 'operator_word'):
        return True
    if kind == 'default':
        # 'default' covers identifiers AND single operator/punctuation chars.
        return len(tok) == 1 and tok in '=+-*/%<>&|^~,([{:;@'
    return False


def _merge_unary_signs(stream):
    """Merge pass: a unary `+`/`-` attached directly to a numeric literal is
    merged INTO the 'number' token (`-5`, `+0.5`) when the preceding
    significant token says it's a sign, not binary arithmetic. The inline number
    drag widget then owns the sign, so a drag can cross zero in one gesture.
    Whitespace arrives as its own raw tokens, so a held sign only merges when
    the digits follow immediately (`- 5` stays two tokens)."""
    prev = None   # last significant (non-whitespace, non-comment) token
    held = None   # '+'/'-' waiting to see if a number follows it
    for tok, kind in stream:
        if held is not None:
            if kind == 'number':
                merged = (held[0] + tok, 'number')
                yield merged
                prev = merged
                held = None
                continue
            yield held
            prev = held
            held = None
        if kind == 'default' and tok in ('+', '-') and _unary_sign_context(prev):
            held = (tok, kind)
            continue
        yield tok, kind
        if not tok.isspace() and kind != 'comment':
            prev = (tok, kind)
    if held is not None:
        yield held


def _is_float_literal(tok):
    """True for a decimal float literal token (after sign merge): has a '.' or
    exponent, and isn't a hex/oct/bin int (whose digits can contain 'e')."""
    t = tok.lstrip('+-').lower()
    return not t.startswith(('0x', '0o', '0b')) and ('.' in t or 'e' in t)


def _merge_color_tuples(stream):
    """Merge pass: `(f, f, f)` — an open paren, exactly three FLOAT literals
    separated by commas (spaces allowed), closed on the same line — becomes one
    'color3' token, rendered by the inline color-picker widget. Only fires in
    tuple-literal positions (`x = (...)`, `tint=(...)`, `return (...)`), judged
    by the token before the '(' via _unary_sign_context — an identifier or
    closing bracket there means a CALL's argument list (`f(0.1, 0.5, 1.0)`),
    which stays untouched."""
    prev = None  # last significant token, for the call-vs-tuple judgement
    buf = []     # tokens collected since a candidate '('
    nfloats = 0
    expect = None  # 'num' | 'comma' - alternates while buffering
    for tok, kind in stream:
        while True:
            if not buf:
                if tok == '(' and kind == 'default' and _unary_sign_context(prev):
                    buf = [(tok, kind)]
                    nfloats, expect = 0, 'num'
                else:
                    yield tok, kind
                    if not tok.isspace() and kind != 'comment':
                        prev = (tok, kind)
                break
            if tok == ' ':
                buf.append((tok, kind))
                break
            if kind == 'number' and expect == 'num' and nfloats < 3 and _is_float_literal(tok):
                buf.append((tok, kind))
                nfloats += 1
                expect = 'comma'
                break
            if tok == ',' and kind == 'default' and expect == 'comma' and nfloats < 3:
                buf.append((tok, kind))
                expect = 'num'
                break
            if tok == ')' and kind == 'default' and expect == 'comma' and nfloats == 3:
                buf.append((tok, kind))
                merged = (''.join(t for t, _ in buf), 'color3')
                yield merged
                prev = merged
                buf = []
                break
            # Failed match: flush the buffer and re-process this token from
            # scratch (it may itself open a new candidate '(').
            for b in buf:
                yield b
                if not b[0].isspace():
                    prev = b
            buf = []
            continue
    for b in buf:
        yield b


def tokenize(text):
    """Yields (text, color_key) tuples with Darcula-style token categories.

    Full pipeline: raw scan → unary-sign merge (`-5` is one number token, so
    the drag widget can cross zero) → color-tuple merge (`(1.0, 0.5, 0.2)` is
    one 'color3' token, rendered as an inline color picker)."""
    return _merge_color_tuples(_merge_unary_signs(_tokenize_raw(text)))


# --- Viewport tokenization ---------------------------------------------------
# Re-tokenizing the whole buffer on every keystroke is the dominant per-edit
# cost on a long span (~40ms of tokenize+vcols for ~1600 lines). But only the
# lines inside the clip rect are ever drawn, so we tokenize ONLY the visible
# window each frame - O(visible) instead of O(buffer), which also makes scroll
# and re very cheap.
#
# The one thing a window can't see on its own is the lexer state at its top: a
# visible line may start inside a multi-line `"""` docstring opened far above.
# We track that with `_line_open` - one entry per line, the string opener active
# at that line's start (None when outside any string) - maintained incrementally
# (only the edited lines are re-scanned). To tokenize a window whose first line
# starts inside a string, PREPEND that opener so `_tokenize_raw` resumes
# in-string, then strip it back off; this reuses the tokens UNMODIFIED, so the
# window's coloring is identical to the matching portion of `list(tokenize(text))`.

def _opener_quote(tok):
    '''The string-opening quote of a string token, skipping any f/r/b/u prefix:
    \'\"\"\"\', "\'\'\'", \'\"\' or "\'".'''
    i, n = 0, len(tok)
    while i < n and tok[i] in 'fFrRbBuU':
        i += 1
    if tok[i:i + 3] in ('"""', "'''"):
        return tok[i:i + 3]
    return tok[i:i + 1]


def _line_offsets(text):
    """Char offset of each line start; offs[i] is the start offset of line i
    (offs[0] == 0). len(offs) == number of lines."""
    offs = [0]
    i = text.find('\n')
    while i != -1:
        offs.append(i + 1)
        i = text.find('\n', i + 1)
    return offs


def _line_open_full(text):
    """(line_offsets, line_open) computed from scratch. line_open[i] is the
    string state active at the START of line i — None outside any string, else
    a (closing_quote, color_kind) pair for the multi-line string spanning into
    the line. The kind is carried because a PREFIXED triple (`r'''…`, `f\"\"\"…`)
    colors as 'string', not 'string_doc' — only a bare triple is 'string_doc'.
    Derived straight from `_tokenize_raw`, so it agrees with `tokenize()`
    exactly. O(buffer); used on first render, then maintained incrementally."""
    offs = _line_offsets(text)
    line_open = [None] * len(offs)
    line = 0
    for tok, kind in _tokenize_raw(text):
        if tok == '\n':
            line += 1                       # bare newline → next line starts clean
        elif '\n' in tok:
            # Only string/string_doc tokens carry embedded newlines; each line
            # the string continues onto starts inside it.
            qk = (_opener_quote(tok), kind) if kind in ('string', 'string_doc') else None
            for ch in tok:
                if ch == '\n':
                    line += 1
                    if line < len(line_open):
                        line_open[line] = qk
    return offs, line_open


def _diff_span(a, b):
    """(common_prefix_len, a_suffix_start, b_suffix_start) for two strings.
    Binary search on slice equality so the comparisons run at C speed — a
    mid-buffer single-char edit costs O(log n) compares, not the O(n) of a
    Python char loop (the difference between ~6ms and ~0.1ms on a big file)."""
    n = min(len(a), len(b))
    plo, phi = 0, n
    while plo < phi:                      # longest common prefix
        mid = (plo + phi + 1) // 2
        if a[:mid] == b[:mid]:
            plo = mid
        else:
            phi = mid - 1
    lo = plo
    slo, shi = 0, n - lo                  # longest common suffix (no prefix overlap)
    while slo < shi:
        mid = (slo + shi + 1) // 2
        if a[len(a) - mid:] == b[len(b) - mid:]:
            slo = mid
        else:
            shi = mid - 1
    return lo, len(a) - slo, len(b) - slo


def _update_line_open(prev_text, prev_offs, prev_open, text):
    """Incrementally recompute (line_offsets, line_open) for `text`. Re-scans
    only from the nearest clean line at/before the edit until the lexer state
    reconverges with the unchanged tail at a clean line boundary; everything
    before/after is reused. Output equals `_line_open_full(text)`."""
    if prev_text is None or prev_offs is None or prev_open is None or text == prev_text:
        return (prev_offs, prev_open) if text == prev_text and prev_offs is not None \
            else _line_open_full(text)

    olen, nlen = len(prev_text), len(text)
    lo, _old_hi, new_hi = _diff_span(prev_text, text)
    delta = nlen - olen

    new_offs = _line_offsets(text)
    cf = bisect.bisect_right(new_offs, lo) - 1      # first changed line (new coords)
    # line_open is valid through line cf (depends only on unchanged preceding
    # lines). Back up to the last clean line at/before cf to start the re-lex.
    sl = cf
    while sl > 0 and prev_open[sl] is not None:
        sl -= 1
    start_off = new_offs[sl]
    old_clean = {prev_offs[k]: k for k in range(len(prev_offs)) if prev_open[k] is None}

    tail = [None]                # line_open for line sl (clean by construction)
    off = start_off
    stop_old = None
    for tok, kind in _tokenize_raw(text[start_off:]):
        if '\n' not in tok:
            off += len(tok)
            continue
        qk = (_opener_quote(tok), kind) if kind in ('string', 'string_doc') else None
        for ch in tok:
            off += 1
            if ch != '\n':
                continue
            state = None if tok == '\n' else qk
            if state is None and off >= new_hi:
                oc = old_clean.get(off - delta)   # same clean line in the old tail?
                if oc is not None:
                    stop_old = oc                 # reconverged → reuse old suffix
                    break
            tail.append(state)
        if stop_old is not None:
            break

    new_open = prev_open[:sl] + tail + (prev_open[stop_old:] if stop_old is not None else [])
    return new_offs, new_open


def _resume_in_string(body, opener):
    """Tokenize `body` given that it BEGINS inside a string. `opener` is the
    (closing_quote, color_kind) pair recorded in `line_open` (the kind matters:
    a prefixed triple colors 'string', a bare triple 'string_doc'). Emits the
    resumed string prefix with that kind, then tokenizes the code after it
    closes — seeding the merge passes with a string-kind prev so that code gets
    the same unary-sign/color-tuple context it has globally (where a closed
    string precedes it). Matches the global coloring char-for-char."""
    quote, skind = opener
    if len(quote) == 3:                        # triple: next literal close
        c = body.find(quote)
        cut = c + 3 if c != -1 else len(body)
    else:                                      # single/double: first unescaped quote
        cut, esc = len(body), False
        for p, ch in enumerate(body):
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == quote:
                cut = p + 1
                break
    head = list(_split_icons(body[:cut], skind))

    def _seeded():
        yield ('\x00', 'string')               # sentinel prev: can't merge, dropped below
        yield from _tokenize_raw(body[cut:])
    rest = list(_merge_color_tuples(_merge_unary_signs(_seeded())))
    if rest and rest[0] == ('\x00', 'string'):
        rest.pop(0)
    return head + rest


def _window_tokens(text, line_offs, line_open, v0, v1, lookback=12):
    """Merged tokens for just the line range [v0, v1] (plus `lookback` lines of
    context above, so the merge passes have correct left-context for v0's first
    line), and the absolute (start_line, start_offset) the first token sits at.
    The per-character coloring matches the corresponding span of
    `list(tokenize(text))` — including windows that open inside a docstring."""
    nlines = len(line_offs)
    if nlines == 0:
        return 0, 0, []
    v0 = max(0, min(v0, nlines - 1))
    v1 = max(v0, min(v1, nlines - 1))
    wl = max(0, v0 - lookback)
    start_off = line_offs[wl]
    end_off = line_offs[v1 + 1] if v1 + 1 < nlines else len(text)
    opener = line_open[wl] if wl < len(line_open) else None
    body = text[start_off:end_off]
    toks = _resume_in_string(body, opener) if opener else list(tokenize(body))
    return wl, start_off, toks


_LINE_STARTS_CACHE: dict = {}   # id(text) -> (text, [line-start char offsets])


def _line_starts(text):
    """Char index where each line begins, memoized by text IDENTITY. The editor
    renders one buffer in tight per-span loops, so this is ~always a hit; the id
    key is guarded by holding the text ref (`e[0] is text`) so a GC'd id can't
    alias a different string. Turns _index_to_line_col / _line_col_to_index from
    O(index)/O(line) buffer scans into O(log n) bisects — the usage-wash loop
    calls them per span EVERY frame, so on a long buffer with many spans the old
    scan was O(spans x buffer) (the add-a-line frame-time blowup)."""
    e = _LINE_STARTS_CACHE.get(id(text))
    if e is not None and e[0] is text:
        return e[1]
    starts = [0]
    ap = starts.append
    i = text.find('\n')
    while i != -1:
        ap(i + 1)
        i = text.find('\n', i + 1)
    if len(_LINE_STARTS_CACHE) > 8:
        _LINE_STARTS_CACHE.clear()       # bounded: just a few open buffers, no leak
    _LINE_STARTS_CACHE[id(text)] = (text, starts)
    return starts


def _index_to_line_col(text, index):
    starts = _line_starts(text)
    line = bisect.bisect_right(starts, index) - 1
    if line < 0:
        line = 0
    return line, index - starts[line]


def _line_col_to_index(text, line, col):
    starts = _line_starts(text)
    if line < 0:
        return 0
    if line >= len(starts):
        return len(text)
    base = starts[line]
    line_end = starts[line + 1] - 1 if line + 1 < len(starts) else len(text)
    return min(base + col, line_end)


def _get_line_start(text, index):
    nl = text.rfind('\n', 0, index)
    return nl + 1 if nl != -1 else 0


def _get_line_end(text, index):
    nl = text.find('\n', index)
    return nl if nl != -1 else len(text)


def _mono_char_w():
    """Glyph advance for the monospace editor font (current imgui font)."""
    return imgui.calc_text_size("0").x


def _build_vcols(text, tokens, token_views):
    """Per-source-char VISUAL-COLUMN map for inline token-view widths. Returns a
    list `vcols` (len = len(text)+1) where vcols[i] = the visual column (in cells,
    line-relative — reset after each '\\n') at which source char i starts. A
    char_width-N inline widget thus reserves N cells visually while staying ONE
    source character for editing/caret. Returns None when no inline views apply
    (the fast path: 1 char == 1 cell everywhere)."""
    # Whole-token widgets are exactly token-width (1 cell per token char), so
    # they don't disturb the column map - except lead_cells accessory views,
    # which shift the token's text right by the lead. Only per-char views with
    # a widened char_width and lead_cells views need vcols at all.
    def _bends_grid(v):
        if not isinstance(v, dict) or v.get("char_width") is None:
            return False
        return bool(v.get("lead_cells")) if v.get("whole_token") else True
    if not token_views or not any(
            isinstance(k, str) and _bends_grid(v) for k, v in token_views.items()):
        return None
    n = len(text)
    vcols = [0.0] * (n + 1)
    col = 0.0
    i = 0
    for tok, ck in tokens:
        view = token_views.get(ck) if isinstance(ck, str) else None
        cw = view.get("char_width") if (view and view.get("char_width") is not None) else None
        if cw is not None and view.get("whole_token"):
            # Token text is 1 cell per char; an accessory view's lead_cells
            # shift where the text starts (the widget takes in the lead area).
            if tok and '\n' not in tok:
                col += view.get("lead_cells", 0)
            cw = None
        for ch in tok:
            if i >= n:
                break
            vcols[i] = col
            col = 0.0 if ch == '\n' else col + (cw if cw is not None else 1.0)
            i += 1
    vcols[n] = col
    return vcols


class _WinVCols:
    """Window-relative visual-column map. `arr[i]` is the line-relative visual
    column (in cells) of source char `off + i`, where `arr` came from
    `_build_vcols` over just the rendered window. `cell(idx)` returns the column
    for an ABSOLUTE source index, or None when `idx` falls outside the window —
    callers then use the plain character column, which is correct because
    off-window positions are viewport-culled and never actually drawn."""
    __slots__ = ('arr', 'off')

    def __init__(self, arr, off):
        self.arr, self.off = arr, off

    def cell(self, idx):
        i = idx - self.off
        return self.arr[i] if 0 <= i < len(self.arr) else None


def _char_pos_to_xy(text, index, origin_x, origin_y, line_px, vcols=None):
    line, col = _index_to_line_col(text, index)
    vx = vcols.cell(index) if vcols is not None else None
    if vx is None:
        vx = col                            # off-window / no inline widgets: plain column
    x = origin_x + vx * _mono_char_w()
    y = origin_y + line * line_px
    return x, y


def _xy_to_char_index(text, mx, my, origin_x, origin_y, line_px, vcols=None):
    lines = text.split('\n')
    line_num = int((my - origin_y) / line_px)
    line_num = max(0, min(line_num, len(lines) - 1))

    line_text = lines[line_num]
    char_w = _mono_char_w()
    abs_start = 0
    for l in range(line_num):
        abs_start += len(lines[l]) + 1

    if vcols is None:
        col = round((mx - origin_x) / char_w) if char_w else 0
    else:
        # Pick the source col on this line whose visual position is closest to the
        # click, so a wide inline widget reads as a single caret stop. Outside the
        # window/ range (clicks are always inside it) the plain column `c` is used.
        target = (mx - origin_x) / char_w if char_w else 0.0
        best_col, best_d = 0, float('inf')
        for c in range(len(line_text) + 1):
            cell = vcols.cell(abs_start + c)
            cell = float(c) if cell is None else cell
            d = abs(cell - target)
            if d < best_d:
                best_d, best_col = d, c
            elif cell - target > 1.0:
                break
        col = best_col
    col = max(0, min(col, len(line_text)))
    return min(abs_start + col, len(text))


def _word_boundary_left(text, pos):
    if pos <= 0:
        return 0
    pos -= 1
    while pos > 0 and text[pos] in WORD_DELIMITERS:
        pos -= 1
    while pos > 0 and text[pos - 1] not in WORD_DELIMITERS:
        pos -= 1
    return pos


def _word_boundary_right(text, pos):
    n = len(text)
    if pos >= n:
        return n
    while pos < n and text[pos] in WORD_DELIMITERS:
        pos += 1
    while pos < n and text[pos] not in WORD_DELIMITERS:
        pos += 1
    return pos


# Brackets select one-at-a-time even when adjacent (so '{ ['' etc. are
# separately selectable), while runs of other punctuation (==, +=, ...) group.
SOLO_CHARS = '(){}[]'


def _char_class(c):
    """Character class for double-click selection units. Runs of the same class
    select together; newlines and brackets are one char per unit."""
    if c in SOLO_CHARS:
        return 'solo'
    if c.isalnum() or c == '_':
        return 'word'
    if c == '\n':
        return 'nl'
    if c in ' \t\r':
        return 'space'
    return 'punct'


def _select_unit_left(text, pos):
    """Start of the click-selection unit containing `pos`.

    Unlike _word_boundary_left (word navigation, which skips over punctuation to
    the next identifier), this anchors on the character under the caret and
    grows a run of its own class — so a lone '{' or '(', a run of operators, a
    blank-line newline, or a stretch of spaces is each selectable on its own.
    """
    n = len(text)
    if pos <= 0:
        return 0
    i = pos if pos < n else pos - 1
    cls = _char_class(text[i])
    if cls in ('nl', 'solo'):
        return i  # a single newline (blank line) or bracket is its own unit
    start = i
    while start > 0 and _char_class(text[start - 1]) == cls:
        start -= 1
    return start


def _select_unit_right(text, pos):
    """End of the click-selection unit containing `pos` (see _select_unit_left)."""
    n = len(text)
    if pos >= n:
        return n
    cls = _char_class(text[pos])
    if cls in ('nl', 'solo'):
        return pos + 1
    end = pos
    while end < n and _char_class(text[end]) == cls:
        end += 1
    return end


def _unit_left_of(text, pos):
    """Start of the click-selection unit ending just LEFT of the caret at `pos`
    — the span a ctrl-backspace removes. Anchors on text[pos-1] (the char left of
    the caret), unlike _select_unit_left, which anchors on the char UNDER the
    caret for double-click selection. Anchoring on the right char there made
    ctrl-backspace a no-op whenever the caret sat just before a bracket/operator
    or newline (the common case in code)."""
    if pos <= 0:
        return 0
    cls = _char_class(text[pos - 1])
    if cls in ('nl', 'solo'):
        return pos - 1  # a single bracket or newline is its own unit
    start = pos - 1
    while start > 0 and _char_class(text[start - 1]) == cls:
        start -= 1
    return start


def _get_indent(text, index):
    ls = _get_line_start(text, index)
    indent = 0
    while ls + indent < len(text) and text[ls + indent] == ' ':
        indent += 1
    return indent


def _unclosed_opener(text, pos):
    """Index of the innermost (, [ or { still open just before `pos`, ignoring
    brackets inside strings and # comments, or None.

    Forward-scans a bounded window [start, pos) maintaining a stack of open
    bracket positions while tracking ' / " strings (incl. triple-quoted, with
    backslash escapes) and line comments — so a bracket in a `# (note)` comment
    or a `"("` literal is NOT mistaken for real syntax (that was the bug behind
    continuation lines indenting way out: a `(` in a comment far above was read
    as the enclosing bracket). `start` is snapped to a line boundary; a multi-
    line string straddling that boundary may misparse, but only ever degrades to
    'no bracket' (the plain-indent fallback), never a spurious match."""
    start = max(0, pos - 4000)
    if start:
        start = text.rfind('\n', 0, start) + 1   # snap to a line start
    stack = []
    quote = None          # active string delimiter ("'", '"', "'''", '"""'), or None
    i = start
    while i < pos:
        c = text[i]
        if quote is not None:
            if c == '\\' and len(quote) == 1:    # escape, only in single-char strings
                i += 2
                continue
            if text.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
            i += 1
        elif c == '#':                           # line comment → skip to EOL
            nl = text.find('\n', i)
            if nl == -1:
                break
            i = nl + 1
        elif c == '"' or c == "'":
            quote = c * 3 if text.startswith(c * 3, i) else c
            i += len(quote)
        elif c in '([{':
            stack.append(i)
            i += 1
        elif c in ')]}':
            if stack:
                stack.pop()
            i += 1
        else:
            i += 1
    return stack[-1] if stack else None


def _open_bracket_indent(text, pos):
    """If `pos` sits inside an unclosed (, [ or {, the indent (space count) a
    line opened there should take to align with that bracket's scope; else None.
    Lets Enter inside a multi-line call/list/dict line up its continuation
    instead of falling back to the line's own (often zero) indent.

    Aligns just PAST the opener when content follows it on the same line (visual
    style: `foo(a,` → next line under `a`); otherwise a hanging indent of the
    opener line's own indent + one tab (`foo(` at line end → +4)."""
    op = _unclosed_opener(text, pos)
    if op is None:
        return None
    ls = _get_line_start(text, op)
    line_end = text.find('\n', op)
    if line_end == -1:
        line_end = len(text)
    if text[op + 1:line_end].strip():        # content after the opener
        return (op - ls) + 1                 # align just past it
    return _get_indent(text, op) + 4         # hanging indent


def _prev_indent_stop(text, line_start, col):
    """The indent column a backspace in leading whitespace should land on: the
    previous meaningful stop strictly left of `col`, using the same ([{ cue as
    Enter/Tab. Inside a bracket continuation a line DEEPER than the cue steps
    back toward it one nesting level at a time (cue + 4k); at or below the cue —
    including a misaligned line shallower than it — it falls to the previous
    4-col tab stop, NOT straight out to column 0. Outside a bracket it's always
    the previous 4-col tab stop (the prior behaviour)."""
    opener = _unclosed_opener(text, line_start)
    if opener is not None:
        cue = _open_bracket_indent(text, line_start)
        if col > cue:
            return cue + 4 * ((col - 1 - cue) // 4)   # step back toward the cue
    return ((col - 1) // 4) * 4                        # at/below cue → 4-col stop


def _indent_lines(text, lo, hi, dedent):
    """Indent (or dedent) every line covered by [lo, hi] by one tab stop.
    Returns (new_text, new_lo, new_hi). For selections, new_lo snaps to the
    start of the first affected line so the whole shifted block stays highlighted."""
    indent = '    '
    has_sel = lo != hi

    old_lines = text.split('\n')
    old_starts = [0]
    for line in old_lines:
        old_starts.append(old_starts[-1] + len(line) + 1)

    def line_of(pos):
        L = 0
        while L + 1 < len(old_lines) and old_starts[L + 1] <= pos:
            L += 1
        return L

    line_lo = line_of(lo)
    line_hi = line_of(hi - 1) if has_sel else line_lo

    new_lines = list(old_lines)
    delta = [0] * len(old_lines)  # positive = chars removed, negative = chars added
    for i in range(line_lo, line_hi + 1):
        if dedent:
            n = 0
            while n < len(indent) and n < len(new_lines[i]) and new_lines[i][n] == ' ':
                n += 1
            new_lines[i] = new_lines[i][n:]
            delta[i] = n
        else:
            new_lines[i] = indent + new_lines[i]
            delta[i] = -len(indent)

    new_text = '\n'.join(new_lines)
    new_starts = [0]
    for line in new_lines:
        new_starts.append(new_starts[-1] + len(line) + 1)

    def adjust(pos):
        L = line_of(pos)
        old_col = pos - old_starts[L]
        d = delta[L]
        if d > 0:
            new_col = max(0, old_col - d)
        else:
            new_col = old_col - d  # d negative, so subtract → add
        new_col = min(new_col, len(new_lines[L]))
        return new_starts[L] + new_col

    if has_sel:
        return new_text, new_starts[line_lo], adjust(hi)
    return new_text, adjust(lo), adjust(hi)


def _reindent_paste(clipboard, target):
    """Re-indent a pasted block so its FIRST line lands exactly at the caret
    (`target` is the whitespace prefix already before the caret) and the rest
    keep their indentation RELATIVE to that first line. The result is the string
    to splice at the caret (it does NOT include the caret's existing prefix).

    The first line is stripped of its own leading whitespace and dropped right
    at the caret; every later line is re-indented by (its indent − `base`) on
    top of `target`. `base` is normally the first line's own indent. But a first
    line that is SHALLOWER than the block body while being a complete statement —
    not a block opener (trailing `:`) and not an unclosed continuation (net-open
    bracket / trailing backslash) — can only be that shallow because the
    selection clipped its leading indent. There we anchor on the body's own base
    instead, so the first line aligns with its sibling statements rather than the
    body getting shoved in by the spurious gap (a `def foo():` header or a
    `foo(arg1,` continuation still anchors on the first line, so its body nests /
    stays aligned). Blank lines stay empty so no trailing whitespace is added."""
    lines = clipboard.split('\n')
    nb = [(i, len(l) - len(l.lstrip(' '))) for i, l in enumerate(lines) if l.strip()]
    if not nb:
        return clipboard
    target_n = len(target)
    first_i, base = nb[0]
    body_indents = [ind for _, ind in nb[1:]]
    if body_indents:
        body_base = min(body_indents)
        head = lines[first_i].rstrip()
        opens_block = head.endswith(':')
        net_open = sum((c in '([{') - (c in ')]}') for c in head)
        continues = net_open > 0 or head.endswith('\\')
        if base < body_base and not opens_block and not continues:
            base = body_base
    out = []
    for i, l in enumerate(lines):
        if not l.strip():
            out.append('')
        elif i == first_i:
            out.append(l.lstrip(' '))
        else:
            rel = (len(l) - len(l.lstrip(' '))) - base
            out.append(' ' * max(0, target_n + rel) + l.lstrip(' '))
    return '\n'.join(out)


def _toggle_comment(text, lo, hi):
    """Toggle '# ' Python comments on lines covered by [lo, hi].
    Returns (new_text, new_lo, new_hi). Empty lines are skipped. If every
    non-empty affected line is already commented, uncomments them all;
    otherwise comments them at the min-indent column for visual alignment."""
    has_sel = lo != hi

    old_lines = text.split('\n')
    old_starts = [0]
    for line in old_lines:
        old_starts.append(old_starts[-1] + len(line) + 1)

    def line_of(pos):
        L = 0
        while L + 1 < len(old_lines) and old_starts[L + 1] <= pos:
            L += 1
        return L

    line_lo = line_of(lo)
    line_hi = line_of(hi - 1) if has_sel else line_lo

    def indent_of(line):
        return len(line) - len(line.lstrip(' '))

    non_empty = [i for i in range(line_lo, line_hi + 1) if old_lines[i].strip()]
    if not non_empty:
        return text, lo, hi

    all_commented = all(old_lines[i].lstrip(' ').startswith('#') for i in non_empty)
    min_indent = min(indent_of(old_lines[i]) for i in non_empty)

    new_lines = list(old_lines)
    edits = {}  # line_idx -> (col, delta): chars inserted (>0) or removed (<0) at col
    for i in non_empty:
        line = old_lines[i]
        if all_commented:
            ind = indent_of(line)
            after = line[ind:]
            if after.startswith('# '):
                new_lines[i] = line[:ind] + after[2:]
                edits[i] = (ind, -2)
            else:
                new_lines[i] = line[:ind] + after[1:]
                edits[i] = (ind, -1)
        else:
            new_lines[i] = line[:min_indent] + '# ' + line[min_indent:]
            edits[i] = (min_indent, 2)

    new_text = '\n'.join(new_lines)
    new_starts = [0]
    for line in new_lines:
        new_starts.append(new_starts[-1] + len(line) + 1)

    def adjust(pos):
        L = line_of(pos)
        old_col = pos - old_starts[L]
        if L in edits:
            col, d = edits[L]
            if d > 0:
                new_col = old_col + d if old_col >= col else old_col
            else:
                if old_col <= col:
                    new_col = old_col
                else:
                    new_col = max(col, old_col + d)
        else:
            new_col = old_col
        new_col = min(new_col, len(new_lines[L]))
        return new_starts[L] + new_col

    if has_sel:
        return new_text, new_starts[line_lo], adjust(hi)
    return new_text, adjust(lo), adjust(hi)


def _has_selection(ds):
    return ds.text_selection_start != ds.text_selection_end


def _sel_range(ds):
    return min(ds.text_selection_start, ds.text_selection_end), max(ds.text_selection_start, ds.text_selection_end)


def _delete_selection(text, ds):
    lo, hi = _sel_range(ds)
    return text[:lo] + text[hi:], lo


def _find_matches(text, term):
    """Case-insensitive, non-overlapping substring match ranges (start, end)."""
    matches = []
    term = str(term) if term else ""
    if not term:
        return matches
    low_text = text.lower()
    low_term = term.lower()
    start = 0
    while True:
        idx = low_text.find(low_term, start)
        if idx == -1:
            break
        matches.append((idx, idx + len(low_term)))
        start = idx + len(low_term)
    return matches


def _word_under_cursor(text, pos):
    """The identifier-like token the caret sits in (or just past) as
    (start, end, word) — or None when the caret isn't on a word character.

    'Word' is the alnum/underscore run (the same class double-click selection
    uses), so it spans a whole identifier and nothing else — no dots, no
    operators, no surrounding punctuation. Purely positional: no CST or syntax
    metadata is consulted."""
    n = len(text)
    if n == 0:
        return None
    # Prefer the char under the caret; fall back to the one just left of it so a
    # caret resting at a word's right edge still picks that word.
    i = pos
    if i >= n or _char_class(text[i]) != 'word':
        i = pos - 1
    if i < 0 or i >= n or _char_class(text[i]) != 'word':
        return None
    start = i
    while start > 0 and _char_class(text[start - 1]) == 'word':
        start -= 1
    end = i + 1
    while end < n and _char_class(text[end]) == 'word':
        end += 1
    return start, end, text[start:end]


def _word_match_ranges(text, word):
    """Whole-word (identifier-bounded) occurrences of `word` in `text` as
    (start, end) ranges — a dumb, case-sensitive character match that ignores
    all syntax/CST metadata. A hit is rejected when an adjacent character is a
    word char, so `i` never matches inside `if` and `id` never inside `width`."""
    ranges = []
    if not word:
        return ranges
    wlen = len(word)
    n = len(text)
    start = 0
    while True:
        idx = text.find(word, start)
        if idx == -1:
            break
        b_ok = idx == 0 or _char_class(text[idx - 1]) != 'word'
        a = idx + wlen
        a_ok = a >= n or _char_class(text[a]) != 'word'
        if b_ok and a_ok:
            ranges.append((idx, a))
        start = idx + wlen
    return ranges


def _scroll_into_view(ds, top_abs, bottom_abs, margin=40.0, center=False):
    """Scroll the nearest scrollable ancestor (or the view itself) so the
    screen-space band [top_abs, bottom_abs] is visible.

    The editor doesn't always own its scrollbar — when rendered in a fixed
    window it scrolls itself, but in the code chain a parent container scrolls
    (and the editor's own scroll_offset is forced to 0). Walking up _parent to
    the node whose scroll_visible is set, then nudging that node's scroll_offset
    by the on-screen overflow, scrolls the right thing in both layouts.

    With center=True the band is centered vertically in the viewport instead of
    just nudged to the nearest margin edge — used for search-result navigation,
    where the match should land in the middle of the view rather than stuck at
    the top/bottom. Vertical only; the horizontal scroll is never touched. The
    centered offset is clamped at the content ends, so a match near the top or
    bottom of the document lands as close to center as the scroll range allows.
    """
    node = ds
    seen = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if getattr(node, 'scroll_visible', False):
            view_top = node.abs_top + node.header_height
            view_bottom = node.abs_top + (node.height or 0)
            sx, sy = node.scroll_offset
            if center:
                # Align the band's midline with the viewport's midline (Y only,
                # horizontal sx untouched). Clamped at the content ends below.
                delta = ((top_abs + bottom_abs) * 0.5) - ((view_top + view_bottom) * 0.5)
                if abs(delta) > 0.5:
                    __old_scroll = node.scroll_offset
                    node.scroll_offset = (sx, max(0, min(sy + delta, node._max_scroll_y)))
                    request_render()
                return
            # No clamping here - _ancestor_scroll enforces the scroll bound at
            # the source, so overshoot past the content ends doesn't accumulate.
            if top_abs < view_top + margin:
                current_x = node.scroll_offset[0]
                new_offset = sy - (view_top + margin - top_abs)
                __old_scroll = node.scroll_offset
                node.scroll_offset = (current_x,
                                      max(0, min(new_offset, node._max_scroll_y)))

                request_render()
            elif bottom_abs > view_bottom - margin:
                current_x = node.scroll_offset[0]
                new_offset = sy + (bottom_abs - (view_bottom - margin))
                __old_scroll = node.scroll_offset
                node.scroll_offset = (current_x,
                                      max(0, min(new_offset, node._max_scroll_y)))

                request_render()
            return
        nxt = node._parent
        node = nxt if nxt is not node else None


def _code_tree_errors(code_tree):
    """(line, message) parse-error markers carried by the routed code_tree, if
    any. code_tree is the GeneralParse round-tripped in via the chain; a failed
    parse may surface as a ParseError (a dict subclass) exposing .line/.error or
    __line__/__error__ keys, and the background lint pass (code_checks) ships a
    whole LIST under __errors__ (draw_text_from_code_cache builds it: the parse
    error, if any, plus every undefined-name / call-signature finding).
    Duck-typed to dodge an import cycle with libcst_conversion."""
    if code_tree is None:
        return []
    line = getattr(code_tree, 'line', None)
    err = getattr(code_tree, 'error', None)
    if line and err:
        return [(int(line), str(err))]
    if isinstance(code_tree, dict):
        markers = code_tree.get('__errors__')
        if markers:
            return [(int(ln or 1), str(msg)) for ln, msg in markers]
        if code_tree.get('__error__'):
            return [(int(code_tree.get('__line__') or 1), str(code_tree['__error__']))]
    return []


def _exception_errors(error):
    """(line, message) markers from an exception routed into the editor — a
    parse/compile failure over the buffer's own source, delivered via the mode
    route (see draw_modes). Lines are 1-based and align with the rendered text:
    the failed chain parsed this same source, so the editor_line/lineno maps
    straight onto a rendered line. Handles libcst ParserSyntaxError (editor_line)
    and builtin SyntaxError (lineno); other exceptions carry no source line, so
    they produce no highlight."""
    if error is None or not isinstance(error, BaseException):
        return []
    line = getattr(error, 'editor_line', None) or getattr(error, 'lineno', None)
    if line is None:
        return []
    try:
        line = int(line)
    except (TypeError, ValueError):
        return []
    # Prefer the clean message: libcst exposes `.message`, builtin SyntaxError
    # exposes `.msg` ("duplicate name 'x' ...") - `str(e)` would tack on the
    # noisy "(<file>, line ...)" suffix, so reach for the attrs first.
    msg = (getattr(error, 'message', None)
           or getattr(error, 'msg', None)
           or str(error))
    return [(line, msg)]


def _describe_code_tree(code_tree):
    """One-line readout of what round-tripped into draw_text as code_tree, for
    the debug indicator."""
    if code_tree is None:
        return "None"
    errs = _code_tree_errors(code_tree)
    if errs:
        return f"ParseError @ line {errs[0][0]}: {errs[0][1][:40]}"
    name = type(code_tree).__name__
    if isinstance(code_tree, dict):
        return f"{name} ({len(code_tree)} keys)"
    return name


@render_func(is_default_for=(CodeLine), show_bg=True, use_cache=True, 
             disable_scroll=False, with_header=draw_header, shadow=False, 
             show_name=False, with_footer=draw_footer, determines_height=False,
             selectable=False, searchable=True, bg_offset=-3, show_add_delete=False)
def draw_text(input_value: str, height=None,
              left_mouse_down=False, 
              left_mouse_drag=False, left_mouse_held=False,
              horizontal_scroll_drag=False, search_text="", 
              ctrl_b_down=False,
              single_line=False, is_search_box=False,
              draw_state=None, request_focus=False, 
              wrap=False, line_height=1.149, font=Font.JETBRAINS_MONO_19, jump_to=None,
              code_tree=None, code_dict=None, error=None, token_views=None,
              syntax_highlight=True, is_diff=False, line_numbers=None,
              completion_source=None, unique=0):

    ds = draw_state   
    # Plain-text mode (codec tells "not Python source"): no Darcula colors and
    # no inline token widgets - both are artifacts of the Python tokenizer.
    if not syntax_highlight:
        token_views = {}
    elif token_views is None:
        token_views = DEFAULT_TOKEN_VIEWS   # global experiment settings (see a

    # Symbol-usage source: the parse arrives as `code_tree` in the
    # address_to_general_parse routes, as `code_dict` in the CODE_UI routes
    # (cst_module_to_dict - which is also where the run_jedi() pass attaches
    # __symbol_usages__). Links are file-absolute, so the buffer's file offset
    # comes from the parse's line_offset when set, else from the jump_to span.
    _usage_tree = code_tree if code_tree is not None else code_dict
    _usage_off = getattr(_usage_tree, 'line_offset', 0) or 0
    if not _usage_off and jump_to is not None:
        _usage_off = getattr(jump_to, 'start', 0) or 0
        
    # Per-editor state for the code-suggestions popup. Lives here (not gated on
    # focus) because the popup's menu window is latched and must be drawn EVERY
    # frame with closed_state toggled, even when the editor is unfocused.
    if getattr(ds, '_ac_state', None) is None:
        ds._ac_state = DropDownState()
    ac_state = ds._ac_state
    
    # Same deal for the usage-jump picker (multi-user symbol double-click).
    if getattr(ds, '_uj_state', None) is None:
        ds._uj_state = DropDownState()
    uj_state = ds._uj_state
    # Imported in-function to avoid a module-load import cycle (toggles pulls in
    # decoration/window machinery). For the spell-check button + squiggles below.
    from src.lsd.gl_gui.toggles import Toggles

    # Error markers to highlight in red: the routed code_tree's parse errors plus
    # any exception routed in via the mode route (e.g. draw_modes hands us the
    # chain_in failure so the offending source line lights up here). Computed up
    # front so the message can ride along into the file header bar.
    _ct_errors = _code_tree_errors(code_tree) if code_tree is not None else None
    _err_markers = list(_ct_errors) if _ct_errors else []
    _err_markers += _exception_errors(error)
    # Suppression (clearing _err_markers and _err_msg while keyboard editing) is
    # applied AFTER the keyboard recompute below, so it can read this frame's
    # popup state and the freshly-stamped edit time - see _ERR_SUPPRESS_SEC.

    # Jump-to-source button drawn inline at the top (before the monospace font
    # push, so it uses the normal UI font), above the text body. The first error
    # message (if any) is no longer shown inline here - it floats in a bar pinned
    # to the bottom of the view (see the error footer after the body is drawn).
    bar_height = 0.0
    _err_msg = None
    if jump_to is not None:
        _err_msg = _err_markers[0][1] if _err_markers else None
        # Float the jump-to/error bar at the top of the visible viewport instead
        # of letting it scroll away with the code. When the body has scrolled up
        # above its clip rect, shift the bar down by that overflow so it stays
        # pinned to the clip top; at scroll 0 the content top equals the clip top
        # so float_dy is 0 and the bar is in its natural place. Drawing it at the
        # shifted (on-screen) position also keeps draw_jump_to's own clip rect from
        # collapsing once the content top passes above the viewport.
        _bx, _by = imgui.get_cursor_screen_pos()
        float_dy = max(0.0, draw_state.abs_clip_rect[1] - _by)
        # The pin only holds while there's enough view above the clip top: once
        # the view's bottom edge rises to meet the bar, the bar follows that edge
        # up and scrolls away like everything else. The bar's natural position
        # is the view top, so its maximum downward shift before its bottom
        # passes the view bottom is height - bar_height (last frame's measure).
        _bar_h = getattr(draw_state, "_float_bar_height", None) or 34.0
        if draw_state.height:
            float_dy = max(0.0, min(float_dy, draw_state.height - _bar_h))
        imgui.set_cursor_screen_pos((_bx, _by + float_dy))
        draw_jump_to(jump_to, width=draw_state.content_width, unique=unique)
        bar_height = imgui.get_cursor_screen_pos()[1] - (_by + float_dy)
        draw_state._float_bar_height = bar_height
        # Resume body layout at the real (unscrolled) content position so the code
        # lines keep their normal positions; only the bar was floated. The text
        # clip below is raised by bar_height so glyphs never paint over the bar.
        imgui.set_cursor_screen_pos((_bx, _by + bar_height))

    _font_pushed = False
    if font is not None and Melty.font_mgr is not None:
        _font_handle = Melty.font_mgr.get(font)
        if _font_handle is not None:
            imgui.push_font(_font_handle)
            _font_pushed = True

    # Character advance. Every caller uses JetBrains Mono (monospace), so one
    # character advance lets us position and measure text by character count
    # instead of calling imgui.calc_text_size per glyph/slice each frame.
    char_w = imgui.calc_text_size("0").x
    changed = False
    original_input = input_value
    # No line limit: the editor shows the WHOLE span. Off-screen lines are
    # already viewport-culled in every draw loop below (rect_min_y/rect_max_y)
    # and tokenization is cached by text value, so a long function costs an
    # O(n) position walk per frame, not per-line GPU remeasurements. The old
    # `max_lines = 1000` cap truncated the visible/editable text AND - because
    # its save-time rebuild re-stitched the hidden tail with no newline - ate one
    # boundary newline per save, progressively merging lines at line 1000 of any
    # longer function (the draw_text / abs_clip_rect_local corruptions).
    text = input_value
    io = imgui.get_io()
    line_px = imgui.get_text_line_height() * line_height

    # Viewport tokenization: tokenize ONLY the clipped line range each frame
    # (see the `_line_open` / `_window_tokens` machinery above), so the per-frame
    # syntax cost is O(visible) instead of O(buffer). `_window()` returns
    # (start_line, start_offset, tokens, vcols) for the current text + visible
    # range, cached on the draw_state. It's lazy + keyed by (text, range), so a
    # click (pre-edit text) and the render (post-edit text) each get a window
    # for their state, but the render loop reuses the click's computation.
    def _window():
        nlines = text.count('\n') + 1
        # Visible line band from the clip rect (Y only) - the SAME live
        # abs_clip_rect + bar_height the draw-cull below uses, so the window
        # always covers exactly the lines that get drawn. A few lines of margin
        # keep caret/selection edges just past the clip correct and absorb a
        # frame of drag-scroll.
        _clip = draw_state.abs_clip_rect
        if line_px:
            v0 = int((_clip[1] + bar_height - top) / line_px) - 3
            v1 = int((_clip[3] - top) / line_px) + 3
        else:
            v0, v1 = 0, nlines - 1
        v0 = max(0, min(v0, nlines - 1))
        v1 = max(v0, min(v1, nlines - 1))
        key = (text, v0, v1, syntax_highlight, id(token_views) if token_views else 0)
        if getattr(ds, '_win_key', None) == key:
            return ds._win_data
            
        if syntax_highlight:
            if getattr(ds, '_lo_text', None) != text:
                ds._lo_offs, ds._lo_open = _update_line_open(
                    getattr(ds, '_lo_text', None), getattr(ds, '_lo_offs', None),
                    getattr(ds, '_lo_open', None), text)
                ds._lo_text = text
            wl, start_off, toks = _window_tokens(text, ds._lo_offs, ds._lo_open, v0, v1)
            win_len = sum(len(t) for t, _ in toks)
            arr = _build_vcols(text[start_off:start_off + win_len], toks, token_views) \
                if token_views else None
            vcols = _WinVCols(arr, start_off) if arr is not None else None
        else:
            # Plain mode: the visible lines as ONE 'default' token (the segment
            # loop below splits it at newlines). No strings → no line_open needed.
            offs = _line_offsets(text)
            wl, start_off = v0, offs[v0]
            end_off = offs[v1 + 1] if v1 + 1 < len(offs) else len(text)
            win_text = text[start_off:end_off]
            toks = [(win_text, 'default')] if win_text else []
            vcols = None
        ds._win_key = key
        ds._win_data = (wl, start_off, toks, vcols)
        return ds._win_data

    def _get_vcols():
        return _window()[3]

    left = imgui.get_cursor_screen_pos()[0]
    top = imgui.get_cursor_screen_pos()[1]


    # --- Line-number gutter ---
    # Shown only when the routed address (jump_to) supplies a starting line, so
    # a function body span shows its true file line numbers. Plain buffers with
    # no address or single-line cells (search box, inline text editors) get no
    # gutter. gutter_w is folded into origin_x, so every downstream operation
    # (scroll, search, cursor, mouse hit-testing) shifts with it; the numbers
    # themselves are drawn in their own clip column at the end so
    # horizontally-scrolled code never slides underneath them.
    # Explicit per-line numbers (diff mode passes the real file line for each
    # +/- line - they're non-contiguous, so no sequential offset can express
    # them) take precedence over the jump_to.start sequential numbering.
    show_gutter = (not single_line and not is_search_box
                   and (line_numbers is not None
                        or (jump_to is not None
                            and getattr(jump_to, 'start', None) is not None)))
    if show_gutter and line_numbers is not None:
        line_offset = 0
        last_line_no = max((n for n in line_numbers if n is not None), default=1)
        gutter_digits = max(len(str(last_line_no)), 2)
        gutter_w = gutter_digits * char_w + 12.0
    elif show_gutter:
        line_offset = jump_to.start
        last_line_no = line_offset + text.count('\n') + 1
        gutter_digits = max(len(str(last_line_no)), 2)
        gutter_w = gutter_digits * char_w + 12.0
    else:
        line_offset = 0
        gutter_w = 0.0

    text_visible_width = draw_state.content_width - gutter_w

    # Snapshot the clip rect in the same scroll frame as `left`/`top`. Those
    # come from the imgui cursor the wrapper positioned at abs_top *before* this
    # func ran; the drag handlers just below then mutate scroll_offset (here and
    # in _scroll_into_view, which is an ancestor) mid-render. abs_clip_rect
    # is computed live from abs_top, so reading it after those mutations makes
    # the clip lead the content - which imgui already placed at the pre-mutation
    # scroll - by one frame's drag delta, showing as a clip that lags the text.
    # Capturing it here keeps content and clip in the same frame; the scroll
    # delta lands next frame, when the wrapper re-positions the content too.
    clip_rect_snapshot = draw_state.abs_clip_rect

    # Right-click drag pans both axes. Vertical uses the framework's
    # scroll_offset (the framework skips writing it while button 2 is down,
    # so our edits aren't clobbered mid-drag). Horizontal uses our own
    # text_h_scroll since the framework only manages vertical scroll.
    if horizontal_scroll_drag:
        ds.text_h_scroll -= horizontal_scroll_drag.dx
        ds.text_h_scroll -= horizontal_scroll_drag.dx
        sx, sy = ds.scroll_offset
        ds.scroll_offset = (sx, sy - horizontal_scroll_drag.dy)

    origin_x = left + gutter_w - ds.text_h_scroll
    origin_y = top

    # Keystrokes come from the GLFW-callback queue (Melty.frame_key_events:
    # ordered (glfw_key, mods) for PRESS/REPEAT this frame), so nothing is
    # dropped on slow frames the way imgui.is_key_pressed (current frame only)
    # would. But GLFW doesn't emit REPEAT actions on any platform, so in the
    # focused editor we supplement the queue with imgui's synthesized auto-repeat
    # (io.key_repeat_delay/rate) for held keys - skipping any key GLFW already
    # reported this frame so we never double-input. `pressed(k)` is membership;
    # the per-char loop iterates in order.
    _frame_keys = list(Melty.frame_key_events)
    if Melty.text_focused_ds is ds:
        _glfw_this_frame = {k for k, _m in _frame_keys}
        _repeat_mods = ((glfw.MOD_SHIFT if io.key_shift else 0)
                        | (glfw.MOD_CONTROL if io.key_ctrl else 0)
                        | (glfw.MOD_ALT if getattr(io, 'key_alt', False) else 0))
        _any_down = False
        for _rk in _REPEATABLE_KEYS:
            if imgui.is_key_down(_rk):
                _any_down = True
            if _rk not in _glfw_this_frame and imgui.is_key_pressed(_rk, repeat=True):
                _frame_keys.append((_rk, _repeat_mods))
        # The loop otherwise sleeps on wait_events between GLFW events; keep it
        # rendering while a key is held so imgui's repeat cadence is sampled.
        if _any_down:
            request_render()
    _fired = {k for k, _m in _frame_keys}
    pressed = lambda k: k in _fired

    # --- Mouse handling ---
    is_focused = Melty.text_focused_ds is ds
    # A rebuilt cache can hand us a fresh draw_state object for the same tile;
    # rebind focus by tile id so a cache hit doesn't silently drop it.
    if (not is_focused and Melty.text_focused_ds is not None
            and getattr(Melty.text_focused_ds, '_tile_id', None) == ds._tile_id):
        if Toggles.TextEditor.text_focus_stack_trace:
            print(f"[focus-grant] rebind -> {ds.name} ({ds._tile_id}) "
                  f"from ds {id(Melty.text_focused_ds)}")
        Melty.text_focused_ds = ds
        Melty._text_focus_grant_frame = Melty.frame_count
        is_focused = True
    if request_focus:
        if Toggles.TextEditor.text_focus_stack_trace and Melty.text_focused_ds is not ds:
            print(f"[focus-grant] request_focus -> {ds.name} ({ds._tile_id})")
        Melty.text_focused_ds = ds
        # Stamp the grant frame so the same-frame request_focus grace (see
        # Melty.clear_focus) protects this claim from the very click that
        # opened the search box / dropdown / menu owning it.
        Melty._text_focus_grant_frame = Melty.frame_count
        is_focused = True
        
    def _try_usage_jump(pos, force_picker=False):
        """Usage jump at buffer index `pos` (double-click / Ctrl+B): one
        counterpart opens straight in IntelliJ; several open the usage-jump
        picker under the symbol. `force_picker` opens the picker even for a
        SINGLE counterpart (the Toggles.TextEditor.double_click_opens_dropdown
        behavior) instead of jumping straight. True if the jump or picker
        happened (a span with zero targets returns False -> word-select)."""
        _vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
        for _us, _ue, _su, _at_def in _usage_spans(ds, text, _usage_tree, _usage_off, _vpath):
            if _us <= pos < _ue:
                _targets = _usage_jump_targets(
                    _su, at_def=_at_def,
                    view_path=_vpath,
                    view_span=(_usage_off + 1, _usage_off + text.count('\n') + 1))
                if _targets and (len(_targets) > 1 or force_picker):
                    _items, _tags = _usage_ref_items(_targets)
                    ds._uj_items = _items
                    ds._uj_tags = _tags
                    ds._uj_anchor = _us   # picker hangs under the symbol
                    ds._uj_index = 0
                    ds._uj_open = True
                    uj_state._kbd_mode = True
                    uj_state.cursor_path = (next(iter(_items)),)
                    uj_state.open_path = ()
                    request_render()
                    return True
                if _targets:
                    _open_usage_ref(_targets[0])
                    return True
                return False
        return False

    if left_mouse_down:
        if Toggles.TextEditor.text_focus_stack_trace and Melty.text_focused_ds is not ds:
            print(f"[focus-grant] click -> {ds.name} ({ds._tile_id})")
        Melty.text_focused_ds = ds
        Melty._text_focus_grant_frame = Melty.frame_count
        is_focused = True
        # A fresh click anywhere in the editor dismisses the usage-jump picker
        # (the double-click branch below re-opens it when it should). Clicks on
        # the picker itself never land here - it floats in its own window, so
        # this hover-ranged event doesn't fire.
        ds._uj_open = False
        ds.text_cursor_blink_time = time.time()
        click_pos = _xy_to_char_index(text, io.mouse_pos.x, io.mouse_pos.y,
                                      origin_x, origin_y, line_px, vcols=_get_vcols())
        
        now = time.time()
        within_window = (now - ds.text_double_click_time < 0.3
                         and abs(click_pos - ds.text_last_click_pos) <= 1)
        ds.text_click_count = ds.text_click_count + 1 if within_window else 1
        ds.text_double_click_time = now
        ds.text_last_click_pos = click_pos

        if ds.text_click_count == 2:
            # Double-click on a symbol that has users → jump like IntelliJ
            # instead of word-selecting (the washed background is the
            # affordance). One counterpart jumps you there; several open
            # the usage-jump picker (the same latched dropdown as the
            # code-suggestion popup) under the symbol so the user picks the
            # target. Anywhere else, word-select. (Ctrl+B does the same at the
            # caret - see the standalone handler below the click state.)
            _jumped = _try_usage_jump(
                click_pos,
                force_picker=Toggles.TextEditor.double_click_opens_dropdown)
            if _jumped:
                ds.text_drag_mode = 'char'
                ds.text_cursor_pos = click_pos
                ds.text_selection_start = ds.text_selection_end = click_pos
                ds.text_drag_anchor_lo = ds.text_drag_anchor_hi = click_pos
            else:
                ds.text_drag_mode = 'word'
                ds.text_selection_start = _select_unit_left(text, click_pos)
                ds.text_selection_end = _select_unit_right(text, click_pos)
                ds.text_cursor_pos = ds.text_selection_end
                ds.text_drag_anchor_lo = ds.text_selection_start
                ds.text_drag_anchor_hi = ds.text_selection_end
        elif ds.text_click_count >= 3:
            ds.text_drag_mode = 'line'
            line_start = _get_line_start(text, click_pos)
            line_end = _get_line_end(text, click_pos)
            if line_end < len(text):
                line_end += 1  # include trailing newline so delete deletes the line
            ds.text_selection_start = line_start
            ds.text_selection_end = line_end
            ds.text_cursor_pos = ds.text_selection_end
            ds.text_drag_anchor_lo = line_start
            ds.text_drag_anchor_hi = line_end
        else:
            ds.text_drag_mode = 'char'
            ds.text_cursor_pos = click_pos
            if io.key_shift:
                ds.text_selection_end = click_pos
            else:
                ds.text_selection_start = click_pos
                ds.text_selection_end = click_pos
            ds.text_drag_anchor_lo = ds.text_selection_start
            ds.text_drag_anchor_hi = ds.text_selection_end

    # Extend the selection on cursor motion, and also every frame the button is
    # held (left_mouse_held) once a drag is underway - so holding the cursor
    # past the top/bottom edge keeps auto-scrolling and selecting more text,
    # not just while the mouse is moving.
    if left_mouse_drag:
        mx = left_mouse_drag.x if left_mouse_drag else io.mouse_pos.x
        my = left_mouse_drag.y if left_mouse_drag else io.mouse_pos.y
        # Auto-scroll when the cursor hs/passes the view's top or bottom edge
        # so the selection can reach text outside the viewport. No-ops when the
        # cursor is comfortably inside.
        _scroll_into_view(ds, my, my)
        drag_pos = _xy_to_char_index(text, mx, my,
                                     origin_x, origin_y, line_px, vcols=_get_vcols())
        anchor_lo = ds.text_drag_anchor_lo
        anchor_hi = ds.text_drag_anchor_hi
        if ds.text_drag_mode in ('word', 'line') and (anchor_lo != anchor_hi):
            # Snap the moving end to the word/line boundary under the mouse,
            # then merge with the anchor span so the originally-selected
            # word/line stays fully highlighted while dragging either way.
            if ds.text_drag_mode == 'word':
                edge_lo = _select_unit_left(text, drag_pos)
                edge_hi = _select_unit_right(text, drag_pos)
            else:
                edge_lo = _get_line_start(text, drag_pos)
                edge_hi = _get_line_end(text, drag_pos)
                if edge_hi < len(text):
                    edge_hi += 1
            if drag_pos < anchor_lo:
                # Extending left: anchor's far (right) edge is the fixed end.
                ds.text_selection_start = anchor_hi
                ds.text_selection_end = edge_lo
                ds.text_cursor_pos = edge_lo
            else:
                # At/right of the anchor: anchor's left edge is fixed.
                ds.text_selection_start = anchor_lo
                ds.text_selection_end = max(anchor_hi, edge_hi)
                ds.text_cursor_pos = ds.text_selection_end
        else:
            ds.text_selection_end = drag_pos
            ds.text_cursor_pos = drag_pos
        ds.text_cursor_blink_time = time.time()

    # Ctrl+B - IntelliJ-style "go to declaration" at the CARET, no mouse
    # involved. (This flag used to be read only inside the click handler above,
    # so the shortcut silently required a simultaneous mouse press.) The event
    # is global-routed, so it reaches the editor under the pointer; gate on
    # focus so a stale caret in some other merely-hovered editor can't jump.
    if (ctrl_b_down and is_focused and not single_line and not is_search_box
            and not getattr(ds, '_uj_open', False)):
        _try_usage_jump(min(ds.text_cursor_pos, max(len(text) - 1, 0)))

    # --- Keyboard handling ---
    if is_focused:
        shift = io.key_shift
        ctrl = io.key_ctrl

        # --- Code-suggestion popup: navigation & accept ---
        # Real editors don't suggest in the find box or inline single-line
        # value fields, so gate that out. (ac_state was set up at the top.)
        # Exception: a single-line box that has its own `completion_source`
        # (the context-aware Eval REPL) can autocomplete -- it drives candidates
        # off the live scope cache instead of the parsed code_tree.
        ac_enabled = (not is_search_box
                      and (not single_line or completion_source is not None))
        if not ac_enabled:
            ds._ac_open = False
        # These run BEFORE the normal Arrow/Enter/Tab handlers and eat their
        # keys (discard from `_fired`) when the popup is open, so the same press
        # controls the suggestion list instead of moving the caret / inserting a
        # newline. Driven off LAST frame's open state + candidate list, i.e. the
        # popup the user is actually looking at this keypress.
        if ac_enabled and getattr(ds, '_ac_open', False):
            _ac_cands = getattr(ds, '_ac_candidates', None) or []
            _ac_idx = getattr(ds, '_ac_index', 0)
            if pressed(glfw.KEY_ESCAPE):
                # Dismiss and remember this site so it doesn't re-open
                # while the caret stays put (cleared once the caret moves on).
                ds._ac_open = False
                ds._ac_suppress_anchor = getattr(ds, '_ac_anchor', -1)
                ds._ac_request_anchor = -1
                _fired.discard(glfw.KEY_ESCAPE)
            elif (pressed(glfw.KEY_UP) or pressed(glfw.KEY_DOWN)) and _ac_cands:
                step = 1 if pressed(glfw.KEY_DOWN) else -1
                _ac_idx = (_ac_idx + step) % len(_ac_cands)
                ds._ac_index = _ac_idx
                ac_state._kbd_mode = True
                ac_state.cursor_path = (_ac_cands[_ac_idx],)
                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)
                  or pressed(glfw.KEY_TAB)) and _ac_cands and not ctrl:
                chosen = _ac_cands[min(_ac_idx, len(_ac_cands) - 1)]
                anchor = getattr(ds, '_ac_anchor', ds.text_cursor_pos)
                # Replace the half-typed identifier [anchor, caret) with the pick.
                text = text[:anchor] + chosen + text[ds.text_cursor_pos:]
                ds.text_cursor_pos = anchor + len(chosen)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                ds.text_cursor_blink_time = time.time()
                ds._ac_open = False
                ds._ac_request_anchor = -1
                changed = True
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)
                _fired.discard(glfw.KEY_TAB)

        # --- Usage-jump picker: navigation & accept --- same key model as the
        # suggestion popup above: while open, Esc/arrows/Enter drive the picker
        # and are consumed before the caret handlers see them.
        if getattr(ds, '_uj_open', False):
            _uj_keys = list(getattr(ds, '_uj_items', None) or ())
            _uj_idx = getattr(ds, '_uj_index', 0)
            if pressed(glfw.KEY_ESCAPE):
                ds._uj_open = False
                _fired.discard(glfw.KEY_ESCAPE)
            elif (pressed(glfw.KEY_UP) or pressed(glfw.KEY_DOWN)) and _uj_keys:
                step = 1 if pressed(glfw.KEY_DOWN) else -1
                _uj_idx = (_uj_idx + step) % len(_uj_keys)
                ds._uj_index = _uj_idx
                uj_state._kbd_mode = True
                uj_state.cursor_path = (_uj_keys[_uj_idx],)
                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)) and _uj_keys and not ctrl:
                _open_usage_ref(ds._uj_items[_uj_keys[min(_uj_idx, len(_uj_keys) - 1)]])
                ds._uj_open = False
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)
        # --- Typed characters --- drained in order, using each key event's own
        # modifiers so fast shift-typing across a slow frame stays shifted.
        typed_dot_this_frame = False
        typed_word_char_this_frame = False

        for _fk, _fmods in _frame_keys:
            if _fmods & glfw.MOD_CONTROL:
                continue
            _cm = _KEY_CHAR_MAP.get(_fk)
            if _cm is None:
                continue
            ds.text_cursor_blink_time = time.time()
            ch = _cm[1] if (_fmods & glfw.MOD_SHIFT) else _cm[0]
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
            text = text[:ds.text_cursor_pos] + ch + text[ds.text_cursor_pos:]
            ds.text_cursor_pos += len(ch)
            ds.text_selection_start = ds.text_cursor_pos
            ds.text_selection_end = ds.text_cursor_pos
            # Only a typed '.' (attribute access) opens the popup as you go;
            # plain identifier typing doesn't - Ctrl+P requests it explicitly.
            # (The REPL REPL relaxes this below: a `completion_source` box also
            # opens on identifier typing, so suggestions track every keystroke.)
            if ch == '.':
                typed_dot_this_frame = True
            elif ch.isalnum() or ch == '_':
                typed_word_char_this_frame = True
            changed = True


        # --- Tab / Shift+Tab ---
        if pressed(glfw.KEY_TAB) and not ctrl:
            ds.text_cursor_blink_time = time.time()
            # Bracket-aware align (same ([{ cue as Enter): when adjusting a single
            # line's own indent (no selection, caret in the leading whitespace)
            # and the line is a bracket continuation, Tab pulls an under-indented
            # line UP to the cue and Shift+Tab pulls an over-indented line DOWN to
            # it - e.g. a stray `show_name=False,` snaps under the `@renderable(`.
            _ls = _get_line_start(text, ds.text_cursor_pos)
            _cur = _get_indent(text, _ls)
            _target = _open_bracket_indent(text, _ls)
            _align = (_target is not None and not _has_selection(ds)
                      and not text[_ls:ds.text_cursor_pos].strip()
                      and ((_cur < _target) if not shift else (_cur > _target)))
            if _align:
                text = text[:_ls] + ' ' * _target + text[_ls + _cur:]
                ds.text_cursor_pos = _ls + _target
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
            elif shift or _has_selection(ds):
                if _has_selection(ds):
                    lo, hi = _sel_range(ds)
                else:
                    lo = hi = ds.text_cursor_pos
                text, new_lo, new_hi = _indent_lines(text, lo, hi, dedent=shift)
                ds.text_selection_start = new_lo
                ds.text_selection_end = new_hi
                ds.text_cursor_pos = new_hi
            else:
                insert = '    '
                text = text[:ds.text_cursor_pos] + insert + text[ds.text_cursor_pos:]
                ds.text_cursor_pos += len(insert)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
            changed = True


        # --- Enter --- (skipped for single-line fields like the search box,
        # where Enter is reserved for find-next / Shift+Enter find-prev).
        # Ctrl+Enter is reserved for recompile (general_go_to_address), so we
        # don't insert a newline when Ctrl is held.
        if (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)) and not single_line and not ctrl:
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
            pos = ds.text_cursor_pos
            # Bracket-aware auto-indent. Inside an unclosed (, [ or { align to
            # that bracket's scope (just past the opener, or a hanging indent
            # when nothing follows it) so multi-line signatures / lists / dicts
            # line up instead of snapping to the line's own indent. If the caret
            # is instead on a continuation line continuation bracket already CLOSED on
            # this line, dedent back to the statement's opening-line indent
            # (e.g. after `...)` of a multi-line decorator snaps back to col 0).
            # Otherwise keep the current line's indentation.
            indent = _open_bracket_indent(text, pos)
            if indent is None:
                opener = _unclosed_opener(text, _get_line_start(text, pos))
                indent = _get_indent(text, opener) if opener is not None \
                    else _get_indent(text, pos)
            # The remainder of the current line moves down to the new line. Strip
            # ITS leading spaces (only up to this line's end - NOT the next
            # line's indent) so they don't stack on top of the indent we insert.
            # Without this, whitespace right of the caret compounds with every
            # Enter: the new line ends up `indent + trailing` wide, the caret
            # lands mid-whitespace, and the next Enter measures that larger indent
            # - marching the caret ever rightward instead of fixing the line's
            # indentation.
            tail = pos
            line_end = text.find('\n', pos)
            stop = line_end if line_end != -1 else len(text)
            while tail < stop and text[tail] == ' ':
                tail += 1
            text = text[:pos] + '\n' + ' ' * indent + text[tail:]
            ds.text_cursor_pos = pos + 1 + indent
            ds.text_selection_start = ds.text_cursor_pos
            ds.text_selection_end = ds.text_cursor_pos
            changed = True


        # --- Backspace ---
        if pressed(glfw.KEY_BACKSPACE):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True
            elif ds.text_cursor_pos > 0:
                if ctrl:
                    # Same granular behaviour as ctrl-click selection so a
                    # ctrl-backspace stops at a bracket/operator instead of
                    # eating a whole run of '{(' etc. Anchored on the char LEFT of
                    # the caret so it deletes the unit behind the caret (not the
                    # one under it, which left it a no-op before a bracket).
                    new_pos = _unit_left_of(text, ds.text_cursor_pos)
                    text = text[:new_pos] + text[ds.text_cursor_pos:]
                    ds.text_cursor_pos = new_pos
                else:
                    # Indent-aware backspace. When the caret is in a line's
                    # whitespace section (everything to its left on the line is
                    # spaces), snap back to the previous 4-col tab stop instead
                    # of removing a fixed 4 / a single char. A misaligned indent
                    # (e.g. 6 spaces) collapses to the nearest stop (4) rather
                    # than deleting 4 and leaving 2 stray spaces; an aligned
                    # full space deletes a whole tab; a lone stray space snaps
                    # to its own. Outside the indent it's a plain char delete.
                    line_start = _get_line_start(text, ds.text_cursor_pos)
                    col = ds.text_cursor_pos - line_start
                    in_indent = col > 0 and not text[line_start:ds.text_cursor_pos].strip(' ')
                    if in_indent:
                        new_pos = line_start + ((col - 1) // 4) * 4
                    else:
                        new_pos = ds.text_cursor_pos - 1
                    text = text[:new_pos] + text[ds.text_cursor_pos:]
                    ds.text_cursor_pos = new_pos
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True

        # --- Delete ---
        if pressed(glfw.KEY_DELETE):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True
            elif ds.text_cursor_pos < len(text):
                if ctrl:
                    new_pos = _select_unit_right(text, ds.text_cursor_pos)
                    text = text[:ds.text_cursor_pos] + text[new_pos:]
                else:
                    text = text[:ds.text_cursor_pos] + text[ds.text_cursor_pos + 1:]
                changed = True

        # --- Left ---
        if pressed(glfw.KEY_LEFT):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = _word_boundary_left(text, ds.text_cursor_pos)
            elif _has_selection(ds) and not shift:
                ds.text_cursor_pos = min(ds.text_selection_start, ds.text_selection_end)
            elif ds.text_cursor_pos > 0:
                ds.text_cursor_pos -= 1
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Right ---
        if pressed(glfw.KEY_RIGHT):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = _word_boundary_right(text, ds.text_cursor_pos)
            elif _has_selection(ds) and not shift:
                ds.text_cursor_pos = max(ds.text_selection_start, ds.text_selection_end)
            elif ds.text_cursor_pos < len(text):
                ds.text_cursor_pos += 1
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Up ---
        if pressed(glfw.KEY_UP):
            _dbg = getattr(Melty, '_ac_debug', None)
            if _dbg:
                _dbg[-1]['cursor_moved'] = True
            ds.text_cursor_blink_time = time.time()
            line, col = _index_to_line_col(text, ds.text_cursor_pos)
            if line > 0:
                ds.text_cursor_pos = _line_col_to_index(text, line - 1, col)
            else:
                ds.text_cursor_pos = 0
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Down ---
        if pressed(glfw.KEY_DOWN):
            _dbg = getattr(Melty, '_ac_debug', None)
            if _dbg:
                _dbg[-1]['cursor_moved'] = True
            ds.text_cursor_blink_time = time.time()
            line, col = _index_to_line_col(text, ds.text_cursor_pos)
            total_lines = text.count('\n')
            if line < total_lines:
                ds.text_cursor_pos = _line_col_to_index(text, line + 1, col)
            else:
                ds.text_cursor_pos = len(text)
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Home ---
        if pressed(glfw.KEY_HOME):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = 0
            else:
                ds.text_cursor_pos = _get_line_start(text, ds.text_cursor_pos)
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- End ---
        if pressed(glfw.KEY_END):
            ds.text_cursor_blink_time = time.time()
            if ctrl:
                ds.text_cursor_pos = len(text)
            else:
                ds.text_cursor_pos = _get_line_end(text, ds.text_cursor_pos)
            if shift:
                ds.text_selection_end = ds.text_cursor_pos
            else:
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos

        # --- Ctrl+A ---
        if ctrl and pressed(glfw.KEY_A):
            ds.text_selection_start = 0
            ds.text_selection_end = len(text)
            ds.text_cursor_pos = len(text)

        # --- Ctrl+C ---
        if ctrl and pressed(glfw.KEY_C):
            if _has_selection(ds):
                lo, hi = _sel_range(ds)
                imgui.set_clipboard_text(text[lo:hi])

        # --- Ctrl+X ---
        if ctrl and pressed(glfw.KEY_X):
            if _has_selection(ds):
                lo, hi = _sel_range(ds)
                imgui.set_clipboard_text(text[lo:hi])
                text, ds.text_cursor_pos = _delete_selection(text, ds)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True

        # --- Ctrl+V ---
        if ctrl and pressed(glfw.KEY_V):
            ds.text_cursor_blink_time = time.time()
            clipboard = imgui.get_clipboard_text()
            if clipboard:
                if _has_selection(ds):
                    text, ds.text_cursor_pos = _delete_selection(text, ds)
                # Smart reindent on paste. A copied indented line block is dropped
                # at the caret's own indentation, preserving the block's RELATIVE
                # indentation, instead of pushing the block indent on top of the
                # line's (double-indenting). Only when the caret is on a line's
                # leading whitespace (the "paste onto a fresh indented line" case)
                # and the text is multi-line or carries leading spaces; plain
                # inline pastes (a token mid-statement) are left untouched.
                line_start = _get_line_start(text, ds.text_cursor_pos)
                prefix = text[line_start:ds.text_cursor_pos]
                reindent = (not prefix.strip()
                            and ('\n' in clipboard or clipboard[:1].isspace()))
                insert = _reindent_paste(clipboard, prefix) if reindent else clipboard
                text = text[:ds.text_cursor_pos] + insert + text[ds.text_cursor_pos:]
                ds.text_cursor_pos += len(insert)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True

        # --- Ctrl+/ (toggle line comment) ---
        if ctrl and pressed(glfw.KEY_SLASH):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                lo, hi = _sel_range(ds)
            else:
                lo = hi = ds.text_cursor_pos
            text, new_lo, new_hi = _toggle_comment(text, lo, hi)
            ds.text_selection_start = new_lo
            ds.text_selection_end = new_hi
            ds.text_cursor_pos = new_hi
            changed = True

        # --- Ctrl+I (insert Font Awesome icon glyph) ---
        # Inserts a placeholder glyph at the caret; the "icon" token_views renderer
        # immediately dresses it as the inline icon-picker dropdown, so this is
        # the keyboard entry point into icon picking.
        if ctrl and pressed(glfw.KEY_I):
            ds.text_cursor_blink_time = time.time()
            if _has_selection(ds):
                text, ds.text_cursor_pos = _delete_selection(text, ds)
            text = text[:ds.text_cursor_pos] + GENERIC_ICON + text[ds.text_cursor_pos:]
            ds.text_cursor_pos += len(GENERIC_ICON)
            ds.text_selection_start = ds.text_cursor_pos
            ds.text_selection_end = ds.text_cursor_pos
            changed = True


        # Any buffer edit dismisses the usage-jump picker - its spans (and the
        # anchor it hangs off) are stale the moment the text shifts.
        if changed and getattr(ds, '_uj_open', False):
            ds._uj_open = False

        # --- Code-suggestion popup: toggle visibility + rebuild candidates ---
        # Runs after every text-mutating key so the prefix reflects the final
        # buffer. Produces the list THIS frame's render draws and next frame's
        # nav reads. `_ac_anchor` is the span an accepted pick overwrites.
        if ac_enabled:
            prefix, anchor, dot_trigger = _completion_context(text, ds.text_cursor_pos)
            sup = getattr(ds, '_ac_suppress_anchor', -1)
            if sup != -1 and sup != anchor:
                ds._ac_suppress_anchor = sup = -1  # caret moved on; allow reopen
            # Explicit-trigger model: the popup only opens on a TYPED '.'
            # (attribute access) or Ctrl+P. The trigger pins the completion site
            # (`_ac_request_anchor`); the popup stays up there - re-filtering as
            # the prefix grows/shrinks - until the caret leaves the site, Esc, or
            # an accepted pick. A bare caret move (e.g. clicking right after an
            # attribute) never opens it.
            # A `completion_source` box (the Eval REPL) is REPL-style: it also
            # opens on plain identifier typing, so suggestions track every
            # keystroke without a Ctrl+P. The body keeps its explicit model.
            repl_open = completion_source is not None and typed_word_char_this_frame
            if typed_dot_this_frame or (ctrl and pressed(glfw.KEY_P)) or repl_open:
                ds._ac_request_anchor = anchor
                ds._ac_suppress_anchor = sup = -1  # explicit ask overrides a prior Esc
            req = getattr(ds, '_ac_request_anchor', -1)
            if req != -1 and req != anchor:
                ds._ac_request_anchor = req = -1  # caret left the trigger site
            suppressed = sup != -1 and sup == anchor
            was_open = getattr(ds, '_ac_open', False)
            want = req != -1 and req == anchor and not suppressed
            if want and completion_source is not None:
                # Eval REPL path - candidates come from the live scope cache
                # (FuncsMetadata), not the parsed code tree/jedi. Synchronous: the
                # source resolves member access via the recorded type's dict
                # (vs a live module's getattr) and bare names from the scope, both
                # with EXACT type tags. We still filter by the half-typed prefix.
                raw = []
                # try:
                #     raw = completion_source(text, anchor, prefix, dot_trigger) or []
                # except Exception:
                #     raw = []
                cands = _filter_completions(raw, prefix)
            elif want and dot_trigger:
                # Member access (`imgui.`, `foo.bar`) - resolve the receiver's
                # REAL members with jedi (async, off-thread). Until they arrive,
                # keep the popup closed (scoped names aren't members) and keep the
                # body repainting so the future gets polled. Unresolvable
                # receivers (a bare local, `self.`) just return nothing.
                members, pending = _ensure_member_completions(ds, text, anchor)
                if members is not None:
                    cands = _filter_completions(members, prefix)
                else:
                    cands = []   # jedi still resolving; its done-callback wakes us once
            elif want:
                # Bare identifier: scope-aware names from the parsed tree. The
                # POOL depends on the tree + the caret's line (its scope), NOT the
                # prefix, so cache it + only rebuild when those change. The body
                # re-runs every frame while the popup is open (keep-alive
                # invalidate); without this we'd re-walk the parse and build the
                # LineMap each frame just to filter by prefix.
                _ac_line = _index_to_line_col(text, ds.text_cursor_pos)[0]
                _pool_key = (id(code_tree), _ac_line, len(text))
                if getattr(ds, '_ac_pool_key', None) != _pool_key:
                    ds._ac_pool = _completion_pool(code_tree, text, _ac_line)
                    ds._ac_pool_key = _pool_key
                cands = _filter_completions(ds._ac_pool, prefix)
            else:
                cands = []
            if cands:
                # cands is [(name, kind)]. Names drive nav/scroll/highlight; the
                # kind becomes a dim per-row tag (func/class/var/...) via kind_tags.
                names = [n for n, _ in cands]
                if prefix != getattr(ds, '_ac_prefix', None) or not was_open:
                    ds._ac_index = 0  # list changed shape, restart at the top match
                    # Assert keyboard-select mode so the top match is highlighted
                    # immediately (the dropdown only paints the cursor_path row
                    # when _kbd_mode is set; otherwise it waits for hover). A mouse
                    # press flips back to hover (handled in the popup render).
                    ac_state._kbd_mode = True
                    if not was_open:
                        request_render()
                ds._ac_index = min(getattr(ds, '_ac_index', 0), len(names) - 1)
                ds._ac_open = True
                ds._ac_anchor = anchor
                ds._ac_prefix = prefix
                ds._ac_candidates = names
                ds._ac_kinds = {n: _kind_tag(k) for n, k in cands}
                ac_state.cursor_path = (names[ds._ac_index],)
                ac_state.open_path = ()
            else:
                ds._ac_open = False

        # --- Function call parameter hints (signature help) ---
        # Independent of the completion popup - when the caret sits inside a
        # call's parens, resolve the callee's signature (jedi, async) and show it
        # with the current argument highlighted. The active arg index is recomputed
        # locally each frame (cheap); jedi is only re-queried when the call
        # changes. `_ac_sig_show` gates the hint paint. No off for the Eval REPL
        # box (completion_source): jedi can't see its runtime-typed locals, and we
        # don't want a subprocess signature job fired every keystroke in a one-liner.
        if ac_enabled and completion_source is None:
            _open_paren, _arg_index = _call_context(text, ds.text_cursor_pos)
            if _open_paren is not None and _ensure_signature_help(
                    ds, text, _open_paren, ds.text_cursor_pos) is not None:
                ds._ac_sig_active = _arg_index
                ds._ac_sig_open_paren = _open_paren   # lets the hint align under the call name
                ds._ac_sig_show = True
            else:
                ds._ac_sig_show = False

    # Clamp
    ds.text_cursor_pos = max(0, min(ds.text_cursor_pos, len(text)))
    ds.text_selection_start = max(0, min(ds.text_selection_start, len(text)))
    ds.text_selection_end = max(0, min(ds.text_selection_end, len(text)))

    # Visual-column map over the render region (text is final now). `_colx(idx)`
    # gives the line-relative visual x (px) of a source index, honouring all
    # token-view widths; with no views it's just the plain character column.
    vcols = _get_vcols()
    def _colx(idx, line_start=None):
        # Line-relative visual x (px) of source `idx`. With inline views in this
        # window, read the window vcols; otherwise (idx off-window - those aren't
        # drawn) it's just the character column, O(1) if line_start is known.
        if vcols is not None:
            cell = vcols.cell(idx)
            if cell is not None:
                return cell * char_w
        col = (idx - line_start) if line_start is not None else _index_to_line_col(text, idx)[1]
        return col * char_w

    # Hide the parse-error display while it's STALE: the buffer has been edited
    # since this error/code_tree was parsed (the reparse runs in the body),
    # so its line numbers are out of date or it may already be fixed. The stale
    # flag is set + cleared at the end of the body (see "Parse-error staleness").
    # Also hide while a completion/signature popup is up (code mid-edit).
    if (getattr(ds, '_err_stale', False)
            or (is_focused and (getattr(ds, '_ac_open', False)
                                or getattr(ds, '_ac_sig_show', False)))):
        _err_markers = []
        _err_msg = None

    # --- Find-in-text search ---
    # The term arrives either forwarded from an ancestor search owner (as a
    # SearchTerm carrying the shared cross-view session) or, when this editor
    # hosts the find UI itself, on ds.search_text with a session it pushed.
    # We search locally, register our matches to the session so every view
    # combines into one global set, and scroll to the global-current match
    # when it lands in this view.
    search_term = search_text or (ds.search_text if ds.search_active else "")
    search_matches = _find_matches(text, search_term)

    # Which local match (if any) is the global-current one is decided by a
    # search owner's pre-body tree walk (search_walk), not by claiming here:
    # the walk is the single source for both the count and the selection, so
    # off-screen views the render skips can't shift the indices. We just read
    # the local index the walk stashed on us and highlight/scroll to it.
    if isinstance(search_term, SearchTerm):
        session = search_term
    elif ds.search_active and ds._search_session is not None:
        session = ds._search_session
    else:
        session = None

    local_count = len(search_matches)
    if session is not None:
        current_local = ds._search_active_local
        if current_local is not None and current_local >= local_count:
            current_local = None
        # Scroll to it on a full-search frame (term change or nav).
        should_scroll = current_local is not None and session.scroll_to
    else:
        current_local = None
        should_scroll = False

    # Stash a matcher so a search owner can recount this editor's matches by
    # walking the live draw_state tree (DrawState.descendants / search_walk)
    # without re-rendering it - the key to counting off-screen editors. Set
    # every render (capturing the current text) so an editor that has since
    # scrolled out still contributes its count. The find UI's own input box
    # (is_search_box) must never self-count, so it clears any matcher - its text
    # IS the query, so a matcher inside would always self-match (phantom +1).
    if not is_search_box:
        _match_text = text
        ds._search_matcher = (
            lambda term, sess, _t=_match_text: sess.claim(len(_find_matches(_t, term))))
    else:
        ds._search_matcher = None

    if should_scroll:
        ms, me = search_matches[current_local]
        line, _col = _index_to_line_col(text, ms)
        # Vertical: scroll the editor (or its scroll parent) so the match
        # is fully on screen. Pass the line's full vertical band [top, bottom] in
        # screen space - _scroll_into_view takes (top_abs, bottom_abs), so a
        # match ABOVE the viewport scrolls up and one BELOW scrolls down.
        # Anchor on origin_y (the actual rendered content top, == abs_top minus
        # the editor's own vertical scroll) - the SAME origin the highlight is
        # drawn at below (origin_y + m_line * line_px). Using ds.abs_top here
        # would ignore the editor's self-scroll, so the computed origin always
        # sat at-or-below the true one: the view only ever scrolled down (never
        # up) and the match landed off-screen whenever the editor owned its
        # scrollbar.
        match_top_abs = origin_y + line * line_px
        _scroll_into_view(ds, match_top_abs, match_top_abs + line_px, center=True)

        # Horizontal: default back to the line start (h_scroll 0) while paging
        # through results, scrolling to only when the match wouldn't fit.
        match_x = _colx(ms)
        match_x_end = _colx(me)
        edge_padding = 20.0
        if text_visible_width > 0:
            if match_x_end <= text_visible_width - edge_padding:
                ds.text_h_scroll = 0.0
            else:
                # Pin the match's end to the right edge so we scroll the least
                # amount needed to reveal it, instead of dragging it to the left.
                ds.text_h_scroll = max(0.0, match_x_end - text_visible_width + edge_padding)
        request_render()

    # --- Horizontal auto-scroll ---
    # Only kicks in when the cursor moved this frame, so middle-drag pans
    # are not snapped back. Brings the cursor into view on a single line.
    visible_width = text_visible_width
    if ds.text_cursor_pos != ds.text_prev_cursor_pos and visible_width > 0:
        cursor_logical_x = _colx(ds.text_cursor_pos)
        edge_padding = 20.0
        if cursor_logical_x - ds.text_h_scroll < edge_padding:
            ds.text_h_scroll = max(0.0, cursor_logical_x - edge_padding)
        elif cursor_logical_x - ds.text_h_scroll > visible_width - edge_padding:
            ds.text_h_scroll = cursor_logical_x - visible_width + edge_padding

    # --- Vertical auto-scroll ---
    # Vertical counterpart of the horizontal follow above: when the caret moves
    # to a line off the top/bottom of the viewport (typing past the last visible
    # line, wheeling/paging the cursor away, pasting a multi-line block), scroll
    # the editor - or its scroll container - so the caret's line comes back into
    # view. Same cursor-moved test so wheel/middle-drag pans that leave the caret
    # put are not snapped back. Anchors on origin_y and hands _scroll_into_view
    # the caret line's full vertical band exactly like the search scroll above.
    if ds.text_cursor_pos != ds.text_prev_cursor_pos and line_px:
        cursor_line, _ = _index_to_line_col(text, ds.text_cursor_pos)
        cursor_top_abs = origin_y + cursor_line * line_px
        _scroll_into_view(ds, cursor_top_abs, cursor_top_abs + line_px)

    ds.text_prev_cursor_pos = ds.text_cursor_pos

    # Clamp h_scroll to content bounds - the widest line drives the limit. Uses
    # plain character count (vcols now covers only the visible window, not the
    # whole buffer); inline widgets widen a line by a couple of cells, so the
    # h-scroll limit can be a hair short on widget-heavy lines - harmless.
    max_line_width = max((len(l) for l in text.split('\n')), default=0) * char_w
    max_h_scroll = max(0.0, max_line_width - visible_width + 50.0)
    ds.text_h_scroll = max(0.0, min(ds.text_h_scroll, max_h_scroll))
    origin_x = left + gutter_w - ds.text_h_scroll

    # --- Drawing ---
    draw_list = imgui.get_window_draw_list()
    # Text content is clipped to start after the gutter, so highlights never
    # bleed under the line numbers when scrolled horizontally.
    rect_min_x = left + gutter_w
    # Clip the text body to start below the floating jump-to bar so scrolled code
    # never appears over it (the bar is drawn above, before the body).
    rect_min_y = draw_state.abs_clip_rect[1] + bar_height
    rect_max_x = left + draw_state.content_width
    rect_max_y = draw_state.abs_clip_rect[3]

    draw_list.push_clip_rect(rect_min_x, rect_min_y, rect_max_x, rect_max_y, True)

    # Selection
    if _has_selection(ds):
        sel_color = (0.2, 0.4, 0.8, 0.4)  # rgba(51, 102, 204, 0.4)
        lo, hi = _sel_range(ds)
        lines = text.split('\n')
        line_abs_start = 0
        for line_idx, line_text in enumerate(lines):
            line_abs_end = line_abs_start + len(line_text)
            sy = origin_y + line_idx * line_px
            if (line_abs_end >= lo and line_abs_start <= hi
                    and sy + line_px >= rect_min_y and sy <= rect_max_y):
                sel_start_in_line = max(0, lo - line_abs_start)
                sel_end_in_line = min(len(line_text), hi - line_abs_start)
                sx = origin_x + _colx(line_abs_start + sel_start_in_line, line_start=line_abs_start)
                ex = origin_x + _colx(line_abs_start + sel_end_in_line, line_start=line_abs_start)
                if hi > line_abs_end and line_abs_end >= lo:
                    # selection runs past the newline → extend one cell past EOL
                    ex = origin_x + _colx(line_abs_end, line_start=line_abs_start) + char_w
                draw_list.add_rect_filled(sx, sy, ex, sy + line_px, imgui.get_color_u32_rgba(*sel_color))
            line_abs_start = line_abs_end + 1

    # Token-occurrence highlight: when the caret rests on an identifier that
    # appears more than once, wash a subtle background behind every place that
    # exact token shows up - INCLUDING the one under the caret. A dumb,
    # identifier-bounded character match (see _word_match_ranges) - no CST /
    # symbol-usage index involved - so it works in any text, even mid-edit or
    # unparseable. A unique identifier (its own occurrence and no other) lights
    # nothing up. Drawn under the usage washes / search glow / glyphs.
    if (is_focused and not is_search_box
            and Toggles.TextEditor.highlight_token_matches):
        _tok = _word_under_cursor(text, ds.text_cursor_pos)
        if _tok is not None:
            _t_start, _t_end, _t_word = _tok
            _ranges = _word_match_ranges(text, _t_word)
            # Only when the token recurs (its own occurrence plus at least one
            # other) - so the caret's own occurrence is washed too.
            if len(_ranges) > 1:
                _tm_color = imgui.get_color_u32_rgba(*Toggles.TextEditor.token_match_tint)
                for _ms, _me in _ranges:
                    _m_line, _ = _index_to_line_col(text, _ms)
                    sy = origin_y + _m_line * line_px
                    ey = sy + line_px
                    if ey < rect_min_y or sy > rect_max_y:
                        continue
                    sx = origin_x + _colx(_ms)
                    ex = origin_x + _colx(_me)
                    draw_list.add_rect_filled(sx - 1, sy + 1, ex + 1, ey - 1, _tm_color, 3.0)

    # Symbol-usage washes: a slight background behind every occurrence of a
    # symbol that has callers elsewhere - the affordance that a double-click
    # jumps to its users (see the mouse handler). The wash rides a blue→orange
    # color ramp from the DROPDOWN size (_usage_target_count - the same list
    # _try_usage_jump would show), so the user answers "how many places does
    # this click go": a usage site away from its definition jumps to one
    # place and stays cool blue however popular the symbol is project-wide;
    # the definition of a six-caller function reads hot. Drawn before (under)
    # the search highlights and the glyphs.
    _u_vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
    _uspans = _usage_spans(ds, text, _usage_tree, _usage_off, _u_vpath)
    if _uspans:
        _u_vspan = (_usage_off + 1, _usage_off + text.count('\n') + 1)
        for _us, _ue, _su, _at_def in _uspans:
            u_line, _ = _index_to_line_col(text, _us)
            sy = origin_y + u_line * line_px
            ey = sy + line_px
            if ey < rect_min_y or sy > rect_max_y:
                continue
            sx = origin_x + _colx(_us)
            ex = origin_x + _colx(_ue)
            usage_bg = _usage_wash_color(_usage_target_count(ds, _su, _at_def, _u_vpath, _u_vspan))
            draw_list.add_rect_filled(sx - 1, sy + 1, ex + 1, ey - 1, usage_bg, 3.0)

    # Search match highlights (drawn under the text so glyphs stay readable).
    # The current match radiates a circular gradient glow with its rect cut out
    # so the matched text stays visible; the rest get a thin border. Look is
    # tunable via Toggles.SearchSettings (see search_glow.draw_search_highlight).
    if search_matches:
        for m_idx, (ms, me) in enumerate(search_matches):
            m_line, _ = _index_to_line_col(text, ms)
            sx = origin_x + _colx(ms)
            ex = origin_x + _colx(me)
            sy = origin_y + m_line * line_px
            ey = sy + line_px
            if ey < rect_min_y or sy > rect_max_y:
                continue
            draw_search_highlight(draw_list, sx, sy, ex, ey, current=(m_idx == current_local))

    # Parse/compile-error line highlight from the routed code_tree or a routed
    # exception: a translucent red band spanning the offending line, drawn under
    # the glyphs so the code stays readable. The message itself rides in the file
    # header (see draw_jump_to_bar), not painted over the code.
    if _err_markers:
        err_bg = (0.824, 0.157, 0.157, 0.431)  # translucent red
        for err_line, _msg in _err_markers:
            ey0 = origin_y + (err_line - 1) * line_px
            ey1 = ey0 + line_px
            if ey1 < rect_min_y or ey0 > rect_max_y:
                continue
            draw_list.add_rect_filled(origin_x - 4, ey0, origin_x + visible_width, ey1, imgui.get_color_u32_rgba(*err_bg))

    # Diff wash highlights: in is_diff mode each line's leading marker (the +/- left over
    # half the unified diff, with the ---/+++/@@ headers already stripped by the
    # caller) drives a full-width background - added lines green, deleted lines
    # yellow - drawn under the glyphs so the code stays readable.
    if is_diff:
        add_bg = (0.157, 0.627, 0.157, 0.353)  # translucent green
        del_bg = (0.824, 0.745, 0.157, 0.353)  # translucent yellow
        for line_idx, line_text in enumerate(text.split('\n')):
            c = line_text[:1]
            bg = add_bg if c == '+' else del_bg if c == '-' else None
            if bg is None:
                continue
            dy0 = origin_y + line_idx * line_px
            dy1 = dy0 + line_px
            if dy1 < rect_min_y or dy0 > rect_max_y:
                continue
            draw_list.add_rect_filled(origin_x - 4, dy0, origin_x + visible_width, dy1, imgui.get_color_u32_rgba(*bg))

    # Syntax-highlighted text - only the visible window is tokenized (see
    # `_window`), so this is O(visible) not O(buffer). The loop starts at the
    # window's first line and source offset; tokens above it (the merge-context
    # lookback) are processed but viewport-culled. Each token is drawn one
    # line-segment at a time with a single add_text call rather than per glyph.
    win_line, win_off, tokens, _ = _window()

    x = origin_x
    y = origin_y + win_line * line_px   # window's first line (lookback above the clip)
    src_i = win_off    # ABSOLUTE source index at the start of the current token
    _tv_idx = 0        # Nth inline view drawn this frame - its STABLE name. Render
                       # order stays stable frame-to-frame (so each view keeps its
                       # state), unlike source/line position which shifts on edits.
    _tv_edit = None    # (src_index, src_len, new_value) from an inline view that changed
    _tv_click = None   # (src_index, src_len, right_half) - press landed on a whole-token widget
    for token, color_key in tokens:
        color = COLORS[color_key]
        # Inline token view: a str-keyed token_views entry with a char_width draws
        # a widget INSTEAD of this token's text, occupying char_width cells (see
        # the token-views note above). type-keyed entries are handled by the
        # overlay pass after the body.
        _view = token_views.get(color_key) if token_views else None
        _inline = _view is not None and _view.get("char_width") is not None
        # Whole-token inline view: one widget for the entire token (e.g. a
        # clickable "True" word, a drag for "3.14") rather than one per char.
        # These tokens never contain '\n', so no segment loop is needed. Two
        # layouts, picked by the spec's lead_cells:
        #  - REPLACE (lead_cells absent): the widget IS the token - exactly
        #    len(token) cells, identity vcols, sits in the grid like the
        #    literal it replaces.
        #  - ACCESSORY (lead_cells=N): the editor draws the token TEXT itself,
        #    normally (same color/grid, fully editable as text), shifted right
        #    by N cells; the widget gets only the N-cell lead area to its left
        #    (e.g. a color swatch). vcols reflects the shift for caret/click.
        # Either way a changed return splices the whole token.
        if _inline and _view.get("whole_token") and token and '\n' not in token:
            _lead = _view.get("lead_cells", 0)
            _cells = _lead + len(token)
            if y + line_px >= rect_min_y and y <= rect_max_y:
                _name = f"{ds.name}_tv{_tv_idx}"
                _tv_idx += 1
                _save_cur = imgui.get_cursor_screen_pos()
                # pad_px widens a REPLACE widget's view - and so its clip rect -
                # a few px past the token cells on both sides, giving the frame
                # breathing room around the glyphs. (Expanding inside the
                # renderer doesn't work: drawing clips at the view boundary.)
                # The cells the token reserves in the grid stay exact.
                _pad = 0 if _lead else _view.get("pad_px", 0)
                imgui.set_cursor_screen_pos((x - _pad, y))
                _w = (_lead * char_w) if _lead else (len(token) * char_w + 2 * _pad)
                try:
                    _res = _view["renderer"](token, width=_w, height=line_px, name=_name)
                except Exception:                    _res = None
                imgui.set_cursor_screen_pos(_save_cur)
                if _lead:
                    draw_list.add_text(x + _lead * char_w, y, color, token)
                if (isinstance(_res, tuple) and len(_res) >= 2 and _res[0]
                        and isinstance(_res[1], str) and _res[1] != token):
                    _tv_edit = (src_i, len(token), _res[1])
                # owns_mouse REPLACE widgets consume the melty mouse events, so
                # a press on them never reaches the editor's click handling -
                # read the raw press and place the caret beside the literal
                # instead. Replace REPLACE widgets (bool) and ACCESSORY widgets
                # skip this: the toggle takes normal editor clicks, and a press
                # on an accessory (opening its popover) shouldn't move the caret.
                # Skipped while an imgui input owns the keyboard (want_text_input):
                # that press belongs to the widget's typing mode (caret moves,
                # select) and refocusing the editor would double-feed keystrokes.
                if (_view.get("owns_mouse") and not _lead
                        and imgui.is_mouse_clicked(0)
                        and not io.want_text_input
                        and x <= io.mouse_pos.x < x + _cells * char_w
                        and y <= io.mouse_pos.y < y + line_px):
                    _tv_click = (src_i, len(token),
                                 io.mouse_pos.x >= x + _cells * char_w * 0.5)
            x += _cells * char_w
            src_i += len(token)
            continue
        start = 0
        while True:
            nl = token.find('\n', start)
            seg = token[start:nl] if nl != -1 else token[start:]
            if seg and y + line_px >= rect_min_y and y <= rect_max_y:
                if _inline:
                    # Inline view: a render_func drawn char-by-source-char, each in
                    # a char_width cell (source stays one char per glyph, matching
                    # the vcols map). Called like any widget - (input_value)→
                    # (changed, new_value) - positioned into the cell via the cursor;
                    # a changed result splices the new value into the source below.
                    _cw = _view["char_width"]
                    _ix = x
                    for _ci, _ch in enumerate(seg):
                        _src = src_i + start + _ci         # source pos (for the edit splice)
                        _name = f"{ds.name}_tv{_tv_idx}"   # render-order index (stable name)
                        _tv_idx += 1
                        _save_cur = imgui.get_cursor_screen_pos()
                        imgui.set_cursor_screen_pos((_ix, y))
                        try:
                            _res = _view["renderer"](_ch, width=_cw * char_w, height=line_px, name=_name)
                        except Exception:
                            _res = None
                        imgui.set_cursor_screen_pos(_save_cur)
                        if (isinstance(_res, tuple) and len(_res) >= 2 and _res[0]
                                and isinstance(_res[1], str) and _res[1] != _ch):
                            _tv_edit = (_src, 1, _res[1])
                        _ix += _cw * char_w
                elif color_key == 'icon':
                    # Font Awesome glyphs aren't monospaced - their natural width
                    # differs from char_w. Draw each in its own standard-width cell
                    # (so surrounding code stays grid-aligned) and nudge it 1px left
                    # to sit better in the cell.
                    ix = x
                    for ch in seg:
                        draw_list.add_text(ix - 1, y, color, ch)
                        ix += char_w
                else:
                    draw_list.add_text(x, y, color, seg)
            if nl == -1:
                # Inline views: each char occupies char_width cells; else 1 cell.
                x += len(seg) * (_view["char_width"] if _inline else 1) * char_w
                break
            x = origin_x
            y += line_px
            start = nl + 1
        src_i += len(token)

    # An inline view (e.g. the icon dropdown) changed its value - splice the new
    # text in for the view's source char and report the edit, so the framework
    # reparses/saves exactly as if it were typed.
    if _tv_edit is not None:
        _es, _el, _ev = _tv_edit
        text = text[:_es] + _ev + text[_es + _el:]
        ds.text_cursor_pos = _es + len(_ev)
        ds.text_selection_start = ds.text_selection_end = ds.text_cursor_pos
        if not _ev:
            # The widget deleted ITSELF (e.g. the number input's buffer was
            # emptied and backspace pressed again) - hand the keyboard back to
            # the editor at the literal's position so deletion keeps feeling
            # like normal text editing.
            Melty.text_focused_ds = ds
            ds.text_cursor_blink_time = time.time()
        changed = True

    # A press on a whole-token widget also places the editor caret beside the
    # literal: left half → before it, right half → after it - and focuses the
    # editor, so typing after a widget interaction feels like editing text.
    # Applied after the splice so it overrides its caret-at-end default; if the
    # same widget changed the value (bool toggle), use the new token's length.
    if _tv_click is not None:
        _cs, _cl, _right = _tv_click
        if _tv_edit is not None and _tv_edit[0] == _cs:
            _cl = len(_tv_edit[2])
        Melty.text_focused_ds = ds
        ds.text_cursor_pos = _cs + _cl if _right else _cs
        ds.text_selection_start = ds.text_selection_end = ds.text_cursor_pos
        ds.text_cursor_blink_time = time.time()

    # Token views keyed by code_tree node TYPE (e.g. Conditional) — overlay pass,
    # positioned by each node's span. Runs after the inline text so widgets paint
    # on top of the code they annotate. The PARSE arrives as code_tree in the
    # chain routes but as code_dict on the code-host-cache route (where
    # code_tree carries only the error dict - see draw_text_editor_code_cache),
    # so prefer code_dict when both are present; it's the node tree with spans.
    _tv_tree = code_dict if code_dict is not None else code_tree
    if token_views and _tv_tree is not None:
        # Span cols are in the PARSE's coords (dedented on the code-host
        # route); shift the origin by the indent delta so node overlays land
        # on the glyphs, which use the buffer's file-indented chars.
        _tv_shift = _parse_col_shift(text, getattr(_tv_tree, 'source', '') or '')
        _draw_cst_token_views(_tv_tree, token_views, origin_x + _tv_shift * char_w,
                              origin_y, line_px, char_w, ds,
                              line_offset=_usage_off, jump_to=jump_to)

    # --- Spell-check squiggles -------------------------------------------------
    # Red wavy lines under unknown words. Gated behind the global toggle and
    # only recomputed when the buffer text changes (cached on the draw_state), so
    # scrolling / cursor-blink repaints never re-scan. Drawn after the glyphs and
    # inside the text clip rect so the squiggles scroll with the code.
    #
    # TODO(symbol-aware): this currently spell-checks every alphabetic word in the
    # buffer (find_misspellings(text)). THIS is the integration point - when the
    # libcst parsing work lands, drive the span list off the routed `code_tree`
    # instead: only check tokens belonging to comment / string / docstring /
    # identifier symbols, splitting identifiers on camelCase / snake_case. Do NOT
    # reuse this view's syntax `tokenize()` for that - the libcst symbol tree is
    # the source of truth. Replace the find_misspellings(text) call below with a
    # tree-driven list of (start, end, word) spans; the rendering stays the same.
    if Toggles.TextEditor.enable_spell_check:
        if getattr(ds, '_spell_cache_text', None) != text:
            from src.lsd.gl_gui.view.core_views import spell_check
            ds._spell_cache_text = text
            ds._spell_errors = spell_check.find_misspellings(text)
        spell_color = 0xFF0000FF  # red (ABGR)
        period = 4.0   # px per complete zig-zag
        amp = 1.6      # px above/below the baseline
        for ws, we, _word in ds._spell_errors:
            e_line, _ = _index_to_line_col(text, ws)
            sx = origin_x + _colx(ws)
            ex = origin_x + _colx(we)
            base_y = origin_y + e_line * line_px + line_px - 2.0
            if base_y < rect_min_y or base_y > rect_max_y:
                continue
            # Triangle-wave squiggle from short segments (see the add_line
            # idiom is here; no reliance on add_polyline).
            px, py = sx, base_y
            up = True
            cx = sx
            while cx < ex:
                nx = min(cx + period / 2.0, ex)
                ny = base_y - amp if up else base_y + amp
                draw_list.add_line(px, py, nx, ny, spell_color, 1.0)
                px, py = nx, ny
                cx = nx
                up = not up

    # Cursor. Drawn at the caret even while a selection exists, so the active
    # (moving) edge of a drag or shift-selection shows where delete and arrow
    # keys will act from - text_cursor_pos already tracks that location.
    blink_cursor = False
    if is_focused:
        if not blink_cursor or (time.time() - ds.text_cursor_blink_time) % 1.0 < 0.5:
            cx, cy = _char_pos_to_xy(text, ds.text_cursor_pos, origin_x, origin_y, line_px, vcols=vcols)
            current_line_rect = (int(origin_x), int(cy + 1), int(origin_x + visible_width), int(cy + line_px + 1))
            line_highlight_color = imgui.get_color_u32_rgba(*Tint.cursor_tint()[:3], 0.05)
            draw_list.channels_set_current(Core.melty.get_channel() - 1)  # draw under the text
            draw_list.add_rect_filled(*current_line_rect, line_highlight_color)
            draw_list.channels_set_current(Core.melty.get_channel() + 1)  # draw under the text

            imgui_color = imgui.get_color_u32_rgba(*Tint.cursor_tint()[:3], 1.0)
            draw_list.add_line(cx, cy, cx, cy + line_px, imgui_color, 2.0)
            # Highlight selection

    # Function call parameter hint, floated over the code (within the body clip so
    # it never rides up onto the header). Only for the focused editor.
    if Melty.text_focused_ds is ds:
        _draw_signature_hint(ds, draw_state, text, origin_x, origin_y, line_px, vcols=vcols)

    draw_list.pop_clip_rect()
    # --- Line-number gutter ---
    # Drawn after the text body in its own clip column (left to → gutter_w) so
    # the numbers stay fixed while code scrolls horizontally under them. Numbers
    # ride origin_y, so they scroll vertically in lockstep with their lines. The
    # cursor's line is brightened for emphasis.
    if show_gutter and gutter_w > 0:
        gutter_bg = (0.11, 0.129, 0.149, 1.0)  # faint gray column
        num_color = COLORS['comment']
        cur_color = COLORS['default']
        cur_line = _index_to_line_col(text, ds.text_cursor_pos)[0] if is_focused else -1
        # Clamp the column's top to the text body (origin_y) so the fill doesn't
        # ride up over the header bar above it; rect_min_y still works once the
        # body has scrolled up past the clip top.
        gutter_top = max(rect_min_y, origin_y)
        draw_list.push_clip_rect(left, gutter_top, left + gutter_w, rect_max_y, True)
        draw_list.add_rect_filled(left, gutter_top, left + gutter_w, rect_max_y, imgui.get_color_u32_rgba(*gutter_bg))
        total_lines = text.count('\n') + 1
        for line_idx in range(total_lines):
            ly = origin_y + line_idx * line_px
            if ly + line_px < gutter_top or ly > rect_max_y:
                continue
            if line_numbers is not None:
                # Trailing empty line (diff text ends in \n) has no number; so do
                # any line whose number was explicitly None.
                num = line_numbers[line_idx] if line_idx < len(line_numbers) else None
                if num is None:
                    continue
                num_str = str(num)
            else:
                num_str = str(line_offset + line_idx + 1)
            nx = left + gutter_w - 6.0 - len(num_str) * char_w
            draw_list.add_text(nx, ly, cur_color if line_idx == cur_line else num_color, num_str)
        draw_list.pop_clip_rect()

    if changed:
        text_height = (text.count('\n') + 1) * line_px + 2
    else:
        text_height = (input_value.count('\n') + 1) * line_px + 2

    # text_width = max(vcols) if vcols else max((len(l) for l in text.split('\n')), default=0) * char_w

    # --- Code-suggest popup (dropdown menu anchored to the caret) ---
    # Rendered after the body (and after the monospace font is popped, so its
    # rows use the normal UI font) so it floats above the code. We reuse the
    # dropdown's menu render with its own search box suppressed - the editor
    # owns text focus and the half-typed identifier IS the filter. A flat
    # name->name dict makes each leaf return the chosen identifier; a mouse click
    # bubbles back as (changed, pick) and we splice it in like the keyboard accept.
    # Mode.WINDOW menus are LATCHED - once drawn they persist until explicitly
    # closed, so we must call draw_dd_menu EVERY frame and toggle `closed=` rather
    # than gating the call (a gated call would leave the last-open frame painted).
    # Only the open state feeds real items / drives the keep-alive repaint.
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_dd_menu
    _ac_show = (Melty.text_focused_ds is draw_state and getattr(ds, '_ac_open', False)
                and bool(getattr(ds, '_ac_candidates', None)))
    _ac_cands = ds._ac_candidates if _ac_show else []
    _ac_items = {n: n for n in _ac_cands}
    _ac_anchor = getattr(ds, '_ac_anchor', ds.text_cursor_pos)
    _ac_x, _ac_y = _char_pos_to_xy(text, _ac_anchor, origin_x, origin_y, line_px, vcols=vcols)
    if _ac_show:
        # Keyboard-vs-hover highlight. The menu paints the keyboard cursor only in
        # _kbd_mode, else the hovered row - so we keep _kbd_mode True while the
        # mouse is NOT over the popup (selection always shown, never goes
        # blank) and ONLY drop to hover if the mouse actually MOVES over it. A
        # resting pointer never drives the highlight, so arrow nav keeps working
        # even with the mouse parked over the popup.
        _mp = imgui.get_mouse_pos()
        _lm = getattr(ac_state, '_last_mouse', None)
        _pop_x0, _pop_y0 = _ac_x, _ac_y + line_px
        _pop_h = min(len(_ac_cands) * 24 + 10, 312)        # ~row height, capped
        _over = (_pop_x0 - 4 <= _mp[0] <= _pop_x0 + 400
                 and _pop_y0 - 2 <= _mp[1] <= _pop_y0 + _pop_h)
        _moved = _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5)
        if not _over:
            ac_state._kbd_mode = True       # mouse away → keyboard selection shown
        elif _moved:
            ac_state._kbd_mode = False      # actively moving over it → hover drives
        # over + resting → leave as-is (so an arrow's _kbd_mode=True persists)
        ac_state._last_mouse = (_mp[0], _mp[1])

        # The popup is a CACHED latched window - re-calling draw_dd_menu does NOT
        # repaint it (verified: the highlight sticks on the row it first opened on).
        # Force its tile dirty here so the current selection/hover row actually
        # paints. Must be invalidate_up (it cascades to CHILD tiles below): a plain
        # invalidate leaves the dd_menu collection inside the window clip, so it
        # blit-skips and the rows never re-render with the new cursor pos - same
        # mechanism the begin_frame popover code uses for regular dropdowns. This
        # body only re-runs on events (keys/hover), so it's one invalidate per
        # interaction, not a per-frame spin. The tile id (found by name below)
        # carries a hash, so look it up rather than hard-code it.
        _mt = getattr(ds, '_ac_menu_tile', None)
        if _mt is not None:
            Melty.cache.invalidate_up(_mt, force=True)

    imgui.dummy(draw_state.content_width, max(draw_state._kwargs.get("min_height", 0), text_height))

    # draw_dd_menu is a LATCHED window: called every frame with closed=not _ac_show
    # so it persists when this (slow) body is skipped. Hover/keys wake the loop;
    # background results wake it via the future's done-callback (_ac_on_future).
    ac_changed, ac_pick = draw_dd_menu(
        _ac_items, name=f"{ds.name}_ac_menu", view_offset=False,
        temp=True, show_search=False, swoosh=False, closed=not _ac_show, height=300, auto_resize=False,
        window_pos=(_ac_x - draw_state.abs_left, _ac_y - draw_state.abs_top + line_px), text_align="left",
        row_tags=(getattr(ds, '_ac_kinds', None) if _ac_show else None),
        parent_window=draw_state, root_state=ac_state, path_prefix=())
    # Cache the popup window's tile id (it carries a hash) so the invalidate above
    # can find it next frame. Scanned once; updates if the tile is rebuilt.
    if _ac_show:
        _mt = getattr(ds, '_ac_menu_tile', None)
        if _mt is None or _mt not in Melty.cache._tiles:
            _pref = f"{ds.name}_ac_menu##"
            for _k in Melty.cache._tiles:
                if _k.startswith(_pref):
                    ds._ac_menu_tile = _k
                    break
    # Hover may have moved the menu's cursor (when the mouse is over it); mirror
    # that back into our selection index so Enter/arrows continue from the hovered row.
    if _ac_show and not ac_state._kbd_mode:
        _cp = ac_state.cursor_path
        if isinstance(_cp, tuple) and len(_cp) == 1 and _cp[0] in _ac_cands:
            ds._ac_index = _ac_cands.index(_cp[0])
    if ac_changed and isinstance(ac_pick, str):
        anchor = ds._ac_anchor
        text = text[:anchor] + ac_pick + text[ds.text_cursor_pos:]
        ds.text_cursor_pos = anchor + len(ac_pick)
        ds.text_selection_start = ds.text_cursor_pos
        ds.text_selection_end = ds.text_cursor_pos
        ds._ac_open = False
        ds._ac_request_anchor = -1
        changed = True


    # --- Usage-jump picker (multi-use symbols) ---
    # Same latched window contract as the suggestion popup above: draw_dd_menu
    # is called EVERY frame with closed= toggled. Rows are the symbol's users
    # ({scope_id: UsageRef}, with the text as the dim row tag); a pick - mouse
    # or Enter (handled in the key block) - opens that site in IntelliJ.
    _uj_show = (Melty.text_focused_ds is draw_state and getattr(ds, '_uj_open', False)
                and bool(getattr(ds, '_uj_items', None)))
    _uj_items = ds._uj_items if _uj_show else {}
    _uj_anchor = getattr(ds, '_uj_anchor', ds.text_cursor_pos)
    _uj_x, _uj_y = _char_pos_to_xy(text, _uj_anchor, origin_x, origin_y, line_px, vcols=vcols)
    if _uj_show:
        # Keyboard-vs-hover highlight: same dance as the suggestion popup -
        # keyboard selection shows unless the mouse actively MOVES over the
        # popup; a resting pointer never steals the highlight.
        _mp = imgui.get_mouse_pos()
        _lm = getattr(uj_state, '_last_mouse', None)
        _pop_x0, _pop_y0 = _uj_x, _uj_y + line_px
        _pop_h = min(len(_uj_items) * 24 + 10, 312)
        _over = (_pop_x0 - 4 <= _mp[0] <= _pop_x0 + 400
                 and _pop_y0 - 2 <= _mp[1] <= _pop_y0 + _pop_h)
        _moved = _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5)
        if not _over:
            uj_state._kbd_mode = True
        elif _moved:
            uj_state._kbd_mode = False
        uj_state._last_mouse = (_mp[0], _mp[1])
        # Cached latched window: force its tile dirty per interaction so the
        # selection/hover row actually repaints (see the AC popup note above).
        _mt = getattr(ds, '_uj_menu_tile', None)
        if _mt is not None:
            Melty.cache.invalidate_up(_mt, force=True)

    uj_changed, uj_pick = draw_dd_menu(
        _uj_items, name=f"{ds.name}_uj_menu", view_offset=False, show_bg=True,
        temp=True, show_search=False, swoosh=False, closed=not _uj_show, min_height=140, bg_offset=0, auto_resize=False, min_width=500,
        window_pos=(_uj_x - draw_state.abs_left, _uj_y - draw_state.abs_top + line_px), text_align="left",
        row_tags=(getattr(ds, '_uj_tags', None) if _uj_show else None),
        parent_window=draw_state, root_state=uj_state, path_prefix=(), tint=(0.06, 0.08277813, 0.13))
    if _uj_show:
        _mt = getattr(ds, '_uj_menu_tile', None)
        if _mt is None or _mt not in Melty.cache._tiles:
            _pref = f"{ds.name}_uj_menu##"
            for _k in Melty.cache._tiles:
                if _k.startswith(_pref):
                    ds._uj_menu_tile = _k
                    break
    # Mirror a hover-moved cursor back into the keyboard index so Enter/arrows
    # continue from the hovered row.
    if _uj_show and not uj_state._kbd_mode:
        _cp = uj_state.cursor_path
        if isinstance(_cp, tuple) and len(_cp) == 1 and _cp[0] in _uj_items:
            ds._uj_index = list(_uj_items).index(_cp[0])
    if uj_changed and getattr(uj_pick, 'path', None) is not None:
        _open_usage_ref(uj_pick)
        ds._uj_open = False

    if _font_pushed:
        imgui.pop_font()

    # --- Floating error box pinned to the bottom of the view ---
    # The first error message used to ride inline in the jump-to header at the
    # top of the view; instead float it in a box along the bottom edge of the
    # visible viewport so it stays put while the code scrolls and never pushes
    # the header down. Drawn after the body (and after the monospace font pop, so
    # it uses the normal UI font) so it paints over the code. Save the cursor,
    # paint at the bottom, then restore it so the rest of the layout is untouched.
    if jump_to is not None and _err_msg:
        _save_cursor = imgui.get_cursor_screen_pos()
        clip_l, _clip_t, clip_r, clip_b = draw_state.abs_clip_rect
        pad_x, pad_y, margin = 8, 5, 6
        box_h = imgui.get_text_line_height() + pad_y * 2
        bx0 = clip_l + margin
        bx1 = clip_r - margin
        by1 = clip_b - margin
        by0 = by1 - box_h
        # Same red-tinted fill and outline as the (former) header error row.
        fill_col = (0.275, 0.118, 0.157, 0.922)
        line_col = (0.588, 0.235, 0.275, 1.0)
        err_draw_list = imgui.get_window_draw_list()
        err_draw_list.add_rect_filled(bx0, by0, bx1, by1, imgui.get_color_u32_rgba(*fill_col), 4.0)
        err_draw_list.add_rect(bx0, by0, bx1, by1, imgui.get_color_u32_rgba(*line_col), 4.0)
        # Truncate to the box width so a long message doesn't overflow. The box
        # shows the FIRST marker (every marker still gets its red line wash);
        # with more than one, say so rather than silently hiding the rest.
        msg = str(_err_msg).split('\n', 1)[0]
        if len(_err_markers) > 1:
            msg = f"{msg}   (+{len(_err_markers) - 1} more)"
        avail = max(0, (bx1 - bx0) - 2 * pad_x)
        if imgui.calc_text_size(msg).x > avail:
            ch_w = max(1.0, imgui.calc_text_size("x").x)
            keep = max(3, int(avail / ch_w) - 1)
            msg = msg[:keep] + "…"
        imgui.set_cursor_screen_pos((bx0 + pad_x, by0 + pad_y))
        imgui.text_colored(msg, 1.0, 0.5, 0.46, 1.0)
        imgui.set_cursor_screen_pos(_save_cursor)

    # window_pos is an offset from the parent window's absolute origin. The menu
    # window carries an intrinsic ~one-row top offset (draw_dropdown back-compensates
    # the same way), so anchor at the caret's line top minus a line to sit it snug
    # under the insertion site instead of a line too low.

    # --- Parse-error staleness tracking
    # The error messages come from a BACKGROUND reparse, so the moment the buffer
    # changes they describe an OLD buffer - wrong line numbers (esp. after adding
    # / removing lines) or an error that's already been fixed. Mark them stale the
    # moment the editable text changes, and keep them stale until a FRESH parse
    # result arrives - detected as a new `error` / `code_tree` object pair
    # (the chain hands back the same cached object until it reparses). This holds
    # the message off for exactly the reparse gap, with no timing guess, and the
    # text-compare catches every edit including pure newline insertions.
    _parse_pair = (error, code_tree)
    _prev_text = getattr(ds, '_err_prev_text', None)
    if _prev_text is None:
        ds._err_prev_text = text                  # baseline on first render
    elif text != _prev_text:
        ds._err_prev_text = text
        if not getattr(ds, '_err_stale', False):
            ds._err_stale = True
            ds._err_stale_pair = _parse_pair       # this parse is now outdated
    elif getattr(ds, '_err_stale', False):
        _sp = getattr(ds, '_err_stale_pair', (None, None))
        if not (error is _sp[0] and code_tree is _sp[1]):
            ds._err_stale = False                  # a fresh parse landed

    if changed:
        return True, text
    return False, original_input