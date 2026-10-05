"""Project fallback names stay local without probing every source file."""
import sys
from pathlib import Path

from meltygui.code import source_context, symbol_roster as roster


def test_names_rebuild_reuses_directory_ownership_and_live_tables(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / 'pyproject.toml').touch()
    nested = root / 'nested'
    nested.mkdir()
    (nested / 'pyproject.toml').touch()
    project = source_context.SourceContext(root)
    monkeypatch.setattr(roster, 'analysis_project', lambda project=None, path=None: project)
    monkeypatch.setattr(sys, '_lsd_symbol_roster', {}, raising=False)
    state = roster._state()
    first = str(root / 'first.py')
    child = str(nested / 'child.py')
    state['tables'][first] = roster.extract_table(first, 'def original(): pass\n')
    state['tables'][child] = roster.extract_table(child, 'def nested_only(): pass\n')
    names, _ = roster._name_indexes(project)
    assert set(names) == {'original'}

    # Colour refreshes see new/live source while the directory cache is warm.
    live = roster.extract_table(first, 'def edited(): pass\n')
    state['live'][first] = (live, None)
    second = str(root / 'second.py')
    state['tables'][second] = roster.extract_table(second, 'def added(): pass\n')
    roster._project_state(project)['gen'] += 1

    def unexpected_probe(*args, **kwargs):
        raise AssertionError('Name refresh repeated filesystem ownership probes')
    monkeypatch.setattr(Path, 'stat', unexpected_probe)
    monkeypatch.setattr(Path, 'resolve', unexpected_probe)
    names, _ = roster._name_indexes(project)
    assert set(names) == {'edited', 'added'}
    assert names['edited'][0] is live.entries[0]
