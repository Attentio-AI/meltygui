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


@pytest.fixture
def metadata_notifications(monkeypatch):
    from meltygui.core.melty import Melty
    queued = []
    monkeypatch.setattr(Melty, 'post_to_render', queued.append)

    def deliver():
        while queued:
            queued.pop(0)()

    return queued, deliver


def test_path_observers_coalesce_local_edits_and_unsubscribe(stores, metadata_notifications):
    store, _ = stores
    queued, deliver = metadata_notifications
    calls, general = [], []
    first = lambda: calls.append(('first', store.get('/a')))
    second = lambda: calls.append(('second', store.get('/a')))
    other = lambda: calls.append(('other', store.get('/b')))
    store.listeners.append(lambda: general.append(True))
    store.subscribe('/a', first)
    store.subscribe('/a', first)
    store.subscribe('/a', second)
    store.subscribe('/b', other)
    store['/a'] = {'breakpoints': {('site',): {'enabled': True}}}
    store['/a']['breakpoints'][('site',)]['enabled'] = False
    store.touch('/a')  # Raw nested edits use the persistence notification contract.
    assert len(queued) == 1 and not calls
    deliver()
    assert [name for name, _ in calls] == ['first', 'second']
    assert not calls[0][1]['breakpoints'][('site',)]['enabled']
    assert general == [True]
    calls.clear()
    store['/a'] = {'icon': 'replacement'}
    store.unsubscribe('/a', first)
    deliver()
    assert calls == [('second', store['/a'])]
    calls.clear()
    del store['/a']
    deliver()
    assert calls == [('second', None)]
    calls.clear()
    store['/b'] = {'icon': 'b'}
    deliver()
    assert calls == [('other', store['/b'])]
    calls.clear()
    store.clear()
    deliver()
    assert sorted(calls) == [('other', None), ('second', None)]
    store.unsubscribe('/a', second)
    store.unsubscribe('/b', other)
    assert not store._path_listeners


def test_path_observers_receive_external_merge_without_reader(stores, metadata_notifications):
    writer, reader = stores
    _, deliver = metadata_notifications
    writer['/a'] = {'icon': 'before'}
    writer['/b'] = {'icon': 'unchanged'}
    writer.flush()
    reader.load()
    deliver()
    seen, other = [], []
    reader.subscribe('/a', lambda: seen.append(reader.get('/a')))
    reader.subscribe('/b', lambda: other.append(True))
    writer['/a']['icon'] = 'after'
    writer.flush()
    # The poller marks a reload and queues delivery even with no view reading.
    reader._pending = True
    reader._queue_repaint()
    deliver()
    assert len(seen) == 1 and seen[0]['icon'] == 'after'
    assert not other
    seen.clear()
    del writer['/a']
    writer.flush()
    reader.load()
    assert not seen
    deliver()
    assert seen == [None] and not other


@pytest.mark.parametrize('entrypoint', ['subscribe', 'unsubscribe', 'touch', 'load', 'delivery'])
def test_path_observers_migrate_live_store_without_resetting_state(
        stores, metadata_notifications, entrypoint):
    store, writer = stores
    _, deliver = metadata_notifications
    store['/local'] = {'icon': 'pending'}
    timer, dirty, listeners = store._timer, store._dirty, store.listeners
    general, seen = [], []
    listeners.append(lambda: general.append(True))
    callback = lambda: seen.append(store.get('/local'))
    del store._path_listeners
    del store._changed_paths

    if entrypoint == 'subscribe':
        store.subscribe('/local', callback)
    elif entrypoint == 'unsubscribe':
        store.unsubscribe('/local', callback)
    elif entrypoint == 'touch':
        store.touch('/other')
    elif entrypoint == 'load':
        writer['/external'] = {'icon': 'external'}
        writer.flush()
        store.load()
    else:
        deliver()  # A callback queued before hotswap executes the new definition.

    assert store._dirty is dirty and '/local' in dirty
    assert store.listeners is listeners
    assert store['/local']['icon'] == 'pending'
    if entrypoint != 'touch':
        assert store._timer is timer
    store.subscribe('/local', callback)
    deliver()
    general.clear()
    seen.clear()
    store['/local']['icon'] = 'after'
    deliver()
    assert general == [True]
    assert len(seen) == 1 and seen[0]['icon'] == 'after'
    store.unsubscribe('/local', callback)
