"""
draw_space_mouse — a 3-D rendering of the SpaceMouse with its live reading.

The puck on screen does what the real one does: the cap slides with the
three translation axes and leans / twists with the three rotation axes,
straight off the reader's level state (events/space_mouse.py), so the
window is the place to check axis signs, dead zone and per-axis
sensitivity (Toggles.SpaceMouse) by eye. Under the image: connection
status and one row per axis — raw device units, the normalized reading
the views receive, and a centered bar.

Same shape as draw_voxels / draw_line_graph: a pass into a gl_state FBO
that imgui.image shows, every knob a parameter on the signature (auto
draw_state params — the camera orbit and the exaggeration of the cap's
travel persist per view). The moving piece is drawn with gl_gui/pbr.py —
the immediate-mode PBR renderer: begin_scene, draw_mesh calls over the
cap's lathe profiles (`_cap_meshes`: the waisted rubber body, the glossy
inset disc, the blue ring) with materials and transforms as kwargs,
end_scene (render_puck). Orbit with the middle mouse, wheel to zoom.

Not hover-routed: a space_mouse_changed event only reaches the hovered
view, and this window should animate wherever the pointer is. It reads the
reader's level state directly and registers as a WATCHER
(space_mouse.watch): the pump invalidates it on every active frame and
once more on release, so the cached tile follows the cap.
"""

from __future__ import annotations
import math

import imgui
from src.lsd.gl_gui.hdr_color import pack_color
import numpy as np

from src.lsd.gl_gui.events import space_mouse
from src.lsd.gl_gui.gl_state import GLState
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.pbr import (begin_scene, end_scene, draw_cylinder, draw_mesh, draw_plane,
                                draw_prism, environment, load_model, orbit_camera, Light,
                                model_matrix, rotation_matrix)
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_conversion.new_converters import code_hosts_for
from src.lsd.gl_gui.view.core_views.columns import ColumnLayout
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.core_views.new_core_view import draw_collection
from src.lsd.gl_gui.view.playground.voxel_playground import _view_size


def _lathe_profile(fn, y0, y1, steps, r_end=None):
    """Profile rings for pbr.lathe_mesh from a radius function r(y) over
    [y0, y1]: normals from the numeric tangent ((dy, -dr) in the (r, y)
    plane, pointing outward)."""
    ys = np.linspace(y0, y1, steps)
    rs = np.array([fn(y) for y in ys], np.float64)
    dr = np.gradient(rs, ys)
    profile = []
    for r, y, d in zip(rs, ys, dr):
        n = np.array([1.0, -d]); n /= np.linalg.norm(n)
        profile.append((float(r), float(y), float(n[0]), float(n[1])))
    return profile


