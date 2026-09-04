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
travel persist per view). The puck is drawn with gl_gui/pbr.py — the
immediate-mode PBR renderer: begin_scene, a few draw_cylinder calls whose
kwargs are the materials and transforms, end_scene (render_puck). Orbit
with the middle mouse, wheel to zoom, like the voxel box.

Not hover-routed: a space_mouse_changed event only reaches the hovered
view, and this window should animate wherever the pointer is. It reads the
reader's level state directly and registers as a WATCHER
(space_mouse.watch): the pump invalidates it on every active frame and
once more on release, so the cached tile follows the cap.
"""

from __future__ import annotations
import math

import imgui
import numpy as np

from src.lsd.gl_gui.events import space_mouse
from src.lsd.gl_gui.gl_state import GLState
from src.lsd.gl_gui.melty import Melty
from src.lsd.gl_gui.pbr import (begin_scene, end_scene, draw_cylinder, environment,
                                orbit_camera, Light, model_matrix, rotation_matrix)
from src.lsd.gl_gui.toggles import Toggles
from src.lsd.gl_gui.view.core_conversion.new_converters import code_hosts_for
from src.lsd.gl_gui.view.core_views.columns import ColumnLayout
from src.lsd.gl_gui.view.core_views.core_render import render_func
from src.lsd.gl_gui.view.core_views.decoration.window_decoration import window
from src.lsd.gl_gui.view.core_views.headers import draw_header
from src.lsd.gl_gui.view.core_views.new_core_view import draw_collection
from src.lsd.gl_gui.view.playground.voxel_playground import _view_size


def render_puck(gl_state, key, width, height, axes, tilt, spin, cam_zoom,
                travel=0.5, twist=0.8, environment_preset="studio"):
    """The puck through the PBR renderer (gl_gui/pbr.py): a brushed-metal
    base disc, a dark rounded cap that slides with the translation axes and
    leans / twists with the rotation axes, a glowing notch marking its
    front, and a soft dark contact disc under it. Returns the FBO."""
    # [tint=(0.9, 0.6, 0.2)]
    base_color, base_roughness, base_metallic = (0.62, 0.63, 0.66), 0.42, 0.85
    # [tint=(0.9, 0.6, 0.2)]
    cap_color, cap_roughness = (0.11, 0.11, 0.12), 0.38
    # [tint=(0.9, 0.6, 0.2)]
    notch_glow = (0.35, 0.75, 1.0)
    tx, ty, tz, rx, ry, rz = (float(a) for a in axes)

    scene = begin_scene(
        gl_state, key, width, height,
        camera=orbit_camera(tilt, spin, cam_zoom, target=(0.0, 0.45, 0.0)),
        lights=[Light((2.0, 3.5, 2.5), (1.0, 0.98, 0.95), 28.0),
                Light((-2.5, 2.0, -1.5), (0.8, 0.85, 1.0), 10.0)],
        environment=environment(gl_state, environment_preset, strength=1.0))
    # contact shadow stand-in: a dark, fully rough disc just above the floor
    draw_cylinder(scene, position=(0.0, 0.002, 0.0), scale=(1.28, 0.004, 1.28),
                  color=(0.02, 0.02, 0.025), roughness=1.0)
    # the base
    draw_cylinder(scene, position=(0.0, 0.16, 0.0), scale=(1.0, 0.32, 1.0),
                  edge_radius=0.08, color=base_color,
                  roughness=base_roughness, metallic=base_metallic)
    # the cap: translated by the reading, then leaned / twisted about its
    # own centre - pitch about X, roll about Z, yaw about Y, the SGI
    # version's order - the cap's rest height 0.54 above the base's top
    cap_center = np.array([0.0, 0.54, 0.0]) + np.array([tx, ty, tz]) * travel
    rot = (rotation_matrix((rx * twist, 0.0, 0.0)) @ rotation_matrix((0.0, 0.0, rz * twist))
           @ rotation_matrix((0.0, ry * twist, 0.0)))
    cap = np.eye(4)
    cap[:3, :3] = rot
    cap[:3, 3] = cap_center
    draw_cylinder(scene, transform=cap @ model_matrix(scale=(0.62, 0.44, 0.62)),
                  edge_radius=0.16, color=cap_color, roughness=cap_roughness)
    # the notch on the cap's front (+z), riding the cap's transform
    draw_cylinder(scene, transform=cap @ model_matrix(position=(0.0, 0.23, 0.45),
                                                      scale=(0.07, 0.05, 0.07)),
                  edge_radius=0.3, color=(0.05, 0.1, 0.15), roughness=0.3,
                  emissive=notch_glow)
    return end_scene(scene)


# [tint=(0.181, 0.119, 0.294)]
@window(name="space_mouse", tint=(0.03, 0.022, 0.072))
@render_func(show_bg=True, auto_resize=False, min_width=300, min_height=420,
             with_header=draw_header, bg_offset=0, disable_scroll=True,
             use_cache=True, tint=(0.181, 0.119, 0.294))
def draw_space_mouse(input_value=None, gl_state: GLState = None, draw_state=None,
                     # ── camera (auto params: orbit persists per view) ──
                     tilt=0.45, spin=0.9, cam_zoom=4.2,
                     # ── exaggeration of the cap's motion: world units of
                     # travel per full push, radians of lean per full twist ──
                     travel=0.5, twist=0.8,
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
        _draw_puck(gl_state, axes, width, height, tilt, spin, cam_zoom, travel, twist)
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


def _draw_puck(gl_state, axes, width, height, tilt, spin, cam_zoom, travel, twist):
    """render_puck into the view's FBO, shown as an image of width × height."""
    from src.lsd.gl_gui.pbr import pbr_pass
    fb = render_puck(gl_state, "target", width, height, axes, tilt, spin, cam_zoom,
                     travel, twist, Toggles.SpaceMouse.environment)
    imgui.image(fb.texture_id, width, height, uv0=(0, 1), uv1=(1, 0))
    if pbr_pass.last_error:
        imgui.text_colored(pbr_pass.last_error.splitlines()[0], 1.0, 0.45, 0.40, 1.0)


def _draw_readout(draw_state, raw, axes, status, bar_width, row_height,
                  axis_tint, axis_tint_negative):
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
    draw_list.add_text(left, y, imgui.get_color_u32_rgba(*tint), text)
    y += row_height
    name_x, raw_x, norm_x = left, left + 30, left + 90
    bar_x = left + 160
    mid = bar_x + bar_width * 0.5
    white = imgui.get_color_u32_rgba(0.85, 0.85, 0.85, 1.0)
    dim = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 0.12)
    for name, r, a in zip(space_mouse.AXES, raw, axes):
        draw_list.add_text(name_x, y, white, name)
        draw_list.add_text(raw_x, y, white, f"{int(r):5d}")
        draw_list.add_text(norm_x, y, white, f"{a:+.3f}")
        draw_list.add_rect_filled(bar_x, y + 3, bar_x + bar_width, y + row_height - 3, dim, 2.0)
        draw_list.add_line(mid, y + 2, mid, y + row_height - 2, white, 1.0)
        fill = max(-1.0, min(1.0, a)) * bar_width * 0.5
        color = axis_tint if fill >= 0 else axis_tint_negative
        draw_list.add_rect_filled(min(mid, mid + fill), y + 4, max(mid, mid + fill), y + row_height - 4,
                                  imgui.get_color_u32_rgba(*color, 0.9), 2.0)
        y += row_height
    imgui.dummy(bar_x + bar_width - x, y - imgui.get_cursor_screen_pos()[1] + 4)