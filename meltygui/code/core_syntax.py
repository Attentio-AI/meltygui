"""core_syntax — the libcst-free cst_dict: a syntax dict whose round-trip is
text surgery on the ORIGINAL source, not code generation from the dict.

    text ──parse_to_dict──► GeneralParse (same types as libcst_conversion)
                              + gp["__origin__"] = Origin(text, items, seqs)
    user mutates the gp in place (draw_collection, focus, live_apply, …)
    general_parse_to_str(gp) = apply_edits(origin.text, diff(gp, origin))

The dict alone can't rebuild the code (it drops everything it doesn't surface),
so it never tries to. The reverse path diffs the LIVE dict against the flat
`Origin` tables — one `Item` per surfaced site (its value span, the statement
extent that moves/deletes with it, its indent, the value it was parsed as) and
one `Seq` per ordered container (a body, a parameter list, a call's arguments,
a literal's elements) — and emits `TextEdit`s into the original string:

  * a changed leaf      → replace its value span (`render`, styled on the old text)
  * an added key        → a synthesized statement / `k=v` / element at its dict position
  * a removed key       → delete its extent (leading comments included, like libcst)
  * a reordered Seq     → ONE region edit that re-concatenates the members' source
                          slices verbatim in the new order (gaps stay in their slots)

Everything not touched is copied byte-for-byte, so "unchanged code comes back
exactly" holds by construction. The parser only has to find spans for what the
dict surfaces; everything else is opaque text that survives untouched.

Parser backend: Python's `ast` (C, native char positions since 3.8) plus
`tokenize` for comments — no libcst anywhere. Both hold the GIL; the seam for a
GIL-free / incremental backend (tree-sitter) is `_Src` + `_Extractor`, which
only need (kind, start, end, fields) per node. `reparse_reusing` is the live
path for now: a full re-extract whose result reuses every unchanged value
object of the previous parse by identity, so draw_states stay stable — the
previous tree is never mutated (the studio's held tree is bubbling-wrapped:
a dict mutation there reads as a user edit).

Dict shape matches `libcst_conversion` key-for-key (assignment names, `x#1`
occurrence keys in function bodies, `func()` / `func()#N` call keys with
`__pos_names__`, `if##N` branches with `##if` / `name()##if` condition keys,
`for … in …` / `try` / `except …` block keys via `_occ_key`, `Comment` keys,
`__overrides__` from `# [k=v]` comments, `decorators` / `parameters` / `locals`
on functions, `__init__` self.X fields on classes). Positional call args bind
to a sibling def's parameter names, else to the callee's runtime signature
(same resolvers as the libcst path: builtins + the src scope), else `argN`.
"""

from __future__ import annotations

import ast
import bisect
import enum
import io
import tokenize
from dataclasses import dataclass, field
from typing import Any

from src.lsd.gl_gui.view.core_conversion.libcst_conversion import (
    GeneralParse, ClassParse, EnumParse, FunctionParse, CallParse, DecorationParse,
    Comment, CodeLine, Conditional, Loop, Try, Except, NO_DEFAULT, NoDefault, Span,
    _SKIP_PARAMS, _UNREADABLE, _float_to_str, _floats_match, _is_dunder, _occ_key,
    _override_changed, _parse_override_comment, _format_override_comment,
    _reformat_override_comment, _resolve_as_enum, _resolve_callable_by_name,
    _resolve_callable_by_parts, _cached_signature,
)

ORIGIN_KEY = "__origin__"

_DEF_TYPES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
_SIMPLE_LITERAL_TYPES = (str, int, float, bool, type(None))


class CoreSyntaxError(ValueError):
    """The edited text no longer parses. `.text` is the produced text so a
    caller can still show it (the libcst path wraps this as a ParseError)."""

    def __init__(self, message, text, lineno=0, offset=0):
        super().__init__(message)
        self.text = text
        self.lineno = lineno
        self.offset = offset


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Source text, line table, ast byte columns → char offsets                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _Src:
    """The parsed text with a line-start table. `ast` reports columns in UTF-8
    BYTES; every span here is in CHARACTERS (what str slicing and the editor
    use), converted per line only when the line isn't ASCII."""

    __slots__ = ("text", "line_starts", "newline")

    def __init__(self, text):
        self.text = text
        starts = [0]
        find = text.find
        i = find("\n")
        while i != -1:
            starts.append(i + 1)
            i = find("\n", i + 1)
        self.line_starts = starts
        crlf = text.count("\r\n")
        self.newline = "\r\n" if crlf and crlf * 2 > text.count("\n") else "\n"

    @property
    def line_count(self):
        return len(self.line_starts)

    def line_start(self, lineno):
        return self.line_starts[lineno - 1]

    def next_line_start(self, lineno):
        """Offset just past line `lineno`'s terminator (len(text) on the last line).
        `next_line_start(0)` is 0, so "the line after the cursor" works from a
        cursor that hasn't consumed anything yet."""
        if lineno <= 0:
            return 0
        if lineno < len(self.line_starts):
            return self.line_starts[lineno]
        return len(self.text)

    def line_end(self, lineno):
        """Offset of the line's terminator (or len(text)) — [line_start, line_end)
        is the line's content without its newline."""
        if lineno < len(self.line_starts):
            e = self.line_starts[lineno] - 1
            if e > 0 and self.text[e - 1] == "\r":
                e -= 1
            return e
        return len(self.text)

    def line_text(self, lineno):
        return self.text[self.line_start(lineno):self.line_end(lineno)]

    def offset(self, lineno, col_bytes):
        start = self.line_start(lineno)
        line = self.text[start:self.next_line_start(lineno)]
        if line.isascii():
            return start + col_bytes
        return start + len(line.encode("utf-8")[:col_bytes].decode("utf-8", "ignore"))

    def node_span(self, node):
        return (self.offset(node.lineno, node.col_offset),
                self.offset(node.end_lineno, node.end_col_offset))

    def linecol(self, offset):
        lineno = bisect.bisect_right(self.line_starts, offset)
        return lineno, offset - self.line_starts[lineno - 1]

    def span_obj(self, start, end) -> Span:
        (sl, sc), (el, ec) = self.linecol(start), self.linecol(end)
        return Span(sl, sc, el, ec)

    def indent_of_line(self, lineno):
        line = self.line_text(lineno)
        return line[:len(line) - len(line.lstrip())]

    def is_blank(self, lineno):
        return not self.line_text(lineno).strip()


