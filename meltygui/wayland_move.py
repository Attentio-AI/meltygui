"""Compositor-driven move / resize for the studio's native-Wayland window.

Wayland forbids a client to position itself, and GLFW exposes no route to
xdg_toplevel.move() — but that request IS the interface: it is what GLFW's
caption strip, libdecor's title bar and GNOME's Super+drag all send, and the
compositor then drives the window (snapping, tiling, edge resistance).
GLFW only hides the xdg_toplevel proxy. This module finds it and speaks the
protocol through libwayland-client's public marshal API:

  * the wl_surface GLFW hands out (glfw.get_wayland_window) carries the
    _GLFWwindow as its user data; that struct holds the xdg_toplevel proxy.
    _find_proxy scans it word by word with process_vm_readv (unmapped
    memory reads as a miss, never a fault) for a pointer that reads as a
    wl_object whose interface->name is "xdg_toplevel", then resolves the
    move/resize opcodes BY NAME from that interface's method table — no
    struct offsets, no hardcoded opcodes, so a GLFW or libwayland bump
    degrades to "unavailable", not to a crash.
  * the compositor only honours a move/resize carrying the serial of the
    button press that is still held, and GLFW keeps its serial private, so
    attach() binds this client's OWN wl_seat + wl_pointer on the same
    connection: every wl_pointer of the seat receives the button events,
    the listener records the press serial.
  * the grab swallows the button RELEASE (the X11 path synthesizes one;
    GLFW's Wayland state can't be poked), so begin_* arms a mask that
    button_masked() reports until GLFW itself sees the next real event on
    that button (attach chains a mouse-button callback for that); the
    imgui poll (SplitOverlayRenderer.process_inputs) and the legacy poll
    (LSDStudio.on_mouse) read the mask, titlebar.py feeds the handler its
    synthetic release.

Everything runs on the render thread — the thread that dispatches GLFW's
wl_display. Toggles: Toggles.Melty.wayland_native_frame (libdecor off,
which is when GLFW owns the xdg_toplevel), Toggles.Melty.move_drag_anywhere.
Tests: tests/test_wayland_move.py.
"""
import ctypes
import os
import struct

from src.lsd.gl_gui import window_api as glfw

# Two scopes, both surviving hotswap (module re-exec reuses the dicts).
#
# _STATE is the WINDOW's: the proxies GLFW owns for one OS window (its
# xdg_toplevel, xdg_surface, wl_surface, native window) and the per-window
# protocol opcodes. surface.py swaps them per Surface (MODULE_GLOBALS), so a
# melty app with several windows sees the active window's proxies.
#
# _CONN is the CONNECTION's - one per wl_display, never swapped: the seat
# and pointer bound for the press serials, the registry, the compositor,
# the relative pointer, the toplevel-tag manager, the listener tables and
# the ctypes callbacks they point at (`keep`). Binding these per window was
# the 09-12 abort on closing one of two windows: each Surface bound its own
# wl_pointer to a listener table that lived only in that Surface's
# swapped copy of the state; the Surface was collected, the table freed,
# and the next pointer.enter on the still-registered proxy hit a NULL slot
# ("listener function for opcode 0 of wl_pointer is NULL").
_STATE = globals().get("_STATE") or {
    "attached": False, "window": None, "display": None, "toplevel": None, "surface": None,
    "xdg_surface": None, "opcodes": {}, "prev_button_cb": None, "error": None, "tag": None,
}
_CONN = globals().get("_CONN") or {
    "display": None, "registry": None, "seat": None, "pointer": None, "compositor": None,
    "relative_manager": None, "relative_manager_iface": None, "relative_iface": None,
    "relative_pointer": None, "tag_manager": None, "tag_iface": None,
    "rel_x": 0.0, "rel_y": 0.0, "rel_events": 0,
    "press_serial": 0, "grab_serial": 0, "press_button": None, "held": set(),
    "enter_serial": 0, "masked": set(), "keep": [], "pointer_listener": None,
    "opcodes": {}, "error": None,
}

# xdg_toplevel.resize_edge values (xdg-shell.xml).
EDGE_NONE, EDGE_TOP, EDGE_BOTTOM, EDGE_LEFT = 0, 1, 2, 4
EDGE_TOP_LEFT, EDGE_BOTTOM_LEFT, EDGE_RIGHT = 5, 6, 8
EDGE_TOP_RIGHT, EDGE_BOTTOM_RIGHT = 9, 10

# wl_pointer.button state
_BTN_PRESSED = 1
# wl_proxy_marshal_flags: destroy the request after sending (wl_region one-shots)
WL_MARSHAL_FLAG_DESTROY = 1
# Linux evdev button codes → GLFW buttons
_EVDEV_TO_GLFW = {0x110: glfw.MOUSE_BUTTON_LEFT, 0x111: glfw.MOUSE_BUTTON_RIGHT,
                  0x112: glfw.MOUSE_BUTTON_MIDDLE}

_SCAN_BYTES = 4096       # _GLFWwindow is ~1-2 KB; heap past it reads fine
_CHUNK = 256


class _iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


class _wl_message(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("signature", ctypes.c_char_p),
                ("types", ctypes.c_void_p)]


