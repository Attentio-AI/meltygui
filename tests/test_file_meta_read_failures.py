"""An unavailable shared catalog is never treated as an empty replacement."""
import atexit
import builtins
import errno
import pickle

import pytest

from meltygui.models.file_meta import FileMetaProxy


def catalog(path):
    data = pickle.dumps({'format': 1, 'entries': {'/project': {'tint': (1, .5, 0, 1)}}})
    path.write_bytes(data)
    return data


def test_failed_remote_read_preserves_loaded_entries_and_disk(tmp_path, monkeypatch):
    path = tmp_path / 'file_meta.pkl'
    before = catalog(path)
    store = FileMetaProxy(path)
    atexit.unregister(store.flush)
    original_open = builtins.open
    def unavailable(file, *args, **kwargs):
        if file == path:
            raise OSError(errno.ENOTCONN, 'SSH connection unavailable')
        return original_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', unavailable)
    with pytest.raises(OSError, match='SSH connection unavailable'):
        store.load()
    assert dict.__getitem__(store, '/project')['tint'] == (1, .5, 0, 1)
    with pytest.raises(OSError):
        FileMetaProxy(path)
    assert path.read_bytes() == before


def test_failed_remote_stat_is_not_a_missing_catalog(tmp_path, monkeypatch):
    path = tmp_path / 'file_meta.pkl'
    catalog(path)
    original_stat = type(path).stat
    def unavailable(file, *args, **kwargs):
        if file == path:
            raise OSError(errno.EIO, 'Remote stat failed')
        return original_stat(file, *args, **kwargs)
    monkeypatch.setattr(type(path), 'stat', unavailable)
    with pytest.raises(OSError, match='Remote stat failed'):
        FileMetaProxy(path)


@pytest.mark.parametrize('data', [b'broken pickle', pickle.dumps({'entries': None})])
def test_corrupt_catalog_cannot_be_replaced_by_registration(tmp_path, data):
    path = tmp_path / 'file_meta.pkl'
    path.write_bytes(data)
    with pytest.raises((pickle.UnpicklingError, ValueError)):
        FileMetaProxy(path)
    assert path.read_bytes() == data


def test_a_genuinely_missing_catalog_can_be_created(tmp_path):
    store = FileMetaProxy(tmp_path / 'file_meta.pkl')
    atexit.unregister(store.flush)
    assert not store