def _scan_comments(text):
    """{lineno: (col, text)} for standalone comment lines and for trailing
    (same-line-as-code) comments. tokenize's columns are in characters."""
    standalone, trailing = {}, {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            line, col = tok.start
            if tok.line[:col].strip() == "":
                standalone[line] = (col, tok.string)
            else:
                trailing[line] = (col, tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return standalone, trailing


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Site tables                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class TextEdit:
    start: int
    end: int
    replacement: str


@dataclass
class RegionEdit:
    """A rebuilt region: `pieces` are literal strings or (start, end) source
    slices copied verbatim (with any nested edits inside them applied)."""
    start: int
    end: int
    pieces: list


@dataclass
class Item:
    """One surfaced site. `extent` is what a delete removes (leading comment
    lines and blank lines above, the statement, its trailing comment and
    newline); `core` is what a reorder moves (extent minus the blank lines
    above it); `value_span` is the editable expression, None when there is
    none (a def, a block, a parameter without a default — `slot` then says
    where a value would be inserted)."""
    path: tuple
    key: Any
    kind: str                       # value | param | kwarg | element | pair | def | block | header | comment | trailing | override | decorator | pseudo
    extent: tuple
    core: tuple
    value_span: tuple | None
    orig: Any
    indent: str = ""
    slot: int | None = None
    code_end: int | None = None     # end of the statement's code (trailing comments start here)
    seq: int | None = None
    comment_key: Any = None         # override items: the Comment key that shares their span
    shadowed: bool = False          # a later statement re-bound this key: fixed in place, never diffed


@dataclass
class Seq:
    """An ordered container of items in the source."""
    id: int
    owner: tuple                    # node path whose keys this Seq holds
    kind: str                       # body | params | args | elements | pairs | decorators
    items: list = field(default_factory=list)
    region: tuple = (0, 0)          # [first core start, last core end)
    insert_at: int = 0              # where a first member goes when there is none
    indent: str = ""
    sep: str = ", "


class Origin:
    """The residual: the parsed text plus the flat site tables."""

    def __init__(self, text):
        self.text = text
        self.src = _Src(text)
        self.items: dict[tuple, Item] = {}
        self.seqs: dict[int, Seq] = {}
        self.default_seq: dict[tuple, int] = {}     # node path → seq new keys of that node join
        self.owned: dict[tuple, list] = {}          # node path → seq ids whose deletes it answers
        self.loose: dict[tuple, list] = {}          # node path → items outside any Seq (comments, overrides)
        self.generation = 0
        self.file_path = None
        self.line_offset = 0
        self._next_seq_id = 0

    def new_seq(self, owner, kind, *, default=True, indent="", insert_at=0, sep=", "):
        seq = Seq(self._next_seq_id, owner, kind, indent=indent, insert_at=insert_at, sep=sep)
        self._next_seq_id += 1       # never len(seqs): ids must survive drop_seq
        self.seqs[seq.id] = seq
        self.owned.setdefault(owner, []).append(seq.id)
        if default:
            self.default_seq[owner] = seq.id
        return seq

    def drop_seq(self, seq):
        self.seqs.pop(seq.id, None)
        owned = self.owned.get(seq.owner)
        if owned and seq.id in owned:
            owned.remove(seq.id)
        if self.default_seq.get(seq.owner) == seq.id:
            del self.default_seq[seq.owner]

    def add(self, item, seq=None):
        if seq is not None:
            item.seq = seq.id
            seq.items.append(item)
        else:
            self.loose.setdefault(item.path[:-1], []).append(item)
        prev = self.items.get(item.path)
        if prev is not None:
            prev.shadowed = True
        self.items[item.path] = item
        return item

    def shadow(self, path):
        """A key is being re-bound: the earlier binding stays a fixed slot in its
        Seq (never diffed) and everything nested under it is unreachable from
        the dict, so its tables go. Call BEFORE extracting the new value."""
        prev = self.items.get(path)
        if prev is None:
            return
        prev.shadowed = True
        n = len(path)
        for p in [p for p in self.items if len(p) > n and p[:n] == path]:
            del self.items[p]
        for sid in [sid for sid, sq in self.seqs.items() if len(sq.owner) >= n and sq.owner[:n] == path]:
            self.drop_seq(self.seqs[sid])
        for p in [p for p in self.loose if len(p) >= n and p[:n] == path]:
            del self.loose[p]

    def seal_seq(self, seq):
        """Region = the members' cores; a body's region starts at the first
        member so the header line / blank lines above stay outside it."""
        if seq.items:
            seq.region = (seq.items[0].core[0], seq.items[-1].core[1])
        else:
            seq.region = (seq.insert_at, seq.insert_at)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                      Forward: text → GeneralParse + Origin                             ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def parse_to_dict(text, *, file_path=None, line_offset=0) -> GeneralParse:
    """Parse `text` into a GeneralParse with `gp["__origin__"]` attached.
    Raises SyntaxError when the text doesn't parse."""
    tree = ast.parse(text)
    origin = Origin(text)
    origin.file_path = file_path
    origin.line_offset = line_offset
    ex = _Extractor(origin)
    gp = ex.module(tree)
    gp.file_path = file_path
    gp.line_offset = line_offset
    gp[ORIGIN_KEY] = origin
    return gp


def _dotted_parts(node):
    """["a", "b", "c"] for a.b.c made of Names/Attributes, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        parts.reverse()
        return parts
    return None


def _call_func_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = _dotted_parts(func)
        return ".".join(parts) if parts is not None else func.attr
    return None


def _assign_target_name(stmt):
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        return stmt.target.id
    return None


def _stmt_call(stmt, nonname_targets):
    """The Call a statement surfaces as a CallParse: a bare call statement, or
    (when `nonname_targets`) a call assigned to a non-Name target."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return stmt.value
    if not nonname_targets:
        return None
    if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
        if not (len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            return stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Call):
        if not isinstance(stmt.target, ast.Name):
            return stmt.value
    return None


def _is_enum_classdef(node):
    for base in list(node.bases) + [k.value for k in node.keywords if k.arg == "metaclass"]:
        parts = _dotted_parts(base)
        last = parts[-1] if parts else None
        if last and (last.endswith("Enum") or last.endswith("Flag")):
            return True
    return False


def _param_names(funcdef):
    a = funcdef.args
    names = []
    for p in list(a.posonlyargs) + list(a.args):
        if not names and p.arg in _SKIP_PARAMS:
            continue
        names.append(p.arg)
    return names


def _local_signatures(stmts):
    return {s.name: _param_names(s) for s in stmts if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}


class _Extractor:
    def __init__(self, origin):
        self.origin = origin
        self.src = origin.src
        self.standalone, self.trailing = _scan_comments(origin.text)
        self.cursor = 0     # last consumed line (1-based); 0 = nothing yet

    # ── spans on the dict (the consumers' view: Span / _child_spans) ─────────

    def _stamp(self, obj, start, end):
        try:
            obj.span = self.src.span_obj(start, end)
        except AttributeError:
            pass
        return obj

    def _record_child(self, container, key, value, start, end):
        if isinstance(value, dict) and getattr(value, "span", None) is not None:
            return
        cs = getattr(container, "_child_spans", None)
        if cs is None:
            cs = {}
            try:
                container._child_spans = cs
            except AttributeError:
                return
        cs[key] = self.src.span_obj(start, end)

    # ── comments ─────────────────────────────────────────────────────────────

    def _leading_groups(self, upto_line):
        """Comment groups on the lines (cursor, upto_line): adjacent comment
        lines form one group, blank lines separate groups, and an override
        `# [...]` comment (possibly split over several lines) is always its
        own group. Each group: (first_line, last_line, texts, is_override)."""
        runs, run = [], []
        for ln in range(self.cursor + 1, upto_line):
            c = self.standalone.get(ln)
            if c is None:
                if run:
                    runs.append(run)
                    run = []
                continue
            run.append((ln, c[1]))
        if run:
            runs.append(run)
        groups = []
        for run in runs:
            k = plain = 0
            while k < len(run):
                ov_end = self._override_run_end(run, k)
                if ov_end is None:
                    k += 1
                    continue
                if plain < k:
                    groups.append(self._group(run[plain:k], False))
                groups.append(self._group(run[k:ov_end], True))
                k = plain = ov_end
            if plain < len(run):
                groups.append(self._group(run[plain:], False))
        return groups

    @staticmethod
    def _group(run, is_override):
        return (run[0][0], run[-1][0], [t for _, t in run], is_override)

    @staticmethod
    def _override_run_end(run, i):
        if not run[i][1].lstrip("#").strip().startswith("["):
            return None
        for j in range(i, len(run)):
            if run[j][1].rstrip().endswith("]"):
                joined = "\n".join(t for _, t in run[i:j + 1])
                if _parse_override_comment(joined) is not None:
                    return j + 1
        return None

    def _comment_extent(self, first_line, last_line):
        start = self.src.line_start(first_line)
        end = self.src.next_line_start(last_line)
        return start, end

    def _comment_span(self, first_line, last_line):
        col = self.standalone[first_line][0]
        return self.src.line_start(first_line) + col, self.src.line_end(last_line)

    def _surface_comment(self, out, path, group):
        first, last, texts, _ = group
        text = "\n".join(texts)
        c = Comment(text)
        out[c] = c
        extent = self._comment_extent(first, last)
        self.origin.add(Item(path + (c,), c, "comment", extent, extent,
                             self._comment_span(first, last), text,
                             indent=self.src.indent_of_line(first)))
        return c

    def _merge_override(self, comment, out, path, span, indent):
        """A scope-level override comment (module header, trailing, above a
        block): first one owns `__overrides__`; recorded as an override item
        sharing the Comment's span."""
        if isinstance(out.get("__overrides__"), dict):
            return
        parsed = _parse_override_comment(str(comment))
        if parsed:
            out["__overrides__"] = parsed
            self.origin.add(Item(path + ("__overrides__",), "__overrides__", "override",
                                 span, span, span, dict(parsed), indent=indent,
                                 comment_key=comment))

    def _trailing_comment(self, stmt, out, path, key):
        tc = self.trailing.get(stmt.end_lineno)
        if tc is None:
            return
        col, text = tc
        code_end = self.src.node_span(stmt)[1]
        c = Comment(text, inline=key)
        out[c] = c
        start = self.src.line_start(stmt.end_lineno) + col
        end = self.src.line_end(stmt.end_lineno)
        self.origin.add(Item(path + (c,), c, "trailing", (code_end, end), (code_end, end),
                             (start, end), text, indent=self.src.indent_of_line(stmt.end_lineno)))
        self._merge_override(c, out, path, (start, end), self.src.indent_of_line(stmt.end_lineno))

    def _consume_footer(self, body_indent_len):
        """Comment lines after a block's last statement that sit at the block's
        indent (libcst's IndentedBlock.footer) belong to nobody's dict; skip
        them so they don't become the next statement's leading comments."""
        ln = self.cursor + 1
        last_taken = self.cursor
        while ln <= self.src.line_count:
            c = self.standalone.get(ln)
            if c is not None:
                if c[0] < body_indent_len:
                    break
                last_taken = ln
            elif not self.src.is_blank(ln):
                break
            ln += 1
        self.cursor = last_taken

    # ── statement extents ────────────────────────────────────────────────────

    def _stmt_first_line(self, stmt):
        decs = getattr(stmt, "decorator_list", None)
        if decs:
            return min(stmt.lineno, decs[0].lineno)
        return stmt.lineno

    def _header_end_line(self, stmt):
        """The line the block header ends on (the `:` line) — the last non-blank,
        non-comment line before the body's first statement."""
        body = stmt.body
        ln = self._stmt_first_line(body[0]) - 1     # a decorated first member starts at its decorator
        first = self._stmt_first_line(stmt)
        while ln > first and (self.src.is_blank(ln) or ln in self.standalone):
            ln -= 1
        return ln

    def _keyword_line(self, body_stmts, low):
        """The `else:` / `finally:` / `try:` line above a body — the last code
        line before its first statement, not below `low`."""
        ln = self._stmt_first_line(body_stmts[0]) - 1
        while ln > low and (self.src.is_blank(ln) or ln in self.standalone):
            ln -= 1
        return ln

    # ── module ───────────────────────────────────────────────────────────────

    def module(self, tree):
        gp = GeneralParse(source=self.origin.text)
        seq = self.origin.new_seq((), "body", indent="", insert_at=0)
        self._body(tree.body, gp, (), seq, scope="module")
        self.origin.seal_seq(seq)
        return gp

    # ── bodies ───────────────────────────────────────────────────────────────

    def _body(self, stmts, out, path, seq, *, scope, block_state=None):
        """Walk `stmts` in source order into `out`. `scope` decides the
        surfacing rules: module / class / function (function bodies get
        occurrence keys, blocks and non-Name-target calls)."""
        is_function = scope == "function"
        nonname_calls = scope != "class"
        local_sigs = _local_signatures(stmts)
        call_seen: dict[str, int] = {}
        counts: dict[str, int] = {}
        seen: dict[str, int] = {}
        if is_function:
            for s in stmts:
                n = _assign_target_name(s)
                if n is not None:
                    counts[n] = counts.get(n, 0) + 1
        if block_state is None:
            block_state = ({"if": 0, "elif": 0, "else": 0}, {})
        cond_counters, block_occ = block_state
        prev_key = None

        for stmt in stmts:
            first_line = self._stmt_first_line(stmt)
            same_line = first_line <= self.cursor       # `a = 1; b = 2`
            gap_start = self.src.next_line_start(self.cursor) if not same_line else self.src.node_span(stmt)[0]
            groups = [] if same_line else self._leading_groups(first_line)
            core_start = self.src.line_start(groups[0][0]) if groups else (
                self.src.line_start(first_line) if not same_line else gap_start)
            indent = self.src.indent_of_line(first_line)
            is_def = isinstance(stmt, _DEF_TYPES)
            is_block = isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.Try, getattr(ast, "TryStar", ast.Try)))
            field_override = child_override = None

            for g in groups:
                if g[3]:    # override comment
                    if is_def:
                        if child_override is None:
                            child_override = g
                    elif is_block and is_function:
                        c = self._surface_comment(out, path, g)
                        self._merge_override(c, out, path, self._comment_span(g[0], g[1]),
                                             self.src.indent_of_line(g[0]))
                    elif scope == "module" and self.cursor == 0 and not out:
                        # Module header: an override comment is both a Comment and the module's __overrides__.
                        c = self._surface_comment(out, path, g)
                        self._merge_override(c, out, path, self._comment_span(g[0], g[1]),
                                             self.src.indent_of_line(g[0]))
                    elif field_override is None:
                        field_override = g
                else:
                    self._surface_comment(out, path, g)

            key = None
            if is_def:
                key = self._def(stmt, out, path, seq, scope, child_override, gap_start, core_start)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and _assign_target_name(stmt) is not None:
                name = _assign_target_name(stmt)
                if is_function:
                    occ = seen.get(name, 0)
                    seen[name] = occ + 1
                    key = name if (counts[name] == 1 or occ == 0) else f"{name}#{occ}"
                else:
                    key = name
                self.origin.shadow(path + (key,))
                value = self._value(stmt.value, path + (key,))
                out[key] = value
                vs = self.src.node_span(stmt.value)
                self._record_child(out, key, value, *vs)
                self.origin.add(Item(path + (key,), key, "value", (gap_start, 0), (core_start, 0), vs, value,
                                     indent=indent, code_end=self.src.node_span(stmt)[1]), seq)
            elif _stmt_call(stmt, nonname_calls) is not None:
                call = _stmt_call(stmt, nonname_calls)
                fname = _call_func_name(call) or "call"
                occ = call_seen.get(fname, 0)
                call_seen[fname] = occ + 1
                key = f"{fname}()" if occ == 0 else f"{fname}()#{occ}"
                self.origin.shadow(path + (key,))
                # A statement-level call is surface even with nothing readable
                # (`live_view()`): libcst's _surface_ keeps the empty CallParse
                # too, and live_view keys its capture sites on that entry.
                parsed = self._call(call, path + (key,), CallParse, local_sigs.get(fname),
                                    allow_empty=True)
                if parsed is None:
                    key = None
                else:
                    out[key] = parsed
                    cs = self.src.node_span(call)
                    self.origin.add(Item(path + (key,), key, "value", (gap_start, 0), (core_start, 0), cs, parsed,
                                         indent=indent, code_end=self.src.node_span(stmt)[1]), seq)
            elif is_function and isinstance(stmt, ast.If):
                self._if_chain(stmt, out, path, seq, cond_counters, block_occ, gap_start, core_start, indent)
            elif is_function and isinstance(stmt, (ast.For, ast.AsyncFor)):
                self._for_loop(stmt, out, path, seq, block_occ, gap_start, core_start, indent)
            elif is_function and isinstance(stmt, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                self._try_block(stmt, out, path, seq, block_occ, gap_start, core_start, indent)

            if key is not None and not is_def and not same_line:
                self._trailing_comment(stmt, out, path, key)
                if field_override is not None:
                    self._attach_field_override(field_override, out, path, key)

            if not is_def and not is_block:
                self.cursor = max(self.cursor, stmt.end_lineno)
            if key is not None:
                item = self.origin.items[path + (key,)]
                end = self.src.next_line_start(self.cursor) if not same_line else self.src.node_span(stmt)[1]
                item.extent = (item.extent[0], end)
                item.core = (item.core[0], end)
            prev_key = key

    def _attach_field_override(self, group, out, path, key):
        first, last, texts, _ = group
        parsed = _parse_override_comment("\n".join(texts))
        if not parsed:
            return
        overrides = out.get("__overrides__")
        if not isinstance(overrides, dict):
            overrides = {}
            out["__overrides__"] = overrides
        slot = f"__{key}__"
        if slot in overrides:
            return
        overrides[slot] = parsed
        span = self._comment_span(first, last)
        self.origin.add(Item(path + ("__overrides__", slot), slot, "override",
                             self._comment_extent(first, last), span, span, dict(parsed),
                             indent=self.src.indent_of_line(first)))

    # ── defs ─────────────────────────────────────────────────────────────────

    def _def(self, stmt, out, path, seq, scope, child_override, gap_start, core_start):
        name = stmt.name
        child_path = path + (name,)
        first_line = self._stmt_first_line(stmt)
        indent = self.src.indent_of_line(first_line)
        is_init = (scope == "class" and isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and name == "__init__")
        if is_init:
            # __init__ is not a member; its self.X assignments surface as class
            # fields (see _init_fields, run by _class after the body walk).
            self.cursor = max(self.cursor, stmt.end_lineno)
            return None
        code_span = (self.src.line_start(first_line) + len(indent), self.src.node_span(stmt)[1])
        self.origin.shadow(child_path)
        if isinstance(stmt, ast.ClassDef):
            child = self._class(stmt, child_path)
        else:
            child = self._function(stmt, child_path)
        out[name] = child
        item = self.origin.add(Item(child_path, name, "def", (gap_start, 0), (core_start, 0), None, None,
                                    indent=indent, code_end=code_span[1]), seq)
        if child_override is not None:
            parsed = _parse_override_comment("\n".join(child_override[2]))
            if parsed:
                existing = child.get("__overrides__")
                if isinstance(existing, dict):
                    for k, v in parsed.items():
                        existing.setdefault(k, v)
                else:
                    child["__overrides__"] = parsed
                span = self._comment_span(child_override[0], child_override[1])
                self.origin.add(Item(child_path + ("__overrides__",), "__overrides__", "override",
                                     self._comment_extent(child_override[0], child_override[1]), span, span,
                                     dict(parsed), indent=self.src.indent_of_line(child_override[0])))
        return name

    def _class(self, node, path):
        cls = EnumParse if _is_enum_classdef(node) else ClassParse
        first_line = self._stmt_first_line(node)
        start = self.src.line_start(first_line) + len(self.src.indent_of_line(first_line))
        end = self.src.node_span(node)[1]
        readable = cls(source=self.origin.text[start:end])
        readable.def_name = node.name
        self._stamp(readable, start, end)
        decorators = self._decorators(node, path)
        if decorators:
            readable["decorators"] = decorators
        self.cursor = self._header_end_line(node)
        body_indent = self.src.indent_of_line(node.body[0].lineno)
        seq = self.origin.new_seq(path, "body", indent=body_indent,
                                  insert_at=self.src.line_start(node.body[0].lineno))
        self._body(node.body, readable, path, seq, scope="class")
        self._consume_footer(len(body_indent))
        self.origin.seal_seq(seq)
        self._init_fields(node, readable, path)
        return readable

    def _init_fields(self, node, readable, path):
        init = next((s for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and s.name == "__init__"), None)
        if init is None:
            return
        body_indent = self.src.indent_of_line(init.body[0].lineno)
        seq = self.origin.new_seq(path, "body", default=False, indent=body_indent,
                                  insert_at=self.src.line_start(init.body[0].lineno))
        for stmt in init.body:
            target = value = None
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign):
                target, value = stmt.target, stmt.value
            if (target is None or value is None or not isinstance(target, ast.Attribute)
                    or not isinstance(target.value, ast.Name) or target.value.id != "self"):
                continue
            attr = target.attr
            if attr in readable:
                continue
            py = self._value(value, path + (attr,))
            readable[attr] = py
            vs = self.src.node_span(value)
            self._record_child(readable, attr, py, *vs)
            extent = (self.src.line_start(stmt.lineno), self.src.next_line_start(stmt.end_lineno))
            self.origin.add(Item(path + (attr,), attr, "value", extent, extent, vs, py,
                                 indent=self.src.indent_of_line(stmt.lineno),
                                 code_end=self.src.node_span(stmt)[1]), seq)
        self.origin.seal_seq(seq)

    def _function(self, node, path):
        first_line = self._stmt_first_line(node)
        start = self.src.line_start(first_line) + len(self.src.indent_of_line(first_line))
        end = self.src.node_span(node)[1]
        readable = FunctionParse(source=self.origin.text[start:end])
        readable.def_name = node.name
        self._stamp(readable, start, end)
        decorators = self._decorators(node, path)
        if decorators:
            readable["decorators"] = decorators
        params = self._params(node, path + ("parameters",))
        if params:
            readable["parameters"] = params
            self.origin.add(Item(path + ("parameters",), "parameters", "pseudo", (0, 0), (0, 0), None, params))
        self.cursor = self._header_end_line(node)
        body_indent = self.src.indent_of_line(node.body[0].lineno)
        locals_ = GeneralParse(source="")
        seq = self.origin.new_seq(path + ("locals",), "body", indent=body_indent,
                                  insert_at=self.src.line_start(node.body[0].lineno))
        self._body(node.body, locals_, path + ("locals",), seq, scope="function")
        self._consume_footer(len(body_indent))
        self.origin.seal_seq(seq)
        if locals_:
            bs, be = self.src.node_span(node.body[0])[0], self.src.node_span(node.body[-1])[1]
            self._stamp(locals_, bs, be)
            readable["locals"] = locals_
            self.origin.add(Item(path + ("locals",), "locals", "pseudo", (0, 0), (0, 0), None, locals_))
        else:
            self.origin.drop_seq(seq)
        return readable

    def _decorators(self, node, path):
        if not node.decorator_list:
            return {}
        result = {}
        dpath = path + ("decorators",)
        seq = self.origin.new_seq(dpath, "decorators",
                                  indent=self.src.indent_of_line(node.decorator_list[0].lineno),
                                  insert_at=self.src.line_start(node.decorator_list[0].lineno))
        for dec in node.decorator_list:
            extent = (self.src.line_start(dec.lineno), self.src.next_line_start(dec.end_lineno))
            if isinstance(dec, ast.Call):
                name = _call_func_name(dec)
                if name is None:
                    continue
                self.origin.shadow(dpath + (name,))
                parsed = self._call(dec, dpath + (name,), DecorationParse, None, allow_empty=True)
                if parsed is None:
                    parsed = CodeLine(self._text(dec))
                result[name] = parsed
                self.origin.add(Item(dpath + (name,), name, "decorator", extent, extent,
                                     self.src.node_span(dec), parsed,
                                     indent=self.src.indent_of_line(dec.lineno)), seq)
            else:
                code = self._text(dec)
                result[code] = code
                self.origin.add(Item(dpath + (code,), code, "decorator", extent, extent,
                                     self.src.node_span(dec), code,
                                     indent=self.src.indent_of_line(dec.lineno)), seq)
        self.origin.seal_seq(seq)
        if result:
            self.origin.add(Item(dpath, "decorators", "pseudo", (0, 0), (0, 0), None, result))
        else:
            self.origin.drop_seq(seq)
        return result

    def _params(self, node, path):
        a = node.args
        ordered = []    # (arg, default) in mstion order: params, posonly, kwonly
        n_pos = len(a.posonlyargs) + len(a.args)
        defaults = [None] * (n_pos - len(a.defaults)) + list(a.defaults)
        regular = [(p, defaults[len(a.posonlyargs) + i]) for i, p in enumerate(a.args)]
        posonly = [(p, defaults[i]) for i, p in enumerate(a.posonlyargs)]
        kwonly = [(p, a.kw_defaults[i]) for i, p in enumerate(a.kwonlyargs)]
        ordered = regular + posonly + kwonly
        if not ordered:
            return None
        result = GeneralParse(source="")
        all_params = [p for p, _ in ordered]
        seq = self.origin.new_seq(path, "params", sep=", ")
        first = True
        for p, default in ordered:
            if p.arg in _SKIP_PARAMS:
                continue
            ps = self.src.node_span(p)
            if first:
                seq.insert_at = ps[0]
                first = False
            if default is not None:
                value = self._value(default, path + (p.arg,))
                vs = self.src.node_span(default)
                extent = (ps[0], vs[1])
            else:
                value = NO_DEFAULT
                vs = None
                extent = ps
            result[p.arg] = value
            if vs is not None:
                self._record_child(result, p.arg, value, *vs)
            self.origin.add(Item(path + (p.arg,), p.arg, "param", extent, extent, vs, value,
                                 slot=ps[1]), seq)
        if all_params:
            self._stamp(result, self.src.node_span(all_params[0])[0],
                        max(self.src.node_span(p)[1] for p in all_params))
        # Separator style from the source: multi-line parameter lists keep their line breaks.
        if len(seq.items) >= 2:
            seq.sep = self.origin.text[seq.items[0].extent[1]:seq.items[1].extent[0]]
        self.origin.seal_seq(seq)
        if not result:
            self.origin.drop_seq(seq)
            return None
        return result

    # ── blocks (nested bodies) ─────────────────────────────────────────────

    def _block_body(self, stmts, out, path, header_line, block_state):
        """Extract a nested block body into `out` (a Conditional/Loop/Try)."""
        self.cursor = header_line
        body_indent = self.src.indent_of_line(stmts[0].lineno)
        seq = self.origin.new_seq(path, "body", indent=body_indent,
                                  insert_at=self.src.line_start(stmts[0].lineno))
        self._body(stmts, out, path, seq, scope="function")
        self._consume_footer(len(body_indent))
        self.origin.seal_seq(seq)
        return seq

    def _finish_block_item(self, key, path, seq, gap_start, core_start, indent, kind="block"):
        end = self.src.next_line_start(self.cursor)
        self.origin.add(Item(path + (key,), key, kind, (gap_start, end), (core_start, end), None, None,
                             indent=indent), seq)

    def _condition(self, test, branch_path, keyword):
        call = test.value if isinstance(test, ast.Subscript) and isinstance(test.value, ast.Call) else test
        if isinstance(call, ast.Call):
            cond_key = f"{_call_func_name(call) or 'call'}()##{keyword}"
        else:
            cond_key = f"##{keyword}"
        if isinstance(test, ast.Subscript) and isinstance(test.value, ast.Call):
            inner = self._call(test.value, branch_path + (cond_key,), CallParse, None)
            value = inner if inner is not None else CodeLine(self._text(test))
        else:
            value = self._value(test, branch_path + (cond_key,))
        return cond_key, value

    def _if_chain(self, node, out, path, seq, cond_counters, block_occ, gap_start, core_start, indent):
        first = True
        while True:
            keyword = "if" if first else "elif"
            idx = cond_counters[keyword]
            cond_counters[keyword] += 1
            key = f"{keyword}##{idx}"
            branch_path = path + (key,)
            branch = Conditional(condition=key)
            cond_key, cond_value = self._condition(node.test, branch_path, keyword)
            branch[cond_key] = cond_value
            ts = self.src.node_span(node.test)
            self._record_child(branch, cond_key, cond_value, *ts)
            self.origin.add(Item(branch_path + (cond_key,), cond_key, "header", ts, ts, ts, cond_value))
            self._block_body(node.body, branch, branch_path, node.test.end_lineno, (cond_counters, block_occ))
            self._stamp(branch, ts[0], self.src.node_span(node.body[-1])[1])
            out[key] = branch
            self._finish_block_item(key, path, seq, gap_start, core_start, indent)
            first = False
            orelse = node.orelse
            if not orelse:
                return
            if (len(orelse) == 1 and isinstance(orelse[0], ast.If)
                    and self.src.line_text(orelse[0].lineno).lstrip().startswith("elif")):
                node = orelse[0]
                gap_start = core_start = self.src.line_start(node.lineno)
                indent = self.src.indent_of_line(node.lineno)
                continue
            idx = cond_counters["else"]
            cond_counters["else"] += 1
            key = f"else##{idx}"
            branch_path = path + (key,)
            branch = Conditional(condition=key)
            kw_line = self._keyword_line(orelse, self.cursor)
            else_seq = self._block_body(orelse, branch, branch_path, kw_line, (cond_counters, block_occ))
            if branch:
                self._stamp(branch, self.src.line_start(kw_line), self.src.node_span(orelse[-1])[1])
                out[key] = branch
                gs = self.src.line_start(kw_line)
                self._finish_block_item(key, path, seq, gs, gs, self.src.indent_of_line(kw_line))
            else:
                self.origin.drop_seq(else_seq)
            return

    def _for_loop(self, node, out, path, seq, block_occ, gap_start, core_start, indent):
        target = self._text(node.target)
        it = self._text(node.iter)
        key = _occ_key(f"for {target} in {it}", block_occ)
        loop_path = path + (key,)
        loop = Loop(target=target, iter=it)
        self._block_body(node.body, loop, loop_path, node.iter.end_lineno, None)
        range_args = self._range_args(node.iter, loop_path + ("range",))
        if range_args is not None:
            loop["range"] = range_args
            rs = (self.src.node_span(node.iter.args[0])[0], self.src.node_span(node.iter.args[-1])[1])
            self._record_child(loop, "range", range_args, *rs)
            self.origin.add(Item(loop_path + ("range",), "range", "value", rs, rs, rs, range_args))
        self._stamp(loop, *self.src.node_span(node))
        out[key] = loop
        self._finish_block_item(key, path, seq, gap_start, core_start, indent)
        if node.orelse:
            branch = Conditional(condition="else")
            kw_line = self._keyword_line(node.orelse, self.cursor)
            else_seq = self._block_body(node.orelse, branch, path + (f"{key} else",), kw_line, None)
            if branch:
                self._stamp(branch, self.src.line_start(kw_line), self.src.node_span(node.orelse[-1])[1])
                out[f"{key} else"] = branch
                gs = self.src.line_start(kw_line)
                self._finish_block_item(f"{key} else", path, seq, gs, gs, self.src.indent_of_line(kw_line))
            else:
                self.origin.drop_seq(else_seq)

    def _range_args(self, iter_node, path):
        if not (isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Name)
                and iter_node.func.id == "range" and iter_node.args and not iter_node.keywords):
            return None
        args = []
        seq = self.origin.new_seq(path, "elements", sep=", ")
        for i, a in enumerate(iter_node.args):
            v = self._literal(a)
            if v is _UNREADABLE:
                self.origin.drop_seq(seq)
                for j in range(i):
                    self.origin.items.pop(path + (j,), None)
                return None
            args.append(v)
            sp = self.src.node_span(a)
            self.origin.add(Item(path + (i,), i, "element", sp, sp, sp, v), seq)
        self.origin.seal_seq(seq)
        return args

    def _try_block(self, node, out, path, seq, block_occ, gap_start, core_start, indent):
        try_key = _occ_key("try", block_occ)
        body = Try(header="try")
        tseq = self._block_body(node.body, body, path + (try_key,), node.lineno, None)
        if body:
            self._stamp(body, self.src.node_span(node.body[0])[0], self.src.node_span(node.body[-1])[1])
            out[try_key] = body
            self._finish_block_item(try_key, path, seq, gap_start, core_start, indent)
        else:
            self.origin.drop_seq(tseq)
        star = isinstance(node, getattr(ast, "TryStar", ())) and not isinstance(node, ast.Try)
        for handler in node.handlers:
            parts = ["except*" if star else "except"]
            if handler.type is not None:
                parts.append(self._text(handler.type))
            if handler.name is not None:
                parts += ["as", handler.name]
            header = " ".join(parts)
            hkey = _occ_key(header, block_occ)
            hbody = Except(header=header)
            hline = handler.type.end_lineno if handler.type is not None else handler.lineno
            hseq = self._block_body(handler.body, hbody, path + (hkey,), hline, None)
            if hbody:
                self._stamp(hbody, *self.src.node_span(handler))
                out[hkey] = hbody
                gs = self.src.line_start(handler.lineno)
                self._finish_block_item(hkey, path, seq, gs, gs, self.src.indent_of_line(handler.lineno))
            else:
                self.origin.drop_seq(hseq)
        if node.orelse:
            ekey = _occ_key("try else", block_occ)
            ebody = Try(header="try else")
            kw_line = self._keyword_line(node.orelse, self.cursor)
            eseq = self._block_body(node.orelse, ebody, path + (ekey,), kw_line, None)
            if ebody:
                self._stamp(ebody, self.src.line_start(kw_line), self.src.node_span(node.orelse[-1])[1])
                out[ekey] = ebody
                gs = self.src.line_start(kw_line)
                self._finish_block_item(ekey, path, seq, gs, gs, self.src.indent_of_line(kw_line))
            else:
                self.origin.drop_seq(eseq)
        if node.finalbody:
            fkey = _occ_key("finally", block_occ)
            fbody = Try(header="finally")
            kw_line = self._keyword_line(node.finalbody, self.cursor)
            fseq = self._block_body(node.finalbody, fbody, path + (fkey,), kw_line, None)
            if fbody:
                self._stamp(fbody, self.src.line_start(kw_line), self.src.node_span(node.finalbody[-1])[1])
                out[fkey] = fbody
                gs = self.src.line_start(kw_line)
                self._finish_block_item(fkey, path, seq, gs, gs, self.src.indent_of_line(kw_line))
            else:
                self.origin.drop_seq(fseq)

    # ── values ───────────────────────────────────────────────────────────────

    def _text(self, node):
        s, e = self.src.node_span(node)
        return self.origin.text[s:e]

    def _literal(self, node):
        """A plain literal's Python value, else _UNREADABLE (no containers)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, _SIMPLE_LITERAL_TYPES):
            return node.value
        if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd))
                and isinstance(node.operand, ast.Constant)
                and isinstance(node.operand.value, (int, float)) and not isinstance(node.operand.value, bool)):
            return -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value
        return _UNREADABLE

    def _value(self, node, path):
        """The Python value of an expression: literals, containers of values
        (elements recorded as items), calls as CallParse, src-resolvable names
        as live objects, anything else as CodeLine(source)."""
        lit = self._literal(node)
        if lit is not _UNREADABLE:
            return lit
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Call):
            inner = self._call(node.value, path, CallParse, None)
            if inner is not None:
                return inner
            return CodeLine(self._text(node))
        if isinstance(node, ast.Call):
            parsed = self._call(node, path, CallParse, None)
            return parsed if parsed is not None else CodeLine(self._text(node))
        if isinstance(node, (ast.Tuple, ast.List)):
            if any(isinstance(e, ast.Starred) for e in node.elts):
                return CodeLine(self._text(node))
            seq = self.origin.new_seq(path, "elements", sep=", ")
            values = []
            for i, e in enumerate(node.elts):
                v = self._value(e, path + (i,))
                values.append(v)
                sp = self.src.node_span(e)
                self.origin.add(Item(path + (i,), i, "element", sp, sp, sp, v), seq)
            if len(seq.items) >= 2:
                seq.sep = self.origin.text[seq.items[0].extent[1]:seq.items[1].extent[0]]
            if seq.items:
                self.origin.seal_seq(seq)
            else:
                self.origin.drop_seq(seq)
            return tuple(values) if isinstance(node, ast.Tuple) else values
        if isinstance(node, ast.Dict):
            if any(k is None for k in node.keys):
                return CodeLine(self._text(node))
            keys = []
            for k in node.keys:
                try:
                    kv = ast.literal_eval(k)
                    hash(kv)
                except Exception:
                    return CodeLine(self._text(node))
                keys.append(kv)
            seq = self.origin.new_seq(path, "pairs", sep=", ")
            result = {}
            for k, knode, vnode in zip(keys, node.keys, node.values):
                self.origin.shadow(path + (k,))
                v = self._value(vnode, path + (k,))
                result[k] = v
                ks, ke = self.src.node_span(knode)
                vs = self.src.node_span(vnode)
                self.origin.add(Item(path + (k,), k, "pair", (ks, vs[1]), (ks, vs[1]), vs, v), seq)
            if len(seq.items) >= 2:
                seq.sep = self.origin.text[seq.items[0].extent[1]:seq.items[1].extent[0]]
            if seq.items:
                self.origin.seal_seq(seq)
            else:
                self.origin.drop_seq(seq)
            return result
        if isinstance(node, ast.Set):
            elements = []
            for e in node.elts:
                v = self._literal(e)
                if v is _UNREADABLE:
                    return CodeLine(self._text(node))
                elements.append(v)
            return set(elements)
        if isinstance(node, (ast.Name, ast.Attribute)):
            parts = _dotted_parts(node)
            if parts is not None:
                if len(parts) == 1:
                    resolved = _resolve_callable_by_name(parts[0])
                else:
                    resolved = _resolve_as_enum(parts)
                    if resolved is _UNREADABLE:
                        resolved = _resolve_callable_by_parts(parts)
                if resolved is not _UNREADABLE:
                    return resolved
        return CodeLine(self._text(node))

    @staticmethod
    def _runtime_positional_names(call):
        """Ordered positional parameter names of the resolved callee (leading
        self/cls dropped, stops at *args), or None when it can't be resolved or
        inspected — mirrors libcst_conversion._call_positional_param_names."""
        func = call.func
        obj = _UNREADABLE
        if isinstance(func, ast.Name):
            obj = _resolve_callable_by_name(func.id)
        elif isinstance(func, ast.Attribute):
            parts = _dotted_parts(func)
            if parts is not None:
                obj = _resolve_callable_by_parts(parts)
        if obj is _UNREADABLE or not callable(obj):
            return None
        try:
            sig = _cached_signature(obj)
        except TypeError:
            sig = None
        if sig is None:
            return None
        names = []
        for p in sig.parameters.values():
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                if not names and p.name in _SKIP_PARAMS:
                    continue
                names.append(p.name)
            elif p.kind == p.VAR_POSITIONAL:
                break
        return names

    def _call(self, call, path, result_cls, pos_names_override, allow_empty=False):
        """Arguments keyed by parameter name (see CallParse). Returns None when
        nothing is readable (the caller falls back to a CodeLine) unless
        `allow_empty`."""
        readable = result_cls(source=self._text(call), func_name=_call_func_name(call))
        positional = [a for a in call.args if not isinstance(a, ast.Starred)]
        if pos_names_override is not None:
            pos_names = pos_names_override
        elif positional:
            # The callee's runtime signature (builtins + the src scope, like the
            # libcst path - `live_view(x)` calls its arg `value`), else argN.
            pos_names = self._runtime_positional_names(call)
            if pos_names is None:
                pos_names = [f"arg{i}" for i in range(len(positional))]
                if any(n in {k.arg for k in call.keywords} for n in pos_names):
                    pos_names = None
        else:
            pos_names = None
        seq = self.origin.new_seq(path, "args", sep=", ")
        pos_idx = 0
        for a in call.args:
            if isinstance(a, ast.Starred):
                continue
            if pos_names is not None and pos_idx < len(pos_names):
                k = pos_names[pos_idx]
                v = self._value(a, path + (k,))
                readable[k] = v
                sp = self.src.node_span(a)
                self._record_child(readable, k, v, *sp)
                self.origin.add(Item(path + (k,), k, "element", sp, sp, sp, v), seq)
            pos_idx += 1
        for kw in call.keywords:
            if kw.arg is None:
                continue        # **splat passes through in the text
            v = self._value(kw.value, path + (kw.arg,))
            readable[kw.arg] = v
            vs = self.src.node_span(kw.value)
            ks = self.src.node_span(kw)[0]
            self._record_child(readable, kw.arg, v, *vs)
            self.origin.add(Item(path + (kw.arg,), kw.arg, "kwarg", (ks, vs[1]), (ks, vs[1]), vs, v), seq)
        if len(seq.items) >= 2:
            seq.sep = self.origin.text[seq.items[0].extent[1]:seq.items[1].extent[0]]
        if not seq.items:
            # Insertion point for the first argument: just inside the closing brace.
            seq.insert_at = self.src.node_span(call)[1] - 1
        self.origin.seal_seq(seq)
        if not readable and not allow_empty:
            self.origin.drop_seq(seq)
            return None
        if pos_names:
            readable["__pos_names__"] = list(pos_names)
        self._stamp(readable, *self.src.node_span(call))
        return readable


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Value codec: equality + rendering styled on the old text                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _parse_kind(obj):
    """The parse class of a node, seen through a bubbling subclass (the studio's
    held tree is reclassed in place to `Bubbling_<Base>`)."""
    t = type(obj)
    # Generated `Bubbling_<Base>` reclasses AND the static `_BubblingDict` /
    # `_BubblingList` copies that replace plain container containers.
    if t.__module__.endswith(".bubbling") or t.__name__.startswith("Bubbling_"):
        from src.lsd.gl_gui.view.core_conversion.bubbling import base_of_bubbling
        return base_of_bubbling(t)
    return t


def _same_kind(a, b):
    """Same container family (a bubbling list IS a list), else same type."""
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list)
    if isinstance(a, tuple) or isinstance(b, tuple):
        return isinstance(a, tuple) and isinstance(b, tuple)
    if isinstance(a, dict) or isinstance(b, dict):
        return isinstance(a, dict) and isinstance(b, dict)
    return type(a) is type(b)


def values_equal(a, b):
    """Semantic equality for leaf values: the float32-noise rule for floats,
    type-strict for bools/ints/strs (1 vs 1.0 vs True are different code),
    identity for enum members and callables."""
    if a is b:
        return True
    if isinstance(a, NoDefault) or isinstance(b, NoDefault):
        return isinstance(a, NoDefault) and isinstance(b, NoDefault)   # TODO: any instance (pickle)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, float) and isinstance(b, float):
        return _floats_match(a, b)
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        return type(a) is type(b) and a == b
    if isinstance(a, CodeLine) or isinstance(b, CodeLine):
        return type(a) is type(b) and str(a) == str(b)
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, (list, tuple)) and _same_kind(a, b):
        return len(a) == len(b) and all(values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        ka = [k for k in a if not _is_dunder(k)]
        kb = [k for k in b if not _is_dunder(k)]
        return ka == kb and all(values_equal(a[k], b[k]) for k in ka)
    if isinstance(a, set) and isinstance(b, set):
        return a == b
    if isinstance(a, enum.Enum) or callable(a) or isinstance(b, enum.Enum) or callable(b):
        return False
    try:
        return bool(a == b)
    except Exception:
        return False


def _qualname_text(obj):
    qualname = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
    if not qualname:
        return None
    parts = [p for p in qualname.split(".") if not p.startswith("<")]
    return ".".join(parts) if parts else None


def _render_str(value, old_text):
    if (old_text and len(old_text) >= 2 and old_text[0] in "'\"" and old_text[-1] == old_text[0]
            and not old_text.startswith(("'''", '"""'))):
        q = old_text[0]
        escaped = (value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
                   .replace("\t", "\\t").replace(q, "\\" + q))
        return f"{q}{escaped}{q}"
    return repr(value)


