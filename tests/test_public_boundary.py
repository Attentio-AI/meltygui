"""The public toolkit must work when no commercial package is available."""
import ast
from pathlib import Path
import subprocess
import sys
import meltygui


def test_public_sources_have_no_private_imports():
    for path in Path(meltygui.__file__).parent.rglob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                     [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            assert not any(name.startswith(('meltygui_pro', 'meltyprivate')) for name in names), path


def test_standalone_text_inspection_and_symbol_usage(tmp_path):
    script = '''
import importlib.abc
import sys
class NoPrivate(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'meltygui_pro', 'meltyprivate'}:
            raise AssertionError('Public toolkit imported ' + fullname)
sys.meta_path.insert(0, NoPrivate())
import meltygui
from meltygui import draw_text, draw_any, draw_folder_files
from meltygui.code import symbol_roster
from meltygui.editor import pending_save, usage_picker, source_preview
from meltygui.code.source_context import analysis_project
from meltygui.state.core_undo import UndoManager
from meltygui.core.files import file_tree_core as file_tree
from meltygui.core.automation import action_core as actions
from meltygui.model import trace_report_model as crash_reports
from meltygui.core.diagnostics import trace_core as stack_trace_view
assert not hasattr(meltygui, 'draw_code_editor')
assert not hasattr(meltygui, 'global_search')
assert not hasattr(meltygui, 'EditorProjectState')
assert not hasattr(meltygui, 'mark_project')
assert not any(n.startswith(('meltygui_pro', 'meltyprivate')) for n in sys.modules)
context = analysis_project(path=__file__) if '__file__' in globals() else analysis_project()
assert context.environment is None
assert context.source_paths
from pathlib import Path
root = Path(sys.argv[1])
(root / 'pyproject.toml').touch()
source = root / 'source.py'
source.write_text('def target():\\n    return 1\\n\\nanswer = target()\\n')
entry = symbol_roster.table_for(source).by_name['target'][0]
usages = symbol_roster.usages_of(entry, project=analysis_project(root))
assert any(usage.path == str(source) and usage.line == 4 for usage in usages), usages
'''
    subprocess.run([sys.executable, '-I', '-c', script, str(tmp_path)], check=True, close_fds=False)


def test_source_context_uses_nearest_nested_source_root(tmp_path):
    from meltygui.code.source_context import analysis_project
    root = tmp_path / 'parent'; nested = root / 'child'
    nested.mkdir(parents=True)
    (root / 'pyproject.toml').touch(); (nested / 'pyproject.toml').touch()
    path = nested / 'module.py'; path.write_text('value = 1\n')
    assert analysis_project(path=path).root == str(nested)
