"""Native host lifecycle. Does not import the desktop GLFW/OpenGL entry point."""
from __future__ import annotations

import importlib
import io
import os
from pathlib import Path
import sys

_app = None
_host = None


class Host:
    """The only native services the Python application needs during bootstrap."""

    def __init__(self, native):
        self._native = native

    def request_frame(self):
        """Wake display pacing; safe to call from a Python worker thread."""
        self._native.request_frame()

    def set_keyboard_visible(self, visible):
        self._native.set_keyboard_visible(bool(visible))

    def set_safe_zone(self, inset):
        """Update the native top inset; subsequent frames report its real bounds."""
        self._native.set_safe_zone(float(inset))

    def get_clipboard_text(self):
        """Return UIKit's cached text without blocking the render thread."""
        return self._native.get_clipboard_text()

    def set_clipboard_text(self, text):
        self._native.set_clipboard_text(text)


class _Log(io.TextIOBase):
    def __init__(self, native):
        self._native = native

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        if text:
            self._native.write_log(text)
        return len(text)

    def flush(self):
        pass


def _source_files(root):
    """Metadata, not content hashes, identifies the files supplied by a build."""
    return {p.relative_to(root).as_posix(): [p.stat().st_mtime_ns, p.stat().st_size]
            for p in sorted(root.rglob('*')) if p.is_file()}


def _application_source(bundle, support):
    """Update managed app files in place; never erase device-created saves."""
    import json
    import plistlib
    import shutil
    import tempfile
    import uuid

    settings = plistlib.loads((bundle / 'HostSettings.plist').read_bytes())
    generation = uuid.UUID(settings['source_generation']).hex
    root = Path(support).resolve() / 'meltygui' / 'app-source'
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    if state:
        source = root / uuid.UUID(state['directory']).hex
    else:
        # Keep the previous bootstrap's actual path: saved tabs refer to it.
        previous = [p for p in root.iterdir() if p.is_dir() and len(p.name) == 32
                    and all(c in '0123456789abcdef' for c in p.name)]
        source = max(previous, key=lambda p: p.stat().st_mtime_ns) if previous else root / generation
    incoming = bundle / 'app'
    inputs = settings.get('source_files')
    if inputs is None:
        inputs = _source_files(incoming)
    token = generation
    update = root.parent / 'app-update'
    descriptor = update / 'update.json'
    if descriptor.is_file():
        candidate = json.loads(descriptor.read_text())
        if candidate['base_generation'] == generation:
            token = uuid.UUID(candidate['generation']).hex
            incoming = update / token / 'app'
            inputs = candidate['files']
    if state.get('generation') != token:
        # Validate and stage the complete update before touching writable data.
        for name in inputs:
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or not name:
                raise ValueError('Invalid application update path')
            if not (incoming / relative).resolve().is_relative_to(incoming.resolve()):
                raise ValueError('Application update escapes its source directory')
            if not (source / relative).resolve().is_relative_to(source.resolve()):
                raise ValueError('Application update escapes its writable directory')
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=root) as temporary:
            staged = Path(temporary) / 'app'
            shutil.copytree(incoming, staged)
            source.mkdir(exist_ok=True)
            old_inputs, old_files = state.get('inputs', {}), state.get('files', {})
            files = dict(old_files)
            for name, stamp in inputs.items():
                destination = source / name
                if old_inputs.get(name) == stamp and destination.is_file():
                    continue  # Keep device edits when this host file did not change.
                if destination.exists():
                    actual = [destination.stat().st_mtime_ns, destination.stat().st_size]
                    if old_files.get(name) != actual:
                        backup = root / 'backups' / token / name
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(destination, backup)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged / name, destination)
                if destination.suffix == '.py':
                    for bytecode in (destination.parent / '__pycache__').glob(destination.stem + '.*.pyc'):
                        bytecode.unlink()
                files[name] = [destination.stat().st_mtime_ns, destination.stat().st_size]
            for name in old_inputs.keys() - inputs.keys():
                destination = source / name
                if destination.is_file() and [destination.stat().st_mtime_ns, destination.stat().st_size] == old_files.get(name):
                    destination.unlink()  # Only remove unchanged, formerly managed files.
                files.pop(name, None)
            state = dict(directory=source.name, generation=token, inputs=inputs, files=files)
            pending = root / '.state.json'
            pending.write_text(json.dumps(state))
            pending.replace(state_path)
        # Payloads are immutable until committed, so a failed upload can never
        # corrupt a previously committed update that has not launched yet.
        for payload in update.glob('*'):
            if (payload.is_dir() and payload.name != token and len(payload.name) == 32
                    and all(c in '0123456789abcdef' for c in payload.name)):
                shutil.rmtree(payload)
    index = next(i for i, path in enumerate(sys.path)
                 if Path(path).resolve() == bundle / 'app')
    sys.path[index] = str(source)
    importlib.invalidate_caches()
    return source


def initialize(config):
    """Create the application once on the native render thread.

    The entry module is an ordinary MeltyGUI app. Install the native owner
    before importing it so its normal boot/window/run calls use this host.
    """
    global _app, _host
    if _app is not None:
        raise RuntimeError("The iOS application is already initialized")
    import _melty_ios

    sys.stdout = sys.stderr = _Log(_melty_ios)
    _host = Host(_melty_ios)
    for key in ("documents", "workspace", "application_support", "cache"):
        Path(config[key]).mkdir(parents=True, exist_ok=True)
    os.chdir(config["workspace"])
    # ctypes wrappers need the signed libraries' actual container paths. The
    # app may move to a new container on every installation.
    bundle = Path(__file__).resolve().parents[1]
    frameworks = bundle / "Frameworks"
    os.environ["SPATIALINDEX_C_LIBRARY"] = str(frameworks / "spatialindex_c.framework/spatialindex_c")
    os.environ["DYLD_FRAMEWORK_PATH"] = str(frameworks)
    certificates = bundle / "app_packages/certifi/cacert.pem"
    if certificates.is_file():
        os.environ.setdefault("SSL_CERT_FILE", str(certificates))
    if not config["renderer_available"]:
        raise RuntimeError("The native host requires MeltyMetalRenderer and its shader library")
    source = _application_source(bundle, config['application_support'])
    from meltygui.core.runtime.app import _register_editable
    _register_editable(source)
    from meltygui.core.runtime.native_app import start_native_application
    _app = start_native_application(dict(config), _host)
    print(f"{_app.config['app_id']}: embedded CPython {sys.version.split()[0]} initialized")


def frame(info, events):
    """True requests another frame; False puts the display link to sleep."""
    if _app is None:
        raise RuntimeError("The iOS application has not initialized")
    return bool(_app.frame(info, events))


def suspend():
    if _app is not None:
        _app.suspend()


def presented():
    """A frame was submitted; GPU completion is owned by the native host."""
    callback = getattr(_app, "presented", None)
    if callback is not None:
        callback()


def resume():
    if _app is not None:
        _app.resume()


def close():
    global _app
    if _app is not None:
        _app.close()
        _app = None