def _container_style(old_text):
    """(open, close, sep, trailing_comma) reusing the old literal's layout: a
    multi-line literal keeps its line breaks and continuation indent."""
    if not old_text:
        return None
    opener, closer = old_text[0], old_text[-1]
    if opener not in "([{" or closer not in ")]}":
        return None
    inner = old_text[1:-1]
    trailing = inner.rstrip().endswith(",")
    sep = ", "
    if "\n" in inner:
        after = inner.split("\n", 1)[1]
        indent = after[:len(after) - len(after.lstrip())]
        sep = ",\n" + indent
    return opener, closer, sep, trailing


def render(value, old_text=None, orig=None):
    """Source text for `value`, styled on `old_text` (the text it replaces)."""
    if isinstance(value, CodeLine):
        return str(value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, float):
        return _float_to_str(value, old_text if isinstance(orig, float) else None)
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, Comment):
        return str(value)
    if isinstance(value, str):
        return _render_str(value, old_text if isinstance(orig, str) and not isinstance(orig, CodeLine) else None)
    if isinstance(value, enum.Enum):
        if isinstance(orig, enum.Enum) and type(orig) is type(value) and old_text and "." in old_text:
            return old_text.rsplit(".", 1)[0] + "." + value.name
        return f"{type(value).__qualname__}.{value.name}"
    if isinstance(value, CallParse):
        args = ", ".join(f"{k}={render(v)}" for k, v in value.items() if not _is_dunder(k))
        return f"{value.func_name or 'call'}({args})"
    if isinstance(value, dict):
        style = _container_style(old_text) if isinstance(orig, dict) else None
        opener, closer, sep, trailing = style or ("{", "}", ", ", False)
        parts = [f"{render(k)}: {render(v)}" for k, v in value.items() if not _is_dunder(k)]
        return opener + sep.join(parts) + ("," if trailing and parts else "") + closer
    if isinstance(value, (list, tuple)):
        is_tuple = isinstance(value, tuple)
        style = _container_style(old_text) if _same_kind(orig, value) else None
        if style is None:
            if is_tuple and old_text and isinstance(orig, tuple) and old_text[0] != "(":
                opener, closer = "", ""        # a bare `x = 1, 2` tuple keeps its bareness
            else:
                opener, closer = ("(", ")") if is_tuple else ("[", "]")
            sep, trailing = ", ", False
        else:
            opener, closer, sep, trailing = style
        parts = [render(v) for v in value]
        if is_tuple and len(parts) == 1:
            return opener + parts[0] + "," + closer
        return opener + sep.join(parts) + ("," if trailing and parts else "") + closer
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(render(v) for v in sorted(value, key=repr)) + "}" if value else "set()"
    if callable(value):
        name = _qualname_text(value)
        if name is not None:
            return name
    return repr(value)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Reverse: diff the live dict against the Origin → text edits                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _managed_keys(node):
    if isinstance(node, dict):
        return [k for k in node if not _is_dunder(k)]
    if isinstance(node, (list, tuple)):
        return list(range(len(node)))
    return []