class _wl_interface(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("version", ctypes.c_int),
                ("method_count", ctypes.c_int), ("methods", ctypes.POINTER(_wl_message)),
                ("event_count", ctypes.c_int), ("events", ctypes.POINTER(_wl_message))]


_libc = None
_wl = None
_egl = None
_STATE.setdefault("egl_window", None)
_STATE.setdefault("offset_armed", False)


class _wl_egl_window(ctypes.Structure):
    """libwayland-egl's struct wl_egl_window (public header, version 3):
    what GLFW hands EGL for the surface. dx/dy are the buffer OFFSET the EGL
    driver puts on the next wl_surface.attach — and for an xdg toplevel the
    compositor applies that offset as a window MOVE (Mutter:
    meta_window_wayland_finish_move_resize, rect.x += dx). The one
    client-initiated move Wayland allows."""
    _fields_ = [("version", ctypes.c_ssize_t),
                ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("dx", ctypes.c_int), ("dy", ctypes.c_int),
                ("attached_width", ctypes.c_int), ("attached_height", ctypes.c_int),
                ("driver_private", ctypes.c_void_p),
                ("resize_callback", ctypes.c_void_p),
                ("destroy_window_callback", ctypes.c_void_p),
                ("surface", ctypes.c_void_p)]


_WL_EGL_WINDOW_VERSION = 3


def _egl_lib():
    global _egl
    if _egl is None:
        _egl = ctypes.CDLL("libwayland-egl.so.1")
        _egl.wl_egl_window_resize.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int]
        _egl.wl_egl_window_resize.restype = None
    return _egl


def _find_egl_window(base, surface, span=_SCAN_BYTES):
    """The wl_egl_window in the _GLFWwindow struct at ``base``: a pointer
    to a struct whose version word is WL_EGL_WINDOW_VERSION, whose size is
    sane and whose surface field is OUR wl_surface."""
    if not base or not surface:
        return None
    size = ctypes.sizeof(_wl_egl_window)
    for off in range(0, span, _CHUNK):
        chunk = _read(base + off, _CHUNK)
        if chunk is None:
            return None
        for i in range(0, _CHUNK, 8):
            cand = struct.unpack_from("<Q", chunk, i)[0]
            if not cand or cand & 7:
                continue
            raw = _read(cand, size)
            if raw is None:
                continue
            win = _wl_egl_window.from_buffer_copy(raw)
            if (win.version == _WL_EGL_WINDOW_VERSION and win.surface == surface
                    and 0 < win.width < 65536 and 0 < win.height < 65536):
                return cand
    return None


def _ensure_egl_window():
    """The EGL window, found at attach — or here, lazily, after a hotswap
    of this module (attach runs once at boot; the surface is kept). A failed
    scan is remembered as 0 so it isn't repeated every frame."""
    win = _STATE["egl_window"]
    if win is None and _STATE.get("surface"):
        try:
            _, wl = _c()
            glfw_window = wl.wl_proxy_get_user_data(_STATE["surface"])
            win = _find_egl_window(glfw_window, _STATE["surface"]) or 0
        except Exception:
            win = 0
        _STATE["egl_window"] = win
    return win or None


def offset_available():
    """Can the surface carry a buffer offset (a client-side window move)?"""
    return bool(_ensure_egl_window())


def set_surface_offset(dx, dy):
    """Arm the buffer offset for the NEXT swap: wl_egl_window_resize with
    the window's current size and (dx, dy) — the EGL driver's resize
    callback records them and the swap's wl_surface.attach carries them,
    which the compositor applies as a move of the window by (dx, dy). Call
    right before the swap, AFTER any GLFW resize of the frame (GLFW's own
    wl_egl_window_resize resets the offset to 0). Returns True when armed."""
    win = _ensure_egl_window()
    if not win:
        return False
    raw = _read(win, ctypes.sizeof(_wl_egl_window))
    if raw is None:
        _STATE["egl_window"] = None
        return False
    cur = _wl_egl_window.from_buffer_copy(raw)
    _egl_lib().wl_egl_window_resize(win, cur.width, cur.height, int(dx), int(dy))
    _STATE["offset_armed"] = bool(dx or dy)
    return True


def clear_surface_offset():
    """Next frame's start: an armed offset is one-shot on the driver side
    (Mesa zeroes it after the attach) but the struct keeps the values —
    reset them so no later attach can carry it again."""
    if _STATE["offset_armed"]:
        set_surface_offset(0, 0)
        _STATE["offset_armed"] = False


