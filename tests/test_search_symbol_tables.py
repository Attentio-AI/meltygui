import os
from types import SimpleNamespace
from meltygui import text_index as index


def test_overlay_cache_tracks_repeated_edits_and_deletion(tmp_path, monkeypatch):
    path = tmp_path / 'sample.py'
    path.write_text('def first(): pass\n')
    state = {'seg': SimpleNamespace(paths=[], symbols=[]),
             'dirty': {str(path)}, 'extra': set()}
    monkeypatch.setattr(index, '_state', lambda root: state)
    for name in ('_ensure_segment', '_sweep', '_maybe_rebuild'):
        monkeypatch.setattr(index, name, lambda *args: None)
    monkeypatch.setattr(index, '_pending_gens', lambda: {})
    monkeypatch.setattr(index, '_current_text', lambda p: open(p).read())
    first = index.symbol_tables(tmp_path)
    assert first[1][0][1][0][0] == 'first'
    assert index.symbol_tables(tmp_path)[1] is first[1]
    path.write_text('def second_name(): pass\n')
    second = index.symbol_tables(tmp_path)
    assert second[0] != first[0]
    assert second[1][0][1][0][0] == 'second_name'
    path.unlink()
    assert index.symbol_tables(tmp_path)[1] == []