def diff(gp, origin=None):
    """All edits that turn `origin.text` into the text `gp` now describes."""
    if origin is None:
        origin = gp[ORIGIN_KEY]
    edits = []
    _walk(gp, (), origin, edits)
    return edits


def _walk(node, path, origin, edits):
    keys = _managed_keys(node)
    if isinstance(node, dict):
        # Dunder-named DEFS are editable too - `__init__` methods, the chain's
        # synthetic `__melty_*_wrap__` snippet wrappers - just never reordered.
        keys += [k for k in node if _is_dunder(k)
                 and getattr(origin.items.get(path + (k,)), "kind", None) == "def"]
    present_by_seq: dict[int, list] = {}
    new_keys = []
    for k in keys:
        item = origin.items.get(path + (k,))
        if item is None:
            new_keys.append(k)
            continue
        if item.seq is not None and not _is_dunder(k):
            present_by_seq.setdefault(item.seq, []).append(k)
        if item.kind in ("comment", "trailing"):
            _diff_comment(node[k], item, origin, edits)
        elif item.kind == "override":
            continue
        else:
            _diff_value(node[k], item, path + (k,), origin, edits)
    if isinstance(node, dict) and type(node) is not dict:
        _diff_overrides(node, path, origin, edits)
    # Comments / trailing comments live outside the Seqs: a key that drops the dict is a delete.
    for item in origin.loose.get(path, ()):
        if (item.kind in ("comment", "trailing") and not item.shadowed
                and not (isinstance(node, dict) and item.key in node)):
            edits.append(TextEdit(*item.extent, ""))

    for seq_id in origin.owned.get(path, []):
        seq = origin.seqs[seq_id]
        present = present_by_seq.get(seq_id, [])
        present_set = set(present)
        # Source order of the keys, each at its first binding (a re-bound key
        # keeps its first slot in dict order too); dunders never take part.
        old = []
        for it in seq.items:
            if not _is_dunder(it.key) and it.key not in old:
                old.append(it.key)
        removed = [k for k in old if k not in present_set]
        adds = [k for k in new_keys if origin.default_seq.get(path) == seq_id
                and not isinstance(node[k], GeneralParse)]
        reordered = [k for k in old if k in present_set] != present
        if not (removed or adds or reordered):
            continue
        if seq.kind in ("body", "decorators") and not reordered:
            for k in removed:
                edits.append(TextEdit(*origin.items[path + (k,)].extent, ""))
            for k in adds:
                edits.append(_insert_member(node, k, path, seq, origin))
        else:
            edits.append(_rebuild_seq(node, keys, path, seq, present_set, set(adds), origin))