def _c():
    global _libc, _wl
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.process_vm_readv.argtypes = [ctypes.c_int, ctypes.POINTER(_iovec), ctypes.c_ulong,
                                           ctypes.POINTER(_iovec), ctypes.c_ulong, ctypes.c_ulong]
        _libc.process_vm_readv.restype = ctypes.c_ssize_t
    if _wl is None:
        # The same soname GLFW dlopens, so this is the library its proxies
        # live in (dlopen of an already-loaded library returns its handle).
        _wl = ctypes.CDLL("libwayland-client.so.0")
        _wl.wl_proxy_get_user_data.argtypes = [ctypes.c_void_p]
        _wl.wl_proxy_get_user_data.restype = ctypes.c_void_p
        _wl.wl_proxy_get_class.argtypes = [ctypes.c_void_p]
        _wl.wl_proxy_get_class.restype = ctypes.c_char_p
        _wl.wl_proxy_get_version.argtypes = [ctypes.c_void_p]
        _wl.wl_proxy_get_version.restype = ctypes.c_uint32
        _wl.wl_proxy_add_listener.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        _wl.wl_proxy_add_listener.restype = ctypes.c_int
        # variadic: the static prefix is typed, the variable args passed as
        # explicit ctypes instances (ints/pointers only: SysV passes them
        # in registers exactly as the generated C stubs do)
        _wl.wl_proxy_marshal_flags.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                               ctypes.c_uint32, ctypes.c_uint32]
        _wl.wl_proxy_marshal_flags.restype = ctypes.c_void_p
        _wl.wl_display_roundtrip.argtypes = [ctypes.c_void_p]
        _wl.wl_display_roundtrip.restype = ctypes.c_int
        _wl.wl_display_flush.argtypes = [ctypes.c_void_p]
        _wl.wl_display_flush.restype = ctypes.c_int
    return _libc, _wl


# ---------------------------------------------------------------------------
# Safe memory reads + the proxy scan
# ---------------------------------------------------------------------------

def _iface_addr(wl, name):
    """Address of one of libwayland-client's exported wl_interface structs."""
    return ctypes.addressof(ctypes.c_char.in_dll(wl, name))


def _read(addr, n):
    """n bytes at addr, or None when any of it is unmapped (EFAULT) — a
    syscall copy, never a dereference, so a stale pointer can't fault."""
    if not addr or n <= 0:
        return None
    libc, _ = _c()
    buf = ctypes.create_string_buffer(n)
    local = _iovec(ctypes.addressof(buf), n)
    remote = _iovec(addr, n)
    got = libc.process_vm_readv(os.getpid(), ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    return buf.raw if got == n else None


def _read_word(addr):
    raw = _read(addr, 8)
    return struct.unpack("<Q", raw)[0] if raw else None


def _read_cstr(addr, cap=48):
    raw = _read(addr, cap)
    if raw is None:
        # The string may sit right before an unmapped page: shrink
        for n in (32, 16, 8):
            raw = _read(addr, n)
            if raw is not None:
                break
    return raw.split(b"\0", 1)[0] if raw else None


def _proxy_class(ptr):
    """Interface name of a wl_proxy candidate, read defensively: wl_proxy
    begins with wl_object {const wl_interface *interface; ...} and
    wl_interface begins with {const char *name; ...}."""
    if not ptr or ptr & 7:
        return None
    iface = _read_word(ptr)
    if not iface or iface & 7:
        return None
    name_ptr = _read_word(iface)
    if not name_ptr:
        return None
    return _read_cstr(name_ptr)


def _find_proxy(base, class_name, span=_SCAN_BYTES):
    """First pointer-sized word in [base, base+span) that reads as a
    wl_proxy of interface `class_name` (bytes). Chunked so a struct near the
    end of a mapping still scans up to the edge."""
    if not base:
        return None
    for off in range(0, span, _CHUNK):
        chunk = _read(base + off, _CHUNK)
        if chunk is None:
            return None
        for i in range(0, _CHUNK, 8):
            cand = struct.unpack_from("<Q", chunk, i)[0]
            if cand and not (cand & 7) and _proxy_class(cand) == class_name:
                return cand
    return None


def _opcodes(proxy, names):
    """{name: opcode} for the proxy's requests, resolved from its interface's
    method table (the compiled xdg_toplevel_interface inside GLFW)."""
    iface_ptr = _read_word(proxy)
    raw = _read(iface_ptr, ctypes.sizeof(_wl_interface))
    if raw is None:
        return {}
    return _opcodes_of(_wl_interface.from_buffer_copy(raw), names)


def _opcodes_of(iface, names):
    """{name: opcode} from a wl_interface struct (a proxy's, read defensively,
    or one of libwayland's exported ones like wl_region_interface)."""
    count = iface.method_count
    methods_ptr = ctypes.cast(iface.methods, ctypes.c_void_p).value
    out = {}
    size = ctypes.sizeof(_wl_message)
    for op in range(max(0, min(count, 64))):
        mraw = _read(methods_ptr + op * size, size)
        if mraw is None:
            break
        name_ptr = struct.unpack_from("<Q", mraw, 0)[0]
        name = _read_cstr(name_ptr)
        if name in names:
            out[name.decode()] = op
    return out


# ---------------------------------------------------------------------------
# zwp_relative_pointer_v1, built by hand (not in libwayland-client): raw
# pointer motion in SCREEN space. A surface-drag reads the pointer in
# surface coordinates, and when the compositor moves the surface under it
# (its keep-on-screen push once the bottom edge leaves the screen) the
# surface-relative pointer jumps by the same amount - the drag read that
# as further movement, grew the window more, the compositor pushed again: a
# feedback loop that ran the top edge straight up to the screen edge.
# relative pointer is unaffected by where the surface is.
# ---------------------------------------------------------------------------

def _wl_message_array(entries, keep):
    """A wl_message[] from (name, signature, [interface addr or None, ...])."""
    arr = (_wl_message * max(1, len(entries)))()
    for i, (name, signature, types) in enumerate(entries):
        types_arr = (ctypes.c_void_p * max(1, len(types)))(*[t for t in types])
        keep.append(types_arr)
        arr[i].name = name
        arr[i].signature = signature
        arr[i].types = ctypes.addressof(types_arr)
    keep.append(arr)
    return arr


def _build_relative_pointer_interfaces(wl, keep):
    """The two wl_interface structs of relative-pointer-unstable-v1, kept
    alive for the proxies' lifetime (libwayland stores the pointers)."""
    rel = _wl_interface()
    manager = _wl_interface()
    keep.extend([rel, manager])
    rel_methods = _wl_message_array([(b"destroy", b"", [])], keep)
    rel_events = _wl_message_array([(b"relative_motion", b"uuffff", [None] * 6)], keep)
    rel.name = b"zwp_relative_pointer_v1"
    rel.version = 1
    rel.method_count = 1
    rel.methods = ctypes.cast(rel_methods, ctypes.POINTER(_wl_message))
    rel.event_count = 1
    rel.events = ctypes.cast(rel_events, ctypes.POINTER(_wl_message))
    mgr_methods = _wl_message_array([
        (b"destroy", b"", []),
        (b"get_relative_pointer", b"no", [ctypes.addressof(rel), _iface_addr(wl, "wl_pointer_interface")]),
    ], keep)
    manager.name = b"zwp_relative_pointer_manager_v1"
    manager.version = 1
    manager.method_count = 2
    manager.methods = ctypes.cast(mgr_methods, ctypes.POINTER(_wl_message))
    manager.event_count = 0
    manager.events = ctypes.cast(_wl_message_array([], keep), ctypes.POINTER(_wl_message))
    return manager, rel


_RELATIVE_MOTION_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32,
                                       ctypes.c_int32, ctypes.c_int32)


