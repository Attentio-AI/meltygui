"""Module moves keep running state and make both import paths canonical."""
import importlib
import inspect
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from meltygui.core.module_compatibility import install_module_aliases
from meltygui.code.file_converters import _recompile_module, stamp_module_baseline
from meltygui.core.melty import Melty
from meltygui.code.fileref import to_address


SOURCE = '''from . import starts
starts.append("initialized")
state = {"value": 1}
class Settings:
    value = 2
    def read(self):
        return state["value"], self.value
def read():
    return state["value"]
def make_callback():
    def callback():
        return state["value"]
    return callback
callback = make_callback()
'''


@pytest.fixture
def package(tmp_path, monkeypatch):
    from meltygui.core import module_names
    monkeypatch.setattr(module_names, '_MODULES', module_names._MODULES.copy())
    path = tmp_path / 'module_move_fixture'
    path.mkdir()
    (path / '__init__.py').write_text('starts = []\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    original_finders = list(sys.meta_path)
    yield path
    sys.meta_path[:] = original_finders
    for name in tuple(sys.modules):
        if name == 'module_move_fixture' or name.startswith('module_move_fixture.'):
            del sys.modules[name]


def test_install_updates_live_saved_names_without_replacing_the_map(package, monkeypatch):
    from meltygui.core import module_names

    names = {'historical.fixture': 'module_move_fixture.old'}
    monkeypatch.setattr(module_names, '_MODULES', names)
    install_module_aliases({'module_move_fixture.old': 'module_move_fixture.new'})
    assert module_names._MODULES is names
    assert module_names.canonical_name('historical.fixture.Settings') == 'module_move_fixture.new.Settings'
    assert module_names.canonical_name('module_move_fixture.old.Settings') == 'module_move_fixture.new.Settings'


def test_reinstall_reads_the_updated_manifest_in_a_running_session(package, monkeypatch):
    from meltygui.core import module_compatibility

    monkeypatch.setattr(module_compatibility, '__file__', str(package / 'compatibility.py'))
    manifest = package / 'legacy_modules.json'
    manifest.write_text('{}')
    install_module_aliases()
    source = 'state = {"count": 1}\ndef read():\n    return state["count"]\n'
    (package / 'old.py').write_text(source)
    original = importlib.import_module('module_move_fixture.old')
    original.state['count'] = 42
    (package / 'old.py').rename(package / 'new.py')
    manifest.write_text(json.dumps({'module_move_fixture.old': 'module_move_fixture.new'}))
    install_module_aliases()
    current = importlib.import_module('module_move_fixture.new')
    assert current is original
    assert current.read() == 42
    assert Path(current.__file__) == package / 'new.py'


@pytest.mark.parametrize('legacy_first', [False, True])
def test_cold_alias_initializes_once_and_keeps_source_location(package, legacy_first):
    (package / 'new.py').write_text(SOURCE)
    aliases = {'module_move_fixture.old': 'module_move_fixture.new'}
    finder = install_module_aliases(aliases)
    assert install_module_aliases(aliases) is finder
    first, second = ('old', 'new') if legacy_first else ('new', 'old')
    module = importlib.import_module('module_move_fixture.' + first)
    assert importlib.import_module('module_move_fixture.' + second) is module
    assert module.__name__ == 'module_move_fixture.new'
    assert module.__spec__.name == 'module_move_fixture.new'
    assert inspect.getsourcefile(module.read) == str(package / 'new.py')
    assert importlib.import_module('module_move_fixture').starts == ['initialized']


def test_live_move_preserves_singletons_callbacks_and_later_hotswap(package, monkeypatch):
    original_path = package / 'old.py'
    original_path.write_text(SOURCE)
    module = importlib.import_module('module_move_fixture.old')
    stamp_module_baseline(module, SOURCE)
    state, read, callback, settings_type = module.state, module.read, module.callback, module.Settings
    settings = settings_type()
    state['value'] = 42
    settings_type.value = 73
    for value in (module, read, settings_type):
        assert to_address(value).path == original_path
    new_path = package / 'new.py'
    original_path.rename(new_path)
    install_module_aliases({'module_move_fixture.old': 'module_move_fixture.new'})

    assert importlib.import_module('module_move_fixture.new') is module
    assert importlib.import_module('module_move_fixture.old') is module
    assert importlib.import_module('module_move_fixture').starts == ['initialized']
    assert module.state is state
    assert module.read is read
    assert module.callback is callback
    assert module.Settings is settings_type
    assert settings.read() == (42, 73)
    assert callback() == 42
    assert inspect.getsourcefile(read) == str(new_path)
    assert inspect.getsourcefile(callback) == str(new_path)
    assert inspect.getsourcefile(settings_type) == str(new_path)
    assert 'return state["value"]' in inspect.getsource(read)
    for value in (module, read, settings_type):
        assert to_address(value).path == new_path

    monkeypatch.setattr(Melty, 'cache', Mock())
    edited = SOURCE.replace('return state["value"]\ndef make_callback',
                            'return state["value"] + 1\ndef make_callback')
    new_path.write_text(edited)
    assert _recompile_module(module, edited, str(new_path)) is None
    assert module.read is read
    assert read() == 43
    assert module.state is state
    assert settings.read() == (42, 73)