def _fixed(item):
    """Members the diff never touches and a rebuild keeps in their slot: dunder
    keys (`__all__`, the libcst patcher's _is_dunder rule) and bindings a later
    statement shadowed."""
    return item.shadowed or _is_dunder(item.key)


def _diff_value(new, item, path, origin, edits):
    orig = item.orig
    seq_id = origin.default_seq.get(path)
    if isinstance(new, dict) and isinstance(orig, dict) and seq_id is not None:
        _walk(new, path, origin, edits)
        return
    if isinstance(new, (list, tuple)) and _same_kind(new, orig) and seq_id is not None:
        _walk(new, path, origin, edits)
        return
    if item.kind in ("def", "block", "pseudo"):
        if isinstance(new, dict):
            _walk(new, path, origin, edits)
        return
    if item.value_span is None:
        if item.kind == "param" and not isinstance(new, NoDefault):
            edits.append(TextEdit(item.slot, item.slot, "=" + render(new)))
        return
    if item.kind == "param" and isinstance(new, NoDefault):
        edits.append(TextEdit(item.slot, item.value_span[1], ""))
        return
    if new is orig or values_equal(new, orig):
        return
    old_text = origin.text[item.value_span[0]:item.value_span[1]]
    edits.append(TextEdit(*item.value_span, render(new, old_text, orig)))


