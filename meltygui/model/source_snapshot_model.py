"""Prepared data belonging to one immutable source version, shared by its consumers."""
import ast
import os
import threading
import types


class SourceSnapshot:
    def __init__(self, path, text, disk_mtime=None):
        self.path = os.path.abspath(path)
        self.text = text
        self.disk_mtime = disk_mtime if disk_mtime is not None else getattr(text, '_disk_mtime', None)
        self.code_tree = self.index = self.compiled = None
        self.code_lines = {}
        self.occurrences = {}
        self.error = None
        self.ready = False
        self._lock = threading.Lock()

    def matches(self, path, text=None):
        if path is None or os.path.abspath(path) != self.path:
            return False
        return (text is None or text is self.text or
                (self.disk_mtime is not None and
                 getattr(text, '_disk_mtime', None) == self.disk_mtime and
                 getattr(text, '_disk_span', None) == (os.path.realpath(self.path), None, None)))

    def prepare(self):
        """Worker-only: competing editor/execution workers reuse one preparation."""
        with self._lock:
            if self.ready:
                if self.error is not None:
                    raise self.error
                return self
            try:
                from meltygui.code.core_syntax import parse_to_dict
                self.code_tree = parse_to_dict(self.text, file_path=self.path, frontend='scan')
                self.index = self.code_tree['__origin__'].source_site_index
                tree = ast.parse(self.text, filename=self.path)
                self.compiled = compile(tree, self.path, 'exec')
                scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                          ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
                scope_nodes = [tree] + [node for node in ast.walk(tree) if isinstance(node, scopes)]
                maps = {}
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
                                occurrences.append((sites[0].path, name, current.lineno))
                        for child in ast.iter_child_nodes(current):
                            visit(child)
                    visit(node)
                    maps[id(node)] = tuple(occurrences)
                def register(code):
                    self.code_lines[code] = {line for _, _, line in code.co_lines() if line is not None}
                    if code.co_name == '<module>':
                        node = tree
                    else:
                        candidates = [node for node in scope_nodes[1:]
                                      if getattr(node, 'name', None) == code.co_name and
                                      min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', ())])
                                      == code.co_firstlineno]
                        node = candidates[0] if candidates else None
                    self.occurrences[code] = (getattr(node, 'lineno', None), maps.get(id(node), ()))
                    for constant in code.co_consts:
                        if isinstance(constant, types.CodeType):
                            register(constant)
                register(self.compiled)
            except BaseException as error:
                self.error = error
                raise
            finally:
                self.ready = True
        return self


class SourceInspection:
    """Producer-neutral references attached to prepared source-site keys.

    Bindings are (identity, source_key, label, value, occurrence_line) tuples.
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
            for identity, source_key, name, value, line in self.bindings:
                # Old windows address a line/name. It is derived once here;
                # the producer retains source keys and reference identities.
                key = (f'line:{line}#{name}',)
                store.__live_values__[key] = value
                store.__live_labels__[key] = name
            self._live_store = store
        return self._live_store
