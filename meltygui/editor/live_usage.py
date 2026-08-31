"""Display-time USAGE index for live values.

A usage of a captured symbol is never captured and never holds the value:
`build_usage_index` maps each occurrence of a bound name inside an
instrumented def to its position, and the paint pass (live_view_views.
_draw_usage_labels) resolves the governing BINDING at draw time and reads
that binding's single store entry by reference. Nothing runs in the user's
function for usages, and the only derived data is the formatted label.

stdlib-only (tokenize) on purpose: exact word boundaries, and string/comment
interiors are excluded for free. The index is rebuilt only when the parse
source changes or the store's key set grows (the caller memoizes) — never
per frame.
"""

import bisect
import io
import keyword
import tokenize

# A NAME right after one of these is never a value usage: definitions,
# import aliases, loop/with/except binding targets, lambda params,
# attribute access.
_SKIP_AFTER = frozenset((
    "def", "class", "import", "from", "as", "global", "nonlocal",
    "for", "lambda", "."))

_OPEN_BRACKETS = frozenset("([{")
_CLOSE_BRACKETS = frozenset(")]}")
_SKIP_TOKEN_TYPES = frozenset((
    tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
    tokenize.COMMENT, tokenize.ENCODING, tokenize.ENDMARKER))


def binding_name(key_path, labels):
    """The SYMBOL a store key binds — its label when that's a plain
    identifier, else the name encoded in the key tail ('x', 'x#1',
    'line:N#x'). None for keys that don't name a symbol (call-site keys
    like 'live_view()#1', expression labels, keyword tails)."""
    label = (labels or {}).get(key_path)
    if isinstance(label, str) and label.isidentifier() \
            and not keyword.iskeyword(label):
        return label
    tail = key_path[-1] if key_path else None
    if not isinstance(tail, str):
        return None
    if tail.startswith("line:"):
        name = tail.partition("#")[2]
    else:
        name = tail.partition("#")[0]
    if name.isidentifier() and not keyword.iskeyword(name):
        return name
    return None


def _next_significant(tokens, i):
    """The next token past NL/comment/indent noise, else None."""
    for j in range(i + 1, len(tokens)):
        if tokens[j][0] not in _SKIP_TOKEN_TYPES:
            return tokens[j]
    return None


def build_usage_index(def_text, first_line, binding_lines, exclude_ranges=()):
    """Occurrences of the bound names inside one def's source.

    `def_text` is the def's source slice (its `def` line first), sitting at
    parse line `first_line`; `binding_lines` maps name → SORTED parse lines
    of that name's binding sites (from the store's anchors);
    `exclude_ranges` are (lo, hi) parse-line ranges to skip — nested def
    bodies, where an occurrence is the closure's own scope, not a value
    read of the outer binding.

    Returns [(parse_line, col, name, last_on_line), …] sorted by position:
    every NAME token equal to a bound name that is a genuine USE — not an
    attribute (`.name`), not a keyword argument (`name=` inside brackets),
    not a binding/definition target (first occurrence of the name on one
    of its own binding lines, or a NAME right after def/for/as/…), and
    only with at least one binding textually ABOVE it (same-line bindings
    govern nothing on their own line: in `x = x + 1` the RHS x reads the
    previous binding). `last_on_line` is True when no further CODE token
    follows on the occurrence's line (comments don't count) — the pill may
    then run past the symbol's width, since there's nothing to cover."""
    out = []
    # The slice keeps the module's indentation; tokenize likes column-0
    # code, so strip the first line's indent from every line that carries it
    # and tack it back onto the reported columns. Lines that don't start
    # with it live inside multi-line strings - the tokenizer consumes those
    # as part of the STRING token, so leaving them untouched is fine.
    lines = def_text.split("\n")
    indent = len(lines[0]) - len(lines[0].lstrip()) if lines else 0
    if indent:
        pad = " " * indent
        lines = [l[indent:] if l.startswith(pad) else l for l in lines]
        def_text = "\n".join(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(def_text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out
    previous = None            # last significant token string
    depth = 0                  # bracket depth, for the kwarg test
    seen_on_line = set()       # (line, name) - binding-target skip
    for i, tok in enumerate(tokens):
        token_type, string, start = tok[0], tok[1], tok[2]
        if token_type in _SKIP_TOKEN_TYPES:
            continue
        if token_type == tokenize.OP:
            if string in _OPEN_BRACKETS:
                depth += 1
            elif string in _CLOSE_BRACKETS:
                depth = max(0, depth - 1)
            previous = string
            continue
        if token_type != tokenize.NAME:
            previous = string
            continue
        name = string
        line = first_line - 1 + start[0]
        col = start[1] + indent
        skip_after = previous in _SKIP_AFTER
        previous = name
        binding_list = binding_lines.get(name)
        if not binding_list or skip_after or keyword.iskeyword(name):
            continue
        if any(lo <= line <= hi for lo, hi in exclude_ranges):
            continue
        # Keyword argument: `f(name=...)` names the call's parameter, not
        # this local - a bare `=` (never `==`) inside brackets.
        if depth > 0:
            nxt = _next_significant(tokens, i)
            if nxt is not None and nxt[0] == tokenize.OP and nxt[1] == "=":
                continue
        # Binding target: the FIRST occurrence of the name on one of its
        # own binding lines is the assignment's left side; later ones on
        # the same line (`x = x + 1`) are reads of the previous binding.
        first_on_line = (line, name) not in seen_on_line
        seen_on_line.add((line, name))
        if first_on_line and _in_sorted(binding_list, line):
            continue
        if bisect.bisect_left(binding_list, line) == 0:
            continue          # nothing bound above - nothing to show
        nxt = _next_significant(tokens, i)
        last_on_line = nxt is None or nxt[2][0] > start[0]
        out.append((line, col, name, last_on_line))
    return out


def _in_sorted(sorted_lines, line):
    ix = bisect.bisect_left(sorted_lines, line)
    return ix < len(sorted_lines) and sorted_lines[ix] == line


def governing_key(binding_lines, binding_keys, line, publish_seq):
    """The store key a usage at parse `line` reads: among this name's
    bindings textually ABOVE the line, the one that PUBLISHED last
    (`publish_seq`: key → monotonic publish counter — the branch-not-taken
    rule: the textually-later `else:` binding loses to the `if:` binding
    that actually ran). Unpublished/unknown keys rank below every published
    one; ties (no seq data at all) fall back to textual order — the latest
    binding above wins. None when no binding sits above the line."""
    ix = bisect.bisect_left(binding_lines, line)
    best = None
    best_rank = None
    for j in range(ix):
        key = binding_keys[j]
        rank = (publish_seq.get(key, -1), j)
        if best_rank is None or rank > best_rank:
            best, best_rank = key, rank
    return best
