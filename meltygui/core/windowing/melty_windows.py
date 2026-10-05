"""Melty Windows v1: automatic per-surface gesture ownership on macOS.

The local service grants a short capability lease; geometry stays on the app's
render thread. No app schema, layout graph, AX permission or per-frame IPC.
"""
import ctypes
import json
import os
import socket
import stat
import sys
import threading
import time

_STATE = globals().get('_STATE') or {
    'lock': threading.Lock(), 'windows': set(), 'expires': 0., 'thread': None,
    'stop': threading.Event(), 'numbers': {},
}
_OBJC = globals().get('_OBJC')
_LIVE_RESIZE = globals().get('_LIVE_RESIZE')
PATH = os.path.expanduser('~/Library/Application Support/Melty Windows/surfaces-v1.sock')


def _native_value(window, selector_name):
    # GLFW exposes NSWindow*. Only scalar ObjC messages are needed, avoiding
    # a PyObjC dependency and structure-return ABI differences on Intel Macs.
    global _OBJC
    from meltygui.core.windowing import window_api as glfw
    native = glfw.get_cocoa_window(window)
    if not native:
        return None
    if _OBJC is None:
        objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        selector = objc.sel_registerName
        selector.argtypes, selector.restype = [ctypes.c_char_p], ctypes.c_void_p
        send = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)(
            ('objc_msgSend', objc))
        _OBJC = (send, {name: selector(name) for name in (b'windowNumber', b'styleMask')})
    send, selectors = _OBJC
    return send(native, selectors[selector_name])


def _window_number(window):
    from meltygui.core.windowing import window_api as glfw
    native = glfw.get_cocoa_window(window)
    if native in _STATE['numbers']:
        return _STATE['numbers'][native]
    number = _native_value(window, b'windowNumber')
    if number is not None and 0 < number <= 0xFFFFFFFF:
        _STATE['numbers'][native] = number
        return number
    return None


def _exchange(windows):
    info = os.lstat(PATH)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError('untrusted surface endpoint')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(.1)
        connection.connect(PATH)
        connection.sendall(json.dumps({'version': 1, 'operation': 'claim',
                                       'windows': sorted(windows)}).encode() + b'\n')
        data = b''
        while not data.endswith(b'\n') and len(data) < 4096:
            part = connection.recv(4096 - len(data))
            if not part:
                raise OSError('surface service disconnected')
            data += part
    reply = json.loads(data)
    return (isinstance(reply, dict) and type(reply.get('version')) is int
            and reply.get('version') == 1 and reply.get('capability') == 'native-edges'
            and reply.get('enabled') is True and type(reply.get('lease_seconds')) is int
            and reply.get('lease_seconds') == 1)


def _poll():
    while not _STATE['stop'].is_set():
        with _STATE['lock']:
            windows = set(_STATE['windows'])
        began = time.monotonic()
        try:
            active = _exchange(windows)
        except (OSError, ValueError, TypeError):
            active = False
        with _STATE['lock']:
            was_active = _STATE['expires'] > began
            accepted = windows if active else set()
            changed = accepted != _STATE.get('accepted', set())
            _STATE['accepted'] = accepted
            _STATE['expires'] = began + 1 if active else 0.
        if changed or active != was_active:
            from meltygui.core.windowing.glfw_utils import request_render
            request_render()
        _STATE['stop'].wait(.25)


def available(window):
    if sys.platform != 'darwin' or window is None:
        return False
    # A green-button fullscreen Space is not GLFW monitor fullscreen.
    style = _native_value(window, b'styleMask')
    if style is None or style & (1 << 14):  # NSWindowStyleMaskFullScreen
        return False
    number = _window_number(window)
    if number is None:
        return False
    with _STATE['lock']:
        _STATE['windows'].add(number)
        if _STATE['thread'] is None:
            _STATE['stop'].clear()
            _STATE['thread'] = threading.Thread(target=_poll, name='melty-windows', daemon=True)
            _STATE['thread'].start()
        return (number in _STATE.get('accepted', ())
                and time.monotonic() < _STATE['expires'])


