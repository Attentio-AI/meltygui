import bisect
import builtins as _builtins
import keyword
import re
import time

import glfw
import imgui

from src.lsd.gl_gui.model.core_model.draw_state import DropDownState
from src.lsd.gl_gui.toggles import Tint
from src.lsd.gl_gui.view.core_conversion.libcst_conversion import CodeLine
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.headers import draw_header, draw_footer
from src.lsd.gl_gui.view.core_views.search_glow import draw_search_highlight_multi
from src.lsd.gl_gui.melty import Melty, SearchTerm
from src.lsd.gl_gui.perf_trace import trace as _ptrace
from src.lsd.gl_gui.fonts import Font
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.jump_to import draw_jump_to
from src.lsd.gl_gui.view.core_views.decoration.core_decoration import defaults, Core
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.new_core_view import draw


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
    'comment': _hex('#5e6265'),  # Comment - muted & darker so comments recede
    'line_no': _hex('#808080'),  # Gutter numbers keep the old comment color
    'number': _hex('#6897bb'),  # Number
    'color3': _hex('#6897bb'),  # merged color tuple `(r, g, b[, a])` (fallback text color)
    'icon': _hex('#56b6c2'),  # Font Awesome / PUA glyph (cyan, distinct from strings)
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

    def add(name, kind):
        if name and name not in seen and len(name) > 1 and name not in _PY_KEYWORDS:
            seen.add(name)
            pool.append((name, kind))

    scan_text = text
    if code_tree is not None:
        try:
            for name, kind in completions_at(code_tree, line):
                add(name, kind)
        except Exception:
            pass  # never let a parse hiccup kill typing
        # With a parse the buffer scan only backfills JUST-TYPED locals the
        # tree hasn't caught up to - those sit at or above the caret, so cap
        # the scan there. This keeps names completions_at position-filtered
        # (locals defined below the caret) from re-entering as "name" rows.
        # No tree → scan everything; it's the only source source.
        scan_text = "\n".join(text.split("\n")[:line + 1])
    for name in _IDENT_RE.findall(scan_text):
        add(name, "name")
    for name in dir(_builtins):
        if not name.startswith("_"):
            add(name, "builtin")
    for kw in keyword.kwlist:
        if len(kw) > 1 and kw not in seen:
            seen.add(kw)
            pool.append((kw, "kw"))
    if func is not None:
        from src.lsd.gl_gui.func_metadata import FuncsMetadata, _type_name
        meta = FuncsMetadata.get(func)
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
    m = getattr(Toggles.TextEditor, "AC_SNIPPETS", None) or {}
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
              "method": "fn", "builtin": ""}


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
    `.`) keeps one group. The exact word already fully typed is dropped so we
    never suggest what's on screen."""
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
    ranked = [r for g in groups for r in g]
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


def _pos_in_string_or_comment(text, idx, offs, line_open):
    """True when a character typed at index `idx` would land inside a string
    literal or a comment — where the code popup must stay quiet. Line-local:
    resumes from the per-line string state (`line_open`, same source as the
    tokenizer) and scans only [line_start, idx). An f-string counts as string
    even inside its {braces} — a rare miss, never a false popup."""
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
            return True                       # rest of the line is comment
        if c in "\"'":
            if text.startswith(c * 3, i):
                in_str = c * 3
                i += 3
            else:
                in_str = c
                i += 1
            continue
        i += 1
    return in_str is not None


# First top-level def in the buffer - a function span's own def sits at column
# 0 (spans keep file indentation, so a method in a class span won't match).
_DEF_NAME_RE = re.compile(r'^def\s+(\w+)', re.MULTILINE)


def _ac_live_context(ds, text, address):
    """(module_globals, live_func) for the span being edited: the live module
    the file is loaded as (hotswap-aware — richest of the dual src./non-src
    identities, via code_checks._module_for) and, when the buffer is a top-level
    function span, the live function object itself (its FuncsMetadata carries
    runtime-observed scope types). Cached on the draw_state per (path,
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
        return ds._ac_jedi_members, False

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
    ds._ac_member_tints = None   # jedi path - no live receiver file to scan

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
        color = imgui.get_color_u32_rgba(text_tint[0], text_tint[1], text_tint[2], 1.0)
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
        _n_colors = 1
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


def _fmt_color_channel(v):
    """Format a 0..1 channel back into source as a FLOAT literal (the merged
    token needs at least one float channel, and a dragged value is fractional
    anyway; untouched channels keep their original text — ints stay ints)."""
    s = f"{max(0.0, min(1.0, v)):.3f}".rstrip('0')
    return s + '0' if s.endswith('.') else s


@render_func(use_cache=True, show_bg=False, shadow=False, with_header=None,
             show_name=False, selectable=False, z_offset=3)
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
    left_mouse_* declared (never read) for the event latch — see draw_icon_selector."""
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


