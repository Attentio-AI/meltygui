"""Application metadata consumed by staging and the native project generator."""
from pathlib import Path
import tomllib


def read_application(directory):
    root = Path(directory).resolve()
    manifest = root / 'pyproject.toml'
    data = tomllib.loads(manifest.read_text()) if manifest.is_file() else {}
    config = dict(data.get('tool', {}).get('melty', {}).get('app', {}))
    entry = config.get('entry', 'main.py')
    module = entry.removesuffix('.py').replace('/', '.')
    if not all(part.isidentifier() for part in module.split('.')):
        raise ValueError('Application entry must be a Python module or relative Python file')
    config.update(root=root, entry_module=module)
    config.setdefault('bundle_id', 'local.melty.app')
    config.setdefault('resources', [])
    config.setdefault('sources', ['*.py'])
    config.setdefault('dependencies', data.get('project', {}).get('dependencies', ['meltygui']))
    return config
