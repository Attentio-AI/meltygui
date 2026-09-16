"""File import graph — which files import which files.

Built from the meltygui syntax scanner (`melty_scan.scan` → `iter_imports`),
never from `ast`: the same tokenize-based front end the meltygui syntax trees
use, so the graph reads exactly the statements the trees do. Every `.py`
under the root is scanned once (`build_import_graph`), each import resolved
to a FILE under the same root:

  import a.b.c            → root/a/b/c.py | root/a/b/c/__init__.py
  from a.b import x, y    → the module a.b, PLUS a/b/x.py | a/b/x/__init__.py
                            when the imported name is itself a submodule
  from ..m import x       → `level` dots up from the importing file's folder

Absolute imports resolve against the tree root and its ANCESTORS (the
repo keeps `src/…` on sys.path from the repo root, so a tree rooted at
`src/` still reads `from src.lsd… import …`), like sys.path; a target is
kept only when it lies inside the tree root. Anything that doesn't land on
a project file (stdlib, site-packages, a name that is a symbol rather than
a module) is dropped. The result is an
`ImportGraph` with both directions. Building runs on a background thread
(`start_build`), the file tree polls `ImportGraphBuild.result`.
"""

from __future__ import annotations

import math
import os
import pickle
import threading
import time
from pathlib import Path


from meltygui.text_index import _SKIP_DIRS
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.code.melty_scan import Import
from meltygui.code.melty_scan import ImportFrom
from meltygui.code.melty_scan import ScanError
from meltygui.code.melty_scan import alias
from meltygui.code.melty_scan import scan_imports
from meltygui.code.melty_scan import _k


