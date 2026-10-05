"""Ownership scales with directories while preserving filesystem semantics."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from meltygui.code import source_context


@pytest.fixture
def ownership(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(source_context, '_ownership_cache', source_context._OwnershipCache())
    monkeypatch.setattr(source_context, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def advance(clock):
    clock.now += source_context.OWNERSHIP_CHECK_SECONDS + 0.01


def test_more_than_4096_canonical_files_share_directory_checks(tmp_path, ownership, monkeypatch):
    root = tmp_path.resolve()
    (root / '.git').mkdir()
    child = root / 'package'
    child.mkdir()
    project = source_context.SourceContext(root)
    paths = [str(child / f'file_{index}.py') for index in range(5000)]
    original_stat = os.stat
    probes = []

    def stat(path, *args, **kwargs):
        probes.append(os.fspath(path))
        return original_stat(path, *args, **kwargs)

    def unexpected_resolve(*args, **kwargs):
        raise AssertionError('Canonical file ownership resolved a source path')

    monkeypatch.setattr(os, 'stat', stat)
    monkeypatch.setattr(Path, 'resolve', unexpected_resolve)
    assert all(project.owns(path, canonical_file=True) for path in paths)
    cold = len(probes)
    assert 0 < cold <= 8  # package's markers and its already shared root
    probes.clear()
    assert all(project.owns(path, canonical_file=True) for path in paths)
    assert project.resolves(str(child / 'newly_indexed.py'), canonical_file=True)
    assert not probes  # also no probe for a new file in a known directory

    advance(ownership)
    assert all(project.owns(path, canonical_file=True) for path in paths)
    assert 0 < len(probes) <= cold  # expiry checks directories once, not 5,000 files


def test_unmarked_directories_keep_their_own_fallback(tmp_path, ownership):
    root = tmp_path.resolve()
    first, second = root / 'first', root / 'second'
    first.mkdir(); second.mkdir()
    assert source_context.owning_root(first / 'a.py', canonical_file=True) == str(first)
    assert source_context.owning_root(second / 'b.py', canonical_file=True) == str(second)
    assert source_context.owning_root(root / 'c.py', canonical_file=True) == str(root)
    assert source_context.owning_root(first) == str(first)


def test_nested_marker_creation_and_removal_refresh_descendants(tmp_path, ownership):
    root = tmp_path.resolve()
    (root / '.git').mkdir()
    nested = root / 'nested'
    child = nested / 'package'
    child.mkdir(parents=True)
    path = child / 'file.py'
    assert source_context.owning_root(path, canonical_file=True) == str(root)
    marker = nested / 'pyproject.toml'
    marker.touch()
    advance(ownership)
    assert source_context.owning_root(path, canonical_file=True) == str(nested)
    marker.unlink()
    advance(ownership)
    assert source_context.owning_root(path, canonical_file=True) == str(root)


def test_marker_symlink_target_can_appear_and_disappear_elsewhere(tmp_path, ownership):
    root = tmp_path.resolve()
    (root / '.git').mkdir()
    child = root / 'child'
    outside = root / 'outside'
    child.mkdir(); outside.mkdir()
    target = outside / 'metadata'
    (child / '.git').symlink_to(target)
    path = child / 'file.py'
    assert source_context.owning_root(path, canonical_file=True) == str(root)
    target.mkdir()
    advance(ownership)
    assert source_context.owning_root(path, canonical_file=True) == str(child)
    target.rmdir()
    advance(ownership)
    assert source_context.owning_root(path, canonical_file=True) == str(root)


def test_generic_directory_and_file_symlink_retargeting(tmp_path, ownership):
    root = tmp_path.resolve()
    a, b = root / 'a', root / 'b'
    for directory in (a, b):
        directory.mkdir()
        (directory / '.git').mkdir()
        (directory / 'file.py').touch()
    alias, file_alias = root / 'alias', root / 'file_alias.py'
    alias.symlink_to(a, target_is_directory=True)
    file_alias.symlink_to(a / 'file.py')
    assert source_context.owning_root(alias / 'file.py') == str(a)
    assert source_context.owning_root(file_alias) == str(a)
    alias.unlink(); alias.symlink_to(b, target_is_directory=True)
    file_alias.unlink(); file_alias.symlink_to(b / 'file.py')
    advance(ownership)
    assert source_context.owning_root(alias / 'file.py') == str(b)
    assert source_context.owning_root(file_alias) == str(b)


def test_generic_symlink_parent_components_keep_resolve_order(tmp_path, ownership):
    root = tmp_path.resolve()
    real = root / 'real'
    child = real / 'child'
    child.mkdir(parents=True)
    (real / '.git').mkdir()
    alias = root / 'alias'
    alias.symlink_to(child, target_is_directory=True)
    assert source_context.owning_root(alias / '..' / 'file.py') == str(real)


def test_relative_paths_follow_cwd_and_home_without_waiting(tmp_path, ownership, monkeypatch):
    root = tmp_path.resolve()
    a, b = root / 'a', root / 'b'
    a.mkdir(); b.mkdir()
    monkeypatch.chdir(a)
    monkeypatch.setenv('HOME', str(a))
    assert source_context.owning_root('file.py') == str(a)
    assert source_context.owning_root('~/file.py') == str(a)
    monkeypatch.chdir(b)
    monkeypatch.setenv('HOME', str(b))
    assert source_context.owning_root('file.py') == str(b)
    assert source_context.owning_root('~/file.py') == str(b)


def test_generic_path_can_change_between_directory_and_file(tmp_path, ownership):
    root = tmp_path.resolve()
    path = root / 'entry'
    path.mkdir()
    assert source_context.owning_root(path) == str(path)
    path.rmdir(); path.touch()
    advance(ownership)
    assert source_context.owning_root(path) == str(root)


def test_generic_path_cache_does_not_flush_after_4096_files(tmp_path, ownership, monkeypatch):
    root = tmp_path.resolve()
    (root / '.git').mkdir()
    paths = [root / f'file_{index}.py' for index in range(4200)]
    assert all(source_context.owning_root(path) == str(root) for path in paths)

    def unexpected_probe(*args, **kwargs):
        raise AssertionError('Warm generic ownership cache repeated filesystem probes')

    monkeypatch.setattr(os, 'stat', unexpected_probe)
    monkeypatch.setattr(os, 'lstat', unexpected_probe)
    monkeypatch.setattr(Path, 'resolve', unexpected_probe)
    assert all(source_context.owning_root(path) == str(root) for path in paths)


def test_skipped_source_directories_remain_excluded(tmp_path, ownership):
    root = tmp_path.resolve()
    (root / '.git').mkdir()
    project = source_context.SourceContext(root)
    assert not project.owns(str(root / '.venv' / 'dependency.py'), canonical_file=True)
    assert not project.owns(str(root / 'other' / '__pycache__' / 'module.py'), canonical_file=True)


def test_one_snapshot_does_not_expire_during_a_slow_scan(tmp_path, ownership, monkeypatch):
    root = tmp_path.resolve()
    (root / '.git').mkdir()
    child = root / 'package'
    child.mkdir()
    project = source_context.SourceContext(root)
    snapshot = project.ownership_snapshot()
    original = os.stat
    probes = []
    def stat(path, *args, **kwargs):
        probes.append(os.fspath(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', stat)
    assert snapshot.owns(str(child / 'first.py'), canonical_file=True)
    cold = len(probes)
    for position in range(100):
        advance(ownership)  # simulate GIL contention beyond the normal TTL
        assert snapshot.owns(str(child / f'file_{position}.py'), canonical_file=True)
    assert len(probes) == cold
    assert project.ownership_snapshot().owns(str(child / 'next.py'), canonical_file=True)
    assert len(probes) > cold


def test_marker_event_invalidates_ancestors_without_expiring_other_directories(tmp_path, ownership, monkeypatch):
    root = tmp_path.resolve()
    (root / '.git').mkdir()
    child = root / 'child'
    child.mkdir()
    path = str(child / 'file.py')
    assert source_context.owning_root(path, canonical_file=True) == str(root)
    (child / 'pyproject.toml').touch()
    source_context.invalidate_ownership(str(child))
    assert source_context.owning_root(path, canonical_file=True) == str(child)
    (child / 'pyproject.toml').unlink()
    source_context.invalidate_ownership(str(child))
    original = os.stat
    probes = []
    def stat(path, *args, **kwargs):
        probes.append(os.fspath(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', stat)
    assert source_context.owning_root(path, canonical_file=True) == str(root)
    assert probes
    assert all(os.path.dirname(path) == str(child) for path in probes)
