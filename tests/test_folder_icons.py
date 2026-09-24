"""Desktop icon discovery, content fallbacks and cache invalidation without GL."""
from pathlib import Path
import time

from PIL import Image
import pytest

from meltygui.model.folder_icon_model import FolderIcons, Probe, load_folder_icon, resolve_icon, stamp


def png(path, color='red', size=(24, 16)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGBA', size, color).save(path)
    return path


def desktop(folder, icon, name='app.desktop', extra=''):
    path = folder / name
    path.write_text(f'[Desktop Entry]\nType=Application\nName=Example\nIcon={icon}\n{extra}')
    return path


@pytest.fixture
def theme_env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('XDG_DATA_DIRS', str(tmp_path / 'system'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    return tmp_path / 'data/icons'


def test_absolute_icon_prefers_matching_desktop_and_keeps_aspect(tmp_path):
    folder = tmp_path / 'app'
    folder.mkdir()
    desktop(folder, png(tmp_path / 'other.png', 'blue'), 'aaa.desktop')
    chosen = png(tmp_path / 'chosen.png', size=(128, 64))
    desktop(folder, chosen, 'app.desktop')
    pixels, dependencies = load_folder_icon(folder)
    assert pixels.path == chosen
    assert (pixels.width, pixels.height) == (64, 32)
    assert chosen in dependencies and folder in dependencies


def test_relative_icon_and_desktop_escaping(tmp_path):
    image = png(tmp_path / 'art/my icon.png')
    desktop(tmp_path, r'art/my\sicon.png')
    pixels, _ = load_folder_icon(tmp_path)
    assert pixels.path == image


def test_broken_hidden_and_non_application_entries_fall_through(tmp_path):
    (tmp_path / 'a.desktop').write_text('not a desktop file')
    desktop(tmp_path, '/missing/icon.png', 'b.desktop')
    broken = tmp_path / 'broken.png'
    broken.write_text('invalid image')
    desktop(tmp_path, broken, 'c.desktop')
    image = png(tmp_path / 'valid.png')
    desktop(tmp_path, image, 'd.desktop', 'Hidden=true\n')
    (tmp_path / 'e.desktop').write_text(f'[Desktop Entry]\nType=Link\nIcon={image}\n')
    assert load_folder_icon(tmp_path)[0] is None
    desktop(tmp_path, image, 'f.desktop')
    assert load_folder_icon(tmp_path)[0].path == image


def test_named_icon_inherits_theme_and_falls_back_to_hicolor(tmp_path, theme_env):
    config = tmp_path / 'config/gtk-3.0'
    config.mkdir(parents=True)
    (config / 'settings.ini').write_text('[Settings]\ngtk-icon-theme-name=Example\n')
    for name, extra in [('Example', 'Inherits=Parent\n'), ('Parent', ''), ('hicolor', '')]:
        root = theme_env / name
        root.mkdir(parents=True)
        (root / 'index.theme').write_text(
            '[Icon Theme]\nDirectories=64x64/apps\n' + extra +
            '[64x64/apps]\nSize=64\nType=Fixed\n')
    inherited = png(theme_env / 'Parent/64x64/apps/sample.png')
    fallback = png(theme_env / 'hicolor/64x64/apps/fallback.png')
    assert resolve_icon('sample', tmp_path, Probe()) == inherited
    assert resolve_icon('fallback', tmp_path, Probe()) == fallback
    assert resolve_icon('missing', tmp_path, Probe()) is None


def test_svg_icon(tmp_path):
    import shutil
    if shutil.which('gdk-pixbuf-thumbnailer') is None:
        pytest.skip('System SVG decoder unavailable')
    icon = tmp_path / 'icon.svg'
    icon.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="12">'
                    '<rect width="24" height="12" fill="green"/></svg>')
    desktop(tmp_path, icon)
    pixels, _ = load_folder_icon(tmp_path)
    assert pixels is not None
    assert pixels.width == 2 * pixels.height


def finish(cache, folder):
    deadline = time.monotonic() + 3
    while cache.pending and time.monotonic() < deadline:
        time.sleep(.01)
        cache.consume([folder])
    assert not cache.pending
    return cache.get(folder)


def test_cache_refreshes_added_desktop_changed_icon_and_deleted_icon(tmp_path):
    cache = FolderIcons()
    try:
        cache.consume([tmp_path])
        cache.dispatch(lambda: None)
        assert finish(cache, tmp_path) is None
        image = png(tmp_path / 'art/icon.png')
        entry = desktop(tmp_path, image)
        cache.dispatch(lambda: None)
        first = finish(cache, tmp_path)
        assert first is not None
        assert str(image.parent) in cache.watch_directories()
        cache.dispatch(lambda: None)
        assert not cache.pending
        assert cache.get(tmp_path) is first
        png(image, 'blue', (32, 16))
        cache.dispatch(lambda: None)
        second = finish(cache, tmp_path)
        assert second is not first and second.pixels.data != first.pixels.data
        image.unlink()
        cache.dispatch(lambda: None)
        assert finish(cache, tmp_path) is None
        # A broken icon can be repaired without changing its desktop entry.
        png(image, 'green')
        cache.dispatch(lambda: None)
        assert finish(cache, tmp_path) is not None
        entry.unlink()
        cache.dispatch(lambda: None)
        assert finish(cache, tmp_path) is None
        cache.consume([])
        assert not cache.entries
    finally:
        cache.close()
    assert cache.executor is None and not cache.pending


def test_desktop_icon_reference_change_invalidates_cached_result(tmp_path):
    red = png(tmp_path / 'red.png')
    blue = png(tmp_path / 'blue.png', 'blue')
    entry = desktop(tmp_path, red)
    first, dependencies = load_folder_icon(tmp_path)
    desktop(tmp_path, blue)
    assert stamp(entry) != dependencies[entry]
    assert first.path != load_folder_icon(tmp_path)[0].path


def test_shared_workers_are_borrowed_and_pending_work_is_cancelled(tmp_path):
    from concurrent.futures import Future
    cache = FolderIcons()
    future = Future()
    calls = []
    cache.consume([tmp_path])
    cache.dispatch(lambda: None, submit=lambda *args: calls.append(args) or future)
    assert cache.executor is None
    assert calls == [(load_folder_icon, tmp_path)]
    cache.close()
    assert future.cancelled()


def test_breadcrumb_rows_carry_shared_path_icons_and_metadata(tmp_path):
    from meltygui.view.file_view import _crumb_menu
    from meltygui.model.folder_icon_model import PathIcon
    folder = tmp_path / 'app'
    folder.mkdir()
    file = tmp_path / 'notes.txt'
    file.touch()
    memo = {}
    metadata = {str(folder): {'icon': 'custom'}}
    rows = _crumb_menu(tmp_path, None, metadata, memo)
    assert rows == {'app': str(folder), 'notes.txt': str(file)}
    assert memo['icons'][str(folder)] == PathIcon(str(folder), True, 'custom')
    assert memo['icons'][str(file)] == PathIcon(str(file), False)


def test_event_driven_cache_does_no_filesystem_work_during_warm_paint(tmp_path, monkeypatch):
    import threading
    from meltygui.model import folder_icon_model as model
    image = png(tmp_path / 'icon.png')
    desktop(tmp_path, image)
    cache = FolderIcons()
    render_thread = threading.get_ident()
    try:
        cache.consume([tmp_path])
        cache.dispatch(lambda: None, check_stale=False)
        first = finish(cache, tmp_path)
        original_stamp = model.stamp

        def worker_stamp(path):
            assert threading.get_ident() != render_thread, 'filesystem probe on render thread'
            return original_stamp(path)

        monkeypatch.setattr(model, 'stamp', worker_stamp)
        for _ in range(20):
            cache.consume([tmp_path])
            assert str(tmp_path) in cache.watch_directories()
            cache.dispatch(lambda: None, check_stale=False)
            assert not cache.pending and cache.get(tmp_path) is first

        # A watch notification checks freshness on a worker; unchanged artwork
        # keeps the same texture (and therefore does not upload again).
        cache.mark_dirty()
        cache.dispatch(lambda: None, check_stale=False)
        assert finish(cache, tmp_path) is first
        png(image, 'blue')
        cache.mark_dirty()
        cache.dispatch(lambda: None, check_stale=False)
        assert finish(cache, tmp_path) is not first
    finally:
        cache.close()


def test_event_tracking_can_hotswap_onto_an_existing_cache(tmp_path):
    cache = FolderIcons()
    cache.entries[tmp_path] = (None, {tmp_path: stamp(tmp_path)})
    cache.requested = {tmp_path}
    original_entries = cache.entries
    del cache.dirty
    del cache._watch_directories
    cache.consume([tmp_path])
    cache.dispatch(lambda: None, check_stale=False)
    assert cache.entries is original_entries and not cache.pending
    assert str(tmp_path) in cache.watch_directories()
    cache.close()
