"""SSH roots and SFTP operations. OpenSSH owns authentication and host verification.

The cache is shared file data, not view state. Each worker owns and closes its
connection. No socket, worker or cached listing is serialized into settings.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, unquote
import os
import select
import shutil
import stat as stat_module
import subprocess
import tempfile
import threading
import time
import uuid
import atexit
import hashlib
import json
import weakref

from meltygui.model.file_location_model import FileLocation


@dataclass
class SSH:
    target: str
    directory: str = '~'
    port: int | None = None

    def __post_init__(self):
        if not self.target or self.target.startswith('-') or any(c.isspace() for c in self.target):
            raise ValueError('Use an SSH config alias or user@host')
        if any(c in self.target for c in '/?#'):
            raise ValueError('The SSH target must not include a path')
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError('Invalid SSH port')
        if not (self.directory.startswith('/') or self.directory == '~' or self.directory.startswith('~/')):
            raise ValueError('SSH directories must be absolute or relative to ~')

    @property
    def location(self):
        self.__post_init__()
        authority = self.target + (f':{self.port}' if self.port is not None else '')
        return FileLocation(f'sftp://{authority}/{quote(self.directory.lstrip("/"), safe="/")}')


def ssh_arguments(location, *, subsystem=False):
    uri = urlsplit(str(location))
    if uri.scheme != 'sftp' or not uri.hostname or uri.password is not None:
        raise ValueError('Invalid SSH file location')
    host = uri.hostname
    if ':' in host:
        host = f'[{host}]'
    target = f'{unquote(uri.username)}@{host}' if uri.username else host
    if target.startswith('-') or any(c.isspace() for c in target):
        raise ValueError('Invalid SSH target')
    executable = shutil.which('ssh')
    if executable is None:
        raise OSError('OpenSSH is not installed')
    args = [executable, '-T', '-oBatchMode=yes', '-oStrictHostKeyChecking=yes',
            '-oConnectTimeout=8', '-oServerAliveInterval=5', '-oServerAliveCountMax=3']
    if uri.port is not None:
        args += ['-p', str(uri.port)]
    if subsystem:
        args += ['-s']
    return args + [target]


class _SFTPChannel:
    """The small socket interface Paramiko needs over OpenSSH's binary pipes."""
    def __init__(self, location):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(ssh_arguments(location, subsystem=True) + ['sftp'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors,
            close_fds=False, bufsize=0)

    def send(self, data):
        if not select.select([], [self.process.stdin], [], 20)[1]:
            raise TimeoutError('SSH write timed out')
        return os.write(self.process.stdin.fileno(), data)

    def get_name(self):
        return 'openssh-sftp'

    def recv(self, size):
        if not select.select([self.process.stdout], [], [], 20)[0]:
            raise TimeoutError('SSH read timed out')
        data = os.read(self.process.stdout.fileno(), size)
        if not data:
            self.errors.seek(0)
            detail = self.errors.read(4096).decode('utf-8', 'replace').strip()
            raise OSError(detail or 'SSH connection closed')
        return data

    def close(self):
        self.process.stdin.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.errors.close()


@contextmanager
def sftp(location):
    from paramiko import SFTPClient
    channel = _SFTPChannel(location)
    try:
        client = SFTPClient(channel)
        yield client
    finally:
        channel.close()


def native_path(client, location):
    path = FileLocation.parse(location).remote_path
    if path == '/~' or path.startswith('/~/'):
        return client.normalize('.') + path[2:]
    return path


def stamp(attributes):
    return (attributes.st_mtime, attributes.st_size, attributes.st_mode)


_cache = globals().get('_cache', {})
_lock = globals().get('_lock', threading.RLock())


def entry(location):
    with _lock:
        return _cache.setdefault(str(location), {})


def is_renaming(location):
    location = FileLocation.parse(location)
    return any(_cache.get(str(parent), {}).get('renaming') for parent in (location, *location.parents))


def begin_rename(location):
    if any(FileLocation.parse(key).is_within(location) and (state.get('loading') or state.get('saving'))
           for key, state in list(_cache.items())):
        raise ValueError('Wait for the remote file operation to finish before renaming.')
    entry(location)['renaming'] = True
    for key in list(_cache):
        if FileLocation.parse(key).is_within(location):
            _wake(key)


def finish_rename(old, new=None):
    entry(old).pop('renaming', None)
    for key in list(_cache):
        location = FileLocation.parse(key)
        if location.is_within(old):
            if new is not None:
                moved = new / location.relative_to(old)
                state = _cache.pop(key)
                state['path'] = moved.remote_path
                _cache[str(moved)] = state
                _wake(moved)
            else:
                _wake(key)


def subscribe(location, draw_state):
    state = entry(location)
    with _lock:
        if callable(draw_state):
            state.setdefault('listeners', {})[(id(draw_state.__self__), draw_state.__func__)] = weakref.WeakMethod(draw_state)
        else:
            state.setdefault('views', weakref.WeakSet()).add(draw_state)


def _wake(location):
    from meltygui.core.melty import Melty
    from meltygui.core.windowing.glfw_utils import request_render
    def adopt():
        listeners = entry(location).get('listeners', {})
        for key, listener in tuple(listeners.items()):
            callback = listener()
            if callback is None:
                listeners.pop(key, None)
            else:
                callback()
        for view in list(entry(location).get('views', ())):
            view.invalidate()
    Melty.post_to_render(adopt)
    request_render()


def _operation(location, operation):
    with sftp(location) as client:
        path = client.normalize(native_path(client, location))
        attributes = client.stat(path)
        result = {'stat': attributes, 'path': path}
        if operation == 'rows':
            rows = []
            for attr in client.listdir_attr(path):
                child = FileLocation.parse(location).joinpath(attr.filename)
                # List symlinks but do not recursively enter them.
                rows.append((child, stat_module.S_ISDIR(attr.st_mode)))
                cached = entry(child)
                cached['stat'] = attr
                if 'data_stat' in cached and stamp(cached['data_stat']) != stamp(attr):
                    cached['stale'] = True
                    _wake(child)
            result['rows'] = sorted(rows, key=lambda row: (not row[1], row[0].name.casefold()))
        elif operation == 'data':
            with client.open(path, 'rb') as stream:
                result['data'] = stream.read()
            result['read_id'] = uuid.uuid4().hex
            result['data_stat'] = attributes
            result['stale'] = False
            if stamp(client.stat(path)) != stamp(attributes):
                raise OSError('File changed during download; refresh to retry')
        return result


def request(location, operation='stat', *, refresh=False):
    """A bounded, debounced daemon job. Readers keep the previous good snapshot."""
    if is_renaming(location):
        return
    state = entry(location)
    with _lock:
        if state.get('loading'):
            if operation not in state:
                state['next_operation'] = operation
            return
        if not refresh and state.get('error') and time.monotonic() - state.get('checked', 0) < 2:
            return
        if not refresh and operation in state:
            return
        state['loading'] = True

    def work():
        try:
            result = _operation(location, operation)
            with _lock:
                state.update(result)
                state.pop('error', None)
        except Exception as error:
            with _lock:
                state['error'] = error
        finally:
            with _lock:
                state.update(loading=False, checked=time.monotonic())
            _wake(location)
            next_operation = state.pop('next_operation', None)
            if next_operation and next_operation not in state and not state.get('error'):
                request(location, next_operation)

    threading.Thread(target=work, name='ssh-files', daemon=True).start()


def cached_stat(location):
    state = entry(location)
    request(location)
    if 'stat' in state:
        return state['stat']
    raise state.get('error') or OSError('Loading SSH file…')


def read_bytes(location):
    state = entry(location)
    # The codec already loads on a worker. Render-time manifest reads use cache.
    if 'data' not in state and threading.current_thread() is not threading.main_thread():
        state.update(_operation(location, 'data'))
    if 'data' in state:
        return state['data']
    request(location, 'data')
    raise state.get('error') or OSError('Loading SSH file…')


def list_directory(location, show_hidden=False):
    state = entry(location)
    request(location, 'rows', refresh=time.monotonic() - state.get('checked', 0) > 5)
    return [row for row in state.get('rows', ()) if show_hidden or not row[0].name.startswith('.')]


def write_bytes(location, data, expected, *, force=False):
    """Optimistic SFTP save; never downgrade atomic replace to delete + rename."""
    with sftp(location) as client:
        path = native_path(client, location)
        # Replace the resolved target, not a symlink itself.
        try:
            current = client.stat(path)
            path = client.normalize(path)
        except FileNotFoundError:
            current = None
            parent, name = path.rsplit('/', 1)
            path = client.normalize(parent or '/') + '/' + name
        if not force and (stamp(current) if current else None) != expected:
            raise ValueError('Remote file changed since it was loaded')
        temporary = f'{path}.melty-{uuid.uuid4().hex}.tmp'
        replacing = False
        try:
            with client.open(temporary, 'wx') as stream:
                stream.write(data)
                stream.flush()
            if current is not None:
                client.chmod(temporary, stat_module.S_IMODE(current.st_mode))
                if not force and stamp(client.stat(path)) != stamp(current):
                    raise ValueError('Remote file changed during upload')
                replacing = True
                client.posix_rename(temporary, path)
            else:
                # Standard SFTP rename refuses to replace an existing target.
                replacing = True
                client.rename(temporary, path)
            attributes = client.stat(path)
        except OSError as error:
            if replacing:
                raise OSError(f'Save outcome unconfirmed; check the remote file before retrying: {error}') from error
            raise
        finally:
            try:
                client.remove(temporary)
            except OSError:
                pass
    entry(location).update(data=data, stat=attributes, data_stat=attributes, stale=False, checked=time.monotonic())
    entry(location).pop('error', None)
    return attributes


def resolve_root(root):
    location = root.location if isinstance(root, SSH) else FileLocation.parse(root)
    with sftp(location) as client:
        path = client.normalize(native_path(client, location))
        if not stat_module.S_ISDIR(client.stat(path).st_mode):
            raise ValueError('SSH root is not a directory')
        return location._with_path(path)


def mkdir(location):
    with sftp(location) as client:
        client.mkdir(native_path(client, location))


def create(directory, name, folder=False):
    """Create a free name on the worker; never probe the server while drawing."""
    with sftp(directory) as client:
        base = native_path(client, directory)
        original = name
        number = 2
        while True:
            path = directory / name
            try:
                client.lstat(base + '/' + name)
            except FileNotFoundError:
                if folder:
                    client.mkdir(base + '/' + name)
                else:
                    with client.open(base + '/' + name, 'wx'):
                        pass
                entry(path)['stat'] = client.stat(base + '/' + name)
                return path
            stem, suffix = os.path.splitext(original)
            name = f'{stem} ({number}){suffix}'
            number += 1


def rename(source, destination):
    if source.filesystem_id != destination.filesystem_id:
        raise ValueError('Cross-filesystem moves are not supported')
    with sftp(source) as client:
        client.rename(native_path(client, source), native_path(client, destination))


_recovery_dirty = globals().get('_recovery_dirty', {})
_recovery_timer = None


def _recovery_path(location):
    from meltygui.core.runtime.paths import data_root
    # Hash the identity, never file contents. Filenames cannot escape this folder.
    key = hashlib.sha256(str(location).encode()).hexdigest()
    return data_root('meltygui') / 'ssh-drafts' / (key + '.json')


def recovered_edit(location):
    with _lock:
        if str(location) in _recovery_dirty:
            return _recovery_dirty[str(location)]
    try:
        record = json.loads(_recovery_path(location).read_text())
        return record if record['key'] == str(location) else None
    except FileNotFoundError:
        return None


def save_recovery(address, text):
    global _recovery_timer
    if not isinstance(text, str):
        return
    expected = getattr(address, '_remote_stamp', None)
    if expected is None:
        old = recovered_edit(address.path)
        expected = old['expected'] if old else (stamp(entry(address.path)['stat']) if 'stat' in entry(address.path) else None)
        address._remote_stamp = tuple(expected) if expected is not None else None
    record = dict(key=str(address.path), text=text, expected=expected,
                  encoding=getattr(address, '_remote_encoding', 'utf-8'),
                  newline=getattr(address, '_remote_newline', '\n'))
    with _lock:
        _recovery_dirty[str(address.path)] = record
        if _recovery_timer is None or not _recovery_timer.is_alive():
            _recovery_timer = threading.Timer(0.2, flush_recovery)
            _recovery_timer.daemon = True
            _recovery_timer.start()


def clear_recovery(location):
    with _lock:
        _recovery_dirty[str(location)] = None
    flush_recovery()


@atexit.register
def flush_recovery():
    # Serialize disk replacement with edit/clear operations. Closing the app
    # waits for this bounded local write, never for a disconnected SSH server.
    with _lock:
        for key, record in list(_recovery_dirty.items()):
            path = _recovery_path(key)
            try:
                if record is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd, temporary = tempfile.mkstemp(dir=path.parent)
                    try:
                        with os.fdopen(fd, 'w') as stream:
                            json.dump(record, stream)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, path)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                del _recovery_dirty[key]
            except OSError as error:
                entry(key)['save_error'] = f'Cannot preserve SSH draft: {error}'
