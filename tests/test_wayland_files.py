import os

from meltygui.core.windowing.backends.wayland_files import read_paths


def test_only_local_file_uris():
    read, write = os.pipe()
    with os.fdopen(write, 'wb') as pipe:
        pipe.write(b'# comment\r\nfile:///tmp/a%20b.png\r\nfile://localhost/tmp/ok\n'
                   b'https://example.com/file\nfile://elsewhere/tmp/a\nfile:/tmp/a%00b\n')
    assert read_paths(read) == ('/tmp/a b.png', '/tmp/ok')
