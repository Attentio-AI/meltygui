import bisect
import builtins as _builtins
import keyword
import math
import re
import time

from meltygui.core.runtime.paths import debug_log_path
import meltygui.core.windowing.window_api as glfw
import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
from meltygui.hdr_color import unpack_color
from meltygui.hdr_color import scale_alpha

from meltygui.state.new_core_model import DropDownState
from meltygui.state.new_core_model import TextEditorState
from meltygui.core.rendering.render_funcs import RenderFuncs
from meltygui.core.runtime.toggles import Tint
from meltygui.code.libcst_conversion import CodeLine
from meltygui.core.cache.tile_marks import add_shadow
from meltygui.core.cache.tile_marks import add_glow
from meltygui.core.cache.tile_marks import clear_glows
from meltygui.core.core_render import render_func
from meltygui.core.core_render import SCROLLBAR_MARGIN
import meltygui.core.input.mouse_cursor as mouse_cursor
from meltygui.core.melty import Melty
from meltygui.core.melty import SearchTerm
from meltygui.core.diagnostics.perf_trace import trace as _ptrace
from meltygui.core.styling.fonts import Font
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.core.rendering.core_decoration import defaults
from meltygui.core.rendering.core_decoration import Core
from meltygui.core.rendering.window_decoration import window
from meltygui.core.runtime.toggles import Swoosh
from meltygui.completion.fim import FimState
from meltygui.editor.source_tools import SourceToolsState


def _hex(h):
    """Convert '#rrggbb' to imgui packed u32 color (ABGR format)."""
    r = int(h[1:3], 16)
    g = int(h[3:5], 16)
    b = int(h[5:7], 16)
    return pack_color(r / 255.0, g / 255.0, b / 255.0, 1.0)


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
    # f-string replacement-field punctuation: `{` `}`, the `!r` conversion and the
    # `:` opening the format spec (IntelliJ paints these keyword orange). The
    # expression between them gets ordinary code kinds, the spec stays 'string'.
    'fstring_delim': _hex('#cc7832'),
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
def _strip_comments(text):
    # Strings are not bindings either: `label = "files"` must not leak a
    # function-local `files` into the namespace. Preserve line positions.
    pattern = r'''"""[\s\S]*?(?:"""|$)|\x27\x27\x27[\s\S]*?(?:\x27\x27\x27|$)|"(?:[^"\\\n]|\\.)*(?:"|$)|\x27(?:[^\x27\\\n]|\\.)*(?:\x27|$)|#[^\n]*'''
    return re.sub(pattern, lambda match: "\n" * match.group().count("\n"), text)

_SCOPE_HEAD_RE = re.compile(r'^([ \t]*)(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)')


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
    from meltygui.code.libcst_conversion import completions_at
    pool, seen = [], set()

    meta = {}
    if func is not None:
        from meltygui.core.rendering.func_metadata import FuncsMetadata
        from meltygui.core.rendering.func_metadata import _type_name
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
    lines = _blank_foreign_scopes(_strip_comments(text).split("\n"), line)
    scan_text = "\n".join(lines[:line + 1] if code_tree is not None else lines)
    # Recognize live assignments before backfilling identifiers. Otherwise a
    # fully typed local is indistinguishable from the incomplete token echoed
    # by the scan, and completion keeps stealing Enter after a parse lands.
    for binding in re.finditer(
            r'(?m)^([ \t]*)([A-Za-z_]\w*)[ \t]*(?::[^=\n]+)?=(?!=)', scan_text):
        add(binding.group(2), "local" if binding.group(1) else "var")
    for name in _BARE_IDENT_RE.findall(scan_text):
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
        from meltygui.core.rendering.func_metadata import _receiver_before
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
    from meltygui.core.runtime.toggles import Toggles
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


def _filter_completions(pool, prefix, users=None, tints=None, keep_exact=False):
    """Prefix matches first; scope outranks tint and popularity within a group.

    Automatic suggestions stop at a complete symbol. Ctrl+Space can explicitly
    ask for alternatives, including the exact symbol. Unclassified exact names
    are only the incomplete token echoed by the fallback scan.
    """
    exact = [(n, k) for (n, k) in pool if n == prefix and k != "name"]
    if exact and not keep_exact:
        return []  # a complete symbol must not steal the next newline
    rows = [(n, k) for (n, k) in pool if n != prefix]
    if not prefix:
        # Empty prefix only happens right after a '.', where a pile of dunders is
        # noise - hide them (typing a leading '_' brings them back via the else).
        groups = [[(n, k) for n, k in rows if not n.startswith("_")]]
    else:
        p = prefix.lower()
        groups = [[(n, k) for n, k in rows if n.lower().startswith(p)],
                  [(n, k) for n, k in rows if p in n.lower() and not n.lower().startswith(p)]]
    def _key(row):
        n, kind = row
        scope_rank = (0 if kind in {"param", "local"} else
                      1 if kind == "name" else
                      3 if kind in {"builtin", "kw", "auto_import"} else 2)
        tinted = 0 if (tints and n in tints) else 1
        count = users.get(n, 0) if users else 0
        return (scope_rank, tinted, -count)
    for group in groups:
        group.sort(key=_key)
    ranked = exact + [r for g in groups for r in g]
    return ranked[:_AC_MAX_ROWS]


def _completion_replace_end(text, cursor, pick, snippets=None):
    """Replace a whole identifier, but leave snippet punctuation untouched.

    Runs only on acceptance; the scan touches the word, never the file.
    """
    if snippets and pick in snippets:
        return cursor
    while cursor < len(text) and (text[cursor].isalnum() or text[cursor] == '_'):
        cursor += 1
    return cursor


