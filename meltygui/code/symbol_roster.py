"""Symbol roster — text-derived definitions + metadata, the source of truth for
inline code tints and Ctrl+B, with NO dependence on live objects.

The old pipelines (definition tints in `_collect_def_tints`, the Ctrl+B usage
graph in `libcst_conversion._symbol_refs_index`) resolved symbols against
IMPORTED objects (`vars(module)`, `id(obj)`, `co_firstlineno`), so a def you
had only just typed — or edited without a hotswap — was invisible or stale,
and every line came in DISK coordinates that had to be bridged to the
editor's pending text. The roster instead reads the same text the editor
shows: PendingSave's in-memory file (disk + every unsaved span edit) for
every file, and the LIVE buffer for the file being drawn. Add a tinted class
to one file and the references in another wash on the next frame; nothing
is saved, recompiled or hotswapped.

Three layers, all plain text:

  FileTable   per .py file: every class / def (any nesting, closures included)
              and every module- or class-level assignment, as Entry rows with
              a QUALNAME built from the indent stack, the block extent, and
              the tint its source declares (decorator kwarg / `# [tint=...]`
              comment / class-body `tint = (...)`, via the editor's own
              `_scan_def_tint_lines`), plus the file's import table.
              Cached per file on a content-free key: (identity of the
              FileWatch disk string, pending generation) — or the live
              buffer's identity when the editor hands one in.
  resolve()   textual name resolution of a dotted chain in a file's context:
              the file's own table → its imports (module dotted path mapped
              onto the src tree, relative imports included) → a unique
              same-named definition anywhere in the roster (so a brand-new
              class paints before its import line exists). No scope
              analysis: `self.x` / attribute access on values is None.
  usages_of() the reverse index: trigram candidates (text_index.candidate_paths)
              → code-only identifier scan → each occurrence resolved IN ITS
              OWN FILE'S context and kept only when it resolves back to the
              same entry. Member names (`Foo.bar`) additionally accept
              `<anything>.bar` when `bar` is the only member of that name in
              the roster (flagged `probable`).

`set_tint` is the lens write-through: a tint edit stamps the entry before the
source round-trips, so a drag never waits for re-extraction; the override is
dropped as soon as the re-extracted table agrees (or ages out).

Everything here is pure Python over strings — standalone-testable. Editor
integration lives in `view/core_views/roster_tints.py`.
"""
from __future__ import annotations

import keyword
import os
import re
import sys
import threading
import time
from pathlib import Path

from meltygui.code.source_context import analysis_project

# ── Entry / table shapes ─────────────────────────────────────────────────────

class Entry:
    """One definition. `line`/`end` are 1-based inclusive FILE lines (end =
    last line before the dedent, the block the editor's wash covers); `col`
    is the 0-based column of the NAME token on `line`. `kind` is "class" /
    "def" / "var". `qualname` is the indent-derived dotted path ("Toggles.
    TextEditor.enable_live_view", "draw_text._try_usage_jump")."""
    __slots__ = ("path", "qualname", "name", "kind", "line", "end", "indent",
                 "col", "tint", "parent", "sig")

    def __init__(self, path, qualname, name, kind, line, end, indent, col,
                 tint, parent, sig):
        self.path = path
        self.qualname = qualname
        self.name = name
        self.kind = kind
        self.line = line
        self.end = end
        self.indent = indent
        self.col = col
        self.tint = tint
        self.parent = parent
        self.sig = sig

    @property
    def ident(self):
        return (self.path, self.qualname)

    def __repr__(self):
        return (f"Entry({self.kind} {self.qualname} @ {os.path.basename(self.path)}:"
                f"{self.line}-{self.end} tint={self.tint})")


class FileTable:
    __slots__ = ("path", "key", "entries", "by_qualname", "by_name", "imports",
                 "star_imports", "nlines", "_scope_index")

    def __init__(self, path, key, entries, imports, star_imports, nlines):
        self._scope_index = None
        self.path = path
        self.key = key
        self.entries = tuple(entries)
        self.by_qualname = {}
        self.by_name = {}
        for e in entries:
            self.by_qualname.setdefault(e.qualname, e)
            self.by_name.setdefault(e.name, []).append(e)
        self.imports = imports            # alias -> (module_dotted, attr | None)
        self.star_imports = star_imports  # [module_dotted]
        self.nlines = nlines

    def scope_at(self, line):
        """Innermost class/def Entry whose block contains 1-based `line`, or
        None at module level. Per-line index built lazily (one pass over the
        entries' blocks) so a pass resolving thousands of chains pays O(1)."""
        idx = self._scope_index
        if idx is None:
            idx = self._scope_index = self._build_scope_index()
        if 1 <= line <= len(idx):
            ix = idx[line - 1]
            return self.entries[ix] if ix >= 0 else None
        return None

    def _build_scope_index(self):
        n = self.nlines
        idx = [-1] * n
        for i, e in enumerate(self.entries):
            if e.kind == "var":
                continue
            lo, hi = max(0, e.line - 1), min(n, e.end)
            for k in range(lo, hi):
                idx[k] = i       # later (inner) entries overwrite outer ones
        return idx

    def visible_scopes(self, scope):
        """Qualnames whose members a bare name in `scope` (an Entry or None)
        can see, innermost first — Python's rule: the innermost scope itself,
        then enclosing FUNCTIONS (closures see them); enclosing CLASS bodies
        are skipped (a method can't see its class's names unqualified)."""
        out = []
        first = True
        while scope is not None:
            if first or scope.kind != "class":
                out.append(scope.qualname)
            first = False
            scope = self.by_qualname.get(scope.parent) if scope.parent else None
        return out

    def enclosing_class(self, scope):
        """Qualname of the nearest enclosing class of `scope` (for self/cls)."""
        while scope is not None:
            if scope.kind == "class":
                return scope.qualname
            scope = self.by_qualname.get(scope.parent) if scope.parent else None
        return None


_DEF_RE = re.compile(r"^(\s*)(?:async\s+)?(class|def)\s+([A-Za-z_]\w*)")
_ASSIGN_RE = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)")
_IMPORT_FROM_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.*)$")
_IMPORT_RE = re.compile(r"^\s*import\s+(.*)$")
# Code-only identifier-chain scan: strings and comments are escaped by the
# leading alternatives (and ignored); group 5 is a dotted chain.
_CODE_RE = re.compile(
    r'("""|\'\'\')(?:\\.|(?!\1).)*?\1'
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'"
    r'|#[^\n]*'
    r'|([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)', re.S)   # group 2 = the chain
_KEYWORDS = frozenset(keyword.kwlist) | {"self", "cls", "True", "False", "None"}


