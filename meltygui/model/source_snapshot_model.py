"""Prepared data belonging to one immutable source version, shared by its consumers."""
import ast
import os
import threading
import types
from collections.abc import MutableMapping
from meltygui.model.file_location_model import file_path, is_remote


def _source_path(path):
    return str(file_path(path)) if is_remote(path) else os.path.abspath(path)


def same_source_version(path, original, current, disk_mtime=None):
    """Identity for pending text; matching full-file provenance for disk text."""
    if current is original:
        return True
    if disk_mtime is None:
        disk_mtime = getattr(original, '_disk_mtime', None)
    return (disk_mtime is not None and
            getattr(current, '_disk_mtime', None) == disk_mtime and
            getattr(current, '_disk_span', None) == (os.path.realpath(path), None, None))


class CodeIdentityMap(MutableMapping):
    """CPython code equality ignores filenames; monitoring requires identity."""
    def __init__(self, values=()):
        self._entries = {}
        self.update(values)

    def __getitem__(self, code):
        return self._entries[id(code)][1]

    def __setitem__(self, code, value):
        self._entries[id(code)] = (code, value)

    def __delitem__(self, code):
        del self._entries[id(code)]

    def __iter__(self):
        return (code for code, value in tuple(self._entries.values()))

    def __len__(self):
        return len(self._entries)