def _completion_selection(candidates, index, state):
    # The latched menu paints after the editor. Read its last painted cursor
    # when handling keys, rather than overwriting it with yesterday's index.
    path = state.cursor_path
    if isinstance(path, tuple) and len(path) == 1 and path[0] in candidates:
        return candidates.index(path[0])
    return min(index, len(candidates) - 1)


def _completion_popup_rect(x, y, line_height, clip, max_height):
    """Keep assistance inside the editor, above its tabs, with room to scroll."""
    below = max(0, clip[3] - (y + line_height))
    above = max(0, y - clip[1])
    height = min(max_height, max(above, below))
    top = y + line_height if below >= height else y - height
    return max(clip[0], min(x, clip[2] - 300)), max(clip[1], top), height


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
    from meltygui.code.code_checks import _module_for
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
                    from meltygui.code.chain_converters import _enclosing_function
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
            from meltygui.core.melty import Melty
            if tile is not None:
                Melty.cache.invalidate(tile)
        except Exception:
            pass
        try:
            import meltygui.core.windowing.glfw_utils as glfw_utils
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
        from meltygui.core.rendering.func_metadata import member_completions
        from meltygui.core.rendering.func_metadata import _receiver_before
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
        from meltygui.code.libcst_conversion import submit_member_completion
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
        from meltygui.core.rendering.func_metadata import _receiver_before
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
        from meltygui.code.libcst_conversion import submit_signature_help
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
    from meltygui.core.runtime.toggles import Toggles
    try:
        from meltygui.completion.fim_context import editor_view
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
            from meltygui.core.diagnostics.notifications import notify
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
    col = pack_color(0.66, 0.70, 0.78, 0.55)
    hint = pack_color(0.66, 0.70, 0.78, 0.32)
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
    """Show the active signature after the current source line, or at the
    editor's bottom edge when it does not fit. Non-interactive; mono font active.
    """
    if not getattr(ds, '_ac_sig_show', False) or getattr(ds, '_ac_open', False):
        return
    sig = getattr(ds, '_ac_sig_data', None)
    if not sig:
        return
    name, params, types = sig
    active = getattr(ds, '_ac_sig_active', 0)
    if params:
        active = max(0, min(active, len(params) - 1))   # extra args ride the last (*args)

    name_col = pack_color(0.55, 0.78, 1.0, 1.0)
    dim = pack_color(0.72, 0.76, 0.84, 1.0)
    acc = pack_color(1.0, 0.84, 0.42, 1.0)
    type_col = pack_color(0.55, 0.72, 0.55, 1.0)   # dim green for the type

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
    # Prefer unused space after the current source line. When it does not fit,
    # dock at the editor's bottom edge instead of covering the preceding code.
    line_end = _get_line_end(text, ds.text_cursor_pos)
    end_x, end_y = _char_pos_to_xy(text, line_end, origin_x, origin_y, line_px, vcols=vcols)
    if end_x + total + 24 <= clip[2]:
        base_x, hy = end_x + 16, end_y
    else:
        base_x, hy = origin_x + 7, clip[3] - th - 6

    pad = 7
    x0 = base_x - pad
    x1 = min(clip[2] - 2, base_x + total + pad)   # overflow clips on the right
    bg = pack_color(0.11, 0.12, 0.15, 0.97)
    border = pack_color(0.30, 0.33, 0.42, 0.9)
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
from meltygui.model.icon_model import FA_ICONS
from meltygui.model.icon_model import FA_GLYPH_SET
ICON_COLLECTION = FA_ICONS
GENERIC_ICON = "\uf005"  # star - the placeholder Ctrl+I inserts; pick the real one from the dropdown

                  

from meltygui.view.text_view import draw_icon_selector_plain

draw_icon_selector_plain._plain_tv = True
                




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
    from meltygui.view.decoration_view import draw_bg
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


from meltygui.view.text_view import draw_bool_token_plain
# Marks a renderer as wrapper-less for draw_text: the call site passes these
# the editor's draw_state (editor_ds) so they can keep the EDITOR tile live
# during a gesture - a plain widget has no tile of its own to invalidate.
draw_bool_token_plain._plain_tv = True


from meltygui.view.text_view import draw_number_token_plain

draw_number_token_plain._plain_tv = True


def _fmt_color_channel(v):
    """Format a 0..1 channel back into source as a FLOAT literal (the merged
    token needs at least one float channel, and a dragged value is fractional
    anyway; untouched channels keep their original text — ints stay ints)."""
    s = f"{max(0.0, min(1.0, v)):.3f}".rstrip('0')
    return s + '0' if s.endswith('.') else s
       
        


from meltygui.view.text_view import draw_color3_token_plain

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


from meltygui.view.text_view import draw_colorhex_token_plain

draw_colorhex_token_plain._plain_tv = True


