"""Entry scripts keep their source identity in the shared renderer catalog."""
from functools import wraps

import pytest

from meltygui.models.file_meta import FileMetaProxy


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(FileMetaProxy, '_ensure_poller', lambda self: None)
    value = FileMetaProxy(tmp_path / 'metadata.pkl')
    yield value
    value.flush()


def function_at(path, module='__main__'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('def editor(value=None): return value\n')
    namespace = {'__name__': module}
    exec(compile(path.read_text(), str(path), 'exec'), namespace)
    return namespace['editor']


def test_entry_scripts_with_same_name_stay_distinct(store, tmp_path):
    for repo in ('first', 'second'):
        path = tmp_path / repo / 'editor.py'
        store.register_render_func(function_at(path))
    keys = store.render_func_paths()
    assert keys == [f'{tmp_path / repo / "editor.py"}::editor' for repo in ('first', 'second')]
    for key in keys:
        assert store[key]['source_path'] == key.split('::')[0]
        assert store[key]['source_qualname'] == 'editor'


def test_wrapper_uses_original_source_and_preserves_metadata(store, tmp_path):
    source = tmp_path / 'app' / 'editor.py'
    original = function_at(source, 'app.editor')

    @wraps(original)
    def wrapper(*args, **kwargs):
        return original(*args, **kwargs)

    store['app.editor.editor'] = {'tint': (0.2, 0.4, 0.6), 'icon': 'x'}
    store.register_render_func(wrapper)
    entry = store['app.editor.editor']
    assert entry['source_path'] == str(source)
    assert entry['source_qualname'] == 'editor'
    assert entry['tint'] == (0.2, 0.4, 0.6)
    assert entry['icon'] == 'x'
    before = store.generation
    store.register_render_func(wrapper)
    assert store.generation == before
