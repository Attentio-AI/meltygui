"""Read a Wayland text/uri-list pipe away from the compositor dispatch thread."""
import os
import select
import time
from urllib.parse import unquote_to_bytes, urlsplit


def read_paths(descriptor):
    limit = 1024 * 1024
    deadline = time.monotonic() + 5.0
    data = bytearray()
    with os.fdopen(descriptor, 'rb', buffering=0) as pipe:
        while len(data) <= limit:
            if not select.select([pipe], [], [], max(0.0, deadline - time.monotonic()))[0]:
                return ()
            chunk = pipe.read(65536)
            if not chunk:
                break
            data.extend(chunk)
        else:
            return ()
    paths = []
    for line in data.decode('utf-8', 'replace').splitlines():
        if not line or line.startswith('#'):
            continue
        try:
            uri = urlsplit(line)
        except ValueError:
            continue
        if uri.scheme != 'file' or uri.netloc not in ('', 'localhost') or uri.query or uri.fragment:
            continue
        path = os.fsdecode(unquote_to_bytes(uri.path))
        if os.path.isabs(path) and '\x00' not in path:
            paths.append(path)
    return tuple(paths)