def _in_live_resize(window):
    """Whether AppKit, rather than the app's solver, owns a modal edge drag."""
    global _LIVE_RESIZE
    from meltygui.core.windowing import window_api as glfw
    if _LIVE_RESIZE is None:
        objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        selector = objc.sel_registerName
        selector.argtypes, selector.restype = [ctypes.c_char_p], ctypes.c_void_p
        send = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(
            ('objc_msgSend', objc))
        _LIVE_RESIZE = (send, selector(b'inLiveResize'))
    send, selector = _LIVE_RESIZE
    return bool(send(glfw.get_cocoa_window(window), selector))


def defer_refresh(window):
    """Queue cooperative app resizes for the ordinary render loop.

    Cocoa can deliver display refresh inside a Core Animation transaction.
    Applying another solver resize and swapping there feeds the next refresh
    back into event dispatch. Native modal resizing still needs inline redraw.
    This check never registers a new surface or performs service IPC.
    """
    if sys.platform != 'darwin':
        return False
    number = _window_number(window)
    with _STATE['lock']:
        owned = (number in _STATE.get('accepted', ())
                 and time.monotonic() < _STATE['expires'])
    return owned and not _in_live_resize(window)


def forget(window):
    if sys.platform != 'darwin':
        return
    number = _window_number(window)
    _STATE['numbers'] = {key: value for key, value in _STATE['numbers'].items() if value != number}
    with _STATE['lock']:
        _STATE['windows'].discard(number)
        _STATE.get('accepted', set()).discard(number)


def stop():
    _STATE['stop'].set()
    thread = _STATE['thread']
    if thread is not None:
        thread.join(timeout=.5)
    with _STATE['lock']:
        _STATE.update(thread=None, expires=0., windows=set(), accepted=set(), numbers={})


def workarea(window):
    """Choose a display in logical points, never from a Retina video-mode size."""
    from meltygui.core.windowing import window_api as glfw
    x, y = glfw.get_window_pos(window)
    width, height = glfw.get_window_size(window)
    cx, cy = x + width / 2, y + height / 2
    areas = [glfw.get_monitor_workarea(monitor) for monitor in glfw.get_monitors()]
    if not areas:
        areas = [glfw.get_monitor_workarea(glfw.get_primary_monitor())]

    def distance(area):
        ax, ay, aw, ah = area
        return max(ax - cx, 0, cx - ax - aw) ** 2 + max(ay - cy, 0, cy - ay - ah) ** 2

    ax, ay, aw, ah = min(areas, key=distance)
    return float(ax), float(ay), float(ax + aw), float(ay + ah)


def observe(window):
    """Top-left content geometry in logical screen points, including on Retina.

    GLFW's Cocoa coordinates already match the solver/input units. Reserve OS
    decorations at all workarea edges so content can't push a titlebar offscreen.
    """
    from meltygui.core.windowing import window_api as glfw, titlebar
    x, y = glfw.get_window_pos(window)
    width, height = glfw.get_window_size(window)
    left, top, right, bottom = glfw.get_window_frame_size(window)
    ax, ay, ar, ab = titlebar._workarea_for(window)
    area = (ax + left, ay + top, ar - ax - left - right, ab - ay - top - bottom)
    return ((float(x), float(y)), area, 'cocoa',
            (float(x + width), float(y + height)), _window_number(window))


def apply(window, width, height, offset):
    """Apply position and size together before the next render/input pass."""
    from meltygui.core.windowing import window_api as glfw
    x, y = glfw.get_window_pos(window)
    dx, dy = offset or (0, 0)
    glfw.set_window_size(window, int(width), int(height))
    glfw.set_window_pos(window, int(x + dx), int(y + dy))
