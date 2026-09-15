"""Editor consumers of the symbol roster (`core_conversion/symbol_roster.py`):

  collect_def_tints  — the definition-tint washes (blocks + per-occurrence
                       spans + line bands + name→rgb map) in EXACTLY the
                       4-tuple shape `_collect_def_tints` returns, built from
                       the live buffer + roster instead of the cst-dict +
                       usage graph. No live objects, no background pass: a
                       tinted def typed into ANY file paints its references
                       here on the next rebuild.
  ctrl_b_lookup      — Ctrl+B at a buffer index: usage → its definition
                       (roster resolve), definition → its usages (roster
                       reverse lookup over the trigram index). Pending /
                       live-buffer coordinates throughout.

Kept out of text_editor.py so the roster path is one small, hotswappable
unit; text_editor only gates between the two pipelines.
"""
from __future__ import annotations

import bisect
import re
import time

import meltygui.code.symbol_roster as roster

_ASN_RE = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*[:=](?!=)")
_IDENT_RE = re.compile(r"[A-Za-z_][\w.]*")
_DEF_LINE_RE = re.compile(r"\s*(?:async\s+)?(def|class)\s")


def _decor_start(lines, def_line):
    """Top of a def's wash: extend upward over the decorator / comment run
    directly above the class/def keyword (reverse paren-balanced), stopping
    at a blank line or any other statement."""
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


def collect_def_tints(text, line_offset=0, view_path=None, window=None,
                      line_open=None, hold_live=True, world=None, table=None, project=None):
    """(blocks, spans, line_tints, name_tints) — see module doc.

    blocks:     [(def_buf_line, indent_buf_index, end_buf_line, tint)] per
                tinted class/def DEFINED in this buffer.
    spans:      [(start, end, rgb, scale)] per occurrence of a symbol whose
                definition — here or in another file — carries a tint.
    line_tints: [(buf_line, rgb, scale, text_start, text_end)].
    name_tints: {name: rgb} for the completion popup.

    `window` = (lo, hi) 0-based buffer lines: occurrences (spans, line
    bands, the assignment sweep) are only computed inside it — the editor
    passes its visible band plus margin, so the per-rebuild cost is
    O(viewport), not O(buffer). Blocks always cover the whole buffer (they
    come from the table, not a scan). `line_open` is the editor's per-line
    string state (text_editor._update_line_open) so the scan can start at
    the nearest string-clean line at/above `lo`; without it the start backs
    up to the nearest column-0 def/class/decorator/import line.

    `hold_live=False` for READ-ONLY previews of the file (global-search
    rows: one-line span views of the pending text): the buffer is then NOT
    installed as the file's live roster override. Holding it would let
    every row of the same file stomp the hold in turn — each splice differs
    (a class's 1-line span has no tint comment / extent), so the roster
    generation bumped per row per frame, every roster consumer re-keyed and
    re-rendered continuously, and cross-file lookups into that file saw a
    different table each frame (washes flickering in the search list).

    `world` (a symbol_roster.World) makes the buffer a view of ANOTHER
    version of its file — the merge window's disk / sync-frame panes: its
    own table comes from the world (built from the same text object the
    pane shows) and every cross-file reference resolves through the
    world's tables, so `Toggles.Foo` paints in the tint that version of
    toggles.py carries. `table` overrides only the buffer's OWN table (a
    detached_table of a staged result) while other files stay the
    studio's. Neither installs anything in the roster."""
    if view_path is None:
        return ((), (), (), {})
    project = roster.analysis_project(project, view_path)
    with roster.pass_scope():
        return _collect(text, line_offset, view_path, window, line_open,
                        hold_live=hold_live, world=world, table=table, project=project)


# Local-binding memo: (id(text), path) -> (text, {scope-qualname tuple:
# bindings}). A pure scroll re-runs the windowed pass on the SAME buffer; the
# 5000-line draw_text scope's binding cost (~10ms) must not be paid per chunk
# crossed. A few slots (not one): the merge window runs three panes of ONE
# file with three texts like this in a row. The text is held so its id
# stays unique while the entry lives.
_bind_memo = {}


def _bindings_memo(text, own, line_offset, lines, scopes):
    if not scopes:
        return {}
    key = (id(text), own.path)
    held = _bind_memo.get(key)
    if held is None or held[0] is not text:
        if len(_bind_memo) >= 8:
            _bind_memo.clear()
        held = _bind_memo[key] = (text, {})
    sk = tuple(e.qualname for e in scopes)
    b = held[1].get(sk)
    if b is None:
        b = held[1][sk] = roster.local_bindings(text, own, line_offset, lines, scopes)
    return b