def _diff_comment(new, item, origin, edits):
    text = str(new)
    if text == item.orig:
        return
    nl = origin.src.newline
    edits.append(TextEdit(*item.value_span, (nl + item.indent).join(text.split("\n"))))


def _diff_overrides(node, path, origin, edits):
    ov = node.get("__overrides__")
    if not isinstance(ov, dict):
        ov = {}
    plain = {k: v for k, v in ov.items() if not _is_dunder(k)}
    _diff_override_comment(plain, path + ("__overrides__",), node, path, origin, edits, field_key=None)
    for k, v in ov.items():
        if _is_dunder(k) and isinstance(v, dict) and len(k) > 4:
            _diff_override_comment(v, path + ("__overrides__", k), node, path, origin, edits,
                                   field_key=k[2:-2])
    # A field override whose comment was removed from the dict entirely.
    for item in origin.loose.get(path + ("__overrides__",), ()):
        if item.kind == "override" and item.key not in ov and not item.shadowed:
            edits.append(TextEdit(*item.extent, ""))


def _diff_override_comment(pairs, ipath, node, path, origin, edits, field_key):
    item = origin.items.get(ipath)
    if item is not None and item.kind != "override":
        item = None         # a dict literal keyed "__overrides__" is not an override comment
    nl = origin.src.newline
    if item is not None:
        if item.comment_key is not None:
            current = node.get(item.comment_key)
            if current is not None and str(current) != item.orig and str(current) != str(item.comment_key):
                return          # the raw comment text was edited - it wins
        if not pairs:
            if item.comment_key is None:
                edits.append(TextEdit(*item.extent, ""))
            return
        if _override_changed(pairs, item.orig):
            old_text = origin.text[item.value_span[0]:item.value_span[1]]
            old_joined = "\n".join(ln.strip() for ln in old_text.split("\n"))
            text = _reformat_override_comment(old_joined, pairs)
            edits.append(TextEdit(*item.value_span, (nl + item.indent).join(text.split("\n"))))
        return
    if not pairs:
        return
    # No comment yet → insert one above the owner (a dict's own line, or the field's statement).
    if field_key is not None:
        target = origin.items.get(path + (field_key,))
    else:
        target = origin.items.get(path)
    if target is None:
        if path == () and field_key is None:
            edits.append(TextEdit(0, 0, _format_override_comment(pairs) + nl))
        return
    at = _owner_line_start(target, origin)
    edits.append(TextEdit(at, at, target.indent + _format_override_comment(pairs) + nl))


