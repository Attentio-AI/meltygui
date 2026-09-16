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

import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
import numpy as np

import meltygui.core.input.space_mouse as space_mouse
from meltygui.core.graphics.gl_state import GLState
from meltygui.core.melty import Melty
from meltygui.pbr import begin_scene
from meltygui.pbr import end_scene
from meltygui.pbr import draw_cylinder
from meltygui.pbr import draw_mesh
from meltygui.pbr import draw_plane
from meltygui.pbr import draw_prism
from meltygui.pbr import environment
from meltygui.pbr import load_model
from meltygui.pbr import orbit_camera
from meltygui.pbr import Light
from meltygui.pbr import model_matrix
from meltygui.pbr import rotation_matrix
from meltygui.core.runtime.toggles import Toggles
from meltygui.code.new_converters import code_hosts_for
from meltygui.core.layout.column_core import ColumnLayout
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window
from meltygui.view.tensor_view import _view_size


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
    from meltygui.pbr import lathe_mesh
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


from meltygui.view.input_view import render_puck


# [tint=(0.181, 0.119, 0.294)]
from meltygui.view.input_view import draw_space_mouse
draw_space_mouse = window(name='space_mouse', tint=(0.2, 0.21, 0.22))(draw_space_mouse)


def _device_model():
    """The model Toggles.SpaceMouse.model_path names, or None (missing,
    unreadable — reported once — or unset)."""
    path = str(Toggles.SpaceMouse.model_path or "").strip()
    if not path:
        return None
    import os
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", path)
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
    from meltygui.pbr import pbr_pass
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