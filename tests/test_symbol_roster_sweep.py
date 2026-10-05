"""Source reconciliation never waits for disk/parsing on the drawing thread."""
import sys
import threading
from pathlib import Path

import pytest

from meltygui.code import symbol_roster as roster
from meltygui.core.melty import Melty
from meltygui.editor.pending_save import PendingSave


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(sys, '_lsd_symbol_roster', {}, raising=False)
    monkeypatch.setattr(PendingSave, '_pending_gen', {})
    st = roster._state()
    st['universe_kicked'] = True
    st['test_posts'] = []
    monkeypatch.setattr(Melty, 'post_to_render', st['test_posts'].append)
    yield st
    for thread in threading.enumerate():
        if thread.name == 'source-refresh':
            thread.join(2)
            assert not thread.is_alive()


def test_sweep_coalesces_disk_reads_on_one_worker(state, tmp_path, monkeypatch):
    first, second = (str(tmp_path / name) for name in ('first.py', 'second.py'))
    state['tables'][first] = roster.extract_table(first, 'def old(): pass\n', 0)
    state['tables'][second] = roster.extract_table(second, 'def other(): pass\n', 0)
    started, release = threading.Event(), threading.Event()
    main = threading.get_ident()
    readers = set()

    def key(path):
        readers.add(threading.get_ident())
        assert threading.get_ident() != main, 'Drawing performed a source read'
        started.set()
        assert release.wait(2)
        return 1

    monkeypatch.setattr(roster, '_file_key', key)
    monkeypatch.setattr(roster, '_current_text', lambda p: 'def changed(): pass\n')
    try:
        roster.sweep()
        assert started.wait(1)
        for _ in range(5):
            state.setdefault('dirty_paths', set()).add(second)
            roster.sweep()
        assert state['tables'][first].key == 0
        assert len([t for t in threading.enumerate() if t.name == 'source-refresh']) == 1
    finally:
        release.set()
    for thread in threading.enumerate():
        if thread.name == 'source-refresh':
            thread.join(2)
            assert not thread.is_alive()
    assert state['tables'][first].key == 0  # worker cannot mutate a drawing pass
    assert len(state['test_posts']) == 1
    state['test_posts'].pop()()
    assert len(readers) == 1
    assert state['tables'][first].entries[0].name == 'changed'


def test_force_refresh_stays_synchronous_and_releases_stale_live_hold(state, tmp_path, monkeypatch):
    path = str(tmp_path / 'source.py')
    state['tables'][path] = roster.extract_table(path, 'def disk(): pass\n', 0)
    state['live'][path] = (roster.extract_table(path, 'def unsaved(): pass\n'), 0)
    monkeypatch.setattr(roster, '_file_key', lambda p: 1)
    monkeypatch.setattr(roster, '_current_text', lambda p: 'def saved(): pass\n')
    roster.sweep(force=True)
    assert state['tables'][path].entries[0].name == 'saved'
    assert path not in state['live']
    assert not state.get('sweep_running')


def test_worker_discards_parse_superseded_by_newer_edit(state, tmp_path, monkeypatch):
    path = str(tmp_path / 'source.py')
    state['tables'][path] = roster.extract_table(path, 'def old(): pass\n', 0)
    current = [1, 'def intermediate(): pass\n']
    live = roster.extract_table(path, 'def typing(): pass\n')
    monkeypatch.setattr(roster, '_file_key', lambda p: current[0])
    monkeypatch.setattr(roster, '_current_text', lambda p: current[1])
    extract = roster.extract_table
    def changed_during_parse(*args, **kwargs):
        table = extract(*args, **kwargs)
        current[:] = [2, 'def latest(): pass\n']
        state['live'][path] = (live, 2)
        return table
    monkeypatch.setattr(roster, 'extract_table', changed_during_parse)
    state['frozen'] = 1
    installed = []
    install = roster._install
    def record_install(st, path, table):
        installed.append(table.key)
        install(st, path, table)
    monkeypatch.setattr(roster, '_install', record_install)
    roster.sweep(force=True)
    assert installed == [2]
    assert state['tables'][path].key == 2
    assert state['tables'][path].entries[0].name == 'latest'
    assert state['live'][path][0] is live
    assert not state.get('sweep_running')
    assert not state.get('sweep_pending')


