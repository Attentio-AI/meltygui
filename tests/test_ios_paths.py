"""Desktop XDG and iOS sandbox locations, without an iOS runtime or display."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from meltygui.core.runtime import app_session, app_settings, paths


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / 'Container-A'
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(paths, 'sys', SimpleNamespace(platform='ios'))
    return home


def test_ios_keeps_user_projects_in_documents_and_private_data_in_library(sandbox, monkeypatch):
    # A desktop launcher environment must not redirect iOS writes out of its
    # native directories or expose settings/session data through Documents.
    for variable in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):
        monkeypatch.setenv(variable, '/outside-sandbox')
    support = sandbox / 'Library' / 'Application Support' / 'editor'
    assert app_settings.settings_path('editor') == support / 'settings.json'
    assert app_session.session_path('editor') == support / 'session.pkl'
    assert paths.data_root('editor') == support
    assert paths.cache_root('editor') == sandbox / 'Library' / 'Caches' / 'editor'
    assert paths.cache_root() == sandbox / 'Library' / 'Caches' / 'meltygui'
    assert paths.documents_root() == sandbox / 'Documents'
    assert paths.workspace_root() == sandbox / 'Documents' / 'Projects'
    assert paths.default_file_directory() == paths.workspace_root()
    assert not sandbox.exists()  # resolving locations doesn't create them


def test_settings_write_to_private_storage_and_follow_container_relocation(sandbox, tmp_path, monkeypatch):
    original = app_settings.settings_path('editor')
    settings = app_settings.AppSettings({'font_size': 14}, 'Editor', original)
    settings.values['font_size'] = 18
    assert settings.save() == original
    moved = tmp_path / 'Container-B'
    sandbox.rename(moved)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: moved))
    reopened = app_settings.AppSettings({'font_size': 14}, 'Editor', app_settings.settings_path('editor'))
    assert reopened.values['font_size'] == 18
    assert app_session.session_path('editor').parent == reopened.path.parent
    assert paths.workspace_root() == moved / 'Documents' / 'Projects'
    assert not (moved / 'Documents').exists()


@pytest.mark.parametrize('platform', ['linux', 'darwin', 'win32'])
def test_desktop_keeps_existing_xdg_defaults(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(paths, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    for variable in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):
        monkeypatch.delenv(variable, raising=False)
    assert app_settings.settings_dir('editor') == tmp_path / '.config' / 'editor'
    assert app_session.session_dir('editor') == tmp_path / '.local' / 'state' / 'editor'
    assert paths.data_root('editor') == tmp_path / '.local' / 'share' / 'editor'
    assert paths.cache_root() == tmp_path / '.cache' / 'meltygui'
    assert paths.default_file_directory() == tmp_path
    project = tmp_path / 'source-project'
    monkeypatch.setattr(paths, 'application_root', lambda: project)
    assert paths.workspace_root() == project


def test_desktop_respects_independent_xdg_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'sys', SimpleNamespace(platform='darwin'))
    for variable, name in [('XDG_CONFIG_HOME', 'config'), ('XDG_STATE_HOME', 'state'),
                           ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache')]:
        monkeypatch.setenv(variable, str(tmp_path / name))
    assert app_settings.settings_dir('editor') == tmp_path / 'config' / 'editor'
    assert app_session.session_dir('editor') == tmp_path / 'state' / 'editor'
    assert paths.data_root('editor') == tmp_path / 'data' / 'editor'
    assert paths.cache_root('editor') == tmp_path / 'cache' / 'editor'


@pytest.mark.parametrize('app_id', ['', '.', '..', '../editor', '/tmp/editor', 'a/b', r'a\b', 'C:editor', 'a\0b'])
def test_app_id_cannot_escape_its_storage_root(sandbox, app_id):
    for directory in (paths.config_root, paths.state_root, paths.data_root, paths.cache_root):
        with pytest.raises(ValueError):
            directory(app_id)


@pytest.fixture
def file_consumers(monkeypatch):
    from meltygui.files import fast_file_explorer
    from meltygui.model import chat_folder_model, import_graph_model
    from meltygui.models import file_meta
    modules = (file_meta, import_graph_model, chat_folder_model, fast_file_explorer)
    for module in modules:
        monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='ios'))
    monkeypatch.setattr(chat_folder_model, '_settings', None)
    monkeypatch.delenv('MELTY_FILE_META', raising=False)
    return modules


def test_editor_data_and_cache_consumers_use_native_ios_locations(sandbox, file_consumers, monkeypatch):
    file_meta, imports, chat, _explorer = file_consumers
    assert file_meta.default_store_path() == paths.data_root() / 'file_meta.pkl'
    assert imports.cache_path() == paths.cache_root() / 'import_graph_cache.pkl'
    assert chat.settings_path() == paths.cache_root('melty-claude') / 'folders.json'
    # Exercise their real writers/readers, keeping cache payloads outside Files.
    cached = {'demo.py': (123, 42, ())}
    imports.save_cache(cached)
    assert imports.load_cache() == cached
    chat.folder_settings()['show_all_folders'] = True
    chat.save_folder_settings()
    monkeypatch.setattr(chat, '_settings', None)
    assert chat.folder_settings()['show_all_folders']
    assert not (sandbox / 'Documents').exists()


def test_file_metadata_allows_in_container_override_and_rejects_escape(sandbox, file_consumers,
                                                                     tmp_path, monkeypatch):
    file_meta = file_consumers[0]
    override = sandbox / 'Library' / 'test-meta.pkl'
    monkeypatch.setenv('MELTY_FILE_META', str(override))
    assert file_meta.default_store_path() == override
    sandbox.mkdir()
    monkeypatch.chdir(sandbox)
    monkeypatch.setenv('MELTY_FILE_META', 'Library/test-meta.pkl')
    assert file_meta.default_store_path() == override
    for outside in (str(tmp_path / 'outside.pkl'), '../outside.pkl'):
        monkeypatch.setenv('MELTY_FILE_META', outside)
        with pytest.raises(ValueError, match='MELTY_FILE_META.*iOS app container'):
            file_meta.default_store_path()
    (sandbox / 'escape').symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv('MELTY_FILE_META', str(sandbox / 'escape' / 'outside.pkl'))
    with pytest.raises(ValueError, match='MELTY_FILE_META.*iOS app container'):
        file_meta.default_store_path()


def test_ios_shortcuts_are_the_same_real_projects_documents_paths(sandbox, file_consumers):
    explorer = file_consumers[3]
    paths.workspace_root().mkdir(parents=True)
    assert explorer.shortcut_directories() == [
        ('Projects', paths.workspace_root()), ('Documents', paths.documents_root())]
    assert explorer.shortcut_directories(sandbox) == explorer.shortcut_directories()
    assert all(path.is_dir() for _label, path in explorer.shortcut_directories())


def test_legacy_desktop_data_locations_and_shortcuts_do_not_move(tmp_path, monkeypatch, file_consumers):
    file_meta, imports, chat, explorer = file_consumers
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    for module in file_consumers:
        monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'other-cache'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'other-data'))
    assert file_meta.default_store_path() == tmp_path / '.melty' / 'file_meta.pkl'
    assert imports.cache_path() == tmp_path / '.lsd' / 'import_graph_cache.pkl'
    assert chat.settings_path() == tmp_path / '.cache' / 'melty-claude' / 'folders.json'
    monkeypatch.setenv('MELTY_FILE_META', 'relative-meta.pkl')
    assert file_meta.default_store_path() == Path('relative-meta.pkl')
    (tmp_path / '.config').mkdir()
    (tmp_path / 'My Documents').mkdir()
    (tmp_path / '.config' / 'user-dirs.dirs').write_text('XDG_DOCUMENTS_DIR="$HOME/My Documents"\n')
    assert explorer.shortcut_directories() == [
        ('Home', tmp_path), ('My Documents', tmp_path / 'My Documents'), ('Computer', Path('/'))]


def test_chat_path_is_not_cached_at_import(sandbox, file_consumers, tmp_path, monkeypatch):
    chat = file_consumers[2]
    old_path = chat.settings_path()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'Container-B'))
    assert chat.settings_path() != old_path
    assert chat.settings_path() == paths.cache_root('melty-claude') / 'folders.json'