def _scan_tint(lines, line_no, name, tint_lines=None, lookback=40):
    """Explicit source tint of the definition/assignment at 1-based line_no
    via the editor's resolver; None standalone (tests) or when untinted.
    `tint_lines` (sorted 0-based indices of lines containing "tint", from
    tint_line_index) is a prefilter: a tint can only come from the line
    itself or the comment/decorator run above it, so a def with no "tint"
    within `lookback` lines above is skipped without the (regex-heavy) scan."""
    if tint_lines is not None:
        import bisect
        i = line_no - 1
        k = bisect.bisect_right(tint_lines, i)
        if k == 0:
            return None
        if lookback is None:
            # Comment scan mode (assignments): the tint could sit anywhere in
            # the `#` run directly above - an 8-line `# [tint=..., cam_zoom=
            # ..., light_pos=(...)]` override is common - so back up over it.
            j = i - 1
            while j >= 0 and j > i - 64 and lines[j].lstrip().startswith("#"):
                j -= 1
            if tint_lines[k - 1] <= j:
                return None
        elif tint_lines[k - 1] < i - lookback:
            return None
    try:
        from meltygui.editor.text_editor import _scan_def_tint_lines
    except ImportError:
        return None
    try:
        res = _scan_def_tint_lines(lines, line_no, name)
        return tuple(res[0][:3]) if res else None
    except Exception:
        return None


_TINT_ASSIGN_RE = re.compile(r"tint\s*=\s*\(")


def tint_line_index(lines):
    """Sorted 0-based indices of lines that could DECLARE a tint — a
    `tint=(` assignment form (decorator kwarg, `# [tint=(...)]` comment,
    class-body `tint = (...)`) — the cheap prefilter for every explicit-tint
    scan over a buffer. Prose merely mentioning "tint" doesn't count."""
    return [i for i, l in enumerate(lines) if "tint" in l and _TINT_ASSIGN_RE.search(l)]


# Identity-keyed scan that pins its table; stored separately from the
# published tables so this optimization adds no slots to live FileTables.
_EXTRACT_SCANS = {}


def extract_table(path, text, key=None, with_tints=True, previous=None):
    """Build a FileTable from `text` in ONE indent-stack pass (same shape as
    text_index._extract_symbols, plus qualnames, assignments and imports).
    Defs nest by indent: a def inside a def is an entry ("outer.inner" — a
    closure the live-object roster never had). Assignments are recorded at
    module level and in class bodies only (locals are the usage graph's
    business), first binding per qualname wins."""
    lines = text.split("\n")
    tl = tint_line_index(lines) if with_tints else None
    entries = []
    open_ix = []            # indices into `entries` of the open class/def stack
    imports, stars = {}, []
    pending_import = None   # a multi-line import statement being collected
    paren_depth = 0
    start = 0
    checkpoints = []
    scan = _EXTRACT_SCANS.get(id(previous)) if with_tints else None
    if scan is not None and scan[0] is previous:
        old_text = scan[1]
        # The caller already found a generation/identity miss. Locate the
        # prefix to resume a scan; this comparison is not cache invalidation.
        prefix = 0
        limit = min(len(old_text), len(text))
        for step in (65536, 4096, 256, 16, 1):
            while prefix + step <= limit and old_text[prefix:prefix + step] == text[prefix:prefix + step]:
                prefix += step
        # Tint lookup can look ahead 40 lines. Start earlier so edits to
        # a class-body tint also refresh the class's preceding checkpoints.
        before = max(0, text.count('\n', 0, prefix) - 64)
        for checkpoint in scan[2]:
            if checkpoint[0] > before:
                break
            checkpoints.append(checkpoint)
        if checkpoints:
            start, count, saved_imports, saved_stars = checkpoints.pop()
            entries = list(previous.entries[:count])
            imports, stars = dict(saved_imports), list(saved_stars)
    for i in range(start, len(lines)):
        ln = lines[i]
        s = ln.strip()
        if pending_import is not None:
            pending_import += " " + s.rstrip("\\").strip()
            paren_depth += s.count("(") - s.count(")")
            if paren_depth <= 0 and not s.endswith("\\"):
                _parse_import(pending_import, path, imports, stars)
                pending_import = None
            continue
        if not s or s.startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        while open_ix and entries[open_ix[-1]].indent >= indent:
            entries[open_ix.pop()].end = i          # 1-based inclusive: i+1 is outside
        if with_tints and indent == 0 and not open_ix:
            checkpoints.append((i, len(entries), dict(imports), tuple(stars)))
        m = _DEF_RE.match(ln) if s.startswith(('def', 'class', 'async')) else None
        if m is not None:
            name = m.group(3)
            parent = entries[open_ix[-1]] if open_ix else None
            qn = f"{parent.qualname}.{name}" if parent is not None else name
            e = Entry(path, qn, name, m.group(2), i + 1, len(lines), indent,
                      m.start(3), _scan_tint(lines, i + 1, name, tl) if with_tints else None,
                      parent.qualname if parent is not None else None,
                      ln.rstrip()[:160])
            entries.append(e)
            open_ix.append(len(entries) - 1)
            continue
        if s.startswith(("import ", "from ")) and (s.startswith("import ")
                                                   or " import" in s):
            paren_depth = s.count("(") - s.count(")")
            if paren_depth > 0 or s.endswith("\\"):
                pending_import = s.rstrip("\\").strip()
            else:
                _parse_import(s, path, imports, stars)
            continue
        # Local assignments never enter the roster. Avoid running the
        # assignment regexp over each expression in a nested function.
        if open_ix and any(entries[ix].kind == "def" for ix in open_ix):
            continue
        m = _ASSIGN_RE.match(ln) if '=' in s else None
        if m is not None:
            # Module level or directly in a class body (no def on the stack).
            parent = entries[open_ix[-1]] if open_ix else None
            name = m.group(2)
            qn = f"{parent.qualname}.{name}" if parent is not None else name
            if any(e.qualname == qn for e in entries[-64:]):
                continue   # re-binding: first binding wins per name (cheap win)
            entries.append(Entry(path, qn, name, "var", i + 1, i + 1, indent,
                                 m.start(2),
                                 _scan_tint(lines, i + 1, None, tl, None) if with_tints else None,
                                 parent.qualname if parent is not None else None,
                                 ln.rstrip()[:160]))
    if pending_import is not None:
        _parse_import(pending_import, path, imports, stars)
    # Entries still open at EOF end on the last non-blank line.
    last = len(lines)
    while last > 1 and not lines[last - 1].strip():
        last -= 1
    for ix in open_ix:
        entries[ix].end = last
    table = FileTable(path, key, entries, imports, stars, len(lines))
    if with_tints:
        if len(_EXTRACT_SCANS) >= 64:
            del _EXTRACT_SCANS[next(iter(_EXTRACT_SCANS))]
        _EXTRACT_SCANS[id(table)] = (table, text, checkpoints)
    return table


def _parse_import(stmt, path, imports, stars):
    """Fill `imports` {alias: (module, attr|None)} from one import statement
    (multi-line already joined). `from X import a as b, c` → b:(X,a), c:(X,c);
    `import a.b.c as z` → z:(a.b.c, None); `import a.b.c` → a:(a, None).
    Relative `from .x import y` resolves against the file's package."""
    stmt = stmt.replace("(", " ").replace(")", " ")
    m = _IMPORT_FROM_RE.match(stmt)
    if m is not None:
        mod = m.group(1)  # Relative imports resolve in the consumer's package context.
        if mod is None:
            return
        names = m.group(2)
        for part in names.split(","):
            part = part.strip()
            if not part:
                continue
            if part == "*":
                stars.append(mod)
                continue
            bits = part.split()
            name = bits[0]
            alias = bits[2] if len(bits) >= 3 and bits[1] == "as" else name
            if re.fullmatch(r"[A-Za-z_]\w*", name) and re.fullmatch(r"[A-Za-z_]\w*", alias):
                imports[alias] = (mod, name)
        return
    m = _IMPORT_RE.match(stmt)
    if m is not None:
        for part in m.group(1).split(","):
            bits = part.split()
            if not bits:
                continue
            mod = bits[0]
            if len(bits) >= 3 and bits[1] == "as":
                imports[bits[2]] = (mod, None)
            else:
                top = mod.split(".", 1)[0]
                imports.setdefault(top, (top, None))