def _color_swatch_plain(s, vals, splice, width, height, name, editor_ds):
    """The swatch + latched picker shared by the tuple and hex-string color
    widgets: `vals` are the 3/4 parsed channels, `splice(new_color)` renders
    the edited channels back into source text. Returns (changed, text)."""
    from meltygui.core.rendering.mode import Mode
    from meltygui.view.color_view import draw_color_picker
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
    from meltygui.view.color_view import color_picker_height
    from meltygui.view.color_view import color_picker_width
    from meltygui.view.color_view import color_picker_top_offset
    picker_h = color_picker_height(len(vals))
    # window_pos is relative to the imgui cursor at call time - park the
    # cursor back on the swatch's top-left so (0, 10) anchors just under it,
    # exactly like the wrapped popover.
    imgui.set_cursor_screen_pos((x, y))
    color_changed, new_color, pick_ds = draw_color_picker(
        tuple(vals), name=f"{name}_picker", closed=not want_open,
        window_pos=(0, color_picker_top_offset()),
        parent_window=editor_ds, width=color_picker_width(), height=picker_h,
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
    from meltygui.editor.pending_save import PendingSave
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
    from meltygui.code.chain_converters import _modules_for_file
    from meltygui.code.chain_converters import _resolved
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
        if got is None:
            return None
        if not modules:
            # Give standalone defs an owner discoverable by live_view without
            # executing the file's top-level side effects.
            import hashlib
            import sys
            module_name = '_melty_fnrun_' + hashlib.sha256(
                str(target).encode()).hexdigest()
            module = types.ModuleType(module_name)
            module.__file__ = str(target)
            package = []
            parent = target.parent
            while (parent / '__init__.py').is_file():
                package.insert(0, parent.name)
                parent = parent.parent
            module.__package__ = '.'.join(package)
            sys.modules[module_name] = module
            modules.append(module)
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
            from meltygui.code.live_instrument import _delta_above
            from meltygui.code.live_instrument import _pending_gen
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
            import meltygui.code.chain_converters as _cc
            from meltygui.code.live_view import adopt_live_store
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
        import meltygui.code.chain_converters as _cc
        _evicted = False
        for module in modules:
            _twin = module.__dict__.pop(f"_fnrun_live_{def_name}", None)
            if _twin is not None:
                _evicted = True
                # The evicted twin's store moves to the live function it
                # converges on - never orphaned with its state.
                from meltygui.code.live_view import adopt_live_store
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
    from meltygui.editor.pending_save import PendingSave
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
    from meltygui.editor.pending_save import PendingSave
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
        except Exception as error:
            raise ImportError(f"Cannot run {file_path}: {ast.unparse(node)}: {error}") from error


def _fnrun_find_def_node(tree, def_name, line):
    """FunctionParse node named `def_name` nearest 1-indexed parse `line` in
    the routed code tree, or None. Name-first, line as the tie-break between
    same-named methods — the same policy as _fnrun_resolve."""
    from meltygui.code.libcst_conversion import FunctionParse
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
    from meltygui.code.libcst_conversion import CodeLine
    from meltygui.code.libcst_conversion import _python_to_cst_expr
    from meltygui.code.libcst_conversion import _cst_node_to_code
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
    from meltygui.code.libcst_conversion import CodeLine
    from meltygui.code.libcst_conversion import NoDefault
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
    none. Blocking on the caller (the console uses a worker), with the
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
            from meltygui.code.live_instrument import run_instrumented
            run_instrumented(fn, **params)
        else:
            fn(**params)
        return True, None
    except Exception as e:
        from meltygui.core.rendering.render_dispatch import _format_run_error
        from meltygui.core.rendering.render_dispatch import _respond_to_cuda_oom
        from meltygui.utils.render_utils import print_colored_traceback
        print(f"Error calling function '{fn.__name__}': {e}")
        print_colored_traceback(*sys.exc_info())
        _respond_to_cuda_oom(e, fn.__name__)
        return False, _format_run_error(e)


def _fnrun_console(editor_ds, skey):
    from meltygui.models.function_console import FunctionConsole
    consoles = editor_ds.__dict__.setdefault('_fnrun_consoles', {})
    if skey not in consoles:
        consoles[skey] = FunctionConsole(wake=request_render)
    return consoles[skey]


def _fnrun_start(editor_ds, file_path, def_line, def_name,
                 instrumented=True, params=None, queue_if_busy=False):
    """Resolve on the UI thread, run with interactive streams on a worker."""
    skey = (str(file_path), def_name)
    console = _fnrun_console(editor_ds, skey)
    queued = editor_ds.__dict__.setdefault('_fnrun_queued', {})
    statuses = editor_ds.__dict__.setdefault('_fnrun_status', {})
    if console.running or statuses.get(skey, (None,))[0] == 'running':
        if queue_if_busy:
            queued[skey] = (file_path, def_line, def_name, instrumented, params)
        return False
    mode = 'live' if instrumented else 'run'
    from meltygui.core.runtime.extensions import call
    try:
        external_run = call('source_run', file_path, def_name, params, console, instrumented)
        python = external_run is not None
        fn = None if python else _fnrun_resolve(file_path, def_line, def_name, prefer_pending=True)
    except Exception as error:
        statuses[skey] = ('err', str(error), mode)
        editor_ds.invalidate()
        request_render()
        return False
    if fn is None and not python:
        statuses[skey] = ('err', f"couldn't resolve '{def_name}' — not found in live modules or source", mode)
        editor_ds.invalidate()
        request_render()
        return False
    statuses[skey] = ('running', None, mode)

    def done(result):
        def finish():
            ok, error = result
            statuses[skey] = (('ok', Melty.frame_count, mode) if ok
                              else ('err', error, mode))
            if instrumented:
                _fnrun_after_live_run(editor_ds)
            editor_ds.invalidate()
            request_render()
            pending = queued.pop(skey, None)
            if pending is not None:
                _fnrun_start(editor_ds, *pending)
        Melty.post_to_render(finish)

    console.start(lambda: external_run() if python
                  else _fnrun_run(fn, instrumented=instrumented, params=params), done)
    editor_ds.invalidate()
    request_render()
    return True


def _draw_fnrun_console(console, draw_state, unique):
    from meltygui.view.header_view import flat_button
    text, running, waiting = console.snapshot()
    imgui.text('Console — ' + ('Waiting for input' if waiting else 'Running' if running else 'Finished' if console.thread else 'Ready'))
    RenderFuncs.draw_text(text or '', name=f'console-output##{unique}',
                          width=max(200, draw_state.width - 16), height=220,
                          editable=False, syntax_highlight=False, autocomplete=False,
                          wrap=True, show_header=False, show_widgets=False,
                          use_cache=False)
    if running:
        box = RenderFuncs.draw_text(console.draft, name=f'console-input##{unique}',
                                    width=max(200, draw_state.width - 16), height=30,
                                    single_line=True, syntax_highlight=False,
                                    autocomplete=False, show_header=False,
                                    return_extras=True)
        if box[0]:
            console.draft = box[1]
        input_ds = box[2]
        enter = (Melty.text_focused_ds is input_ds and
                 any(key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER)
                     for key, _ in Melty.frame_key_events))
        send = flat_button(f'Send##console-send{unique}', draw_state,
                           f'console-send::{unique}', height=24)
        imgui.same_line()
        eof = flat_button(f'End input##console-eof{unique}', draw_state,
                          f'console-eof::{unique}', height=24)
        if send or enter:
            console.send(console.draft)
            console.draft = ''
            draw_state.invalidate()
        if eof:
            console.close_input()
    # Keep the frame's ancestors repainting while output arrives on a worker.
    if running:
        draw_state.invalidate()


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
    from meltygui.core.runtime.toggles import Toggles
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


