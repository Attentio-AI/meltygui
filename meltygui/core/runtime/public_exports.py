"""Discover library render functions without executing their modules.

Discovery runs once, on first public view lookup/introspection, not on package
import or per frame. Editable source changes are discovered by the next
interpreter run; packaged builds can supply an index generated during packaging.
"""
from functools import cache
from pathlib import Path


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
    for path in sorted(Path(root).rglob('*.py')):
        relative = path.relative_to(root)
        if any(part in ('examples', '__pycache__') for part in relative.parts):
            continue
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
        # Editable checkouts have no index and retain source discovery.
        import json
        return {name: tuple(spec) for name, spec in json.loads(index.read_text()).items()}
    return discover_render_exports(root)
