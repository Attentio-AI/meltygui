"""Drawing consumes name snapshots without scanning or probing source files."""
import os
import sys
import threading
import time

import pytest

from meltygui.code import source_context, symbol_roster as roster
from meltygui.core.melty import Melty
from meltygui.core.windowing import glfw_utils


@pytest.fixture
def index(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / 'pyproject.toml').touch()
    project = source_context.SourceContext(root)
    monkeypatch.setattr(source_context, '_ownership_cache', source_context._OwnershipCache())
    monkeypatch.setattr(sys, '_lsd_symbol_roster', {}, raising=False)
    monkeypatch.setattr(roster, 'analysis_project', lambda project=None, path=None: project)
    monkeypatch.setattr(Melty, '_frame_draw_start', 0.0, raising=False)
    monkeypatch.setattr(glfw_utils, '_render_thread_id', threading.get_ident())
    posts = []
    monkeypatch.setattr(Melty, 'post_to_render', posts.append)
    # The fake frame stays open until the test delivers its callback. Parking
    # itself is covered by runtime tests; these tests control worker barriers.
    from meltygui.code import libcst_conversion
    monkeypatch.setattr(libcst_conversion, '_park_while_frame', lambda: None)
    state = roster._state()
    record = roster._project_state(project)
    yield project, state, record, posts
    join_workers()


def join_workers():
    for thread in threading.enumerate():
        if thread.name == 'symbol-names':
            thread.join(2)
            assert not thread.is_alive()


def install(state, path, name):
    table = roster.extract_table(str(path), f'def {name}(): pass\n')
    roster._install(state, str(path), table)
    return table


def test_expired_large_corpus_is_only_scanned_by_one_worker(index, monkeypatch):
    project, state, record, posts = index
    first = os.path.join(project.root, 'first.py')
    install(state, first, 'before')
    held = roster._name_indexes(project)
    for position in range(5000):
        path = os.path.join(project.root, f'file_{position}.py')
        state['tables'][path] = roster.FileTable(path, 0, [], {}, [], 1)
    install(state, first, 'after')
    record['names_checked_at'] = -float('inf')
    entered, release = threading.Event(), threading.Event()
    main = threading.get_ident()
    original_owns = source_context.OwnershipSnapshot.owns
    checked = []

    def owns(self, path, **kwargs):
        assert threading.get_ident() != main, 'Drawing scanned project ownership'
        checked.append(path)
        entered.set()
        assert release.wait(2)
        return original_owns(self, path, **kwargs)

    monkeypatch.setattr(source_context.OwnershipSnapshot, 'owns', owns)
    original_stat = os.stat
    def stat(path, *args, **kwargs):
        assert threading.get_ident() != main, 'Drawing probed the filesystem'
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', stat)
    Melty._frame_draw_start = time.monotonic()
    try:
        with roster.pass_scope():
            assert roster._name_indexes(project) is held
            assert entered.wait(1)
            for _ in range(5):
                assert roster._name_indexes(project) is held
            assert len([t for t in threading.enumerate() if t.name == 'symbol-names']) == 1
            release.set()
            join_workers()
            assert record['names'] is held  # worker cannot publish mid-draw
            assert len(posts) == 1
            posts.pop()()
            assert roster._name_indexes(project) is held  # existing pass stays frozen
        assert set(roster._name_indexes(project)[0]) == {'after'}
        assert set(held[0]) == {'before'}
        assert len(checked) == 5001
    finally:
        release.set()
        Melty._frame_draw_start = 0.0


@pytest.mark.parametrize('change', ['install', 'live', 'delete'])
def test_source_change_before_delivery_rejects_old_names(index, monkeypatch, change):
    project, state, record, posts = index
    path = os.path.join(project.root, 'source.py')
    install(state, path, 'old')
    held = roster._name_indexes(project)
    install(state, path, 'intermediate')
    result = roster._prepare_name_indexes(state, record)
    if change == 'install':
        install(state, path, 'latest')
    elif change == 'live':
        state['live'][path] = (roster.extract_table(path, 'def typing(): pass\n'), 0)
        roster._gen_bump(state, path)
    else:
        state['tables'].pop(path)
        roster._gen_bump(state, path)
    state['names_results'] = {id(record): (record, result)}
    retries = []
    monkeypatch.setattr(roster, '_queue_name_indexes', lambda st, rec: retries.append(rec))
    roster._apply_name_indexes(state)
    assert record['names'] is held
    assert retries == [record]
    expected = {'install': {'latest'}, 'live': {'typing'}, 'delete': set()}[change]
    assert set(roster._name_indexes(project)[0]) == expected