def _on_relative_motion(data, pointer, utime_hi, utime_lo, dx, dy, dx_unaccel, dy_unaccel):
    # wl_fixed_t: 24.8 fixed point. dx/dy are the coordinate deltas - the
    # pointer's on-screen motion.
    _CONN["rel_x"] += int(dx) / 256.0
    _CONN["rel_y"] += int(dy) / 256.0
    _CONN["rel_events"] += 1


def _setup_relative_pointer():
    """After the registry roundtrip: a relative pointer for our wl_pointer."""
    manager, pointer = _CONN["relative_manager"], _CONN["pointer"]
    if not manager or not pointer or _CONN["relative_pointer"]:
        return False
    _, wl = _c()
    rel_iface = _CONN["relative_iface"]
    # get_relative_pointer(new_id, wl_pointer) - "no"
    rel = wl.wl_proxy_marshal_flags(manager, 1, ctypes.addressof(rel_iface),
                                    wl.wl_proxy_get_version(manager), 0,
                                    ctypes.c_void_p(None), ctypes.c_void_p(pointer))
    if not rel:
        return False
    cb = _RELATIVE_MOTION_CB(_on_relative_motion)
    table = (ctypes.c_void_p * 1)(ctypes.cast(cb, ctypes.c_void_p).value)
    _CONN["keep"].extend([cb, table])
    wl.wl_proxy_add_listener(rel, ctypes.addressof(table), None)
    _CONN["relative_pointer"] = rel
    return True


def relative_motion_available():
    """Whether this connection has received relative motion, not just bound it.

    An agent seat can advertise the protocol and accept the subscription
    while delivering only absolute wl_pointer motion. Treating its untouched
    totals as a stationary hand makes surface-slide compensation erase every
    drag. Wait for an actual relative event before using these totals.
    """
    return bool(_CONN["relative_pointer"]) and _CONN["rel_events"] > 0


def relative_motion_total():
    """Accumulated screen-space pointer motion (px) since attach — an
    arbitrary origin; gestures latch a value and use the difference."""
    return _CONN["rel_x"], _CONN["rel_y"]


# ---------------------------------------------------------------------------
# xdg_toplevel_tag_v1, built by hand: a stable per-window TAG not an
# app id. A compositor that remembers floating sizes / positions keys on
# app id + tag (Lukas's Hyprland: `persistent_size`, ignore_title on), so
# without a tag every window of one app shared ONE remembered box and two
# @glfw_window roots opened on top of each other, each at the other's center.
# ---------------------------------------------------------------------------

def _build_toplevel_tag_interface(keep):
    """The xdg_toplevel_tag_manager_v1 wl_interface (xdg-toplevel-tag-v1.xml:
    destroy, set_toplevel_tag(toplevel, tag), set_toplevel_description)."""
    manager = _wl_interface()
    keep.append(manager)
    methods = _wl_message_array([
        (b"destroy", b"", []),
        (b"set_toplevel_tag", b"os", [None, None]),
        (b"set_toplevel_description", b"os", [None, None]),
    ], keep)
    manager.name = b"xdg_toplevel_tag_manager_v1"
    manager.version = 1
    manager.method_count = 3
    manager.methods = ctypes.cast(methods, ctypes.POINTER(_wl_message))
    manager.event_count = 0
    manager.events = ctypes.cast(_wl_message_array([], keep), ctypes.POINTER(_wl_message))
    return manager


