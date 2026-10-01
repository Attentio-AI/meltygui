"""GNOME history is a virtual location, with owned asynchronous snapshots."""
from concurrent.futures import Future
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, ElementTree

from meltygui.model.file_location_model import (
    BOOKMARK_NS, MIME_NS, RECENT_URI, FileLocation, RecentFiles, read_recent_files,
)


def history(filename, entries):
    root = Element('xbel')
    for uri, modified, private, mime in entries:
        item = SubElement(root, 'bookmark', href=uri, modified=modified)
        info = SubElement(SubElement(item, 'info'), 'metadata')
        SubElement(info, f'{{{MIME_NS}}}mime-type', type=mime)
        if private:
            SubElement(info, f'{{{BOOKMARK_NS}}}private')
    ElementTree(root).write(filename)


def test_gnome_filters_and_modified_order(tmp_path):
    old, new, secret = [tmp_path / name for name in ('old.txt', 'new file.txt', 'private.txt')]
    for p in (old, new, secret):
        p.touch()
    filename = tmp_path / 'recently-used.xbel'
    history(filename, [
        (old.as_uri(), '2026-01-01T00:00:00Z', False, 'text/plain'),
        (new.as_uri(), '2026-02-01T00:00:00Z', False, 'text/plain'),
        (old.as_uri(), '2025-01-01T00:00:00Z', False, 'text/plain'),
        (secret.as_uri(), '2026-03-01T00:00:00Z', True, 'text/plain'),
        (tmp_path.as_uri(), '', False, 'inode/directory'),
        ((tmp_path / 'missing').as_uri(), '', False, 'text/plain'),
        ('sftp://host/file', '', False, 'text/plain'),
    ])
    assert read_recent_files(filename) == [(new, False), (old, False)]


def test_virtual_location_has_no_filesystem_parent():
    recent = FileLocation(RECENT_URI)
    assert recent.is_recent and recent.directory is None and recent.parent is None
    assert FileLocation('/tmp/example').parent == '/tmp'


def test_async_refresh_error_recovery_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    filename = tmp_path / 'recently-used.xbel'
    model = RecentFiles()
    jobs, notifications = [], []
    def submit(fn):
        future = Future()
        jobs.append((fn, future))
        return future
    def finish():
        fn, future = jobs[-1]
        future.set_result(fn())
        model.consume()
    notify = lambda: notifications.append(True)
    model.dispatch(notify, submit)
    assert not model.loaded
    finish()
    assert model.loaded and model.rows == [] and model.error is None
    model.dispatch(notify, submit)
    assert len(jobs) == 1
    filename.write_text('<broken')
    model.dispatch(notify, submit)
    finish()
    assert model.error and model.rows == []
    item = tmp_path / 'file.txt'
    item.touch()
    history(filename, [(item.as_uri(), '2026-01-01T00:00:00Z', False, 'text/plain')])
    model.dispatch(notify, submit)
    assert model.rows == []
    finish()
    assert model.rows == [(item, False)] and model.error is None
    filename.unlink()
    model.dispatch(notify, submit)
    finish()
    assert model.rows == []
    filename.write_text('<xbel/>')
    model.dispatch(notify, submit)
    model.close()
    assert jobs[-1][1].cancelled()
    assert len(notifications) == 4


def test_pending_recent_work_is_not_persisted():
    from meltygui.state.file_state import FileExplorerState
    state = FileExplorerState()
    state._recent_files = RecentFiles()
    assert '_recent_files' not in state.to_dict()