def _fnrun_panel_sync_entry(editor_ds, skey):
    """[last text, deadline, unacknowledged writes], shared by both paths."""
    memo = editor_ds.__dict__.setdefault('_fnrun_sig_sync', {})
    entry = memo.setdefault(skey, [None, None, {}])
    if len(entry) == 2:                 # adopt an already-open dict on hotswap
        entry.append({})
    return entry


def _fnrun_sync_panel_params(shown, seen, pending, params_node, text, line):
    """Merge signature text into the stable panel dict after the quiet window.

    The parse can lag the buffer. Read defaults from text, and keep local
    edits until that text acknowledges the latest queued splice. Deferred
    window draws and hold callbacks must all keep editing the SAME dict.
    """
    import libcst
    from meltygui.code.libcst_conversion import _cst_to_python_or_raw

    changed = False
    for key in dict.fromkeys((*shown, *params_node)):
        if not isinstance(key, str) or key.startswith('__'):
            continue
        span = _fnrun_sig_default_span(text, line, key)
        if span is None:
            continue
        source = text[span[0]:span[1]]
        current = _fnrun_param_src(shown[key]) if key in shown else None
        if key in pending:
            if source != pending[key]:
                continue              # an older echo of a panel edit
            del pending[key]
        if key in shown and current != seen.get(key):
            continue                  # a newer edit still inside the hold timer
        if source == current:
            continue
        try:
            value = _cst_to_python_or_raw(libcst.parse_expression(source))
        except Exception:
            continue                  # broken expression while typing
        dict.__setitem__(shown, key, value)
        seen[key] = _fnrun_param_src(value)
        changed = True
    return changed


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
        _fnrun_panel_sync_entry(editor_ds, skey)[2][pk] = new_src
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
    from meltygui.editor.pending_save import PendingSave
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
        _fnrun_start(editor_ds, file_path, def_line, def_name,
                     instrumented=True, queue_if_busy=True)

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





from meltygui.view.text_view import draw_run_fn_token_plain

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
from meltygui.editor.live_view_views import install_token_views as _install_live_view_tv
from meltygui.editor.live_view_views import flush_selected_markers as _flush_selected_markers
_install_live_view_tv(DEFAULT_TOKEN_VIEWS)


def _parse_col_shift(buffer_text, parse_source):
    """Uniform column delta between the rendered buffer and the parse's own
    source: the code-host route parses function spans DEDENTED while the
    buffer keeps the file's indent, so every span col sits that many cells
    left of its glyph. Compared on the first line that's non-blank in both;
    0 when the sources agree (whole-file editors)."""
    if not parse_source or parse_source is buffer_text:
        return 0
    buffer_start = parse_start = 0
    for index in range(51):
        buffer_end = buffer_text.find('\n', buffer_start) if index < 50 else -1
        parse_end = parse_source.find('\n', parse_start) if index < 50 else -1
        a = buffer_text[buffer_start:len(buffer_text) if buffer_end < 0 else buffer_end]
        b = parse_source[parse_start:len(parse_source) if parse_end < 0 else parse_end]
        if a.strip() and b.strip():
            return (len(a) - len(a.lstrip())) - (len(b) - len(b.lstrip()))
        if buffer_end < 0 or parse_end < 0:
            break
        buffer_start, parse_start = buffer_end + 1, parse_end + 1
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


_TEXT_SPLICE_CACHE = {}


def _text_splice(old_text, new_text):
    key = (id(old_text), id(new_text))
    cached = _TEXT_SPLICE_CACHE.get(key)
    if cached is not None and cached[0] is old_text and cached[1] is new_text:
        return cached[2]
    result = _compute_text_splice(old_text, new_text)
    if len(_TEXT_SPLICE_CACHE) >= 16:
        del _TEXT_SPLICE_CACHE[next(iter(_TEXT_SPLICE_CACHE))]
    _TEXT_SPLICE_CACHE[key] = (old_text, new_text, result)
    return result


def _compute_text_splice(old_text, new_text):
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
        from meltygui.code.libcst_conversion import LineMap
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
    from meltygui.core.runtime.extensions import call
    return call('source_owner', ds)


