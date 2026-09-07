"""wp_color_management_v1 for the studio's Wayland surface.

Tags the GLFW window's wl_surface with an IMAGE DESCRIPTION so the
compositor knows what the pixels mean. Without a tag the surface is plain
sRGB and the compositor maps 1.0 to its SDR reference white; with a PQ tag
the presentation pass (scene_target.present, Toggles.HDR.output = "pq")
encodes the linear scRGB scene as BT.2020 + ST 2084 and colours above
white(1) / outside sRGB reach the panel.

Marshalled by hand over libwayland-client's public API exactly like
wayland_move.py (whose ctypes mirrors and helpers this reuses): the
interfaces are built as `wl_interface` structs from the protocol tables
below, opcodes are the message indices, a second wl_registry on GLFW's
connection binds `wp_color_manager_v1`. A compositor without the global
(GNOME, an SDR Hyprland session) leaves `available()` False and the studio
stays an untagged sRGB window.

Flow (`sync(window)` once per frame from Melty.post_frame; "auto", the
default, resolves to "pq" whenever the manager is bound — resolved_output):
    Toggles.HDR.output "pq"   → create_parametric_creator → set_primaries_named
                                 (bt2020) + set_tf_named (st2084_pq) +
                                 set_luminances (0, 10000, reference) → create →
                                 ready → get_surface → set_image_description
    Toggles.HDR.output "srgb" → unset_image_description
The next commit (GLFW's swap) applies it. Hotswap-safe module state.
"""
from __future__ import annotations

import ctypes

import glfw

from src.lsd.gl_gui.wayland_move import (_c, _iface_addr, _wl_interface, _wl_message,
                                         _wl_message_array)

_STATE = globals().get("_STATE") or {
    "display": None, "surface": None, "registry": None, "manager": None, "manager_version": 0,
    "cm_surface": None, "description": None, "applied": None, "wanted": None,
    "supported_tf": set(), "supported_primaries": set(), "supported_features": set(),
    "done": False, "ready": None, "failed": None, "identity": None,
    "reference_nits": None, "keep": [], "ifaces": {}, "error": None, "attached_to": None,
}

# wp_color_manager_v1 enums (color-management-v1.xml)
TF_EXT_LINEAR, TF_SRGB, TF_EXT_SRGB, TF_ST2084_PQ, TF_HLG = 5, 9, 10, 11, 13
PRIMARIES_SRGB, PRIMARIES_BT2020, PRIMARIES_DISPLAY_P3 = 1, 6, 9
FEATURE_PARAMETRIC, FEATURE_SET_LUMINANCES, FEATURE_WINDOWS_SCRGB = 1, 4, 7
INTENT_PERCEPTUAL = 0
_BIND_VERSION = 1      # v1: enough for a parametric PQ / scRGB description


# ---------------------------------------------------------------------------
# Interfaces (message tables lifted from the XML: opcode = index)
# ---------------------------------------------------------------------------