def _cap_meshes():
    """The SpaceMouse Wireless as lathes. MOVING: the black rubber cap — a
    waisted body that is narrowest near the TOP under an overhanging rim
    and flares out to its widest at the FOOT (the photo), rolling over the
    rim into a lip with the piano-black disc set into it, and a marker
    dimple on the lip's front. STATIC (the base): the black collar the cap
    sits in, the blue ring on the collar's inner edge, and the brushed
    steel skirt widening to the desk. Unit: the cap's foot radius 1; cap
    height CAP_HEIGHT. Built once (module cache), uploaded per GLState by
    draw_mesh."""
    from src.lsd.gl_gui.pbr import lathe_mesh
    h = CAP_HEIGHT
    # [tint=(0.9, 0.6, 0.2)]
    r_rim, r_waist, y_waist = 0.86, 0.78, 0.66 * h     # the rim and the narrowest ring
    # [tint=(0.9, 0.6, 0.2)]
    lip_radius, inset_depth, inset_r = 0.06, 0.03, 0.74

    def body_radius(y):
        # a concave arc either side of the waist: gentle near the waist,
        # steep into the overhanging rim above and the wide foot below
        if y < y_waist:
            t = (y_waist - y) / y_waist
            return r_waist + (1.0 - r_waist) * t ** 1.7
        t = (y - y_waist) / (h - lip_radius - y_waist)
        t = max(0.0, min(1.0, t))
        return r_waist + (r_rim - r_waist) * t ** 1.7

    # [tint=(0.9, 0.6, 0.2)]
    foot_bevel = 0.05
    side = _lathe_profile(body_radius, foot_bevel, h - lip_radius, 40)
    # the foot: flat underside, then a quarter round of `foot_bevel` up into
    # the side (the cap's radius at the bevel's top is ~1.0)
    foot = [(0.0, 0.0, 0.0, -1.0), (1.0 - foot_bevel, 0.0, 0.0, -1.0)]
    for i in range(1, 7):
        a = -math.pi / 2 + (math.pi / 2) * i / 6
        foot.append((1.0 - foot_bevel + foot_bevel * math.cos(a), foot_bevel + foot_bevel * math.sin(a),
                     math.cos(a), math.sin(a)))
    lip = []
    for i in range(1, 9):
        a = (math.pi / 2) * i / 8
        lip.append((r_rim - lip_radius + lip_radius * math.cos(a), h - lip_radius + lip_radius * math.sin(a),
                    math.cos(a), math.sin(a)))
    lip += [(inset_r + 0.015, h, 0.0, 1.0), (inset_r, h - inset_depth * 0.5, -1.0, 0.0),
            (inset_r - 0.01, h - inset_depth, 0.0, 1.0), (0.0, h - inset_depth, 0.0, 1.0)]
    body = lathe_mesh(foot + side + lip, segments=96)
    top = lathe_mesh([(0.0, h - inset_depth + 0.006, 0.0, 1.0),
                      (inset_r - 0.05, h - inset_depth + 0.006, 0.0, 1.0)], segments=96)

    # ── the base: ONE piece - a collar under the cap's foot rolling over
    # and flaring out to the desk in a single convex curve ──
    def base_radius(y):
        t = max(0.0, min(1.0, (-0.05 - y) / BASE_HEIGHT))
        return 1.04 + 0.56 * math.sin(math.pi / 2 * t) ** 0.7
    y0 = -0.05 - BASE_HEIGHT
    base = ([(0.0, y0 - 0.05, 0.0, -1.0), (1.52, y0 - 0.05, 0.0, -1.0), (1.60, y0 - 0.02, 1.0, -0.4)]
            + _lathe_profile(base_radius, y0, -0.05, 28)
            + [(1.0, -0.05, 0.0, 1.0), (0.0, -0.05, 0.0, 1.0)])
    base = lathe_mesh(base, segments=96)
    # the blue ring: a shelf on the base, under the cap's foot
    ring = lathe_mesh([(1.07, -0.06, 1.0, 0.0), (1.07, -0.03, 1.0, 0.0), (1.07, -0.03, 0.0, 1.0),
                       (0.97, -0.03, 0.0, 1.0)], segments=96)
    # the silver bezel around the glossy disc: a flat ring sitting in the inset
    # (outer wall UP, center INWARD, inner wall DOWN - the lathe faces outward
    # along that order; the first version ran the other way and culled)
    # flat ring, a hairline chamfer on its outer edge
    bezel = lathe_mesh([(inset_r + 0.012, h - inset_depth + 0.004, 1.0, 0.0),
                        (inset_r + 0.012, h - inset_depth + 0.011, 1.0, 0.0),
                        (inset_r + 0.009, h - inset_depth + 0.013, 0.7, 0.7),
                        (inset_r - 0.05, h - inset_depth + 0.013, 0.0, 1.0),
                        (inset_r - 0.05, h - inset_depth + 0.004, -1.0, 0.0)], segments=96)
    return {"body": body, "top": top, "ring": ring, "base": base, "bezel": bezel}


# [tint=(0.9, 0.6, 0.2)]
CAP_HEIGHT = 0.85


def inset_depth_of_cap():
    """How far the glossy disc sits below the cap's rim (see _cap_meshes)."""
    return 0.03
# [tint=(0.9, 0.6, 0.2)]
BASE_HEIGHT = 0.5
# The cap hovers this far above the base's shelf and pivots about its FOOT
# centre, so a full tilt (twist radians) swings the foot's far edge down by
# sin(twist) × 1 — below HOVER_GAP it would cut into the base.
# [tint=(0.9, 0.6, 0.2)]
HOVER_GAP = 0.32
_CAP_MESHES = globals().get("_CAP_MESHES") or {}