def _open_usage_ref(ref, token=None, editor_window=None):
    """Open one UsageRef (Ctrl+B) in the in-app code editor: opens the file's
    tab, summons the editor window, and stashes the line on
    OpenFiles.jump_to_line — draw_code_editor consumes it to place the caret
    (the editor's cursor-follow scroll then brings it into view). `token`
    rides along on OpenFiles.jump_to_token so the caret lands ON the symbol
    rather than at the line's first code character; `editor_window` keeps the
    jump in the originating editor instance."""
    from meltygui.core.runtime.extensions import open_source as open_in_editor
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
        with open(debug_log_path("uj_debug.log"), "a") as f:
            f.write(f"[f{Melty.frame_count}] {msg}\n")
    except OSError:
        pass


# --- Ctrl+B usage-graph consistency check ------------------------------------
# The fresh single-line recheck (usage_recompute) recomputes exactly what the
# background graph should already hold for the caret's line. Any disagreement
# IS the stuck-stale-graph bug observed at the moment it's reproduced - so
# every Ctrl+B recheck diffs the two and dumps a full forensic block here.
_USAGE_MISMATCH_LOG = debug_log_path("usage_graph_mismatch.log")


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
    from meltygui.models.file_meta import file_meta_store
    entry = file_meta_store().get(str(p)) if p is not None else None
    t = entry.get('tint') if isinstance(entry, dict) else None
    return tuple(t) if t else None


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
    from meltygui.code.libcst_conversion import _parse_override_comment
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
        from meltygui.editor.pending_save import PendingSave
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
            
        from meltygui.editor.pending_save import PendingSave
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
        from meltygui.editor.pending_save import PendingSave
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
        from meltygui.editor.pending_save import PendingSave
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


from meltygui.model.color_model import _brightness_clamp


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
    from meltygui.core.runtime.toggles import rgb_to_hsv
    from meltygui.core.runtime.toggles import hsv_to_rgb
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
    from meltygui.core.runtime.toggles import rgb_to_hsv
    from meltygui.core.runtime.toggles import hsv_to_rgb
    from meltygui.core.runtime.toggles import Toggles
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
    got = scale_alpha(packed, max(0.0, min(1.0, alpha_factor)))
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
    r, g, b, a = unpack_color(packed)
    r += (rgb[0] - r) * k
    g += (rgb[1] - g) * k
    b += (rgb[2] - b) * k
    out = pack_color(r, g, b, a)
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
        from meltygui.editor.pending_save import PendingSave
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
        from meltygui.editor.pending_save import PendingSave
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
    from meltygui.core.runtime.toggles import Toggles
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
    from meltygui.code.libcst_conversion import _parse_override_comment
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
    from meltygui.core.runtime.toggles import Toggles
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
        import meltygui.code.symbol_roster as _sr
        project = _sr.analysis_project(path=view_path)
        _sr.sweep(project=project)          # throttled: notices pending/disk edits in OTHER files
        _sr.register_consumer(ds, project=project, path=view_path)   # a later roster gen invalidates this editor
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
        _ckey = (_DEF_TINTS_VER, "roster", project.key, _sr.generation(project), id(text), line_offset,
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
                    from meltygui.editor.roster_tints import collect_def_tints as _roster_collect
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
                                             table=table, project=project)
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
    from meltygui.core.runtime.toggles import Toggles
    r, g, b, a = Toggles.TextEditor.usage_tint(n_targets)
    return pack_color(r, g, b, max(0.0, min(1.0, a)))


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


# Nested replacement fields deeper than this (`{x:{w:{p}}}` is depth 3) stay
# plain spec text - keeps a pathological `{:{:{:...` line from recursing away.
_FSTRING_MAX_NEST = 4


def _fstring_literal(s, a, b, out):
    """Append the literal (non-field) run s[a:b] of an f-string to `out` in the
    string colour, PUA icons split out like in any other string."""
    if a >= b:
        return
    run = s[a:b]
    if any(_is_icon_char(c) for c in run):
        out.extend(_split_icons(run, 'string'))
    else:
        out.append((run, 'string'))


def _skip_quoted(s, k, end):
    """Index just past the string literal opening at s[k] (a quote char) inside
    an f-string field expression, clamped to `end` when it never closes."""
    q = s[k:k + 3] if s[k:k + 3] in ('"""', "'''") else s[k]
    k += len(q)
    while k < end:
        if s[k] == '\\' and len(q) == 1:
            k += 2
        elif s.startswith(q, k):
            return min(end, k + len(q))
        else:
            k += 1
    return end