class ImportGraph:
    """`imports[file]` = files it imports, `imported_by[file]` = files that
    import it (both `set[Path]`, keys = resolved absolute paths).
    `errors` = files the scanner refused (path → message)."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.imports: dict[Path, set[Path]] = {}
        self.imported_by: dict[Path, set[Path]] = {}
        self.errors: dict[Path, str] = {}
        self.built_at = 0.0
        self.seconds = 0.0
        self.layout: dict[Path, tuple[float, float]] = {}   # node → (x, y) in [-1, 1]
        self.columns = 0        # layered layout depth (layout_graph)
        self.layers: list[list[Path]] = []   # column → nodes in vertical order

    def add(self, importer, imported):
        if importer == imported:
            return
        self.imports.setdefault(importer, set()).add(imported)
        self.imported_by.setdefault(imported, set()).add(importer)

    @property
    def file_count(self):
        return len(self.imports)

    @property
    def edge_count(self):
        return sum(len(v) for v in self.imports.values())

    @property
    def max_importers(self):
        """The largest importer count of any file (0 for an empty graph) —
        the normalizer for usage-driven visuals."""
        if not self.imported_by:
            return 0
        return max(len(v) for v in self.imported_by.values())

    def usage(self, path):
        """Importer count of `path` on a 0..1 log scale against the graph's
        most-used file — a few importers already lift a file noticeably,
        the long tail of heavily used ones spreads over the top half."""
        top = self.max_importers
        if top <= 0:
            return 0.0
        n = len(self.importers_of(path))
        return math.log1p(n) / math.log1p(top) if n else 0.0

    def imports_of(self, path):
        return self.imports.get(Path(path), set())

    def importers_of(self, path):
        return self.imported_by.get(Path(path), set())


def project_python_files(root):
    """Every `.py` under `root`, skipping the trigram index's skip set, venvs,
    dot-directories and __pycache__ — the same pruning the studio's other
    project walks use."""
    root = Path(root)
    out = []
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for p in entries:
            name = p.name
            if p.is_dir():
                if name in _SKIP_DIRS or name.startswith(".") or "venv" in name:
                    continue
                stack.append(p)
            elif p.suffix == ".py":
                out.append(p)
    return out


# How many ancestors of the tree root absolute imports may resolve against
# (root first, then its parent, ...) - the repo root sits two above `src/`.
IMPORT_ROOT_DEPTH = 3


def import_roots(root):
    root = Path(root).resolve()
    roots = [root]
    for _ in range(IMPORT_ROOT_DEPTH - 1):
        if roots[-1].parent == roots[-1]:
            break
        roots.append(roots[-1].parent)
    return roots


def _module_file_under(base, parts):
    """The file a dotted module `parts` names under `base`, or None."""
    if not parts:
        return None
    target = base.joinpath(*parts)
    candidate = target.with_suffix(".py")
    if candidate.is_file():
        return candidate
    candidate = target / "__init__.py"
    if candidate.is_file():
        return candidate
    return None


def _module_file(root, parts, roots=None, memo=None):
    """`parts` resolved against the import roots in order (first hit wins,
    like sys.path); only a file inside the tree `root` counts. `memo`
    (one dict per build) caches the answer per (search bases, parts): the
    same module is imported from dozens of files, and every probe is a
    stat — a warm build was 290 ms of stats without it."""
    root = Path(root)
    search = tuple(roots or import_roots(root))
    key = (search, tuple(parts))
    if memo is not None and key in memo:
        return memo[key]
    found = None
    for base in search:
        f = _module_file_under(base, parts)
        if f is not None:
            found = f if (f == root or root in f.parents) else None
            break
    if memo is not None:
        memo[key] = found
    return found


def resolve_import(root, importer, node, roots=None, memo=None):
    """The project files one Import / ImportFrom node of `importer` refers
    to (a list; empty when nothing under `root` matches)."""
    root = Path(root).resolve()
    roots = roots or import_roots(root)
    found = []
    if _k(node) == "Import":
        for a in node.names:
            f = _module_file(root, a.name.split("."), roots, memo)
            if f is not None:
                found.append(f)
        return found
    # ImportFrom: the base package is the importer's folder `level` dots up
    # (level 1 is its own folder) for a relative import, the root otherwise.
    if node.level:
        # Relative: anchored on the importer's folder, no root search.
        base = importer.parent
        for _ in range(node.level - 1):
            base = base.parent
        module_parts = node.module.split(".") if node.module else []
        search = [base]
    else:
        module_parts = node.module.split(".")
        search = roots
    module_file = _module_file(root, module_parts, search, memo) if module_parts else None
    if module_file is not None:
        found.append(module_file)
    for a in node.names:
        if a.name == "*":
            continue
        f = _module_file(root, module_parts + [a.name], search, memo)
        if f is not None:
            found.append(f)
    return found


# ── per-file import cache: (mtime, size) + the file's import records ─────────
# Invalidation is content-free (stat only - never a digest); a hit costs one
# stat, a miss one tokenize pass (scan_imports). Persisted in ~/.lsd so the
# first build of a session is as fast as a rebuild. Records are plain tuples:
#   ("Import", None, 0, ((name, asname), ...)) / ("ImportFrom", module, level, names)

CACHE_VERSION = 1


def cache_path():
    return Path.home() / ".lsd" / "import_graph_cache.pkl"


def load_cache(path=None):
    path = path or cache_path()
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
            return data["files"]
    except (OSError, pickle.PickleError, EOFError, KeyError, TypeError, ValueError):
        pass
    return {}


def save_cache(files, path=None):
    path = path or cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump({"version": CACHE_VERSION, "files": files}, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except OSError:
        pass


def _records(nodes):
    return tuple((_k(n), getattr(n, "module", None), getattr(n, "level", 0),
                  tuple((a.name, a.asname) for a in n.names)) for n in nodes)


def _nodes(records):
    out = []
    for kind, module, level, names in records:
        aliases = [alias(name, asname) for name, asname in names]
        out.append(Import(aliases) if kind == "Import" else ImportFrom(module, aliases, level))
    return out


def build_import_graph(root, files=None, read_text=None, cache=None, persist=True):
    """Scan every project file (or `files`) and resolve its imports. Pure:
    no Melty state, safe on a background thread. `read_text(path)` lets the
    caller serve pending (unsaved) text (a file so served bypasses the
    cache); `cache` = a files dict (load_cache) — None loads the persisted
    one; `persist` writes it back when anything was rescanned."""
    root = Path(root).resolve()
    started = time.monotonic()
    graph = ImportGraph(root)
    roots = import_roots(root)
    if files is None:
        files = project_python_files(root)
    if cache is None:
        cache = load_cache() if persist else {}
    dirty = False
    memo = {}
    for path in files:
        path = Path(path)
        graph.imports.setdefault(path, set())
        key = str(path)
        try:
            if read_text is None:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                hit = cache.get(key)
                if hit is not None and hit[0] == stamp:
                    records = hit[1]
                else:
                    # A scan failure is cached too (with its message), else an
                    # unparsable file would be rescanned on every build.
                    try:
                        records = _records(scan_imports(path.read_text(encoding="utf-8", errors="replace")))
                    except ScanError as e:
                        records = str(e)
                    cache[key] = (stamp, records)
                    dirty = True
            else:
                records = _records(scan_imports(read_text(path)))
        except ScanError as e:
            graph.errors[path] = str(e)
            continue
        except OSError as e:
            graph.errors[path] = str(e)
            continue
        if isinstance(records, str):
            graph.errors[path] = records
            continue
        for node in _nodes(records):
            for target in resolve_import(root, path, node, roots, memo):
                graph.add(path, target)
    if dirty and persist:
        save_cache(cache)
    graph.seconds = time.monotonic() - started
    graph.built_at = time.time()
    return graph


# ── layout: layered, left to right ───────────────────────────────────────────

def layout_graph(graph, sweeps=8, seed=0):
    """Stamp `graph.layout` — one (x, y) in [-1, 1] per file — as a LAYERED
    left-to-right drawing (the Sugiyama idea, kept simple):

      column 0  = the roots: files nothing imports (isolated files too)
      column c  = one past the deepest column among the file's importers,
                  so `toggles.py` — imported from everywhere — lands at the
                  far right and every import line runs left → right.

    Cycles (the repo has them) are broken by usage rank — an edge from a
    more-used file to a less-used one is ignored for the column assignment
    only — so a hub can never be pulled left by a cycle. Within a column, nodes are ordered
    by `sweeps` barycenter passes over their neighbours' rows (down then up),
    which keeps lines short and mostly uncrossed; rows share one pitch, so a
    tall column fills the height and a short one sits centred. Deterministic."""
    nodes = sorted(set(graph.imports) | set(graph.imported_by))
    n = len(nodes)
    if n == 0:
        graph.layout = {}
        return graph.layout
    index = {p: i for i, p in enumerate(nodes)}
    # edges importer → imported (order grows along an import)
    out = [[] for _ in range(n)]
    for a, targets in graph.imports.items():
        for b in targets:
            if a in index and b in index and a != b:
                out[index[a]].append(index[b])
    for lst in out:
        lst.sort()

    # ── cycle breaking: rank files by USAGE (importer count, ties → the file
    # with more imports first) and keep, for the column assignment only,
    # the edges that run from a less-used file to a more-used one. That is a
    # DAG by construction, and it decides the cycles the way the picture
    # should read: the hubs everything imports can only ever move RIGHT.
    rank = sorted(range(n), key=lambda v: (len(graph.importers_of(nodes[v])),
                                           -len(graph.imports_of(nodes[v])), nodes[v].name))
    position = {v: i for i, v in enumerate(rank)}
    dag_out = [[w for w in out[v] if position[w] > position[v]] for v in range(n)]
    indeg = [0] * n
    for v in range(n):
        for w in dag_out[v]:
            indeg[w] += 1

    # ── columns: longest path from the roots (Kahn order) ──
    column = [0] * n
    ready = [v for v in range(n) if indeg[v] == 0]
    order = []
    while ready:
        v = ready.pop(0)
        order.append(v)
        for w in dag_out[v]:
            column[w] = max(column[w], column[v] + 1)
            indeg[w] -= 1
            if indeg[w] == 0:
                ready.append(w)
    columns = max(column) + 1
    layers = [[] for _ in range(columns)]
    for v in range(n):
        layers[column[v]].append(v)

    # ── rows: barycenter sweeps over ALL neighbours (both edge directions) ──
    neighbours = [set() for _ in range(n)]
    for v in range(n):
        for w in out[v]:
            neighbours[v].add(w)
            neighbours[w].add(v)
    row = [0.0] * n
    for layer in layers:
        for i, v in enumerate(layer):
            row[v] = float(i)

    def sweep(layer_indices):
        for c in layer_indices:
            layer = layers[c]
            keyed = []
            for v in layer:
                adjacent = [row[w] for w in neighbours[v] if column[w] != c]
                bary = sum(adjacent) / len(adjacent) if adjacent else row[v]
                keyed.append((bary, nodes[v].name, v))
            keyed.sort()
            layers[c] = [v for _b, _name, v in keyed]
            for i, v in enumerate(layers[c]):
                row[v] = float(i)

    for _ in range(sweeps):
        sweep(range(1, columns))
        sweep(range(columns - 2, -1, -1))

    # ── normalize: x by column, y by row with one shared pitch ──
    tallest = max(len(layer) for layer in layers)
    pitch = 2.0 / max(1, tallest)
    graph.layout = {}
    for c, layer in enumerate(layers):
        x = -1.0 + 2.0 * c / max(1, columns - 1) if columns > 1 else 0.0
        offset = (len(layer) - 1) * 0.5
        for i, v in enumerate(layer):
            graph.layout[nodes[v]] = (float(x), float((i - offset) * pitch))
    graph.columns = columns
    # The column/row ORDER itself, for views that draw draw in pixels
    # (import_graph_view lays labelled boxes out from it, rather of the
    # normalized coordinates above).
    graph.layers = [[nodes[v] for v in layer] for layer in layers]
    return graph.layout


# ── the shared current graph: built by either window, read by both ─────────────
# Hotswap-guarded module state (the file re-execs on hotswap; the graph
# survives). Data, not UI state - each window's own injected state keeps
# its selection / camera.
_CURRENT = globals().get("_CURRENT", {"graph": None})


def current():
    """The last graph built in this session (any window), or None."""
    return _CURRENT["graph"]


def set_current(graph):
    _CURRENT["graph"] = graph


class ImportGraphBuild:
    """One background build: `running` while the thread works, then
    `result` (an ImportGraph) or `error`."""

    def __init__(self, root):
        self.root = Path(root)
        self.running = True
        self.result = None
        self.error = None
        self._thread = threading.Thread(target=self._run, name="import-graph",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        try:
            graph = build_import_graph(self.root)
            layout_graph(graph)
            set_current(graph)
            self.result = graph
        except Exception as e:      # surfaced on the UI's status line
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.running = False
            request_render()


def start_build(root):
    return ImportGraphBuild(root)