_DEF_LIKE_COL0 = re.compile(r"(?:async\s+)?(?:def|class)\s|@|import\s|from\s")


def _scan_range(lines, line_start_idx, text, window, line_open):
    """(scan_start_index, scan_end_index, lo_line, hi_line) for the chain
    scan: the window clamped to the buffer, its start backed up to a line
    that is not inside a multi-line string."""
    n = len(lines)
    if window is None:
        return 0, len(text), 0, n - 1
    lo = max(0, min(int(window[0]), n - 1))
    hi = max(lo, min(int(window[1]), n - 1))
    sl = lo
    if line_open is not None and len(line_open) >= n:
        while sl > 0 and line_open[sl] is not None:
            sl -= 1
    else:
        while sl > 0 and not _DEF_LIKE_COL0.match(lines[sl]):
            sl -= 1
    start = line_start_idx[sl]
    end = line_start_idx[hi + 1] - 1 if hi + 1 < len(line_start_idx) else len(text)
    return start, min(end, len(text)), lo, hi


def _collect(text, line_offset, view_path, window=None, line_open=None,
             hold_live=True, world=None, table=None, project=None):
    from meltygui.toggles import Toggles
    lines = text.split("\n")
    line_start_idx = [0]
    for l in lines:
        line_start_idx.append(line_start_idx[-1] + len(l) + 1)
    vpath = roster._norm(str(view_path))
    # The buffer's own table: an explicit one (a staged result), the world's
    # (disk / sync-frame pane - the world's text for this path IS the pane's
    # text), else the roster's - except a preview buffer resolves against
    # the PENDING text (or whatever live hold a real editor of this file
    # keeps) instead of becoming the hold.
    if table is not None:
        own = table
    elif world is not None:
        own = world.table(vpath)
    else:
        own = roster.table_for(vpath, live_text=text if hold_live else None,
                               line_offset=line_offset)
    scan_start, scan_end, win_lo, win_hi = _scan_range(lines, line_start_idx, text,
                                                       window, line_open)

    # ── blocks: tinted class/def entries of this file that sit in the buffer ──
    blocks = []
    block_range = {}                  # entry qualname -> (buf_start, buf_end)
    for e in own.entries:
        if e.tint is None or e.kind == "var":
            continue
        bl = e.line - 1 - line_offset
        if not (0 <= bl < len(lines)):
            continue
        lt = lines[bl]
        indent = len(lt) - len(lt.lstrip())
        end = min(e.end - 1 - line_offset, len(lines) - 1)
        # Trim trailing blank lines off the block (the indent walk leaves
        # them outside, but a span-view's pending table may see past).
        while end > bl and not lines[end].strip():
            end -= 1
        start = _decor_start(lines, bl)
        blocks.append((start, line_start_idx[start] + indent, end, tuple(e.tint[:3])))
        block_range[e.qualname] = (start, end)

    # ── locals: bindings of the function scopes touching the window ──
    # A local is first-class: its binding(s) + every occurrence in its scope
    # (and nested closures) are a symbol; it SHADOWS roster names; and its
    # tint is its own `# [tint=...]` or - with propagation on - a faded
    # blend of what its binding line reads (chained locals fade per hop).
    from meltygui.editor.text import _scan_def_tint_lines
    fade = Toggles.TextEditor.def_propagation_fade
    mix_on = Toggles.TextEditor.def_tint_propagation
    win_file_lo, win_file_hi = win_lo + 1 + line_offset, win_hi + 1 + line_offset
    scopes = [e for e in own.entries
              if e.kind == "def" and e.line <= win_file_hi and e.end >= win_file_lo]
    # Binding / tint SOURCE text: the buffer itself when it is the whole
    # file; for a SPAN buffer (a function body or a one-line search), use the
    # pending file - a local defined above the span (a param, an earlier
    # assignment) must still colour its uses inside it.
    # A staged / detached table was built from this very buffer - it is the
    # whole file just like a live one.
    whole_file = (line_offset == 0
                  and (world is not None or table is not None
                       or (isinstance(own.key, tuple) and own.key[0] == "live")))
    if whole_file:
        ftext, flines, foff = text, lines, 0
    else:
        ftext = roster.file_text(vpath)
        flines, foff = ftext.split("\n"), 0
    bindings = _bindings_memo(ftext, own, foff, flines, scopes)
    local_occ = {}      # (scope, name) -> [(start, end, buf_line)]
    tint_lines = roster.tint_line_index(flines)  # prefilter for explicit-tint scans

    # ── spans: every identifier chain in code, resolved per occurrence ──
    spans = []
    seen_spans = set()
    name_tint = {}      # roster-resolved prefix / leaf name -> (rgb, scale)
    memo = {}           # (scope qualname, chain) -> [(n_parts, entry)] (tinted only)
    for s, e_, chain in roster.iter_chains(text, scan_start, scan_end):
        ln = bisect.bisect_right(line_start_idx, s) - 1
        if ln < win_lo:
            continue          # lead-in from the string-clean start: not visible
        sc = own.scope_at(ln + 1 + line_offset)
        scq = sc.qualname if sc is not None else None
        if bindings and sc is not None:
            dot = chain.find(".")
            first = chain if dot == -1 else chain[:dot]
            lk = roster.local_key(own, sc, first, bindings)
            if lk is not None:
                local_occ.setdefault(lk, []).append((s, s + len(first), ln))
                continue      # a local shadows every roster name
        mkey = (scq, chain)
        got = memo.get(mkey)
        if got is None:
            got = []
            for ent, n in roster.resolve_prefixes(vpath, chain, own, scope=sc,
                                                  world=world, project=project):
                if ent.tint is not None:
                    got.append((n, ent))
            memo[mkey] = got
        if not got:
            continue
        parts = chain.split(".")
        for n, ent in got:
            prefix = ".".join(parts[:n])
            # The wash covers only the resolved SEGMENT (`TextEditor` in
            # `Toggles.TextEditor.x`), not the whole prefix back to the
            # chain start - the earlier parts carry their own rects.
            pe = s + len(prefix)
            ps = pe - len(parts[n - 1])
            in_own = ent.path == vpath
            rng = block_range.get(ent.qualname) if in_own else None
            if rng is not None and rng[0] <= ln <= rng[1]:
                continue             # structurally redundant inside its own def block
            if (ps, pe) in seen_spans:
                continue
            seen_spans.add((ps, pe))
            rgb = tuple(ent.tint[:3])
            at_own_def = in_own and (ln + 1 + line_offset) == ent.line
            spans.append((ps, pe, rgb, 1.0, at_own_def))
            name_tint[prefix] = (rgb, 1.0)
            name_tint.setdefault(ent.name, (rgb, 1.0))

    # ── local tints: own comment > propagation blend; then every occurrence ──
    # local_tint[(scope, name)] = [(from_file_line, rgb, scale)] - segments sorted
    # in order: the FIRST binding's tint owns the name, a LATER binding with
    # its own `# [tint=...]` takes over from that line on  Computed LAZILY for
    # the locals that occur in the window (plus the locals their binding
    # lines read, recursively - that's the propagation chain), never for
    # every binding of a 5000-line function.
    local_tint = {}
    _IN_PROGRESS = ()

    def _seg_at(segs, at_line):
        cur = segs[0]
        for sg in segs:
            if sg[0] <= at_line:
                cur = sg
        return cur

    def _tok_tint(tok, sc, at_line, depth):
        """Tint of an RHS token read in scope `sc` on file line `at_line`: a
        visible local's tint IN EFFECT at that line first (innermost scope
        wins; a later rebinding doesn't colour earlier reads), else the
        roster's."""
        dot = tok.find(".")
        first = tok if dot == -1 else tok[:dot]
        lk = roster.local_key(own, sc, first, bindings) if sc is not None else None
        if lk is not None:
            segs = _local_segments(lk, depth + 1)
            return _seg_at(segs, at_line)[1:] if segs else None
        t = name_tint.get(tok)
        if t is None and dot != -1:
            t = name_tint.get(first)
        return t

    def _rhs_of(b, lt):
        code = roster._code_part(lt)
        if b.kind == "assign":
            p = code.find("=", b.col)
            return code[p + 1:] if p != -1 else ""
        if b.kind == "for":
            p = code.find(" in ", b.col)
            return code[p + 4:] if p != -1 else ""
        if b.kind == "walrus":
            p = code.find(":=", b.col)
            return code[p + 2:] if p != -1 else ""
        return ""

    def _local_segments(lk, depth=0):
        got = local_tint.get(lk)
        if got is not None:
            return () if got is _IN_PROGRESS else got
        local_tint[lk] = _IN_PROGRESS          # cycle guard (x = x + 1)
        segs = []
        sc_e = own.by_qualname.get(lk[0])
        blist = bindings.get(lk, ())
        for b in blist:
            bl = b.line - 1 - foff
            if not (0 <= bl < len(flines)):
                continue
            lt = flines[bl]
            own_t = None
            if roster._scan_tint(flines, bl + 1, None, tint_lines, None) is not None:
                try:
                    res = _scan_def_tint_lines(flines, bl + 1, None)
                except Exception:
                    res = None
                if res is not None and res[1] == bl + 1:
                    own_t = (tuple(res[0][:3]), 1.0)
            if own_t is not None:
                segs.append((b.line, own_t[0], own_t[1]))
            elif not segs and mix_on and b is blist[0] and depth < 6:
                rhs = _rhs_of(b, lt)
                if not rhs:
                    continue
                contribs = [t for t in (_tok_tint(tok, sc_e, b.line, depth)
                                        for tok in _IDENT_RE.findall(rhs))
                            if t is not None]
                if not contribs:
                    continue
                uniq = list(dict.fromkeys(contribs))
                n = len(uniq)
                rgb = tuple(sum(c[0][i] for c in uniq) / n for i in range(3))
                scale = fade * (sum(c[1] for c in uniq) / n)
                if scale < 0.2:
                    continue
                segs.append((b.line, rgb, scale))
        segs.sort()
        local_tint[lk] = segs
        return segs

    for lk, occs in local_occ.items():
        segs = _local_segments(lk)
        if not segs:
            continue
        for (s, e, ln) in occs:
            fl = ln + 1 + line_offset
            seg = _seg_at(segs, fl)
            if (s, e) in seen_spans:
                continue
            seen_spans.add((s, e))
            spans.append((s, e, seg[1], seg[2], fl == seg[0]))
        name_tint.setdefault(lk[1], (segs[-1][1], segs[-1][2]))

    blocks.sort()
    # Misresolution filter (ported): an assignment-target span inside a
    # tinted block that isn't its own original def line is a same-name
    # field of THIS class coloured by another definition; drop it.
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
    line_mix = {}
    for sp in spans:
        ln = bisect.bisect_right(line_start_idx, sp[0]) - 1
        line_mix.setdefault(ln, []).append(sp)
    line_tints = []
    for ln, entries in line_mix.items():
        own_e = [e for e in entries if e[4] and e[3] >= 1.0]
        pick = own_e if own_e else entries
        uniq = list(dict.fromkeys((e[2], e[3]) for e in pick))
        n = len(uniq)
        rgb = tuple(sum(c[0][i] for c in uniq) / n for i in range(3))
        lt = lines[ln] if 0 <= ln < len(lines) else ""
        ind = len(lt) - len(lt.lstrip())
        line_tints.append((ln, rgb, sum(c[1] for c in uniq) / n,
                           line_start_idx[ln] + ind,
                           line_start_idx[ln] + max(len(lt.rstrip()), ind + 1)))
    line_tints.sort()
    spans = [sp[:4] for sp in spans]
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    name_tints = {}
    for nm, t in name_tint.items():
        if t is not None:
            name_tints[nm] = tuple(t[0][:3])
    for nm, t in name_tint.items():
        if t is not None and "." in nm:
            name_tints.setdefault(nm.rsplit(".", 1)[-1], tuple(t[0][:3]))
    return (tuple(blocks), tuple(spans), tuple(line_tints), name_tints)


