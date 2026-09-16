"""A picker must keep its watches when inotify fails at startup or navigation."""
import errno
from pathlib import Path
import threading

import pytest
from watchdog.observers.api import BaseObserver, EventEmitter
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

from meltygui.core.melty import FileWatch


class LimitedEmitter(EventEmitter):
    remaining = 0
    error = errno.EMFILE

    def on_thread_start(self):
        if self.remaining == 0:
            raise OSError(self.error, 'native watch limit')
        type(self).remaining -= 1

    def queue_events(self, timeout):
        self.stopped_event.wait(timeout)


@pytest.fixture
def watches(monkeypatch):
    observer = BaseObserver(LimitedEmitter)
    monkeypatch.setattr(FileWatch, 'observer', observer)
    monkeypatch.setattr(FileWatch, 'handler', FileSystemEventHandler())
    monkeypatch.setattr(FileWatch, '_watched_dirs', set())
    monkeypatch.setattr(FileWatch, '_recursive_roots', set())
    monkeypatch.setattr(FileWatch, '_dir_watches', {})
    monkeypatch.setattr(LimitedEmitter, 'remaining', 0)
    monkeypatch.setattr(LimitedEmitter, 'error', errno.EMFILE)
    arrived = threading.Event()
    paths = []

    def changed(event):
        paths.append(event.src_path)
        if Path(event.src_path).name == 'created.py':
            arrived.set()

    monkeypatch.setattr(FileWatch, '_on_event', changed)
    yield observer, arrived, paths
    for current in (observer, FileWatch.observer):
        current.stop()
        if current.is_alive():
            current.join(timeout=3)


@pytest.mark.parametrize('error', [errno.EMFILE, errno.ENFILE, errno.ENOSPC])
def test_queued_watch_start_falls_back_and_delivers_events(watches, tmp_path, monkeypatch, error):
    previous, arrived, paths = watches
    monkeypatch.setattr(LimitedEmitter, 'error', error)
    assert FileWatch.watch_recursive(str(tmp_path))
    FileWatch.start()
    assert isinstance(FileWatch.observer, PollingObserver)
    assert not previous.emitters
    active = FileWatch.observer
    FileWatch.start()
    assert FileWatch.observer is active
    created = tmp_path / 'created.py'
    created.touch()
    assert arrived.wait(3), paths
    assert str(created) in paths


def test_partial_start_stops_started_emitters_and_restores_all_watches(watches, tmp_path, monkeypatch):
    previous, _, _ = watches
    monkeypatch.setattr(LimitedEmitter, 'remaining', 1)
    directories = [tmp_path / 'one', tmp_path / 'two']
    for path in directories:
        path.mkdir()
        assert FileWatch.watch_dir(str(path))
    emitters = tuple(previous.emitters)
    FileWatch.start()
    assert isinstance(FileWatch.observer, PollingObserver)
    assert all(not emitter.is_alive() for emitter in emitters)
    assert set(FileWatch._dir_watches) == {str(path) for path in directories}
    assert len(FileWatch.observer.emitters) == 2
    FileWatch.unwatch_dir(str(directories[0]))
    assert len(FileWatch.observer.emitters) == 1


@pytest.mark.parametrize('recursive', [False, True])
def test_new_watch_on_running_observer_falls_back(watches, tmp_path, recursive):
    previous, arrived, _ = watches
    FileWatch.start()
    register = FileWatch.watch_recursive if recursive else FileWatch.watch_dir
    assert register(str(tmp_path))
    assert isinstance(FileWatch.observer, PollingObserver)
    assert not previous.is_alive()
    (tmp_path / 'created.py').touch()
    assert arrived.wait(3)


def test_unrelated_startup_error_is_not_disguised_as_resource_exhaustion(watches, tmp_path, monkeypatch):
    previous, _, _ = watches
    monkeypatch.setattr(LimitedEmitter, 'error', errno.EACCES)
    FileWatch.watch_dir(str(tmp_path))
    with pytest.raises(OSError) as error:
        FileWatch.start()
    assert error.value.errno == errno.EACCES
    assert FileWatch.observer is previous