def _owner_line_start(item, origin):
    """Start of the line the item's own code begins on (below its leading
    comments): where a new override comment goes."""
    if item.code_end is None:
        return item.core[0]
    # Walk back from the core/code to the line start of the statement's first line.
    start = item.core[0]
    text = origin.text
    lines_start = start
    # The statement's first code line is the first non-comment, non-blank line in the core.
    pos = start
    while pos < item.core[1]:
        line_end = text.find("\n", pos)
        if line_end == -1:
            line_end = len(text)
        stripped = text[pos:line_end].strip()
        if stripped and not stripped.startswith("#"):
            return pos
        pos = line_end + 1
    return lines_start


def _render_member(node, key, seq, origin, indent):
    """Source text for a NEW member of `seq` — a statement line for a body,
    `k=v` for args, `k: v` for pairs, the value for elements/params."""
    value = node[key] if isinstance(node, dict) else node[key]
    nl = origin.src.newline
    kind = seq.kind
    if kind == "body":
        if isinstance(value, Comment):
            return "".join(indent + ln + nl for ln in str(value).split("\n"))
        if isinstance(value, CallParse):
            return indent + render(value) + nl
        return f"{indent}{key} = {render(value)}{nl}"
    if kind == "decorators":
        if isinstance(value, str) and not isinstance(value, CodeLine) and value == key:
            return f"{indent}@{value}{nl}"
        if isinstance(value, CallParse):
            return f"{indent}@{render(value)}{nl}"
        return f"{indent}@{value}{nl}"
    if kind == "params":
        return str(key) if isinstance(value, NoDefault) else f"{key}={render(value)}"
    if kind == "args":
        pos_names = node.get("__pos_names__") if isinstance(node, dict) else None
        if pos_names and key in pos_names:
            return render(value)
        return f"{key}={render(value)}"
    if kind == "pairs":
        return f"{render(key)}: {render(value)}"
    return render(value)