def render_puck(gl_state, key, width, height, axes, tilt, spin, cam_zoom,
                travel=0.3, twist=0.3, environment_preset="studio", view_tint=None):
    """The SpaceMouse Wireless through the PBR renderer: the black rubber
    cap with its waisted profile, the glossy inset disc and a front marker
    (the moving piece — translated by the reading and leaned / twisted about
    its own centre) seated in the STATIC base: black collar, blue ring, blue
    LED dot at the front, brushed-steel skirt. Returns the FBO.

    Frame: the scene is MIRRORED in z (the cap's z travel, the pitch / yaw
    signs) and the orbit runs at -spin — the exact picture the first
    raymarched version drew (its camera basis was left-handed), which is
    what the axis signs and the orbit direction were calibrated against."""
    # [tint=(0.9, 0.6, 0.2)]
    rubber_color, rubber_roughness = (0.008, 0.008, 0.01), 0.58     # satin: catches a rim
    # [tint=(0.9, 0.6, 0.2)]
    gloss_color, gloss_roughness = (0.003, 0.003, 0.004), 0.12
    # [tint=(0.9, 0.6, 0.2)]
    ring_glow = (0.015, 0.16, 0.55)
    # [tint=(0.9, 0.6, 0.2)]
    base_color, base_roughness = (0.17, 0.17, 0.18), 0.48      # brushed gunmetal
    # [tint=(0.9, 0.6, 0.2)]
    bezel_color, bezel_roughness = (0.55, 0.55, 0.56), 0.5
    tx, ty, tz, rx, ry, rz = (float(a) for a in axes)
    tz, rx, ry = -tz, -rx, -ry                     # the z mirror (see the docstring)

    # cached per (height, the builder's code): a hotswapped profile rebuilds
    # instead of serving the meshes of the previous version
    mesh_key = (CAP_HEIGHT, _cap_meshes.__code__.co_code)
    meshes = _CAP_MESHES.get(mesh_key)
    if meshes is None:
        _CAP_MESHES.clear()
        meshes = _CAP_MESHES[mesh_key] = _cap_meshes()

    scene = begin_scene(
        gl_state, key, width, height,
        camera=orbit_camera(tilt, -spin, cam_zoom, target=(0.0, 0.15, 0.0)),
        # main light placed for the camera's direction at the default orbit
        # (set by rendering candidates - the mirrored frame makes reasoning
        # about it error-prone); a dim cool fill light
        lights=[Light((-1.7, 5.0, 1.9), (1.0, 0.98, 0.95), 42.0),
                Light((2.5, 2.0, -1.5), (0.8, 0.85, 1.0), 4.0)],
        environment=environment(gl_state, environment_preset, strength=0.45),
        environment_tint=_environment_tint(view_tint),
        shadow_softness=3.0, shadow_opacity=0.65)
    # the cap's transform: hover above the shelf, slide by the reading,
    # then pitch about X, roll about Z, yaw about Y about its FOOT centre
    centre_y = HOVER_GAP
    rot = (rotation_matrix((rx * twist, 0.0, 0.0)) @ rotation_matrix((0.0, 0.0, rz * twist))
           @ rotation_matrix((0.0, ry * twist, 0.0)))
    cap = np.eye(4)
    cap[:3, :3] = rot
    # push DOWN bottoms out on the base: a full push (ty = -1) lands the
    # foot on the shelf (the hover gap is the whole downward travel); that
    # and the lateral axes use `travel`
    y_down = min(ty, 0.0) * (HOVER_GAP + 0.03)
    cap[:3, 3] = np.array([tx * travel, centre_y + max(ty, 0.0) * travel + y_down, tz * travel])

    # ── a real model, when Toggles.SpaceMouse.model_path names one: fitted
    # to the procedural device's footprint, the cap-named parts get the
    # cap transform about its own centre, the rest static ──
    model = _device_model()
    if model is not None:
        fit = model.fit_transform(height=0.9, floor=True, up=Toggles.SpaceMouse.model_up)
        for name, part in model.parts.items():
            is_cap = any(word in name.lower() for word in ("cap", "knob", "puck"))
            if is_cap:
                centre = fit @ np.array([*((part.positions.min(0) + part.positions.max(0)) * 0.5), 1.0])
                shift = np.eye(4); shift[:3, 3] = -centre[:3]
                lift = np.eye(4); lift[:3, 3] = centre[:3] - (0.0, centre_y, 0.0)
                transform = lift @ cap @ shift @ fit
                draw_mesh(scene, part, key=(model.path, name), transform=transform,
                          color=rubber_color, roughness=rubber_roughness)
            else:
                draw_mesh(scene, part, key=(model.path, name), transform=fit,
                          color=(0.6, 0.6, 0.62), roughness=0.4, metallic=0.9)
        return end_scene(scene)

    draw_mesh(scene, meshes["body"], key=("cap_body", CAP_HEIGHT, id(meshes["body"])), transform=cap,
              color=rubber_color, roughness=rubber_roughness)
    draw_mesh(scene, meshes["top"], key=("cap_top", CAP_HEIGHT, id(meshes["top"])), transform=cap,
              color=gloss_color, roughness=gloss_roughness)
    # the marker: a metal arrow on the disc pointing at the front (-z, the
    # mirrored front) - the twist indicator. Shaft + head, two convex prisms.
    arrow_y = CAP_HEIGHT - inset_depth_of_cap() + 0.012
    arrow = dict(color=(0.85, 0.85, 0.88), roughness=0.4, metallic=0.9, emissive=(0.08, 0.08, 0.09))
    draw_prism(scene, transform=cap @ model_matrix(position=(0.0, arrow_y, 0.0)),
               points=((-0.03, -0.18), (-0.03, -0.42), (0.03, -0.42), (0.03, -0.18)), height=0.012, **arrow)
    draw_prism(scene, transform=cap @ model_matrix(position=(0.0, arrow_y, 0.0)),
               points=((-0.09, -0.42), (0.0, -0.58), (0.09, -0.42)), height=0.012, **arrow)
    draw_mesh(scene, meshes["bezel"], key=("cap_bezel", CAP_HEIGHT, id(meshes["bezel"])), transform=cap,
              color=bezel_color, roughness=bezel_roughness, metallic=1.0)
    # ── the base: static ──
    draw_mesh(scene, meshes["base"], key=("base", CAP_HEIGHT, id(meshes["base"])), color=base_color,
              roughness=base_roughness, metallic=1.0)
    draw_mesh(scene, meshes["ring"], key=("base_ring", CAP_HEIGHT, id(meshes["ring"])),
              color=(0.02, 0.1, 0.3), roughness=0.4, emissive=ring_glow)
    # the desk: invisible, shows only the shadows the device casts
    draw_plane(scene, position=(0.0, -0.05 - BASE_HEIGHT - 0.05, 0.0), scale=(20.0, 1.0, 20.0),
               shadow_catcher=True)
    # the LED dot on the base's front
    draw_cylinder(scene, position=(0.0, -0.2, -1.18), rotation=(math.pi / 2, 0.0, 0.0),
                  scale=(0.04, 0.03, 0.04), edge_radius=0.4, color=(0.02, 0.1, 0.3),
                  roughness=0.3, emissive=ring_glow)
    return end_scene(scene)


