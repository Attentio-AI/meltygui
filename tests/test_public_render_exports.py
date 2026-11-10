"""Public views follow their decorators, without importing every renderer."""
import subprocess
import sys

import pytest

from meltygui.core.runtime.public_exports import discover_render_exports


@pytest.fixture
def editable(tmp_path, monkeypatch):
    import meltygui.core.runtime.public_exports as exports
    root = tmp_path / 'library'
    module = root / 'core/runtime/public_exports.py'
    module.parent.mkdir(parents=True)
    monkeypatch.setattr(exports, '__file__', str(module))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    exports.render_exports.cache_clear()
    yield root, exports
    exports.render_exports.cache_clear()


def test_editable_index_skips_parsing_on_next_launch(editable, monkeypatch):
    root, exports = editable
    (root / 'view.py').write_text('from meltygui import render_func\n@render_func\ndef draw_test(): pass\n')
    expected = exports.render_exports()
    assert expected == {'draw_test': ('meltygui.view', 'draw_test')}
    exports.render_exports.cache_clear()
    monkeypatch.setattr(exports, 'discover_render_exports', lambda root: pytest.fail('source scan'))
    assert exports.render_exports() == expected


def test_editable_index_tracks_edits_additions_and_deletions(editable):
    import os
    root, exports = editable
    view = root / 'view.py'
    source = 'from meltygui import render_func\n@render_func\ndef first(): pass\n'
    view.write_text(source)
    assert set(exports.render_exports()) == {'first'}
    stamp = view.stat()
    view.write_text(source.replace('first', 'other'))  # Same size, newer mtime.
    os.utime(view, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    exports.render_exports.cache_clear()
    assert set(exports.render_exports()) == {'other'}
    added = root / 'added.py'
    added.write_text(source)
    exports.render_exports.cache_clear()
    assert set(exports.render_exports()) == {'first', 'other'}
    view.unlink()
    exports.render_exports.cache_clear()
    assert set(exports.render_exports()) == {'first'}
    added.write_text('def first(): pass\n')
    exports.render_exports.cache_clear()
    assert exports.render_exports() == {}


def test_editable_index_recovers_from_corrupt_or_unwritable_cache(editable, tmp_path, monkeypatch):
    root, exports = editable
    (root / 'view.py').write_text('from meltygui import render_func\n@render_func\ndef first(): pass\n')
    expected = exports.render_exports()
    index, = (tmp_path / 'cache').rglob('*.json')
    for broken in ('{', '[]', '{"version": 1}'):
        index.write_text(broken)
        exports.render_exports.cache_clear()
        assert exports.render_exports() == expected
    blocked = tmp_path / 'blocked'
    blocked.write_text('not a directory')
    monkeypatch.setenv('XDG_CACHE_HOME', str(blocked))
    exports.render_exports.cache_clear()
    assert exports.render_exports() == expected


def test_submodule_lookup_does_not_discover_render_functions(monkeypatch):
    import types
    import meltygui
    module = types.ModuleType('meltygui.pending_import')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(meltygui, '_render_exports', lambda: pytest.fail('source scan'))
    assert meltygui.pending_import is module


def test_parallel_first_lookups_share_discovery(editable, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from unittest.mock import Mock
    root, exports = editable
    (root / 'view.py').write_text('from meltygui import render_func\n@render_func\ndef first(): pass\n')
    discover = Mock(wraps=exports.discover_render_exports)
    monkeypatch.setattr(exports, 'discover_render_exports', discover)
    ready = Barrier(2)

    def lookup():
        ready.wait(timeout=5)
        return exports.render_exports()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(lookup), pool.submit(lookup)
        assert first.result() == second.result() == {'first': ('meltygui.view', 'first')}
    discover.assert_called_once_with(root)


def test_packaged_index_avoids_discovery_and_preserves_specs(tmp_path, monkeypatch):
    import meltygui.core.runtime.public_exports as exports
    module = tmp_path / 'core/runtime/public_exports.py'
    module.parent.mkdir(parents=True)
    monkeypatch.setattr(exports, '__file__', str(module))
    expected = {'draw_bool': ('meltygui.view.control_view', 'draw_bool')}
    view = tmp_path / 'view/control_view.py'
    view.parent.mkdir()
    view.write_text('from meltygui import render_func\n@render_func\ndef draw_bool(): pass\n')
    exports.write_render_exports(tmp_path)
    monkeypatch.setattr(exports, 'discover_render_exports', lambda root: pytest.fail('source scan'))
    exports.render_exports.cache_clear()
    try:
        assert exports.render_exports() == expected
    finally:
        exports.render_exports.cache_clear()


def test_discovery_handles_decorator_forms_and_ignores_local_functions(tmp_path):
    (tmp_path / 'views.py').write_text('''
from meltygui.core.core_render import render_func as render
import meltygui as mg
from meltygui import render_func
@render
def first(): pass
@render(use_cache=True)
def second(): pass
@mg.render_func()
def third(): pass
@render_func
def fourth(): pass
if True:
    @render
    def conditional(): pass
def factory():
    @render
    def local(): pass
class Views:
    @render
    def method(self): pass
def plain(): pass
raise RuntimeError("Discovery must not import this module")
''')
    exports = discover_render_exports(tmp_path)
    assert set(exports) == {'first', 'second', 'third', 'fourth', 'conditional'}
    assert exports['second'] == ('meltygui.views', 'second')
    # A newly authored view needs no edit to a separate export list.
    (tmp_path / 'new_view.py').write_text('''
from meltygui import render_func
@render_func()
def newly_added(): pass
''')
    assert 'newly_added' in discover_render_exports(tmp_path)


def test_duplicate_names_report_both_modules(tmp_path):
    source = 'from meltygui import render_func\n@render_func\ndef duplicate(): pass\n'
    for name in ('first', 'second'):
        (tmp_path / f'{name}.py').write_text(source)
    with pytest.raises(RuntimeError, match=r'duplicate.*meltygui.first and meltygui.second'):
        discover_render_exports(tmp_path)


def test_public_lookup_is_lazy_and_preserves_function_identity():
    result = subprocess.run([sys.executable, '-c', '''
import sys
import meltygui
assert 'meltygui.core.runtime.public_exports' not in sys.modules
names = dir(meltygui)
assert 'draw_bool' in names and 'draw_tab_bar' in names
assert 'meltygui.view.control_view' not in sys.modules
assert 'meltygui.view.tab_view' not in sys.modules
assert 'draw_bool' in meltygui.__all__
from meltygui import draw_bool
from meltygui.view.control_view import draw_bool as original
assert draw_bool is original is meltygui.draw_bool
try:
    meltygui.this_render_function_does_not_exist
except AttributeError:
    pass
else:
    raise AssertionError('Unknown exports must raise AttributeError')
'''], capture_output=True, text=True, close_fds=False)
    assert result.returncode == 0, result.stdout + result.stderr
