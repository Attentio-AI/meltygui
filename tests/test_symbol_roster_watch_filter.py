"""Generated files must not restart project analysis or repaint its editors."""
import sys

import pytest

from meltygui.code import source_context, symbol_roster as roster


@pytest.fixture
def watched(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, '_lsd_symbol_roster', {}, raising=False)
    project = source_context.SourceContext(tmp_path)
    state = roster._state()
    record = roster._project_state(project)
    record['universe'], record['complete'] = ('source.py',), True
    notifications = []
    monkeypatch.setattr(roster, '_schedule_notify', lambda: notifications.append(True))
    state['consumers'][object()] = 0
    return state, record, notifications


@pytest.mark.parametrize('directory', ['build', 'dist', '__pycache__', '.venv'])
def test_generated_source_burst_does_not_invalidate(watched, tmp_path, directory):
    state, record, notifications = watched
    before = state['gen'], record['gen'], record['universe']
    for i in range(300):
        roster._project_file_changed(str(tmp_path / 'platforms' / directory / 'pkg' / f'file{i}.py'))
    assert (state['gen'], record['gen'], record['universe']) == before
    assert record['complete']
    assert not state.get('dirty_paths')
    assert not state.get('path_generations')
    assert not notifications


def test_generated_project_marker_does_not_invalidate_ownership(watched, tmp_path, monkeypatch):
    state, record, notifications = watched
    changes = []
    monkeypatch.setattr(source_context, 'invalidate_ownership', changes.append)
    roster._project_file_changed(str(tmp_path / 'build' / 'pkg' / 'pyproject.toml'))
    assert not changes and not notifications
    assert record['complete']


@pytest.mark.parametrize('reason', ['table', 'live', 'observed', 'nested_project', 'import_root'])
def test_explicitly_used_generated_source_still_updates(watched, tmp_path, reason):
    state, record, notifications = watched
    path = str(tmp_path / 'build' / 'pkg' / 'source.py')
    if reason == 'table':
        state['tables'][path] = roster.extract_table(path, 'def existing(): pass\n')
    elif reason == 'live':
        state['live'][path] = (roster.extract_table(path, 'def editing(): pass\n'), None)
    elif reason == 'observed':
        record['observed'].add(path)
    elif reason == 'nested_project':
        roster._project_state(source_context.SourceContext(tmp_path / 'build' / 'pkg'))
    else:
        record['project'].import_paths += (str(tmp_path / 'build' / 'pkg'),)
    before = state['gen']
    roster._project_file_changed(path)
    assert state['gen'] == before + 1
    assert path in state['dirty_paths']
    assert notifications


@pytest.mark.parametrize('relative', ['src/new.py', 'src/removed.pyi', '.venv/pyvenv.cfg', '.venv/site-packages/editable.pth'])
def test_source_and_environment_changes_still_update(watched, tmp_path, relative):
    state, record, notifications = watched
    path = str(tmp_path / relative)
    before = state['gen']
    roster._project_file_changed(path)
    assert state['gen'] == before + 1
    assert path in state['dirty_paths']
    assert notifications