def _fstring_field(s, i, end, out, nest=1):
    """Tokenize ONE replacement field of an f-string: s[i] is its `{`, `end`
    bounds the scan (the string's closing quote is never part of a field).
    Appends to `out` and returns the index after the field:
      `{` `}`, the `!r` conversion and the `:` that opens the format spec ->
      'fstring_delim'; the expression -> ordinary code tokens (`_tokenize_raw`,
      so nested strings / f-strings, numbers, calls all colour as usual); the
      format spec -> string colour, with its own nested `{width}` fields
      tokenized the same way. A field that never closes (half-typed) just ends
      at `end`: everything typed so far is coloured as the expression."""
    out.append(('{', 'fstring_delim'))
    k = i + 1
    depth = 0
    while k < end:
        c = s[k]
        if c in '([{':
            depth += 1
        elif c in ')]':
            depth = max(0, depth - 1)
        elif c == '}':
            if depth == 0:
                break
            depth -= 1
        elif c in '"\'':
            k = _skip_quoted(s, k, end)
            continue
        elif depth == 0 and c == ':':
            break
        elif c == '!':
            if s[k + 1:k + 2] == '=':
                k += 1                  # `!=` - step over the `=` too
            elif depth == 0:
                break
        k += 1
    k = min(k, end)
    if k > i + 1:
        out.extend(_tokenize_raw(s[i + 1:k]))
    if k < end and s[k] == '!':         # conversion: !r / !s / !a
        m = k + 1
        while m < end and (s[m].isalnum() or s[m] == '_'):
            m += 1
        out.append((s[k:m], 'fstring_delim'))
        k = m
        while m < end and s[m] not in ':}':
            m += 1
        if m > k:                       # junk after the conversion: plain
            out.append((s[k:m], 'default'))
            k = m
    if k < end and s[k] == ':':         # format spec, may hold nested fields
        out.append((':', 'fstring_delim'))
        k += 1
        lit = k
        while k < end and s[k] != '}':
            if s[k] == '{' and nest < _FSTRING_MAX_NEST:
                _fstring_literal(s, lit, k, out)
                k = lit = _fstring_field(s, k, end, out, nest + 1)
            else:
                k += 1
        _fstring_literal(s, lit, k, out)
    if k < end and s[k] == '}':
        out.append(('}', 'fstring_delim'))
        k += 1
    return k


def _split_fstring(s, quote, body_start=0, raw=False):
    """Split an f-string token into (substr, color_key) runs, the way
    `_split_icons` splits a plain string: literal text (prefix and quotes
    included) stays 'string', every replacement field goes through
    `_fstring_field`. `{{` / `}}` are escapes and stay literal, so does
    `\\N{NAME}` (not in a raw f-string, where a backslash is just a char).
    `s[:body_start]` is the prefix + opening quote (0 when `s` RESUMES inside
    an f-string, see `_resume_in_string`); `quote` is the closing quote. The
    runs concatenate to exactly `s`, and a field-less f-string is still ONE
    'string' token."""
    n = len(s)
    body_end = n - len(quote) if (n - len(quote) >= body_start and s.endswith(quote)) else n
    out = []
    i, lit = body_start, 0
    while i < body_end:
        c = s[i]
        if c == '\\' and not raw:
            if s.startswith('N{', i + 1):
                close = s.find('}', i + 3, body_end)
                i = close + 1 if close != -1 else body_end
            else:
                i += 1 if s[i + 1:i + 2] in ('{', '}') else 2
        elif c in '{}' and i + 1 < body_end and s[i + 1] == c:
            i += 2
        elif c == '{':
            _fstring_literal(s, lit, i, out)
            # A field of a single-quoted f-string never runs past its line: a
            # half-typed `f"{x` leaves the next lines string-coloured, the
            # same as a window that resumes there (`_resume_in_string`).
            nl = s.find('\n', i, body_end) if len(quote) == 1 else -1
            i = lit = _fstring_field(s, i, nl if nl != -1 else body_end, out)
        else:
            i += 1
    _fstring_literal(s, lit, n, out)
    return out


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


def _tokenize_raw(text, fstring_fields=True):
    """Yields (text, color_key) tuples with Darcula-style token categories.
    Raw pass — see tokenize() below for the unary-sign merge.
    `fstring_fields=False` keeps an f-string one opaque 'string' token (the
    lexer-state reference below wants whole string extents)."""
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
            prefix = text[i:prefix_end].lower()
            if 'f' in prefix and fstring_fields:
                # f-string: replacement fields are code, the rest stays string.
                closer = _opener_quote(text[i:end])
                yield from _split_fstring(text[i:end], closer, prefix_end - i + len(closer),
                                          raw='r' in prefix)
            else:
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
    if kind == 'fstring_delim':
        return tok == '{'                # f"{-5}": the field opens an expression
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
# 'string_doc' and a prefixed one (`r'''`, `b"""`) is 'string'. An f-string
# (any quote style) records the state kind 'fstring' - it colors 'string' too,
# but a window resuming inside it keeps tokenizing its `{replacement fields}`.
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
            # The prefix itself (truthy): callers tell an f-string from r'' / b''.
            return text[pos:q]
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


def _string_state_kind(tok, kind):
    """The `line_open` state kind of a string: its colour kind, except that an
    f-string ('f' in the prefix of `tok` - a whole token or just the prefix)
    records 'fstring', so a window resuming inside it (`_resume_in_string`)
    still tokenizes its replacement fields."""
    if kind == 'string':
        for c in tok[:2]:
            if c in 'fF':
                return 'fstring'
            if c in '"\'':
                break
    return kind


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
            yield i, end, (tok, _string_state_kind(pre, 'string') if pre else 'string_doc')
        else:
            end = n
            for mm in _LEX_SQ_RE[tok].finditer(text, i + 1):
                if mm.group() == tok:
                    end = mm.end()
                    break
            yield i, end, (tok, _string_state_kind(_quote_is_prefixed(text, i) or '', 'string'))
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
    for tok, kind in _tokenize_raw(text, fstring_fields=False):
        if tok == '\n':
            line += 1                       # bare newline → next line starts clean
        elif '\n' in tok:
            # Only string/string_doc tokens carry embedded newlines; each line
            # the string continues onto starts inside it. (f-strings come
            # whole here - fstring_fields=False - and record as 'fstring'.)
            qk = (_opener_quote(tok), _string_state_kind(tok, kind)) \
                if kind in ('string', 'string_doc') else None
            for ch in tok:
                if ch == '\n':
                    line += 1
                    if line < len(line_open):
                        line_open[line] = qk
    return offs, line_open