def tag_available():
    return bool(_CONN["tag_manager"])


def set_toplevel_tag(toplevel, tag):
    """xdg_toplevel_tag_manager_v1.set_toplevel_tag(toplevel, tag): the
    window's stable identity for the compositor (an untranslated string
    like the @glfw_window name). True when the request went out."""
    manager = _CONN["tag_manager"]
    if not manager or not toplevel or not tag:
        return False
    _, wl = _c()
    wl.wl_proxy_marshal_flags(manager, 1, None, wl.wl_proxy_get_version(manager), 0,
                              ctypes.c_void_p(toplevel), ctypes.c_char_p(str(tag).encode()))
    if _CONN["display"]:
        wl.wl_display_flush(_CONN["display"])
    return True


# ---------------------------------------------------------------------------
# Own seat + pointer: the press serial
# ---------------------------------------------------------------------------

_GLOBAL_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.c_char_p, ctypes.c_uint32)
_GLOBAL_REMOVE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
_ENTER_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                             ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32)
_LEAVE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)
_BUTTON_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)
_ANY_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                           ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)


def _on_global(data, registry, name, interface, version):
    if interface == b"zwp_relative_pointer_manager_v1" and _CONN["relative_manager"] is None:
        _, wl = _c()
        manager_iface = _CONN.get("relative_manager_iface")
        if manager_iface is not None:
            _CONN["relative_manager"] = wl.wl_proxy_marshal_flags(
                registry, _CONN["opcodes"]["bind"], ctypes.addressof(manager_iface), 1, 0,
                ctypes.c_uint32(name), ctypes.c_char_p(b"zwp_relative_pointer_manager_v1"),
                ctypes.c_uint32(1), ctypes.c_void_p(None))
        return
    if interface == b"xdg_toplevel_tag_manager_v1" and _CONN["tag_manager"] is None:
        _, wl = _c()
        tag_iface = _CONN.get("tag_iface")
        if tag_iface is not None:
            _CONN["tag_manager"] = wl.wl_proxy_marshal_flags(
                registry, _CONN["opcodes"]["bind"], ctypes.addressof(tag_iface), 1, 0,
                ctypes.c_uint32(name), ctypes.c_char_p(b"xdg_toplevel_tag_manager_v1"),
                ctypes.c_uint32(1), ctypes.c_void_p(None))
        return
    if interface == b"wl_compositor" and _CONN["compositor"] is None:
        _, wl = _c()
        comp_iface = _iface_addr(wl, "wl_compositor_interface")
        ver = min(int(version), 4)
        _CONN["compositor"] = wl.wl_proxy_marshal_flags(
            registry, _CONN["opcodes"]["bind"], comp_iface, ver, 0,
            ctypes.c_uint32(name), ctypes.c_char_p(b"wl_compositor"),
            ctypes.c_uint32(ver), ctypes.c_void_p(None))
        return
    if interface == b"wl_seat" and _CONN["seat"] is None:
        _, wl = _c()
        seat_iface = _iface_addr(wl, "wl_seat_interface")
        ver = min(int(version), 5)
        # wl_registry.bind(name, interface, version, new_id) - "usun"
        seat = wl.wl_proxy_marshal_flags(registry, _CONN["opcodes"]["bind"], seat_iface, ver, 0,
                                         ctypes.c_uint32(name), ctypes.c_char_p(b"wl_seat"),
                                         ctypes.c_uint32(ver), ctypes.c_void_p(None))
        _CONN["seat"] = seat
        if seat:
            pointer_iface = _iface_addr(wl, "wl_pointer_interface")
            # wl_seat.get_pointer(new_id) - "n", opcode 0
            ptr = wl.wl_proxy_marshal_flags(seat, 0, pointer_iface, ver, 0, ctypes.c_void_p(None))
            _CONN["pointer"] = ptr
            if ptr:
                wl.wl_proxy_add_listener(ptr, _CONN["pointer_listener"], None)


def _on_global_remove(data, registry, name):
    pass


def _on_enter(data, pointer, serial, surface, sx, sy):
    _CONN["enter_serial"] = int(serial)


def _on_leave(data, pointer, serial, surface):
    # Under an exclusive grab the compositor sends no leave until the release,
    # so a leave with buttons still held means the grab was broken (a freeze
    # tripped Mutter's not-responding handling, a focus steal): the release
    # will be delivered elsewhere and GLFW's button state stays PRESS for
    # good. Mark the buttons as up, and mask GLFW's stale reading until
    # its next real event on them (note_glfw_button).
    held = _CONN["held"]
    if held:
        _CONN["masked"].update(held)
        held.clear()


def _on_button(data, pointer, serial, time_ms, button, state):
    glfw_button = _EVDEV_TO_GLFW.get(int(button), int(button))
    if int(state) == _BTN_PRESSED:
        if not _CONN["held"]:
            # First button in a button sequence: the compositor's pointer
            # grab starts here. A chord's second button gets its own serial.
            _CONN["grab_serial"] = int(serial)
        _CONN["press_serial"] = int(serial)
        _CONN["press_button"] = glfw_button
        _CONN["held"].add(glfw_button)
    else:
        _CONN["held"].discard(glfw_button)


