"""File-based symbol analysis in the running interpreter.

Applications may provide another context (for example an isolated interpreter).
The default has no project settings, environment selection, or package installs.
"""
import os
import sys
from pathlib import Path
from meltygui.core.runtime.extensions import get


class SourceContext:
    def __init__(self, root):
        self.root = str(Path(root).expanduser().resolve())
        self.source_paths = (self.root,)
        if (Path(self.root) / 'src').is_dir():
            self.source_paths += (str(Path(self.root) / 'src'),)
        self.import_paths = tuple(dict.fromkeys((*self.source_paths, *sys.path)))
        self.environment = None

    @property
    def key(self):
        return self.root, self.import_paths, self.environment

    def refresh(self):
        return self

    def contains(self, path):
        return str(path) == self.root or str(path).startswith(self.root + os.sep)

    def owns(self, path):
        from meltygui.text_index import _SKIP_DIRS
        return self.contains(path) and not any(
            part in _SKIP_DIRS for part in Path(path).relative_to(self.root).parts[:-1]) and owning_root(path) == self.root

    def resolves(self, path):
        return self.owns(path) or any(
            root and (str(path) == root or str(path).startswith(root + os.sep))
            for root in self.import_paths if root not in self.source_paths)


def owning_root(path):
    from meltygui.code.fileref import project_root_of
    target = Path(path).expanduser().resolve()
    return str(project_root_of(target) or (target if target.is_dir() else target.parent))


_contexts = {}


def analysis_project(project=None, path=None):
    provider = get('source_context')
    if provider is not None:
        return provider(project=project, path=path)
    if hasattr(project, 'refresh'):
        return project.refresh()
    if project is None:
        from meltygui.core.runtime.paths import application_root
        project = owning_root(path) if path is not None else application_root()
    root = str(Path(project).expanduser().resolve())
    if root not in _contexts:
        _contexts[root] = SourceContext(root)
    return _contexts[root]