# ── Ctrl+B ────────────────────────────────────────────────────────────────────

class _Sym:
    """The tiny `su`-shaped object _present_usage_targets reads `.name` off."""
    __slots__ = ("name", "entry")

    def __init__(self, name, entry):
        self.name = name
        self.entry = entry


def ctrl_b_lookup(full_text, pos, view_path, line_offset=0, project=None):
    """Resolve Ctrl+B at full-buffer index `pos`. Returns None when the
    caret isn't on a resolvable symbol, else
      (start, end, sym, at_def, targets)
    with start/end the caret part's span in `full_text` coordinates, `sym`
    carrying `.name`, and `targets` a list of UsageRef: [definition] at a
    usage, or the usages at the definition. Coordinates are pending/live
    throughout — no disk bridge."""
    from meltygui.code.libcst_conversion import UsageRef
    from pathlib import Path
    if view_path is None:
        return None
    hit = roster.chain_at(full_text, pos)
    if hit is None:
        return None
    cs, ce, chain, part_ix = hit
    parts = chain.split(".")
    vpath = roster._norm(str(view_path))
    t0 = time.perf_counter()
    project = roster.analysis_project(project, view_path)
    roster.sweep(force=True, project=project)       # Ctrl+B is live and wants fresh tables
    with roster.pass_scope():
        return _lookup(full_text, pos, vpath, line_offset, cs, ce, chain, parts,
                       part_ix, t0, project)


