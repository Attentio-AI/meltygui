"""tint_model: tints read once through melty_meta, refreshed after other writers, saved off thread."""
import os
import subprocess
import sys
from types import SimpleNamespace

import melty_meta
import pytest


@pytest.fixture
def folder(tmp_path):
    (tmp_path / 'repo').mkdir()
    (tmp_path / 'repo' / 'main.py').write_text('')
    return tmp_path / 'repo'


@pytest.fixture
def repaints(tints, monkeypatch):
    calls = []
    monkeypatch.setattr(tints, '_repaint', lambda: calls.append(True))
    return calls


def _other_process(path, colour):
    """Paint `path` from another interpreter, as another app would."""
    subprocess.run([sys.executable, '-c', 'import sys, melty_meta; melty_meta.set_tint(sys.argv[1], eval(sys.argv[2]))',
                    str(path), repr(colour)], check=True, close_fds=False, env=os.environ.copy())


def test_reads_painted_unpainted_and_transparent(tints, folder):
    melty_meta.set_tint(folder, (0.2, 0.5, 0.3))
    melty_meta.set_tint(folder / 'main.py', (0.8, 0.3, 0.2, 1.0))
    melty_meta.put(folder / 'README.md', 'tint', [0.1, 0.1, 0.1, 0])
    assert tints.tint(folder) == (0.2, 0.5, 0.3)
    assert tints.tint(str(folder / 'main.py')) == (0.8, 0.3, 0.2, 1.0)
    assert tints.tint(folder / 'README.md') is None
    assert tints.tint(folder / 'missing.txt') is None


def test_second_read_is_memory_only(tints, folder, monkeypatch):
    melty_meta.set_tint(folder, (0.2, 0.5, 0.3))
    assert tints.tint(folder) == (0.2, 0.5, 0.3)

    def disk(*_):
        raise AssertionError('read the disk again')

    monkeypatch.setattr(melty_meta, 'tint', disk)
    monkeypatch.setattr(melty_meta, 'load', disk)
    monkeypatch.setattr(tints, '_stamp', disk)
    assert tints.tint(folder) == (0.2, 0.5, 0.3)


def test_another_process_write_refreshes_and_repaints(tints, folder, repaints):
    assert tints.tint(folder / 'main.py') is None
    assert tints.tint(folder) is None
    tints._check()
    assert not repaints
    _other_process(folder / 'main.py', (0.3, 0.4, 0.9))
    assert tints.tint(folder / 'main.py') is None            # held until the watcher sees it
    tints._check()
    assert repaints
    assert tints.tint(folder / 'main.py') == (0.3, 0.4, 0.9)
    repaints.clear()
    tints._check()
    assert not repaints


def test_global_store_change_refreshes_everything(tints, tmp_path, repaints):
    other = tmp_path / 'other'
    other.mkdir()
    assert tints.tint(other) is None
    melty_meta.keep_out(other)                               # its tint now lives in the global store
    _other_process(other, (0.5, 0.5, 0.1))
    assert not (other / '.melty').exists()
    tints._check()
    assert repaints and tints.tint(other) == (0.5, 0.5, 0.1)


def test_write_shows_at_once_and_a_drag_saves_once(tints, folder, repaints, monkeypatch):
    saved = []
    real = melty_meta.set_tint
    monkeypatch.setattr(melty_meta, 'set_tint', lambda path, value: saved.append(value) or real(path, value))
    for red in (0.1, 0.2, 0.30001):
        tints.set_tint(folder / 'main.py', (red, 0.4, 0.6))
    assert tints.tint(folder / 'main.py') == (0.3, 0.4, 0.6)
    assert repaints and not saved
    tints.flush()
    assert saved == [(0.30001, 0.4, 0.6)]
    assert melty_meta.tint(folder / 'main.py') == (0.3, 0.4, 0.6)
    assert tints.tint(folder / 'main.py') == (0.3, 0.4, 0.6)
    tints.set_tint(folder / 'main.py', (0.3, 0.4, 0.6))       # what is already there: nothing to save
    tints.flush()
    assert len(saved) == 1


def test_unpaint_removes_the_file(tints, folder):
    tints.set_tint(folder, (0.2, 0.5, 0.3))
    tints.flush()
    assert (folder / '.melty').exists()
    tints.set_tint(folder, None)
    assert tints.tint(folder) is None
    tints.flush()
    assert not (folder / '.melty').exists()


def test_failed_save_is_reported_and_shows_disk(tints, folder, monkeypatch):
    import meltygui.core.diagnostics.notifications as notifications
    shown = []
    monkeypatch.setattr(notifications, 'notify', lambda text, **kwargs: shown.append(text))
    (folder / '.melty').write_text('{damaged')
    tints.set_tint(folder / 'main.py', (0.9, 0.1, 0.1))
    assert tints.tint(folder / 'main.py') == (0.9, 0.1, 0.1)
    tints.flush()
    assert len(shown) == 1 and 'main.py' in shown[0]
    assert tints.tint(folder / 'main.py') is None
    assert (folder / '.melty').read_text() == '{damaged'


def test_codec_layer_reads_and_writes_tint_model(tints, folder, monkeypatch):
    import meltygui.models.file_meta as file_meta
    from meltygui.code.new_codecs import Codec
    store = {str(folder / 'main.py'): file_meta.FileMeta({'tint': (0.1, 0.1, 0.1), 'icon': 'x'})}
    monkeypatch.setattr(file_meta, 'file_meta_store', lambda: store)
    ds = SimpleNamespace(_file_meta=str(folder / 'main.py'))
    assert Codec.file_tint(ds) is None                       # the old entry's tint is stale
    assert Codec.update_file_meta(ds, 'tint', (0.4, 0.5, 0.6))
    assert Codec.file_tint(ds) == (0.4, 0.5, 0.6)
    assert dict.__getitem__(store[str(folder / 'main.py')], 'tint') == (0.1, 0.1, 0.1)
    assert Codec.update_file_meta(ds, 'icon', 'y') and store[str(folder / 'main.py')]['icon'] == 'y'
    tints.flush()
    assert melty_meta.tint(folder / 'main.py') == (0.4, 0.5, 0.6)