def _diff_span(a, b):
    """(common_prefix_len, a_suffix_start, b_suffix_start) for two strings.
    Share the identity-keyed edit calculation with the other incremental
    consumers instead of locating the same change repeatedly."""
    splice = _text_splice(a, b)
    if splice is None:
        return len(a), len(a), len(b)
    return splice[0], splice[1], splice[1] + splice[2]


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

    first = bisect.bisect_right(prev_offs, lo)
    last = bisect.bisect_right(prev_offs, _old_hi)
    inserted = [lo + match.end() for match in re.finditer('\n', text[lo:new_hi])]
    new_offs = (prev_offs[:first] + inserted +
                [offset + delta for offset in prev_offs[last:]])
    if id(text) not in _LINE_STARTS_CACHE and len(_LINE_STARTS_CACHE) >= 16:
        del _LINE_STARTS_CACHE[next(iter(_LINE_STARTS_CACHE))]
    _LINE_STARTS_CACHE[id(text)] = (text, new_offs)
    cf = bisect.bisect_right(new_offs, lo) - 1      # first changed line (new coords)
    # line_open is valid through line cf (depends only on unchanged preceding
    # lines). Back up to the last clean line at/before cf to start the re-lex.
    sl = cf
    while sl > 0 and prev_open[sl] is not None:
        sl -= 1
    start_off = new_offs[sl]

    tail = [None]                # line_open for line sl (clean by construction)
    stop_old = None
    for off, state in _iter_newline_states(text, start_off):
        if state is None and off >= new_hi:
            oc = bisect.bisect_left(prev_offs, off - delta)
            if (oc < len(prev_offs) and prev_offs[oc] == off - delta
                    and prev_open[oc] is None):
                stop_old = oc                 # reconverged → reuse old suffix
                break
        tail.append(state)

    new_open = prev_open[:sl] + tail + (prev_open[stop_old:] if stop_old is not None else [])
    return new_offs, new_open


def _resume_in_string(body, opener):
    """Tokenize `body` given that it BEGINS inside a string. `opener` is the
    (closing_quote, color_kind) pair recorded in `line_open` (the kind matters:
    a prefixed triple colors 'string', a bare triple 'string_doc', and
    'fstring' colors 'string' with its replacement fields tokenized as code -
    the resume assumes the line starts in LITERAL text, so only a field that
    itself spans lines loses its code coloring on the later lines). Emits the
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
    head = (_split_fstring(body[:cut], quote) if skind == 'fstring'
            else list(_split_icons(body[:cut], skind)))

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


# How far left of a long-line band cut `_banded_tokens` re-scans an f-string
# to learn whether the cut sits inside a `{field}`. Past this the band resumes
# as literal text (a field that far into one f-string is not worth the scan).
_FSTRING_BAND_LOOKBACK = 20000


def _drop_leading_chars(toks, count):
    """`toks` without its first `count` source chars (a token straddling the
    cut keeps its kind for the part that remains)."""
    out = []
    for tok, kind in toks:
        if count >= len(tok):
            count -= len(tok)
        elif count:
            out.append((tok[count:], kind))
            count = 0
        else:
            out.append((tok, kind))
    return out


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
        span_start = ls
        if a > ls:
            out.append((text[ls:a], 'clipped'))
            state, span_start = _lex_state_at(text, ls, state, a)
        # Tokenize the band alone (no trailing newline), then clip the rest.
        # A cut inside an f-string can land in literal text OR in a `{field}`;
        # only the string's start tells, so scan from there (bounded) and drop
        # the part left of the cut.
        body0 = span_start + (len(state[0]) if span_start > ls else 0) \
            if isinstance(state, tuple) and state[1] == 'fstring' else a
        if body0 < a <= body0 + _FSTRING_BAND_LOOKBACK:
            btoks = _drop_leading_chars(_tokenize_from(text[body0:b], state), a - body0)
        else:
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
    if id(text) not in _LINE_STARTS_CACHE and len(_LINE_STARTS_CACHE) >= 16:
        del _LINE_STARTS_CACHE[next(iter(_LINE_STARTS_CACHE))]
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
        # with the estimated editor line the band corresponds to.ll
        # from ptygui.notifications import notify
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
    from meltygui.code.new_converters import _compile_check
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
    if m.startswith("No module named '"):
        return m.split("'", 2)[1]
    if m.startswith("name '"):
        end = m.find("'", 6)
        if end > 6:
            return m[6:end]
    return ""


def _diagnostic_line_text(text, index):
    starts = _line_starts(text)
    if not 0 <= index < len(starts):
        return ''
    end = starts[index + 1] - 1 if index + 1 < len(starts) else len(text)
    return text[starts[index]:end]


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
    from meltygui.code.chain_converters import _ensure_import_lines
    path = getattr(jump_to, 'path', None)
    start = getattr(jump_to, 'start', None)
    if path is None or start is None:
        lines = text.split('\n')
        new_lines, inserted, _ = _ensure_import_lines(lines, stmt)
        if not inserted:
            return False, text
        from meltygui.code.code_checks import invalidate_import_bindings
        invalidate_import_bindings(path)
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
        from meltygui.core.melty import Melty
        from meltygui.editor.pending_save import PendingSave
        from meltygui.code.new_codecs import TypeCodec
        from meltygui.code.new_codecs import _span_fingerprint
        from meltygui.code.fileref import Address
        rp = _P(target_real)
        from meltygui.code.code_checks import invalidate_import_bindings
        invalidate_import_bindings(rp)
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


def _ac_import_rows(ds, cands, prefix, jump_to=None, explicit=False):
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
    if not prefix or (len(prefix) < 3 and not explicit):
        return cands
    if not explicit and any(name == prefix and kind != "name"
                            for name, kind in (getattr(ds, '_ac_pool', None) or ())):
        return cands
    try:
        from meltygui.code.code_checks import project_importables
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
            from meltygui.code.symbol_roster import table_for
            table = table_for(path)
            real.update(table.imports)
            real.update(entry.name for entry in table.entries if entry.parent is None)
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
        if (n != prefix and n not in have and n not in real and n.lower().startswith(p)):
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
    if '#' not in stripped:
        return stripped.rstrip()
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


def _same_guide_shape(old, text):
    """Whether an inline edit leaves the guide scanner's inputs unchanged."""
    edit = _text_splice(old, text)
    if edit is None:
        return True
    start, end, delta, line_delta, line, end_line = edit
    if line_delta or line != end_line:
        return False
    def shape(source, end):
        begin = source.rfind('\n', 0, start) + 1
        stop = source.find('\n', end)
        row = source[begin:len(source) if stop < 0 else stop]
        stripped = row.lstrip()
        return (bool(stripped), len(row) - len(stripped),
                not stripped.startswith('#') and _guide_code_part(stripped).endswith(':'))
    return shape(old, end) == shape(text, end + delta)


