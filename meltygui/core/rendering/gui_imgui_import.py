"""Opt-in spelling of Melty's binding for the experimental public GUI API.

The two distributions have independent native ImGui globals. Never mix their
contexts. Install the importer without loading either binding during startup.
"""
import importlib
import importlib.util
import sys


class _ImGuiImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'imgui' or fullname.startswith('imgui.'):
            canonical = 'meltygui_imgui' + fullname[len('imgui'):]
            return importlib.util.spec_from_loader(fullname, _ImGuiLoader(canonical))


class _ImGuiLoader:
    def __init__(self, canonical):
        self.canonical = canonical

    def create_module(self, spec):
        self.module = importlib.import_module(self.canonical)
        self.original_spec = self.module.__spec__
        return self.module

    def exec_module(self, module):
        # importlib assigns the alias spec even when create_module returns an
        # existing module. Keep the canonical metadata for reload/navigation.
        module.__spec__ = self.original_spec


def enable_imgui_import():
    loaded = sys.modules.get('imgui')
    if loaded is not None and loaded is not sys.modules.get('meltygui_imgui'):
        raise ImportError('Import meltygui.gui or meltygui.os_window before import imgui; '
                          'the upstream imgui binding has already loaded a separate native runtime. '
                          'Alternatively use from meltygui import imgui throughout your app.')
    if not any(isinstance(finder, _ImGuiImport) for finder in sys.meta_path):
        sys.meta_path.insert(0, _ImGuiImport())