def _absolutize(mod, path, project=None):
    """Resolve a relative import in its package, including src layouts."""
    if not mod.startswith("."):
        return mod
    project = analysis_project(project, path)
    dots = len(mod) - len(mod.lstrip("."))
    parent = Path(path).parent
    # Prefer the deepest import root containing the file. The root/src
    # pair supports both `pkg` and the checkout's explicit `src.pkg` imports.
    roots = [Path(root) for root in project.import_paths
             if parent.is_relative_to(root) and parent != Path(root)]
    if not roots:
        return None
    root = max(roots, key=lambda value: len(value.parts))
    package = list(parent.relative_to(root).parts)
    if dots > len(package):
        return None
    package = package[:len(package) - dots + 1]
    rest = mod.lstrip(".")
    return ".".join(package + ([rest] if rest else [])) or None


def _src_root():
    from meltygui.code.libcst_conversion import _SRC_PREFIX
    return _SRC_PREFIX.rstrip("/")


_mod_path_cache = {}


def module_to_path(dotted, project=None):
    """Resolve a module against this project's ordered source/venv paths."""
    if not dotted:
        return None
    from meltygui.core.module_names import canonical_name
    dotted = canonical_name(dotted)
    project = analysis_project(project)
    key = (project.key, dotted)
    hit = _mod_path_cache.get(key)
    if hit is not None:
        # Misses expire so a newly created module becomes resolvable.
        if hit[0] or time.monotonic() - hit[1] < 2.0:
            return hit[0] or None
    found = None
    parts = dotted.split(".")
    for base in project.import_paths:
        path = Path(base).joinpath(*parts)
        for candidate in (path / "__init__.py", path.with_suffix(".py"), path.with_suffix(".pyi")):
            if candidate.is_file():
                found = str(candidate.resolve())
                break
        if found:
            break
    if len(_mod_path_cache) > 4096:
        _mod_path_cache.clear()
    _mod_path_cache[key] = (found, time.monotonic())
    return found


# ── Table management ──────────────────────────────────────────────────────────────

_STATE_DEFAULTS = {
    "tables": dict,        # str(path) -> FileTable (pending text)
    "texts": dict,         # str(path) -> (key, pending text) - see file_text
    "live": dict,          # str(path) -> (FileTable from an old save, pending key)
    "overrides": dict,     # (path, qualname) -> (tint, stamp_time)
    "gen": int,
    "lock": threading.RLock,
    "universe": lambda: None,   # [str(path)] of every .py under src, or None
    "universe_at": float,
    "by_name": lambda: None,    # name -> [Entry] module-level defs, lazily built
    "by_name_gen": lambda: -1,
    "by_leaf": lambda: None,    # leaf name -> [Entry] members (qualname has '.')
    "loading": bool,
    "frozen": int,
    "pass_names": lambda: None,
    "last_sweep": float,
    "last_full_sweep": float,
    "consumers": dict,     # draw_state -> gen it last drew with (see register_consumer)
    "notify_timer": lambda: None,
    "universe_kicked": bool,
    "disk_gen": int,      # bumps on every disk write / sync-frame move (see World)
}


def _state():
    """Process-wide roster state, adopted through `sys` so it survives
    restart-in-place / module hotswap; keys added later are backfilled."""
    st = getattr(sys, "_lsd_symbol_roster", None)
    if not isinstance(st, dict):
        st = sys._lsd_symbol_roster = {}
    for k, mk in _STATE_DEFAULTS.items():
        if k not in st:
            st[k] = mk()
    return st


def _project_state(project):
    project = analysis_project(project)
    records = _state().setdefault("projects", {})
    key = project.key
    if key not in records:
        records[key] = {"project": project, "gen": 0, "names": None,
                        "names_gen": -1, "universe": None, "universe_at": 0,
                        "loading": False, "complete": False, "paths": set(), "observed": set()}
    return records[key]


def generation(project=None):
    return _state()["gen"] if project is None else _project_state(project)["gen"]


def disk_generation():
    """Generation of the NON-studio worlds (disk / sync frame): bumped by
    FileWatch on every fs event and by ExternalChanges when a sync frame
    advances. Tint caches over a World key on it — the studio generation
    only moves for pending / live tables."""
    return _state()["disk_gen"]


def bump_disk_generation():
    st = _state()
    with st["lock"]:
        st["disk_gen"] += 1


# ── Worlds ───────────────────────────────────────────────────────────────────
# The roster's default tables are the STUDIO's truth: pending text, shadowed
# by a live editor's hold. A read-only view into some OTHER version of a file
# - the merge window's external (disk) and original (sync-frame) panes -
# must resolve its cross-file references against the same version of the
# other file, or `Toggles.Foo` in b.py's disk text paints in the studio's
# view while it carries another. A World is that consistent set: tables
# built from a text source, DETACHED (never installed as pending or live,
# never bumping the studio generation), memoized on the source text's
# identity (a disk write changes the code_cache string; a new baseline is
# a new object). Resolution's last-resort name index stays the studio's.

class World:
    __slots__ = ("name", "text_of", "_tables")

    def __init__(self, name, text_of):
        self.name = name
        self.text_of = text_of          # resolved path str -> text or None
        self._tables = {}               # path -> (text, FileTable)

    def table(self, path):
        """The FileTable of `path` in this world; the studio's table when
        the world has no text for it (unreadable / never read)."""
        p = _norm(path)
        text = self.text_of(p)
        if not isinstance(text, str):
            return table_for(p)
        held = self._tables.get(p)
        if held is not None and held[0] is text:
            return held[1]
        tbl = extract_table(p, text, (self.name, id(text)))
        if len(self._tables) > 256:
            self._tables.clear()
        self._tables[p] = (text, tbl)
        return tbl

    def generation(self):
        return disk_generation()


_detached = {}   # path -> (text, FileTable) - see detached_table


def detached_table(path, text):
    """A FileTable for `text` as the content of `path`, built OUTSIDE the
    roster's caches: not installed as the file's pending table, not held as
    its live override, no generation bump. For a read-only pane whose text
    is neither the studio's pending truth nor a live editor buffer (the
    merge window's staged result) — it resolves against the studio's other
    files while its own blocks / scopes come from what it shows. Memoized
    on the text's identity (the text is held, so the id can't recycle)."""
    p = _norm(path)
    held = _detached.get(p)
    if held is not None and held[0] is text:
        return held[1]
    tbl = extract_table(p, text, ("detached", id(text)))
    if len(_detached) > 64:
        _detached.clear()
    _detached[p] = (text, tbl)
    return tbl


# ── Consumer notification ───────────────────────────────────────────────────
# Editors are cached tiles: their body (where _def_tints runs) only re-runs
# when invalidated, so a roster change made elsewhere - a tint change in
# another file, the background universe load finishing, a lens write - must
# invalidate the editors that drew roster tints that once per pass;
# fan-out is coalesced (one timer, ~60ms) so the first universe load (one
# gen bump per file) doesn't invalidate every editor per table. Same
# mechanics as RenderHost._notify_consumers_now (safe off-thread).

