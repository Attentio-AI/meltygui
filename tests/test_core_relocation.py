"""Core moves preserve lazy mode loading, saved state and editable source addresses."""
import importlib
import io
from pathlib import Path
import pickle
import subprocess
import sys


def test_modes_remain_lazy_until_used():
    result = subprocess.run(
        [sys.executable, '-c', '''
import pickle
import sys
from meltygui.core.modes import Modes
handle = Modes.FILE_TREE
restored = pickle.loads(pickle.dumps(handle))
assert 'meltygui.core.mode' not in sys.modules
assert 'meltygui.debug.mode' not in sys.modules
from meltygui.core.mode import Mode
assert handle._resolve() is restored._resolve() is Mode.FILE_TREE
from meltygui.modes import Modes as legacy_modes
from meltygui.debug.mode import Mode as legacy_mode
assert legacy_modes is Modes and legacy_mode is Mode
'''],
        close_fds=False, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_saved_core_classes_and_modes_keep_identity():
    from meltygui.core.dict_conversion import DictConversion
    from meltygui.core.load_save_v2 import LSDUnpickler
    from meltygui.core.mode import Mode
    from meltygui.core.module_names import canonical_name
    from meltygui.core.render_host import RenderHost

    for old, name, expected in (
        ('meltygui.debug.mode', 'Mode', Mode),
        ('meltygui.state.dict_conversion', 'DictConversion', DictConversion),
        ('meltygui.code.render_host', 'RenderHost', RenderHost),
    ):
        saved = f'c{old}\n{name}\n.'.encode()
        assert pickle.loads(saved) is expected
        assert LSDUnpickler(io.BytesIO(saved)).load() is expected
        assert canonical_name(old + '.' + name) == expected.__module__ + '.' + name
    assert pickle.loads(pickle.dumps(Mode.FILE_TREE)) is Mode.FILE_TREE


def test_source_editing_follows_core_definitions():
    from meltygui.code.fileref import to_address
    from meltygui.code.file_converters import load_text
    from meltygui.core.mode import Mode
    from meltygui.core.modes import _Modes
    from meltygui.core.render_funcs import _RenderFuncs

    for value in (Mode, _Modes, _RenderFuncs):
        module = importlib.import_module(value.__module__)
        address = to_address(value)
        assert address.path == Path(module.__file__)
        assert address.path.parent.name == 'core'
        assert f'class {value.__name__}' in load_text(address)


def test_old_imports_navigate_to_core_after_a_move(monkeypatch):
    import meltygui
    from meltygui.code import symbol_roster
    from meltygui.code.source_context import analysis_project

    root = Path(meltygui.__file__).resolve().parents[1]
    project = analysis_project(root)
    for old, new in (
        ('meltygui.debug.mode', 'meltygui.core.mode'),
        ('meltygui.rendering.core_render', 'meltygui.core.core_render'),
        ('meltygui.views.columns', 'meltygui.core.column_core'),
    ):
        monkeypatch.setitem(symbol_roster._mod_path_cache, (project.key, old), ('/removed/source.py', 0))
        expected = str(root / (new.replace('.', '/') + '.py'))
        assert symbol_roster.module_to_path(old, project=project) == expected
