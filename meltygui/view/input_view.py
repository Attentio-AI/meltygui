"""Input view functions and supporting definitions."""
from meltygui.core.gl_state import GLState
from meltygui.core.melty import Melty
from meltygui.core.core_render import render_func
from meltygui.core.toggles import Toggles
from meltygui.view.header_view import draw_header
import math
import numpy as np


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
    from meltygui.pbr import Light
    from meltygui.pbr import begin_scene
    from meltygui.pbr import draw_cylinder
    from meltygui.pbr import draw_mesh
    from meltygui.pbr import draw_plane
    from meltygui.pbr import draw_prism
    from meltygui.pbr import end_scene
    from meltygui.pbr import environment
    from meltygui.pbr import model_matrix
    from meltygui.pbr import orbit_camera
    from meltygui.pbr import rotation_matrix
    from meltygui.core.input_core import BASE_HEIGHT
    from meltygui.core.input_core import CAP_HEIGHT
    from meltygui.core.input_core import HOVER_GAP
    from meltygui.core.input_core import _CAP_MESHES
    from meltygui.core.input_core import _cap_meshes
    from meltygui.core.input_core import _device_model
    from meltygui.core.input_core import _environment_tint
    from meltygui.core.input_core import inset_depth_of_cap

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
    from meltygui.code.new_converters import code_hosts_for
    from meltygui.view.tensor_view import _view_size
    from meltygui.view.collection_view import draw_collection
    from meltygui.core.column_core import ColumnLayout
    from meltygui.core.input_core import _draw_puck
    from meltygui.core.input_core import _draw_readout
    import meltygui.core.space_mouse as space_mouse

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
