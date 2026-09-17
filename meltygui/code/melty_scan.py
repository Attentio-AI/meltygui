"""melty_scan — the standalone front half of core_syntax: text → syntax dict.

STANDALONE ON PURPOSE: stdlib only, no Melty / libcst imports, so it runs in a
3.12 SUBINTERPRETER (its own GIL — the parse of a big file never stalls the
render thread) and its output crosses back as pickled plain data. Three parts:

  scan(text)              the "cst-lite" parser: `tokenize` (C) → statements →
                          ast-SHAPED nodes (same class names / fields as `ast`,
                          columns in CHARACTERS) for exactly the subset the dict
                          surfaces; anything else is an `Opaque` node with a
                          span. Purely functional over the token list.
  Extractor               the dict builder (moved here from core_syntax) — runs on
                          real `ast` nodes or scanner nodes alike (kind = class
                          name), emitting either the studio's parse classes
                          (in-process) or the neutral `N*` classes below (worker),
                          plus the Origin tables.
  scan_extract(text)      the worker entry: scan → extract (neutral) → validate
                          with ast.parse → picklable result.

The `ast` front end stays (core_syntax.parse_to_dict(frontend="ast")) as the
oracle: tests/test_core_syntax.py compares both on every file under src/.
"""

from __future__ import annotations

import ast
import bisect
import codecs
import io
import keyword
import tokenize
from typing import Any

_SKIP_PARAMS = {"self", "cls"}
_SIMPLE_LITERAL_TYPES = (str, int, float, bool, type(None))
_DEF_KINDS = ("ClassDef", "FunctionDef", "AsyncFunctionDef")
_BLOCK_KINDS = ("If", "For", "AsyncFor", "Try", "TryStar")
UNRESOLVED = object()