@pytest.mark.parametrize('change', ['table', 'watcher'])
def test_refresh_rechecks_changes_after_its_key_check(state, tmp_path, monkeypatch, change):
    path = str(tmp_path / 'source.py')
    state['tables'][path] = roster.extract_table(path, 'def old(): pass\n', 0)
    latest = roster.extract_table(path, 'def latest(): pass\n', 2)
    current = [1]
    checked, release = threading.Event(), threading.Event()
    reads = []
    def key(p):
        value = current[0]
        reads.append(value)
        if len(reads) == 2:
            checked.set()
            assert release.wait(2)
        return value
    monkeypatch.setattr(roster, '_file_key', key)
    monkeypatch.setattr(roster, '_current_text', lambda p: 'def latest(): pass\n' if current[0] == 2
                        else 'def intermediate(): pass\n')
    worker = threading.Thread(target=roster._pending_table, args=(state, path),
                              kwargs={'allow_frozen': False})
    worker.start()
    try:
        assert checked.wait(1)
        current[0] = 2
        if change == 'table':
            roster._install(state, path, latest)
        else:
            roster._project_file_changed(path)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert state['tables'][path].key == 2
    assert state['tables'][path].entries[0].name == 'latest'
    if change == 'table':
        assert state['tables'][path] is latest


def test_deferred_result_keeps_live_parts_until_render_delivery(state, tmp_path, monkeypatch):
    path = str(tmp_path / 'source.py')
    original = state['tables'][path] = roster.extract_table(path, 'def old(): pass\n', 0)
    live = roster.extract_table(path, 'def typing(): pass\n')
    state['live'][path] = (live, 0)
    parts = state.setdefault('live_parts', {})[path] = {'buffer': object()}
    monkeypatch.setattr(roster, '_file_key', lambda p: 1)
    monkeypatch.setattr(roster, '_current_text', lambda p: 'def saved(): pass\n')
    roster._refresh_sweep_paths(state, {path}, deferred=True)
    assert state['tables'][path] is original
    assert state['live_parts'][path] is parts
    state['test_posts'].pop()()
    assert state['tables'][path].key == 1
    assert path not in state['live']
    assert path not in state['live_parts']


@pytest.mark.parametrize('change', ['table', 'watcher', 'pending', 'cached_text'])
def test_deferred_result_retries_changes_before_render_delivery(state, tmp_path, monkeypatch, change):
    path = str(tmp_path / 'source.py')
    original = state['tables'][path] = roster.extract_table(path, 'def old(): pass\n', 0)
    monkeypatch.setattr(roster, '_file_key', lambda p: 1)
    monkeypatch.setattr(roster, '_current_text', lambda p: 'def stale(): pass\n')
    monkeypatch.setattr(Melty, 'code_cache', {})
    roster._refresh_sweep_paths(state, {path}, deferred=True)
    if change == 'table':
        original = state['tables'][path] = roster.extract_table(path, 'def latest(): pass\n', 2)
    elif change == 'watcher':
        roster._project_file_changed(path)
    elif change == 'pending':
        PendingSave._pending_gen[Path(path)] = 2
    else:
        Melty.code_cache[path] = 'new disk text\n'
    retries = []
    monkeypatch.setattr(roster, '_queue_sweep', lambda st, paths: retries.append(paths))
    state['test_posts'].pop()()
    assert state['tables'][path] is original
    assert retries == [{path}]


def test_large_result_delivery_with_pending_edits_does_not_resolve_paths(state, tmp_path, monkeypatch):
    paths = [str(tmp_path / f'file_{i}.py') for i in range(5000)]
    state['tables'].update((p, roster.extract_table(p, '', 0)) for p in paths)
    monkeypatch.setattr(PendingSave, '_pending_gen', {Path('unresolved/edited.py'): 1})
    monkeypatch.setattr(roster, '_file_key', lambda p: 1)
    monkeypatch.setattr(roster, '_current_text', lambda p: 'def fresh(): pass\n')
    def unexpected_io(*args, **kwargs):
        raise AssertionError('Result delivery performed filesystem work')
    monkeypatch.setattr(Path, 'resolve', unexpected_io)
    monkeypatch.setattr(Path, 'stat', unexpected_io)
    roster._refresh_sweep_paths(state, paths, deferred=True)
    assert len(state['test_posts']) == 1
    state['test_posts'].pop()()
    assert all(state['tables'][p].key == 1 for p in paths)
