"""Normalized source sweeps keep warm reads cheap and external edits fresh."""
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys

from meltygui.code import symbol_roster as roster
from meltygui.core.melty import FileWatch, Melty
from meltygui.editor.external_changes import ExternalChanges
from meltygui.editor.pending_save import PendingSave
from meltygui.editor import text_editor


def test_source_read_is_utf8_even_when_launcher_uses_ascii(tmp_path):
    path = tmp_path / 'source.py'
    source = '# Editor \u2014 settings\nlabel = "caf\u00e9"\n'
    path.write_text(source, encoding='utf-8')
    program = '''
import locale
from pathlib import Path
import sys
from meltygui.core.melty import Melty
from meltygui.editor.pending_save import PendingSave
assert not sys.flags.utf8_mode
if sys.platform != 'win32':
    assert locale.getencoding().lower() in ('ascii', 'us-ascii', 'ansi_x3.4-1968')
path = Path(sys.argv[1])
expected = path.read_text(encoding='utf-8')
text = PendingSave.current_file_text(str(path))
assert text == expected, repr(text)
assert Melty.read_code(str(path)) is text
'''
    env = dict(os.environ, LC_ALL='C', LANG='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0')
    result = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', program, str(path)],
                            env=env, close_fds=False, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_large_cached_source_sweep_does_not_resolve_files(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    cached = {str(root / f'file_{i}.py'): f'value = {i}\n' for i in range(5000)}
    monkeypatch.setattr(Melty, 'code_cache', cached)
    first = next(iter(cached))
    monkeypatch.setattr(PendingSave, '_pending_gen', {Path(first): 2})
    monkeypatch.setattr(text_editor, '_PENDING_GEN_MAP', (None, {}))
    assert text_editor._pending_gen_of(first, canonical_file=True) == 2

    def unexpected_probe(*args, **kwargs):
        raise AssertionError('Warm source sweep repeated filesystem resolution')
    monkeypatch.setattr(Path, 'resolve', unexpected_probe)
    monkeypatch.setattr(Path, 'stat', unexpected_probe)
    for _ in range(2):
        assert [roster._file_key(path) for path in cached] == [
            (id(text), 2 if path == first else 0) for path, text in cached.items()]


def test_canonical_reads_see_watcher_invalidations_without_resolving_again(tmp_path, monkeypatch):
    path = tmp_path.resolve() / 'source.py'
    path.write_text('before = 1\n')
    key = str(path)
    monkeypatch.setattr(Melty, 'code_cache', {})
    monkeypatch.setattr(Melty, 'frame_count', 2)
    monkeypatch.setattr(PendingSave, 'pending_saves', {})
    monkeypatch.setattr(PendingSave, '_pending_gen', {})
    monkeypatch.setattr(FileWatch, 'project_tracked', set())
    monkeypatch.setattr(FileWatch, 'global_listeners', [])
    monkeypatch.setattr(FileWatch, 'path_to_draw_states', {})
    baselines = []
    monkeypatch.setattr(ExternalChanges, 'on_file_event', lambda p, text: baselines.append((p, text)))
    before = Melty.read_code(key, canonical_file=True)

    def unexpected_resolve(*args, **kwargs):
        raise AssertionError('Canonical source key was resolved twice')
    monkeypatch.setattr(Path, 'resolve', unexpected_resolve)
    assert PendingSave.current_file_text(key, canonical_file=True) is before
    path.write_text('after = 2\n')
    FileWatch._on_event(SimpleNamespace(src_path=key))
    after = PendingSave.current_file_text(key, canonical_file=True)
    assert after == 'after = 2\n'
    assert after is not before
    assert baselines == [(key, before)]
    assert roster._file_key(key) == (id(after), 0)


def test_resolution_is_shared_within_pass_but_symlinks_refresh_next_pass(tmp_path, monkeypatch):
    first, second = tmp_path / 'first.py', tmp_path / 'second.py'
    first.touch()
    second.touch()
    link = tmp_path / 'linked.py'
    link.symlink_to(first)
    expected_first, expected_second = str(first.resolve()), str(second.resolve())
    monkeypatch.setattr(sys, '_lsd_symbol_roster', {}, raising=False)
    calls = []
    resolve = Path.resolve
    def counted(path, *args, **kwargs):
        calls.append(str(path))
        return resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', counted)
    with roster.pass_scope():
        assert roster._norm(link) == expected_first
        with roster.pass_scope():
            for _ in range(20):
                assert roster._norm(link) == expected_first
                assert roster._norm(expected_first) == expected_first
    assert calls == [str(link)]
    link.unlink()
    link.symlink_to(second)
    with roster.pass_scope():
        assert roster._norm(link) == expected_second
    assert calls == [str(link), str(link)]