def register_consumer(draw_state, project=None, path=None):
    """Mark `draw_state` as having drawn roster-derived content this pass."""
    if draw_state is None:
        return
    st = _state()
    if project is None:
        st["consumers"][draw_state] = st["gen"]
    else:
        project = analysis_project(project)
        record = _project_state(project)
        if path is not None:
            record["observed"].add(str(path))
        st["consumers"][draw_state] = (project.key, record["gen"])
    if len(st["consumers"]) > 128:
        st["consumers"] = {ds: g for ds, g in st["consumers"].items()
                           if not getattr(ds, "closed", False)}


def _schedule_notify():
    st = _state()
    with st["lock"]:
        t = st.get("notify_timer")
        if t is not None:
            return                      # already pending: coalesce
        t = threading.Timer(0.06, _notify_consumers)
        t.daemon = True
        st["notify_timer"] = t
        t.start()


def _notify_consumers():
    st = _state()
    with st["lock"]:
        st["notify_timer"] = None
        targets = [(ds, g) for ds, g in list(st["consumers"].items())
                   if (g[1] < st.get("projects", {}).get(g[0], {}).get("gen", g[1])
                       if isinstance(g, tuple) else g < st["gen"])]
    if not targets:
        return
    try:
        from meltygui.core.melty import Melty
        from meltygui.core.windowing.glfw_utils import request_render
        from meltygui.core.cache.invalidation_tracker import Note
    except Exception:
        return
    for ds, _g in targets:
        tid = getattr(ds, "_tile_id", None)
        if tid is None or getattr(ds, "closed", False):
            continue
        try:
            Melty.cache.invalidate_up(tid, force=True, max_depth=8,
                                      note=Note(name="symbol roster changed",
                                                tint=(0.9, 0.7, 0.3), draw_state=ds))
        except Exception:
            pass
    try:
        request_render()
    except Exception:
        pass


def _gen_bump(st, path=None):
    """Bump the generation (caller holds the lock) and wake the consumers."""
    st["gen"] += 1
    st["by_name_gen"] = -1
    for record in list(st.get("projects", {}).values()):
        if path is None or record["project"].resolves(path) or path in record["observed"]:
            record["gen"] += 1
    if st["consumers"]:
        _schedule_notify()


class pass_scope:
    """Freeze per-file key checks + name indexes for the duration of one
    consumer pass (a tint collect, a Ctrl+B lookup): inside the scope a table
    already in the cache is served without re-deriving its key (Melty.read_code
    + pending gen + a path resolve per call — ~50µs, but a pass resolves
    thousands of chains), and the roster-wide name indexes rebuild at most
    once even as newly touched files land. Re-entrant."""
    def __enter__(self):
        st = _state()
        st["frozen"] = st.get("frozen", 0) + 1
        if st["frozen"] == 1:
            st["pass_names"] = None
            st["pass_project_names"] = {}
        return self

    def __exit__(self, *exc):
        st = _state()
        st["frozen"] -= 1
        if st["frozen"] == 0:
            st["pass_names"] = None
            st["pass_project_names"] = {}
        return False


def _bump():
    st = _state()
    with st["lock"]:
        _gen_bump(st)


def _file_key(path):
    """Content-free identity of a file's CURRENT text: (identity of the
    FileWatch-cached disk string, pending generation). Never hashes."""
    try:
        from meltygui.core.melty import Melty
        disk = Melty.read_code(path)
        did = id(disk) if disk is not None else None
    except Exception:
        did = None
    try:
        from meltygui.editor.text_editor import _pending_gen_of
        gen = _pending_gen_of(path)
    except Exception:
        gen = 0
    if did is None:
        try:
            st = os.stat(path)
            did = (st.st_mtime_ns, st.st_size)
        except OSError:
            did = 0
    return (did, gen)


def _current_text(path):
    try:
        from meltygui.editor.pending_save import PendingSave
        t = PendingSave.current_file_text(Path(path))
        if t is not None:
            return t
    except Exception:
        pass
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def file_text(path):
    """The CURRENT text of `path` (pending truth), cached on the same
    content-free key as its table so repeated readers share one string
    (identity-stable — memo keys can use id())."""
    st = _state()
    p = _norm(path)
    key = _file_key(p)
    ent = st["texts"].get(p)
    if ent is not None and ent[0] == key:
        return ent[1]
    text = _current_text(p) or ""
    if len(st["texts"]) > 256:
        st["texts"].clear()
    st["texts"][p] = (key, text)
    return text


def table_for(path, live_text=None, line_offset=0):
    """The FileTable for `path` (resolved str or Path).

    Without `live_text`: the PENDING table (PendingSave.current_file_text),
    rebuilt when its content-free key moved — unless a fresher LIVE table
    for the file is held (see below), which then wins.

    `live_text` is an editor buffer for this file — the truth AHEAD of
    pending while typing / dragging: with line_offset 0 it IS the file; a
    span view (offset > 0) is spliced over the pending table (its entries
    replace the pending ones inside the span's range, shifted to file
    coordinates). The result is held as the file's live override until the
    pending key moves (the edits landed, or another writer touched the
    file), so every consumer — the other editors' tint passes, Ctrl+B from
    another file — resolves against what this editor shows."""
    st = _state()
    p = _norm(path)
    if live_text is not None:
        # Several live buffers of ONE file can be open at once - the whole
        # file in the editor plus a def→call span per stack-trace pane, or
        # four panes of meltygui.py in one trace. Each is a PART
        # (st["live_parts"][p]: part key → part) and the file's live table
        # is their MERGE; the table used to be the single last-keyed
        # buffer, so panes of one file replaced each other's spans every
        # frame and bumped the generation each time (`_tables_differ`:
        # different spans, different entries) - every editor's def-tint
        # key missed every frame (2–4 ms each, 09-01).
        lk = ("live", id(live_text), line_offset)
        held = st["live"].get(p)
        parts = st.setdefault("live_parts", {}).setdefault(p, {})
        if (held is not None and lk in parts
                and held[0].key == ("live", frozenset(parts))):
            return held[0]
        if line_offset == 0:
            previous = next((part[2] for part in parts.values() if part[0] == 0), None)
            whole = extract_table(p, live_text, lk, previous=previous)
            part = (0, whole.nlines, whole, live_text)
            # One whole whole buffer at a time (a single editor buffer).
            for k in [k for k in parts if k[2] == 0]:
                parts.pop(k, None)
        else:
            n = live_text.count("\n") + 1
            lo, hi = line_offset + 1, line_offset + n
            span_tbl = extract_table(p, live_text, ("span", id(live_text)))
            for e in span_tbl.entries:
                e.line += line_offset
                e.end += line_offset
            part = (lo, hi, span_tbl, live_text)   # the text pins the id
            # A re-keyed buffer at the same offset is the same pane after
            # an edit: its previous buffer is stale.
            for k in [k for k in parts if k[2] == line_offset]:
                parts.pop(k, None)
        parts[lk] = part
        whole_part = next((pt for pt in parts.values() if pt[0] == 0), None)
        if whole_part is not None:
            base = whole_part[2]
            pkey = held[1] if held is not None else _file_key(p)
        else:
            base = _pending_table(st, p)
            pkey = base.key
        spans = [pt for pt in parts.values() if pt[0] != 0]
        if spans:
            merged = [e for e in base.entries
                      if not any(lo <= e.line <= hi for lo, hi, _t, _x in spans)]
            imports = dict(base.imports)
            nlines = base.nlines
            for lo, hi, span_tbl, _x in spans:
                merged.extend(span_tbl.entries)
                imports.update(span_tbl.imports)
                nlines = max(nlines, hi)
            merged.sort(key=lambda e: e.line)
            tbl = FileTable(p, ("live", frozenset(parts)), merged, imports,
                            base.star_imports, nlines)
        else:
            tbl = FileTable(p, ("live", frozenset(parts)), base.entries,
                            base.imports, base.star_imports, base.nlines)
        prev = held[0] if held is not None else st["tables"].get(p)
        with st["lock"]:
            st["live"][p] = (tbl, pkey)
            _apply_overrides(st, p, tbl)
            if prev is None or _tables_differ(prev, tbl):
                _gen_bump(st, p)
        return tbl
    ent = _pending_table(st, p)
    held = st["live"].get(p)
    if held is not None:
        if held[1] == ent.key:
            return held[0]          # live still ahead of (or equal to) pending
        with st["lock"]:
            st["live"].pop(p, None)  # pending moved on: the live hold is stale
            st.get("live_parts", {}).pop(p, None)
    return ent