def _on_any(*args):
    pass


def _build_listeners():
    """Function tables sized to the interfaces' event counts, every slot
    filled (libwayland invokes the slot for any event that arrives)."""
    _, wl = _c()
    reg_iface = _wl_interface.in_dll(wl, "wl_registry_interface")
    ptr_iface = _wl_interface.in_dll(wl, "wl_pointer_interface")
    keep = _CONN["keep"]
    reg_fns = [_GLOBAL_CB(_on_global), _GLOBAL_REMOVE_CB(_on_global_remove)]
    while len(reg_fns) < reg_iface.event_count:
        reg_fns.append(_ANY_CB(_on_any))
    ptr_fns = [_ENTER_CB(_on_enter), _LEAVE_CB(_on_leave), _ANY_CB(_on_any), _BUTTON_CB(_on_button)]
    while len(ptr_fns) < ptr_iface.event_count:
        ptr_fns.append(_ANY_CB(_on_any))
    keep.extend(reg_fns + ptr_fns)
    reg_tab = (ctypes.c_void_p * len(reg_fns))(*[ctypes.cast(f, ctypes.c_void_p).value for f in reg_fns])
    ptr_tab = (ctypes.c_void_p * len(ptr_fns))(*[ctypes.cast(f, ctypes.c_void_p).value for f in ptr_fns])
    keep.extend([reg_tab, ptr_tab])
    return ctypes.addressof(reg_tab), ctypes.addressof(ptr_tab)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

def _bind_connection(display):
    """Once per wl_display: the registry, and through it the seat + pointer
    (press serials), the compositor (input regions), the relative pointer
    manager (screen-space right-drag motion) and the toplevel-tag manager.
    Every window of the process shares these. False with the reason in
    _CONN["error"] when the seat is missing."""
    _, wl = _c()
    if _CONN["display"] != display:
        # A new connection (new GLFW window): the old seat/pointer/registry
        # proxies are dangling - never marshal on them again.
        _CONN.update(seat=None, pointer=None, registry=None, compositor=None, press_serial=0,
                     grab_serial=0, press_button=None, held=set(), enter_serial=0,
                     relative_manager=None, relative_pointer=None, tag_manager=None,
                     opcodes={}, error=None)
        _CONN["masked"].clear()
    _CONN["display"] = display
    if _CONN["registry"]:
        return bool(_CONN["pointer"])
    keep = _CONN["keep"]
    if _CONN.get("relative_manager_iface") is None:
        manager_iface, rel_iface = _build_relative_pointer_interfaces(wl, keep)
        _CONN["relative_manager_iface"] = manager_iface
        _CONN["relative_iface"] = rel_iface
    if _CONN.get("tag_iface") is None:
        _CONN["tag_iface"] = _build_toplevel_tag_interface(keep)
    reg_tab, ptr_tab = _build_listeners()
    _CONN["pointer_listener"] = ptr_tab
    # registry: wl_display.get_registry (opcode 1, "n"); bind is opcode 0
    _CONN["opcodes"]["bind"] = 0
    reg_iface = _iface_addr(wl, "wl_registry_interface")
    registry = wl.wl_proxy_marshal_flags(display, 1, reg_iface, wl.wl_proxy_get_version(display), 0,
                                         ctypes.c_void_p(None))
    if not registry:
        _CONN["error"] = "wl_display.get_registry failed"
        return False
    _CONN["registry"] = registry
    wl.wl_proxy_add_listener(registry, reg_tab, None)
    wl.wl_display_roundtrip(display)       # globals → seat → pointer, compositor, managers
    if not _CONN["pointer"]:
        _CONN["error"] = "no wl_seat advertised"
        return False
    _setup_relative_pointer()               # screen-space motion for the right-drag
    # Input region plumbing (set_input_rect): wl_compositor.create_region,
    # wl_region.add/destroy - by name.
    if _CONN["compositor"]:
        _CONN["opcodes"].update(_opcodes(_CONN["compositor"], {b"create_region"}))
        region_iface = _wl_interface.in_dll(wl, "wl_region_interface")
        _CONN["opcodes"].update({"region_" + k: v for k, v in
                                 _opcodes_of(region_iface, {b"add", b"destroy"}).items()})
    _CONN["error"] = None
    return True