def _build_interfaces(wl, keep):
    """The five wl_interface structs this module speaks, kept alive for the
    proxies' lifetime. A message we never send may carry None types."""
    manager, surface, feedback = _wl_interface(), _wl_interface(), _wl_interface()
    creator, desc, info = _wl_interface(), _wl_interface(), _wl_interface()
    keep.extend([manager, surface, feedback, creator, desc, info])

    def fill(iface, name, version, methods, events):
        iface.name = name
        iface.version = version
        iface.method_count = len(methods)
        iface.methods = ctypes.cast(_wl_message_array(methods, keep), ctypes.POINTER(_wl_message))
        iface.event_count = len(events)
        iface.events = ctypes.cast(_wl_message_array(events, keep), ctypes.POINTER(_wl_message))

    a_desc, a_creator, a_surface, a_feedback, a_info = (ctypes.addressof(x) for x in
                                                        (desc, creator, surface, feedback, info))
    wl_surface = _iface_addr(wl, "wl_surface_interface")
    wl_output = _iface_addr(wl, "wl_output_interface")

    fill(info, b"wp_image_description_info_v1", 1, [], [
        (b"done", b"", []), (b"icc_file", b"hu", [None, None]),
        (b"primaries", b"iiiiiiii", [None] * 8), (b"primaries_named", b"u", [None]),
        (b"tf_power", b"u", [None]), (b"tf_named", b"u", [None]),
        (b"luminances", b"uuu", [None] * 3), (b"target_primaries", b"iiiiiiii", [None] * 8),
        (b"target_luminance", b"uu", [None, None]), (b"target_max_cll", b"u", [None]),
        (b"target_max_fall", b"u", [None]),
    ])
    fill(desc, b"wp_image_description_v1", 1, [
        (b"destroy", b"", []), (b"get_information", b"n", [a_info]),
    ], [
        (b"failed", b"us", [None, None]), (b"ready", b"u", [None]), (b"ready2", b"2uu", [None, None]),
    ])
    fill(creator, b"wp_image_description_creator_params_v1", 1, [
        (b"create", b"n", [a_desc]), (b"set_tf_named", b"u", [None]), (b"set_tf_power", b"u", [None]),
        (b"set_primaries_named", b"u", [None]), (b"set_primaries", b"iiiiiiii", [None] * 8),
        (b"set_luminances", b"uuu", [None] * 3),
        (b"set_mastering_display_primaries", b"iiiiiiii", [None] * 8),
        (b"set_mastering_luminance", b"uu", [None, None]),
        (b"set_max_cll", b"u", [None]), (b"set_max_fall", b"u", [None]),
    ], [])
    fill(surface, b"wp_color_management_surface_v1", 1, [
        (b"destroy", b"", []), (b"set_image_description", b"ou", [a_desc, None]),
        (b"unset_image_description", b"", []),
    ], [])
    fill(feedback, b"wp_color_management_surface_feedback_v1", 1, [
        (b"destroy", b"", []), (b"get_preferred", b"n", [a_desc]), (b"get_preferred_parametric", b"n", [a_desc]),
    ], [
        (b"preferred_changed", b"u", [None]), (b"preferred_changed2", b"2uu", [None, None]),
    ])
    fill(manager, b"wp_color_manager_v1", 1, [
        (b"destroy", b"", []), (b"get_output", b"no", [None, wl_output]),
        (b"get_surface", b"no", [a_surface, wl_surface]),
        (b"get_surface_feedback", b"no", [a_feedback, wl_surface]),
        (b"create_icc_creator", b"n", [None]), (b"create_parametric_creator", b"n", [a_creator]),
        (b"create_windows_scrgb", b"n", [a_desc]), (b"get_image_description", b"2no", [a_desc, None]),
        (b"create_windows_bt2100", b"3n", [a_desc]),
    ], [
        (b"supported_intent", b"u", [None]), (b"supported_feature", b"u", [None]),
        (b"supported_tf_named", b"u", [None]), (b"supported_primaries_named", b"u", [None]),
        (b"done", b"", []),
    ])
    return {"manager": manager, "surface": surface, "feedback": feedback,
            "creator": creator, "desc": desc, "info": info}


# ---------------------------------------------------------------------------
# Listeners
# ---------------------------------------------------------------------------

_GLOBAL_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.c_char_p, ctypes.c_uint32)
_GLOBAL_REMOVE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
_UINT_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
_VOID_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_FAILED_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p)
_UINT2_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)


def _on_global(data, registry, name, interface, version):
    if interface == b"wp_color_manager_v1" and _STATE["manager"] is None:
        _, wl = _c()
        ver = min(int(version), _BIND_VERSION)
        manager = wl.wl_proxy_marshal_flags(
            registry, 0, ctypes.addressof(_STATE["ifaces"]["manager"]), ver, 0,
            ctypes.c_uint32(name), ctypes.c_char_p(b"wp_color_manager_v1"),
            ctypes.c_uint32(ver), ctypes.c_void_p(None))
        if manager:
            _STATE["manager"] = manager
            _STATE["manager_version"] = ver
            wl.wl_proxy_add_listener(manager, _STATE["manager_listener"], None)


def _on_global_remove(data, registry, name):
    pass


def _on_intent(data, proxy, value):
    pass


def _on_feature(data, proxy, value):
    _STATE["supported_features"].add(int(value))


def _on_tf(data, proxy, value):
    _STATE["supported_tf"].add(int(value))


def _on_primaries(data, proxy, value):
    _STATE["supported_primaries"].add(int(value))


def _on_done(data, proxy):
    _STATE["done"] = True


def _on_failed(data, proxy, cause, msg):
    _STATE["failed"] = (int(cause), (msg or b"").decode(errors="replace"))
    _STATE["ready"] = False


def _on_ready(data, proxy, identity):
    _STATE["identity"] = int(identity)
    _STATE["ready"] = True


def _on_ready2(data, proxy, hi, lo):
    _STATE["identity"] = (int(hi) << 32) | int(lo)
    _STATE["ready"] = True


def _table(callbacks):
    cbs = [ctor(fn) for ctor, fn in callbacks]
    table = (ctypes.c_void_p * len(cbs))(*[ctypes.cast(cb, ctypes.c_void_p).value for cb in cbs])
    _STATE["keep"].extend(cbs + [table])
    return ctypes.addressof(table)


