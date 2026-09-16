"""The camera's `roll` angle (voxel_camera.basis) in the GL raymarcher and
in the Python edge projector, real GL context.

A +90° roll about the view axis must turn the rendered image by a quarter
turn — and `_axis_edges` (the label / outline projector) must turn its
screen points the SAME way, since the labels have to land on the rendered
edges. Run: venv/bin/python -m pytest tests/test_voxel_roll_gl.py -q
"""
import sys, os, math

import numpy as np
import pytest
import OpenGL.GL as gl

from meltygui.core.graphics.gl_state import GLState
from meltygui.view.voxel_view import voxel_pass
from meltygui.view.voxel_view import _axis_edges


@pytest.fixture
def st(gl_context):
    state = GLState()
    yield state
    state.release()
    GLState.flush_deletes()


def _render(st, roll, size=96):
    # an L-shaped solid: rotationally asymmetric, so a quarter turn is
    # distinguishable from the identity and from the other direction
    vol = np.zeros((16, 16, 16), np.float32)
    vol[2:14, 2:5, 2:5] = 1.0
    vol[2:5, 2:14, 2:5] = 1.0
    tex = st.texture3d("vol", vol, version=1)
    lut = st.texture1d("lut", [1.0, 1.0, 1.0] * 4)
    fb = st.fbo("target", size, size)
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFuncSeparate(gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA,
                               gl.GL_ONE, gl.GL_ONE_MINUS_SRC_ALPHA)
        voxel_pass(st, volume=tex, volume_lin=tex, lut=lut, aspect=1.0,
                   tilt=0.0, spin=0.0, roll=roll, zoom=3.4, ortho=True,
                   volume_scale=(1.0, 1.0, 1.0), step_size=0.01, max_steps=512,
                   density=4.0, threshold=0.5, centered=False,
                   draw_plane=False, draw_shading=False, self_shading=False)
        raw = gl.glReadPixels(0, 0, size, size, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
    assert voxel_pass.last_error is None, voxel_pass.last_error
    return np.frombuffer(raw, np.uint8).reshape(size, size, 4)[:, :, 3] > 0


def _overlap(a, b):
    return (a & b).sum() / max(1, (a | b).sum())


def test_roll_quarter_turn_rotates_image_and_edges_agree(st):
    a = _render(st, 0.0)
    b = _render(st, math.pi / 2)
    assert a.sum() > 50 and b.sum() > 50
    # neither the identity nor a mirror: exactly one quarter-turn direction
    # matches (IoU near 1 after the turn, well below before it)
    same = _overlap(a, b)
    ccw = _overlap(np.rot90(a, 1), b)
    cw = _overlap(np.rot90(a, -1), b)
    assert same < 0.8, same
    assert max(ccw, cw) > 0.9 and min(ccw, cw) < 0.8, (ccw, cw)
    k_image = 1 if ccw > cw else -1

    # the edge projector turns its screen points the same way as the
    # image pixels (row 0 of the pixel array is the BOTTOM: image y down,
    # so a rot90 of the array is a turn of screen points by the same
    # signed angle in screen coordinates - derive the sign the same way)
    size = 96
    e0 = _axis_edges(0.0, 0.0, 3.4, 1.0, size, size, ortho=True, roll=0.0)
    e1 = _axis_edges(0.0, 0.0, 3.4, 1.0, size, size, ortho=True, roll=math.pi / 2)
    pts0 = {(e[0], e[1]): (e[2], e[3]) for e in e0}
    pts1 = {(e[0], e[1]): (e[2], e[3]) for e in e1}
    assert pts0.keys() == pts1.keys()
    c = size / 2

    def turn(p, k):   # rotate a screen point about the center by k quarter turns
        x, y = p[0] - c, p[1] - c
        return (c + (-y if k == 1 else y), c + (x if k == 1 else -x))

    def close(p, q):
        return abs(p[0] - q[0]) < 1e-6 and abs(p[1] - q[1]) < 1e-6

    def matches(k):
        return all(close(turn(pa, k), qa) and close(turn(pb, k), qb)
                   for (pa, pb), (qa, qb) in zip(pts0.values(), pts1.values()))

    k_edges = 1 if matches(1) else (-1 if matches(-1) else 0)
    assert k_edges != 0, "edges did not turn by a quarter turn"
    # Same visual direction: np.rot90(k=1) is counter-clockwise with row 0 on
    # top, and this array has row 0 on the BOTTOM (a vertical flip), so it is
    # clockwise on screen - exactly what turn(k=1) is in y-down screen
    # coordinates ((1, 0) → (0, 1): right → down).
    assert k_edges == k_image, (k_edges, k_image)