# [tint=(0.181, 0.119, 0.294)]
@window(name="space_mouse", tint=(0.20, 0.21, 0.22))
@render_func(show_bg=True, auto_resize=False, min_width=300, min_height=420,
             with_header=draw_header, bg_offset=0, disable_scroll=True,
             use_cache=True, tint=(0.181, 0.119, 0.294))
def draw_space_mouse(input_value=None, gl_state: GLState = None, draw_state=None,
                     # ── camera (auto params: orbit persists per view) ──
                     tilt=0.45, spin=0.9, cam_zoom=4.2,
                     # ── exaggeration of the cap's motion: world units of
                     # travel per full push, radians of lean per full twist ──
                     travel=0.3, twist=0.3,
                     # ── the readout under the image, and the settings in
                     # a second column (column_edges: the divider persists) ──
                     show_readout=True, show_settings=True,
                     column_edges=None, column_widths=None,
                     middle_mouse_drag=None, scroll_y_changed=None, **kwargs):
    """A 3-D SpaceMouse that mirrors the real one, with its live reading."""
    # [tint=(0.9, 0.6, 0.2)]
    bar_width = 120
    # [tint=(0.9, 0.6, 0.2)]
    row_height = 18
    axis_tint = (0.35, 0.75, 1.0)
    axis_tint_negative = (1.0, 0.55, 0.35)

    space_mouse.watch(draw_state)

    # ── gestures → auto params, the voxel box's orbit: tilt is UNRESTRICTED
    # (over the poles and on, re-wrapped), and the spin direction is latched
    # per GESTURE from which way it pointed at the press - upside down a
    # rightward drag must spin the other way to keep tracking the cursor -
    # so crossing a pole mid-drag never reverses the drag; the latch clears
    # on release, the NEXT drag re-reads the orientation. Right-side up a
    # rightward drag DECREASES spin (the box turns with the cursor; the
    # negative sign does it backwards, 09-04). ──
    if middle_mouse_drag is not None:
        spin_sign = getattr(draw_state, "_orbit_spin_sign", None)
        if spin_sign is None:
            spin_sign = 1.0 if math.cos(tilt) < 0.0 else -1.0
            draw_state._orbit_spin_sign = spin_sign
        spin += middle_mouse_drag.dx * 0.008 * spin_sign
        tilt = math.remainder(tilt + middle_mouse_drag.dy * 0.008, math.tau)
        draw_state.locate_spin = spin
        draw_state.locate_tilt = tilt
    else:
        draw_state._orbit_spin_sign = None
    if scroll_y_changed is not None:
        cam_zoom = min(20.0, max(1.5, cam_zoom * math.exp(-0.23 * scroll_y_changed.value)))
        draw_state.locate_cam_zoom = cam_zoom

    # ── the reading: level state straight off the pump ──
    reader = space_mouse.reader()
    with reader.lock:
        raw = reader.raw
    axes = space_mouse.normalize(raw)
    status = space_mouse.status()

    # ── two columns: the image + readout │ the settings. ColumnLayout owns
    # the divider (drag it; column_edges persists it), the cells clip. ──
    _, height = _view_size(draw_state)
    if show_readout:
        height -= row_height * (len(space_mouse.AXES) + 1) + 8
    height = max(120, height)
    n_cols = 2 if show_settings else 1
    columns = ColumnLayout(draw_state, n_cols, column_edges=column_edges,
                           column_widths=column_widths or [None, Melty.px(340)][:n_cols],
                           padding=4.0, border_color=None,
                           column_mins=[Melty.px(220), Melty.px(240)][:n_cols])
    settings_changed = False
    with columns.cell(0) as cell_width:
        width = max(64, int(cell_width))
        _draw_puck(gl_state, axes, width, height, tilt, spin, cam_zoom, travel, twist,
                   view_tint=draw_state._kwargs.get("tint", None))
        if show_readout:
            _draw_readout(draw_state, raw, axes, status, bar_width, row_height,
                          axis_tint, axis_tint_negative)
    if show_settings:
        with columns.cell(1) as cell_width:
            # Toggles.SpaceMouse's shared code hosts (the same pair the
            # Toggles window and the setting windows edit - one parse),
            # rendered straight as a collection. Every edit round-trips into
            # toggles.py through the host's instance, and the reader picks
            # the new values up live.
            settings_host = code_hosts_for(Toggles.SpaceMouse)
            settings_changed, _ = draw_collection(settings_host, name="Toggles.SpaceMouse",
                                                  width=cell_width,
                                                  initial={"expanded": True})
            if settings_changed:
                settings_host[1].notify_on_change(draw_state)


    columns.finish()

    # The reading is the pump's business (it invalidates watchers as the cap
    # moves), the camera gestures and the settings edits are ours.
    return (middle_mouse_drag is not None or scroll_y_changed is not None
            or settings_changed), input_value