def _lookup(full_text, pos, vpath, line_offset, cs, ce, chain, parts, part_ix, t0, project=None):
    from meltygui.code.libcst_conversion import UsageRef
    from pathlib import Path
    own = roster.table_for(vpath, live_text=full_text, line_offset=line_offset)
    caret_line = full_text.count("\n", 0, cs) + 1 + line_offset
    sc = own.scope_at(caret_line)
    # Locals first: a bare name bound in the caret's function (or an
    # enclosing one) is that local - binding <-> every occurrence in scope.
    if sc is not None and part_ix == 0:
        loc = _local_ctrl_b(full_text, line_offset, own, sc, parts[0], cs, caret_line)
        if loc is not None:
            return loc
    res = roster.resolve_prefixes(vpath, parts[:part_ix + 1], own, scope=sc, project=project)
    entry = None
    for ent, n in res:
        if n == part_ix + 1:
            entry = ent
    if entry is None:
        return None
    # The caret part's own span.
    off = cs
    for i, p in enumerate(parts):
        if i == part_ix:
            ps, pe = off, off + len(p)
            break
        off += len(p) + 1
    else:
        ps, pe = cs, ce
    file_line = full_text.count("\n", 0, ps) + 1 + line_offset
    at_def = (entry.path == vpath and entry.line == file_line)
    sym = _Sym(entry.name, entry)
    if not at_def:
        d = UsageRef(Path(entry.path), entry.line, entry.col,
                     scope=entry.parent or "<module>", module_name="")
        return ps, pe, sym, False, [d]
    uses = roster.usages_of(entry, live={vpath: (full_text, line_offset)}, project=project)
    targets = [UsageRef(Path(u.path), u.line, u.col, scope=u.scope,
                        module_name="") for u in uses]
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"[roster ctrl+b] {entry.qualname} ({entry.kind}) -> {len(targets)} usages "
          f"in {ms:.0f}ms")
    return ps, pe, sym, True, targets