# The default callback-widget set: when draw_text is called with no token_views,
# Font Awesome glyphs ("icon" tokens) become inline icon-picker dropdowns,
# True/False become double-click-to-toggle words, numeric literals become drag
# widgets, and color tuples (3 or 4 numeric channels, RGBA) become color
# swatches. Add more entries here to make other token kinds interactive by
# default.
# For whole_token entries char_width is also the "this is inline" flag - the
# widget REPLACES the text at exactly token-width (len(token) cells), unless
# lead_cells=N makes it an ACCESSORY: the text draws normally (shifted N cells
# right) and the widget gets only the N-cell lead area beside it. owns_mouse
# marks widgets that consume clicks (they subscribe to the left_mouse_*
# events); for REPLACE widgets draw_text then emulates the caret placement a
# text click would have given. pad_px widens a REPLACE widget's view N px per
# side past the token cells (visual breathing room; the grid stays exact).
DEFAULT_TOKEN_VIEWS = {
    # tint: the widget's bg wash, passed as a call kwarg at the draw_text call
    # sites - decorator-level for the provenance color and no longer reaches
    # render kwargs.
    "icon": {"renderer": draw_icon_selector, "char_width": 3,
             "tint": (0.77, 0.66, 0.20, 1.00)},
    "bool": {"renderer": draw_bool_token, "char_width": 1, "whole_token": True,
             "tint": (0.911, 0.305, 0.0)},
    "number": {"renderer": draw_number_token, "char_width": 1, "whole_token": True,
               "owns_mouse": True, "pad_px": 2, "tint": (0.026, 0.041, 0.056)},
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


def _shift_usage_spans(spans, splice):
    """Remap [(start, end, su, at_def)] through a text splice: spans before it
    keep, after it shift, overlapping it drop (the real recompute re-derives
    them at the next debounce expiry)."""
    p, old_end, d, _dl, _el, _oel = splice
    out = []
    for sp in spans:
        if sp[1] <= p:
            out.append(sp)
        elif sp[0] >= old_end:
            out.append((sp[0] + d, sp[1] + d) + sp[2:])
    return tuple(out)


def _shift_def_tints(res, splice):
    """Remap a cached _collect_def_tints result through a text splice — index
    entries shift like _shift_usage_spans; line entries shift by the splice's
    line delta; the block containing the edit keeps its head and stretches its
    end (typing inside a tinted class must not drop its wash)."""
    blocks, spans, line_tints, comment_tints, name_tints = res
    p, old_end, d, dl, el, oel = splice

    nb = []
    for (bl, idx, bend, tint) in blocks:
        if bend < el:
            nb.append((bl, idx, bend, tint))
        elif bl > oel:
            nb.append((bl + dl, idx + d, bend + dl, tint))
        elif bl < el or (bl == el and idx <= p):
            nb.append((bl, idx, bend + dl, tint))   # edit inside the block
        # else: block head inside the edited region - drop until recompute

    def _idx_spans(entries):
        out = []
        for sp in entries:
            if sp[1] <= p:
                out.append(sp)
            elif sp[0] >= old_end:
                out.append((sp[0] + d, sp[1] + d) + sp[2:])
        return tuple(out)

    nl = []
    for (ln, rgb, sc, si, ei) in line_tints:
        if ln < el and ei <= p:
            nl.append((ln, rgb, sc, si, ei))
        elif ln > oel and si >= old_end:
            nl.append((ln + dl, rgb, sc, si + d, ei + d))
        # else: the edited line's band - drop until recompute
    return (tuple(nb), _idx_spans(spans), tuple(nl),
            _idx_spans(comment_tints), name_tints)


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
    if _old_key != key:
        now = time.monotonic()
        if (getattr(ds, "_usage_spans", None) is not None
                and (_typing_hot()
                     or now - getattr(ds, "_usage_spans_time", 0.0) < _TINT_RECOMPUTE_MIN_S)):
            request_render()   # typing/debounced: serve held (remapped below), retry later
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
            _base, _sp_bt = text, None
            _src = getattr(code_tree, 'source', None)
            if isinstance(_src, str) and _src != text:
                _cand = _text_splice(_src, text)
                if _cand is not None and (_cand[1] - _cand[0]) <= 512 and abs(_cand[2]) <= 512:
                    _base, _sp_bt = _src, _cand
            try:
                _fresh = _collect_usage_spans(code_tree, _base, line_offset, view_path)
            except Exception:
                _fresh = ()
            if _sp_bt is not None:
                _fresh = _shift_usage_spans(_fresh, _sp_bt)
            # DEBUG timeline: a recompute that sheds >30% of the held spans is
            # the blink-out signature - name WHICH key component moved and
            # which text base was collected on.
            _prev_n = len(getattr(ds, "_usage_spans", ()) or ())
            if _prev_n >= 10 and len(_fresh) < _prev_n * 0.7:
                _why = ("cold" if _old_key is None else ",".join(
                    n for n, i in (("tree", 0), ("su", 1), ("off", 2), ("path", 3))
                    if _old_key[i] != key[i]))
                _ptrace("usage spans DROP on recompute", prev=_prev_n,
                        new=len(_fresh), changed=_why,
                        base=("source" if _sp_bt is not None else
                              "text" if _base is text else "source=text"),
                        src_is_str=isinstance(_src, str))
            ds._usage_spans = _fresh
            ds._usage_spans_key = key
            ds._usage_spans_time = now
            ds._usage_spans_text = text
            ds._usage_tc = {}   # per-(su, at_def) jump-target counts; valid per span set
    # Text drift since the held set was computed (typing between reparses):
    # remap through the edit so spans track the buffer. Chained per keystroke;
    # the stored text always reflects what the stored spans are aligned to.
    prev_text = getattr(ds, "_usage_spans_text", None)
    if (prev_text is not None and prev_text is not text
            and getattr(ds, "_usage_spans", None)):
        splice = _text_splice(prev_text, text)
        if splice is not None:
            _n0 = len(ds._usage_spans)
            ds._usage_spans = _shift_usage_spans(ds._usage_spans, splice)
            if _n0 >= 10 and len(ds._usage_spans) < _n0 * 0.7:
                _ptrace("usage spans DROP on remap", prev=_n0,
                        new=len(ds._usage_spans), splice=str(splice))
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
_DEF_TINTS_VER = 25


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

    def _block_extent(buf_line):
        if not (0 <= buf_line < len(lines)):
            return None
        # The recorded def line may be the decorated statement's first line
        # (`@defaults X`); the wash starts at the class/def keyword.
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
        return buf_line, line_start_idx[buf_line] + indent, end

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
                    blk = _block_extent(ln - 1 - line_offset)
                    if blk is not None:
                        blocks.append((*blk, tint))
                        # Save the block range under both line conventions
                        # - the index-recorded def line (may be the decorator)
                        # and the keyword class line (what the scanner's
                        # src_line reports) - so either lookup below hits.
                        rng = (blk[0], blk[2])
                        own_block_range[ln] = rng
                        own_block_range[blk[0] + 1 + line_offset] = rng
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
    from src.lsd.gl_gui.toggles import Toggles as _Tg
    fade = _Tg.TextEditor.def_propagation_fade
    if _Tg.TextEditor.def_tint_propagation:
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
    mix_on = _Tg.TextEditor.def_tint_propagation
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
    # Comment-text tints: every override comment carrying tint=(...) gets its
    # TEXT drawn in that color (not background) - the comment names a color,
    # so it wears it. [(start_index, end_index, tint)] covering the comment
    # run (or the trailing-comment tail of an assignment line).
    from src.lsd.gl_gui.view.core_conversion.libcst_conversion import _parse_override_comment
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
                # Prose lines may sit above the override in the same run -
                # find the first comment SUFFIX; only its lines wear tint.
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
    return (tuple(blocks), tuple(spans), tuple(line_tints),
            tuple(comment_tints), name_tints)


def _def_tints(ds, text, code_tree, line_offset=0, view_path=None):
    """Cached-per-(code_tree, text) wrapper around _collect_def_tints — the
    exact key discipline of _usage_spans: the top-level __symbol_usages__
    map's identity rides in the key so the background usage pass's in-place
    arrival busts the cache.

    The pending-gen key component excludes THIS file's own gen: typing here
    bumps the file's gen once per keystroke (on the bg save thread, so the
    bump lands a frame AFTER the text-change miss), and keying on the raw
    total made every keystroke pay the O(buffer) collect twice. Own edits
    are already covered by `text` / tree identity; the gen term only needs
    to catch tint-comment edits queued in OTHER files (read through the
    _XFILE PendingSave caches). A tint edit in another SPAN of this same
    file refreshes on the next tree/text churn instead of instantly.

    Recompute is additionally debounced (_TINT_RECOMPUTE_MIN_S): a key
    change inside the window serves the last-good result and re-requests a
    frame, so the trailing recompute lands once the window expires —
    fast typing pays the collect a few times a second, not per keystroke.
    Stale spans can sit a hair off the glyphs for that window; they're
    translucent washes, and the background parse churn already did this."""
    if not isinstance(code_tree, dict):
        return ((), (), (), (), {})
    su_top = code_tree.get("__symbol_usages__")
    # NO text in the key - same reasoning as _usage_spans: text-only churn is
    # handled exactly by the shift remap; a recompute on a stale tree
    # re-verified stale sites against shifted text which which washes out on
    # every inserted newline. Fresh tree / symbol attach / cross-file pending
    # gen still recompute.
    key = (_DEF_TINTS_VER, id(code_tree), id(su_top), line_offset,
           str(view_path), _pending_total_gen() - _pending_gen_of(view_path))
    if getattr(ds, "_def_tints_key", None) != key:
        now = time.monotonic()
        if (getattr(ds, "_def_tints", None) is not None
                and (_typing_hot()
                     or now - getattr(ds, "_def_tints_time", 0.0) < _TINT_RECOMPUTE_MIN_S)):
            request_render()   # typing/debounced: serve held (remapped below), retry later
        else:
            # Collect on the tree's own text + remap to the buffer - same
            # root-cause fix as _usage_spans (see there).
            _base, _sp_bt = text, None
            _src = getattr(code_tree, 'source', None)
            if isinstance(_src, str) and _src != text:
                _cand = _text_splice(_src, text)
                if _cand is not None and (_cand[1] - _cand[0]) <= 512 and abs(_cand[2]) <= 512:
                    _base, _sp_bt = _src, _cand
            try:
                _fresh = _collect_def_tints(code_tree, _base, line_offset, view_path)
            except Exception:
                _fresh = ((), (), (), (), {})
            if _sp_bt is not None:
                _fresh = _shift_def_tints(_fresh, _sp_bt)
            ds._def_tints = _fresh
            ds._def_tints_key = key
            ds._def_tints_time = now
            ds._def_tints_text = text
    # Glue the held washes to the new text (see _usage_spans).
    prev_text = getattr(ds, "_def_tints_text", None)
    if prev_text is not None and prev_text is not text:
        splice = _text_splice(prev_text, text)
        if splice is not None:
            ds._def_tints = _shift_def_tints(ds._def_tints, splice)
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
             show_name=False, with_footer=draw_footer, determines_height=False, saturation=0.2,
             selectable=False, searchable=True, bg_offset=-1.8, show_add_delete=False)