@pytest.mark.parametrize('marker', ['pyproject.toml', '.git'])
def test_marker_changes_before_delivery_invalidate_ownership(index, monkeypatch, marker):
    project, state, record, _ = index
    nested = os.path.join(project.root, 'nested')
    os.mkdir(nested)
    path = os.path.join(nested, 'source.py')
    install(state, path, 'nested_name')
    assert set(roster._name_indexes(project)[0]) == {'nested_name'}
    result = roster._prepare_name_indexes(state, record)
    marker_path = os.path.join(nested, marker)
    if marker == '.git':
        os.mkdir(marker_path)
    else:
        open(marker_path, 'w').close()
    roster._project_file_changed(marker_path)
    state['names_results'] = {id(record): (record, result)}
    retries = []
    monkeypatch.setattr(roster, '_queue_name_indexes', lambda st, rec: retries.append(rec))
    roster._apply_name_indexes(state)
    assert retries == [record]
    Melty._frame_draw_start = time.monotonic()
    assert not roster._name_indexes(project)[0]  # old project scope is unavailable immediately
    Melty._frame_draw_start = 0.0
    assert not roster._name_indexes(project)[0]

    result = roster._prepare_name_indexes(state, record)
    os.rmdir(marker_path) if marker == '.git' else os.unlink(marker_path)
    roster._project_file_changed(marker_path)
    state['names_results'] = {id(record): (record, result)}
    roster._apply_name_indexes(state)
    assert set(roster._name_indexes(project)[0]) == {'nested_name'}


def test_another_threads_frame_does_not_make_explicit_lookup_async(index):
    project, state, record, posts = index
    install(state, os.path.join(project.root, 'source.py'), 'available')
    Melty._frame_draw_start = time.monotonic()
    result = []
    thread = threading.Thread(target=lambda: result.append(roster._name_indexes(project)))
    thread.start(); thread.join(2)
    assert not thread.is_alive()
    assert set(result[0][0]) == {'available'}
    assert not posts


@pytest.mark.parametrize('remove', [False, True])
def test_marker_event_during_scan_rejects_the_inflight_snapshot(index, monkeypatch, remove):
    project, state, record, posts = index
    nested = os.path.join(project.root, 'nested')
    os.mkdir(nested)
    marker = os.path.join(nested, '.git')
    if remove:
        os.mkdir(marker)
    install(state, os.path.join(nested, 'source.py'), 'nested_name')
    before = roster._name_indexes(project)
    record['names_checked_at'] = -float('inf')
    entered, release = threading.Event(), threading.Event()
    owns = source_context.OwnershipSnapshot.owns

    def pause(self, path, **kwargs):
        value = owns(self, path, **kwargs)
        entered.set()
        assert release.wait(2)
        return value

    monkeypatch.setattr(source_context.OwnershipSnapshot, 'owns', pause)
    Melty._frame_draw_start = time.monotonic()
    try:
        assert roster._name_indexes(project) is before
        assert entered.wait(1)
        os.rmdir(marker) if remove else os.mkdir(marker)
        roster._project_file_changed(marker)
    finally:
        release.set()
        Melty._frame_draw_start = 0.0
    join_workers()
    retries = []
    monkeypatch.setattr(roster, '_queue_name_indexes', lambda st, rec: retries.append(rec))
    posts.pop()()
    assert record['names'] is before
    assert retries == [record]
    assert set(roster._name_indexes(project)[0]) == ({'nested_name'} if remove else set())


def test_publish_retains_ownership_sample_time(index, monkeypatch):
    project, state, record, _ = index
    install(state, os.path.join(project.root, 'source.py'), 'available')
    result = roster._prepare_name_indexes(state, record)
    state['names_results'] = {id(record): (record, result)}
    monkeypatch.setattr(roster.time, 'monotonic', lambda: result[-1] + 10)
    roster._apply_name_indexes(state)
    assert record['names_checked_at'] == result[-1]
    assert roster._names_need_refresh(record, project.ownership_revision, result[-1] + 10)


def test_generation_invalidation_never_probes_ownership(index, monkeypatch):
    project, state, record, _ = index
    def unexpected_ownership(*args, **kwargs):
        raise AssertionError('Publishing a table checked project ownership')
    monkeypatch.setattr(project, 'resolves', unexpected_ownership)
    monkeypatch.setattr(project, 'owns', unexpected_ownership)
    before = record['gen']
    install(state, os.path.join(project.root, 'source.py'), 'available')
    assert record['gen'] == before + 1
    roster._project_file_changed(os.path.join(project.root, 'source.py'))
    assert record['gen'] == before + 2
