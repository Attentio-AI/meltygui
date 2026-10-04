"""Stable workspace locations survive container changes and remain real files."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from meltygui.core.files.workspace_paths import decode_workspace_path, encode_workspace_path
from meltygui.core.runtime import paths


def test_edit_run_and_reopen_file_after_container_relocation(tmp_path, monkeypatch):
    first_container = tmp_path / 'Container-A'
    monkeypatch.setattr(paths, 'sys', SimpleNamespace(platform='ios'))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: first_container))
    filename = paths.workspace_root() / 'Demo project' / 'café.py'
    filename.parent.mkdir(parents=True)
    filename.write_text('result = 6 * 7\n', encoding='utf-8')
    saved = encode_workspace_path(filename)
    assert saved == 'Demo project/café.py'
    assert str(first_container) not in saved

    second_container = tmp_path / 'Container-B'
    first_container.rename(second_container)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: second_container))
    reopened = decode_workspace_path(saved)
    namespace = {}
    exec(compile(reopened.read_text(encoding='utf-8'), str(reopened), 'exec'), namespace)
    assert namespace['result'] == 42
    reopened.write_text('result = 7 * 7\n', encoding='utf-8')
    assert (paths.workspace_root() / saved).read_text(encoding='utf-8') == 'result = 7 * 7\n'


def test_missing_file_and_root_are_valid_locations_independent_of_cwd(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert encode_workspace_path('new/main.py', root=root) == 'new/main.py'
    assert decode_workspace_path('new/main.py', root=root) == root / 'new' / 'main.py'
    assert encode_workspace_path(root, root=root) == '.'
    assert decode_workspace_path('.', root=root) == root
    assert not root.exists()


@pytest.mark.parametrize('value', ['', '/tmp/outside.py', '//outside/file.py', '../outside.py',
                                  'project/../../outside.py', 'project/../main.py', 'bad\0name'])
def test_saved_locations_reject_absolute_paths_and_parent_traversal(tmp_path, value):
    with pytest.raises(ValueError):
        decode_workspace_path(value, root=tmp_path)


def test_similarly_named_neighbor_is_not_in_the_workspace(tmp_path):
    with pytest.raises(ValueError):
        encode_workspace_path(tmp_path / 'workspace-other' / 'main.py', root=tmp_path / 'workspace')


def test_symlinks_cannot_escape_the_workspace(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'external').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        encode_workspace_path(root / 'external' / 'new.py', root=root)
    with pytest.raises(ValueError):
        decode_workspace_path('external/new.py', root=root)


def test_workspace_root_symlink_and_internal_links_are_supported(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'project').mkdir()
    (root / 'alias').symlink_to(root / 'project', target_is_directory=True)
    linked_root = tmp_path / 'linked-workspace'
    linked_root.symlink_to(root, target_is_directory=True)
    saved = encode_workspace_path(linked_root / 'alias' / 'main.py', root=linked_root)
    assert saved == 'project/main.py'
    assert decode_workspace_path(saved, root=linked_root) == root / 'project' / 'main.py'


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX filename semantics')
def test_posix_filename_characters_are_not_reinterpreted_as_windows_paths(tmp_path):
    filename = tmp_path / 'project' / 'name:with\\backslash.py'
    saved = encode_workspace_path(filename, root=tmp_path)
    assert decode_workspace_path(saved, root=tmp_path) == filename