def _update_line_widths(old, text, widths):
    edit = _text_splice(old, text)
    if edit is None:
        return widths
    start, end, delta, line_delta, line, end_line = edit
    begin = text.rfind('\n', 0, start) + 1
    stop = text.find('\n', end + delta)
    rows = text[begin:len(text) if stop < 0 else stop].split('\n')
    return widths[:line] + [len(row.rstrip()) for row in rows] + widths[end_line + 1:]


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
    from meltygui.core.runtime.toggles import Toggles
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


def fold_child_scopes(ranges, roots):
    """The scopes ONE level inside each of `roots`: for every root, the
    first nesting level of the ranges strictly inside it (a function's
    own if/for/with blocks and nested defs, not the blocks inside those).
    Collapse-all folds these along with the roots, so a root expanded
    later shows its members folded rather than fully open; expand-all
    opens them back. `ranges` are normalized (sorted, strictly nested)
    and `roots` a subset of them. Returned in `ranges` order."""
    roots = sorted(set(roots))
    out = []
    root_i = 0
    open_end = -1                # end of the current child being skipped
    for fold_range in ranges:
        while root_i < len(roots) and roots[root_i][1] < fold_range[0]:
            root_i += 1
            open_end = -1
        if root_i >= len(roots):
            break
        root = roots[root_i]
        if fold_range == root or fold_range[0] < root[0]:
            continue             # the root itself, or a range above it
        if fold_range[0] > open_end:
            out.append(fold_range)
            open_end = fold_range[1]
    return out


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
    from meltygui.core.runtime.toggles import Toggles
    if not Toggles.TextEditor.hide_meta_comment_folds or not key_of:
        return frozenset()
    return frozenset(r for r in ranges
                     if key_of.get(r, ('',))[0] == 'comment')


class _FoldLineNumbers:
    """Project only the gutter rows requested by the visible band."""
    def __init__(self, mapping, numbers=None, offset=0):
        self.mapping = mapping
        self.numbers = numbers
        self.offset = offset

    def __len__(self):
        return len(self.mapping)

    def __getitem__(self, index):
        line = self.mapping[index]
        if self.numbers is None:
            return self.offset + line + 1
        return self.numbers[line] if line < len(self.numbers) else None


def _fold_update_inline(old_text, text, built):
    """Reuse a fold layout for an edit confined to one visible, non-header line.

    Hidden text and line mappings stay identical. Structural edits, seam
    deletions and header edits use the full builder instead.
    """
    splice = _text_splice(old_text, text)
    if splice is None:
        return built
    start, end, delta, line_delta, line, end_line = splice
    if line_delta or line != end_line or '\n' in text[start:end + delta]:
        return None
    display, segments, folds, mapping = built
    if any(rng[0] == line for rng, *_ in folds):
        return None
    hidden_before = 0
    for anchor, hidden, rng, headless in segments:
        full_anchor = anchor + hidden_before
        if start < full_anchor + len(hidden) and end > full_anchor:
            return None
        if start == end and full_anchor < start <= full_anchor + len(hidden):
            return None
        if full_anchor < start:
            hidden_before += len(hidden)
        else:
            break
    display_start, display_end = start - hidden_before, end - hidden_before
    display_line = display.count('\n', 0, display_start)
    new_display = display[:display_start] + text[start:end + delta] + display[display_end:]
    def shifted(offset):
        return offset + delta if offset >= display_end else offset
    new_segments = [(shifted(anchor), hidden, rng, headless)
                    for anchor, hidden, rng, headless in segments]
    new_folds = []
    for rng, row, collapsed, count, width, anchor, hidden_length in folds:
        new_anchor = shifted(anchor)
        if not collapsed:
            hidden_length = shifted(anchor + hidden_length) - new_anchor
        new_folds.append((rng, row, collapsed, count,
                          width + delta if row == display_line else width,
                          new_anchor, hidden_length))
    return new_display, new_segments, new_folds, mapping


def _fold_build(text, ranges, collapsed, headless=frozenset()):
    """Fold layout for draw_text's collapsible line ranges.

    `headless` — collapsed ranges (comment runs) that may hide their HEADER
    line too, when the run carries a meltygui `# [...]` line: the whole run
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