def _build_listeners():
    _STATE["registry_listener"] = _table([(_GLOBAL_CB, _on_global), (_GLOBAL_REMOVE_CB, _on_global_remove)])
    _STATE["manager_listener"] = _table([(_UINT_CB, _on_intent), (_UINT_CB, _on_feature),
                                         (_UINT_CB, _on_tf), (_UINT_CB, _on_primaries),
                                         (_VOID_CB, _on_done)])
    _STATE["desc_listener"] = _table([(_FAILED_CB, _on_failed), (_UINT_CB, _on_ready),
                                      (_UINT2_CB, _on_ready2)])


# ---------------------------------------------------------------------------
# Attach / describe / apply
# ---------------------------------------------------------------------------

def attach(display, surface) -> bool:
    """Bind the colour manager on `display` (a wl_display*) for `surface`
    (a wl_surface*). Returns True when the compositor offers it; the reason
    for a False sits in last_error(). Idempotent per (display, surface)."""
    if _STATE["attached_to"] == (display, surface):
        return available()
    _STATE["attached_to"] = (display, surface)
    try:
        _, wl = _c()
        if not display or not surface:
            _STATE["error"] = "no Wayland display/surface"
            return False
        if _STATE["display"] != display:
            _STATE.update(registry=None, manager=None, cm_surface=None, description=None,
                          applied=None, done=False, supported_tf=set(), supported_primaries=set(),
                          supported_features=set())
        _STATE.update(display=display, surface=surface)
        if not _STATE["ifaces"]:
            _STATE["ifaces"] = _build_interfaces(wl, _STATE["keep"])
            _build_listeners()
        if not hasattr(wl, "_melty_color_bound"):
            wl.wl_proxy_destroy.argtypes = [ctypes.c_void_p]
            wl.wl_proxy_destroy.restype = None
            wl._melty_color_bound = True
        if _STATE["registry"] is None:
            reg_iface = _iface_addr(wl, "wl_registry_interface")
            registry = wl.wl_proxy_marshal_flags(display, 1, reg_iface, wl.wl_proxy_get_version(display), 0,
                                                 ctypes.c_void_p(None))
            if not registry:
                _STATE["error"] = "wl_display.get_registry failed"
                return False
            _STATE["registry"] = registry
            wl.wl_proxy_add_listener(registry, _STATE["registry_listener"], None)
            wl.wl_display_roundtrip(display)      # globals → manager bound
            wl.wl_display_roundtrip(display)      # manager's supported_* + done
        if _STATE["manager"] is None:
            _STATE["error"] = "compositor offers no wp_color_manager_v1"
            return False
        _STATE["error"] = None
        return True
    except Exception as e:      # never take the studio down over a colour tag
        _STATE["error"] = f"{type(e).__name__}: {e}"
        return False


def available() -> bool:
    return bool(_STATE["manager"] and _STATE["error"] is None)


def last_error():
    return _STATE["error"]


def supported() -> dict:
    return {"tf": sorted(_STATE["supported_tf"]), "primaries": sorted(_STATE["supported_primaries"]),
            "features": sorted(_STATE["supported_features"]), "version": _STATE["manager_version"]}


def _destroy(key):
    proxy = _STATE.get(key)
    if proxy:
        _, wl = _c()
        wl.wl_proxy_marshal_flags(proxy, 0, ctypes.c_void_p(None), wl.wl_proxy_get_version(proxy),
                                  1)      # WL_MARSHAL_FLAG_DESTROY: request 0 is destroy on all of these
        _STATE[key] = None


def create_description(primaries: int, tf: int, reference_nits: float | None = None,
                       max_nits: float = 10000.0) -> bool:
    """A parametric image description; blocks for the compositor's ready /
    failed. On success it replaces `_STATE["description"]`."""
    _, wl = _c()
    manager, display = _STATE["manager"], _STATE["display"]
    if not manager:
        return False
    if tf not in _STATE["supported_tf"] or primaries not in _STATE["supported_primaries"]:
        _STATE["error"] = f"compositor lacks tf {tf} / primaries {primaries}: {supported()}"
        return False
    ifaces = _STATE["ifaces"]
    ver = _STATE["manager_version"]
    creator = wl.wl_proxy_marshal_flags(manager, 5, ctypes.addressof(ifaces["creator"]), ver, 0,
                                        ctypes.c_void_p(None))
    if not creator:
        _STATE["error"] = "create_parametric_creator failed"
        return False
    wl.wl_proxy_marshal_flags(creator, 3, ctypes.c_void_p(None), ver, 0, ctypes.c_uint32(primaries))
    wl.wl_proxy_marshal_flags(creator, 1, ctypes.c_void_p(None), ver, 0, ctypes.c_uint32(tf))
    used_reference = None
    if reference_nits and FEATURE_SET_LUMINANCES in _STATE["supported_features"]:
        # min_lum is in 0.0001 cd/m², the others in cd/m²
        wl.wl_proxy_marshal_flags(creator, 5, ctypes.c_void_p(None), ver, 0,
                                  ctypes.c_uint32(0), ctypes.c_uint32(int(max_nits)),
                                  ctypes.c_uint32(int(round(reference_nits))))
        used_reference = float(int(round(reference_nits)))
    _STATE.update(ready=None, failed=None, identity=None)
    # create(new_id) consumes the creator (the protocol destroys it); we
    # still free the client proxy.
    desc = wl.wl_proxy_marshal_flags(creator, 0, ctypes.addressof(ifaces["desc"]), ver, 0,
                                     ctypes.c_void_p(None))
    wl.wl_proxy_destroy(creator)
    if not desc:
        _STATE["error"] = "image description create failed"
        return False
    wl.wl_proxy_add_listener(desc, _STATE["desc_listener"], None)
    for _ in range(4):
        wl.wl_display_roundtrip(display)
        if _STATE["ready"] is not None:
            break
    if not _STATE["ready"]:
        _STATE["error"] = f"image description failed: {_STATE['failed']}"
        wl.wl_proxy_marshal_flags(desc, 0, ctypes.c_void_p(None), ver, 1)
        return False
    _destroy("description")
    _STATE["description"] = desc
    _STATE["reference_nits"] = used_reference
    return True