def _pending_table(st, p):
    ent = st["tables"].get(p)
    if ent is not None and st.get("frozen"):
        return ent                      # inside a pass: no key re-derivation
    key = _file_key(p)
    if ent is None or ent.key != key:
        text = _current_text(p)
        tbl = extract_table(p, text or "", key, previous=ent)
        _install(st, p, tbl)
        ent = tbl
    return ent


def _install(st, p, tbl):
    with st["lock"]:
        prev = st["tables"].get(p)
        st["tables"][p] = tbl
        _apply_overrides(st, p, tbl)
        if prev is None or _tables_differ(prev, tbl):
            _gen_bump(st, p)


def _apply_overrides(st, p, tbl):
    """Drop lens overrides the fresh text now agrees with (or that aged
    out); re-stamp the rest onto the new table."""
    now = time.monotonic()
    for (op, oq), (ot, stamp) in list(st["overrides"].items()):
        if op != p:
            continue
        e = tbl.by_qualname.get(oq)
        if (e is not None and e.tint == ot) or now - stamp > 3.0:
            del st["overrides"][(op, oq)]
        elif e is not None:
            e.tint = ot


def sweep(force=False, project=None):
    """Notice edits in OTHER files cheaply: re-key every file with queued
    pending edits (O(edited files), every call ≥100ms apart) and every cached
    table every 2s (catches external disk changes through FileWatch's
    replaced string). Changed files re-extract and bump the generation, which
    is what re-keys the editors' tint caches. Call outside a pass."""
    st = _state()
    now = time.monotonic()
    if project is not None:
        ensure_universe(blocking=False, project=project)
    if project is None and not st["universe_kicked"]:
        # First consumer: load every src file's table in the background so the
        # by-name fallback sees the whole tree; consumers are notified as
        # tables land (coalesced), so tints fill in without a full pass.
        st["universe_kicked"] = True
        try:
            ensure_universe(blocking=False)
        except Exception:
            pass
    dirty = st.setdefault("dirty_paths", set())
    if not force and not dirty and now - st.get("last_sweep", 0.0) < 0.1:
        return
    st["last_sweep"] = now
    full = force or now - st.get("last_full_sweep", 0.0) > 2.0
    if full:
        st["last_full_sweep"] = now
    paths = set(dirty)
    dirty.difference_update(paths)
    try:
        from meltygui.editor.pending_save import PendingSave
        for pp in list(PendingSave._pending_gen):
            paths.add(_norm(pp))
    except Exception:
        pass
    if full:
        paths.update(st["tables"])
    for p in paths:
        ent = st["tables"].get(p)
        if ent is None:
            continue
        if ent.key != _file_key(p):
            _pending_table(st, p)       # re-extracts + bumps gen if it matters
            held = st["live"].get(p)
            if held is not None and held[1] != st["tables"][p].key:
                with st["lock"]:
                    st["live"].pop(p, None)
                    st.get("live_parts", {}).pop(p, None)


def _tables_differ(a, b):
    """Would cross-file consumers see a difference? Names, kinds, tints and
    imports — NOT lines: a keystroke inside a body shifts every entry below
    it, and re-keying every other editor's tint cache for that is churn (an
    editor's own buffer is in its own key)."""
    if len(a.entries) != len(b.entries):
        return True
    for x, y in zip(a.entries, b.entries):
        if x.qualname != y.qualname or x.tint != y.tint or x.kind != y.kind:
            return True
    return a.imports != b.imports


def _norm(path):
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError):
        return str(path)


def set_tint(path, qualname, tint):
    """Lens write-through: make `qualname` in `path` read `tint` NOW (before
    the source round-trips through PendingSave). Dropped once the
    re-extracted table agrees, or after a few seconds."""
    st = _state()
    p = _norm(path)
    t = tuple(tint[:3]) if tint is not None else None
    with st["lock"]:
        st["overrides"][(p, qualname)] = (t, time.monotonic())
        held = st["live"].get(p)
        for tbl in (st["tables"].get(p), held[0] if held is not None else None):
            if tbl is not None:
                e = tbl.by_qualname.get(qualname)
                if e is not None:
                    e.tint = t
        _gen_bump(st, p)


# ── Universe (every project .py) ───────────────────────────────────────────

_UNIVERSE_TTL = 30.0


def universe_paths(project=None):
    """Every project .py, excluding environments and generated directories."""
    project = analysis_project(project)
    record = _project_state(project)
    now = time.monotonic()
    if record["universe"] is not None and now - record["universe_at"] < _UNIVERSE_TTL:
        return record["universe"]
    from meltygui.text_index import _walk_rel_files
    paths = [_norm(os.path.join(project.root, relative))
             for relative in _walk_rel_files(project.root) if relative.endswith(".py")]
    paths = [path for path in paths if project.owns(path)]
    record["universe"], record["universe_at"] = paths, now
    record["paths"] = set(paths)
    return paths


def ensure_universe(blocking=False, project=None):
    """Warm this project's name index, off the render thread by default."""
    project = analysis_project(project)
    record = _project_state(project)
    state = _state()
    if not blocking and record["loading"]:
        return False

    def load():
        try:
            _watch_project(project)
            for path in universe_paths(project):
                if path not in state["tables"]:
                    table_for(path)
                    if not blocking:
                        time.sleep(0.0005)
            record["complete"] = True
        finally:
            record["loading"] = False
    if blocking:
        load()
        return True
    # A cached universe is enough; the walk itself happens on the worker.
    if (record["universe"] is not None
            and time.monotonic() - record["universe_at"] < _UNIVERSE_TTL
            and record["complete"]):
        return True
    record["loading"] = True
    threading.Thread(target=load, daemon=True, name="project-symbol-roster").start()
    return False