def _k(node):
    return type(node).__name__


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Neutral data objects (what the worker emits; materialized on the main thread) ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class NParse(dict):
    def __init__(self, *args, source="", **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source


class NGeneralParse(NParse):
    pass


class NClassParse(NParse):
    pass


class NEnumParse(NClassParse):
    pass


class NFunctionParse(NParse):
    pass


class NCallParse(NParse):
    def __init__(self, *args, func_name=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.func_name = func_name


class NDecorationParse(NCallParse):
    pass


class NConditional(dict):
    def __init__(self, *args, condition=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.condition = condition


class NLoop(dict):
    def __init__(self, *args, target=None, iter=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target = target
        self.iter = iter


class NTry(dict):
    def __init__(self, *args, header=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.header = header


class NExcept(dict):
    def __init__(self, *args, header=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.header = header


class NComment(str):
    def __new__(cls, text, inline=None):
        instance = super().__new__(cls, text)
        instance.inline = inline
        return instance

    def __eq__(self, other):
        return isinstance(other, NComment) and str(self) == str(other) and self.inline == other.inline

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(("__comment__", str(self), self.inline))

    def __reduce__(self):
        return (NComment, (str(self), self.inline))


class NCodeLine(str):
    pass


class NameRef(str):
    """A dotted name the worker can't resolve (that needs the live src scope):
    materialize turns it into the callable / enum member or a CodeLine."""
    def __new__(cls, text, parts):
        instance = super().__new__(cls, text)
        instance.parts = list(parts)
        return instance

    def __reduce__(self):
        return (NameRef, (str(self), self.parts))


class NNoDefault:
    def __repr__(self):
        return "NO_DEFAULT"

    def __reduce__(self):
        return (NNoDefault, ())        # constructor form: the main side maps it to the NO_DEFAULT singleton


N_NO_DEFAULT = NNoDefault()


class NSpan:
    """A span whose LINES are relative to a Base cell (the enclosing top-level
    statement) — the main side maps it to core_syntax.RelSpan."""
    __slots__ = ("base", "rel_start_line", "start_col", "rel_end_line", "end_col")

    def __init__(self, base, rel_start_line, start_col, rel_end_line, end_col):
        self.base = base
        self.rel_start_line = rel_start_line
        self.start_col = start_col
        self.rel_end_line = rel_end_line
        self.end_col = end_col

    @property
    def start_line(self):
        return self.rel_start_line + self.base.line

    @property
    def end_line(self):
        return self.rel_end_line + self.base.line

    def __reduce__(self):
        return (NSpan, (self.base, self.rel_start_line, self.start_col, self.rel_end_line, self.end_col))

    def __repr__(self):
        return f"Span({self.start_line}:{self.start_col}–{self.end_line}:{self.end_col})"


class Types:
    """The output vocabulary of the Extractor. `resolve(parts)` → live object or
    UNRESOLVED (None = defer as NameRef); `positional_names(parts)` → the
    callee's positional parameter names or None (None hook = defer)."""

    def __init__(self, *, GeneralParse, ClassParse, EnumParse, FunctionParse, CallParse,
                 DecorationParse, Comment, CodeLine, Conditional, Loop, Try, Except,
                 NO_DEFAULT, Span, resolve=None, positional_names=None):
        self.GeneralParse = GeneralParse
        self.ClassParse = ClassParse
        self.EnumParse = EnumParse
        self.FunctionParse = FunctionParse
        self.CallParse = CallParse
        self.DecorationParse = DecorationParse
        self.Comment = Comment
        self.CodeLine = CodeLine
        self.Conditional = Conditional
        self.Loop = Loop
        self.Try = Try
        self.Except = Except
        self.NO_DEFAULT = NO_DEFAULT
        self.Span = Span
        self.resolve = resolve
        self.positional_names = positional_names


NEUTRAL_TYPES = Types(
    GeneralParse=NGeneralParse, ClassParse=NClassParse, EnumParse=NEnumParse,
    FunctionParse=NFunctionParse, CallParse=NCallParse, DecorationParse=NDecorationParse,
    Comment=NComment, CodeLine=NCodeLine, Conditional=NConditional, Loop=NLoop, Try=NTry,
    Except=NExcept, NO_DEFAULT=N_NO_DEFAULT, Span=NSpan)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Override comments & block keys (copies of libcst_conversion's helpers)      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def parse_override_comment(text):
    """'# [k=v, ...]' → dict, else None. Same rules as libcst_conversion."""
    if not isinstance(text, str):
        return None
    body = " ".join(ln.strip().lstrip("#").strip() for ln in text.split("\n")).strip()
    if not (body.startswith("[") and body.endswith("]")):
        return None
    inner = body[1:-1].strip()
    if not inner:
        return None
    try:
        call = ast.parse(f"dict({inner})", mode="eval").body
        if not isinstance(call, ast.Call) or call.args:
            return None
        parsed = {}
        for kw in call.keywords:
            if kw.arg is None:
                return None
            if kw.arg == "view_func" and isinstance(kw.value, (ast.Name, ast.Attribute)):
                reference = ast.unparse(kw.value)
                if not all(part.isidentifier() for part in reference.split(".")):
                    return None
                parsed[kw.arg] = reference
            else:
                parsed[kw.arg] = ast.literal_eval(kw.value)
        return parsed or None
    except (SyntaxError, ValueError, TypeError):
        return None


def occ_key(base, occ_counter):
    n = occ_counter.get(base, 0)
    occ_counter[base] = n + 1
    return base if n == 0 else f"{base}##{n}"


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Source text                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _Src:
    """The parsed text with a line-start table. `ast` reports columns in UTF-8
    BYTES; scanner nodes carry CHARACTER columns (`_charcols`); every span here
    is in characters."""

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

    @classmethod
    def spliced(cls, old, new_text, rs, re_, delta, region_src):
        """The line table of `new_text` from `old`'s, when only [rs, re_) was
        replaced (by a region whose own table is `region_src`): lines before
        stay, the region's are re-based, lines after shift by `delta`."""
        self = cls.__new__(cls)
        self.text = new_text
        starts = old.line_starts
        lo = bisect.bisect_right(starts, rs)          # lines starting at or before rs stay (rs is a line start)
        hi = bisect.bisect_left(starts, re_)          # first line start at/after re_
        region = [rs + x for x in region_src.line_starts[1:]]
        self.line_starts = starts[:lo] + region + [x + delta for x in starts[hi:]]
        self.newline = old.newline
        return self

    @property
    def line_count(self):
        return len(self.line_starts)

    def line_start(self, lineno):
        return self.line_starts[lineno - 1]

    def next_line_start(self, lineno):
        if lineno <= 0:
            return 0
        if lineno < len(self.line_starts):
            return self.line_starts[lineno]
        return len(self.text)

    def line_end(self, lineno):
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
        if getattr(node, "_charcols", False):
            return (self.line_starts[node.lineno - 1] + node.col_offset,
                    self.line_starts[node.end_lineno - 1] + node.end_col_offset)
        return (self.offset(node.lineno, node.col_offset),
                self.offset(node.end_lineno, node.end_col_offset))

    def linecol(self, offset):
        lineno = bisect.bisect_right(self.line_starts, offset)
        return lineno, offset - self.line_starts[lineno - 1]

    def indent_of_line(self, lineno):
        line = self.line_text(lineno)
        return line[:len(line) - len(line.lstrip())]

    def is_blank(self, lineno):
        return not self.line_text(lineno).strip()


def scan_comments(text):
    """{lineno: (col, text)} for standalone comment lines and for trailing
    (same-line-as-code) comments — the `ast` front end's comment source."""
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
# ║  Origin tables                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class Base:
    """The anchor of one TOP-LEVEL statement: the char offset and 1-based line
    where its extent starts. Every Item / Seq inside the statement stores its
    positions RELATIVE to its Base, so an edit above the statement moves it
    by touching this one cell — the incremental reparse shifts ~hundreds of
    Bases instead of ~ten thousand items."""
    __slots__ = ("offset", "line")

    def __init__(self, offset=0, line=1):
        self.offset = offset
        self.line = line

    def __reduce__(self):
        return (Base, (self.offset, self.line))

    def __repr__(self):
        return f"Base({self.offset}@{self.line})"


ZERO_BASE = Base(0, 1)


class Item:
    """One surfaced site. `extent` is what a delete removes (leading comment
    lines and blank lines above, the statement, its trailing comment and
    newline); `core` is what a reorder moves (extent minus the blank lines
    above it); `value_span` is the editable expression, None when there is
    none (a def, a block, a parameter without a default — `slot` then says
    where a value would be inserted). Positions read and write ABSOLUTE
    offsets; they are stored relative to `base`."""
    __slots__ = ("base", "path", "key", "kind", "_extent", "_core", "_value_span", "orig", "indent",
                 "_slot", "_code_end", "seq", "comment_key", "shadowed")

    def __init__(self, base, path, key, kind, extent, core, value_span, orig, indent="", slot=None,
                 code_end=None, seq=None, comment_key=None, shadowed=False):
        self.base = base
        self.path = path
        self.key = key
        self.kind = kind            # value | param | kwarg | element | pair | def | block | header | comment | trailing | override | decorator | pseudo
        self.extent = extent
        self.core = core
        self.value_span = value_span
        self.orig = orig
        self.indent = indent
        self.slot = slot
        self.code_end = code_end    # end of the statement's code (trailing comments append here)
        self.seq = seq
        self.comment_key = comment_key      # override items: the comment key that shares their span
        self.shadowed = shadowed            # a later statement re-bound this key: fixed in place, never diffed

    @property
    def extent(self):
        o = self.base.offset
        return (self._extent[0] + o, self._extent[1] + o)

    @extent.setter
    def extent(self, v):
        o = self.base.offset
        self._extent = (v[0] - o, v[1] - o)

    @property
    def core(self):
        o = self.base.offset
        return (self._core[0] + o, self._core[1] + o)

    @core.setter
    def core(self, v):
        o = self.base.offset
        self._core = (v[0] - o, v[1] - o)

    @property
    def value_span(self):
        if self._value_span is None:
            return None
        o = self.base.offset
        return (self._value_span[0] + o, self._value_span[1] + o)

    @value_span.setter
    def value_span(self, v):
        if v is None:
            self._value_span = None
        else:
            o = self.base.offset
            self._value_span = (v[0] - o, v[1] - o)

    @property
    def slot(self):
        return None if self._slot is None else self._slot + self.base.offset

    @slot.setter
    def slot(self, v):
        self._slot = None if v is None else v - self.base.offset

    @property
    def code_end(self):
        return None if self._code_end is None else self._code_end + self.base.offset

    @code_end.setter
    def code_end(self, v):
        self._code_end = None if v is None else v - self.base.offset

    def __reduce__(self):
        return (Item, (self.base, self.path, self.key, self.kind, self.extent, self.core, self.value_span,
                       self.orig, self.indent, self.slot, self.code_end, self.seq, self.comment_key,
                       self.shadowed))

    def __repr__(self):
        return (f"Item({self.path!r}, {self.kind}, extent={self.extent}, value_span={self.value_span}"
                f"{', shadowed' if self.shadowed else ''})")


class Seq:
    """An ordered container of items in the source. `region` = [first core
    start, last core end) (or the insertion point when empty); `insert_at` is
    stored relative to `base`."""
    __slots__ = ("id", "owner", "kind", "items", "base", "_insert_at", "indent", "sep")

    def __init__(self, id, owner, kind, items=None, base=ZERO_BASE, insert_at=0, indent="", sep=", "):
        self.id = id
        self.owner = owner                  # node path whose keys this Seq holds
        self.kind = kind                    # body | params | args | elements | pairs | decorators
        self.items = items if items is not None else []
        self.base = base
        self.insert_at = insert_at
        self.indent = indent
        self.sep = sep

    @property
    def insert_at(self):
        return self._insert_at + self.base.offset

    @insert_at.setter
    def insert_at(self, v):
        self._insert_at = v - self.base.offset

    @property
    def region(self):
        if self.items:
            return (self.items[0].core[0], self.items[-1].core[1])
        return (self.insert_at, self.insert_at)

    def __reduce__(self):
        return (Seq, (self.id, self.owner, self.kind, self.items, self.base, self.insert_at, self.indent, self.sep))

    def __repr__(self):
        return f"Seq(#{self.id} {self.kind} owner={self.owner!r} keys={[it.key for it in self.items]})"


class Origin:
    """The residual: the parsed text plus the flat site tables."""

    def __init__(self, text):
        self.text = text
        self.src = _Src(text)
        self.items: dict[tuple, Item] = {}
        self.seqs: dict[int, Seq] = {}
        self.default_seq: dict[tuple, int] = {}     # node path → seq new items of that node join
        self.owned: dict[tuple, list] = {}          # node path → seq ids whose deletes it answers
        self.loose: dict[tuple, list] = {}          # node path → items outside any Seq (comments, etc)
        # Every TOP-LEVEL statement, surfaced or not, as (Base, relative extent
        # end, key-or-None) in source order - the extents within the module body -
        # the incremental reparse re-scans exactly the statements an edit touches
        # and re-bases the rest. `top_extents()` gives these absolute.
        self.top_stmts: list = []
        self.generation = 0
        self.file_path = None
        self.line_offset = 0
        self._next_seq_id = 0

    def top_extents(self):
        return [(b.offset, b.offset + rel_end, key) for b, rel_end, key in self.top_stmts]

    def new_seq(self, owner, kind, *, base=ZERO_BASE, default=True, indent="", insert_at=0, sep=", "):
        seq = Seq(self._next_seq_id, owner, kind, base=base, indent=indent, insert_at=insert_at, sep=sep)
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
        if owned is not None and not owned:
            del self.owned[seq.owner]
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
        pass                                # `region` derives from the items / insert point


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Scanner: tokenize → ast-shaped nodes                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _Node:
    """Base of the scanner's nodes: the `ast` field names, CHARACTER columns."""
    __slots__ = ("lineno", "col_offset", "end_lineno", "end_col_offset")
    _charcols = True

    def _pos(self, first, last):
        self.lineno, self.col_offset = first.start
        self.end_lineno, self.end_col_offset = last.end
        return self

    def __repr__(self):
        return f"<{_k(self)} {self.lineno}:{self.col_offset}-{self.end_lineno}:{self.end_col_offset}>"


class Module(_Node):
    __slots__ = ("body",)

    def __init__(self, body):
        self.body = body
        self.lineno = self.col_offset = 0
        self.end_lineno = self.end_col_offset = 0


class Opaque(_Node):
    """A statement / expression the dict doesn't model. `body` (compound
    statements) is consumed but never surfaced."""
    __slots__ = ("body",)

    def __init__(self, body=None):
        self.body = body or []


class alias(_Node):
    """`name [as asname]` of an import statement (ast.alias shape)."""
    __slots__ = ("name", "asname")

    def __init__(self, name, asname=None):
        self.name = name
        self.asname = asname


class Import(_Node):
    """`import a.b [as c], d` — ast.Import shape. Surfaced for the file
    import graph (view/playground/file_graph.py); the dict ignores it."""
    __slots__ = ("names",)

    def __init__(self, names):
        self.names = names


class ImportFrom(_Node):
    """`from [..]mod import x [as y], (…)` / `import *` — ast.ImportFrom
    shape: `module` None for a bare relative import, `level` = leading dots."""
    __slots__ = ("module", "names", "level")

    def __init__(self, module, names, level):
        self.module = module
        self.names = names
        self.level = level


class ClassDef(_Node):
    __slots__ = ("name", "bases", "keywords", "body", "decorator_list")

    def __init__(self, name, bases, keywords, body, decorator_list):
        self.name, self.bases, self.keywords = name, bases, keywords
        self.body, self.decorator_list = body, decorator_list


class FunctionDef(_Node):
    __slots__ = ("name", "args", "body", "decorator_list", "returns")

    def __init__(self, name, args, body, decorator_list, returns=None):
        self.name, self.args, self.body = name, args, body
        self.decorator_list, self.returns = decorator_list, returns


class AsyncFunctionDef(FunctionDef):
    __slots__ = ()


class arguments:
    __slots__ = ("posonlyargs", "args", "vararg", "kwonlyargs", "kw_defaults", "kwarg", "defaults")

    def __init__(self):
        self.posonlyargs, self.args, self.kwonlyargs = [], [], []
        self.kw_defaults, self.defaults = [], []
        self.vararg = self.kwarg = None


class arg(_Node):
    __slots__ = ("arg", "annotation")

    def __init__(self, name, annotation=None):
        self.arg, self.annotation = name, annotation


class Assign(_Node):
    __slots__ = ("targets", "value")

    def __init__(self, targets, value):
        self.targets, self.value = targets, value


class AnnAssign(_Node):
    __slots__ = ("target", "annotation", "value")

    def __init__(self, target, annotation, value):
        self.target, self.annotation, self.value = target, annotation, value


class Expr(_Node):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class If(_Node):
    __slots__ = ("test", "body", "orelse")

    def __init__(self, test, body, orelse):
        self.test, self.body, self.orelse = test, body, orelse


class For(_Node):
    __slots__ = ("target", "iter", "body", "orelse")

    def __init__(self, target, iter, body, orelse):
        self.target, self.iter, self.body, self.orelse = target, iter, body, orelse


class AsyncFor(For):
    __slots__ = ()


class Try(_Node):
    __slots__ = ("body", "handlers", "orelse", "finalbody")

    def __init__(self, body, handlers, orelse, finalbody):
        self.body, self.handlers, self.orelse, self.finalbody = body, handlers, orelse, finalbody


class TryStar(Try):
    __slots__ = ()


class ExceptHandler(_Node):
    __slots__ = ("type", "name", "body")

    def __init__(self, type, name, body):
        self.type, self.name, self.body = type, name, body


# expressions
class Name(_Node):
    __slots__ = ("id",)

    def __init__(self, id):
        self.id = id


class Attribute(_Node):
    __slots__ = ("value", "attr")

    def __init__(self, value, attr):
        self.value, self.attr = value, attr


class Constant(_Node):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class USub:
    pass


class UAdd:
    pass


class UnaryOp(_Node):
    __slots__ = ("op", "operand")

    def __init__(self, op, operand):
        self.op, self.operand = op, operand


class Call(_Node):
    __slots__ = ("func", "args", "keywords")

    def __init__(self, func, args, keywords):
        self.func, self.args, self.keywords = func, args, keywords


class keyword_(_Node):
    __slots__ = ("arg", "value")

    def __init__(self, arg, value):
        self.arg, self.value = arg, value


keyword_.__name__ = "keyword"


class Starred(_Node):
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class Tuple(_Node):
    __slots__ = ("elts",)

    def __init__(self, elts):
        self.elts = elts


class List(_Node):
    __slots__ = ("elts",)

    def __init__(self, elts):
        self.elts = elts


class Set(_Node):
    __slots__ = ("elts",)

    def __init__(self, elts):
        self.elts = elts


class Dict(_Node):
    __slots__ = ("keys", "values")

    def __init__(self, keys, values):
        self.keys, self.values = keys, values


class Subscript(_Node):
    __slots__ = ("value", "slice")

    def __init__(self, value, slice):
        self.value, self.slice = value, slice


_OP, _NAME, _NUMBER, _STRING = tokenize.OP, tokenize.NAME, tokenize.NUMBER, tokenize.STRING
_NEWLINE, _INDENT, _DEDENT, _ENDMARKER = tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER
_FSTRING_START = getattr(tokenize, "FSTRING_START", -1)
_FSTRING_END = getattr(tokenize, "FSTRING_END", -2)
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")", "]", "}"}
# The keywords that start a statement the dict never does (`return`,
# `import`, ...). Soft keywords (`match`, `case`, `type`, `_`) are allowed
# unless the header rule in compound_or_simple says otherwise.
_STMT_KEYWORDS = set(keyword.kwlist) - {"True", "False", "None", "lambda", "not", "await"}
_UNARY_NUM = {"-": USub, "+": UAdd}


class ScanError(SyntaxError):
    pass


def _number(s):
    try:
        if s[-1] in "jJ":
            return complex(s)
        return int(s, 0) if not any(c in s for c in ".eE") or s[:2].lower() == "0x" else float(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return UNRESOLVED


def _string(tok):
    """The value of one STRING token (no f-strings here: those are FSTRING_*
    tokens). Raw strings pass through; bytes decode their own escapes."""
    i = 0
    while i < len(tok) and tok[i] in "rRbBuU":
        i += 1
    prefix = tok[:i].lower()
    body = tok[i:]
    q = body[:3] if body[:3] in ('"""', "'''") else body[:1]
    inner = body[len(q):-len(q)]
    raw = "r" in prefix
    if "b" in prefix:
        if raw:
            return inner.encode("latin-1", "backslashreplace")
        return codecs.escape_decode(inner.encode("latin-1", "backslashreplace"))[0]
    if raw or "\\" not in inner:
        return inner
    # unicode_escape reads latin-1 bytes; non-latin-1 chars are round-tripped as
    # \\uXXXX, so they survive the trip.
    return inner.encode("latin-1", "backslashreplace").decode("unicode_escape")


def _is_whole_fstring(tok):
    """Tokenizers before Python 3.12 give a whole f-string as ONE STRING token
    (no FSTRING_* tokens): it is opaque code, never a constant."""
    return tok.type == _STRING and "f" in tok.string[:tok.string.index(tok.string[-1])].lower()


def _has_depth0(toks, string, tok_type=_OP):
    """Index of the first `string` token at bracket depth 0 (lambda parameter
    lists excluded, their `=` / `:` are not the statement's), else None."""
    depth = 0
    lam = 0
    for i, t in enumerate(toks):
        if t.type == _OP:
            if t.string in _OPEN:
                depth += 1
            elif t.string in _CLOSE:
                depth -= 1
            elif depth == 0:
                if lam and t.string == ":":
                    lam -= 1
                    continue
                if not lam and t.type == tok_type and t.string == string:
                    return i
        elif t.type == _NAME and t.string == "lambda" and depth == 0:
            lam += 1
        elif depth == 0 and not lam and t.type == tok_type and t.string == string:
            return i
    return None


def _split_depth0(toks, string):
    """Split at depth-0 `string` tokens. A depth-0 `lambda … :` is one unit:
    its parameter commas / `=` defaults never split (`f(lambda a, b=1: a)`)."""
    parts, cur, depth, lam = [], [], 0, 0
    for t in toks:
        if t.type == _OP and t.string in _OPEN:
            depth += 1
        elif t.type == _OP and t.string in _CLOSE:
            depth -= 1
        elif depth == 0 and t.type == _NAME and t.string == "lambda":
            lam += 1
        elif depth == 0 and lam and t.type == _OP and t.string == ":":
            lam -= 1
            cur.append(t)
            continue
        if depth == 0 and not lam and t.type == _OP and t.string == string:
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    parts.append(cur)
    return parts


def _match(toks, i):
    """Index of the bracket closing toks[i], else None."""
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j]
        if t.type == _OP:
            if t.string in _OPEN:
                depth += 1
            elif t.string in _CLOSE:
                depth -= 1
                if depth == 0:
                    return j
    return None


class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def logical_line(self):
        """Tokens of the next logical line (NEWLINE consumed, not returned)."""
        out = []
        toks = self.toks
        n = len(toks)
        while self.i < n:
            t = toks[self.i]
            self.i += 1
            if t.type == _NEWLINE:
                break
            if t.type in (_INDENT, _DEDENT, _ENDMARKER):
                self.i -= 1
                break
            out.append(t)
        return out

    # ── blocks ───────────────────────────────────────────────────────────────

    def block(self):
        stmts = []
        while True:
            t = self.peek()
            if t.type in (_DEDENT, _ENDMARKER):
                return stmts
            if t.type == _INDENT:
                raise ScanError("unexpected indent", ("<text>", t.start[0], t.start[1] + 1, t.line))
            stmts.extend(self.statement())

    def indented_block(self, after_tok):
        t = self.peek()
        if t.type != _INDENT:
            raise ScanError("expected an indented block", ("<text>", after_tok.end[0], after_tok.end[1] + 1, after_tok.line))
        self.i += 1
        stmts = self.block()
        if self.peek().type == _DEDENT:
            self.i += 1
        return stmts

    def body_after(self, toks, colon):
        """The body of a compound header: inline statements after the colon, or
        the indented block that follows."""
        rest = toks[colon + 1:]
        if rest:
            return self.simple_statements(rest)
        return self.indented_block(toks[colon])

    # ── statements ───────────────────────────────────────────────────────────

    def statement(self):
        toks = self.logical_line()
        if not toks:
            return []
        t0 = toks[0]
        if t0.type == _OP and t0.string == "@":
            decorators = [self.expr(toks[1:], t0)]
            while True:
                toks = self.logical_line()
                if toks and toks[0].type == _OP and toks[0].string == "@":
                    decorators.append(self.expr(toks[1:], toks[0]))
                    continue
                break
            stmts = self.compound_or_simple(toks)
            if stmts and _k(stmts[0]) in _DEF_KINDS:
                stmts[0].decorator_list = decorators
            return stmts
        return self.compound_or_simple(toks)

    def compound_or_simple(self, toks):
        t0 = toks[0]
        if t0.type != _NAME:
            return self.simple_statements(toks)
        kw = t0.string
        if kw == "async" and len(toks) > 1 and toks[1].type == _NAME and toks[1].string in ("def", "for", "with"):
            kw = "async " + toks[1].string
        if kw in ("def", "async def"):
            return [self.funcdef(toks, kw == "async def")]
        if kw == "class":
            return [self.classdef(toks)]
        if kw == "if":
            return [self.if_stmt(toks)]
        if kw in ("for", "async for"):
            return [self.for_stmt(toks, kw == "async for")]
        if kw == "try":
            return [self.try_stmt(toks)]
        if kw in ("while", "with", "async with"):
            return [self.opaque_compound(toks)]
        if kw in ("match", "case") and toks[-1].type == _OP and toks[-1].string == ":" and self.peek().type == _INDENT:
            return [self.opaque_compound(toks)]
        return self.simple_statements(toks)

    def header_colon(self, toks, start=1):
        c = _has_depth0(toks[start:], ":")
        if c is None:
            raise ScanError("expected ':'", ("<text>", toks[0].start[0], toks[0].start[1] + 1, toks[0].line))
        return c + start

    def opaque_compound(self, toks):
        colon = self.header_colon(toks, 0)
        body = self.body_after(toks, colon)
        node = Opaque(body)
        last = self._last_tok(body) or toks[-1]
        node._pos(toks[0], last)
        # while/for-else on an opaque loop: consume the else block too.
        if toks[0].string == "while" and self.peek().type == _NAME and self.peek().string == "else":
            etoks = self.logical_line()
            ebody = self.body_after(etoks, self.header_colon(etoks, 0))
            last = self._last_tok(ebody) or etoks[-1]
            node.end_lineno, node.end_col_offset = last.end
        return node

    @staticmethod
    def _last_tok(stmts):
        """A pseudo token for the end of the last statement of a body."""
        if not stmts:
            return None
        s = stmts[-1]
        return _EndTok(s.end_lineno, s.end_col_offset)

    def funcdef(self, toks, is_async):
        start = toks[0]
        j = 1 if not is_async else 2
        name_tok = toks[j]
        open_idx = j + 1
        close = _match(toks, open_idx)
        if close is None or toks[open_idx].string != "(":
            raise ScanError("bad def", ("<text>", start.start[0], start.start[1] + 1, start.line))
        args = self.params(toks[open_idx + 1:close])
        colon = self.header_colon(toks, close + 1)
        returns = self.expr(toks[close + 2:colon], toks[close]) if colon > close + 2 else None
        body = self.body_after(toks, colon)
        node = (AsyncFunctionDef if is_async else FunctionDef)(name_tok.string, args, body, [], returns)
        node._pos(start, self._last_tok(body) or toks[colon])
        return node

    def classdef(self, toks):
        start = toks[0]
        name_tok = toks[1]
        bases, keywords = [], []
        colon = self.header_colon(toks, 2)
        if colon > 2 and toks[2].type == _OP and toks[2].string == "(":
            close = _match(toks, 2)
            for part in _split_depth0(toks[3:close], ","):
                if not part:
                    continue
                eq = _has_depth0(part, "=")
                if eq is not None and eq == 1 and part[0].type == _NAME:
                    keywords.append(keyword_(part[0].string, self.expr(part[2:], part[1]))._pos(part[0], part[-1]))
                else:
                    bases.append(self.expr(part, part[0]))
        body = self.body_after(toks, colon)
        node = ClassDef(name_tok.string, bases, keywords, body, [])
        node._pos(start, self._last_tok(body) or toks[colon])
        return node

    def if_stmt(self, toks):
        start = toks[0]
        colon = self.header_colon(toks)
        test = self.expr(toks[1:colon], toks[0])
        body = self.body_after(toks, colon)
        node = If(test, body, [])
        node._pos(start, self._last_tok(body) or toks[colon])
        nxt = self.peek()
        if nxt.type == _NAME and nxt.string == "elif":
            etoks = self.logical_line()
            sub = self.if_stmt(etoks)
            node.orelse = [sub]
            node.end_lineno, node.end_col_offset = sub.end_lineno, sub.end_col_offset
        elif nxt.type == _NAME and nxt.string == "else":
            etoks = self.logical_line()
            node.orelse = self.body_after(etoks, self.header_colon(etoks, 0))
            last = self._last_tok(node.orelse) or etoks[-1]
            node.end_lineno, node.end_col_offset = last.end
        return node

    def for_stmt(self, toks, is_async):
        start = toks[0]
        j = 2 if is_async else 1
        colon = self.header_colon(toks, j)
        in_idx = _has_depth0(toks[j:colon], "in", _NAME)
        if in_idx is None:
            raise ScanError("bad for", ("<text>", start.start[0], start.start[1] + 1, start.line))
        in_idx += j
        target = self.expr(toks[j:in_idx], toks[j - 1])
        it = self.expr(toks[in_idx + 1:colon], toks[in_idx])
        body = self.body_after(toks, colon)
        node = (AsyncFor if is_async else For)(target, it, body, [])
        node._pos(start, self._last_tok(body) or toks[colon])
        nxt = self.peek()
        if nxt.type == _NAME and nxt.string == "else":
            etoks = self.logical_line()
            node.orelse = self.body_after(etoks, self.header_colon(etoks, 0))
            last = self._last_tok(node.orelse) or etoks[-1]
            node.end_lineno, node.end_col_offset = last.end
        return node

    def try_stmt(self, toks):
        start = toks[0]
        body = self.body_after(toks, self.header_colon(toks, 0))
        handlers, orelse, finalbody = [], [], []
        star = False
        last = self._last_tok(body) or toks[-1]
        while True:
            nxt = self.peek()
            if nxt.type != _NAME or nxt.string not in ("except", "else", "finally"):
                break
            htoks = self.logical_line()
            colon = self.header_colon(htoks, 0)
            if htoks[0].string == "except":
                j = 1
                if j < colon and htoks[j].type == _OP and htoks[j].string == "*":
                    star = True
                    j += 1
                as_idx = _has_depth0(htoks[j:colon], "as", _NAME)
                typ = name = None
                if as_idx is not None:
                    as_idx += j
                    typ = self.expr(htoks[j:as_idx], htoks[j - 1]) if as_idx > j else None
                    name = htoks[as_idx + 1].string
                elif colon > j:
                    typ = self.expr(htoks[j:colon], htoks[j - 1])
                hbody = self.body_after(htoks, colon)
                h = ExceptHandler(typ, name, hbody)
                h._pos(htoks[0], self._last_tok(hbody) or htoks[colon])
                handlers.append(h)
                last = self._last_tok(hbody) or htoks[colon]
            elif htoks[0].string == "else":
                orelse = self.body_after(htoks, colon)
                last = self._last_tok(orelse) or htoks[colon]
            else:
                finalbody = self.body_after(htoks, colon)
                last = self._last_tok(finalbody) or htoks[colon]
        node = (TryStar if star else Try)(body, handlers, orelse, finalbody)
        node._pos(start, last)
        return node

    def params(self, toks):
        a = arguments()
        posonly_seen = False
        kwonly = False
        for part in _split_depth0(toks, ","):
            if not part:
                continue
            t0 = part[0]
            if t0.type == _OP and t0.string == "/":
                a.posonlyargs, a.args = a.args, []
                posonly_seen = True
                continue
            if t0.type == _OP and t0.string == "*":
                if len(part) == 1:
                    kwonly = True
                else:
                    a.vararg = self._arg(part[1:])
                    kwonly = True
                continue
            if t0.type == _OP and t0.string == "**":
                a.kwarg = self._arg(part[1:])
                continue
            eq = _has_depth0(part, "=")
            if eq is None:
                p = self._arg(part)
                default = None
            else:
                p = self._arg(part[:eq])
                default = self.expr(part[eq + 1:], part[eq])
            if kwonly:
                a.kwonlyargs.append(p)
                a.kw_defaults.append(default)
            else:
                a.args.append(p)
                if default is not None:
                    a.defaults.append(default)
        return a

    def _arg(self, toks):
        name = toks[0]
        colon = _has_depth0(toks, ":")
        ann = self.expr(toks[colon + 1:], toks[colon]) if colon is not None and colon + 1 < len(toks) else None
        node = arg(name.string, ann)
        node._pos(name, toks[-1] if ann is not None else name)
        return node

    def simple_statements(self, toks):
        out = []
        for part in _split_depth0(toks, ";"):
            if part:
                out.append(self.simple(part))
        return out

    def simple(self, toks):
        t0 = toks[0]
        if t0.type == _NAME and t0.string in ("import", "from"):
            node = self.import_stmt(toks)
            if node is not None:
                return node
        if t0.type == _NAME and t0.string in _STMT_KEYWORDS:
            return Opaque()._pos(t0, toks[-1])
        eq = _has_depth0(toks, "=")
        if eq is None:
            colon = _has_depth0(toks, ":")
            if colon == 1 and t0.type == _NAME:
                node = AnnAssign(Name(t0.string)._pos(t0, t0), self.expr(toks[2:], toks[1]), None)
                return node._pos(t0, toks[-1])
            return Expr(self.expr(toks, t0))._pos(t0, toks[-1])
        lhs, rhs = toks[:eq], toks[eq + 1:]
        if not lhs or not rhs:
            return Opaque()._pos(t0, toks[-1])
        eq2 = _has_depth0(rhs, "=")
        if eq2 is not None:
            # chained assignment: two targets → never surfaced, like ast's Assign with 2+ targets
            value = self.expr(rhs[eq2 + 1:], rhs[eq2])
            node = Assign([self.target(lhs), self.target(rhs[:eq2])], value)
            return node._pos(t0, toks[-1])
        colon = _has_depth0(lhs, ":")
        if colon is not None:
            node = AnnAssign(self.target(lhs[:colon]), self.expr(lhs[colon + 1:], lhs[colon]), self.expr(rhs, toks[eq]))
            return node._pos(t0, toks[-1])
        node = Assign([self.target(lhs)], self.expr(rhs, toks[eq]))
        return node._pos(t0, toks[-1])

    # ── imports ──────────────────────────────────────────────────────────────

    def import_stmt(self, toks):
        return parse_import(toks)

    @staticmethod
    def _import_aliases(toks):
        return _import_aliases(toks)


    def target(self, toks):
        if len(toks) == 1 and toks[0].type == _NAME:
            return Name(toks[0].string)._pos(toks[0], toks[0])
        chain = self.postfix(toks)
        if chain is not None and _k(chain) in ("Attribute", "Subscript"):
            return chain
        return Opaque()._pos(toks[0], toks[-1])

    # ── expressions ──────────────────────────────────────────────────────────

    def expr(self, toks, anchor):
        if not toks:
            return Opaque()._pos(anchor, anchor)
        t0, tl = toks[0], toks[-1]
        n = len(toks)
        if n == 1:
            if t0.type == _NUMBER:
                v = _number(t0.string)
                return (Constant(v) if v is not UNRESOLVED else Opaque())._pos(t0, t0)
            if t0.type == _STRING:
                if _is_whole_fstring(t0):
                    return Opaque()._pos(t0, t0)
                return Constant(_string(t0.string))._pos(t0, t0)
            if t0.type == _NAME:
                if t0.string in ("True", "False", "None"):
                    return Constant({"True": True, "False": False, "None": None}[t0.string])._pos(t0, t0)
                if t0.string not in _STMT_KEYWORDS and t0.string not in ("lambda", "not", "await"):
                    return Name(t0.string)._pos(t0, t0)
                return Opaque()._pos(t0, t0)
            if t0.type == _OP and t0.string == "...":
                return Constant(Ellipsis)._pos(t0, t0)
            return Opaque()._pos(t0, t0)
        if n == 2 and t0.type == _OP and t0.string in _UNARY_NUM and tl.type == _NUMBER:
            v = _number(tl.string)
            if v is UNRESOLVED:
                return Opaque()._pos(t0, tl)
            return UnaryOp(_UNARY_NUM[t0.string](), Constant(v)._pos(tl, tl))._pos(t0, tl)
        if all(t.type == _STRING for t in toks):
            if any(_is_whole_fstring(t) for t in toks):
                return Opaque()._pos(t0, tl)
            try:
                parts = [_string(t.string) for t in toks]
                if all(isinstance(p, str) for p in parts) or all(isinstance(p, bytes) for p in parts):
                    return Constant(parts[0][:0].join(parts))._pos(t0, tl)
            except (UnicodeDecodeError, ValueError):
                pass
            return Opaque()._pos(t0, tl)
        if _has_depth0(toks, ",") is not None and not (t0.type == _OP and t0.string in _OPEN and _match(toks, 0) == n - 1):
            elts = self.elements(_split_depth0(toks, ","))
            if elts is None:
                return Opaque()._pos(t0, tl)
            return Tuple(elts)._pos(t0, tl)
        chain = self.postfix(toks)
        if chain is not None:
            return chain
        return Opaque()._pos(t0, tl)

    def elements(self, parts):
        """Element nodes of a display; None when a part is a comprehension /
        yield (the whole display then stays opaque)."""
        out = []
        for part in parts:
            if not part:
                continue
            if any(t.type == _NAME and t.string in ("for", "yield") for t in part
                   if _has_depth0([t], t.string, _NAME) is not None):
                pass
            if _has_depth0(part, "for", _NAME) is not None or _has_depth0(part, "yield", _NAME) is not None:
                return None
            if part[0].type == _OP and part[0].string == "*":
                out.append(Starred(self.expr(part[1:], part[0]))._pos(part[0], part[-1]))
            else:
                out.append(self.expr(part, part[0]))
        return out

    def atom(self, toks):
        """(node, next index) for the atom at toks[0], or None."""
        t0 = toks[0]
        if t0.type == _OP and t0.string in _OPEN:
            j = _match(toks, 0)
            if j is None:
                return None
            inner = toks[1:j]
            node = self.display(t0.string, inner, t0, toks[j])
            return (node, j + 1) if node is not None else None
        if t0.type == _NAME:
            if t0.string in ("True", "False", "None"):
                return Constant({"True": True, "False": False, "None": None}[t0.string])._pos(t0, t0), 1
            if t0.string in _STMT_KEYWORDS or t0.string in ("lambda", "not", "await"):
                return None
            return Name(t0.string)._pos(t0, t0), 1
        if t0.type == _NUMBER:
            v = _number(t0.string)
            return (Constant(v)._pos(t0, t0), 1) if v is not UNRESOLVED else None
        if t0.type == _STRING or t0.type == _FSTRING_START:
            # A run of string parts: plain STRING tokens and/or whole f-strings
            # (FSTRING_START ... FSTRING_END, which nest). All plain → Constant;
            # any f-string → an opaque JoinedStr with the run's span.
            k, depth, fstr = 0, 0, False
            n = len(toks)
            while k < n:
                t = toks[k]
                if t.type == _FSTRING_START:
                    depth += 1
                    fstr = True
                elif t.type == _FSTRING_END:
                    depth -= 1
                elif depth == 0 and t.type != _STRING:
                    break
                elif depth == 0 and _is_whole_fstring(t):
                    fstr = True
                k += 1
            if depth != 0:
                return None
            if fstr:
                return Opaque()._pos(t0, toks[k - 1]), k
            node = self.expr(toks[:k], t0)
            return (node, k) if _k(node) == "Constant" else None
        return None

    def display(self, opener, inner, open_tok, close_tok):
        if opener == "(":
            if not inner:
                return Tuple([])._pos(open_tok, close_tok)
            if _has_depth0(inner, ",") is not None:
                if _has_depth0(inner, "for", _NAME) is not None or _has_depth0(inner, "yield", _NAME) is not None:
                    return Opaque()._pos(open_tok, close_tok)
                elts = self.elements(_split_depth0(inner, ","))
                if elts is None:
                    return Opaque()._pos(open_tok, close_tok)
                return Tuple(elts)._pos(open_tok, close_tok)
            if _has_depth0(inner, "for", _NAME) is not None or _has_depth0(inner, "yield", _NAME) is not None:
                return Opaque()._pos(open_tok, close_tok)
            return self.expr(inner, open_tok)      # (x): the inner node, its original span
        if opener == "[":
            if _has_depth0(inner, "for", _NAME) is not None:
                return Opaque()._pos(open_tok, close_tok)
            elts = self.elements(_split_depth0(inner, ",")) if inner else []
            if elts is None:
                return Opaque()._pos(open_tok, close_tok)
            return List(elts)._pos(open_tok, close_tok)
        # {
        if not inner:
            return Dict([], [])._pos(open_tok, close_tok)
        if _has_depth0(inner, "for", _NAME) is not None:
            return Opaque()._pos(open_tok, close_tok)
        parts = [p for p in _split_depth0(inner, ",") if p]
        is_dict = any((p[0].type == _OP and p[0].string == "**") or _has_depth0(p, ":") is not None for p in parts)
        if is_dict:
            keys, values = [], []
            for p in parts:
                if p[0].type == _OP and p[0].string == "**":
                    keys.append(None)
                    values.append(self.expr(p[1:], p[0]))
                    continue
                c = _has_depth0(p, ":")
                if c is None:
                    return Opaque()._pos(open_tok, close_tok)
                keys.append(self.expr(p[:c], p[0]))
                values.append(self.expr(p[c + 1:], p[c]))
            return Dict(keys, values)._pos(open_tok, close_tok)
        elts = self.elements(parts)
        if elts is None:
            return Opaque()._pos(open_tok, close_tok)
        return Set(elts)._pos(open_tok, close_tok)

    def postfix(self, toks):
        """atom ('.' NAME | '(' args ')' | '[' ... ']')* covering ALL of toks, else None."""
        got = self.atom(toks)
        if got is None:
            return None
        node, i = got
        n = len(toks)
        while i < n:
            t = toks[i]
            if t.type == _OP and t.string == "." and i + 1 < n and toks[i + 1].type == _NAME:
                node = Attribute(node, toks[i + 1].string)._pos(toks[0], toks[i + 1])
                i += 2
            elif t.type == _OP and t.string == "(":
                j = _match(toks, i)
                if j is None:
                    return None
                args, keywords = self.call_args(toks[i + 1:j], toks[i], toks[j])
                node = Call(node, args, keywords)._pos(toks[0], toks[j])
                i = j + 1
            elif t.type == _OP and t.string == "[":
                j = _match(toks, i)
                if j is None:
                    return None
                node = Subscript(node, Opaque()._pos(toks[i], toks[j]))._pos(toks[0], toks[j])
                i = j + 1
            else:
                return None
        return node

    def call_args(self, toks, open_tok, close_tok):
        args, keywords = [], []
        if toks and _has_depth0(toks, "for", _NAME) is not None:
            # A depth-0 `for` inside call parentheses can only be a SOLE generator
            # argument (`f(x for x in y)`, `"".join(t for _, t in run)`; its own
            # commas do not split it); ast spans it from `(` to `)`.
            return [Opaque()._pos(open_tok, close_tok)], []
        parts = _split_depth0(toks, ",")
        for part in parts:
            if not part:
                continue
            p0 = part[0]
            if p0.type == _OP and p0.string == "*":
                args.append(Starred(self.expr(part[1:], p0))._pos(p0, part[-1]))
                continue
            if p0.type == _OP and p0.string == "**":
                keywords.append(keyword_(None, self.expr(part[1:], p0))._pos(p0, part[-1]))
                continue
            if len(part) > 2 and p0.type == _NAME and part[1].type == _OP and part[1].string == "=":
                keywords.append(keyword_(p0.string, self.expr(part[2:], part[1]))._pos(p0, part[-1]))
                continue
            if _has_depth0(part, "for", _NAME) is not None or _has_depth0(part, ":=") is not None:
                args.append(Opaque()._pos(p0, part[-1]))
                continue
            args.append(self.expr(part, p0))
        return args, keywords


class _EndTok:
    """A position-only stand-in for 'the end of a nested body'."""
    __slots__ = ("start", "end", "line", "type", "string")

    def __init__(self, line, col):
        self.start = self.end = (line, col)
        self.line = ""
        self.type = -1
        self.string = ""


def parse_import(toks):
    """`import …` / `from … import …` → Import / ImportFrom, or None when
    the tokens don't read as one (the caller falls back to Opaque). A
    module-level function so scan_imports can run it straight off the
    token stream without building a tree."""
    t0 = toks[0]
    if t0.string == "import":
        names = _import_aliases(toks[1:])
        return Import(names)._pos(t0, toks[-1]) if names else None
    # from [dots] [module] import names
    level, i = 0, 1
    while i < len(toks) and toks[i].type == _OP and toks[i].string in (".", "..."):
        level += len(toks[i].string)
        i += 1
    module_parts = []
    while i < len(toks) and not (toks[i].type == _NAME and toks[i].string == "import"):
        t = toks[i]
        if t.type == _NAME or (t.type == _OP and t.string == "."):
            module_parts.append(t.string)
        else:
            return None
        i += 1
    if i >= len(toks) or (not module_parts and level == 0):
        return None
    rest = toks[i + 1:]
    if rest and rest[0].type == _OP and rest[0].string == "(":
        close = _match(rest, 0)
        rest = rest[1:close] if close is not None else rest[1:]
    if len(rest) == 1 and rest[0].type == _OP and rest[0].string == "*":
        names = [alias("*")._pos(rest[0], rest[0])]
    else:
        names = _import_aliases(rest)
    if not names:
        return None
    module = "".join(module_parts) or None
    return ImportFrom(module, names, level)._pos(t0, toks[-1])


def _import_aliases(toks):
    """`a.b [as c], d [as e]` → [alias]; None on anything unexpected."""
    out = []
    for part in _split_depth0(toks, ","):
        if not part:
            continue
        parts, asname, j = [], None, 0
        while j < len(part):
            t = part[j]
            if t.type == _NAME and t.string == "as":
                if j + 1 != len(part) - 1 or part[j + 1].type != _NAME:
                    return None
                asname = part[j + 1].string
                break
            if t.type == _NAME or (t.type == _OP and t.string == "."):
                parts.append(t.string)
            else:
                return None
            j += 1
        if not parts:
            return None
        out.append(alias("".join(parts), asname)._pos(part[0], part[-1]))
    return out


_BODY_FIELDS = ("body", "orelse", "handlers", "finalbody")


def _token_scan_error(text, error):
    """Tokenizers before Python 3.12 report an unclosed bracket or string at the
    END of the text; the compiler names the line that opened it."""
    msg, (line, col) = error.args[0], error.args[1] if len(error.args) > 1 else (0, 0)
    try:
        compile(text, "<text>", "exec")
    except SyntaxError as located:
        return ScanError(located.msg, ("<text>", located.lineno or line, located.offset or col + 1, ""))
    except ValueError:
        pass
    return ScanError(msg, ("<text>", line, col + 1, ""))


def scan_imports(text):
    """Every Import / ImportFrom of `text`, in source order, WITHOUT building
    the tree — the file import graph's path (file_graph.py). The token
    stream alone decides what is a statement start (after NEWLINE / INDENT
    / DEDENT / a depth-0 `;`), so imports inside strings and comments are
    never seen, exactly like scan(); each `import` / `from` statement's
    tokens go through the same parse_import. ~6× cheaper than scan() on
    the src tree (tokenize is the whole cost). Raises ScanError like scan()."""
    out = []
    stmt = None                       # tokens of the import statement being collected
    at_start = True
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            tt = t.type
            if tt in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
                continue
            if stmt is not None:
                if tt == _NEWLINE or tt == _ENDMARKER or (tt == _OP and t.string == ";"):
                    node = parse_import(stmt)
                    if node is not None:
                        out.append(node)
                    stmt = None
                    at_start = True
                else:
                    stmt.append(t)
                continue
            if tt in (_NEWLINE, _INDENT, _DEDENT):
                at_start = True
                continue
            if tt == _OP and t.string == ";":
                at_start = True
                continue
            if at_start and tt == _NAME and t.string in ("import", "from"):
                stmt = [t]
            at_start = False
    except tokenize.TokenError as e:
        raise _token_scan_error(text, e) from None
    except (IndentationError, SyntaxError) as e:
        raise ScanError(str(e), ("<text>", getattr(e, "lineno", 0) or 0, getattr(e, "offset", 0) or 0, "")) from None
    if stmt is not None:
        node = parse_import(stmt)
        if node is not None:
            out.append(node)
    return out


def iter_imports(node):
    """Every Import / ImportFrom node under `node` (a scan() Module or any
    scanner node), in source order, at any nesting — function bodies,
    if/try blocks and opaque compounds included."""
    stack = [node]
    while stack:
        n = stack.pop()
        k = _k(n)
        if k in ("Import", "ImportFrom"):
            yield n
            continue
        children = []
        for field in _BODY_FIELDS:
            body = getattr(n, field, None)
            if isinstance(body, list):
                children.extend(body)
        stack.extend(reversed(children))


def scan(text):
    """text → (Module, standalone comments, trailing comments). Raises
    SyntaxError (a ScanError) for what the tokenizer / block structure can't
    take; anything else parses — validation against `ast` is the caller's."""
    standalone, trailing = {}, {}
    sig = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            tt = t.type
            if tt == tokenize.COMMENT:
                line, col = t.start
                if t.line[:col].strip() == "":
                    standalone[line] = (col, t.string)
                else:
                    trailing[line] = (col, t.string)
            elif tt in (tokenize.NL, tokenize.ENCODING):
                continue
            else:
                sig.append(t)
    except tokenize.TokenError as e:
        raise _token_scan_error(text, e) from None
    except (IndentationError, SyntaxError) as e:
        raise ScanError(str(e), ("<text>", getattr(e, "lineno", 0) or 0, getattr(e, "offset", 0) or 0, "")) from None
    if not sig or sig[-1].type != _ENDMARKER:
        sig.append(_EndTok(text.count("\n") + 2, 0))
        sig[-1].type = _ENDMARKER
    p = _Parser(sig)
    body = p.block()
    return Module(body), standalone, trailing


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Node helpers used by both front ends                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _dotted_parts(node):
    parts = []
    while _k(node) == "Attribute":
        parts.append(node.attr)
        node = node.value
    if _k(node) == "Name":
        parts.append(node.id)
        parts.reverse()
        return parts
    return None


def _call_func_name(call):
    func = call.func
    k = _k(func)
    if k == "Name":
        return func.id
    if k == "Attribute":
        parts = _dotted_parts(func)
        return ".".join(parts) if parts is not None else func.attr
    return None


def _assign_target_name(stmt):
    k = _k(stmt)
    if k == "Assign" and len(stmt.targets) == 1 and _k(stmt.targets[0]) == "Name":
        return stmt.targets[0].id
    if k == "AnnAssign" and _k(stmt.target) == "Name" and stmt.value is not None:
        return stmt.target.id
    return None


def _stmt_call(stmt, nonname_targets):
    k = _k(stmt)
    if k == "Expr" and _k(stmt.value) == "Call":
        return stmt.value
    if not nonname_targets:
        return None
    if k == "Assign" and _k(stmt.value) == "Call":
        if not (len(stmt.targets) == 1 and _k(stmt.targets[0]) == "Name"):
            return stmt.value
    if k == "AnnAssign" and stmt.value is not None and _k(stmt.value) == "Call":
        if _k(stmt.target) != "Name":
            return stmt.value
    return None


def _is_enum_classdef(node):
    for base in list(node.bases) + [kw.value for kw in node.keywords if kw.arg == "metaclass"]:
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
    return {s.name: _param_names(s) for s in stmts if _k(s) in ("FunctionDef", "AsyncFunctionDef")}


def _const_value(node):
    """The value of a literal-only expression (dict keys), else raise ValueError."""
    k = _k(node)
    if k == "Constant":
        return node.value
    if k == "UnaryOp" and _k(node.op) in ("USub", "UAdd") and _k(node.operand) == "Constant" \
            and isinstance(node.operand.value, (int, float, complex)) and not isinstance(node.operand.value, bool):
        return -node.operand.value if _k(node.op) == "USub" else node.operand.value
    if k == "Tuple":
        return tuple(_const_value(e) for e in node.elts)
    raise ValueError("not a literal")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Extractor: nodes → dict + Origin                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class Extractor:
    def __init__(self, origin, types, comments, module_header=True):
        self.origin = origin
        self.src = origin.src
        self.T = types
        self.standalone, self.trailing = comments
        # False for a REGION parse (an incremental reparse of statements that
        # don't start the file): an override comment above its first
        # statement is that statement's, not a module-header override.
        self.module_header = module_header
        self._base = ZERO_BASE      # the current TOP-LEVEL statement's Base cell
        self.cursor = 0     # last consumed line (1-based); 0 = nothing yet
        # >0 while extracting call arguments / container elements: a CallParse
        # statement there gets no .span (libcst's span map only covers a
        # statement's direct value call), so LineMap depth matches.
        self._nested = 0

    # ── spans on the dict (the consumers' view: Span / _child_spans) ─────────

    def span_obj(self, start, end):
        (sl, sc), (el, ec) = self.src.linecol(start), self.src.linecol(end)
        b = self._base
        return self.T.Span(b, sl - b.line, sc, el - b.line, ec)

    def _stamp(self, obj, start, end):
        try:
            obj.span = self.span_obj(start, end)
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
        cs[key] = self.span_obj(start, end)

    # ── comments ─────────────────────────────────────────────────────────────

    def _leading_groups(self, upto_line):
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
                if parse_override_comment(joined) is not None:
                    return j + 1
        return None

    def _comment_extent(self, first_line, last_line):
        return self.src.line_start(first_line), self.src.next_line_start(last_line)

    def _comment_span(self, first_line, last_line):
        col = self.standalone[first_line][0]
        return self.src.line_start(first_line) + col, self.src.line_end(last_line)

    def _surface_comment(self, out, path, group):
        first, last, texts, _ = group
        text = "\n".join(texts)
        c = self.T.Comment(text)
        out[c] = c
        extent = self._comment_extent(first, last)
        self.origin.add(Item(self._base, path + (c,), c, "comment", extent, extent,
                             self._comment_span(first, last), text,
                             indent=self.src.indent_of_line(first)))
        return c

    def _merge_override(self, comment, out, path, span, indent):
        if isinstance(out.get("__overrides__"), dict):
            return
        parsed = parse_override_comment(str(comment))
        if parsed:
            out["__overrides__"] = parsed
            self.origin.add(Item(self._base, path + ("__overrides__",), "__overrides__", "override",
                                 span, span, span, dict(parsed), indent=indent,
                                 comment_key=comment))

    def _trailing_comment(self, stmt, out, path, key):
        tc = self.trailing.get(stmt.end_lineno)
        if tc is None:
            return
        col, text = tc
        code_end = self.src.node_span(stmt)[1]
        c = self.T.Comment(text, inline=key)
        out[c] = c
        start = self.src.line_start(stmt.end_lineno) + col
        end = self.src.line_end(stmt.end_lineno)
        self.origin.add(Item(self._base, path + (c,), c, "trailing", (code_end, end), (code_end, end),
                             (start, end), text, indent=self.src.indent_of_line(stmt.end_lineno)))
        self._merge_override(c, out, path, (start, end), self.src.indent_of_line(stmt.end_lineno))

    def _consume_footer(self, body_indent_len):
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
        body = stmt.body
        ln = self._stmt_first_line(body[0]) - 1
        first = self._stmt_first_line(stmt)
        while ln > first and (self.src.is_blank(ln) or ln in self.standalone):
            ln -= 1
        return ln

    def _keyword_line(self, body_stmts, low):
        ln = self._stmt_first_line(body_stmts[0]) - 1
        while ln > low and (self.src.is_blank(ln) or ln in self.standalone):
            ln -= 1
        return ln

    # ── module ───────────────────────────────────────────────────────────────

    def module(self, tree):
        gp = self.T.GeneralParse(source=self.origin.text)
        seq = self.origin.new_seq((), "body", base=ZERO_BASE, indent="", insert_at=0)
        self._body(tree.body, gp, (), seq, scope="module")
        self.origin.seal_seq(seq)
        return gp

    # ── bodies ───────────────────────────────────────────────────────────────

    def _body(self, stmts, out, path, seq, *, scope, block_state=None):
        T = self.T
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

        for stmt in stmts:
            kind = _k(stmt)
            first_line = self._stmt_first_line(stmt)
            same_line = first_line <= self.cursor       # `a = 1; b = 2`
            gap_start = self.src.next_line_start(self.cursor) if not same_line else self.src.node_span(stmt)[0]
            if scope == "module":
                self._base = Base(gap_start, self.src.linecol(gap_start)[0])
            groups = [] if same_line else self._leading_groups(first_line)
            core_start = self.src.line_start(groups[0][0]) if groups else (
                self.src.line_start(first_line) if not same_line else gap_start)
            indent = self.src.indent_of_line(first_line)
            is_def = kind in _DEF_KINDS
            is_block = kind in _BLOCK_KINDS
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
                    elif scope == "module" and self.module_header and self.cursor == 0 and not out:
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
            elif kind in ("Assign", "AnnAssign") and _assign_target_name(stmt) is not None:
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
                self.origin.add(Item(self._base, path + (key,), key, "value", (gap_start, 0), (core_start, 0), vs, value,
                                     indent=indent, code_end=self.src.node_span(stmt)[1]), seq)
            elif _stmt_call(stmt, nonname_calls) is not None:
                call = _stmt_call(stmt, nonname_calls)
                fname = _call_func_name(call) or "call"
                occ = call_seen.get(fname, 0)
                call_seen[fname] = occ + 1
                key = f"{fname}()" if occ == 0 else f"{fname}()#{occ}"
                self.origin.shadow(path + (key,))
                parsed = self._call(call, path + (key,), T.CallParse, local_sigs.get(fname), allow_empty=True)
                if parsed is None:
                    key = None
                else:
                    out[key] = parsed
                    cs = self.src.node_span(call)
                    self.origin.add(Item(self._base, path + (key,), key, "value", (gap_start, 0), (core_start, 0), cs, parsed,
                                         indent=indent, code_end=self.src.node_span(stmt)[1]), seq)
            elif is_function and kind == "If":
                self._if_chain(stmt, out, path, seq, cond_counters, block_occ, gap_start, core_start, indent)
            elif is_function and kind in ("For", "AsyncFor"):
                self._for_loop(stmt, out, path, seq, block_occ, gap_start, core_start, indent)
            elif is_function and kind in ("Try", "TryStar"):
                self._try_block(stmt, out, path, seq, block_occ, gap_start, core_start, indent)

            if key is not None and not is_def and not same_line:
                self._trailing_comment(stmt, out, path, key)
                if field_override is not None:
                    self._attach_field_override(field_override, out, path, key)

            if not is_def and not is_block:
                self.cursor = max(self.cursor, stmt.end_lineno)
            end = self.src.next_line_start(self.cursor) if not same_line else self.src.node_span(stmt)[1]
            if key is not None:
                item = self.origin.items[path + (key,)]
                item.extent = (item.extent[0], end)
                item.core = (item.core[0], end)
            if scope == "module":
                self.origin.top_stmts.append((self._base, end - gap_start, key))

    def _attach_field_override(self, group, out, path, key):
        first, last, texts, _ = group
        parsed = parse_override_comment("\n".join(texts))
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
        self.origin.add(Item(self._base, path + ("__overrides__", slot), slot, "override",
                             self._comment_extent(first, last), span, span, dict(parsed),
                             indent=self.src.indent_of_line(first)))

    # ── definitions ─────────────────────────────────────────────────────────────────

    def _def(self, stmt, out, path, seq, scope, child_override, gap_start, core_start):
        name = stmt.name
        child_path = path + (name,)
        first_line = self._stmt_first_line(stmt)
        indent = self.src.indent_of_line(first_line)
        is_init = (scope == "class" and _k(stmt) in ("FunctionDef", "AsyncFunctionDef") and name == "__init__")
        if is_init:
            self.cursor = max(self.cursor, stmt.end_lineno)
            return None
        code_span = (self.src.line_start(first_line) + len(indent), self.src.node_span(stmt)[1])
        self.origin.shadow(child_path)
        if _k(stmt) == "ClassDef":
            child = self._class(stmt, child_path)
        else:
            child = self._function(stmt, child_path)
        out[name] = child
        self.origin.add(Item(self._base, child_path, name, "def", (gap_start, 0), (core_start, 0), None, None,
                             indent=indent, code_end=code_span[1]), seq)
        if child_override is not None:
            parsed = parse_override_comment("\n".join(child_override[2]))
            if parsed:
                existing = child.get("__overrides__")
                if isinstance(existing, dict):
                    for k, v in parsed.items():
                        existing.setdefault(k, v)
                else:
                    child["__overrides__"] = parsed
                span = self._comment_span(child_override[0], child_override[1])
                self.origin.add(Item(self._base, child_path + ("__overrides__",), "__overrides__", "override",
                                     self._comment_extent(child_override[0], child_override[1]), span, span,
                                     dict(parsed), indent=self.src.indent_of_line(child_override[0])))
        return name

    def _class(self, node, path):
        T = self.T
        cls = T.EnumParse if _is_enum_classdef(node) else T.ClassParse
        first_line = self._stmt_first_line(node)
        start = self.src.line_start(first_line) + len(self.src.indent_of_line(first_line))
        end = self.src.node_span(node)[1]
        readable = cls(source=self.origin.text[start:end])
        readable.def_name = node.name
        self._stamp(readable, self.src.node_span(node)[0], end)
        decorators = self._decorators(node, path)
        if decorators:
            readable["decorators"] = decorators
        self.cursor = self._header_end_line(node)
        body_indent = self.src.indent_of_line(node.body[0].lineno)
        seq = self.origin.new_seq(path, "body", base=self._base, indent=body_indent,
                                  insert_at=self.src.line_start(node.body[0].lineno))
        self._body(node.body, readable, path, seq, scope="class")
        self._consume_footer(len(body_indent))
        self.origin.seal_seq(seq)
        self._init_fields(node, readable, path)
        return readable

    def _init_fields(self, node, readable, path):
        init = next((s for s in node.body if _k(s) in ("FunctionDef", "AsyncFunctionDef")
                     and s.name == "__init__"), None)
        if init is None:
            return
        body_indent = self.src.indent_of_line(init.body[0].lineno)
        seq = self.origin.new_seq(path, "body", base=self._base, default=False, indent=body_indent,
                                  insert_at=self.src.line_start(init.body[0].lineno))
        for stmt in init.body:
            target = value = None
            k = _k(stmt)
            if k == "Assign" and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif k == "AnnAssign":
                target, value = stmt.target, stmt.value
            if (target is None or value is None or _k(target) != "Attribute"
                    or _k(target.value) != "Name" or target.value.id != "self"):
                continue
            attr = target.attr
            if attr in readable:
                continue
            py = self._value(value, path + (attr,))
            readable[attr] = py
            vs = self.src.node_span(value)
            self._record_child(readable, attr, py, *vs)
            extent = (self.src.line_start(stmt.lineno), self.src.next_line_start(stmt.end_lineno))
            self.origin.add(Item(self._base, path + (attr,), attr, "value", extent, extent, vs, py,
                                 indent=self.src.indent_of_line(stmt.lineno),
                                 code_end=self.src.node_span(stmt)[1]), seq)
        self.origin.seal_seq(seq)

    def _function(self, node, path):
        T = self.T
        first_line = self._stmt_first_line(node)
        start = self.src.line_start(first_line) + len(self.src.indent_of_line(first_line))
        end = self.src.node_span(node)[1]
        readable = T.FunctionParse(source=self.origin.text[start:end])
        readable.def_name = node.name
        self._stamp(readable, self.src.node_span(node)[0], end)
        decorators = self._decorators(node, path)
        if decorators:
            readable["decorators"] = decorators
        params = self._params(node, path + ("parameters",))
        if params:
            readable["parameters"] = params
            self.origin.add(Item(ZERO_BASE, path + ("parameters",), "parameters", "pseudo", (0, 0), (0, 0), None, params))
        self.cursor = self._header_end_line(node)
        body_indent = self.src.indent_of_line(node.body[0].lineno)
        locals_ = T.GeneralParse(source="")
        seq = self.origin.new_seq(path + ("locals",), "body", base=self._base, indent=body_indent,
                                  insert_at=self.src.line_start(node.body[0].lineno))
        self._body(node.body, locals_, path + ("locals",), seq, scope="function")
        self._consume_footer(len(body_indent))
        self.origin.seal_seq(seq)
        if locals_:
            bs, be = self.src.node_span(node.body[0])[0], self.src.node_span(node.body[-1])[1]
            self._stamp(locals_, bs, be)
            readable["locals"] = locals_
            self.origin.add(Item(ZERO_BASE, path + ("locals",), "locals", "pseudo", (0, 0), (0, 0), None, locals_))
        else:
            self.origin.drop_seq(seq)
        return readable

    def _decorators(self, node, path):
        T = self.T
        if not node.decorator_list:
            return {}
        result = {}
        dpath = path + ("decorators",)
        seq = self.origin.new_seq(dpath, "decorators", base=self._base,
                                  indent=self.src.indent_of_line(node.decorator_list[0].lineno),
                                  insert_at=self.src.line_start(node.decorator_list[0].lineno))
        for dec in node.decorator_list:
            extent = (self.src.line_start(dec.lineno), self.src.next_line_start(dec.end_lineno))
            if _k(dec) == "Call":
                name = _call_func_name(dec)
                if name is None:
                    continue
                self.origin.shadow(dpath + (name,))
                self._nested += 1
                try:
                    parsed = self._call(dec, dpath + (name,), T.DecorationParse, None, allow_empty=True)
                finally:
                    self._nested -= 1
                if parsed is None:
                    parsed = T.CodeLine(self._text(dec))
                result[name] = parsed
                self.origin.add(Item(self._base, dpath + (name,), name, "decorator", extent, extent,
                                     self.src.node_span(dec), parsed,
                                     indent=self.src.indent_of_line(dec.lineno)), seq)
            else:
                code = self._text(dec)
                result[code] = code
                self.origin.add(Item(self._base, dpath + (code,), code, "decorator", extent, extent,
                                     self.src.node_span(dec), code,
                                     indent=self.src.indent_of_line(dec.lineno)), seq)
        self.origin.seal_seq(seq)
        if result:
            self.origin.add(Item(ZERO_BASE, dpath, "decorators", "pseudo", (0, 0), (0, 0), None, result))
        else:
            self.origin.drop_seq(seq)
        return result

    def _params(self, node, path):
        T = self.T
        a = node.args
        n_pos = len(a.posonlyargs) + len(a.args)
        defaults = [None] * (n_pos - len(a.defaults)) + list(a.defaults)
        regular = [(p, defaults[len(a.posonlyargs) + i]) for i, p in enumerate(a.args)]
        posonly = [(p, defaults[i]) for i, p in enumerate(a.posonlyargs)]
        kwonly = [(p, a.kw_defaults[i]) for i, p in enumerate(a.kwonlyargs)]
        ordered = regular + posonly + kwonly
        if not ordered:
            return None
        result = T.GeneralParse(source="")
        all_params = [p for p, _ in ordered]
        seq = self.origin.new_seq(path, "params", base=self._base, sep=", ")
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
                value = T.NO_DEFAULT
                vs = None
                extent = ps
            result[p.arg] = value
            self.origin.add(Item(self._base, path + (p.arg,), p.arg, "param", extent, extent, vs, value,
                                 slot=ps[1]), seq)
        if all_params:
            self._stamp(result, self.src.node_span(all_params[0])[0],
                        max(self.src.node_span(p)[1] for p in all_params))
        if len(seq.items) >= 2:
            seq.sep = self.origin.text[seq.items[0].extent[1]:seq.items[1].extent[0]]
        self.origin.seal_seq(seq)
        if not result:
            self.origin.drop_seq(seq)
            return None
        return result

    # ── blocks (function body) ─────────────────────────────────────────────

    def _block_body(self, stmts, out, path, header_line, block_state):
        self.cursor = header_line
        body_indent = self.src.indent_of_line(stmts[0].lineno)
        seq = self.origin.new_seq(path, "body", base=self._base, indent=body_indent,
                                  insert_at=self.src.line_start(stmts[0].lineno))
        self._body(stmts, out, path, seq, scope="function")
        self._consume_footer(len(body_indent))
        self.origin.seal_seq(seq)
        return seq

    def _finish_block_item(self, key, path, seq, gap_start, core_start, indent, kind="block"):
        end = self.src.next_line_start(self.cursor)
        self.origin.add(Item(self._base, path + (key,), key, kind, (gap_start, end), (core_start, end), None, None,
                             indent=indent), seq)

    def _condition(self, test, branch_path, keyword):
        T = self.T
        sub_call = _k(test) == "Subscript" and _k(test.value) == "Call"
        call = test.value if sub_call else test
        if _k(call) == "Call":
            cond_key = f"{_call_func_name(call) or 'call'}()##{keyword}"
        else:
            cond_key = f"##{keyword}"
        if sub_call:
            inner = self._call(test.value, branch_path + (cond_key,), T.CallParse, None)
            value = inner if inner is not None else T.CodeLine(self._text(test))
        else:
            value = self._value(test, branch_path + (cond_key,))
        return cond_key, value

    def _if_chain(self, node, out, path, seq, cond_counters, block_occ, gap_start, core_start, indent):
        T = self.T
        first = True
        while True:
            keyword = "if" if first else "elif"
            idx = cond_counters[keyword]
            cond_counters[keyword] += 1
            key = f"{keyword}##{idx}"
            branch_path = path + (key,)
            branch = T.Conditional(condition=key)
            cond_key, cond_value = self._condition(node.test, branch_path, keyword)
            branch[cond_key] = cond_value
            ts = self.src.node_span(node.test)
            self._record_child(branch, cond_key, cond_value, *ts)
            self.origin.add(Item(self._base, branch_path + (cond_key,), cond_key, "header", ts, ts, ts, cond_value))
            self._block_body(node.body, branch, branch_path, node.test.end_lineno, (cond_counters, block_occ))
            self._stamp(branch, ts[0], self.src.node_span(node.body[-1])[1])
            out[key] = branch
            self._finish_block_item(key, path, seq, gap_start, core_start, indent)
            first = False
            orelse = node.orelse
            if not orelse:
                return
            if (len(orelse) == 1 and _k(orelse[0]) == "If"
                    and self.src.line_text(orelse[0].lineno).lstrip().startswith("elif")):
                node = orelse[0]
                gap_start = core_start = self.src.line_start(node.lineno)
                indent = self.src.indent_of_line(node.lineno)
                continue
            idx = cond_counters["else"]
            cond_counters["else"] += 1
            key = f"else##{idx}"
            branch_path = path + (key,)
            branch = T.Conditional(condition=key)
            kw_line = self._keyword_line(orelse, self.cursor)
            else_seq = self._block_body(orelse, branch, branch_path, kw_line, (cond_counters, block_occ))
            if branch:
                self._stamp(branch, self.src.node_span(orelse[0])[0], self.src.node_span(orelse[-1])[1])
                out[key] = branch
                gs = self.src.line_start(kw_line)
                self._finish_block_item(key, path, seq, gs, gs, self.src.indent_of_line(kw_line))
            else:
                self.origin.drop_seq(else_seq)
            return

    def _for_loop(self, node, out, path, seq, block_occ, gap_start, core_start, indent):
        T = self.T
        target = self._text(node.target)
        it = self._text(node.iter)
        key = occ_key(f"for {target} in {it}", block_occ)
        loop_path = path + (key,)
        loop = T.Loop(target=target, iter=it)
        self._block_body(node.body, loop, loop_path, node.iter.end_lineno, None)
        range_args = self._range_args(node.iter, loop_path + ("range",))
        if range_args is not None:
            loop["range"] = range_args
            rs = (self.src.node_span(node.iter.args[0])[0], self.src.node_span(node.iter.args[-1])[1])
            self._record_child(loop, "range", range_args, *rs)
            self.origin.add(Item(self._base, loop_path + ("range",), "range", "value", rs, rs, rs, range_args))
        self._stamp(loop, *self.src.node_span(node))
        out[key] = loop
        self._finish_block_item(key, path, seq, gap_start, core_start, indent)
        if node.orelse:
            branch = T.Conditional(condition="else")
            kw_line = self._keyword_line(node.orelse, self.cursor)
            else_seq = self._block_body(node.orelse, branch, path + (f"{key} else",), kw_line, None)
            if branch:
                self._stamp(branch, self.src.node_span(node.orelse[0])[0], self.src.node_span(node.orelse[-1])[1])
                out[f"{key} else"] = branch
                gs = self.src.line_start(kw_line)
                self._finish_block_item(f"{key} else", path, seq, gs, gs, self.src.indent_of_line(kw_line))
            else:
                self.origin.drop_seq(else_seq)

    def _range_args(self, iter_node, path):
        if not (_k(iter_node) == "Call" and _k(iter_node.func) == "Name"
                and iter_node.func.id == "range" and iter_node.args and not iter_node.keywords):
            return None
        args = []
        seq = self.origin.new_seq(path, "elements", base=self._base, sep=", ",
                                  insert_at=self.src.node_span(iter_node.args[0])[0])
        for i, a in enumerate(iter_node.args):
            v = self._literal(a)
            if v is UNRESOLVED:
                self.origin.drop_seq(seq)
                for j in range(i):
                    self.origin.items.pop(path + (j,), None)
                return None
            args.append(v)
            sp = self.src.node_span(a)
            self.origin.add(Item(self._base, path + (i,), i, "element", sp, sp, sp, v), seq)
        self.origin.seal_seq(seq)
        return args

    def _try_block(self, node, out, path, seq, block_occ, gap_start, core_start, indent):
        T = self.T
        try_key = occ_key("try", block_occ)
        body = T.Try(header="try")
        tseq = self._block_body(node.body, body, path + (try_key,), node.lineno, None)
        if body:
            self._stamp(body, self.src.node_span(node.body[0])[0], self.src.node_span(node.body[-1])[1])
            out[try_key] = body
            self._finish_block_item(try_key, path, seq, gap_start, core_start, indent)
        else:
            self.origin.drop_seq(tseq)
        star = _k(node) == "TryStar"
        for handler in node.handlers:
            parts = ["except*" if star else "except"]
            if handler.type is not None:
                parts.append(self._text(handler.type))
            if handler.name is not None:
                parts += ["as", handler.name]
            header = " ".join(parts)
            hkey = occ_key(header, block_occ)
            hbody = T.Except(header=header)
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
            ekey = occ_key("try else", block_occ)
            ebody = T.Try(header="try else")
            kw_line = self._keyword_line(node.orelse, self.cursor)
            eseq = self._block_body(node.orelse, ebody, path + (ekey,), kw_line, None)
            if ebody:
                self._stamp(ebody, self.src.node_span(node.orelse[0])[0], self.src.node_span(node.orelse[-1])[1])
                out[ekey] = ebody
                gs = self.src.line_start(kw_line)
                self._finish_block_item(ekey, path, seq, gs, gs, self.src.indent_of_line(kw_line))
            else:
                self.origin.drop_seq(eseq)
        if node.finalbody:
            fkey = occ_key("finally", block_occ)
            fbody = T.Try(header="finally")
            kw_line = self._keyword_line(node.finalbody, self.cursor)
            fseq = self._block_body(node.finalbody, fbody, path + (fkey,), kw_line, None)
            if fbody:
                self._stamp(fbody, self.src.node_span(node.finalbody[0])[0], self.src.node_span(node.finalbody[-1])[1])
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
        k = _k(node)
        if k == "Constant" and isinstance(node.value, _SIMPLE_LITERAL_TYPES):
            return node.value
        if (k == "UnaryOp" and _k(node.op) in ("USub", "UAdd") and _k(node.operand) == "Constant"
                and isinstance(node.operand.value, (int, float)) and not isinstance(node.operand.value, bool)):
            return -node.operand.value if _k(node.op) == "USub" else node.operand.value
        return UNRESOLVED

    def _value(self, node, path):
        T = self.T
        lit = self._literal(node)
        if lit is not UNRESOLVED:
            return lit
        k = _k(node)
        if k == "Subscript" and _k(node.value) == "Call":
            inner = self._call(node.value, path, T.CallParse, None)
            if inner is not None:
                return inner
            return T.CodeLine(self._text(node))
        if k == "Call":
            parsed = self._call(node, path, T.CallParse, None)
            return parsed if parsed is not None else T.CodeLine(self._text(node))
        if k in ("Tuple", "List"):
            if any(_k(e) == "Starred" for e in node.elts):
                return T.CodeLine(self._text(node))
            cstart = self.src.node_span(node)[0]
            seq = self.origin.new_seq(path, "elements", base=self._base, sep=", ",
                                      insert_at=cstart + (1 if self.origin.text[cstart] in "([{" else 0))
            values = []
            self._nested += 1
            for i, e in enumerate(node.elts):
                v = self._value(e, path + (i,))
                values.append(v)
                sp = self.src.node_span(e)
                self.origin.add(Item(self._base, path + (i,), i, "element", sp, sp, sp, v), seq)
            self._nested -= 1
            if len(seq.items) >= 2:
                seq.sep = self.origin.text[seq.items[0].extent[1]:seq.items[1].extent[0]]
            if seq.items:
                self.origin.seal_seq(seq)
            else:
                self.origin.drop_seq(seq)
            return tuple(values) if k == "Tuple" else values
        if k == "Dict":
            if any(kn is None for kn in node.keys):
                return T.CodeLine(self._text(node))
            keys = []
            for kn in node.keys:
                try:
                    kv = _const_value(kn)
                    hash(kv)
                except Exception:
                    # Mode dictionaries use type expressions as keys. Keep
                    # those expressions unevaluated while exposing their
                    # nested kwargs to the same editable source tree.
                    kv = T.CodeLine(self._text(kn))
                keys.append(kv)
            seq = self.origin.new_seq(path, "pairs", base=self._base, sep=", ",
                                      insert_at=self.src.node_span(node)[0] + 1)
            result = {}
            self._nested += 1
            for kv, knode, vnode in zip(keys, node.keys, node.values):
                self.origin.shadow(path + (kv,))
                v = self._value(vnode, path + (kv,))
                result[kv] = v
                ks, ke = self.src.node_span(knode)
                vs = self.src.node_span(vnode)
                self.origin.add(Item(self._base, path + (kv,), kv, "pair", (ks, vs[1]), (ks, vs[1]), vs, v), seq)
            self._nested -= 1
            if len(seq.items) >= 2:
                seq.sep = self.origin.text[seq.items[0].extent[1]:seq.items[1].extent[0]]
            if seq.items:
                self.origin.seal_seq(seq)
            else:
                self.origin.drop_seq(seq)
            return result
        if k == "Set":
            elements = []
            for e in node.elts:
                v = self._literal(e)
                if v is UNRESOLVED:
                    return T.CodeLine(self._text(node))
                elements.append(v)
            return set(elements)
        if k in ("Name", "Attribute"):
            parts = _dotted_parts(node)
            if parts is not None:
                if T.resolve is None:
                    return NameRef(self._text(node), parts)     # worker: the main side resolves
                resolved = T.resolve(parts)
                if resolved is not UNRESOLVED:
                    return resolved
        return T.CodeLine(self._text(node))

    def _call(self, call, path, result_cls, pos_names_override, allow_empty=False):
        T = self.T
        readable = result_cls(source=self._text(call), func_name=_call_func_name(call))
        positional = [a for a in call.args if _k(a) != "Starred"]
        pending = None
        if pos_names_override is not None:
            pos_names = pos_names_override
        elif positional:
            parts = _dotted_parts(call.func)
            pos_names = None
            if parts is not None:
                if T.positional_names is None:
                    pending = parts          # worker: the main side binds the runtime signature
                else:
                    pos_names = T.positional_names(parts)
            if pos_names is None:
                pos_names = [f"arg{i}" for i in range(len(positional))]
                if any(n in {kw.arg for kw in call.keywords} for n in pos_names):
                    pos_names = None
                    pending = None
        else:
            pos_names = None
        seq = self.origin.new_seq(path, "args", base=self._base, sep=", ")
        pos_idx = 0
        nested = self._nested > 0
        self._nested += 1
        for a in call.args:
            if _k(a) == "Starred":
                continue
            if pos_names is not None and pos_idx < len(pos_names):
                key = pos_names[pos_idx]
                v = self._value(a, path + (key,))
                readable[key] = v
                sp = self.src.node_span(a)
                self.origin.add(Item(self._base, path + (key,), key, "element", sp, sp, sp, v), seq)
            pos_idx += 1
        for kw in call.keywords:
            if kw.arg is None:
                continue
            v = self._value(kw.value, path + (kw.arg,))
            readable[kw.arg] = v
            vs = self.src.node_span(kw.value)
            ks = self.src.node_span(kw)[0]
            self.origin.add(Item(self._base, path + (kw.arg,), kw.arg, "kwarg", (ks, vs[1]), (ks, vs[1]), vs, v), seq)
        self._nested -= 1
        if len(seq.items) >= 2:
            seq.sep = self.origin.text[seq.items[0].extent[1]:seq.items[1].extent[0]]
        # Always anchored just inside the closing paren: the main side may drop
        # positionals a runtime signature doesn't name (`print(x)`) and re-seal.
        seq.insert_at = self.src.node_span(call)[1] - 1
        self.origin.seal_seq(seq)
        if not readable and not allow_empty:
            self.origin.drop_seq(seq)
            return None
        callee_span = self.src.node_span(call.func)
        self.origin.add(Item(self._base, path + ("__callee__",), "__callee__", "callee",
                             callee_span, callee_span, callee_span, self._text(call.func)))
        if pos_names:
            readable["__pos_names__"] = list(pos_names)
        if pending is not None:
            readable._pos_pending = pending
        if not nested:
            self._stamp(readable, *self.src.node_span(call))
        return readable


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Worker entry                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def extract(text, *, frontend, types, file_path=None, line_offset=0, module_header=True):
    """(gp, origin) for `text` through one front end: "scan" (the tokenizer
    parser) or "ast" (Python's parser, the oracle)."""
    if frontend == "ast":
        tree = ast.parse(text)
        comments = scan_comments(text)
    else:
        tree, standalone, trailing = scan(text)
        comments = (standalone, trailing)
    origin = Origin(text)
    origin.file_path = file_path
    origin.line_offset = line_offset
    gp = Extractor(origin, types, comments, module_header=module_header).module(tree)
    return gp, origin


def scan_extract(text, *, validate=False):
    """The worker's job: scanner front end, neutral types, validated by
    ast.parse (whose SyntaxError is reported as data — nothing raises across
    the interpreter boundary). Returns ("ok", gp, origin) or
    ("error", message, lineno, offset)."""
    if validate:
        try:
            ast.parse(text)
        except SyntaxError as e:
            return ("error", e.msg, e.lineno or 0, e.offset or 0)
    try:
        gp, origin = extract(text, frontend="scan", types=NEUTRAL_TYPES)
    except SyntaxError as e:
        return ("error", str(e.msg if hasattr(e, "msg") else e), getattr(e, "lineno", 0) or 0,
                getattr(e, "offset", 0) or 0)
    return ("ok", gp, origin)
