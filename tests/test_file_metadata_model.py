"""Supplied metadata values and external merge keep their backing identity."""
from types import SimpleNamespace

import pytest

from meltygui.model.file_metadata_model import set_row_tint, set_row_order
from meltygui.models.file_meta import FileMetaProxy


@pytest.fixture
def stores(tmp_path, monkeypatch):
    # Persistence is explicit in these tests; do not leave immortal legacy pollers.
    monkeypatch.setattr(FileMetaProxy, '_ensure_poller', lambda self: None)
    first = FileMetaProxy(tmp_path / 'metadata.pkl')
    second = FileMetaProxy(first.path)
    yield first, second
    first.flush()
    second.flush()


def test_external_reload_preserves_held_entry_and_local_deletion(stores):
    first, second = stores
    first['kept'] = {'icon': 'before'}
    first['deleted'] = {'icon': 'remove'}
    first.flush()
    second.load()
    held = second['kept']
    del second['deleted']
    first['kept']['icon'] = 'after'
    first.flush()
    second.load()
    assert second['kept'] is held
    assert held['icon'] == 'after'
    assert 'deleted' not in second
    second.flush()
    first.load()
    assert 'deleted' not in first


def test_plain_values_and_persistence_share_edit_contract(stores):
    store, reread = stores
    values = {}
    set_row_tint(values, '/first', (0.2, 0.4, 0.6, 1.0))
    set_row_order(values, ['/second', '/first'])
    store.update(values)
    store.flush()
    reread.load()
    assert reread['/first']['tint'] == (0.2, 0.4, 0.6, 1.0)
    assert reread['/second']['order'] == 0
    set_row_tint(store, '/first', None)
    assert not dict.__contains__(store['/first'], 'tint')


def test_directory_listing_uses_model_io(tmp_path):
    from meltygui.model.file_model import list_directory, _dir_mtime_ns
    (tmp_path / 'folder').mkdir()
    (tmp_path / 'visible.txt').touch()
    (tmp_path / '.hidden').touch()
    assert list_directory(tmp_path) == [(tmp_path / 'folder', True),
                                        (tmp_path / 'visible.txt', False)]
    assert len(list_directory(tmp_path, show_hidden=True)) == 3
    assert _dir_mtime_ns(tmp_path) > 0


def test_metadata_service_is_not_saved_as_view_control():
    from meltygui.core.rendering.parameter_core import view_param_names
    from meltygui.view.file_view import draw_file_listing
    assert 'file_metadata' not in view_param_names(SimpleNamespace(_view_func=draw_file_listing))


def test_watch_navigation_keeps_other_views_and_queues_invalidation(monkeypatch):
    from meltygui.core.files import file_explorer_core as runtime
    class View:
        def __init__(self): self.invalidations = 0
        def invalidate(self): self.invalidations += 1
    first, second = View(), View()
    first_state, second_state = SimpleNamespace(_watched=None), SimpleNamespace(_watched=None)
    queues, retired = [], []
    monkeypatch.setattr(runtime, '_WATCHERS', {})
    monkeypatch.setattr(runtime.FileWatch, 'global_listeners', [])
    monkeypatch.setattr(runtime.FileWatch, 'start', lambda: None)
    monkeypatch.setattr(runtime.FileWatch, 'watch_dir', lambda path: None)
    monkeypatch.setattr(runtime.FileWatch, 'unwatch_dir', retired.append)
    monkeypatch.setattr(runtime.Melty, 'post_to_render', queues.append)
    runtime.watch_directory(first, first_state, '/first')
    runtime.watch_directory(second, second_state, '/first')
    runtime.watch_directory(first, first_state, '/second')
    assert not retired
    runtime._on_file_event('/first/new.txt')
    assert not first.invalidations and not second.invalidations
    queues.pop()()
    assert not first.invalidations and second.invalidations == 1
    runtime.watch_directory(second, second_state, '/second')
    assert retired == ['/first']


def test_metadata_injection_respects_explicit_dictionary(gl_context, monkeypatch):
    from conftest import begin_frame, end_frame
    from test_render_func_integration import _init_melty, _tick_frame
    from meltygui.core.core_render import render_func
    import meltygui.models.file_meta as metadata
    runtime = _init_melty()
    shared, supplied, seen = {}, {}, []
    monkeypatch.setattr(metadata, 'file_meta_store', lambda: shared)

    @render_func(tint=(0.3, 0.5, 0.7), show_bg=False, use_cache=False)
    def probe(input_value: int, file_metadata=None):
        seen.append(file_metadata)
        return False, input_value

    for kwargs in ({}, {'file_metadata': supplied}, {}):
        _tick_frame(runtime)
        begin_frame()
        try:
            probe(1, name='metadata injection', **kwargs)
        finally:
            end_frame()
    assert len(seen) == 3
    assert seen[0] is shared and seen[1] is supplied and seen[2] is shared