def _local_ctrl_b(full_text, line_offset, own, sc, name, cs, caret_line):
    """Ctrl+B on a function local. Returns (start, end, sym, at_def, targets)
    or None when `name` isn't a local visible from `sc`."""
    from meltygui.code.libcst_conversion import UsageRef
    from pathlib import Path
    # Outermost enclosing def: its scope covers every nested scope.
    outer = sc
    while outer is not None and outer.parent:
        pe = own.by_qualname.get(outer.parent)
        if pe is None or pe.kind != "def":
            break
        outer = pe
    lines = full_text.split("\n")
    bindings = roster.local_bindings(full_text, own, line_offset, lines, [outer])
    lk = roster.local_key(own, sc, name, bindings)
    if lk is None:
        return None
    scope_e = own.by_qualname.get(lk[0])
    if scope_e is None:
        return None
    starts = roster._line_starts(full_text)
    b0 = max(0, scope_e.line - 1 - line_offset)
    b1 = min(len(lines) - 1, scope_e.end - 1 - line_offset)
    lo_idx = starts[b0]
    hi_idx = starts[b1 + 1] - 1 if b1 + 1 < len(starts) else len(full_text)
    occ = []
    for s, e, chain in roster.iter_chains(full_text, lo_idx, hi_idx):
        dot = chain.find(".")
        first = chain if dot == -1 else chain[:dot]
        if first != name:
            continue
        ln = full_text.count("\n", 0, s)      # cheap enough for one scope
        fl = ln + 1 + line_offset
        osc = own.scope_at(fl)
        if roster.local_key(own, osc, name, bindings) != lk:
            continue
        occ.append((s, s + len(first), fl, s - starts[ln]))
    blines = {b.line for b in bindings[lk]}
    sym = _Sym(name, None)
    at_def = caret_line in blines
    if at_def:
        targets = [UsageRef(Path(own.path), fl, col, scope=lk[0], module_name="")
                   for (s, e, fl, col) in occ if not (s <= cs < e)]
    else:
        cands = [b for b in bindings[lk] if b.line <= caret_line] or bindings[lk][:1]
        b = cands[-1]
        targets = [UsageRef(Path(own.path), b.line, b.col, scope=lk[0], module_name="")]
    return cs, cs + len(name), sym, at_def, targets
