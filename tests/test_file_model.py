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


def test_metadata_order_and_tint_round_trip(tmp_path):
    held = {'a': tmp_path / 'a', 'b': tmp_path / 'b'}
    metadata = {str(tmp_path / 'b'): {'order': 0, 'tint': (0.2, 0.4, 0.6, 1.0)},
                str(tmp_path / 'a'): {'order': 1}}
    assert file_model._apply_meta(held, tmp_path, metadata)
    assert [key for key in held if key != '__overrides__'] == ['b', 'a']
    assert held['__overrides__']['__b__']['tint'] == (0.2, 0.4, 0.6, 1.0)
    collected = {}
    file_model._collect_meta(held, tmp_path, collected)
    assert collected[str(tmp_path / 'b')]['order'] == 0
    assert collected[str(tmp_path / 'b')]['tint'] == (0.2, 0.4, 0.6, 1.0)
    # FileMeta adds its default icon field when the plain mapping becomes a model.
    file_model._apply_meta(held, tmp_path, collected)
    assert not file_model._apply_meta(held, tmp_path, collected)


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
