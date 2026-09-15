"""Mmapped trigram full-text index — the engine behind global search's Text tab.

Design (the Google-Code-Search / ripgrep-index lineage), so search cost stays
O(results) instead of O(corpus):

  segment — an immutable on-disk index file, mmapped read-only: sorted uint32
            trigram keys + fixed-width uint32 postings of file ids. A query's
            trigrams intersect their posting lists down to a small candidate
            set; only candidates are read and verified.
  overlay — files whose CURRENT text may disagree with the segment (unsaved
            pending edits, external disk changes, brand-new files). Kept as a
            dirty SET, not a text copy: dirty files are linearly scanned at
            query time with pending-aware text. The set stays small between
            rebuilds; growing past _REBUILD_DIRTY triggers a background
            rebuild that folds the overlay back into a fresh segment.

Invalidation is content-free: the per-file signature is
(st_mtime_ns, st_size, pending_gen) — disk edits move the stat pair, in-app
edits bump PendingSave._pending_gen. Signatures are compared lazily on a
short TTL when a search actually runs; nothing walks or stats while search
is idle.

Pending edits are the source of truth: both indexing and match verification
read through PendingSave.current_file_text (disk with every unsaved span
edit spliced in), so hits reflect what the editor shows and hit line numbers
are in PENDING coordinates — the same coordinates the editor buffer uses, so
jumps land where the match is.

Restart-in-place: live state (segment mmap, dirty set, locks) is a plain
dict adopted through sys — shared across re-execs and module hotswaps. A
full process restart just re-mmaps the segment file; only the staleness
sweep re-runs cold.

Scale notes: search scales to multi-GB corpora (posting intersection +
candidate-only reads). The BUILD is the v1 shortcut — one in-memory numpy
sort of all (trigram, file) pairs — fine to roughly a GB of text; beyond
that it should shard into merged sub-segments (the search path would not
change). Postings are fixed uint32 (no varint) — ~4x larger on disk than
delta-varint, but mmap only pages in what queries touch.
"""

import hashlib
import mmap
import os
import pickle
import struct
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np

_MAGIC = b"LSDTGI4\n"       # v2: symbol tables; v3: line tints; v4: raw
                            # ( (stripped) file/sigils; search rows render
                            # through draw_text, whose usage syntaxes map by
                            # file path, so buffers must keep their indent
# magic, n_len, meta_len, n_tri, keys_off, offs_off, post_off
_HDR = struct.Struct("<8s6Q")

_TEXT_EXTS = {
    ".py", ".pyi", ".md", ".txt", ".rst", ".ini", ".cfg", ".toml", ".json",
    ".yaml", ".yml", ".sh", ".glsl", ".frag", ".vert", ".comp", ".c", ".h",
    ".cpp", ".hpp", ".cu", ".cuh", ".js", ".ts", ".css", ".html", ".xml",
}
_SKIP_DIRS = {"__pycache__", ".git", ".hg", ".idea", ".vscode", "node_modules",
              ".venv", "venv", ".mypy_cache", ".pytest_cache"}
_MAX_FILE_BYTES = 8 << 20      # bigger than this → skipped (generated blobs)
_STAT_TTL = 5.0                # seconds between staleness stat sweeps
_WALK_TTL = 30.0               # seconds between new-file directory walks
_REBUILD_DIRTY = 64            # overlay size that triggers a background rebuild
_PER_FILE_CAP = 5              # max content hits reported per file
_CAND_CAP = 4000               # max segment candidates verified per query
_FILE_HIT_CAP = 20             # max FILE-NAME hits per query
_SYMBOL_HIT_CAP = 40           # max symbol-content hits per query


# ── Roots and paths ───────────────────────────────────────────────────────────

def _search_root() -> str:
    """The same src root the Files tab's labels are relative to."""
    from meltygui.code.libcst_conversion import _SRC_PREFIX
    return str(Path(_SRC_PREFIX))


