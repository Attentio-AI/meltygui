"""Discover library render functions without executing their modules.

Editable checkouts cache the index between launches, validating source mtimes
and sizes on first public view lookup. Changed sources are discovered by the
next interpreter run; packaged builds can supply an immutable generated index.
"""
from functools import cache
from pathlib import Path
from threading import Lock

_discovery_lock = Lock()


def _sources(root):
    return sorted(path for path in Path(root).rglob('*.py')
                  if not any(part in ('examples', '__pycache__')
                             for part in path.relative_to(root).parts))


def _manifest(root):
    files = {}
    for path in _sources(root):
        stat = path.stat()
        files[str(path.relative_to(root))] = [stat.st_mtime_ns, stat.st_size]
    return files


def _module_statements(statements):
    """Include conditional module definitions, but never methods/local functions."""
    import ast
    for node in statements:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ('body', 'orelse', 'finalbody'):
            yield from _module_statements(getattr(node, field, ()))
        for handler in getattr(node, 'handlers', ()):
            yield from _module_statements(handler.body)


def discover_render_exports(root):
    """Map decorated module-level functions to their canonical import locations."""
    import ast
    import importlib.util
    exports = {}
    canonical = 'meltygui.core.core_render.render_func'
    for path in _sources(root):
        relative = path.relative_to(root)
        source = path.read_text(encoding='utf-8')
        if 'render_func' not in source:
            continue
        parts = relative.with_suffix('').parts
        module = '.'.join(('meltygui', *parts))
        if parts[-1] == '__init__':
            module = module.removesuffix('.__init__')
        package = module if parts[-1] == '__init__' else module.rpartition('.')[0]
        statements = list(_module_statements(ast.parse(source, filename=str(path)).body))
        aliases = {'render_func': canonical} if module == 'meltygui.core.core_render' else {}
        for node in statements:
            if isinstance(node, ast.ImportFrom):
                origin = node.module or ''
                if node.level:
                    origin = importlib.util.resolve_name('.' * node.level + origin, package)
                for name in node.names:
                    aliases[name.asname or name.name] = origin + '.' + name.name
            elif isinstance(node, ast.Import):
                for name in node.names:
                    aliases[name.asname or name.name.split('.')[0]] = (
                        name.name if name.asname else name.name.split('.')[0])
        for node in statements:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                suffix = []
                while isinstance(target, ast.Attribute):
                    suffix.insert(0, target.attr)
                    target = target.value
                if not isinstance(target, ast.Name):
                    continue
                resolved = '.'.join((aliases.get(target.id, target.id), *suffix))
                if resolved not in (canonical, 'meltygui.render_func'):
                    continue
                spec = (module, node.name)
                previous = exports.get(node.name)
                if previous is not None and previous != spec:
                    raise RuntimeError(f'Duplicate public render function {node.name!r}: '
                                       f'{previous[0]} and {module}')
                exports[node.name] = spec
    return exports


def write_render_exports(root):
    """Generate the export index for an immutable packaged dependency tree."""
    import json
    root = Path(root)
    exports = discover_render_exports(root)
    (root / '_render_exports.json').write_text(json.dumps(exports, sort_keys=True))


@cache
def render_exports():
    root = Path(__file__).resolve().parents[2]
    index = root / '_render_exports.json'
    if index.is_file():
        # Packaging generates this from the exact bundled dependency sources.
        import json
        return {name: tuple(spec) for name, spec in json.loads(index.read_text()).items()}
    # Serialize first lookups from the render and import threads.
    with _discovery_lock:
        return _editable_exports(root)


def _editable_exports(root):
    import hashlib
    import json
    import os
    import tempfile
    from meltygui.core.runtime.paths import cache_root

    key = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
    path = cache_root() / 'render-exports' / f'{key}.json'
    files = _manifest(root)
    try:
        saved = json.loads(path.read_text())
        if saved['version'] == 1 and saved['files'] == files:
            exports = saved['exports']
            if isinstance(exports, dict) and all(
                    isinstance(name, str) and isinstance(spec, list) and len(spec) == 2
                    and all(isinstance(value, str) for value in spec)
                    for name, spec in exports.items()):
                return {name: tuple(spec) for name, spec in exports.items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass

    exports = discover_render_exports(root)
    # Do not save a mixed generation if a source changed during discovery.
    if files != _manifest(root):
        return exports
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'version': 1, 'files': files, 'exports': exports}, stream)
        os.replace(temporary, path)
    except OSError:
        pass  # A read-only cache must not prevent public view imports.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return exports
