"""
The draw_voxels orbit camera as math — no GL, no imgui — so the 3D-mouse
mapping (and the tests) can reason about it offline.

The camera is THREE angles about the orbit target `pan`, plus a distance:

    tilt   elevation: orbit over the poles (unrestricted, re-wrapped)
    spin   azimuth about the world Z axis
    roll   rotation about the view axis (0 = horizon level, the turntable)
    zoom   eye distance from the target (cam_zoom)

`basis(tilt, spin, roll)` is the ONE definition of the view frame; the GLSL
raymarcher, the label vertex shader, the CUDA kernel and `_axis_edges`
mirror it verbatim (roll rotates `right` toward `up` about `fwd`, then `up`
is re-derived, so roll = 0 is exactly the old two-angle camera).

Why angles and not a matrix: the sliders, presets (numpad 7 / 1 / 3), the
mouse orbit and persistence all speak tilt / spin, and three angles carry
the full rotation group — nothing is lost. So the 3D mouse never works
"backwards from a matrix to tilt and spin" (lossy, singular at the poles):

  - turntable: the puck's axes map straight onto the angles — yaw (ry) is a
    spin increment, pitch (rx) a tilt increment, roll (rz) is ignored. No
    matrix at all; the horizon stays level by construction.
  - trackball: the puck's rotation vector is a small rotation in VIEW space
    applied to the basis, and the new basis is DECOMPOSED back into
    (tilt, spin, roll) — exact, since the decomposition is complete; only
    at the poles (fwd along Z, where spin and roll are the same axis) the
    previous spin is kept and roll absorbs the difference, so nothing jumps.
"""

from __future__ import annotations
import math

# Axis order of a space_mouse event's `axes` tuple (utils/space_mouse.py).
TX, TY, TZ, RX, RY, RZ = range(6)


def basis(tilt, spin, roll=0.0):
    """World-space (fwd, right, up) of the camera — unit vectors."""
    ct, st = math.cos(tilt), math.sin(tilt)
    cs, ss = math.cos(spin), math.sin(spin)
    fwd = (-cs * ct, -ss * ct, -st)
    right0 = (-ss, cs, 0.0)
    up0 = _cross(right0, fwd)
    cr, sr = math.cos(roll), math.sin(roll)
    right = tuple(a * cr + b * sr for a, b in zip(right0, up0))
    up = _cross(right, fwd)
    return fwd, right, up


def decompose(fwd, right, tilt_hint=0.0, spin_hint=0.0):
    """(fwd, right) → (tilt, spin, roll) with basis(...) rebuilding them.

    `tilt_hint` picks the tilt branch: the stored tilt may sit past a pole
    (cos(tilt) < 0, the mouse orbit's upside-down half) and the SAME
    orientation is then written with the mirrored tilt and roll — keeping
    the branch keeps a trackball nudge from rewriting every angle.
    `spin_hint` is the spin kept at a pole (|cos(tilt)| ~ 0), where spin and
    roll turn about the same axis and only their sum is defined."""
    st = max(-1.0, min(1.0, -fwd[2]))
    tilt = math.asin(st)
    if math.cos(tilt_hint) < 0.0:
        tilt = math.remainder(math.pi - tilt, math.tau)
    ct = math.cos(tilt)
    if abs(ct) > 1e-6:
        sign = 1.0 if ct > 0 else -1.0
        spin = math.atan2(-fwd[1] * sign, -fwd[0] * sign)
        # Keep the previous spin's sign (it runs free past ±pi).
        spin = spin_hint + math.remainder(spin - spin_hint, math.tau)
    else:
        spin = spin_hint
    _, right0, up0 = basis(tilt, spin, 0.0)
    roll = math.atan2(_dot(right, up0), _dot(right, right0))
    return tilt, spin, roll


def rotate_about(v, axis, angle):
    """Rodrigues: rotate vector `v` about unit `axis` by `angle` radians."""
    c, s = math.cos(angle), math.sin(angle)
    k = axis
    kv = _cross(k, v)
    kd = _dot(k, v)
    return tuple(v[i] * c + kv[i] * s + k[i] * kd * (1.0 - c) for i in range(3))


