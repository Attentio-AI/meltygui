"""File import graph — which files import which files.

Built from the melty syntax scanner (`melty_scan.scan` → `iter_imports`),
never from `ast`: the same tokenize-based front end the melty syntax trees
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

import threading
import time
from pathlib import Path

from src.lsd.gl_gui.text_index import _SKIP_DIRS
from src.lsd.gl_gui.utils.glfw_utils import request_render
from src.lsd.gl_gui.view.core_conversion.melty_scan import ScanError, iter_imports, scan, _k


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


def _module_file(root, parts, roots=None):
    """`parts` resolved against the import roots in order (first hit wins,
    like sys.path); only a file inside the tree `root` counts."""
    root = Path(root)
    for base in (roots or import_roots(root)):
        f = _module_file_under(base, parts)
        if f is not None:
            return f if (f == root or root in f.parents) else None
    return None


def resolve_import(root, importer, node, roots=None):
    """The project files one Import / ImportFrom node of `importer` refers
    to (a list; empty when nothing under `root` matches)."""
    root = Path(root).resolve()
    roots = roots or import_roots(root)
    found = []
    if _k(node) == "Import":
        for a in node.names:
            f = _module_file(root, a.name.split("."), roots)
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
    module_file = _module_file(root, module_parts, search) if module_parts else None
    if module_file is not None:
        found.append(module_file)
    for a in node.names:
        if a.name == "*":
            continue
        f = _module_file(root, module_parts + [a.name], search)
        if f is not None:
            found.append(f)
    return found


def build_import_graph(root, files=None, read_text=None):
    """Scan every project file (or `files`) and resolve its imports. Pure:
    no Melty state, safe on a background thread. `read_text(path)` lets the
    caller serve pending (unsaved) text; default reads the disk."""
    root = Path(root).resolve()
    started = time.monotonic()
    graph = ImportGraph(root)
    roots = import_roots(root)
    if files is None:
        files = project_python_files(root)
    if read_text is None:
        def read_text(path):
            return path.read_text(encoding="utf-8", errors="replace")
    for path in files:
        path = Path(path)
        graph.imports.setdefault(path, set())
        try:
            module, _standalone, _trailing = scan(read_text(path))
        except ScanError as e:
            graph.errors[path] = str(e)
            continue
        except OSError as e:
            graph.errors[path] = str(e)
            continue
        for node in iter_imports(module):
            for target in resolve_import(root, path, node, roots):
                graph.add(path, target)
    graph.seconds = time.monotonic() - started
    graph.built_at = time.time()
    return graph


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
            self.result = build_import_graph(self.root)
        except Exception as e:      # surfaced on the UI's status line
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.running = False
            request_render()


def start_build(root):
    return ImportGraphBuild(root)
