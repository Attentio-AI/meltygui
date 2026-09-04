import bisect
import builtins as _builtins
import keyword
import math
import re
import time

import glfw
import imgui

from src.lsd.gl_gui.model.core_model.draw_state import (DropDownState,
                                                        TextEditorState)
from src.lsd.gl_gui.render_funcs import RenderFuncs
from src.lsd.gl_gui.toggles import Tint
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import CodeLine
from src.lsd.gl_gui.view.core_views.blit_offscreen import add_shadow, add_glow, clear_glows
from src.lsd.gl_gui.view.core_views.core_render import render_func, SCROLLBAR_MARGIN
from src.lsd.gl_gui.view.core_views.headers import draw_header, draw_footer
from src.lsd.gl_gui.view.core_views.search_glow import draw_search_highlight_multi
from src.lsd.gl_gui import mouse_cursor
from src.lsd.gl_gui.melty import Melty, SearchTerm
from src.lsd.gl_gui.perf_trace import trace as _ptrace
from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.jump_to import draw_jump_to
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults, Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.toggles import Swoosh
from src.lsd.gl_gui.fim import FimState


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
    # Off-screen stretch of a long line, never drawn (see _window_tokens band)
    'clipped': _hex('#a9b7c6'),
    'keyword': _hex('#cc7832'),  # Keyword
    'keyword_const': _hex('#cc7832'),  # Keyword.Constant (None)
    'bool': _hex('#cc7832'),  # True/False - own key so color highlighting can target them
    'def': _hex('#cc7832'),  # bare `def` keyword - own key so token_views can target defs
    'operator_word': _hex('#cc7832'),  # Operator.Word (and, or, not, in, is)
    'builtin_pseudo': _hex('#94558d'),  # Name.Builtin.Pseudo (self, cls)
    'builtin': _hex('#8583cf'),  # Name.Builtin (len, isinstance, str, Exception, ...) - Darcula light purple
    'def_name': _hex('#56a8f5'),  # Name.Function (declaration) — IntelliJ Dark blue
    'decorator': _hex('#bbb529'),  # Name.Decorator
    'string': _hex('#6a8759'),  # String
    'string_doc': _hex('#629755'),  # String.Doc (docstrings)
    'comment': _hex('#5e6265'),  # Comment - muted & darker so comments recede
    'line_no': _hex('#808080'),  # Gutter numbers keep the old comment color
    'number': _hex('#6897bb'),  # Number
    'color3': _hex('#6897bb'),  # merged color tuple `(r, g, b[, a])` (fallback text color)
    'colorhex': _hex('#6a8759'),  # a string literal that IS a hex color (`'#8888c6'`) - string color, swatch beside it
    'icon': _hex('#56b6c2'),  # Font Awesome / PUA glyph (cyan, distinct from strings)
}

class _LoadingSentinel(str):
    """draw_text(LOADING): the caller's real buffer is still loading —
    render the instant-restore stand-in from the TextEditorState viewport
    snapshot instead of nothing. A str subclass (not None) because the
    render_func wrapper serves its input cache for a None input_value
    without running the body; the value is deliberately unequal to any
    real buffer so input-change detection always fires on the swap-in."""
    __slots__ = ()

LOADING = _LoadingSentinel("\x00__lsd_loading__\x00")


KEYWORDS = {'def', 'class', 'if', 'else', 'elif', 'for', 'while',
            'return', 'import', 'from', 'with', 'as', 'try', 'except',
            'finally', 'raise', 'yield', 'pass', 'break', 'continue',
            'lambda', 'global', 'nonlocal', 'del', 'assert', 'async', 'await'}

KEYWORD_CONSTS = {'True', 'False', 'None'}

OPERATOR_WORDS = {'and', 'or', 'not', 'in', 'is'}

BUILTIN_PSEUDO = {'self', 'cls'}

# Every builtin function / type / exception (`len`, `isinstance`, `str`,
# `frozenset`, `Exception`, ...) colors light purple. Not the dunders
# (`__import__`) and not the keyword constants, those keep their own kinds.
BUILTINS = frozenset(n for n in dir(_builtins)
                     if not n.startswith('_') and n not in KEYWORD_CONSTS)

WORD_DELIMITERS = ' \t\n\r,.;:!?()[]{}\'\"=+-*/<>@#$%^&|~`\\'


# --- Code-suggestion (autocomplete) --------------------------------------------
# Drives the dropdown popup (draw_dd_menu) anchored at the caret. IDE trigger
# model (IntelliJ-style): the popup opens as you TYPE — an identifier char
# (scope completion), a '.' (member access), or inside an import line (module
# completion) - or explicitly on Ctrl+Space (Ctrl+P asks for an fn-call
# PARAM HINT instead - see the signature-help block). Typing in a comment or
# string stays quiet (checked against the same incremental _line_open lexer
# state the viewport tokenizer maintains), as does naming something NEW right
# after def/class/for/as/....
#
# Candidate sources, best first:
#   bare identifier - libcst_conversion.completions_at over the routed code_tree
#     (scope-aware: params/locals → class members → module names → imports →
#     indexed symbols), then a plain buffer scan (just-typed locals the parse
#     hasn't caught up to), then builtins + keywords at the lowest priority.
#     Kind tags upgrade to exact runtime type names where FuncsMetadata has
#     observed the edited function's scope (the eval REPL records it).
#   dotted receiver / import line - _ensure_member_completions: first the LIVE
#     module namespace of the edited file (instant, exact - `imgui.`, `Melty.`,
#     any top-level name, walked with func_metadata.member_completions),
#     falling back to jedi over the WHOLE surrounding file (async, subprocess
#     pool - resolves `self.`, just-typed locals, import statements).

_IDENT_RE = re.compile(r'[A-Za-z_]\w*')
# Buffer-scan variant: identifiers NOT preceded by a '.' - an attribute spelling
# (`my_object.dog`) names a member of some OTHER thing, not a scope symbol, so
# it must not surface as a bare suggestion. A name that also occurs bare
# somewhere still matches there and stays in the pool.
_BARE_IDENT_RE = re.compile(r'(?<![.\w])[A-Za-z_]\w*')
# Comment strip for the same scan - comment prose must never become
# suggestions. Single-line strings are matched FIRST and kept, so a '#' inside
# one can't eat the code after it; a bare '#' then drops the rest of the line.
# One C-speed sub, no lexer state - cheap enough for the per-keystroke pool
# rebuild. A '#' inside a still-unterminated string or a multi-line triple
# quote is over-stripped, but that only ever drops STRING words, never bare.
_SCAN_COMMENT_RE = re.compile(
    r'''("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|#[^\n]*''')


def _strip_comments(text):
    # \1 keeps a matched string alternate; for a bare match the group didn't
    # participate and sub() substitutes empty; all C-speed, no per-match lambda.
    return _SCAN_COMMENT_RE.sub(r'\1', text)

_SCOPE_HEAD_RE = re.compile(r'^(\s*)(def|class)\s+([A-Za-z_]\w*)')


def _blank_foreign_scopes(lines, caret_line):
    """Blank (in place) every buffer region whose names are invisible at
    0-indexed `caret_line`, mirroring Python's lookup rules:

    - a def/class block NOT containing the caret is blanked whole; only a
      TOP-LEVEL block keeps its name (a module-level def/class is referencable
      from anywhere, a nested one only through its container),
    - a caret-containing CLASS whose caret sits in a nested def/class also has
      its direct body lines blanked — class-scope names never resolve bare
      from inside a method (`enable_jedi` in a Toggles method is a NameError).

    Pure indentation walk over the live buffer, so it also covers just-typed
    defs the parse hasn't caught up to (and the no-parse fallback, where the
    scan is the only candidate source). Trailing blank lines are trimmed from
    a block before the containment test, so a caret on an empty line between
    two defs counts as OUTSIDE the one above."""
    n = len(lines)

    def block_end(i, indent):
        j = i + 1
        while j < n:
            s = lines[j]
            if s.strip() and len(s) - len(s.lstrip()) <= indent:
                break
            j += 1
        while j - 1 > i and not lines[j - 1].strip():
            j -= 1
        return j

    def walk(lo, hi, blank_direct):
        i = lo
        while i < hi:
            m = _SCOPE_HEAD_RE.match(lines[i])
            if m is None:
                if blank_direct and i != caret_line:
                    lines[i] = ""
                i += 1
                continue
            indent = len(m.group(1))
            end = block_end(i, indent)
            if i <= caret_line < end:
                # Is the caret on one of this block's DIRECT lines, or in
                # a nested def/class of it?
                nested = False
                j = i + 1
                while j < end:
                    m2 = _SCOPE_HEAD_RE.match(lines[j])
                    if m2 is not None:
                        e2 = block_end(j, len(m2.group(1)))
                        if j <= caret_line < e2:
                            nested = True
                            break
                        j = max(e2, j + 1)
                    else:
                        j += 1
                if blank_direct:
                    # The surrounding class body is invisible from the caret,
                    # and this block's NAME is one of its attrs - drop the
                    # name but keep the rest of the line (the def's params
                    # are real scope names for the caret inside it).
                    lines[i] = m.group(1) + m.group(2) + lines[i][m.end():]
                walk(i + 1, end,
                     blank_direct=(m.group(2) == "class" and nested))
            else:
                # A blanked block keeps its NAME - it's a binding in the
                # scope we're walking (module global, parent-def local, or a
                # class attr when the caret is directly in that class body),
                # and the caret-line cap downstream drops it when it's bound
                # below the caret. Under blank_direct the surrounding class
                # block itself is invisible, so the name goes too.
                lines[i] = "" if blank_direct else m.group(3)
                for k in range(i + 1, end):
                    lines[k] = ""
            i = end
    walk(0, n, False)
    return lines
# Identifier immediately to the left of a position - the half-typed word the
# popup filters by (and the span an accepted suggestion replaces).
_PREFIX_RE = re.compile(r'[A-Za-z_]\w*$')
_PY_KEYWORDS = frozenset(keyword.kwlist)
_AC_MAX_ROWS = 40  # cap so a huge file can't render a million-row popup

def _completion_context(text, cursor):
    """The completion site at `cursor`: the identifier `prefix` being typed, the
    `anchor` index where it starts (== cursor when there's no prefix yet), and
    whether the char just before the prefix is a `.` (attribute access). The
    anchor is the span an accepted suggestion overwrites.

    Bounded backward scan, NOT `_PREFIX_RE.search(text[:cursor])`: a
    `...$`-anchored search walks the regex engine over every position from 0,
    O(buffer) — measured 6.1ms per call on a 120KB buffer, and this runs
    every focused frame (it WAS the editor's mystery per-frame keyboard
    cost). Semantics preserved: the trailing word-char run, started at its
    leftmost letter/underscore (digits can't open an identifier) — with one
    deliberate divergence: the regex's `$` also matched before a trailing
    newline, so a caret at the START of a line inherited the previous line's
    last identifier as its prefix (an accepted pick would have overwritten
    text on the line above). Here a caret after '\\n' gets the correct empty
    prefix."""
    i = cursor
    lo = max(0, cursor - 512)   # names are short; bound the walk
    while i > lo and (text[i - 1].isalnum() or text[i - 1] == '_'):
        i -= 1
    while i < cursor and not (text[i].isalpha() or text[i] == '_'):
        i += 1
    prefix = text[i:cursor]
    anchor = cursor - len(prefix)
    if anchor - 1 >= 0 and len(text) > anchor - 1:
        dot_trigger = anchor > 0 and text[anchor - 1] == "."
        return prefix, anchor, dot_trigger
    else:
        return prefix, anchor, False


def _completion_pool(code_tree, text, line, func=None):
    """Ordered (name, kind) candidate pool for a caret on 0-indexed `line`,
    best-first. The scope-aware names from the parsed `code_tree` lead (params,
    locals, members, module, imports, jedi symbols — see `completions_at`); a
    plain identifier scan of the live buffer follows (just-typed locals that
    haven't round-tripped through libcst yet); builtins and keywords close the
    list at the lowest priority, IDE-style. De-duplicated keeping the first
    (highest-ranked) occurrence of each name. When the edited span's live
    function `func` has runtime-observed scope types (FuncsMetadata, recorded by
    the eval REPL), those names' kind tags upgrade to the exact type name — the
    popup reads `draw_state  DrawState` even with an unhinted signature."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import completions_at
    pool, seen = [], set()

    meta = {}
    if func is not None:
        from src.lsd.gl_gui.func_metadata import FuncsMetadata, _type_name
        meta = FuncsMetadata.get(func)

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
    # Runtime-observed scope names (FuncsMetadata) as candidates in their own
    # right, not just kind upgrades: a call-expression span (the context menu's
    # inline editor) never MENTIONS its enclosing function's locals, so neither
    # the tree nor the buffer scan can offer them - the recorded scope is the
    # only source that knows `draw_state`/`kwargs` are typeable here.
    for _name, _vm in meta.items():
        add(_name, _type_name(_vm.type) or "local")
    # The buffer scan only backfills JUST-TYPED names the tree hasn't caught up
    # to (or could, when there's no parse) - but a name is only a valid
    # suggestion where it's in scope. Blank def/class blocks that don't enclose
    # the caret (their locals/params are invisible here; block names survive),
    # and with a tree also cap at the caret's line - a name bound below it in
    # the caret's own scope isn't defined yet. Without a tree the cap is
    # skipped: later module-level names ARE valid, and the tree isn't going to
    # supply them.
    lines = _blank_foreign_scopes(text.split("\n"), line)
    scan_text = "\n".join(lines[:line + 1] if code_tree is not None else lines)
    for name in _BARE_IDENT_RE.findall(_strip_comments(scan_text)):
        add(name, "name")
    for name in dir(_builtins):
        if not name.startswith("_"):
            add(name, "builtin")
    for kw in keyword.kwlist:
        if len(kw) > 1 and kw not in seen:
            seen.add(kw)
            pool.append((kw, "kw"))
    if meta:
        pool = [(n, (_type_name(meta[n].type) or k) if n in meta else k)
                for n, k in pool]
    return pool


def _usage_user_counts(ds, tree):
    """name -> total usage-site count from the buffer's symbol-usage graph —
    the popup's popularity ranking. Dotted symbols also add their count under
    the bare last segment so member candidates (offered as bare names after a
    `.`) rank too. Cached per (tree, su-map) identity on the draw_state; the
    in-place background usage attach changes id(su_top) and busts it — the
    same key discipline as _def_tints."""
    if not isinstance(tree, dict):
        return None
    key = (id(tree), id(tree.get("__symbol_usages__")))
    if getattr(ds, "_ac_users_key", None) == key:
        return ds._ac_users
    counts = {}
    seen = set()

    def walk(node, depth=0):
        if not isinstance(node, dict) or depth > 64 or id(node) in seen:
            return
        seen.add(id(node))
        su_map = node.get("__symbol_usages__")
        if isinstance(su_map, dict):
            for su in su_map.values():
                if id(su) in seen:
                    continue
                seen.add(id(su))
                nm = getattr(su, "name", None)
                n = len(getattr(su, "sites", None) or ())
                if not nm or not n:
                    continue
                nm = str(nm)
                counts[nm] = counts.get(nm, 0) + n
                last = nm.rsplit(".", 1)[-1]
                if last != nm:
                    counts[last] = counts.get(last, 0) + n
        for k, v in node.items():
            if k in ("__cst__", "__symbol_usages__", "__overrides__"):
                continue
            if isinstance(v, dict):
                walk(v, depth + 1)

    walk(tree)
    ds._ac_users = counts
    ds._ac_users_key = key
    return counts


# callable -> "(a, b)" suffix (or None). Keyed by the object itself (strong
# ref keeps id stable - id-keyed caches bit us before, see live_view capture);
# bounded, reset on hotswap.
_SIG_CACHE = {}


def _callable_param_suffix(obj):
    """'(param, param2)' display suffix for a callable, or None. Parameter
    NAMES only (*args/**kw starred), leading self/cls dropped, long lists
    ellipsized — this is popup decoration, not signature help."""
    try:
        got = _SIG_CACHE.get(obj, _SIG_MISS)
    except TypeError:
        return None                      # unhashable callable - skip
    if got is not _SIG_MISS:
        return got
    import inspect
    s = None
    try:
        names = []
        for p in inspect.signature(obj).parameters.values():
            n = p.name
            if p.kind == p.VAR_POSITIONAL:
                n = "*" + n
            elif p.kind == p.VAR_KEYWORD:
                n = "**" + n
            names.append(n)
        if names and names[0] in ("self", "cls"):
            names = names[1:]
        s = "(" + ", ".join(names) + ")"
        if len(s) > 48:
            s = s[:47] + "…)"
    except (TypeError, ValueError):
        s = None
    if len(_SIG_CACHE) > 4096:
        _SIG_CACHE.clear()
    _SIG_CACHE[obj] = s
    return s


_SIG_MISS = object()


def _ac_param_suffixes(ds, text, cands, anchor, dot_trigger, address):
    """{name: '(a, b)'} for the callable candidates in `cands`, resolved from
    the live module namespace (bare names) or the live dotted receiver (after
    a '.'). Display-only decoration for the popup rows. Per-row cost is a
    dict/getattr lookup plus the memoized signature; receivers are only
    walked when they resolve to a module or class, so no instance property
    can fire."""
    ns, _func = _ac_live_context(ds, text, address)
    base = None
    if dot_trigger:
        from src.lsd.gl_gui.func_metadata import _receiver_before
        base = _live_receiver_obj(ns, _receiver_before(text, anchor))
        import types as _types
        if not isinstance(base, (type, _types.ModuleType)):
            base = None
    if base is None and not ns:
        return None
    out = {}
    for name, _kind in cands:
        try:
            obj = getattr(base, name, None) if base is not None else ns.get(name)
        except Exception:
            continue
        if not callable(obj) or isinstance(obj, type):
            continue                     # classes are out; funcs/methods only
        s = _callable_param_suffix(obj)
        if s:
            out[name] = s
    return out or None


# Flattened snippet map, memoized on the Toggles.TextEditor.AC_SNIPPETS dict:
# (memo key, {trigger: [Snippet]}, max trigger len). Rebuilt when a hotswap /
# live edit changes the dict, so new shortcuts work on the next keystroke.
_SNIP_FLAT = (None, {}, 0)


def _snippet_triggers():
    """{trigger: [Snippet]} from Toggles.TextEditor.AC_SNIPPETS. Keys may be
    one trigger string or a tuple of alias triggers; values a Snippet or a
    list of them."""
    global _SNIP_FLAT
    from src.lsd.gl_gui.toggles import Toggles
    m = Toggles.TextEditor.AC_SNIPPETS or {}
    key = (id(m), len(m))
    if _SNIP_FLAT[0] != key:
        import dataclasses
        flat = {}
        for k, v in m.items():
            snips = list(v) if isinstance(v, (list, tuple)) else [v]
            keys = (k,) if isinstance(k, str) else tuple(str(t) for t in k)
            # Empty labels fall back to the key's word-like trigger (the
            # natural row name for ("white", "(")), else the insert text -
            # labels are the row identity (popup items / accept lookup), so
            # entries sharing a label must not collapse onto ''.
            word = next((t for t in keys if t.replace("_", "").isalnum()), None)
            snips = [dataclasses.replace(s, label=(word or s.insert))
                     if not s.label else s for s in snips]
            for trig in keys:
                flat.setdefault(trig, []).extend(snips)
        _SNIP_FLAT = (key, flat, max((len(t) for t in flat), default=0))
    return _SNIP_FLAT[1]

def _snippet_context(ds, text, cursor, changed):
    """The armed snippet-trigger site at `cursor`, or None. Arms on the edit
    that leaves a trigger ending exactly at the caret; stays armed — the
    chars typed after the trigger become the popup's filter prefix — until
    the caret leaves the site, the trigger text changes under it, or the
    prefix stops looking like a filter (newline / >40 chars). Deliberately
    NOT gated on comments/strings: comment templates ('#[') are the point.
    Returns (trigger_start, filter_prefix, [Snippet])."""
    trigs = _snippet_triggers()
    if not trigs:
        ds._ac_snip_site = None
        return None
    site = getattr(ds, "_ac_snip_site", None)
    if changed:
        head = text[max(0, cursor - _SNIP_FLAT[2]):cursor]
        for t in trigs:
            if not head.endswith(t):
                continue
            start = cursor - len(t)
            # Word boundary: an identifier-char trigger ('t') must not arm
            # mid-word - typing 'tint' would otherwise treat its last 't' as
            # a fresh trigger and accept would splice inside the word
            # ('tintint=('). Symbol triggers ('#[') arm anywhere.
            if (t and (t[0].isalnum() or t[0] == "_") and start > 0
                    and (text[start - 1].isalnum() or text[start - 1] == "_")):
                continue
            site = (start, t)
            break
    if site is None:
        return None
    start, t = site
    end = start + len(t)
    if (start < 0 or text[start:end] != t or cursor < end
            or cursor - end > 40):
        ds._ac_snip_site = None
        return None
    pfx = text[end:cursor]
    if "\n" in pfx:
        ds._ac_snip_site = None
        return None
    ds._ac_snip_site = site
    return start, pfx, trigs[t]

def _punct_run(s):
    """True when `s` is nothing but punctuation — the only characters the
    snippet overtype dedupe may drop (brackets/closers), never content."""
    return bool(s) and all(not c.isalnum() and not c.isspace() and c != "_"
                           for c in s)


_BRACKET_OF = {")": "(", "]": "[", "}": "{"}


def _unmatched_openers(s):
    """{opener: count} of unmatched (, [, { in `s` — the overtype credits a
    replaced span / head trim contributes (see _ac_pick_insert)."""
    counts = {"(": 0, "[": 0, "{": 0}
    for c in s:
        if c in counts:
            counts[c] += 1
        else:
            o = _BRACKET_OF.get(c)
            if o and counts[o] > 0:
                counts[o] -= 1
    return counts


def _ac_pick_insert(ds, pick, following="", preceding="", replaced="",
                    line_prefix=None):
    """(text_to_insert, caret_offset) for an accepted popup pick: a snippet's
    template (with $0 stripped, caret at its position) when `pick` is one of
    the current popup's snippet rows, else the identifier itself.

    `preceding`/`following` are the buffer text just around the replaced
    span. Overtype dedupe for chaining snippets inside existing structure:
    drop the longest PUNCTUATION prefix of the template already sitting
    before the span ('(1.0…' typed inside 'tint=(' must not double the
    paren) and the longest punctuation tail the following text already
    starts with (')]' closers). With a $0 the tail scan stays behind the
    caret; without one the whole template end is eligible and the caret
    lands before the pre-existing closers."""
    snips = getattr(ds, "_ac_snips", None)
    sn = snips.get(pick) if snips else None
    if sn is None:
        return pick, len(pick), []
    ins = sn.insert
    # Multi-line snippets auto-indent like paste does (_reindent_paste):
    # first line over the trigger site, later lines re-indented up to it.
    # `line_prefix` is the text before the anchor on its line - pure
    # whitespace anchors there (the paste rule); a mid-line site anchors the
    # continuation lines to the anchor COLUMN instead. Runs BEFORE the $0
    # extraction so the caret lands correctly in the re-indented text.
    if "\n" in ins and line_prefix is not None:
        target = (line_prefix if not line_prefix.strip()
                  else " " * len(line_prefix))
        ins = _reindent_paste(ins, target)
    # Tab stops: $0...$9 mark caret positions, visited in NUMERIC order (the
    # first is where accept leaves the caret; Tab hops between the rest -
    # see the tab_stop handler in the key logic). All markers are stripped;
    # offsets are into the stripped text.
    _marks = [(int(m.group(1)), m.start()) for m in re.finditer(r"\$(\d)", ins)]
    had_caret = bool(_marks)
    if had_caret:
        _off = {}
        for idx, (n, p) in enumerate(sorted(_marks, key=lambda t: t[1])):
            _off.setdefault(n, p - 2 * idx)
        ins = re.sub(r"\$(\d)", "", ins)
        stops = [_off[n] for n in sorted(_off)]
    else:
        stops = [len(ins)]
    # Bracket credits: a template CLOSER may only be deduped against the
    # following text when its opener is double-provided - the following
    # closer must be genuinely free, not the closer of an existing bracket
    # (accepting 'tint=(...)' inside `f(tint|)` must not drop ')', or f( ends
    # up unclosed). Credits come from unmatched openers in the REPLACED span
    # (the '# [' trigger being re-inserted frees its old ']') and from
    # head-trimmed openers (the '(' we skipped re-insertion frees one ')').
    credits = _unmatched_openers(replaced)
    if preceding:
        for k in range(min(len(ins), len(preceding)), 0, -1):
            head = ins[:k]
            if _punct_run(head) and preceding.endswith(head):
                for c in head:
                    if c in credits:
                        credits[c] += 1
                ins = ins[k:]
                stops = [max(0, s - k) for s in stops]
                break
    tail = ins[max(stops):] if had_caret else ins
    if tail and following:
        for k in range(len(tail), 0, -1):
            t = tail[-k:]
            if not (_punct_run(t) and following.startswith(t)):
                continue
            need = {}
            for c in t:
                o = _BRACKET_OF.get(c)
                if o:
                    need[o] = need.get(o, 0) + 1
            if all(credits.get(o, 0) >= n for o, n in need.items()):
                ins = ins[:len(ins) - k]
                break
    stops = [min(s, len(ins)) for s in stops]
    return ins, stops[0], stops[1:]

# Internal completion `kind` → short display tag shown dim on the right of each
# row. "name" (a bare buffer identifier we couldn't classify) maps to "" so no
# tag is drawn for it.
_KIND_TAGS = {"param": "param", "local": "local", "var": "var", "func": "fn",
              "class": "class", "member": "attr", "module": "mod",
              "import": "import", "symbol": "sym", "instance": "var",
              "kw": "kw", "path": "path", "name": "",
              "method": "fn", "builtin": "", "auto_import": "+ import"}


def _kind_tag(kind):
    return _KIND_TAGS.get(kind, kind)


def _filter_completions(pool, prefix, users=None, tints=None):
    """Filter the ordered (name, kind) `pool` by `prefix`, returning the matching
    (name, kind) rows. Prefix matches (case-insensitive) come before looser
    substring matches. Within each group rows rank: TINTED symbols first (the
    definition-tint names — the popup's colored rows), then by `users` count
    (the buffer's usage-graph site totals) descending, then alphabetically —
    except untinted count-0 rows, which keep the pool's own scope ranking
    (locals before builtins) via the stable sort. Empty prefix (right after a
    `.`) keeps one group. The exact word already fully typed ranks first when
    it's a REAL symbol (a classified kind) — visible as confirmation rather
    than vanishing. An unclassified "name" exact row is just the half-typed
    token echoed back by the buffer scan, not a valid pick — dropped, so the
    top row stays a completion that actually does something."""
    exact = [(n, k) for (n, k) in pool if n == prefix and k != "name"]
    rows = [(n, k) for (n, k) in pool if n != prefix]
    if not prefix:
        # Empty prefix only happens right after a '.', where a pile of dunders is
        # noise - hide them (typing a leading '_' brings them back via the else).
        groups = [[(n, k) for n, k in rows if not n.startswith("_")]]
    else:
        p = prefix.lower()
        groups = [[(n, k) for n, k in rows if n.lower().startswith(p)],
                  [(n, k) for n, k in rows if p in n.lower() and not n.lower().startswith(p)]]
    if users or tints:
        def _key(row):
            n = row[0]
            tinted = 0 if (tints and n in tints) else 1
            c = users.get(n, 0) if users else 0
            return (tinted, -c, n.lower() if (c or not tinted) else "")
        for g in groups:
            g.sort(key=_key)
    ranked = exact + [r for g in groups for r in g]
    return ranked[:_AC_MAX_ROWS]


# Name-DEFINING keywords: an identifier typed right after one is a NEW name
# (`def foo`, `for x`, `open() as f`)); nothing can complete a name being
# invented, so the auto-popup stays quiet there. NOT in the set: with/del/
# except/global/nonlocal - those are followed by names that already exist.
# import/from lines are handled separately (they route to jedi, which
# completes module paths natively).
_DEF_SITE_KEYWORDS = {"def", "class", "for", "as", "lambda"}


def _defining_keyword_before(text, anchor):
    """True when the word immediately left of `anchor` (skipping spaces/tabs) is
    a name-DEFINING keyword — the caret is naming something new."""
    i = anchor
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    j = i
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] == "_"):
        j -= 1
    return text[j:i] in _DEF_SITE_KEYWORDS


_IMPORT_LINE_RE = re.compile(r'\s*(from|import)\s')


def _import_line_context(text, anchor): 
    """True when `anchor` sits on an import-statement line. Bare identifiers
    there are module names / import targets — names the static scope pool can't
    know — so the caller routes them to jedi (which completes import statements
    natively, full-file mode). Parenthesized multi-line import bodies aren't
    detected (their lines don't start with from/import); those fall back to the
    scope pool, which is merely unhelpful, not wrong."""
    ls = _get_line_start(text, anchor)
    return _IMPORT_LINE_RE.match(text, ls, anchor) is not None

def _ac_lex_state(ds, text):
    """The editor's incremental (line_offsets, line_open) lexer state for `text`,
    refreshed through the same ds cache the viewport tokenizer uses — on a typed
    frame the completion gate pays only the incremental re-lex of the edited
    lines, and the render's `_window()` call afterwards gets a cache hit."""
    if getattr(ds, '_lo_text', None) != text:
        ds._lo_offs, ds._lo_open = _update_line_open(
            getattr(ds, '_lo_text', None), getattr(ds, '_lo_offs', None),
            getattr(ds, '_lo_open', None), text)
        ds._lo_text = text
    return ds._lo_offs, ds._lo_open


def _line_lex_at(text, idx, offs, line_open):
    """Line-local lex state at `idx`: (in_str, hash_idx) — the still-open
    string opener (or None) and the index of the '#' that opened a comment
    covering `idx` (or None; at most one is non-None). Resumes from the
    per-line string state (`line_open`, same source as the tokenizer) and
    scans only [line_start, idx). An f-string counts as string even inside
    its {braces} — a rare miss for the callers, never a false positive."""
    li = bisect.bisect_right(offs, idx) - 1
    i = offs[li] if 0 <= li < len(offs) else 0
    opener = line_open[li] if 0 <= li < len(line_open) else None
    in_str = opener[0] if opener else None    # the quote that would close it
    while i < idx:
        c = text[i]
        if in_str is not None:
            if len(in_str) == 3:
                if text.startswith(in_str, i):
                    in_str = None
                    i += 3
                    continue
                i += 1
            else:
                if c == "\\":
                    i += 2
                    continue
                if c == in_str or c == "\n":
                    in_str = None   # newline ends an unterminated single-quote
                i += 1
            continue
        if c == "#":
            return None, i                    # rest of the line is comment
        if c in "\"'":
            if text.startswith(c * 3, i):
                in_str = c * 3
                i += 3
            else:
                in_str = c
                i += 1
            continue
        i += 1
    return in_str, None


def _pos_in_string_or_comment(text, idx, offs, line_open):
    """True when a character typed at index `idx` would land inside a string
    literal or a comment — where the code popup must stay quiet."""
    in_str, hash_idx = _line_lex_at(text, idx, offs, line_open)
    return in_str is not None or hash_idx is not None


def _comment_continuation(text, pos, stop, offs, line_open):
    """(indent, marker) to re-open a comment being split by Enter at `pos`, or
    None when the split isn't mid-comment. `marker` is the comment's own '#'
    run plus its trailing space (`# `, `## `, bare `#`), `indent` the '#'s
    column — so a trailing comment after code re-anchors under its '#' rather
    than the statement's indent. None when the caret still sits inside the
    marker itself (the moved-down tail already starts with '#'). `stop` is the
    current line's end index (exclusive)."""
    h = _line_lex_at(text, pos, offs, line_open)[1]
    if h is None:
        return None
    ce = h
    while ce < stop and text[ce] == '#':
        ce += 1
    if ce < stop and text[ce] == ' ':
        ce += 1
    if pos < ce:
        return None
    return h - _get_line_start(text, pos), text[h:ce]


_STR_PREFIX_CHARS = frozenset('rRbBfFuU')


def _string_split(text, pos, stop, offs, line_open):
    """Enter inside a SINGLE-quoted string literal: (new_text, new_caret) that
    keeps the buffer valid Python, or None when the split isn't mid-string
    (or can't be made safely). The literal is closed at the caret and
    reopened on the next line with the same prefix + quote (`f"…"` stays an
    f-string), aligned under the original opener — implicit concatenation.
    When the literal isn't already inside an open (, [ or {, it is wrapped
    in parentheses (inserted before the prefix and after the closing quote)
    so the continuation line parses. Triple-quoted strings are left to the
    plain newline path (a raw newline is already valid there). Declines
    (None) when the caret follows an odd run of backslashes (splitting
    would orphan an escape) or the literal has no closer on this line
    (already broken — nothing to keep valid)."""
    in_str, _h = _line_lex_at(text, pos, offs, line_open)
    if in_str is None or len(in_str) != 1:
        return None
    ls = _get_line_start(text, pos)
    # Opener: the unescaped `in_str` quote nearest before pos on this line.
    # _line_lex_at already proved pos is inside a string opened on this line
    # (single-quote state never survives a newline), so re-scan forwards from
    # the line start with the same rules to locate it.
    i, q_at, cur = ls, None, None
    while i < pos:
        c = text[i]
        if cur is not None:
            if c == '\\':
                i += 2
                continue
            if c == cur:
                cur = None
            i += 1
            continue
        if c == '#':
            return None
        if c in '\'"':
            if text.startswith(c * 3, i):
                return None          # triple inside the scan - leave it
            cur, q_at = c, i
        i += 1
    if cur is None or q_at is None:
        return None
    # Odd backslash run right before the caret → the split would orphan it.
    bs = 0
    while pos - 1 - bs >= q_at + 1 and text[pos - 1 - bs] == '\\':
        bs += 1
    if bs % 2:
        return None
    # Closer on this line (escape-aware); none → already unterminated.
    j, close_at = pos, None
    while j < stop:
        c = text[j]
        if c == '\\':
            j += 2
            continue
        if c == in_str:
            close_at = j
            break
        j += 1
    if close_at is None:
        return None
    # Any prefix (f / rb / ...) immediately before the opener.
    ps = q_at
    while ps > ls and text[ps - 1] in _STR_PREFIX_CHARS:
        ps -= 1
    if ps > ls and (text[ps - 1].isalnum() or text[ps - 1] == '_'):
        ps = q_at                     # identifier char before it: not a prefix
    prefix = text[ps:q_at]
    wrap = _unclosed_opener(text, ps) is None
    col = ps - ls + (1 if wrap else 0)
    head = text[:ps] + ('(' if wrap else '') + text[ps:pos] + in_str
    cont = '\n' + ' ' * col + prefix + in_str
    tail = text[pos:close_at + 1] + (')' if wrap else '') + text[close_at + 1:]
    return head + cont + tail, len(head) + len(cont)


# First top-level def in the buffer - a function span's own def sits at column
# 0 (spans keep file indentation, so a method in a class span won't match).
_DEF_NAME_RE = re.compile(r'^def\s+(\w+)', re.MULTILINE)


def _ac_live_context(ds, text, address):
    """(module_globals, live_func) for the span being edited: the live module
    the file is loaded as (hotswap-aware — richest of the dual src./non-src
    identities, via code_checks._module_for) and the live function object whose
    scope the buffer edits (its FuncsMetadata carries runtime-observed scope
    types). The function resolves from the buffer's own top-level ``def`` when
    it has one, else from the ADDRESS — the enclosing function a CallerCodec
    span already carries as ``.source``, or the cached _enclosing_function walk
    for any other def-less span (a method body, a class-var line). That address
    fallback is what lets the context menu's caller editors complete
    ``draw_state.`` etc. against the types recorded from the captured stack's
    f_locals (record_stack_scope_types). Cached on the draw_state per (path,
    span-start); a file that isn't imported caches (None, None) and the dot
    path falls through to jedi."""
    if address is None or getattr(address, "path", None) is None:
        return None, None
    key = (str(address.path), getattr(address, "start", None))
    if getattr(ds, '_ac_live_ctx_key', None) == key:
        return ds._ac_live_ctx
    from src.lsd.gl_gui.view.core_conversion.code_checks import _module_for
    ns = func = None
    try:
        mod = _module_for(address.path)
        if mod is not None:
            ns = vars(mod)
            m = _DEF_NAME_RE.search(text)
            if m:
                func = ns.get(m.group(1))
            if func is None:
                import types as _types
                src = getattr(address, "source", None)
                if isinstance(src, _types.FunctionType):
                    func = src
                elif getattr(address, "start", None) is not None:
                    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
                        _enclosing_function)
                    # address.start is a 0-indexed line; the walk expects
                    # 1-based co_firstlineno's.
                    func = _enclosing_function(str(address.path), address.start + 1)
    except Exception:
        pass
    ds._ac_live_ctx = (ns, func)
    ds._ac_live_ctx_key = key
    return ns, func






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


def _ensure_member_completions(ds, text, anchor, address=None):
    """Type-aware member candidates for the dotted receiver ending at `anchor`
    (index just past the '.'), or for the import statement the anchor sits in.
    Resolution ladder, cheapest first:
      1. LIVE — the receiver is getattr-walked from the edited file's live
         module namespace / builtins, or the edited function's runtime-observed
         scope types (FuncsMetadata). Synchronous, exact, chainable: `imgui.`,
         `Melty.cache.`, any module-level name answers instantly, no subprocess.
      2. JEDI — full-file static inference in the background pool (`self.`,
         locals built from project classes, import lines). Submits ONE job per
         receiver context, polls without blocking.
    Returns (members, pending): `members` is an ordered [(name, kind)] once
    ready (else None); `pending` is True while a jedi job is in flight, so the
    caller keeps the body repainting to poll it. The receiver key is anchored at
    the '.', so it's stable while the user types the member stem — one
    resolution, local filtering."""
    line0, col = _index_to_line_col(text, anchor)
    line_start = _get_line_start(text, anchor)
    key = (line0, text[line_start:anchor])   # the receiver expression on this line

    if getattr(ds, '_ac_jedi_done_key', None) == key:
        if ds._ac_jedi_members:
            # Restore the member tint map: other completion paths (bare names,
            # imports, import) null _ac_member_tints every frame they run, so a
            # cached revisit of this dot site must re-stamp it or the popup rows
            # lose their colors intermittently.
            ds._ac_member_tints = getattr(ds, '_ac_jedi_member_tints', None)
            return ds._ac_jedi_members, False
        # Cached EMPTY result: it may predate the receiver existing in the
        # live module (a just-added import resolved to nothing at the time).
        # Drop the latch and fall through - the live-ns walk below is cheap
        # and answers the moment the receiver appears; jedi is NOT resubmitted
        # for an unchanged key (_ac_jedi_req_key still matches), so an
        # unresolvable receiver doesn't loop.
        ds._ac_jedi_done_key = None

    if text[max(anchor - 1, 0):anchor] == ".":
        from src.lsd.gl_gui.func_metadata import member_completions, _receiver_before
        rcv = _receiver_before(text, anchor)
        # A receiver head followed by )/]/quote is a call/index/literal access
        # (`foo().cache.`) - its NAME means nothing in the module namespace, so
        # only jedi (which infers the real type) may answer it.
        head_start = anchor - 1 - len(rcv)
        if rcv and (head_start < 1 or text[head_start - 1] not in ")]\"'"):
            ns, func = _ac_live_context(ds, text, address)
            if ns is not None or func is not None:
                try:
                    rows = member_completions(func, rcv, globals_ns=ns)
                except Exception:
                    rows = []
                if rows:
                    ds._ac_jedi_members = rows
                    ds._ac_jedi_done_key = key
                    ds._ac_jedi_future = None
                    # Tinted members from the receiver's defining file, so
                    # members never referenced in THIS buffer still color
                    # (the buffer usage graph can't know them). Resolved
                    # once per receiver; the scan itself is stat-cached.
                    ds._ac_member_tints = _file_name_tints(
                        _live_receiver_file(ns, rcv))
                    ds._ac_jedi_member_tints = ds._ac_member_tints
                    return rows, False

    if getattr(ds, '_ac_jedi_req_key', None) != key:
        # Receiver changed - request new completions (drops any stale future).
        # The done-callback wakes us once when it lands; no per-frame polling.
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import submit_member_completion
        ds._ac_jedi_future = _wake_on_future(
            submit_member_completion(text, line0, col, address), ds)
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
    # Jedi answered, so the live-ns walk couldn't see the receiver
    # (typically a function-local import). Recover the tint file through the
    # buffer's top() statements; None when that dead-ends too.
    if text[max(anchor - 1, 0):anchor] == ".":
        from src.lsd.gl_gui.func_metadata import _receiver_before
        ds._ac_member_tints = _file_name_tints(
            _receiver_file_via_imports(text, _receiver_before(text, anchor)))
    else:
        ds._ac_member_tints = None   # import-line completion - no file
    ds._ac_jedi_member_tints = ds._ac_member_tints

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


def _ensure_signature_help(ds, text, open_paren, cursor, address=None):
    """Signature of the call whose '(' is at `open_paren`, via jedi (async, same
    pool + full-file/fallback context as completion — with an `address`, src and
    self. callees resolve too). Submits ONE job per callee (keyed at the paren,
    stable while typing args), polls without blocking, and returns
    (name, [param_names]) once ready, else None."""
    callee = _callee_at(text, open_paren)
    if not callee:
        return None
    key = (open_paren, callee)
    if getattr(ds, '_ac_sig_done_key', None) == key:
        return ds._ac_sig_data
    if getattr(ds, '_ac_sig_req_key', None) != key:
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import submit_signature_help
        line0, col = _index_to_line_col(text, cursor)
        ds._ac_sig_future = _wake_on_future(
            submit_signature_help(text, line0, col, address), ds)
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


# Keys whose buffer edits never trigger a FIM generation (fim.py): deleting,
# indenting and breaking a line are structure, not content.
_FIM_NON_TRIGGER_KEYS = frozenset({glfw.KEY_BACKSPACE, glfw.KEY_DELETE, glfw.KEY_TAB,
                                   glfw.KEY_ENTER, glfw.KEY_KP_ENTER})


def _fim_poll(ds, fim_state, text, address, profile, typed=False):
    """Drive the editor's FimState for this frame (fim.py): build the
    EditorView (cheap — whole-file work is lazy) and poll. `typed` = the
    buffer changed this frame (only typing triggers a request). Provider /
    context errors surface ONCE per distinct message as a notification."""
    from src.lsd.gl_gui.toggles import Toggles
    try:
        from src.lsd.gl_gui.fim_context import editor_view
        view = editor_view(text, ds.text_cursor_pos, address)
        ghost = fim_state.poll(text, ds.text_cursor_pos, view=view,
                               profile=profile or "", ds=ds, typed=bool(typed))
    except Exception:
        if Toggles.Fim.debug_print:
            import traceback
            traceback.print_exc()
        return None
    err = fim_state.error
    if err and err != getattr(ds, '_fim_err_shown', None):
        ds._fim_err_shown = err
        try:
            from src.lsd.gl_gui.notifications import notify
            notify(f"FIM: {err}", tint=(1.0, 0.65, 0.4, 1.0), tag="fim")
        except Exception:
            pass
    return ghost


def _draw_fim_ghost(ds, ghost, text, origin_x, origin_y, line_px, vcols=None):
    """Ghost text for the FIM chunk: the first segment inline after the
    caret (dim), any further lines drawn dim over the lines below the caret
    (no box, no layout change — folds/heights untouched), and a faint `+N`
    when more is buffered beyond this chunk. A pending request with no
    complete line yet shows a single dim ellipsis."""
    dl = imgui.get_window_draw_list()
    col = imgui.get_color_u32_rgba(0.66, 0.70, 0.78, 0.55)
    hint = imgui.get_color_u32_rgba(0.66, 0.70, 0.78, 0.32)
    x, y = _char_pos_to_xy(text, ds.text_cursor_pos, origin_x, origin_y, line_px, vcols=vcols)
    if not ghost.text:
        dl.add_text(x + 2, y, hint, "…")
        return
    segs = ghost.text.split("\n")
    first = segs[0]
    if first:
        dl.add_text(x, y, col, first)
    end_x = x + imgui.calc_text_size(first).x if first else x
    end_y = y
    rest = segs[1:]
    if rest and rest[-1] == "":
        rest = rest[:-1]
    if rest:
        # Further segments float over the lines below the caret on the plain
        # line grid - no box, no background - so they read as dim overtype.
        ch = _mono_char_w()
        bx, by = origin_x, y + line_px
        for i, ln in enumerate(rest):
            dl.add_text(bx, by + i * line_px, col, ln)
        end_x = bx + len(rest[-1]) * ch
        end_y = by + (len(rest) - 1) * line_px
    if ghost.more_lines:
        dl.add_text(end_x + 8, end_y, hint, f"+{ghost.more_lines}")


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
# The real default is assigned below the token renderers (it references them,
# and they are defined later in the file) - keep this forward-declaration so
# anything importing the name before then sees a value.
DEFAULT_TOKEN_VIEWS = None

# --- Token views: draw widgets in place of (or above) tokenized code ----------
# `draw_text(..., token_views=...)` maps a token kind to a renderer that draws a
# widget instead of / on top of the code. Two kinds of key, dispatched by type:
#
#   token_views = {
#       "icon":      {"renderer": draw_icon_selector_plain, "char_width": 4},
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
#      ride beside their text instead of replacing it.
#      TRAILING (`"trail_cells": N`): the mirror of ACCESSORY - the token
#      text draws normally in place and the widget occupies the N-cell area
#      right AFTER it; the rest of the line shifts right by N (vcols carries
#      it). E.g. the def run buttons between a def's name and its `(`.
#  • type key → matched (isinstance) against nodes of the routed code_tree
#    (Conditional/Loop/…); positioned by the node's `.span`. With `char_width=None`
#    it's a non-inline OVERLAY drawing callback (floats over/by the code, doesn't
#    edit text): `renderer(x, y, w, h, draw_state=, char_w=, line_px=, node=, span=)`.
# 
# `draw_icon_selector_plain` (below) is the reference inline widget: an editable
# glyph chip that opens a searchable picker popover.

# The full Font Awesome set: {icon-name: glyph} read from the bundled font's cmap
# (see fa_icons.py - generated, do not hand-edit). The dropdown lists the NAMES
# (searchable, e.g. type "arrow") and picks the glyph value. FA_GLYPH_SET gives an
# O(1) "is this a known glyph?" check for the current-value fallback below.
from src.lsd.gl_gui.view.core_views.fa_icons import FA_ICONS, FA_GLYPH_SET
ICON_COLLECTION = FA_ICONS
GENERIC_ICON = "\uf005"  # star - the placeholder Ctrl+I inserts; pick the real one from the dropdown

                  

def draw_icon_selector_plain(input_value, width=20, height=20, name=None,
                             tint=None, text_tint=None, editor_ds=None, **kwargs):
    """Inline Font Awesome icon picker without the @render_func wrapper — the
    glyph is drawn straight into the editor tile as a chip (see
    draw_number_token_plain for why plain: the ~90µs wrapper per widget per
    frame dominates with many inline widgets). Same renderer contract:
    `(glyph) -> (changed, new_glyph)`; a changed return splices the picked
    glyph in for this source char.

    Replaces the old wrapped draw_icon_selector, which routed through
    draw_dropdown: that registered a nested @window trigger PER ICON TOKEN
    (windows outlived their render-order-named call sites — the nested-window
    leak) and ellipsis-trimmed the glyph against the trigger's own text pad
    (the "..." cell). Here the trigger is just a centered add_text.

    The picker is a LATCHED draw_dd_menu popover parented to the EDITOR —
    the same latch pattern as draw_color3_token_plain's color picker: called
    every frame this widget renders with `closed=` toggled; open state lives
    on editor_ds (_icon_open_name) with Melty.popover_focused_ds pointing at
    the menu window, so outside clicks and the nav-key wake ride the standard
    popover machinery. draw_text closes the menu if this widget stops
    rendering while open (scrolled/edited away), so the window can never
    outlive its call site. Caret suppression for presses on the chip comes
    from ds._plain_tv_rects (owns_mouse), like the other plain widgets."""
    from src.lsd.gl_gui.view.core_views.new_core_view import (
        draw_dd_menu, _dd_handle_keys, _dd_close)
    from src.lsd.gl_gui.view.core_conversion.cache_tree import UNSET_VALUE
    cur = input_value if isinstance(input_value, str) else ""
    x, y = imgui.get_cursor_screen_pos()
    w = max(1.0, width)
    h = max(1.0, height)
    _plain_tv_bg(x, y, w, h, tint=tint, bg_offset=0)
    dl = imgui.get_window_draw_list()
    if text_tint is not None:
        color = imgui.get_color_u32_rgba(text_tint[0], text_tint[1], text_tint[2],
                                         text_tint[3] if len(text_tint) > 3 else 1.0)
    else:
        color = COLORS['icon']
    # FA glyphs aren't monospaced - center the glyph's real width in the chip.
    _gw = imgui.calc_text_size(cur)[0] if cur else 0.0
    dl.add_text(x + (w - _gw) * 0.5, y, color, cur)
    io = imgui.get_io()
    hovered = x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h
    if hovered:
        dl.add_rect(x, y, x + w, y + h,
                    imgui.get_color_u32_rgba(1, 1, 1, 0.25), 5.0)
    clicked = hovered and imgui.is_mouse_clicked(0)
    if editor_ds is None:
        return False, cur

    menus = getattr(editor_ds, '_icon_menus', None)
    if menus is None:
        menus = editor_ds._icon_menus = {}
    root = getattr(editor_ds, '_icon_dd_root', None)
    if root is None:
        root = editor_ds._icon_dd_root = DropDownState()
    menu_ds = menus.get(name)
    open_prev = getattr(editor_ds, '_icon_open_name', None) == name
    still_open = menu_ds is not None and Melty.popover_focused_ds is menu_ds
    if clicked:
        want_open = not open_prev
    elif open_prev and not still_open:
        want_open = False   # dismissed externally (scroll away, other popover)
    else:
        want_open = open_prev
    if want_open and any(k == glfw.KEY_ESCAPE for k, _ in Melty.frame_key_events):
        want_open = False
        
    # Always include the current glyph so the menu can display/round-trip it
    # even if it isn't one of the defaults.
    coll = ICON_COLLECTION if (not cur or cur in FA_GLYPH_SET) else {cur: cur, **ICON_COLLECTION}

    if want_open and not open_prev:
        # First open: empty query, let the search box grab text focus for a few
        # frames (the opening click's clear_focus can race the box), and land
        # the highlight on the current glyph's row.
        root.search_query = ""
        root.search = ""
        root._focus_search = 8
        root._kbd_mode = True
        root._had_focus = False
        root._last_mouse = None
        _sel = next((k for k, v in coll.items() if v == cur), None)
        root.cursor_path = (_sel,) if _sel is not None else ()
        root.open_path = ()
        root.selected_path = root.cursor_path
        # Snap the menu's scroll to the highlighted row once the window exists
        # (its draw_state lags the first open by a frame) - countdown, not a
        # per-frame check, so wheel scrolling doesn't take over.
        root._snap_frames = 3
        Melty._popover_open_frame = Melty.frame_count  # grace the opening click
        request_render()

    if want_open:
        # A mouse move switches back to mouse mode so the highlight follows the
        # pointer again (until the next arrow key locks keyboard mode).
        _mp = imgui.get_mouse_pos()
        _lm = getattr(root, "_last_mouse", None)
        if _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5):
            root._kbd_mode = False
        root._last_mouse = (_mp[0], _mp[1])

    # Latched menu window - called every frame this widget renders with
    # `closed=` toggled so it persists when the (cached) editor body is
    # skipped. window_pos is relative to the imgui cursor at call time - set
    # it on the chip's top-left so (0, h) anchors just under the glyph.
    imgui.set_cursor_screen_pos((x, y))
    changed, picked, menu_ds = draw_dd_menu(
        coll, name=f"{name}_icon_menu", closed=not want_open, temp=True,
        swoosh=False, window_pos=(0, h), max_height=500, tint=tint,
        parent_window=editor_ds, disable_scroll=False, text_align="left",
        root_state=root, path_prefix=(), return_extras=True)
    menus[name] = menu_ds

    def _close_pick():
        Melty.popover_focused_ds = None
        _dd_close(root)
        editor_ds._icon_open_name = None
        Melty.cache.invalidate_up(editor_ds._tile_id, max_depth=10, force=True)
        request_render()

    if want_open:
        editor_ds._icon_open_name = name
        editor_ds._icon_seen = (name, Melty.frame_count)
        Melty.popover_focused_ds = menu_ds
        if changed and isinstance(picked, str):
            _close_pick()
            return True, picked

        # Arrows / Enter only while the menu's search box owns the keyboard,
        # so they don't also drive whatever editor was focused before.
        box_tile = getattr(root, "_search_box_tile", None)
        text_focused = (Melty.text_focused_ds is not None and box_tile is not None
                        and getattr(Melty.text_focused_ds, "_tile_id", None) == box_tile)
        _nav_hit = False
        if text_focused:
            _nav_hit = any(k in (glfw.KEY_UP, glfw.KEY_DOWN, glfw.KEY_ENTER,
                                 glfw.KEY_KP_ENTER)
                           for k, _ in Melty.frame_key_events)
            kpick = _dd_handle_keys(coll, root,
                                    search=getattr(root, "search", "") or "",
                                    text_focused=True)
            if kpick is not UNSET_VALUE and isinstance(kpick, str):
                _close_pick()
                return True, kpick

        # Keep the keyboard-cursor row visible: on the open snap (countdown -
        # the menu window's draw lags the first frame) and key nav hits.
        # _dd_handle_keys doesn't scroll by itself (the AC popup calls
        # _dd_scroll_cursor_into_view too); rows sit below the search box, so
        # its measured height is the row-0 offset.
        _snap = getattr(root, "_snap_frames", 0)
        if (_snap > 0 or _nav_hit) and menu_ds is not None:
            from src.lsd.gl_gui.view.core_views.new_core_view import (
                _dd_rows_at, _dd_scroll_cursor_into_view, _dd_as_tuple)
            if _snap > 0:
                root._snap_frames = _snap - 1
            _cp = _dd_as_tuple(root.cursor_path)
            if _cp:
                _rkeys = [r[0] for r in
                          _dd_rows_at(coll, (), getattr(root, "search", "") or "")]
                if _cp[-1] in _rkeys:
                    _box = (Melty.cache.key_to_draw_state.get(box_tile)
                            if box_tile is not None else None)
                    _off = (_box.height + 6) if (_box is not None
                                                 and _box.height) else 30
                    _dd_scroll_cursor_into_view(menu_ds, _rkeys.index(_cp[-1]),
                                                row0_offset=_off)

        # Focus settle (bounded): while the box hasn't confirmed focus and the
        # retry budget lasts, re-run its renderer so request_text() again -
        # same recipe as draw_dropdown's settle block.
        if getattr(root, "_focus_search", 0) > 0:
            if box_tile is not None:
                Melty.cache.invalidate_up(box_tile, force=True)
            Melty.cache.invalidate_up(editor_ds._tile_id, max_depth=10, force=True)
            request_render()
    else:
        if getattr(editor_ds, '_icon_open_name', None) == name:
            editor_ds._icon_open_name = None
            _dd_close(root)
        if menu_ds is not None and Melty.popover_focused_ds is menu_ds:
            Melty.popover_focused_ds = None
        if open_prev or clicked:
            request_render()
    return False, cur

draw_icon_selector_plain._plain_tv = True
                

@render_func(use_cache=True, show_bg=True, shadow=True, with_header=None, z_offset=3, tint=(0.911, 0.305, 0.0),
             show_name=False, selectable=False, bg_offset=0)
def draw_bool_token(input_value, draw_state=None, text_tint=None, **kwargs):
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
    # text_tint (from the editor, inside a tint-carrying override comment):
    # the word wears the comment's color instead of keyword-blue, so
    # widgets inside colored comments stop shouting.
    if text_tint is not None:
        color = imgui.get_color_u32_rgba(text_tint[0], text_tint[1], text_tint[2],
                                         text_tint[3] if len(text_tint) > 3 else 1.0)
    else:
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


@render_func(use_cache=True, show_bg=True, shadow=True, with_header=None, z_offset=2,
             show_name=False, selectable=False, bg_offset=-1, tint=(0.026, 0.041, 0.056), wrap=True)
def draw_number_token(input_value, draw_state=None, text_tint=None,
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
    Drag is the widget's ONLY job — there is no typing mode (imgui's temp
    input is disabled via SLIDER_FLAGS_NO_INPUT). Text editing goes through
    the editor itself: a click on the widget places the editor caret at the
    character under the mouse (the _tv_click path in draw_text), and from
    there the literal edits like any other text.
    left_mouse_* are declared (never read) to win the event latch over the editor —
    a drag that starts on the widget latches here, so the editor doesn't grow a
    text selection while a value is being dragged."""
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
    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
    # Inside a tint-carrying override comment the editor hands us text_tint:
    # digits wear the comment's hue and the frame frame dims toward it too, so
    # number widgets in colored comments stop reading as bright blue sliders.
    if text_tint is not None:
        push_style_color(imgui.COLOR_TEXT, text_tint[0], text_tint[1], text_tint[2])
        push_style_color(imgui.COLOR_FRAME_BACKGROUND,
                         text_tint[0] * 0.22, text_tint[1] * 0.22, text_tint[2] * 0.22)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED,
                         text_tint[0] * 0.32, text_tint[1] * 0.32, text_tint[2] * 0.32)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE,
                         text_tint[0] * 0.42, text_tint[1] * 0.42, text_tint[2] * 0.42)
        _n_colors = 4
    else:
        push_style_color(imgui.COLOR_TEXT, 0.41, 0.59, 0.73)          # number blue
        # Never leave the drag frame on imgui's global theme color (white):
        # same dark editor-look fill the tinted branch uses, from number blue.
        push_style_color(imgui.COLOR_FRAME_BACKGROUND, 0.41 * 0.14, 0.59 * 0.14, 0.73 * 0.14)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED, 0.41 * 0.22, 0.59 * 0.22, 0.73 * 0.22)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE, 0.41 * 0.30, 0.59 * 0.30, 0.73 * 0.30)
        _n_colors = 4
    def _pop_styles():
        pop_style_color(_n_colors)
        pop_style_var()

    imgui.set_next_item_width(draw_state.width)
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
        # Speed follows the literal's decimal places: one pixel of drag moves
        # the last significant digit (0.001 → 0.001/px), scaling up with
        # magnitude for large values. E-notation has no fixed precision, so
        # it stays purely magnitude-based.
        if '.' in s and 'e' not in s.lower():
            prec = min(6, max(1, len(s.split('.', 1)[1])))
            step = 10.0 ** -prec
        else:
            step = max(1e-6, abs(val) * 0.01)
        speed = max(step, abs(val) * 0.005)
        changed, new = imgui.drag_float("##num_tv", val, change_speed=speed,
                                        min_value=0, max_value=0, format=disp,
                                        flags=imgui.SLIDER_FLAGS_NO_INPUT)
    _pop_styles()
    if changed and new != val:
        return True, fmt_back(new)
    return False, s


def _plain_tv_bg(x, y, w, h, tint=None, bg_offset=0, max_bg_value=None,
                 shadow_offset=1.0):
    """Background box for a PLAIN inline token widget (no @render_func): the
    same draw_bg call the wrapper's show_bg path makes (bypass=True skips its
    wrapper), with the call-site tint pushed on the style manager the way
    core_render does — draw_bg colors from the current style tint, so pushing
    it is what tints the box. The previous tint is restored (these run
    mid-draw_text; a leaked tint would recolor the rest of the editor frame).
    No z_offset: plain widgets draw INTO the editor's tile instead of
    compositing above it in a view of their own — their shadow is a
    standalone add_shadow depth mark (same pattern as fast_dock rows: these
    aren't draw_states the compositor can see). clip=True snapshots the
    editor's live clip rect, so partially scrolled widgets clip correctly."""
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_bg
    if shadow_offset is not None:
        add_shadow((x, y, w, h), offset=shadow_offset, corner_radius=5.0)
    sm = Melty.global_attrs['style_manager']
    # Field-only tint swap (push_tint_fields): draw_bg colours from the
    # style manager's current_rgb / hsv, not from imgui's colour tables, so
    # re-applying the 35-entry table twice per widget (~6 µs, half the
    # widget's cost at ~40 widgets per frame) bought nothing.
    prev = None
    if tint is not None and len(tint) >= 3:
        prev = sm.push_tint_fields(*tint[:4])
    draw_bg(bypass=True, left=x, top=y, width=w, height=h,
            rounding=5.0, bg_offset=bg_offset, depth=Melty.shadow_depth,
            opacity=1.0, style_manager=sm, nested_bg=bg_offset >= 0,
            max_bg_value=max_bg_value)
    if prev is not None:
        sm.pop_tint_fields(prev)


def draw_bool_token_plain(input_value, width=20, height=20, name=None,
                          tint=None, text_tint=None, **kwargs):
    """draw_bool_token without the @render_func wrapper — raw imgui drawn
    directly into the editor's tile (the wrapper costs ~90µs/call, which adds
    up with many inline widgets; see draw_bool_token for the interaction
    rationale). Same call shape and (changed, value) return; the call site
    sets the cursor to the cell and passes width/height. No view, no
    draw_state, no event subscription — clicks fall through to the editor
    exactly as before (pass-through IS the bool widget's design; the caret-in
    fallback in draw_text still handles the double-click when the caret hides
    the widget)."""
    word = input_value if input_value in ("True", "False") else "True"
    x, y = imgui.get_cursor_screen_pos()
    w = max(1.0, width)
    h = max(1.0, height)
    _plain_tv_bg(x, y, w, h, tint=tint, bg_offset=0)
    if text_tint is not None:
        color = imgui.get_color_u32_rgba(text_tint[0], text_tint[1], text_tint[2],
                                         text_tint[3] if len(text_tint) > 3 else 1.0)
    else:
        color = COLORS['bool']
    imgui.get_window_draw_list().add_text(x, y, color, word)
    io = imgui.get_io()
    if (x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h
            and imgui.is_mouse_double_clicked(0)):
        return True, ("False" if word == "True" else "True")
    return False, input_value
# Marks a renderer as wrapper-less for draw_text: the call site passes these
# the editor's draw_state (editor_ds) so they can keep the EDITOR tile live
# during a gesture - a plain widget has no tile of its own to invalidate.
draw_bool_token_plain._plain_tv = True


def draw_number_token_plain(input_value, width=20, height=20, name=None,
                            tint=None, text_tint=None, editor_ds=None,
                            max_bg_value=0.15, **kwargs):
    """draw_number_token without the @render_func wrapper — see that docstring
    for the interaction design (drag-only, caret via the editor's _tv_click
    path). What the wrapper used to provide is done inline:
    - Cursor/size come from the call site; drawing goes into the editor tile.
    - push_id(name): no per-view imgui ID scope anymore, and every widget
      shares the "##num_tv" label — without this all drags alias one item.
    - The left_mouse_* subscription that latched drags away from the editor
      is replaced by editor-side suppression: draw_text records this widget's
      rect (ds._plain_tv_rects) and its press/drag handlers skip caret and
      selection for gestures that start inside one. Registering with the
      InputHandler from here doesn't work — registrations are per-RENDERED-
      frame, and with event-driven rendering the editor body is usually
      cached on the frame whose registrations the press resolves against.
      The imgui drag itself needs no melty events; it runs off raw input.
    - While the drag is active the EDITOR tile is force-invalidated each
      frame: the imgui item only exists on frames the editor body runs, so a
      cached editor would freeze the drag after its first value change."""
    from src.lsd.gl_gui.utils.custom_views import (push_style_var, pop_style_var,
                                                   push_style_color, pop_style_color)
    s = input_value if isinstance(input_value, str) else str(input_value)
    kind, val, fmt_back, disp = _parse_number_token(s)
    x, y = imgui.get_cursor_screen_pos()
    if kind is None:
        imgui.get_window_draw_list().add_text(x, y, COLORS['number'], s)
        return False, s
    w = max(1.0, width)
    h = max(1.0, height)
    # Chrome (shadow + chip bg + drag-frame fill) only while the pointer is
    # on the chip or a drag is in flight; at rest the number reads as plain
    # text. Mid-drag the pointer can leave the rect, so the widget that was
    # active on the LAST body run (stashed in editor_ds) keeps its chrome.
    io = imgui.get_io()
    hovered = (x <= io.mouse_pos.x < x + w and y <= io.mouse_pos.y < y + h)
    show_chrome = hovered or (
        editor_ds is not None
        and getattr(editor_ds, '_tv_active_key', None) == name)
    # Hover-edge invalidation (same pattern as _fnrun_hover): the chrome only
    # draws when the cached editor tile repaints, so flip it on the edges.
    if editor_ds is not None:
        _hov_reg = getattr(editor_ds, '_tv_hover', None)
        if _hov_reg is None:
            _hov_reg = editor_ds._tv_hover = {}
        if _hov_reg.get(name) != show_chrome:
            _hov_reg[name] = show_chrome
            editor_ds.invalidate()
            request_render()
    if show_chrome:
        # Legibility guard (same pattern as button's max_bg_brightness): the
        # depth ramp + bleed can push the chip's fill bright in deeply nested
        # views, washing out the light digits - cap the painted value.
        # Tinted-comment chips are part of the comment's surface, not raised
        # above it - no shadow there.
        _plain_tv_bg(x, y, w, h, tint=tint, bg_offset=-1,
                     max_bg_value=max_bg_value,
                     shadow_offset=None if text_tint is not None else 1.0)

    push_style_var(imgui.STYLE_FRAME_PADDING, (0, 0))
    if not show_chrome:
        # Text only; the drag frame paints nothing at rest.
        if text_tint is not None:
            _ta = text_tint[3] if len(text_tint) > 3 else 1.0
            push_style_color(imgui.COLOR_TEXT, text_tint[0], text_tint[1],
                             text_tint[2], _ta)
        else:
            push_style_color(imgui.COLOR_TEXT, 0.41, 0.59, 0.73)  # number blue
        push_style_color(imgui.COLOR_FRAME_BACKGROUND, 0, 0, 0, 0)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED, 0, 0, 0, 0)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE, 0, 0, 0, 0)
        _n_colors = 4
    elif text_tint is not None:
        # The 4th text_tint component is the fade-out (see presentation dim
        # on colored comment widgets) - the drag-frame fill honors it too,
        # or it would paint over the dimmed chip at full opacity.
        _ta = text_tint[3] if len(text_tint) > 3 else 1.0
        push_style_color(imgui.COLOR_TEXT, text_tint[0], text_tint[1], text_tint[2], _ta)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND,
                         text_tint[0] * 0.14, text_tint[1] * 0.14, text_tint[2] * 0.14, _ta)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED,
                         text_tint[0] * 0.22, text_tint[1] * 0.22, text_tint[2] * 0.22, _ta)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE,
                         text_tint[0] * 0.30, text_tint[1] * 0.30, text_tint[2] * 0.30, _ta)
        _n_colors = 4
    else:
        push_style_color(imgui.COLOR_TEXT, 0.41, 0.59, 0.73)          # number chip
        # Never leave the dragged frame on imgui's default theme color (bright):
        # same dark editor-look fill the tinted branch uses, from number blue.
        push_style_color(imgui.COLOR_FRAME_BACKGROUND, 0.41 * 0.14, 0.59 * 0.14, 0.73 * 0.14)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_HOVERED, 0.41 * 0.22, 0.59 * 0.22, 0.73 * 0.22)
        push_style_color(imgui.COLOR_FRAME_BACKGROUND_ACTIVE, 0.41 * 0.30, 0.59 * 0.30, 0.73 * 0.30)
        _n_colors = 4

    imgui.push_id(name or "num_tv")
    imgui.set_next_item_width(w)
    changed, new, active = False, val, False
    try:
        if kind == 'int':
            speed = max(0.2, abs(val) * 0.01)
            changed, new = imgui.drag_int("##num_tv", val, change_speed=speed,
                                          min_value=0, max_value=0,
                                          flags=imgui.SLIDER_FLAGS_NO_INPUT)
        else:
            # Speed rules identical to draw_number_token: one displayed digit
            # per step, scaling with magnitude; e-notation magnitude-only.
            if '.' in s and 'e' not in s.lower():
                prec = min(6, max(1, len(s.split('.', 1)[1])))
                step = 10.0 ** -prec
            else:
                step = max(1e-6, abs(val) * 0.01)
            speed = max(step, abs(val) * 0.005)
            changed, new = imgui.drag_float("##num_tv", val, change_speed=speed,
                                            min_value=0, max_value=0, format=disp,
                                            flags=imgui.SLIDER_FLAGS_NO_INPUT)
        active = imgui.is_item_active()
    except Exception:
        changed = False
    finally:
        imgui.pop_id()
        pop_style_color(_n_colors)
        pop_style_var()

    if active:
        if editor_ds is not None:
            Melty.cache.invalidate_up(editor_ds._tile_id, max_depth=10, force=True)
        request_render()
    # Chrome liveness for the show_chrome check above: track which widget
    # holds the drag so its chrome survives the pointer sliding off the chip.
    if editor_ds is not None:
        if active:
            editor_ds._tv_active_key = name
        elif getattr(editor_ds, '_tv_active_key', None) == name:
            editor_ds._tv_active_key = None
    if changed and new != val:
        return True, fmt_back(new)
    return False, s

draw_number_token_plain._plain_tv = True


def _fmt_color_channel(v):
    """Format a 0..1 channel back into source as a FLOAT literal (the merged
    token needs at least one float channel, and a dragged value is fractional
    anyway; untouched channels keep their original text — ints stay ints)."""
    s = f"{max(0.0, min(1.0, v)):.3f}".rstrip('0')
    return s + '0' if s.endswith('.') else s
       
        
@render_func(use_cache=True, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, z_offset=3, tint=(0.85, 0.45, 0.05))
def draw_color3_token(input_value, draw_state=None,
                      left_mouse_down=False, left_mouse_drag=False, left_mouse_held=False,
                      **kwargs):
    """Inline color swatch for a color tuple — ACCESSORY (lead_cells) renderer
    for 'color3' tokens (`(1.0, 0.5, 0.2)` or RGBA `(1.0, 0.5, 0.2, 0.5)` —
    the fourth channel is alpha — with ints 0/1 allowed as channels; merged by
    tokenize). The editor draws the tuple TEXT itself, normally — fully
    editable, caret/selection like any code — and this widget only gets the
    lead area to its LEFT, where it draws a swatch (split alpha preview when 4
    channels). Clicking the swatch opens the MELTY color-picker popover (same
    pattern as draw_tuple's swatch: popover_focused_ds identity is the open
    state, the picker window is latched — drawn every frame with closed=
    toggled — anchored under the swatch, dismissed by outside click / Esc).
    NEVER imgui's built-in popup: melty windowing has diverged (shadows,
    z-order, cached render tiles) and they don't compose. Edits splice the
    reformatted tuple back — changed channels become float literals, untouched
    channels keep their original text — so the token re-merges.
    Draws nothing if the tuple doesn't parse (the text is still there).
    left_mouse_* declared (never read) for the event latch — see draw_number_token."""
    from src.lsd.gl_gui.view.mode import Mode
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_color_picker
    s = input_value if isinstance(input_value, str) else str(input_value)
    parts = [p.strip() for p in s.strip('()').split(',')]
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return False, s
    if len(vals) not in (3, 4):
        return False, s
    has_alpha = len(vals) == 4
    r, g, b = vals[0], vals[1], vals[2]
    a = vals[3] if has_alpha else 1.0

    # Square-ish swatch inset in the lead area, vertically centered on the line;
    # the extra cell width to its right is the gap before the text.
    _sw = max(6.0, min(draw_state.width - 3, draw_state.height - 4))
    _cx, _cy = imgui.get_cursor_screen_pos()
    imgui.set_cursor_screen_pos((_cx, _cy + (draw_state.height - _sw) * 0.5))
    is_open = Melty.popover_focused_ds is draw_state
    # ALPHA_PREVIEW_HALF splits the swatch - half composited at the real alpha
    # over a checkerboard, half opaque - so RGBA transparency shows in the chip.
    flags = imgui.COLOR_EDIT_NO_TOOLTIP | (imgui.COLOR_EDIT_ALPHA_PREVIEW_HALF if has_alpha else 0)
    if imgui.color_button("##color3_tv", r, g, b, a,
                          flags=flags,
                          width=_sw, height=_sw):
        Melty.popover_focused_ds = None if is_open else draw_state
        if not is_open:
            Melty._popover_open_frame = Melty.frame_count  # grace the opening click
        request_render()
    is_open = Melty.popover_focused_ds is draw_state  # reflect the close this frame

    # Fixed-size popover (closable windows don't auto-resize; the picker body is
    # live imgui the framework can't measure): SV square + N channel rows + hex.
    picker_h = 180 + 14 + len(vals) * 26 + 26
    color_changed, new_color = draw_color_picker(
        tuple(vals), name=f"{draw_state.name}_picker", closed=not is_open,
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
        if color_changed and new_color is not None:
            out = [old_text if new_v == old_v else _fmt_color_channel(new_v)
                   for old_text, old_v, new_v in zip(parts, vals, new_color)]
            return True, "(" + ", ".join(out) + ")"
        if color_changed:
            # The picker's delete affordance returned None - meaningless for a
            # code literal. Just dismiss the popover and leave the text alone.
            Melty.popover_focused_ds = None
            request_render()
    return False, s

def draw_color3_token_plain(input_value, width=20, height=20, name=None,
                            editor_ds=None, **kwargs):
    """draw_color3_token without the @render_func wrapper — raw swatch drawn
    into the editor tile (see draw_number_token_plain for why: the ~90µs
    wrapper per widget per frame dominates with many inline widgets). Same
    ACCESSORY contract and (changed, value) return; interaction design in
    draw_color3_token's docstring. What the wrapper used to provide, inline:
    - The swatch had its own draw_state whose identity WAS the popover open
      state. Now the PICKER window's draw_state (latched via return_extras,
      like the AC popup) fills that role in Melty.popover_focused_ds — it's a
      real windowed view, so the nav-key wake (invalidate_up on the popover
      owner) climbs through parent_window=editor_ds and re-runs this code for
      Esc handling.
    - Toggle state can't be read back from popover_focused_ds alone: the
      swatch has no ds, so it is never in clear_focus's protect closure, and
      the very click meant to CLOSE the popover clears the slot before this
      body runs (reading the slot would then re-open it). editor_ds holds a
      per-widget latch (_c3_open_name) of what we last rendered; a latch-open
      widget whose picker lost the slot was dismissed externally (outside
      click, another popover) and closes.
    - Caret suppression for swatch presses comes from ds._plain_tv_rects —
      the call site registers the lead-area rect (owns_mouse), replacing the
      wrapper's left_mouse_* event latch.
    - While the picker is being dragged the EDITOR tile is force-invalidated
      each frame: the edit round-trips through the source splice, so a cached
      editor would freeze the value after its first change."""
    s = input_value if isinstance(input_value, str) else str(input_value)
    parts = [p.strip() for p in s.strip('()').split(',')]
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return False, s
    if len(vals) not in (3, 4):
        return False, s
    def _splice(new_color):
        return "(" + ", ".join(
            old_text if new_v == old_v else _fmt_color_channel(new_v)
            for old_text, old_v, new_v in zip(parts, vals, new_color)) + ")"
    return _color_swatch_plain(s, vals, _splice, width, height, name, editor_ds)

draw_color3_token_plain._plain_tv = True


def _parse_hex_color(s):
    """`'#rgb'` / `'#rrggbb'` / `'#rrggbbaa'` (quotes included) → list of
    0..1 floats, or None."""
    body = s[2:-1]
    if len(body) == 3:
        body = ''.join(c * 2 for c in body)
    try:
        return [int(body[i:i + 2], 16) / 255.0 for i in range(0, len(body), 2)]
    except ValueError:
        return None


def draw_colorhex_token_plain(input_value, width=20, height=20, name=None,
                              editor_ds=None, **kwargs):
    """Inline color swatch for a hex-color STRING literal (`'#8888c6'`,
    `"#fff"`, RGBA `'#8888c680'`) — the 'colorhex' token kind, split off by
    _split_icons. Same ACCESSORY contract and picker as
    draw_color3_token_plain; an edit writes the color back as lowercase
    `#rrggbb` (`#rrggbbaa` when the literal carried alpha) inside the
    original quotes, so a 3-digit short form expands on its first edit."""
    s = input_value if isinstance(input_value, str) else str(input_value)
    vals = _parse_hex_color(s)
    if vals is None or len(vals) not in (3, 4):
        return False, s
    quote = s[0]

    def _splice(new_color):
        chans = ''.join(f"{round(max(0.0, min(1.0, v)) * 255):02x}"
                        for v in new_color[:len(vals)])
        return f"{quote}#{chans}{quote}"
    return _color_swatch_plain(s, vals, _splice, width, height, name, editor_ds)

draw_colorhex_token_plain._plain_tv = True


def _color_swatch_plain(s, vals, splice, width, height, name, editor_ds):
    """The swatch + latched picker shared by the tuple and hex-string color
    widgets: `vals` are the 3/4 parsed channels, `splice(new_color)` renders
    the edited channels back into source text. Returns (changed, text)."""
    from src.lsd.gl_gui.view.mode import Mode
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_color_picker
    has_alpha = len(vals) == 4
    r, g, b = vals[0], vals[1], vals[2]
    a = vals[3] if has_alpha else 1.0

    # Square-ish swatch inset into the lead area, vertically centered on the line.
    x, y = imgui.get_cursor_screen_pos()
    _sw = max(6.0, min(width - 3, height - 4))
    imgui.set_cursor_screen_pos((x, y + (height - _sw) * 0.5))
    # ALPHA_PREVIEW_HALF splits the swatch - half composited at the real alpha
    # over a checkerboard, half opaque - so RGBA transparency shows in the chip.
    flags = imgui.COLOR_EDIT_NO_TOOLTIP | (imgui.COLOR_EDIT_ALPHA_PREVIEW_HALF
                                           if has_alpha else 0)
    imgui.push_id(name or "c3_tv")
    try:
        clicked = imgui.color_button("##color3_tv", r, g, b, a, flags=flags,
                                     width=_sw, height=_sw)
    except Exception:
        clicked = False
    finally:
        imgui.pop_id()
    if editor_ds is None:
        return False, s

    pickers = getattr(editor_ds, '_c3_pickers', None)
    if pickers is None:
        pickers = editor_ds._c3_pickers = {}
    pick_ds = pickers.get(name)
    open_prev = getattr(editor_ds, '_c3_open_name', None) == name
    still_open = pick_ds is not None and Melty.popover_focused_ds is pick_ds
    if clicked:
        want_open = not open_prev
    elif open_prev and not still_open:
        want_open = False   # dismissed externally (outside click, other popover)
    else:
        want_open = open_prev
    if want_open and any(k == glfw.KEY_ESCAPE for k, _ in Melty.frame_key_events):
        want_open = False

    # Latched picker window - called every frame with closed= toggled so it
    # persists when the (cached) editor body is skipped. Fixed size: closable
    # windows don't auto-resize and the picker body is raw imgui the framework
    # can't measure - SV square + N channel rows + hex.
    picker_h = 180 + 14 + len(vals) * 26 + 26
    # window_pos is relative to the imgui cursor at call time - park the
    # cursor back on the swatch's top-left so (0, 10) anchors just under it,
    # exactly like the wrapped popover.
    imgui.set_cursor_screen_pos((x, y))
    color_changed, new_color, pick_ds = draw_color_picker(
        tuple(vals), name=f"{name}_picker", closed=not want_open,
        window_pos=(0, 10),
        parent_window=editor_ds, width=216, height=picker_h,
        mode=Mode.POPOVER, return_extras=True)
    pickers[name] = pick_ds

    if want_open and color_changed and new_color is None:
        # The picker's delete affordance - meaningless for a color literal;
        # just close the popover and leave the color alone.
        want_open = False
        color_changed = False
    
    if want_open:
        if not open_prev:
            Melty._popover_open_frame = Melty.frame_count  # grace the opening frame
            request_render()
        editor_ds._c3_open_name = name
        Melty.popover_focused_ds = pick_ds
        # Keep re-rendering while a picker slider/square is being dragged so
        # the spliced value flows back into the picker each frame despite the
        # editor's cache.
        if Melty.imgui_any_item_active or imgui.is_mouse_down(0):
            Melty.cache.invalidate_up(editor_ds._tile_id, max_depth=10, force=True)
            request_render()
        if color_changed and new_color is not None:
            return True, splice(new_color)
    else:
        if getattr(editor_ds, '_c3_open_name', None) == name:
            editor_ds._c3_open_name = None
        if pick_ds is not None and Melty.popover_focused_ds is pick_ds:
            Melty.popover_focused_ds = None
        if open_prev or clicked:
            request_render()
    return False, s


def _fnrun_extract_def(file_path, def_line, def_name):
    """(dedented source, 0-based pending start line) of the
    `def <def_name>` block at (pending) file line `def_line`, or None. Reads
    PendingSave.current_file_text — the in-memory truth — so a just-typed
    function runs without a disk write. A small ±line scan tolerates
    pending/disk drift; the block ends at the first non-empty line back at
    (or left of) the def's own indent."""
    from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    text = PendingSave.current_file_text(str(file_path))
    if text is None:
        return None
    return _fnrun_extract_def_text(text, def_line, def_name)


def _fnrun_extract_def_text(text, def_line, def_name):
    """_fnrun_extract_def over an explicit `text` (1-based `def_line` in
    it) — the editor buffer, for callers whose truth is what's displayed.

    `def_line` is a HINT, not an address: callers hand in disk-coordinate
    lines (co_firstlineno, the widget's _usage_off mapping) while `text`
    is usually the PENDING buffer, and unsaved edits above the def shift
    the two apart by any amount; the nearest same-named def wins. Cost is
    O(file) only in C (a compiled regex scan for the def headers + one
    newline count to the chosen one) plus O(def) in Python for the block —
    never a whole-file split: this runs per keystroke."""
    hits = []
    needle = "def " + def_name
    pos = 0
    n = len(text)
    while True:
        off = text.find(needle, pos)
        if off < 0:
            break
        pos = off + 1
        end = off + len(needle)
        if end < n and (text[end].isalnum() or text[end] == '_'):
            continue                        # longer name sharing the prefix
        head = off
        if text.startswith("async ", max(0, off - 6)) and off >= 6:
            head = off - 6
        ls = text.rfind('\n', 0, head) + 1
        if text[ls:head].strip():
            continue                        # not at the start of its line
        hits.append((ls, head - ls))        # (line start offset, indent)
    if not hits:
        return None
    if len(hits) == 1:
        off, indent = hits[0]
    else:
        # Nearest header to the hint line: rank by line distance, which
        # needs each hit's line - newline counts up to each hit are C-speed.
        want = max(0, def_line - 1)
        off, indent = min(hits, key=lambda h: abs(text.count('\n', 0, h[0])
                                                   - want))
    start = text.count('\n', 0, off)
    out = []
    pos = off
    n = len(text)
    first = True
    while pos < n:
        nl = text.find('\n', pos)
        if nl < 0:
            nl = n
        ln = text[pos:nl]
        if not first and ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            break
        out.append(ln[indent:] if len(ln) >= indent else ln)
        first = False
        pos = nl + 1
    return "\n".join(out), start


# (resolved file, def name) -> (src_key, fn): the last pending-def compile,
# reused when only literal parameter DEFAULTS changed - see _fnrun_src_key.
_FNRUN_COMPILE_CACHE = {}


def _fnrun_src_key(src, start0):
    """Reuse key for a pending-def compile: the def's source with every
    LITERAL default expression masked out, plus its pending start line.
    Equal keys ⇒ the previously compiled function is safe to reuse:
      - masked (literal) defaults are exactly the values the run passes as
        explicit kwargs (_fnrun_params_from_node), so the stale compiled
        default never evaluates;
      - a CodeLine/expression default is NOT masked (ast.literal_eval fails)
        — the run omits those so the COMPILED default evaluates, so an edit
        there must recompile;
      - param renames/adds, annotation or body edits change the key text;
      - start0 pins the coordinate-true padding — the instrumented twin
        re-reads source at co_firstlineno, so a def that moved lines needs a
        fresh compile.
    None ⇒ unparseable signature, caller always compiles."""
    import ast
    po = src.find('(')
    if po < 0:
        return None
    n = len(src)
    i = po + 1
    depth = 1
    quote = None
    seg_start = i
    segs = []
    while i < n and depth > 0:
        c = src[i]
        if quote:
            if c == '\\':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in '\'"':
            quote = c
        elif c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
            if depth == 0:
                segs.append(src[seg_start:i])
                break
        elif c == ',' and depth == 1:
            segs.append(src[seg_start:i])
            seg_start = i + 1
        i += 1
    if depth:
        return None
    masked = []
    for seg in segs:
        # First top-level '=' splits name (+annotation) from the default-
        # scan at depth 0 so '=' inside a bracketed annotation never splits.
        d = 0
        q = None
        eq = -1
        for j, c in enumerate(seg):
            if q:
                if c == '\\':
                    continue
                if c == q:
                    q = None
            elif c in '\'"':
                q = c
            elif c in '([{':
                d += 1
            elif c in ')]}':
                d -= 1
            elif c == '=' and d == 0 and seg[j:j + 2] != '==':
                eq = j
                break
        if eq < 0:
            masked.append(seg.strip())
            continue
        head, default = seg[:eq].strip(), seg[eq + 1:].strip()
        try:
            ast.literal_eval(default)
            masked.append(f"{head}=<LIT>")
        except Exception:
            masked.append(f"{head}={default}")
    return (src[:po], tuple(masked), src[i:], start0)


def _fnrun_resolve(file_path, def_line, def_name=None, prefer_pending=False):
    """The live function named `def_name` defined in `file_path` — resolved by
    NAME over the file's live modules (module + class vars), with `def_line`
    only breaking ties between same-named defs (methods of different classes).

    `prefer_pending=True` flips the order: exec-compile the def's LATEST
    pending source FIRST (the same fallback path below), so a just-made edit
    — a params-panel default spliced into the signature moments ago — runs
    immediately instead of waiting out the background reparse/live-apply
    that would eventually refresh the live function. The live-module walk
    becomes the fallback (unparseable pending text, def not found). The
    panel's Run / Run Visualize buttons pass it.

    Deliberately NOT chain_converters._enclosing_function: that resolver keys
    its cache on disk mtime — studio edits are PendingSave-deferred, so a
    recompile_all() hotswap changes the module without touching disk and a
    pre-hotswap miss stayed cached — and it matches by co_firstlineno (DISK
    coords) against our pending-buffer line, so any line drift plus the old
    name guard read as "couldn't resolve". Clicks are rare; a fresh walk per
    click needs no cache.

    Functions not reachable from module vars — NESTED defs, or a brand-new
    def that hasn't been hotswapped yet — fall back to exec'ing the def's
    PENDING source block in a COPY of the module's namespace: module globals
    resolve, the module itself is never polluted, and a closure over enclosing
    locals surfaces as a NameError on the run (quick-testing semantics)."""
    if not file_path or not def_name:
        return None
    import inspect
    import types
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _modules_for_file, _resolved)
    try:
        target = _resolved(str(file_path))
    except (OSError, ValueError):
        return None
    modules = _modules_for_file(target)

    def _exec_pending():
        """Compile + exec the def's block from the PENDING file text (the
        in-memory truth) in a lazy copy of the module namespace. Returns the
        exec'd function, or None when extraction/compile fails."""
        got = _fnrun_extract_def(file_path, def_line, def_name)
        if got is None or not modules:
            return None
        src, start0 = got
        # Reuse the compiled function while the def's source (and its
        # pending start line) is unchanged.
        _ck = (str(target), def_name)
        # Keyed on the FULL source: the compiled defaults are the run's
        # inputs now (no explicit kwargs), so a default-only edit must
        # recompile too - masking literals (_fnrun_src_key) guarantees the
        # change overrode them. A def compile is sub-millisecond.
        _key = (src, start0)
        _hit = _FNRUN_COMPILE_CACHE.get(_ck)
        if _key is not None and _hit is not None and _hit[0] == _key:
            return _hit[1]
        defs = _fnrun_pending_defs(file_path)

        # The file scope, lazily: the namespace starts as a copy of the live
        # module's vars, and any name the run hits that ISN'T there (another
        # just-typed function, a pending class) execs just its own block from
        # the pending source on first use - never the whole file's side
        # effects.
        ns = _FnRunNamespace(dict(vars(modules[0])), file_path, defs)
        # The namespace holds only imports that LIVE when executed - a def typed
        # alongside a new import needs that import exec'd here now.
        _fnrun_ensure_imports(ns, file_path)
        # Pad the (dedented) block back to its DISK start line so the
        # compiled function is COORDINATE-TRUE in the convention everything
        # else speaks: co_firstlineno = DISK coords (the in-session
        # truth - hotswapped module functions keep it, the live_view
        # instrumenter bridges disk → pending itself via _stamp_delta, and
        # the published `line:N#name` store keys are disk-anchored too).
        # Padding to the PENDING line instead (as this did) shifted every
        # key this twin published by the unsaved delta above the def, so a
        # run from here and a run of the live function alternated key
        # sets - each run re-keyed every site, closing (or mis-anchoring)
        # every open live-value store. A block compiled at line 1 is just
        # as wrong the other way (the instrumenter fell back to an
        # un-instrumented run). Two passes: the delta is defined at a disk
        # line, which we only know after travers above it once.
        disk0 = start0
        try:
            from src.lsd.gl_gui.view.core_conversion.live_instrument import (
                _delta_above, _pending_gen)
            if _pending_gen(str(file_path)):
                _d = _delta_above(str(file_path), start0 + 1)
                _d = _delta_above(str(file_path), max(1, start0 - _d + 1))
                disk0 = max(0, start0 - _d)
        except Exception:
            disk0 = start0
        try:
            exec(compile("\n" * disk0 + src, str(file_path), 'exec'), ns)
        except Exception:
            return None
        fn = ns.get(def_name)
        if not callable(fn):
            return None
        # _build_twin copies fn.__globals__ into a PLAIN dict (losing the
        # lazy __missing__), so the twin's own body must find its file-scope
        # names already bound: touch every direct reference that lives in the
        # pending top level. Transitive callees see nothing - a memoized
        # helper keeps THIS namespace as its __globals__, leaving resolution
        # intact.
        _fnrun_materialize_refs(ns, src)
        fn.__fnrun_exec__ = True
        if def_name in defs:
            # TOP-LEVEL defs: park the exec'd fn on the module under a
            # private key so live_view's overlay resolver
            # (_enclosing_function's module-vars walk - it reads VALUES, keys
            # don't matter) can find the same object the run publishes to;
            # the coordinate-true firstlineno lets it win nearest-def for
            # exactly its own body lines. Re-parked (same key) each resolve,
            # and the resolver's mtime-keyed cache is purged so the overlay
            # sees the NEW object - pending edits never bump the mtime.
            # Nested defs are ephemeral: a module-level entry whose
            # firstlineno sits INSIDE the outer function would steal the
            # outer def's own marker resolution.
            from src.lsd.gl_gui.view.core_conversion import (
                chain_converters as _cc)
            from src.lsd.gl_gui.view.core_conversion.live_view import (
                adopt_live_store)
            # The previously parked twin (and the real module function, if
            # it ever ran with a store) hand their live store to this one:
            # every body edit compiles a NEW function object here, and a
            # superseding def owns its own __live_values__ - whole run
            # generations of stacked tensors pinned by literally nothing
            # referenced any more (the live-lab VRAM climb while typing).
            _prev = modules[0].__dict__.get(f"_fnrun_live_{def_name}")
            adopt_live_store(_prev, fn)
            _live_fn = modules[0].__dict__.get(def_name)
            if isinstance(_live_fn, types.FunctionType):
                adopt_live_store(inspect.unwrap(_live_fn), fn)
            modules[0].__dict__[f"_fnrun_live_{def_name}"] = fn
            for k in [k for k in _cc._ENCLOSING_FN_CACHE
                      if k[0] == str(target)]:
                del _cc._ENCLOSING_FN_CACHE[k]
        if _key is not None:
            _FNRUN_COMPILE_CACHE[_ck] = (_key, fn)
        return fn

    if prefer_pending:
        fn = _exec_pending()
        if fn is not None:
            return fn

    cands = []

    def consider(fn):
        inner = inspect.unwrap(fn)
        code = getattr(inner, "__code__", None)
        if code is None or getattr(inner, "__name__", None) != def_name:
            return
        if getattr(inner, "__fnrun_exec__", False):
            # A previously parked exec-fallback twin (see below) - never a
            # resolution target: skipping it makes every click re-exec the
            # LATEST pending source instead of replaying the parked code.
            return
        try:
            same = _resolved(code.co_filename) == target
        except (OSError, ValueError):
            same = code.co_filename == str(target)
        if same and inner not in cands:
            cands.append(inner)

    def walk(scope):
        for val in list(vars(scope).values()):
            if isinstance(val, types.FunctionType):
                consider(val)
            elif isinstance(val, (staticmethod, classmethod)):
                f = getattr(val, "__func__", None)
                if isinstance(f, types.FunctionType):
                    consider(f)
            elif isinstance(val, type):
                walk(val)

    for module in modules:
        walk(module)
    if cands:
        # A single name match runs regardless of line; several (same-named
        # methods) pick the nearest co_firstlineno - stable under
        # pending/disk drift, but enough to separate distinct classes.
        fn = min(cands, key=lambda f: abs(f.__code__.co_firstlineno
                                          - (def_line or 0)))
        # A just-added top-of-file import may not be executed yet (span
        # recompiles don't re-exec the module head) - heal the function's
        # namespace from the pending source so the run finds it. Missing
        # imports win; live state is never clobbered.
        _fnrun_ensure_imports(fn.__globals__, file_path)
        # ONE owner per def: a parked exec twin from earlier runs (before
        # this def went live) sits in module vars at the SAME firstlineno -
        # the overlay's _enclosing_function can tie-break to it (or serve it
        # from its mtime-keyed cache, which pending edits never bump) while
        # the run publishes to the live fn and markers read the store, and
        # fill another gap "first run worked, later ones didn't". Evict the
        # twin and purge the resolver cache so both sides converge on the
        # live function.
        from src.lsd.gl_gui.view.core_conversion import (
            chain_converters as _cc)
        _evicted = False
        for module in modules:
            _twin = module.__dict__.pop(f"_fnrun_live_{def_name}", None)
            if _twin is not None:
                _evicted = True
                # The evicted twin's store moves to the live function it
                # converges on - never orphaned with its state.
                from src.lsd.gl_gui.view.core_conversion.live_view import (
                    adopt_live_store)
                adopt_live_store(_twin, fn)
        if _evicted or any(k[0] == str(target)
                           for k in _cc._ENCLOSING_FN_CACHE):
            for k in [k for k in _cc._ENCLOSING_FN_CACHE
                      if k[0] == str(target)]:
                del _cc._ENCLOSING_FN_CACHE[k]
        return fn

    return _exec_pending()


def _fnrun_materialize_refs(ns, src):
    """Bind, in `ns`, every pending top-level def/class the given source
    DIRECTLY references (ast Name loads) — via the namespace's own lazy
    __missing__. Needed only because consumers snapshot the namespace into a
    plain dict (live_instrument's twin globals)."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    for n in ast.walk(tree):
        if (isinstance(n, ast.Name) and n.id in ns._fnrun_defs
                and n.id not in ns):
            try:
                ns[n.id]
            except Exception:
                pass


def _fnrun_pending_defs(file_path):
    """{name: top-level FunctionDef/AsyncFunctionDef/ClassDef ast node} of the
    file's PENDING source — the lookup table _FnRunNamespace resolves missing
    names from. Later definitions of a name win, matching file execution."""
    import ast
    from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    text = PendingSave.current_file_text(str(file_path))
    if not text:
        return {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    return {node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef))}


class _FnRunNamespace(dict):
    """Globals for the exec-fallback run: a copy of the live module's vars
    whose MISSING names resolve lazily from the file's PENDING source. CPython
    routes LOAD_GLOBAL through __missing__ for dict-subclass globals, so the
    running function lands here exactly where it would have raised NameError;
    the first use of another top-level def/class execs just that block — in
    THIS same namespace, so its own callees and decorators chain the same way
    — giving a pending function the full file scope without executing the
    whole file's module-level side effects. Resolved names cache by the exec's
    own binding; anything not in the file's top level raises KeyError →
    the normal NameError on the run."""

    def __init__(self, base, file_path, defs):
        super().__init__(base)
        self._fnrun_file = str(file_path)
        self._fnrun_defs = defs
        self._fnrun_loading = set()

    def __missing__(self, key):
        import ast
        node = self._fnrun_defs.get(key)
        if node is None or key in self._fnrun_loading:
            raise KeyError(key)
        self._fnrun_loading.add(key)
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         self._fnrun_file, 'exec'), self)
        finally:
            self._fnrun_loading.discard(key)
        if key not in self:
            raise KeyError(key)
        return self[key]


def _fnrun_ensure_imports(ns, file_path):
    """Execute the file's TOP-LEVEL import statements — read from the PENDING
    source, the same truth the def extraction uses — into `ns`, so a run sees
    every import the file declares even when the live module never executed
    it (a just-typed import before recompile, or a span recompile that
    doesn't re-exec the module head). Non-destructive: a statement whose
    bound names are all already present is skipped, so nothing live is
    rebound; each statement execs individually and best-effort (one broken
    import never blocks the run). Relative imports resolve via the
    __package__/__name__ already in `ns` (it is, or copies, a real module
    namespace)."""
    import ast
    from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    text = PendingSave.current_file_text(str(file_path))
    if not text:
        return
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        names = [a.asname or a.name.split('.')[0] for a in node.names]
        if '*' not in names and all(n in ns for n in names):
            continue
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         str(file_path), 'exec'), ns)
        except Exception:
            pass


def _fnrun_find_def_node(tree, def_name, line):
    """FunctionParse node named `def_name` nearest 1-indexed parse `line` in
    the routed code tree, or None. Name-first, line as the tie-break between
    same-named methods — the same policy as _fnrun_resolve."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
        FunctionParse)
    best = None
    stack = [tree]
    seen = set()
    while stack:
        node = stack.pop()
        if (not isinstance(node, dict) or id(node) in seen
                or len(seen) > 20000):
            continue
        seen.add(id(node))
        if isinstance(node, FunctionParse):
            nm = getattr(node, 'def_name', None)        # both parsers stamp this
            if nm is None:
                cst_n = node.get('__cst__')
                nm = getattr(getattr(cst_n, 'name', None), 'value', None)
            if nm == def_name:
                sp = getattr(node, 'span', None)
                d = abs(sp.start_line - line) if sp is not None else 1 << 20
                if best is None or d < best[0]:
                    best = (d, node)
        stack.extend(node.values())
    return best[1] if best else None


def _fnrun_sig_default_span(text, def_line0, pname):
    """(start, end) indices of parameter `pname`'s default EXPRESSION inside
    the signature of the def starting on 0-based line `def_line0` of `text`,
    or None. A tiny paren/string scanner over just the signature — multi-line
    signatures, annotations and nested defaults (tuples, calls) all work; the
    span excludes surrounding whitespace, so splicing a new expression there
    is the partial code insertion for a params-panel edit."""
    off = 0
    for _ in range(def_line0):
        nl = text.find('\n', off)
        if nl < 0:
            return None
        off = nl + 1
    po = text.find('(', off)
    if po < 0:
        return None
    n = len(text)
    i = po + 1
    depth = 1
    quote = None
    seg_start = i
    segs = []
    while i < n and depth > 0:
        c = text[i]
        if quote:
            if c == '\\':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in '\'"':
            quote = c
        elif c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
            if depth == 0:
                segs.append((seg_start, i))
                break
        elif c == ',' and depth == 1:
            segs.append((seg_start, i))
            seg_start = i + 1
        i += 1
    for s, e in segs:
        m = re.match(r'\s*\*{0,2}\s*([A-Za-z_]\w*)', text[s:e])
        if not m or m.group(1) != pname:
            continue
        j = s + m.end()
        d2 = 0
        q2 = None
        while j < e:
            c = text[j]
            if q2:
                if c == '\\':
                    j += 2
                    continue
                if c == q2:
                    q2 = None
            elif c in '\'"':
                q2 = c
            elif c in '([{':
                d2 += 1
            elif c in ')]}':
                d2 -= 1
            elif c == '=' and d2 == 0 and text[j + 1:j + 2] != '=':
                v0 = j + 1
                while v0 < e and text[v0] in ' \t\n':
                    v0 += 1
                v1 = e
                while v1 > v0 and text[v1 - 1] in ' \t\n':
                    v1 -= 1
                return (v0, v1)
            j += 1
        return None
    return None


def _fnrun_param_src(value):
    """Source text for a parameter default value — CodeLine passes through
    (it IS source); everything else goes through the central reverse
    converter so formatting rules stay in one place."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
        CodeLine, _python_to_cst_expr, _cst_node_to_code)
    if isinstance(value, CodeLine):
        return str(value)
    try:
        return _cst_node_to_code(_python_to_cst_expr(value)).strip()
    except Exception:
        return repr(value)


def _fnrun_params_from_node(def_node):
    """Explicit run kwargs straight from the cst dict's `parameters` — no
    inspect, and edits made in the params panel take effect immediately
    (the dict IS what the panel edits). Plain Python values pass; NO_DEFAULT
    fills from Melty.global_attrs when available; CodeLine defaults
    (unreducible expressions) are OMITTED so the compiled default evaluates.
    Returns None when the node has no parameters dict (caller falls back to
    the signature-derived path)."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
        CodeLine, NoDefault)
    if not isinstance(def_node, dict):
        return None
    params_node = def_node.get('parameters')
    if not isinstance(params_node, dict):
        return None
    out = {}
    for k, v in params_node.items():
        if not isinstance(k, str) or k.startswith('__'):
            continue
        if isinstance(v, NoDefault):
            if k in Melty.global_attrs:
                out[k] = Melty.global_attrs[k]
        elif isinstance(v, CodeLine):
            continue
        else:
            out[k] = v
    return out


def _fnrun_run(fn, instrumented=False, params=None):
    """Run `fn` the way draw_function's Run button does. kwargs come from
    `params` when given (built from the cst dict's parameters by
    _fnrun_params_from_node — the panel-edited truth); otherwise from the
    signature's defaults, with Melty.global_attrs filling params that have
    none. Blocking, on the render thread (draw_function's default), and the
    same error reporting — colored traceback to the console, the formatted
    message back to the caller. Returns (ok, error_text).

    `instrumented=True` routes the call through live_instrument's
    run_instrumented — the live_view_forward twin path: same return value and
    exceptions, but every assignment publishes a snapshot to `fn`'s live_view
    store, so the editor's snapshot overlay anchors the captured values right
    on the code. Sources the twin can't transform (closures/generators/the
    exec'd nested-def fallback) run un-instrumented, by run_instrumented's
    own fallback."""
    import inspect
    import sys
    if params is None:
        params = {}
        try:
            for pname, param in inspect.signature(fn).parameters.items():
                if pname == 'kwargs':
                    continue
                if param.default is not inspect.Parameter.empty:
                    params[pname] = param.default
                elif pname in Melty.global_attrs:
                    params[pname] = Melty.global_attrs[pname]
        except (TypeError, ValueError):
            params = {}
    try:
        if instrumented:
            from src.lsd.gl_gui.view.core_conversion.live_instrument import (
                run_instrumented)
            run_instrumented(fn, **params)
        else:
            fn(**params)
        return True, None
    except Exception as e:
        from src.lsd.gl_gui.view.core_views.new_core_view import (
            _format_run_error, _respond_to_cuda_oom)
        from src.lsd.gl_gui.utils.custom_views import print_colored_traceback
        print(f"Error calling function '{fn.__name__}': {e}")
        print_colored_traceback(*sys.exc_info())
        _respond_to_cuda_oom(e, fn.__name__)
        return False, _format_run_error(e)


def _fnrun_def_node_for(editor_ds, skey, code_root, def_name, buf_line,
                        tv_text):
    """This def's FunctionParse node, resolved LAZILY (clicks / open panel
    only — never per frame: the code-host trees are Bubbling proxies with
    unstable identity, so a tree-keyed memo misses every access and the walk
    would run per def token per frame). Memoized per widget against the
    BUFFER TEXT identity — an edit swaps the string, and the reparse that
    follows is what changes the tree."""
    if code_root is None or not def_name:
        return None
    cache = getattr(editor_ds, '_fnrun_node_cache', None)
    if cache is None:
        cache = editor_ds._fnrun_node_cache = {}
    got = cache.get(skey)
    if got is not None and got[0] is tv_text:
        return got[1]
    node = _fnrun_find_def_node(code_root, def_name, buf_line or 0)
    cache[skey] = (tv_text, node)
    return node


def _fnrun_detach(node):
    """A shallow copy of a code-host tree node that DOESN'T bubble: same
    class (so it renders and reads exactly like the node — `.span`, the
    `__cst__` keys, the values), but with no `_bubble_root`, so edits made
    to it in the params panel never dirty the code host. A bubbled edit
    makes the host regenerate the editor's source from the TREE, which
    lags the text by the reparse debounce, and that replaced whatever the
    user had typed since. The panel edits the copy; the splice carries
    the edit into the text; the reparse refreshes the real node."""
    # NOT copy.copy: it rebuilds a dict subclass through its own
    # __setitem__ - the bubbling one - and the copied __dict__ still
    # names the original root, so the panel panel dirtied the host once
    # per param. Raw dict update + a __dict__ copy with the root cleared.
    cls = type(node)
    c = cls.__new__(cls)
    dict.update(c, node)
    try:
        c.__dict__.update(getattr(node, "__dict__", {}))
        c._bubble_root = None
    except Exception:
        pass
    return c


def _fnrun_after_live_run(editor_ds):
    """Post-instrumented-run delivery, shared by the inline eye and the
    params panel's eye: force-invalidate every open value window's subtree
    (the publishes went to a store object the markers hadn't registered
    watchers on yet, so live_view's per-publish cascade never fired) and
    latch a few FULL overlay passes so below-the-fold markers render once
    (at a frozen anchor) and forward the fresh values into their windows."""
    editor_ds._lv_full_overlay_until = Melty.frame_count + 3
    for _mds in (getattr(editor_ds, '_lv_marker_ds', None) or {}).values():
        _w = getattr(_mds, '_lv_window_ds', None)
        if _w is not None and not _w.closed:
            Melty.cache.invalidate_up(_w._tile_id, force=True, max_depth=8)
    # The latch only matters if the editor BODY actually runs while it is
    # live - the full overlay pass is part of the editor render, and until
    # the Auto Execute splice deferred nothing else guaranteed a render (a
    # run on a clean editor could let the latch expire silently - the
    # "types freeze once their code scrolls off screen" report). Drive it.
    editor_ds.invalidate()
    request_render()


def _fnrun_auto_exec_on_edit(editor_ds, editor_state, skey, file_path,
                             def_line, def_name, code_root, def_buf_line,
                             tv_text, def_disp_line, status):
    """Auto Execute's CODE-EDIT channel (the panel's param-edit channel is
    draw_fnrun_params_panel): while the def's Auto Execute is on, an edit to
    the def's own text recompiles and instrument-runs it — once, at the
    trailing edge of a typing burst.

    Per widget run: a fresh editor buffer (tv_text identity — the cheap
    signal, never a content compare) arms a per-def deadline; a one-shot
    timer posts the expiry check to the render thread (_fnrun_auto_exec_fire
    — independent of whether the editor tile repaints). At expiry
    the def's source is extracted from the BUFFER (tv_text at
    def_disp_line — the truth the user sees) and reduced to _fnrun_src_key
    with LITERAL defaults masked and no line pin — so the run fires for
    body/param/annotation edits of THIS def only: edits elsewhere in the
    file, line shifts from above, and the panel's own default splices
    (already run) all compare equal. The compile reads PendingSave, which
    the deferred save channel fills BEHIND the buffer, so the run waits
    (150 ms polls, ≤5 s) until every buffer line of the def appears in
    order in pending's (the buffer is display text with folds spliced out,
    so subsequence, not equality) — never compiles a stale generation. Any run observed this frame (`status` — inline or panel)
    refreshes the baseline without running, and the baseline is first
    taken silently, so toggling Auto Execute on or opening the file never
    runs by itself."""
    if editor_state is None or tv_text is None or not def_name:
        return
    if not editor_state.params_auto_execute.get(def_name):
        editor_ds.__dict__.get('_fnrun_edit_watch', {}).pop(skey, None)
        return
    watch = getattr(editor_ds, '_fnrun_edit_watch', None)
    if watch is None:
        watch = editor_ds._fnrun_edit_watch = {}

    def _key_of(text, line1):
        got = _fnrun_extract_def_text(text, line1, def_name)
        if got is None:
            return None
        return _fnrun_change_key(got[0])

    def _buf_key():
        return _key_of(tv_text, (def_disp_line or 0) + 1)

    # entry = [tv_text_seen, deadline, buf_baseline, (unused), give_up_at,
    #         armed ctx, pending-check ctx]. The baseline is the change key
    # (display text with folds spliced out); pending is checked by
    # line-subsequence at expiry (_fnrun_auto_exec_fire). Both ctx tuples
    # carry the text + coordinates for the render-thread stages, which run
    # OUTSIDE the widget.
    ent = watch.get(skey)
    if ent is None:
        _k0 = _buf_key()
        watch[skey] = [tv_text, None, _k0, None, None,
                       (_k0, code_root, def_buf_line, def_line, def_disp_line,
                        tv_text), None]
        return
    if status is not None:
        # A run just finished (inline button / panel): the text it
        # saw is the new baseline; don't chase it with another run.
        ent[2] = _buf_key()
        ent[1] = None
    _fnrun_auto_exec_consider(editor_ds, editor_state, skey, ent, tv_text,
                              code_root, def_disp_line, def_line)


def _fnrun_auto_exec_consider(editor_ds, editor_state, skey, ent, text,
                              code_root, def_disp_line, def_line):
    """Note a possibly-changed buffer for one registered def. Shared by the
    def widget and the editor-level scan (_fnrun_auto_exec_scan) — the
    widget only renders while the def line is in the viewport, so the scan
    keeps auto-exec alive once the user scrolls away. RENDER-THREAD COST:
    one identity test, and on a new text object one Timer start — the def
    extraction + change key run on that timer (_fnrun_auto_exec_check), so
    a keystroke never pays O(def) tokenizing inside draw_text."""
    if ent[0] is text:
        return
    ent[0] = text
    ent[6] = (text, code_root, def_disp_line, def_line)
    import threading as _thr
    pt = getattr(editor_ds, '_fnrun_check_timer', None)
    if pt is not None:
        pt.cancel()
    t = _thr.Timer(0.02, _fnrun_auto_exec_check,
                   args=(editor_ds, editor_state, skey, ent))
    t.daemon = True
    editor_ds._fnrun_check_timer = t
    t.start()


def _fnrun_auto_exec_check(editor_ds, editor_state, skey, ent):
    """Timer thread: did the def's CODE change? Identity alone is not an
    edit (the editor hands out several text objects for one content across
    frames; arming on each pushed the deadline forever) — compare the
    change key against what was last run or armed, and only then arm the
    debounce. A newer buffer supersedes this check (ent[0] moved on)."""
    try:
        text, code_root, def_disp_line, def_line = ent[6]
    except (TypeError, ValueError):
        return
    if ent[0] is not text:
        return
    got = _fnrun_extract_def_text(text, (def_disp_line or 0) + 1, skey[1])
    if got is None:
        return
    nk = _fnrun_change_key(got[0])
    if nk == ent[2] or nk == (ent[5] or (None,))[0]:
        return
    from src.lsd.gl_gui.toggles import Toggles
    dbc = Toggles.TextEditor.fnrun_auto_exec_edit_debounce_ms / 1000.0
    ent[4] = None
    ent[5] = (nk, code_root, got[1] + 1, def_line, got[1], text)
    _fnrun_auto_exec_arm(editor_ds, editor_state, skey, skey[0], skey[1],
                         ent, dbc)


def _fnrun_auto_exec_scan(editor_ds, editor_state, text, code_root):
    """Editor-body hook, once per render: ONE identity test in steady
    state; on a changed buffer, _fnrun_auto_exec_consider for each def the
    widget has registered — so auto-exec keeps firing while the def line
    is scrolled out of view. The def's last known display line seeds the
    extraction hint; its code root is refreshed from the editor's."""
    editor_ds._fnrun_code_root = code_root      # for open-together (fresh tree)
    if editor_state is None or text is getattr(editor_ds, '_fnrun_scan_tv', None):
        return
    editor_ds._fnrun_scan_tv = text
    watch = getattr(editor_ds, '_fnrun_edit_watch', None)
    if not watch:
        return
    for skey, ent in list(watch.items()):
        if ent[5] is None or not editor_state.params_auto_execute.get(skey[1]):
            continue
        _fnrun_auto_exec_consider(editor_ds, editor_state, skey, ent, text,
                                  code_root, ent[5][4], ent[5][3])


def _fnrun_change_key(src):
    """What "the def changed" means for Auto Execute: the def's source
    canonicalized through the tokenizer with COMMENTS dropped and token
    spacing normalized. So a comment edit (`# [tint=…]` included — those
    are read live) or re-spacing within a line never triggers a recompile;
    everything else does — body/signature/annotation/name edits, a default
    TYPED into the signature (the params panel's own splices are
    pre-baselined by _fnrun_queue_panel_splices, so they don't double-run), and line
    changes (blank lines added/removed: NL tokens are kept — a line shift
    re-keys the live sites below it, and the rerun is what re-publishes
    them). Indentation survives as INDENT/DEDENT tokens. Falls back to the
    raw text when the source doesn't tokenize (mid-edit)."""
    import io
    import tokenize
    parts = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            t = tok.type
            if t in (tokenize.COMMENT, tokenize.ENCODING, tokenize.ENDMARKER):
                continue
            if t in (tokenize.NEWLINE, tokenize.NL):
                parts.append("\n")
            elif t == tokenize.INDENT:
                parts.append("\x01")
            elif t == tokenize.DEDENT:
                parts.append("\x02")
            else:
                parts.append(tok.string + " ")
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    return "".join(parts)


def _fnrun_resolve_splices(text, queued):
    """Turn name-queued params-panel splices `(def_name, line_hint, param,
    new_src)` into `(start, length, new_src)` against `text` AS IT IS NOW.
    A queued splice is applied on a later editor body run, so offsets
    captured at queue time are stale after any keystroke in between — the
    old offset-based queue overwrote whatever had shifted into its span.
    Per entry: the def is located by name (nearest the hinted display
    line), the param's default span by the signature scanner, and the new
    source replaces whatever the default holds: a panel edit is the user's
    latest intent for THAT param. (An earlier "drop it if the text no
    longer holds the source the panel last saw" rule compared against a
    record the def widget maintains only while drawn, so with the def
    scrolled away every panel edit was dropped and pending never got the
    value.) Already-equal defaults are skipped."""
    out = []
    for def_name, hint, pk, new_src in queued:
        got = _fnrun_extract_def_text(text, (hint or 0) + 1, def_name)
        if got is None:
            continue
        sp = _fnrun_sig_default_span(text, got[1], pk)
        if sp is None:
            continue
        if text[sp[0]:sp[1]] == new_src:
            continue
        out.append((sp[0], sp[1] - sp[0], new_src))
    return out


def _fnrun_queue_panel_splices(editor_ds, skey, def_name, hint, values=None):
    """Write the params panel's edits back into the code — from ANYWHERE
    (the def widget, or the Auto-Execute hold timer via post_to_render),
    never only from the widget: the widget renders only while the def line
    is in the viewport, so a write-back that waited for it stranded panel
    values out of the text whenever the user scrolled away, and the next
    text→panel sync then "snapped the panel back" to the text's values.

    Works off the editor-held state alone: the DISPLAYED copy
    (`_fnrun_shown_nodes[skey]`, what the panel mutates) and the per-param
    "source the panel last saw" (`_fnrun_param_seen[skey]`). A param whose
    rendered source equals the seen source was not touched — skipped, the
    text is truth. Each changed param is queued BY NAME for
    _fnrun_resolve_splices (the editor body resolves the span against the
    text as it is then and writes the value — a panel edit is the latest
    intent for that param), mirrored RAW into the real tree node (no
    bubbling — a bubbled write regenerates the editor text from the
    lagging tree), and the auto-exec baseline is pre-advanced so the
    landed splice isn't taken for a fresh edit."""
    shown = (getattr(editor_ds, '_fnrun_shown_nodes', None) or {}).get(skey)
    seen = (getattr(editor_ds, '_fnrun_param_seen', None) or {}).get(skey)
    if shown is None or seen is None:
        return
    if values is not None and values is not shown:
        for pk, pv in values.items():
            dict.__setitem__(shown, pk, pv)
    node = ((getattr(editor_ds, '_fnrun_node_cache', None) or {})
            .get(skey) or (None, None))[1]
    params_node = node.get('parameters') if isinstance(node, dict) else None
    queue = editor_ds.__dict__.setdefault('_fnrun_splices', [])
    queued = False
    for pk, pv in shown.items():
        if not isinstance(pk, str) or pk.startswith('__'):
            continue
        new_src = _fnrun_param_src(pv)
        if seen.get(pk) == new_src:
            continue
        queue.append((def_name, hint or 0, pk, new_src))
        seen[pk] = new_src
        queued = True
        if isinstance(params_node, dict):
            dict.__setitem__(params_node, pk, pv)
    if not queued:
        return
    ctx = next((e[5] for k, e in (getattr(editor_ds, '_fnrun_edit_watch', None)
                                  or {}).items() if k[1] == def_name), None)
    if ctx is not None:
        _fnrun_prebaseline_splices(editor_ds, skey, ctx[5], ctx[4], def_name)
    editor_ds.invalidate()
    request_render()


def _fnrun_prebaseline_splices(editor_ds, skey, tv_text, def_disp_line,
                               def_name):
    """The params panel just queued signature splices for a def whose run
    it already triggered: move the Auto Execute baseline to the text AS IT
    WILL READ once the splices land, so the edit-watcher doesn't see the
    landed splice as a fresh edit and run the def a second time."""
    watch = getattr(editor_ds, '_fnrun_edit_watch', None)
    ent = watch.get(skey) if watch else None
    if ent is None:
        return
    text = tv_text
    try:
        for start, length, new in sorted(
                _fnrun_resolve_splices(
                    text, editor_ds.__dict__.get('_fnrun_splices', ())),
                key=lambda t: -t[0]):
            text = text[:start] + new + text[start + length:]
        got = _fnrun_extract_def_text(text, (def_disp_line or 0) + 1, def_name)
    except Exception:
        return
    if got is not None:
        ent[2] = _fnrun_change_key(got[0])
        ent[1] = None            # cancel any pending expiry for the old text


def _fnrun_auto_exec_arm(editor_ds, editor_state, skey, file_path, def_name,
                         ent, delay):
    """Schedule the auto-execute expiry check `delay` seconds out on a
    timer thread (_fnrun_auto_exec_fire does the pending gate there and
    posts only the run to the render thread). Re-arming cancels the
    previous timer."""
    import threading as _thr
    deadline = time.monotonic() + delay
    ent[1] = deadline

    def _fire():
        _fnrun_auto_exec_fire(editor_ds, editor_state, skey, file_path,
                              def_name, ent, deadline)

    pt = getattr(editor_ds, '_fnrun_edit_timer', None)
    if pt is not None:
        pt.cancel()
    t = _thr.Timer(max(delay, 0.01), _fire)      # timer thread; the run
    t.daemon = True                               # alone is posted to render
    editor_ds._fnrun_edit_timer = t
    t.start()


def _fnrun_auto_exec_fire(editor_ds, editor_state, skey, file_path, def_name,
                          ent, deadline):
    """Expiry of the auto-execute debounce (TIMER THREAD — string work
    only; the run is posted to the render thread). Superseded timers (a
    newer arm moved the deadline) and a meanwhile-toggled-off Auto Execute
    are no-ops. The armed change key is compared against the baseline (did
    THIS def change?), then the fire waits until PendingSave carries the
    buffer's def lines (150 ms re-arms, ≤5 s) before compiling — the
    compile reads pending, never a stale generation."""
    if ent[1] != deadline or ent[5] is None:
        return
    if not editor_state.params_auto_execute.get(def_name):
        return
    _armed_key, code_root, def_buf_line, def_line, def_disp_line, tv_text = ent[5]
    ent[1] = None
    key = _armed_key
    if key is None or key == ent[2]:
        return          # the edit didn't touch this def's signature
    from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    ptext = PendingSave.current_file_text(str(file_path))
    pend_def = (_fnrun_extract_def_text(ptext, def_line, def_name)
                if ptext is not None else None)
    # The key as it is NOW (not as armed - an open-to-save or an other
    # edit since the arm would otherwise gate against stale text), and the
    # editor's FULL buffer string, not the display text: display has
    # signature ranges spliced out, so only the full text can be compared
    # EXACTLY to pending. Exact equality is the only safe gate - the
    # earlier "buffer lines are a subsequence of pending" passed on a
    # stale pending after a DELETE (the remaining lines were all still
    # there, in order) and ran the old code.
    full = (getattr(editor_ds, '_kwargs', None) or {}).get('input_value')
    if not isinstance(full, str):
        full = tv_text
    buf_def = _fnrun_extract_def_text(full, def_line, def_name)
    if buf_def is None or pend_def is None or buf_def[0] != pend_def[0]:
        # Pending doesn't carry this buffer yet (the deferred save channel
        # is still behind it) — "has pending moved" is NOT enough: it may
        # have just landed the PREVIOUS edit, and compiling that would run
        # one edit behind and never revisit this one. Poll until pending's
        # def lines equals the buffer's. Bounded so a stuck editor can't
        # poll forever.
        now = time.monotonic()
        if ent[4] is None:
            ent[4] = now + 5.0
        if now < ent[4]:
            _fnrun_auto_exec_arm(editor_ds, editor_state, skey, file_path,
                                 def_name, ent, 0.15)
        return
    ent[2] = key

    def _run():
        statuses = getattr(editor_ds, '_fnrun_status', None)
        if statuses is None:
            statuses = editor_ds._fnrun_status = {}
        fn = _fnrun_resolve(file_path, def_line, def_name, prefer_pending=True)
        if fn is None:
            statuses[skey] = ('err', f"couldn't resolve '{def_name}' — not "
                                     f"found in live modules or source", 'live')
        else:
            # Inputs come from the COMPILED PENDING CODE - and the signature
            # defaults - never from a cached parse node: the gate above
            # made pending equal the editor buffer, so those defaults ARE
            # what the editor (and, once its splice landed, the params
            # panel) shows. Passing node-derived kwargs here calls the def
            # with whatever an older tree held.
            ok, err = _fnrun_run(fn, instrumented=True)
            statuses[skey] = (('ok', Melty.frame_count, 'live') if ok
                              else ('err', err, 'live'))
            _fnrun_after_live_run(editor_ds)
        editor_ds.invalidate()
        request_render()

    Melty.post_to_render(_run)


def fnrun_auto_run_on_open(editor_ds, store_obj):
    """A live-value window was opened BY THE USER (marker double-click /
    gutter) on a def with Auto Execute on: request the routine auto-run —
    clear the def's baseline and arm the same expiry an edit would, so it
    recompiles + runs (coalesced by the arm's timer) and the window fills
    instead of showing the parked rerun hint. No-op until the def widget
    has registered the def (its line rendered once this session)."""
    def_name = getattr(store_obj, "__name__", None)
    watch = getattr(editor_ds, '_fnrun_edit_watch', None) or {}
    skey, ent = next(((k, e) for k, e in watch.items() if k[1] == def_name),
                     (None, None))
    if ent is None or ent[5] is None:
        return
    state = next((v for v in (getattr(editor_ds, "misc", None) or {}).values()
                  if hasattr(v, "params_auto_execute")), None)
    if state is None or not state.params_auto_execute.get(def_name):
        return
    ent[2] = None                       # "changed": the expiry runs now
    ctx = list(ent[5])
    ctx[1] = getattr(editor_ds, '_fnrun_code_root', ctx[1])   # current root
    ent[5] = tuple(ctx)
    ent[4] = None
    _fnrun_auto_exec_arm(editor_ds, state, skey, skey[0], def_name, ent, 0.2)


@render_func(show_bg=False, shadow=False, with_header=None, show_name=False,
             use_cache=False, selectable=False, is_tree=False)
def draw_fnrun_params_panel(input_value=None, draw_state=None, unique=0,
                            fnrun_file=None, fnrun_line=None,
                            fnrun_name=None, editor_ds=None,
                            auto_execute=False, editor_state=None, **kwargs):
    """Body of the def-widget's params window: a Run + instrumented-run row
    above the parameters dict. Both resolve + run the def exactly like the
    widget's inline play/eye buttons, using the panel's CURRENT (possibly
    just-edited) values; status lands in the editor's _fnrun_status, so the
    inline buttons show the same flash/error, and the eye triggers the same
    post-run live-view delivery. The parameters dict renders through its
    normal type routing; (changed, value) propagate to the widget's splice
    logic untouched."""
    from src.lsd.gl_gui.view.core_views.headers import flat_button
    from src.lsd.gl_gui.view.core_views.new_core_view import draw_any
    run = flat_button(f" Run##fnpprun{unique}", draw_state,
                      f"fnpprun::{unique}", height=28,
                      color=(0.499, 0.844, 0.488), corner_radius=5.0,
                      shadow=True)
    imgui.same_line(spacing=6)
    live = flat_button(f" Run Visualize##fnpplive{unique}", draw_state,
                       f"fnpplive::{unique}", height=28, color=(0.13, 0.55, 0.13),
                        corner_radius=5.0, 
                       shadow=True)
    imgui.same_line(spacing=6)
    # Auto Execute: while on, any param edit below triggers Run Visualize
    # with the fresh values. The persisted per-def bool
    # (ScriptEditorState.params_auto_execute - the params_windows's
    # pattern) is read DIRECTLY every frame, like the visibility bool: the
    # `auto_execute` kwarg was captured when the def widget's body last ran
    # inside the editor's blit-cached tile, so it goes stale the moment the
    # checkbox writes the dict - the kwarg is only the no-state fallback.
    if editor_state is not None and fnrun_name:
        auto_execute = bool(editor_state.params_auto_execute.get(fnrun_name))
    _ae_ch, _ae_val = RenderFuncs.draw_bool(bool(auto_execute), name=f"Auto Execute##fnppae{unique}")
    if _ae_ch:
        auto_execute = _ae_val
        if editor_state is not None and fnrun_name:
            editor_state.params_auto_execute[fnrun_name] = _ae_val
        draw_state.invalidate()
        request_render()
    # Ctrl+Enter over the panel = the instrument button. Registered BLOCKING
    
    # with a priority above draw_main's root actions (draw_function_token's
    # pattern), so the main recompile-all flow never runs while the mouse
    # is over this window; the panel shell is use_cache=False, so the
    # subscription re-registers every time the window renders.
    if draw_state.on_action("ctrl_enter_down", priority_delta=1024):
        live = True
    # Params render BEFORE the run block (buttons already laid out above),
    # so an auto-executed run - and any click-triggered one - compiles
    # against the panel's just-edited node.
    _ch, _val = draw_any(input_value, name="parameters", child_kwargs={"syntax_highlight":False}, show_add_delete=False)
    if _ch and auto_execute:
        live = True
    if (run or live) and fnrun_file and fnrun_name:
        _mode = 'live' if live else 'run'
        # prefer_pending: compile the LATEST pending source (a change just
        # spliced from this very panel included) instead of running the stale
        # live function while the background reparse/live-apply catches up.
        fn = _fnrun_resolve(fnrun_file, fnrun_line, fnrun_name,
                            prefer_pending=True)
        if fn is None:
            _st = ('err', f"couldn't resolve '{fnrun_name}' — not found in "
                          f"live modules or source", _mode)
        else:
            ok, err = _fnrun_run(fn, instrumented=live,
                                 params=_fnrun_params_from_node(
                                     {'parameters': input_value}))
            _st = (('ok', Melty.frame_count, _mode) if ok
                   else ('err', err, _mode))
            if live and editor_ds is not None:
                _fnrun_after_live_run(editor_ds)
        if editor_ds is not None:
            _sts = getattr(editor_ds, '_fnrun_status', None)
            if _sts is None:
                _sts = editor_ds._fnrun_status = {}
            _sts[(str(fnrun_file), fnrun_name)] = _st
            editor_ds.invalidate()
        draw_state.invalidate()
        request_render()
    # A params edit writes the CODE from right here - this panel is the only
    # place that knows it happened, and it must not wait for the def widget
    # (which renders only while the def line is in the viewport: routing the
    # write through it left the text - what every run compiles - without the
    # panel's values whenever the def was scrolled away). With auto_execute
    # the edit already ran above; the write-back rides a trailing window so
    # a drag doesn't pay splice → reparse per tick. Otherwise it's immediate.
    if _ch and editor_ds is not None and fnrun_file and fnrun_name:
        _skey = (str(fnrun_file), fnrun_name)
        _pt = getattr(editor_ds, '_fnrun_hold_timer', None)
        if _pt is not None:
            _pt.cancel()
        if auto_execute:
            from src.lsd.gl_gui.toggles import Toggles
            _hd = Toggles.TextEditor.fnrun_text_sync_debounce_ms / 1000.0
            import threading as _thr

            def _flush(_ed=editor_ds, _sk=_skey, _dn=fnrun_name,
                       _hint=fnrun_line or 0, _vals=input_value):
                _fnrun_queue_panel_splices(_ed, _sk, _dn, _hint, values=_vals)

            _t = _thr.Timer(max(_hd, 0.01),
                            lambda: Melty.post_to_render(_flush))
            _t.daemon = True
            editor_ds._fnrun_hold_timer = _t
            _t.start()
        else:
            _fnrun_queue_panel_splices(editor_ds, _skey, fnrun_name,
                                       fnrun_line or 0, values=input_value)
    return _ch, _val


def draw_run_fn_token_plain(input_value, width=20, height=20, name=None,
                            tint=None, text_tint=None, editor_ds=None,
                            file_path=None, def_line=None, def_name=None,
                            code_root=None, def_buf_line=None,
                            tv_text=None, def_disp_line=None,
                            editor_state=None, fn_tint=None,
                            **kwargs):
    """Run buttons for a function definition — GUTTER widget for 'def_name'
    tokens (`gutter: True` in DEFAULT_TOKEN_VIEWS): the name text draws
    normally in the code and this widget is drawn by the gutter pass IN
    PLACE OF the def line's number, sized to the number strip (`width` /
    `height` = the cell). Two flat_buttons: the double-wide PLAY runs the
    INSTRUMENTED twin (run_instrumented — live_view_forward's path), so
    every assignment publishes a snapshot and its marker anchors right in
    this editor via the snapshot overlay (inline, running always means
    inspecting); the sliders open the params panel. Both wear the
    function's definition tint (`fn_tint`, the def-block tint) when it has
    one. Errors print the colored traceback and surface on the run button
    (red; hover shows the message beside it); success flashes it, fading
    over ~45 frames.

    Plain (wrapper-less) like the other token widgets. The click routes
    through flat_button's on_action claim on the EDITOR draw_state — the
    fast-dock model, so blit-cache event delivery holds — and owns_mouse
    registers the lead rect in _plain_tv_rects so a press here never moves
    the caret. Status is keyed by (file, def name), not the render-order
    `name` (that shifts as widgets scroll into view) and not the line
    (that shifts on edits)."""
    from src.lsd.gl_gui.view.core_views.headers import flat_button
    x, y = imgui.get_cursor_screen_pos()
    if editor_ds is None:
        return False, input_value

    statuses = getattr(editor_ds, '_fnrun_status', None)
    if statuses is None:
        statuses = editor_ds._fnrun_status = {}
    skey = (str(file_path), def_name)
    status = statuses.get(skey)

    # Hover-edge invalidation: flat_button's hover styling only shows when the
    # (cached) editor tile repaints, so paint the tile exactly on the edges.
    io = imgui.get_io()
    hovered = (x <= io.mouse_pos.x < x + width
               and y <= io.mouse_pos.y < y + height)
    hov_reg = getattr(editor_ds, '_fnrun_hover', None)
    if hov_reg is None:
        hov_reg = editor_ds._fnrun_hover = {}
    if hov_reg.get(skey) != hovered:
        hov_reg[skey] = hovered
        editor_ds.invalidate()

    # Function tint (the def-block tint) colors both buttons when the def
    # has one; otherwise the live blue / neutral grey defaults.
    _has_tint = fn_tint is not None and len(fn_tint) >= 3
    _c_live = (tuple(fn_tint[:3]) if _has_tint
               else (0.40, 0.53, 0.78))    # draw_function_live's lab blue
    _c_pp = tuple(fn_tint[:3]) if _has_tint else (0.55, 0.58, 0.66)
    if status is not None:
        statuses.pop(skey, None)
    # Two buttons: the DOUBLE-WIDE instrumented run (play glyph) -
    # live_view_forward's twin path via run_instrumented, so every
    # assignment's snapshot marker lands right in THIS editor through the
    # snapshot overlay - and the params-panel toggle (sliders glyph).
    _gap = 3.0
    _unit = max(6.0, (width - 4.0 - 2 * _gap) / 3.0)
    _bw_run = 2 * _unit + _gap
    _bh = max(6.0, height - 4.0)
    _by = y + (height - _bh) * 0.5
    imgui.set_cursor_screen_pos((x, _by))
    live_clicked = flat_button(f"\uf04b##{name}lv", editor_ds,
                               f"fnrunlv::{name}",
                               width=_bw_run, height=_bh, color=_c_live,
                               corner_radius=4.0, shadow=True)
    imgui.set_cursor_screen_pos((x + _bw_run + _gap, _by))
    params_clicked = flat_button(f"\uf1de##{name}pp", editor_ds,
                                 f"fnrunpp::{name}",
                                 width=_unit, height=_bh,
                                 color=_c_pp,
                                 corner_radius=4.0, shadow=True)

    # ── Params panel: the def's `parameters` sub-dict from the cst tree
    # (already injected into draw_text - no cost), rendered with draw_any as
    # a latching closable window. An edit round-trips as PARTIAL CODE
    # INSERTION: the changed param's expression is spliced into the
    # signature via the editor's token-edit channel (_fnrun_splices), so it
    # saves/undoes like a keystroke, and the node is updated in-place so
    # the next run picks the change up immediately.
    # Visibility IS a persisted bool: TextEditorState.params_windows_open
    # (the TabState pattern - kept in draw_text's draw_state.misc and
    # serialized with it). Read directly every frame - no open-prev latch;
    # the toggle click and the header X just write the bool.
    _pp_vis = (editor_state.params_windows_open
               if editor_state is not None else {})
    if params_clicked:
        _pp_vis[def_name] = not _pp_vis.get(def_name, False)
    elif live_clicked and not _pp_vis.get(def_name, False):
        # A manual run SHOWS the params panel (the run itself happens below,
        # in the live_clicked branch) - it never closes an open panel.
        _pp_vis[def_name] = True
    _pp_wins = getattr(editor_ds, '_fnrun_params_wins', None)
    if _pp_wins is None:
        _pp_wins = editor_ds._fnrun_params_wins = {}
    _pw = _pp_wins.get(skey)
    # Header-X lands during the DEFERRED closing frame (after our stamp
    # closing frame) - the persistent window ds carries closed=True now;
    # mirror it into the visibility bool before reading it.
    if (_pw is not None and _pw.closed and not params_clicked
            and _pp_vis.get(def_name)):
        _pp_vis[def_name] = False
    _pp_want = bool(_pp_vis.get(def_name))
    _params_node = None
    if _pp_want or params_clicked or (_pw is not None and not _pw.closed):
        # Lazy node resolution - only for the toggle click, while the panel
        # is open, or for the one closing click; closed-panel frames pay
        # nothing (the resolve walk is click-scale cheap, not frame-scale).
        _def_node = _fnrun_def_node_for(editor_ds, skey, code_root,
                                        def_name, def_buf_line, tv_text)
        _params_node = (_def_node.get('parameters')
                        if isinstance(_def_node, dict) else None)
    # DISPLAYED-node latch (also read further down): the background reparse
    # rebuilds the tree mid-typing, so the node handed to the panel is held
    # across frames and only advanced quieting.
    _shown_map = getattr(editor_ds, '_fnrun_shown_nodes', None)
    if _shown_map is None:
        _shown_map = editor_ds._fnrun_shown_nodes = {}
    if _params_node is None and _pp_want:
        # Transient miss - the cst tree is mid-rebuild after an edit (a
        # newline shifting the def used to land here every time), or a
        # held/failed parse. Keep the panel up on the node it last showed;
        # closing is the USER's act (toggle click / header X), never a
        # parse hiccup's. Only a def with no node ever seen closes.
        _params_node = _shown_map.get(skey)
        if _params_node is None:
            _pp_vis[def_name] = False
            _pp_want = False
    if _params_node is not None and (
            _pp_want or (_pw is not None and not _pw.closed)):
        from src.lsd.gl_gui.view.mode import Mode
        # imgui.set_cursor_screen_pos((x, y))
        # window_pos only on FIRST spawn - passing it every call re-pins the
        # panel under the button and eats the user's drags (the live-value
        # windows follow the same set-once rule). No width: the window
        # wraps/resizes normally. swoosh=False - no connector ribbon.
        _pp_kwargs = {}
        # if _pw is None:
        #     _pp_kwargs["window_pos"] = (0, height + 6)
        # DISPLAYED node latch: the background reparse (small-file average
        # ~119ms) rebuilds the tree MID-TYPING, so restamping the fresh
        # `_params_node` per run showed half-typed defaults in the panel
        # instead of the panel-sync debounce below - the reparse was a
        # second, undebounced channel into the panel. Hold the last shown
        # node and hand THAT to the panel; the swap to the current node
        # happens only in the debounce-expiry branch below.
        _shown = _shown_map.get(skey)
        if _shown is None:
            _shown = _shown_map[skey] = _fnrun_detach(_params_node)
        # Per-param "source the panel last SAW": a param whose rendered
        # source still equals that was not touched in the panel, and the
        # splice below must leave its expression alone: the shown node can be
        # an older parse than the code (that is the latch's point), and
        # writing the changed param back snapped typed-in defaults
        # back to stale values ("the inputs revert when a live view
        # opens"). Seeded from the node on first show; advanced by the
        # text→panel sync and by each splice.
        _seen_map = getattr(editor_ds, '_fnrun_param_seen', None)
        if _seen_map is None:
            _seen_map = editor_ds._fnrun_param_seen = {}
        _seen = _seen_map.get(skey)
        if _seen is None:
            _seen = _seen_map[skey] = {
                _pk: _fnrun_param_src(_pv) for _pk, _pv in _shown.items()
                if isinstance(_pk, str) and not _pk.startswith('__')}
        _pch, _pnv, _pw = draw_fnrun_params_panel(
            _shown, name=f"{def_name} params##fnpp::{def_name}",
            mode=Mode.WINDOW, closed=not _pp_want,
            parent_window=editor_ds, return_extras=True, swoosh=False,
            fnrun_file=file_path, fnrun_line=def_line, fnrun_name=def_name,
            editor_ds=editor_ds,
            auto_execute=bool(editor_state.params_auto_execute.get(def_name))
            if editor_state is not None else False,
            editor_state=editor_state)
        _pp_wins[skey] = _pw
        if _pw.closed and _pp_want and not params_clicked:
            _pp_vis[def_name] = False   # X-closed inline (same-frame close)
            _pp_want = False
        # TEMP diag: which link of the panel→splice hop fires (remove with
        # the other fnrun diag once the value window round-trip is solid).
        _ptrace("fnrun widget panel-ret", def_name=def_name, pch=bool(_pch),
                pnv=type(_pnv).__name__, tv=tv_text is not None)

        # The panel writes its own edits to the code (draw_fnrun_params_panel
        # → _fnrun_queue_panel_splices); this widget only syncs TEXT → PANEL.
        if isinstance(_params_node, dict) and tv_text is not None:
            # TEXT → PANEL sync (the reverse of the splice above): a
            # signature edit done in the editor shows in the open panel
            # immediately instead of waiting out the debounced background
            # reparse. Same diff primitive again - each param's text
            # slice vs the node's rendered source - with the changed
            # expression parsed through the central converter. RAW writes
            # (dict.__setitem__, no bubbling): this is a deferred sync of a
            # tree the reparse will overwrite anyway, and a bubbled write
            # would run the code host → chain_out per keystroke (the echo
            # storm). Gated on text identity so it runs once per buffer
            # change, not per frame; a mid-typing unparseable expression
            # just skips until it parses. Guarded by `and not _pch`: on
            # panel-edit frame the splice above is the truth flowing the
            # other way.
            _sync_memo = getattr(editor_ds, '_fnrun_sig_sync', None)
            if _sync_memo is None:
                _sync_memo = editor_ds._fnrun_sig_sync = {}
            # TRAILING DEBOUNCE, re-armed per keystroke: entry is
            # [tv_text_seen, deadline]; deadline None = already synced. A
            # fresh buffer arms the window (a one-shot timer wakes the
            # event-driven render loop at expiry — the widget itself only
            # runs on frames), and the sync below fires once, quiet-side.
            from src.lsd.gl_gui.toggles import Toggles
            _dbc = Toggles.TextEditor.fnrun_text_sync_debounce_ms / 1000.0
            _ent = _sync_memo.get(skey)
            _due = False
            if _ent is None or _ent[0] is not tv_text:
                _sync_memo[skey] = [tv_text, time.monotonic() + _dbc]
                if _dbc <= 0:
                    _due = True
                else:
                    import threading as _thr
                    _pt = getattr(editor_ds, '_fnrun_sync_timer', None)
                    if _pt is not None:
                        _pt.cancel()
                    _t = _thr.Timer(_dbc, request_render)
                    _t.daemon = True
                    editor_ds._fnrun_sync_timer = _t
                    _t.start()
            elif _ent[1] is not None:
                if time.monotonic() >= _ent[1]:
                    _ent[1] = None
                    _due = True
                else:
                    # Inside the quiet window: the timer above produces the
                    # expiry frame; keep this tile un-cached until then so
                    # the widget actually re-runs on it.
                    editor_ds.invalidate()
            if _due:
                _synced = False
                for _pk in list(_params_node.keys()):
                    if not isinstance(_pk, str) or _pk.startswith('__'):
                        continue
                    _sp = _fnrun_sig_default_span(tv_text,
                                                  def_disp_line or 0, _pk)
                    if _sp is None:
                        continue
                    _txt = tv_text[_sp[0]:_sp[1]]
                    if _txt == _fnrun_param_src(_params_node[_pk]):
                        continue
                    try:
                        import libcst as _cst_mod
                        from src.lsd.gl_gui.view.core_conversion.\
                            libcst_conversion import _cst_to_python_or_raw
                        _val = _cst_to_python_or_raw(
                            _cst_mod.parse_expression(_txt))
                    except Exception:
                        continue        # mid-typing fragment - try later
                    dict.__setitem__(_params_node, _pk, _val)
                    dict.__setitem__(_shown, _pk, _val)   # the displayed copy
                    _seen[_pk] = _txt
                    _synced = True
                # Expiry is ALSO the only place the displayed-node cach
                # advances to the current (usually post-reparse) node - the
                # panel shows new values exactly once, quiet-side.
                if ((_synced or _shown is not _params_node)
                        and _pw is not None and _pw._tile_id is not None):
                    Melty.cache.invalidate_up(_pw._tile_id, force=True,
                                              max_depth=8)
                    request_render()
                # The latch advances to the current parse: its values come
                # from the editor, so they are all "seen". Detached copy -
                # see _fnrun_detach.
                _seen.update({
                    _pk: _fnrun_param_src(_pv)
                    for _pk, _pv in _params_node.items()
                    if isinstance(_pk, str) and not _pk.startswith('__')})
                _shown_map[skey] = _fnrun_detach(_params_node)
    _fnrun_auto_exec_on_edit(editor_ds, editor_state, skey, file_path,
                             def_line, def_name, code_root, def_buf_line,
                             tv_text, def_disp_line, status)
    if hovered and status is not None and status[0] == 'err' and status[1]:
        # Error readout beside the button - draw-list text, hover-only (the
        # hover-edge invalidation above repaints it in and out).
        dl = imgui.get_window_draw_list()
        dl.add_text(x + width + 6.0, y - height,
                    imgui.get_color_u32_rgba(1.0, 0.45, 0.40, 1.0), status[1])

    if live_clicked:
        _mode = 'live'
        # Same truth as the auto-run: the pending code, with its own
        # signature defaults (no node-derived kwargs, see the auto-run).
        fn = _fnrun_resolve(file_path, def_line, def_name, prefer_pending=True)
        if fn is None:
            statuses[skey] = ('err', f"couldn't resolve '{def_name}' — not "
                                     f"found in live modules or source", _mode)
        else:
            ok, err = _fnrun_run(fn, instrumented=True)
            statuses[skey] = (('ok', Melty.frame_count, _mode) if ok
                              else ('err', err, _mode))
            _fnrun_after_live_run(editor_ds)
        editor_ds.invalidate()
        request_render()
    return False, input_value

draw_run_fn_token_plain._plain_tv = True

# The default callback-widget set: when draw_text is called with no token_views,
# Font Awesome glyphs ("icon" tokens) become inline icon-picker dropdowns,
# True/False become double-click-to-toggle words, numeric literals become drag
# widgets, and color tuples (3 or 4 numeric channels, RGBA) and hex color
# strings (`'#8888c6'`) become color swatches. Add more entries here to make other token kinds interactive by
# default.
# For whole_token entries char_width is also the "this is inline" flag - the
# widget REPLACES the text at exactly token-width (len(token) cells), unless
# lead_cells=N makes it an ACCESSORY: the text draws normally (shifted N cells
# right) and the widget gets only the N-cell lead space beside it, or
# trail_cells=N makes it TRAILING: the text draws in place and the widget
# gets the N-cell area directly after it (the rest of the line shifts).
# gutter=True makes it a GUTTER widget: the token draws as plain text and the
# renderer runs in the gutter pass on the token's line, IN PLACE OF the line
# number (cell = the number strip right of the live-marker buttons, width /
# height passed like any token widget). The body only records it
# (ds._gutter_views), so it costs nothing on the text grid. owns_mouse
# marks widgets that consume clicks (they subscribe to the left_mouse_*
# events); for REPLACE widgets draw_text then emulates the caret placement a
# text click would have given. pad_px widens a REPLACE widget's view N px per
# side past the token cells (visual breathing room; the grid stays exact).
DEFAULT_TOKEN_VIEWS = {
    # tint: the widget's bg wash, passed as a call kwarg at the draw_text call
    # sites - decorator-level for the provenance color and no longer reaches
    # render kwargs.
    # icon uses the PLAIN renderer too - the older wrapped renderer registered a
    # nested dropdown @window per icon token (leak) and "..."-trimmed the
    # glyph; owns_mouse suppresses caret placement for chip presses.
    "icon": {"renderer": draw_icon_selector_plain, "char_width": 3,
             "owns_mouse": True, "tint": (0.77, 0.66, 0.20, 1.00)},
    # bool/number use custom PLAIN (wrapper-less) renderers: with many inline
    # widgets on screen the ~90µs @render_func wrapper per widget per frame
    # dominated; these draw straight into the editor tile with raw imgui.
    "bool": {"renderer": draw_bool_token_plain, "char_width": 1, "whole_token": True,
             "tint": (0.911, 0.305, 0.0)},
    "number": {"renderer": draw_number_token_plain, "char_width": 1, "whole_token": True,
               "owns_mouse": True, "pad_px": 2, "tint": (0.026, 0.041, 0.056)},
    "color3": {"renderer": draw_color3_token_plain, "char_width": 1, "whole_token": True,
               "owns_mouse": True, "lead_cells": 2},
    # Hex color strings (`'#8888c6'`) get the same swatch beside the literal.
    "colorhex": {"renderer": draw_colorhex_token_plain, "char_width": 1, "whole_token": True,
                 "owns_mouse": True, "lead_cells": 2},
    # Function definitions: the run buttons live in the GUTTER, in place of
    # the def line's number (`gutter: True` - see the gutter-widget note in
    # the token-views block above); the name draws and edits normally and
    # the code tree is untouched. The double-wide play runs the
    # instrumented twin (live_view_forward's path); the sliders open the
    # params panel (cst-dict defaults, syncable back into the signature).
    "def_name": {"renderer": draw_run_fn_token_plain, "char_width": 1,
                 "whole_token": True, "owns_mouse": True, "gutter": True},
}

# live_view() call sites get an anchor marker + nested value window (the first
# type-keyed overlay entry). Import from its own module so the widgets and
# their live_view imports stay out of this file.
from src.lsd.gl_gui.view.core_views.live_view_views import (
    install_token_views as _install_live_view_tv,
    flush_selected_markers as _flush_selected_markers)
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


def _lv_line_map(parse_source, buffer_text):
    """Line-number bridge, parse-space → buffer-space, for the token overlays:
    while a chain_in merge is in flight the buffer has moved (Enter presses,
    typing) but node spans are still in the HELD parse's coordinates — without
    the bridge every live-view marker sits on stale lines for the whole
    debounce+merge window. Prefix/suffix line diff, tolerant of whitespace-
    only-line differences (the parse source is dedent-normalized). Returns
    map(line) -> line | None (None = inside the changed region: no
    trustworthy anchor, that marker skips the frame), or None overall when
    the texts already line up (no bridging needed)."""
    if not parse_source or not buffer_text or parse_source is buffer_text:
        return None
    a = parse_source.split("\n")
    b = buffer_text.split("\n")
    na, nb = len(a), len(b)

    def eq(x, y2):
        # STRIPPED comparison: the code-host route parses spans DEDENTED
        # (see _parse_col_shift) and blank lines ws-normalized - raw equality
        # marked every line of a method span as changed, mapped every anchor to
        # None, and blanked the live views entirely. Stripped equality keeps
        # the diff positional; a false boundary line costs at most a
        # one-line marker shift for one merge window.
        return x.strip() == y2.strip()

    pre = 0
    m = min(na, nb)
    while pre < m and eq(a[pre], b[pre]):
        pre += 1
    if pre == na and na == nb:
        return None
    suf = 0
    while suf < (na - pre) and suf < (nb - pre) and eq(a[na - 1 - suf], b[nb - 1 - suf]):
        suf += 1
    delta = nb - na
    safe_tail = na - suf            # 1-based old lines > safe_tail shift by delta

    def _map(line):
        if line <= pre:
            return line
        if line > safe_tail:
            return line + delta
        return None
    return _map


_TV_RENDERER_ERRORS = set()   # (renderer name, str(exc)) already reported


def _draw_cst_token_views(code_tree, token_views, origin_x, origin_y, line_px, char_w, ds,
                          line_offset=0, jump_to=None, buffer_text=None,
                          sel_lo=None, sel_hi=None, fold_line_map=None,
                          display_shift=None,
                          sel_caret=None, fold_d2b=None, caret_line=None,
                          live_store=None, col_shift=0):
    """Overlay pass for the TYPE-keyed entries of `token_views`: walk the code_tree
    for nodes matching a key type and call its renderer positioned at the node's
    span. Lines are 1-indexed relative to the editor's source (== code_tree.source),
    so span line 1 sits at origin_y. Runs after the text body. `root`/`line_offset`/
    `jump_to` ride along so a renderer can resolve file-absolute context (the
    live_view overlay maps its node span back to a file line). `sel_caret` is
    the selection's moving end: the live_view overlays queue every token
    inside the selection and `flush_selected_markers` (called once after the
    walk) lets only the one nearest the caret preview."""
    if not token_views or not isinstance(code_tree, dict):
        return
    type_specs = [(k, v) for k, v in token_views.items() if isinstance(k, type)]
    if not type_specs:
        return
    # Parse→buffer line bridge (see _lv_line_map), rebuilt only when either
    # text object changes - one ~1ms diff per edit, then cached. The cache
    # holds STRONG REFS to both texts and compares identity: keying on bare
    # id() was a mangler - a keystroke frees the old buffer string and the
    # replacement often reuses that exact id (same allocator size class), so
    # the stale map (typically None = "no bridging needed") kept hitting and
    # every overlay element sat in unmapped parse coordinates until the edit
    # was reverted. A held ref pins that id, making `is` sound.
    line_map = None
    if buffer_text is not None:
        _src = getattr(code_tree, "source", None)
        if (getattr(ds, "_lv_lmap_src", None) is _src
                and getattr(ds, "_lv_lmap_buf", None) is buffer_text):
            line_map = ds._lv_lmap
        else:
            line_map = _lv_line_map(_src, buffer_text)
            ds._lv_lmap_src, ds._lv_lmap_buf = _src, buffer_text
            ds._lv_lmap = line_map
    _bridge = line_map     # the edit bridge alone - the prune below rounds
    if fold_line_map is not None:
        # Folds collapsed: the diff bridge above ran against the FULL buffer
        # (buffer_text=_fold_full at the call site) - it can only describe ONE
        # contiguous change, and the collapsed folds against the display text
        # read as one single changed region swallowing every visible line
        # between them (markers there mapped to None and vanished). Compose
        # the exact buffer→display fold projection on top instead; it's a
        # fresh closure over the current fold layout, so only the fold-free
        # bridge is cached.
        _bridge = line_map
        if _bridge is None:
            line_map = fold_line_map
        else:
            def line_map(line, _b=_bridge, _f=fold_line_map):
                bl = _b(line)
                return None if bl is None else _f(bl)
    if display_shift is not None:
        # THIS frame's edit (frame-start display text → edited text): the
        # bridge above diffs the parse against the frame-start buffer and
        # the fold layout is the frame-start layout, so without this every
        # overlay stamp below an inserted / deleted line sat one line off
        # for the next frame - the live pills after the cursor flashed away
        # (09-01). Composed last: display line → shifted display line.
        _pre_shift = line_map

        def line_map(line, _i=_pre_shift, _s=display_shift):
            ln = _i(line) if _i is not None else line
            return None if ln is None else _s(ln)
    if line_map is not None:
        # Identity of what the (possibly per-frame) composed map is built
        # from - the overlays' idle-pass memos key on it (a fresh closure
        # per frame would otherwise force a full pass on EVERY frame of the
        # idle widget, i.e. for the whole reparse debounce after every
        # keystroke: the typing lag, 09-01). `_d2b` rides along for the
        # replay's display→parse inverse.
        _fd2b = getattr(fold_line_map, "_d2b", None) if fold_line_map is not None else None
        try:
            line_map._lv_key = (id(_bridge) if _bridge is not None else 0,
                                id(_fd2b) if _fd2b is not None else 0,
                                getattr(display_shift, "_sp", None))
            line_map._d2b = _fd2b
            line_map._lv_pins = (_bridge, _fd2b)
        except AttributeError:
            pass
    seen = set()

    # Viewport prune bounds in buffer-line space (+±1 line slack): a node's
    # span bounds its whole subtree's source region, so a spanned node
    # entirely outside the visible band can drop its subtree without
    # descending - on a large file this is most of the tree, and the full
    # walk (every node, every frame) was the dominant overlay cost. Nodes
    # whose span endpoints don't map (mid-edit region) are processed normally.
    _clip = getattr(ds, 'abs_clip_rect', None)
    _vis_lo = _vis_hi = None
    if (_clip is not None and line_px > 0
            and getattr(ds, '_lv_full_overlay_until', 0) <= Melty.frame_count):
        # _lv_full_overlay_until (stamped by the def-widget's instrumented
        # run): a few FULL passes with the prune off, so below-the-fold
        # widgets render once and write the fresh values into their
        # latched value buffers.
        _vis_lo = (_clip[1] - origin_y) / line_px
        _vis_hi = (_clip[3] - origin_y) / line_px + 2

    _tvw = [0, 0, 0]   # nodes visited, pruned, renderer calls (perf trace)

    # The walk only decides WHICH (node, span, spec) get a renderer call;
    # the calls themselves run every frame below. That decision depends on
    # the tree, the visible band, the fold layout, the edit bridge and the
    # type specs - all unchanged from frame to frame during a selection
    # drag or hover repaint - so the hit list is memoized on the draw_state
    # (~570 nodes visited per frame for ~30 hits, 0.7 ms → a list walk).
    _tv_key = (code_tree, _vis_lo, _vis_hi, fold_d2b, _bridge,
               tuple(id(k) for k, _ in type_specs))
    _tv_memo = getattr(ds, '_tv_walk_memo', None)
    if (_tv_memo is not None and _tv_memo[0][0] is code_tree
            and _tv_memo[0][3] is fold_d2b and _tv_memo[0][4] is _bridge
            and _tv_memo[0][1:3] == _tv_key[1:3] and _tv_memo[0][5] == _tv_key[5]):
        _hits = _tv_memo[1]
    else:
        _hits = []

        def walk(node, depth=0):
            if not isinstance(node, dict) or depth > 64 or id(node) in seen:
                return
            seen.add(id(node))
            _tvw[0] += 1
            span = getattr(node, 'span', None)
            if span is not None and _vis_lo is not None:
                _p0, _p1 = span.start_line, getattr(span, 'end_line', span.start_line)
                # Prune on the BUFFER span through the edit bridge only, then
                # round it onto visible display lines through the fold table:
                # first visible line at/after the start, last at/before the
                # end. The composed line_map returned None for an endpoint
                # hidden in a collapsed fold, and None read as "visit normally"
                # - with the diff-mode folds collapsed that walked 4.4k nodes a
                # frame and pruned 160 (8 ms of every selection frame).
                _b0 = _bridge(_p0) if _bridge else _p0
                _b1 = _bridge(_p1) if _bridge else _p1
                if _b0 is not None and _b1 is not None:
                    if fold_d2b is not None:
                        _i0 = bisect.bisect_left(fold_d2b, _b0 - 1)
                        _i1 = bisect.bisect_right(fold_d2b, _b1 - 1) - 1
                        if _i0 > _i1:
                            _tvw[1] += 1    # the whole subtree is folded away
                            return
                        _b0, _b1 = _i0 + 1, _i1 + 1
                    if _b1 < _vis_lo or _b0 > _vis_hi:
                        _tvw[1] += 1
                        return
            if span is not None:
                for ktype, spec in type_specs:
                    if isinstance(node, ktype):
                        _hits.append((node, span, spec))
                        break
            for v in node.values():
                walk(v, depth + 1)

        walk(code_tree)
        ds._tv_walk_memo = (_tv_key, _hits)

    # Cursor aware, like the inline token views: renderers position
    # themselves with set_cursor_screen_pos (the live_view markers), so the
    # caller's size-decaring area must measure from wherever the body left
    # the cursor - just below the last marker drawn.
    _save_cur = imgui.get_cursor_screen_pos()
    _tvt0 = time.perf_counter()
    for node, span, spec in _hits:
        _sl = line_map(span.start_line) if line_map else span.start_line
        if _sl is None:
            continue   # starts inside the edit region - skip this frame
        y = origin_y + (_sl - 1) * line_px
        h = (span.end_line - span.start_line + 1) * line_px
        x = origin_x + getattr(span, 'start_col', 0) * char_w
        _tvw[2] += 1
        try:
            spec["renderer"](x=x, y=y, w=max(0.0, ds.content_width - (x - origin_x)),
                             h=h, draw_state=ds, char_w=char_w, line_px=line_px,
                             node=node, span=span, root=code_tree,
                             line_offset=line_offset, jump_to=jump_to,
                             line_map=line_map, sel_lo=sel_lo,
                             sel_hi=sel_hi, caret_line=caret_line,
                             live_store=live_store, col_shift=col_shift)
        except Exception as _tv_exc:
            # Swallowed so one bad overlay can't take the editor down - but
            # NOT silently: a renderer that raises every frame never stamps
            # its trailing gaps, the stale-stamp prune below closes them,
            # and the labels flicker with no trace of why (09-01). Once per
            # (renderer, error).
            _tv_key = (getattr(spec["renderer"], "__name__", "?"), repr(_tv_exc))
            if _tv_key not in _TV_RENDERER_ERRORS:
                _TV_RENDERER_ERRORS.add(_tv_key)
                import sys as _tv_sys
                import traceback
                print(f"token view renderer {_tv_key[0]} raised (further "
                      f"repeats suppressed):", file=_tv_sys.stderr)
                traceback.print_exc()
    _tvms = (time.perf_counter() - _tvt0) * 1000.0
    # TEMP perf: one dump per slow overlay pass - who costs most the walk itself
    # or the renderer calls (see the snapshot overlay's own line and its keys).
    if _tvms >= 4.0:
        _ptrace("tv_overlay walk", ms=round(_tvms, 1), nodes=_tvw[0],
                pruned=_tvw[1], calls=_tvw[2])
    # Live-view selection election: the overlays QUEUED every token inside
    # the selection; flush them now - still inside the cursor-neutral bracket
    # - with exactly ONE elected to preview a value window.
    try:
        _flush_selected_markers(draw_state=ds, sel_caret=sel_caret)
    except Exception as e:
        print(f"live_view: selected-marker flush failed: {e!r}")
    imgui.set_cursor_screen_pos(_save_cur)


# --- Symbol usages: highlight & Ctrl+B jump-to-caller -------------------------
# The parse pipeline attaches {name: SymbolUsage} maps to GeneralParse nodes
# under "__symbol_usages__" (see libcst_conversion.populate_symbol_usages /
# _distribute_by_name). Each SymbolUsage carries `sites` — file-absolute
# (line, col) occurrences of the symbol within this view's source — and
# `callers` — cross-project UsageRefs. The editor washes a slight background
# behind every site whose symbol HAS callers, its color climbing a blue→orange
# heat ramp with the user count; Ctrl+B jumps like IntelliJ (the same key as the jump-to
# button) — toward the callers when the definition is in this view, back to the
# definition from a usage site. A single target jumps straight there; several
# open the usage-jump picker (the same latched dropdown as the code-suggestion
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
                    # Shared verify-then-recover (see _site_span): same-line
                    # search first, then nearby lines - index sites are
                    # PENDING coords and lag the buffer by a few lines under
                    # rapid line-count edits.
                    sp = _site_span(text, ln, col, name, line_offset)
                    if sp is None:
                        continue
                    idx, end = sp
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


# Minimum seconds between O(buffer) span/tint recomputes per editor - the
# debounce window _usage_spans and _def_tints serve last-good results inside.
_TINT_RECOMPUTE_MIN_S = 0.25

# NO recompute while typing is hot (last input younger than this): during a
# burst the index sites' coordinate base (PENDING text) lags the new buffer
# by many lines, so a recompute mid-burst both DROPS spans (verification
# before even the 12-line recovery) and MIS-LOCS the ones it recovers -
# while the splice remap of the held set tracks every keystroke exactly. The
# recompute runs once input quiets and pending/index have caught up.
_TINT_INPUT_QUIET_S = 0.35


def _typing_hot():
    last = getattr(Melty, "_last_input_time", 0.0)
    return bool(last) and time.monotonic() - last < _TINT_INPUT_QUIET_S


def _text_splice(old_text, new_text):
    """The single covering splice turning old_text into new_text:
    (p, old_end, d_chars, d_lines, edit_line, old_end_line) — common prefix
    ends at p, common suffix begins at old_end in OLD coords; or None when
    equal. A multi-region diff still yields one covering splice, so remapping
    stays correct (entries inside it are dropped, not misplaced). Used to keep
    the DEBOUNCED wash/highlight span sets glued to the text between real
    recomputes — without it they drew at pre-edit indices and visibly slid off
    the glyphs while typing."""
    if old_text is new_text:
        return None
    lo, ln = len(old_text), len(new_text)
    n = min(lo, ln)
    # Block-compare (C-level slice ==) to narrow down to a short char loop - a
    # Python char loop over a 124k buffer would cost ~10ms per keystroke.
    B = 4096
    p = 0
    while p + B <= n and old_text[p:p + B] == new_text[p:p + B]:
        p += B
    stop = min(n, p + B)
    while p < stop and old_text[p] == new_text[p]:
        p += 1
    if p == n and lo == ln:
        return None
    s = 0
    max_s = n - p
    while s + B <= max_s and old_text[lo - s - B:lo - s] == new_text[ln - s - B:ln - s]:
        s += B
    sstop = min(max_s, s + B)
    while s < sstop and old_text[lo - 1 - s] == new_text[ln - 1 - s]:
        s += 1
    old_end = lo - s
    d = ln - lo
    d_lines = (new_text.count("\n", p, ln - s) - old_text.count("\n", p, old_end))
    edit_line = old_text.count("\n", 0, p)
    old_end_line = old_text.count("\n", 0, old_end)
    return p, old_end, d, d_lines, edit_line, old_end_line


# ---- Statement-anchored wash coordinates -----------------------------------
# The wash/tint results are collected once against a BASE text (often the
# tree's own tree.source) and then rendered against whatever the buffer says
# current. The old bridge was splice arithmetic that DROPPED every entry
# overlapping the edit - destructively, chained per keystroke - so an entry
# lost once never came back until the next line happened to change (the
# missing-tints-after-edit problem). The bridge is now identity-based: each
# entry is ANCHORED to its line's content and, when the tree maps, the
# owning statement's cst key path (LineMap NodeRef.path - unique within a
# scope and independent of line numbers). Resolution re-derives
# current-buffer coordinates FROM THE RAW RESULT on every cursor move: first
# by splice arithmetic, then by line-content identity, verified by a
# bounded monotonic content search. Entries drop only while their anchored
# line no longer exists - and reappear on undo, because resolution always
# restarts from the immutable raw result instead of mutating it.

def _anchor_key_paths(code_tree, lines_needed, base_text):
    """{0-based base line -> cst key path tuple} — the statement identity that
    owns each anchored line, resolved through the tree's LineMap. Best-effort:
    {} when the tree's positions weren't computed from base_text (the dedented
    code-host route) or the map can't build. The path is carried on the anchor
    as the durable statement association; line resolution itself verifies by
    content, which needs no tree."""
    if not lines_needed or not isinstance(code_tree, dict):
        return {}
    if getattr(code_tree, "source", None) is not base_text:
        return {}
    try:
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import LineMap
        lm = LineMap(code_tree)
        out = {}
        for ln in lines_needed:
            ref = lm.node_at_line(ln + 1)
            if ref is not None:
                out[ln] = ref.path
        return out
    except Exception:
        return {}


def _anchor_span_set(entry_lines, extra_lines, base_text, code_tree):
    """Build the (line_map, span_lines) anchor pair: span_lines pins each
    entry to its 0-based base line; line_map holds, per referenced line, the
    line's text (content-identity match target) and its statement key path."""
    lines = base_text.split("\n")
    needed = set(entry_lines)
    needed.update(extra_lines)
    paths = _anchor_key_paths(code_tree, needed, base_text)
    line_map = {ln: (lines[ln] if 0 <= ln < len(lines) else "", paths.get(ln))
                for ln in needed}
    return (line_map, tuple(entry_lines))


def _span_entry_lines(entries, base_text):
    starts = _line_starts(base_text)
    return [bisect.bisect_right(starts, sp[0]) - 1 for sp in entries]


def _resolve_anchor_lines(line_map, base_text, text):
    """{base 0-based line -> current 0-based line | None} for every anchored
    line. None means the line's exact content no longer exists near where the
    edit put it — the only case that drops entries. Monotonic (a floor tracks
    the last resolved line) so two anchors can never cross."""
    if base_text is text:
        return {ln: ln for ln in line_map}
    cur_lines = text.split("\n")
    n = len(cur_lines)
    splice = _text_splice(base_text, text)
    if splice is None:            # Same content, different identity
        return {ln: (ln if 0 <= ln < n else None) for ln in line_map}
    _p, _oe, _d, dl, el, oel = splice
    out = {}
    floor = 0
    for ln in sorted(line_map):
        ltext = line_map[ln][0]
        if ln < el:
            guess = ln
        elif ln > oel:
            guess = ln + dl
        else:
            guess = ln + max(dl, 0)
        got = None
        if floor <= guess < n and cur_lines[guess] == ltext:
            got = guess
        elif not ltext.strip():
            # Whitespace-only anchor (a block's blank end line): content can't
            # identify it - accept the arithmetic guess.
            got = guess if floor <= guess < n else None
        else:
            lo = max(floor, guess - 64)
            hi = min(n, guess + 65)
            best = None
            for j in range(lo, hi):
                if cur_lines[j] == ltext and (
                        best is None or abs(j - guess) < abs(best - guess)):
                    best = j
            got = best
        out[ln] = got
        if got is not None:
            floor = got + 1
    return out


def _resolve_usage_spans(raw, anchors, base_text, text):
    """Project a raw [(start, end, su, at_def)] set (base-text coords) onto the
    current buffer via its anchors. Columns transfer verbatim — a resolved
    line's content is identical by construction."""
    if base_text is text or not raw:
        return raw
    line_map, span_lines = anchors
    m = _resolve_anchor_lines(line_map, base_text, text)
    b_starts = _line_starts(base_text)
    c_starts = _line_starts(text)
    out = []
    for sp, bl in zip(raw, span_lines):
        nl = m.get(bl)
        if nl is None:
            continue
        s = c_starts[nl] + (sp[0] - b_starts[bl])
        out.append((s, s + (sp[1] - sp[0])) + sp[2:])
    return tuple(out)


def _resolve_def_tints(raw, anchors, base_text, text):
    """Project a raw _collect_def_tints 4-tuple (base-text coords) onto the
    current buffer via its anchors. name_tints is coordinate-free and passes
    through. A block whose end line vanished keeps its head and carries its
    old extent, clamped — matching the old stretch behavior for edits inside
    a tinted block."""
    if base_text is text:
        return raw
    blocks, spans, line_tints, name_tints = raw
    line_map, span_lines = anchors
    m = _resolve_anchor_lines(line_map, base_text, text)
    b_starts = _line_starts(base_text)
    c_starts = _line_starts(text)
    n_cur = len(c_starts)

    def _reidx(idx, bl, nl):
        return c_starts[nl] + (idx - b_starts[bl])

    nb = []
    for (bl, idx, bend, tint) in blocks:
        nl = m.get(bl)
        if nl is None:
            continue
        ne = m.get(bend)
        if ne is None or ne < nl:
            ne = min(bend + (nl - bl), n_cur - 1)
        nb.append((nl, _reidx(idx, bl, nl), ne, tint))

    nsp = []
    for sp, bl in zip(spans, span_lines):
        nl = m.get(bl)
        if nl is None:
            continue
        s = _reidx(sp[0], bl, nl)
        nsp.append((s, s + (sp[1] - sp[0])) + sp[2:])

    nlt = []
    for (ln, rgb, sc, si, ei) in line_tints:
        nl = m.get(ln)
        if nl is None:
            continue
        nlt.append((nl, rgb, sc, _reidx(si, ln, nl), _reidx(ei, ln, nl)))
    return (tuple(nb), tuple(nsp), tuple(nlt), name_tints)


def _display_splice_shift(sp, text_now, blocks=(), line_tints=(), spans=(),
                          comments=()):
    """One-frame arithmetic remap of already-display-projected overlay coords
    across THIS body run's edit splice (frame-start display text -> edited
    display text). Needed only while a fold is collapsed: there the tint/wash
    resolve targets the frame-start FULL buffer (the fold display remap is
    built on the frame-start layout, so a post-edit resolve target would be
    remapped wrongly anyway), which leaves every wash lagging the glyphs by
    exactly the current edit for one frame — the "off by the last typed/
    deleted character" flicker. Entries starting inside the edited region
    drop for the frame; the real content-anchored resolve re-finds them on
    the next frame's buffer. Head lines are re-derived from the corrected
    index (a stored-line shift guess would misplace entries on a split
    line); block END lines have no index, so they take the line guess."""
    p, oe, d, dl, el, oel = sp
    starts = _line_starts(text_now)

    def _ix(i):
        if i >= oe:
            return i + d
        if i < p:       # strictly before: i == p means a deletion (p < oe)
            return i    # starts on removed content and must drop with it -
        return None     # pure insertion at i (oe == p) shifts via i >= oe

    def _ln_of(i):
        return bisect.bisect_right(starts, i) - 1

    def _ln_guess(ln):
        return ln + dl if ln > oel else (ln if ln < el else ln + max(dl, 0))

    nb = []
    for (bl, idx, bend, tt) in blocks:
        ni = _ix(idx)
        if ni is None:
            continue
        nb.append((_ln_of(ni), ni, _ln_guess(bend), tt))
    nlt = []
    for (ln, rgb, sc, si, ei) in line_tints:
        nsi = _ix(si)
        if nsi is None:
            continue
        nei = _ix(ei)
        nlt.append((_ln_of(nsi), rgb, sc, nsi,
                    nei if nei is not None else nsi + (ei - si)))
    nsp = []
    for sp_ in spans:
        ns = _ix(sp_[0])
        if ns is None:
            continue
        nsp.append((ns, ns + (sp_[1] - sp_[0])) + tuple(sp_[2:]))
    nc = []
    for (si, ei, rgb) in comments:
        ns = _ix(si)
        if ns is None:
            continue
        ne = _ix(ei)
        nc.append((ns, ne if ne is not None else ns + (ei - si), rgb))
    return tuple(nb), tuple(nlt), tuple(nsp), tuple(nc)


def _display_edit_splice(old, new):
    """Splice tuple (p, oe, d, dl, el, oel) for _display_splice_shift, from
    the frame-start display text to this body run's edited display text:
    common-prefix end `p`, old-text splice end `oe`, char delta `d`, line
    delta `dl`, and the old text's splice start/end lines `el`/`oel`. Block
    slice compares (C speed) with a char-loop only inside the boundary block,
    so the O(buffer) scan stays cheap; runs on edit frames only."""
    n = min(len(old), len(new))
    blk = 4096
    p = 0
    while p < n and old[p:p + blk] == new[p:p + blk]:
        p += blk
    while p < n and old[p] == new[p]:
        p += 1
    p = min(p, n)
    lim = n - p
    s = 0
    while (s + blk <= lim
           and old[len(old) - s - blk:len(old) - s]
           == new[len(new) - s - blk:len(new) - s]):
        s += blk
    while s < lim and old[len(old) - 1 - s] == new[len(new) - 1 - s]:
        s += 1
    oe = len(old) - s
    return (p, oe, len(new) - len(old),
            new.count('\n') - old.count('\n'),
            old.count('\n', 0, p), old.count('\n', 0, oe))


def _usage_spans(ds, text, code_tree, line_offset=0, view_path=None):
    """Cached-per-(code_tree, text) wrapper around _collect_usage_spans. The
    top-level __symbol_usages__ map's identity rides in the key: the background
    usage pass fills it in-place on an already-rendered code_tree (fresh dict
    per compute), so its arrival must bust the cache even though the tree and
    text are unchanged.

    Debounced like _def_tints: the collect walks every usage in the buffer
    (~15ms on a 2200-line file) on the render thread, so a key change inside
    _TINT_RECOMPUTE_MIN_S serves the last-good span set and re-requests a
    frame for the trailing recompute — per-keystroke cost becomes a few
    recomputes per second."""
    if code_tree is None:
        return ()
    su_top = code_tree.get("__symbol_usages__") if isinstance(code_tree, dict) else None
    # NO text in the key: a text-only change is handled CORRECTLY by the splice
    # remap below, while a recompute against the same (stale) tree re-verifies
    # stale index sites against the new text and DROPS everything below an
    # inserted newline - the highlights-blink-out-on-Enter bug. Recompute ONLY
    # when a fresh tree from background attach actually arrives (identity change),
    # which carries refreshed positions and rebuilds exactly.
    key = (id(code_tree), id(su_top), line_offset, str(view_path))
    _old_key = getattr(ds, '_usage_spans_key', None)
    # Cold-migration: a hotswapped ds carrying only the legacy resolved tuple -
    # recompute rather than serving spans the new resolve path can't re-anchor.
    if _old_key != key or getattr(ds, "_usage_spans_raw", None) is None:
        now = time.monotonic()
        # One layer in typing: an incremental merge ships a fresh tree
        # whose per-node __symbol_usages__ hasn't been attached yet
        # (the carried flat map on gp.symbol_usage is the marker - see
        # _carry_symbols). Collecting NOW would find no sus and replace the
        # held spans with nothing - hold the last-good spans (splice-remapped
        # below) until the frame-boundary attach lands and busts the key.
        # The carry marker is the `_needs_distribute` flag + a carried DICT
        # (_carry_symbols). NOT a simple truthy `symbol_usage`: a tree with no
        # symbol layer holds the default `[None]` (truthy), which would it
        # "pending" forever and - with request_render() - turned every
        # re-render of the editor into a frame: hover → repaint → request →
        # frame → hover ... (thousands of hover_change notes, no input).
        _su_pending = (su_top is None
                       and getattr(code_tree, "_needs_distribute", False)
                       and isinstance(getattr(code_tree, "symbol_usage", None), dict))
        _held = getattr(ds, "_usage_spans", None) is not None
        if _held and (_typing_hot()
                      or now - getattr(ds, "_usage_spans_time", 0.0) < _TINT_RECOMPUTE_MIN_S):
            request_render()   # typing/debounced: serve held (remapped below), retry later
        elif _held and _su_pending:
            pass   # hold; the frame-boundary attach (attach_to_render) wakes the loop & busts the key
        else:
            # Collect against the TREE'S OWN text when the buffer has moved on:
            # a reparse that lands mid-burst carries sites for the text it was
            # parsed from (gp.source) - verifying these against the NEWER buffer
            # dropped every span below the edits (highlights blinking out while
            # holding Enter). Collecting on the matching text base instead;
            # the splice remap then shifts the result to the current buffer
            # exactly. Guard: a totally-different source (the DEDENTED
            # code-host route) yields one giant covering splice - fall back to
            # the buffer collect there instead of dropping everything.
            _base = text
            _src = getattr(code_tree, 'source', None)
            if isinstance(_src, str) and _src != text:
                _cand = _text_splice(_src, text)
                if _cand is not None and (_cand[1] - _cand[0]) <= 512 and abs(_cand[2]) <= 512:
                    _base = _src
            try:
                _fresh = _collect_usage_spans(code_tree, _base, line_offset, view_path)
            except Exception:
                _fresh = ()
            # The raw result + anchors are IMMUTABLE in base coords; every
            # text change re-resolves from them (see the anchor block above) -
            # no destructive per-keystroke remap here.
            ds._usage_spans_raw = _fresh
            ds._usage_spans_base = _base
            ds._usage_spans_anchors = _anchor_span_set(
                _span_entry_lines(_fresh, _base), (), _base, code_tree)
            _resolved = _resolve_usage_spans(
                _fresh, ds._usage_spans_anchors, _base, text)
            # DEBUG timeline: a recompute that sheds >30% of the held spans is
            # the blink-out signature - name WHICH key component moved and
            # which text base was collected on.
            _prev_n = len(getattr(ds, "_usage_spans", ()) or ())
            if _prev_n >= 10 and len(_resolved) < _prev_n * 0.7:
                _why = ("cold" if _old_key is None else ",".join(
                    n for n, i in (("tree", 0), ("su", 1), ("off", 2), ("path", 3))
                    if _old_key[i] != key[i]))
                _ptrace("usage spans DROP on recompute", prev=_prev_n,
                        new=len(_resolved), changed=_why,
                        base=("source" if _base is not text else "text"),
                        src_is_str=isinstance(_src, str))
            ds._usage_spans = _resolved
            ds._usage_spans_key = key
            ds._usage_spans_time = now
            ds._usage_spans_text = text
            ds._usage_tc = {}   # per-(su, at_def) jump-target counts; valid per span set
    # Text drift since the held set was computed (typing between reparses):
    # re-resolve from the immutable raw result so entries displaced by the
    # edit are re-found by content and never chained-splice-dropped.
    prev_text = getattr(ds, "_usage_spans_text", None)
    if (prev_text is not None and prev_text is not text
            and getattr(ds, "_usage_spans", None) is not None):
        _raw = getattr(ds, "_usage_spans_raw", None)
        if _raw is not None:
            _n0 = len(ds._usage_spans)
            ds._usage_spans = _resolve_usage_spans(
                _raw, ds._usage_spans_anchors, ds._usage_spans_base, text)
            if _n0 >= 10 and len(ds._usage_spans) < _n0 * 0.7:
                _ptrace("usage spans DROP on resolve", prev=_n0,
                        new=len(ds._usage_spans))
        ds._usage_spans_text = text
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
    # A definition with no path OR no real line (0 = unresolved fallback) is not a
    # jump target - line 0 scrolled the target file to its very top.
    if d is not None and (getattr(d, 'path', None) is None
                          or (getattr(d, 'line', 0) or 0) <= 0):
        d = None
    callers = [c for c in (getattr(su, 'callers', None) or ())
               if getattr(c, 'path', None) is not None]
    # Self-caller filter for CACHED symbol: entries computed before
    # _rebuild_symbol_usages started dropping the declaration-as-caller
    # artifact (pickle-restored / cold-seeded) may still carry the symbol
    # itself as a caller - filter it at display time too so the dropdown
    # never leads with the row you clicked.
    if d is not None:
        _dl, _dp = getattr(d, 'line', None), str(getattr(d, 'path', None))
        callers = [c for c in callers
                   if not (c.line == _dl and str(c.path) == _dp)]

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


def _enclosing_editor_window(ds):
    """The code-editor WINDOW draw_state a text editor renders inside, or
    None (editors outside draw_code_editor — chain views, search boxes).
    Jumps fired inside an editor instance pass this so the target opens in
    the SAME instance instead of hopping to the primary window."""
    w, n = ds, 0
    while w is not None and n < 8:
        if 'draw_code_editor' in (getattr(w, 'name', '') or ''):
            return w
        nw = getattr(w, 'parent_window', None)
        if nw is w:
            return None
        w, n = nw, n + 1
    return None


def _open_usage_ref(ref, token=None, editor_window=None):
    """Open one UsageRef (Ctrl+B) in the in-app code editor: opens the file's
    tab, summons the editor window, and stashes the line on
    OpenFiles.jump_to_line — draw_code_editor consumes it to place the caret
    (the editor's cursor-follow scroll then brings it into view). `token`
    rides along on OpenFiles.jump_to_token so the caret lands ON the symbol
    rather than at the line's first code character; `editor_window` keeps the
    jump in the originating editor instance."""
    from src.lsd.gl_gui.view.playground.open_files import open_in_editor
    open_in_editor(str(ref.path), line_number=getattr(ref, 'line', None),
                   token=token, editor_window=editor_window)


def _focus_in_context_menu_over(editor_ds, max_steps=64):
    """True if the view holding TEXT focus sits inside an open context-menu
    window whose target chain leads back to ``editor_ds``. The usage-jump
    picker is gated on the editor owning text focus, so a context menu opened
    ON the picker (to inspect/edit it) used to kill the popover the moment the
    menu's search box grabbed focus — and the menu, rendered from the popover's
    subtree, died with it. A context-menu window is recognized statelessly by
    the existing mutual link (the menu's input_value is its target draw_state,
    whose ``context_menu_ds`` points back at the menu); the walk climbs
    _parent/parent_window and hops menu→target, and only a walk that crossed at
    least one such hop counts — plain in-subtree focus stays on the normal
    ``text_focused_ds is draw_state`` gate."""
    node = Melty.text_focused_ds
    if node is None or node is editor_ds:
        return False
    # seen is keyed on (node, via_menu): the same ancestor can be reachable both
    # through a menu hop and through the normal parent chain (a context menu's
    # _parent is its target), and whichever path pops first must not block the
    # other - only a via_menu=True arrival at editor_ds returns True.
    stack, seen = [(node, False)], set()
    while stack and len(seen) < 2 * max_steps:
        node, via_menu = stack.pop()
        if node is None or (id(node), via_menu) in seen:
            continue
        seen.add((id(node), via_menu))
        if node is editor_ds:
            if via_menu:
                return True
            continue
        target = getattr(node, "_raw_input_value", None)
        if target is not None and getattr(target, "context_menu_ds", None) is node:
            stack.append((target, True))
        parent = getattr(node, "_parent", None)
        pwin = getattr(node, "parent_window", None)
        if parent is not None and parent is not node:
            stack.append((parent, via_menu))
        if pwin is not None and pwin is not node:
            stack.append((pwin, via_menu))
    return False


def _uj_log(msg):

    """Usage-jump debug trail (event-driven, not per-frame): every Ctrl+B
    press, gutter click, span-scan outcome, and pick lands here with the
    frame count. `tail -f /tmp/uj_debug.log` while reproducing."""
    try:
        with open("/tmp/uj_debug.log", "a") as f:
            f.write(f"[f{Melty.frame_count}] {msg}\n")
    except OSError:
        pass


def _shorten_dotted(s):
    """Cap a dotted name at 2 dots: deep paths (`Toggles.A.B.attr`) collapse
    to their last three parts with a leading dot (`.A.B.attr`) so picker rows
    stay scannable."""
    parts = s.split('.')
    return s if len(parts) <= 3 else '.' + '.'.join(parts[-3:])


# --- Ctrl+B usage-graph consistency check ------------------------------------
# The fresh single-line recheck (usage_recompute) recomputes exactly what the
# background graph should already hold for the caret's line. Any disagreement
# IS the stuck-stale-graph bug observed at the moment it's reproduced - so
# every Ctrl+B recheck diffs the two and dumps a full forensic block here.
_USAGE_MISMATCH_LOG = "/tmp/usage_graph_mismatch.log"


def _tree_usages_on_line(tree, file_line):
    """{display name: SymbolUsage} for every symbol in the tree's nested
    __symbol_usages__ maps with a site on file-absolute `file_line` — the
    EXISTING graph's view of that line, unfiltered (no callers requirement).
    Local-variable entries (key contains \\x1f) are skipped: a single-line
    recheck can't see a local whose binding sits outside the line, so they'd
    be permanent false positives — and they have no cross-file callers to go
    stale anyway."""
    out = {}
    seen = set()

    def walk(node, depth=0):
        if not isinstance(node, dict) or depth > 64 or id(node) in seen:
            return
        seen.add(id(node))
        su_map = node.get("__symbol_usages__")
        if isinstance(su_map, dict):
            for key, su in su_map.items():
                if isinstance(key, str) and "\x1f" in key:
                    continue
                for site in getattr(su, 'sites', None) or ():
                    if site[0] == file_line:
                        out[getattr(su, 'name', key) or key] = su
                        break
        for k, v in node.items():
            if k not in ("__cst__", "__symbol_usages__"):
                walk(v, depth + 1)

    walk(tree)
    return out


def _usage_ref_tup(r):
    """UsageRef -> a comparable/printable (path, line, column) tuple."""
    if r is None:
        return None
    return (str(getattr(r, 'path', None)), getattr(r, 'line', None),
            getattr(r, 'column', None))


def _diff_usage_maps(old, new, file_line):
    """Human-readable difference lines between the existing graph's symbols
    on `file_line` (`old`) and the freshly recomputed ones (`new`); [] =
    consistent. Compares membership, the line's sites, the definition, and
    the caller sets."""
    diffs = []
    for nm in sorted(old.keys() - new.keys()):
        diffs.append(f"MISSING in fresh: {nm!r} — tree has it, recheck did not resolve it")
    for nm in sorted(new.keys() - old.keys()):
        diffs.append(f"MISSING in tree: {nm!r} — recheck resolved it, graph lacks it")
    for nm in sorted(old.keys() & new.keys()):
        o, n = old[nm], new[nm]
        os_ = sorted(tuple(s) for s in (getattr(o, 'sites', None) or ())
                     if s[0] == file_line)
        ns_ = sorted(tuple(s) for s in (getattr(n, 'sites', None) or ())
                     if s[0] == file_line)
        if os_ != ns_:
            diffs.append(f"SITES differ for {nm!r}: tree={os_} fresh={ns_}")
        od = _usage_ref_tup(getattr(o, 'definition', None))
        nd = _usage_ref_tup(getattr(n, 'definition', None))
        if od != nd:
            diffs.append(f"DEFINITION differs for {nm!r}: tree={od} fresh={nd}")
        oc = sorted(_usage_ref_tup(c) for c in getattr(o, 'callers', None) or ())
        nc = sorted(_usage_ref_tup(c) for c in getattr(n, 'callers', None) or ())
        if oc != nc:
            only_o = [c for c in oc if c not in set(nc)]
            only_n = [c for c in nc if c not in set(oc)]
            diffs.append(f"CALLERS differ for {nm!r} "
                         f"(tree={len(oc)} fresh={len(nc)}): "
                         f"only-tree={only_o[:20]} only-fresh={only_n[:20]}")
    return diffs


def _dump_symbol_usage(su, cap=40):
    """Multi-line forensic dump of one SymbolUsage — everything the graph
    knows: all sites, the definition, and (capped) callers."""
    if su is None:
        return "      <absent>"
    lines = [f"      sites={sorted(tuple(s) for s in (getattr(su, 'sites', None) or ()))}",
             f"      definition={_usage_ref_tup(getattr(su, 'definition', None))}"]
    callers = [_usage_ref_tup(c) for c in getattr(su, 'callers', None) or ()]
    lines.append(f"      callers ({len(callers)}):")
    for c in sorted(callers)[:cap]:
        lines.append(f"        {c}")
    if len(callers) > cap:
        lines.append(f"        ... {len(callers) - cap} more")
    return "\n".join(lines)


def _log_usage_mismatch(vpath, file_line, diffs, old, new, ctx):
    """PROMINENT mismatch report: append a self-contained forensic block to
    _USAGE_MISMATCH_LOG (context + verdicts + full old/new dumps of every
    symbol involved — debuggable from the log alone) and bang a red banner
    on stdout pointing at it. Never raises — the check must not break the
    jump it rides on."""
    try:
        import datetime
        names = sorted(set(old) | set(new))
        block = ["=" * 78,
                 f"USAGE GRAPH MISMATCH  {datetime.datetime.now().isoformat(timespec='seconds')}",
                 f"  file={vpath}  line={file_line}",
                 "  " + "  ".join(f"{k}={v}" for k, v in ctx.items()),
                 "  --- differences " + "-" * 40]
        block += [f"  {d}" for d in diffs]
        block.append("  --- full state (tree = existing graph, fresh = recheck) " + "-" * 10)
        for nm in names:
            block.append(f"    {nm!r}:")
            block.append("    tree:")
            block.append(_dump_symbol_usage(old.get(nm)))
            block.append("    fresh:")
            block.append(_dump_symbol_usage(new.get(nm)))
        block.append("")
        with open(_USAGE_MISMATCH_LOG, "a") as f:
            f.write("\n".join(block) + "\n")
        print(f"\033[1;97;41m[usage-mismatch] {getattr(vpath, 'name', vpath)}"
              f":{file_line} — {len(diffs)} difference(s) between the usage "
              f"graph and the fresh recheck — full dump in "
              f"{_USAGE_MISMATCH_LOG}\033[0m")
        _uj_log(f"MISMATCH line={file_line} diffs={len(diffs)} "
                f"-> {_USAGE_MISMATCH_LOG}")
    except Exception:
        pass


def _uj_file_tint(p):
    """The file's FileMeta tint for a picker row (same source the editor tabs
    use), or None."""
    root = getattr(Melty.vis, 'root', None)
    meta = getattr(getattr(root, 'file_meta_collection', None), 'file_meta',
                   None) or {}
    entry = meta.get(str(p)) if p is not None else None
    t = entry.get('tint') if isinstance(entry, dict) else None
    return tuple(t) if t else None


def _usage_ref_items(targets, prefix=""):
    """({label: UsageRef}, {UsageRef: tag}, {UsageRef: (path, line, code)})
    rows for the usage-jump picker: the label is the user's enclosing scope
    (optionally prefixed with the symbol name for merged multi-symbol lists),
    the dim right-aligned tag its file (GlobalSearch layout: the line number
    renders in the code row's own gutter, so the tag drops the :line — it
    keeps it only for rows with no code preview). The third map feeds
    draw_dd_menu's `row_code` — the ACTUAL code line at each site, rendered
    GlobalSearch-style through the real editor. Line text comes from
    PendingSave.current_file_text (pending truth — the same coordinates the
    refs and the jump use), read once per FILE per picker-open, never per
    frame. Duplicate scope labels get a numeric suffix (dict keys feed
    draw_dd_menu, so they must be unique)."""
    from pathlib import Path as _P
    from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    items, tags, code = {}, {}, {}
    file_lines = {}   # str(p) -> splitlines() of the pending text
    for ref in targets:
        scope = (getattr(ref, 'scope', '') or getattr(ref, 'module_name', '')
                 or '<module>')
        scope = _shorten_dotted(scope)
        base = f"{prefix}{scope}"
        # Dedup by INVISIBLEING spaces, not a visible " (n)" counter -
        # dict keys feed draw_dd_menu so they must be unique, but the counter
        # read as noise next to the code previews. The row renderer rstrips
        # for display; trailing spaces render as nothing either way.
        label = base
        while label in items:
            label += " "
        items[label] = ref
        p = getattr(ref, 'path', None)
        ln = getattr(ref, 'line', None)
        line_text = None
        if p is not None and ln:
            key = str(p)
            lines = file_lines.get(key)
            if lines is None:
                try:
                    t = PendingSave.current_file_text(_P(key))
                except Exception:
                    t = None
                lines = t.split('\n') if isinstance(t, str) else []
                file_lines[key] = lines
            if 0 < ln <= len(lines):
                line_text = lines[ln - 1].strip()
        if line_text:
            code[ref] = (str(p), ln, line_text)
            tags[ref] = p.name
        else:
            tags[ref] = f"{p.name}:{ref.line}" if p is not None else f":{ref.line}"
    return items, tags, code


# --- Definition tints: block wash behind tinted class/def bodies + a matching
# wash behind every occurrence of a symbol whose DEFINITION carries a tint ----
# A definition's tint can be in three source forms (the same stores the lens
# system edits): a `@defaults(tint=...)` decorator, a `# [tint=...]` override
# comment above the def, or a `tint = (...)` class-body assignment. For defs
# inside THIS buffer the tint is read straight off the parsed code_tree node
# (decorators / __overrides__ / tint key); for defs in OTHER buffers a small
# mtime-keyed per-file cache scans the few lines around the definition. The
# goal: glance at any `Toggles` occurrence and see the color of its definition.


def _is_color(v):
    return (isinstance(v, (tuple, list)) and 3 <= len(v) <= 4
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v))


def _node_tint(node):
    """The tint a parsed class/def dict node carries, or None. Checks the
    override comment first (the explicit per-instance store), then the
    @defaults decorator, then a class-body `tint = (...)` assignment."""
    ov = node.get("__overrides__")
    if isinstance(ov, dict) and _is_color(ov.get("tint")):
        return tuple(ov["tint"])
    dec = node.get("decorators")
    if isinstance(dec, dict):
        # Any decorator carrying tint= counts (@defaults, @window, ...) - the
        # disk scanner (_tint_from_defaults_line) only matches any '@' line.
        for df in dec.values():
            if isinstance(df, dict) and _is_color(df.get("tint")):
                return tuple(df["tint"])
    if _is_color(node.get("tint")):
        return tuple(node["tint"])
    return None


def _tint_from_defaults_line(s):
    """Extract tint=(...) from a single `@defaults(...)` decorator line."""
    import ast as _ast
    m = re.search(r"\btint\s*=\s*\(", s)
    if not m:
        return None
    start = m.end() - 1
    depth = 0
    for j in range(start, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                try:
                    v = _ast.literal_eval(s[start:j + 1])
                except (ValueError, SyntaxError):
                    return None
                return tuple(v) if _is_color(v) else None
    return None


def _snap_to_def(lines, i, limit=40):
    """Index of the actual `class`/`def` line for a definition recorded at
    line index `i` — index tiers disagree on whether a decorated definition's
    line is the STATEMENT start (the first decorator, e.g. `@window(...)`) or
    the class/def line itself. Walks down over decorator lines (paren-balanced,
    so multi-line decorators are consumed) and comments; returns `i` unchanged
    when the line isn't part of a decorated definition at all."""
    j, depth = i, 0
    while 0 <= j < len(lines) and j < i + limit:
        s = lines[j].strip()
        if depth == 0:
            if re.match(r"(?:class|def)\s", s):
                return j
            if not (s.startswith("@") or s.startswith("#")):
                return i
        depth += s.count("(") - s.count(")")
        j += 1
    return i


def _scan_def_tint_lines(lines, line, name=None, _depth=0):
    """(tint, src_line) for the definition at 1-based `line` of `lines`, in
    SOURCE form — or None. The recorded line is first snapped to the real
    class/def line, then the decorator/comment run above is checked for
    `@...(tint=...)` / `# [tint=...]`, then a short downward body scan for a
    `tint = (...)` class var. ONLY explicit tints count — a member without
    its own tint returns None (no enclosing-class inheritance; the class's
    color is its block wash, and inherited symbol washes were redundant).

    `src_line` is the (snapped, 1-based) def line the tint was read at.
    Callers use it structurally — a tinted class's own occurrences are
    redundant inside its block wash and are never emitted there, independent
    of color values (color-equality filtering popped during tint drags, when
    the live tree and the pending source text disagree for a frame)."""
    import ast as _ast
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import _parse_override_comment
    i = line - 1
    if not (0 <= i < len(lines)):
        return None
    i = _snap_to_def(lines, i)
    # A class/def line's tint belongs ONLY to the def that line defines. A
    # local that merely LIVES on the line - a parameter, whose definition
    # resolves to the def line - must not read the def's decorator/comment
    # tint as its own (that leak propagated a tinted top_func's color onto
    # every local whose binding read one of its parameters).
    if name is not None:
        dm = re.match(r"\s*(?:async\s+)?(?:class|def)\s+([A-Za-z_]\w*)", lines[i])
        if dm and dm.group(1) != str(name).rsplit(".", 1)[-1]:
            return None
    # Decorator tints belong to definitions only. When the anchor line is NOT
    # a class/def line (e.g. the assignment sweep matched `is_tree=False,` - a
    # decorator's CONTINUATION line, or any name=None lookup on a decorated
    # statement), an '@' line above is a decorator context this line sits
    # inside, never this line's own tint store.
    _is_def_line = re.match(r"\s*(?:async\s+)?(?:class|def)\s", lines[i]) is not None
    # Inline trailing override comment on the def line itself - the most
    # common store `x = a * b  # [tint=(...)]`), checked first so an explicit
    # comment tint always beats anything else (including the assignment-
    # propagation blend, which only runs when no explicit tint resolves).
    tm = re.search(r"#.*$", lines[i])
    if tm:
        parsed = _parse_override_comment(tm.group(0))
        if parsed and _is_color(parsed.get("tint")):
            return tuple(parsed["tint"]), i + 1
    comment = []
    j = i - 1
    while j >= 0:
        s = lines[j].strip()
        if s.startswith("#"):
            comment.append(s)
            j -= 1
            continue
        if s.startswith("@"):
            if not _is_def_line:
                # In a decorator's argument list - a kwarg line owns no
                # tint, and comments further up belong to the decorated def.
                break
            t = _tint_from_defaults_line(s)
            if t is not None:
                return t, i + 1
            j -= 1
            continue
        # A class/def line above is the OWNERSHIP boundary - stop; the
        # enclosing-class check below handles inheritance (and reports the
        # class's line as src). Without this, the decorator lookback would
        # read a decorator on the ENCLOSING class as this def's own tint.
        if re.match(r"\s*(?:class|def)\s", lines[j]):
            break
        # Possibly a continuation line of a multi-line decorator; search up a
        # short window for the '@' line that opens it and check the joined
        # statement, so a comment-line decorator above a multi-line one still reads.
        q = j - 1
        while q >= 0 and q >= j - 20 and not lines[q].strip().startswith("@"):
            if not lines[q].strip() or re.match(r"\s*(?:class|def)\s", lines[q]):
                q = -1
                break
            q -= 1
        if q >= 0 and lines[q].strip().startswith("@"):
            if not _is_def_line:
                break                       # same decorator-context guard as above
            t = _tint_from_defaults_line(" ".join(l.strip() for l in lines[q:j + 1]))
            if t is not None:
                return t, i + 1
            j = q - 1
            continue
        break
    if comment:
        # The override comment is the TAIL of the comment run (adjacent to
        # the def); prose comments above it (`# some comment` stacked on top of
        # `# [tint=...]`) would poison any whole-run parse - try suffixes.
        run = list(reversed(comment))
        for k in range(len(run)):
            parsed = _parse_override_comment("\n".join(run[k:]))
            if parsed and _is_color(parsed.get("tint")):
                return tuple(parsed["tint"]), i + 1
    indent = len(lines[i]) - len(lines[i].lstrip())
    if re.match(r"\s*(?:class|def)\s", lines[i]):
        for k in range(i + 1, min(i + 40, len(lines))):
            s = lines[k]
            if not s.strip():
                continue
            if len(s) - len(s.lstrip()) <= indent:
                break
            m = re.match(r"\s*tint\s*=\s*(\(.*\))\s*(#.*)?$", s)
            if m:
                try:
                    v = _ast.literal_eval(m.group(1))
                except (ValueError, SyntaxError):
                    break
                if _is_color(v):
                    return tuple(v), i + 1
                break
            if re.match(r"\s*(?:def|class)\s", s):
                break
    # Deliberately NO enclosing-class inheritance; a member without its own
    # tint remains untinted - the class's color is communicated by its BLOCK
    # wash alone. Inherited symbol washes proved purely redundant (and fed
    # the same redundancy into assignment propagation).
    return None


# symbol -> (def-shape pattern, word pattern) for _verify_def_line; bounded,
# reset on hotswap.
_DEF_LINE_PATS = {}

# path-str -> realpath. Paths are stable for a session; per-rebuild realpath
# syscalls were a measured keystroke cost. Bounded, reset on hotswap.
_REALPATH_CACHE = {}


def _real(p):
    r = _REALPATH_CACHE.get(p)
    if r is None:
        if len(_REALPATH_CACHE) > 4096:
            _REALPATH_CACHE.clear()
        import os
        try:
            r = os.path.realpath(p)
        except OSError:
            r = p
        _REALPATH_CACHE[p] = r
    return r


def _verify_def_line(lines, line, name):
    """Verify-then-recover for a recorded definition line (the def-side twin
    of _site_span): index positions go stale when the definition FILE is
    edited, and a stale line must not silently resolve to the wrong tint (the
    ownership fallback would hand a shifted field its CLASS's color). If the
    symbol's last component isn't on the recorded line, find the nearest
    def-shaped line (`class X` / `def X` / `X = ...` / `X: ...`) that names it
    within a small window; else keep the original."""
    if not name:
        return line
    comp = str(name).rsplit(".", 1)[-1]
    # Per-name pattern memo: the compiled f-string patterns thrash re's own
    # 512-entry cache - at one _verify_def_line per in-file symbol per
    # keystroke, uncached compiles alone cost ~70ms on a big buffer.
    pats = _DEF_LINE_PATS.get(comp)
    if pats is None:
        if len(_DEF_LINE_PATS) > 4096:
            _DEF_LINE_PATS.clear()
        pats = (re.compile(rf"^\s*(?:(?:class|def)\s+{re.escape(comp)}\b|{re.escape(comp)}\s*[:=][^=])"),
                re.compile(rf"\b{re.escape(comp)}\b"))
        _DEF_LINE_PATS[comp] = pats
    pat, word = pats
    i = line - 1
    if 0 <= i < len(lines) and word.search(lines[i]):
        return line
    for off in range(1, 61):
        for j in (i - off, i + off):
            if 0 <= j < len(lines) and pat.match(lines[j]):
                return j + 1
    return line


# (total_gen, {realpath: gen}) - updated only when the total moves; the old
# per-call sum scan over pending entries ran once per cross-file symbol
# per keystroke.
_PENDING_GEN_MAP = (None, {})


def _pending_gen_of(path):
    """PendingSave edit generation for `path` — 0 when it has no queued edits.
    queue_save keys _pending_gen by the address's OWN path value while def
    paths arrive resolved, so lookups go through a realpath-keyed snapshot
    map, refreshed only when the total generation moves."""
    global _PENDING_GEN_MAP
    try:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    except Exception:
        return 0
    gens = PendingSave._pending_gen
    if not gens:
        return 0
    total = sum(gens.values())
    if _PENDING_GEN_MAP[0] != total:
        m = {}
        for k, v in list(gens.items()):
            rp = _real(str(k))
            m[rp] = m.get(rp, 0) + v
        _PENDING_GEN_MAP = (total, m)
    return _PENDING_GEN_MAP[1].get(_real(str(path)), 0)


def _pending_total_gen():
    """Sum of ALL files' PendingSave edit generations — one cheap monotonic
    number that moves whenever any in-app deferred edit lands, used in the
    _def_tints memo key so a tint-comment edit in one file refreshes washes
    in editors viewing OTHER files. Read per frame; the dict is tiny."""
    try:
            
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        return sum(PendingSave._pending_gen.values())
    except Exception:
        return 0


# Twin snapshot map for the TINT-relevant generations (same lazy
# aggregation as _PENDING_GEN_MAP above).
_TINT_GEN_MAP = (None, {})


def _tint_gens():
    """PendingSave's tint-relevant per-file counters — bumped only when a
    queued edit changed a tint-carrying line or the span's line count (see
    PendingSave._tint_relevant_change). The _def_tints memo keys on THESE
    instead of the raw pending gens so a #[...] param drag in another file
    no longer busts every editor's tint memo per frame — that mid-drag
    recompute (against a lagging tree) is what randomly dropped propagated
    washes. Falls back to the raw gens when a pre-hotswap PendingSave has
    no _tint_gen yet (conservative: old behavior)."""
    try:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
    except Exception:
        return None
    gens = getattr(PendingSave, "_tint_gen", None)
    return gens if gens is not None else PendingSave._pending_gen


def _tint_gen_of(path):
    """Tint-relevant edit generation for `path` — 0 when none queued."""
    global _TINT_GEN_MAP
    gens = _tint_gens()
    if not gens:
        return 0
    total = sum(gens.values())
    if _TINT_GEN_MAP[0] != total:
        m = {}
        for k, v in list(gens.items()):
            rp = _real(str(k))
            m[rp] = m.get(rp, 0) + v
        _TINT_GEN_MAP = (total, m)
    return _TINT_GEN_MAP[1].get(_real(str(path)), 0)


def _tint_total_gen():
    gens = _tint_gens()
    return sum(gens.values()) if gens else 0


# path-str -> (stat_key, lines). One pending-overlay text build per file per
# state change, shared by every def scan of the file. Without this, each
# (pos, name) cache missindependently rebuilt current_file_text - and typing bumps
# the file's pending gen per keystroke, wiping the per-fileite cache, so a
# file with N tinted defs paid N × O(file) splices per keystroke on the
# render thread (GIL-held - the cost cProfile smeared into other).
_XFILE_LINES_CACHE = {}

# [calls, line-list rebuilds] — read by _collect_def_tints' slow-rebuild trace
# to say how much of a rebuild went to cross-file scanning.
_XFILE_SCAN_N = [0, 0]


def _scan_def_tint(path, line, name=None):
    """File-reading wrapper around _scan_def_tint_lines. Runs only on a
    cache miss (see _cross_file_def_tint). Reads through PendingSave so a
    tint-comment edit queued in-app (deferred saves never touch disk) is
    seen immediately; falls back to the disk file. The lines list is shared
    per (path, stat/pgen state) via _XFILE_LINES_CACHE."""
    p = str(path)
    _XFILE_SCAN_N[0] += 1
    memo = _XFILE_STAT_MEMO.get(p)
    stat_key = memo[1] if memo is not None else None
    got = _XFILE_LINES_CACHE.get(p)
    if got is not None and stat_key is not None and got[0] == stat_key:
        lines = got[1]
        return _scan_def_tint_lines(lines, _verify_def_line(lines, line, name), name)
    lines = None
    try:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        text = PendingSave.current_file_text(path)
        if text is not None:
            lines = text.split("\n")
    except Exception:
        lines = None
    if lines is None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return None
    _XFILE_SCAN_N[1] += 1
    if stat_key is not None:
        if len(_XFILE_LINES_CACHE) > 64:
            _XFILE_LINES_CACHE.clear()
        _XFILE_LINES_CACHE[p] = (stat_key, lines)
    return _scan_def_tint_lines(lines, _verify_def_line(lines, line, name), name)


# Salt for the _def_tints memo key; bump on any change to the collector or
# scanner logic so hotswapped editors recompute instead of replaying a memo
# built with the old code (draw_state can outlive the hotswap).
_DEF_TINTS_VER = 29
# Roster-mode viewport window quantum (lines): the occurrence scan covers the
# visible band rounded out to whole chunks + one chunk of margin each side.
_DT_WIN_CHUNK = 64


# rgb -> packed comment-text tint; reset on hotswap (collector re-exec) so
# tweaking the factors below shows after a swap.
_COMMENT_TINT_CACHE = {}

# (rgb, factors) -> adjusted rgb for background washes; bounded, reset on
# hotswap. Factors ride in the key so live toggle tweaks show fresh.
_BG_ADJ_CACHE = {}


def _brightness_clamp(r, g, b, min_b, max_b):
    """Clamp PERCEIVED brightness (0.299r + 0.587g + 0.114b) — the
    legibility guard for tinted colors. Both directions SCALE the channels,
    which preserves their ratios and therefore saturation — a dark
    high-saturation tint lifts to a dark high-saturation color, it does NOT
    wash toward gray (a uniform add did, which made lowering the value
    factor also lose saturation). Only true near-black — no hue left to
    preserve — falls back to the uniform add."""
    # Inverted clamp (min above max - mid-drag or experimental toggle values)
    # collapses to the floor: without this, dark colors LIFT to min while
    # bright ones CRUSH to max < min, inverting brightness ordering and pinning
    # every wash to near-identical luminance ("tints stopped responding").
    if 0 < max_b < min_b:
        max_b = min_b
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if lum < min_b:
        if lum > 1e-4:
            k = min_b / lum
            return min(1.0, r * k), min(1.0, g * k), min(1.0, b * k)
        d = min_b - lum
        return min(1.0, r + d), min(1.0, g + d), min(1.0, b + d)
    if lum > max_b > 0 and lum > 0:
        k = max_b / lum
        return r * k, g * k, b * k
    return r, g, b


def _soft_brightness_floor(r, g, b, min_b):
    """COMPRESSIVE floor (comment text): black lifts to min_b, the lift
    fades linearly to zero at the knee (2×min_b), colors above the knee are
    untouched — so raising the floor surfaces the dimmest comments WITHOUT
    dragging every comment to one identical brightness (the hard clamp
    pinned everything the value factor pushed under the floor, turning the
    min knob into a global brightness slider). Monotone: relative
    brightness ordering between comments is preserved. Scales channels
    (saturation kept); uniform add only for true near-black."""
    if min_b <= 0:
        return r, g, b
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    knee = min_b * 2.0
    if lum >= knee:
        return r, g, b
    target = min_b + (lum / knee) * (knee - min_b)
    if lum > 1e-4:
        k = target / lum
        return min(1.0, r * k), min(1.0, g * k), min(1.0, b * k)
    return min(1.0, r + target), min(1.0, g + target), min(1.0, b + target)


def _bg_adjust(rgb, factors):
    """Background-wash color adjustment: the Tint-class hsv factors
    (saturation/value multipliers) plus the perceived-brightness clamp —
    so glyphs stay legible over any tint. `factors` = (sat_f, val_f,
    min_b, max_b) from Toggles.TextEditor."""
    key = (rgb, factors)
    got = _BG_ADJ_CACHE.get(key)
    if got is not None:
        return got
    from src.lsd.gl_gui.toggles import rgb_to_hsv, hsv_to_rgb
    sat_f, val_f, min_b, max_b = factors
    r, g, b = rgb[0], rgb[1], rgb[2]
    if sat_f != 1.0 or val_f != 1.0:
        h, s, v = rgb_to_hsv(r, g, b)
        r, g, b = hsv_to_rgb(h, min(max(s * sat_f, 0.0), 1.0),
                             min(max(v * val_f, 0.0), 1.0))
    out = _brightness_clamp(r, g, b, min_b, max_b)
    if len(_BG_ADJ_CACHE) > 2048:
        _BG_ADJ_CACHE.clear()
    _BG_ADJ_CACHE[key] = out
    return out


def _comment_tint_color(rgb):
    """Comment-text variant of an override tint — slightly desaturated and
    darkened so the colored comment reads as commentary next to the code
    (the same hsv-factor adjustment pattern as toggles.Tint; factors live in
    Toggles.TextEditor, read live), then the shared perceived-brightness
    clamp so a very dark/bright tint's comment stays readable."""
    from src.lsd.gl_gui.toggles import rgb_to_hsv, hsv_to_rgb, Toggles
    saturation_factor = Toggles.TextEditor.comment_tint_saturation
    value_factor = Toggles.TextEditor.comment_tint_value

    h, s, v = rgb_to_hsv(rgb[0], rgb[1], rgb[2])
    r, g, b = hsv_to_rgb(h,
                         min(max(s * saturation_factor, 0.0), 1.0),
                         min(max(v * value_factor, 0.0), 1.0))
    r, g, b = _soft_brightness_floor(r, g, b,
                                     Toggles.TextEditor.comment_min_brightness)
    return _brightness_clamp(r, g, b, 0.0,
                             Toggles.TextEditor.bg_max_brightness)


# (packed_abgr, rgb_tuple, k_milli) -> packed. Syntax colors × tint colors ×
# a handful of k values - tiny, bounded; cleared if it ever balloons.
_GLYPH_MIX_CACHE = {}


def _fade_packed(packed, alpha_factor):
    """Scale a packed-ABGR glyph color's ALPHA by `alpha_factor` (0..1),
    rgb untouched — the diff-preview rows' fade (see draw_text's
    _preview_lines): fading alpha rather than mixing toward black keeps
    the row readable over any background depth."""
    key = (packed, int(alpha_factor * 1000))
    got = _GLYPH_MIX_CACHE.get(key)
    if got is not None:
        return got
    a = int(((packed >> 24) & 0xFF) * max(0.0, min(1.0, alpha_factor)))
    got = (packed & 0x00FFFFFF) | (a << 24)
    _GLYPH_MIX_CACHE[key] = got
    return got


def _mix_packed(packed, rgb, k):
    """Lerp a packed-ABGR glyph color `k` of the way toward `rgb` (0..1
    floats), keeping alpha — the ever-so-slight text tinting that unifies
    glyphs with the wash behind them."""
    key = (packed, rgb, int(k * 1000))
    got = _GLYPH_MIX_CACHE.get(key)
    if got is not None:
        return got
    r = (packed & 0xFF) / 255.0
    g = ((packed >> 8) & 0xFF) / 255.0
    b = ((packed >> 16) & 0xFF) / 255.0
    a = (packed >> 24) & 0xFF
    r += (rgb[0] - r) * k
    g += (rgb[1] - g) * k
    b += (rgb[2] - b) * k
    out = ((a << 24)
           | (min(255, max(0, int(b * 255))) << 16)
           | (min(255, max(0, int(g * 255))) << 8)
           | min(255, max(0, int(r * 255))))
    if len(_GLYPH_MIX_CACHE) > 4096:
        _GLYPH_MIX_CACHE.clear()
    _GLYPH_MIX_CACHE[key] = out
    return out


def _in_comment_override(text, i):
    """True when buffer index `i` sits inside a `# [...]` override comment —
    the presentation-mode widget-fade test (widgets in ANY override comment
    dim, tint-carrying or not). Cheap: one rfind to the line start plus a
    bounded find to `i`."""
    ls = text.rfind('\n', 0, i) + 1
    h = text.find('#', ls, i)
    return h != -1 and text[h + 1:i].lstrip()[:1] == '['

# realpath-str -> ((mtime_ns, size), {def_line: tint | None}). Invalidated by
# stat key (cheap, content-free - per CLAUDES.md we hash contents); the stat
# runs once per unique definition file per collector REBUILD (per edit/parse),
# never per frame. In-app edits pending in PendingSave show after disk write.
_XFILE_TINT_CACHE = {}

# Bumped once per _collect_def_tints rebuild: a path is os.stat'ed at most
# once per rebuild, not once per symbol referencing it (stat churn is the
# keystroke path was measured).
_REBUILD_GEN = 0
_XFILE_STAT_MEMO = {}   # path-str -> (rebuild_gen, stat_key)


def _cross_file_def_tint(path, line, name=None):
    if path is None:
        return None
    p = str(path)
    memo = _XFILE_STAT_MEMO.get(p)
    if memo is not None and memo[0] == _REBUILD_GEN:
        stat_key = memo[1]
        if stat_key is None:
            return None
    else:
        import os
        try:
            st = os.stat(p)
        except OSError:
            _XFILE_STAT_MEMO[p] = (_REBUILD_GEN, None)
            return None
        # Validity = disk stat + PendingSave edit generation: deferred in-app
        # edits never touch disk, so the pending gen is what moves when a tint
        # comment is edited and recompiled without a save-to-disk.
        stat_key = (st.st_mtime_ns, st.st_size, _pending_gen_of(p))
        if len(_XFILE_STAT_MEMO) > 4096:
            _XFILE_STAT_MEMO.clear()
        _XFILE_STAT_MEMO[p] = (_REBUILD_GEN, stat_key)
    entry = _XFILE_TINT_CACHE.get(p)
    if entry is None or entry[0] != stat_key:
        entry = (stat_key, {})
        _XFILE_TINT_CACHE[p] = entry
    per_def = entry[1]
    key = (line, name)
    if key not in per_def:
        per_def[key] = _scan_def_tint(p, line, name)
    return per_def[key]


# (realpath, pending-gen, before_line) -> net line delta of queued edits fully
# above; bounded, reset on hotswap.
_PENDING_DELTA_CACHE = {}


def _pending_line_delta(path, before_line):
    """Net line-count change of PendingSave span edits that sit fully ABOVE
    0-based file line `before_line` of `path` — the shift between DISK
    coordinates (addresses / span starts, the in-session invariant) and
    PENDING-text coordinates (what the symbol index computes sites in, via
    current_file_text's splices). Added to the buffer's usage offset so sites
    keep landing on the right buffer lines while an unsaved edit above the
    span has grown or shrunk the file. Cached per (path, pending gen)."""
    if path is None or not before_line:
        return 0
    gen = _pending_gen_of(path)
    if not gen:
        return 0
    rp = _real(str(path))
    key = (rp, gen, before_line)
    got = _PENDING_DELTA_CACHE.get(key)
    if got is not None:
        return got
    delta = 0
    try:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        for addr, (codec, kwargs) in list(PendingSave.pending_saves.items()):
            data = kwargs.get("data")
            start, end = getattr(addr, "start", None), getattr(addr, "end", None)
            if (not isinstance(data, str) or start is None or end is None
                    or end > before_line):
                continue
            if _real(str(addr.path)) != rp:
                continue
            d = data[:-1] if data.endswith("\n") else data
            delta += (d.count("\n") + 1) - (end - start)
    except Exception:
        return 0
    if len(_PENDING_DELTA_CACHE) > 512:
        _PENDING_DELTA_CACHE.clear()
    _PENDING_DELTA_CACHE[key] = delta
    return delta


# path-str -> (stat_key, {name: rgb}) - every name in the file whose definition
# has an explicit tint. One O(file) scan per (disk stat, pending-gen) state;
# resolved when member completions land, never per frame. Bounded, reset on
# hotswap.
_XFILE_NAME_TINTS = {}
_TINT_OWNER_DEF_RE = re.compile(r"\s*(?:async\s+)?(?:class|def)\s+([A-Za-z_]\w*)")
_TINT_OWNER_ASSIGN_RE = re.compile(r"\s*([A-Za-z_]\w*)\s*[:=](?!=)")

def _file_name_tints(path):
    """{name: rgb} for every definition in `path` carrying an explicit tint.
    The autocomplete popup colors member candidates with this when the
    receiver's class/module lives in `path` — the buffer's usage graph only
    knows symbols USED in the buffer, so an unused member (`Toggles.
    yield_to_ui` offered after `Toggles.`) would otherwise show untinted.
    Each `tint=` marker line is anchored to the def/assignment that owns it,
    then confirmed through _scan_def_tint_lines so the ownership rules stay
    canonical (a plain `tint=` kwarg in a call resolves to no tint there)."""
    if path is None:
        return None
    p = str(path)
    import os
    try:
        st = os.stat(p)
    except OSError:
        return None
    stat_key = (st.st_mtime_ns, st.st_size, _pending_gen_of(p))
    got = _XFILE_NAME_TINTS.get(p)
    if got is not None and got[0] == stat_key:
        return got[1]
    lines = None
    try:
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        pend = PendingSave.current_file_text(p)
        if pend is not None:
            lines = pend.split("\n")
    except Exception:
        lines = None
    if lines is None:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return None
    out = {}
    for i, raw in enumerate(lines):
        if "tint=" not in raw:
            continue
        # Anchor the marker to the def-shaped line that owns it: the marker
        # line itself (trailing comment / bare def / assignment), or -
        # for a comment run or decorator line - the first def/assignment
        # below (paren-balanced, so multi-line calls are consumed).
        owner = name = None
        m = _TINT_OWNER_DEF_RE.match(raw) or _TINT_OWNER_ASSIGN_RE.match(raw)
        if m is not None:
            owner, name = i, m.group(1)
        else:
            depth = 0
            for j in range(i, min(i + 40, len(lines))):
                s = lines[j].strip()
                if depth == 0 and j > i and not s.startswith(("#", "@")):
                    m = (_TINT_OWNER_DEF_RE.match(lines[j])
                         or _TINT_OWNER_ASSIGN_RE.match(lines[j]))
                    if m is not None:
                        owner, name = j, m.group(1)
                    break
                depth += s.count("(") - s.count(")")
        if owner is None or name in out:
            continue
        res = _scan_def_tint_lines(lines, owner + 1, name)
        if res is not None:
            out[name] = tuple(res[0][:3])
    if len(_XFILE_NAME_TINTS) > 64:
        _XFILE_NAME_TINTS.clear()
    _XFILE_NAME_TINTS[p] = (stat_key, out)
    return out


def _live_receiver_obj(ns, rcv):
    """The live object the dotted receiver `rcv` getattr-resolves to in module
    namespace `ns`, or None when the walk dead-ends."""
    parts = [s for s in (rcv or "").split(".") if s]
    if not parts or not ns:
        return None
    obj = ns.get(parts[0])
    for part in parts[1:]:
        if obj is None:
            return None
        try:
            obj = getattr(obj, part, None)
        except Exception:
            return None
    return obj


def _receiver_file_via_imports(text, rcv):
    """Defining file of `rcv`'s head resolved through the buffer's own import
    statements (module-level OR function-local). The live-namespace walk only
    sees module globals, so a receiver imported inside a function body
    (`from ...toggles import Toggles` mid-function) dead-ends there and the
    members come from jedi — this recovers the tint file for that path. None
    when no import of the head is found or the module isn't loaded."""
    head = (rcv or "").split(".")[0]
    if not head:
        return None
    import sys as _sys
    modname = None
    for m in re.finditer(r"^[ \t]*from[ \t]+([\w.]+)[ \t]+import[ \t]+([^\n]*)",
                         text, re.M):
        if re.search(r"\b%s\b" % re.escape(head), m.group(2).split("#")[0]):
            modname = m.group(1)
            break
    if modname is None:
        for m in re.finditer(r"^[ \t]*import[ \t]+([\w.]+)(?:[ \t]+as[ \t]+(\w+))?",
                             text, re.M):
            if (m.group(2) or m.group(1).split(".")[0]) == head:
                modname = m.group(1)
                break
    if modname is None:
        return None
    mod = _sys.modules.get(modname)
    if mod is None:   # hotswap dual identity: try the other src. spelling
        alt = modname[4:] if modname.startswith("src.") else "src." + modname
        mod = _sys.modules.get(alt)
    return getattr(mod, "__file__", None) if mod is not None else None


def _live_receiver_file(ns, rcv):
    """Defining file of the live object `rcv` getattr-resolves to in module
    namespace `ns`: the module's own __file__ for a module, else the file of
    the (type's) defining module. None when the walk dead-ends."""
    obj = _live_receiver_obj(ns, rcv)
    if obj is None:
        return None
    try:
        import inspect
        import sys as _sys
        if inspect.ismodule(obj):
            return getattr(obj, "__file__", None)
        cls = obj if isinstance(obj, type) else type(obj)
        mod = _sys.modules.get(getattr(cls, "__module__", None) or "")
        return getattr(mod, "__file__", None)
    except Exception:
        return None


# How far (in lines, each direction) _site_span searches for a drifted name.
# Index sites are PENDING-text coords refreshed a few times a second, so under
# rapid line-count typing (hold Enter) they lag the buffer by a handful of
# lines; same-line-only recovery dropped every index-sited symbol below the
# caret (the Toggles.profile_mode-loses-its-wash bug) while fresher-sited
# spans survived. Nearest-first, word-boundary matched - recovering onto a
# nearby occurrence of the SAME name is benign (dedupe collapses overlaps).
# Quiet-time safety net only (recomputes are suppressed while typing is hot -
# see _typing_hot); residual drift during quiet is a line or less, so a wide
# window mostly buys re-anchors on long names.
_SITE_RECOVER_LINES = 4


def def_block_end(lines, li):
    """Last 0-based line of the definition whose `def`/`class` statement is
    `lines[li]` -- the body's indented extent, trailing blank lines excluded.
    Returns `li` itself when the line isn't a def/class statement (or the
    body is empty)."""
    if not (0 <= li < len(lines)):
        return li
    head = lines[li]
    if re.match(r"\s*(?:async\s+)?(?:def|class)\s", head) is None:
        return li
    ind = len(head) - len(head.lstrip())
    end = li
    for j in range(li + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        if len(ln) - len(ln.lstrip()) <= ind:
            break
        end = j
    return end


def fold_focus_scope(ds, text, li):
    """Collapse every fold in the buffer EXCEPT the chain enclosing 0-based
    line `li` -- a symbol jump's "show me just this": the landed def/class
    and each ancestor scope stay open, everything else (sibling defs, other
    classes, the import block, comment runs) folds. Inside the landed
    scope, its own nested scopes stay open too; only its default-collapsed
    folds (docstrings, comment runs) fold. Writes the durable KEYS and
    primes ds._fold_cache with the new layout so a fold_project_jump right
    after maps through it. No-op (False) when the editor has no fold layout
    for this buffer yet."""
    _fc = getattr(ds, '_fold_cache', None)
    kf = getattr(ds, '_fold_key_of', None)
    if _fc is None or _fc[0] is not text or not kf:
        return False
    ranges = _fc[1][0]
    sc = getattr(ds, '_scope_rng_cache', None)
    default_col = set(sc[1][1]) if (sc is not None and sc[0] is text) else set()
    target = next((r for r in ranges if r[0] == li), None)
    col = set()
    for r in ranges:
        if r[0] <= li <= r[1]:
            continue  # the landed scope or one of its ancestors: open
        if (target is not None and target[0] < r[0] and r[1] <= target[1]
                and r not in default_col):
            continue  # a scope nested inside the landed one: open
        col.add(r)
    # `ranges` is the layout's combined tuple (scope + diff spans) - split
    # the new collapse set between the two stores so the body's key harvest
    # and the diff layer's drift carry each see only their own tuples.
    diff_set = getattr(ds, '_diff_rng_set', None) or frozenset()
    ds._fold_collapsed = {r for r in col if r not in diff_set}
    if getattr(ds, '_diff_fold_collapsed', None) is not None:
        new_diff = {r for r in col if r in diff_set}
        if new_diff != ds._diff_fold_collapsed:
            # A deliberate reshape of the diff layer: count it as a hand
            # takeover so an automatic expand_diff switch doesn't undo it.
            ds._diff_manual_gen = getattr(ds, '_diff_manual_gen', 0) + 1
        ds._diff_fold_collapsed = new_diff
    ds._fold_keys = {kf[r] for r in ds._fold_collapsed if r in kf}
    exp = getattr(ds, '_fold_search_exp', None)
    if exp:
        exp.clear()
    if getattr(ds, '_fold_search_exp_keys', None):
        ds._fold_search_exp_keys = set()
    if getattr(ds, '_diff_search_exp', None):
        ds._diff_search_exp = set()
    _hl = _fold_headless_set(kf, ranges)
    built = _fold_build(text, ranges, col, _hl)
    ds._fold_cache = (text, (ranges, frozenset(col), _hl), built)
    ds.invalidate()
    return True


def fold_display_line(ds, text, bli):
    """Project a FULL-buffer 0-based line to the editor's fold display space
    (identity while nothing is collapsed / the fold layout was built against
    another buffer). A line hidden inside a collapsed fold maps to the
    fold's head line. Read-only counterpart of fold_project_jump -- expands
    nothing."""
    if not (getattr(ds, '_fold_collapsed', None)
            or getattr(ds, '_diff_fold_collapsed', None)):
        return bli
    _fc = getattr(ds, '_fold_cache', None)
    if _fc is None or _fc[0] is not text:
        return bli
    d2b = _fc[2][3]
    if not d2b:
        return bli
    return max(0, bisect.bisect_right(d2b, bli) - 1)


def fold_buffer_line_at(ds, text, pos):
    """FULL-buffer 0-based line of caret display offset `pos` -- the inverse
    of fold_display_line for a CHAR offset. The caret lives in fold display
    space (the spliced text draw_text renders), so counting newlines in the
    full buffer up to it lands short by every collapsed body above the
    caret; this counts in the display text and maps the display line back
    through the layout's disp->buf table. Identity while nothing is
    collapsed / the layout was built against another buffer."""
    _fc = getattr(ds, '_fold_cache', None)
    if (not (getattr(ds, '_fold_collapsed', None)
             or getattr(ds, '_diff_fold_collapsed', None))
            or _fc is None or _fc[0] is not text):
        return text.count('\n', 0, pos)
    disp, d2b = _fc[2][0], _fc[2][3]
    dl = disp.count('\n', 0, max(0, min(pos, len(disp))))
    if not d2b:
        return dl
    return d2b[min(dl, len(d2b) - 1)]


def jump_emph_cols(text, pos, span=None):
    """Column span (col0, col1) the jump emphasis should flash for a landing
    at buffer index `pos` — shared by the cross-file consumption
    (draw_code_editor) and the same-file usage jump. Preference order: the
    explicit token `span` ((start, end) buffer indices, e.g. from
    _site_span), else the def/class NAME on the landing line, else the
    identifier at `pos`, else the line's indent→rstrip code span. None when
    the line has no code at all."""
    ls = text.rfind('\n', 0, pos) + 1
    le = text.find('\n', ls)
    line = text[ls:] if le == -1 else text[ls:le]
    if span is not None and span[0] >= ls:
        return (span[0] - ls, span[1] - ls)
    m = re.match(r"\s*(?:async\s+)?(?:def|class)\s+(\w+)", line)
    if m is None:
        m = re.compile(r"[A-Za-z_]\w*").match(line, pos - ls)
    if m is not None:
        g = m.lastindex or 0
        return (m.start(g), m.end(g))
    code = line.rstrip()
    indent = len(code) - len(code.lstrip())
    return (indent, len(code)) if len(code) > indent else None


def _site_span(text, ln, col, name, line_offset):
    """(start_index, end_index) in the buffer for one file-absolute (ln, col)
    occurrence of `name`, or None. Same verify-then-recover logic as
    _collect_usage_spans: the fast import-index records the STATEMENT start
    col, and the buffer may have drifted since the pass ran."""
    buf_line = ln - 1 - line_offset
    if buf_line < 0:
        return None
    idx = _line_col_to_index(text, buf_line, col)
    end = idx + len(name)
    if text[idx:end] == name:
        return idx, end
    starts = _line_starts(text)
    nlines = len(starts)

    def _find_on_line(bl):
        if not (0 <= bl < nlines):
            return None
        ls = starts[bl]
        le = starts[bl + 1] - 1 if bl + 1 < nlines else len(text)
        p = text.find(name, ls, le)
        while p != -1:
            b_ok = p == 0 or not (text[p - 1].isalnum() or text[p - 1] == "_")
            a = p + len(name)
            a_ok = a >= len(text) or not (text[a].isalnum() or text[a] == "_")
            if b_ok and a_ok:
                return p
            p = text.find(name, p + 1, le)
        return None

    p = _find_on_line(buf_line)
    if p is None:
        for off in range(1, _SITE_RECOVER_LINES + 1):
            p = _find_on_line(buf_line + off)
            if p is None:
                p = _find_on_line(buf_line - off)
            if p is not None:
                break
    if p is None:
        return None
    return p, p + len(name)


def _recover_def_pos(text, token):
    """Buffer index of `token`'s name inside the buffer's UNIQUE
    `class`/`def` statement for it, or None. The wide-drift fallback for
    definition jumps: the symbol index recomputes lazily, so a served def
    line can be stale by more lines than _site_span's ±4 recovery covers —
    a `class GlobalSearch` jump landed 10 lines high after edits above it.
    Only an unambiguous single match retargets; zero or several matches
    keep the recorded line."""
    leaf = (token or '').rsplit('.', 1)[-1]
    if not leaf:
        return None
    ms = list(re.finditer(
        rf'^[ \t]*(?:async[ \t]+)?(?:class|def)[ \t]+({re.escape(leaf)})\b',
        text, re.M))
    return ms[0].start(1) if len(ms) == 1 else None


def _collect_def_tints(code_tree, text, line_offset=0, view_path=None):
    """(blocks, spans) for the definition-tint washes.

    blocks: [(def_buf_line, indent_buf_index, end_buf_line, tint)] — one per
        tinted class/def defined in THIS buffer; the rect runs from the def
        keyword's first character down to the last line before the dedent.
    spans: [(start_index, end_index, tint)] — one per occurrence (SymbolUsage
        site) of any symbol whose definition — in-buffer or cross-file —
        carries a tint.

    One tree walk (same shape/guards as _collect_usage_spans); block extents
    come from a single split of the buffer text. Rebuilds only when the cached
    key in _def_tints changes (per edit/parse), never per frame."""
    # Slow-rebuild trace: phase markers, reported only when the whole rebuild
    # crosses 100ms (the 3s main-thread hit of 2026-07-31 hid in here).
    _pt0 = time.perf_counter()
    _pcpu0 = time.thread_time()
    _pmarks = []
    _pxf0 = tuple(_XFILE_SCAN_N)

    def _pm(label):
        _pmarks.append((label, time.perf_counter()))

    lines = text.split("\n")
    line_start_idx = [0]
    for l in lines:
        line_start_idx.append(line_start_idx[-1] + len(l) + 1)

    global _REBUILD_GEN
    _REBUILD_GEN += 1        # one os.stat per external def file per rebuild

    blocks, spans = [], []
    tinted_lines = {}                    # file line of a tinted in-buffer def -> tint
    own_block_range = {}                 # any def line -> (buf_start, buf_end) of its block
    node_seen, su_seen = set(), set()
    all_sus = []
    _in_file_memo = {}
    _view_rp = _real(str(view_path)) if view_path is not None else None

    def _def_in_file(d):
        dp = getattr(d, "path", None)
        if dp is None or _view_rp is None:
            return False
        k = str(dp)
        got = _in_file_memo.get(k)
        if got is None:
            got = _in_file_memo[k] = (_real(k) == _view_rp)
        return got

    def _find_def_line(name):
        # Fallback when no SymbolUsage names the def: first `class|def name`
        # in the buffer, returned as a file line.
        m = re.search(rf"^[ \t]*(?:class|def)\s+{re.escape(name)}\b", text, re.M)
        if m is None:
            return None
        return text.count("\n", 0, m.start()) + 1 + line_offset

    def _decor_start(def_line, indent):
        # Re reverse mirror of _snap_to_def: extend the block top over the
        # decorator/comment run directly above the class/def keyword, so the
        # wash covers `@window(...)` etc. Reverse paren-balanced - a
        # multi-line `@defaults(\n    foo,\n)` is consumed from its closing
        # paren up to its `@` opener - stopping at a blank line or any
        # non-decorator statement. Comments between decorators are stepped
        # over but only an actual `@` line moves the top.
        start, j, depth = def_line, def_line - 1, 0
        while j >= 0 and j > def_line - 40:
            s = lines[j].strip()
            if not s and depth == 0:
                break
            depth += s.count(")") - s.count("(")
            if depth == 0:
                if s.startswith("@"):
                    start = j
                elif not s.startswith("#"):
                    break
            elif depth < 0:
                break
            j -= 1
        return start

    def _block_extent(buf_line, name=None):
        if not (0 <= buf_line < len(lines)):
            return None
        # The recorded def line may be the decorated statement's first line
        # (`@defaults(...)`); snap to the class/def keyword for the indent
        # and body extent, then extend the top back up over the decorators.
        # Otherwise reverse-recover it by NAME (a plain, non-render_func def's
        # def line is co_firstlineno in DISK coordinates - one unsaved
        # edit above it and the recorded line lands on a body line, whose
        # indent then misplaced the whole wash one indent in).
        if name:
            buf_line = _verify_def_line(lines, buf_line + 1, name) - 1
        buf_line = _snap_to_def(lines, buf_line)
        def_line_text = lines[buf_line]
        indent = len(def_line_text) - len(def_line_text.lstrip())
        end = buf_line
        for k in range(buf_line + 1, len(lines)):
            s = lines[k]
            if not s.strip():
                continue
            if len(s) - len(s.lstrip()) <= indent:
                break
            end = k
        start = _decor_start(buf_line, indent)
        return start, line_start_idx[start] + indent, end, buf_line

    def walk(node, depth=0):
        if not isinstance(node, dict) or depth > 64 or id(node) in node_seen:
            return
        node_seen.add(id(node))
        su_map = node.get("__symbol_usages__")
        if isinstance(su_map, dict):
            for su_key, su in su_map.items():
                if id(su) not in su_seen:
                    su_seen.add(id(su))
                    all_sus.append((su_key, su))
        for k, v in node.items():
            if k in ("__cst__", "__symbol_usages__", "__overrides__", "decorators"):
                continue
            if not isinstance(v, dict):
                continue
            tint = _node_tint(v)
            if tint is not None and isinstance(k, str):
                su = su_map.get(k) if isinstance(su_map, dict) else None
                d = getattr(su, "definition", None)
                ln = (d.line if d is not None and getattr(d, "line", None)
                      and _def_in_file(d) else None)
                if ln is None:
                    ln = _find_def_line(k)
                if ln is not None:
                    # Keyed by line but OWNED by name: the su lookup below
                    # must not assign this tint to a different symbol whose
                    # definition merely resolves to the same line (parameters
                    # of a tinted def resolve to the def line).
                    tinted_lines[ln] = (tint, k.split("#", 1)[0])
                    blk = _block_extent(ln - 1 - line_offset, k.split("#", 1)[0])
                    if blk is not None:
                        _b_start, _b_lidx, _b_bend, _b_def = blk
                        blocks.append((_b_start, _b_lidx, _b_bend, tint))
                        # Save the block range under both line conventions
                        # - the index-recorded def line (may be the decorator)
                        # and the keyword class line (what the scanner's
                        # src_line reports) - so either lookup below hits.
                        # The range itself starts at the decorator-extended
                        # top so spans inside the decorators stay suppressed.
                        rng = (_b_start, _b_bend)
                        own_block_range[ln] = rng
                        own_block_range[_b_def + 1 + line_offset] = rng
            walk(v, depth + 1)

    _pm("init")
    walk(code_tree)
    _pm("walk")

    # Collect tints per symbol: (rgb, scale, drop_range) - scale multiplies the
    # wash alpha at draw time (1.0 for a symbol's own definition tint;
    # propagated locals fade below). drop_range is the STRUCTURAL redundancy
    # zone: the buffer line range of the in-file class block this symbol's
    # tint comes from (its own block for a tinted class, the ENCLOSING class's
    # block for an inherited field's tint) - spans inside those are never
    # emitted, so the ancestor already says it. Color-independent on purpose:
    # comparing colors popped washes in/out during tint drags while the live
    # text and the pending source gen momentarily disagreed.
    su_tint = {}                     # id(su) -> ((r, g, b), scale, drop_range|None)
    untinted_locals = []             # SymbolUsage — in-file local bindings

    # Per-rebuild memo + heuristic prefilter for live-buffer tint scans: an
    # explicit tint needs a trailing '#', a class/def/@-shaped line (body /
    # decorator scan), or a comment/decorator line directly above - any
    # other line can't produce one, so don't walk it. Shared by the su loop
    # and the assignment sweep (measured: ~900 unfiltered scans/keystroke).
    _MISS = object()
    _scan_memo = {}                  # (buf_line_1based, name) -> res | None
    # su-loop profile: [verify_s, scan_s, xfile_s, worst (ms, line, name)] -
    # feeds the SLOW-rebuild trace so a blow-up names its exact helper + symbol.
    _live_prof = [0.0, 0.0, 0.0, None]

    def _scan_live(bl, name):
        key = (bl, name)
        got = _scan_memo.get(key, _MISS)
        if got is not _MISS:
            return got
        _t1 = time.perf_counter()
        vl = _verify_def_line(lines, bl, name) if name else bl
        _t2 = time.perf_counter()
        _live_prof[0] += _t2 - _t1
        i = vl - 1
        lt = lines[i] if 0 <= i < len(lines) else ""
        s = lt.lstrip()
        worth = ("#" in lt or s.startswith(("class ", "def ", "@"))
                 or (i > 0 and lines[i - 1].lstrip().startswith(("#", "@"))))
        res = _scan_def_tint_lines(lines, vl, name) if worth else None
        _t3 = time.perf_counter()
        _live_prof[1] += _t3 - _t2
        _ms = (_t3 - _t1) * 1000.0
        if _live_prof[3] is None or _ms > _live_prof[3][0]:
            _live_prof[3] = (round(_ms, 1), bl, str(name))
        _scan_memo[key] = res
        return res
    sites_by_line = {}               # file line -> [(su, col)] for RHS lookup
    for su_key, su in all_sus:
        for site in getattr(su, "sites", None) or ():
            sites_by_line.setdefault(site[0], []).append((su, site[1]))
        d = getattr(su, "definition", None)
        if d is None or not getattr(d, "line", None):
            continue
        in_file = _def_in_file(d)
        rec = tinted_lines.get(d.line) if in_file else None
        tint = (rec[0] if rec is not None and rec[1] ==
                str(getattr(su, "name", "") or "").rsplit(".", 1)[-1] else None)
        src_line = d.line if tint is not None else None
        if tint is None:
            # Defs INSIDE the viewed buffer scan the LIVE buffer text: the
            # same text every other layer uses this frame. Going through
            # PendingSave here (as cross-file does) made su-resolved tints
            # LAG during tint drags: the pending-save channel is a trailing
            # writer, so sweep-resolved ones tracked the live value while
            # su-resolved ones served the queued text and snapped on each
            # pending-save catch-up - mixed-source frames read as flicker.
            bl = d.line - line_offset
            if in_file and 1 <= bl <= len(lines):
                res = _scan_live(bl, getattr(su, "name", None))
                if res is not None:
                    tint, src_line = res[0], res[1] + line_offset
            else:
                # Cross-file defs (and same-file defs OUTSIDE the viewed
                # buffer) resolve from source through the mtime/pgen-keyed
                # cache - the name re-anchors a stale recorded line first.
                _t1 = time.perf_counter()
                res = _cross_file_def_tint(getattr(d, "path", None), d.line,
                                           getattr(su, "name", None))
                _live_prof[2] += time.perf_counter() - _t1
                if res is not None:
                    tint, src_line = res
        if tint is not None:
            rng = own_block_range.get(src_line) if in_file else None
            su_tint[id(su)] = (tuple(tint[:3]), 1.0, rng)
        elif (isinstance(su_key, str) and "\x1f" in su_key and in_file):
            untinted_locals.append(su)

    _pm("su_loop")
    # Assignment propagation: a local whose binding line uses tinted symbols
    # adopts a faded blend of their colors (`is_profiling = Toggles.profile_mode
    # == ProfileMode.ON` washes as a blend of those tints) - so a value keeps
    # its color trail as it flows through code. Iterated so a local defined
    # from an already-propagated local fades one step further per hop.
    from src.lsd.gl_gui.toggles import Toggles
    fade = Toggles.TextEditor.def_propagation_fade
    if Toggles.TextEditor.def_tint_propagation:
        for _pass in range(4):
            changed = False
            for su in untinted_locals:
                if id(su) in su_tint:
                    continue
                d = su.definition
                # One voice per column: a dotted chain has the base and the
                # full member path at the SAME col (`Toggles.profile_mode` →
                # 'Toggles' and the chain). The MOST SPECIFIC (longest) member
                # is the value actually bound - it alone speaks for the group,
                # so `x = Toggles.profile_mode` blends pure profile_mode, and
                # `x = Toggles.brightness` (untinted member) blends NOTHING
                # from that column rather than leaking the namespace color.
                by_col = {}
                for osu, col in sites_by_line.get(d.line, ()):
                    if osu is su and col == getattr(d, "column", None):
                        continue                     # the binding itself
                    # A name DEFINED on this line is not a read: parameters
                    # appear on the `def` line, where the (tinted) function's
                    # own def-site occurrence sits - without this, every
                    # param of a tinted def blended the def's own color and
                    # the whole body inherited it hop by hop. The recorded
                    # definition line may be the DECORATOR definition line
                    # while the occurrence sits on the def keyword's line, so
                    # both sides compare through _snap_to_def.
                    od = getattr(osu, "definition", None)
                    if (od is not None and getattr(od, "line", None) is not None
                            and _def_in_file(od)):
                        obl = od.line - 1 - line_offset
                        dbl = d.line - 1 - line_offset
                        if (0 <= obl < len(lines) and 0 <= dbl < len(lines)
                                and _snap_to_def(lines, obl) == _snap_to_def(lines, dbl)):
                            continue
                    cur = by_col.get(col)
                    if cur is None or len(getattr(osu, "name", None) or "") > \
                            len(getattr(cur, "name", None) or ""):
                        by_col[col] = osu
                contribs = [su_tint[id(o)] for o in by_col.values()
                            if id(o) in su_tint]
                if not contribs:
                    continue
                uniq = list(dict.fromkeys((c[0], c[1]) for c in contribs))
                n = len(uniq)
                rgb = tuple(sum(c[0][i] for c in uniq) / n for i in range(3))
                scale = fade * (sum(c[1] for c in uniq) / n)
                if scale >= 0.2:                      # stop fading into noise
                    su_tint[id(su)] = (rgb, scale, None)
                    changed = True
            if not changed:
                break

    _pm("propagation")
    seen_spans = set()
    for su_key, su in all_sus:
        t = su_tint.get(id(su))
        name = getattr(su, "name", None)
        if t is None or not name:
            continue
        rgb, scale, drop_range = t
        d = getattr(su, "definition", None)
        def_line = getattr(d, "line", None) if d is not None else None
        for site in getattr(su, "sites", None) or ():
            # Structural redundancy: this symbol's tint COMES FROM the
            # in-buffer class block covering drop_range - occurrences inside
            # it are never emitted (or filtered after the fact), so nothing
            # can pop during a tint drag.
            buf_ln = site[0] - 1 - line_offset
            if drop_range is not None and drop_range[0] <= buf_ln <= drop_range[1]:
                continue
            span = _site_span(text, site[0], site[1], name, line_offset)
            if span is not None and span not in seen_spans:
                seen_spans.add(span)
                # 5th field: is this occurrence ON the definition line the
                # tint was resolved from? Consumed (and stripped) by the
                # misresolution filter below.
                spans.append((span[0], span[1], rgb, scale, site[0] == def_line))

    # Bindings with no symbol-usage entry: a local never referenced afterwards
    # (`stack = 2*stack_1`, `stack_3 = stack_1 * stack`) gets no
    # __symbol_usages__ entry at all - the usage graph only tracks referenced
    # names - so the su-driven assignments and propagation above can't see it.
    # This text sweep covers them: walk assignment lines top-to-bottom with a
    # name→tint map seeded from every tinted symbol,
    #  - an OWN '# [tint=...]' (leading run or trailing) washes at 1.0, and
    #  - otherwise, inside a def body, the RHS tokens' tints blend exactly
    #    like su-based propagation (unique colors averaged, fade per hop,
    #    exact dotted-token match wins - the namespace color never leaks).
    # Top-to-bottom order makes chains work (stack → stack_3 → ...) and each
    # result feeds the map for later lines. Su-covered spans are skipped via
    # seen_spans; su-based-but-untinted bindings get the text blend as a
    # bonus (their contributors may themselves be su-less).
    _pm("emit_spans")
    _asn_re = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*[:=](?!=)")
    _ident_re = re.compile(r"[A-Za-z_][\w.]*")
    name_tint = {}
    for su_key, su in all_sus:
        t = su_tint.get(id(su))
        nm = getattr(su, "name", None)
        if t is not None and nm:
            name_tint[nm] = (t[0], t[1])
    # Per-line "inside a def body" flags (one linear pass): these stay in
    # function scope - class-body fields shouldn't pick up RHS blends.
    _in_def = [False] * len(lines)
    _scopes = []
    for bi, lt in enumerate(lines):
        s = lt.strip()
        if not s or s.startswith("#"):
            _in_def[bi] = any(kd == "def" for _, kd in _scopes)
            continue
        ind = len(lt) - len(lt.lstrip())
        while _scopes and _scopes[-1][0] >= ind:
            _scopes.pop()
        _in_def[bi] = any(kd == "def" for _, kd in _scopes)
        m2 = re.match(r"(?:async\s+)?(def|class)\s", s)
        if m2:
            _scopes.append((ind, m2.group(1)))
    mix_on = Toggles.TextEditor.def_tint_propagation
    for bi, lt in enumerate(lines):
        m = _asn_re.match(lt)
        if m is None:
            continue
        name = m.group(2)
        start = line_start_idx[bi] + len(m.group(1))
        end = start + len(m.group(2))
        covered = (start, end) in seen_spans
        res = _scan_live(bi + 1, None)
        if res is not None and res[1] == bi + 1:
            rgb, scale = tuple(res[0][:3]), 1.0      # own comment tint wins
            if covered:
                # A own-comment RE-binding overrides the su-derived color
                # from here on: the usage graph ties a local to its FIRST
                # binding, so a later `# [tint=...]` + rebinding would
                # otherwise keep it in the first binding's color.
                for si, sp in enumerate(spans):
                    if sp[0] >= start and text[sp[0]:sp[1]] == name:
                        spans[si] = (sp[0], sp[1], rgb, scale, sp[4])
                name_tint[name] = (rgb, scale)
                continue
        elif covered:
            name_tint.setdefault(name, None)   # su span exists; map set above
            continue
        elif mix_on and _in_def[bi]:
            rhs = lt[m.end():].split("#", 1)[0]
            contribs = [name_tint[tok] for tok in _ident_re.findall(rhs)
                        if name_tint.get(tok) is not None]
            if not contribs:
                continue
            uniq = list(dict.fromkeys(contribs))
            n = len(uniq)
            rgb = tuple(sum(c[0][i] for c in uniq) / n for i in range(3))
            scale = fade * (sum(c[1] for c in uniq) / n)
            if scale < 0.2:
                continue
        else:
            continue
        seen_spans.add((start, end))
        spans.append((start, end, rgb, scale, True))
        name_tint[name] = (rgb, scale)

    _pm("asn_sweep")
    blocks.sort()
    # Misresolution filter (the same-block/same-tint redundancy is handled
    # STRUCTURALLY at emission via drop_range - see above): drop an
    # ASSIGNMENT-TARGET span that is NOT on its own resolved definition line
    # when it sits inside any tinted block. `name = ...` inside a class
    # defines the class's attribute, so a wash there colored by a definition
    # recorded ELSEWHERE is a same-name misresolution (two classes sharing a
    # field name - the index hands one class's def the OTHER's tint). A def
    # site that IS its recorded definition keeps its own explicit tint
    # (a '# [tint=...]' field like profile_mode); faded propagation blends
    # (scale < 1) are exempt so a long name keeps its color-trail anchor.
    if blocks and spans:
        kept = []
        for sp in spans:
            scale, at_own_def = sp[3], sp[4]
            if scale >= 1.0 and not at_own_def:
                ln = bisect.bisect_right(line_start_idx, sp[0]) - 1
                if any(b[0] <= ln <= b[2] for b in blocks):
                    ls = line_start_idx[ln]
                    le = (line_start_idx[ln + 1] - 1
                          if ln + 1 < len(line_start_idx) else len(text))
                    if (text[ls:sp[0]].strip() == ""
                            and re.match(r"\s*(?::[^=\n]+)?=[^=]",
                                         text[sp[1]:le]) is not None):
                        continue
            kept.append(sp)
        spans = kept
    # Per-LINE tints: one very subtle full-width band per line that carries
    # any symbol wash. A definition ON the line with its own EXPLICIT tint
    # (at_own_def + full scale - every full-scale tint is explicit now that
    # inheritance is gone) claims the line outright: `stack = 2 * stack_1`
    # bands in stack's comment blue, not a stack/stack_1 gradient. Only lines
    # with no explicit owner mix their span colors (propagation's blend
    # colors). Runs on the pre-strip 5-tuples for the at_own_def flag.
    line_mix = {}
    for sp in spans:
        ln = bisect.bisect_right(line_start_idx, sp[0]) - 1
        line_mix.setdefault(ln, []).append(sp)
    line_tints = []
    for ln, entries in line_mix.items():
        own = [e for e in entries if e[4] and e[3] >= 1.0]
        pick = own if own else entries
        uniq = list(dict.fromkeys((e[2], e[3]) for e in pick))
        n = len(uniq)
        rgb = tuple(sum(c[0][i] for c in uniq) / n for i in range(3))
        # Text extent (buffer indices of first/last non-ws char) - the glow
        # lights the text on the line, not the full editor width.
        lt = lines[ln] if 0 <= ln < len(lines) else ""
        ind = len(lt) - len(lt.lstrip())
        line_tints.append((ln, rgb, sum(c[1] for c in uniq) / n,
                           line_start_idx[ln] + ind,
                           line_start_idx[ln] + max(len(lt.rstrip()), ind + 1)))
    line_tints.sort()
    spans = [sp[:4] for sp in spans]
    # Longer spans first for equal starts: a dotted path records overlapping
    # sites (`Toggles`, `Toggles.TextEditor`, `Toggles.TextEditor.x`) and the
    # draw order is paint order - the member-chain wash goes down first, then
    # the base symbol's own color wins on its own token.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    # Exported name→rgb map for consumers outside the wash pipeline (the
    # autocomplete popup colors its candidate rows with it). Dotted symbols
    # also register with their last segment (setdefault - an explicit name wins)
    # so member completions after `Receiver.` match by the last attribute name.
    name_tints = {}
    for nm, t in name_tint.items():
        if t is not None:
            name_tints[nm] = tuple(t[0][:3])
    for nm, t in name_tint.items():
        if t is not None and "." in nm:
            name_tints.setdefault(nm.rsplit(".", 1)[-1], tuple(t[0][:3]))
    _pm("tail")
    _ptot = (time.perf_counter() - _pt0) * 1000.0
    if _ptot >= 100.0:
        _prev = _pt0
        _bd = []
        for _lbl, _tm in _pmarks:
            _bd.append(f"{_lbl}={(_tm - _prev) * 1000.0:.0f}")
            _prev = _tm
        _ptrace("def_tints SLOW rebuild", total_ms=round(_ptot, 1),
                cpu_ms=round((time.thread_time() - _pcpu0) * 1000.0, 1),
                lines=len(lines), sus=len(all_sus),
                xf_scans=_XFILE_SCAN_N[0] - _pxf0[0],
                xf_builds=_XFILE_SCAN_N[1] - _pxf0[1],
                live_scans=len(_scan_memo),
                verify_ms=round(_live_prof[0] * 1000.0, 1),
                scan_ms=round(_live_prof[1] * 1000.0, 1),
                xf_ms=round(_live_prof[2] * 1000.0, 1),
                worst=_live_prof[3], breakdown=" ".join(_bd))
    return (tuple(blocks), tuple(spans), tuple(line_tints), name_tints)


def _collect_comment_tints(text):
    """Comment-text tints: every override comment carrying tint=(...) gets its
    TEXT drawn in that color (no background) — the comment names a color, so
    it wears it. [(start_index, end_index, rgb)] covering the comment run (or
    the trailing-comment tail of an assignment line). Pure TEXT scan — no
    code_tree — so a freshly typed tint comment colors immediately instead of
    waiting for the cst-dict round trip like the definition washes do."""
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import _parse_override_comment
    if "tint=" not in text:
        return ()
    lines = text.split("\n")
    line_start_idx = [0]
    for l in lines:
        line_start_idx.append(line_start_idx[-1] + len(l) + 1)
    comment_tints = []
    bi = 0
    while bi < len(lines):
        s = lines[bi].strip()
        if s.startswith("#"):
            j = bi
            while j + 1 < len(lines) and lines[j + 1].strip().startswith("#"):
                j += 1
            run = lines[bi:j + 1]
            if any("tint=" in l for l in run):
                # Prose lines may sit above the override in a comment run -
                # find the longest parsing SUFFIX; only its lines get color.
                for k in range(len(run)):
                    parsed = _parse_override_comment(
                        "\n".join(l.strip() for l in run[k:]))
                    if parsed and _is_color(parsed.get("tint")):
                        sb = bi + k
                        comment_tints.append(
                            (line_start_idx[sb] + (len(lines[sb]) - len(lines[sb].lstrip())),
                             line_start_idx[j] + len(lines[j].rstrip()),
                             tuple(parsed["tint"])[:3]))
                        break
            bi = j + 1
            continue
        if "tint=" in lines[bi] and "#" in lines[bi]:
            p = lines[bi].find("#")
            parsed = _parse_override_comment(lines[bi][p:])
            if parsed and _is_color(parsed.get("tint")):
                comment_tints.append((line_start_idx[bi] + p,
                                      line_start_idx[bi] + len(lines[bi].rstrip()),
                                      tuple(parsed["tint"])[:3]))
        bi += 1
    return tuple(comment_tints)


def _splice_comment_tints(held, new_text, sp):
    """Incremental _collect_comment_tints through a single text splice: held
    spans before the edit keep, spans after shift by the char delta, and only
    the edited region — expanded to whole lines, then over the contiguous
    comment run around it (an edit can join/split runs, and the prose-suffix
    parse reads whole runs) — is rescanned. Any held span intersecting the
    region lies fully inside it (runs are contiguous, so the expansion
    swallows the whole run), which is what makes the keep/shift split exact.
    Region cost is O(edit + surrounding run), not O(buffer)."""
    p, old_end, d = sp[0], sp[1], sp[2]
    rs = new_text.rfind("\n", 0, p) + 1              # start of first touched line
    re_ = new_text.find("\n", max(p, old_end + d))   # end of last touched line
    if re_ == -1:
        re_ = len(new_text)
    while rs > 0:                                    # expand up over the comment run
        ls = new_text.rfind("\n", 0, rs - 1) + 1
        if new_text[ls:rs - 1].lstrip().startswith("#"):
            rs = ls
        else:
            break
    nlen = len(new_text)
    while re_ < nlen:                                # ...and down
        le = new_text.find("\n", re_ + 1)
        if le == -1:
            le = nlen
        if new_text[re_ + 1:le].lstrip().startswith("#"):
            re_ = le
        else:
            break
    ore = re_ - d               # old-coords region end (identical to text)
    kept, shifted = [], []
    for span in held:
        if span[1] <= rs:       # rs <= p, so text below p is coord-identical
            kept.append(span)
        elif span[0] >= ore:
            shifted.append((span[0] + d, span[1] + d, span[2]))
        # else: inside the rescanned region - replaced below
    add = [(si + rs, ei + rs, rgb)
           for (si, ei, rgb) in _collect_comment_tints(new_text[rs:re_])]
    return tuple(kept + add + shifted)


def _comment_tints(ds, text):
    """Cached-per-text wrapper around _collect_comment_tints. Unlike
    _def_tints there is no tree in the key and no debounce: its whole point
    is repainting the comment the frame it's edited. Keyed on text IDENTITY
    (every edit makes a new str; held by the ds so the id can't be reused).
    A keystroke takes the splice-incremental path (_splice_comment_tints) —
    the full sweep re-split and re-walked every buffer line per edit, a
    measured ~2.5ms of the edited frame on a big file."""
    if getattr(ds, "_comment_tints_text", None) is not text:
        prev = getattr(ds, "_comment_tints_text", None)
        held = getattr(ds, "_comment_tints", None)
        out = None
        if prev is not None and held is not None:
            sp = _text_splice(prev, text)
            if sp is not None:
                out = _splice_comment_tints(held, text, sp)
            else:
                out = held          # same text, new identity
        if out is None:
            out = _collect_comment_tints(text)
        ds._comment_tints = out
        ds._comment_tints_text = text
    return ds._comment_tints


def _def_tints(ds, text, code_tree, line_offset=0, view_path=None, vis=None,
               hold_live=True, world=None, table=None):
    """Cached-per-(code_tree, text) wrapper around _collect_def_tints — the
    exact key discipline of _usage_spans: the top-level __symbol_usages__
    map's identity rides in the key so the background usage pass's in-place
    arrival busts the cache.

    The gen key component is the TINT-relevant generation (_tint_gens —
    bumps only when a queued edit changed a tint line or a span's line
    count), excluding THIS file's own gen: typing here bumps gens once per
    keystroke (on the bg save thread, so the bump lands a frame AFTER the
    text-change miss), and keying on the raw total made every keystroke pay
    the O(buffer) collect twice — while a #[...] param drag in ANOTHER file
    bumped it per frame and forced mid-drag recomputes that dropped
    propagated washes. Own edits are already covered by `text` / tree
    identity; the gen term only needs to catch tint-affecting edits queued
    in OTHER files (read through the _XFILE PendingSave caches). A tint
    edit in another SPAN of this same file refreshes on the next tree/text
    churn instead of instantly.

    Recompute is additionally debounced (_TINT_RECOMPUTE_MIN_S): a key
    change inside the window serves the last-good result and re-requests a
    frame, so the trailing recompute lands once the window expires —
    fast typing pays the collect a few times a second, not per keystroke.
    Stale spans can sit a hair off the glyphs for that window; they're
    translucent washes, and the background parse churn already did this."""
    from src.lsd.gl_gui.toggles import Toggles
    # Roster mode (Toggles.TextEditor.roster_def_tints): washes come from the
    # text-derived symbol roster (roster_tints.collect_def_tints) - no
    # cst-dict, no __symbol_usages__. The key is the buffer text identity + the
    # roster generation (bumps when ANY file's table changed, incl. a def
    # typed in another file), so a new tinted def anywhere repaints this
    # after the debounce; the typing-hot hold + anchor remap below are shared.
    _roster = bool(Toggles.TextEditor.roster_def_tints) and view_path is not None
    if not _roster and not isinstance(code_tree, dict):
        return ((), (), (), {})
    # Hotswap shape check: a held result from before the comment-tints split
    # was a 5-tuple; drop it rather than serve it through the debounce path.
    if (getattr(ds, "_def_tints", None) is not None
            and len(ds._def_tints) != 4):
        ds._def_tints = None
        ds._def_tints_key = None
        ds._def_tints_raw = None
    su_top = code_tree.get("__symbol_usages__") if isinstance(code_tree, dict) else None
    _win = None
    if _roster:
        from src.lsd.gl_gui.view.core_conversion import symbol_roster as _sr
        _sr.sweep()          # throttled: notices pending/disk edits in OTHER files
        _sr.register_consumer(ds)   # a later roster change invalidates this editor
        # Viewport-only occurrence scan: the window is the visible band
        # quantized to _DT_WIN_CHUNK-line chunks with a chunk of margin each
        # side, so small scrolls stay inside the computed window and a
        # bigger one recomputes ONE cheap windowed pass (no debounce - see
        # below: only the window moved, the content key is unchanged).
        if vis is not None:
            _c = _DT_WIN_CHUNK
            _win = (max(0, (int(vis[0]) // _c - 1) * _c),
                    (int(vis[1]) // _c + 2) * _c - 1)
        # The world pane keys on the world's generation too (disk writes /
        # sync table moves in OTHER files don't move the studio generation)
        # and on its own table's identity.
        _ckey = (_DEF_TINTS_VER, "roster", _sr.generation(), id(text), line_offset,
                 str(view_path),
                 (world.name, world.generation()) if world is not None else None,
                 id(table) if table is not None else None)
        key = _ckey + (_win,)
    else:
        # NO text in the key - same reasoning as _usage_spans: text-only drift is
        # handled exactly by the splice remap; a recompute on a stale tree
        # re-verified stale sites against shifted text and blinked washes out on
        # every inserted newline. Fresh tree / symbol map / cross-file pending
        # gen still recompute.
        key = (_DEF_TINTS_VER, id(code_tree), id(su_top), line_offset,
               str(view_path), _tint_total_gen() - _tint_gen_of(view_path))
    # raw-missing: a hotswapped ds has only the legacy resolved tuple.
    if (getattr(ds, "_def_tints_key", None) != key
            or getattr(ds, "_def_tints_raw", None) is None):
        now = time.monotonic()
        # Symbol layer in flight (see _usage_spans): a merged tree whose
        # carried flat map hasn't been folded into per-node
        # __symbol_usages__ yet would collect ZERO sus - every propagated
        # symbol gone until the next good recompute. Hold the last-good
        # result instead; the attach's fresh su map busts the key.
        _su_pending = (not _roster and su_top is None
                       and getattr(code_tree, "symbol_usage", None))
        # A pure window move (same content key) recomputes right away: it's
        # a ~ms windowed scan and holding it would leave the freshly scrolled-
        # in lines unwashed for the debounce window.
        _win_only = (_roster and getattr(ds, "_def_tints_ckey", None) == _ckey)
        if (getattr(ds, "_def_tints", None) is not None and not _win_only
                and (_typing_hot() or _su_pending
                     or now - getattr(ds, "_def_tints_time", 0.0) < _TINT_RECOMPUTE_MIN_S)):
            request_render()   # typing/debounced: serve held (remapped below), retry later
        else:
            # Collect on the tree's own text, then RESOLVE onto the buffer -
            # same root-cause fix as _usage_spans (see the anchor block).
            _base = text
            _src = getattr(code_tree, 'source', None)
            if isinstance(_src, str) and _src != text:
                _cand = _text_splice(_src, text)
                if _cand is not None and (_cand[1] - _cand[0]) <= 512 and abs(_cand[2]) <= 512:
                    _base = _src
            try:
                if _roster:
                    from src.lsd.gl_gui.view.core_views.roster_tints import (
                        collect_def_tints as _roster_collect)
                    _base = text        # no tree: the buffer IS the base
                    # Per-line string state for the windowed pass's clean
                    # start: reuse the viewport tokenizer's incremental state
                    # when it's for this very buffer, else keep our own.
                    if getattr(ds, "_lo_text", None) is text:
                        _lo_open = ds._lo_open
                    else:
                        ds._dt_lo_offs, ds._dt_lo_open = _update_line_open(
                            getattr(ds, "_dt_lo_text", None),
                            getattr(ds, "_dt_lo_offs", None),
                            getattr(ds, "_dt_lo_open", None), text)
                        ds._dt_lo_text = text
                        _lo_open = ds._dt_lo_open
                    _fresh = _roster_collect(_base, line_offset, view_path,
                                             window=_win, line_open=_lo_open,
                                             hold_live=hold_live, world=world,
                                             table=table)
                    ds._def_tints_ckey = _ckey
                else:
                    _fresh = _collect_def_tints(code_tree, _base, line_offset, view_path)
            except Exception:
                import traceback
                traceback.print_exc()
                _fresh = ((), (), (), {})
            _blocks, _spans, _lts, _ = _fresh
            ds._def_tints_raw = _fresh
            ds._def_tints_base = _base
            ds._def_tints_anchors = _anchor_span_set(
                _span_entry_lines(_spans, _base),
                [b[0] for b in _blocks] + [b[2] for b in _blocks]
                + [lt[0] for lt in _lts],
                _base, code_tree)
            ds._def_tints = _resolve_def_tints(
                _fresh, ds._def_tints_anchors, _base, text)
            ds._def_tints_key = key
            ds._def_tints_time = now
            ds._def_tints_text = text
    # Re-resolve the held washes onto the edited buffer from the immutable raw
    # result (see _usage_spans) - displaced lines are re-found by content.
    prev_text = getattr(ds, "_def_tints_text", None)
    if prev_text is not None and prev_text is not text:
        _raw = getattr(ds, "_def_tints_raw", None)
        if _raw is not None:
            ds._def_tints = _resolve_def_tints(
                _raw, ds._def_tints_anchors, ds._def_tints_base, text)
        ds._def_tints_text = text
    return ds._def_tints


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


# `'#rgb'`, `'#rrggbb'` or `'#rrggbbaa'` between matching quotes, nothing else.
_HEX_COLOR_STRING_RE = re.compile(r"""^(['"])#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\1$""")


def _split_icons(s, base):
    """Split `s` into (substr, color_key) runs so PUA icon glyphs paint as 'icon'
    while the surrounding text keeps `base` — lets an icon embedded in a string
    token (the common case) stand out without recolouring the whole literal."""
    if base == 'string' and _HEX_COLOR_STRING_RE.match(s):
        # The string literal is a hex color (`'#8888c6'`, `"#fff"`, RGBA
        # `'#8888c680'`): its own kind so the inline swatch widget targets it.
        yield s, 'colorhex'
        return
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


# An override comment holds live values (`# [tint=(0.1, 0.2), nf_on=True]`,
# possibly split across several '#' lines - see _parse_override_comment in
# libcst_conversion). Matches LINE-LOCALLY, because viewport tokenization may
# start on a continuation line that never shows the opening '# [': a '#', an
# optional '[', then `key=` with nothing between the identifier and the '='
# (prose like `# x = 5 by default` stays a code comment) and no '==' anywhere.
_OVERRIDE_COMMENT_RE = re.compile(r'^#\s*\[?\s*[A-Za-z_]\w*=(?!=)')

# Widget-eligible kinds an override comment's values keep; every other
# sub-token (keys, brackets, strings, commas) is washed back to 'comment'.
_OVERRIDE_VALUE_KINDS = frozenset(('bool', 'number', 'color3', 'icon'))


def _tokenize_override_comment(comment):
    """Sub-tokenize an override-comment line so its VALUES get their
    widget-eligible kinds — 'bool' (double-click toggle), 'number' (drag),
    'color3' (swatch), 'icon' — while keys and punctuation keep the plain
    'comment' color. The body runs the same local pipeline as code (raw scan
    + sign and color-tuple merges), so `z_offset=-1` drags across zero and a
    color tuple — `tint=(0.1, 0.2, 0.3)`, RGBA, or mixed int/float like
    `tint=(0, 0.002, 0.004)` — merges into one color3 token. Token
    texts still concatenate to exactly `comment` (no newlines inside), so the
    vcols/caret math and line_open bookkeeping stay intact."""
    split = 1
    while split < len(comment) and comment[split] in ' \t':
        split += 1
    yield comment[:split], 'comment'
    for tok, kind in _merge_color_tuples(_merge_unary_signs(_tokenize_raw(comment[split:]))):
        yield tok, kind if kind in _OVERRIDE_VALUE_KINDS else 'comment'


def _tokenize_raw(text):
    """Yields (text, color_key) tuples with Darcula-style token categories.
    Raw pass — see tokenize() below for the unary-sign merge."""
    i = 0
    n = len(text)
    after_def = False   # last meaningful token was `def` - next word is a fn name

    while i < n:
        # --- Comments ---
        if text[i] == '#':
            end = text.find('\n', i)
            end = end if end != -1 else n
            comment = text[i:end]
            if _OVERRIDE_COMMENT_RE.match(comment):
                yield from _tokenize_override_comment(comment)
            else:
                yield comment, 'comment'
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
                # `def` keeps keyword coloring but has its own key (like
                # 'bool') so highlight_views can target function definitions.
                yield word, 'def' if word == 'def' else 'keyword'
            elif word in BUILTIN_PSEUDO:
                yield word, 'builtin_pseudo'
            elif after_def:
                yield word, 'def_name'
            elif word in BUILTINS and not (i > 0 and text[i - 1] == '.'):
                # `len(x)` but not `foo.len` - an attribute named after a
                # builtin is the object's own, not the builtin.
                yield word, 'builtin'
            else:
                yield word, 'default'
            after_def = word == 'def'
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
            if text[i] not in ' \t':
                after_def = False
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


def _is_int_channel(tok):
    """True for an int literal usable as a 0..1 color channel: exactly 0 or 1.
    Other ints are far more likely shapes/strides/counts than channels, so a
    tuple containing them never merges into a color token."""
    try:
        return int(tok) in (0, 1)
    except ValueError:
        return False


def _merge_color_tuples(stream):
    """Merge pass: `(c, c, c)` or `(c, c, c, c)` — an open paren, three or four
    numeric channels separated by commas (spaces allowed), closed on the same
    line — becomes one 'color3' token, rendered by the inline color-picker
    widget (four channels = RGBA, the fourth is alpha). A channel is a FLOAT
    literal or the ints 0/1 (mixed tuples like `(0, 0.002, 0.004)` are common
    color spellings), but at least one channel must be a float — an all-int
    tuple reads as a shape/stride, not a color. Only fires in tuple-literal
    positions (`x = (...)`, `tint=(...)`, `return (...)`), judged by the token
    before the '(' via _unary_sign_context — an identifier or closing bracket
    there means a CALL's argument list (`f(0.1, 0.5, 1.0)`), which stays
    untouched."""
    prev = None  # last significant token, for the call-vs-tuple judgement
    buf = []     # tokens collected since a candidate '('
    n_nums = 0
    has_float = False
    expect = None  # 'num' | 'comma' - alternates while buffering
    for tok, kind in stream:
        while True:
            if not buf:
                if tok == '(' and kind == 'default' and _unary_sign_context(prev):
                    buf = [(tok, kind)]
                    n_nums, has_float, expect = 0, False, 'num'
                else:
                    yield tok, kind
                    if not tok.isspace() and kind != 'comment':
                        prev = (tok, kind)
                break
            if tok == ' ':
                buf.append((tok, kind))
                break
            if (kind == 'number' and expect == 'num' and n_nums < 4
                    and (_is_float_literal(tok) or _is_int_channel(tok))):
                buf.append((tok, kind))
                n_nums += 1
                has_float = has_float or _is_float_literal(tok)
                expect = 'comma'
                break
            if tok == ',' and kind == 'default' and expect == 'comma' and n_nums < 4:
                buf.append((tok, kind))
                expect = 'num'
                break
            if (tok == ')' and kind == 'default' and expect == 'comma'
                    and n_nums in (3, 4) and has_float):
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


# --- String/comment STATE scanner ------------------------------------------
# `_tokenize_raw` is a per-token Python loop: ~35ms over a 200k-char line). The
# line_open bookkeeping (and the long-line column band below) only need to know
# where strings and comments are, not every identifier, so this scanner jumps
# between the characters that can change lexer state with C-speed regex
# searches: a loop iteration per quote/hash rather than per token. It
# replicates `_tokenize_raw`'s string rules exactly (tested against it in
# tests/test_incremental_tokenize.py): triple quotes close at the next literal
# triple (no escapes), single/double quotes accept backslash escapes and DO run
# across newlines until closed, comments end at the newline, a bare triple is
# 'string_doc' and a prefixed one (`r'''`, `f"""`) is 'string'.
_LEX_OUT_RE = re.compile('#|"""|\'\'\'|"|\'')
_LEX_SQ_RE = {'"': re.compile(r'\\.|"', re.DOTALL),
              "'": re.compile(r"\\.|'", re.DOTALL)}
_LEX_PREFIX = frozenset('fFrRbBuU')


def _quote_is_prefixed(text, q):
    """Would `_tokenize_raw` reach the quote at `q` through its string-PREFIX
    branch (`r"`, `rb"`, `f'''`)? A prefix letter only counts when the scanner
    lands ON it as a token start — glued to an identifier (`xr"`) the word
    branch swallows it, a decorator (`@r"`) swallows its dotted name, a number
    (`1_r"`) ends right before it. Exact: replays the tokenizer's decorator /
    number / word branches over the [alnum_.@] run that ends at the quote."""
    if q == 0 or text[q - 1] not in _LEX_PREFIX:
        return False
    j = q
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] in '_.@'):
        j -= 1
    n = len(text)
    pos = j
    while pos < q:
        ch = text[pos]
        if (ch in _LEX_PREFIX
                and (q == pos + 1
                     or (q == pos + 2 and text[pos + 1] in _LEX_PREFIX))):
            return True
        if ch == '@' and (pos == 0 or text[pos - 1] in '\n '):
            pos += 1
            while pos < n and (text[pos].isalnum() or text[pos] in '_.'):
                pos += 1
        elif ch.isdigit() or (ch == '.' and pos + 1 < n and text[pos + 1].isdigit()):
            end = pos
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
            pos = end
        elif ch.isalpha() or ch == '_':
            while pos < n and (text[pos].isalnum() or text[pos] == '_'):
                pos += 1
        else:
            pos += 1
    return False


def _iter_lex_spans(text, pos=0, state=None, stop=None):
    """Yield (start, end, state) for every string and comment token of
    `_tokenize_raw(text[pos:])` in order, where `state` is None for a comment
    and the (closing_quote, color_kind) pair for a string. `state` seeds a scan
    that begins INSIDE a string (the span then starts at `pos`). Stops early
    once a span would start at/after `stop`. Everything between spans is code."""
    n = len(text)
    if stop is None:
        stop = n
    if state is not None:
        q, _kind = state
        if len(q) == 3:
            c = text.find(q, pos)
            end = c + 3 if c != -1 else n
        else:
            end = n
            for m in _LEX_SQ_RE[q].finditer(text, pos):
                if m.group() == q:
                    end = m.end()
                    break
        yield pos, end, state
        pos = end
    search = _LEX_OUT_RE.search
    while pos < stop:
        m = search(text, pos)
        if m is None or m.start() >= stop:
            return
        i = m.start()
        tok = m.group()
        if tok == '#':
            end = text.find('\n', i)
            end = end if end != -1 else n
            yield i, end, None
        elif len(tok) == 3:
            pre = _quote_is_prefixed(text, i)
            c = text.find(tok, i + 3)
            end = c + 3 if c != -1 else n
            yield i, end, (tok, 'string' if pre else 'string_doc')
        else:
            end = n
            for mm in _LEX_SQ_RE[tok].finditer(text, i + 1):
                if mm.group() == tok:
                    end = mm.end()
                    break
            yield i, end, (tok, 'string')
        pos = end


def _iter_newline_states(text, pos=0, state=None):
    """Yield (offset_after_newline, line_open_state) for every newline of
    `text[pos:]`, in order — the state a line START inherits: None when the
    newline is code or ends a comment, the string's (quote, kind) when it sits
    inside a string. Lazy, so callers can stop at reconvergence."""
    find = text.find
    for a, b, st in _iter_lex_spans(text, pos, state):
        nl = find('\n', pos, a)
        while nl != -1:
            yield nl + 1, None
            nl = find('\n', nl + 1, a)
        if st is not None:
            nl = find('\n', a, b)
            while nl != -1:
                yield nl + 1, st
                nl = find('\n', nl + 1, b)
        pos = b
    nl = find('\n', pos)
    while nl != -1:
        yield nl + 1, None
        nl = find('\n', nl + 1)


def _lex_state_at(text, pos, state, target):
    """Lexer state at offset `target` given the scan starts at `pos` in `state`:
    (None, target) for code, ('comment', span_start) inside a comment, or
    ((quote, kind), span_start) inside a string."""
    for a, b, st in _iter_lex_spans(text, pos, state, stop=target + 1):
        if a <= target < b:
            return ('comment' if st is None else st), a
        if a > target:
            break
    return None, target


def _line_open_full(text):
    """(line_offsets, line_open) computed from scratch. line_open[i] is the
    string state active at the START of line i — None outside any string, else
    a (closing_quote, color_kind) pair for the multi-line string spanning into
    the line. The kind is carried because a PREFIXED triple (`r'''…`, `f\"\"\"…`)
    colors as 'string', not 'string_doc' — only a bare triple is 'string_doc'.
    Agrees with `tokenize()` exactly (`_line_open_full_ref` is the reference
    derived straight from `_tokenize_raw`; the scanner is tested against it).
    O(quotes), not O(tokens); used on first render, then maintained
    incrementally."""
    offs = _line_offsets(text)
    line_open = [None] * len(offs)
    line = 0
    for _off, st in _iter_newline_states(text):
        line += 1
        if st is not None and line < len(line_open):
            line_open[line] = st
    return offs, line_open


def _line_open_full_ref(text):
    """Reference implementation of `_line_open_full` via `_tokenize_raw` (the
    scanner must match this char-for-char; see tests/test_incremental_tokenize)."""
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
    stop_old = None
    for off, state in _iter_newline_states(text, start_off):
        if state is None and off >= new_hi:
            oc = old_clean.get(off - delta)   # same clean line in the old tail?
            if oc is not None:
                stop_old = oc                 # reconverged → reuse old suffix
                break
        tail.append(state)

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


def _window_tokens(text, line_offs, line_open, v0, v1, lookback=12, band=None):
    """Merged tokens for just the line range [v0, v1] (plus `lookback` lines of
    context above, so the merge passes have correct left-context for v0's first
    line), and the absolute (start_line, start_offset) the first token sits at.
    The per-character coloring matches the corresponding span of
    `list(tokenize(text))` — including windows that open inside a docstring.

    `band=(c0, c1, long_cols)` clips LONG lines horizontally too: any line in
    the range longer than `long_cols` chars is tokenized only over columns
    [c0, c1) (the visible span plus margin); the stretches either side come
    back as single ('...', 'clipped') tokens of the exact source length, so
    consumers that walk offsets stay exact while the glyph loop / vcols skip
    them in O(1). The lexer state at the cut comes from `_lex_state_at`, so a
    band opening mid-string or mid-comment colors correctly; the merge passes
    see only `lookback`-style local context at the left cut (a color tuple or
    unary sign straddling the cut is covered by the band margin)."""
    nlines = len(line_offs)
    if nlines == 0:
        return 0, 0, []
    v0 = max(0, min(v0, nlines - 1))
    v1 = max(v0, min(v1, nlines - 1))
    wl = max(0, v0 - lookback)
    start_off = line_offs[wl]
    end_off = line_offs[v1 + 1] if v1 + 1 < nlines else len(text)
    opener = line_open[wl] if wl < len(line_open) else None
    if band is not None:
        c0, c1, long_cols = band
        # Long lines in the range, segment the tokens at them; short runs in
        # between tokenize as one chunk (their line_open seeds the resume).
        long_lines = [ln for ln in range(wl, v1 + 1)
                      if ((line_offs[ln + 1] - 1 if ln + 1 < nlines else len(text))
                          - line_offs[ln]) > long_cols]
        if long_lines:
            return wl, start_off, _banded_tokens(
                text, line_offs, line_open, wl, v1, end_off, long_lines, c0, c1)
    body = text[start_off:end_off]
    toks = _resume_in_string(body, opener) if opener else list(tokenize(body))
    return wl, start_off, toks


def _tokenize_from(body, state):
    """Tokenize `body` given the lexer state at its start: None → code,
    'comment' → the rest of the line is one comment token, (quote, kind) →
    resume inside that string."""
    if not body:
        return []
    if state is None:
        return list(tokenize(body))
    if state == 'comment':
        nl = body.find('\n')
        if nl == -1:
            return [(body, 'comment')]
        return [(body[:nl], 'comment')] + list(tokenize(body[nl:]))
    return _resume_in_string(body, state)


def _banded_tokens(text, line_offs, line_open, wl, v1, end_off, long_lines, c0, c1):
    """Token list for lines [wl, v1] where `long_lines` (ascending) are cut to
    columns [c0, c1) — see _window_tokens. Short stretches between long lines
    are tokenized whole; each long line becomes clipped-prefix, band tokens,
    clipped-suffix (+ its newline)."""
    nlines = len(line_offs)
    out = []
    pos = line_offs[wl]
    for ln in long_lines:
        ls = line_offs[ln]
        le = line_offs[ln + 1] - 1 if ln + 1 < nlines else len(text)
        # Short lines since the last cut, opening in their first line's state.
        if ls > pos:
            st_line = bisect.bisect_right(line_offs, pos) - 1
            out.extend(_tokenize_from(text[pos:ls], line_open[st_line]))
        a = ls + max(0, c0)
        b = min(le, ls + c1)
        if a >= le:
            a = b = le
        elif b < a:
            b = a
        state = line_open[ln]
        if a > ls:
            out.append((text[ls:a], 'clipped'))
            state, _span_start = _lex_state_at(text, ls, state, a)
        # Tokenize the band alone (no trailing newline), then clip the rest.
        btoks = _tokenize_from(text[a:b], state)
        out.extend(btoks)
        if b < le:
            out.append((text[b:le], 'clipped'))
        if le < end_off:
            out.append(('\n', 'default'))
        pos = le + 1
    if pos < end_off:
        st_line = bisect.bisect_right(line_offs, pos) - 1
        out.extend(_tokenize_from(text[pos:end_off], line_open[st_line]))
    return out


_LINE_STARTS_CACHE: dict = {}   # id(text) -> (text, [line-start char offsets])


def _line_offsets_cached(text):
    """`_line_offsets(text)` through the identity memo below (same list shape:
    the start offset of every line, offs[0] == 0)."""
    return _line_starts(text)


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


def _build_vcols(text, tokens, token_views, pos_trails=None):
    """Per-source-char VISUAL-COLUMN map for inline token-view widths. Returns a
    list `vcols` (len = len(text)+1) where vcols[i] = the visual column (in cells,
    line-relative — reset after each '\\n') at which source char i starts. A
    char_width-N inline widget thus reserves N cells visually while staying ONE
    source character for editing/caret. Returns None when no inline views apply
    (the fast path: 1 char == 1 cell everywhere).

    `pos_trails` maps a WINDOW-RELATIVE char index to a cell count inserted
    right BEFORE that char — a POSITIONAL trailing accessory (the live-usage
    value labels: the gap opens after a specific symbol occurrence rather
    than after every token of a kind). The rest of the line shifts right by
    the inserted cells, exactly like a trail_cells view."""
    # Whole-token widgets are exactly token-width (1 cell per source char), so
    # they don't disturb the column map - except lead_cells / trail_cells
    # accessory views, which shift the token's text (lead) or the rest of
    # the line (trail) right. Only per-char views with a widened char_width
    # and lead/trail views need vcols at all.
    def _bends_grid(v):
        if not isinstance(v, dict) or v.get("char_width") is None:
            return False
        if v.get("whole_token"):
            return bool(v.get("lead_cells") or v.get("trail_cells"))
        return True
    if not pos_trails:
        if not token_views or not any(
                isinstance(k, str) and _bends_grid(v) for k, v in token_views.items()):
            return None
        # Only the VISIBLE tokens matter: if none of them carries a grid-bending
        # view there is nothing to map (the common case - and on a long-line
        # window it skips creating a per-char array the size of the buffer).
        if not any(ck != 'clipped' and _bends_grid(token_views.get(ck))
                   for _, ck in tokens):
            return None
    n = len(text)
    vcols = [0.0] * (n + 1)
    col = 0.0
    i = 0
    for tok, ck in tokens:
        if ck == 'clipped':
            # Off-screen stretch of a wrapped line: identity columns, assigned
            # at C speed (a 200k-char run would still cost ~10ms here).
            # A positional gap can start a clipped token (tokens are split
            # at every gap boundary), so honor it here too.
            if pos_trails is not None:
                _pt = pos_trails.get(i)
                if _pt:
                    col += _pt
            L = min(len(tok), n - i)
            ic = int(col)
            vcols[i:i + L] = range(ic, ic + L) if ic == col else [col + k for k in range(L)]
            col += L
            i += L
            continue
        view = (token_views.get(ck)
                if token_views and isinstance(ck, str) else None)
        cw = view.get("char_width") if (view and view.get("char_width") is not None) else None
        trail = 0
        if cw is not None and view.get("whole_token"):
            # Token text is 1 cell per char; an accessory view's lead_cells
            # shift where the text starts (the widget sits in the same area),
            # a trailing view's trail_cells shift what FOLLOWS the token.
            if tok and '\n' not in tok:
                col += view.get("lead_cells", 0)
                trail = view.get("trail_cells", 0)
            cw = None
        for ch in tok:
            if i >= n:
                break
            if pos_trails is not None:
                _pt = pos_trails.get(i)
                if _pt:
                    col += _pt      # positional gap right BEFORE this char
            vcols[i] = col
            col = 0.0 if ch == '\n' else col + (cw if cw is not None else 1.0)
            i += 1
        col += trail
    if pos_trails is not None:
        _pt = pos_trails.get(n)
        if _pt:
            col += _pt              # gap opens at the window's end
    vcols[n] = col
    return vcols


def _split_tokens_at(toks, boundaries):
    """Split (text, key) window tokens so every window-relative index in
    `boundaries` (sorted) STARTS a token. The glyph pass inserts the
    positional live-usage gaps by testing token STARTS only — splitting
    here keeps that test out of the per-char hot paths and lets the
    merged-run fast path stay intact (a gap token never joins a run)."""
    out = []
    i = 0
    bi = 0
    n = len(boundaries)
    for tok, ck in toks:
        end = i + len(tok)
        while bi < n and boundaries[bi] <= i:
            bi += 1
        start = i
        while bi < n and boundaries[bi] < end:
            cut = boundaries[bi]
            out.append((tok[start - i:cut - i], ck))
            start = cut
            bi += 1
        out.append((tok[start - i:], ck))
        i = end
    return out


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


def _block_open_extra(text, pos, block_indent=4):
    """Extra indent Enter adds when the code BEFORE `pos` on its line opens a
    block — `def f():`, `class C:`, `if x:` ... — i.e. the line's code part
    (trailing comment stripped, quotes honoured) ends with ':' and nothing
    remains after the caret but whitespace / a comment. 0 otherwise. The
    caller has already ruled out an unclosed bracket (a `:` inside one is a
    slice or a dict, not a block)."""
    head = _guide_code_part(text[_get_line_start(text, pos):pos].strip())
    if not head.endswith(':'):
        return 0
    rest = text[pos:_get_line_end(text, pos)].strip()
    if rest and not rest.startswith('#'):
        return 0
    return block_indent


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


def _is_highlightable_word(word):
    """True for a word the caret token-match wash should light: a plain
    identifier. Keywords, `True`/`False`/`None`, `and`/`or`/`not`/`in`/`is`
    and number literals (a leading digit — `_word_under_cursor` lumps digits
    into 'word') are not symbols and never highlight."""
    if not word or word[0].isdigit():
        return False
    return not (word in KEYWORDS or word in KEYWORD_CONSTS
                or word in OPERATOR_WORDS or word in _PY_KEYWORDS)


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
    def _notify_scroll(kind, node, old_sy, new_sy):
        pass
        # # Audit trail for flaky scroll-to-line: every programmatic scro
        # write lands in the notification center under the "scroll" tag,
        # with the estimated editor line the band corresponds to a
        # from src.gsd.gl_gui.notifications import notify
        # lp = getattr(ds, '_diff_line_px', None)
        # inset = getattr(ds, '_diff_top_inset', 0) or 0
        # est_line = None
        # if lp:
        #     # Invert band → line with the scroll that was in e
            # caller computed top_abs (the write above has moved node).ffect when the
        #     _scroll_at_calc = old_sy if node is ds else ds.scroll_offset[1]
        #     est_line = round((top_abs - ds.abs_top - inset
        #                       + _sy_at_calc) / lp) + 1
        # notify(f"scroll {kind} node={getattr(node, 'name', None)} "
        #        f"ds={getattr(ds, 'name', None)} line≈{est_line} "
        #        f"y={old_sy:.0f}->{new_sy:.0f} band=({top_abs:.0f},{bottom_abs:.0f}) "
        #        f"view=({node.abs_top + node.header_height:.0f},"
        #        f"{node.abs_top + (node.height or 0):.0f}) "
        #        f"max_y={getattr(node, '_max_scroll_y', None)} center={center}",
        #        tag="scroll")

    node = ds
    seen = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if getattr(node, 'scroll_visible', False):
            view_top = node.abs_top + node.header_height
            view_bottom = node.abs_top + (node.height or 0)
            sx, sy = node.scroll_offset
            if center:
                # Already fully visible (inside the margins)? Leave the scroll
                # alone. Centering unconditionally rewrote scroll_offset on
                # every search keystroke - mid-render, while invalidated
                # ancestors were still remeasuring - so the view jittered by a
                # frame each keypress. Only recenter when the band is actually
                # out-of the viewport.
                if (top_abs >= view_top + margin
                        and bottom_abs <= view_bottom - margin):
                    return
                # Align the band's midline with the viewport's midline (Y only,
                # horizontal sx untouched). Clamped at the content ends below.
                delta = ((top_abs + bottom_abs) * 0.5) - ((view_top + view_bottom) * 0.5)
                if abs(delta) > 0.5:
                    __old_scroll = node.scroll_offset
                    node.scroll_offset = (sx, max(0, min(sy + delta, node._max_scroll_y)))
                    _notify_scroll("center", node, sy, node.scroll_offset[1])
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
                _notify_scroll("nudge-up", node, sy, node.scroll_offset[1])
                request_render()
            elif bottom_abs > view_bottom - margin:
                current_x = node.scroll_offset[0]
                new_offset = sy + (bottom_abs - (view_bottom - margin))
                __old_scroll = node.scroll_offset
                node.scroll_offset = (current_x,
                                      max(0, min(new_offset, node._max_scroll_y)))
                _notify_scroll("nudge-down", node, sy, node.scroll_offset[1])
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


def _top_level_chunks(lines):
    """Start indices of the top-level statement chunks of dedented `lines`:
    a column-0 line at bracket depth 0 (strings / comments skipped), and
    always a `def` / `class` / `@` / `import` / `from` line (they can't sit
    inside a bracket, so an unclosed one above stops swallowing the file).
    A decorator run joins the def below it."""
    starts = []
    depth = 0
    quote = None
    deco_open = False     # the current chunk is a decorator run awaiting its def
    for idx, ln in enumerate(lines):
        head = ln[:1]
        # Clauses of the statement above (`except:` / `else:` / `elif` /
        # `finally` / `case`) continue its chunk.
        _clause = ln.startswith(('except', 'else', 'elif', 'finally', 'case '))
        if head and not head.isspace() and head not in ')]}#' and quote is None and not _clause:
            if depth == 0 or ln.startswith(('def ', 'class ', '@', 'import ', 'from ', 'async def ')):
                depth = 0
                quote = None
                is_def = ln.startswith(('def ', 'class ', 'async def '))
                if deco_open:
                    deco_open = not is_def      # join; a def closes the run
                else:
                    starts.append(idx)
                    deco_open = ln.startswith('@')
        k, n = 0, len(ln)
        while k < n:
            c = ln[k]
            if quote is None:
                if c == '#':
                    break
                if ln.startswith(('"""', "'''"), k):
                    quote = ln[k:k + 3]
                    k += 3
                    continue
                if c in '"\'':
                    quote = c
                elif c in '([{':
                    depth += 1
                elif c in ')]}':
                    depth = max(0, depth - 1)
            elif c == '\\':
                k += 1
            elif ln.startswith(quote, k):
                k += len(quote)
                quote = None
                continue
            k += 1
        if quote is not None and len(quote) == 1:
            quote = None      # a single-quoted string never spans lines
    return starts


def _mask_error_line(lines, idx):
    """`lines` with line `idx` replaced by a harmless statement at its own
    indent — `if 1:` when a deeper-indented line follows (the broken line
    opened a block), else `pass` — so the chunk can be re-checked for the
    NEXT error past this one."""
    ln = lines[idx]
    indent = len(ln) - len(ln.lstrip())
    nxt_indent = None
    for k in range(idx + 1, len(lines)):
        if lines[k].strip():
            nxt_indent = len(lines[k]) - len(lines[k].lstrip())
            break
    stub = 'if 1:' if (nxt_indent is not None and nxt_indent > indent) else 'pass'
    out = list(lines)
    out[idx] = ' ' * indent + stub
    return out


def _compile_check_more(text, first_error, max_more=8, per_chunk=4):
    """Further SyntaxErrors beside `first_error` (compile() reports only one,
    and not the topmost): compile every top-level chunk (`_top_level_chunks`)
    on its own; after each error the offending line is masked
    (`_mask_error_line`) and the chunk re-checked, up to `per_chunk` errors
    per chunk — so several mistakes inside ONE def are all found. Results in
    line order, `lineno` in `text`'s coordinates, the first error's own line
    left out. A masked / cut construct can report a spurious follow-up; rare
    in practice."""
    import textwrap
    from src.lsd.gl_gui.view.core_conversion.new_converters import _compile_check
    lines = textwrap.dedent(text).split('\n')
    starts = _top_level_chunks(lines)
    first_ln = getattr(first_error, 'lineno', None) or 0
    found = []
    for n, s0 in enumerate(starts):
        s1 = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = lines[s0:s1]
        seen = set()
        for _ in range(per_chunk):
            err = _compile_check('\n'.join(chunk))
            if err is None or getattr(err, 'lineno', None) is None:
                break
            rel = err.lineno - 1
            if rel in seen or not (0 <= rel < len(chunk)):
                break
            seen.add(rel)
            err.lineno = err.lineno + s0
            if err.lineno != first_ln:
                found.append(err)
            chunk = _mask_error_line(chunk, rel)
        if len(found) >= max_more:
            break
    found.sort(key=lambda e: e.lineno)
    return found[:max_more]


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


def _missing_name(msg):
    """The undefined name a lint marker reports ("name 'np' is not defined
    ...") — '' when the message has another shape."""
    m = str(msg)
    if m.startswith("name '"):
        end = m.find("'", 6)
        if end > 6:
            return m[6:end]
    return ""


def _apply_import_fix(stmt, jump_to, text):
    """Apply one missing-import quick-fix statement. Returns (changed, text).

    Whole-file buffer (no span on the address): insert the statement into the
    BUFFER after its leading import block and return the new text — the
    editor's normal edit→save path persists it, and the reparse clears the
    marker statically.

    Span buffer (a function/class edited on its own): the import belongs at
    the top of the FILE, outside the buffer. Two coordinated effects:
      * exec the statement into every live module loaded from the file (both
        src.-prefixed identities — see the dual-identity memory), so the code
        actually runs and the next lint pass sees the name bound;
      * queue the import as a REAL PendingSave entry — a zero-width span at
        the end of the file's leading import block (disk coordinates), whose
        data is the statement. It shows up in the pending diff immediately,
        current_file_text splices it for every whole-file consumer, and the
        regular flush (apply_all_saves) writes it like any other edit. One
        accumulating entry per file (marked `_auto_import`) so several fixes
        never collide on the same address; its source is a plain marker
        string so recompile_all skips it (the exec above already did the live
        half), with the module riding on `_shift_source` so codec.save still
        shifts live linenos when the write lands."""
    from src.lsd.gl_gui.view.core_conversion.chain_converters import (
        _ensure_import_lines)
    path = getattr(jump_to, 'path', None)
    start = getattr(jump_to, 'start', None)
    if path is None or start is None:
        lines = text.split('\n')
        new_lines, inserted, _ = _ensure_import_lines(lines, stmt)
        if not inserted:
            return False, text
        return True, '\n'.join(new_lines)

    # ── Span buffer: live-module exec + pending file-top insert ──────────────
    import os
    import sys as _sys
    target = str(path)
    try:
        target_real = os.path.realpath(target)
    except OSError:
        target_real = target
    live_mod = None
    for mod in list(_sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f is not None and (f == target or f == target_real):
            try:
                exec(compile(stmt, "<auto-import>", "exec"), vars(mod))
            except Exception:
                continue            # a failing import must never break the session
            try:
                if live_mod is None or len(vars(mod)) > len(vars(live_mod)):
                    live_mod = mod  # richest identity is the one that really ran
            except TypeError:
                pass

    try:
        from pathlib import Path as _P
        from src.lsd.gl_gui.melty import Melty
        from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
        from src.lsd.gl_gui.view.core_conversion.new_codecs import (
            TypeCodec, _span_fingerprint)
        from src.lsd.gl_gui.view.core_conversion.address import Address
        rp = _P(target_real)
        # Dedup against the file as it WOULD save - disk plus every queued
        # edit (including an earlier auto-import entry).
        merged = PendingSave.current_file_text(rp)
        if merged is not None:
            _, _would_insert, _ = _ensure_import_lines(merged.split('\n'), stmt)
            if not _would_insert:
                return False, text
        # One accumulating entry per file: a second fix appends its line.
        for addr, (codec, kw) in list(PendingSave.pending_saves.items()):
            if getattr(addr, '_auto_import', False) and addr.path == rp:
                data = kw.get('data') or ''
                if stmt not in data.split('\n'):
                    new_data = (data + '\n' + stmt) if data else stmt
                    PendingSave.queue_save(addr, codec,
                                           **{**kw, 'data': new_data})
                return False, text
        # Fresh entry: insertion point in DISK lines (pending entries splice
        # into disk text - see current_file_text / apply_all_saves).
        disk = Melty.read_code(rp)
        if disk is None:
            return False, text
        disk_lines = disk.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        _, _ins, _idx = _ensure_import_lines(disk_lines, stmt)
        if not _ins:
            return False, text
        address = Address(rp, start=_idx, end=_idx, source=f"auto-import:{rp}")
        address._auto_import = True
        address._shift_source = live_mod
        address._span_fp = _span_fingerprint([])   # a zero-width span is empty
        PendingSave.originals[address] = ""
        PendingSave.queue_save(address, TypeCodec, data=stmt)
    except Exception:
        pass                        # the live exec above already fixed the session
    return False, text


# How many import-shortcut rows the popup appends after the scope candidates -
# past a handful they're noise (the prefix narrows them fast anyway).
_AC_MAX_IMPORT_ROWS = 8


# Candidate kinds that mean "the buffer/scope really accounts for this name" -
# they exclude an import shortcut for it. A plain "name" row (the unclassified
# buffer-identifier scan) does NOT: for an importable symbol it's the useless
# default, so it upgrades to the import row instead of sitting above it.
_AC_REAL_KINDS_EXCLUDED = ("name",)


def _ac_import_rows(ds, cands, prefix, jump_to=None):
    """Merge import shortcuts (code_checks.project_importables — the main
    package's classes/modules) into the candidate list, returning the new
    list. A plain unclassified "name" row matching an importable UPGRADES in
    place to the import row (same rank, one row — never a dead default above
    a live one); importables with no row at all append after the scope
    candidates. Names with a real kind anywhere in the scope pool (local,
    param, already imported, …) suppress their shortcut. Accepting an import
    row completes the name AND inserts its import (_ac_apply_auto_import).
    Stamps name→stmt on the draw_state for the accept paths — cleared here
    and by the other candidate branches so a stale map never fires on a
    same-named ordinary pick."""
    ds._ac_import_stmts = None
    if not prefix or len(prefix) < 2:
        return cands
    try:
        from src.lsd.gl_gui.view.core_conversion.code_checks import (
            project_importables, _module_text_binds)
        rows, stmts = project_importables()
    except Exception:
        return cands
    real = {n for n, k in cands if k not in _AC_REAL_KINDS_EXCLUDED}
    real |= {n for n, k in (getattr(ds, '_ac_pool', None) or ())
             if k not in _AC_REAL_KINDS_EXCLUDED}
    # Names the FILE already binds at module scope (pending-save imports)
    # never get a shortcut - crucial for span buffers, whose import block
    # lives at the file top, outside the buffer the scope pool can see.
    path = getattr(jump_to, 'path', None)
    if path is not None:
        try:
            real |= _module_text_binds(path) or set()
        except Exception:
            pass
    picked = {}
    merged = []
    for n, k in cands:
        if k in _AC_REAL_KINDS_EXCLUDED and n not in real and n in stmts:
            merged.append((n, "auto_import"))
            picked[n] = stmts[n]
        else:
            merged.append((n, k))
    have = {n for n, _ in merged}
    p = prefix.lower()
    extra = 0
    for n, k in rows:
        if (n not in have and n not in real and n.lower().startswith(p)):
            merged.append((n, k))
            picked[n] = stmts[n]
            extra += 1
            if extra >= _AC_MAX_IMPORT_ROWS:
                break
    if picked:
        ds._ac_import_stmts = picked
    return merged


def _ac_apply_auto_import(ds, pick, jump_to, text):
    """When the accepted `pick` was an import-shortcut row, insert its import
    statement through the quick-fix machinery (_apply_import_fix: buffer
    insert for whole-file buffers, live exec + pending file-top entry for
    span buffers) and shift the caret past any text inserted above it.
    Returns the (possibly grown) buffer text; a no-op for ordinary picks."""
    stmt = (getattr(ds, '_ac_import_stmts', None) or {}).get(pick)
    if not stmt:
        return text
    fx_changed, fx_text = _apply_import_fix(stmt, jump_to, text)
    if not fx_changed:
        return text
    p, m = 0, min(len(text), len(fx_text))
    while p < m and text[p] == fx_text[p]:
        p += 1
    if p <= ds.text_cursor_pos:
        ds.text_cursor_pos += len(fx_text) - len(text)
    return fx_text


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



# NOT `_SCOPE_HEAD_RE` - that name is the completion pool's group-capturing
# scope regex defined near the top of this module; a module-level redefine
# here silently clobbered it and broken `_blank_foreign_scopes` (m.group(1)
# → IndexError). This one matches STRIPPED lines and needs no groups.
_FOLD_SCOPE_HEAD_RE = re.compile(r'(?:async\s+)?(?:def|class)\s')
_FOLD_SCOPE_NAME_RE = re.compile(r'(?:async\s+)?(?:def|class)\s+(\w+)')
_FOLD_IMPORT_RE = re.compile(r'(?:import|from)\s')
# Compound-statement block headers (keyword first, a ':' last before any
# trailing comment). A multiline header (`if (a and\n b):` deliberately
# doesn't match: its first line has no ':' - a it just doesn't fold.
_FOLD_BLOCK_RE = re.compile(
    r'(?:(async)\s+)?(if|elif|else|for|while|try|except|finally|with|match|case)'
    r'\b.*:\s*(?:#.*)?$')


# Bump when _scope_fold_ranges()` default_collapsed SOURCES change so
# already-seeded (session-lived) editors only the new defaults in once.
# v2: multiline comment runs joined imports as default-collapsed.
# v3: string scanner fixed (quoted triple-quotes no longer suppress
#     comment-run detection below them), so re-union the recovered runs.
# v4: multiline strings (docstrings / GLSL blocks) joined default_collapsed.
# v5: single-line `# [...]` metadata comments (a (s, s) range, laid out only
#     as a header-less fold) joined default_collapsed.
_FOLD_SEED_VER = 5


_FOLD_OUT_RE = re.compile("[#'\"]")
_FOLD_CLOSE_RE = {d: re.compile(r"\\.|" + re.escape(d))
                  for d in ('"', "'", '"""', "'''")}


def _guide_code_part(stripped):
    """`stripped` minus a trailing `# comment` (quotes honoured), rstripped."""
    quote = None
    i, n = 0, len(stripped)
    while i < n:
        c = stripped[i]
        if quote is None:
            if c == '#':
                return stripped[:i].rstrip()
            if c in '"\'':
                quote = c
        elif c == '\\':
            i += 1
        elif c == quote:
            quote = None
        i += 1
    return stripped.rstrip()


def _scope_guide_tints(segments, blocks):
    """{segment: rgb} for every guide whose head line lies inside a
    definition-tint block — the INNERMOST such block, `blocks` being the
    `_dt_blocks` 4-tuples `(start_line, char_idx, end_line, tint)` the
    washes paint (start = the decorator run's first line). One merged
    sweep over both sorted lists; segments outside every block are absent
    from the result (they take the file / neutral colour)."""
    sorted_blocks = sorted(((_b[0], _b[2], tuple(_b[3][:3]))
                            for _b in blocks if _b[3]),
                           key=lambda _b: (_b[0], -_b[1]))
    tints, open_blocks, next_block = {}, [], 0
    for segment in segments:
        head = segment[0]
        while next_block < len(sorted_blocks) and sorted_blocks[next_block][0] <= head:
            open_blocks.append(sorted_blocks[next_block])
            next_block += 1
        while open_blocks and open_blocks[-1][1] < head:
            open_blocks.pop()
        owner = next((_b for _b in reversed(open_blocks)
                      if _b[0] <= head <= _b[1]), None)
        if owner is not None:
            tints[segment] = owner[2]
    return tints


def _scope_guide_segments(text):
    """Indent-guide segments for `text`: (head_line, end_line, column) per
    indented BLOCK — `head_line` is a code line ending in ':' (a trailing
    comment allowed) whose next non-blank line is indented deeper,
    `column` its indent (chars), `end_line` the last non-blank line before
    the indent drops back to the head's level or less. The ':' rule keeps
    wrapped statements (a long call's continuation lines, an indented
    comment before one) from reading as scopes. Purely line-based like
    _scope_fold_ranges: O(lines), works mid-edit on broken buffers.
    Sorted by head line."""
    segments = []
    stack = []                  # (indent, head_line) of the open blocks
    # The current logical STATEMENT: a deeper-indented line after a line
    # that did not open a block continues it (a wrapped call, a multi-line
    # def signature), so a block whose ':' closes a wrapped header is keyed
    # on the statement's FIRST line - the `def` line the definition washes
    # use - at the statement's indent.
    stmt_line = stmt_indent = None
    prev_opens = False
    last_code = -1
    for i, line in enumerate(text.split('\n')):
        stripped = line.lstrip()
        if not stripped:
            continue
        indent = len(line) - len(stripped)
        if prev_opens and stmt_indent is not None and indent > stmt_indent:
            stack.append((stmt_indent, stmt_line))
            stmt_line, stmt_indent = i, indent
        elif stmt_indent is None or indent <= stmt_indent or prev_opens:
            while stack and indent <= stack[-1][0]:
                open_indent, head = stack.pop()
                segments.append((head, last_code, open_indent))
            stmt_line, stmt_indent = i, indent
        # Else: a continuation line of the current statement
        last_code = i
        prev_opens = (not stripped.startswith('#')
                      and _guide_code_part(stripped).endswith(':'))
    while stack:
        open_indent, head = stack.pop()
        segments.append((head, last_code, open_indent))
    segments.sort()
    return segments


def _scope_fold_ranges(text):
    """(ranges, default_collapsed, key_of) fold sources for `text` — the
    scope_collapse=True feed for draw_text's fold layer. key_of maps each
    range tuple to a line-independent identity (scope qualname path,
    comment/string first-line text, the import-block constant): collapse
    state persists as these keys (ds._fold_keys), never as line numbers, so
    edits that shift lines can't orphan a collapsed fold and the scan itself
    may be debounced off the keystroke path. Line/indentation
    based rather than ast.parse on purpose: it's O(lines), and it keeps
    working on the syntactically broken buffers every mid-edit frame
    produces, where a parse-based scan would go stale per keystroke.

    Five sources:
      scopes — every def / async def / class keeps its header (decorators
        stay above, visible) and hides down to its last non-blank body line;
        nested scopes each get their own range (the normalizer accepts
        strict nesting).
      blocks — if/elif/else/for/while/try/except/finally/with/match/case
        (Toggles.TextEditor.block_fold_ranges). Same header-kept, body-hidden
        shape as scopes, nested on the same indent stack, so `else:` at the
        `if`'s indent pops the `if` fold and opens its own. Keyed on the
        enclosing def/class qualname + the header text (blocks don't extend
        the qualname path a nested def sees). Never default-collapsed.
      multiline strings — docstrings / GLSL blocks. The fold keeps BOTH
        delimiter lines visible ((open, close-1) hides only the interior) so
        the collapsed display text still tokenizes as a TERMINATED string —
        hiding the closer would paint the rest of the file string-colored.
        These join default_collapsed like comment runs: folded on load,
        re-folded by collapse-all, left alone by expand-all.
      comment runs — >=2 consecutive same-indent full-line '#' comments.
        All runs join default_collapsed: multiline comments start folded;
        collapse-all re-folds them but expand-all leaves them collapsed.
        A SINGLE `# [...]` metadata line is a run of its own — a (s, s)
        range that only ever lays out header-less (the line itself hides,
        Toggles.TextEditor.hide_meta_comment_folds); _fold_build drops it
        from the layout when it can't (toggle off, no line below).
      top import block — first module-level import down to the last import
        before other module-level code (blank lines, comments and paren /
        backslash continuations stay inside). Also returned in
        default_collapsed: imports start folded on a fresh editor."""
    from src.lsd.gl_gui.toggles import Toggles
    blocks_on = Toggles.TextEditor.block_fold_ranges
    lines = text.split('\n')
    out = []
    default_col = []
    key_of = {}          # (start, end) -> stable fold identity
    _kcount = {}

    def _emit(s0, e0, key, default=False):
        # Duplicate identities (two runs opening with the same comment line,
        # a redefined def) disambiguate by emission index - unique for a
        # given text; only inserting an identical sibling range re-numbers.
        out.append((s0, e0))
        n = _kcount.get(key, 0)
        _kcount[key] = n + 1
        key_of[(s0, e0)] = key if n == 0 else key + (n,)
        if default:
            default_col.append((s0, e0))

    # Pass 1 - multiline strings. Their interior (and closing) lines go in
    # str_interior so the scope/comment/import scan below treats them as
    # opaque: indent-0 GLSL inside a def must not pop the def's scope, and a
    # "def " inside a string must not open a phantom one.
    str_interior = set()
    str_open = None              # (open_line, delim) while inside a string
    # Char-level state machine, NOT find('\"\"\"'): a triple-quote inside an
    # ordinary string literal (`ln.find('\"\"\"', pos)`) or after a trailing
    # inline comment must not open a phantom multiline string - a raw find
    # scanner marks everything below such a line as string interior, which
    # then suppresses scope/comment detection for the rest of the file.
    # Event-driven: the scan skips between the chars that can change state
    # (quotes, '#', backslashes) at C speed - a 200k char line used to cost
    # ~10ms in the per-char Python walk this replaced, with identical rules.
    _out_search = _FOLD_OUT_RE.search
    for i, ln in enumerate(lines):
        j, L = 0, len(ln)
        while True:
            if str_open is not None:
                s0, delim = str_open
                closed = -1
                for m in _FOLD_CLOSE_RE[delim].finditer(ln, j):
                    if m.group() == delim:
                        closed = m.start()
                        break
                if closed == -1:
                    if i != s0:
                        str_interior.add(i)
                    break            # whole line is string interior
                if i != s0:
                    str_interior.add(i)
                    if i - 1 > s0:
                        _emit(s0, i - 1, ('str', lines[s0].strip()),
                              default=True)
                str_open = None
                j = closed + 3
                continue
            m = _out_search(ln, j)
            if m is None:
                break
            j = m.start()
            c = ln[j]
            if c == '#':
                break                # comment - rest of the line is inert
            if ln.startswith(c * 3, j):
                str_open = (i, c * 3)
                j += 3
                continue
            # Single-quoted string: opaque to the closing quote (or the
            # end on a broken buffer) so a '\"\"\"' INSIDE it stays inert.
            j += 1
            end = L
            for m in _FOLD_CLOSE_RE[c].finditer(ln, j):
                if m.group() == c:
                    end = m.end()
                    break
            j = end
    # Pass 2 - scopes, comment runs, top import block.
    stack = []                   # (indent, header_line, scope_path, block_key)
                                 # block_key None for def/class, else the
                                 # ('block', ...) identity of a compound stmt
    last_code = -1               # last non-blank line seen
    run_start = run_ind = None   # current same-indent comment run

    def _close_run(end):
        if run_start is None:
            return
        if end > run_start:
            # A multiline comment run is folded and is skipped by
            # expand/collapse-all (same treatment as the top import block);
            # runs toggle via their own badge or the caret-scoped shortcuts.
            _emit(run_start, end, ('comment', lines[run_start].strip()),
                  default=True)
        elif (end == run_start
              and lines[run_start].lstrip().startswith(('# [', '#['))):
            # A lone metadata comment: a single-line fold, folded away
            # header-and-all when the meta toggle is on (see _fold_build).
            _emit(run_start, end, ('comment', lines[run_start].strip()),
                  default=True)

    imp_first = imp_last = None
    imp_done = imp_cont = False
    imp_depth = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if i in str_interior:
            _close_run(i - 1)
            run_start = run_ind = None
            if s:
                last_code = i
            continue
        is_comment = s.startswith('#')
        if is_comment:
            ind = len(ln) - len(ln.lstrip())
            if run_start is None:
                run_start, run_ind = i, ind
            elif ind != run_ind:
                _close_run(i - 1)
                run_start, run_ind = i, ind
        else:
            _close_run(i - 1)
            run_start = run_ind = None
        if not s:
            continue
        ind = len(ln) - len(ln.lstrip())
        while stack and ind <= stack[-1][0]:
            _, hdr, _spath, _bkey = stack.pop()
            if last_code > hdr:
                _emit(hdr, last_code, _bkey or (('scope',) + _spath))
        if not imp_done and not is_comment:
            if imp_cont:
                imp_last = i
                imp_depth += ln.count('(') - ln.count(')')
                imp_cont = imp_depth > 0 or s.endswith('\\')
            elif ind == 0 and _FOLD_IMPORT_RE.match(s):
                if imp_first is None:
                    imp_first = i
                imp_last = i
                imp_depth = ln.count('(') - ln.count(')')
                imp_cont = imp_depth > 0 or s.endswith('\\')
            elif imp_first is not None and ind == 0:
                imp_done = True   # first non-import module-level statement
        if _FOLD_SCOPE_HEAD_RE.match(s):
            _snm = _FOLD_SCOPE_NAME_RE.match(s)
            stack.append((ind, i, (stack[-1][2] if stack else ())
                          + ((_snm.group(1) if _snm else '?'),), None))
        elif blocks_on and not is_comment and _FOLD_BLOCK_RE.match(s):
            _spath = stack[-1][2] if stack else ()
            _bm = _FOLD_BLOCK_RE.match(s)
            stack.append((ind, i, _spath,
                          ('block',) + _spath + (_bm.group(2), s)))
        last_code = i
    _close_run(len(lines) - 1)
    for _, hdr, _spath, _bkey in stack:
        if last_code > hdr:
            _emit(hdr, last_code, _bkey or (('scope',) + _spath))
    if imp_first is not None and imp_last is not None and imp_last > imp_first:
        _emit(imp_first, imp_last, ('imports',), default=True)
    out.sort()
    return out, default_col, key_of


def _fold_splice_shift(old_text, new_text):
    """Range shifter for the single covering splice old_text -> new_text:
    (shift, exact, d_lines). shift(r) maps an old (start, end) fold tuple
    into new-text line coords; exact(r) says whether that mapping's START
    line is provably right for r. Identity (and exact) when the texts are
    equal or the edit changed no line count.

    _text_splice is PREFIX-greedy, so an edit touching the boundary of a
    blank-line run is attributed to the line BELOW the run — which can be
    a fold header:
    - a pure INSERTION whose char position sits at/before the start of its
      attributed line TRANSLATES a fold starting on that line (its header
      moved down) instead of stretching over it — stretching there
      swallowed the header into the hidden body (the caret-jumps-into-
      collapsed-fold mangle; _fold_reassemble resolves the same ambiguity
      suffix-first, and the two must agree);
    - a DELETION / replacement whose old region ends exactly at a line
      start leaves that line whole: it TRANSLATES up by the lines removed
      above it (blank lines deleted right above a collapsed comment run —
      the stretch rule kept the header index, sliding the fold onto the
      run's SECOND line, which still passed the '#' check: two comment
      lines showed until the quiet rescan).
    Anything else whose header line lies inside the edit's line span while
    the line count changed is NOT exact: the header may have moved, been
    joined, or died — callers must rescan rather than guess."""
    spl = _text_splice(old_text, new_text)
    if spl is None or not spl[3]:
        return (lambda r: r), (lambda r: True), 0
    p, oe, _d, dl, el, oel = spl
    ols = _line_starts(old_text)
    ins_at_line_start = p == oe and el < len(ols) and p <= ols[el]
    del_to_line_start = p < oe and oel < len(ols) and oe == ols[oel]

    def translated(r):
        return (r[0] > oel or (ins_at_line_start and r[0] >= el)
                or (del_to_line_start and r[0] >= oel))

    def shift(r):
        if r[1] < el:
            return r
        if translated(r):
            return (r[0] + dl, r[1] + dl)
        return (r[0], max(r[0], r[1] + dl))

    def exact(r):
        return r[1] < el or r[0] < el or translated(r)

    return shift, exact, dl


def _fold_carry(old_text, new_text, scan_result, collapsed_keys):
    """Carry a held _scope_fold_ranges result across one edit without
    rescanning: shift the range tuples through the edit's single covering
    splice (_fold_splice_shift; fold KEYS are line-independent and never
    move). Returns the shifted (ranges, default_col, key_of) — or None when
    the carry can't be trusted and the caller must run the real scan:

    - a COLLAPSED fold whose header line sits inside a line-count-changing
      edit without a provable translation (see _fold_splice_shift) — the
      shifted range would hide the wrong lines;
    - Verification: every COLLAPSED fold's carried header line must still
      look like its key (def/class name for scopes, '#' for comment runs,
      the exact opening line for strings, an import for the block). Any
      mismatch — a seam edit, an ambiguity the shift rules don't model —
      returns None: correctness is the scan's job; the carry only skips it
      when provably safe. Expanded folds aren't checked (a wrong range there
      hides nothing; the trailing rescan corrects it)."""
    ranges, dcol, key_of = scan_result
    shift, exact, dl = _fold_splice_shift(old_text, new_text)
    if dl:
        if collapsed_keys:
            _ck = set(collapsed_keys)
            if any(k in _ck and not exact(r) for r, k in key_of.items()):
                return None
        key_of = {shift(r): k for r, k in key_of.items()}
        ranges = [shift(r) for r in ranges]
        dcol = [shift(r) for r in dcol]
    if collapsed_keys:
        nls = _line_starts(new_text)
        range_of = {k: r for r, k in key_of.items()}
        for k in collapsed_keys:
            r = range_of.get(k)
            if r is None:
                continue
            if not (0 <= r[0] < len(nls) and r[0] <= r[1]):
                return None
            ls = nls[r[0]]
            le = nls[r[0] + 1] - 1 if r[0] + 1 < len(nls) else len(new_text)
            hline = new_text[ls:le].strip()
            kind = k[0]
            if kind == 'scope':
                m = _FOLD_SCOPE_NAME_RE.match(hline)
                name = next((s for s in reversed(k[1:])
                             if isinstance(s, str)), None)
                if m is None or (name is not None and m.group(1) != name):
                    return None
            elif kind == 'block':
                m = _FOLD_BLOCK_RE.match(hline)
                if m is None or m.group(2) != k[-2]:
                    return None
            elif kind == 'comment':
                if not hline.startswith('#'):
                    return None
            elif kind == 'imports':
                if not _FOLD_IMPORT_RE.match(hline):
                    return None
            elif kind == 'str':
                if hline != k[1]:
                    return None
    return ranges, dcol, key_of


def _fold_rekey(old_text, old_scan, new_text, new_scan, keys):
    """Carry collapse KEYS across a rescan that may have re-identified the
    folds. A key names a fold by its header text (comment runs, strings) or
    its name path (scopes, blocks), so an edit to a COLLAPSED fold's header
    line — a # [tint=...] value drag on a comment run's first line, a def
    rename, typing at a collapsed header's end — minted a new key, the old
    one projected to nothing, and the fold popped open (only when the edit
    hit the FIRST line of the run: body-line edits kept the key — the
    "sometimes" in the report). Each key's old range is shifted through
    the edit (_fold_splice_shift) and matched to the new scan's fold that
    starts on the same line with the same kind; a match adopts the new key
    (range identity beats name identity — an identical run pasted above
    re-numbers the duplicates, and the collapsed one must stay the
    collapsed one). Anything else keeps its old key: still valid, or an
    orphan that re-projects when its fold reappears (undo). Returns the
    remapped set; `keys` itself is never mutated."""
    if not keys:
        return keys
    old_range_of = {k: r for r, k in old_scan[2].items()}
    by_start = {(r[0], k[0]): k for r, k in new_scan[2].items()}
    shift, exact, _dl = _fold_splice_shift(old_text, new_text)
    out = set()
    for k in keys:
        r = old_range_of.get(k)
        nk = None
        if r is not None and exact(r):
            nk = by_start.get((shift(r)[0], k[0]))
        out.add(k if nk is None else nk)
    return out


def _split_gaps_at_collapsed(gaps, forest, collapsed):
    """Diff gaps split so none STRADDLES a collapsed scope range. The union
    layout (_fold_build) chains hidden runs through collapsed ranges that
    start inside one another — a gap piece, a hand-collapsed block that
    starts inside it and ends past it, the next gap piece — and the
    chained run can swallow a string delimiter each piece kept visible on
    its own (_string_neutral_ranges judges one range at a time): the
    attention walkthrough's closing docstring quote vanished and the whole
    file painted as a string (09-01). A gap whose header sits inside (or
    on) a collapsed scope resumes after it; a gap a collapsed scope
    starts inside and outruns ends before the scope's header and resumes
    after the scope. Collapsed scopes NESTED in a gap and every EXPANDED
    scope are left alone (overlap is fine — that is what keeps their
    chevrons). Runs before the string clamp so every piece is clamped as
    it will actually hide. `gaps`, `forest` normalized; returns a sorted
    list."""
    straddlers = sorted(r for r in forest if r in collapsed)
    if not straddlers:
        return list(gaps)
    out = []
    for gap_start, gap_end in gaps:
        cur = gap_start
        for scope_start, scope_end in straddlers:
            if scope_end < cur:
                continue
            if scope_start > gap_end:
                break
            if scope_start <= cur:              # header inside / on the scope
                cur = scope_end + 1
                continue
            if scope_end > gap_end:             # starts inside, ends past
                if scope_start - 1 > cur:
                    out.append((cur, scope_start - 1))
                cur = scope_end + 1
                break
        if gap_end > cur:
            out.append((cur, gap_end))
    return out


def _carry_diff_collapse(prev_ranges, prev_col, ranges):
    """Diff-layer collapse state carried across DRIFT: each collapsed
    range of the previous layout (`prev_col`, drawn from the sorted
    `prev_ranges`) marks every new range overlapping it; a new range
    overlapping nothing collapsed starts expanded. Both lists sorted by
    start; one merged sweep."""
    new_col, pi = set(), 0
    for r in ranges:
        while pi < len(prev_ranges) and prev_ranges[pi][1] < r[0]:
            pi += 1
        pj = pi
        while pj < len(prev_ranges) and prev_ranges[pj][0] <= r[1]:
            if prev_ranges[pj] in prev_col:
                new_col.add(r)
                break
            pj += 1
    return new_col


def _string_neutral_ranges(ds, text, ranges):
    """Diff-gap fold ranges arrive lexer-blind (open_files._diff_gap_folds):
    a range can hide one delimiter of a multiline string without the other,
    so the collapsed display text tokenizes with an unterminated (or
    spuriously opened) string and everything below the fold paints
    string-colored. Scope folds are safe by construction (they keep both
    delimiter lines visible — see _scope_fold_ranges); this clamps caller
    ranges to the same guarantee: shrink each range from the bottom until
    the string state entering its hidden lines equals the state at the
    first line kept visible below it, so splicing the hidden lines out
    cannot change the lexer state on any visible line. The per-line state
    is the same `line_open` the viewport tokenizer runs on, maintained
    incrementally for the FULL buffer on ds._flo_* (the ds._lo_* copy
    tracks the DISPLAY text, so it can't serve here).

    A range that straddles a delimiter is SPLIT, not just truncated — the
    remainder below the revealed delimiter line re-folds under its own
    header, so a docstring near the top of a big unchanged gap costs one
    visible line, not the whole rest of the gap."""
    if getattr(ds, '_flo_text', None) is not text:
        ds._flo_offs, ds._flo_open = _update_line_open(
            getattr(ds, '_flo_text', None), getattr(ds, '_flo_offs', None),
            getattr(ds, '_flo_open', None), text)
        ds._flo_text = text
    lo = ds._flo_open
    n = len(lo)
    out = []
    for s, e in ranges:
        s, e = int(s), min(int(e), n - 1)
        # Farthest line in (s, e+1] holding each string state; j == n stands
        # for "past EOF", state None (hiding through EOF leaves nothing below).
        far = {}
        for j in range(min(e + 1, n), s, -1):
            far.setdefault(lo[j] if j < n else None, j)
        while s < e:
            top = lo[s + 1] if s + 1 < n else None
            j = far.get(top, -1)     # first visible line below the hidden run
            if j - 1 > s:
                out.append((s, j - 1))
                s = j                # revealed line heads the next
            else:
                s += 1
    return out


def fold_root_scopes(ranges, skip=()):
    """The collapse/expand-all target set: the ranges of the FIRST nesting
    level holding at least two scopes. `ranges` are normalized (start, end)
    line ranges sorted by start; `skip` (default-collapsed: imports,
    comment runs) never counts toward the two. A level that is a single
    scope — a file that is one class or one function — is looked through
    to its children, so collapse-all folds the members rather than the
    one span; a level with nothing inside the lone scope stays at it."""
    ranges = list(ranges)
    skip = set(skip or ())
    while True:
        level, open_end = [], -1
        for fold_range in ranges:
            if fold_range[0] > open_end:
                level.append(fold_range)
                open_end = fold_range[1]
        counted = [r for r in level if r not in skip]
        if len(counted) != 1:
            return level
        lone = counted[0]
        inner = [r for r in ranges
                 if r != lone and lone[0] <= r[0] and r[1] <= lone[1]]
        if not inner:
            return level
        ranges = inner


def _fold_normalize_ranges(n_lines, ranges):
    """Caller fold ranges → sorted, clipped (start, end) tuples. 0-based
    INCLUSIVE buffer lines; a collapsed range keeps line `start` visible and
    hides start+1..end. Ranges that would hide nothing (end <= start, start
    past the buffer) are dropped. Strictly NESTED ranges are kept (scope
    folding needs them — an inner def folds on its own while its class is
    expanded); partial overlaps are dropped."""
    out, ends = [], []           # ends: open enclosing ranges' end lines
    for s, e in sorted((int(s), int(e)) for s, e in ranges):
        e = min(e, n_lines - 1)
        if e <= s or s >= n_lines - 1:
            continue
        while ends and s > ends[-1]:
            ends.pop()
        if ends and (e > ends[-1] or s == out[-1][0]):
            continue             # straddles the enclosing range, or dup start
        out.append((s, e))
        ends.append(e)
    return out


def _fold_normalize_union(n_lines, ranges, collapsed=()):
    """_fold_normalize_ranges for the UNION layout (scope folds + diff
    gaps): partial overlaps are KEPT — a diff gap routinely starts or ends
    mid-scope, and _fold_build hides the union of what every collapsed
    range hides (see its walk), so nothing has to nest. Sorted, clipped,
    de-duplicated; the walk handles ONE range per header line, so of
    ranges sharing a start a COLLAPSED one stays (the longest, it hides
    the most — the walk's union extension covers the rest anyway), else
    the shortest. A diff gap whose header (slid down by the preview rows)
    lands on a def / if / for header shares the line with that scope's
    fold; keeping the expanded scope there dropped the collapsed gap from
    the layout and the whole unchanged stretch showed (09-01)."""
    out, last_start = [], None
    for s, e in sorted({(int(s), int(e)) for s, e in ranges}):
        e = min(e, n_lines - 1)
        # e == s is the single-line metadata comment (header-less only -
        # _fold_build drops it when it can't lay out that way).
        if e < s or s >= n_lines - 1:
            continue
        if s == last_start:
            if (s, e) in collapsed:
                out[-1] = (s, e)       # collapsed beats expanded / shorter
            continue
        out.append((s, e))
        last_start = s
    return out


def _fold_headless_set(key_of, ranges):
    """The COMMENT runs among `ranges` `_fold_build` may fold header-and-all
    (Toggles.TextEditor.hide_meta_comment_folds) — the build itself keeps
    the ones without a `# [` metadata line. Over ALL ranges, not just the
    collapsed ones: an EXPANDED single-line meta comment needs it to know
    whether to offer a chevron. Part of the fold cache key, so flipping
    the toggle relays out on the next frame."""
    from src.lsd.gl_gui.toggles import Toggles
    if not Toggles.TextEditor.hide_meta_comment_folds or not key_of:
        return frozenset()
    return frozenset(r for r in ranges
                     if key_of.get(r, ('',))[0] == 'comment')


def _fold_build(text, ranges, collapsed, headless=frozenset()):
    """Fold layout for draw_text's collapsible line ranges.

    `headless` — collapsed ranges (comment runs) that may hide their HEADER
    line too, when the run carries a melty `# [...]` line: the whole run
    leaves the display, the fold ANCHORS at the end of the display line
    above it (the splice seam) and its CHEVRON sits on the line BELOW it —
    the line the metadata annotates — so the fold entry's display_line is
    that line while anchor/hidden_len still describe the seam above.
    Skipped for a run on line 0 or at the end of the file, or when the
    line below heads a COLLAPSED fold (that row's chevron is taken —
    `_fold_header_map`: collapsed beats expanded, so an expanded def /
    diff gap starting there yields its chevron instead). Two folds on one
    seam (a collapsed fold ending right above the run) are fine: segments
    sort by buffer position, so they splice back in order.

    Returns (display_text, segments, folds, disp_to_buf):
      display_text — `text` with every COLLAPSED range's hidden lines
        (start+1..end; start..end for a headless fold) spliced out; `text`
        itself when nothing is collapsed.
      segments — [(anchor_offset_in_display, hidden_str, rng, headless)]
        per collapsed fold. hidden_str starts with the '\\n' that followed
        the header line (preceded it, for a headless fold), so inserting it
        back at the anchor reproduces `text` exactly.
      folds — [(rng, display_line, is_collapsed, n_hidden, header_len,
        anchor_offset, hidden_len)] for EVERY normalized range (expanded
        folds still need badge geometry). anchor/hidden_len describe the
        CURRENT display layout, so a caret can be shifted across a toggle.
      disp_to_buf — buffer line per display line; None when identity.
    """
    lines = text.split('\n')
    rngs = _fold_normalize_union(len(lines), ranges, collapsed)
    # Full, line offsets (needed for hidden extraction + expanded anchors).
    foffs, off = [], 0
    for l in lines:
        foffs.append(off)
        off += len(l) + 1
    folds, segments = [], []
    if (not any(r in collapsed for r in rngs)
            and not any(r[0] == r[1] and r in headless for r in rngs)):
        # (an eligible single-line meta comment takes the walk below even
        # with nothing collapsed - its chevron needs the walk's checks)
        for s, e in rngs:
            if e == s:
                continue     # single-line meta comment: header-less only
            anchor = foffs[s] + len(lines[s])
            hidden_len = foffs[e] + len(lines[e]) - anchor
            folds.append(((s, e), s, False, e - s, len(lines[s]),
                          anchor, hidden_len))
        return text, segments, folds, None
    disp, disp_to_buf, pend = [], [], []
    ri, buf = 0, 0
    _META_PREFIXES = ('# [', '#[')

    def _headless_ok(s, e, hidden_end, rj):
        # Header-and-all: needs a display line above to anchor on, a line
        # below to yield the chevron whose own fold (rngs[rj] is the first
        # range past the hidden run) is not COLLAPSED - an expanded def or
        # diff gap starting there yields the badge (a compare diff's
        # unchanged gap begins right after an expanded comment run, 09-02) -
        # and a `# [` line somewhere in the run.
        if (s, e) not in headless or len(disp) < 2:
            return False
        if hidden_end + 1 >= len(lines):
            return False
        if (rj < len(rngs) and rngs[rj][0] == hidden_end + 1
                and rngs[rj] in collapsed):
            return False
        return any(lines[i].lstrip().startswith(_META_PREFIXES)
                   for i in range(s, e + 1))
    while buf < len(lines):
        disp_to_buf.append(buf)
        disp.append(lines[buf])
        # Ranges starting inside a collapsed fold's hidden body were jumped
        # over - they contribute no badge and no segment while collapsed
        # (their own collapsed state is preserved untouched for when the
        # outer fold reopens).
        while ri < len(rngs) and rngs[ri][0] < buf:
            ri += 1
        if ri < len(rngs) and rngs[ri][0] == buf:
            s, e = rngs[ri]
            ri += 1
            is_col = (s, e) in collapsed
            if is_col:
                # The hidden run is the UNION: a collapsed range starting
                # inside it (a diff gap straddling this scope, a scope
                # run straddling a collapsed gap) extends the run to its
                # own end - ranges don't nest (partial overlaps are
                # what the union layout is made of). The swallowed range
                # keeps its own collapsed state for when this one opens.
                hidden_end = e
                rj = ri
                while rj < len(rngs) and rngs[rj][0] <= hidden_end:
                    if rngs[rj] in collapsed and rngs[rj][1] > hidden_end:
                        hidden_end = rngs[rj][1]
                    rj += 1
                if _headless_ok(s, e, hidden_end, rj):
                    # The header line goes too: the fold hangs off the
                    # line above (dropped from the display just like the
                    # body is).
                    disp.pop()
                    disp_to_buf.pop()
                    pend.append(((s, e), len(disp) - 1, True, hidden_end,
                                 True))
                elif e > s:
                    pend.append(((s, e), len(disp) - 1, True, hidden_end,
                                 False))
                else:
                    buf += 1     # a lone meta line that can't hide: just text
                    continue
                buf = hidden_end + 1
                continue
            if e > s or _headless_ok(s, e, e, ri):
                # (an expanded single-line meta comment wears its chevron
                # only where collapsing it would actually hide it)
                pend.append(((s, e), len(disp) - 1, False, e, False))
        buf += 1
    display_text = '\n'.join(disp)
    doffs, off = [], 0
    for l in disp:
        doffs.append(off)
        off += len(l) + 1
    for rng, dl, is_col, hidden_end, is_headless in pend:
        s, e = rng
        anchor = doffs[dl] + len(disp[dl])
        if is_col:
            # A headless fold's hidden run opens with the '\n' that
            # precedes its header (foffs[s] - 1), so splicing it back at
            # the anchor reproduces `s` exactly like a regular fold's;
            # its chevron and badge sit on the line BELOW the seam.
            hid_start = foffs[s] - 1 if is_headless else foffs[s] + len(lines[s])
            hidden = text[hid_start:foffs[hidden_end] + len(lines[hidden_end])]
            chev_dl = dl + 1 if is_headless else dl
            folds.append((rng, chev_dl, True,
                          hidden_end - s + (1 if is_headless else 0),
                          len(disp[chev_dl]), anchor, len(hidden)))
            segments.append((anchor, hidden, rng, is_headless))
        else:
            # hidden_len = the DISPLAY chars this fold would remove if it
            # collapsed now (a collapsed range inside its span is already
            # gone) - what the caller's caret adjust needs.
            j = bisect.bisect_right(disp_to_buf, e) - 1
            folds.append((rng, dl, False, e - s, len(disp[dl]),
                          anchor, doffs[j] + len(disp[j]) - anchor))
    if not segments:
        # Nothing hid after all (the only collapsed ranges were meta lines
        # that couldn't lay out header-less): the identity contract.
        return text, segments, folds, None
    # Buffer order: two segments can share a seam (a collapsed fold ending
    # right above a headless run) and must splice back in buffer order.
    segments.sort(key=_fold_segment_order)
    return display_text, segments, folds, disp_to_buf


def _fold_segment_order(segment):
    """Sort key for fold segments: anchor, then buffer position."""
    return segment[0], segment[2]


def _fold_header_map(folds):
    """display line -> (range, collapsed?) for the gutter chevrons. One
    chevron per row: a COLLAPSED fold beats an expanded one on the same
    row (a hidden `# [` run's chevron sits on the line below it, which may
    head an expanded def / diff gap — the hidden text needs the chevron
    more than the collapse shortcut does; the expanded fold gets its row
    back once the run is open)."""
    out = {}
    for f in folds:
        prev = out.get(f[1])
        if prev is None or (f[2] and not prev[1]):
            out[f[1]] = (f[0], f[2])
    return out


def _fold_reassemble(old_disp, new_disp, segments, collapsed):
    """Splice the hidden fold segments back into the EDITED display text so
    the changed return hands the caller the FULL buffer.

    Anchors are offsets into old_disp (the pre-edit display text). The frame's
    single edit region is located by chunked common prefix/suffix; anchors
    after it shift by the edit's length delta, and collapsed range tuples
    below the edit shift by its newline delta so they keep matching the
    caller's recomputed fold_ranges next frame. An edit that overlaps a seam
    force-expands that fold (its hidden text is still spliced back, clamped
    to the edit region's end) — the neighborhood changed under it, so showing
    everything beats guessing. Returns (full_text, new_collapsed_set,
    force_expanded, moved) — force_expanded holds the ORIGINAL range tuples
    of seam-expanded folds so the caller can drop their fold KEYS (the
    durable collapse state; see the projection block in the draw_text body);
    moved maps each dnl-shifted collapsed tuple old -> new so the caller can
    replay the shift onto its own per-set stores (scope vs diff)."""
    lo, ln = len(old_disp), len(new_disp)
    m = min(lo, ln)
    # Maximal SUFFIX first, prefix capped to what's left: at a seam an
    # insertion is ambiguous (prefix and suffix both want the boundary
    # newline), and suffix-priority resolves it so text typed at a collapsed
    # header's end stays in the header rather than pasting below the fold.
    suf = 0
    while suf < m:
        step = min(4096, m - suf)
        if old_disp[lo - suf - step:lo - suf] == new_disp[ln - suf - step:ln - suf]:
            suf += step
            continue
        e = suf + step
        while suf < e and old_disp[lo - suf - 1] == new_disp[ln - suf - 1]:
            suf += 1
        break
    p, max_p = 0, m - suf
    while p < max_p:
        step = min(4096, max_p - p)
        if old_disp[p:p + step] == new_disp[p:p + step]:
            p += step
            continue
        e = p + step
        while p < e and old_disp[p] == new_disp[p]:
            p += 1
        break
    delta = ln - lo
    dnl = (new_disp.count('\n', p, ln - suf)
           - old_disp.count('\n', p, lo - suf))
    new_col = set(collapsed)
    force_expanded = set()
    moved = {}      # old range tuple -> its dnl-shifted replacement
    parts, pos = [], 0
    for a, hidden, rng, headless in sorted(segments, key=_fold_segment_order):
        # A pure insertion exactly at the anchor whose text STARTS with a
        # newline (typing at the collapsed header's end, or at the start of
        # the line below the badge - the same display text either way) is a
        # new LINE at the boundary, not header content: it lands BELOW the
        # collapsed fold (the below-fold branch), never as the fold's first
        # visible line. Typed characters at the header's end (no leading
        # newline) still belong to the header via the first branch.
        # A HEADLESS fold (a collapsed `# [` comment run) is the exception:
        # the run belongs to the line BELOW it, so a new line at its seam
        # goes ABOVE the hidden text - landing it below separated the
        # comment from the line it annotates (screenshot, 09-02).
        nl_at_anchor = (delta > 0 and p == a and p == lo - suf
                       and new_disp.startswith('\n', p) and not headless)
        if a >= lo - suf and not nl_at_anchor:
            # Edit ends at or before the seam - includes a pure suffix AT
            # the anchor (typing at the collapsed header's end: lo-suf == a),
            # which belongs to the header, so the hidden text goes after it.
            na = a + delta
            if dnl:
                new_col.discard(rng)
                new_col.add((rng[0] + dnl, rng[1] + dnl))
                moved[rng] = (rng[0] + dnl, rng[1] + dnl)
        elif a < p or (a == p and delta > 0):
            # Edit strictly after the seam (below the fold).
            na = a
        else:
            deleted = old_disp[p:lo - suf]
            if (ln - suf == p and p == a and deleted.strip() == ''
                    and (new_disp.startswith('\n', p)
                         or p == len(new_disp))):
                # Pure whitespace deletion pinned to the anchor with a
                # newline still terminating the header there: the user
                # deleted BLANK LINE(S) from the run below the badge -
                # suffix-priority attributes any delete from that run to
                # its first newline (the anchor), but nothing joined onto the
                # header, so the lines died below the fold. Splice the body
                # back in place to keep the fold; force-expanding here
                # popped the fold (and the caret) open on every blank-line
                # delete below a collapsed def.
                na = a
            else:
                # The seam itself was edited (e.g. forward-deleting the
                # newline after a collapsed header, joining content onto it)
                # - force-expand so the user sees what changed; the hidden
                # text splices back clamped to the edit.
                na = min(max(a, p), ln - suf)
                new_col.discard(rng)
                force_expanded.add(rng)
        na = max(na, pos)
        parts.append(new_disp[pos:na])
        parts.append(hidden)
        pos = na
    parts.append(new_disp[pos:])
    return ''.join(parts), new_col, force_expanded, moved


def fold_project_jump(ds, text, pos, li):
    """Project a FULL-buffer jump target (char `pos`, 0-based line `li`) into
    the editor's fold display space, expanding any collapsed fold hiding the
    target first — a jump must reveal its destination. External jump writers
    (the open-files jump_to_line consumer, scope-up auto-select) call this
    right before stamping caret/scroll onto the draw_state; it's a no-op
    (identity return) while the editor has nothing collapsed.

    The post-expand layout is built here and PRIMED into ds._fold_cache —
    same text identity, same range tuple, same collapsed set as the next body
    run will compute, so the body gets a cache hit and lays out exactly the
    geometry these coordinates were mapped through."""
    scope_col = getattr(ds, '_fold_collapsed', None) or set()
    diff_col = getattr(ds, '_diff_fold_collapsed', None)
    col = scope_col | (diff_col or set())     # the layout's collapse union
    if not col:
        return pos, li
    _fc = getattr(ds, '_fold_cache', None)
    if _fc is None or _fc[0] is not text:
        # The fold_cache was built against a different buffer than the
        # jump's - rather than guess a mapping, expand everything. With no
        # collapsed fold, full coords ARE display coords.
        scope_col.clear()
        if diff_col:
            diff_col.clear()
        if getattr(ds, '_fold_keys', None) is not None:
            ds._fold_keys = set()   # keys are the durable truth - sync them
        ds.invalidate()
        return pos, li
    ranges = _fc[1][0]
    # Any fold HIDING li (body line) or LEADED by li (r[0] == li): landing on
    # a collapsed def's own line must open its body first, or the jump just
    # parks the caret on the collapse line and shows nothing.
    hiding = [r for r in col if r[0] <= li <= r[1]]
    for r in hiding:
        col.discard(r)
        scope_col.discard(r)
        if diff_col and r in diff_col:
            diff_col.discard(r)
            # An ACTIVE expand_diff=False switch would re-collapse this
            # piece next frame and swallow the landing - the reveal counts
            # as a hand takeover, so the owner clears its switch to None.
            ds._diff_manual_gen = getattr(ds, '_diff_manual_gen', 0) + 1
    # External mutation: the body's harvest won't run until its next frame,
    # and its top-of-frame key->range projection would otherwise re-collapse
    # what this jump just expanded - drop the expanded folds' keys too.
    _kf = getattr(ds, '_fold_key_of', None)
    if hiding and _kf and getattr(ds, '_fold_keys', None) is not None:
        ds._fold_keys -= {_kf[r] for r in hiding if r in _kf}
    _hl = _fold_headless_set(_kf, ranges)
    built = _fold_build(text, ranges, col, _hl)
    if hiding:
        ds._fold_cache = (text, (ranges, frozenset(col), _hl), built)
        ds.invalidate()
    disp, segments, _folds, d2b = built
    if not segments:
        return pos, li
    dli = bisect.bisect_right(d2b, li) - 1     # li is visible now - exact hit
    dpos = _line_starts(disp)[dli] + (pos - _line_starts(text)[li])
    return dpos, dli


@render_func(is_default_for=(CodeLine), show_bg=True, use_cache=True, disable_scroll=False, with_header=draw_header,
             shadow=False, max_bg_depth=0, max_bg_value=0.05,
             show_name=False, with_footer=draw_footer, determines_height=False, saturation=1.7,
             selectable=False, searchable=True, bg_offset=-0.6, show_add_delete=False)
@window
def draw_text(input_value: str, height=None,
              left_mouse_down=False,
              left_mouse_drag=False, left_mouse_held=False,
              horizontal_scroll_drag=False, search_text="", 
              ctrl_b_down=False, ctrl_shift_b_down=False,
              ctrl_minus_down=False, ctrl_equal_down=False,
              ctrl_shift_minus_down=False, ctrl_shift_equal_down=False,
              single_line=False, is_search_box=False, focusable=True,
              draw_state=None, text_editor_state: TextEditorState = None,
              request_focus=False, select_all_on_focus=False,
              wrap=False, line_height=1.2, font=Font.FONTAWESOME_MONO_19, jump_to=None,
              code_tree=None, code_dict=None, error=None, token_views=None,
              live_store=None,
              import_fixes=None,
              syntax_highlight=True, is_diff=False, line_numbers=None,
              completion_source=None, show_jump_bar=True, show_file_header=True,
              manual_search=False, fold_ranges=None, scope_collapse=True,
              default_collapsed_lines=None,
              diff_fold_ranges=None, expand_diff=None,
              gutter_indent=False,
              scroll_bar_width=8.0, scroll_bar_brightness=5.9,
              autocomplete=True, unique=0,
              show_widgets=True, show_root_backgrounds=True,
              highlight_token_matches=True, roster_live_hold=True,
              roster_world=None, roster_table=None,
              fim="", fim_state: FimState = None):
    """`show_widgets=False` hides every inline token widget (run/eye buttons,
    number drags, bool switches, icon pickers -- the token_views layer).
    `highlight_token_matches=False` turns off the caret-rest same-token wash
    for this editor (embeds like global-search rows: the wash, drawn under
    the definition tints, read as washed-out symbol colours there).
    `show_root_backgrounds=False` skips the definition block wash of ROOT
    symbols (blocks no other block in this buffer contains) -- for embeds
    that paint the enclosing class's background themselves (global search
    rows), so the wash isn't drawn twice. Honoured only while
    Toggles.TextEditor.root_symbol_tints is False.
    `roster_live_hold=False` marks this buffer a READ-ONLY preview of its
    file (global-search rows): its def tints resolve against the roster's
    pending table instead of installing the buffer as the file's live
    override (see roster_tints.collect_def_tints).
    `roster_world` (a symbol_roster.World) / `roster_table` (a
    detached_table): the buffer shows ANOTHER version of its file (the merge
    window's disk / sync-frame / staged panes) — its definition tints resolve
    through that world's tables / its own detached table, never the
    studio's hold for the file; either also lets the file path come from
    `file_key` when there is no `jump_to`."""
    ds = draw_state
    # ── Instant restore (input_value==LOADING) ─────────────────────────────
    # A caller whose real buffer is still loading (draw_code_editor's
    # loading_frame) passes the LOADING sentinel: rebuild a same-shape
    # stand-in from the persisted viewport snapshot (TextEditorState - see
    # its restore_ fields): blank lines up to the visible band, the band's
    # captured text, blank lines after. Same line count → same content
    # height → the persisted scroll lands unmoved, so this editor's first
    # frame shows the code the last one did, before any disk read. The real
    # text swaps in through the normal content-change path when it loads;
    # edits to the stand-in are discarded (changed forced back at the
    # return). The snapshot capture at the tail skips restore, so the
    # stand-in never overwrites the real snapshot. (A sentinel, not None:
    # the render_func() serves its input cache for None without actually
    # running this body.)
    restore_active = isinstance(input_value, _LoadingSentinel)
    _restore_caret = None
    if restore_active:
        # The stand-in is display-SHAPED (line count) but not length-faithful
        # - mostly bare newlines. The persisted caret (a char offset into the
        # REAL buffer) would clamp to the stand-in's short tail - the file's
        # LAST line - and the caret-rest would slam the display scroll to
        # EOF (and permanently corrupt the caret). Caret state is frozen
        # across stand-in frames: stashed here, put back at the return, so
        # both caret-followers sit restore frames out (they're mid-body,
        # before the put-back).
        _restore_caret = (ds.text_cursor_pos, ds.text_selection_start,
                          ds.text_selection_end)
        if text_editor_state is not None and text_editor_state.restore_text:
            _before = max(0, int(text_editor_state.restore_first_line))
            _snap_lines = text_editor_state.restore_text.count("\n") + 1
            _after = max(0, int(text_editor_state.restore_total_lines)
                         - _before - _snap_lines)
            input_value = ("\n" * _before + text_editor_state.restore_text
                           + "\n" * _after)
        else:
            input_value = ""

    cursor_pos = imgui.get_cursor_screen_pos()  # ← cursor_pos = (13.5, 13.5)

    # --- Perf instrumentation (typing latency) --------------------------------
    # Section marks: each _pf(label) closes the section since the previous mark.
    # One summary line per edited frame — plus any frame >= 8ms — goes to the
    # pe_trace timeline (/tmp/lsd_symbol_perf.log) so draw_text's own cost can
    # be read against the background reparse/index lines around it.
    # [tint=(0.483, 0.397, 0.054, 1.0), show_tint=True\]
    _pf_t0 = time.perf_counter()
    _pf_cpu0 = time.thread_time()   # wall≫cpu in the summary = GIL starvation
    _pf_marks = []
    _pf_tok = [0.0, 0]   # accumulated _window() cache-miss time, miss count
    _pf_info = {}        # extra facts for the summary line (span counts, cache hits)
    def _pf(
            label):
        _pf_marks.append((label, time.perf_counter()))

    # --- Collapsible line ranges (fold_ranges=[(s, e), ...]) -----------------
    # Each (start, end) tuple (0-based inclusive buffer lines) is a fold: a
    # badge at the end of line `start` toggles it, and while collapsed lines
    # start+1..end are spliced OUT of the text the body sees - layout, caret,
    # search and tokenize all run on the display text, so all of the linear
    # y = origin_y + line * line_px sites need remapping. The hidden segments
    # are spliced back IN before the changed line, so the caller always
    # round-trips the FULL buffer. Collapse state lives on the draw_state
    # (ds._fold_collapsed, a set of the range tuples).
    _fold_segments, _fold_folds, _fold_d2b = [], None, None
    _fold_full = input_value    # the FULL buffer, kept across the display
                                # substitution below - tree-derived overlays
                                # (washes, error markers) resolve against it
                                # and are then remapped into display coords.
    _fold_bl = _fold_remap_spans = None
    # scope_collapse=True derives fold_ranges from the buffer itself: one
    # fold per Python def/class scope (nested scopes collapse - see
    # _scope_fold_ranges). Re-derived only when the buffer changes; an empt
    # fold_ranges passes. Gated on syntax_highlight - plain-text buffers have
    # no Python context.
    _fold_default_col = None
    # diff_fold_ranges (compare splits): a SECOND, caller-owned span set -
    # the unchanged gaps between code blocks (open_files._diff_gap_folds)
    # - that lives ALONGSIDE the scope folds instead of replacing them. Both
    # sets splice into the one display layout below, each with its own
    # collapse state: scope folds keep ds._fold_keys / _fold_collapsed
    # (keyboard shortcuts, default-collapsed seeding, session restore) while
    # diff folds track on ds._diff_fold_collapsed, seeded by `expand_diff`.
    # In-function import, same cycle-avoidance as the main Toggles import
    # further down (which harmlessly re-binds the same name).
    from src.lsd.gl_gui.toggles import Toggles
    _fold_key_of = None
    # not restore_active: the loading stand-in is ALREADY display-shaped
    # (the snapshot captured fold-spliced display text), so fold processing
    # on it is meaningless - worse, the per-frame key⟷tuple round-trip
    # ("harvested back to keys before the build") projects the seeded
    # restore_fold_keys onto the placeholder's foldless ranges and harvests
    # back an EMPTY set, destroying the persisted fold state before the
    # real text even lands. The fold layer sits the stand-in frames out.
    if (scope_collapse and not fold_ranges and syntax_highlight
            and Toggles.TextEditor.scope_fold_ranges
            and not restore_active
            and not single_line and not is_search_box):
        _sc = getattr(ds, '_scope_rng_cache', None)
        # Keyed on text identity AND _FOLD_SEED_VER: a hotswap that changed
        # the default_collapsed sources must not serve a pre-swap scan to
        # the versioned reseed below. Cache: (text, scan_result, ver,
        # provisional) - provisional entries were splice-carried, not
        # scanned, and are replaced by a real scan once input quiets.
        _sc_ok = (_sc is not None and len(_sc) >= 4
                  and _sc[2] == _FOLD_SEED_VER)
        _sc_hit = _sc_ok and _sc[0] is input_value
        if not _sc_hit and _sc_ok and _typing_hot():
            # Typing burst: the O(file) rescan (~22ms on a large buffer) is
            # this editor's primary per-keystroke cost - skip it and carry
            # the held ranges across the edit (see _fold_carry). A None
            # return (collapsed-fold header verification failed) falls
            # through to the real scan below: never serve ranges that point
            # onto the wrong lines.
            _carried = _fold_carry(_sc[0], input_value, _sc[1],
                                   getattr(ds, '_fold_keys', None))
            if _carried is not None:
                _sc = (input_value, _carried, _FOLD_SEED_VER, True)
                ds._scope_rng_cache = _sc
                _sc_hit = True
        if not _sc_hit or (_sc[3] and not _typing_hot()):
            _new_sc = (input_value, _scope_fold_ranges(input_value),
                       _FOLD_SEED_VER, False)
            if _sc_ok and getattr(ds, '_fold_keys', None):
                # The rescan may have re-identified folds (a collapsed
                # comment block's header line edited by a value drag / typing,
                # a def renamed): move each collapse key onto its fold range
                # now range where its fold landed, or the fold pops open the
                # moment the scan lands (see _fold_rekey). Against the
                # held entry - the old text, not the splice-carried
                # provisional (same text, old keys) on the trailing rescan.
                ds._fold_keys = _fold_rekey(_sc[0], _sc[1], input_value,
                                            _new_sc[1], ds._fold_keys)
                if getattr(ds, '_fold_search_exp_keys', None):
                    ds._fold_search_exp_keys = _fold_rekey(
                        _sc[0], _sc[1], input_value, _new_sc[1],
                        ds._fold_search_exp_keys)
            _sc = _new_sc
            ds._scope_rng_cache = _sc
        if _sc[3]:
            request_render()   # provisional: the trailing rescan needs a frame
        fold_ranges, _fold_default_col, _fold_key_of = _sc[1]
    # --- DIFF fold layer (diff_fold_ranges + expand_diff) --------------------
    # Collapse state per diff span lives on ds._diff_fold_collapsed, a set of
    # the normalized range tuples (no keys - the spans re-derive from the
    # live diff every frame and are carried across drift by overlap below).
    # `expand_diff` is the OWNER's tri-state switch (draw_code_editor /
    # merge_files auto-state - one value for the whole editor): True keeps
    # EVERY span expanded proactively, False keeps every span collapsed
    # (new gaps included; only the search reveal opens spans, restored by
    # its own machinery), None is neutral - each span's badge state is
    # tracked individually and carried across drift by overlap. A manual
    # badge toggle bumps ds._diff_manual_gen; the owner watches that and
    # clears its switch to None in the same frame, so the toggle sticks.
    _diff_rngs, _diff_rng_set = [], frozenset()
    if (diff_fold_ranges and not restore_active and not single_line
            and not is_search_box):
        # Caller gaps → the layer's working ranges: normalize, then
        # string-neutral (multiline string delimiters must never be within
        # one line). A gap stays one range - one header row, one badge,
        # one separator band per unchanged stretch (Lukas 09-01: "a single
        # line between each change"). Gaps and scope folds share the union
        # layout WITHOUT nesting: _fold_build hides the union of what every
        # collapsed range hides, so a gap straddling a def and the def's own
        # fold coexist, each with its chevron (the earlier prune of
        # straddling scopes lost their chevrons, the nest-split before it
        # fragmented every gap). A gap EQUAL to a scope range loses to the
        # scope (same lines, the scope's chevron does the job). Memoized:
        # the gaps re-derive every frame on buffer change.
        _n_lines = len(_line_starts(input_value))
        _gaps_norm = _fold_normalize_ranges(_n_lines, diff_fold_ranges)
        # COLLAPSED scope folds (last frame's projection of the durable
        # state) affect the gaps - _split_gaps_at_collapsed - so they join
        # the memo key (set equality: a few hundred lines at most).
        _scope_col_now = getattr(ds, '_fold_collapsed', None) or set()
        _dsm = getattr(ds, '_diff_split_memo', None)
        if (_dsm is not None and _dsm[0] == _gaps_norm
                and _dsm[1] is fold_ranges and _dsm[2] is input_value
                and _dsm[5] == _scope_col_now):
            _diff_rngs = _dsm[3]
            ds._diff_gap_index = _dsm[4]
        else:
            _forest = _fold_normalize_ranges(_n_lines, fold_ranges or ())
            # Split around collapsed straddlers and clamp for string
            # safety to a FIXED POINT: the clamp's cut can turn a scope
            # nested in the raw gap into a straddler of a piece (and a
            # split piece can trigger a new clamp) - 2 or 3 rounds in
            # practice, 6 as a guard.
            _pieces = list(_gaps_norm)
            for _round in range(6):
                _next = _split_gaps_at_collapsed(_pieces, _forest, _scope_col_now)
                if syntax_highlight:
                    _next = _string_neutral_ranges(ds, input_value, _next)
                if _next == _pieces:
                    break
                _pieces = _next
            _forest = set(_forest)
            _pieces = [g for g in _pieces if g not in _forest]
            _diff_rngs = _fold_normalize_ranges(_n_lines, _pieces)
            # Piece -> owning-gap ordinal, for the two-level owner (pane
            # sync / switch inference in open_files): the panes' gaps
            # correspond by index across the compare split, their pieces
            # don't (each side splits against its own scope structure).
            _gap_starts = [g[0] for g in _gaps_norm]
            _gap_of = {}
            for _p in _diff_rngs:
                _gi = bisect.bisect_right(_gap_starts, _p[0]) - 1
                if _gi >= 0:
                    _gap_of[_p] = _gi
            ds._diff_gap_index = _gap_of
            ds._diff_split_memo = (_gaps_norm, fold_ranges, input_value,
                                   _diff_rngs, _gap_of, set(_scope_col_now))
        _diff_rng_set = frozenset(_diff_rngs)
        _diff_col = getattr(ds, '_diff_fold_collapsed', None)
        _diff_sx = getattr(ds, '_diff_search_exp', None)
        if _diff_sx is None:
            _diff_sx = ds._diff_search_exp = set()
        # A manual-gen bump not yet seen by THIS frame (a badge click last
        # frame, an external fold_project_jump between frames) suspends the
        # active switch for one frame - the owner's watch clears the switch
        # to None in the same frame it sees the bump, so without this the
        # enforcement would undo the hand's change one frame earlier.
        _gen = getattr(ds, '_diff_manual_gen', 0)
        _fresh_manual = _gen != getattr(ds, '_diff_manual_seen_self', 0)
        ds._diff_manual_seen_self = _gen
        # A FLIP of the owner's switch (collapse-all ↔ expand-all) is the
        # only reflow that should keep the view where it was - a set change
        # from drift under a held switch must NOT re-anchor (it would fight
        # the typing scroll).
        _switch_flip = (expand_diff is not None
                        and getattr(ds, '_diff_switch_seen', '?') != expand_diff)
        ds._diff_switch_seen = expand_diff
        if _fresh_manual:
            if _diff_col is None:
                _diff_col = set()
        elif expand_diff is not None:
            # ACTIVE switch: every span kept folded (False - new gaps,
            # drift, external writes included; only the search reveal opens
            # spans, restored by its own machinery) or open (True), every
            # frame. Change-edge gated: writes only when different.
            _new_col = (set(_diff_rngs) - _diff_sx if expand_diff is False
                        else set())
            if _diff_col != _new_col:
                if _switch_flip:
                    # Hold one reference line across the reflow: the CARET's
                    # line when a caret is set and on-screen, otherwise the
                    # viewport's midpoint. Mapped to its buffer line through
                    # the OLD layout now (line_px/height from last frame's
                    # stamps); the post-build block projects it into the new
                    # layout and arms the _fold_scroll_anchor hold.
                    _lp = getattr(ds, '_diff_line_px', None)
                    _ofc = getattr(ds, '_fold_cache', None)
                    if _lp and _ofc is not None and _ofc[0] is input_value:
                        _sy = ds.scroll_offset[1]
                        _mid_dl = (_sy + (ds.height or 0) * 0.5) / _lp
                        _odisp, _od2b = _ofc[2][0], _ofc[2][3]
                        _cp = ds.text_cursor_pos
                        _cp_full_flip = None
                        if _cp is not None:
                            # The caret rides the flip: its FULL-buffer
                            # offset (through the OLD layout) is re-projected
                            # into the new layout post-build - without this
                            # the display offset is stale, gets clamped by
                            # the shorter collapsed text, and the vertical
                            # caret-follow yanks the view away from the
                            # anchor.
                            _cp_c = min(max(_cp, 0), len(_odisp))
                            _cdl = _odisp.count('\n', 0, _cp_c)
                            if _od2b:
                                _col = _cp_c - (_odisp.rfind('\n', 0, _cp_c)
                                                + 1)
                                _cbl = _od2b[min(_cdl, len(_od2b) - 1)]
                                _cp_full_flip = (
                                    _line_starts(input_value)[_cbl] + _col)
                            else:
                                _cp_full_flip = _cp_c
                            # Anchor line: the CARET's when it's on-screen;
                            # an off-screen caret keeps the midpoint
                            # (anchoring it would pull the view away).
                            if (_sy / _lp - 1.0 <= _cdl
                                    <= (_sy + (ds.height or 0)) / _lp + 1.0):
                                _mid_dl = _cdl + 0.5
                        if _od2b:
                            _mi = min(max(int(_mid_dl), 0), len(_od2b) - 1)
                            _mid_bl = _od2b[_mi] + (_mid_dl - int(_mid_dl))
                        else:
                            _mid_bl = _mid_dl
                        ds._diff_flip_anchor = (_mid_bl, _mid_dl, _sy,
                                                _cp_full_flip)
                ds.invalidate()
                request_render()
            _diff_col = _new_col

        elif _diff_col is None:
            # Neutral seed: a fresh draw_state adopts the diff pieces the
            # last session left collapsed (TextEditorState
            # .restore_diff_collapsed - the snapshot block at the tail
            # writes it; the tuples are buffer lines, so they land on
            # today's pieces by overlap across any drift). None captured
            # = expanded.
            _saved = (text_editor_state.restore_diff_collapsed
                      if text_editor_state is not None else None)
            if _saved:
                _saved = sorted(tuple(_r) for _r in _saved)
                _diff_col = _carry_diff_collapse(_saved, set(_saved),
                                                 _diff_rngs)
            else:
                _diff_col = set()
        elif getattr(ds, '_diff_prev_ranges', None) != _diff_rngs:
            # Neutral drift (an edit moved the span around): carry each
            # span's state onto the new span(s) overlapping it; a gap
            # overlapping nothing from last frame starts expanded.
            _diff_col = _carry_diff_collapse(
                getattr(ds, '_diff_prev_ranges', None) or [], _diff_col,
                _diff_rngs)
        ds._diff_fold_collapsed = _diff_col
        if getattr(ds, '_diff_prev_ranges', None) != _diff_rngs:
            ds._diff_prev_ranges = list(_diff_rngs)
    elif (getattr(ds, '_diff_fold_collapsed', None) is not None
          and not restore_active):
        # Compare off: drop the diff state so the next compare reseeds from
        # the switch. (Restore stand-in frames keep it - the real buffer is
        # about to land.)
        ds._diff_fold_collapsed = None
        ds._diff_prev_ranges = None
        ds._diff_search_exp = None
        ds._diff_gap_index = None
    # Stamped for the badge painters (gutter chevrons, fold labels): a range
    # in this set wears the diff tint (Toggles.TextEditor.diff_fold_tint)
    # and its badge toggles the DIFF collapse set.
    ds._diff_rng_set = _diff_rng_set
    # Expand-all (the owner's toggle True): the diff spans are all open and
    # stay open, so their chevrons / badges are noise - the gutter and badge
    # passes draw nothing for them (Lukas 09-01: "hide the diff spans
    # altogether"). The painter still carries the ranges, so a flip back to
    # collapse-all keeps its viewport anchor. Neutral mode keeps the
    # chevrons: an expanded span there is one the hand can re-collapse.
    _hide_expanded_diff = expand_diff is True and bool(_diff_rng_set)
    if ((fold_ranges or _diff_rngs) and not restore_active
            and not single_line and not is_search_box):
        if _fold_key_of is not None:
            # Collapse state is stored as line-independent KEYS (ds._fold_keys
            # / _fold_search_exp_keys); the range-tuple sets every editor
            # below mutates are a per-frame PROJECTION through this frame's
            # key->range map, harvested back to keys right before the build.
            # Tuples never need shifting on edits - a key re-projects onto
            # wherever the fold is now; a key whose fold vanished simply
            # projects to nothing until it reappears. Map stashed on the
            # draw_state for the external writers (fold_project_jump).
            # Inverse map memoized on the key map's identity (~2.5k entries
            # on a big file - rebuilding it every frame was 0.4 ms).
            _fro = getattr(ds, '_fold_range_of_memo', None)
            if _fro is not None and _fro[0] is _fold_key_of:
                _fold_range_of = _fro[1]
            else:
                _fold_range_of = {k: r for r, k in _fold_key_of.items()}
                ds._fold_range_of_memo = (_fold_key_of, _fold_range_of)
            ds._fold_key_of = _fold_key_of
            # Session restore: a brandless draw_state (keys and tuples both
            # unset) gets the persisted fold keys captured last session
            # (TextEditorState.restore_fold_keys - the snapshot block at the
            # tail writes them). The keys are line-independent, so they
            # project onto wherever those folds live in the loaded text; on
            # a restore stand-in they project to nothing (already-spliced
            # display text) and re-project correctly when the real buffer
            # lands. Stamping _fold_seed_ver keeps the default_collapsed
            # union below from re-collapsing folds the user had expanded -
            # the restored set (even an EMPTY one) IS the user's state.
            if (getattr(ds, '_fold_keys', None) is None
                    and getattr(ds, '_fold_collapsed', None) is None
                    and text_editor_state is not None
                    and text_editor_state.restore_fold_keys is not None):
                ds._fold_keys = set(text_editor_state.restore_fold_keys)
                ds._fold_seed_ver = _FOLD_SEED_VER
            if (getattr(ds, '_fold_keys', None) is None
                    and getattr(ds, '_fold_collapsed', None) is not None):
                # Legacy tuple-based state (pre-key session): adopt once.
                ds._fold_keys = {_fold_key_of[r] for r in ds._fold_collapsed
                                 if r in _fold_key_of}
            if getattr(ds, '_fold_keys', None) is not None:
                ds._fold_collapsed = {_fold_range_of[k] for k in ds._fold_keys
                                      if k in _fold_range_of}
            if getattr(ds, '_fold_search_exp_keys', None) is not None:
                ds._fold_search_exp = {
                    _fold_range_of[k] for k in ds._fold_search_exp_keys
                    if k in _fold_range_of}
        # Default-collapsed seeding is VERSIONED: the seed normally fires once
        # per FILE (draw_states are session-lived, surviving hotswap and file
        # close&open), so when the default_collapsed SOURCES change (bump
        # _FOLD_SEED_VER) already-seeded editors union the new defaults in
        # once instead of never seeing them. Union, not replace - the user's
        # own collapsed scopes stay collapsed. The stamp only advances on a
        # frame where the default scan actually ran (_fold_default_col computed)
        # so a diff-mode edit can't swallow the one-shot union.
        if getattr(ds, '_fold_collapsed', None) is None:
            ds._fold_collapsed = set(_fold_default_col or ())
            # Caller-declared default-collapsed folds: `default_collapsed_lines`
            # (0-based buffer HEADER lines) marks the folds STARTING on those
            # lines collapsed on this editor's first sight - the stack trace
            # view seeds each pane with its root scope folded. First-sight only,
            # like the built-in defaults: a badge toggle owns the state after.
            if default_collapsed_lines and fold_ranges:
                _dcl = set(default_collapsed_lines)
                ds._fold_collapsed |= {r for r in fold_ranges
                                       if r[0] in _dcl}
            ds._fold_seed_ver = _FOLD_SEED_VER
        elif (_fold_default_col is not None
              and getattr(ds, '_fold_seed_ver', 0) != _FOLD_SEED_VER):
            ds._fold_seed_ver = _FOLD_SEED_VER
            _new = set(_fold_default_col) - ds._fold_collapsed
            if _new:
                ds._fold_collapsed |= _new
                ds.invalidate()
        if getattr(ds, '_fold_search_exp', None) is None:
            # Folds auto-expanded to reveal the current search match, pending
            # re-collapse when the selection moves on (see the search-driven
            # return block below).
            ds._fold_search_exp = set()
        # Badge click against LAST frame's rects: this frame's layout depends
        # on the toggle, so it must resolve before the display text is built.
        _fold_toggled = None
        if left_mouse_down:
            for _fr, _rng in (getattr(ds, '_fold_badge_rects', None) or []):
                if (_fr[0] <= left_mouse_down.x < _fr[2]
                        and _fr[1] <= left_mouse_down.y < _fr[3]):
                    _fold_toggled = _rng
                    # A diff span toggles the DIFF collapse set; scope /
                    # caller ranges toggle the classic set (the durable one).
                    _is_diff_toggle = (_rng in _diff_rng_set
                                       and ds._diff_fold_collapsed is not None)
                    _tgt = (ds._diff_fold_collapsed if _is_diff_toggle
                            else ds._fold_collapsed)
                    if _rng in _tgt:
                        _tgt.discard(_rng)
                    else:
                        _tgt.add(_rng)
                    if _is_diff_toggle:
                        # Tell the owner a hand took over: draw_code_editor /
                        # merge_files watch this and clear their expand_diff
                        # switch to None (else the active switch would
                        # re-assert next frame and undo the click).
                        ds._diff_manual_gen = getattr(
                            ds, '_diff_manual_gen', 0) + 1
                    # A manual toggle overrides any pending search-restore.
                    ds._fold_search_exp.discard(_rng)
                    if getattr(ds, '_diff_search_exp', None):
                        ds._diff_search_exp.discard(_rng)
                    ds.invalidate()
                    request_render()


                    break
        # Keyboard folding — Ctrl+Minus/Equal collapse/expand the scope at
        # the caret (repeated presses walk outward: Ctrl+- folds the next
        # enclosing open scope, Ctrl+= opens the folds in the next
        # enclosing scope), Ctrl+Shift+Minus/Equal every ROOT scope. With a
        # SELECTION all four act only INSIDE it - on the folds whose HEADER
        # line the selection covers (the badges it sweeps over): Ctrl+-/=
        # fold/open the outermost of those (one level per press), the Shift
        # pair is collapse/expand-all restricted to them; the selection
        # survives the reflow so a follow-up press can act on it again. A
        # selection over no header line switches to the caret rules. The
        # events are hover-routed like any event param but gated on text
        # focus (same rationale as Ctrl+B - only the focused editor may act
        # on its caret). Resolved here, before the display build, for the
        # same reason as the badge click - this frame's layout depends on
        # the toggle.
        _fold_kb_all, _cp_full, _fstarts = False, None, None
        _fold_old_dline = None      # caret's display line BEFORE the toggle
        _fold_sel_full = None       # (start, end) of a kept selection, FULL buffer
        if ((ctrl_minus_down or ctrl_equal_down or ctrl_shift_minus_down
             or ctrl_shift_equal_down)
                and (Melty.text_focused_ds is ds
                     or (Melty.text_focused_ds is not None
                         and getattr(Melty.text_focused_ds, '_tile_id', None)
                         == ds._tile_id))):
            # SCOPE ranges only - the Ctrl+-/= family never touches the
            # diff spans (they answer to expand_diff and their own badges).
            _rngs = _fold_normalize_ranges(len(_line_starts(input_value)),
                                           fold_ranges or ())
            _fstarts = _line_starts(input_value)
            # The caret and selection live in DISPLAY coords - last frame's
            # layout maps them back to full-buffer coords (identity when
            # nothing is collapsed).
            _pc = getattr(ds, '_fold_cache', None)
            _pdisp = _pd2b = None
            if (_pc is not None and _pc[0] is input_value
                    and _pc[2][3] is not None):
                _pdisp, _pd2b = _pc[2][0], _pc[2][3]

            def _disp_to_full(_off):
                """Display offset → (full offset, full line, display line)."""
                if _pd2b is None:                # identity layout last frame
                    _off = min(_off, len(input_value))
                    _ln = input_value.count('\n', 0, _off)
                    return _off, _ln, _ln
                _off = min(_off, len(_pdisp))
                _col = _off - (_pdisp.rfind('\n', 0, _off) + 1)
                _dl = _pdisp.count('\n', 0, _off)
                _ln = _pd2b[min(_dl, len(_pd2b) - 1)]
                return _fstarts[_ln] + _col, _ln, _dl

            _cp_full, _cline, _fold_old_dline = _disp_to_full(
                ds.text_cursor_pos or 0)
            # Selection folds: the folds whose header line the selection
            # covers. Empty → the caret rules below, unchanged.
            _sel_folds = None
            if _has_selection(ds):
                _sel_fs, _sel_ls, _ = _disp_to_full(ds.text_selection_start)
                _sel_fe, _sel_le, _ = _disp_to_full(ds.text_selection_end)
                _sel_lo, _sel_hi = min(_sel_ls, _sel_le), max(_sel_ls, _sel_le)
                # An end sitting at column 0 (the drag ran onto the next
                # line) doesn't cover that line.
                if _sel_hi > _sel_lo and max(_sel_fs, _sel_fe) == _fstarts[_sel_hi]:
                    _sel_hi -= 1
                _sel_folds = [r for r in _rngs if _sel_lo <= r[0] <= _sel_hi]
                if _sel_folds:
                    _fold_sel_full = (_sel_fs, _sel_fe)
                else:
                    _sel_folds = None
            if _sel_folds is not None:
                # Outermost of the covered folds = those not nested in
                # another covered fold (sorted, strictly nested - same walk
                # as the root scan below).
                _skip = set(_fold_default_col or ())
                _outer, _open_end = [], -1
                for _r in _sel_folds:
                    if _r[0] > _open_end:
                        _outer.append(_r)
                        _open_end = _r[1]
                if ctrl_shift_minus_down:
                    # Collapse-all inside: the outermost plus every
                    # default-collapsed run covered (nested ones too - the
                    # same asymmetry as the whole-buffer variant).
                    _targets = set(_outer) | (_skip & set(_sel_folds))
                    ds._fold_collapsed.update(_targets)
                elif ctrl_minus_down:
                    _targets = set(_outer)
                    ds._fold_collapsed.update(_targets)
                elif ctrl_shift_equal_down:
                    # Expand-all inside: everything covered except the
                    # default-collapsed runs.
                    _targets = {r for r in _sel_folds if r not in _skip}
                    ds._fold_collapsed.difference_update(_targets)
                else:
                    # One level out: the VISIBLE collapsed covered folds -
                    # those not hidden inside another collapsed covered
                    # fold. Default-collapsed runs included: a selection
                    # over one is the caret sitting ON it.
                    _targets, _open_end = set(), -1
                    for _r in _sel_folds:
                        if _r in ds._fold_collapsed and _r[0] > _open_end:
                            _targets.add(_r)
                            _open_end = _r[1]
                    ds._fold_collapsed.difference_update(_targets)
                ds._fold_search_exp.difference_update(_targets)
                _fold_kb_all = True
                ds.invalidate()
                request_render()
            elif ctrl_shift_minus_down or ctrl_shift_equal_down:
                # Root scopes: the first nesting level with at least two
                # scopes (see fold_root_scopes - a lone top-level class or
                # function is looked through so collapse-all folds its
                # members, not the whole span).
                # Default-collapsed ranges (top import block, comment runs)
                # are ASYMMETRIC: collapse-all folds them along with the
                # roots (nested comment runs included, so they're still
                # folded if their root is later expanded), but expand-all
                # leaves them untouched - they only expand via their own
                # badge or the caret-scoped shortcuts.
                _skip = set(_fold_default_col or ())
                _roots = fold_root_scopes(_rngs, _skip)
                if ctrl_shift_minus_down:
                    _targets = set(_roots) | _skip
                    ds._fold_collapsed.update(_targets)
                else:
                    _targets = [r for r in _roots if r not in _skip]
                    ds._fold_collapsed.difference_update(_targets)
                ds._fold_search_exp.difference_update(_targets)
                _fold_kb_all = True
                ds.invalidate()
                request_render()
            else:
                _at = [r for r in _rngs if r[0] <= _cline <= r[1]]
                _rng = None
                if ctrl_minus_down:
                    # Innermost not-yet-collapsed scope at the caret.
                    _rng = next((r for r in reversed(_at)
                                 if r not in ds._fold_collapsed), None)
                    if _rng is not None:
                        ds._fold_collapsed.add(_rng)
                else:
                    # Outermost collapsed scope at the caret - the VISIBLE
                    # one (an inner collapsed fold is hidden by its outer).
                    _rng = next((r for r in _at
                                 if r in ds._fold_collapsed), None)
                    if _rng is not None:
                        ds._fold_collapsed.discard(_rng)
                    else:
                        # Everything at the caret is already open: walk
                        # OUTWARD - expand the collapsed folds nested in the
                        # innermost scope at the caret, then (next press) in
                        # the surrounding one, ... up to the whole buffer -
                        # so Ctrl+= repeated progressively reveals more, the
                        # inverse of Ctrl+- collapsing progressively outward.
                        # Default-collapsed folds (docstrings, comment runs,
                        # imports) are skipped like expand-all does; they
                        # open only when the caret touches them.
                        _skip = set(_fold_default_col or ())
                        _scopes = list(reversed(_at)) + [
                            (0, input_value.count('\n'))]
                        for _sr in _scopes:
                            _inside = {r for r in ds._fold_collapsed
                                       if _sr[0] <= r[0] and r[1] <= _sr[1]
                                       and r != _sr and r not in _skip}
                            if _inside:
                                ds._fold_collapsed -= _inside
                                ds._fold_search_exp -= _inside
                                _fold_kb_all = True   # multi-fold caret path
                                ds.invalidate()
                                request_render()
                                break
                if _rng is not None:
                    _fold_toggled = _rng
                    ds._fold_search_exp.discard(_rng)
                    ds.invalidate()
                    request_render()
        # --- Search-driven temporary expansion ---------------------------
        # While the find UI cycles matches, the fold(s) hiding the CURRENT
        # match auto-expand and re-collapse when the selection moves on
        # (pending set: ds._fold_search_exp). The expansion COMMITS - the
        # pending set is dropped, folds stay open - when the search ends or
        # the editor itself takes text focus (the search UI takes it).
        # Edge-keyed on (term, current-match) so a manual re-collapse of
        # an auto-expanded fold isn't fought the very next frame. Matches
        # are found in the FULL buffer (input_value here, pre-splice); the
        # search section below shares _search_match_cache and projects them
        # into display coords on fold/scroll.
        _sq = str(search_text or (ds.search_text if ds.search_active else ""))
        if _sq and Melty.text_focused_ds is not ds:
            _scur = getattr(ds, '_search_active_local', None)
            _sk = (_sq, _scur)
            if getattr(ds, '_fold_search_seen', None) != _sk:
                ds._fold_search_seen = _sk
                _sli = None
                if _scur is not None:
                    _smc = getattr(ds, '_search_match_cache', None)
                    if (_smc is None or _smc[0] is not input_value
                            or _smc[1] != _sq):
                        _smc = (input_value, _sq,
                                _find_matches(input_value, _sq))
                        ds._search_match_cache = _smc
                    if _scur < len(_smc[2]):
                        _sli = input_value.count('\n', 0, _smc[2][_scur][0])
                _fold_sch = False
                _diff_col_live = getattr(ds, '_diff_fold_collapsed', None)
                _diff_exp_live = getattr(ds, '_diff_search_exp', None)
                if _sli is not None:
                    for _r in [r for r in ds._fold_collapsed
                               if r[0] < _sli <= r[1]]:
                        ds._fold_collapsed.discard(_r)
                        ds._fold_search_exp.add(_r)
                        _fold_sch = True
                    # A collapsed DIFF gap hiding the match expands the same
                    # way and restored through its own pending set.
                    if _diff_col_live is not None and _diff_exp_live is not None:
                        for _r in [r for r in _diff_col_live
                                   if r[0] < _sli <= r[1]]:
                            _diff_col_live.discard(_r)
                            _diff_exp_live.add(_r)
                            _fold_sch = True
                # Folds expanded for an EARLIER match restore once the
                # current match leaves them (moves on, or left this editor).
                for _r in [r for r in ds._fold_search_exp
                           if _sli is None or not (r[0] < _sli <= r[1])]:
                    ds._fold_search_exp.discard(_r)
                    ds._fold_collapsed.add(_r)
                    _fold_sch = True
                if _diff_col_live is not None and _diff_exp_live is not None:
                    for _r in [r for r in _diff_exp_live
                               if _sli is None or not (r[0] < _sli <= r[1])]:
                        _diff_exp_live.discard(_r)
                        _diff_col_live.add(_r)
                        _fold_sch = True
                if _fold_sch:
                    # MANY folds could move at once - reuse the collapse/
                    # expand-all caret projection: map the caret to full
                    # coords via LAST frame's layout here, project into the
                    # new layout after the build.
                    _fstarts = _line_starts(input_value)
                    _cp = min(ds.text_cursor_pos or 0, len(input_value))
                    _pc = getattr(ds, '_fold_cache', None)
                    if (_pc is not None and _pc[0] is input_value
                            and _pc[2][3] is not None):
                        _pdisp, _pd2b = _pc[2][0], _pc[2][3]
                        _cp = min(_cp, len(_pdisp))
                        _col = _cp - (_pdisp.rfind('\n', 0, _cp) + 1)
                        _dl = _pdisp.count('\n', 0, _cp)
                        _cp_full = (_fstarts[_pd2b[min(_dl, len(_pd2b) - 1)]]
                                    + _col)
                    else:
                        _cp_full = _cp
                    # Deliberately NO _fold_old_dline: that arms the
                    # collapse-all scroll-anchor hold, which re-asserts the
                    # PRE-expand scroll for several frames and stomps the
                    # search section's scroll-to-match below (Enter on a
                    # match inside a collapsed fold expanded it but the
                    # view stayed put). The search scroll owns the view.
                    _fold_kb_all = True
                    ds.invalidate()
                    request_render()
        elif (ds._fold_search_exp
              or getattr(ds, '_diff_search_exp', None)
              or getattr(ds, '_fold_search_seen', None) is not None):
            # Search over, or focus moved into this editor: commit - the
            # auto-expanded folds stay open. Seen-key resets too, so a
            # reopened search with the same term immediately re-runs the expand.
            ds._fold_search_exp.clear()
            if getattr(ds, '_diff_search_exp', None):
                ds._diff_search_exp.clear()
            ds._fold_search_seen = None
        if _fold_key_of is not None:
            # Harvest: every mutation above worked on the projected tuples;
            # convert back so the KEYS stay the single durable truth.
            ds._fold_keys = {_fold_key_of[r] for r in ds._fold_collapsed
                             if r in _fold_key_of}
            ds._fold_search_exp_keys = {
                _fold_key_of[r] for r in ds._fold_search_exp
                if r in _fold_key_of}
        # The range tuple is memoized on the fold_ranges list's identity:
        # the scope scan hands the same list every frame (~2.4k ranges on a
        # big file, so the tuple was 2.3k genexpr calls a frame).
        # The diff spans are few and re-built per frame, so their tuple is
        # rebuilt inline and simply concatenated on - _fold_build normalizes
        # the union (partial scope/diff overlaps merge deterministically).
        _frt = getattr(ds, '_fold_ranges_tuple', None)
        if _frt is None or _frt[0] is not fold_ranges:
            _frt = (fold_ranges, tuple(tuple(r) for r in (fold_ranges or ())))
            ds._fold_ranges_tuple = _frt
        _fold_union_col = ds._fold_collapsed
        if getattr(ds, '_diff_fold_collapsed', None):
            _fold_union_col = ds._fold_collapsed | ds._diff_fold_collapsed
        # Headless candidates memoized on the range tuple and toggle (the
        # scan hands the same list every frame; ~2.4k lookups otherwise).
        _fhc = getattr(ds, '_fold_headless_cache', None)
        _fh_tog = Toggles.TextEditor.hide_meta_comment_folds
        if _fhc is None or _fhc[0] is not _frt[1] or _fhc[1] != _fh_tog:
            _fhc = (_frt[1], _fh_tog,
                    _fold_headless_set(_fold_key_of, _frt[1]))
            ds._fold_headless_cache = _fhc
        _fk = (_frt[1] + tuple(_diff_rngs), frozenset(_fold_union_col),
               _fhc[2])
        _fc = getattr(ds, '_fold_cache', None)
        if _fc is not None and _fc[0] is input_value and _fc[1] == _fk:
            _fold_built = _fc[2]
        else:
            _fold_built = _fold_build(input_value, _fk[0], _fold_union_col,
                                      _fk[2])
            ds._fold_cache = (input_value, _fk, _fold_built)
        _disp, _fold_segments, _fold_folds, _fold_d2b = _fold_built
        # Caret keeps its glyph across a toggle: offsets up to the toggled
        # fold's anchor are identical in both layouts, so the NEW layout's
        # anchor/hidden-length adjust the old offset directly.
        if _fold_toggled is not None and ds.text_cursor_pos is not None:
            _fi = next((f for f in _fold_folds if f[0] == _fold_toggled), None)
            if _fi is not None:
                _a, _hl = _fi[5], _fi[6]
                _cp = ds.text_cursor_pos
                if _fi[2]:                       # now collapsed
                    ds.text_cursor_pos = (_cp - _hl if _cp > _a + _hl
                                          else min(_cp, _a))
                else:                            # now expanded
                    # The OLD layout's collapsed fold knows what was
                    # hidden where - a headless comment fold anchored on
                    # the line above and hid the header too, which the
                    # expanded version (header-anchored) can't tell.
                    _fo = None
                    if _fc is not None and _fc[0] is input_value:
                        _fo = next((f for f in _fc[2][2]
                                    if f[0] == _fold_toggled and f[2]), None)
                    if _fo is not None:
                        _a, _hl = _fo[5], _fo[6]
                    if _cp > _a:
                        ds.text_cursor_pos = _cp + _hl
            ds.text_selection_start = ds.text_selection_end = ds.text_cursor_pos
        elif _fold_kb_all and ds.text_cursor_pos is not None:
            # Collapse/expand-all can move MANY folds at once, so the single-
            # toggle anchor shift above doesn't apply - project the caret's
            # full-char offset into the NEW layout instead. A caret inside a
            # now-hidden line clamps to its covering fold header's end.
            if _fold_d2b is None:
                def _full_to_disp(_off):
                    return _off
            else:
                _dstarts = _line_starts(_disp)

                def _full_to_disp(_off):
                    _b = bisect.bisect_right(_fstarts, _off) - 1
                    _i = max(bisect.bisect_right(_fold_d2b, _b) - 1, 0)
                    if _fold_d2b[_i] == _b:
                        return _dstarts[_i] + (_off - _fstarts[_b])
                    return (_dstarts[_i + 1] - 1 if _i + 1 < len(_dstarts)
                            else len(_disp))
            ds.text_cursor_pos = _full_to_disp(_cp_full)
            if _fold_sel_full is not None:
                # Selection-toped fold: the selection rides along so the
                # next press can act on the same span again.
                ds.text_selection_start = _full_to_disp(_fold_sel_full[0])
                ds.text_selection_end = _full_to_disp(_fold_sel_full[1])
            else:
                ds.text_selection_start = ds.text_selection_end = ds.text_cursor_pos
            # Anchor the VIEW to the caret: collapse/expand-all reflows the
            # whole layout, so a kept scroll offset lands somewhere random.
            # Stash (new display line, old display line, pre-expand scroll):
            # the scroll write itself happens after line_px is known - and
            # RE-PEATS for a few frames, because ds.invalidate() above
            # resets the measured content height and the wrapper's scroll
            # limit (core_render max_scroll_y) wipes a one-shot write to 0
            # (collapse) or drags it back to the stale pre-expand max
            # (expand) before the new height lands.
            if _fold_old_dline is not None:
                _new_dl = (bisect.bisect_right(
                    _line_starts(_disp), ds.text_cursor_pos) - 1)
                ds._fold_scroll_anchor = (_new_dl, _fold_old_dline,
                                          ds.scroll_offset[1], 8)
        # expand_diff flip: hold the viewport's MIDPOINT line fixed. The
        # midpoint's buffer line was mapped through the OLD layout at the
        # flip (the diff state machine above); project it into NEW
        # layout's display line - a midline now hidden inside a collapsed
        # gap lands on the gap's header row - and arm the same multi-frame
        # scroll-anchor as the keyboard collapse/expand-all above.
        _dfa = getattr(ds, '_diff_flip_anchor', None)
        if _dfa is not None:
            ds._diff_flip_anchor = None
            _mid_bl, _old_mid_dl, _o_sy, _cp_full_flip = _dfa
            if _fold_d2b is None:
                _new_mid_dl = _mid_bl
            else:
                _bi = int(_mid_bl)
                _new_mid_dl = (max(bisect.bisect_right(_fold_d2b, _bi) - 1, 0)
                               + (_mid_bl - _bi))
            ds._fold_scroll_anchor = (_new_mid_dl, _old_mid_dl, _o_sy, 8)
            if _cp_full_flip is not None:
                # Re-project the caret into the new layout (a caret inside a
                # now-hidden body clamps to its covering fold header's end -
                # the kb collapse-all's rule), and sync prev so the vertical
                # caret-follow reads no move: the anchor holds the view.
                if _fold_d2b is None:
                    _new_cp = min(_cp_full_flip, len(input_value))
                else:
                    _ffs = _line_starts(input_value)
                    _dstarts_f = _line_starts(_disp)
                    _bl_f = bisect.bisect_right(_ffs, _cp_full_flip) - 1
                    _i_f = max(bisect.bisect_right(_fold_d2b, _bl_f) - 1, 0)
                    if _fold_d2b[_i_f] == _bl_f:
                        _new_cp = _dstarts_f[_i_f] + (_cp_full_flip
                                                      - _ffs[_bl_f])
                    else:
                        _new_cp = (_dstarts_f[_i_f + 1] - 1
                                   if _i_f + 1 < len(_dstarts_f)
                                   else len(_disp))
                ds.text_cursor_pos = _new_cp
                ds.text_selection_start = ds.text_selection_end = _new_cp
                ds.text_prev_cursor_pos = _new_cp
        if _fold_segments:
            input_value = _disp
            # Full → display coordinate bridges for the tree-derived overlays
            # (error markers, usage washes, def tints). Those all resolve in
            # FULL-buffer coordinates (fold_full); these helpers project the
            # RESULTS into display coords instead of disabling the features.
            _fold_fstarts = _line_starts(_fold_full)
            _fold_dstarts = _line_starts(input_value)

            def _fold_bl(b):
                # 0-based buffer line -> 0-based display line. disp_to_buf is
                # sorted, so the last visible buffer line <= b IS b itself
                # when visible - and a hidden line's covering fold HEADER
                # otherwise (errors on hidden lines light up the collapsed
                # header rather than vanishing).
                return max(bisect.bisect_right(_fold_d2b, b) - 1, 0)

            def _fold_off(o):
                # Full char offset -> disp offset; None while the offset's
                # line is hidden (a wash inside a collapsed body draws nowhere).
                b = bisect.bisect_right(_fold_fstarts, o) - 1
                i = bisect.bisect_right(_fold_d2b, b) - 1
                if i < 0 or _fold_d2b[i] != b:
                    return None
                return _fold_dstarts[i] + (o - _fold_fstarts[b])

            def _fold_remap_spans(spans, slot):
                # Char-offset span tuples (start, end, *rest) -> display
                # coords, hidden spans dropped. Memoized per slot on (spans
                # identity, fold layout identity) - both held in runtime so
                # id reuse doesn't alias - because downstream memos (like
                # usage-heat aggregation) key on the RESULT's id, so it must
                # be stable frame to frame.
                if not spans:
                    return spans
                _mms = getattr(ds, '_fold_span_memo', None)
                if _mms is None:
                    _mms = ds._fold_span_memo = {}
                _mm = _mms.get(slot)
                if (_mm is not None and _mm[0] is spans
                        and _mm[1] is _fold_built):
                    return _mm[2]
                out = []
                for sp in spans:
                    s = _fold_off(sp[0])
                    if s is None:
                        continue
                    e = _fold_off(sp[1])
                    if e is None:
                        e = s + (sp[1] - sp[0])   # span runs through the seam
                    out.append((s, e) + tuple(sp[2:]))
                out = tuple(out)
                _mms[slot] = (spans, _fold_built, out)
                return out

    # Plain-text mode (codec tells "not Python source"): no Darcula colors and
    # no inline token widgets - both are artifacts of the Python tokenizer.
    if not syntax_highlight or not show_widgets:
        token_views = {}

    elif token_views is None:
        token_views = DEFAULT_TOKEN_VIEWS   # an experiment fallback (see a     

    # Symbol-usage source: the parse arrives as `code_tree` in the
    # address_to_general_parse routes, as `code_dict` in the CODE_UI routes
    # (cst_module_to_dict - which is also where the run_jedi() pass attaches
    # __symbol_usages__). Links are file-absolute, so the buffer's file offset
    # comes from the parse's line_offset when set, else from the jump_to span.
    # The FILE route passes the syntax-ERROR MARKER dict ({'__error__', ...})
    # as code_tree while the real (possibly blank-line-repaired) parse rides    # in code_dict - the marker must not shadow the parse, or every tree-
    # derived wash (class blocks, symbol tints) vanishes for the whole
    # duration of the mid-edit syntax error.
    _usage_tree = code_tree if (code_tree is not None
                                and not (isinstance(code_tree, dict)
                                         and "__error__" in code_tree)) else code_dict

    _usage_off = getattr(_usage_tree, 'line_offset', 0) or 0
    if not _usage_off and jump_to is not None:
        _usage_off = getattr(jump_to, 'start', 0) or 0
    # Bridge disk→pending coordinates: index sites are computed over the
    # PENDING file text, while the span start above is a DISK coordinate. An
    # unsaved disk edit above this span that changed the line count shifts
    # every site - fold that shift into the offset (0 when nothing is pending).
    if not getattr(jump_to, 'pending_coords', False):   # search results: already pending
        _usage_off += _pending_line_delta(getattr(jump_to, 'path', None), _usage_off)

    def _view_usage_spans(vpath):
        """Usage spans in DISPLAY coordinates: always collected/resolved
        against the FULL buffer (fold-independent, so the resolve caches stay
        hot across collapse/expand), then projected through the fold remap
        when folds are collapsed."""
        # Late-bound resolve target: with no collapsed fold the display
        # text IS the full buffer, and `text` is the one the glyph pass
        # will draw THIS frame - edits earlier in this body run re-write
        # it - while _fold_full is the frame-start buffer. Resolving
        # against _fold_full put every wash below an inserted newline one
        # line off for exactly the edit frame (the snap-back flicker).
        spans = _usage_spans(ds, _fold_full if _fold_segments else text,
                             _usage_tree, _usage_off, vpath)
        if _fold_remap_spans is not None:
            spans = _fold_remap_spans(spans, 'usage')
            if _disp_sp is not None:
                # A frame with a fold collapsed: the resolve + remap above
                # are frame-start; shift across this run's splice so the
                # washes track the glyphs (see _display_splice_shift).
                _, _, spans, _ = _display_splice_shift(_disp_sp, text,
                                                       spans=spans)
        return spans
    # Per-editor state for the code-suggestions popup. Lives here (not gated on
    # focus) because the popup's menu window is latched and must be drawn EVERY
    # frame with closed_state toggled, even when the editor is unfocused.
    if getattr(ds, '_ac_state', None) is None:
        ds._ac_state = DropDownState()
    ac_state = ds._ac_state

    # Same deal for the usage-jump popup (multi-user symbol Ctrl+B).
    if getattr(ds, '_uj_state', None) is None:
        ds._uj_state = DropDownState()
    uj_state = ds._uj_state
    # Similarly for the import quick-fix chooser (Alt+Enter on a missing-import line).
    if getattr(ds, '_qf_state', None) is None:
        ds._qf_state = DropDownState()
    qf_state = ds._qf_state
    # Imported in-function to avoid a module-load import cycle (toggles pulls in
    # decoration/window machinery). For the spell-check button + squiggles below.
    from src.lsd.gl_gui.toggles import Toggles
    # Error markers to highlight in red: the routed code_tree's parse errors plus
    # any exception routed in via the mode route (e.g. draw_modes hands us the
    # chain_in failure so the offending source line lights up here). Computed up
    # front so the message can ride along into the file header bar.
    if Toggles.TextEditor.check_syntax_errors:
        _ct_errors = _code_tree_errors(code_tree) if code_tree is not None else None
        _err_markers = list(_ct_errors) if _ct_errors else []
        _err_markers += _exception_errors(error)
    else:
        _ct_errors, _err_markers = None, []
    # Fast-path syntax markers (Toggles.TextEditor.fast_syntax_check): the
    # staleness section at the end of the body re-runs a bare compile() per
    # edit and leaves (buffer, SyntaxError-or-None) on ds._fast_err_state.
    # When that record reflects THIS buffer (identity - every real edit is a
    # new string), a found error replaces the background markers outright:
    # they describe an older buffer, this one carries the current line, and
    # being current it survives the stale-hide below. A clean fast check
    # replaces nothing - parsing/lint markers keep the normal debounced flow
    # (compile() says nothing about lint findings).
    _fast_fresh_err = False

    if (Toggles.TextEditor.check_syntax_errors
            and Toggles.TextEditor.fast_syntax_check):
        _fs = getattr(ds, '_fast_err_state', None)
        # Identity against the FULL buffer (_fold_full is input_value when no
        # fold is collapsed): the fast check at the bottom always checks the
        # full text, never the fold-spliced display text.
        if _fs is not None and _fs[0] is _fold_full and _fs[1] is not None:
            _err_markers = _exception_errors(_fs[1])
            # Errors past the first (see _compile_check_more), THIS buffer.
            _fx = getattr(ds, '_fast_err_extra', None)
            if _fx is not None and _fx[0] is _fold_full:
                for _xe in _fx[1]:
                    _err_markers += _exception_errors(_xe)
            _fast_fresh_err = True
    # Import quick-fix bookkeeping. `_qf_fixes` maps line → candidate import
    # statements, fed from the SEPARATE suggestions channel (`import_fixes`,
    # from ModesState.last_imports) - independent of the error markers, so a
    # transient error like a half-typed `json.` never hides the fix. Applied
    # fixes are remembered per payload IDENTITY (`_qf_applied`) and filtered
    # out immediately - a spanless fix doesn't change the buffer, so the
    # stale suggestion/marker would otherwise linger until the next scan; a
    # fresh scan (new identity) resets the memory and re-offers anything the
    # fix didn't actually cure.
    if getattr(ds, '_qf_applied_ct', None) != (id(code_tree), id(import_fixes)):
        ds._qf_applied_ct = (id(code_tree), id(import_fixes))
        ds._qf_applied = set()
    if getattr(ds, '_qf_applied', None):
        _err_markers = [(l, m) for l, m in _err_markers
                        if _missing_name(m) not in ds._qf_applied]
    # Fast-path import suggestions (Toggles.TextEditor.fast_syntax_check): the
    # per-edit section at the end of the body leaves (buffer, {line: [stmts]},
    # background-payload-at-scan-time) on ds._fast_imports_state. It feeds the
    # quick-fix rows when it reflects THIS buffer AND the background channel
    # hasn't swapped in a new payload since the scan (identity on both) - a
    # landed relint is irrelevant (it re-reads the file's pending binds);
    # the fast scan only bridges the debounce gap. The applied-fix memory
    # (_qf_applied) still keys on the background payload identity above and
    # filters the fast rows below, so a just-applied fix isn't re-offered per
    # keystroke while the file's import cache catches up
    _active_fixes = import_fixes
    if Toggles.TextEditor.fast_syntax_check:
        _fi = getattr(ds, '_fast_imports_state', None)
        if _fi is not None and _fi[0] is _fold_full and _fi[2] is import_fixes:
            _active_fixes = _fi[1]
    _qf_fixes = {}


    _qf_names = {}   # line → {names the fixes would bind} - drives the underlines
    if _active_fixes:
        from src.lsd.gl_gui.view.core_conversion.chain_converters import _import_bound_name
        for _ln, _stmts in _active_fixes.items():
            try:
                _ln = int(_ln)
            except (TypeError, ValueError):
                continue
            _row = [_s for _s in _stmts
                    if not (ds._qf_applied and _import_bound_name(_s) in ds._qf_applied)]
            if _row:
                _qf_fixes[_ln] = _row
                _qf_names[_ln] = {n for n in (_import_bound_name(_s) for _s in _row) if n}
    # Fold remap: markers and quick-fix rows carry 1-based FULL-buffer lines;
    # project them onto the display. A marker on a hidden line clamps to its
    # containing fold's line (the shadowed header shows something went wrong
    # inside), and same-header quick-fix rows merge.
    if _fold_bl is not None:
        _err_markers = [(_fold_bl(l - 1) + 1, m) for l, m in _err_markers]
        _rqf, _rqn = {}, {}
        for _ln, _row in _qf_fixes.items():
            _dl = _fold_bl(_ln - 1) + 1
            _rqf.setdefault(_dl, []).extend(_row)
            _rqn.setdefault(_dl, set()).update(_qf_names.get(_ln, ()))
        _qf_fixes, _qf_names = _rqf, _rqn
    # Suppression (clearing _err_markers and _err_msg while keyboard editing) is
    # applied AFTER the keyboard recompute below, so it can read this frame's
    # popup state and the freshly-stamped edit time - see _ERR_SUPPRESS_SEC.
    
    # Jump-to-source button drawn inline at the top (before the monospace font
    # push, so it uses the normal UI font), above the text body. The first error
    # message (if any) is no longer shown inline here - it floats in a small
    # right-aligned box above the error line (see after the body is drawn).
    bar_height = 0.0
    _err_msg = None
    # show_jump_bar=False: jump_to serves ONLY as the buffer's edit scroll
    # offset (tree-derived washes) - no header bar. The search's code rows
    # use a bare offset shim that isn't a full Address, so the bar (which
    # reads .source/.file for its label) must not draw for them.
    show_jump_bar = False # Pin to false
    if jump_to is not None and show_jump_bar:
        _err_msg = _err_markers[0][1] if _err_markers else None
        if not show_file_header:
            # No floating bar: _err_msg still feeds the floating error box
            # after the body, but nothing is drawn inline here. Clear the stashed
            # OpenRectRect so a stale one can't swallow presses.
            draw_state._jump_btn_rect = None
        else:
            # Float the jump-to-file bar at the top of the visible viewport instead
            # of letting it scroll away with the code. If the body has scrolled up
            # under its clip rect, shift the bar down by that overflow so it stays
            # pinned to the clip top; at scroll 0 the content top equals the clip top
            # so float_dy is 0 and the bar sits in its natural place. Drawing it at the
            # shifted (on-screen) cursor also keeps draw_jump_to's own clip rect from
            # collapsing once the content top passes above the viewport.
            _bx, _by = imgui.get_cursor_screen_pos()
            float_dy = max(0.0, draw_state.abs_clip_rect[1] - _by)
            # The float only holds while there's still view BELOW the clip top - once
            # the view's bottom edge rises to meet the bar, the bar rides that edge
            # up and scrolls away like everything else. The bar's natural position
            # is the view top, so its maximum downward shift before its bottom
            # touches the view bottom is height - bar_height (last frame's measure).
            _bar_h = getattr(draw_state, "_float_bar_height", None) or 34.0
            if draw_state.height:
                float_dy = max(0.0, min(float_dy, draw_state.height - _bar_h))
            imgui.set_cursor_screen_pos((_bx, _by + float_dy))
            draw_jump_to(jump_to, width=draw_state.content_width, unique=unique,
                         draw_state=draw_state)
            bar_height = imgui.get_cursor_screen_pos()[1] - (_by + float_dy)
            draw_state._float_bar_height = bar_height
            # Resume body layout at the real (unscrolled) content top so the code
            # lines keep their normal positions; only the bar was floated. The text
            # clip below is raised by bar_height so glyphs never paint over the bar.
            imgui.set_cursor_screen_pos((_bx, _by + bar_height))
    # Manual search row: with manual_search=True the caller skips the floating
    # Find window and this body renders the shared search row itself - at the top
    # of the editor, on the line below the file-editor's nav buttons. Same
    # float-at-clip-top pattern as the jump bar above: the row is PINNED to
    # the visible viewport top (it must not scroll away with the code), while
    # layout continues at the content position so the text keeps its normal
    # coordinates; its height folds into bar_height so the text clip/culling
    # below start at the floating row.
    if manual_search and ds.search_active and not single_line and not is_search_box:
        from src.lsd.gl_gui.view.core_views.new_core_view import draw_search
        _msx, _msy = imgui.get_cursor_screen_pos()
        _ms_h = 50.0
        # Pin at the view's absolute top (plus the jump bar's band when that
        # is showing): abs_top doesn't move with scroll, so the row stays put.
        imgui.set_cursor_screen_pos((_msx, draw_state.abs_top + bar_height))
        draw_search(input_value=ds, width=draw_state.content_width,
                    min_width=100, height=_ms_h, shadow=False,
                    name=f"Find{unique}", return_extras=True)
        bar_height += _ms_h
        imgui.set_cursor_screen_pos((_msx, _msy + _ms_h))
    _pf("head+jump_bar")
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
    # Edit splice for THIS body run's display text (frame-start -> edited),
    # initialized after key handling ("text is final now"); None on non-edit
    # frames and while no fold is collapsed. Feeds _display_splice_shift so
    # fold-remapped washes track the glyphs on the edit frame. Pre-edit
    # _viewed_spans calls inside key handlers leave it as None on purpose:
    # they measure against the not-yet-edited buffer.
    _disp_sp = None

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
    # Collapse/expand toggle view anchoring (see the fold section): hold the
    # caret's line at its pre-toggle scroll position by asserting the absolute
    # target scroll - old scroll shifted by the caret's display-line delta -
    # each frame until the re-measured content height settles (the wrapper's
    # max_scroll_y clamp reads 0/stale height for a frame or two after the
    # toggle's invalidate and would wipe a single line).
    _fsa = getattr(ds, '_fold_scroll_anchor', None)
    _fold_scroll_shift = 0.0
    if _fsa is not None:
        _t_line, _o_line, _o_sy, _fsa_left = _fsa
        if getattr(ds, 'scroll_visible', False):
            # Whole pixels only: line_px is fractional (font height ×
            # line_height), so the anchor target has a sub-pixel part while
            # wheel scrolling keeps scroll_offset integral (core_render ceils
            # it). A fractional scroll shifts the editor's rasterization
            # phase, so lines snapped up/down a pixel after each collapse/
            # expand-all. Rounding also makes the round trip exact:
            # round(round(x) - x) == 0, so expand lands back on the original
            # scroll. The max clamp floors for the same reason.
            _target = float(round(max(0.0, _o_sy + (_t_line - _o_line) * line_px)))
            _mx = getattr(ds, '_max_scroll_y', None)
            if _mx is not None and not ds.invalid_content_height:
                _target = min(_target, float(math.floor(_mx)))   # live max only - stale would cap
            # The wrapper already positioned this frame's content cursor with
            # the PRE-write scroll, so a bare write only shows NEXT frame —
            # the toggle frame flashed the un-adjusted view. origin_y below
            # subtracts this shift so the very first frame paints anchored.
            _fold_scroll_shift = _target - ds.scroll_offset[1]
            ds.scroll_offset = (ds.scroll_offset[0], _target)
        _fsa_left -= 1
        if _fsa_left <= 0 or not getattr(ds, 'scroll_visible', False):
            ds._fold_scroll_anchor = None
        else:
            ds._fold_scroll_anchor = (_t_line, _o_line, _o_sy, _fsa_left)
            request_render()   # the hold needs the follow-up frames
    # Viewport tokenization: tokenize ONLY the clipped line range each frame
    # (see the `_line_open` / `_window_tokens` machinery above), so the per-frame
    # syntax cost is O(visible) instead of O(buffer). `_window()` returns
    # (start_line, start_offset, tokens, vcols) for the current text + visible
    # range, cached on the draw_state. It's lazy + keyed by (text, range), so a
    # click (pre-edit text) and the render (post-edit text) each get a window
    # for their state, but the render loop reuses the click's computation.
    def _window():
        nlines = len(_line_starts(text))
        # Visible line band from the clip rect (Y only) - the SAME live
        # abs_clip_rect + bar_height the draw-cull below uses, so the window
        # always covers exactly the lines that get drawn. A few lines of margin
        # keep caret/selection edges just past the clip correct and absorb a
        # frame of drag-scroll.
        _clip = draw_state.abs_clip_rect
        # origin_y, not `top`: on a fold toggle frame the buffer was
        # rewritten mid-body and origin_y carries the same-frame
        # compensation (_fold_scroll_shift) - banding off the stale `top`
        # tokenized the pre-anchor viewport (and, past the shrunken
        # buffer's end, triggered the plain path below). Every call site
        # runs after origin_y is updated; on normal frames the two are
        # identical.
        if line_px:
            v0 = int((_clip[1] + bar_height - origin_y) / line_px) - 3
            v1 = int((_clip[3] - origin_y) / line_px) + 3
        else:
            v0, v1 = 0, nlines - 1
        v0 = max(0, min(v0, nlines - 1))
        v1 = max(v0, min(v1, nlines - 1))
        # Horizontal band (long lines only): the visible column span from the
        # clip rect's X extent and the h-scroll, widened to a margin and
        # quantized to `long_line_band_cols` steps so the cache survives
        # small h-scrolls; the band only joins the key when some line in the
        # window is longer than `long_line_cols` (short lines: band=None,
        # and the key/tokens are exactly what they were before).
        band = None
        _long_cols = Toggles.TextEditor.long_line_cols
        if len(text) > _long_cols and char_w > 0:
            _offs_b = _line_offsets_cached(text)
            _v0b, _v1b = max(0, v0 - 12), v1
            _nl = len(_offs_b)
            if any(((_offs_b[ln + 1] - 1 if ln + 1 < _nl else len(text)) - _offs_b[ln])
                   > _long_cols for ln in range(_v0b, min(_v1b, _nl - 1) + 1)):
                _text_x0 = left + gutter_w + gutter_margin
                _step = max(64, Toggles.TextEditor.long_line_band_cols)
                _bc0 = int((_clip[0] - _text_x0 + ds.text_h_scroll) / char_w)
                _bc1 = int((_clip[2] - _text_x0 + ds.text_h_scroll) / char_w) + 1
                _bc0 = max(0, (_bc0 // _step - 1) * _step)
                _bc1 = (_bc1 // _step + 2) * _step
                band = (_bc0, _bc1, _long_cols)
        key = (text, v0, v1, syntax_highlight, id(token_views) if token_views else 0, band,
               getattr(ds, '_lv_trail_gen', 0))
        if getattr(ds, '_win_key', None) == key:
            return ds._win_data

        _pf_miss_t = time.perf_counter()
        if syntax_highlight:
            if getattr(ds, '_lo_text', None) != text:
                ds._lo_offs, ds._lo_open = _update_line_open(
                    getattr(ds, '_lo_text', None), getattr(ds, '_lo_offs', None),
                    getattr(ds, '_lo_open', None), text)
                ds._lo_text = text
            wl, start_off, toks = _window_tokens(text, ds._lo_offs, ds._lo_open, v0, v1,
                                                 band=band)
            win_len = sum(len(t) for t, _ in toks)
            # Positional trailing gaps (variable- and value labels): the usage
            # overlay stamps {def_start: (frame, {(line0, col): cells})} on
            # the ds; fold the visible entries into window-relative indexes
            # so _build_vcols opens the gaps, then publish each gap's start
            # cell back (ds._lv_trail_cells) for the overlay to paint into.
            pos_trails = None
            _trail_cells_out = {}
            _tv_subs = getattr(ds, '_lv_trail_views', None)
            if _tv_subs:
                _offs_t = _line_offsets_cached(text)
                # The stamps are LAST frame's - coordinates of the frame-start
                # text. On an edit frame this window executes before the overlay
                # re-stamps, so without a remap every gap below an inserted /
                # deleted line sat one line off for that frame (the math
                # found no gap, the code jittered) - the same one-frame lag
                # _display_splice_shift fixes for the washes. Shift each stamp
                # across the frame's edit splice (frame-start → edited text);
                # stamps inside the edited region drop for the frame.
                _tr_sp = _offs_prev = None
                if text is not original_input and isinstance(original_input, str):
                    _trm = getattr(ds, '_lv_trail_splice', None)
                    if (_trm is None or _trm[0] is not original_input
                            or _trm[1] is not text):
                        _trm = (original_input, text,
                                _display_edit_splice(original_input, text),
                                _line_offsets_cached(original_input))
                        ds._lv_trail_splice = _trm
                    _tr_sp, _offs_prev = _trm[2], _trm[3]
                pos_trails = {}
                _tc_src = []
                for _f, _sub in _tv_subs.values():
                    for (_tl, _tc), _cells in _sub.items():
                        if _tr_sp is not None:
                            if _tl >= len(_offs_prev):
                                continue
                            _i_old = _offs_prev[_tl] + _tc
                            if _i_old >= _tr_sp[1]:
                                _i_new = _i_old + _tr_sp[2]
                            elif _i_old < _tr_sp[0]:
                                _i_new = _i_old
                            else:
                                continue        # inside the edit: re-stamped next frame
                            _tl = bisect.bisect_right(_offs_t, _i_new) - 1
                            if _tl < 0:
                                continue
                            _tc = _i_new - _offs_t[_tl]
                        if v0 <= _tl <= v1 and _tl < len(_offs_t):
                            _wi = _offs_t[_tl] + _tc - start_off
                            pos_trails[_wi] = _cells
                            _tc_src.append(((_tl, _tc), _wi, _cells))
                pos_trails = pos_trails or None
            # The gaps must move the DRAWN glyphs too, not just the caret
            # math: split the tokens so every gap boundary begins a token
            # (the glyph pass shifts x at gap-starting tokens - see
            # _lv_gaps there) and publish the absolute-index map.
            _gap_map = {}
            if pos_trails:
                toks = _split_tokens_at(toks, sorted(pos_trails))
                _gap_map = {start_off + _wi: _c
                            for _wi, _c in pos_trails.items()}
            ds._lv_gap_map = _gap_map
            arr = _build_vcols(text[start_off:start_off + win_len], toks, token_views,
                               pos_trails=pos_trails) \
                if (token_views or pos_trails) else None
            if arr is not None and pos_trails:
                for _key_t, _wi, _cells in _tc_src:
                    if 0 <= _wi < len(arr):
                        _trail_cells_out[_key_t] = arr[_wi] - _cells
            ds._lv_trail_cells = _trail_cells_out
            vcols = _WinVCols(arr, start_off) if arr is not None else None
        else:
            # Plain mode: the visible lines as ONE 'default' token (the segment
            # loop below splits it at newlines). No strings → no line_open needed.
            offs = _line_offsets_cached(text)
            wl, start_off = v0, offs[v0]
            end_off = offs[v1 + 1] if v1 + 1 < len(offs) else len(text)
            if band is not None:
                # Same horizontal cut as the syntax path, one 'default' token
                # per visible band (no lexer state to track in plain mode).
                _bc0, _bc1, _long_cols = band
                toks = []
                for ln in range(v0, v1 + 1):
                    ls = offs[ln]
                    le = offs[ln + 1] - 1 if ln + 1 < len(offs) else len(text)
                    if le - ls > _long_cols:
                        a, b = min(le, ls + _bc0), min(le, ls + _bc1)
                        if a > ls:
                            toks.append((text[ls:a], 'clipped'))
                        if b > a:
                            toks.append((text[a:b], 'default'))
                        if le > b:
                            toks.append((text[b:le], 'clipped'))
                    elif le > ls:
                        toks.append((text[ls:le], 'default'))
                    if le < end_off:
                        toks.append(('\n', 'default'))
            else:
                win_text = text[start_off:end_off]
                toks = [(win_text, 'default')] if win_text else []
            vcols = None
            ds._lv_trail_cells = {}
            ds._lv_gap_map = {}
        ds._win_key = key
        ds._win_data = (wl, start_off, toks, vcols)
        _pf_tok[0] += time.perf_counter() - _pf_miss_t
        _pf_tok[1] += 1
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
    # +/- line — they're non-contiguous, so no sequential offset can express
    # them) take priority over the jump_to.start sequential numbering.
    # Folded buffers: display lines map to NON-contiguous buffer lines, so the
    # sequential jump_to.start numbering would lie below a collapsed fold -
    # hand the gutter the per-display-line numbers instead.
    if _fold_d2b is not None:
        if line_numbers is not None:
            line_numbers = [line_numbers[b] if b < len(line_numbers) else None
                            for b in _fold_d2b]
        elif jump_to is not None and getattr(jump_to, 'start', None) is not None:
            line_numbers = [jump_to.start + b + 1 for b in _fold_d2b]
    show_gutter = (not single_line and not is_search_box
                   and (line_numbers is not None
                        or (jump_to is not None
                            and getattr(jump_to, 'start', None) is not None)))
    if show_gutter and line_numbers is not None:
        line_offset = 0
        # Fixed strip, sized for 5-digit lines: explicit line_numbers rows
        # render side by side (usage-picker / global-search code previews,
        # diff lines), and a per-row digit count made every row's code start
        # at a different column — line 980 got a 3-char strip, 6719 a 4-char
        # strip. One fixed strip lines the code up row to row.
        gutter_digits = 5
        gutter_w = gutter_digits * char_w + 12.0
    elif show_gutter:
        line_offset = jump_to.start
        last_line_no = line_offset + len(_line_starts(text))
        gutter_digits = max(len(str(last_line_no)), 2)
        gutter_w = gutter_digits * char_w + 12.0
    else:
        line_offset = 0
        gutter_w = 0.0

    # Instant-restore gutter: the loading stand-in arrives with neither
    # line_numbers nor jump_to, so the gutter vanished for the loading beat
    # and the code column jumped by gutter_w on the swap-in. Reproduce last
    # frame's gutter exactly - same digit count (the width) and the same
    # per-row NUMBERS AND CHEVRONS (restore_gutter_rows: the band's gutter
    # as painted). The sequential restore_line_offset numbering is only the
    # fallback for a pre-rows snapshot - sequential numbers lie below every
    # collapsed fold and carry no fold arrows, so the gutter visibly
    # snapped (213 → 304) the frame the real buffer landed.
    _restore_hdr = None
    # Stand-in DIFF chrome: {display line: hidden count} for the band's
    # diff-gap headers (0 = expanded) and the preview band hidden display lines,
    # both replayed from the snapshot - the diff paint needs the real
    # buffer, so on stand-in frames these are what makes a collapsed
    # diff split look collapsed (bands, "N lines", tinted chevrons,
    # the fade) instead of continuous code until the text lands.
    _restore_diff = None
    _restore_preview = None
    if restore_active and text_editor_state is not None:
        _rb = max(0, int(text_editor_state.restore_first_line))
        _rdr = text_editor_state.restore_diff_rows
        if _rdr:
            _restore_diff = {_rb + _ro: int(_rn) for _ro, _rn in _rdr.items()}
        _rpr = text_editor_state.restore_preview_rows
        if _rpr:
            _restore_preview = {_rb + _ro for _ro in _rpr}
    ds._diff_restore_rows = _restore_diff      # tests / overlays
    if (restore_active and text_editor_state is not None
            and text_editor_state.restore_gutter_digits > 0):
        show_gutter = True
        line_offset = int(text_editor_state.restore_line_offset)
        gutter_digits = int(text_editor_state.restore_gutter_digits)
        gutter_w = gutter_digits * char_w + 12.0
        _rr = text_editor_state.restore_gutter_rows
        if _rr:
            # Band rows carry their captured number / chevron; the visible
            # padding rows around the band show nothing (the paint treats
            # rows past len(line_numbers) - and None entries - as numberless).
            _rb = max(0, int(text_editor_state.restore_first_line))
            line_numbers = [None] * _rb
            _restore_hdr = {}
            for _rn in _rr:
                if _rn is not None and _rn < 0:
                    # chevron row: range None (a stand-in fold can't toggle
                    # - the paint skips the badge rect), -2 = collapsed.
                    _restore_hdr[len(line_numbers)] = (None, _rn == -2)
                    line_numbers.append(None)
                else:
                    line_numbers.append(_rn)

    # Live-marker open/close column: when the live view has registered
    # markers on this editor (per-line registry stamped by
    # draw_live_view_marker), widen the gutter by one button cell so each
    # marker line gets a raw draw-list toggle next to its number. This
    # persists across frames (rebuilt by each overlay pass), so the width is
    # stable - it only appears at all for buffers that have live markers.
    _lv_btn_w = (15.0 if (show_gutter
                          and Toggles.TextEditor.enable_live_view
                          and getattr(ds, "_lv_gutter_markers", None))
                 else 0.0)
    gutter_w += _lv_btn_w

    # Breathing room between the number strip and the code: folded into the
    # text inset (origin_x → rect_min_x below) only - the strip itself keeps
    # gutter_w, so the numbers stay snug in their column and the margin
    # reads the editor background.
    gutter_margin = 5.0 if gutter_w > 0 else 0.0
    # gutter_indent: an extra inset of one indentation level (4 columns -
    # the editor's `indent = '    '`) between the gutter and column 0, so
    # root-level guides and glyphs don't sit flush against the numbers.
    # Folded into the same margin, so every consumer of the text inset
    # (origin_x, rect_min_x, h-scroll limits, caret hit-test) follows.
    if gutter_indent:
        gutter_margin += 4 * char_w

    text_visible_width = draw_state.content_width - gutter_w - gutter_margin
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

    origin_x = left + gutter_w + gutter_margin - ds.text_h_scroll
    # _fold_scroll_shift: same-frame compensation for the fold-anchor scroll
    # write above (larger scroll = content up = smaller origin), so the
    # collapse/expand next frame paints at the anchored position instead of
    # flashing the stale one.
    origin_y = top - _fold_scroll_shift
    # Scroll value origin_y was captured against. Anything later in THIS body
    # run that rewrites ds.scroll_offset (a usage jump centering its target)
    # leaves origin_y stale by exactly the delta - consumers that run after
    # such a write (the vertical cursor-follow) must shift by
    # (_origin_sy - ds.scroll_offset[1]) to get live coords.
    _origin_sy = ds.scroll_offset[1]

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
    _pf("setup")
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
        # Select-all on a fresh grant (the find box on Ctrl+F): the caller
        # requests this only on the one-shot open/re-open frame, so typing
        # replaces the prefilled term and a single delete clears it.
        if select_all_on_focus and text:
            ds.text_selection_start = 0
            ds.text_selection_end = len(text)
            ds.text_cursor_pos = len(text)

    def _goto_usage_ref(ref, token=None):
        """Route one picked UsageRef: a site that lands inside THIS buffer's
        rendered span just moves the caret — the editor's own cursor-follow
        scroll brings it into view on the next body run — instead of
        round-tripping through the external jump (open tab + jump_to_line),
        which re-summons the editor window and loses the local context.
        Anything outside the span (other file, or a line outside a span
        buffer's range) still goes through _open_usage_ref.

        `token` is the symbol's spelling: when given, the caret lands ON that
        token at the target (definition refs carry the def-STATEMENT line with
        col 0 — the caret used to sit on `def`; caller refs record the
        statement-start col). Verify-recovered by _site_span, so a drifted or
        unfindable token falls back to the old statement-start placement."""
        _line = getattr(ref, 'line', None)
        _rpath = getattr(ref, 'path', None)
        _vp = getattr(jump_to, 'path', None) if jump_to is not None else None
        if _line is not None and _rpath is not None and _vp is not None:
            try:
                import os
                _same = os.path.realpath(str(_rpath)) == os.path.realpath(str(_vp))
            except OSError:
                _same = False
            _li = _line - 1 - _usage_off   # file line to FULL-buffer line index
            if _same and 0 <= _li <= _fold_full.count('\n'):
                # Source line (the symbol the jump left from) - decides below
                # whether to center or keep the current scroll. Display
                # coords, not the projected target line.
                _src_li = text.count(
                    '\n', 0, max(0, min(ds.text_cursor_pos, len(text))))
                # Record on the undo timeline before moving the caret: local
                # jumps bypass open_to_line (the only other recorder), so
                # Ctrl+Shift+Left had nothing to step back to after a same-
                # file jump - broken targets appeared. The timeline stores
                # FILE lines, so the display source line maps back through the
                # fold layout first.
                from src.lsd.gl_gui.view.core_views.core_undo import NavUndo
                _nav_win = _enclosing_editor_window(ds)
                _nav_inst = ((getattr(_nav_win, 'instance', 0) or 0)
                             if _nav_win is not None else 0)
                _nav_src_li = (_fold_d2b[min(_src_li, len(_fold_d2b) - 1)]
                               if _fold_d2b is not None else _src_li)
                NavUndo.record_location(
                    (str(_vp), _nav_src_li + 1 + _usage_off, _nav_inst),
                    (str(_vp), _line, _nav_inst))
                # Target resolution runs in FULL-text space (_fold_full is
                # `text` rendered without folds) - the recorded site, ±4 verify,
                # and def recovery all describe the full file; the projection
                # below maps the final pos/line into display space.
                _offs = _line_offsets(_fold_full)
                _ls = _offs[_li]
                _le = (_offs[_li + 1] - 1) if _li + 1 < len(_offs) else len(_fold_full)
                _pos = None
                _tsp = None
                if token:
                    # Caret ON the jumped-to token (leaf of a dotted path),
                    # not the statement start.
                    _tsp = _site_span(_fold_full, _line,
                                      getattr(ref, 'column', 0) or 0,
                                      token.rsplit('.', 1)[-1], _usage_off)
                    if _tsp is not None:
                        _pos = _tsp[0]
                    else:
                        # Wide-drift recovery: the recorded line is faler
                        # than ±4 lines - retarget to the buffer's unique
                        # class/def statement for the token.
                        _rdp = _recover_def_pos(_fold_full, token)
                        if _rdp is not None:
                            _pos = _rdp
                            _li = _fold_full.count('\n', 0, _pos)
                            _line = _li + 1 + _usage_off
                            _uj_log(f"goto LOCAL def-recover -> line {_line}")
                if _pos is None:
                    _pos = min(_ls + (getattr(ref, 'column', 0) or 0), _le)
                    if _pos == _ls:
                        # No column info - land on the code, not the indent.
                        while _pos < _le and _fold_full[_pos] in ' \t':
                            _pos += 1
                # Fold projection: expands any collapsed fold hiding the
                # target, then maps pos/line into display space. The caret,
                # scroll targets and emphasis below all use the projected
                # values; the emphasis columns need the FULL pos (columns are
                # per-line, identical in both spaces).
                _fpos = _pos
                _pos, _li = fold_project_jump(ds, _fold_full, _pos, _li)
                ds.text_cursor_pos = _pos
                ds.text_selection_start = ds.text_selection_end = _pos
                # Center distant targets so they land with context; a nearby
                # one (< 30 lines) keeps the current scroll, with a minimal
                # edge nudge if it sits just past the viewport. When this
                # editor owns its scrollbar, center by writing the scroll in
                # CONTENT coords (same exact math as the cross-file picke
                # path) - the screen-space _scroll_into_view walk proved
                # frame-timing sensitive (Ctrl+B landed ~1500 lines off while
                # the picker path, ms later in the same body, worked fine).
                _far = abs(_li - _src_li) >= 30
                _sy0 = ds.scroll_offset[1]
                if _far and getattr(ds, 'scroll_visible', False):
                    _target = (_li * line_px
                               - max(0.0, (ds.height or 0) - line_px) * 0.5)
                    _mx = getattr(ds, '_max_scroll_y', None)
                    if _mx is not None:
                        _target = min(_target, _mx)
                    ds.scroll_offset = (ds.scroll_offset[0], max(0.0, _target))
                    from src.lsd.gl_gui.notifications import notify
                    notify(f"scroll goto-local ds={ds.name} line={_line} "
                           f"li={_li} src_li={_src_li} "
                           f"sy={_sy0:.0f}->{ds.scroll_offset[1]:.0f} "
                           f"line_px={line_px} h={ds.height} max_y={_mx}",
                           tag="scroll")
                else:
                    # Same live-origin compensation as the caret-follow - a
                    # scroll write earlier in this path leaves origin_y stale.
                    _ty = (origin_y + (_origin_sy - ds.scroll_offset[1])
                           + _li * line_px)
                    _scroll_into_view(ds, _ty, _ty + line_px, center=_far)
                _uj_log(f"goto LOCAL scroll sy={_sy0:.0f}->{ds.scroll_offset[1]:.0f} "
                        f"far={_far} scroll_visible={getattr(ds, 'scroll_visible', None)} "
                        f"li={_li} src_li={_src_li}")
                Melty.text_focused_ds = ds
                Melty._text_focus_grant_frame = Melty.frame_count
                ds.text_cursor_blink_time = time.time()
                ds.invalidate()
                # Success flash on the target token - same yellow emphasis
                # (and same rect derivation) the cross-file jump gets from
                # draw_code_editor's consumption.
                _eli = _li                    # projected display line
                _cols = jump_emph_cols(_fold_full, _fpos, span=_tsp)

                def _local_jump_rect(ds=ds, li=_eli, cols=_cols):
                    lp = getattr(ds, '_diff_line_px', None) or 16
                    inset = getattr(ds, '_diff_top_inset', 0)
                    y0 = ds.abs_top + inset + li * lp - ds.scroll_offset[1]
                    if y0 < ds.abs_top - lp or y0 > ds.abs_top + (ds.height or 0):
                        return None
                    x0, x1 = ds.abs_left, ds.abs_left + (ds.width or 0)
                    cw = getattr(ds, '_diff_char_w', None)
                    ox = getattr(ds, '_diff_origin_x_off', None)
                    if cols is not None and cw and ox is not None:
                        x0 = max(x0, ds.abs_left + ox + cols[0] * cw - 3)
                        x1 = min(x1, ds.abs_left + ox + cols[1] * cw + 3)
                        if x1 <= x0:
                            return None
                    return (x0, y0 - 1, x1, y0 + lp + 1)

                Melty.emphasize(f"jump_line {ds.name}", _local_jump_rect)
                _uj_log(f"goto LOCAL line={_line} pos={_pos}")
                request_render()
                return
        # External jump: the picked site opens in another tab, so this buffer's
        # per-file draw_text would never run again - its latched picker window
        # would never see another closed=True call and stuck open (a classic
        # orphaned-popover leak). Force-close it before switching away.
        ds._uj_open = False
        _pop = Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None))
        if _pop is not None and not _pop.closed:
            _pop.closed = True
            if _pop._tile_id is not None:
                Melty.cache.invalidate_up(_pop._tile_id, force=True, bypass_clip=True)
        from src.lsd.gl_gui.view.core_views.new_core_view import _dd_close
        _dd_close(uj_state)
        request_render()
        _uj_log(f"goto EXTERNAL {getattr(ref.path, 'name', ref.path)}:"
                f"{getattr(ref, 'line', None)} token={token!r}")
        _open_usage_ref(ref, token=token,
                        editor_window=_enclosing_editor_window(ds))

    def _present_usage_targets(_us, _su, _targets, force_picker):
        """Land a resolved usage jump: one counterpart opens straight in
        IntelliJ style; several (or `force_picker`) open the usage-jump
        picker under the symbol at buffer index `_us`. Always True."""
        if len(_targets) > 1 or force_picker:
            _items, _tags, _code = _usage_ref_items(_targets)
            ds._uj_items = _items
            ds._uj_tags = _tags
            ds._uj_code = _code
            # ref -> symbol spelling, so a picker lands the caret ON the
            # symbol (see _goto_usage_ref).
            ds._uj_names = {t: getattr(_su, 'name', None)
                            for t in _targets}
            ds._uj_anchor = _us   # picker hangs under the symbol
            ds._uj_anchor_gutter = None
            ds._uj_index = 0
            ds._uj_open = True
            ds._uj_open_frame = Melty.frame_count
            uj_state._kbd_mode = True
            uj_state.cursor_path = (next(iter(_items)),)
            uj_state.open_path = ()
            # The picker window is LATCHED - its scroll_offset survives
            # a close, so a reopen would pop up mid-list with the row-0
            # cursor scrolled offscreen. Snap it down to the top.
            from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
            _dd_scroll_cursor_into_view(
                Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None)), 0)
            request_render()
            return True
        _goto_usage_ref(_targets[0],
                        token=getattr(_su, 'name', None))
        return True

    def _usage_recheck(pos):
        """The tree has no jump targets at `pos` — double-check against FRESH
        data before Ctrl+B flashes red, since the background usage graph can
        hold a symbol in a stale no-callers state. Recomputes usage data for
        just the caret's line (full cross-file caller walk), SYNCHRONOUSLY on
        the UI thread — deliberately, to get a feel for the real cost (timed
        into /tmp/uj_debug.log and stdout). Returns a display-coordinate span
        (start, end, SymbolUsage, at_def) for the symbol under the caret, or
        None when the fresh data agrees there's nothing to jump to."""
        _vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
        if _vpath is None:
            return None
        # Caret's display line -> full buffer line (folds splice the display
        # text) -> 1-based file line via the view's offset.
        _dl = text.count('\n', 0, pos)
        _fl = (_fold_d2b[_dl] if _fold_d2b is not None
               and _dl < len(_fold_d2b) else _dl)
        _file_line = _usage_off + _fl + 1
        _t0 = time.monotonic()
        from src.lsd.gl_gui.view.core_conversion.libcst_conversion import usage_data_for_line
        _su_map = usage_data_for_line(str(_vpath), _file_line)
        _ms = (time.monotonic() - _t0) * 1000
        _uj_log(f"recheck {ds.name!r} line={_file_line} "
                f"took {_ms:.1f}ms symbols={len(_su_map)}")
        print(f"[usage-recheck] {getattr(_vpath, 'name', _vpath)}:{_file_line} "
              f"took {_ms:.1f}ms ({len(_su_map)} symbols)")
        # Safety check: the fresh result should AGREE with what the graph
        # already holds for this line - a difference is a stuck-stale-graph
        # bug caught in the act, so diff them and dump a forensic block
        # (_USAGE_MISMATCH_LOG) with everything needed to debug it later.
        # Guarded: the check must never fail the jump it rides on.
        try:
            _old_map = _tree_usages_on_line(_usage_tree, _file_line)
            _new_map = {getattr(_s, 'name', _k) or _k: _s
                        for _k, _s in _su_map.items()
                        if not (isinstance(_k, str) and "\x1f" in _k)
                        and any(s[0] == _file_line
                                for s in (getattr(_s, 'sites', None) or ()))}
            _diffs = _diff_usage_maps(_old_map, _new_map, _file_line)
            if _diffs:
                from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
                    usage_graph_source)
                from src.lsd.gl_gui.view.core_views.pending_save import PendingSave
                _log_usage_mismatch(
                    _vpath, _file_line, _diffs, _old_map, _new_map,
                    ctx=dict(frame=Melty.frame_count,
                             editor=repr(ds.name),
                             recheck_ms=f"{_ms:.1f}",
                             usage_off=_usage_off,
                             view_start=getattr(jump_to, 'start', None),
                             view_lines=len(_line_starts(_fold_full)),
                             graph_source=usage_graph_source(
                                 str(_vpath), _file_line, _file_line + 1),
                             pending_gen=PendingSave.pending_gen_for(_vpath),
                             tree_syms_on_line=len(_old_map),
                             fresh_syms_on_line=len(_new_map)))
        except Exception as _ce:
            _uj_log(f"recheck consistency check RAISED "
                    f"{type(_ce).__name__}: {_ce}")
        if not _su_map:
            return None
        # Same per-site resolution as _collect_usage_spans, narrowed to the
        # caret's line: full-buffer coordinates first, fold remap after.
        import os
        try:
            _vreal = os.path.realpath(str(_vpath))
        except OSError:
            _vreal = None
        _spans = []
        for _key, _su in _su_map.items():
            _name = getattr(_su, 'name', _key) or _key
            _d = getattr(_su, 'definition', None)
            _dp = getattr(_d, 'path', None) if _d is not None else None
            try:
                _def_here = (_dp is not None and _vreal is not None
                             and os.path.realpath(str(_dp)) == _vreal)
            except OSError:
                _def_here = False
            _is_local = isinstance(_key, str) and "\x1f" in _key
            for (_ln, _col) in getattr(_su, 'sites', None) or ():
                if _ln != _file_line:
                    continue
                _sp = _site_span(_fold_full, _ln, _col, _name, _usage_off)
                if _sp is None:
                    continue
                _at_def = (_def_here and _ln == getattr(_d, 'line', None)
                           and (_col == getattr(_d, 'column', None)
                                if _is_local else True))
                _spans.append((_sp[0], _sp[1], _su, _at_def))
        if _fold_remap_spans is not None:
            _spans = _fold_remap_spans(tuple(_spans), 'uj_recheck')
        # Narrowest span containing pos wins (same rule as _try_usage_jump).
        _best = None
        for _sp in _spans:
            if _sp[0] <= pos < _sp[1] and (_best is None
                                           or _sp[1] - _sp[0] < _best[1] - _best[0]):
                _best = _sp
        return _best

    def _roster_ctrl_b(pos):
        """Ctrl+B via the symbol roster at DISPLAY index `pos`: maps the caret
        into full-buffer coordinates, asks roster_tints.ctrl_b_lookup, and
        maps the returned symbol span back through the fold remap. Returns
        (start, end, sym, at_def, targets) or None."""
        _vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
        if _vpath is None:
            return None
        try:
            from src.lsd.gl_gui.view.core_views.roster_tints import ctrl_b_lookup
            _dl = text.count('\n', 0, pos)
            _col = pos - (text.rfind('\n', 0, pos) + 1)
            _fl = (_fold_d2b[_dl] if _fold_d2b is not None
                   and _dl < len(_fold_d2b) else _dl)
            _full = _fold_full if _fold_segments else text
            _fls = _line_starts(_full)
            if not (0 <= _fl < len(_fls)):
                return None
            _bpos = _fls[_fl] + _col
            _r = ctrl_b_lookup(_full, _bpos, _vpath, _usage_off)
        except Exception as _e:
            _uj_log(f"roster ctrl+b RAISED {type(_e).__name__}: {_e}")
            return None
        if _r is None:
            _uj_log(f"roster ctrl+b: no symbol at pos={pos}")
            return None
        _s, _e, _sym, _at_def, _targets = _r
        if _fold_remap_spans is not None:
            _sp = _fold_remap_spans(((_s, _e, _sym, _at_def),), 'uj_roster')
            if _sp:
                _s, _e = _sp[0][0], _sp[0][1]
        _uj_log(f"roster ctrl+b: {getattr(_sym, 'name', '?')} at_def={_at_def} "
                f"targets={len(_targets)}")
        return _s, _e, _sym, _at_def, _targets

    def _try_usage_jump(pos, force_picker=False):
        """Usage jump at buffer index `pos` (Ctrl+B): one counterpart opens
        straight in IntelliJ; several open the usage-jump picker under the
        symbol. `force_picker` opens the picker even for a SINGLE counterpart
        instead of jumping straight. When the tree yields no targets at `pos`
        (no span, or a span whose symbol shows no users), a synchronous
        single-line recheck (_usage_recheck) gets one more chance before
        False is returned and the caller flashes red. With
        Toggles.TextEditor.SymbolUsages.ctrl_b_always_recheck the recheck
        runs FIRST on every press and its fresh result wins; the background
        graph is only the fallback when it resolves nothing under the
        caret."""
        _vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
        _view_span = (_usage_off + 1, _usage_off + len(_line_starts(_fold_full)))
        _rechecked = False
        # Try roster first (Toggles.TextEditor.SymbolUsages.ctrl_b_roster):
        # textual resolution over pending/live spans - a usage jumps to its
        # definition, a definition lists its usages (trigram index +
        # resolve-back). Falls back to the graph/recheck below when the
        # roster can't place the caret's symbol or finds no targets.
        if Toggles.TextEditor.SymbolUsages.ctrl_b_roster and _vpath is not None:
            _rr = _roster_ctrl_b(pos)
            if _rr is not None and _rr[4]:
                _rs, _re_, _rsym, _rat_def, _rtargets = _rr
                return _present_usage_targets(_rs, _rsym, _rtargets, force_picker)
        if Toggles.TextEditor.SymbolUsages.ctrl_b_always_recheck:
            _rechecked = True
            _fresh = _usage_recheck(pos)
            if _fresh is not None:
                _us, _ue, _su, _at_def = _fresh
                _targets = _usage_jump_targets(_su, at_def=_at_def,
                                               view_path=_vpath,
                                               view_span=_view_span)
                if _targets:
                    return _present_usage_targets(_us, _su, _targets,
                                                  force_picker)
        _n_spans = 0
        # NARROWEST span containing pos wins, not the first: a dotted member
        # span (`GlobalStyle.get_global_constant`, anchored at the start of
        # the dotted expression) fully covers the same base-name span, so a
        # caret inside `GlobalStyle` used to hit whichever came first in the
        # sort and jump TO the METHOD. Specific-over-general resolves this:
        # caret in the base chars → the base symbol; caret in the member chars
        # → only the dotted span contains it.
        _best = None
        for _sp in _view_usage_spans(_vpath):
            _n_spans += 1
            if _sp[0] <= pos < _sp[1] and (_best is None
                                           or _sp[1] - _sp[0] < _best[1] - _best[0]):
                _best = _sp
        if _best is not None:
            _us, _ue, _su, _at_def = _best
            _targets = _usage_jump_targets(_su, at_def=_at_def,
                                           view_path=_vpath,
                                           view_span=_view_span)
            if _targets:
                return _present_usage_targets(_us, _su, _targets, force_picker)
            _uj_log(f"try_jump zero targets pos={pos} "
                    f"sym={getattr(_su, 'name', None)!r} — rechecking")
        else:
            _uj_log(f"try_jump MISS pos={pos} spans_scanned={_n_spans} "
                    f"vpath={getattr(_vpath, 'name', _vpath)} — rechecking")
        if not _rechecked:
            _fresh = _usage_recheck(pos)
            if _fresh is not None:
                _us, _ue, _su, _at_def = _fresh
                _targets = _usage_jump_targets(_su, at_def=_at_def,
                                               view_path=_vpath,
                                               view_span=_view_span)
                if _targets:
                    return _present_usage_targets(_us, _su, _targets,
                                                  force_picker)
        return False

    def _line_usage_picker(line):
        """Gutter heat-box click: the usage-jump picker for ALL usage spans
        on `line`, as ONE flat list — each row is "symbol  scope" (symbol
        prefix only when several symbols share the line, capped at 2 dots),
        with the file:line tag on the right. ALWAYS the picker, even for a
        single target — a gutter click asks to SEE the users, not jump.
        True if it opened (a line with no jump targets returns False)."""
        _vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
        _lss = _line_starts(text)
        if not (0 <= line < len(_lss)):
            return False
        _l0 = _lss[line]
        _l1 = _lss[line + 1] if line + 1 < len(_lss) else len(text) + 1
        _vspan = (_usage_off + 1, _usage_off + len(_line_starts(_fold_full)))
        _groups, _seen, _anchor, _names = {}, set(), None, {}
        for _us, _ue, _su, _at_def in _view_usage_spans(_vpath):
            if _us < _l0:
                continue
            if _us >= _l1:
                break
            if _anchor is None:
                _anchor = _us
            # The symbol's label is its text in the buffer - repeated
            # occurrences on the line collapse into one group, and their
            # jump destinations dedupe per group.
            _sym = text[_us:_ue] or getattr(_su, 'name', '?')
            for _t in _usage_jump_targets(_su, at_def=_at_def,
                                          view_path=_vpath, view_span=_vspan):
                _k = (_sym, str(getattr(_t, 'path', None)),
                      getattr(_t, 'line', None))
                if _k not in _seen:
                    _seen.add(_k)
                    _groups.setdefault(_sym, []).append(_t)
                    _names[_t] = getattr(_su, 'name', None) or _sym
        if not _groups:
            return False
        # ONE flat level - nested {symbol: {scope: ref}} submenus were fudly
        # (extra click, submenu-window latching) - each row is
        # "symbol  scope" with the symbol capped at 2 dots.
        _items, _tags, _code = {}, {}, {}
        for _sym, _refs in _groups.items():
            _pref = (f"{_shorten_dotted(_sym)}   "
                     if len(_groups) > 1 else "")
            _sub, _sub_tags, _sub_code = _usage_ref_items(_refs, prefix=_pref)
            _items.update(_sub)
            _tags.update(_sub_tags)
            _code.update(_sub_code)
        ds._uj_items = _items
        ds._uj_tags = _tags
        ds._uj_code = _code
        ds._uj_names = _names
        ds._uj_anchor = _anchor           # fallback if the gutter hides
        ds._uj_anchor_gutter = line       # picker docks beside the heat box
        ds._uj_index = 0
        ds._uj_open = True
        ds._uj_open_frame = Melty.frame_count
        uj_state._kbd_mode = True
        uj_state.open_path = ()
        uj_state.cursor_path = (next(iter(_items)),)
        _uj_log(f"gutter OPEN line={line} rows={len(_items)}")
        # Same latched-scroll snap as _try_usage_jump.
        from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
        _dd_scroll_cursor_into_view(
            Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None)), 0)
        # The picker only shows while its editor owns text focus. The gutter
        # press never reaches the caret/focus path (the event is claimed), so
        # grant it here, same as _goto_usage_ref's local jump.
        Melty.text_focused_ds = ds
        Melty._text_focus_grant_frame = Melty.frame_count
        request_render()
        return True

    # A press inside a PLAIN owns_mouse token widget (number drag - rects
    # recorded by last body run's token loop) belongs to the widget, not the
    # text: skip caret/focus/selection for the whole gesture, matching what the
    # old render_func widget's click latch did. The flag is re-evaluated on
    # EVERY press (no stale latch), and cleared on release below. Caret
    # placement for a clean click still happens via the _try_click raw-mouse
    # path in the token loop.
    # A press on the jump bar's flat close button (rect stashed by
    # set_jump_to) belongs to the button: its click resolves via on_action;
    # the press must not place the caret in the text under the floating bar
    # (the old @render_func button's own draw_state used to claim it).
    if left_mouse_down:
        _jb = getattr(ds, "_jump_btn_rect", None)
        if (_jb is not None
                and _jb[0] <= left_mouse_down.x < _jb[2]
                and _jb[1] <= left_mouse_down.y < _jb[3]):
            left_mouse_down = None
    if left_mouse_down:
        ds._plain_tv_gesture = any(
            _r[0] <= left_mouse_down.x < _r[2] and _r[1] <= left_mouse_down.y < _r[3]
            for _r in getattr(ds, '_plain_tv_rects', ()))
        if ds._plain_tv_gesture:
            left_mouse_down = None

    # Live-marker gutter button press: claim it from the DELIVERED event, not
    # a raw imgui click read - the press that wakes a blit-cached tile is
    # already a frame old when the body runs, so is_mouse_clicked in the
    # gutter pass almost never saw it. The stash is consumed by the gutter
    # pass below (same body run); nulling the event here also keeps the button
    # click from placing the caret / granting text focus.
    if (left_mouse_down and _lv_btn_w
            and left <= left_mouse_down.x < left + _lv_btn_w):
        _lv_pressed_line = int((left_mouse_down.y - origin_y) // line_px)
        if _lv_pressed_line in (getattr(ds, "_lv_gutter_markers", None) or {}):
            ds._lv_btn_pressed_line = _lv_pressed_line
            left_mouse_down = None

    # Usage-heat gutter click: a press in the number column on a line with
    # usage spans opens the usage-jump picker for that line (see
    # _line_usage_picker - always the picker, single-ref included). A line
    # with no jump targets falls through to the normal click path, so plain
    # gutter clicks still place the caret at line start.
    if (left_mouse_down and gutter_w
            and Toggles.TextEditor.usage_heat_gutter
            and left + _lv_btn_w <= left_mouse_down.x < left + gutter_w
            and not any(_br[0] <= left_mouse_down.x < _br[2]
                        and _br[1] <= left_mouse_down.y < _br[3]
                        for _br, _ in (getattr(ds, '_fold_badge_rects', None)
                                       or []))):
        # (fold-arrow presses are excluded above - the toggle handler at the
        # top of the body owns those; without this a click on the arrow of a
        # heat-carrying header ALSO opens the usage picker)
        _uh_line = int((left_mouse_down.y - origin_y) // line_px)
        if _line_usage_picker(_uh_line):
            left_mouse_down = None

    if left_mouse_down:
        if Toggles.TextEditor.text_focus_stack_trace and Melty.text_focused_ds is not ds:
            print(f"[focus-grant] click -> {ds.name} ({ds._tile_id})")
        Melty.text_focused_ds = ds
        Melty._text_focus_grant_frame = Melty.frame_count
        is_focused = True
        # A fresh click anywhere in the editor dismisses the usage-jump picker
        # (Ctrl+B re-opens it when it applies). Clicks on the picker itself
        # never land here - it's in its own window, so this hover-routed
        # event doesn't fire.
        ds._uj_open = False
        # ...and abandons any pending snippet tabstops: a placed caret means
        # the user left the fill-in flow, and a later edit must indent fresh.
        ds._ac_tabstops = None
        # ...and disarms the param hint: a caret placed by CLICK never shows it,
        # even inside the function it's armed for. It re-arms on the next edit
        # within parens, or Ctrl+P (see the signature-help block).
        ds._ac_sig_request_paren = -1
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
            # Double-click word-selects. (Usage jump lives on Ctrl+B - see the
            # standalone handler under the click block.)
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
    # Gestures that started inside a plain token widget never extend a text
    # selection - the drag drives the widget's value adjustment (see the press
    # handler above). Cleared on release in the token-view loop below.
    if left_mouse_drag and getattr(ds, '_plain_tv_gesture', False):
        left_mouse_drag = None
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
        # The latched drag keeps arriving even after the cursor leaves this
        # view, but this handler only runs on frames the (use_cache=True)
        # editor body actually re-renders - and once the cursor is off-view
        # the view's hover-driven per-frame invalidation stops, so the
        # selection/auto-scroll froze at the view edge. Keep the vie
        # re-rendering while the drag is held (similar pattern mirrors
        # draw_overlay_titlebar and the number-token drag sustain).
        ds.invalidate()
        request_render()

    # Ctrl+B - IntelliJ-style "go to declaration" at the CARET, no mouse
    # involved. (This flag used to be read only inside the click handler above,
    # so the shortcut silently required a simultaneous mouse press.) The event
    # is global-routed, so it reaches the editor under the pointer; gate on
    # focus so a stale caret in some other merely-hovered editor can't jump.
    if ctrl_b_down and not single_line and not is_search_box:
        _uj_log(f"ctrl_b {ds.name!r} focused={is_focused} "
                f"(focus_owner={getattr(Melty.text_focused_ds, 'name', None)!r}) "
                f"uj_open={getattr(ds, '_uj_open', False)} "
                f"caret={ds.text_cursor_pos} text_len={len(text)}")
    # Ctrl+Shift+B - open the CARET's line in the external editor (IntelliJ).
    # Same routing/focus gates as Ctrl+B; the display line maps through the
    # fold remap to the full buffer, plus the view's file offset gives the
    # 1-based source line. The launch runs off the render thread - the GUI
    # command blocks until the running instance answers.
    if (ctrl_shift_b_down and is_focused and not single_line
            and not is_search_box):
        _xp = getattr(jump_to, 'path', None) if jump_to is not None else None
        if _xp is not None:
            _xpos = min(ds.text_cursor_pos, max(len(text) - 1, 0))
            _xdl = text.count('\n', 0, _xpos)
            _xfl = (_fold_d2b[_xdl] if _fold_d2b is not None
                    and _xdl < len(_fold_d2b) else _xdl)
            _xline = _usage_off + _xfl + 1
            import threading
            from src.lsd.gl_gui.utils.jump_to_code import open_in_intellij
            threading.Thread(target=open_in_intellij, args=(str(_xp), _xline),
                             daemon=True, name="open_in_intellij").start()
        else:
            from src.lsd.gl_gui.notifications import notify
            notify("No file path for this buffer — can't open it externally.",
                   tint=(1.0, 0.65, 0.4, 1.0), tag="external_editor")

    if (ctrl_b_down and is_focused and not single_line and not is_search_box
            and not getattr(ds, '_uj_open', False)):
        _cb_pos = min(ds.text_cursor_pos, max(len(text) - 1, 0))
        if not _try_usage_jump(_cb_pos):
            # No jump target here - flash the word under the caret red so the
            # shortcut has answers instead of silently doing nothing.
            _w0 = _select_unit_left(text, _cb_pos)
            _w1 = _select_unit_right(text, _cb_pos)
            _vc = _get_vcols()
            _fx0, _fy0 = _char_pos_to_xy(text, _w0, origin_x, origin_y,
                                         line_px, vcols=_vc)
            _fx1, _ = _char_pos_to_xy(text, max(_w1, _w0 + 1), origin_x,
                                      origin_y, line_px, vcols=_vc)
            if _fx1 <= _fx0:   # word wrapped onto the next line - fall back
                _fx1 = _fx0 + imgui.calc_text_size(text[_w0:_w1] or " ").x
            Melty.emphasize(f"jump_fail {ds.name}",
                            (_fx0 - 3, _fy0 - 1, _fx1 + 3, _fy0 + line_px + 1),
                            tint=(0.9, 0.28, 0.22))
            request_render()

    _pf("mouse")
    # --- Keyboard handling ---
    if is_focused:
        shift = io.key_shift
        ctrl = io.key_ctrl

        # The caret can outlive the buffer it was placed in: a jump consume
        # (Ctrl+B) or a persisted draw_state stamps text_cursor_pos against
        # one text, and the content is then cut shorter underneath it
        # (buffer reload / merge adopt / external change). Every handler below
        # indexes text[cursor], so clamp ONCE here instead of per-site.
        if (ds.text_cursor_pos or 0) > len(text):
            ds.text_cursor_pos = len(text)
        if (getattr(ds, 'text_selection_start', 0) or 0) > len(text):
            ds.text_selection_start = len(text)
        if (getattr(ds, 'text_selection_end', 0) or 0) > len(text):
            ds.text_selection_end = len(text)

        # --- Code-suggestion popup: navigation & accept ---
        # Real editors don't suggest in the find box or inline single-line
        # value fields, so gate that out. (ac_state was set up at the top.)
        # Exception: a single-line box that has its own `completion_source`
        # (the context-aware Eval REPL) can autocomplete -- it drives candidates
        # off the live scope cache instead of the parsed code_tree.
        # `autocomplete=False` opts a field out entirely (text-code fields -
        # e.g. the params panel's string boxes render prose, not code) - an
        # explicit completion_source always wins, same as the single_line
        # exception (the Eval REPL asks for candidates on purpose).
        ac_enabled = (not is_search_box
                      and (not single_line or completion_source is not None)
                      and (autocomplete or completion_source is not None))
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
                # Keep the selection cursor visible: nudge the popup window to
                # scroll the minimal amount (no-op while the row is in view).
                from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
                _dd_scroll_cursor_into_view(
                    Melty.cache.key_to_draw_state.get(getattr(ds, '_ac_menu_tile', None)),
                    _ac_idx)
                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)) and _ac_cands and not ctrl:
                # Tab is deliberately NOT an accept key here - it belongs to the
                # FIM ghost text accept (fim.py). This popup accepts on Enter.
                chosen = _ac_cands[min(_ac_idx, len(_ac_cands) - 1)]
                anchor = getattr(ds, '_ac_anchor', ds.text_cursor_pos)
                # Replace the half-typed identifier [anchor, caret) with the
                # pick as Enter inserts, leaving the rest of the word under the
                # caret intact. (Tab is no longer a popup-accept key - it
                # drives the FIM ghost, so the old Tab-overtype is gone.)
                _replace_to = ds.text_cursor_pos

                _ins, _coff, _extra = _ac_pick_insert(ds, chosen,
                                                      following=text[_replace_to:_replace_to + 64],
                                                      preceding=text[max(0, anchor - 64):anchor],
                                                      replaced=text[anchor:_replace_to],
                                                      line_prefix=text[_get_line_start(text, anchor):anchor])
                text = text[:anchor] + _ins + text[_replace_to:]
                ds.text_cursor_pos = anchor + _coff
                # Remaining $N stops: END-relative so fill-in typing at an
                # earlier stop never shifts them (see the Tab-stop handler).
                ds._ac_tabstops = [len(text) - (anchor + s) for s in _extra] or None
                text = _ac_apply_auto_import(ds, chosen, jump_to, text)
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                ds.text_cursor_blink_time = time.time()
                ds._ac_open = False
                ds._ac_request_anchor = -1
                # Disarm the snippet site: the trigger char (if kept by the
                # overtype deducer, e.g. '(') still sits at its recorded spot
                # and the inserted text can match the trigger as a substring,
                # so the stale site would keep the popup alive. A FRESH trigger
                # ending at the new caret (a 0 landing right after '(') still
                # re-arms next frame - deliberate chaining.
                ds._ac_snip_site = None
                changed = True
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)

        # --- FIM ghost text: accept / dismiss (fim_state) --- reads LAST frame's
        # ghost (what the user is looking at). Runs after the suggestion popup's
        # handlers and before the indent / caret handlers, consuming its keys
        # the same way. Tab = the current chunk (Ctrl+Tab = everything
        # buffered), Ctrl+Right = one word. Esc = drop the buffer (not
        # dismiss - Esc keeps its old jobs). Tab drives the ghost even when
        # the completion popup is also open (the popup accepts on Enter).
        _fim_ghost_prev = getattr(ds, '_fim_ghost', None)
        if (fim_state is not None and _fim_ghost_prev is not None and _fim_ghost_prev.text
                and not is_search_box and not single_line):
            if pressed(glfw.KEY_ESCAPE):
                fim_state.dismiss()
                ds._fim_ghost = None
            else:
                _fim_mode = None
                if pressed(glfw.KEY_TAB) and not shift:
                    _fim_mode = "all" if ctrl else "chunk"
                elif pressed(glfw.KEY_RIGHT) and ctrl and not shift:
                    _fim_mode = "word"
                if _fim_mode is not None:
                    _ins = fim_state.accept(_fim_mode)
                    if _ins:
                        _pos = ds.text_cursor_pos
                        text = text[:_pos] + _ins + text[_pos:]
                        ds.text_cursor_pos = _pos + len(_ins)
                        ds.text_selection_start = ds.text_cursor_pos
                        ds.text_selection_end = ds.text_cursor_pos
                        ds.text_cursor_blink_time = time.time()
                        changed = True
                        _fired.discard(glfw.KEY_TAB)
                        _fired.discard(glfw.KEY_RIGHT)

        # --- Usage-jump picker: navigation & accept --- same key model as the
        # suggestion popup above: while open, Esc/arrows/Enter drive the picker
        # and are consumed before the caret handlers see them.
        if getattr(ds, '_uj_open', False):
            # Level-aware nav: the gutter-opened picker nests {symbol:
            # {scope: ref}}; the cursor's level is its path minus the last
            # key. Ctrl+B's flat picker resolves to prefix () and behaves
            # exactly as before.
            def _uj_resolve(_path):
                _v = getattr(ds, '_uj_items', None)
                for _pk in (_path or ()):
                    if not isinstance(_v, dict):
                        return None
                    _v = _v.get(_pk)
                return _v
            _uj_cp = (uj_state.cursor_path
                      if isinstance(uj_state.cursor_path, tuple) else ())
            _uj_prefix = _uj_cp[:-1]
            _uj_lvl = _uj_resolve(_uj_prefix)
            if not isinstance(_uj_lvl, dict) or not _uj_lvl:
                _uj_prefix, _uj_lvl = (), (getattr(ds, '_uj_items', None) or {})
            _uj_keys = list(_uj_lvl)
            _uj_idx = getattr(ds, '_uj_index', 0)
            if _uj_cp and _uj_cp[-1] in _uj_lvl:
                _uj_idx = _uj_keys.index(_uj_cp[-1])
            if pressed(glfw.KEY_ESCAPE):
                ds._uj_open = False
                _fired.discard(glfw.KEY_ESCAPE)
            elif (pressed(glfw.KEY_UP) or pressed(glfw.KEY_DOWN)) and _uj_keys:
                step = 1 if pressed(glfw.KEY_DOWN) else -1
                _uj_idx = (_uj_idx + step) % len(_uj_keys)
                ds._uj_index = _uj_idx
                uj_state._kbd_mode = True
                uj_state.cursor_path = _uj_prefix + (_uj_keys[_uj_idx],)
                # Same scroll-into-view as the suggestion popup's arrow keys
                # - root level only; submenu tiles are their own windows and
                # their lists are short.
                if not _uj_prefix:
                    from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
                    _dd_scroll_cursor_into_view(
                        Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None)),
                        _uj_idx)
                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)) and _uj_keys and not ctrl:
                _pick = _uj_resolve(
                    _uj_prefix + (_uj_keys[min(_uj_idx, len(_uj_keys) - 1)],))
                if isinstance(_pick, dict):
                    # Branch row (a symbol group): descend - expand it and
                    # put the cursor on its first ref.
                    uj_state.open_path = _uj_prefix + (_uj_keys[_uj_idx],)
                    if _pick:
                        uj_state.cursor_path = uj_state.open_path + (next(iter(_pick)),)
                        ds._uj_index = 0
                    request_render()
                elif _pick is not None:
                    _uj_log(f"pick ENTER ref={getattr(getattr(_pick, 'path', None), 'name', None)}:"
                            f"{getattr(_pick, 'line', None)}")
                    _goto_usage_ref(_pick, token=(getattr(ds, '_uj_names', None)
                                                  or {}).get(_pick))
                    ds._uj_open = False
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)

        # --- Import quick-fix (Alt+Enter) --- same key model as the popups
        # above. While the chooser is open, Esc/arrows/Enter drive it (keys
        # consumed before the caret handlers). Otherwise Alt+Enter with the
        # caret on a missing-import site applies the single fix directly, or
        # opens the chooser when several imports could bind the name.
        _alt = getattr(io, 'key_alt', False)
        _qf_enter = pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)
        if getattr(ds, '_qf_open', False):
            _qf_opts = getattr(ds, '_qf_options', None) or []
            _qf_idx = getattr(ds, '_qf_index', 0)
            if pressed(glfw.KEY_ESCAPE):
                ds._qf_open = False
                _fired.discard(glfw.KEY_ESCAPE)
            elif (pressed(glfw.KEY_UP) or pressed(glfw.KEY_DOWN)) and _qf_opts:
                step = 1 if pressed(glfw.KEY_DOWN) else -1
                _qf_idx = (_qf_idx + step) % len(_qf_opts)
                ds._qf_index = _qf_idx
                qf_state._kbd_mode = True
                qf_state.cursor_path = (_qf_opts[_qf_idx],)
                from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
                _dd_scroll_cursor_into_view(
                    Melty.cache.key_to_draw_state.get(getattr(ds, '_qf_menu_tile', None)),
                    _qf_idx)

                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif _qf_enter and _qf_opts and not ctrl:
                _stmt = _qf_opts[min(_qf_idx, len(_qf_opts) - 1)]
                _fx_changed, _fx_text = _apply_import_fix(_stmt, jump_to, text)
                from src.lsd.gl_gui.view.core_conversion.chain_converters import _import_bound_name
                ds._qf_applied.add(_import_bound_name(_stmt))
                if _fx_changed:
                    ds.text_cursor_pos += len(_fx_text) - len(text)
                    text = _fx_text
                    changed = True
                ds._qf_open = False
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)
                request_render()
        elif _alt and _qf_enter and not ctrl and not is_search_box:
            _caret_ln = text.count('\n', 0, ds.text_cursor_pos) + 1
            _qf_opts = _qf_fixes.get(_caret_ln) or []
            if len(_qf_opts) == 1:
                _fx_changed, _fx_text = _apply_import_fix(_qf_opts[0], jump_to, text)
                from src.lsd.gl_gui.view.core_conversion.chain_converters import _import_bound_name
                ds._qf_applied.add(_import_bound_name(_qf_opts[0]))
                if _fx_changed:
                    ds.text_cursor_pos += len(_fx_text) - len(text)
                    text = _fx_text
                    changed = True
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)
                request_render()
            elif len(_qf_opts) > 1:
                ds._qf_options = list(_qf_opts)
                ds._qf_index = 0
                ds._qf_anchor = ds.text_cursor_pos
                ds._qf_open = True
                qf_state._kbd_mode = True
                qf_state.cursor_path = (_qf_opts[0],)
                _fired.discard(glfw.KEY_ENTER)
                _fired.discard(glfw.KEY_KP_ENTER)
                request_render()
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
            # Both a typed dot (member access) and a plain identifier char are
            # popup triggers (IDE model) - the visibility block below decides
            # whether this site actually qualifies (comment/string/def-site gates).
            if ch == '.':
                typed_dot_this_frame = True
            elif ch.isalnum() or ch == '_':
                typed_word_char_this_frame = True
            changed = True




        # --- Snippet tabstops: Tab hops to the next $N of the last accepted
        # template. Stops are stored END-relative (len(text) - pos): fill-in
        # typing at an earlier stop shifts everything after the caret equally,
        # so a later stop's distance from the new END is invariant. Esc or
        # a click abandons the remaining stops (handlers elsewhere).
        if (pressed(glfw.KEY_ESCAPE) and getattr(ds, '_ac_tabstops', None)):
            ds._ac_tabstops = None      # not consumed - Esc has its other handlers
        if (pressed(glfw.KEY_TAB) and not ctrl and not shift
                and getattr(ds, '_ac_tabstops', None)):
            _endrel = ds._ac_tabstops.pop(0)
            if not ds._ac_tabstops:
                ds._ac_tabstops = None
            _pos = max(0, min(len(text), len(text) - _endrel))
            ds.text_cursor_pos = _pos
            ds.text_selection_start = _pos
            ds.text_selection_end = _pos
            ds.text_cursor_blink_time = time.time()
            _fired.discard(glfw.KEY_TAB)

        def _bracket_ctx(p):
            """(text, pos) for the bracket-cue queries below
            (_open_bracket_indent / _unclosed_opener): the FULL buffer with
            `p` mapped through the collapsed fold segments. On the display
            text a collapsed def whose signature spans lines ends its
            visible header in an unclosed '(' — the ')' lives in the hidden
            body — so every Enter/Tab below it read a phantom continuation
            cue and indented to the signature column. Falls back to the
            display text once this frame has already edited it (the segment
            anchors would be stale)."""
            if not _fold_segments or text is not original_input:
                return text, p
            return _fold_full, p + sum(len(_h) for _a, _h, *_ in _fold_segments
                                       if _a < p)

        # --- Tab / Shift+Tab ---
        # Never in a search box: indent is irrelevant there, and the global
        # search window uses Tab/Shift+Tab to switch result highlighted.
        if pressed(glfw.KEY_TAB) and not ctrl and not is_search_box:
            ds.text_cursor_blink_time = time.time()
            # Bracket-aware align (same ([{ cue as Enter): when adjusting a single
            # line's own indent (no selection, caret in the leading whitespace)
            # and the line is a bracket continuation, Tab pulls an under-indented
            # line UP to the cue and Shift+Tab pulls an over-indented line DOWN to
            # it - e.g. a stray `show_name=False,` snaps under the `@renderable(`.
            _ls = _get_line_start(text, ds.text_cursor_pos)
            _cur = _get_indent(text, _ls)
            _target = _open_bracket_indent(*_bracket_ctx(_ls))
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

        # --- Enter / Shift+Enter --- (skipped for single-line fields like the
        # search box, where Enter is reserved for find-next / Shift+Enter
        # find-prev).
        # Ctrl+Enter is reserved for recompile (general_go_to_address), so we
        # don't insert a newline when Ctrl is held.
        if (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)) and not single_line and not ctrl:
            ds.text_cursor_blink_time = time.time()
            if shift:
                # Shift+Enter: start a new line BELOW without splitting the
                # current one - the caret escapes trailing closers like `)]`
                # instead of dragging them along. Indent is computed at the
                # line END with the same bracket cue as plain Enter, so a
                # still-open ([{ on this line gets the scope indent and a
                # balanced line keeps its own indentation.
                eol = text.find('\n', ds.text_cursor_pos)
                pos = eol if eol != -1 else len(text)
                _bt, _bp = _bracket_ctx(pos)
                indent = _open_bracket_indent(_bt, _bp)
                if indent is None:
                    opener = _unclosed_opener(_bt, _get_line_start(_bt, _bp))
                    indent = _get_indent(_bt, opener) if opener is not None \
                        else _get_indent(_bt, _bp) + _block_open_extra(_bt, _bp)
                text = text[:pos] + '\n' + ' ' * indent + text[pos:]
                ds.text_cursor_pos = pos + 1 + indent
                ds.text_selection_start = ds.text_cursor_pos
                ds.text_selection_end = ds.text_cursor_pos
                changed = True
            else:
                if _has_selection(ds):
                    text, ds.text_cursor_pos = _delete_selection(text, ds)
                pos = ds.text_cursor_pos
                # Bracket-aware auto-indent. Inside an unclosed (, [, or { align to
                # that bracket's scope (everything past the opener, or a fixed indent
                # when nothing follows it) so multi-line signatures / lists / dicts
                # line up instead of snapping to the line's own indent. If the caret
                # is instead on a continuation line whose bracket already CLOSED on
                # this line, dedent back to the statement's opening-line indent
                # (e.g. after `...)` in a multi-line decorator → back to col 0).
                # Otherwise keep the current line's indentation.
                _bt, _bp = _bracket_ctx(pos)
                indent = _open_bracket_indent(_bt, _bp)
                if indent is None:
                    opener = _unclosed_opener(_bt, _get_line_start(_bt, _bp))
                    # Block opener (`def f():` / `class C:` / `if x:`) →
                    # one level deeper, see _block_open_extra.
                    indent = _get_indent(_bt, opener) if opener is not None \
                        else _get_indent(_bt, _bp) + _block_open_extra(_bt, _bp)
                # The remainder of the current line moves down to the new line. Strip
                # ITS leading spaces (only up to the line's end - never the next
                # line's indent) so they don't stack on top of the indent we insert.
                # Without this, whitespace sitting after the caret compounds on every
                # Enter: the new line ends up `indent + trailing` wide, the caret
                # sits mid-whitespace, and the next Enter measures that larger indent
                # - marching the caret ever rightward instead of keeping the line's
                # indentation.
                tail = pos
                line_end = text.find('\n', pos)
                stop = line_end if line_end != -1 else len(text)
                while tail < stop and text[tail] == ' ':
                    tail += 1
                # Splitting a comment mid-prose: the moved-down remainder would
                # land as bare whitespace and instantly re-lex as code. Continue the
                # comment instead - the new line re-opens with the '#' run (plus
                # its trailing space) at the '#'s own column, so a trailing
                # comment after code re-anchors under its '#' rather than the
                # statement's indent. Only when real content moves down
                # (tail < stop), Enter at a comment's end starts a fresh line.
                cont = ''
                _split = None
                if syntax_highlight and tail < stop:
                    _offs, _lopen = _ac_lex_state(ds, text)
                    # Splitting a single-quoted string literal: close it,
                    # reopen on the next line (implicit concatenation, parens
                    # added when not already bracketed) so the buffer stays
                    # valid Python - see _string_split.
                    if Toggles.TextEditor.enter_splits_strings:
                        _split = _string_split(text, pos, stop, _offs, _lopen)
                    if _split is None:
                        cc = _comment_continuation(text, pos, stop, _offs,
                                                   _lopen)
                        if cc is not None:
                            indent, cont = cc
                if _split is not None:
                    text, ds.text_cursor_pos = _split
                else:
                    text = text[:pos] + '\n' + ' ' * indent + cont + text[tail:]
                    ds.text_cursor_pos = pos + 1 + indent + len(cont)
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
        # Ctrl+Shift+Left/Right belongs to navigation undo/redo (the root
        # NavUndo hotkeys, which fire even while text is focused) - the editor
        # ignores the chord, giving up extend-selection-by-word for it.
        if pressed(glfw.KEY_LEFT) and not (ctrl and shift):
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
        if pressed(glfw.KEY_RIGHT) and not (ctrl and shift):
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
        # Search box: Up/Down belong to the results list (global search moves
        # its highlight) and the caret snapping to 0/len was pure noise.
        if pressed(glfw.KEY_UP) and not is_search_box:
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
        if pressed(glfw.KEY_DOWN) and not is_search_box:
            _dbg = getattr(Melty, '_ac_debug', None)
            if _dbg:
                _dbg[-1]['cursor_moved'] = True
            ds.text_cursor_blink_time = time.time()
            line, col = _index_to_line_col(text, ds.text_cursor_pos)
            total_lines = len(_line_starts(text)) - 1
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

        _pf("kbd:keys")
        # --- Code-suggestion popup: toggle visibility + rebuild candidates ---
        # Runs after every text-mutating key so the prefix reflects the final
        # buffer. Produces the list THIS frame's render draws and next frame's
        # nav reads. `_ac_anchor` is the span an accepted pick overwrites.
        if ac_enabled:
            prefix, anchor, dot_trigger = _completion_context(text, ds.text_cursor_pos)
            # Snippet shortcut site (Toggles.TextEditor.AC_SNIPPETS), opened by
            # typing a trigger like '#['. Computed before the suppress reset so
            # an Esc at the SNIPPET anchor isn't instantly forgotten (the
            # completion anchor is a different position).
            _snip = _snippet_context(ds, text, ds.text_cursor_pos, changed)
            sup = getattr(ds, '_ac_suppress_anchor', -1)
            if (sup != -1 and sup != anchor
                    and not (_snip is not None and sup == _snip[0])):
                ds._ac_suppress_anchor = sup = -1  # caret moved on; allow reopen
            if _snip is not None and sup == _snip[0]:
                _snip = None                       # Esc'd at the snippet site
            # IDE trigger model (IntelliJ-style): the popup opens as you TYPE -
            # a '.' (member access), an identifier char (scope completion), or
            # inside an import line (module completion) - or explicitly on
            # Ctrl+Space (Ctrl+P asks for the HO HINT instead - see the
            # signature-help block below). The trigger sets the completion site
            # (`_ac_request_anchor`); the popup stays up there — re-filtering as
            # the prefix grows/shrinks - until the caret leaves that site, Esc,
            # or an accepted pick. A bare caret move (e.g. clicking right after
            # an existing '.') never opens it. Typed triggers stay out inside
            # comments/strings (checked against the same incremental lexer
            # state the viewport tokenizer maintains) and right after a
            # name-DEFINING keyword (`def f`, `for x` - a name being referenced
            # has no members); an explicit ask bypasses both gates and also
            # overrides a prior Esc at this site, which typed triggers respect.
            # A `completion_source` box (the Eval REPL) is a one-line eval - no
            # comments, no parse tree - so every typed char re-triggers as-is.
            import_ctx = _import_line_context(text, anchor)
            typed_trigger = False
            if typed_dot_this_frame or typed_word_char_this_frame:
                if completion_source is not None:
                    typed_trigger = True
                elif syntax_highlight and (dot_trigger or import_ctx or prefix):
                    _offs, _lopen = _ac_lex_state(ds, text)
                    typed_trigger = (
                        not _pos_in_string_or_comment(
                            text, anchor - 1 if dot_trigger else anchor,
                            _offs, _lopen)
                        and (dot_trigger or import_ctx
                             or not _defining_keyword_before(text, anchor)))
            if ctrl and pressed(glfw.KEY_SPACE):
                ds._ac_request_anchor = anchor
                ds._ac_suppress_anchor = sup = -1  # explicit ask overrides a prior Esc
            elif typed_trigger and sup != anchor:
                ds._ac_request_anchor = anchor
            req = getattr(ds, '_ac_request_anchor', -1)
            if req != -1 and req != anchor:
                ds._ac_request_anchor = req = -1  # caret left the trigger site
            elif (req != -1 and changed and not prefix and not dot_trigger
                    and _snip is None and not (ctrl and pressed(glfw.KEY_SPACE))):
                # Deleted back to a blank prefix - the site is empty again, so
                # drop the popup instead of showing the unfiltered pool. Dot/
                # import sites keep their empty-prefix popups; typing (or
                # Ctrl+Space) re-triggers as usual.
                ds._ac_request_anchor = req = -1
            suppressed = sup != -1 and sup == anchor
            was_open = getattr(ds, '_ac_open', False)
            want = req != -1 and req == anchor and not suppressed
            # Popularity + tint ranking inputs for _filter_completions: usage-
            # site counts from the buffer's symbol graph (cached per parse
            # identity) and the tinted-name set (buffer map + the member-file
            # map, both already computed for the popup's row colors). Only
            # built while the popup is actually wanted.
            _ac_users = _usage_user_counts(ds, _usage_tree) if want else None
            _ac_tinted = None
            if want:
                _dtc = getattr(ds, '_def_tints', None)
                _ntc = _dtc[3] if _dtc is not None and len(_dtc) == 4 else None
                _mtc = getattr(ds, '_ac_member_tints', None)
                if _ntc or _mtc:
                    _ac_tinted = set(_ntc or ()) | set(_mtc or ())
            if _snip is not None and _snip[1]:
                # Word-like triggers ('t', 'in') are prefixes of real
                # identifiers. Once the typed filter matches NO snippet row
                # (label or insert text), the user is typing an identifier
                # ('token_views'), not asking for a snippet - disarm the site
                # so this same keystroke re-triggers scope completion instead
                # of the exclusive-but-empty snippet popup.
                _p = _snip[1].lower()
                if not any(_p in s.label.lower()
                           or _p in s.insert.replace("$0", "").lower()
                           for s in _snip[2]):
                    ds._ac_snip_site = None
                    _snip = None
            if _snip is None:
                ds._ac_snips = None    # accept must not treat identifiers as snippets
            ds._ac_import_stmts = None  # only the bare-identifier branch sets it
            if _snip is not None:
                # Snippet popup - only at its site: triggers like '#['
                # have no identifier completions, and the accept path replaces
                # the WHOLE snippet with the template. anchor/prefix are
                # remapped to the snippet site so the shared candidate-set
                # block below (and Esc suppression) work there.
                anchor, prefix = _snip[0], _snip[1]
                ds._ac_snips = {s.label: s for s in _snip[2]}
                # A snippet stays offered while the typed filter fits its
                # LABEL or its INSERT text — so typing the expansion itself
                # ('tint=(') keeps the row alive (accept replaces the whole
                # [anchor, caret) span, so nothing duplicates). Map order is
                # the ranking; these lists are hand-authored and tiny.
                _p = prefix.lower()
                cands = []
                for s in _snip[2]:
                    _ins_l = s.insert.replace("$0", "").lower()
                    # Fully typed = the filter is the TAIL of the insert:
                    # nothing left for accept to replace, so stop suggesting.
                    if ((_p in s.label.lower() or _p in _ins_l)
                            and not (_p and _ins_l.endswith(_p))):
                        cands.append((s.label, "snip"))
                ds._ac_member_tints = None
            elif want and completion_source is not None:
                # Eval REPL path - candidates come from the live scope cache
                # (FuncsMetadata), not the parsed code tree/jedi. Synchronous: the
                # source resolves member access via the recorded type's dict
                # (vs a live module's getattr) and bare names from the scope, both
                # with EXACT type tags. We still filter by the half-typed prefix.
                try:
                    raw = completion_source(text, anchor, prefix, dot_trigger) or []
                except Exception:
                    raw = []
                cands = _filter_completions(raw, prefix, users=_ac_users, tints=_ac_tinted)
                ds._ac_member_tints = None
            elif want and (dot_trigger or import_ctx):
                # Member access (`imgui.`, `foo.bar`) or an import line - the
                # live module namespace answers instantly when it can; otherwise
                # jedi resolves the receiver's REAL type / all importable
                # modules (async, off-thread, full-file context). Until that
                # lands, keep the list closed (scoped names aren't helpful) and
                # keep the body repainting so the future gets polled.
                members, pending = _ensure_member_completions(ds, text, anchor, jump_to)
                if members is not None:
                    cands = _filter_completions(members, prefix,
                                                users=_ac_users, tints=_ac_tinted)
                else:
                    cands = []   # jedi still resolving; its done-callback wakes us once
            elif want:
                # Bare identifier: scope-aware names from the parsed tree. The
                # POOL depends on the tree + the caret's line (its scope), NOT the
                # prefix, so cache it + only rebuild when those change. The body
                # re-runs every frame while the popup is open (keep-alive
                # invalidate); without this we'd re-walk the parse and build the
                # LineMap each frame just to filter by prefix.
                # The tree is _usage_tree, NOT raw code_tree: parsers can
                # deliver the parse as code_dict (code_tree=None) or shadow it
                # with the syntax-error marker - raw code_tree there dropped
                # every scope/position filter and the pool fell back to an
                # unfiltered whole-buffer list. _usage_tree already resolves
                # that. Its node spans are BUFFER-relative (only usage SITES
                # are file-absolute), so the buffer caret line is the right
                # coordinate to pass - no _usage_off here.
                _ac_line = _index_to_line_col(text, ds.text_cursor_pos)[0]
                _pool_key = (id(_usage_tree), _ac_line, len(text))
                if getattr(ds, '_ac_pool_key', None) != _pool_key:
                    _pool_func = _ac_live_context(ds, text, jump_to)[1]
                    ds._ac_pool = _completion_pool(_usage_tree, text, _ac_line, _pool_func)
                    ds._ac_pool_key = _pool_key
                cands = _filter_completions(ds._ac_pool, prefix,
                                            users=_ac_users, tints=_ac_tinted)
                # Import shortcuts: global classes/modules the buffer doesn't
                # know yet - accepting one also inserts the import statement.
                cands = _ac_import_rows(ds, cands, prefix, jump_to)
                ds._ac_member_tints = None   # scope names - member map would mislabel
            else:
                cands = []
            if cands:
                # cands is [(name, kind)]. Names drive nav/scroll/highlight; the
                # kind becomes a dim per-row tag (func/class/var/...) via kind_tags.
                names = [n for n, _ in cands]
                if prefix != getattr(ds, '_ac_prefix', None) or not was_open:
                    ds._ac_index = 0  # list changed shape, restart at the top match
                    # Snap the (latched) popup back to the top so the restarted
                    # selection is visible - the popup keeps its scroll_offset
                    # across reshapes and reopens otherwise.
                    from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
                    _dd_scroll_cursor_into_view(
                        Melty.cache.key_to_draw_state.get(getattr(ds, '_ac_menu_tile', None)), 0)
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
                if _snip is not None:
                    # Snippet labels preview their expansion dim (detail wins).
                    # Leading space: the suffix draws flush after the label
                    # (right for noparams, wrong for a label).
                    ds._ac_params = {s.label: " " + (s.detail or s.insert)
                                     for s in _snip[2]}
                else:
                    # Dim '(param, param2)' suffixes for callable rows —
                    # resolved live, memoized per callable in _SIG_CACHE.
                    ds._ac_params = _ac_param_suffixes(ds, text, cands, anchor,
                                                       dot_trigger, jump_to)
                ac_state.cursor_path = (names[ds._ac_index],)
                ac_state.open_path = ()
            else:
                ds._ac_open = False

        # --- Function call parameter hints (signature help) ---
        # Shown while EDITING a call's parameters, never on a bare caret move -
        # a click into existing parens stays quiet (the click handler above
        # disarms), and Ctrl+P asks explicitly. A buffer edit made with the
        # caret inside call parens arms the hint at that call - keyed on the
        # '(' index, so edits after it won't shift - and it stays up (the
        # active arg recomputed locally each frame, jedi re-queried only when
        # the callee changes) until the caret leaves that call's parens.
        # `_ac_sig_show` gates the render below. Skipped in the Eval REPL box
        # (completion_source): jedi can't see its runtime-typed locals, and we
        # don't want a subprocess completion job fired per keystroke in a
        # one-liner.
        _pf("kbd:ac")
        # --- FIM ghost text: reconcile the buffer against this frame's final
        # edit, schedule/continue requests, and stash what the render below
        # (and next frame's accept handler) should show.
        if (fim_state is not None and not is_search_box and not single_line
                and not is_diff and completion_source is None):
            # "typed" = the buffer changed by CONTENT typing. Backspace /
            # Delete / Tab / Enter edit the buffer too but never start a
            # generation (they only reconcile or abort a showing ghost).
            # Read from the raw frame keys - handlers discard consumed keys
            # from `_fired` (an accepted Tab is gone by now).
            _fim_typed = changed and not any(k in _FIM_NON_TRIGGER_KEYS for k, _m in _frame_keys)
            ds._fim_ghost = _fim_poll(ds, fim_state, text, jump_to, fim, typed=_fim_typed)
        else:
            ds._fim_ghost = None
        _pf("kbd:fim")
        if ac_enabled and completion_source is None:
            _open_paren, _arg_index = _call_context(text, ds.text_cursor_pos)
            _sig_req = getattr(ds, '_ac_sig_request_paren', -1)
            if _open_paren is None:
                _sig_req = -1
            elif changed or (ctrl and pressed(glfw.KEY_P)):
                _sig_req = _open_paren
            elif _sig_req != _open_paren:
                _sig_req = -1   # caret move into a call it wasn't armed for
            ds._ac_sig_request_paren = _sig_req
            if _sig_req != -1 and _ensure_signature_help(
                    ds, text, _open_paren, ds.text_cursor_pos, jump_to) is not None:
                ds._ac_sig_active = _arg_index
                ds._ac_sig_open_paren = _open_paren   # lets the hint align under the call name
                ds._ac_sig_show = True
            else:
                ds._ac_sig_show = False

    # Clamp
    ds.text_cursor_pos = max(0, min(ds.text_cursor_pos, len(text)))
    ds.text_selection_start = max(0, min(ds.text_selection_start, len(text)))
    ds.text_selection_end = max(0, min(ds.text_selection_end, len(text)))

    # Display edit splice (fold-collapsed edit frames only): with a collapsed
    # fold, tint/usage resolves against the frame-start FULL buffer and the
    # fold remap is built on the frame-start layout, so their output lags this
    # body run's edit by exactly the typed/deleted characters for one frame.
    # Diff the frame-start display text against the edited one so the washes
    # can be arithmetically shifted by the splice below.
    if _fold_segments and text is not original_input:
        _disp_sp = _display_edit_splice(original_input, text)

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
    # A fresh fast-path marker (see the block up top) is exempt from the stale
    # hide - it was computed against this very text - but still yields to the
    # popup suppression like every other marker.
    if ((getattr(ds, '_err_stale', False) and not _fast_fresh_err)
            or (is_focused and (getattr(ds, '_ac_open', False)
                                or getattr(ds, '_ac_sig_show', False)))):
        _err_markers = []
        _err_msg = None

    _pf("keyboard")
    # --- Find-in-text search ---
    # The term arrives either forwarded from an ancestor search owner (as a
    # SearchTerm carrying the shared cross-view session) or, when this editor
    # hosts the find UI itself, on ds.search_text with a session it pushed.
    # We search locally, register our matches to the session so every view
    # combines into one global set, and scroll to the global-current match
    # when it lands in this view.
    # The find UI's search input box (is_search_box) never matches: its text IS
    # the query, so the term an ancestor owner forwards down would highlight
    # every character typed into it as a hit.
    search_term = ("" if is_search_box
                   else search_text or (ds.search_text if ds.search_active else ""))
    # Match against the FULL buffer, not the fold display text: results
    # inside collapsed folds must be found and counted, and the fold section
    # already auto-expanded when it hides the CURRENT match. Cached
    # by (text identity, term) - shared with the fold section's lookup.
    # Late-bound on edit frames (same pattern as _view_usage_spans): with no
    # collapsed fold the full buffer IS `text`, the buffer the glyph pass
    # draws this frame - keyboard handling above reassigns it, while _fold_full
    # stays the frame-start buffer. Matching _fold_full left every highlight
    # after the caret off by the typed/deleted characters for the edit frame.
    # With a fold collapsed the match stays frame-start (_fold_off projects
    # against the frame-start layout) and the resulting display coords are
    # splice-shifted across this run's edit below.
    _match_base = _fold_full if _fold_segments else text
    _smc = getattr(ds, '_search_match_cache', None)
    if (_smc is not None and _smc[0] is _match_base
            and _smc[1] == str(search_term)):
        search_matches = _smc[2]
    else:
        search_matches = _find_matches(_match_base, search_term)
        ds._search_match_cache = (_match_base, str(search_term), search_matches)
    # Display-coord projection for highlight/scroll; a match still hidden
    # inside a collapsed fold projects to None and just isn't drawn. Slot
    # alignment with search_matches is kept (current_local indexes into it).
    if _fold_segments and search_matches:
        _sm_disp = []
        for _sm_s, _sm_e in search_matches:
            _p0 = _fold_off(_sm_s)
            if _p0 is None:
                _sm_disp.append(None)
            else:
                _p1 = _fold_off(_sm_e)
                _sm_disp.append((_p0, _p1 if _p1 is not None
                                 else _p0 + (_sm_e - _sm_s)))
        if _disp_sp is not None:
            # Edit frame with a fold collapsed: the projections above are
            # frame-start - shift them across this run's splice so the
            # highlights track the glyphs (same remap as the usage washes,
            # done by hand here because _display_splice_shift drops entries
            # and the None slots must survive for index alignment).
            _sp_p, _sp_oe, _sp_d = _disp_sp[0], _disp_sp[1], _disp_sp[2]
            for _i, _m in enumerate(_sm_disp):
                if _m is None:
                    continue
                if _m[0] >= _sp_oe:
                    _sm_disp[_i] = (_m[0] + _sp_d, _m[1] + _sp_d)
                elif _m[1] > _sp_p:
                    # Touches the edited region: the text under it changed -
                    # hide for this frame; next frame the rescan re-finds it.
                    _sm_disp[_i] = None
    else:
        _sm_disp = search_matches

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
        # Scroll to it on a full-search frame (term change / nav). An empty
        # search box never moves the view: no term means nothing to reveal.
        should_scroll = (bool(str(search_term))
                         and current_local is not None and session.scroll_to)
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
        # _match_base: the full buffer (hidden-in-fold matches count too),
        # post-shift when this body run edited it - see the match section above.
        ds._search_matcher = (
            lambda term, sess, _t=_match_base: sess.claim(len(_find_matches(_t, term))))
    else:
        ds._search_matcher = None

    if should_scroll and _sm_disp[current_local] is not None:
        ms, me = _sm_disp[current_local]
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
        # For a multi-line match only the first line drives the horizontal
        # scroll - _colx(me) on a later line would be meaningless here.
        _nl = text.find('\n', ms, me)
        match_x = _colx(ms)
        match_x_end = _colx(me if _nl == -1 else _nl)
        edge_padding = 20.0
        if text_visible_width > 0:
            if match_x_end <= text_visible_width - edge_padding:
                ds.text_h_scroll = 0.0
            else:
                # Pin the match's end to the right edge so we scroll the least
                # amount needed to reveal it, instead of dragging it to the left.
                ds.text_h_scroll = max(0.0, match_x_end - text_visible_width + edge_padding)
        request_render()
    _pf("find_search")


    # --- Horizontal auto-scroll ---
    # Only kicks in when the cursor moved this frame, so middle-drag pans
    # are not snapped back. Brings the cursor into view on a single line.
    visible_width = text_visible_width
    if (ds.text_cursor_pos != ds.text_prev_cursor_pos and visible_width > 0
            and not restore_active):
        cursor_logical_x = _colx(ds.text_cursor_pos)
        edge_padding = 20.0
        if cursor_logical_x - ds.text_h_scroll < edge_padding:
            ds.text_h_scroll = max(0.0, cursor_logical_x - edge_padding)
        elif cursor_logical_x - ds.text_h_scroll > visible_width - edge_padding:
            ds.text_h_scroll = cursor_logical_x - visible_width + edge_padding

    # --- Undo/redo landing ---
    # The wrapper tail stamped `_undo_landing` = (restored text, start, end,
    # frame) when Ctrl+Z / Ctrl+Shift+Z restored this view's value (see the
    # undo interception in pre_render). Once the restored buffer has flowed
    # back in as parent input, scroll the changed range into view (centered when
    # it sits off-screen; untouched when already visible) and flash it - the
    # same yellow emphasis a usage jump gets - so an undo of an edit made
    # far from the viewport is never a silent, invisible change. Consumed at
    # most once; dropped after a few frames if the restored text never shows
    # up (the parent rejected the write).
    _ul = getattr(ds, '_undo_landing', None)
    if _ul is not None and line_px and not is_search_box:
        _ul_text, _ul_a, _ul_b, _ul_frame = _ul
        _ul_stale = Melty.frame_count - _ul_frame > 8
        if _ul_stale:
            ds._undo_landing = None
        elif _fold_full == _ul_text:
            ds._undo_landing = None
            _n = len(_fold_full)
            _ua = min(max(_ul_a, 0), _n)
            _ub = min(max(_ul_b, _ua), _n)
            _uli0 = _fold_full.count('\n', 0, _ua)
            _uli1 = _fold_full.count('\n', 0, _ub)
            # Columns for a single-line change (multi-line, identical in full and
            # white space); a pure delete still gets a 1-cell marker.
            _ucols = None
            if _uli0 == _uli1:
                _uls = _fold_full.rfind('\n', 0, _ua) + 1
                _ucols = (_ua - _uls, max(_ub - _uls, _ua - _uls + 1))
            # Fold projection: expand any collapsed fold hiding the change and
            # map both ends into visible lines (see fold_project_jump).
            _, _uli0 = fold_project_jump(ds, _fold_full, _ua, _uli0)
            if _ub != _ua:
                _, _uli1 = fold_project_jump(ds, _fold_full, _ub, _uli1)
            else:
                _uli1 = _uli0
            # Live-origin compensation, same as the caret-follow below: an
            # earlier fold jump in this body leaves origin_y stale.
            _uty0 = (origin_y + (_origin_sy - ds.scroll_offset[1])
                     + _uli0 * line_px)
            _uty1 = (origin_y + (_origin_sy - ds.scroll_offset[1])
                     + (_uli1 + 1) * line_px)
            _scroll_into_view(ds, _uty0, min(_uty1, _uty0 + 6 * line_px),
                              center=True)

            def _undo_flash_rect(ds=ds, li0=_uli0, li1=_uli1, cols=_ucols):
                lp = getattr(ds, '_diff_line_px', None) or 16
                inset = getattr(ds, '_diff_top_inset', 0)
                y0 = ds.abs_top + inset + li0 * lp - ds.scroll_offset[1]
                y1 = ds.abs_top + inset + (li1 + 1) * lp - ds.scroll_offset[1]
                vt, vb = ds.abs_top, ds.abs_top + (ds.height or 0)
                if y1 < vt or y0 > vb:
                    return None
                x0, x1 = ds.abs_left, ds.abs_left + (ds.width or 0)
                cw = getattr(ds, '_diff_char_w', None)
                ox = getattr(ds, '_diff_origin_x_off', None)
                if cols is not None and cw and ox is not None:
                    x0 = max(x0, ds.abs_left + ox + cols[0] * cw - 3)
                    x1 = min(x1, ds.abs_left + ox + cols[1] * cw + 3)
                    if x1 <= x0:
                        return None
                return (x0, max(y0, vt) - 1, x1, min(y1, vb) + 1)

            Melty.emphasize(f"undo_landing {ds.name}", _undo_flash_rect)
            ds.invalidate()
            request_render()

    # --- Vertical auto-scroll ---
    # Vertical counterpart of the horizontal follow above: when the caret moves
    # to a line off the top/bottom of the viewport (typing past the last visible
    # line, wheeling/paging the cursor away, pasting a multi-line block), scroll
    # the editor - or its scroll container - so the caret's line comes back into
    # view. Same cursor-moved test so wheel/middle-drag pans that leave the caret
    # put are not snapped back. Anchors on origin_y and hands _scroll_into_view
    # the caret line's full vertical band exactly like the search scroll above.
    # Never for the find box: it's pinned to the host view's clip (bottom-left),
    # so _scroll_into_view walks up to the HOST editor's scroll container and
    # nudges it by the box's bottom-height overflow - and since the pin leaves
    # the box put, the same overflow reapplies every keystroke, creeping the
    # host view up a line per typed character.
    if (ds.text_cursor_pos != ds.text_prev_cursor_pos and line_px
            and not is_search_box and not restore_active):
        cursor_line, _ = _index_to_line_col(text, ds.text_cursor_pos)
        # LIVE origin, not the body-start origin_y: the Ctrl+B usage jump runs
        # EARLIER in this editor body (the picker jump is later, which is why
        # only Ctrl+B failed) - it moves the caret AND writes the centered
        # scroll_offset, so origin_y is stale by the jump's scroll time. The
        # follow then computed the caret ~100k px off-screen and slammed its
        # fresh scroll to the clamp (top of file): "jumps to some other line".
        cursor_top_abs = (origin_y + (_origin_sy - ds.scroll_offset[1])
                          + cursor_line * line_px)
        _scroll_into_view(ds, cursor_top_abs, cursor_top_abs + line_px)

    # --- Bring-to-front on caret/selection edits ---
    # Moving the caret or changing the selection in an editor whose window
    # sits behind another raises that window - the keyboard counterpart of
    # click-to-raise (which only fires on clicks). Gated on this editor
    # being text focus so programmatic caret writes into an unfocused
    # editor (external jumps, session restores) don't steal z-order, and on
    # the owning root window not already being front so plain typing in
    # the front editor won't queue a move every keystroke.
    _sel_now = (ds.text_selection_start, ds.text_selection_end)
    if ((ds.text_cursor_pos != ds.text_prev_cursor_pos
            or _sel_now != getattr(ds, '_prev_sel_state', _sel_now))
            and Melty.text_focused_ds is ds):
        _root, _n = ds, 0
        while (_root._tile_id not in Melty.registered_windows
               and _root.parent_window is not None
               and _root.parent_window is not _root and _n < 64):
            _root = _root.parent_window
            _n += 1
        if (_root._tile_id in Melty.registered_windows
                and next(reversed(Melty.registered_windows), None) != _root._tile_id):
            Melty.move_window_to_front(ds)
    ds._prev_sel_state = _sel_now

    ds.text_prev_cursor_pos = ds.text_cursor_pos

    # Clamp h_scroll to content bounds - the widest line drives the limit. Uses
    # plain character count (vcols now covers only the visible window, not the
    # whole buffer); inline widgets widen a line by a couple of cells, so the
    # h-scroll limit can be a hair short on widget-heavy lines - harmless.
    # Memoized by text IDENTITY: the buffer object is stable across
    # selection / caret / scroll frames, so the O(N) scan runs only
    # when the content actually changes.
    _mll = getattr(ds, '_max_line_len', None)
    if _mll is None or _mll[0] is not text:
        _mll = (text, max(map(len, text.split('\n')), default=0))
        ds._max_line_len = _mll
    max_line_width = _mll[1] * char_w
    max_h_scroll = max(0.0, max_line_width - visible_width + 50.0)
    ds.text_h_scroll = max(0.0, min(ds.text_h_scroll, max_h_scroll))
    origin_x = left + gutter_w + gutter_margin - ds.text_h_scroll

    _pf("autoscroll")
    # --- Drawing ---
    draw_list = imgui.get_window_draw_list()
    # Text content is clipped to start after the gutter, so highlights never
    # bleed under the line numbers when scrolled horizontally.
    rect_min_x = left + gutter_w + gutter_margin
    # Clip the text body to start below the floating jump-to bar so scrolled code
    # never appears over it (the bar is drawn above, before the body).
    rect_min_y = draw_state.abs_clip_rect[1] + bar_height
    rect_max_x = left + draw_state.content_width
    # content_width reserves up scroll_bar_width + margin for the scroll bar.
    # While a click-drag is in flight the wrapper clip removes that reserve,
    # so widen the body clip to match and let glyphs run under the bar.
    # freeze_resize panes never gave anything up: content_width already
    # caps the right edge and blit_offscreen draws the bar over it.
    if Melty.on_drag and not getattr(draw_state, "freeze_resize", False):
        rect_max_x += scroll_bar_width
    rect_max_y = draw_state.abs_clip_rect[3]

    # The text ROWS: from the gutter's right edge and the first drawn line's
    # top to the bottom of the last one (with edges once the text scrolls
    # past them). The selection subscription lives here and nowhere else - a
    # left_mouse_drag on the gutter or in the empty space above the first / below
    # the last line is left unsubscribed (event_rect), so it falls through to
    # the enclosing window's move handle and drags the WINDOW.
    # left_mouse_down remains view-wide: a press on the gutter still toggles
    # folds / opens the usage picker / places the caret, and a press below
    # the last line puts the caret at the end.
    text_rows_rect = (rect_min_x, max(rect_min_y, origin_y), rect_max_x,
                      min(rect_max_y, origin_y + len(_line_starts(text)) * line_px))
    draw_state.event_rect(("left_mouse_drag", "left_mouse_held"), text_rows_rect)

    # Native I-beam over the text rows (gutter, jump bar, scrollbar and the
    # window-drag space above the text keep the arrow). Cursor-only
    # subscription - no events - so it ignores the click subs' z-order /
    # blocker rules and sticks through a selection drag.
    # priority_delta=3 lands this AT the view's wrapper registration
    # (core_render registers event params at `priority - 3`): the blocker
    # pass keeps only entries at/above the enclosing closable window's own
    # `priority - 3`, and a delta-0 entry from a wrapper child sits below
    # that and would be pruned by its own window.
    draw_state.on_action([], view_id="text_cursor", rect=text_rows_rect,
                         priority_delta=3, cursor=mouse_cursor.TEXT)

    draw_list.push_clip_rect(rect_min_x, rect_min_y, rect_max_x, rect_max_y, True)

    # Definition tints (drawn FIRST, under everything): a block wash behind
    # every tinted class/def/etc in this buffer - top-left corner at the def
    # keyword's first character, bottom at the last line before the dedent,
    # right edge wrapping the block's widest line - plus a small wash behind every occurrence
    # of a symbol whose definition (here or in another file) carries a tint,
    # in that definition's color. Ties usages to their definitions at a glance.
    _dt_blocks = _dt_spans = _dt_lines = _dt_comments = ()
    # Tints off (or in search box): no block tint under any pill.
    if ds.__dict__.get("_lv_tint_blocks"):
        object.__setattr__(ds, "_lv_tint_blocks", ())
    # Open this body run's glow group: live cache lets glows from the last
    # run drop unless re-emitted below (so toggling tints off or scrolling the
    # bands away really clears them), while cache-skipped frames never reach
    # this point at all. HOLD the group (skip the clear) when tints are
    # ON but the code tree is transiently unreadable - _def_tints returns
    # empty from a non-dict tree BYPASSING its last-good machinery, and a
    # hover-coincident body run during initial parse churn would read
    # that as "tints removed" and drop the retained glow at random.
    _dt_on = Toggles.TextEditor.definition_tints and not is_search_box
    # Roster tints don't read the parse tree - no parse-churn hold needed.
    if not (_dt_on and not Toggles.TextEditor.roster_def_tints
            and not isinstance(_usage_tree, dict)):
        clear_glows(ds)
    if _dt_on:
        _t_dt = time.perf_counter()


        _k_dt = getattr(ds, "_def_tints_key", None)
        # Resolve target: the buffer whose glyph positions will draw THIS frame.
        # With no collapsed folds that is `text` - which key handling above
        # may have just edited - not _fold_full (frame-start buffer). The
        # anchor resolve re-finds displaced lines by name per call, but
        # locks onto the target it's handed; without it the pre-edit buffer
        # drew every wash/glow below an inserted newline one line off for
        # exactly the first frame (the snap-back flicker). With a collapsed
        # fold the display remap below is built on the frame-start layout,
        # so _fold_full stays the consistent (one-frame-stale) target.
        _dt_full = _fold_full if _fold_segments else text
        # Visible band in FULL-buffer lines for the roster's chunked view
        # (display lines via the fold map when a fold is collapsed).
        _dt_vis = None
        if Toggles.TextEditor.roster_def_tints and line_px:
            try:
                _clip = draw_state.abs_clip_rect
                _v0 = max(0, int((_clip[1] + bar_height - top) / line_px) - 3)
                _v1 = max(_v0, int((_clip[3] - top) / line_px) + 3)
                if _fold_d2b is not None and _fold_d2b:
                    _v0 = _fold_d2b[min(_v0, len(_fold_d2b) - 1)]
                    _v1 = _fold_d2b[min(_v1, len(_fold_d2b) - 1)]
                _dt_vis = (_v0, _v1)
            except Exception:
                _dt_vis = None
        # The file the buffer belongs to: the jump_to Address, else - only
        # for a world / detached pane, which never holds the buffer - the
        # wrapper's file_key memo.
        _dt_path = getattr(jump_to, 'path', None) if jump_to is not None else None
        if (_dt_path is None and (roster_world is not None or roster_table is not None)
                and isinstance(getattr(ds, '_file_meta', None), str)):
            _dt_path = ds._file_meta
        _pf("dt:pre")
        _dt_blocks, _dt_spans, _dt_lines, _ = _def_tints(
            ds, _dt_full, _usage_tree, _usage_off, _dt_path,
            vis=_dt_vis, hold_live=roster_live_hold, world=roster_world,
            table=roster_table)
        _pf("dt:def_tints")
        # Fold remap: def tints resolve against the FULL buffer (keeps the
        # last-good/anchor caches fold-independent); project the back into
        # display coords. Blocks whose head line is visible keep their wash,
        # with the extent clamped to the last visible line (a collapsed class
        # still washes its header row); entries living entirely on hidden
        # lines drop.
        if _fold_bl is not None:
            # Memoized like _fold_remap_spans (inputs + fold are held by
            # reference so id reuse can't alias): the remap itself is cheap,
            # but downstream memos (_b_lvls, _scope_surface) key on the
            # OUTPUT lists' identity, so they must be stable frame to frame.
            _dtm = getattr(ds, '_fold_dt_memo', None)
            if (_dtm is not None and _dtm[0] is _dt_blocks
                    and _dtm[1] is _dt_lines and _dtm[2] is _fold_built):
                _dt_blocks, _dt_lines = _dtm[3], _dtm[4]
            else:
                _rb = []
                for _b_ln, _b_ix, _b_end, _b_tt in _dt_blocks:
                    _dl = _fold_bl(_b_ln)
                    if _fold_d2b[_dl] != _b_ln:
                        continue
                    _dix = _fold_off(_b_ix)
                    if _dix is None:
                        continue
                    _rb.append((_dl, _dix, _fold_bl(_b_end), _b_tt))
                _rl = []
                for _l_ln, _l_rgb, _l_sc, _l_si, _l_ei in _dt_lines:
                    _dl = _fold_bl(_l_ln)
                    if _fold_d2b[_dl] != _l_ln:
                        continue
                    _dsi, _dei = _fold_off(_l_si), _fold_off(_l_ei)
                    if _dsi is None:
                        continue
                    _rl.append((_dl, _l_rgb, _l_sc, _dsi,
                                _dei if _dei is not None else _dsi + (_l_ei - _l_si)))
                ds._fold_dt_memo = (_dt_blocks, _dt_lines, _fold_built,
                                    tuple(_rb), tuple(_rl))
                _dt_blocks, _dt_lines = ds._fold_dt_memo[3], ds._fold_dt_memo[4]
            _dt_spans = _fold_remap_spans(_dt_spans, 'dt')
        # The DISPLAY-coordinate block list for the live-value pills
        # (see live_views._pill_tint: a pill wears the tint of the block
        # under its cursor). A tuple ref - the pill memo keys on its identity.
        object.__setattr__(ds, "_lv_tint_blocks", _dt_blocks)
        # Comment-text tints come from a direct scan of the buffer text - no
        # code_tree, no debounce, so a tint comment colors as it's typed
        # instead of waiting on the cst-dict round trip. Scans over the
        # FULL buffer (keeps the cache fold-independent, and a multi-line
        # override truncated at a fold seam wouldn't parse), then projected
        # into display coords: a collapsed run's visible header line keeps
        # its paint, clamped to that line so the color can't run past the
        # seam onto whatever follows the fold badge.
        _pf("dt:fold_remap")
        _dt_comments = _comment_tints(ds, _dt_full)
        _pf("dt:comment_tints")
        if _fold_bl is not None and _dt_comments:
            # Memoized like _fold_remap_spans (inputs + fold layout by
            # identity): the glyph pass's per-token surface memo keys on
            # the tuple's identity, and a fresh tuple every frame missed
            # it on every frame with a fold collapsed.
            _cm = getattr(ds, '_fold_comment_memo', None)
            if (_cm is not None and _cm[0] is _dt_comments
                    and _cm[1] is _fold_built):
                _dt_comments = _cm[2]
            else:
                _rc = []
                for _c_si, _c_ei, _c_rgb in _dt_comments:
                    _dsi = _fold_off(_c_si)
                    if _dsi is None:
                        continue
                    _dei = _fold_off(_c_ei)
                    if _dei is None:      # tail hidden - clamp to the header line
                        _dei = text.find('\n', _dsi)
                        if _dei == -1:
                            _dei = len(text)
                    _rc.append((_dsi, _dei, _c_rgb))
                ds._fold_comment_memo = (_dt_comments, _fold_built, tuple(_rc))
                _dt_comments = ds._fold_comment_memo[2]
        if _disp_sp is not None:
            # Edit frame with a fold collapsed: everything above (resolve +
            # fold remap) is frame-start; shift all four overlay families
            # across this frame's edit splice so the washes/glows track the
            # glyphs instead of lagging by the typed/deleted characters
            # (see _display_splice_shift).
            _dt_blocks, _dt_lines, _dt_spans, _dt_comments = (
                _display_splice_shift(_disp_sp, text, _dt_blocks, _dt_lines,
                                      _dt_spans, _dt_comments))
        _pf("dt:comment_remap+splice")
        _pf_info['dt_call_ms'] = round((time.perf_counter() - _t_dt) * 1000.0, 1)
        _pf_info['dt_miss'] = _k_dt is not getattr(ds, "_def_tints_key", None)
        _pf_info['dt_n'] = (len(_dt_blocks), len(_dt_lines), len(_dt_spans))
        # ALL def-tint washes paint on the UNDER-text channel (same idiom as
        # the cursor-token highlight below): translucent rects must never be
        # able to land over glyphs - the tile pipeline composites re-renders
        # over prior content, so transparent-over-text accumulates copies and
        # clouds the final color with tile count.
        # single_line micro-buffers (global search's code rows) stay on the
        # TEXT channel instead: their single tiles fully repaint on every
        # re-render (no partial-recomposite accumulation to guard against),
        # and the under channel loses to the enclosing window's composite
        # there so washes only showed while typing forced live re-renders. The
        # washes draw before the glyphs, so same-channel command order still
        # keeps them underneath.
        if Melty.channels_split:
            draw_list.channels_set_current(
                Core.melty.get_channel() - (0 if single_line else 1))
        # Per-row color adjustment (hsv shift and brightness clamp) - see
        # _bg_adjust. Blocks, line bands, and symbol washes each get their
        # own saturation/value pair; the brightness clamp is shared.
        _min_b = Toggles.TextEditor.bg_min_brightness
        _max_b = Toggles.TextEditor.bg_max_brightness
        _bg_f = (Toggles.TextEditor.bg_tint_saturation,
                 Toggles.TextEditor.bg_tint_value, _min_b, _max_b)
        _sym_f = (Toggles.TextEditor.symbol_tint_saturation,
                  Toggles.TextEditor.symbol_tint_value, _min_b, _max_b)
        _line_f = (Toggles.TextEditor.line_tint_saturation,
                   Toggles.TextEditor.line_tint_value, _min_b, _max_b)
        _dt_block_a = Toggles.TextEditor.def_block_alpha
        _dt_outline_a = Toggles.TextEditor.def_outline_alpha
        _dt_outline_t = Toggles.TextEditor.def_outline_thickness
        _dt_outline_b = Toggles.TextEditor.def_outline_brightness
        _dt_sym_ol_a = Toggles.TextEditor.def_symbol_outline_alpha
        _dt_sym_ol_t = Toggles.TextEditor.def_symbol_outline_thickness
        _dt_sym_ol_b = Toggles.TextEditor.def_symbol_outline_brightness
        # Compositor shadows under the washes (add_shadow depth marks; 0
        # disables). All marks clip to the visible text rect - partially
        # scrolled rows still draw here.
        _dt_block_sh = Toggles.TextEditor.def_block_shadow_offset
        _dt_sym_sh = Toggles.TextEditor.def_symbol_shadow_offset
        _sh_clip = (rect_min_x, rect_min_y, rect_max_x, rect_max_y)
        # Scope-aware depth: each block's shadow base is its NESTING level
        # (how many other blocks contain it) × the block offset, so a
        # method's wash sits above its class's, and the class above the
        # page surface. Per block the mark then PEELS: top corners at the
        # base (stuck flush to the enclosing scope - no shadow at the top
        # edge), bottom corners one step up, easing down (the peel).
        # Levels are also needed (shadow or not) to keep ROOT blocks apart
        # when root washes are off: the caller's show_root_backgrounds=False
        # (global-search embeds) is honoured only while
        # Toggles.TextEditor.root_symbol_tints is False.
        show_root_backgrounds = (show_root_backgrounds
                                 or Toggles.TextEditor.root_symbol_tints)
        _b_list = list(_dt_blocks) if (_dt_block_sh or not show_root_backgrounds) else []
        # O(blocks²), so memoized by the block tuple's identity (the ref in
        # the memo guards id change) - recomputing every frame was a real
        # render-thread cost on buffers with hundreds of defs.
        _blm = getattr(ds, '_dt_blvl_memo', None)
        _blm_key = (bool(_dt_block_sh), bool(show_root_backgrounds))
        if (_blm is not None and _blm[0] is _dt_blocks
                and _blm[1] == _blm_key):
            _b_lvls = _blm[2]
        else:
            _b_lvls = []
            for _l0, _i0, _e0, _t0 in _b_list:
                _b_lvls.append(sum(
                    1 for _l1, _i1, _e1, _t1 in _b_list
                    if (_l1 <= _l0 and _e0 <= _e1
                        and (_l1, _e1) != (_l0, _e0))))
            ds._dt_blvl_memo = (_dt_blocks, _blm_key, _b_lvls)

        # Per-line result cache for _scope_surface: each call scans every
        # block, and the wash/symbol-shadow passes call it per visible def
        # line per frame, it depends only on (block list, offset knob).
        _ssm = getattr(ds, '_dt_surf_memo', None)
        if (_ssm is None or _ssm[0] is not _dt_blocks
                or _ssm[1] != _dt_block_sh):
            _ssm = ds._dt_surf_memo = (_dt_blocks, _dt_block_sh, {})
        _surf_cache = _ssm[2]

        def _scope_surface(line):
            # Depth of the enclosing-t's surface at `line`: the innermost
            # containing block's base + its peel, interpolated with the same
            # smoothstep the gradient shader applies, so chips ride a
            # constant lift above the surface beneath them.
            best = _surf_cache.get(line)
            if best is not None:
                return best
            best, best_lvl = 0.0, -1
            for _sbi, (_l0, _i0, _e0, _t0) in enumerate(_b_list):
                if _l0 <= line <= _e0 and _b_lvls[_sbi] > best_lvl:
                    best_lvl = _b_lvls[_sbi]
                    t = (line - _l0) / max(1, _e0 - _l0)
                    t = t * t * (3.0 - 2.0 * t)
                    best = _dt_block_sh * (_b_lvls[_sbi] + t)
            _surf_cache[line] = best
            return best

        def _ol_rgb(c, b=None):
            # Outline color: the wash color pushed BRIGHTER than the bg
            # clamp allows - a 1-2px edge needs far more luminance than a
            # translucent fill to pop against the editor background.
            b = _dt_outline_b if b is None else b
            return (min(1.0, c[0] * b),
                    min(1.0, c[1] * b),
                    min(1.0, c[2] * b))
        # Per-line content lengths (rstripped chars) cached on the
        # draw_state per text identity: the block washes below wrap to the
        # widest line in their span instead of running to the view edge.
        _ll = getattr(ds, "_dt_line_lens", None)
        if _dt_blocks and (_ll is None
                           or getattr(ds, "_dt_line_lens_text", None) is not text):
            _ll = ds._dt_line_lens = [len(_l.rstrip()) for _l in text.split('\n')]
            ds._dt_line_lens_text = text
        _pf("w:pre_blocks")
        for _bi, (_b_line, _b_idx, _b_end, _b_tint) in enumerate(_dt_blocks):
            if not show_root_backgrounds and _b_lvls and _b_lvls[_bi] == 0:
                continue  # the embed paints non-symbol backgrounds itself
            sy = origin_y + _b_line * line_px
            ey = origin_y + (_b_end + 1) * line_px
            if ey < rect_min_y or sy > rect_max_y:
                continue
            sx = origin_x + _colx(_b_idx)
            # Wrap to the block's content: right edge at its widest line
            # plus a character of air - the block's TRUE extent, never
            # clamped to the view edge (a clamp put the rounded corners at
            # the clip instead of the content; the draw-list clip cuts an
            # overflowing wash with a sharp edge, as it should). Never
            # narrower than a stub when the span is blank/stale mid-scroll.
            _e0, _e1 = min(_b_line, len(_ll)), min(_b_end + 1, len(_ll))
            _bx1 = origin_x + (max(_ll[_e0:_e1] or (0,)) + 1) * char_w
            _bx1 = max(_bx1, sx + 2 * char_w)
            _b_rgb = _bg_adjust(tuple(_b_tint[:3]), _bg_f)
            _b_col = imgui.get_color_u32_rgba(_b_rgb[0], _b_rgb[1], _b_rgb[2], _dt_block_a)
            if _dt_block_sh:
                # The peel: top corners sit AT the enclosing scope's surface
                # (base = nesting level × block offset - flat, no shadow at
                # the top edge), bottom corners one step above it, depth
                # easing down the block.
                _base = _dt_block_sh * _b_lvls[_bi]
                _peel = _base + _dt_block_sh
                add_shadow((sx, sy, _bx1 - sx, ey - sy),
                           offset=(_base, _base, _peel, _peel),
                           corner_radius=4.0, clip=_sh_clip,
                           draw_state=ds)
            draw_list.add_rect_filled(sx, sy, _bx1, ey, _b_col, 4.0)
            if _dt_outline_a > 0:
                _b_ol = _ol_rgb(_b_rgb)
                draw_list.add_rect(sx, sy, _bx1, ey,
                                   imgui.get_color_u32_rgba(
                                       _b_ol[0], _b_ol[1], _b_ol[2],
                                       _dt_outline_a), 4.0,
                                   thickness=_dt_outline_t)
        # Line tint (the subtlest layer, over blocks, under the symbol
        # washes): one plain wash fitting the line's TEXT extent (indent →
        # last non-ws char), in the line's color - its explicit comment tint
        # if the definition has one, else a mix of its symbol tints.
        # Full-width (def_line_full_width) and feathered (def_line_blur +
        # def_line_blur_radius) variants are toggleable; blurred bands skip
        # the outline since a crisp outline would defeat the feather.
        _dt_line_a = Toggles.TextEditor.def_line_alpha
        if _dt_line_a > 0:
            _dt_line_full = Toggles.TextEditor.def_line_full_width
            _dt_line_blur = Toggles.TextEditor.def_line_blur
            _dt_line_blur_r = Toggles.TextEditor.def_line_blur_radius
            _dt_line_blur_a = Toggles.TextEditor.def_line_blur_alpha

            _dt_line_blur_k = Toggles.TextEditor.def_line_blur_falloff
            _dt_line_blur_n = Toggles.TextEditor.def_line_blur_samples
            _dt_line_blur_minv = Toggles.TextEditor.def_line_blur_min_value
            _dt_line_blur_maxv = Toggles.TextEditor.def_line_blur_max_value
            # GL glow path: the band goes through add_glow to the low-res
            # light buffer and onto the screen via the shadow composite as
            # a real light source (brightens neighbors, pushes back shadows)
            # - one quad instead of the draw-list feather stack below.
            _dt_line_glow = (Toggles.TextEditor.def_line_glow
                             and Toggles.glow and Toggles.filters
                             and not Toggles.draw_legacy)
            _dt_line_glow_i = Toggles.TextEditor.def_line_glow_intensity

            def _glow_rect(x0, y0, x1, y1, rgb, alpha, rounding, line):
                add_glow((x0, y0, x1 - x0, y1 - y0), rgb,
                         intensity=alpha * _dt_line_blur_a * _dt_line_glow_i,
                         radius=float(_dt_line_blur_r),
                         falloff=max(0.0, _dt_line_blur_k),
                         offset=_scope_surface(line),
                         corner_radius=rounding, clip=_sh_clip,
                         draw_state=ds)

            def _blur_rect(x0, y0, x1, y1, rgb, alpha, rounding):
                # Feathered band with an INVERSE-SQUARE profile - a hot
                # core that drops off fast, then a long faint tail, so the
                # band registers as emitted light rather than uniform fog.
                # Each expanding layer draws the DIFFERENCE in the profile
                # at its inner/outer radius, alpha, so cumulative
                # alpha at distance t from the edge is alpha * P(t), where
                # P(t) = inverse-square normalized to 1 at the edge and 0
                # at the blur radius. Cheap draw-list glow; no shader.
                alpha = min(1.0, alpha * _dt_line_blur_a)
                # Sample count from the setting, still capped by the radius
                # (more layers than pixels of radius is pure overdraw).
                steps = max(2, min(int(_dt_line_blur_n),
                                   int(_dt_line_blur_r) + 2))
                k = max(0.0, _dt_line_blur_k)
                floor = 1.0 / (1.0 + k) ** 2
                prev = 1.0
                for _i in range(steps):
                    t = (_i + 1) / steps
                    cur = ((1.0 / (1.0 + k * t) ** 2) - floor) / (1.0 - floor) \
                        if k > 0 else 1.0 - t
                    _c = imgui.get_color_u32_rgba(rgb[0], rgb[1], rgb[2],
                                                  alpha * (prev - cur))
                    prev = cur
                    e = _dt_line_blur_r * t
                    draw_list.add_rect_filled(x0 - e, y0 - e, x1 + e, y1 + e,
                                              _c, rounding + e)
            for _l_line, _l_rgb, _l_sc, _l_s, _l_e in _dt_lines:
                sy = origin_y + _l_line * line_px
                ey = sy + line_px
                if ey < rect_min_y or sy > rect_max_y:
                    continue
                _la = _bg_adjust(tuple(_l_rgb[:3]), _line_f)
                # The blurred band's own brightness clamp, on top of the
                # line_tint_* adjustment above - the feathered glow reads
                # differently from the hard rect at the same value.
                _lb = _brightness_clamp(_la[0], _la[1], _la[2],
                                        _dt_line_blur_minv, _dt_line_blur_maxv) \
                    if (_dt_line_blur and _dt_line_blur_r > 0) else _la
                _l_col = imgui.get_color_u32_rgba(_la[0], _la[1], _la[2],
                                                  _dt_line_a * _l_sc)
                if _dt_line_full:
                    if _dt_line_blur and _dt_line_blur_r > 0:
                        if _dt_line_glow:
                            _glow_rect(rect_min_x, sy, rect_max_x, ey,
                                       _lb, _dt_line_a * _l_sc, 0.0, _l_line)
                        else:
                            _blur_rect(rect_min_x, sy, rect_max_x, ey,
                                       _lb, _dt_line_a * _l_sc, 0.0)
                    else:
                        draw_list.add_rect_filled(rect_min_x, sy, rect_max_x,
                                                  ey, _l_col, 0.0)
                else:
                    sx = origin_x + _colx(_l_s)
                    ex = origin_x + _colx(_l_e)
                    if _dt_line_blur and _dt_line_blur_r > 0:
                        if _dt_line_glow:
                            _glow_rect(sx - 3, sy, ex + 3, ey,
                                       _lb, _dt_line_a * _l_sc, 3.0, _l_line)
                        else:
                            _blur_rect(sx - 3, sy, ex + 3, ey,
                                       _lb, _dt_line_a * _l_sc, 3.0)
                    elif _dt_outline_a > 0:
                        draw_list.add_rect_filled(sx - 3, sy, ex + 3, ey,
                                                  _l_col, 3.0)
                        _l_ol = _ol_rgb(_la)
                        draw_list.add_rect(sx - 3, sy, ex + 3, ey,
                                           imgui.get_color_u32_rgba(
                                               _l_ol[0], _l_ol[1], _l_ol[2],
                                               _dt_outline_a * _l_sc), 3.0,
                                           thickness=_dt_outline_t)
                    else:
                        draw_list.add_rect_filled(sx - 3, sy, ex + 3, ey,
                                                  _l_col, 3.0)
        _dt_sym_a = Toggles.TextEditor.def_symbol_alpha
        # GLow path for the TOKEN chips: each symbol-occurrence wash rect
        # gets its own light emitter (same pipeline as the line bands above),
        # so the light reads as reflecting off the individual token background's
        # surface rather than the whole line's.
        _dt_sym_glow = (Toggles.TextEditor.def_symbol_glow
                        and Toggles.glow and Toggles.filters
                        and not Toggles.draw_legacy)
        _dt_sym_glow_i = Toggles.TextEditor.def_symbol_glow_intensity
        _dt_sym_glow_r = Toggles.TextEditor.def_symbol_glow_radius

        for _s_start, _s_end, _s_tint, _s_scale in _dt_spans:
            _s_line, _ = _index_to_line_col(text, _s_start)
            sy = origin_y + _s_line * line_px
            ey = sy + line_px
            if ey < rect_min_y or sy > rect_max_y:
                continue
            sx = origin_x + _colx(_s_start)
            ex = origin_x + _colx(_s_end)
            # _s_scale < 1 indicates a PROPAGATED tint (reference flow) - same
            # color family, fainter wash per hop from the tinted definition.
            _sa = _bg_adjust(tuple(_s_tint[:3]), _sym_f)
            _s_col = imgui.get_color_u32_rgba(_sa[0], _sa[1], _sa[2],
                                              _dt_sym_a * _s_scale)
            if _dt_sym_sh:
                # Ride the scope surface: the wash's lift is the peeling
                # block surface at its line plus the symbol offset, so a
                # chip deep in a nested method casts off THAT wash, not
                # the entire background's flat depth.
                add_shadow((sx - 1, sy + 1, ex - sx + 2, ey - sy - 2),
                           offset=_scope_surface(_s_line) + _dt_sym_sh,
                           corner_radius=3.0, clip=_sh_clip,
                           draw_state=ds)
            if _dt_sym_glow:
                add_glow((sx - 1, sy + 1, ex - sx + 2, ey - sy - 2), _sa,
                         intensity=_dt_sym_a * _s_scale * _dt_sym_glow_i,
                         radius=float(_dt_sym_glow_r),
                         falloff=max(
                             0.0, Toggles.TextEditor.def_line_blur_falloff),
                         offset=_scope_surface(_s_line) + _dt_sym_sh,
                         corner_radius=3.0, clip=_sh_clip, draw_state=ds)
                
            draw_list.add_rect_filled(sx - 1, sy + 1, ex + 1, ey - 1, _s_col, 3.0)
            if _dt_sym_ol_a > 0:
                _s_ol = _ol_rgb(_sa, _dt_sym_ol_b)
                draw_list.add_rect(sx - 1, sy + 1, ex + 1, ey - 1,
                                   imgui.get_color_u32_rgba(
                                       _s_ol[0], _s_ol[1], _s_ol[2],
                                       _dt_sym_ol_a * _s_scale), 3.0,
                                   thickness=_dt_sym_ol_t)
        _pf("w:lines+spans")
        # Back to the body's text channel for everything after the washes.
        if Melty.channels_split:
            draw_list.channels_set_current(Core.melty.get_channel() + 1)
    _pf("w:channel")

    # Scope guides: a thin vertical line down the head column of every
    # indented block (IntelliJ-style), from the top of the block to
    # the block's last line. Colour is PROGRESSIVE: a tinted def/class
    # block's guide wears its definition tint, a guide with no tint of its
    # own wears its nearest enclosing tinted block's, and outside any
    # tinted block it wears the file's tint, else the neutral
    # Tint.scope_guide. All guide colours go through _bg_adjust
    # with the scope_guide_* hsv knobs. Segments are scanned once per
    # DISPLAY text identity (collapsed folds hide their body, so no guide
    # spans a fold seam); the per-frame work is the lineing only.
    if (Toggles.TextEditor.scope_guides and syntax_highlight
            and not single_line and not is_search_box and line_px):
        _sg_memo = getattr(ds, '_scope_guide_memo', None)
        if _sg_memo is None or _sg_memo[0] is not text:
            # The ROOT scope is one big guide at column 0 spanning the
            # whole file - head -1 so it starts on line 0, and it REPLACES
            # the column-0 block guides (a root-level def's own guide would
            # only retrace it).
            _sg_memo = ds._scope_guide_memo = (
                text, [(-1, text.count('\n'), 0)]
                + [_seg for _seg in _scope_guide_segments(text) if _seg[2] > 0])
        _sg_segments = _sg_memo[1]
        if _sg_segments:
            _sg_factors = (Toggles.TextEditor.scope_guide_saturation,
                           Toggles.TextEditor.scope_guide_value,
                           Toggles.TextEditor.scope_guide_min_value,
                           Toggles.TextEditor.scope_guide_max_value)
            _sg_alpha = Toggles.TextEditor.scope_guide_alpha
            _sg_thick = Toggles.TextEditor.scope_guide_thickness
            # Colour per segment, PROGRESSIVE and by the washes' own rule:
            # a segment wears the tint of its INNERMOST definition block
            # (_dt_blocks - the very rects painted above, decorator-run
            # start to body end) whose extent contains its head line; no
            # containing block, base (the file/neutral base). Resolved
            # once per (segments, definition tints) identity pair - one
            # merged sweep over the two sorted lists - never per frame.
            _sg_cmemo = getattr(ds, '_scope_guide_color_memo', None)
            _sg_blocks = _dt_blocks if _dt_on else ()
            if (_sg_cmemo is None or _sg_cmemo[0] is not _sg_segments
                    or _sg_cmemo[1] is not _sg_blocks):
                _sg_tints = _scope_guide_tints(_sg_segments, _sg_blocks)
                _sg_cmemo = ds._scope_guide_color_memo = (
                    _sg_segments, _sg_blocks, _sg_tints)
            _sg_tinted = _sg_cmemo[2]
            _sg_path = (getattr(jump_to, 'path', None) if jump_to is not None
                        else getattr(ds, '_file_meta', None))
            _sg_file_rgb = (_uj_file_tint(_sg_path) if _sg_path is not None
                            else None)
            _sg_base = tuple((_sg_file_rgb or Tint.scope_guide())[:3])
            _sg_base_col = None
            # The guide the caret sits ON - its line inside the block's span
            # AND its column equals the guide's column (this display focused) -
            # draws brighter: the scope_guide_active_* knobs replace alpha
            # and value for it. Merely being inside the block is not enough
            # (Lukas 08-28): the caret has to touch the line.
            _sg_active = None
            if (Melty.text_focused_ds is ds and ds.text_cursor_pos is not None):
                _sg_cpos = min(ds.text_cursor_pos, len(text))
                _sg_cline = bisect.bisect_right(_line_starts(text), _sg_cpos) - 1
                _sg_ccol = _sg_cpos - (text.rfind('\n', 0, _sg_cpos) + 1)
                for _sg_seg in _sg_segments:
                    if _sg_seg[0] > _sg_cline:
                        break
                    if (_sg_seg[0] < _sg_cline <= _sg_seg[1]
                            and _sg_seg[2] == _sg_ccol
                            and _sg_seg[0] >= 0):        # never the root guide
                        _sg_active = _sg_seg
                        break
            _sg_active_factors = (_sg_factors[0],
                                  Toggles.TextEditor.scope_guide_active_value,
                                  _sg_factors[2],
                                  Toggles.TextEditor.scope_guide_active_max_value)
            _sg_active_alpha = Toggles.TextEditor.scope_guide_active_alpha
            _sg_v0 = int((rect_min_y - origin_y) / line_px) - 1
            _sg_v1 = int((rect_max_y - origin_y) / line_px) + 1
            # Segments sorted by head line; the ones on screen are those whose
            # span meets the visible band.
            for _sg_head, _sg_end, _sg_colc in _sg_segments:
                if _sg_end < _sg_v0:
                    continue
                if _sg_head > _sg_v1:
                    break
                # Three pixels left of the column so the line never touches
                # the glyphs in it (+0.5 centres the 1 px stroke).
                _sg_x = origin_x + _sg_colc * char_w - 3.0 + 0.5
                # Scrolled under the gutter → hidden (the root line at
                # column 0 sits LEFT of the text inset, so minus that much).
                if _sg_x < rect_min_x - 3.0:
                    continue
                _sg_y0 = max(origin_y + (_sg_head + 1) * line_px, rect_min_y)
                # Bottom end pulled up 2 px so the line stops short of the
                # next row's glyphs.
                _sg_y1 = min(origin_y + (_sg_end + 1) * line_px - 2.0, rect_max_y)
                if _sg_y1 <= _sg_y0:
                    continue
                _sg_tint = _sg_tinted.get((_sg_head, _sg_end, _sg_colc))
                if (_sg_head, _sg_end, _sg_colc) == _sg_active:
                    _sg_rgb = _bg_adjust(
                        _sg_tint if _sg_tint is not None else _sg_base,
                        _sg_active_factors)
                    _sg_col = imgui.get_color_u32_rgba(
                        _sg_rgb[0], _sg_rgb[1], _sg_rgb[2], _sg_active_alpha)
                elif _sg_tint is not None:
                    _sg_rgb = _bg_adjust(_sg_tint, _sg_factors)
                    _sg_col = imgui.get_color_u32_rgba(
                        _sg_rgb[0], _sg_rgb[1], _sg_rgb[2], _sg_alpha)
                else:
                    if _sg_base_col is None:
                        _sg_rgb = _bg_adjust(_sg_base, _sg_factors)
                        _sg_base_col = imgui.get_color_u32_rgba(
                            _sg_rgb[0], _sg_rgb[1], _sg_rgb[2], _sg_alpha)
                    _sg_col = _sg_base_col
                if _sg_x < rect_min_x:
                    # The root line sits left of the body clip pushed at
                    # the top of the draw pass: pop it, push one left for
                    # the offset (intersected with the OUTER clip, so the
                    # tile bounds still hold), stroke, restore. Once per
                    # frame - only the root guide gets here.
                    draw_list.pop_clip_rect()
                    draw_list.push_clip_rect(rect_min_x - 3.0, rect_min_y,
                                             rect_max_x, rect_max_y, True)
                    draw_list.add_line(_sg_x, _sg_y0, _sg_x, _sg_y1,
                                       _sg_col, _sg_thick)
                    draw_list.pop_clip_rect()
                    draw_list.push_clip_rect(rect_min_x, rect_min_y,
                                             rect_max_x, rect_max_y, True)
                else:
                    draw_list.add_line(_sg_x, _sg_y0, _sg_x, _sg_y1,
                                       _sg_col, _sg_thick)

    _pf("body:washes")
    # Selection
    if _has_selection(ds):
        sel_color = (*Tint.text_selection()[:3], 0.4)
        lo, hi = _sel_range(ds)
        # Only the lines the selection touches AND the visible band - the
        # buffer-wide range+enumerate this replaced cost ~1.5ms a second on a
        # 12k-line file for every selection drag (memoized line starts →
        # two bisects, then a range over the visible rows).
        _starts = _line_starts(text)
        _n_lines = len(_starts)
        _first = max(bisect.bisect_right(_starts, lo) - 1,
                     int((rect_min_y - origin_y) // line_px) - 1, 0)
        _last = min(bisect.bisect_right(_starts, hi) - 1,
                    int((rect_max_y - origin_y) // line_px) + 1, _n_lines - 1)
        sel_u32 = imgui.get_color_u32_rgba(*sel_color)
        for line_idx in range(_first, _last + 1):
            line_abs_start = _starts[line_idx]
            line_abs_end = (_starts[line_idx + 1] - 1
                            if line_idx + 1 < _n_lines else len(text))
            sy = origin_y + line_idx * line_px
            if (line_abs_end >= lo and line_abs_start <= hi
                    and sy + line_px >= rect_min_y and sy <= rect_max_y):
                sel_start_in_line = max(0, lo - line_abs_start)
                sel_end_in_line = min(line_abs_end - line_abs_start,
                                      hi - line_abs_start)
                sx = origin_x + _colx(line_abs_start + sel_start_in_line, line_start=line_abs_start)
                ex = origin_x + _colx(line_abs_start + sel_end_in_line, line_start=line_abs_start)
                if hi > line_abs_end and line_abs_end >= lo:
                    # selection runs past the newline → extend one cell past EOL
                    ex = origin_x + _colx(line_abs_end, line_start=line_abs_start) + char_w
                draw_list.add_rect_filled(sx, sy, ex, sy + line_px, sel_u32)

    _pf("body:selection")
    # Token-occurrence highlight: when the caret rests on an identifier that
    # appears more than once, wash a subtle background behind every place that
    # exact token shows up - INCLUDING the one under the caret. A dumb,
    # identifier-bounded character match (see _word_match_ranges) - no CST /
    # symbol-usage index involved - so it works in any text, even mid-edit or
    # unparseable. A unique identifier (its own occurrence and no other) lights
    # nothing up. Drawn under the usage washes / search glow / glyphs.
    if (is_focused and not is_search_box and highlight_token_matches
            and Toggles.TextEditor.highlight_token_matches):
        _tok = _word_under_cursor(text, ds.text_cursor_pos)
        # Only IDENTIFIERS light up: keywords (`if`, `None`, `del`), number
        # literals and a caret inside a string / comment wash nothing, and a
        # token that sits inside a string or comment is dropped - the lexer
        # state comes from the same incremental line_open the tokenizer keeps,
        # so each check scans only its own line (see _line_lex_at).
        if _tok is not None and _is_highlightable_word(_tok[2]):
            _lo_offs, _lo_open = _ac_lex_state(ds, text)
            if _pos_in_string_or_comment(text, _tok[0], _lo_offs, _lo_open):
                _tok = None
        else:
            _tok = None
        if _tok is not None:
            _t_start, _t_end, _t_word = _tok
            _ranges = [r for r in _word_match_ranges(text, _t_word)
                       if not _pos_in_string_or_comment(text, r[0], _lo_offs, _lo_open)]
            # Only when the token recurs (its own occurrence plus at least one
            # other) - so the caret's own occurrence is washed too.
            if len(_ranges) > 1:
                _tm_color = imgui.get_color_u32_rgba(*Toggles.TextEditor.token_match_tint)
                for _ms, _me in _ranges:
                    _m_line, _ = _index_to_line_col(text, _ms)
                    sy = origin_y + _m_line * line_px
                    ey = sy + line_px
                  
                    # if ey < rect_min_y - 10.0 or sy > rect_max_y + 10.0:
                    #     continue
                        
                    sx = origin_x + _colx(_ms)
                    ex = origin_x + _colx(_me)
                    
                    # Just the click to highlight is disabled with a flag
                    if highlight_token_matches:
                        draw_list.add_rect_filled(sx - 1, sy + 1, ex + 1, ey - 1, _tm_color, 3.0)

    _pf("body:tok_match")
    # Symbol-usage heat, PER LINE: instead of washing each symbol occurrence
    # inline, the jump-target counts (_usage_target_count - the same list
    # _try_usage_jump would show) of every usage span on a line are SUMMED and
    # the total boxes that line's gutter number and the usage heat color (the
    # blue→orange ramp - more references on the line, hotter number). The
    # per-symbol breakdown deliberately collapses to a per-line summary; Ctrl+B
    # on a symbol still resolves per-span. Counts are gathered here (spans are
    # buffer-indexed) and drawn in the gutter pass below.
    _u_vpath = getattr(jump_to, 'path', None) if jump_to is not None else None
    _t_us = time.perf_counter()
    _uspans = _view_usage_spans(_u_vpath)
    _pf_info['us_call_ms'] = round((time.perf_counter() - _t_us) * 1000.0, 1)
    _pf_info['us_n'] = len(_uspans)
    _usage_line_heat = {}
    if _uspans and Toggles.TextEditor.usage_heat_gutter:
        _u_vspan = (_usage_off + 1, _usage_off + _fold_full.count('\n') + 1)
        # Visible band only: _uspans is sorted by start index
        # (_collect_usage_spans sorts), so bisect the on-screen character
        # range instead of walking every span in the file - the old loop
        # paid a _index_to_line_col per span before its own cull.
        _uls = _line_starts(text)
        _ul0 = max(0, min(len(_uls) - 1,
                          int((rect_min_y - origin_y) // line_px)))
        _ul1 = max(0, min(len(_uls) - 1,
                          int((rect_max_y - origin_y) // line_px) + 2))
        _ui0 = bisect.bisect_left(_uspans, (_uls[_ul0],))
        _ui1 = bisect.bisect_right(_uspans, (_uls[_ul1],))
        # Heat memo: the summed counts only change when the span SET or or
        # visible band moves - an idle repaint re-walked a few hundred spans
        # (bisect + memo-dict hits, but still 3-5ms of Python loop) for the
        # identical dict every frame. Memo on the span tuple's identity + band
        # indices; keeping original ref in the memo guards id reuse. A keystroke
        # replaces the tuple (no remap), so edits still recompute.
        _uh = getattr(ds, "_uh_memo", None)
        if (_uh is not None and _uh[1] is _uspans
                and _uh[0] == (id(_uspans), _ui0, _ui1)):
            _usage_line_heat = _uh[2]
        else:
            for _us, _ue, _su, _at_def in _uspans[_ui0:_ui1]:
                u_line, _ = _index_to_line_col(text, _us)
                sy = origin_y + u_line * line_px
                if sy + line_px < rect_min_y or sy > rect_max_y:
                    continue
                n = _usage_target_count(ds, _su, _at_def, _u_vpath, _u_vspan)
                if n:
                    _usage_line_heat[u_line] = _usage_line_heat.get(u_line, 0) + n
            ds._uh_memo = ((id(_uspans), _ui0, _ui1), _uspans, _usage_line_heat)
            _pf_info['uh_n'] = _ui1 - _ui0

    _pf("body:usage_heat")
    # Search match highlights (drawn under the text so glyphs stay readable).
    # The current match radiates a circular gradient glow with its rect cut out
    # so the matched text stays visible; the rest get a thin border. Look is
    # tunable via Toggles.SearchSettings (see search_glow.draw_search_highlight).
    if _sm_disp:
        for m_idx, _sm_m in enumerate(_sm_disp):
            if _sm_m is None:
                continue     # hidden inside a collapsed fold
            ms, me = _sm_m
            m_line, _ = _index_to_line_col(text, ms)
            # A match may span lines (multi-line search terms): collect one
            # rect per covered line, like the selection wash above. Segments
            # that run through a newline extend one cell past EOL to read as
            # continuing onto the next line. The whole match draws as one
            # glow around the segments' bounding box (per-line glows overlap
            # into a blob) with each segment outlined separately.
            segs = []
            seg_start = ms
            seg_line = m_line
            while True:
                nl = text.find('\n', seg_start, me)
                seg_end = me if nl == -1 else nl
                sy = origin_y + seg_line * line_px
                sx = origin_x + _colx(seg_start)
                ex = origin_x + _colx(seg_end)
                if nl != -1:
                    ex += char_w
                segs.append((sx, sy, ex, sy + line_px))
                if nl == -1:
                    break
                seg_start = nl + 1
                seg_line += 1
            if segs[-1][3] >= rect_min_y and segs[0][1] <= rect_max_y:
                draw_search_highlight_multi(draw_list, segs,
                                            current=(m_idx == current_local))

    _pf("body:search_hl")



    # Parse/semantic messages: each marker is a red button in the GUTTER over
    # its line number (see the gutter pass - `_err_by_line`), clamped to the
    # view's top / bottom with an arrow when the line is off screen. Only the
    # CLICKED marker (`ds._err_open_line`) washes its line red - under the
    # glyphs, with its message box beside it - so errors stay out of the way
    # while scrolling.
    _err_open_ln = getattr(ds, '_err_open_line', None)
    if _err_markers and _err_open_ln is not None:
        # [tint=(0.95, 0.25, 0.25)]
        error_line_wash = (0.824, 0.157, 0.157, 0.431)
        if any(err_line - 1 == _err_open_ln for err_line, _msg in _err_markers):
            ey0 = origin_y + _err_open_ln * line_px
            ey1 = ey0 + line_px
            if not (ey1 < rect_min_y or ey0 > rect_max_y):
                draw_list.add_rect_filled(origin_x - 4, ey0, origin_x + visible_width, ey1,
                                          imgui.get_color_u32_rgba(*error_line_wash))
    # Import quick-fix affordance: every symbol an import would bind wears a
    # translucent yellow underline, and the floating Alt+Enter hint appears at
    # the end of the line only when the mouse is over one of those underlined
    # symbols. Deliberately independent of the error markers (the suggestions
    # travel on their own lines, and the parser error may sit on a DIFFERENT
    # line than the half-typed `json.`). Alt+Enter still stays caret-driven -
    # a caret anywhere on the line applies the fix / opens the chooser (see
    # the keyboard block / the _qf draw_dd_menu below); hovering only reveals
    # the hint. No hover invalidation needed here: the wrapper auto-repaints
    # a bounding boxable tile every frame and once on the leave edge.
    if _qf_fixes:
        _po_hover_ln = None
        _ul_col = imgui.get_color_u32_rgba(0.92, 0.80, 0.18, 0.95)
        for _ul_ln in _qf_fixes:
            _ul_y = origin_y + (_ul_ln - 1) * line_px
            if _ul_y + line_px < rect_min_y or _ul_y > rect_max_y:
                continue
            _ul_names = _qf_names.get(_ul_ln)
            if not _ul_names:
                continue
            _ul_ls = 0
            for _ in range(_ul_ln - 1):
                _nl = text.find('\n', _ul_ls)
                if _nl == -1:
                    break
                _ul_ls = _nl + 1
            _ul_le = text.find('\n', _ul_ls)
            _ul_line_text = text[_ul_ls:] if _ul_le == -1 else text[_ul_ls:_ul_le]
            for _um in re.finditer(r'[A-Za-z_][A-Za-z0-9_]*', _ul_line_text):
                if _um.group(0) not in _ul_names:
                    continue
                # Attribute accesses (`foo.json`) never miss an import - only
                # base identifiers do (mirrors the suggestion scan's rule).
                if _ul_line_text[:_um.start()].rstrip().endswith('.'):
                    continue
                _ux0, _uy = _char_pos_to_xy(text, _ul_ls + _um.start(),
                                            origin_x, origin_y, line_px, vcols=vcols)
                _ux1, _ = _char_pos_to_xy(text, _ul_ls + _um.end(),
                                          origin_x, origin_y, line_px, vcols=vcols)
                draw_list.add_line(_ux0, _uy + line_px - 2, _ux1, _uy + line_px - 2,
                                   _ul_col, 2.0)
                if (_ux0 <= io.mouse_pos.x < _ux1
                        and _uy <= io.mouse_pos.y < _uy + line_px):
                    _po_hover_ln = _ul_ln
        if _po_hover_ln is not None and not getattr(ds, '_qf_open', False):
            _po_ln = _po_hover_ln
            _po_opts = _qf_fixes[_po_ln]
            _po_ls = 0
            for _ in range(_po_ln - 1):
                _po_ls = text.find('\n', _po_ls) + 1
            _po_le = text.find('\n', _po_ls)
            _po_line_text = text[_po_ls:] if _po_le == -1 else text[_po_ls:_po_le]
            _po_label = (f"Alt+Enter  {_po_opts[0]}" if len(_po_opts) == 1
                         else f"Alt+Enter  {len(_po_opts)} imports…")
            _po_w, _po_h = imgui.calc_text_size(_po_label)
            _po_x = origin_x + imgui.calc_text_size(_po_line_text).x + 28
            _po_y = origin_y + (_po_ln - 1) * line_px
            if rect_min_y <= _po_y <= rect_max_y:
                draw_list.add_rect_filled(
                    _po_x - 8, _po_y - 2, _po_x + _po_w + 8, _po_y + _po_h + 4,
                    imgui.get_color_u32_rgba(0.13, 0.16, 0.24, 0.96), 5.0)
                draw_list.add_rect(
                    _po_x - 8, _po_y - 2, _po_x + _po_w + 8, _po_y + _po_h + 4,
                    imgui.get_color_u32_rgba(0.45, 0.60, 0.90, 0.55), 5.0)
                draw_list.add_text(_po_x, _po_y,
                                   imgui.get_color_u32_rgba(0.72, 0.82, 1.0, 1.0),
                                   _po_label)

    # Side-by-side diff anchors: enough for an OUTSIDE ribbon pass (the code
    # editor's compare tile) to map this view's buffer lines onto live screen
    # coords without this body running. Both stored RELATIVE to the view's
    # pane corner - an absolute value goes stale the moment the window moves
    # while this body is a cached blit - so the overlay re-derives
    # origin_y = live abs_top + inset - scroll, origin_x = live abs_left +
    # offset. origin_x is the text start (right of the line-number gutter).
    ds._diff_top_inset = (origin_y + ds.scroll_offset[1]) - ds.abs_top
    ds._diff_line_px = line_px
    ds._diff_origin_x_off = origin_x - ds.abs_left
    ds._diff_char_w = char_w
    # Fold layout bridge: display line -> buffer line (None = identity, no
    # collapsed fold). The ribbon pass projects change buffer-line blocks
    # through this so a change hidden in a collapsed fold snaps its
    # swoosh to the collapse (header) line instead of a linear-extrapolated
    # position inside it.
    ds._diff_d2b = _fold_d2b
    # The text band clip (top inset, BOTTOM inset) relative to the pane
    # box - insets are height-stable, so the ribbon pass can project the
    # band onto the pane's LIVE height. Stashing the BOTTOM EDGE offset
    # froze it at the stash-time height: a freeze_resize pane mid
    # resize-drag serves its blit without re-running this body, and the
    # ribbons clamped to the pre-drag bottom until release. (The pane
    # corner itself is tracked live via ds._abs_left()/_abs_top() - see
    # _pane_pos in open_files.py.)
    ds._diff_clip_off = (rect_min_y - ds.abs_top,
                         (ds.abs_top + (ds.height or 0)) - rect_max_y)

    # Diff washes: in is_diff mode each line's leading marker (the +/- left over
    # from the unified diff, with the ---/+++/@@ headers already stripped by the
    # caller) drives a full-width background — added lines green, deleted lines
    # red, drawn under the glyphs so the code stays readable.
    if is_diff:
        add_bg = (0.157, 0.627, 0.157, 0.353)  # translucent green
        del_bg = (0.824, 0.235, 0.235, 0.353)  # translucent red
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
    _pf("body:err_diff")
    # Syntax-highlighted text - only the visible window is tokenized (see
    # `_window`), so this is O(visible) not O(buffer). The loop starts at the
    # window's first line and source offset; tokens above it (the merge-context
    # lookback) are processed but viewport-culled. Each token is drawn one
    # line-segment at a time with a single add_text call rather than per glyph.
    win_line, win_off, tokens, _ = _window()
    # Def-block tint per def line (display coords, like the token loop's
    # line counter) - the debug run buttons wear their function's tint.
    _fn_tint_lines = ({b[0]: b[3] for b in _dt_blocks}
                      if _dt_blocks else {})
    # Glyph tinting: glyphs under a definition-tint wash lean ever so
    # slightly toward the wash color (syntax color stays the base), so text
    # reads as part of its panel - the same treatment used app-wide. Token
    # granularity: a symbol is per identifier token, so per-token is exact.
    # Cost: one bisect + ≤4-span overlap walk per visible token; the mixed
    # packed color is memoized in _GLYPH_MIX_CACHE.
    _dt_mix = (Toggles.TextEditor.def_text_tint_mix
               if _dt_spans else 0.0)
    _dt_starts = [s[0] for s in _dt_spans] if _dt_mix > 0 else None
    # The mix TARGET gets its own sat/value factors (independent of the
    # wash's bg_tint_* pair) but a shared brightness clamp - same _bg_adjust
    # machinery, different factor tuple.
    _tx_f = (Toggles.TextEditor.text_tint_saturation,
             Toggles.TextEditor.text_tint_value,
             Toggles.TextEditor.bg_min_brightness,
             Toggles.TextEditor.bg_max_brightness)
    # Override comments carrying tint=(...) draw their TEXT in that color -
    # the comment names a definition, so it wears it (text-only, no background).
    # The tint factors join the memo key so any toggle tweaks repaint.
    _ct_starts = [c[0] for c in _dt_comments] if _dt_comments else None
    _ct_factors = (Toggles.TextEditor.comment_tint_saturation,
                   Toggles.TextEditor.comment_tint_value,
                   Toggles.TextEditor.comment_min_brightness,
                   Toggles.TextEditor.bg_max_brightness)
    # Presentation mode: every glyph on a line WITHOUT a def-tint line band
    # lerps toward black (through _mix_packed, sharing the wash-mix memo) -
    # lines carrying a line tint keep full brightness and read like the
    # highlighted content. _dt_lines line numbers are buffer-based, the same
    # space as win_line / the loop's line counter below.
    _pres_lines = None
    _pres_k = 0.0
    if Toggles.presentation_mode and not is_search_box:
        _pres_k = 1.0 - Toggles.TextEditor.presentation_text_brightness
        if _pres_k > 0.0:
            _pres_lines = {l[0] for l in _dt_lines}
    # Diff-gap PREVIEW rows: a collapsed diff piece's header sits
    # Toggles.TextEditor.diff_preview_lines_below below the change's last
    # context line (open_files._diff_gap_folds slides it down), so the
    # lines from there up to and including the header are hidden-gap
    # content kept visible as a peek; likewise a piece's hidden range
    # ends diff_preview_lines_above short of the next change's context,
    # so the lines right after it are the peek from that side. Both are
    # painted with their alpha set by diff_preview_alpha. Display line
    # set, like _pres_lines; any lines a scope fold hides are skipped
    # (exact mapping, never the covering header). O(collapsed pieces ×
    # preview) per body run - never a walk of the buffer.
    # Memoized on the fold LAYOUT's identity: ds._fold_cache is rebuilt
    # exactly when the union ranges or a collapse set change (its key
    # carries both), and _diff_rngs is the memoized gap list - so an idle
    # repaint / scroll / live-edit tick requires one tuple compare, not a
    # walk of every gap (0.55 ms a frame at 322 gaps, measured 09-01).
    # Stand-in frames replay a snapshot's faded previews (_restore_preview,
    # gutter replay below); the diff layer is off, so the gate below
    # never overwrites them.
    _preview_lines = _restore_preview
    # Height of the separator band under a collapsed diff gap's header
    # row; the gap's chevron centers on it (gutter pass + badge pass).
    # [tint=(0.36, 0.62, 0.85)]
    _diff_band_h = 1.5
    # Diff-gap styling color: the file's tint (the same FileMeta color the
    # editor tab wears) for the band under the chevrons and the "N lines"
    # labels. Toggles.TextEditor.diff_fold_tint only in a buffer with no
    # file. Resolved once per body run - two dict reads.
    _dsep_path = (getattr(jump_to, 'path', None) if jump_to is not None
                  else getattr(ds, '_file_meta', None))
    _dsep_rgb = (_uj_file_tint(_dsep_path) if _dsep_path is not None
                 else None) or Toggles.TextEditor.diff_fold_tint
    _preview_alpha = Toggles.TextEditor.diff_preview_alpha
    _preview_n = (Toggles.TextEditor.diff_preview_lines_below,
                  Toggles.TextEditor.diff_preview_lines_above)
    _diff_col_prev = getattr(ds, '_diff_fold_collapsed', None)
    if (_diff_rng_set and _diff_col_prev and max(_preview_n) > 0
            and _preview_alpha < 1.0):
        _pv_layout = ds.__dict__.get('_fold_cache')
        _pv_layout = _pv_layout[2] if _pv_layout is not None else None
        _pv_memo = ds.__dict__.get('_diff_preview_memo')
        if (_pv_memo is not None and _pv_memo[0] is _diff_rngs
                and _pv_memo[1] is _pv_layout and _pv_memo[2] == _preview_n):
            _preview_lines = _pv_memo[3]
        else:
            _preview_lines = set()
            for _prng in _diff_rngs:
                if _prng not in _diff_col_prev:
                    continue
                # BELOW the change above the gap: the rows ending at the
                # header; ABOVE the change below it: the rows right after
                # the hidden range (_n_lines from the diff-range setup
                # above - set whenever _diff_rng_set is). Buffer lines,
                # mapped to display lines.
                for _pv_lo, _pv_hi in (
                        (max(_prng[0] - _preview_n[0] + 1, 0), _prng[0] + 1),
                        (_prng[1] + 1, min(_prng[1] + _preview_n[1] + 1, _n_lines))):
                    for _pb in range(_pv_lo, _pv_hi):
                        if _fold_d2b is None:
                            _preview_lines.add(_pb)
                        else:
                            _pi = bisect.bisect_right(_fold_d2b, _pb) - 1
                            if _pi >= 0 and _fold_d2b[_pi] == _pb:
                                _preview_lines.add(_pi)
            if not _preview_lines:
                _preview_lines = None
            ds._diff_preview_memo = (_diff_rngs, _pv_layout, _preview_n,
                                     _preview_lines)
    ds._diff_preview_lines = _preview_lines     # display coords; tests / overlays
    _pf("body:preview_rows")

    x = origin_x
    y = origin_y + win_line * line_px   # window's first line (lookback above the clip)
    _cur_ln = win_line                  # line number of the glyph being drawn
    src_i = win_off    # ABSOLUTE source index at the start of the current token
    _tv_idx = 0        # Nth inline view drawn this frame - its STABLE name. Render
                       # order stays stable frame-to-frame (so each view keeps its
                       # state), unlike source/line position which shifts on edits.
    _tv_edit = None    # (src_index, src_len, new_value) from an inline view that changed
    _tv_click = None   # (src_index, src_len, right_half) - press landed on a whole-token widget
    # Screen rects of PLAIN owns inline widgets (no render_func, so no event
    # subscription latching drags away from the editor). The press/drag
    # handlers ABOVE read last body-run's list - reset here if the body ran -
    # and skip caret/selection for gestures starting inside one, so a value
    # drag doesn't grow a text selection. Scroll/edit invalidations re-run the
    # body, so the rects track the screen pixels the user actually sees.
    ds._plain_tv_rects = []
    # Gutter widgets recorded by this body run (display line -> (spec, token,
    # name, kwargs)), consumed by the gutter pass below. Frame-local.
    _gutter_views = {}
    # Auto-exec edit watch for defs whose widget is scrolled out of view:
    # one identity test per render (see _fnrun_auto_exec_scan).
    _fnrun_auto_exec_scan(ds, text_editor_state, text,
                          code_dict if code_dict is not None else code_tree)
    # Glyph pass. ~900 tokens a frame: the plain single-line token is the
    # common case and gets the fast path below: no segment loop, and
    # consecutive same-colour ASCII tokens on a line merge to ONE add_text
    # (monospace: imgui's advance per glyph == char_w, so a merged run lands
    # every glyph exactly where separate draws did - the harness in the
    # perf notes asserts calc_text_size(run) == len(run) * char_w). Runs are
    # flushed before any other draw path so screen order is unchanged.
    _inline_by_key = ({k: (v, v.get("char_width") is not None)
                       for k, v in token_views.items() if isinstance(k, str)}
                      if token_views else {})
    _run_parts = None       # pending merged run: [tokens], start x/y, color, end x
    _run_x = _run_y = _run_end = 0.0
    _run_col = 0
    # Per-token packed colours memoized in the draw_state: the comment-tint /
    # def-tint resolution below (a bisect + span walk + hsv mix per token)
    # depends only on the cached token window and the cached tint tables,
    # which are the same objects frame after frame during a drag / hover
    # session - so a hit replaces all of it with a packed value per token.
    _tc_key = (tokens, win_off, _dt_spans, _dt_comments, _dt_mix, _ct_factors, _tx_f)
    _tc_memo = getattr(ds, '_tok_color_memo', None)
    _tok_colors = None
    if (_tc_memo is not None and _tc_memo[0][0] is tokens and _tc_memo[0][1] == win_off
            and _tc_memo[0][2] is _dt_spans and _tc_memo[0][3] is _dt_comments
            and _tc_memo[0][4:] == _tc_key[4:]):
        _tok_colors = _tc_memo[1]
    _tok_colors_new = [] if _tok_colors is None else None
    # Draw runs (built with the colour memo): consecutive PLAIN tokens on a
    # line with the same resolved colour become one run - {first token
    # index: (n_tokens, text, num_chars, colour)} - so a memo hit draws a run
    # with one add_text and skips its member tokens with a counter instead
    # of pushing ~900 tokens through the per-token path every frame.
    _runs = _tc_memo[2] if (_tok_colors is not None and len(_tc_memo) > 2) else None
    _plain_rec = [] if _tok_colors_new is not None else None
    # Positional live-usage gaps (absolute source index → width): stamped by
    # _window() beside the vcols trails. Tokens are pre-split so a gap
    # always STARTS a token - one dict probe per token, all draw paths
    # (runs, widgets, segments) use the shifted x.
    _lv_gaps = ds.__dict__.get('_lv_gap_map') or None
    _at_gap = False
    _skip_n = 0
    _ti = -1
    for token, color_key in tokens:
        _ti += 1
        if _skip_n:
            _skip_n -= 1
            continue
        _at_gap = False
        if _lv_gaps is not None:
            _gp = _lv_gaps.get(src_i)
            if _gp:
                x += _gp * char_w
                _at_gap = True
        if _runs is not None:
            _run = _runs.get(_ti)
            if _run is not None:
                _rn, _rtext, _rchars, _rcol = _run
                if _rtext and y + line_px >= rect_min_y and y <= rect_max_y:
                    _seg_col = (_rcol if _pres_lines is None
                                or _cur_ln in _pres_lines
                                else _mix_packed(_rcol, (0.0, 0.0, 0.0), _pres_k))
                    if _preview_lines is not None and _cur_ln in _preview_lines:
                        _seg_col = _fade_packed(_seg_col, _preview_alpha)
                    draw_list.add_text(x, y, _seg_col, _rtext)
                x += _rchars * char_w
                src_i += _rchars
                _skip_n = _rn - 1
                continue
        if color_key == 'clipped':
            # Off-screen stretch on a visible line (see _window_tokens band):
            # never contains a newline, never draws - just advance.
            if _run_parts is not None:
                draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
                _run_parts = None
            if _tok_colors_new is not None:
                _tok_colors_new.append(0)
            x += len(token) * char_w
            src_i += len(token)
            continue
        if _tok_colors is not None:
            color = _tok_colors[_ti]
        else:
            color = COLORS[color_key]
            # Inside a color-carrying override comment, the comment text AND the
            # merged color-tuple token (color3 - the token's `(r, g, b)` text)
            # wear the comment's adjusted color; other value types (numbers,
            # bools) keep their own token colors.
            if _ct_starts is not None and color_key in ('comment', 'color3'):
                _ci = bisect.bisect_right(_ct_starts, src_i) - 1
                if _ci >= 0 and src_i < _dt_comments[_ci][1]:
                    _cc = _dt_comments[_ci][2]

                    _ck = (_cc, _ct_factors)
                    _pk = _COMMENT_TINT_CACHE.get(_ck)
                    if _pk is None:
                        _cr, _cg, _cb = _comment_tint_color(_cc)
                        _pk = imgui.get_color_u32_rgba(_cr, _cg, _cb, 1.0)
                        if len(_COMMENT_TINT_CACHE) > 1024:
                            _COMMENT_TINT_CACHE.clear()
                        _COMMENT_TINT_CACHE[_ck] = _pk
                    color = _pk
            elif _dt_mix > 0:
                # Spans sort (start, -width): at a given start the SHORTEST comes
                # last, so bisect lands on the base symbol for the base token; the
                # short backward walk finds the chain span still covering a
                # member token past the base's end.
                _si = bisect.bisect_right(_dt_starts, src_i) - 1
                for _k in range(_si, max(-1, _si - 4), -1):
                    _sp = _dt_spans[_k]
                    if _sp[1] <= src_i:
                        continue
                    if _sp[0] <= src_i:
                        # Mix toward the tint through the TEXT factor pair
                        # (text_tint_saturation/value + shared brightness clamp).
                        color = _mix_packed(color, _bg_adjust(tuple(_sp[2][:3]), _tx_f),
                                            _dt_mix * _sp[3])
                    break
            # Inline token view: a str-keyed token_views entry with a char_width draws
            # a widget INSTEAD of this token's text, occupying char_width cells (see
            # the token-views note above). type-keyed entries are handled by the
            # gutter pass after the body.
            _tok_colors_new.append(color)
        _vi = _inline_by_key.get(color_key)
        if _vi is None:
            _view, _inline = None, False
        else:
            _view, _inline = _vi
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
            if _run_parts is not None:
                draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
                _run_parts = None
            # Presentation dim for the whole-token paths below (caret-in text,
            # with lead text) - safe to overwrite as this branch continues.
            if _pres_lines is not None and _cur_ln not in _pres_lines:
                color = _mix_packed(color, (0.0, 0.0, 0.0), _pres_k)
            if _preview_lines is not None and _cur_ln in _preview_lines:
                color = _fade_packed(color, _preview_alpha)
            # GUTTER (`gutter`): the token is plain text only; the widget is
            # recorded for the gutter pass (drawn on this line in place of
            # the line number) - no lead / trail cells, no caret hiding.
            _gutter = bool(_view.get("gutter"))
            _lead = 0 if _gutter else _view.get("lead_cells", 0)
            # TRAILING (`trail_cells`): the accessory's mirror - the token
            # text draws in place, the widget gets the trail cells right
            # after it (the rest of the line shifts; vcols carries it).
            _trail = 0 if _gutter else _view.get("trail_cells", 0)
            _cells = _lead + len(token) + _trail
            _wx = x + (_lead + len(token)) * char_w if _trail else x
            # While the editor caret sits on TOP a REPLACE token, the widget
            # gets out of the way entirely: the token rides as plain text, so
            # caret, selection and typing behave like any other code, and the
            # widget returns when the caret leaves. (The widget view
            # composites ABOVE the editor tile, so a caret under it would be
            # invisible anyway.) _tv_idx is still consumed so the OTHER
            # visible widgets keep their render-order names (and state).
            _caret_in = (not _lead and not _trail and not _gutter
                         and Melty.text_focused_ds is ds
                         and src_i <= ds.text_cursor_pos <= src_i + len(token))
            if _caret_in and y + line_px >= rect_min_y and y <= rect_max_y:
                _tv_idx += 1
                draw_list.add_text(x, y, color, token)
                # Pass-through widgets (bool) rely on clicks reaching the
                # editor, so the FIRST click of a double-click places the
                # caret in the token and lands us here - the hidden widget
                # can't see the second click. Honor the double-click toggle
                # for it: flip the literal exactly as draw_bool_token would.
                if (color_key == 'bool' and token in ('True', 'False')
                        and x <= io.mouse_pos.x < x + len(token) * char_w
                        and y <= io.mouse_pos.y < y + line_px
                        and imgui.is_mouse_double_clicked(0)):
                    _tv_edit = (src_i, len(token),
                                'False' if token == 'True' else 'True', False)
            elif y + line_px >= rect_min_y and y <= rect_max_y:
                _name = f"{ds.name}_tv{_tv_idx}"
                _tv_idx += 1
                _save_cur = imgui.get_cursor_screen_pos()
                # pad_px widens a REPLACE widget's view - and so its clip rect -
                # a few px past the token cells on both sides, giving the frame
                # breathing room around the glyphs. (Expanding inside the
                # renderer doesn't work: drawing clips at the view boundary.)
                # The cells the token reserves in the grid stay exact.
                _pad = 0 if (_lead or _trail) else _view.get("pad_px", 0)
                imgui.set_cursor_screen_pos((_wx - _pad, y))
                if _trail:
                    _w = _trail * char_w
                elif _lead:
                    _w = _lead * char_w
                else:
                    _w = len(token) * char_w + 2 * _pad
                # Inside a tint-carrying override comment, bool/number
                # widgets adopt the comment's (adjusted) color for clutter
                # reduction; the same widgets in code keep their own color.
                _extra = {}
                if color_key in ('bool', 'number'):
                    _wc = None
                    if _ct_starts is not None:
                        _wci = bisect.bisect_right(_ct_starts, src_i) - 1
                        if _wci >= 0 and src_i < _dt_comments[_wci][1]:
                            _wc = _comment_tint_color(_dt_comments[_wci][2])
                    # Presentation dim: a widget in ANY `# [...]` override
                    # comment fades with its line - its comment tint when it
                    # has one, else its own default token color. The widget
                    # draws its own text via text_tint (float rgb, not the
                    # packed `color` dimmed at the branch entry), so scale
                    # here - boosted by presentation_widget_boost to visually
                    # match the comment background (the widget hsv path reads
                    # darker).
                    if (_pres_lines is not None and _cur_ln not in _pres_lines
                            and (_wc is not None
                                 or _in_comment_override(text, src_i))):
                        _w_tinted = _wc is not None
                        if _wc is None:
                            # Untinted widgets fade to the comment GREY -
                            # their native token blue reads as live code,
                            # not comment, at presentation dim.
                            _dc = COLORS['comment']
                            _wc = ((_dc & 0xFF) / 255.0,
                                   ((_dc >> 8) & 0xFF) / 255.0,
                                   ((_dc >> 16) & 0xFF) / 255.0)
                        _pb = min(1.0, (1.0 - _pres_k)
                                  * Toggles.TextEditor.presentation_widget_boost)
                        _wc = (_wc[0] * _pb, _wc[1] * _pb, _wc[2] * _pb)
                        # Background chip a step brighter than the text so
                        # the widget still stands like a block on a dim line.
                        _bb = Toggles.TextEditor.presentation_widget_bg_boost
                        if color_key == 'number':
                            # The number chip paints brighter than the bool's
                            # smaller one at the same intensity (depth-ramped bg +
                            # dragable fill stack) - extra dim for its bg,
                            # and lower the legibility cap to match.
                            _bb *= Toggles.TextEditor.presentation_number_bg_dim
                            _extra['max_bg_value'] = (
                                0.25 * Toggles.TextEditor.presentation_number_bg_dim)
                        _extra['tint'] = (min(1.0, _wc[0] * _bb),
                                          min(1.0, _wc[1] * _bb),
                                          min(1.0, _wc[2] * _bb))
                        # COLORED comment widgets get a transparency cut on
                        # top of the dim - saturated colors read brighter than
                        # the grey at equal value (4th component = text
                        # tint, honored by the token renderers).
                        if _w_tinted:
                            _wc = _wc + (Toggles.TextEditor.presentation_widget_alpha,)
                    if _wc is not None:
                        _extra['text_tint'] = _wc
                        if color_key == 'bool':
                            _extra.setdefault('tint', _wc)   # the bool's bg box too
                if color_key == 'number':
                    # Number chip adopts the EDITOR's tint (ds.tint) instead
                    # of its own dark constant so it blends with the
                    # surrounding view; draw_bg's bg_offset=-1 step plus the
                    # max_bg_value cap keeps it reading as a chip. setdefault:
                    # presentation mode's dimmed tint above still wins.
                    _extra.setdefault('tint', getattr(ds, 'tint', None)
                                      or _view.get("tint"))
                elif _view.get("tint") is not None:
                    _extra.setdefault('tint', _view["tint"])
                if color_key == 'def_name':
                    # Run-button context: which function this token heads -
                    # resolved by absolute file line (display line from y,
                    # projected through the fold layout to a buffer line,
                    # then _usage_off maps buffer → file, same offset the
                    # live-view overlays work with) plus the def's name -
                    # the token itself.
                    _dl = int((y - origin_y) / line_px + 0.5)
                    _bl = (_fold_d2b[_dl]
                           if _fold_d2b is not None and 0 <= _dl < len(_fold_d2b)
                           else _dl)
                    _fn_root = code_dict if code_dict is not None else code_tree
                    _fp = (getattr(_fn_root, 'file_path', None)
                           or getattr(getattr(_fn_root, 'address', None), 'path', None)
                           or getattr(jump_to, 'path', None))
                    _extra['file_path'] = str(_fp) if _fp else None
                    _extra['def_line'] = _usage_off + _bl + 1
                    _extra['def_name'] = token
                    # Params for above: hand the widget the ROOTED TREE
                    # plus coordinates - never the resolved def node. The
                    # code-host now serves Bubbling proxies whose identity
                    # doesn't survive re-access, so any per-token memo keyed
                    # on the tree misses every frame and the node lookup
                    # walked the whole parse per def token per frame. The
                    # widget resolves lazily (on click / while its panel is
                    # open), memoized against the buffer text identity.
                    _extra['code_root'] = _fn_root
                    _extra['def_buf_line'] = _bl + 1
                    _extra['tv_text'] = text
                    _extra['def_disp_line'] = _dl
                    _extra['editor_state'] = text_editor_state
                    _extra['fn_tint'] = _fn_tint_lines.get(_cur_ln)
                # Plain (wrapper-less) renderers need the editor's draw_state:
                # they have no tile of their own, so gesture liveness requires
                # invalidating the EDITOR tile (see draw_number_token_plain).
                if getattr(_view["renderer"], "_plain_tv", False):
                    _extra['editor_ds'] = ds
                    # ACCESSORY plain widgets register too: _w is just the
                    # lead area there, so the token text keeps normal clicks
                    # while a press on the swatch doesn't move the caret
                    # (the wrapped version's left_mouse_down latch did this).
                    if _view.get("owns_mouse") and not _gutter:
                        ds._plain_tv_rects.append((_wx - _pad, y, _wx - _pad + _w, y + line_px))
                if _gutter:
                    # Deferred to the gutter pass: it draws the widget on
                    # this display line (the gutter owns the cell geometry and
                    # the owns_mouse rect) - here the token is plain text.
                    _gutter_views[_cur_ln] = (_view, token, _name, _extra)
                    _res = None
                else:
                    try:
                        _res = _view["renderer"](token, width=_w, height=line_px,
                                                 name=_name, **_extra)
                    except Exception:                    _res = None
                imgui.set_cursor_screen_pos(_save_cur)
                if _lead or _trail or _gutter:
                    draw_list.add_text(x + _lead * char_w, y, color, token)
                if (isinstance(_res, tuple) and len(_res) >= 2 and _res[0]
                        and isinstance(_res[1], str) and _res[1] != token):
                    # owns_mouse REPLACE edits come from a value DRAG - the
                    # caret must not be stamped into the token by the splice
                    # (a caret in the token hides the widget, killing the
                    # drag on its first value change); see the splice below.
                    _tv_edit = (src_i, len(token), _res[1],
                                bool(_view.get("owns_mouse")) and not _lead)
                # owns_mouse REPLACE widgets consume the melty mouse events, so
                # a press on them never reaches the editor's click handling -
                # read the raw mouse and place the caret at the column under
                # it, exactly like a text click (the widget's cells are
                # identity vcols, one cell per source char). Fires on RELEASE
                # without drag, not on press: a click-and-drag is a value
                # adjustment and must keep the widget alive (placing a caret
                # hides it - see _caret_in above), while a plain click hands
                # the token over to text editing. get_mouse_drag_delta stays
                # (0,0) until the drag threshold is ever exceeded, so a drag
                # that circles back to its origin still counts as a drag.
                # Pass-through widgets (bool) and ACCESSORY widgets skip this:
                # their text takes normal editor clicks, and a press on an
                # accessory (opening its popover) shouldn't move the caret.
                if (_view.get("owns_mouse") and not _lead and not _trail
                        and not _gutter
                        and x <= io.mouse_pos.x < x + _cells * char_w
                        and y <= io.mouse_pos.y < y + line_px):
                    if imgui.is_mouse_clicked(0):
                        ds._tv_press_time = time.time()
                    if imgui.is_mouse_released(0):
                        # A click must also be SHORT: press-and-hold is an
                        # (abandoned) drag, and releasing it in-place must not
                        # move the caret. pyimgui doesn't expose imgui's
                        # mouse_click_duration, so the press time on our own.
                        _dd = imgui.get_mouse_drag_delta(0)
                        if (_dd.x == 0 and _dd.y == 0
                                and time.time() - getattr(ds, '_tv_press_time', 0) < 0.33):
                            _col = int((io.mouse_pos.x - x) / char_w + 0.5)
                            _tv_click = (src_i, len(token),
                                         min(len(token), max(0, _col)))
            x += _cells * char_w
            src_i += len(token)
            continue
        if not _inline and color_key != 'icon' and '\n' not in token:
            # Plain single-line token (the common case): no segment loop.
            # A gap-starting token never records as mergeable: the memoized
            # run replay draws whole runs and skips members, so a gap
            # swallowed into it would lose its x shift (the live merge below
            # breaks this - the shifted x fails the _run_end == x
            # join test).
            if _plain_rec is not None:
                _plain_rec.append((_ti, color,
                                   token.isascii() and '\t' not in token
                                   and not _at_gap, token))
            if token and y + line_px >= rect_min_y and y <= rect_max_y:
                _seg_col = (color if _pres_lines is None
                            or _cur_ln in _pres_lines
                            else _mix_packed(color, (0.0, 0.0, 0.0), _pres_k))
                if _preview_lines is not None and _cur_ln in _preview_lines:
                    _seg_col = _fade_packed(_seg_col, _preview_alpha)
                _mergeable = token.isascii() and '\t' not in token
                if (_run_parts is not None and _mergeable and _run_y == y
                        and _run_col == _seg_col and _run_end == x):
                    _run_parts.append(token)
                    _run_end = x + len(token) * char_w
                else:
                    if _run_parts is not None:
                        draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
                        _run_parts = None
                    if _mergeable:
                        _run_parts = [token]
                        _run_x, _run_y, _run_col = x, y, _seg_col
                        _run_end = x + len(token) * char_w
                    else:
                        draw_list.add_text(x, y, _seg_col, token)
            x += len(token) * char_w
            src_i += len(token)
            continue
        if _run_parts is not None:
            draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
            _run_parts = None
        start = 0

        while True:
            nl = token.find('\n', start)
            seg = token[start:nl] if nl != -1 else token[start:]
            if seg and y + line_px >= rect_min_y and y <= rect_max_y:
                _seg_col = (color if _pres_lines is None
                            or _cur_ln in _pres_lines
                            else _mix_packed(color, (0.0, 0.0, 0.0), _pres_k))
                if _preview_lines is not None and _cur_ln in _preview_lines:
                    _seg_col = _fade_packed(_seg_col, _preview_alpha)
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
                        # Plain (wrapper-less) renderers get the editor's
                        # draw_state and, with owns_mouse, a caret-suppression
                        # rect; the same plumbing the whole-token path has.
                        _extra = ({'tint': _view["tint"]}
                                  if _view.get("tint") is not None else {})
                        if getattr(_view["renderer"], "_plain_tv", False):
                            _extra['editor_ds'] = ds
                            if _view.get("owns_mouse"):
                                ds._plain_tv_rects.append(
                                    (_ix, y, _ix + _cw * char_w, y + line_px))
                        try:
                            _res = _view["renderer"](_ch, width=_cw * char_w, height=line_px, name=_name,
                                                     **_extra)
                        except Exception:
                            _res = None
                        imgui.set_cursor_screen_pos(_save_cur)
                        if (isinstance(_res, tuple) and len(_res) >= 2 and _res[0]
                                and isinstance(_res[1], str) and _res[1] != _ch):
                            _tv_edit = (_src, 1, _res[1], False)
                        _ix += _cw * char_w
                elif color_key == 'icon':
                    # Font Awesome glyphs aren't monospaced - their natural width
                    # differs from char_w. Draw each in its own standard-width cell
                    # (so surrounding code stays grid-aligned) and nudge it 1px left
                    # to sit better in the cell.
                    ix = x
                    for ch in seg:
                        draw_list.add_text(ix - 1, y, _seg_col, ch)
                        ix += char_w
                else:
                    draw_list.add_text(x, y, _seg_col, seg)
            if nl == -1:
                # Inline views: each char occupies char_width cells; else 1 cell.
                x += len(seg) * (_view["char_width"] if _inline else 1) * char_w
                break
            x = origin_x
            y += line_px
            _cur_ln += 1

            start = nl + 1
        src_i += len(token)
    if _run_parts is not None:
        draw_list.add_text(_run_x, _run_y, _run_col, ''.join(_run_parts))
        _run_parts = None
    if _tok_colors_new is not None and len(_tok_colors_new) == len(tokens):
        # Fold the plain tokens into runs (see _plain above).
        _new_runs = {}
        _r_start = _r_n = _r_chars = 0
        _r_parts = []
        _r_col = None
        _r_merge = False
        _prev_ti = -2
        for _pti, _pcol, _pmerge, _ptok in _plain_rec:
            if (_r_n and _pti == _prev_ti + 1 and _pmerge and _r_merge
                    and _pcol == _r_col):
                _r_parts.append(_ptok)
                _r_n += 1
                _r_chars += len(_ptok)
            else:
                if _r_n:
                    _new_runs[_r_start] = (_r_n, ''.join(_r_parts), _r_chars, _r_col)
                _r_start, _r_n, _r_chars = _pti, 1, len(_ptok)
                _r_parts = [_ptok]
                _r_col, _r_merge = _pcol, _pmerge
            _prev_ti = _pti
        if _r_n:
            _new_runs[_r_start] = (_r_n, ''.join(_r_parts), _r_chars, _r_col)
        ds._tok_color_memo = (_tc_key, _tok_colors_new, _new_runs)

    _pf("body:glyphs")
    # An inline view (e.g. the icon dropdown) changed its value - splice the new
    # text in for the view's source char and report the edit, so the framework
    # reparses/saves exactly as if it were typed.
    # Params-panel splices (PARTIAL CODE INSERTION): the def widget's
    # new values replace each changed default's expression inside the
    # parenthesis - applied bottom-up so earlier splices never shift later
    # ones, through the same text path as token-widget edits, so they
    # save/undo like keystrokes.
    _pp_splices = ds.__dict__.pop('_fnrun_splices', None)
    if _pp_splices:
        _pp_splices = _fnrun_resolve_splices(text, _pp_splices)
    if _pp_splices:
        for _ps, _pl, _pv in sorted(_pp_splices, reverse=True):
            _ptrace("editor params-splice", name=ds.name, at=_ps,
                    old=repr(text[_ps:_ps + _pl][:24]), new=repr(_pv[:24]))
            text = text[:_ps] + _pv + text[_ps + _pl:]
            _d = len(_pv) - _pl
            if _d:
                for _attr in ('text_cursor_pos', 'text_selection_start',
                              'text_selection_end'):
                    _v = getattr(ds, _attr)
                    if _v >= _ps + _pl:
                        setattr(ds, _attr, _v + _d)
        changed = True
    if _tv_edit is not None:
        _es, _el, _ev, _keep_caret = _tv_edit
        # Timeline: every token-widget splice, with old→new content. A splice
        # with NO mouse gesture is the echo-storm signature - this line names
        # the token (and so the widget) that fired.
        _ptrace("editor token-splice", name=ds.name, at=_es,
                old=repr(text[_es:_es + _el][:24]), new=repr(_ev[:24]))
        text = text[:_es] + _ev + text[_es + _el:]
        if _keep_caret:
            # Widget widget edit: leave the caret where it is (stamping it into
            # the token would hide the cursor mid-drag - see _caret_in). Only
            # shift positions sitting at/after the splice, when the token's
            # length changed, so the selection stays on the same line.
            # Also latch "this mouse gesture edited a value" - the release
            # handler below must NOT place the caret after a value drag, and
            # short drags (1-6px: enough to edit, under imgui's drag
            # threshold) are indistinguishable from clicks by mouse motion
            # alone. Cleared on every mouse release.
            ds._tv_gesture_edited = True
            _d = len(_ev) - _el
            if _d:
                for _attr in ('text_cursor_pos', 'text_selection_start',
                              'text_selection_end'):
                    _v = getattr(ds, _attr)
                    if _v >= _es + _el:
                        setattr(ds, _attr, _v + _d)
        else:
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
    # A press on a whole-token widget also places the editor caret INSIDE the
    # literal at the clicked column - and focuses the editor - so the literal
    # feels like any other text; the widget's only extra behavior is the drag.
    # Applied after the splice so it overrides its caret-at-end default; if the
    # same press changed the value, clamp to the NEW token's length.
    if _tv_click is not None and not getattr(ds, '_tv_gesture_edited', False):
        _cs, _cl, _col = _tv_click
        if _tv_edit is not None and _tv_edit[0] == _cs:
            _cl = len(_tv_edit[2])
        Melty.text_focused_ds = ds
        ds.text_cursor_pos = _cs + min(_col, _cl)
        ds.text_selection_start = ds.text_selection_end = ds.text_cursor_pos
        ds.text_cursor_blink_time = time.time()
    # The gesture-edited latch lives for exactly one mouse gesture: every
    # release ends it (checked above too, so the release that ENDS a value
    # drag is still suppressed).
    if getattr(ds, '_tv_gesture_edited', False) and imgui.is_mouse_released(0):
        ds._tv_gesture_edited = False
    # End of a plain-widget gesture: selection suppression lifts. (Also
    # re-evaluated on every fresh press, so a missed release event can't
    # leave it latched across frames.)
    if getattr(ds, '_plain_tv_gesture', False) and imgui.is_mouse_released(0):
        ds._plain_tv_gesture = False

    # Token views keyed by code_tree node TYPE (e.g. Conditional) — overlay pass,
    # positioned by each node's span. Runs after the inline text so widgets paint
    # on top of the code they annotate. The PARSE arrives as code_tree in the
    # chain routes but as code_dict on the code-host-cache route (where
    # code_tree carries only the error dict - see draw_text_editor_code_cache),
    # so prefer code_dict when both are present; it's the node tree with spans.
    _tv_tree = code_dict if code_dict is not None else code_tree
    if token_views and _tv_tree is not None:
        # Same-frame edit delta for the overlays (see display_shift in
        # _draw_cst_token_views): built from the display edit _window
        # memoized for this frame's trail remap (`_lv_trail_splice`), or
        # computed here when that path didn't run.
        _tv_disp_shift = None
        if text is not original_input and isinstance(original_input, str):
            _tvm = getattr(ds, '_lv_trail_splice', None)
            if (_tvm is None or _tvm[0] is not original_input
                    or _tvm[1] is not text):
                _tvm = (original_input, text,
                        _display_edit_splice(original_input, text),
                        _line_offsets_cached(original_input))
                ds._lv_trail_splice = _tvm
            _tv_sp, _tv_offs_prev = _tvm[2], _tvm[3]
            if _tv_sp is not None:
                def _tv_disp_shift(ln, _sp=_tv_sp, _offs=_tv_offs_prev):
                    # 1-based display line of the frame-start text → this
                    # frame's line. Lines past the edit shift by its line
                    # delta; the edited line keeps its number when the
                    # deletion happened at its start or end (Enter at a line
                    # end is an insertion at the NEXT line's start, so
                    # that line shifts whole); a mid-line split re-anchors
                    # the line (None).
                    _p, _oe, _d, _dl, _el, _oel = _sp
                    i0 = ln - 1
                    if i0 > _oel:
                        return ln + _dl
                    if i0 < _el:
                        return ln
                    if _el == _oel and i0 < len(_offs):
                        _ls = _offs[i0]
                        _le = (_offs[i0 + 1] - 1 if i0 + 1 < len(_offs)
                               else None)
                        if _p <= _ls:
                            return ln + _dl
                        if _le is not None and _p >= _le and _oe >= _le:
                            return ln
                    return None
                _tv_disp_shift._sp = _tv_sp
        # Span cols are in the PARSE's coords (dedented on the code-host
        # route); shift the origin by the indent delta so node overlays land
        # on the glyphs, which use the buffer's file-indented chars.
        _tv_shift = _parse_col_shift(text, getattr(_tv_tree, 'source', '') or '')
        # Caret position in buffer space (1-indexed line, non-shifted col) for
        # the live_view overlays - with hover preview off, a marker whose symbol
        # the caret sits on shows its value window instead.
        # Value widgets show their window only while SELECTED: hand the
        # focused editor's non-empty selection down as (line, col) bounds
        # (1-indexed lines, shift-corrected cols); a blank editor passes None.
        # The caret (the selection's MOVING end) rides along too: of the
        # widgets inside the selection only the one nearest it - the last
        # one selected - shows its window (live_view_views.flush_selected()).
        # Caret's 0-based DISPLAY line while this editor owns text focus -
        # the live-view inline pills hide on that line so the real code is
        # editable under the caret. `text` is display space here (widgets token
        # spliced), same space as the pills' fold to lines. O(caret offset)
        # once per focused repaint.
        _tv_caret_line = None
        if Melty.text_focused_ds is ds:
            _tv_caret_line, _ = _index_to_line_col(text, ds.text_cursor_pos)
        _sel_lo = _sel_hi = _sel_caret = None
        if Melty.text_focused_ds is ds and _has_selection(ds):
            _s0, _s1 = _sel_range(ds)
            _l0, _c0 = _index_to_line_col(text, _s0)
            _l1, _c1 = _index_to_line_col(text, _s1)
            _sel_lo = (_l0 + 1, _c0 - _tv_shift)
            _sel_hi = (_l1 + 1, _c1 - _tv_shift)
            _lc, _cc = _index_to_line_col(text, ds.text_cursor_pos)
            _sel_caret = (_lc + 1, _cc - _tv_shift)
        # Folds collapsed? pass the FULL buffer to the parse→buffer diff
        # bridge (the display text has one deletion per collapsed fold, and
        # the single-region diff maps everything between the first and last
        # fold to None - live-view markers on visible lines vanished) and
        # map buffer→display exactly via the fold layout.
        _tv_fold_lm = None
        _tv_buf = text
        if _fold_bl is not None:
            _tv_buf = _fold_full

            def _tv_fold_lm(line, _d2b=_fold_d2b):
                # 1-indexed full-buffer line → 1-indexed display line;
                # None while hidden inside a collapsed region.
                b = line - 1
                i = bisect.bisect_right(_d2b, b) - 1
                if i < 0 or _d2b[i] != b:
                    return None
                return i + 1
            # The layout the closure projects through, for the snapshot
            # view's idle-pass memo (live_view_views): the closure is
            # fresh every frame, the layout list only changes with a fold
            # toggle - so the memo keys are frame and the display lines match.
            _tv_fold_lm._d2b = _fold_d2b
        _draw_cst_token_views(_tv_tree, token_views, origin_x + _tv_shift * char_w,
                              origin_y, line_px, char_w, ds,
                              line_offset=_usage_off, jump_to=jump_to,
                              buffer_text=_tv_buf, sel_lo=_sel_lo,
                              sel_hi=_sel_hi, fold_line_map=_tv_fold_lm,
                              sel_caret=_sel_caret, fold_d2b=_fold_d2b,
                              caret_line=_tv_caret_line, live_store=live_store,
                              col_shift=_tv_shift, display_shift=_tv_disp_shift)

    # Live-usage trailing gaps: drop labels from defs whose overlay didn't
    # re-stamp THIS frame (scrolled out, store cleared, live view toggled
    # off) so their reserved gaps close on the next layout. The usage pass
    # stamps (frame, span) per def - see live_view_views._draw_usage_labels.
    _tv_trails = getattr(ds, '_lv_trail_views', None)
    if _tv_trails:
        _tv_now = Melty.frame_count
        _tv_stale = [k for k, (f, _s) in _tv_trails.items() if f != _tv_now]
        for k in _tv_stale:
            del _tv_trails[k]
        if _tv_stale:
            ds._lv_trail_gen = getattr(ds, '_lv_trail_gen', 0) + 1
            ds.invalidate()
            request_render()

    _pf("body:tv_overlay")
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
    _pf("squiggles+hint")
    # --- Line-number gutter ---
    # Drawn after the text body in its own clip column (left to → gutter_w) so
    # the numbers stay fixed while code scrolls horizontally under them. Numbers
    # ride origin_y, so they scroll vertically in lockstep with their lines. The
    # cursor's line is brightened for emphasis.
    # Fold headers swap the line number for the fold chevron (both states) -
    # lookup: display line -> (range, collapsed?). Badge rects reset HERE,
    # before subsequent passes that append to them (gutter chevrons below, collapsed
    # "N lines" labels in the folding pass above the body).
    ds._fold_badge_rects = []
    _fh_c = getattr(ds, '_fold_hdr_cache', None)
    if _fold_folds and _fh_c is not None and _fh_c[0] is _fold_folds:
        _fold_hdr = _fh_c[1]     # same fold layout as last frame
    else:
        _fold_hdr = _fold_header_map(_fold_folds) if _fold_folds else {}
        ds._fold_hdr_cache = (_fold_folds, _fold_hdr)
    if _restore_hdr:
        # Stand-in frames: chevrons replayed throughout the gutter (the fold
        # layer sits restore frames below, so _fold_folds is empty). Range is
        # None - draw-only, no badge rect, nothing to toggle.
        _fold_hdr = _restore_hdr
    if show_gutter and gutter_w > 0:
        gutter_bg = (*Tint.line_number_bg()[:3], 1.0)  # dark tinted gray
        num_color = imgui.get_color_u32_rgba(*Tint.line_number_tint()[:3], 1.0)
        cur_color = imgui.get_color_u32_rgba(*Tint.cursor_tint()[:3], 1.0)
        cur_line = _index_to_line_col(text, ds.text_cursor_pos)[0] if is_focused else -1
        # Clamp the column's top to the text body (origin_y) so the fill doesn't
        # ride up over the header bar above it; rect_min_y still works once the
        # body has scrolled up past the clip top.
        gutter_top = max(rect_min_y, origin_y)
        _gut_sh = Toggles.TextEditor.gutter_shadow_offset
        _uh_sh = Toggles.TextEditor.usage_heat_shadow_offset
        _uh_sh_max = Toggles.TextEditor.usage_heat_shadow_max
        _gut_clip = (left, gutter_top, left + gutter_w, rect_max_y)
        if _gut_sh:
            # Recessed strip (negative shadow): the code surface casts into
            # the gutter along its edge. The rect IS the exact strip (the
            # shadow sits outside _sh_clip's text-body bounds), so no clip.
            add_shadow((left, gutter_top, gutter_w, rect_max_y - gutter_top),
                       offset=_gut_sh, corner_radius=0.0, clip=False,
                       draw_state=ds)
        # gutter_indent: the fold chevrons move out of the number strip
        # into the indent band to its right (the 4-column inset through
        # column 0), right-aligned there — so the numbers keep their column
        # and the arrow sits where the root-level indent guide would start.
        # The clip widens to cover the band; the fill stays the strip.
        _chev_in_indent = bool(gutter_indent)
        _gut_clip_r = left + gutter_w + (gutter_margin if _chev_in_indent else 0.0)
        draw_list.push_clip_rect(left, gutter_top, _gut_clip_r, rect_max_y, True)
        draw_list.add_rect_filled(left, gutter_top, left + gutter_w, rect_max_y, imgui.get_color_u32_rgba(*gutter_bg))
        # Line-tint lookup for the heat wash below: a line with a definition
        # tint draws its number with THAT color instead of the usage heat ramp.
        _dt_line_map = {l[0]: l for l in _dt_lines} if _dt_lines else {}
        # Live-marker open/close buttons (per _lv_btn_w above): raw draw-list
        # icons; deliberately NOT melty buttons since a render_func per line
        # would dominate the gutter pass. Click toggles every marker on the
        # line via set_marker_open and invalidates this tile so their marker
        # bodies re-run and create/hide their value windows.
        _lv_marks = (getattr(ds, "_lv_gutter_markers", None) or {}) if _lv_btn_w else {}
        total_lines = text.count('\n') + 1
        # Visible band only - the old range(total_lines) walked every line of
        # the file per frame and culled inside the loop; the number/heat-box
        # work only ever applies to on-screen lines, so calculate the band once
        # and iterate just those.
        _gl0 = max(0, int((gutter_top - origin_y) // line_px))
        _gl1 = min(total_lines, int((rect_max_y - origin_y) // line_px) + 2)

        # Error buttons: one per marker line, red flat_button with the
        # warning glyph over the number. Off-screen markers still show —
        # clamped to the strip's top / bottom row with an arrow toward the
        # line (see _draw_error_button). A click toggles the message box
        # (`ds._err_open_line`); the box also closes when its marker goes.
        # [tint=(0.95, 0.25, 0.25)]
        error_button_color = (0.85, 0.12, 0.14)
        # Bg brightness knobs: flat_button's theme mix + clamp mute a bg to
        # 0.25 brightness by default - lifted here so the chip reads RED.
        error_button_value = 0.45
        error_button_max_brightness = 0.6
        # [tint=(1.0, 0.75, 0.72)]
        error_icon_color = (1.0, 0.80, 0.78, 1.0)
        error_icon = "\uf071"
        error_up_icon = "\uf077"
        error_down_icon = "\uf078"
        _err_by_line = {}
        for _el, _em in _err_markers:
            _err_by_line.setdefault(_el - 1, _em)
        if (getattr(ds, '_err_open_line', None) is not None
                and ds._err_open_line not in _err_by_line):
            ds._err_open_line = None

        def _draw_error_button(line_idx, ly, x1, arrow=None):
            """The marker's gutter button at row `ly` — its own line, or the
            clamped top / bottom row with `arrow` = up / down glyph, where
            the arrow is its OWN button (left) that scrolls the error line
            into view (centered), next to the error button proper."""
            from src.lsd.gl_gui.view.core_views.headers import flat_button
            _eb_x0 = left + _lv_btn_w + 2.0
            _eb_h = max(6.0, line_px - 4.0)
            _eb_save = imgui.get_cursor_screen_pos()
            if arrow is not None:
                _ar_w = max(10.0, (x1 - _eb_x0) * 0.4)
                imgui.set_cursor_screen_pos((_eb_x0, ly + (line_px - _eb_h) * 0.5))
                ds._plain_tv_rects.append((_eb_x0, ly, _eb_x0 + _ar_w, ly + line_px))
                if flat_button(f"{arrow}##{ds.name}errgo{line_idx}", ds,
                               f"errgo::{line_idx}", width=_ar_w, height=_eb_h,
                               color=error_button_color, corner_radius=4.0,
                               shadow=True, text_pad=1,
                               tint_value=error_button_value,
                               max_bg_brightness=error_button_max_brightness,
                               text_color=error_icon_color):
                    # Center the error line (the Ctrl+B goto's math: an
                    # offset in tile coords, capped at the max scroll).
                    _target = (line_idx * line_px
                               - max(0.0, (ds.height or 0) - line_px) * 0.5)
                    _mx = getattr(ds, '_max_scroll_y', None)
                    if _mx is not None:
                        _target = min(_target, _mx)
                    ds.scroll_offset = (ds.scroll_offset[0], max(0.0, _target))
                    ds.invalidate()
                    request_render()
                _eb_x0 += _ar_w + 2.0
            _eb_w = max(12.0, x1 - _eb_x0)
            _eb_label = error_icon
            imgui.set_cursor_screen_pos((_eb_x0, ly + (line_px - _eb_h) * 0.5))
            ds._plain_tv_rects.append((_eb_x0, ly, _eb_x0 + _eb_w, ly + line_px))
            _eb_hit = flat_button(f"{_eb_label}##{ds.name}err{line_idx}", ds,
                           f"err::{line_idx}", width=_eb_w, height=_eb_h,
                           color=error_button_color, corner_radius=4.0,
                           shadow=True, text_pad=2, factor=0.1,
                           tint_value=error_button_value,
                           max_bg_brightness=error_button_max_brightness,
                           text_color=error_icon_color)
            imgui.set_cursor_screen_pos(_eb_save)
            if _eb_hit:
                ds._err_open_line = (None if getattr(ds, '_err_open_line', None) == line_idx
                                     else line_idx)
                ds.invalidate()
                request_render()
                
        def _draw_gutter_widget(line_idx, ly, x1):
            """Draw the line's gutter widget in the number cell, from the
            live-marker button column to `x1`: an ERROR button when the line
            carries a parse/compile marker, else a `gutter: True` token view
            (the def run buttons). True when drawn; False (draw the number
            instead) when the line has none or the cell is too narrow."""
            if line_idx in _err_by_line:
                _draw_error_button(line_idx, ly, x1)
                return True
            _gv = _gutter_views.get(line_idx)
            if _gv is None:
                return False
            _gv_spec, _gv_tok, _gv_name, _gv_extra = _gv
            _gv_x0 = left + _lv_btn_w + 2.0
            if x1 - _gv_x0 < 12.0:
                return False
            _gv_save = imgui.get_cursor_screen_pos()
            imgui.set_cursor_screen_pos((_gv_x0, ly))
            if _gv_spec.get("owns_mouse"):
                ds._plain_tv_rects.append((_gv_x0, ly, x1, ly + line_px))
            try:
                _gv_spec["renderer"](_gv_tok, width=x1 - _gv_x0,
                                     height=line_px, name=_gv_name,
                                     **_gv_extra)
            except Exception:
                pass
            imgui.set_cursor_screen_pos(_gv_save)
            return True

        # Per-line number string + x memo, keyed on the numbers table's
        # identity, the offset and the gutter geometry (~60 visible lines a
        # frame built `str(num)` and a float expression each; the visible band
        # only changes on scroll, so this fills lazily and then hits).
        _gn_key = (line_numbers, line_offset, char_w, left + gutter_w)
        _gn_memo = getattr(ds, '_gutter_num_memo', None)
        if (_gn_memo is None or _gn_memo[0][0] is not line_numbers
                or _gn_memo[0][1:] != _gn_key[1:]):
            _gn_memo = (_gn_key, {})
            ds._gutter_num_memo = _gn_memo
        _gn = _gn_memo[1]
        _gn_base = left + gutter_w - 6.0
        for line_idx in range(_gl0, _gl1):
            ly = origin_y + line_idx * line_px
            if ly + line_px < gutter_top or ly > rect_max_y:
                continue
            _gne = _gn.get(line_idx)
            if _gne is None:
                if line_numbers is not None:
                    # Trailing empty line (the text ends in \n) has no number; so do
                    # any line whose number was explicitly None.
                    num = line_numbers[line_idx] if line_idx < len(line_numbers) else None
                    if num is None:
                        _gn[line_idx] = False
                        continue
                    num_str = str(num)
                else:
                    num_str = str(line_offset + line_idx + 1)
                _gne = _gn[line_idx] = (num_str, _gn_base - len(num_str) * char_w)
            elif _gne is False:
                continue
            num_str, nx = _gne
            # Usage heat box (see the aggregation pass above): a rounded wash
            # around the number, summed over every usage token on the line -
            # colored by the line's definition tint when it has one, so the
            # gutter mark matches the line's light.
            heat = _usage_line_heat.get(line_idx)
            if heat:
                _lt = _dt_line_map.get(line_idx)
                if _lt is not None:
                    _ga = _bg_adjust(tuple(_lt[1][:3]), _bg_f)
                    _hb = imgui.get_color_u32_rgba(_ga[0], _ga[1], _ga[2],
                                                   0.55 * _lt[2])
                else:
                    _hb = _usage_wash_color(heat)
                _hx0, _hx1 = nx - 3.0, left + gutter_w - 3.0
                _hy0, _hy1 = ly + 1, ly + line_px - 1
                if line_idx in _fold_hdr and not _chev_in_indent:
                    # Fold header: no number for the box, and the fold
                    # arrow shares the cell - shrink to a half-size strip
                    # (right-aligned, vertically centered) so the arrow gets
                    # breathing room to its left.
                    _hx0 = (_hx0 + _hx1) * 0.5
                    _hy0 = ly + line_px * 0.25
                    _hy1 = ly + line_px * 0.75
                if _uh_sh:
                    # Heat-scaled lift: the box's depth is the usage count
                    # times the per-usage offset, magnitude-capped so a
                    # hub line doesn't cast across the whole strip.
                    _uh_off = _uh_sh * heat
                    _uh_off = max(-_uh_sh_max, min(_uh_sh_max, _uh_off))
                    add_shadow((_hx0, _hy0, _hx1 - _hx0, _hy1 - _hy0),
                               offset=_uh_off, corner_radius=3.0,
                               clip=_gut_clip, draw_state=ds)
                draw_list.add_rect_filled(_hx0, _hy0, _hx1, _hy1, _hb, 3.0)
            _fh = _fold_hdr.get(line_idx)
            if (_fh is not None and _hide_expanded_diff and not _fh[1]
                    and _fh[0] in _diff_rng_set):
                _fh = None      # expanded diff span after expand-all: no chevron
            if _fh is not None:
                # Fold header line: chevron in place of the number; the
                # toggle handler at the top of the body reads these on
                # next frame.
                _rng_g, _col_g = _fh
                if _chev_in_indent:
                    # Indent band: arrow right-aligned in the band, number
                    # keeps its place in the strip; the hit zone is the band.
                    _band_l = left + gutter_w
                    _gcx = _gut_clip_r - 4.0 - char_w * 0.5
                    _gr = (_band_l, ly, _gut_clip_r, ly + line_px)
                    if not _draw_gutter_widget(line_idx, ly, left + gutter_w - 3.0):
                        draw_list.add_text(nx, ly, cur_color if line_idx == cur_line
                                           else num_color, num_str)
                elif heat:
                    # Heat chip exists (right half of the cell): arrow sits
                    # left of it with breathing room, and the hit region stops
                    # at the chip so clicking it still opens the usage box
                    # instead of toggling the fold.
                    _chip_l = ((nx - 3.0) + (left + gutter_w - 3.0)) * 0.5
                    _gcx = max(_chip_l - 8.0, left + _lv_btn_w + 5.0)
                    _gr = (left + _lv_btn_w, ly, _chip_l - 2.0, ly + line_px)
                else:
                    # No chip: right-align the arrow with the line numbers;
                    # a gutter widget takes the cell left of the arrow.
                    _gcx = left + gutter_w - 6.0 - char_w
                    _gr = (left + _lv_btn_w, ly, left + gutter_w, ly + line_px)
                    if _draw_gutter_widget(line_idx, ly, _gcx - 8.0):
                        _gr = (_gcx - 6.0, ly, left + gutter_w, ly + line_px)
                _ghov = (_gr[0] <= io.mouse_pos.x < _gr[2]
                         and _gr[1] <= io.mouse_pos.y < _gr[3])
                # Diff folds wear their own tint so the two fold kinds read
                # apart in the strip (scope folds stay the neutral grey).
                # A snapshot-replayed row (range=None) is a diff header when
                # the stand-in's diff rows say so.
                _is_diff_g = (_rng_g in _diff_rng_set if _rng_g is not None
                              else (_restore_diff is not None
                                    and line_idx in _restore_diff))
                if _is_diff_g:
                    _dft = Toggles.TextEditor.diff_fold_tint
                    _gcc = imgui.get_color_u32_rgba(
                        *_dsep_rgb[:3], min(1.0, _dft[3] + (0.35 if _ghov else 0.0)))
                else:
                    _gcc = imgui.get_color_u32_rgba(
                        0.9, 0.9, 0.9, 0.55 if _ghov else 0.31)
                _gcy = ly + line_px * 0.5
                if _col_g and _is_diff_g:
                    # Collapsed diff gap: the chevron sits ON the separator
                    # band under the header row (the badge pass below it at
                    # the row's bottom edge), not at the row's middle.
                    _gcy = ly + line_px - _diff_band_h * 0.5
                if _col_g:
                    # right-pointing chevron: click to expand
                    draw_list.add_triangle_filled(_gcx - 2.5, _gcy - 4.0,
                                                  _gcx - 2.5, _gcy + 4.0,
                                                  _gcx + 3.5, _gcy, _gcc)
                else:
                    # down-pointing chevron: click to collapse
                    draw_list.add_triangle_filled(_gcx - 4.0, _gcy - 2.5,
                                                  _gcx + 4.0, _gcy - 2.5,
                                                  _gcx, _gcy + 3.5, _gcc)
                if _rng_g is not None:   # None = no replay, paint-only
                    ds._fold_badge_rects.append((_gr, _rng_g))
            elif ((line_idx in _err_by_line or line_idx in _gutter_views)
                  and _draw_gutter_widget(line_idx, ly, left + gutter_w - 3.0)):
                pass    # a gutter widget took the number cell
            else:
                # Plain line: the number (a gutter widget takes its cell -
                # def lines are fold headers, so they mostly land above).
                # A diff-preview row fades its number with its glyphs.
                _num_col = cur_color if line_idx == cur_line else num_color
                if _preview_lines is not None and line_idx in _preview_lines:
                    _num_col = _fade_packed(_num_col, _preview_alpha)
                draw_list.add_text(nx, ly, _num_col, num_str)
            _mlist = _lv_marks.get(line_idx)
            if _mlist:
                _open = any(getattr(m, "_lv_open", False) for m in _mlist)
                # Every marker on the line renders its value INLINE (simple
                # builtins - see draw_live_view_marker's inline=True): no
                # window to open or close, so the cell shows an inert info
                # glyph instead of the magnifier toggle.
                _inline_all = all(getattr(m, "_lv_inline", False)
                                  for m in _mlist)
                # Hit zone: the FULL button cell (whole column width x whole
                # line height), not just the glyph - the icon itself is tiny.
                _bhov = (not _inline_all
                         and left <= io.mouse_pos.x < left + _lv_btn_w
                         and ly <= io.mouse_pos.y < ly + line_px)
                # Icon tint defaults to the LINE's color: the definition tint when
                # the line has one (same adjust as the heat box), else the
                # number color this line is drawn with.
                _blt = _dt_line_map.get(line_idx)
                if _blt is not None:
                    _bga = _bg_adjust(tuple(_blt[1][:3]), _bg_f)
                    _bc = imgui.get_color_u32_rgba(_bga[0], _bga[1], _bga[2], 1.0)
                else:
                    _bc = cur_color if line_idx == cur_line else num_color
                _bcx = left + _lv_btn_w * 0.5
                _bcy = ly + line_px * 0.5
                if _bhov:
                    draw_list.add_rect_filled(
                        left + 1.0, ly + 1.0, left + _lv_btn_w - 1.0,
                        ly + line_px - 1.0,
                        imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.10), 3.0)
                if _inline_all:
                    # info icon: circle + dot + stem - the value is already
                    # shown inline, nothing to open.
                    _br = 3.6
                    draw_list.add_circle(_bcx, _bcy, _br, _bc, 12, 1.2)
                    draw_list.add_circle_filled(_bcx, _bcy - 1.6, 0.8, _bc)
                    draw_list.add_line(_bcx, _bcy - 0.1,
                                       _bcx, _bcy + 2.0, _bc, 1.2)
                elif _open:
                    # × close button
                    _br = 3.5
                    draw_list.add_line(_bcx - _br, _bcy - _br,
                                       _bcx + _br, _bcy + _br, _bc, 1.6)
                    draw_list.add_line(_bcx - _br, _bcy + _br,
                                       _bcx + _br, _bcy - _br, _bc, 1.6)
                else:
                    # inspect icon: magnifier (circle + handle)
                    _br = 2.8
                    _bgx, _bgy = _bcx - 1.2, _bcy - 1.2
                    draw_list.add_circle(_bgx, _bgy, _br, _bc, 12, 1.4)
                    _bhx = _br * 0.707
                    draw_list.add_line(_bgx + _bhx, _bgy + _bhx,
                                       _bgx + _br + 2.6, _bgy + _br + 2.6,
                                       _bc, 1.4)
                if getattr(ds, "_lv_btn_pressed_line", None) == line_idx:
                    ds._lv_btn_pressed_line = None
                    if not _inline_all:
                        from src.lsd.gl_gui.view.core_views.live_view_views import (
                            set_marker_open)
                        for m in _mlist:
                            set_marker_open(m, not _open)
                        ds.invalidate()
                        request_render()
                    
                
        # An unconsumed press stash dies with the pass - a press on a line
        # whose marker disappeared must not fire on a later frame's layout.
        ds._lv_btn_pressed_line = None
        # Off-screen markers: clamp their buttons to the strip's top /
        # bottom row (still on the gutter) with an arrow toward the line, so
        # an error anywhere in the file is one click away.
        if _err_by_line:
            _eb_x1 = left + gutter_w - 3.0
            _eb_up = [l for l in _err_by_line if origin_y + (l + 1) * line_px < gutter_top]
            _eb_dn = [l for l in _err_by_line if origin_y + l * line_px > rect_max_y]
            if _eb_up:
                _draw_error_button(max(_eb_up), gutter_top + 2.0, _eb_x1,
                                   arrow=error_up_icon)
            if _eb_dn:
                _draw_error_button(min(_eb_dn), rect_max_y - line_px - 2.0, _eb_x1,
                                   arrow=error_down_icon)
        draw_list.pop_clip_rect()

    # --- Fold labels ---------------------------------------------------------
    # The fold chevrons live in the GUTTER (in place of the header's line
    # number - see the gutter pass above); here a collapsed fold keeps its
    # "N lines" label at the end of the header (also a click target), and a
    # gutterless editor falls back to end-of-line chevrons so folds stay
    # reachable. Rects are stashed for NEXT frame's toggle handler at the
    # top of the body (raw draw-list widgets, same tech as the live-view
    # gutter markers - a render_func per fold would be overkill); the list
    # was created above during gutter pass.
    if _fold_folds:
        draw_list.push_clip_rect(left + gutter_w, rect_min_y,
                                 left + ds.content_width, rect_max_y, True)
        _fm_y = (line_px - imgui.get_text_line_height()) * 0.5
        _need_chev = gutter_w <= 0.0    # no gutter: chevrons fall back here
        # Collapsed diff gap separator band (color resolved beside
        # _diff_band_h at the preview-rows block).
        _dsep_col = imgui.get_color_u32_rgba(*_dsep_rgb[:3], 0.35)
        # A def fold header is widened by the run buttons trailing the def's
        # name (the def_name token view's trail_cells); the badge - placed
        # from the header's CHAR length - shifts with them.
        _def_tv = token_views.get('def_name') if token_views else None
        _trail_px = (_def_tv.get("trail_cells", 0) * char_w
                     if isinstance(_def_tv, dict) else 0.0)
        # Folds are in display-line order; bisect to the visible band rather
        # than the every fold in the file (2.5k in a 12k-line file).
        _fdl_c = getattr(ds, '_fold_dl_cache', None)
        if _fdl_c is None or _fdl_c[0] is not _fold_folds:
            _fdl_c = (_fold_folds, [_f[1] for _f in _fold_folds])
            ds._fold_dl_cache = _fdl_c
        _fv0 = int((rect_min_y - origin_y) // line_px) - 2
        _fv1 = int((rect_max_y - origin_y) // line_px) + 1
        for _rng, _dl, _fcol, _nh, _hlen, _fa, _fhl in _fold_folds[
                bisect.bisect_left(_fdl_c[1], _fv0):
                bisect.bisect_right(_fdl_c[1], _fv1)]:
            _fy = origin_y + _dl * line_px
            if _fy > rect_max_y or _fy + 2 * line_px < rect_min_y:
                continue
            if _hide_expanded_diff and not _fcol and _rng in _diff_rng_set:
                continue    # expanded diff span under expand-all: no badge
            _bx = origin_x + _hlen * char_w + char_w
            if _trail_px:
                # Header line text = the _hlen chars ending at the anchor.
                _hl_s = text[max(0, _fa - _hlen):_fa].lstrip()
                if _hl_s.startswith('def ') or _hl_s.startswith('async def '):
                    _bx += _trail_px

            _is_diff_fold = _rng in (getattr(ds, '_diff_rng_set', None) or ())
            if _fcol and not _need_chev and not _is_diff_fold:
                # Gutter owns the chevron and the "N lines" label is not
                # drawn (see below): no badge rect here - an INVISIBLE
                # badge after the header text meant that placing the caret
                # at the end of the line toggled the fold.
                continue
            if _fcol:
                # [tint=(0.656, 0.044, 0.615), show_tint=True]
                _lbl = f"{_nh} lines"
                _bw = (16.0 if _need_chev else 4.0) + len(_lbl) * char_w + 8.0
            elif _need_chev:
                _lbl = None
                _bw = 16.0
            else:
                continue    # expanded + no chevron: nothing on the line
            _fr = (_bx, _fy + 1.0, _bx + _bw, _fy + line_px - 1.0)
            _fhov = (_fr[0] <= io.mouse_pos.x < _fr[2]
                     and _fr[1] <= io.mouse_pos.y < _fr[3])

            if _is_diff_fold:
                _dft = Toggles.TextEditor.diff_fold_tint
                _fcc = imgui.get_color_u32_rgba(
                    *_dsep_rgb[:3], min(1.0, _dft[3] + (0.3 if _fhov else 0.0)))
                if _fcol:
                    # Collapsed diff gap: a thin separator line across the
                    # row under the header - hidden UNCHANGED code, visually
                    # distinct from a folded scope - in the file's tint.
                    _dby = _fy + line_px - _diff_band_h
                    draw_list.add_rect_filled(
                        left + gutter_w, _dby, left + ds.content_width,
                        _dby + _diff_band_h, _dsep_col)
            else:
                _fcc = imgui.get_color_u32_rgba(
                    0.9, 0.9, 0.9, 0.4 if _fhov else 0.31)
            _fcx, _fcy = _fr[0] + 8.0, (_fr[1] + _fr[3]) * 0.5
            if _is_diff_fold and _fcol:
                # The chevron sits ON the separator band (see the gutter
                # pass for the same rule).
                _fcy = _fy + line_px - _diff_band_h * 0.5
            if _need_chev:
                if _fcol:
                    # right-pointing chevron: click to expand
                    draw_list.add_triangle_filled(_fcx - 2.5, _fcy - 4.0,
                                                  _fcx - 2.5, _fcy + 4.0,
                                                  _fcx + 3.5, _fcy, _fcc)
                else:
                    # down-pointing chevron: click to collapse
                    draw_list.add_triangle_filled(_fcx - 4.0, _fcy - 2.5,
                                                  _fcx + 4.0, _fcy - 2.5,
                                                  _fcx, _fcy + 3.5, _fcc)
            # Add this if you want a display of the number of collapsed lines
            # if _fcol:
            # #     draw_list.add_text(_fr[0] + (16.0 if _need_chev else 4.
            #                        _fy + _fm_y, _fcc, _lbl)
            # Collapsed diff gaps DO display their count - "N lines" in the
            # diff tint beside the badge, part of the fold styling.
            if _is_diff_fold and _fcol:
                draw_list.add_text(_fr[0] + (16.0 if _need_chev else 4.0),
                                   _fy + _fm_y, _fcc, _lbl)
            ds._fold_badge_rects.append((_fr, _rng))
        draw_list.pop_clip_rect()
    elif _restore_diff:
        # Stand-in frames: the fold layer is off, so replay the snapshot of
        # diff-gap chrome - the separator band + "N lines" - for every
        # COLLAPSED gap header in the visible band (same geometry and order
        # as the live pass above; the gutter pass painted the chevron on
        # the band). Paint-only: no badge rects, nothing to toggle. The
        # header's length comes from the stand-in's own line (the band text
        # IS the display text), so the label lands where the live pass will.
        draw_list.push_clip_rect(left + gutter_w, rect_min_y,
                                 left + ds.content_width, rect_max_y, True)
        _fm_y = (line_px - imgui.get_text_line_height()) * 0.5
        _need_chev = gutter_w <= 0.0
        _dsep_col = imgui.get_color_u32_rgba(*_dsep_rgb[:3], 0.35)
        _fcc = imgui.get_color_u32_rgba(
            *_dsep_rgb[:3], Toggles.TextEditor.diff_fold_tint[3])
        _rd_offs = _line_starts(text)
        _fv0 = int((rect_min_y - origin_y) // line_px) - 2
        _fv1 = int((rect_max_y - origin_y) // line_px) + 1
        for _rd_line in sorted(_restore_diff):
            if _rd_line < _fv0 or _rd_line > _fv1 or _rd_line >= len(_rd_offs):
                continue
            _rd_n = _restore_diff[_rd_line]
            if not _rd_n:
                continue    # expanded gap: gutter chevron only
            _fy = origin_y + _rd_line * line_px
            _dby = _fy + line_px - _diff_band_h
            draw_list.add_rect_filled(
                left + gutter_w, _dby, left + ds.content_width,
                _dby + _diff_band_h, _dsep_col)
            _rd_end = (_rd_offs[_rd_line + 1] - 1
                       if _rd_line + 1 < len(_rd_offs) else len(text))
            _hlen = _rd_end - _rd_offs[_rd_line]
            _bx = origin_x + _hlen * char_w + char_w
            if _need_chev:
                _fcy = _fy + line_px - _diff_band_h * 0.5
                draw_list.add_triangle_filled(_bx + 5.5, _fcy - 4.0,
                                              _bx + 5.5, _fcy + 4.0,
                                              _bx + 11.5, _fcy, _fcc)
            draw_list.add_text(_bx + (16.0 if _need_chev else 4.0),
                               _fy + _fm_y, _fcc, f"{_rd_n} lines")
        draw_list.pop_clip_rect()

    if changed:
        text_height = (text.count('\n') + 1) * line_px + 2
    else:
        text_height = len(_line_starts(input_value)) * line_px + 2

    # text_width = max(vcols) if vcols else max((len(l) for l in text.split('\n')), default=0) * char_w

    _pf("gutter")
    if jump_to is not None and not single_line and not is_search_box:
        # --- Usage-graph source (debug label, at top-right) ------------
        # Where this span's symbol-usage graph came from: "fresh" (full
        # recompute this session), "disk" (pickle warm-start), "sys" (adopted
        # across a restart-in-place) - plus "+Ni" for N incremental passes on
        # that base. Reads the provenance map libcst_conversion maintains at
        # each store; "none" = no tracked span covers this view yet.
        if (Toggles.TextEditor.SymbolUsages.show_usage_graph_source
                and getattr(jump_to, 'path', None) is not None):
            from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
                usage_graph_source)
            _ug_start = (getattr(jump_to, 'start', 0) or 0) + 1
            _ug_txt = usage_graph_source(
                str(jump_to.path), _ug_start,
                _ug_start + _fold_full.count('\n'))
            _ug_dot = {"fresh": (0.25, 0.85, 0.35, 0.95),
                       "disk": (0.9, 0.65, 0.15, 0.9),
                       "sys": (0.35, 0.6, 0.9, 0.9)}.get(
                (_ug_txt or "").split("+")[0], (0.5, 0.5, 0.5, 0.6))
            _ug_txt = _ug_txt or "none"
            _ug_x1 = rect_max_x - 8.0
            _ug_y0 = rect_min_y + 4.0
            _ug_w = len(_ug_txt) * 7.5 + 20.0
            draw_list.add_rect_filled(
                _ug_x1 - _ug_w, _ug_y0, _ug_x1, _ug_y0 + 17.0,
                imgui.get_color_u32_rgba(0.08, 0.08, 0.08, 0.6), 8.5)
            draw_list.add_circle_filled(_ug_x1 - _ug_w + 9.0, _ug_y0 + 8.5, 3.5,
                                        imgui.get_color_u32_rgba(*_ug_dot))
            draw_list.add_text(_ug_x1 - _ug_w + 16.0, _ug_y0 + 1.5,
                               imgui.get_color_u32_rgba(0.85, 0.85, 0.85, 0.85),
                               _ug_txt)

    # Icon-picker orphan close: the picker popover is latched by its icon
    # picker's body (draw_icon_selector_plain) - if that widget stopped
    # rendering this frame (token scrolled out, edited away, sort-order name
    # shifted) the menu popover would keep its last closed=False stamp forever
    # and float on as a ghost. The editor owns the latch state, so close it
    # here whenever the open widget wasn't seen this frame.
    _io_name = getattr(ds, '_icon_open_name', None)
    if _io_name is not None:
        _io_seen = getattr(ds, '_icon_seen', None)
        if not (_io_seen and _io_seen[0] == _io_name
                and _io_seen[1] == Melty.frame_count):
            _io_menu = (getattr(ds, '_icon_menus', None) or {}).get(_io_name)
            if _io_menu is not None:
                _io_menu.closed = True
                if Melty.popover_focused_ds is _io_menu:
                    Melty.popover_focused_ds = None
            _io_root = getattr(ds, '_icon_dd_root', None)
            if _io_root is not None:
                from src.lsd.gl_gui.view.core_views.new_core_view import _dd_close
                _dd_close(_io_root)   # collapse paths / release the box's text focus
            ds._icon_open_name = None
            request_render()

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
    # Row colors from the definition-tint pass: a candidate whose symbol
    # carries a tint renders its row in that color, matching the editor's
    # washes. The name→int map rides the cached _def_tints result already
    # computed this frame (len guard: a differently-shaped tuple may linger on
    # a pre-hotswap draw_state); resolving here is one dict lookup per row.
    _dt = getattr(ds, '_def_tints', None)
    _nt = _dt[3] if _ac_show and _dt is not None and len(_dt) == 4 else None
    _mt = getattr(ds, '_ac_member_tints', None) if _ac_show else None
    # Snippet rows carry their own author-set tint (Snippet.tint) - it wins
    # over the symbol maps (a snippet label isn't a symbol).
    _sn = getattr(ds, '_ac_snips', None) if _ac_show else None
    _ac_tints = None
    if _nt or _mt or _sn:
        _ac_tints = {}
        for n in _ac_cands:
            # The member map (receiver's defining file) wins - it's exact for
            # the receiver, while the namespace map's dotted-name last-segment
            # lookup is only a guess for bare member names.
            t = None
            if _sn:
                _s = _sn.get(n)
                t = tuple(_s.tint[:3]) if _s is not None and getattr(_s, 'tint', None) else None
            t = t or (_mt.get(n) if _mt else None) or (_nt.get(n) if _nt else None)
            if t is not None:
                _ac_tints[n] = t
        _ac_tints = _ac_tints or None
    # Draw FIM ghost text (fim.py): the visible chunk under the caret, extra
    # lines in an anchor below, drawn while the mono font is still pushed.
    _fim_ghost = getattr(ds, '_fim_ghost', None) if is_focused else None
    if _fim_ghost is not None and (_fim_ghost.text or _fim_ghost.pending):
        _draw_fim_ghost(ds, _fim_ghost, text, origin_x, origin_y, line_px, vcols)

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
        _pop_ds = Melty.cache.key_to_draw_state.get(getattr(ds, '_ac_menu_tile', None))
        if _pop_ds is not None and _pop_ds.width and _pop_ds.height:
            # REAL window rect (live abs pos - cached abs lags a frame during a
            # parent-window drag). Survives a user resize; a hardcoded estimate
            # here left a narrow band where _kbd_mode was re-forced every frame,
            # so rows outside it never hover-highlighted.
            _px0, _py0 = _pop_ds._abs_left(), _pop_ds._abs_top()
            _over = (_px0 - 4 <= _mp[0] <= _px0 + _pop_ds.width + 4
                     and _py0 - 2 <= _mp[1] <= _py0 + _pop_ds.height)
        else:
            # First-open-frame fallback before the popup's tile id is found.
            _pop_h = min(len(_ac_cands) * 24 + 10, 800)        # ~row height, cap
            _over = (_pop_x0 - 4 <= _mp[0] <= _pop_x0 + 400
                     and _pop_y0 - 2 <= _mp[1] <= _pop_y0 + _pop_h)
        _moved = _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5)
        if not _over:
            ac_state._kbd_mode = True       # mouse away → keyboard selection shown
        elif _moved:
            ac_state._kbd_mode = False      # actively moving over it → hover drives
        # over + resting → leave as-is (so an arrow's _kbd_mode=True persists)
        ac_state._last_mouse = (_mp[0], _mp[1])

    imgui.dummy(draw_state.content_width, max(draw_state._kwargs.get("min_height", 0), text_height))

    # draw_dd_menu is a LATCHED window: called every frame with closed=not _ac_show
    # so it persists when this (cached) body is skipped. Hover/keys wake the loop;
    # background results wake it via the future's done-callback (_wake_on_future).
    # [tint=(0.867, 0.255, 0.255), show_tint=True]
    ac_changed, ac_pick, _ac_menu_ds = draw_dd_menu(
        _ac_items, name=f"{ds.name}_ac_menu", view_offset=False,
        temp=True, show_search=False, swoosh=False, closed=not _ac_show, max_height=800,
        window_pos=(_ac_x - draw_state.abs_left, _ac_y - draw_state.abs_top + line_px), text_align="left",
        row_tags=(getattr(ds, '_ac_kinds', None) if _ac_show else None),
        row_tints=(_ac_tints or None),
        row_suffixes=(getattr(ds, '_ac_params', None) if _ac_show else None),
        parent_window=draw_state, root_state=ac_state, path_prefix=(),
        return_extras=True)


    # Latch the popup's exact tile id from the call itself (return_extras hands
    # back its draw_state on every wrapper path, including closed/deferred). The
    # old name-prefix scan of cache._tiles mis-latched ANOTHER editor's popup
    # whenever they share a prefix - every RenderHost editor is named "value" -
    # and since the wrong popup stayed live the latch never healed: that editor's
    # popup only repainted while hovered. Restamped every call, so a rebuilt
    # tile or renamed editor re-latches automatically.
    if _ac_menu_ds is not None:
        ds._ac_menu_tile = _ac_menu_ds._tile_id
    # Hover may have moved the menu's cursor (when the mouse is over it); mirror
    # that back into our selection index so Enter/arrows continue from the hovered row.
    if _ac_show and not ac_state._kbd_mode:
        _cp = ac_state.cursor_path
        if isinstance(_cp, tuple) and len(_cp) == 1 and _cp[0] in _ac_cands:
            ds._ac_index = _ac_cands.index(_cp[0])
    # The popup is a CACHED latched window with no kwargs cache key - repaints
    # happen only via explicit invalidation. Fire it ONLY on a real change edge:
    # the candidate list / kind tags (typing, jedi landing), the keyboard row,
    # or the highlight mode. Hover repaints need nothing here (a currently-
    # hovered tile re-renders every frame), so a resting pointer or held key
    # costs zero invalidates. Must be invalidate_up - it cascades to child
    # tiles (a plain invalidate leaves inner collections due to blit-skip).
    # It lands before the parent window dispatch on end_frame, so the menu
    # repaints the same frame; request_render backstops bad orderings.
    if _ac_show:
        _sig = (ds._ac_candidates, ds._ac_kinds, ds._ac_index, _ac_tints,
                bool(getattr(ac_state, '_kbd_mode', True)))
        if _sig != getattr(ds, '_ac_menu_sig', None):
            ds._ac_menu_sig = _sig
            _mt = getattr(ds, '_ac_menu_tile', None)
            if _mt is not None:
                Melty.cache.invalidate_up(_mt, force=True)
                request_render()
    else:
        ds._ac_menu_sig = None   # force one repaint on the next open
    if ac_changed and isinstance(ac_pick, str):
        anchor = ds._ac_anchor
        _ins, _coff, _extra = _ac_pick_insert(
            ds, ac_pick, following=text[ds.text_cursor_pos:ds.text_cursor_pos + 64],
            preceding=text[max(0, anchor - 64):anchor],
            replaced=text[anchor:ds.text_cursor_pos],
            line_prefix=text[_get_line_start(text, anchor):anchor])
        text = text[:anchor] + _ins + text[ds.text_cursor_pos:]
        ds.text_cursor_pos = anchor + _coff
        ds._ac_tabstops = [len(text) - (anchor + s) for s in _extra] or None
        text = _ac_apply_auto_import(ds, ac_pick, jump_to, text)
        ds.text_selection_start = ds.text_cursor_pos
        ds.text_selection_end = ds.text_cursor_pos
        ds._ac_open = False
        ds._ac_request_anchor = -1
        ds._ac_snip_site = None   # same disarm as a keyboard accept
        changed = True

    _pf("ac_popup")
    # --- Usage-jump picker (multi-use symbols) ---
    # Same latched window contract as the suggestion popup above: draw_dd_menu
    # is called EVERY frame with closed= toggled. Rows are the symbol's users
    # ({scope_id: UsageRef}, with the text as the dim row tag); a pick - mouse
    # or Enter (handled in the key block) - opens that site in IntelliJ.
    _uj_show = ((Melty.text_focused_ds is draw_state
                 or _focus_in_context_menu_over(draw_state))
                and getattr(ds, '_uj_open', False)
                and bool(getattr(ds, '_uj_items', None)))
    # Edge-log the "flagged open but not shown" state - an invisible-but-open
    # picker still gates Ctrl+B off (its `not _uj_open` check), which looks
    # exactly like "the shortcut is broken".
    _uj_hidden = getattr(ds, '_uj_open', False) and not _uj_show
    if _uj_hidden != getattr(ds, '_uj_hidden_prev', False):
        ds._uj_hidden_prev = _uj_hidden
        if _uj_hidden:
            _uj_log(f"picker OPEN-BUT-HIDDEN {ds.name!r} "
                    f"focus_owner={getattr(Melty.text_focused_ds, 'name', None)!r} "
                    f"items={len(getattr(ds, '_uj_items', None) or {})}")
    _uj_items = ds._uj_items if _uj_show else {}
    _uj_anchor = getattr(ds, '_uj_anchor', ds.text_cursor_pos)
    _uj_gut = getattr(ds, '_uj_anchor_gutter', None)
    if _uj_gut is not None and show_gutter and gutter_w > 0:
        # Gutter-opened picker docks beside the clicked heat box: right of
        # the gutter bar, top aligned to the line. Downside both the
        # window_pos and the first-open hover fallback add line_px to
        # _uj_y (the under-the-symbol convention), so subtract one line high.
        _uj_x = left + gutter_w + 4.0
        _uj_y = origin_y + (_uj_gut - 1) * line_px
    else:
        _uj_x, _uj_y = _char_pos_to_xy(text, _uj_anchor, origin_x, origin_y, line_px, vcols=vcols)
    if _uj_show:
        # Keyboard-vs-hover highlight: same dance as the suggestion popup -
        # keyboard selection shows unless the mouse actively MOVES over the
        # popup; a resting pointer never steals the highlight.
        _mp = imgui.get_mouse_pos()
        _lm = getattr(uj_state, '_last_mouse', None)
        _pop_x0, _pop_y0 = _uj_x, _uj_y + line_px
        _pop_ds = Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None))
        if _pop_ds is not None and _pop_ds.width and _pop_ds.height:
            # REAL window rect - the picker is auto-resize (and its 500px
            # min-width already exceeded the fallback 400px estimate, leaving a
            # hover-dead right strip). See the AC popup note above.
            _px0, _py0 = _pop_ds._abs_left(), _pop_ds._abs_top()
            _over = (_px0 - 4 <= _mp[0] <= _px0 + _pop_ds.width + 4
                     and _py0 - 2 <= _mp[1] <= _py0 + _pop_ds.height)
        else:
            # First-open-frame fallback before the picker's tile id is known.
            _pop_h = min(len(_uj_items) * 24 + 10, 312)
            _over = (_pop_x0 - 4 <= _mp[0] <= _pop_x0 + 680
                     and _pop_y0 - 2 <= _mp[1] <= _pop_y0 + _pop_h)
        _moved = _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5)
        if not _over:
            uj_state._kbd_mode = True
        elif _moved:
            uj_state._kbd_mode = False
        uj_state._last_mouse = (_mp[0], _mp[1])

    # Per-user file tints (the ref's FileMeta color, same as the editor tabs).
    _uj_row_tints = ({v: _uj_file_tint(getattr(v, 'path', None))
                      for v in _uj_items.values()} if _uj_show else None)
    # [tint=(0.071, 0.354, 0.511), show_tint=True]
    uj_changed, uj_pick, _uj_menu_ds = draw_dd_menu(
        _uj_items, name=f"{ds.name}_uj_menu", view_offset=False, show_bg=True,
        temp=True, show_search=False, swoosh=False, closed=not _uj_show, bg_offset=0, min_width=680,
        window_pos=(_uj_x - draw_state.abs_left, _uj_y - draw_state.abs_top + line_px), text_align="left",
        row_tags=(getattr(ds, '_uj_tags', None) if _uj_show else None), mode=None,
        row_tints=_uj_row_tints,
        row_code=(getattr(ds, '_uj_code', None) if _uj_show else None),
        parent_window=draw_state, root_state=uj_state, path_prefix=(), tint=(0.06, 0.08277813, 0.13),
        return_extras=True)


    # Exact tile id from the call above - the old name-prefix scan mis-landed
    # across same-named editors (see the AC popup note above).
    if _uj_menu_ds is not None:
        ds._uj_menu_tile = _uj_menu_ds._tile_id

    # Mirror a hover-moved cursor back into the keyboard index so Enter/arrows
    # continue from the hovered row.
    if _uj_show and not uj_state._kbd_mode:
        _cp = uj_state.cursor_path
        if isinstance(_cp, tuple) and len(_cp) == 1 and _cp[0] in _uj_items:
            ds._uj_index = list(_uj_items).index(_cp[0])
    # Change-gated repaint - one invalidate per real change edge (row swap,
    # arrow nav, keyboard-mode flip), zero on parked idle frames. Same
    # design as the AC popup block above.
    if _uj_show:
        _sig = (ds._uj_items, getattr(ds, '_uj_index', 0),
                bool(getattr(uj_state, '_kbd_mode', True)))
        if _sig != getattr(ds, '_uj_menu_sig', None):
            ds._uj_menu_sig = _sig
            _mt = getattr(ds, '_uj_menu_tile', None)
            if _mt is not None:
                Melty.cache.invalidate_up(_mt, force=True)
                request_render()
    else:
        ds._uj_menu_sig = None   # force one repaint on the next open
    # Ignore a mouse pick landing on the picker's very open frame(s): the
    # window is LATCHED, so on a re-open it can draw one frame at its stale
    # previous position/contents - a click there replayed the LAST session's
    # row (seen as "gutter click instantly jumps to the previously-jumped
    # file"). Real picks always come ≥2 frames after the open.
    if uj_changed:
        _uj_log(f"pick changed={uj_changed} "
                f"ref={getattr(getattr(uj_pick, 'path', None), 'name', None)}:"
                f"{getattr(uj_pick, 'line', None)} "
                f"open_age={Melty.frame_count - getattr(ds, '_uj_open_frame', -99)}")
    if (uj_changed and getattr(uj_pick, 'path', None) is not None
            and Melty.frame_count - getattr(ds, '_uj_open_frame', -99) > 1):
        _goto_usage_ref(uj_pick, token=(getattr(ds, '_uj_names', None)
                                        or {}).get(uj_pick))
        ds._uj_open = False

    # --- Import quick-fix chooser --- same latched-window contract as the two
    # popups above: draw_dd_menu called EVERY frame with closed= toggled. Rows
    # are the candidate import statements for the caret line's missing name;
    # a pick - mouse or Enter (handled in the key block) - applies the fix.
    _qf_show = (Melty.text_focused_ds is draw_state
                and getattr(ds, '_qf_open', False)
                and bool(getattr(ds, '_qf_options', None)))
    _qf_items = {s: s for s in (ds._qf_options if _qf_show else [])}
    _qf_anchor = getattr(ds, '_qf_anchor', ds.text_cursor_pos)
    _qf_x, _qf_y = _char_pos_to_xy(text, _qf_anchor, origin_x, origin_y, line_px, vcols=vcols)
    qf_changed, qf_pick, _qf_menu_ds = draw_dd_menu(
        _qf_items, name=f"{ds.name}_qf_menu", view_offset=False,
        temp=True, show_search=False, swoosh=False, closed=not _qf_show, max_height=400,
        window_pos=(_qf_x - draw_state.abs_left, _qf_y - draw_state.abs_top + line_px),
        text_align="left", parent_window=draw_state, root_state=qf_state,
        path_prefix=(), return_extras=True)
    if _qf_menu_ds is not None:
        ds._qf_menu_tile = _qf_menu_ds._tile_id
    # Change-gated repaint - one invalidate per real change edge (options swap,
    # arrow nav); same design as the popups above.
    if _qf_show:
        _sig = (tuple(ds._qf_options), getattr(ds, '_qf_index', 0))
        if _sig != getattr(ds, '_qf_menu_sig', None):
            ds._qf_menu_sig = _sig
            _mt = getattr(ds, '_qf_menu_tile', None)
            # if _mt is not None:
                # Melty.cache.invalidate_up(_mt, force=True)
                # request_render()
    else:
        ds._qf_menu_sig = None      # force one repaint on the next open
    if qf_changed and isinstance(qf_pick, str):
        _fx_changed, _fx_text = _apply_import_fix(qf_pick, jump_to, text)
        from src.lsd.gl_gui.view.core_conversion.chain_converters import _import_bound_name
        if getattr(ds, '_qf_applied', None) is None:
            ds._qf_applied = set()
        ds._qf_applied.add(_import_bound_name(qf_pick))
        if _fx_changed:
            ds.text_cursor_pos += len(_fx_text) - len(text)
            text = _fx_text
            changed = True
        ds._qf_open = False
        request_render()

    if _font_pushed:
        imgui.pop_font()

    _pf("uj_picker")
    # --- Floating error box pinned to the bottom of the view ---
    # The first error message floats inside a box sitting flush ABOVE the
    # offending line, aligned flush to the editor's right edge, so it never
    # covers the line it describes. Long messages wrap inside a capped-width
    # box. Drawn after the body (and after the monospace font pop, so it uses
    # the default UI font); save the cursor, paint, restore, layout untouched.
    _err_open_line = getattr(ds, '_err_open_line', None)
    _err_open_msg = (dict((l - 1, m) for l, m in _err_markers).get(_err_open_line)
                     if _err_markers and _err_open_line is not None else None)
    if jump_to is not None and _err_msg and _err_open_msg is not None:
        _save_cursor = imgui.get_cursor_screen_pos()
        clip_l, clip_t, clip_r, clip_b = draw_state.abs_clip_rect
        # Right margin clears the overlay scrollbar (it draws over the
        # content edge) plus breathing room — the old 6 px sat under it.
        # [tint=(0.95, 0.55, 0.35)]
        error_box_right_margin = 26
        pad_x, pad_y, margin = 8, 5, 6
        # The box shows the CLICKED marker's FULL message (every marker has
        # a gutter icon); a click on the box copies it.
        msg = str(_err_open_msg).rstrip()
        max_w = min(520.0, max(80.0, (clip_r - clip_l) - 2 * pad_x
                                       - margin - error_box_right_margin))
        _ts = imgui.calc_text_size(msg, False, max_w)
        box_w = _ts.x + 2 * pad_x
        box_h = _ts.y + 2 * pad_y
        bx1 = clip_r - error_box_right_margin
        bx0 = bx1 - box_w
        # Anchor flush against the error line's top (no gap); if the line sits
        # too close to the viewport top for the box to fit, flip it below.
        _err_line_top = origin_y + _err_open_line * line_px
        by1 = min(_err_line_top, clip_b - margin)
        by0 = by1 - box_h
        if by0 < clip_t + margin:
            by0 = min(_err_line_top + line_px, clip_b - margin - box_h)
            by1 = by0 + box_h
        # Keep the box inside the viewport vertically.
        by0 = max(clip_t + margin, min(by0, clip_b - margin - box_h))
        by1 = by0 + box_h
        fill_col = (0.275, 0.118, 0.157, 0.922)
        line_col = (0.588, 0.235, 0.275, 1.0)
        err_draw_list = imgui.get_window_draw_list()
        err_draw_list.add_rect_filled(bx0, by0, bx1, by1, imgui.get_color_u32_rgba(*fill_col), 4.0)
        err_draw_list.add_rect(bx0, by0, bx1, by1, imgui.get_color_u32_rgba(*line_col), 4.0)
        imgui.set_cursor_screen_pos((bx0 + pad_x, by0 + pad_y))
        imgui.push_text_wrap_pos(imgui.get_cursor_pos_x() + max_w)
        imgui.text_colored(msg, 1.0, 0.72, 0.68, 1.0)
        imgui.pop_text_wrap_pos()
        imgui.set_cursor_screen_pos(_save_cursor)
        # Click on the message = copy it (draw_button's routing: an on_action
        # claim on the editor's draw_state, bypassed on cache hits). A press
        # here must not move the caret either.
        ds._plain_tv_rects.append((bx0, by0, bx1, by1))
        if ds.on_action("left_mouse_clicked", view_id="err_box",
                        rect=(bx0, by0, bx1, by1), priority_delta=4,
                        cursor=mouse_cursor.ARROW) is not None:
            imgui.set_clipboard_text(msg)
            from src.lsd.gl_gui.notifications import notify
            notify("Error message copied", tag="error_copy")
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
    # Reassemble the FULL text once for everything downstream: the fast
    # syntax/import checks must never compile the fold-spliced display text
    # (a collapsed def has no body - a guaranteed false syntax error), and
    # the changed return hands the caller the full text. With no collapsed
    # fold this is `text` unchanged, so the section below operates exactly as
    # before.
    if _fold_segments:
        if changed:
            # The reassemble works on the layout's UNION collapse set; the
            # moved map replays each dnl shift onto the diff collapse, so
            # scope and diff state stay separate.
            _full_now, _union_after, _fold_dropped, _fold_moved = \
                _fold_reassemble(original_input, text, _fold_segments,
                                 _fold_union_col)
            ds._fold_collapsed = {_fold_moved.get(r, r)
                                  for r in ds._fold_collapsed
                                  if r not in _fold_dropped}
            if getattr(ds, '_diff_fold_collapsed', None):
                ds._diff_fold_collapsed = {
                    _fold_moved.get(r, r) for r in ds._diff_fold_collapsed
                    if r not in _fold_dropped}
            # A seam-edited fold force-expanded: its KEY must drop too, or
            # the key->range projection next frame will re-collapse it.
            # (The dnl-shifted tuples change nothing - keys are line-only.)
            if (_fold_dropped and _fold_key_of is not None
                    and getattr(ds, '_fold_keys', None) is not None):
                ds._fold_keys -= {_fold_key_of[r] for r in _fold_dropped
                                  if r in _fold_key_of}
        else:
            _full_now = _fold_full
    else:
        _full_now = text
    _parse_pair = (error, code_tree)
    _prev_text = getattr(ds, '_err_prev_text', None)
    if _prev_text is None:
        ds._err_prev_text = _full_now             # baseline on first render
    elif _full_now != _prev_text:
        ds._err_prev_text = _full_now
        if not getattr(ds, '_err_stale', False):
            ds._err_stale = True
            ds._err_stale_pair = _parse_pair       # this parse is now outdated
        # Fast-path syntax check (consumed by the marker block up top): re-check
        # the edited buffer inline so the NEXT frame shows/clears a red marker
        # immediately instead of waiting out the background reparse debounce.
        # Only for code buffers already in the error path (syntax_highlight
        # + a resolved file - never plain-text fields), size-capped
        # (fast_check_max_chars, another toggle) because compile() is O(buffer)
        # on the render thread. _compile_check dedents and handles in-function
        # errors, so span buffers check clean.
        _fast_ok = (Toggles.TextEditor.check_syntax_errors
                    and Toggles.TextEditor.fast_syntax_check
                    and syntax_highlight and jump_to is not None
                    and not single_line)
        if _fast_ok and len(_full_now) <= Toggles.TextEditor.fast_check_max_chars:
            from src.lsd.gl_gui.view.core_conversion.new_converters import _compile_check
            _fe = _compile_check(_full_now)
            ds._fast_err_state = (_full_now, _fe)
            ds._fast_err_extra = ((_full_now, _compile_check_more(_full_now, _fe))
                                  if _fe is not None else None)
            # Import-suggestion fast path (consumed by the quick-fix block up
            # top): a warm incremental scan is O(changed region) per keystroke
            # (~0.4ms). Gated on has_scan_state - a path's first scan is
            # O(buffer tokenize + module-binds parse), ~1s max, and must be
            # to the background workers; they warm the incremental fast-path state
            # and this path takes over from the next keystroke on. The current
            # background payload rides along so the top block can tell when a
            # landed relint/reparse superseded this scan.
            try:
                from src.lsd.gl_gui.view.core_conversion.code_checks import (
                    collect_import_suggestions, has_scan_state)
                _fi_path = getattr(jump_to, 'path', None)
                if has_scan_state(_fi_path):
                    _fi_scan = collect_import_suggestions(_full_now, path=_fi_path)
                    ds._fast_imports_state = (_full_now, _fi_scan or {}, import_fixes)
                else:
                    ds._fast_imports_state = None
            except Exception:
                ds._fast_imports_state = None
        elif (_fast_ok and _prev_text is not None
                and Toggles.TextEditor.check_changed_region):
            # Over-cap buffer: compile only the changed top-level span, diffed
            # against the pre-edit text (_prev_text - still the OLD buffer
            # here; ds._err_prev_text was already advanced above). The
            # heuristic in _region_compile_check means a region cut mid-
            # string/bracket reports "ambiguous", not a false error. Held-
            # error rules: a fresh failure over a previously-clean region is
            # real (show it); both-fail keeps an already-held error alive
            # (same-function still being typed, fresh line mapping); a clean
            # region clears a held error only if it LIVES IN that region -
            # one found in a DIFFERENT region stays across the edit, its
            # line shifted by the edit's delta when the edit sat above it, so
            # big-file errors never wait out the background debounce to
            # reappear. "skip" (huge paste) drops the held state - the buffer
            # changed structurally and the background parse re-flags.
            from src.lsd.gl_gui.view.core_conversion.new_converters import (
                _region_compile_check)
            _r_status, _r_err, _r_span = _region_compile_check(
                _prev_text, _full_now, Toggles.TextEditor.fast_check_max_chars)
            _held = getattr(ds, '_fast_err_state', None)
            _held_err = _held[1] if _held is not None else None
            # A held error OUTSIDE the edited span survives every outcome:
            # above the edit its line is unchanged, below it shifts by the
            # edit's delta. Inside the span its fate depends on the status
            # (or stays None): clean clears it, error/ambiguous re-map it
            # to the fresh compile, skip defers it to the background parse.
            _keep, _inside = None, False
            _ln = getattr(_held_err, 'lineno', None) if _held_err else None
            if _ln is not None and _r_span is not None:
                _s, _e_old, _dlt = _r_span
                _inside = _s < _ln <= _e_old
                if not _inside:
                    if _ln > _e_old:
                        _held_err.lineno = _ln + _dlt
                    _keep = _held_err
            if _r_status == "error":
                ds._fast_err_state = (_full_now, _r_err)
            elif _r_status == "ambiguous" and _held_err is not None:
                # Same error re-type gets a fresh line mapping; a held
                # error from ANOTHER region keeps the extraction artifact.
                ds._fast_err_state = (_full_now, _r_err if _inside else _keep)
            else:
                ds._fast_err_state = (_full_now, _keep)
            # Import-suggestion fast path for over-cap buffers too: the
            # scanner's warm incremental step is O(changed region) regardless
            # of buffer size - only its full fallback (first scan, big paste,
            # tokenizer trouble) is O(buffer). incremental_only refuses
            # exactly that fallback: a None result leaves the fast state
            # unset and the debounced background channel authoritative (its
            # next scan re-warms the incremental state).
            try:
                from src.lsd.gl_gui.view.core_conversion.code_checks import (
                    collect_import_suggestions, has_scan_state)
                _fi_path = getattr(jump_to, 'path', None)
                _fi_scan = (collect_import_suggestions(_full_now, path=_fi_path,
                                                       incremental_only=True)
                            if has_scan_state(_fi_path) else None)
                ds._fast_imports_state = (None if _fi_scan is None
                                          else (_full_now, _fi_scan, import_fixes))
            except Exception:
                ds._fast_imports_state = None
        else:
            ds._fast_err_state = None
            ds._fast_imports_state = None
        if not (_fast_ok and len(_full_now) <= Toggles.TextEditor.fast_check_max_chars):
            ds._fast_err_extra = None   # over-cap / off: only the first error
    elif getattr(ds, '_err_stale', False):
        _sp = getattr(ds, '_err_stale_pair', (None, None))
        if not (error is _sp[0] and code_tree is _sp[1]):
            ds._err_stale = False                  # a fresh parse landed

    # ── Viewport snapshot (for instant-restore source) ────────────────────
    # Capture what this editor SHOWED: the visible display-line band and the
    # text, persisted by text_editor_state (@exclude'd so these per-scroll
    # writes must never invalidate). Band math mirrors _window()'s. Display
    # space on purpose: with a fold collapsed the snapshot is what was ON
    # SCREEN, and the restored stand-in reproduces the look, not the folds
    # (they re-derive when the real buffer lands). Skipped when WE are the
    # stand-in, and for one-line boxes.
    if (text_editor_state is not None and not restore_active
            and not single_line and not is_search_box and line_px):
        try:
            _clip = draw_state.abs_clip_rect
            _sn_n = len(_line_starts(text))
            _sv0 = max(0, min(int((_clip[1] + bar_height - top) / line_px) - 3,
                              _sn_n - 1))
            _sv1 = max(_sv0, min(int((_clip[3] - top) / line_px) + 3, _sn_n - 1))
            _soffs = _line_offsets_cached(text)
            _ss = _soffs[_sv0]
            _se = (_soffs[_sv1 + 1] - 1) if _sv1 + 1 < len(_soffs) else len(text)
            text_editor_state.restore_first_line = _sv0
            text_editor_state.restore_total_lines = _sn_n
            text_editor_state.restore_text = text[_ss:_se]
            # Gutter shape (digits decide the number column's X) and the fold
            # keys (line-independent collapse identities - the ONLY place
            # fold state survives a session; ds._fold_keys itself never
            # serializes). None keys = fold layer disabled for this buffer.
            text_editor_state.restore_gutter_digits = (
                int(gutter_digits) if gutter_w > 0 else 0)
            text_editor_state.restore_line_offset = int(line_offset)
            # The band's gutter EXACTLY as painted this frame: number per
            # row (line_numbers is already fold-remapped display-space),
            # -1/-2 for a fold-header chevron (expanded/collapsed), None
            # for a numberless row. The stand-in replays these so the
            # gutter doesn't change when the real buffer lands.
            if gutter_w > 0:
                _sgr = []
                for _ri in range(_sv0, _sv1 + 1):
                    _sfh = _fold_hdr.get(_ri)
                    if (_sfh is not None and _hide_expanded_diff
                            and not _sfh[1] and _sfh[0] in _diff_rng_set):
                        _sfh = None   # expand-all: the gutter painted its number
                    if _sfh is not None:
                        _sgr.append(-2 if _sfh[1] else -1)
                    elif line_numbers is not None:
                        _sn = (line_numbers[_ri]
                               if _ri < len(line_numbers) else None)
                        _sgr.append(int(_sn) if _sn is not None else None)
                    else:
                        _sgr.append(int(line_offset) + _ri + 1)
                text_editor_state.restore_gutter_rows = _sgr
            else:
                text_editor_state.restore_gutter_rows = None
            # The band's DIFF-gap headers ({band row: hidden count}, 0 =
            # expanded) and its preview-fade rows, so the stand-in wears
            # the collapsed compare split's look - separator color, "N
            # lines" labels, tinted chevrons, the fade - from frame 1
            # (the diff layer itself needs the real buffer). Bisect over the
            # fold list (display-line sorted, the badge pass's cache) and
            # one membership test per band row: O(band), never a walk.
            _sdr = {}
            _fdl_s = getattr(ds, '_fold_dl_cache', None)
            if (_diff_rng_set and _fold_folds and _fdl_s is not None
                    and _fdl_s[0] is _fold_folds):
                for _sf in _fold_folds[bisect.bisect_left(_fdl_s[1], _sv0):
                                       bisect.bisect_right(_fdl_s[1], _sv1)]:
                    if _sf[0] not in _diff_rng_set:
                        continue
                    if _hide_expanded_diff and not _sf[2]:
                        continue   # expand-all: no chevron, no badge
                    _sdr[_sf[1] - _sv0] = int(_sf[3]) if _sf[2] else 0
            text_editor_state.restore_diff_rows = _sdr
            text_editor_state.restore_preview_rows = (
                [_ri - _sv0 for _ri in range(_sv0, _sv1 + 1)
                 if _ri in _preview_lines]
                if _preview_lines else None)
            _sfk = getattr(ds, '_fold_keys', None)
            text_editor_state.restore_fold_keys = (
                None if _sfk is None else list(_sfk))
            # The diff layer's collapse history, while the layer is active
            # (turn off leaves the last capture in place - the file may
            # come back into a compare next session). Change-edge test via
            # a ds-side copy: the set is a few hundred tuples at most.
            _sdc = getattr(ds, '_diff_fold_collapsed', None)
            if _sdc is not None and getattr(ds, '_diff_persist_memo', None) != _sdc:
                ds._diff_persist_memo = set(_sdc)
                text_editor_state.restore_diff_collapsed = sorted(_sdc)
        except Exception:
            pass   # a snapshot failure cannot take down the editor
    if restore_active:
        changed = False   # stand-in edits are discarded, never propagated
        # Caret state frozen across the stand-in (see the restore branch):
        # the body's clamp against the short stand-in is undone, and prev is
        # synced so the first REAL-text frame sees no caret "move" - the
        # follow must not yank the restored scroll toward the caret.
        if _restore_caret is not None:
            (ds.text_cursor_pos, ds.text_selection_start,
             ds.text_selection_end) = _restore_caret
            ds.text_prev_cursor_pos = ds.text_cursor_pos

    _pf("errbox+tail")
    # Emit the per-section breakdown for every edited frame (typing latency is the
    # target) plus any anomalous slow frame, so idle repaints are silent.
    _pf_total_ms = (time.perf_counter() - _pf_t0) * 1000.0
    if changed or _pf_total_ms >= 8.0:
        _prev_t = _pf_t0
        _parts = []
        for _lbl, _tm in _pf_marks:
            _ms = (_tm - _prev_t) * 1000.0
            _prev_t = _tm
            if _ms >= 0.05:
                _parts.append((_lbl, _ms))
        _parts.sort(key=lambda p: -p[1])
        _bd = " ".join(f"{_l}={_m:.1f}" for _l, _m in _parts)
        if _pf_tok[1]:
            _bd += f" (tokenize_miss={_pf_tok[0] * 1000.0:.1f}x{_pf_tok[1]})"
        _ptrace("draw_text perf", name=ds.name, total_ms=round(_pf_total_ms, 1),
                cpu_ms=round((time.thread_time() - _pf_cpu0) * 1000.0, 1),
                changed=changed, lines=len(_line_starts(text)), breakdown=_bd,
                **_pf_info)

    if changed:
        # Timeline: WHAT changed. Chunked common-prefix scan: equal 4KB slices
        # skip at C speed, per-char refinement only inside the first differing
        # chunk. The old per-char zip walk was O(edit position) of work per
        # keystroke (~9ms measured at 119k chars in) - and it ran after
        # the _pf summary above, so no breakdown section ever showed it.
        _old = original_input if isinstance(original_input, str) else ""
        _m = min(len(_old), len(text))
        _di = 0
        while _di < _m:
            _step = min(4096, _m - _di)
            if _old[_di:_di + _step] == text[_di:_di + _step]:
                _di += _step
                continue
            _e = _di + _step
            while _di < _e and _old[_di] == text[_di]:
                _di += 1
            break
        _ptrace("editor CHANGED", name=ds.name, old_len=len(_old), new_len=len(text),
                diff_at=_di, old=repr(_old[_di:_di + 24]), new=repr(text[_di:_di + 24]))
        return True, _full_now
    # Unchanged: hand back the FULL buffer, never the fold-spliced display
    # text (original_input IS the display text while a fold is collapsed -
    # returning it would drop every fold line if the wrapper propagates
    # the unchanged).
    return False, (_fold_full if _fold_segments else original_input)