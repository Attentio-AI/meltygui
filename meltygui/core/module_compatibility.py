"""Legacy module names share the canonical module, including live runtime state."""
import gc
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import types


LEGACY_MODULES = json.loads(Path(__file__).with_name('legacy_modules.json').read_text())


def _relocate_namespace(module, old_name, spec, aliases):
    """Rehome an unchanged module without rerunning its initialization.

    Whole-file relocations preserve line layout. Rebase code filenames so source
    inspection and subsequent definition hotswaps use the new file. Functions,
    classes, dictionaries, worker closures and the module itself keep identity.
    """
    namespace = vars(module)
    old_file = namespace.get('__file__')
    namespace.update(__name__=spec.name, __package__=spec.parent,
                     __file__=spec.origin, __spec__=spec, __loader__=spec.loader,
                     __cached__=spec.cached)
    address_module = sys.modules.get("meltygui.code.fileref")
    invalidate_address = vars(address_module).get("invalidate_address_cache") if address_module is not None else None
    if invalidate_address is not None:
        invalidate_address(module)
    code_cache = {}
    visited = set()

    def code_at_destination(code):
        if code.co_filename != old_file:
            return code
        if code not in code_cache:
            constants = tuple(code_at_destination(value) if isinstance(value, types.CodeType) else value
                              for value in code.co_consts)
            code_cache[code] = code.replace(co_filename=spec.origin, co_consts=constants)
        return code_cache[code]

    def baseline_at_destination(owner):
        baseline = vars(owner).get('__hotswap_attr_src__')
        if isinstance(baseline, dict):
            for field, expression in baseline.items():
                for old, new in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True):
                    expression = expression.replace(old, new)
                baseline[field] = expression

    def definition_at_destination(value):
        if id(value) in visited:
            return
        visited.add(id(value))
        if isinstance(value, types.FunctionType):
            if invalidate_address is not None and (value.__module__ == old_name or value.__globals__ is namespace):
                invalidate_address(value)
            if value.__module__ == old_name:
                value.__module__ = spec.name
            if value.__globals__ is namespace:
                value.__code__ = code_at_destination(value.__code__)
            wrapped = vars(value).get('__wrapped__')
            if isinstance(wrapped, types.FunctionType):
                definition_at_destination(wrapped)
            for cell in value.__closure__ or ():
                try:
                    contents = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(contents, types.FunctionType):
                    definition_at_destination(contents)
        elif isinstance(value, type) and value.__module__ == old_name:
            if invalidate_address is not None:
                invalidate_address(value)
            value.__module__ = spec.name
            baseline_at_destination(value)
            for member in tuple(vars(value).values()):
                if isinstance(member, (types.FunctionType, type)):
                    definition_at_destination(member)
                elif isinstance(member, (staticmethod, classmethod)):
                    definition_at_destination(member.__func__)
                elif isinstance(member, property):
                    for function in (member.fget, member.fset, member.fdel):
                        if function is not None:
                            definition_at_destination(function)

    baseline_at_destination(module)
    for value in tuple(namespace.values()):
        if isinstance(value, (types.FunctionType, type)):
            definition_at_destination(value)
    # A worker or event callback may retain a closure that is no longer exported
    # by its module. Rebase those too, without walking arbitrary runtime objects.
    for value in gc.get_objects():
        if type(value) is types.FunctionType and value.__globals__ is namespace:
            definition_at_destination(value)


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target):
        self.target = target

    def create_module(self, spec):
        module = importlib.import_module(self.target)
        self.canonical_spec = module.__spec__
        return module

    def exec_module(self, module):
        # Import machinery sets __spec__ even when create_module returned an
        # existing module. Keep reload and source inspection on the real file.
        module.__spec__ = self.canonical_spec


class _RelocationLoader(importlib.abc.Loader):
    def __init__(self, module, old_name, canonical_spec, aliases):
        self.module = module
        self.old_name = old_name
        self.canonical_spec = canonical_spec
        self.aliases = aliases

    def create_module(self, spec):
        return self.module

    def exec_module(self, module):
        _relocate_namespace(module, self.old_name, self.canonical_spec, self.aliases)


class _LegacyModuleFinder(importlib.abc.MetaPathFinder):
    def __init__(self, aliases):
        self.aliases = aliases
        self.old_names = {new: old for old, new in aliases.items()}

    def find_spec(self, fullname, path=None, target=None):
        canonical = self.aliases.get(fullname)
        if canonical is not None:
            return importlib.util.spec_from_loader(fullname, _AliasLoader(canonical))
        old_name = self.old_names.get(fullname)
        if old_name is None or old_name not in sys.modules or target is not None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None:
            return None
        loader = _RelocationLoader(sys.modules[old_name], old_name, spec, self.aliases)
        return importlib.util.spec_from_file_location(fullname, spec.origin, loader=loader)


def install_module_aliases(aliases=None):
    """Install lazy aliases and adopt any already-loaded legacy modules."""
    aliases = LEGACY_MODULES if aliases is None else aliases
    for finder in sys.meta_path:
        if isinstance(finder, _LegacyModuleFinder) and finder.aliases == aliases:
            return finder
    finder = _LegacyModuleFinder(aliases)
    sys.meta_path.insert(0, finder)
    for old_name, new_name in aliases.items():
        if old_name not in sys.modules:
            continue
        canonical = importlib.import_module(new_name)
        sys.modules[old_name] = canonical
        parent, _, child = old_name.rpartition('.')
        if parent in sys.modules:
            setattr(sys.modules[parent], child, canonical)
    return finder