def attach(window, tag=None):
    """Find the window's xdg_toplevel (+ xdg_surface, EGL window), bind the
    connection's seat + pointer for serials (once per display) and chain a
    GLFW mouse-button callback that clears the post-grab mask. Render
    thread, once per window, AFTER Melty.init_input_backend (so the chain
    runs first). ``tag`` (a stable string, the @glfw_window name) is set as
    the toplevel's xdg tag when the compositor offers the protocol.
    Returns True when moves are available; the reason for a False sits in
    last_error()."""
    if _STATE["attached"] and _STATE["window"] == window:
        return available()
    _STATE["attached"] = True
    _STATE["window"] = window
    _STATE["tag"] = tag
    try:
        if glfw.get_platform() != glfw.PLATFORM_WAYLAND:
            _STATE["error"] = "not a Wayland session"
            return False
        _, wl = _c()
        display = glfw.get_wayland_display()
        surface = glfw.get_wayland_window(window)
        if not display or not surface:
            _STATE["error"] = "GLFW exposes no Wayland display/surface"
            return False
        _STATE["display"] = display
        native_window = glfw.is_native_window(window)
        glfw_window = None if native_window else wl.wl_proxy_get_user_data(surface)
        toplevel = window.toplevel if native_window else _find_proxy(glfw_window, b"xdg_toplevel")
        if not toplevel or wl.wl_proxy_get_class(toplevel) != b"xdg_toplevel":
            _STATE["error"] = ("no xdg_toplevel in the GLFW window (libdecor owns "
                               "the surface? Toggles.Melty.wayland_native_frame)")
            return False
        ops = _opcodes(toplevel, {b"move", b"resize"})
        if "move" not in ops or "resize" not in ops:
            _STATE["error"] = f"xdg_toplevel request table unreadable: {ops}"
            return False
        _STATE["toplevel"] = toplevel
        _STATE["opcodes"].update(ops)
        # The xdg_surface beside it: set_window_geometry tells the compositor
        # the window's REAL edges (the content rect inside the shadow margin).
        xdg_surface = window.xdg_surface if native_window else _find_proxy(glfw_window, b"xdg_surface")
        if xdg_surface and wl.wl_proxy_get_class(xdg_surface) == b"xdg_surface":
            geo = _opcodes(xdg_surface, {b"set_window_geometry"})
            if "set_window_geometry" in geo:
                _STATE["xdg_surface"] = xdg_surface
                _STATE["opcodes"].update(geo)
        _STATE["surface"] = surface
        # The EGL window beside them: its attach offset is the client-side
        # window rect the OS-edge physics rides (os_frame.flush at the flip).
        _STATE["egl_window"] = window.egl_window if native_window else _find_egl_window(glfw_window, surface)
        if not _bind_connection(display):
            _STATE["error"] = _CONN["error"]
            return False
        # wl_surface.set_input_region - by name, on this window's surface.
        _STATE["opcodes"].update({"surface_" + k: v for k, v in
                                  _opcodes(surface, {b"set_input_region"}).items()})
        if tag:
            set_toplevel_tag(toplevel, tag)
        _STATE["prev_button_cb"] = glfw.set_mouse_button_callback(window, _glfw_button)
        _STATE["error"] = None
        return True
    except Exception as e:      # never take the studio down over a move
        _STATE["error"] = f"{type(e).__name__}: {e}"
        return False


def detach(window):
    """The window is going away (Surface.destroy, before glfw.destroy_window):
    forget its proxies — GLFW destroys them with the window. The
    connection's seat, pointer, registry and managers stay bound for the
    other windows; nothing of theirs is per window."""
    if _STATE["window"] != window:
        return
    _STATE.update(attached=False, window=None, toplevel=None, xdg_surface=None, surface=None,
                  egl_window=None, offset_armed=False, prev_button_cb=None, tag=None, opcodes={})


def available():
    return bool(_STATE["toplevel"] and _CONN["pointer"] and _STATE["error"] is None)


def last_error():
    return _STATE["error"]


def press_serial():
    return _CONN["press_serial"]


def enter_serial():
    """Serial of the last wl_pointer.enter on this client: it changes when the
    pointer comes back after a compositor grab — the grab-over signal."""
    return _CONN["enter_serial"]


def _grab(opcode_name, *extra):
    """Send the move/resize request. The compositor honours it only with
    the serial of the press it keys the pointer grab to — for a single
    button that is simply the press; with two buttons held it is either the
    sequence's first press or the latest one depending on the compositor, so
    both are sent when they differ:
    exactly one matches and starts the grab, the other is ignored."""
    if not available():
        return False
    serials = []
    for serial in (_CONN["grab_serial"], _CONN["press_serial"]):
        if serial and serial not in serials:
            serials.append(serial)
    if not serials:
        return False
    _, wl = _c()
    toplevel = _STATE["toplevel"]
    for serial in serials:
        args = [ctypes.c_void_p(_CONN["seat"]), ctypes.c_uint32(serial)] + list(extra)
        wl.wl_proxy_marshal_flags(toplevel, _STATE["opcodes"][opcode_name], None,
                                  wl.wl_proxy_get_version(toplevel), 0, *args)
    wl.wl_display_flush(_STATE["display"])
    # The grab swallows the release of EVERY button held until now (two
    # when a second button joined before the grab).
    held = set(_CONN["held"])
    if _CONN["press_button"] is not None:
        held.add(_CONN["press_button"])
    _CONN["masked"].update(held)
    return True


def toplevel_proxy():
    """The attached window's xdg_toplevel proxy (for set_parent), or None."""
    return _STATE.get("toplevel")