def _segment_file(root: str) -> Path:
    d = Path.home() / ".lsd" / "text_index"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha1(root.encode()).hexdigest()[:16] + ".tgi")


def _under(path: str, root: str) -> bool:
    """`path` is `root` or inside it — a DIRECTORY prefix. A bare
    str.startswith put /a/bc/x.py under root /a/b once sibling projects
    were open side by side."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _resolve_root(root) -> str:
    """The index root as a str: the caller's `root` (a meltygui app's project),
    else the src root. Every index entry point takes an optional root so a
    standalone app searches ITS files, not the framework checkout."""
    return str(root) if root else _search_root()


def warm(root=None):
    """Build (or load) the segment for `root` ahead of the first search, so
    the first keystroke doesn't pay the walk. Call from a background thread."""
    root = _resolve_root(root)
    st = _state(root)
    _ensure_segment(st, root)
    _sweep(st, root)


def symbol_tables(root=None):
    """The class/def tables of every indexed .py file under `root`, pending
    edits included: ``(key, [(rel, symbols)])`` with `symbols` in the
    _extract_symbols shape ``(name, line, indent, kind, tint, end, sig)`` and
    every indexed file listed (non-.py files with an empty table). `key` is
    a cheap identity for memoizing anything derived from the tables — it
    changes when the segment is rebuilt, a file appears / goes stale, or an
    edit is queued. Overlay (dirty / new) files are extracted live and
    shadow their segment entries. Builds the index on first use: call from
    a background thread (the global-search worker does)."""
    root = _resolve_root(root)
    st = _state(root)
    _ensure_segment(st, root)
    _sweep(st, root)
    _maybe_rebuild(st, root)
    seg = st["seg"]
    overlay = sorted(st["dirty"] | st["extra"])
    gens = _pending_gens()
    key = (root, id(seg), tuple(overlay),
           sum(g for p, g in gens.items() if _under(p, root)))
    out = []
    seen = set()
    for ap in overlay:
        rel = os.path.relpath(ap, root)
        seen.add(rel)
        text = _current_text(ap)
        out.append((rel, _extract_symbols(text, rel.endswith(".py")) if text else []))
    if seg is not None:
        for rel, syms in zip(seg.paths, seg.symbols):
            if rel not in seen:
                out.append((rel, syms))
    out.sort(key=lambda t: t[0])
    return key, out


# ── Pending-aware reads (lazy imports: keep this module standalone-testable) ──

def _pending_gens() -> dict:
    """{resolved abs path str: summed pending generation} — tiny (only files
    with queued edits). Resolved here because PendingSave keys by the Address's
    own Path object, which may be spelled differently."""
    try:
        from meltygui.editor.pending_save import PendingSave
    except ImportError:
        return {}
    out = {}
    for p, g in list(PendingSave._pending_gen.items()):
        try:
            out[str(Path(p).resolve())] = out.get(str(Path(p).resolve()), 0) + g
        except OSError:
            continue
    return out


def _current_text(abs_path: str):
    """The file as the app sees it: disk with unsaved span edits spliced in.
    Falls back to a plain disk read outside the app (tests)."""
    try:
        from meltygui.editor.pending_save import PendingSave
        return PendingSave.current_file_text(Path(abs_path))
    except ImportError:
        try:
            return Path(abs_path).read_text(errors="replace")
        except OSError:
            return None


# ── Symbol extraction (names + definition tints) ──────────────────────────────
# Per .py file the index also carries a class/def table: (name, 1-based
# line, indent, kind, tint). Tints come from the editor's own definition-tint
# resolver (_scan_def_tint_lines - style kwarg / # [tint=...] comment /
# class-body var), so a search row is coloured exactly like the definition's
# block wash in the editor - Melty's colour coding is first-class in search:
# symbol hits carry their OWN tint, else hits the tint of the ENCLOSING
# block (indent-stack walk in _enclosing_tint).

import re as _re