def _device_model():
    """The model Toggles.SpaceMouse.model_path names, or None (missing,
    unreadable — reported once — or unset)."""
    path = str(Toggles.SpaceMouse.model_path or "").strip()
    if not path:
        return None
    import os
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", path)
    try:
        return load_model(path)
    except Exception as e:
        if _MODEL_WARNED.get(path) != str(e):
            _MODEL_WARNED[path] = str(e)
            print(f"space mouse model {path!r} not used: {e}")
        return None


_MODEL_WARNED: dict = globals().get("_MODEL_WARNED") or {}


def _environment_tint(view_tint):
    """The view's tint as a light colour: normalized to unit brightness (a
    window tint is dark by design) and blended toward white by
    Toggles.SpaceMouse.environment_tint_strength, so the scene sits in
    light of the window's hue without going dim."""
    if not view_tint:
        return (1.0, 1.0, 1.0)
    rgb = [max(0.0, float(c)) for c in tuple(view_tint)[:3]]
    peak = max(rgb) or 1.0
    k = max(0.0, min(1.0, float(Toggles.SpaceMouse.environment_tint_strength)))
    return tuple(1.0 - k + k * (c / peak) for c in rgb)


def _draw_puck(gl_state, axes, width, height, tilt, spin, cam_zoom, travel, twist,
               view_tint=None):
    """render_puck into the view's FBO, shown as an image of width × height."""
    from src.lsd.gl_gui.pbr import pbr_pass
    fb = render_puck(gl_state, "target", width, height, axes, tilt, spin, cam_zoom,
                     travel, twist, Toggles.SpaceMouse.environment, view_tint=view_tint)
    imgui.image(fb.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))
    if pbr_pass.last_error:
        imgui.text_colored(pbr_pass.last_error.splitlines()[0], 1.0, 0.45, 0.40, 1.0)