def set_parent(child_toplevel, parent_toplevel):
    """xdg_toplevel.set_parent(child, parent): the compositor keeps the
    child stacked above its parent and minimises them together (app.py's
    child surfaces). Both proxies come from toplevel_proxy() of the
    respective attached windows. True when the request went out."""
    if not child_toplevel:
        return False
    try:
        _, wl = _c()
        opcodes = _opcodes(child_toplevel, (b"set_parent",))
        if "set_parent" not in opcodes:
            return False
        wl.wl_proxy_marshal_flags(child_toplevel, opcodes["set_parent"], None,
                                  wl.wl_proxy_get_version(child_toplevel), 0,
                                  ctypes.c_void_p(parent_toplevel or 0))
        if _STATE.get("display"):
            wl.wl_display_flush(_STATE["display"])
        return True
    except Exception as ex:
        _STATE["error"] = f"set_parent: {ex}"
        return False


def begin_move(window=None):
    """xdg_toplevel.move with the held press's serial: the compositor moves
    the window from here (Super+drag semantics). The pressed button is
    masked until GLFW reports its next real event — see button_masked."""
    return _grab("move")


def begin_resize(window=None, edges=EDGE_BOTTOM_RIGHT):
    """xdg_toplevel.resize from the given edge/corner (EDGE_* constants)."""
    return _grab("resize", ctypes.c_uint32(int(edges)))


def geometry_available():
    return bool(_STATE["xdg_surface"] and "set_window_geometry" in _STATE["opcodes"])


def set_window_geometry(x, y, w, h):
    """xdg_surface.set_window_geometry: the rect of the surface the
    compositor treats as the window — placement, edge constraints, snapping,
    maximize sizes all use it — so a shadow margin outside it may hang off
    the screen. Double-buffered: applies at the next commit (swap). A
    compositor configure then names a GEOMETRY size; GLFW applies it to the
    surface, and titlebar.on_surface_resized grows the surface back by the
    margin."""
    if not geometry_available():
        return False
    _, wl = _c()
    xdg_surface = _STATE["xdg_surface"]
    wl.wl_proxy_marshal_flags(xdg_surface, _STATE["opcodes"]["set_window_geometry"], None,
                              wl.wl_proxy_get_version(xdg_surface), 0,
                              ctypes.c_int32(int(x)), ctypes.c_int32(int(y)),
                              ctypes.c_int32(int(w)), ctypes.c_int32(int(h)))
    wl.wl_display_flush(_STATE["display"])
    return True


def input_region_available():
    ops = {**_CONN["opcodes"], **_STATE["opcodes"]}
    return bool(_CONN["compositor"] and _STATE["surface"]
                and all(k in ops for k in ("create_region", "surface_set_input_region",
                                           "region_add", "region_destroy")))


def set_input_rect(rect):
    """wl_surface.set_input_region: `rect` = (x, y, w, h) in surface pixels
    that receives pointer input — the CONTENT rect of the frameless window,
    so clicks in its transparent shadow margin fall through to whatever is
    behind; None restores the whole surface. Takes effect at the next
    commit (swap). The wl_region is a one-shot: the compositor copies it at
    set time, so it is destroyed right after."""
    if not input_region_available():
        return False
    _, wl = _c()
    ops = {**_CONN["opcodes"], **_STATE["opcodes"]}
    surface = _STATE["surface"]
    if rect is None:
        wl.wl_proxy_marshal_flags(surface, ops["surface_set_input_region"], None,
                                  wl.wl_proxy_get_version(surface), 0, ctypes.c_void_p(None))
    else:
        x, y, w, h = (int(v) for v in rect)
        region_iface = _iface_addr(wl, "wl_region_interface")
        compositor = _CONN["compositor"]
        region = wl.wl_proxy_marshal_flags(compositor, ops["create_region"], region_iface,
                                           wl.wl_proxy_get_version(compositor), 0, ctypes.c_void_p(None))
        if not region:
            return False
        version = wl.wl_proxy_get_version(region)
        wl.wl_proxy_marshal_flags(region, ops["region_add"], None, version, 0,
                                  ctypes.c_int32(x), ctypes.c_int32(y), ctypes.c_int32(w), ctypes.c_int32(h))
        wl.wl_proxy_marshal_flags(surface, ops["surface_set_input_region"], None,
                                  wl.wl_proxy_get_version(surface), 0, ctypes.c_void_p(region))
        wl.wl_proxy_marshal_flags(region, ops["region_destroy"], None, version,
                                  WL_MARSHAL_FLAG_DESTROY)
    wl.wl_display_flush(_STATE["display"])
    return True


def button_masked(button):
    """True while GLFW's level state for `button` is a stale PRESS: the
    compositor's grab took the release. Polls (imgui, LSDStudio.on_mouse)
    read the button as up while this holds."""
    return button in _CONN["masked"]


def masked_buttons():
    return set(_CONN["masked"])


def button_held(button):
    """Our own wl_pointer's level state for `button` (GLFW index): True /
    False, or None when no pointer is bound (X11, attach failed) — then
    there is no independent truth and the caller must not trust this."""
    if not _CONN["pointer"]:
        return None
    return button in _CONN["held"]


def note_glfw_button(button, action):
    """GLFW saw a real event for `button` — its state is truthful again."""
    _CONN["masked"].discard(button)


def _glfw_button(window, button, action, mods):
    note_glfw_button(button, action)
    prev = _STATE["prev_button_cb"]
    if prev is not None:
        prev(window, button, action, mods)
