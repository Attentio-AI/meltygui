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


def _application_source(bundle, support):
    """Run this build's app from real editable files outside the signed bundle.

    Keep device edits on relaunch, including a moved install container. Only
    the packaging generation resets them. Dependencies stay in app_packages;
    inspect, source navigation and hotswap all see the app's actual import path.
    """
    import plistlib
    import shutil
    import tempfile
    import uuid

    settings = plistlib.loads((bundle / 'HostSettings.plist').read_bytes())
    generation = uuid.UUID(settings['source_generation']).hex
    root = Path(support).resolve() / 'meltygui' / 'app-source'
    source = root / generation
    if not source.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        # Publish a complete tree. A failed copy leaves the previous build's
        # edits intact and the next launch can retry safely.
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=root) as temporary:
            staged = Path(temporary) / 'app'
            shutil.copytree(bundle / 'app', staged)
            staged.rename(source)
        for previous in root.iterdir():
            if previous != source and previous.is_dir():
                shutil.rmtree(previous)
    # Replace the host's app search root at the same precedence, before any app
    # module is imported. Never leave bundled modules mixed with editable ones.
    # NSURL/Python can spell the same container as /var or /private/var.
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
