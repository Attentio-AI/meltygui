"""The iOS local mirror supplies catalog change events without a polling thread."""
import atexit
import pickle
import sys
from types import SimpleNamespace

from meltygui.models.file_meta import FileMetaProxy


def test_ios_catalog_uses_mirror_notifications(tmp_path, monkeypatch):
    path = tmp_path / 'file_meta.pkl'
    path.write_bytes(pickle.dumps({'format': 1, 'entries': {'old': {'icon': 'one'}}}))
    store = FileMetaProxy(path)
    atexit.unregister(store.flush)
    watchers, repaints = [], []
    monkeypatch.setitem(sys.modules, '_hub_files', SimpleNamespace(watch=watchers.append))
    monkeypatch.setattr(store, '_queue_repaint', lambda: repaints.append(True))
    def unexpected_thread(*args, **kwargs):
        raise AssertionError('iOS must not create a half-second catalog poller')
    monkeypatch.setattr('meltygui.models.file_meta.threading.Thread', unexpected_thread)
    store._ensure_poller()
    store._ensure_poller()
    assert len(watchers) == 1
    watchers[0]()
    assert not repaints
    path.write_bytes(pickle.dumps({'format': 1, 'entries': {'new': {'icon': 'two'}}}))
    watchers[0]()
    assert store._pending and repaints == [True]
    assert dict.__contains__(store, 'old')  # Publication requests a render-thread reload.
    assert store['new']['icon'] == 'two'
    assert not dict.__contains__(store, 'old')


def test_unavailable_notification_preserves_current_catalog(tmp_path, monkeypatch):
    path = tmp_path / 'file_meta.pkl'
    path.write_bytes(pickle.dumps({'format': 1, 'entries': {'kept': {'icon': 'one'}}}))
    store = FileMetaProxy(path)
    atexit.unregister(store.flush)
    def unavailable():
        raise OSError('temporarily unavailable')
    monkeypatch.setattr(store, '_stat_sig', unavailable)
    store._poll_once()
    assert dict.__getitem__(store, 'kept')['icon'] == 'one'
    assert not store._pending