@window
def draw_text(input_value: str, height=None,
              left_mouse_down=False, 
              left_mouse_drag=False, left_mouse_held=False,
              horizontal_scroll_drag=False, search_text="", 
              ctrl_b_down=False,
              single_line=False, is_search_box=False,
              draw_state=None, request_focus=False, select_all_on_focus=False,
              wrap=False, line_height=1.149, font=Font.JETBRAINS_MONO_19, jump_to=None,
              code_tree=None, code_dict=None, error=None, token_views=None,
              import_fixes=None,
              syntax_highlight=True, is_diff=False, line_numbers=None,
              completion_source=None, unique=0):
    ds = draw_state
    # --- Perf instrumentation (typing latency) --------------------------------
    # Section marks: each _pf(label) closes the section since the previous mark.
    # One summary line per edited frame — plus any frame >= 8ms — goes to the
    # per_trace timeline (/tmp/lsd_symbol_perf.log) so draw_text's own cost can
    # be read against the background reparse/index lines around it.
    _pf_t0 = time.perf_counter()
    _pf_cpu0 = time.thread_time()   # wall≫cpu in the summary = GIL starvation
    _pf_marks = []
    _pf_tok = [0.0, 0]   # accumulated _window() cache-miss time, miss count
    _pf_info = {}        # extra facts for the summary line (span counts, cache hits)
    
    
    def _pf(label):
        _pf_marks.append((label, time.perf_counter()))

    # Plain-text mode (codec tells "not Python source"): no Darcula colors and
    # no inline token widgets - both are artifacts of the Python tokenizer.
    if not syntax_highlight:
        token_views = {}
    elif token_views is None:
        token_views = DEFAULT_TOKEN_VIEWS   # an experiment fallback (see a     

    # Symbol-usage source: the parse arrives as `code_tree` in the
    # address_to_general_parse routes, as `code_dict` in the CODE_UI routes
    # (cst_module_to_dict - which is also where the run_jedi() pass attaches
    # __symbol_usages__). Links are file-absolute, so the buffer's file offset
    # comes from the parse's line_offset when set, else from the jump_to span.
    # The FILE route passes the syntax-ERROR MARKER dict ({'__error__', ...})
    # in code_tree while the real (possibly blank-line-repaired) parse rides
    # in code_dict - the marker must not shadow the parse, or every tree-
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
    _usage_off += _pending_line_delta(getattr(jump_to, 'path', None), _usage_off)
        
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
        if _fs is not None and _fs[0] is input_value and _fs[1] is not None:
            _err_markers = _exception_errors(_fs[1])
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
    # keystroke while the file's bind cache catches up.
    _active_fixes = import_fixes
    if Toggles.TextEditor.fast_syntax_check:
        _fi = getattr(ds, '_fast_imports_state', None)
        if _fi is not None and _fi[0] is input_value and _fi[2] is import_fixes:
            _active_fixes = _fi[1]
    _qf_fixes = {}
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
        v0 = max(0, min(v0, nlines - -12))
        v1 = max(v0, min(v1, nlines - 1))
        key = (text, v0, v1, syntax_highlight, id(token_views) if token_views else 0)
        if getattr(ds, '_win_key', None) == key:
            return ds._win_data

        _pf_miss_t = time.perf_counter()
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
        
    def _try_usage_jump(pos, force_picker=False):
        """Usage jump at buffer index `pos` (Ctrl+B): one counterpart opens
        straight in IntelliJ; several open the usage-jump picker under the
        symbol. `force_picker` opens the picker even for a SINGLE counterpart
        instead of jumping straight. True if the jump or picker happened (a
        span with zero targets returns False)."""
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
                    # The picker window is LATCHED - its scroll state survives
                    # a close, so a repeat can come up mid-list with the index-0
                    # cursor scrolled offscreen. Snap it back to the top.
                    from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
                    _dd_scroll_cursor_into_view(
                        Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None)), 0)
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

    _pf("mouse")
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
                # Keep the selection cursor visible: nudge the popup window to
                # scroll the minimal amount (no-op while the row is in view).
                from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
                _dd_scroll_cursor_into_view(
                    Melty.cache.key_to_draw_state.get(getattr(ds, '_ac_menu_tile', None)),
                    _ac_idx)
                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)
                  or pressed(glfw.KEY_TAB)) and _ac_cands and not ctrl:
                chosen = _ac_cands[min(_ac_idx, len(_ac_cands) - 1)]
                anchor = getattr(ds, '_ac_anchor', ds.text_cursor_pos)
                # Replace the half-typed identifier [anchor, caret) with the
                # pick. Tab additionally overwrites the rest of the word under
                # the caret (IntelliJ semantics); Enter inserts, leaving it.
                _replace_to = ds.text_cursor_pos
                if pressed(glfw.KEY_TAB):
                    while _replace_to < len(text) and (text[_replace_to].isalnum()
                                                       or text[_replace_to] == '_'):
                        _replace_to += 1
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
                # Same scroll-into-view as the suggestion popup's arrow nav.
                from src.lsd.gl_gui.view.core_views.new_core_view import _dd_scroll_cursor_into_view
                _dd_scroll_cursor_into_view(
                    Melty.cache.key_to_draw_state.get(getattr(ds, '_uj_menu_tile', None)),
                    _uj_idx)
                _fired.discard(glfw.KEY_UP)
                _fired.discard(glfw.KEY_DOWN)
                request_render()
            elif (pressed(glfw.KEY_ENTER) or pressed(glfw.KEY_KP_ENTER)) and _uj_keys and not ctrl:
                _open_usage_ref(ds._uj_items[_uj_keys[min(_uj_idx, len(_uj_keys) - 1)]])
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
                indent = _open_bracket_indent(text, pos)
                if indent is None:
                    opener = _unclosed_opener(text, _get_line_start(text, pos))
                    indent = _get_indent(text, opener) if opener is not None \
                        else _get_indent(text, pos)
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
                indent = _open_bracket_indent(text, pos)
                if indent is None:
                    opener = _unclosed_opener(text, _get_line_start(text, pos))
                    indent = _get_indent(text, opener) if opener is not None \
                        else _get_indent(text, pos)
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
                _ntc = _dtc[4] if _dtc is not None and len(_dtc) > 4 else None
                _mtc = getattr(ds, '_ac_member_tints', None)
                if _ntc or _mtc:
                    _ac_tinted = set(_ntc or ()) | set(_mtc or ())
            if _snip is None:
                ds._ac_snips = None    # accept must not treat identifiers as snippets
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
                _ac_line = _index_to_line_col(text, ds.text_cursor_pos)[0]
                _pool_key = (id(code_tree), _ac_line, len(text))
                if getattr(ds, '_ac_pool_key', None) != _pool_key:
                    _pool_func = _ac_live_context(ds, text, jump_to)[1]
                    ds._ac_pool = _completion_pool(code_tree, text, _ac_line, _pool_func)
                    ds._ac_pool_key = _pool_key
                cands = _filter_completions(ds._ac_pool, prefix,
                                            users=_ac_users, tints=_ac_tinted)
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

    _pf("autoscroll")
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

    # Definition tints (drawn FIRST, under everything): a block wash behind
    # every tinted class/def/etc in this buffer - top-left corner at the def
    # keyword's first character, bottom at the last line before the dedent,
    # right side at the view edge - and a small wash behind every occurrence
    # of a symbol whose definition (here or in another file) carries a tint,
    # in that definition's color. Ties usages to their definitions at a glance.
    _dt_blocks = _dt_spans = _dt_lines = _dt_comments = ()
    if Toggles.TextEditor.definition_tints and not is_search_box:
        _t_dt = time.perf_counter()
        _k_dt = getattr(ds, "_def_tints_key", None)
        _dt_blocks, _dt_spans, _dt_lines, _dt_comments, _ = _def_tints(
            ds, text, _usage_tree, _usage_off,
            getattr(jump_to, 'path', None) if jump_to is not None else None)
        _pf_info['dt_call_ms'] = round((time.perf_counter() - _t_dt) * 1000.0, 1)
        _pf_info['dt_miss'] = _k_dt is not getattr(ds, "_def_tints_key", None)
        _pf_info['dt_n'] = (len(_dt_blocks), len(_dt_lines), len(_dt_spans))
        # ALL def-tint washes paint on the UNDER-text channel (same idiom as
        # the cursor-token highlight below): translucent rects must never be
        # able to land over glyphs - the tile pipeline composites re-renders
        # over prior content, so transparent-over-text accumulates copies and
        # clouds the final color with tile count.
        if Melty.channels_split:
            draw_list.channels_set_current(Core.melty.get_channel() - 1)
        # Shared background color adjustment (hsv factors + brightness clamp)
        # for every usage-tint wash below - see _bg_adjust.
        _bg_f = (Toggles.TextEditor.bg_tint_saturation,
                 Toggles.TextEditor.bg_tint_value,
                 Toggles.TextEditor.bg_min_brightness,
                 Toggles.TextEditor.bg_max_brightness)
        _dt_block_a = Toggles.TextEditor.def_block_alpha
        for _b_line, _b_idx, _b_end, _b_tint in _dt_blocks:
            sy = origin_y + _b_line * line_px
            ey = origin_y + (_b_end + 1) * line_px
            if ey < rect_min_y or sy > rect_max_y:
                continue
            sx = origin_x + _colx(_b_idx)
            _b_rgb = _bg_adjust(tuple(_b_tint[:3]), _bg_f)
            _b_col = imgui.get_color_u32_rgba(_b_rgb[0], _b_rgb[1], _b_rgb[2], _dt_block_a)
            draw_list.add_rect_filled(sx, sy, rect_max_x, ey, _b_col, 4.0)
        # Line tint (the subtlest layer, over blocks, under the symbol
        # washes): one plain wash fitting the line's TEXT extent (indent →
        # last non-ws char), in the line's color - its explicit comment tint
        # if the definition has one, else a mix of its symbol tints.
        # (Full-width and feathered/glow variants were tried and reverted:
        # a simple band hugging the relevant text wins.)
        _dt_line_a = Toggles.TextEditor.def_line_alpha
        if _dt_line_a > 0:
            _dt_line_full = Toggles.TextEditor.def_line_full_width
            for _l_line, _l_rgb, _l_sc, _l_s, _l_e in _dt_lines:
                sy = origin_y + _l_line * line_px
                ey = sy + line_px
                if ey < rect_min_y or sy > rect_max_y:
                    continue
                _la = _bg_adjust(tuple(_l_rgb[:3]), _bg_f)
                _l_col = imgui.get_color_u32_rgba(_la[0], _la[1], _la[2],
                                                  _dt_line_a * _l_sc)
                if _dt_line_full:
                    draw_list.add_rect_filled(rect_min_x, sy, rect_max_x, ey,
                                              _l_col, 0.0)
                else:
                    sx = origin_x + _colx(_l_s)
                    ex = origin_x + _colx(_l_e)
                    draw_list.add_rect_filled(sx - 3, sy, ex + 3, ey, _l_col, 3.0)
        _dt_sym_a = Toggles.TextEditor.def_symbol_alpha
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
            _sa = _bg_adjust(tuple(_s_tint[:3]), _bg_f)
            _s_col = imgui.get_color_u32_rgba(_sa[0], _sa[1], _sa[2],
                                              _dt_sym_a * _s_scale)
            draw_list.add_rect_filled(sx - 1, sy + 1, ex + 1, ey - 1, _s_col, 3.0)
        # Back to the body's text channel for everything after the washes.
        if Melty.channels_split:
            draw_list.channels_set_current(Core.melty.get_channel() + 1)

    _pf("body:washes")
    # Selection
    if _has_selection(ds):
        sel_color = (*Tint.text_selection()[:3], 0.4)
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

    _pf("body:selection")
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
    _uspans = _usage_spans(ds, text, _usage_tree, _usage_off, _u_vpath)
    _pf_info['us_call_ms'] = round((time.perf_counter() - _t_us) * 1000.0, 1)
    _pf_info['us_n'] = len(_uspans)
    _usage_line_heat = {}
    if _uspans:
        _u_vspan = (_usage_off + 1, _usage_off + text.count('\n') + 1)
        for _us, _ue, _su, _at_def in _uspans:
            u_line, _ = _index_to_line_col(text, _us)
            sy = origin_y + u_line * line_px
            if sy + line_px < rect_min_y or sy > rect_max_y:
                continue
            n = _usage_target_count(ds, _su, _at_def, _u_vpath, _u_vspan)
            if n:
                _usage_line_heat[u_line] = _usage_line_heat.get(u_line, 0) + n

    _pf("body:usage_heat")
    # Search match highlights (drawn under the text so glyphs stay readable).
    # The current match radiates a circular gradient glow with its rect cut out
    # so the matched text stays visible; the rest get a thin border. Look is
    # tunable via Toggles.SearchSettings (see search_glow.draw_search_highlight).
    if search_matches:
        for m_idx, (ms, me) in enumerate(search_matches):
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
    # Import quick-fix popover: caret parked on a line using a symbol an
    # import could bind → a small floating hint at the end of that line.
    # Deliberately independent of the error markers (the suggestions travel
    # on their own channel, and a parser error may sit on a DIFFERENT line
    # than the half-typed `json.`). Alt+Enter applies the fix (or opens the
    # chooser widget when several imports could bind the name - see the
    # keyboard block and the _qf draw_dd_menu below).
    if True:
        if _qf_fixes and Melty.text_focused_ds is draw_state \
                and not getattr(ds, '_qf_open', False):
            _po_ln = text.count('\n', 0, ds.text_cursor_pos) + 1
            _po_opts = _qf_fixes.get(_po_ln)
            if _po_opts:
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
        # Inside a tint-carrying override comment, the comment text and the
        # merged color-tuple token (color3 - the picker's `(r, g, b)` text)
        # wear the comment's adjusted color; other value widgets (numbers,
        # bools) keep their own token color.
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
            # Spans sort (start, -len): at a shared start the SHORTEST is
            # last, so bisect lands on the base symbol for a given token; the
            # short backward walk finds the chain span still covering a
            # member token at the base's end.
            _si = bisect.bisect_right(_dt_starts, src_i) - 1
            for _k in range(_si, max(-1, _si - 4), -1):
                _sp = _dt_spans[_k]
                if _sp[1] <= src_i:
                    continue
                if _sp[0] <= src_i:
                    # Mix toward the wash through the TEXT factor pair
                    # (text_tint_saturation/value + shared brightness clamp).
                    color = _mix_packed(color, _bg_adjust(tuple(_sp[2][:3]), _tx_f),
                                        _dt_mix * _sp[3])
                break
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
            # While the editor caret sits on TOP a REPLACE token, the widget
            # gets out of the way entirely: the token rides as plain text, so
            # caret, selection and typing behave like any other code, and the
            # widget returns when the caret leaves. (The widget view
            # composites ABOVE the editor tile, so a caret under it would be
            # invisible anyway.) _tv_idx is still consumed so the OTHER
            # visible widgets keep their render-order names (and state).
            _caret_in = (not _lead and Melty.text_focused_ds is ds
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
                _pad = 0 if _lead else _view.get("pad_px", 0)
                imgui.set_cursor_screen_pos((x - _pad, y))
                _w = (_lead * char_w) if _lead else (len(token) * char_w + 2 * _pad)
                # Inside a tint-carrying override comment, bool/number
                # widgets adopt the comment's (adjusted) color for clutter
                # reduction; the same widgets in code keep their own color.
                _extra = {}
                if _ct_starts is not None and color_key in ('bool', 'number'):
                    _wci = bisect.bisect_right(_ct_starts, src_i) - 1
                    if _wci >= 0 and src_i < _dt_comments[_wci][1]:
                        _wc = _comment_tint_color(_dt_comments[_wci][2])
                        _extra['text_tint'] = _wc
                        if color_key == 'bool':
                            _extra['tint'] = _wc   # tint wrapper's bg box too
                if _view.get("tint") is not None:
                    _extra.setdefault('tint', _view["tint"])
                try:
                    _res = _view["renderer"](token, width=_w, height=line_px,
                                             name=_name, **_extra)
                except Exception:                    _res = None
                imgui.set_cursor_screen_pos(_save_cur)
                if _lead:
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
                if (_view.get("owns_mouse") and not _lead
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
                            _res = _view["renderer"](_ch, width=_cw * char_w, height=line_px, name=_name,
                                                     **({'tint': _view["tint"]} if _view.get("tint") is not None else {}))
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

    _pf("body:glyphs")
    # An inline view (e.g. the icon dropdown) changed its value - splice the new
    # text in for the view's source char and report the edit, so the framework
    # reparses/saves exactly as if it were typed.
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
    if show_gutter and gutter_w > 0:
        gutter_bg = (*Tint.line_number_bg()[:3], 1.0)  # dark tinted gray
        num_color = imgui.get_color_u32_rgba(*Tint.line_number_tint()[:3], 1.0)
        cur_color = imgui.get_color_u32_rgba(*Tint.cursor_tint()[:3], 1.0)
        cur_line = _index_to_line_col(text, ds.text_cursor_pos)[0] if is_focused else -1
        # Clamp the column's top to the text body (origin_y) so the fill doesn't
        # ride up over the header bar above it; rect_min_y still works once the
        # body has scrolled up past the clip top.
        gutter_top = max(rect_min_y, origin_y)
        draw_list.push_clip_rect(left, gutter_top, left + gutter_w, rect_max_y, True)
        draw_list.add_rect_filled(left, gutter_top, left + gutter_w, rect_max_y, imgui.get_color_u32_rgba(*gutter_bg))
        # Line-tint lookup for the heat wash below: a line with a definition
        # tint draws its number with THAT color instead of the usage heat ramp.
        _dt_line_map = {l[0]: l for l in _dt_lines} if _dt_lines else {}
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
                draw_list.add_rect_filled(nx - 3.0, ly + 1, left + gutter_w - 3.0,
                                          ly + line_px - 1, _hb, 3.0)
            draw_list.add_text(nx, ly, cur_color if line_idx == cur_line else num_color, num_str)
        draw_list.pop_clip_rect()

    if changed:
        text_height = (text.count('\n') + 1) * line_px + 2
    else:
        text_height = (input_value.count('\n') + 1) * line_px + 2

    # text_width = max(vcols) if vcols else max((len(l) for l in text.split('\n')), default=0) * char_w

    _pf("gutter")
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
    # computed this frame (len guard: an old 4-tuple may linger on a
    # pre-hotswap draw_state). Cost here is one dict hit per name.
    _dt = getattr(ds, '_def_tints', None)
    _nt = _dt[4] if _ac_show and _dt is not None and len(_dt) > 4 else None
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
    # so it persists when this (slow) body is skipped. Hover/keys wake the loop;
    # background results wake it via the future's done-callback (_ac_on_future).
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
            _over = (_pop_x0 - 4 <= _mp[0] <= _pop_x0 + 400
                     and _pop_y0 - 2 <= _mp[1] <= _pop_y0 + _pop_h)
        _moved = _lm is not None and (abs(_mp[0] - _lm[0]) > 0.5 or abs(_mp[1] - _lm[1]) > 0.5)
        if not _over:
            uj_state._kbd_mode = True
        elif _moved:
            uj_state._kbd_mode = False
        uj_state._last_mouse = (_mp[0], _mp[1])

    uj_changed, uj_pick, _uj_menu_ds = draw_dd_menu(
        _uj_items, name=f"{ds.name}_uj_menu", view_offset=False, show_bg=True,
        temp=True, show_search=False, swoosh=False, closed=not _uj_show, min_height=140, bg_offset=0, auto_resize=False, min_width=500,
        window_pos=(_uj_x - draw_state.abs_left, _uj_y - draw_state.abs_top + line_px), text_align="left",
        row_tags=(getattr(ds, '_uj_tags', None) if _uj_show else None),
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
    if uj_changed and getattr(uj_pick, 'path', None) is not None:
        _open_usage_ref(uj_pick)
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
        if _fast_ok and len(text) <= Toggles.TextEditor.fast_check_max_chars:
            from src.lsd.gl_gui.view.core_conversion.new_converters import _compile_check
            ds._fast_err_state = (text, _compile_check(text))
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
                    _fi_scan = collect_import_suggestions(text, path=_fi_path)
                    ds._fast_imports_state = (text, _fi_scan or {}, import_fixes)
                else:
                    ds._fast_imports_state = None
            except Exception:
                ds._fast_imports_state = None
        elif (_fast_ok and _prev_text is not None
                and Toggles.TextEditor.fast_check_changed_region):
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
                _prev_text, text, Toggles.TextEditor.fast_check_max_chars)
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
                ds._fast_err_state = (text, _r_err)
            elif _r_status == "ambiguous" and _held_err is not None:
                # Same error re-type gets a fresh line mapping; a held
                # error from ANOTHER region keeps the extraction artifact.
                ds._fast_err_state = (text, _r_err if _inside else _keep)
            else:
                ds._fast_err_state = (text, _keep)
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
                _fi_scan = (collect_import_suggestions(text, path=_fi_path,
                                                       incremental_only=True)
                            if has_scan_state(_fi_path) else None)
                ds._fast_imports_state = (None if _fi_scan is None
                                          else (text, _fi_scan, import_fixes))
            except Exception:
                ds._fast_imports_state = None
        else:
            ds._fast_err_state = None
            ds._fast_imports_state = None
    elif getattr(ds, '_err_stale', False):
        _sp = getattr(ds, '_err_stale_pair', (None, None))
        if not (error is _sp[0] and code_tree is _sp[1]):
            ds._err_stale = False                  # a fresh parse landed

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
                changed=changed, lines=text.count('\n') + 1, breakdown=_bd,
                **_pf_info)

    if changed:
        # Timeline: WHAT changed. zip is iterator, so the scan stops at the first
        # differing char; only an (anomalous) identical-text change pays O(n).
        _old = original_input if isinstance(original_input, str) else ""
        _di = next((_j for _j, (_a, _b) in enumerate(zip(_old, text)) if _a != _b),
                   min(len(_old), len(text)))
        _ptrace("editor CHANGED", name=ds.name, old_len=len(_old), new_len=len(text),
                diff_at=_di, old=repr(_old[_di:_di + 24]), new=repr(text[_di:_di + 24]))
        return True, text
    return False, original_input