def _name_indexes(project=None):
    """Project-local fallback names; unrelated repos cannot introduce ambiguity."""
    project = analysis_project(project)
    state = _state()
    record = _project_state(project)
    pass_names = state.setdefault("pass_project_names", {})
    if state.get("frozen") and project.key in pass_names:
        return pass_names[project.key]
    if record["names"] is None or record["names_gen"] != record["gen"]:
        by_name, by_leaf = {}, {}
        effective = dict(state["tables"])
        effective.update((path, held[0]) for path, held in list(state["live"].items()))
        for path, table in effective.items():
            if not project.owns(path):
                continue
            for entry in table.entries:
                index = by_leaf if "." in entry.qualname else by_name
                index.setdefault(entry.name, []).append(entry)
        record["names"] = (by_name, by_leaf)
        record["names_gen"] = record["gen"]
    if state.get("frozen"):
        pass_names[project.key] = record["names"]
    return record["names"]


# ── Resolution ───────────────────────────────────────────────────────────────

def _walk_qualname(tbl, parts):
    """Longest prefix of `parts` that is a qualname in `tbl` → (entry, n)."""
    best, qn = None, None
    for k, p in enumerate(parts):
        qn = p if qn is None else f"{qn}.{p}"
        e = tbl.by_qualname.get(qn)
        if e is None:
            break
        best = (e, k + 1)
    return best


def resolve(path, chain, table=None, allow_fallback=True, scope=None,
            world=None, project=None):
    """The Entry a dotted `chain` (str or list of parts) denotes in the
    context of file `path`, or None. Returns the entry for the WHOLE chain
    only (use `resolve_prefixes` for per-prefix). Order: enclosing scopes
    (`scope` = the innermost Entry the reference sits in; closures see their
    enclosing functions, class bodies only themselves) → own-file module
    level → import table (module path mapped textually, greedy over
    submodules) → unique same-named module-level definition anywhere in the
    roster. `self.x` / `cls.x` inside a class resolve to that class's member."""
    n = len(chain.split(".")) if isinstance(chain, str) else len(chain)
    r = resolve_prefixes(path, chain, table, allow_fallback, scope, world=world, project=project)
    return r[-1][0] if r and r[-1][1] == n else None


def resolve_prefixes(path, chain, table=None, allow_fallback=True, scope=None,
                     world=None, project=None):
    """[(entry, n_parts)] for every prefix of `chain` that resolves, shortest
    first (`Toggles`, `Toggles.TextEditor`, `Toggles.TextEditor.x`). The
    prefixes beyond the first resolved one walk qualnames inside the first
    hit's table. `scope`: the innermost Entry the reference sits in (None =
    module level) — see `resolve`. `world`: the World whose tables the
    OTHER files resolve through (imports, star imports); None = the
    studio's (pending + live holds). The last-resort unique-name fallback
    uses the owning project’s pending/live index."""
    project = analysis_project(project, path)
    parts = chain.split(".") if isinstance(chain, str) else list(chain)
    if not parts:
        return []
    tables = world.table if world is not None else table_for
    if table is not None:
        tbl = table                      # caller's table: no path resolve / call
    else:
        tbl = tables(_norm(path)) if path is not None else None
    first = parts[0]
    # 0. self / cls inside a class: the enclosing class's members.
    if first in ("self", "cls") and tbl is not None and scope is not None and len(parts) > 1:
        cq = tbl.enclosing_class(scope)
        if cq is not None:
            w = _walk_qualname(tbl, cq.split(".") + parts[1:])
            if w is not None and w[1] > cq.count(".") + 1:
                skip = cq.count(".") + 1       # the class's prefix parts
                return [(e, n - skip + 1) for e, n in _expand(w, tbl, cq.split(".") + parts[1:])
                        if n > skip]
        return []
    if first in _KEYWORDS:
        return []
    # 1. enclosing scopes, innermost first (closure / class-body visibility)
    if tbl is not None and scope is not None:
        for sq in tbl.visible_scopes(scope):
            qn = f"{sq}.{first}"
            if qn in tbl.by_qualname:
                sparts = sq.split(".")
                w = _walk_qualname(tbl, sparts + parts)
                skip = len(sparts)
                return [(e, n - skip) for e, n in _expand(w, tbl, sparts + parts)
                        if n > skip]
    # 2. own file's module level
    if tbl is not None and first in tbl.by_qualname:
        return _expand(_walk_qualname(tbl, parts), tbl, parts)
    # 3. imports
    if tbl is not None and first in tbl.imports:
        mod, attr = tbl.imports[first]
        mod = _absolutize(mod, tbl.path, project)
        if mod is None:
            return []
        if attr is None:
            # `import a.b.c [as z]`: greedy longest module prefix that is a file.
            best = None
            for k in range(len(parts), 0, -1):
                dotted = mod if k == 1 else mod + "." + ".".join(parts[1:k])
                mp = module_to_path(dotted, project)
                if mp is not None:
                    best = (mp, k)
                    break
            if best is not None:
                mp, k = best
                t2 = tables(mp)
                if k == len(parts):
                    return []           # the chain IS a path, not a symbol
                w = _walk_qualname(t2, parts[k:])
                if w is not None:
                    return [(e, n + k) for e, n in _expand(w, t2, parts[k:])]
            return []
        mp = module_to_path(mod, project)
        if mp is not None:
            t2 = tables(mp)
            if attr in t2.by_qualname:
                w = _walk_qualname(t2, [attr] + parts[1:])
                return [(e, n) for e, n in _expand(w, t2, [attr] + parts[1:])]
        # `from pkg import submodule`
        mp2 = module_to_path(f"{mod}.{attr}", project)
        if mp2 is not None and len(parts) > 1:
            t2 = tables(mp2)
            w = _walk_qualname(t2, parts[1:])
            if w is not None:
                return [(e, n + 1) for e, n in _expand(w, t2, parts[1:])]
        return []
    if tbl is not None and tbl.star_imports:
        for mod in tbl.star_imports:
            mod = _absolutize(mod, tbl.path, project)
            mp = module_to_path(mod, project)
            if mp is None:
                continue
            t2 = tables(mp)
            if first in t2.by_qualname:
                return _expand(_walk_qualname(t2, parts), t2, parts)
    # 4. unique definition anywhere
    if allow_fallback:
        by_name, _leaf = _name_indexes(project)
        cands = by_name.get(first)
        if cands and len(cands) == 1:
            st = _state()
            held = st["live"].get(cands[0].path)
            t2 = held[0] if held is not None else st["tables"].get(cands[0].path)
            if t2 is not None:
                return _expand(_walk_qualname(t2, parts), t2, parts)
    return []


def _expand(walked, tbl, parts):
    """Turn the longest-prefix walk into the per-prefix list."""
    if walked is None:
        return []
    out, qn = [], None
    for k in range(walked[1]):
        qn = parts[k] if qn is None else f"{qn}.{parts[k]}"
        e = tbl.by_qualname.get(qn)
        if e is not None:
            out.append((e, k + 1))
    return out


def tint_of(path, chain, table=None):
    e = resolve(path, chain, table)
    return e.tint if e is not None else None


# ── Occurrence scan + reverse lookup ─────────────────────────────────────────

