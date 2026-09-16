"""Translate identifiers in older saved sessions to the canonical package."""
import json
from pathlib import Path

_MODULES = json.loads(Path(__file__).with_name('module_map.json').read_text())


def canonical_name(name):
    parts = name.split('.')
    for boundary in range(len(parts), 0, -1):
        prefix = '.'.join(parts[:boundary])
        if prefix in _MODULES:
            return '.'.join([_MODULES[prefix], *parts[boundary:]])
    return name


def register_names(names):
    """Let application packages migrate their own persisted identifiers."""
    _MODULES.update(names)
