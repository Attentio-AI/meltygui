"""Name refreshes replace changed files while preserving held lookup snapshots."""
import sys

import pytest

from meltygui.code import symbol_roster as roster


@pytest.fixture
def index(tmp_path, monkeypatch):
    class Project:
        key = (str(tmp_path),)
        allowed = None
        on_owns = None

        def __init__(self):
            self.checked = []

        def owns(self, path, *, canonical_file=False):
            assert canonical_file
            self.checked.append(path)
            if self.on_owns is not None:
                callback, self.on_owns = self.on_owns, None
                callback()
            return self.allowed is None or path in self.allowed

        def resolves(self, path, *, canonical_file=False):
            return True

    project = Project()
    monkeypatch.setattr(sys, '_lsd_symbol_roster', {}, raising=False)
    monkeypatch.setattr(roster, 'analysis_project', lambda project=None, path=None: project)
    state = roster._state()
    record = roster._project_state(project)

    def install(name, text):
        path = str(tmp_path / name)
        table = roster.extract_table(path, text)
        roster._install(state, path, table)
        return path, table

    return project, state, record, install


def test_changed_file_reuses_unchanged_entries_but_checks_every_owner(index):
    project, state, record, install = index
    stable_path, stable = install('stable.py', 'def stable(): pass\n')
    changed_path, _ = install('changed.py', 'def before(): pass\n')
    before = roster._name_indexes(project)

    class AlreadyIndexed(tuple):
        def __iter__(self):
            raise AssertionError('An unchanged file was re-indexed')

    stable.entries = AlreadyIndexed(stable.entries)
    install('changed.py', 'def after(): pass\n')
    project.checked.clear()
    after = roster._name_indexes(project)
    assert project.checked == [stable_path, changed_path]
    assert set(after[0]) == {'stable', 'after'}
    assert after[0]['stable'] is before[0]['stable']
    assert set(before[0]) == {'stable', 'before'}
    assert record['names_tables'][stable_path] is stable


def test_live_override_removal_restores_the_pending_table(index):
    project, state, record, install = index
    path, pending = install('source.py', 'def pending(): pass\n')
    roster._name_indexes(project)
    live = roster.extract_table(path, 'def live(): pass\n')
    state['live'][path] = (live, pending.key)
    roster._gen_bump(state, path)
    held = roster._name_indexes(project)
    assert set(held[0]) == {'live'}
    assert record['names_tables'][path] is live

    state['live'].pop(path)
    roster._gen_bump(state, path)
    restored = roster._name_indexes(project)
    assert set(restored[0]) == {'pending'}
    assert restored[0]['pending'][0] is pending.entries[0]
    assert set(held[0]) == {'live'}


def test_duplicate_names_preserve_ambiguity_and_file_order(index):
    project, state, record, install = index
    first, _ = install('first.py', 'class First:\n    def member(self): pass\ndef shared(): pass\n')
    second, _ = install('second.py', 'class Second:\n    def member(self): pass\ndef shared(): pass\n')
    original = roster._name_indexes(project)
    assert len(original[0]['shared']) == len(original[1]['member']) == 2

    install('first.py', 'class Renamed:\n    def member(self): pass\ndef shared(): pass\n')
    changed = roster._name_indexes(project)
    assert [entry.path for entry in changed[0]['shared']] == [first, second]
    assert [entry.path for entry in changed[1]['member']] == [first, second]

    state['tables'].pop(second)
    roster._gen_bump(state, second)
    unique = roster._name_indexes(project)
    assert [entry.path for entry in unique[0]['shared']] == [first]
    assert [entry.path for entry in unique[1]['member']] == [first]
    assert len(original[0]['shared']) == len(original[1]['member']) == 2


def test_ownership_changes_reindex_the_same_table_identity(index):
    project, state, record, install = index
    first, table = install('first.py', 'def first(): pass\n')
    second, _ = install('second.py', 'def second(): pass\n')
    original = roster._name_indexes(project)
    project.allowed = {second}
    roster._gen_bump(state)
    excluded = roster._name_indexes(project)
    assert set(excluded[0]) == {'second'}
    assert first not in record['names_tables']

    project.allowed = None
    roster._gen_bump(state)
    included = roster._name_indexes(project)
    assert set(included[0]) == {'first', 'second'}
    assert record['names_tables'][first] is table
    assert set(excluded[0]) == {'second'}
    assert set(original[0]) == {'first', 'second'}


def test_an_active_pass_keeps_its_name_snapshot(index):
    project, state, record, install = index
    install('source.py', 'def before(): pass\n')
    with roster.pass_scope():
        snapshot = roster._name_indexes(project)
        install('source.py', 'def after(): pass\n')
        assert roster._name_indexes(project) is snapshot
        assert set(snapshot[0]) == {'before'}
    assert set(roster._name_indexes(project)[0]) == {'after'}
    assert set(snapshot[0]) == {'before'}


def test_existing_record_without_per_file_snapshot_is_adopted(index):
    project, state, record, install = index
    path, _ = install('source.py', 'def before(): pass\n')
    old = roster._name_indexes(project)
    del record['names_tables']
    # A pre-change record can have a warm generation but no incremental state.
    replacement = roster.extract_table(path, 'def current(): pass\n')
    state['tables'][path] = replacement
    assert record['names_gen'] == record['gen']
    current = roster._name_indexes(project)
    assert set(current[0]) == {'current'}
    assert record['names_tables'][path] is replacement
    assert set(old[0]) == {'before'}


def test_install_during_ownership_scan_is_not_marked_already_indexed(index):
    project, state, record, install = index
    install('first.py', 'def first(): pass\n')
    project.on_owns = lambda: install('second.py', 'def second(): pass\n')
    snapshot = roster._name_indexes(project)
    assert set(snapshot[0]) == {'first'}
    assert record['names_gen'] < record['gen']
    assert set(roster._name_indexes(project)[0]) == {'first', 'second'}
