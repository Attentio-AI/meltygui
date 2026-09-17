"""Platform guards that keep a non-Linux (Windows) start free of tracebacks."""
import io
import socket
import sys

import meltygui.core.graphics.gl_state as gl_state
import meltygui.core.input.space_mouse as space_mouse
import meltygui.core.runtime.app as app
import meltygui.core.windowing.geometry_feed as geometry_feed


def test_geometry_feed_has_no_backend_off_linux(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setitem(geometry_feed._STATE, 'thread', None)
    assert geometry_feed.backend() is None
    assert geometry_feed.start() is None
    geometry_feed.ensure_started()
    assert geometry_feed._STATE['thread'] is None


def test_space_mouse_without_unix_sockets_reports_instead_of_raising(monkeypatch):
    monkeypatch.delattr(socket, 'AF_UNIX', raising=False)      # already absent on Windows
    reader = object.__new__(space_mouse._Reader)       # no thread: only _connect is under test
    reader._sock, reader.error, reader.connected = None, None, False
    assert reader._connect() is False
    assert 'unix sockets' in reader.error


def test_legacy_encoded_output_switches_to_utf8(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding='cp1252')
    monkeypatch.setattr(sys, 'stdout', stream)
    monkeypatch.setattr(sys, 'stderr', None)             # pythonw has no streams at all
    app._utf8_output()
    stream.write('▌ ─')
    stream.flush()
    assert raw.getvalue().decode('utf-8') == '▌ ─'


def test_free_vram_is_unknown_without_the_memory_info_extension(monkeypatch):
    monkeypatch.setattr(gl_state, 'is_gl_thread', lambda: True)
    monkeypatch.setattr(gl_state, '_gl_limits', {'max_3d': 2048, 'max_2d': 16384,
                                                  'total_vram_kb': None, 'vram_info': False})
    assert gl_state.gl_free_vram_kb() is None
    assert gl_state.texture3d_fit((96, 96, 96), 4) == ((96, 96, 96), [])


def test_jedi_pool_uses_spawn_where_forkserver_is_missing(monkeypatch):
    import multiprocessing
    import meltygui.code.libcst_conversion as libcst_conversion
    monkeypatch.setattr(libcst_conversion, '_jedi_mp_ctx', None)
    monkeypatch.setattr(multiprocessing, 'get_all_start_methods', lambda: ['spawn'])
    assert libcst_conversion._get_jedi_mp_ctx().get_start_method() == 'spawn'


def test_windows_auto_scale_follows_the_monitor_content_scale(monkeypatch):
    import meltygui.core.styling.fonts as fonts
    import meltygui.core.windowing.window_api as window_api
    # Module attributes shadow window_api's lazy GLFW forwarding, so no display is needed.
    monkeypatch.setitem(vars(window_api), 'get_primary_monitor', lambda: 'monitor')
    monkeypatch.setitem(vars(window_api), 'get_monitor_content_scale', lambda monitor: (1.25, 1.25))
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert fonts.detect_auto_scale() == 1.25


def test_enhanced_titlebar_is_linux_only(monkeypatch):
    import meltygui.core.windowing.titlebar as titlebar
    monkeypatch.setattr(titlebar, '_on_wayland', lambda: False)
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert not titlebar.backend_supported()
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert titlebar.backend_supported()


def test_debug_logs_live_in_the_system_temp_folder(tmp_path, monkeypatch):
    import tempfile
    from meltygui.core.runtime.paths import debug_log_path
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    assert debug_log_path('x.log') == str(tmp_path / 'x.log')


def test_moving_a_file_over_an_existing_one_replaces_it(tmp_path):
    import meltygui.model.file_model as file_model
    source, target = tmp_path / 'a.txt', tmp_path / 'b.txt'
    source.write_text('new'); target.write_text('old')
    pending = {source}
    file_model._create(target, source, pending)
    assert target.read_text() == 'new' and not source.exists() and not pending
