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
