"""Decoration discovers renderers without drawing or importing them on lookup."""
import os
import subprocess
import sys

import pytest

from meltygui.core.core_render import render_func
from meltygui.core.melty import Melty
from meltygui.models import file_meta


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(file_meta, 'SAVE_DELAY_S', 3600)
    monkeypatch.setattr(file_meta.FileMetaProxy, '_ensure_poller', lambda self: None)
    store = file_meta.FileMetaProxy(tmp_path / 'metadata.pkl')
    monkeypatch.setattr(file_meta, 'file_meta_store', lambda: store)
    monkeypatch.setattr(Melty, 'render_funcs_by_name', dict(Melty.render_funcs_by_name))
    yield store
    store.flush()


def test_decorators_record_full_paths_without_rendering(catalog):
    for module, decorator in [('app_one.views', '@render_func'),
                              ('app_two.views', '@render_func(tint=(0.2, 0.4, 0.6))')]:
        namespace = {'__name__': module, 'render_func': render_func}
        exec(f'{decorator}\ndef draw_example(input_value: int):\n'
             '    raise AssertionError("discovery must not draw")\n', namespace)

    assert catalog.render_func_paths() == [
        'app_one.views.draw_example', 'app_two.views.draw_example']
    catalog.flush()
    reader = file_meta.FileMetaProxy(catalog.path)
    assert reader.render_func_paths() == catalog.render_func_paths()
    assert reader['app_one.views.draw_example']['is_render_func'] is True


def test_redecoration_preserves_metadata_and_does_not_write_again(catalog):
    def draw_example(input_value: int):
        return False, input_value

    path = f'{draw_example.__module__}.{draw_example.__qualname__}'
    catalog[path] = {'icon': 'existing'}
    held = catalog[path]
    render_func(draw_example)
    assert catalog[path] is held
    assert held['icon'] == 'existing'
    assert catalog.render_func_paths() == [path]
    catalog.flush()
    generation = catalog.generation
    render_func(draw_example)
    assert catalog.generation == generation
    assert not catalog._dirty


def test_catalog_keeps_qualified_class_names_and_ignores_file_entries(catalog):
    class Views:
        @render_func()
        def draw_example(input_value: int):
            return False, input_value

    catalog['/source/file.py'] = {'icon': 'file'}
    func = Views.draw_example
    assert catalog.render_func_paths() == [f'{func.__module__}.{func.__qualname__}']


def test_other_app_registration_merges_with_pending_local_entries(catalog):
    namespace = {'__name__': 'local_app', 'render_func': render_func}
    exec('@render_func\ndef draw_example(input_value: int):\n'
         '    return False, input_value\n', namespace)
    result = subprocess.run(
        [sys.executable, '-c', '''
from meltygui.core.core_render import render_func
from meltygui.models.file_meta import file_meta_store
@render_func
def draw_example(input_value: int):
    return False, input_value
file_meta_store().flush()
'''], env={**os.environ, 'MELTY_FILE_META': str(catalog.path)},
        capture_output=True, text=True, close_fds=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    catalog.flush()
    reader = file_meta.FileMetaProxy(catalog.path)
    assert {'local_app.draw_example', '__main__.draw_example'} <= set(reader.render_func_paths())
