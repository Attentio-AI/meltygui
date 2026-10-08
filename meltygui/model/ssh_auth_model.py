"""Direct SSH for sandboxed hosts; credentials stay in the device Keychain."""
import base64
import hashlib
import io
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit, unquote


def server(location):
    uri = urlsplit(str(location))
    if uri.scheme != 'sftp' or not uri.hostname or uri.password is not None:
        raise ValueError('Invalid SSH location')
    return uri.hostname, uri.port or 22, unquote(uri.username or '')


def credential_account(location):
    host, port, user = server(location)
    return f'ssh:{host}:{port}:{user}'


def credentials(location):
    import _melty_ios
    value = _melty_ios.ssh_credentials(credential_account(location))
    if value is None:
        raise OSError('Set up Password or SSH Key in Settings → Project Roots.')
    return json.loads(value)


def known_hosts_path():
    # The direct client's trust store belongs to the app. In particular, an
    # iOS container's top-level directory is not a writable desktop home.
    from meltygui.core.runtime.paths import config_root
    return config_root() / 'ssh' / 'known_hosts'


def host_key_name(location):
    host, port, _ = server(location)
    return host if port == 22 else f'[{host}]:{port}'


class UnknownHostKey(OSError):
    def __init__(self, hostname, key):
        self.hostname = hostname
        self.key = key
        self.fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
        super().__init__(f'Confirm the host key in Settings → Project Roots ({self.fingerprint}).')


_hosts_lock = globals().get('_hosts_lock', threading.Lock())


def trust_host(location, key):
    """Called only after the user confirms this exact fingerprint."""
    from paramiko import HostKeys
    import os
    import tempfile
    path = known_hosts_path()
    with _hosts_lock:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        keys = HostKeys(str(path)) if path.exists() else HostKeys()
        name = host_key_name(location)
        if name in keys and not keys.check(name, key):
            raise OSError('The saved host key has changed. Verify it before replacing it.')
        keys.add(name, key.get_name(), key)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        os.close(fd)
        try:
            keys.save(temporary)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def private_key(text, passphrase=''):
    import paramiko
    for kind in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return kind.from_private_key(io.StringIO(text), password=passphrase or None)
        except (paramiko.SSHException, ValueError):
            pass
    raise OSError('Could not unlock the private key. Check the key and passphrase.')


def connect(location):
    import paramiko
    host, port, user = server(location)
    secret = credentials(location)
    user = user or secret.get('username', '')
    if not user:
        raise OSError('Set an SSH username in Settings → Project Roots.')
    key = private_key(secret['private_key'], secret.get('passphrase', '')) if secret.get('private_key') else None

    class ConfirmHost(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            raise UnknownHostKey(hostname, key)

    client = paramiko.SSHClient()
    path = known_hosts_path()
    if path.exists():
        client.load_host_keys(str(path))
    client.set_missing_host_key_policy(ConfirmHost())
    try:
        client.connect(host, port=port, username=user, password=secret.get('password') or None,
                       pkey=key, look_for_keys=False, allow_agent=False,
                       timeout=8, banner_timeout=8, auth_timeout=12, channel_timeout=20)
        client.get_transport().set_keepalive(5)
        return client
    except UnknownHostKey:
        client.close()
        raise
    except paramiko.BadHostKeyException:
        client.close()
        raise OSError('SSH host key changed. Connection refused; verify the server identity.') from None
    except paramiko.AuthenticationException:
        client.close()
        raise OSError('SSH authentication failed. Check the username, password or private key.') from None
    except Exception:
        client.close()
        raise


class SSHProcess:
    """The task runner's binary-stream process interface over a Paramiko channel."""
    def __init__(self, location, command):
        self.client = connect(location)
        try:
            self.channel = self.client.get_transport().open_session(timeout=20)
            self.channel.exec_command(command)
            self.stdin = self.channel.makefile_stdin('wb')
            self.stdout = self.channel.makefile('rb')
            self.stderr = self.channel.makefile_stderr('rb')
        except Exception:
            self.client.close()
            raise

    def poll(self):
        return self.channel.recv_exit_status() if self.channel.exit_status_ready() else None

    def wait(self):
        try:
            return self.channel.recv_exit_status()
        finally:
            self.client.close()

    def terminate(self):
        self.client.close()
