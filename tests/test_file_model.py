"""File-tree adapter behavior must survive the model/view/core move."""
from pathlib import Path
from types import SimpleNamespace

from meltygui.model import file_model
from meltygui.core.runtime.toggles import Toggles


def test_move_between_folders_and_undo_preserves_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(Toggles.FileSafety, 'block_file_delete', False)
    source = tmp_path / 'a'
    target = tmp_path / 'z'
    source.mkdir()
    target.mkdir()
    original = source / 'note.txt'
    original.write_text('keep these contents')
    disk = file_model._scan(tmp_path)
    held, seen = {}, set()
    file_model._reconcile(held, disk, tmp_path, seen)
    held['z']['note.txt'] = held['a'].pop('note.txt')
    file_model._reconcile(held, disk, tmp_path, seen)
    assert (target / 'note.txt').read_text() == 'keep these contents'
    assert not original.exists()
    held['a']['note.txt'] = original  # undo restores the original, now stale Path
    del held['z']['note.txt']
    file_model._reconcile(held, disk, tmp_path, seen)
    assert original.read_text() == 'keep these contents'
    assert not (target / 'note.txt').exists()


def test_roots_reconcile_independently_and_keep_tree_identity(tmp_path):
    roots = [tmp_path / 'one', tmp_path / 'two']
    states = []
    for root in roots:
        root.mkdir()
        (root / 'first.txt').write_text(root.name)
        held, seen = {}, set()
        disk = file_model._scan(root)
        file_model._reconcile(held, disk, root, seen)
        states.append((held, seen))
    first, seen = states[0]
    (roots[0] / 'second.txt').write_text('new')
    (roots[0] / 'first.txt').unlink()
    file_model._reconcile(first, file_model._scan(roots[0]), roots[0], seen)
    assert states[0][0] is first
    assert set(first) == {'second.txt'}
    assert set(states[1][0]) == {'first.txt'}
    assert states[1][0]['first.txt'] == roots[1] / 'first.txt'


def test_create_and_delete_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(Toggles.FileSafety, 'block_file_delete', True)
    held, disk, seen = {'new.txt': 'created from a dict edit'}, {}, set()
    file_model._reconcile(held, disk, tmp_path, seen)
    path = tmp_path / 'new.txt'
    assert path.read_text() == 'created from a dict edit'
    assert held['new.txt'] == path
    del held['new.txt']
    file_model._reconcile(held, disk, tmp_path, seen)
    assert path.exists()
    file_model._reconcile(held, file_model._scan(tmp_path), tmp_path, seen)
    assert held['new.txt'] == path


def test_metadata_order_and_tint_round_trip(tmp_path, tints):
    import melty_meta
    for name in 'abc':
        (tmp_path / name).write_text('')
    melty_meta.set_tint(tmp_path / 'b', (0.2, 0.4, 0.6, 1.0))
    held = {'a': tmp_path / 'a', 'b': tmp_path / 'b', 'c': tmp_path / 'c'}
    # The old store's `tint` is stale: the tint comes from the `.melty`.
    metadata = {str(tmp_path / 'b'): {'order': 0},
                str(tmp_path / 'a'): {'order': 1, 'tint': (0.9, 0.9, 0.9)}}
    assert file_model._apply_meta(held, tmp_path, metadata)
    assert [key for key in held if key != '__overrides__'] == ['b', 'a', 'c']
    assert held['__overrides__']['__b__']['tint'] == (0.2, 0.4, 0.6, 1.0)
    assert '__a__' not in held['__overrides__']
    held['__overrides__']['__c__'] = {'tint': (0.7, 0.1, 0.1)}     # painted in the tree
    collected = {str(tmp_path / 'a'): {'order': 1, 'tint': (0.9, 0.9, 0.9)}}
    file_model._collect_meta(held, tmp_path, collected)
    assert collected[str(tmp_path / 'b')] == {'order': 0}
    assert collected[str(tmp_path / 'a')]['tint'] == (0.9, 0.9, 0.9)   # left as it was
    assert 'tint' not in collected[str(tmp_path / 'c')]
    tints.flush()
    assert melty_meta.tint(tmp_path / 'c') == (0.7, 0.1, 0.1)
    assert melty_meta.tint(tmp_path / 'b') == (0.2, 0.4, 0.6, 1.0)
    # FileMeta adds its default icon field when the plain mapping becomes a model.
    file_model._apply_meta(held, tmp_path, collected)
    assert not file_model._apply_meta(held, tmp_path, collected)
    del held['__overrides__']['__c__']['tint']                         # unpainted in the tree
    file_model._collect_meta(held, tmp_path, collected)
    tints.flush()
    assert melty_meta.tint(tmp_path / 'c') is None


def test_tree_renderer_preserves_mutation_result(monkeypatch):
    from meltygui.view import file_view
    held = {}

    def edit(value, **kwargs):
        value['new.txt'] = 'new file'
        return True, value

    monkeypatch.setattr(file_view.RenderFuncs, 'draw_collection', edit)
    changed, returned = file_view.draw_file_tree.__wrapped__(
        held, SimpleNamespace(content_width=400), Path('/tmp/files'))
    assert changed and returned is held
    assert held['new.txt'] == 'new file'


def test_file_runtime_owns_the_public_entry_point_and_hosts():
    import meltygui
    from meltygui.core.files import file_core
    assert meltygui.draw_folder_files is file_core.draw_folder_files
    assert file_core._proxies[file_core.ROOT] is file_core.files_proxy
    assert file_core._proxies[file_core.TEST_FOLDER] is file_core.test_folder_proxy
    assert file_core.ROOT == Path(meltygui.__file__).parent / 'files'


def test_collect_never_unpaints_a_tint_painted_elsewhere(tmp_path, tints):
    """folder_io's run is apply, view, collect. A tint another process writes between runs
    must survive both before the watcher's refresh (the cache still says unpainted) and after
    it; only removing the tint this run applied unpaints."""
    import json
    import melty_meta

    def run(held):
        file_model._apply_meta(held, tmp_path, {})
        file_model._collect_meta(held, tmp_path, {})
        tints.flush()

    (tmp_path / 'a.py').write_text('')
    (tmp_path / 'b.py').write_text('')
    held = {'a.py': tmp_path / 'a.py', 'b.py': tmp_path / 'b.py'}
    run(held)
    assert tints.tint(tmp_path / 'a.py') is None
    # Another process (a git checkout) writes the folder's `.melty` directly.
    (tmp_path / '.melty').write_text(json.dumps({'files': {'a.py': {'tint': [0.2, 0.6, 0.3]}}}))
    run(held)                                   # the cache is stale: nothing applied, nothing removed
    assert melty_meta.tint(tmp_path / 'a.py') == (0.2, 0.6, 0.3)
    tints._check()                              # the watcher's refresh
    run(held)
    assert held['__overrides__']['__a.py__']['tint'] == (0.2, 0.6, 0.3)
    assert melty_meta.tint(tmp_path / 'a.py') == (0.2, 0.6, 0.3)
    run(held)
    assert melty_meta.tint(tmp_path / 'a.py') == (0.2, 0.6, 0.3)
    del held['__overrides__']['__a.py__']['tint']   # the user unpaints it in the tree
    file_model._collect_meta(held, tmp_path, {})
    tints.flush()
    assert melty_meta.tint(tmp_path / 'a.py') is None
    assert not (tmp_path / '.melty').exists()