_DEF_RE = _re.compile(r"^(\s*)(?:async\s+)?(class|def)\s+([A-Za-z_]\w*)")


def _scan_tint(lines, line_no, name):
    """Explicit tint of the definition at 1-based line_no, or None. Uses the
    editor's resolver in-app; standalone (tests) falls back to None."""
    try:
        from meltygui.editor.text import _scan_def_tint_lines
    except ImportError:
        return None
    try:
        res = _scan_def_tint_lines(lines, line_no, name)
        return tuple(res[0][:3]) if res else None
    except Exception:
        return None


def _extract_symbols(text: str, is_py: bool) -> list:
    """[(name, line, indent, kind, tint, end)] for every class/def in a .py
    buffer, in line order — line/end are a 1-based INCLUSIVE block span (end =
    last line before the dedent, same block the editor's tint wash covers).
    One pass: any code line at indent d closes the open defs at indent >= d.
    Comment lines never close a block (a col-0 comment inside a body is
    common); blank lines are skipped the same way. Non-.py files → []."""
    if not is_py:
        return []
    lines = text.split("\n")
    out, open_ix = [], []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        while open_ix and out[open_ix[-1]][2] >= indent:
            out[open_ix.pop()][5] = i          # 1-based inclusive: line i+1 is
        m = _DEF_RE.match(ln)                  # the first OUTSIDE the block
        if m is not None:
            out.append([m.group(3), i + 1, indent, m.group(2),
                        _scan_tint(lines, i + 1, m.group(3)), len(lines),
                        ln.rstrip()[:160]])  # sig: the entire def line (widt
            open_ix.append(len(out) - 1)     # kept for column-faithful display)
    return [tuple(x) for x in out]


def _parse_comment_tint(comment):
    """Tint tuple from a `# [tint=(...)]` override comment, or None. Uses the
    app's canonical override parser when importable; a literal-eval fallback
    keeps the module standalone (tests)."""
    try:
        from meltygui.code.libcst_conversion import _parse_override_comment
        parsed = _parse_override_comment(comment)
        t = parsed.get("tint") if parsed else None
    except ImportError:
        m = _re.search(r"tint\s*=\s*(\([^)]*\))", comment)
        if m is None:
            return None
        import ast
        try:
            t = ast.literal_eval(m.group(1))
        except (ValueError, SyntaxError):
            return None
    if (isinstance(t, (tuple, list)) and 3 <= len(t) <= 4
            and all(isinstance(c, (int, float)) for c in t)):
        return tuple(t[:3])
    return None


def _extract_line_tints(text: str, is_py: bool) -> list:
    """[(start, end, tint)] 1-based inclusive spans for `# [tint=(...)]`
    override comments — the editor's per-LINE tint wash, which mostly
    annotates assignments (Toggles settings, class attrs), not defs. A
    standalone comment washes itself through the next code line (skipping
    blanks and further comments — decorator runs get the wash too, harmless);
    an inline trailing comment washes its own line. Def-attached comments are
    ALSO resolved by _scan_tint for the symbol table; here they just wash the
    comment/def lines themselves, matching the editor."""
    if not is_py:
        return []
    out = []
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        s = ln.strip()
        if s.startswith("#") and "[" in s:
            tint = _parse_comment_tint(s)
            if tint is not None:
                j = i + 1
                while j < n and (not lines[j].strip()
                                 or lines[j].strip().startswith("#")):
                    j += 1
                out.append((i + 1, j + 1 if j < n else i + 1, tint))
                i = j + 1
                continue
        elif "#" in ln and "[" in ln:
            m = _re.search(r"#.*$", ln)
            tint = _parse_comment_tint(m.group(0)) if m else None
            if tint is not None:
                out.append((i + 1, i + 1, tint))
        i += 1
    return out