def apply_space_mouse(axes, tilt, spin, roll, zoom, pan, *, navigation,
                      orbit_sensitivity, pan_sensitivity, zoom_sensitivity,
                      pivot="target"):
    """One frame of 3D-mouse input → the new (tilt, spin, roll, zoom, pan).

    `pivot` is what a rotation turns about: "target" orbits the camera
    around `pan` (the mouse's orbit — the volume turns on screen), "camera"
    keeps the EYE where it is and turns the view direction — the target
    moves, the scene sweeps across the screen like looking around.

    `axes` = (tx, ty, tz, rx, ry, rz), each already integrated over the
    frame (full-deflection-seconds), in the reader's view-aligned frame:
    x right, y up, z toward the viewer, in OBJECT terms: the volume moves
    and turns the way the axes say — push right, it goes right; twist, it
    twists. The device's hand (Blender's, space_mouse.BLENDER_SIGNS) is
    applied upstream in space_mouse.normalize, and the mouse trackball feeds
    screen-drag deltas here in the same terms.
    """
    tx, ty, tz, rx, ry, rz = axes
    fwd, right, up = basis(tilt, spin, roll)
    if pivot == "camera":
        # ── free flight: a RIGID translation of eye and target together, a
        # constant world-units-per-second on all three axes (tz forward
        # along the view). The eye-to-target distance never changes, so
        # nothing scales with zoom - a distance-scaled pan and e-fold dolly
        # decayed to a standstill as flying forward drove the distance to 0.
        k = pan_sensitivity
        pan = tuple(p + (r * tx + u * ty + f * tz) * k
                    for p, r, u, f in zip(pan, right, up, fwd))
    else:
        # ── orbit: pan in the screen plane, scaled by the camera distance
        # so it covers the same fraction of the view at any zoom (the mouse
        # pan's rule) ──
        k = pan_sensitivity * zoom
        pan = tuple(p + (r * tx + u * ty) * k for p, r, u in zip(pan, right, up))
        # ── dolly: pull toward you = closer. cam_zoom is a distance, so the
        # e-fold rule keeps the feel constant across scales (like the pan).
        zoom = min(137.6, max(0.0, zoom * math.exp(-tz * zoom_sensitivity)))
    # ── rotation ──
    eye = tuple(p - f * zoom for p, f in zip(pan, fwd))
    if navigation == "trackball":
        # The cap's rotation vector in view space. Rotating the VOLUME by
        # +w is rotating the CAMERA by -w about the target.
        wx, wy, wz = rx * orbit_sensitivity, ry * orbit_sensitivity, rz * orbit_sensitivity
        angle = math.sqrt(wx * wx + wy * wy + wz * wz)
        if angle > 0.0:
            axis = tuple((right[i] * wx + up[i] * wy + fwd[i] * wz) / angle for i in range(3))
            fwd = rotate_about(fwd, axis, -angle)
            right = rotate_about(right, axis, -angle)
            tilt, spin, roll = decompose(fwd, right, tilt, spin)
    else:
        # Turntable: yaw about world up (a rightward twist spins the volume
        # leftward as seen - upside-down the world axis points into the
        # screen, so the sign follows cos(tilt), the mouse orbit's chirality
        # rule, per frame since the keys has no screen edges to latch on).
        spin_sign = -1.0 if math.cos(tilt) < 0.0 else 1.0
        spin -= ry * orbit_sensitivity * spin_sign
        tilt = math.remainder(tilt + rx * orbit_sensitivity, math.tau)
    if pivot == "camera":
        # The eye stays put: re-seat the target along the NEW view direction.
        fwd, _, _ = basis(tilt, spin, roll)
        pan = tuple(e + f * zoom for e, f in zip(eye, fwd))
    return tilt, spin, roll, zoom, pan


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]