class SourceSnapshot:
    def __init__(self, path, text, disk_mtime=None):
        self.path = _source_path(path)
        self.text = text
        self.disk_mtime = disk_mtime if disk_mtime is not None else getattr(text, '_disk_mtime', None)
        self.code_tree = self.index = self.compiled = None
        self.code_lines = CodeIdentityMap()
        self.line_codes = {}
        self.occurrences = CodeIdentityMap()
        self.occurrence_offsets = {}
        self.error = None
        self.ready = False
        self._lock = threading.Lock()
        self._existing_ast = None
        self._ast_root = None
        self._scope_maps = {}
        self._reference_codes = None
        self._scope_nodes_by_location = {}
        self.index_ready = False
        self.index_error = None
        self._index_worker = None
        self._index_callbacks = set()
        self._index_lock = threading.Lock()

    @classmethod
    def adopt_running(cls, legacy, codes):
        """One-time hotswap adoption; keep monitored code and index identities."""
        path, text, index, tree = legacy[:4]
        snapshot = cls(path, text, legacy[-1])
        snapshot.index = index
        snapshot.code_tree = legacy[4] if len(legacy) == 6 else None
        snapshot._existing_ast = tree
        snapshot.code_lines = CodeIdentityMap(codes)
        snapshot.compiled = next((code for code in codes if code.co_name == '<module>'), None)
        return snapshot

    def matches(self, path, text=None):
        if path is None or _source_path(path) != self.path:
            return False
        return text is None or same_source_version(self.path, self.text, text, self.disk_mtime)

    def _ensure_index_runtime(self):
        # Preserve already-prepared live snapshots across definition updates.
        if not hasattr(self, 'index_ready'):
            self.index_ready = self.index is not None
            self.index_error = None
            self._index_worker = None
            self._index_callbacks = set()
            self._index_lock = threading.Lock()

    def prepare_index(self):
        """Worker-only source keys; no AST or executable-code preparation."""
        self._ensure_index_runtime()
        with self._index_lock:
            if self.index_ready:
                if self.index_error is not None:
                    raise self.index_error
                return self
            try:
                from meltygui.code.core_syntax import parse_to_dict
                if self.code_tree is None:
                    self.code_tree = parse_to_dict(self.text, file_path=self.path, frontend='scan')
                if self.index is None:
                    self.index = self.code_tree['__origin__'].source_site_index
            except BaseException as error:
                self.index_error = error
                raise
            finally:
                self.index_ready = True
        return self

    def request_index(self, on_ready):
        """One model-owned worker per version; deliver completion on the render thread."""
        from meltygui.model.file_location_model import is_remote
        if is_remote(self.path):
            return  # Cross-file source analysis needs a host-side index.
        self._ensure_index_runtime()
        if self.index_ready:
            return
        self._index_callbacks.add(on_ready)
        if self._index_worker is not None:
            return
        def build():
            from meltygui.core.melty import Melty
            try:
                self.prepare_index()
            except Exception:
                # The result/error belongs to the snapshot, including invalid source.
                pass
            finally:
                def deliver():
                    self._index_worker = None
                    callbacks, self._index_callbacks = self._index_callbacks, set()
                    for callback in callbacks:
                        callback(frame_delta=1)
                Melty.post_to_render(deliver)
        self._index_worker = threading.Thread(target=build, name='source-index', daemon=True)
        self._index_worker.start()

    def prepare(self, actual_code=None):
        """Worker-only execution analysis, shared across consumers of this version."""
        self.prepare_index()
        if not isinstance(self.code_lines, CodeIdentityMap):
            self.code_lines = CodeIdentityMap(self.code_lines)
            self.occurrences = CodeIdentityMap(self.occurrences)
            self.line_codes = {}
            for code, lines in self.code_lines.items():
                for line in lines:
                    self.line_codes.setdefault(line, []).append(code)
        if not hasattr(self, 'line_codes'):
            self.line_codes = {}
            for code, lines in self.code_lines.items():
                for line in lines:
                    self.line_codes.setdefault(line, []).append(code)
        with self._lock:
            if '_register_code' in vars(self):
                del self._register_code  # migrate the earlier closure-based registrar
            if self.ready and (actual_code is None or getattr(self, '_ast_root', None) is not None):
                if self.error is not None:
                    raise self.error
                if actual_code is not None:
                    self._verify_code(actual_code)
                    self._register_code(actual_code)
                return self
            try:
                tree = self._existing_ast if self._existing_ast is not None else ast.parse(self.text, filename=self.path)
                if self.compiled is None:
                    self.compiled = compile(tree, self.path, 'exec', dont_inherit=True)
                scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                          ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
                scope_nodes = [tree] + [node for node in ast.walk(tree) if isinstance(node, scopes)]
                maps = {}
                by_location = {}
                for node in scope_nodes:
                    occurrences = []
                    def visit(current):
                        if current is not node and isinstance(current, scopes):
                            return
                        name = (current.id if isinstance(current, ast.Name) else
                                current.arg if isinstance(current, ast.arg) else None)
                        if name is not None:
                            sites = self.index.at_line(current.lineno)
                            if sites:
                                occurrence_id = len(self.occurrence_offsets)
                                self.occurrence_offsets[occurrence_id] = current.lineno - sites[0].start_line
                                occurrences.append((sites[0].path, name, occurrence_id))
                        for child in ast.iter_child_nodes(current):
                            visit(child)
                    visit(node)
                    maps[id(node)] = tuple(occurrences)
                    if node is not tree:
                        name = getattr(node, 'name', {ast.Lambda: '<lambda>', ast.ListComp: '<listcomp>',
                            ast.SetComp: '<setcomp>', ast.DictComp: '<dictcomp>', ast.GeneratorExp: '<genexpr>'}.get(type(node)))
                        line = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', ())])
                        by_location.setdefault((name, line), []).append(node)
                self._ast_root = tree
                self._scope_maps = maps
                self._scope_nodes_by_location = by_location
                if actual_code is not None:
                    self._verify_code(actual_code)
                self._register_code(actual_code if actual_code is not None else self.compiled)
                self._existing_ast = None
            except BaseException as error:
                self.error = error
                raise
            finally:
                self.ready = True
        return self

    def _verify_code(self, code):
        """Do not attach today's source keys to a different loaded code version."""
        if getattr(self, '_reference_codes', None) is None:
            self._reference_codes = set()
            def collect(current):
                self._reference_codes.add(current)
                for value in current.co_consts:
                    if isinstance(value, types.CodeType):
                        collect(value)
            collect(self.compiled)
        if code not in self._reference_codes:
            raise ValueError(f'Loaded code does not match source: {self.path}')

    def _register_code(self, code):
        if code in self.code_lines:
            # Legacy adopted codes still need their occurrence map.
            if code in self.occurrences:
                return
        self.code_lines[code] = {line for _, _, line in code.co_lines() if line is not None}
        for line in self.code_lines[code]:
            self.line_codes.setdefault(line, []).append(code)
        if code.co_name == '<module>':
            node = self._ast_root
        else:
            candidates = self._scope_nodes_by_location.get((code.co_name, code.co_firstlineno), ())
            # Lambdas/comprehensions may share a name and first line.
            # Their expression bytecode positions identify the actual
            # AST occurrence, including nested lambdas on that line.
            positions = [(start, end, col, end_col)
                         for start, end, col, end_col in code.co_positions()
                         if None not in (start, end, col, end_col) and (col, end_col) != (0, 0)]
            def contains_positions(candidate):
                expression = candidate.body if isinstance(candidate, ast.Lambda) else candidate
                return all((expression.lineno, expression.col_offset) <= (start, col)
                           and (end, end_col) <= (expression.end_lineno, expression.end_col_offset)
                           for start, end, col, end_col in positions)
            positioned = [candidate for candidate in candidates if contains_positions(candidate)]
            node = min(positioned, key=lambda candidate: (
                candidate.end_lineno - candidate.lineno,
                candidate.end_col_offset - candidate.col_offset)) if positioned else (
                candidates[0] if candidates else None)
        self.occurrences[code] = (getattr(node, 'lineno', None), self._scope_maps.get(id(node), ()))
        for constant in code.co_consts:
            if isinstance(constant, types.CodeType):
                self._register_code(constant)


class SourceInspection:
    """Producer-neutral references attached to prepared source-site keys.

    Bindings are (identity, source_key, label, value, occurrence_id) tuples.
    The legacy live-value overlay adapter is a presentation boundary only.
    """
    def __init__(self, source, bindings, *, execution_key=None, name='', def_line=None):
        self.source = source
        self.bindings = tuple(bindings)
        self.execution_key = execution_key
        self.name = name
        self.def_line = def_line
        self._live_store = None

    @property
    def live_store(self):
        if self._live_store is None:
            from meltygui.code.live_view import LocalValueStore
            store = LocalValueStore(self.name)
            store.__live_fallback__ = True
            if self.def_line is not None:
                store.__def_line__ = self.def_line
            for identity, source_key, name, value, occurrence_id in self.bindings:
                site = self.source.index.sites.get(source_key)
                if site is None:
                    continue
                line = site.start_line + self.source.occurrence_offsets[occurrence_id]
                # Old windows address a line/name. It is derived once here;
                # the producer retains source keys and reference identities.
                key = (f'line:{line}#{name}',)
                store.__live_values__[key] = value
                store.__live_labels__[key] = name
            self._live_store = store
        return self._live_store