def _line_tint(line_tints, hit_line):
    """Tint of the override-comment span containing hit_line, or None.
    Spans are line-ordered; the first containing span wins (they don't
    meaningfully nest)."""
    for s, e, t in line_tints:
        if s > hit_line:
            break
        if hit_line <= e:
            return t
    return None


def _enclosing_tint(symbols, hit_line):
    """Tint of the innermost tinted definition whose block span contains
    `hit_line` — the block wash the hit renders inside in the editor. Later
    matching symbols are deeper (siblings are excluded by their end), so the
    last match wins. None when no enclosing definition declares a tint."""
    tint = None
    for _name, ln, _indent, _kind, t, end, _sig in symbols:
        if ln > hit_line:
            break
        if hit_line <= end and t is not None:
            tint = t
    return tint


# ── Trigram primitives ────────────────────────────────────────────────────────

def _trigrams(lowered: bytes) -> np.ndarray:
    """Sorted unique uint32 trigram keys of a lowercased utf-8 buffer
    (b0<<16 | b1<<8 | b2 — always < 2^24)."""
    a = np.frombuffer(lowered, np.uint8)
    if a.size < 3:
        return np.empty(0, np.uint32)
    k = ((a[:-2].astype(np.uint32) << 16)
         | (a[1:-1].astype(np.uint32) << 8)
         | a[2:])
    return np.unique(k)


def _lower_bytes(text: str) -> bytes:
    # str.lower before encode so case folding is unicode-consistent with the
    # query's str lowering (bytes.lower is ASCII-only).
    return text.lower().encode("utf-8", "replace")


# ── Segment: the immutable mmapped index file ─────────────────────────────────

class _Segment:
    """Read-only view over one index file. Numeric sections are numpy views
    straight into the mmap (nothing copied); only the meta blob (paths, sigs)
    loads into RAM."""

    def __init__(self, path: Path):
        with open(path, "rb") as f:
            self._mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        magic, meta_off, meta_len, n_tri, keys_off, offs_off, post_off = \
            _HDR.unpack_from(self._mm, 0)
        if magic != _MAGIC:
            raise ValueError(f"bad index magic in {path}")
        meta = pickle.loads(self._mm[meta_off:meta_off + meta_len])
        self.root = meta["root"]
        self.paths = meta["paths"]          # rel path strs, id = path index
        self.sigs = meta["sigs"]            # (mtime_ns, size, pending_gen)
        self.symbols = meta["symbols"]      # per-file [(name, line, indent,
                                            #  kind, tint, end, sig)]
        self.line_tints = meta["line_tints"]  # per-file [(start, end, tint)]
        self.keys = np.frombuffer(self._mm, np.uint32, int(n_tri), int(keys_off))
        self.offs = np.frombuffer(self._mm, np.uint64, int(n_tri) + 1, int(offs_off))
        self.post_off = int(post_off)
        self.id_by_path = {p: i for i, p in enumerate(self.paths)}

    def postings(self, key: int):
        """uint32 file ids containing trigram `key`, or None if absent."""
        i = int(np.searchsorted(self.keys, np.uint32(key)))
        if i >= len(self.keys) or int(self.keys[i]) != key:
            return None
        a, b = int(self.offs[i]), int(self.offs[i + 1])
        return np.frombuffer(self._mm, np.uint32, b - a, self.post_off + 4 * a)


def _walk_rel_files(root: str) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in _TEXT_EXTS:
                out.append(os.path.relpath(os.path.join(dirpath, fn), root))
    out.sort()
    return out


def _pad8(f):
    f.write(b"\0" * (-f.tell() % 8))


