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

import glfw

# Survives reloads (module re-exec reuses the existing dict).
_STATE = globals().get("_STATE") or {
    "attached": False, "window": None, "display": None, "toplevel": None,
    "seat": None, "pointer": None, "registry": None, "compositor": None, "surface": None,
    "xdg_surface": None,
    "press_serial": 0, "grab_serial": 0,
    "press_button": None, "held": set(), "enter_serial": 0, "masked": set(), "keep": [],
    "opcodes": {}, "prev_button_cb": None, "error": None,
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
    if interface == b"wl_compositor" and _STATE["compositor"] is None:
        _, wl = _c()
        comp_iface = _iface_addr(wl, "wl_compositor_interface")
        ver = min(int(version), 4)
        _STATE["compositor"] = wl.wl_proxy_marshal_flags(
            registry, _STATE["opcodes"]["bind"], comp_iface, ver, 0,
            ctypes.c_uint32(name), ctypes.c_char_p(b"wl_compositor"),
            ctypes.c_uint32(ver), ctypes.c_void_p(None))
        return
    if interface == b"wl_seat" and _STATE["seat"] is None:
        _, wl = _c()
        seat_iface = _iface_addr(wl, "wl_seat_interface")
        ver = min(int(version), 5)
        # wl_registry.bind(name, interface, version, new_id) - "usun"
        seat = wl.wl_proxy_marshal_flags(registry, _STATE["opcodes"]["bind"], seat_iface, ver, 0,
                                         ctypes.c_uint32(name), ctypes.c_char_p(b"wl_seat"),
                                         ctypes.c_uint32(ver), ctypes.c_void_p(None))
        _STATE["seat"] = seat
        if seat:
            pointer_iface = _iface_addr(wl, "wl_pointer_interface")
            # wl_seat.get_pointer(new_id) - "n", opcode 0
            ptr = wl.wl_proxy_marshal_flags(seat, 0, pointer_iface, ver, 0, ctypes.c_void_p(None))
            _STATE["pointer"] = ptr
            if ptr:
                wl.wl_proxy_add_listener(ptr, _STATE["pointer_listener"], None)


def _on_global_remove(data, registry, name):
    pass


def _on_enter(data, pointer, serial, surface, sx, sy):
    _STATE["enter_serial"] = int(serial)


def _on_leave(data, pointer, serial, surface):
    pass


def _on_button(data, pointer, serial, time_ms, button, state):
    glfw_button = _EVDEV_TO_GLFW.get(int(button), int(button))
    if int(state) == _BTN_PRESSED:
        if not _STATE["held"]:
            # First button in a button sequence: the compositor's pointer
            # grab starts here. A chord's second button gets its own serial.
            _STATE["grab_serial"] = int(serial)
        _STATE["press_serial"] = int(serial)
        _STATE["press_button"] = glfw_button
        _STATE["held"].add(glfw_button)
    else:
        _STATE["held"].discard(glfw_button)


def _on_any(*args):
    pass


def _build_listeners():
    """Function tables sized to the interfaces' event counts, every slot
    filled (libwayland invokes the slot for any event that arrives)."""
    _, wl = _c()
    reg_iface = _wl_interface.in_dll(wl, "wl_registry_interface")
    ptr_iface = _wl_interface.in_dll(wl, "wl_pointer_interface")
    keep = _STATE["keep"]
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

def attach(window):
    """Find the window's xdg_toplevel, bind a seat + pointer for serials and
    chain a GLFW mouse-button callback that clears the post-grab mask. Render
    thread, once, AFTER Melty.init_input_backend (so the chain runs first).
    Returns True when moves are available; the reason for a False sits in
    last_error()."""
    if _STATE["attached"] and _STATE["window"] == window:
        return available()
    _STATE["attached"] = True
    _STATE["window"] = window
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
        if _STATE["display"] != display:
            # A new connection (fresh GLFW window): the old seat/pointer/registry
            # proxies are stale: never marshal on them again.
            _STATE.update(seat=None, pointer=None, registry=None, press_serial=0, grab_serial=0,
                          press_button=None, held=set(), enter_serial=0)
        _STATE["display"] = display
        _STATE["masked"].clear()
        glfw_window = wl.wl_proxy_get_user_data(surface)
        toplevel = _find_proxy(glfw_window, b"xdg_toplevel")
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
        xdg_surface = _find_proxy(glfw_window, b"xdg_surface")
        if xdg_surface and wl.wl_proxy_get_class(xdg_surface) == b"xdg_surface":
            geo = _opcodes(xdg_surface, {b"set_window_geometry"})
            if "set_window_geometry" in geo:
                _STATE["xdg_surface"] = xdg_surface
                _STATE["opcodes"].update(geo)
        # registry: wl_display.get_registry (opcode 1: "interface"); bind is opcode 0
        reg_iface = _iface_addr(wl, "wl_registry_interface")
        _STATE["opcodes"]["bind"] = 0
        _STATE["surface"] = surface
        reg_tab, ptr_tab = _build_listeners()
        _STATE["pointer_listener"] = ptr_tab
        registry = wl.wl_proxy_marshal_flags(display, 1, reg_iface, wl.wl_proxy_get_version(display), 0,
                                             ctypes.c_void_p(None))
        if not registry:
            _STATE["error"] = "wl_display.get_registry failed"
            return False
        _STATE["registry"] = registry
        wl.wl_proxy_add_listener(registry, reg_tab, None)
        wl.wl_display_roundtrip(display)       # globals: seat → pointer, compositor
        if not _STATE["pointer"]:
            _STATE["error"] = "no wl_seat advertised"
            return False
        # Input region plumbing (set_input_rect): wl_compositor.create_region,
        # wl_region.add/destroy, wl_surface.set_input_region - by name.
        if _STATE["compositor"]:
            _STATE["opcodes"].update(_opcodes(_STATE["compositor"], {b"create_region"}))
            _STATE["opcodes"].update({"surface_" + k: v for k, v in
                                      _opcodes(surface, {b"set_input_region"}).items()})
            region_iface = _wl_interface.in_dll(wl, "wl_region_interface")
            _STATE["opcodes"].update({"region_" + k: v for k, v in
                                      _opcodes_of(region_iface, {b"add", b"destroy"}).items()})
        _STATE["prev_button_cb"] = glfw.set_mouse_button_callback(window, _glfw_button)
        _STATE["error"] = None
        return True
    except Exception as e:      # never take the studio down over a move
        _STATE["error"] = f"{type(e).__name__}: {e}"
        return False


def available():
    return bool(_STATE["toplevel"] and _STATE["pointer"] and _STATE["error"] is None)


def last_error():
    return _STATE["error"]


def press_serial():
    return _STATE["press_serial"]


def _grab(opcode_name, *extra):
    """Send the move/resize request. The compositor honours it only with
    the serial of the press it keys the pointer grab to — for a single
    button that is simply the press; for a CHORD (left+right = the
    top-left corner) it is either the sequence's first press or the latest
    one depending on the compositor, so both are sent when they differ:
    exactly one matches and starts the grab, the other is ignored."""
    if not available():
        return False
    serials = []
    for serial in (_STATE["grab_serial"], _STATE["press_serial"]):
        if serial and serial not in serials:
            serials.append(serial)
    if not serials:
        return False
    _, wl = _c()
    toplevel = _STATE["toplevel"]
    for serial in serials:
        args = [ctypes.c_void_p(_STATE["seat"]), ctypes.c_uint32(serial)] + list(extra)
        wl.wl_proxy_marshal_flags(toplevel, _STATE["opcodes"][opcode_name], None,
                                  wl.wl_proxy_get_version(toplevel), 0, *args)
    wl.wl_display_flush(_STATE["display"])
    # The grab swallows the release of every button held right now (a
    # left+right corner grab holds two).
    held = set(_STATE["held"])
    if _STATE["press_button"] is not None:
        held.add(_STATE["press_button"])
    _STATE["masked"].update(held)
    return True


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
    ops = _STATE["opcodes"]
    return bool(_STATE["compositor"] and _STATE["surface"]
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
    ops = _STATE["opcodes"]
    surface = _STATE["surface"]
    if rect is None:
        wl.wl_proxy_marshal_flags(surface, ops["surface_set_input_region"], None,
                                  wl.wl_proxy_get_version(surface), 0, ctypes.c_void_p(None))
    else:
        x, y, w, h = (int(v) for v in rect)
        region_iface = _iface_addr(wl, "wl_region_interface")
        compositor = _STATE["compositor"]
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
    return button in _STATE["masked"]


def masked_buttons():
    return set(_STATE["masked"])


def note_glfw_button(button, action):
    """GLFW saw a real event for `button` — its state is truthful again."""
    _STATE["masked"].discard(button)


def _glfw_button(window, button, action, mods):
    note_glfw_button(button, action)
    prev = _STATE["prev_button_cb"]
    if prev is not None:
        prev(window, button, action, mods)