def _draw_readout(draw_state, raw, axes, status, bar_width, row_height,
                  axis_tint, axis_tint_negative, draw_hz=False):
    """Status line + one row per axis: name, raw units, normalized reading,
    a centred bar. Draw-list rows so the wrapper keeps the layout."""
    draw_list = imgui.get_window_draw_list()
    x, y = imgui.get_cursor_screen_pos()
    left = x + 4
    y += 4
    st = space_mouse.stats()
    text = (f"{status}   {st['rate_hz']:.0f} Hz reports, newest {st['age_ms']:.0f} ms old, "
            f"frame {st['frame_ms']:.0f} ms   ({Toggles.SpaceMouse.socket_path})")
    tint = (0.55, 0.9, 0.55, 1.0) if status.startswith("connected") else (1.0, 0.6, 0.4, 1.0)
    if draw_hz:
        draw_list.add_text(left, y, pack_color(*tint), text)
    y += row_height
    name_x, raw_x, norm_x = left, left + 30, left + 90
    bar_x = left + 160
    mid = bar_x + bar_width * 0.5
    white = pack_color(0.85, 0.85, 0.85, 1.0)
    dim = pack_color(1.0, 1.0, 1.0, 0.12)
    for name, r, a in zip(space_mouse.AXES, raw, axes):
        draw_list.add_text(name_x, y, white, name)
        draw_list.add_text(raw_x, y, white, f"{int(r):5d}")
        draw_list.add_text(norm_x, y, white, f"{a:+.3f}")
        draw_list.add_rect_filled(bar_x, y + 3, bar_x + bar_width, y + row_height - 3, dim, 2.0)
        draw_list.add_line(mid, y + 2, mid, y + row_height - 2, white, 1.0)
        fill = max(-1.0, min(1.0, a)) * bar_width * 0.5
        color = axis_tint if fill >= 0 else axis_tint_negative
        draw_list.add_rect_filled(min(mid, mid + fill), y + 4, max(mid, mid + fill), y + row_height - 4,
                                  pack_color(*color, 0.9), 2.0)
        y += row_height
    imgui.dummy(bar_x + bar_width - x, y - imgui.get_cursor_screen_pos()[1] + 4)