def iter_chains(text, start=0, end=None):
    """Yield (start, end, chain) for every identifier chain in CODE — strings
    and comments skipped — in one regex pass (~8ms per 13k lines; pass a
    [start, end) slice to scan a window — start it on a string-clean line)."""
    if end is None:
        end = len(text)
    for m in _CODE_RE.finditer(text, start, end):
        if m.group(2) is not None:
            yield m.start(2), m.end(2), m.group(2)


class Usage:
    __slots__ = ("path", "line", "col", "scope", "probable")

    def __init__(self, path, line, col, scope, probable=False):
        self.path = path
        self.line = line
        self.col = col
        self.scope = scope
        self.probable = probable

    def __repr__(self):
        return f"Usage({os.path.basename(self.path)}:{self.line}:{self.col} in {self.scope}{' ?' if self.probable else ''})"


def usages_of(entry, live=None, max_files=400, include_probable=True, project=None):
    """Every code occurrence in the owning project that resolves to `entry`
    (excluding its own definition line). `live` = {path: (text, line_offset)}
    of editor buffers to read instead of pending text. Runs the trigram
    candidate query (builds the text index on first call — call off the
    render thread the first time)."""
    with pass_scope():
        return _usages_of(entry, live, max_files, include_probable, project)


def _usages_of(entry, live, max_files, include_probable, project=None):
    project = analysis_project(project, entry.path)
    name = entry.name
    try:
        from meltygui.text_index import candidate_paths
        cands = candidate_paths(name, root=project.root)
    except Exception:
        cands = list(universe_paths(project))
    cands = [_norm(c) for c in cands if project.owns(str(c))]
    # New/live files may not yet be in the disk index.
    cands.extend(path for path in list(_state()["live"])
                 if project.owns(path) and path not in cands)
    if entry.path not in cands:
        cands.insert(0, entry.path)
    if live:
        for lp in live:
            lp = _norm(lp)
            if project.owns(lp) and lp not in cands:
                cands.insert(0, lp)
    _by_name, by_leaf = _name_indexes(project)
    is_member = "." in entry.qualname
    leaf_unique = is_member and len(by_leaf.get(name, ())) == 1
    word = re.compile(r"(?<![\w.])" + re.escape(name) + r"(?!\w)")
    out = []
    for ap in cands[:max_files]:
        lt = (live or {}).get(ap)
        text, off = None, 0
        if lt is not None:
            ltext, loff = lt
            tbl = table_for(ap, live_text=ltext, line_offset=loff)
            if not loff:
                text = ltext        # whole-file buffer: scan it directly
            # (a span buffer is scanned via the pending whole file text)
        else:
            tbl = table_for(ap)
        if text is None:
            parts = _state().get("live_parts", {}).get(ap, {})
            whole = next((part for part in list(parts.values()) if part[0] == 0), None)
            text = whole[3] if whole is not None else _current_text(ap)
        if not text or name not in text:
            continue
        if not word.search(text) and ("." + name) not in text:
            continue
        starts = None
        memo = {}
        for s, e, chain in iter_chains(text):
            if name not in chain:
                continue
            parts = chain.split(".")
            try:
                k = parts.index(name)
            except ValueError:
                continue
            prefix = ".".join(parts[:k + 1])
            if starts is None:
                starts = _line_starts(text)
            import bisect
            ln = bisect.bisect_right(starts, s) - 1
            file_line = ln + 1 + off
            sc = tbl.scope_at(file_line)
            mkey = (prefix, sc.qualname if sc is not None else None)
            got = memo.get(mkey)
            if got is None:
                res = resolve_prefixes(ap, parts[:k + 1], tbl, scope=sc, project=project)
                hit = None
                for ent, n in res:
                    if n == k + 1:
                        hit = ent
                probable = False
                if hit is None and k > 0 and leaf_unique:
                    hit, probable = entry, True
                got = memo[mkey] = (hit, probable)
            hit, probable = got
            if hit is None or hit.ident != entry.ident:
                continue
            if probable and not include_probable:
                continue
            col = s - starts[ln] + (len(prefix) - len(name))
            if file_line == entry.line and ap == entry.path:
                continue     # skip definition itself
            out.append(Usage(ap, file_line, col, sc.qualname if sc else "<module>",
                             probable))
    return out