def _build_segment(root: str, dst: Path):
    """Walk `root`, index every text file (pending text as truth), write the
    segment atomically. Runs on a background thread — pure CPU/IO, no GL."""
    t0 = time.perf_counter()
    pend = _pending_gens()
    rels = _walk_rel_files(root)
    paths, sigs, symbols, line_tints = [], [], [], []
    key_parts, id_parts = [], []
    for rel in rels:
        ap = os.path.join(root, rel)
        try:
            st = os.stat(ap)
        except OSError:
            continue
        if st.st_size > _MAX_FILE_BYTES:
            continue
        rp = ap
        try:
            rp = str(Path(ap).resolve())
        except OSError:
            pass
        gen = pend.get(rp, 0)
        text = _current_text(ap) if gen else None
        if text is None:
            try:
                with open(ap, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            if b"\0" in raw[:4096]:
                continue                    # binary despite the extension
            text = raw.decode("utf-8", "replace")
        fid = len(paths)
        tris = _trigrams(_lower_bytes(text))
        paths.append(rel)
        sigs.append((st.st_mtime_ns, st.st_size, gen))
        symbols.append(_extract_symbols(text, rel.endswith(".py")))
        line_tints.append(_extract_line_tints(text, rel.endswith(".py")))
        if tris.size:
            key_parts.append(tris)
            id_parts.append(np.full(tris.size, fid, np.uint32))

    if key_parts:
        keys = np.concatenate(key_parts)
        ids = np.concatenate(id_parts)
        order = np.lexsort((ids, keys))     # by key, then file id ascending
        keys, post = keys[order], ids[order]
        uniq, first = np.unique(keys, return_index=True)
        offs = np.append(first, len(keys)).astype(np.uint64)
    else:
        uniq = np.empty(0, np.uint32)
        offs = np.zeros(1, np.uint64)
        post = np.empty(0, np.uint32)

    meta = pickle.dumps({"root": root, "paths": paths, "sigs": sigs,
                         "symbols": symbols, "line_tints": line_tints})
    tmp = dst.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(b"\0" * _HDR.size)
        _pad8(f); meta_off = f.tell(); f.write(meta)
        _pad8(f); keys_off = f.tell(); f.write(uniq.tobytes())
        _pad8(f); offs_off = f.tell(); f.write(offs.tobytes())
        _pad8(f); post_off = f.tell(); f.write(post.tobytes())
        f.seek(0)
        f.write(_HDR.pack(_MAGIC, meta_off, len(meta), len(uniq),
                          keys_off, offs_off, post_off))
    os.replace(tmp, dst)
    print(f"text_index: built {len(paths)} files, {len(uniq)} trigrams, "
          f"{post.size} postings in {time.perf_counter() - t0:.2f}s -> {dst}")


# ── Live state (adopted through sys, survives edit-in-place/hotswap) ──────

def _state(root: str) -> dict:
    store = getattr(sys, "_lsd_text_index_state", None)
    if not isinstance(store, dict):
        store = sys._lsd_text_index_state = {}
    st = store.get(root)
    if st is not None:
        # Adopted across a restart-in-place: the segment object was built by an
        # earlier session and references THAT session's _Segment class, which
        # pins its entire module graph. Same source → same layout → re-point it.
        seg = st.get("seg")
        if seg is not None and type(seg) is not _Segment:
            try:
                seg.__class__ = _Segment
            except TypeError:
                st["seg"] = None          # layout changed: rebuild lazily
    if st is None:
        st = store[root] = {
            "seg": None,          # _Segment | None
            "dirty": set(),       # abs paths whose segment entry is stale
            "extra": set(),       # abs paths not in the segment (new files)
            "lock": threading.Lock(),
            "last_stat": 0.0,
            "last_walk": 0.0,
            "building": False,
        }
    return st


def _ensure_segment(st, root):
    with st["lock"]:
        if st["seg"] is not None:
            return
        sp = _segment_file(root)
        if sp.exists():
            try:
                st["seg"] = _Segment(sp)
                return
            except Exception:
                traceback.print_exc()       # corrupt → rebuild below
        _build_segment(root, sp)            # first build: synchronous, but the
        st["seg"] = _Segment(sp)            # caller is already off-thread


def _sweep(st, root):
    """Refresh the dirty/extra overlay from cheap signals. Stat sweep and
    directory walk are TTL-throttled; the pending-gen compare is O(edited
    files) and runs every time. At very large file counts the stat sweep is
    the piece to move to watcher-driven marking — everything else is O(small)."""
    seg = st["seg"]
    now = time.monotonic()
    pend = _pending_gens()
    if seg is not None:
        for rp, gen in pend.items():
            if not _under(rp, root):
                continue
            fid = seg.id_by_path.get(os.path.relpath(rp, root))
            if fid is None:
                st["extra"].add(rp)
            elif seg.sigs[fid][2] != gen:
                st["dirty"].add(rp)
        if now - st["last_stat"] > _STAT_TTL:
            st["last_stat"] = now
            for fid, rel in enumerate(seg.paths):
                ap = os.path.join(root, rel)
                sig = seg.sigs[fid]
                try:
                    s = os.stat(ap)
                    if (s.st_mtime_ns, s.st_size) != (sig[0], sig[1]):
                        st["dirty"].add(ap)
                except OSError:
                    st["dirty"].add(ap)     # deleted: scan yields nothing
        if now - st["last_walk"] > _WALK_TTL:
            st["last_walk"] = now
            known = seg.id_by_path
            for rel in _walk_rel_files(root):
                if rel not in known:
                    st["extra"].add(os.path.join(root, rel))


def _maybe_rebuild(st, root):
    if len(st["dirty"]) + len(st["extra"]) <= _REBUILD_DIRTY or st["building"]:
        return
    st["building"] = True

    def _run():
        try:
            sp = _segment_file(root)
            _build_segment(root, sp)
            with st["lock"]:
                st["seg"] = _Segment(sp)    # old mmap freed when views drop
                st["dirty"].clear()
                st["extra"].clear()
                st["last_stat"] = time.monotonic()
        except Exception:
            traceback.print_exc()
        finally:
            st["building"] = False

    threading.Thread(target=_run, daemon=True, name="text-index-rebuild").start()


# ── Search ─────────────────────────────────────────────────────────────────────

def _candidates(seg, qb: bytes):
    """File ids whose trigram set covers every query trigram (superset of the
    true matches — verification prunes). None postings for any trigram means
    no segment file can contain the query."""
    tris = {qb[i:i + 3] for i in range(len(qb) - 2)}
    lists = []
    for t in tris:
        p = seg.postings((t[0] << 16) | (t[1] << 8) | t[2])
        if p is None:
            return np.empty(0, np.uint32)
        lists.append(p)
    lists.sort(key=len)
    cand = lists[0]
    for p in lists[1:]:
        if not cand.size:
            break
        cand = np.intersect1d(cand, p, assume_unique=True)
    return cand


def _verify(abs_path: str, text: str, ql: str, per_file=_PER_FILE_CAP):
    """(1-based line, RAW line text — indent kept, so renderers can map file
    columns) for each case-insensitive occurrence,
    capped. Offsets are found on the lowered text and snippets cut from the
    original at the same indices; the rare unicode case where lower() changes
    the string's length (e.g. İ) would skew them, so such files fall back to
    lowercased snippets — offsets then index the buffer they came from."""
    tl = text.lower()
    if len(tl) != len(text):
        text = tl
    out = []
    pos = tl.find(ql)
    while pos != -1 and len(out) < per_file:
        line = tl.count("\n", 0, pos) + 1
        ls = text.rfind("\n", 0, pos) + 1
        le = text.find("\n", pos)
        out.append((line, text[ls: le if le != -1 else len(text)].rstrip()))
        nl = tl.find("\n", pos)             # one hit per line reads better
        pos = tl.find(ql, nl + 1) if nl != -1 else -1
    return out


def _hit(kind, root, rel, line, text, tint, scope=None):
    return {"kind": kind, "path": os.path.join(root, rel), "rel": rel,
            "line": line, "text": text, "tint": tint, "scope": scope}


def _enclosing_scope(symbols, hit_line):
    """Qualname chain (tuple of def/class names, outermost first) of the
    definitions whose block span contains `hit_line` — the scope a content
    hit sits in ("Toggles", "TextEditor"); () at module level. Same
    containment walk as _enclosing_tint."""
    chain = []
    for name, ln, indent, _kind, _t, end, _sig in symbols:
        if ln > hit_line:
            break
        if hit_line <= end:
            while chain and chain[-1][1] >= indent:
                chain.pop()
            chain.append((name, indent))
    return tuple(n for n, _i in chain)


def candidate_paths(query: str, exts=(".py",), root=None) -> list:
    """Absolute paths whose CURRENT text may contain `query` (case-
    insensitive): every overlay file (dirty / new — their segment entry is
    stale, so they are always candidates) plus the segment files whose
    trigram postings cover the query. A SUPERSET of the true matches —
    callers verify against the text. `exts` filters by extension. The
    symbol roster's reverse lookup (usages of a definition) is built on
    this: O(results) posting intersection instead of a scan of every file.
    Call from a background thread on first use: it builds the index."""
    ql = query.lower()
    qb = ql.encode("utf-8", "replace")
    root = _resolve_root(root)
    st = _state(root)
    _ensure_segment(st, root)
    _sweep(st, root)
    _maybe_rebuild(st, root)
    seg = st["seg"]
    out = []
    seen = set()
    for ap in sorted(st["dirty"] | st["extra"]):
        if os.path.splitext(ap)[1].lower() in exts and ap not in seen:
            seen.add(ap)
            out.append(ap)
    if seg is not None:
        if len(qb) < 3:
            fids = range(len(seg.paths))      # too short to trigram: everything
        else:
            fids = (int(f) for f in _candidates(seg, qb))
        for fid in fids:
            rel = seg.paths[fid]
            if os.path.splitext(rel)[1].lower() not in exts:
                continue
            ap = os.path.join(root, rel)
            if ap not in seen:
                seen.add(ap)
                out.append(ap)
    return out


def _park_ui():
    """Park this (background) thread while the render thread is mid-frame —
    libcst_conversion._park_while_frame, reached through sys.modules so
    text_index gains no import edge on the conversion stack (and offline
    callers that never loaded it just don't park). Why: search() is a CPU
    chunk (~140 ms warm for a common word — a lower()+scan per candidate
    file), and a CPU-bound background thread GIL-convoys every frame it
    overlaps; parking at file boundaries keeps keystroke frames smooth."""
    m = sys.modules.get("meltygui.code.libcst_conversion")
    if m is not None:
        park = getattr(m, "_park_while_frame", None)
        if park is not None:
            park()


def search(query: str, limit=200, per_file=_PER_FILE_CAP, cancelled=None, root=None):
    """Case-insensitive search over `root` (default: the src root — see
    _search_root; a meltygui app passes its own project roots, one call per
    root), pending edits included.
    Returns hit dicts {kind, path, rel, line, text, tint} in three kinds,
    listed in this order:
      file   — the file's NAME matches (line None, text = basename)
      symbol — a class/def NAME matches (text = the def line, tint = the
               definition's own explicit tint)
      line   — full-text content match (text = the line, tint = the innermost
               enclosing tinted definition's — the editor's block wash,
               scope = the enclosing def/class qualname chain as a tuple)
    Tints are index-resolved and None when no source tint applies (the UI
    falls back to FileMeta / category tints). Overlay (dirty/new) files are
    served live and shadow their stale segment entries. `per_file` caps the
    content hits reported per file (default _PER_FILE_CAP). Call from a
    background thread: the first call builds the index.

    `cancelled` (a nullary callable) makes the scan ABANDONABLE: checked at
    file boundaries, and a True return abandons the search and returns None
    (not a partial list — the caller must land nothing). The global-search
    text pass hands its generation staleness here so a new keystroke stops
    a now-pointless scan within one file instead of finishing ~140 ms of
    dead work that would GIL-convoy the keystroke's frame."""
    ql = query.lower()
    qb = ql.encode("utf-8", "replace")
    if len(qb) < 3:
        return []
    root = _resolve_root(root)
    st = _state(root)
    _ensure_segment(st, root)
    _sweep(st, root)
    _maybe_rebuild(st, root)

    seg = st["seg"]
    overlay = sorted(st["dirty"] | st["extra"])
    overlay_rel = {os.path.relpath(p, root) for p in overlay}
    file_hits, sym_hits, line_hits = [], [], []

    # ── file-name hits: the whole indexed universe + unseen extras ──
    universe = list(seg.paths) if seg is not None else []
    universe += [r for r in (os.path.relpath(p, root) for p in st["extra"])
                 if seg is None or r not in seg.id_by_path]
    for rel in universe:
        if ql in os.path.basename(rel).lower():
            file_hits.append(_hit("file", root, rel, None,
                                  os.path.basename(rel), None))
            if len(file_hits) >= _FILE_HIT_CAP:
                break

    def _sym_match(rel, syms):
        for name, ln, _ind, _kind, tint, _end, sig in syms:
            if ql in name.lower():
                sym_hits.append((name.lower(), _hit("symbol", root, rel, ln,
                                                    sig, tint)))

    # ── overlay files: live symbol table + content scan (freshest wins) ──
    scanned = {}
    for ap in overlay:
        if cancelled is not None and cancelled():
            return None
        _park_ui()
        text = _current_text(ap)
        rel = os.path.relpath(ap, root)
        scanned[ap] = None
        if not text:
            continue
        syms = _extract_symbols(text, rel.endswith(".py"))
        ltints = _extract_line_tints(text, rel.endswith(".py"))
        _sym_match(rel, syms)
        if len(line_hits) < limit:
            for line, snippet in _verify(ap, text, ql, per_file):
                line_hits.append(_hit("line", root, rel, line, snippet,
                                      _line_tint(ltints, line)
                                      or _enclosing_tint(syms, line),
                                      _enclosing_scope(syms, line)))

    # ── segment symbol tables (overlay files shadowed above) ──
    if seg is not None:
        for i, (rel, syms) in enumerate(zip(seg.paths, seg.symbols)):
            if i % 64 == 0:
                if cancelled is not None and cancelled():
                    return None
                _park_ui()
            if len(sym_hits) >= _SYMBOL_HIT_CAP * 10:
                break                       # raw pool; ranked + trimmed below
            if rel not in overlay_rel:
                _sym_match(rel, syms)
    # Name-prefix matches ahead of nameested ones (the scorer's prefix flag),
    # then shorter names - the tightest match tops the list.
    sym_hits.sort(key=lambda t: (not t[0].startswith(ql), len(t[0]), t[0]))
    sym_hits = [h for _n, h in sym_hits[:_SYMBOL_HIT_CAP]]

    # ── segment content candidates ──
    if seg is not None and len(line_hits) < limit:
        for fid in _candidates(seg, qb)[:_CAND_CAP]:
            if len(line_hits) >= limit:
                break
            # Per-file boundary: the lower()+scan below is the search's cost
            # center, so this is where cancellation and frame-parking bite.
            if cancelled is not None and cancelled():
                return None
            _park_ui()
            fid = int(fid)
            ap = os.path.join(root, seg.paths[fid])
            if ap in scanned:
                continue
            text = _current_text(ap)
            if not text:
                continue
            for line, snippet in _verify(ap, text, ql, per_file):
                line_hits.append(_hit("line", root, seg.paths[fid], line,
                                      snippet,
                                      _line_tint(seg.line_tints[fid], line)
                                      or _enclosing_tint(seg.symbols[fid], line),
                                      _enclosing_scope(seg.symbols[fid], line)))
    return (file_hits + sym_hits + line_hits)[:limit]
