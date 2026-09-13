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
                                 set_luminances (desktop min, desktop max, reference) → create →
                                 ready → get_surface → set_image_description
    Toggles.HDR.output "srgb" → unset_image_description
The next commit (GLFW's swap) applies it. Hotswap-safe module state.

REFERENCE WHITE follows the desktop: `attach` also takes a
wp_color_management_surface_feedback_v1 and `query_preferred` reads the
compositor's PREFERRED parametric description for the surface
(get_preferred_parametric → get_information → the `luminances` event) — its
reference luminance is what the compositor shows an untagged sRGB window's
white as (Hyprland: the monitor's sdr_max_luminance, see Monitor.cpp
`applyCMType`), so `desired_reference()` hands it to set_luminances AND the
PQ encode and a colour of 1.0 lands at the same nits as every SDR window.
A `preferred_changed` event (the user moved the desktop's SDR white, or
the window changed monitor) re-queries and re-tags on the next sync.
Without the feature (no feedback, no luminances event) the fallback is
`Toggles.HDR.pq_reference_nits`; `Toggles.HDR.follow_desktop_white = False`
pins the toggle's value regardless.
"""
from __future__ import annotations

import ctypes

import glfw

from src.lsd.gl_gui.wayland_move import (_c, _iface_addr, _wl_interface, _wl_message,
                                         _wl_message_array)

# Two scopes (see wayland_move for the same split): _STATE is the SURFACE's
# - its wp_color_management_surface, feedback, description, and mode -
# swapped per Surface by surface.swap; _CONN is the CONNECTION's - the
# registry, the bound manager and what it supports, the interface tables,
# the listener tables and their ctypes callbacks (`keep`) - bound once per
# wl_display and shared by every window (never swapped, never collected
# with the window).
_STATE = globals().get("_STATE") or {
    "display": None, "surface": None,
    "cm_surface": None, "description": None, "applied": None, "wanted": None,
    "ready": None, "failed": None, "identity": None,
    "reference_nits": None, "error": None, "attached_to": None,
}
_CONN = globals().get("_CONN") or {
    "display": None, "registry": None, "manager": None, "manager_version": 0,
    "supported_tf": set(), "supported_primaries": set(), "supported_features": set(),
    "done": False, "keep": [], "ifaces": {}, "error": None,
}
# Fields added after the first release (a hotswapped module keeps the old dict).
for _key, _default in (("feedback", None), ("preferred_reference", None), ("preferred_luminances", None),
                       ("preferred_dirty", False),
                       ("preferred_query_failed", None), ("info_luminances", None), ("info_done", False),
                       ("synced_reference", None)):
    _STATE.setdefault(_key, _default)

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
_UINT3_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                             ctypes.c_uint32)
_ICC_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32)
_INT8_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, *([ctypes.c_int32] * 8))


def _on_global(data, registry, name, interface, version):
    if interface == b"wp_color_manager_v1" and _CONN["manager"] is None:
        _, wl = _c()
        ver = min(int(version), _BIND_VERSION)
        manager = wl.wl_proxy_marshal_flags(
            registry, 0, ctypes.addressof(_CONN["ifaces"]["manager"]), ver, 0,
            ctypes.c_uint32(name), ctypes.c_char_p(b"wp_color_manager_v1"),
            ctypes.c_uint32(ver), ctypes.c_void_p(None))
        if manager:
            _CONN["manager"] = manager
            _CONN["manager_version"] = ver
            wl.wl_proxy_add_listener(manager, _CONN["manager_listener"], None)


def _on_global_remove(data, registry, name):
    pass


def _on_intent(data, proxy, value):
    pass


def _on_feature(data, proxy, value):
    _CONN["supported_features"].add(int(value))


def _on_tf(data, proxy, value):
    _CONN["supported_tf"].add(int(value))


def _on_primaries(data, proxy, value):
    _CONN["supported_primaries"].add(int(value))


def _on_done(data, proxy):
    _CONN["done"] = True


def _on_failed(data, proxy, cause, msg):
    _STATE["failed"] = (int(cause), (msg or b"").decode(errors="replace"))
    _STATE["ready"] = False


def _on_ready(data, proxy, identity):
    _STATE["identity"] = int(identity)
    _STATE["ready"] = True


def _on_ready2(data, proxy, hi, lo):
    _STATE["identity"] = (int(hi) << 32) | int(lo)
    _STATE["ready"] = True


def _on_preferred_changed(data, proxy, *identity):
    # The compositor's preferred description for the surface changed (SDR
    # white moved, monitor changed): re-read it on the next sync, which
    # runs in post_frame — so ask for a frame (an idle app otherwise keeps
    # the stale tag until the next input).
    _STATE["preferred_dirty"] = True
    from src.lsd.gl_gui.utils.glfw_utils import request_render
    request_render()


def _on_info_luminances(data, proxy, min_lum, max_lum, reference):
    # min is in 0.0001 cd/m², max and reference in cd/m²
    _STATE["info_luminances"] = (int(min_lum) / 10000.0, float(max_lum), float(reference))


def _on_info_done(data, proxy):
    _STATE["info_done"] = True


def _on_info_icc(data, proxy, fd, size):
    import os
    try:
        os.close(int(fd))       # never sent for a parametric info; a leak otherwise
    except OSError:
        pass


def _on_info_ignore(data, proxy, *args):
    pass


def _table(callbacks):
    cbs = [ctor(fn) for ctor, fn in callbacks]
    table = (ctypes.c_void_p * len(cbs))(*[ctypes.cast(cb, ctypes.c_void_p).value for cb in cbs])
    _CONN["keep"].extend(cbs + [table])
    return ctypes.addressof(table)


def _build_listeners():
    _CONN["registry_listener"] = _table([(_GLOBAL_CB, _on_global), (_GLOBAL_REMOVE_CB, _on_global_remove)])
    _CONN["manager_listener"] = _table([(_UINT_CB, _on_intent), (_UINT_CB, _on_feature),
                                        (_UINT_CB, _on_tf), (_UINT_CB, _on_primaries),
                                        (_VOID_CB, _on_done)])
    _CONN["desc_listener"] = _table([(_FAILED_CB, _on_failed), (_UINT_CB, _on_ready),
                                     (_UINT2_CB, _on_ready2)])
    _CONN["feedback_listener"] = _table([(_UINT_CB, _on_preferred_changed),
                                         (_UINT2_CB, _on_preferred_changed)])
    # wp_image_description_info_v1 events, in table order (see _build_interfaces)
    _CONN["info_listener"] = _table([
        (_VOID_CB, _on_info_done), (_ICC_CB, _on_info_icc), (_INT8_CB, _on_info_ignore),
        (_UINT_CB, _on_info_ignore), (_UINT_CB, _on_info_ignore), (_UINT_CB, _on_info_ignore),
        (_UINT3_CB, _on_info_luminances), (_INT8_CB, _on_info_ignore), (_UINT2_CB, _on_info_ignore),
        (_UINT_CB, _on_info_ignore), (_UINT_CB, _on_info_ignore),
    ])


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
        if _STATE["display"] != display or _STATE["surface"] != surface:
            _STATE.update(cm_surface=None, description=None, applied=None, wanted=None,
                          feedback=None, preferred_reference=None, preferred_luminances=None,
                          preferred_dirty=False, preferred_query_failed=None, synced_reference=None)
        _STATE.update(display=display, surface=surface)
        if not hasattr(wl, "_melty_color_bound"):
            wl.wl_proxy_destroy.argtypes = [ctypes.c_void_p]
            wl.wl_proxy_destroy.restype = None
            wl._melty_color_bound = True
        if not _bind_connection(display):
            _STATE["error"] = _CONN["error"]
            return False
        _STATE["error"] = None
        query_preferred()         # the desktop's SDR white; a failure here only means the fallback
        return True
    except Exception as e:      # never take the studio down over a colour tag
        _STATE["error"] = f"{type(e).__name__}: {e}"
        return False


def _bind_connection(display) -> bool:
    """Once per wl_display: the registry, the colour manager and its
    supported_* tables. False with the reason in _CONN["error"]."""
    _, wl = _c()
    if _CONN["display"] != display:
        # A new connection (fresh GLFW instance): the old proxies are dangling.
        _CONN.update(registry=None, manager=None, manager_version=0, done=False, supported_tf=set(),
                     supported_primaries=set(), supported_features=set(), error=None)
    _CONN["display"] = display
    if not _CONN["ifaces"]:
        _CONN["ifaces"] = _build_interfaces(wl, _CONN["keep"])
        _build_listeners()
    if _CONN["registry"] is None:
        reg_iface = _iface_addr(wl, "wl_registry_interface")
        registry = wl.wl_proxy_marshal_flags(display, 1, reg_iface, wl.wl_proxy_get_version(display), 0,
                                             ctypes.c_void_p(None))
        if not registry:
            _CONN["error"] = "wl_display.get_registry failed"
            return False
        _CONN["registry"] = registry
        wl.wl_proxy_add_listener(registry, _CONN["registry_listener"], None)
        wl.wl_display_roundtrip(display)      # globals → manager bound
        wl.wl_display_roundtrip(display)      # manager's supported_* + done
    if _CONN["manager"] is None:
        _CONN["error"] = "compositor offers no wp_color_manager_v1"
        return False
    _CONN["error"] = None
    return True


def detach(surface=None):
    """The surface is going away (Surface.destroy, before glfw.destroy_window):
    destroy ITS colour-management objects — feedback, surface tag,
    description — and forget them. The connection's manager stays bound
    for the other windows. ``surface`` None: whatever _STATE holds."""
    if surface is not None and _STATE["surface"] != surface:
        return
    for key in ("feedback", "cm_surface", "description"):
        try:
            _destroy(key)
        except Exception:
            _STATE[key] = None
    _STATE.update(surface=None, attached_to=None, applied=None, wanted=None, synced_reference=None,
                  preferred_reference=None, preferred_luminances=None, preferred_dirty=False)


def available() -> bool:
    return bool(_CONN["manager"] and _STATE["error"] is None)


def last_error():
    return _STATE["error"]


def supported() -> dict:
    return {"tf": sorted(_CONN["supported_tf"]), "primaries": sorted(_CONN["supported_primaries"]),
            "features": sorted(_CONN["supported_features"]), "version": _CONN["manager_version"]}


def _destroy(key):
    proxy = _STATE.get(key)
    if proxy:
        _, wl = _c()
        wl.wl_proxy_marshal_flags(proxy, 0, ctypes.c_void_p(None), wl.wl_proxy_get_version(proxy),
                                  1)      # WL_MARSHAL_FLAG_DESTROY: request 0 is destroy on all of these
        _STATE[key] = None


def create_description(primaries: int, tf: int, reference_nits: float | None = None,
                       max_nits: float = 10000.0, min_nits: float = 0.0) -> bool:
    """A parametric image description; blocks for the compositor's ready /
    failed. On success it replaces `_STATE["description"]`."""
    _, wl = _c()
    manager, display = _CONN["manager"], _STATE["display"]
    if not manager:
        return False
    if tf not in _CONN["supported_tf"] or primaries not in _CONN["supported_primaries"]:
        _STATE["error"] = f"compositor lacks tf {tf} / primaries {primaries}: {supported()}"
        return False
    ifaces = _CONN["ifaces"]
    ver = _CONN["manager_version"]
    creator = wl.wl_proxy_marshal_flags(manager, 5, ctypes.addressof(ifaces["creator"]), ver, 0,
                                        ctypes.c_void_p(None))
    if not creator:
        _STATE["error"] = "create_parametric_creator failed"
        return False
    wl.wl_proxy_marshal_flags(creator, 3, ctypes.c_void_p(None), ver, 0, ctypes.c_uint32(primaries))
    wl.wl_proxy_marshal_flags(creator, 1, ctypes.c_void_p(None), ver, 0, ctypes.c_uint32(tf))
    used_reference = None
    if reference_nits and FEATURE_SET_LUMINANCES in _CONN["supported_features"]:
        # min_lum is in 0.0001 cd/m², the others in cd/m²
        wl.wl_proxy_marshal_flags(creator, 5, ctypes.c_void_p(None), ver, 0,
                                  ctypes.c_uint32(int(round(min_nits * 10000))), ctypes.c_uint32(int(max_nits)),
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
    wl.wl_proxy_add_listener(desc, _CONN["desc_listener"], None)
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
    manager, surface, desc = _CONN["manager"], _STATE["surface"], _STATE["description"]
    if not (manager and surface and desc):
        return False
    ver = _CONN["manager_version"]
    if _STATE["cm_surface"] is None:
        cm = wl.wl_proxy_marshal_flags(manager, 2, ctypes.addressof(_CONN["ifaces"]["surface"]), ver, 0,
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
    wl.wl_proxy_marshal_flags(cm, 2, ctypes.c_void_p(None), _CONN["manager_version"], 0)
    wl.wl_display_flush(_STATE["display"])
    return True


def reference_nits():
    """The reference white the compositor was told for the applied PQ tag,
    or None (untagged / luminances unsupported → the compositor's default)."""
    return _STATE["reference_nits"] if _STATE["applied"] == "pq" else None


def _ensure_feedback() -> bool:
    _, wl = _c()
    manager, surface = _CONN["manager"], _STATE["surface"]
    if not (manager and surface):
        return False
    if _STATE["feedback"] is None:
        fb = wl.wl_proxy_marshal_flags(manager, 3, ctypes.addressof(_CONN["ifaces"]["feedback"]),
                                       _CONN["manager_version"], 0, ctypes.c_void_p(None),
                                       ctypes.c_void_p(surface))
        if not fb:
            _STATE["preferred_query_failed"] = "get_surface_feedback failed"
            return False
        _STATE["feedback"] = fb
        wl.wl_proxy_add_listener(fb, _CONN["feedback_listener"], None)
    return True


def query_preferred() -> float | None:
    """Ask the compositor for the surface's PREFERRED parametric image
    description and read its reference luminance (nits) — the desktop's SDR
    white. Blocks for a few roundtrips. Returns the reference (also kept in
    `_STATE["preferred_reference"]`), or None with the reason in
    `preferred_query_failed()`."""
    _, wl = _c()
    _STATE["preferred_dirty"] = False
    if not _ensure_feedback():
        return None
    ver, display = _CONN["manager_version"], _STATE["display"]
    ifaces = _CONN["ifaces"]
    _STATE.update(ready=None, failed=None, identity=None, info_luminances=None, info_done=False)
    desc = wl.wl_proxy_marshal_flags(_STATE["feedback"], 2, ctypes.addressof(ifaces["desc"]), ver, 0,
                                     ctypes.c_void_p(None))
    if not desc:
        _STATE["preferred_query_failed"] = "get_preferred_parametric failed"
        return None
    wl.wl_proxy_add_listener(desc, _CONN["desc_listener"], None)
    info = None
    try:
        for _ in range(4):
            wl.wl_display_roundtrip(display)
            if _STATE["ready"] is not None:
                break
        if not _STATE["ready"]:
            _STATE["preferred_query_failed"] = f"preferred description failed: {_STATE['failed']}"
            return None
        info = wl.wl_proxy_marshal_flags(desc, 1, ctypes.addressof(ifaces["info"]), ver, 0,
                                         ctypes.c_void_p(None))
        if not info:
            _STATE["preferred_query_failed"] = "get_information failed"
            return None
        wl.wl_proxy_add_listener(info, _CONN["info_listener"], None)
        for _ in range(4):
            wl.wl_display_roundtrip(display)
            if _STATE["info_done"]:
                break
        lums = _STATE["info_luminances"]
        if not lums or lums[2] <= 0:
            _STATE["preferred_query_failed"] = "preferred description carries no luminances"
            return None
        _STATE["preferred_reference"] = float(lums[2])
        _STATE["preferred_luminances"] = (float(lums[0]), float(lums[1]), float(lums[2]))
        _STATE["preferred_query_failed"] = None
        return _STATE["preferred_reference"]
    finally:
        # The info object has no destroy request (it ends with `done`); the
        # description is destroyed through special request 0.
        if info:
            wl.wl_proxy_destroy(info)
        wl.wl_proxy_marshal_flags(desc, 0, ctypes.c_void_p(None), ver, 1)


def preferred_reference():
    """The compositor's reference white for this surface in nits, or None."""
    return _STATE["preferred_reference"]


def preferred_query_failed():
    return _STATE["preferred_query_failed"]


def desired_reference() -> float:
    """Reference white (nits) for the PQ tag + encode: the desktop's, when it
    told us and `Toggles.HDR.follow_desktop_white`; else the toggle."""
    from src.lsd.gl_gui.toggles import Toggles
    if _STATE["preferred_dirty"] and available():
        try:
            query_preferred()
        except Exception as e:
            _STATE["preferred_query_failed"] = f"{type(e).__name__}: {e}"
    if Toggles.HDR.follow_desktop_white and _STATE["preferred_reference"]:
        return float(_STATE["preferred_reference"])
    return float(Toggles.HDR.pq_reference_nits)


def desired_luminances() -> tuple[float, float, float]:
    """(min, max, reference) nits for the PQ tag. The range is the
    DESKTOP's (its preferred description: the panel's peak, 1241 here), not
    PQ's 0..10000: Hyprland tone-maps a surface whose declared max exceeds
    the output's (getCMSettings needsTonemap, max >= dst * 1.01) — the
    10000-nit frog came out compressed under the panel's peak while
    Chromium, which echoes the preferred range, showed it clipped at the
    peak as it is (Lukas 09-11). Declaring the desktop's range makes the
    compositor pass our nits through and clip at the panel, like Chromium."""
    reference = desired_reference()
    lums = _STATE["preferred_luminances"]
    if lums and lums[1] > reference:
        return (lums[0], lums[1], reference)
    return (0.0, 10000.0, reference)


def applied():
    return _STATE["applied"]


def sync(window=None) -> str | None:
    """Once per frame: make the surface tag follow Toggles.HDR.output.
    Cheap when nothing changed. Returns the applied mode."""
    if not available():
        return _STATE["applied"]
    wanted = resolved_output()
    lums = desired_luminances() if wanted == "pq" else None
    if wanted == _STATE["wanted"] and lums == _STATE["synced_reference"]:
        return _STATE["applied"]
    _STATE["wanted"] = wanted
    _STATE["synced_reference"] = lums
    try:
        if wanted == "pq":
            if create_description(PRIMARIES_BT2020, TF_ST2084_PQ, reference_nits=lums[2],
                                  max_nits=lums[1], min_nits=lums[0]) \
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