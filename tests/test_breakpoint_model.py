"""Breakpoints persist source keys; positions come only from the current parse."""
import pickle

import pytest

from meltygui.code.core_syntax import parse_to_dict, reparse_incremental
from meltygui.model.breakpoint_model import (
    current_breakpoint_index, file_breakpoints, line_has_breakpoint,
    toggle_line_breakpoint,
)
from meltygui.models.file_meta import FileMetaProxy


def parse(text):
    tree = parse_to_dict(text, frontend="scan")
    return tree, current_breakpoint_index(tree, text)


def test_keys_follow_line_shifts_and_unique_statement_permutations():
    source = 'x = 1\ny = 2\n'
    tree, index = parse(source)
    metadata = {}
    assert toggle_line_breakpoint(metadata, '/sample.py', index, 2)
    saved = file_breakpoints(metadata, '/sample.py')
    assert saved == {('y',): {'enabled': True}}
    edited = '\n# heading\ny = 99\nx = 1\n'
    tree = reparse_incremental(tree, edited)
    index = current_breakpoint_index(tree, edited)
    assert index.sites[('y',)].start_line == 3
    assert line_has_breakpoint(index, saved, 3)
    assert not line_has_breakpoint(index, saved, 2)
    assert file_breakpoints(metadata, '/sample.py') is saved
    assert toggle_line_breakpoint(metadata, '/sample.py', index, 3)
    assert file_breakpoints(metadata, '/sample.py') == {}


def test_file_meta_roundtrip_and_two_views_share_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(FileMetaProxy, '_ensure_poller', lambda self: None)
    source = 'def f():\n    result = 1\n    return result\n'
    _, index = parse(source)
    store = FileMetaProxy(tmp_path / 'meta.pkl')
    toggle_line_breakpoint(store, '/sample.py', index, 2)
    store.flush()
    restored = FileMetaProxy(store.path)
    restored.load()
    saved = file_breakpoints(restored, '/sample.py')
    assert saved == {('f', 'locals', 'result'): {'enabled': True}}
    _, other_view = parse(source)
    assert line_has_breakpoint(other_view, saved, 2)
    toggle_line_breakpoint(restored, '/sample.py', other_view, 2)
    restored.flush()
    store.load()
    assert not line_has_breakpoint(index, file_breakpoints(store, '/sample.py'), 2)


def test_multiline_semicolon_and_blank_line_clicks():
    _, index = parse('# comment\n\nx = (\n    1\n)\ny = 2; z = 3\n')
    metadata = {}
    assert not toggle_line_breakpoint(metadata, '/s.py', index, 1)
    assert not toggle_line_breakpoint(metadata, '/s.py', index, 2)
    assert metadata == {}
    toggle_line_breakpoint(metadata, '/s.py', index, 4)
    saved = file_breakpoints(metadata, '/s.py')
    assert saved == {('x',): {'enabled': True}}
    assert line_has_breakpoint(index, saved, 3)
    assert not line_has_breakpoint(index, saved, 4)
    toggle_line_breakpoint(metadata, '/s.py', index, 6)
    assert ('y',) in file_breakpoints(metadata, '/s.py')
    assert ('z',) not in file_breakpoints(metadata, '/s.py')


def test_deleted_key_is_not_rebound_to_its_old_line():
    _, before = parse('x = 1\ny = 2\n')
    metadata = {}
    toggle_line_breakpoint(metadata, '/s.py', before, 2)
    _, after = parse('x = 1\nz = 3\n')
    assert not line_has_breakpoint(after, file_breakpoints(metadata, '/s.py'), 2)
    assert ('y',) in file_breakpoints(metadata, '/s.py')


def test_stale_partial_or_unprepared_parse_is_never_used(monkeypatch):
    text = 'x = 1\n'
    tree, _ = parse(text)
    assert current_breakpoint_index(tree, '\n' + text) is None
    tree.line_offset = 10
    assert current_breakpoint_index(tree, text) is None
    tree.line_offset = 0
    del tree['__origin__']._site_index
    from meltygui.code import melty_scan
    monkeypatch.setattr(melty_scan, 'scan', lambda *a, **k: pytest.fail('render parsed source'))
    assert current_breakpoint_index(tree, text) is None


def test_render_lookups_do_not_parse_or_walk_all_sites(monkeypatch):
    text = ''.join(f'value_{i} = {i}\n' for i in range(1000))
    tree, index = parse(text)
    from meltygui.code import melty_scan
    monkeypatch.setattr(melty_scan, 'scan', lambda *a, **k: pytest.fail('render parsed source'))
    class NoIteration(dict):
        def __iter__(self):
            pytest.fail('render walked all keys')
        def values(self):
            pytest.fail('render walked all sites')
    index.sites = NoIteration(index.sites)
    saved = NoIteration({('value_499',): {'enabled': True}})
    assert current_breakpoint_index(tree, text) is index
    assert index.sites[('value_499',)].start_line == 500
    assert line_has_breakpoint(index, saved, 500)
    assert not line_has_breakpoint(index, saved, 501)


def test_worker_and_cached_parses_bind_to_the_actual_input_snapshot():
    from meltygui.code.new_converters import _bind_parse_source
    text = 'x = 1\n'
    tree = parse_to_dict(text, frontend='worker')
    assert current_breakpoint_index(tree, text) is not None
    restored = pickle.loads(pickle.dumps(tree))
    assert current_breakpoint_index(restored, text) is None
    _bind_parse_source({'code_dict': restored}, text)
    assert current_breakpoint_index(restored, text) is not None


def test_local_metadata_edits_notify_cached_views_once_on_render_thread(tmp_path, monkeypatch):
    from meltygui.core.melty import Melty
    queued, repainted = [], []
    monkeypatch.setattr(FileMetaProxy, '_ensure_poller', lambda self: None)
    monkeypatch.setattr(Melty, 'post_to_render', queued.append)
    monkeypatch.setattr(FileMetaProxy, '_repaint', lambda self: repainted.append(self))
    store = FileMetaProxy(tmp_path / 'meta.pkl')
    _, index = parse('x = 1\ny = 2\n')
    toggle_line_breakpoint(store, '/s.py', index, 1)
    toggle_line_breakpoint(store, '/s.py', index, 2)
    assert len(queued) == 1
    assert not repainted
    queued.pop()()
    assert repainted == [store]
    toggle_line_breakpoint(store, '/s.py', index, 2)
    assert len(queued) == 1
    store.flush()
