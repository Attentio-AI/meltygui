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