def _line_starts(text):
    starts = [0]
    pos = text.find("\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = text.find("\n", pos + 1)
    return starts


def chain_at(text, pos):
    """(start, end, chain, part_index) of the identifier chain under buffer
    index `pos`, or None — the caret's symbol for Ctrl+B."""
    if pos < 0 or pos > len(text):
        return None
    ls = text.rfind("\n", 0, pos) + 1
    le = text.find("\n", pos)
    if le == -1:
        le = len(text)
    line = text[ls:le]
    rel = pos - ls
    for m in re.finditer(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", line):
        if m.start() <= rel <= m.end():
            chain = m.group(0)
            # which part holds the caret
            off = m.start()
            for i, part in enumerate(chain.split(".")):
                if off <= rel <= off + len(part):
                    return ls + m.start(), ls + m.end(), chain, i
                off += len(part) + 1
            return ls + m.start(), ls + m.end(), chain, len(chain.split(".")) - 1
    return None


# ── Function-local bindings (non scopes) ───────────────────────────────────────
# Locals are first-classic table entries, but scoped: a binding appears in the
# innermost DEF whose block holds it and is visible from that def and its
# nested functions (closures), never from an enclosing class body or the
# module. One per line - params (paren-balanced signature), assignment /
# annotated-assignment targets incl. tuple targets, for/comprehension
# targets, `with ... as`, `except ... as`, walrus - honouring global/nonlocal.

class LocalBinding:
    __slots__ = ("scope", "name", "line", "col", "kind")

    def __init__(self, scope, name, line, col, kind):
        self.scope = scope      # qualname of the def that owns the local
        self.name = name
        self.line = line        # 1-based file line of the binding token
        self.col = col          # 0-based column of the name on that line
        self.kind = kind        # "param" / "assign" / "for" / "with" / "except" / "walrus"

    def __repr__(self):
        return f"LocalBinding({self.scope}:{self.name} @{self.line}:{self.col} {self.kind})"


_L_ASSIGN_RE = re.compile(
    r"^\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*(?::[^=\n]+)?=(?!=)")
_L_FOR_RE = re.compile(r"\bfor\s+([A-Za-z_][\w\s,()]*?)\s+in\b")
_L_AS_RE = re.compile(r"\bas\s+([A-Za-z_]\w*)")
_L_WALRUS_RE = re.compile(r"\b([A-Za-z_]\w*)\s*:=")
_L_GLOBAL_RE = re.compile(r"^\s*(?:global|nonlocal)\s+(.+)$")
_L_NAME_RE = re.compile(r"[A-Za-z_]\w*")
_L_CODE_SPLIT_RE = re.compile(r'#.*$')


def _string_end(ln, j):
    """(end, open_quote) for the string literal opening at ln[j] (a quote
    char): `end` is the index just past it, honouring backslash escapes;
    `open_quote` is None when it closes on this line, else the quote the
    literal continues with on the next line (a triple, or a plain quote
    left unterminated mid-edit)."""
    q = ln[j]
    if ln.startswith(q * 3, j):
        e = ln.find(q * 3, j + 3)
        return (len(ln), q * 3) if e == -1 else (e + 3, None)
    k = j + 1
    n = len(ln)
    while k < n:
        c = ln[k]
        if c == "\\":
            k += 2
            continue
        if c == q:
            return k + 1, None
        k += 1
    return n, q


def _code_part(line):
    """The line without a trailing comment — string-aware, so a '#' inside
    a literal (`prompt="# [tint=...]"`) doesn't truncate the code."""
    j, n = 0, len(line)
    while j < n:
        c = line[j]
        if c == "#":
            return line[:j]
        if c in "\"'":
            j, _open = _string_end(line, j)
            continue
        j += 1
    return line


def _signature_params(lines, i):
    """([(name, line_idx, col)], last_line_idx) — the parameters of the def
    starting at 0-based line `i`, walking a paren-balanced, possibly
    multi-line signature, and the line index its closing paren sits on.
    Splits at depth-0 commas; each piece's leading identifier is the param
    (`*`/`**` stripped; `self`, `cls`, bare `*` / `/` skipped)."""
    out = []
    first = lines[i]
    p = first.find("(")
    if p == -1:
        return out, i
    depth = 0
    piece_start = None        # (line_idx, col) of the first non-space char of the piece
    li, j, ln = i, p + 1, first
    guard = 0
    open_q = None             # quote of a string literal spanning lines
    while li < len(lines) and guard < 400:
        guard += 1
        if open_q is not None:
            e = ln.find(open_q)
            if e == -1:
                li += 1
                if li < len(lines):
                    ln = lines[li]
                continue
            j = e + len(open_q)
            open_q = None
        while j < len(ln):
            ch = ln[j]
            if ch == "#":
                break                       # rest of line is a comment
            if ch in "\"'":
                # A default value's string literal: skip it, so a '#',
                # bracket or comma inside it can't end the walk or change depth.
                if piece_start is None and depth == 0:
                    piece_start = (li, j)
                j, open_q = _string_end(ln, j)
                continue
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    if piece_start is not None:
                        out.append(piece_start)
                    return _params_from_pieces(lines, out), li
                depth -= 1
            elif ch == "," and depth == 0:
                if piece_start is not None:
                    out.append(piece_start)
                piece_start = None
            elif piece_start is None and not ch.isspace() and depth == 0:
                piece_start = (li, j)
            j += 1
        li += 1
        if li < len(lines):
            ln = lines[li]
            j = 0
    if piece_start is not None:
        out.append(piece_start)
    return _params_from_pieces(lines, out), min(li, len(lines) - 1)


def _params_from_pieces(lines, starts):
    out = []
    for (li, col) in starts:
        seg = lines[li][col:]
        m = re.match(r"\*{0,2}\s*([A-Za-z_]\w*)", seg)
        if m is None:
            continue
        name = m.group(1)
        if name in ("self", "cls"):
            continue
        out.append((name, li, col + m.start(1)))
    return out


_L_NON_BINDING_STARTS = ("for ", "if ", "elif ", "while ", "return ", "yield ",
                         "del ", "assert ", "raise ", "import ", "from ", "def ",
                         "class ", "@", "pass", "break", "continue", "try",
                         "else", "finally", "lambda", "print(")


def local_bindings(text, table, line_offset=0, lines=None, scopes=None):
    """{(scope_qualname, name): [LocalBinding, …] (line order)} for the
    function scopes of `table` — all of them, or only the Entry objects in
    `scopes` (plus the defs nested inside them; the editor passes the scopes
    intersecting its viewport). `text`/`lines` are the BUFFER; bindings are
    reported in FILE lines (buffer line + 1 + line_offset)."""
    if lines is None:
        lines = text.split("\n")
    n = len(lines)
    if scopes is None:
        ranges = [(e.line, e.end) for e in table.entries if e.kind == "def"]
    else:
        ranges = [(e.line, e.end) for e in scopes if e.kind == "def"]
    merged = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    out = {}
    declared = set()           # (scope, name) declared global/nonlocal

    def add(scope, name, li, col, kind):
        k = (scope, name)
        if k in declared:
            return
        out.setdefault(k, []).append(
            LocalBinding(scope, name, li + 1 + line_offset, col, kind))

    for lo, hi in merged:
        b0 = max(0, lo - 1 - line_offset)
        b1 = min(n - 1, hi - 1 - line_offset)
        skip_to = -1                # last line of a multi-line signature
        for i in range(b0, b1 + 1):
            if i <= skip_to:
                continue
            raw = lines[i]
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            e = table.scope_at(i + 1 + line_offset)
            if e is None or e.kind != "def":
                continue
            scope = e.qualname
            if e.line == i + 1 + line_offset:
                params, skip_to = _signature_params(lines, i)
                for name, li, col in params:
                    add(scope, name, li, col, "param")
                continue
            code = _code_part(raw)
            mg = _L_GLOBAL_RE.match(code)
            if mg is not None:
                for nm in _L_NAME_RE.findall(mg.group(1)):
                    declared.add((scope, nm))
                    out.pop((scope, nm), None)
                continue
            st = code.lstrip()
            if "for " in code:
                for m in _L_FOR_RE.finditer(code):
                    for nm in _L_NAME_RE.finditer(m.group(1)):
                        add(scope, nm.group(0), i, m.start(1) + nm.start(), "for")
            if st.startswith(("with ", "async with ")):
                for m in _L_AS_RE.finditer(code):
                    add(scope, m.group(1), i, m.start(1), "with")
            elif st.startswith("except"):
                m = _L_AS_RE.search(code)
                if m is not None:
                    add(scope, m.group(1), i, m.start(1), "except")
            elif not st.startswith(_L_NON_BINDING_STARTS):
                m = _L_ASSIGN_RE.match(code)
                if m is not None:
                    for nm in _L_NAME_RE.finditer(m.group(1)):
                        add(scope, nm.group(0), i, m.start(1) + nm.start(), "assign")
            if ":=" in code:
                for m in _L_WALRUS_RE.finditer(code):
                    add(scope, m.group(1), i, m.start(1), "walrus")
    return out


def local_key(table, scope, name, bindings):
    """The (scope_qualname, name) binding key a bare `name` refers to from
    `scope` (an Entry or None): the innermost visible FUNCTION scope that
    binds it, else None. Class bodies and the module never own locals."""
    if scope is None or not bindings:
        return None
    for sq in table.visible_scopes(scope):
        k = (sq, name)
        if k in bindings:
            return k
    return None


def _project_file_changed(path):
    if not str(path).endswith((".py", ".pyi", ".pth", "pyvenv.cfg")):
        return
    path = _norm(path)
    state = _state()
    with state["lock"]:
        state.setdefault("dirty_paths", set()).add(path)
        for record in list(state.get("projects", {}).values()):
            if record["project"].resolves(path) and (path not in record["paths"] or not os.path.exists(path)):
                record["universe"] = None
                record["complete"] = False
        _gen_bump(state, path)
    # A formerly missing import may now exist.
    for key, value in list(_mod_path_cache.items()):
        if value[0] is None or value[0] == path:
            _mod_path_cache.pop(key, None)


def _watch_project(project):
    from meltygui.core.melty import FileWatch
    FileWatch.global_listeners[:] = [listener for listener in FileWatch.global_listeners
                                    if getattr(listener, "__name__", "") != "_project_file_changed"]
    FileWatch.global_listeners.append(_project_file_changed)
    FileWatch.watch_recursive(project.root)