def _insert_member(node, key, path, seq, origin):
    """A body/decorator insert as its own zero-width edit at the dict position."""
    keys = _managed_keys(node)
    i = keys.index(key)
    prev = None
    for k in reversed(keys[:i]):
        it = origin.items.get(path + (k,))
        if it is not None and it.seq == seq.id:
            prev = it
            break
    value = node[key]
    if isinstance(value, Comment) and value.inline is not None:
        owner = origin.items.get(path + (value.inline,))
        if owner is not None and owner.code_end is not None:
            return TextEdit(owner.code_end, owner.code_end, "  " + str(value))
    if prev is not None:
        at, indent = prev.extent[1], prev.indent
    else:
        nxt = None
        for k in keys[i + 1:]:
            it = origin.items.get(path + (k,))
            if it is not None and it.seq == seq.id:
                nxt = it
                break
        at = nxt.core[0] if nxt is not None else seq.insert_at
        indent = nxt.indent if nxt is not None else seq.indent
    text = _render_member(node, key, seq, origin, indent)
    if at >= len(origin.text) and origin.text and not origin.text.endswith("\n"):
        text = origin.src.newline + text
    return TextEdit(at, at, text)


def _rebuild_seq(node, keys, path, seq, present_set, adds_set, origin):
    """One RegionEdit re-concatenating the members in the dict's order. The old
    members are SLOTS: a fixed slot (dunder / shadowed binding) keeps its own
    text; the movable slots are filled with the movable keys in the dict's new
    order — kept members as verbatim source slices (nested edits inside them
    still apply), new ones rendered. Surplus movable keys append; surplus slots
    (removed keys) vanish. The gaps between slots stay where they were, so the
    blank-line rhythm / separators survive a reorder."""
    items_by_key = {it.key: it for it in seq.items if not _fixed(it)}
    cores = [it.core for it in seq.items]
    gaps = [(cores[i][1], cores[i + 1][0]) for i in range(len(cores) - 1)]
    movable = [k for k in keys if k in present_set or k in adds_set]
    default_gap = "" if seq.kind in ("body", "decorators") else seq.sep
    pieces = []
    emitted = False
    mi = 0
    last_indent = seq.indent

    def member(k):
        it = items_by_key.get(k)
        if it is not None:
            return it.core, it.indent
        return _render_member(node, k, seq, origin, last_indent), last_indent

    for idx, it in enumerate(seq.items):
        gap = gaps[idx - 1] if idx > 0 else None
        if _fixed(it):
            piece, indent = it.core, it.indent
        elif mi < len(movable):
            piece, indent = member(movable[mi])
            mi += 1
        else:
            continue            # a removed key's slot (and the gap before it)
        if emitted and gap is not None:
            pieces.append(gap)
        pieces.append(piece)
        last_indent = indent
        emitted = True
    while mi < len(movable):
        if emitted:
            pieces.append(default_gap)
        piece, indent = member(movable[mi])
        pieces.append(piece)
        last_indent = indent
        emitted = True
        mi += 1
    start, end = seq.region
    if not pieces and seq.kind in ("elements", "args", "pairs", "params"):
        # Everything removed: take a trailing comma / whitespace with it.
        tail = end
        text = origin.text
        while tail < len(text) and text[tail] in " \t\r\n":
            tail += 1
        if tail < len(text) and text[tail] == ",":
            end = tail + 1
    return RegionEdit(start, end, pieces)


# ── materialize + apply ──────────────────────────────────────────────────────

def apply_edits(text, edits):
    """Apply non-overlapping plain edits (any order) to `text`."""
    out, pos = [], 0
    for e in sorted(edits, key=lambda e: (e.start, e.end)):
        if e.start < pos:
            raise ValueError(f"overlapping edits at {e.start} (previous ended at {pos})")
        out.append(text[pos:e.start])
        out.append(e.replacement)
        pos = e.end
    out.append(text[pos:])
    return "".join(out)


def materialize(text, edits):
    """Flatten RegionEdits into plain TextEdits (absolute offsets), applying the
    edits nested inside each region's source slices."""
    ordered = sorted(edits, key=lambda e: (e.start, -(e.end - e.start)))
    out = []
    i = 0
    while i < len(ordered):
        e = ordered[i]
        if isinstance(e, RegionEdit):
            j = i + 1
            children = []
            while j < len(ordered) and ordered[j].start < e.end:
                if ordered[j].end > e.end:
                    raise ValueError("edit crosses a region boundary")
                children.append(ordered[j])
                j += 1
            out.append(TextEdit(e.start, e.end, _render_region(text, e, children)))
            i = j
        else:
            out.append(e)
            i += 1
    return out


def _render_region(text, region, children):
    flat = materialize(text, children) if children else []
    parts = []
    n = len(region.pieces)
    for idx, p in enumerate(region.pieces):
        if isinstance(p, str):
            parts.append(p)
            continue
        s, e = p
        inner = [TextEdit(c.start - s, c.end - s, c.replacement) for c in flat
                 if s <= c.start and c.end <= e and (c.start < e or (s == e) or idx == n - 1)]
        parts.append(apply_edits(text[s:e], inner) if inner else text[s:e])
    return "".join(parts)


def general_parse_to_str(gp, *, check=True) -> str:
    """The text `gp` now describes. With `check`, the result must parse."""
    origin = gp[ORIGIN_KEY]
    edits = materialize(origin.text, diff(gp, origin))
    new_text = apply_edits(origin.text, edits)
    if check and edits:
        try:
            ast.parse(new_text)
        except SyntaxError as e:
            raise CoreSyntaxError(str(e), new_text, e.lineno or 0, e.offset or 0) from e
    return new_text


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Live path: re-extract, reuse unchanged value objects                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_MISSING = object()

_ROOT_CARRY_ATTRS = ("file_path", "line_offset", "address", "symbol_usage", "_symbol_gen",
                     "usages", "source_ref")


def reparse_reusing(gp, new_text) -> GeneralParse:
    """A fresh parse of `new_text` in which every value object that is
    semantically unchanged from `gp` is REUSED by identity — a nested node
    whose whole subtree is unchanged is the old object itself (its spans
    refreshed) — so draw_states keyed on those objects survive the edit.

    `gp` is NEVER mutated: the studio's held tree is bubbling-wrapped, where a
    dict mutation notifies the code host as a user edit — the libcst
    incremental builds a new root the same way. The root is `gp` itself only
    when nothing surfaced changed (then just its residual moves on). Root
    bookkeeping (address, file_path, symbol usages, …) carries over. Raises
    SyntaxError like parse_to_dict."""
    fresh = parse_to_dict(new_text, file_path=getattr(gp, "file_path", None),
                          line_offset=getattr(gp, "line_offset", 0))
    origin = fresh[ORIGIN_KEY]
    merged = _merge_node(gp, fresh, (), origin)
    if merged is gp:
        gp[ORIGIN_KEY] = origin            # internal key: a raw write OK on a bubbling node
        _copy_node_attrs(gp, fresh)
        return gp
    for attr in _ROOT_CARRY_ATTRS:
        if hasattr(gp, attr):
            setattr(merged, attr, getattr(gp, attr))
    return merged


def _merge_node(old, fresh, path, origin):
    """The node to use in place of `fresh`: `old` itself when the whole subtree
    is unchanged (coordinates refreshed onto it), else `fresh` with every
    unchanged child swapped for the old object. Reads `old` only; writes go
    into `fresh`, which is a plain (unwrapped) parse."""
    changed = [k for k in old if not _is_dunder(k)] != [k for k in fresh if not _is_dunder(k)]
    for k in list(fresh.keys()):
        if _is_dunder(k):
            continue
        v = fresh[k]
        ov = old.get(k, _MISSING)
        if ov is _MISSING:
            changed = True
            continue
        item = origin.items.get(path + (k,))
        if isinstance(ov, dict) and isinstance(v, dict) and _parse_kind(ov) is _parse_kind(v):
            m = _merge_node(ov, v, path + (k,), origin)
            if m is not v:
                fresh[k] = m
            if m is not ov:
                changed = True
            if item is not None:
                item.orig = m
        elif not isinstance(v, dict) and _same_kind(ov, v) and values_equal(ov, v):
            fresh[k] = ov
            if item is not None:
                item.orig = ov
        else:
            changed = True
    if changed:
        # Bookkeeping the fresh parse doesn't produce (`__symbol_usages__`
        # distributed by the symbol generator) rides along on the new node.
        for k, v in old.items():
            if _is_dunder(k) and k not in fresh and k != "__cst__":
                fresh[k] = v
        return fresh
    _copy_node_attrs(old, fresh)
    return old


def _copy_node_attrs(dst, src):
    for attr in ("span", "_child_spans", "source", "condition", "target", "iter", "header",
                 "func_name", "def_name"):
        if hasattr(src, attr):
            try:
                setattr(dst, attr, getattr(src, attr))
            except AttributeError:
                pass