def set_surface_description(intent: int = INTENT_PERCEPTUAL) -> bool:
    """Tag the surface with the current description (create_description first)."""
    _, wl = _c()
    manager, surface, desc = _STATE["manager"], _STATE["surface"], _STATE["description"]
    if not (manager and surface and desc):
        return False
    ver = _STATE["manager_version"]
    if _STATE["cm_surface"] is None:
        cm = wl.wl_proxy_marshal_flags(manager, 2, ctypes.addressof(_STATE["ifaces"]["surface"]), ver, 0,
                                       ctypes.c_void_p(None), ctypes.c_void_p(surface))
        if not cm:
            _STATE["error"] = "get_surface failed"
            return False
        _STATE["cm_surface"] = cm
    wl.wl_proxy_marshal_flags(_STATE["cm_surface"], 1, ctypes.c_void_p(None), ver, 0,
                              ctypes.c_void_p(desc), ctypes.c_uint32(intent))
    wl.wl_display_flush(_STATE["display"])
    return True


def unset_surface_description() -> bool:
    _, wl = _c()
    cm = _STATE["cm_surface"]
    if not cm:
        return True
    wl.wl_proxy_marshal_flags(cm, 2, ctypes.c_void_p(None), _STATE["manager_version"], 0)
    wl.wl_display_flush(_STATE["display"])
    return True


def reference_nits():
    """The reference white the compositor was told for the applied PQ tag,
    or None (untagged / luminances unsupported → the compositor's default)."""
    return _STATE["reference_nits"] if _STATE["applied"] == "pq" else None


def applied():
    return _STATE["applied"]


def sync(window=None) -> str | None:
    """Once per frame: make the surface tag follow Toggles.HDR.output.
    Cheap when nothing changed. Returns the applied mode."""
    if not available():
        return _STATE["applied"]
    wanted = resolved_output()
    if wanted == _STATE["wanted"]:
        return _STATE["applied"]
    _STATE["wanted"] = wanted
    from src.lsd.gl_gui.toggles import Toggles
    try:
        if wanted == "pq":
            if create_description(PRIMARIES_BT2020, TF_ST2084_PQ,
                                  reference_nits=float(Toggles.HDR.pq_reference_nits)) \
                    and set_surface_description():
                _STATE["applied"] = "pq"
            else:
                print(f"wayland_color: PQ tag not applied: {last_error()}")
                _STATE["wanted"] = None       # retry next frame's toggle edge
        else:
            unset_surface_description()
            _STATE["applied"] = "srgb"
    except Exception as e:
        _STATE["error"] = f"{type(e).__name__}: {e}"
        print(f"wayland_color: {e}")
    return _STATE["applied"]


def resolved_output() -> str:
    """Toggles.HDR.output with "auto" resolved: PQ when the compositor offers
    colour management, sRGB otherwise."""
    from src.lsd.gl_gui.toggles import Toggles
    mode = Toggles.HDR.output
    if mode == "auto":
        return "pq" if available() else "srgb"
    return "pq" if mode == "pq" else "srgb"


def attach_window(window) -> bool:
    """attach() for the GLFW studio window."""
    try:
        if glfw.get_platform() != glfw.PLATFORM_WAYLAND:
            _STATE["error"] = "not a Wayland session"
            return False
        return attach(glfw.get_wayland_display(), glfw.get_wayland_window(window))
    except Exception as e:
        _STATE["error"] = f"{type(e).__name__}: {e}"
        return False