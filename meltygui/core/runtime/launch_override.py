"""Launch overrides: values that replace a symbol's source value at launch,
kept in the user's config instead of the code.

A setting like `Toggles.TextEditor.font_size` lives in three places: the
source file, the live class, and (here) `$XDG_CONFIG_HOME/<app_id>/
launch_overrides.json`. An override names a module, the path to the value
inside it and the value as a Python EXPRESSION (the text the source would
hold), so enum members and calls like `Tint(...)` persist the same way
literals do:

    {"version": 1,
     "overrides": {"meltygui.core.runtime.toggles": [
         {"path": ["Toggles", "SomeInnerClass", "my_int_toggle"],
          "value": "2", "default": "1"}]}}

`default` is the source expression the override replaced, kept so a reader
can tell that the code's own value has moved since (the override still
applies: the user's choice wins until they reset it).

`install(app_id)` runs once at boot. A module that is already imported is
patched at once; every other overridden module is patched by an import hook
right after its body runs and before its importer sees it, so nothing is
imported early and a module the app never loads costs nothing. An entry
that does not resolve is reported and skipped: a bad override never stops a
launch.

Writes (`set_override` / `clear_override`, the CodeDict LaunchOverride sink)
only mark the store dirty; `flush()` writes the file (app exit, atexit).

What an override cannot reach: a value the module's own body consumed while
it ran (a decorator argument, a default argument, a module-level copy).
"""
import ast
import atexit
import importlib.abc
import importlib.machinery
import sys
import types

from meltygui.core.runtime.app_settings import read_file
from meltygui.core.runtime.app_settings import settings_dir
from meltygui.core.runtime.app_settings import write_file

FILE_NAME = 'launch_overrides.json'
_FORMAT = 1

_state = dict(path=None, overrides={}, dirty=False, hooked=False)


def overrides_path(app_id):
    return settings_dir(app_id) / FILE_NAME


def install(app_id):
    """Load the app's overrides, patch the modules already imported and hook
    the import of the rest. Idempotent per process."""
    path = overrides_path(app_id)
    if _state['path'] == path:
        return
    _state['path'] = path
    data = read_file(path)
    saved = data.get('overrides') if data.get('version') == _FORMAT else None
    _state['overrides'] = {module: [entry for entry in entries if _well_formed(entry)]
                           for module, entries in (saved or {}).items()
                           if isinstance(entries, list)}
    for module_name in list(_state['overrides']):
        module = sys.modules.get(module_name)
        if module is not None:
            apply_to_module(module)
    if not _state['hooked']:
        sys.meta_path.insert(0, _OverrideFinder())
        atexit.register(flush)
        _state['hooked'] = True


def _well_formed(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('path'), list) and entry['path']
            and isinstance(entry.get('value'), str))


def apply_to_module(module):
    """Set every override of `module` on the live objects. Returns how many
    applied."""
    applied = 0
    for entry in _state['overrides'].get(module.__name__, ()):
        try:
            value = _evaluate(entry['value'], module)
            set_at_path(module, entry['path'], value)
            applied += 1
        except Exception as error:
            print(f"meltygui: launch override {module.__name__}:{'.'.join(map(str, entry['path']))} "
                  f"skipped: {type(error).__name__}: {error}", file=sys.stderr)
    return applied


def _evaluate(expression, module):
    try:
        return ast.literal_eval(expression)
    except (ValueError, SyntaxError):
        return eval(expression, dict(module.__dict__))


def step_into(holder, key):
    """The member `key` names inside a module, class, dict or list."""
    if isinstance(holder, (types.ModuleType, type)):
        return getattr(holder, key)
    return holder[key]


def set_at_path(root, path, value):
    """Set `value` at `path` under `root`, walking attributes through modules
    and classes and items through dicts and lists. A dict or list that is
    replaced by one of the same kind keeps its identity (holders of the old
    object see the new content)."""
    holder = root
    for key in path[:-1]:
        holder = step_into(holder, key)
    key = path[-1]
    try:
        current = step_into(holder, key)
    except (AttributeError, KeyError, IndexError):
        current = None
    if isinstance(current, dict) and isinstance(value, dict) and current is not value:
        current.clear()
        current.update(value)
    elif isinstance(current, list) and isinstance(value, list) and current is not value:
        current[:] = value
    elif isinstance(holder, (types.ModuleType, type)):
        setattr(holder, key, value)
    else:
        holder[key] = value


def delete_at_path(root, path):
    holder = root
    for key in path[:-1]:
        holder = step_into(holder, key)
    if isinstance(holder, (types.ModuleType, type)):
        delattr(holder, path[-1])
    else:
        del holder[path[-1]]


def get_override(module_name, path):
    for entry in _state['overrides'].get(module_name, ()):
        if entry['path'] == list(path):
            return entry
    return None


def set_override(module_name, path, value_source, default_source=None):
    """Record `value_source` (a Python expression) for the symbol. An
    override equal to the code's own value is removed instead."""
    if default_source is not None and value_source == default_source:
        clear_override(module_name, path)
        return
    entry = get_override(module_name, path)
    if entry is None:
        entry = {'path': list(path)}
        _state['overrides'].setdefault(module_name, []).append(entry)
        if default_source is not None:
            entry['default'] = default_source
    entry['value'] = value_source
    _state['dirty'] = True


def clear_override(module_name, path):
    entries = _state['overrides'].get(module_name)
    entry = get_override(module_name, path)
    if entry is None:
        return
    entries.remove(entry)
    if not entries:
        del _state['overrides'][module_name]
    _state['dirty'] = True


def flush():
    """Write the overrides when they changed since the last write."""
    if not _state['dirty'] or _state['path'] is None:
        return
    if write_file(_state['path'], {'version': _FORMAT, 'overrides': _state['overrides']}):
        _state['dirty'] = False


class _OverrideLoader(importlib.abc.Loader):
    """Runs the real loader, then applies the module's overrides."""

    def __init__(self, loader):
        self.loader = loader

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module):
        self.loader.exec_module(module)
        apply_to_module(module)

    def __getattr__(self, name):
        return getattr(self.loader, name)


class _OverrideFinder(importlib.abc.MetaPathFinder):
    """Wraps the loader of a module that has overrides; every other import
    passes through untouched."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _state['overrides']:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, 'find_spec'):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _OverrideLoader(spec.loader)
                return spec
        return None
