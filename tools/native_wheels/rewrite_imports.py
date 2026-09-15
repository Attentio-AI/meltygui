"""Move native binding imports without changing their local Python names."""
import ast
from pathlib import Path

NAMES = {'imgui': 'meltygui_imgui', 'pycuda': 'meltygui_pycuda'}


def rewrite(text):
    tree = ast.parse(text)
    lines = text.splitlines(True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = []
    def replacement(node, value):
        start = offsets[node.lineno - 1] + len(lines[node.lineno - 1].encode()[:node.col_offset].decode())
        stop = offsets[node.end_lineno - 1] + len(lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())
        edits.append((start, stop, value))
    def mapped(name):
        root, dot, tail = name.partition('.')
        return NAMES.get(root, root) + dot + tail
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split('.')[0] in NAMES for a in node.names):
            statements = []
            for alias in node.names:
                name = mapped(alias.name)
                if name == alias.name:
                    statements.append('import ' + name + (f' as {alias.asname}' if alias.asname else ''))
                elif alias.asname or '.' not in alias.name:
                    statements.append(f'import {name} as {alias.asname or alias.name}')
                else:
                    statements.extend([f'import {name}', f'import {NAMES[alias.name.split(".")[0]]} as {alias.name.split(".")[0]}'])
            replacement(node, ('\n' + ' ' * node.col_offset).join(statements))
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module and node.module.split('.')[0] in NAMES:
            replacement(node, 'from ' + mapped(node.module) + ' import ' + ', '.join(a.name + (f' as {a.asname}' if a.asname else '') for a in node.names))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ('import_module', 'importorskip'):
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                value = node.args[0].value
                if value.split('.')[0] in NAMES:
                    replacement(node.args[0], repr(mapped(value)))
    for start, stop, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[stop:]
    return text


if __name__ == '__main__':
    import sys
    for arg in sys.argv[1:]:
        path = Path(arg)
        for file in path.rglob('*.py') if path.is_dir() else [path]:
            before = file.read_text()
            after = rewrite(before)
            if after != before:
                file.write_text(after)
                print(file)
