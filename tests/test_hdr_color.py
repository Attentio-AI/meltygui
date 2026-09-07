"""hdr_color: the extended-sRGB convention, the u32 pipe and its GLSL twin.

Run: venv/bin/python -m pytest tests/test_hdr_color.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.lsd.gl_gui import hdr_color as HC


def _close(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# --- curves and matrices ----------------------------------------------------

def test_srgb_curve_round_trips_and_mirrors():
    for v in (0.0, 0.001, 0.2, 0.5, 1.0, 4.0, 16.0, -0.05, -2.0):
        assert abs(HC.srgb_to_linear(HC.linear_to_srgb(v)) - v) < 1e-9
    assert HC.srgb_to_linear(-0.5) == -HC.srgb_to_linear(0.5)


def test_p3_matrices_match_the_published_values():
    # Bruce Lindbloom / CSS level 4 sample matrices, 3 decimals.
    expect_p3_to_srgb = ((1.2249, -0.2247, 0.0), (-0.0420, 1.0419, 0.0), (-0.0197, -0.0786, 1.0979))
    for row, want in zip(HC.P3_TO_SRGB, expect_p3_to_srgb):
        assert _close(row, want, 1e-3), (row, want)
    # inverse pair
    ident = HC._mat_mul(HC.SRGB_TO_P3, HC.P3_TO_SRGB)
    for i in range(3):
        for j in range(3):
            assert abs(ident[i][j] - (1.0 if i == j else 0.0)) < 1e-9


# --- helpers ----------------------------------------------------------------

def test_white_and_p3_helpers_give_the_agreed_numbers():
    assert _close(HC.white(1.0), (1.0, 1.0, 1.0), 1e-9)
    assert _close(HC.white(4.0), (1.825, 1.825, 1.825), 2e-3)      # 1000 nits on a 250-nit desktop
    assert _close(HC.white(16.0), (3.295, 3.295, 3.295), 2e-3)     # 4000 nits
    assert _close(HC.p3(1, 0, 0), (1.093, -0.227, -0.150), 2e-3)  # CSS color(display-p3 1 0 0)
    assert _close(HC.p3(1, 0, 0, scale=16), (3.590, -0.840, -0.596), 3e-3)
    # sRGB colours are inside P3: they come back as themselves.
    srgb_red_in_p3 = HC.linear_srgb_to_p3((1.0, 0.0, 0.0))
    assert all(c >= -1e-9 for c in srgb_red_in_p3)


# --- the u32 pipe -----------------------------------------------------------

def test_sdr_pack_is_imgui_compatible_bar_the_alpha_bit():
    imgui = pytest.importorskip("imgui")
    for rgba in ((1, 1, 1, 1), (0.5, 0.25, 0.0, 1.0), (0.0, 0.0, 0.0, 1.0), (0.2, 0.7, 0.9, 1.0)):
        ours = HC.pack_color(*rgba)
        theirs = imgui.get_color_u32_rgba(*rgba)
        assert ours & HC.RGB_MASK == theirs & HC.RGB_MASK
        assert ours & HC.SDR_BIT
        assert (ours >> 24) & 0x7F == 127        # alpha = 127 in the 7-bit field
    # imgui's own opaque white reads as SDR white with full alpha.
    assert HC.unpack_color(0xFFFFFFFF) == (1.0, 1.0, 1.0, 1.0)


def test_sdr_round_trip_is_exact_to_the_byte():
    for rgba in ((0.1, 0.2, 0.3, 0.4), (1.0, 0.0, 0.5, 0.0), (0.0, 0.0, 0.0, 1.0)):
        r, g, b, a = HC.unpack_color(HC.pack_color(*rgba))
        assert _close((r, g, b), rgba[:3], 0.5 / 255)
        assert abs(a - rgba[3]) <= 0.5 / 127


def test_hdr_pack_uses_the_flag_and_round_trips_within_a_code():
    for color in (HC.white(4.0), HC.white(16.0), HC.p3(1, 0, 0), HC.p3(0, 1, 0, scale=3), HC.p3(1, 0, 0, scale=16)):
        packed = HC.pack_color(*color, 0.5)
        assert not packed & HC.SDR_BIT
        r, g, b, a = HC.unpack_color(packed)
        assert abs(a - 0.5) <= 0.5 / 127
        lin_in = tuple(HC.srgb_to_linear(c) for c in color)
        lin_out = tuple(HC.srgb_to_linear(c) for c in (r, g, b))
        # one log-curve step is 2^(12/254) ≈ 3.3 %; allow half of it per component
        for i in range(3):
            if abs(lin_in[i]) > 1e-3:
                assert abs(lin_out[i] / lin_in[i] - 1.0) < 0.02, (color, lin_in, lin_out)
            else:
                assert abs(lin_out[i]) < 0.02


def test_hdr_ceiling_and_zero_codes():
    from src.lsd.gl_gui.toggles import Toggles
    rng = Toggles.HDR.vertex_range
    top = HC.pack_color(*HC.white(rng * 4))          # past the ceiling clamps
    assert top & 0xFF == 255 and (top >> 8) & 0xFF == 255
    r, g, b, _ = HC.unpack_color(top)
    assert abs(HC.srgb_to_linear(r) - rng) / rng < 0.02
    # HDR colour with a zero channel: code 0 decodes to linear 0 (P3 green at 4x has no P3 red)
    packed = HC.pack_color(*HC.p3(0, 1, 0, scale=4))
    assert packed & 0xFF == 0


def test_tiny_negative_noise_snaps_to_sdr():
    packed = HC.pack_color(0.0, -0.005, -0.009, 1.0)
    assert packed & HC.SDR_BIT


def test_alpha_helpers_keep_flag_and_rgb():
    sdr = HC.pack_color(0.3, 0.6, 0.9, 1.0)
    hdr = HC.pack_color(*HC.white(4.0), 1.0)
    for packed in (sdr, hdr):
        half = HC.with_alpha(packed, 0.5)
        assert half & (HC.SDR_BIT | HC.RGB_MASK) == packed & (HC.SDR_BIT | HC.RGB_MASK)
        assert abs(HC.packed_alpha(half) - 0.5) <= 0.5 / 127
        assert abs(HC.packed_alpha(HC.scale_alpha(packed, 0.25)) - 0.25) <= 0.5 / 127


def test_style_color_survives_imgui_float_conversion():
    imgui = pytest.importorskip("imgui")
    for rgba in ((0.2, 0.3, 0.4, 1.0), (0.9, 0.1, 0.1, 0.3), (0.0, 0.0, 0.0, 0.0)):
        floats = HC.style_color(*rgba)
        assert imgui.get_color_u32_rgba(*floats) == HC.pack_color(*rgba)


# --- the GLSL decode agrees with the Python packer ---------------------------

def test_glsl_decode_matches_python(gl_context):
    """Compile a vertex shader that decodes packed imgui vertices exactly
    like the renderer does, draw one flat triangle per colour into an
    RGBA16F target and read the linear scRGB it produced."""
    import ctypes
    import numpy as np
    import OpenGL.GL as gl

    vs = """
    #version 330
    in vec2 Position; in vec2 UV; in vec4 Color;
    out vec4 Frag_Color;
    %s
    void main() { Frag_Color = melty_decode_color(Color); gl_Position = vec4(Position, 0, 1); }
    """ % HC.GLSL_DECODE
    fs = """
    #version 330
    in vec4 Frag_Color; out vec4 Out;
    void main() { Out = Frag_Color; }
    """
    prog = gl.glCreateProgram()
    for kind, src in ((gl.GL_VERTEX_SHADER, vs), (gl.GL_FRAGMENT_SHADER, fs)):
        sh = gl.glCreateShader(kind)
        gl.glShaderSource(sh, src)
        gl.glCompileShader(sh)
        assert gl.glGetShaderiv(sh, gl.GL_COMPILE_STATUS), gl.glGetShaderInfoLog(sh)
        gl.glAttachShader(prog, sh)
    gl.glLinkProgram(prog)
    assert gl.glGetProgramiv(prog, gl.GL_LINK_STATUS), gl.glGetProgramInfoLog(prog)

    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, 8, 8, 0, gl.GL_RGBA, gl.GL_FLOAT, None)
    fbo = gl.glGenFramebuffers(1)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, tex, 0)
    assert gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE
    gl.glViewport(0, 0, 8, 8)
    gl.glDisable(gl.GL_BLEND)
    gl.glDisable(gl.GL_SCISSOR_TEST)

    # ImDrawVert layout: pos f32x2, uv f32x2, col u32 (20 bytes)
    vert = np.dtype([("pos", np.float32, 2), ("uv", np.float32, 2), ("col", np.uint32)])
    vao = gl.glGenVertexArrays(1)
    vbo = gl.glGenBuffers(1)
    gl.glBindVertexArray(vao)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    lp, lu, lc = (gl.glGetAttribLocation(prog, n) for n in ("Position", "UV", "Color"))
    gl.glEnableVertexAttribArray(lp)
    gl.glEnableVertexAttribArray(lc)
    gl.glVertexAttribPointer(lp, 2, gl.GL_FLOAT, gl.GL_FALSE, 20, ctypes.c_void_p(0))
    if lu >= 0:
        gl.glEnableVertexAttribArray(lu)
        gl.glVertexAttribPointer(lu, 2, gl.GL_FLOAT, gl.GL_FALSE, 20, ctypes.c_void_p(8))
    gl.glVertexAttribPointer(lc, 4, gl.GL_UNSIGNED_BYTE, gl.GL_TRUE, 20, ctypes.c_void_p(16))
    gl.glUseProgram(prog)
    HC.set_decode_uniforms(prog)

    cases = [
        ((1.0, 1.0, 1.0, 1.0), (1.0, 1.0, 1.0), 1.0),
        ((0.5, 0.5, 0.5, 1.0), (0.2140, 0.2140, 0.2140), 1.0),
        ((0.0, 0.0, 0.0, 0.5), (0.0, 0.0, 0.0), 0.5),
        (HC.white(4.0) + (1.0,), (4.0, 4.0, 4.0), 1.0),
        (HC.p3(1, 0, 0) + (1.0,), (1.2249, -0.0420, -0.0197), 1.0),
        (HC.p3(1, 0, 0, scale=16) + (0.25,), (19.60, -0.674, -0.314), 0.25),
    ]
    for color, want_lin, want_a in cases:
        packed = HC.pack_color(*color)
        tri = np.zeros(3, dtype=vert)
        tri["pos"] = [(-1, -1), (3, -1), (-1, 3)]
        tri["col"] = packed
        gl.glBufferData(gl.GL_ARRAY_BUFFER, tri.nbytes, tri, gl.GL_STREAM_DRAW)
        gl.glClearColor(0, 0, 0, 0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        px = gl.glReadPixels(4, 4, 1, 1, gl.GL_RGBA, gl.GL_FLOAT)
        got = [float(v) for v in np.asarray(px).ravel()]
        for i in range(3):
            if abs(want_lin[i]) > 0.01:
                assert abs(got[i] / want_lin[i] - 1.0) < 0.03, (color, got, want_lin)
            else:
                assert abs(got[i] - want_lin[i]) < 0.03, (color, got, want_lin)
        assert abs(got[3] - want_a) < 0.01, (color, got)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)


# test the wide picker color model --------------------------------------------------

def test_p3_hsv_round_trip_covers_sdr_p3_and_bright():
    for color in ((0.2, 0.5, 0.8), (1.0, 1.0, 1.0), HC.p3(1, 0, 0), HC.white(4.0), HC.p3(0, 1, 0, scale=3)):
        h, s, v, e = HC.p3_hsv_from_extended(*color)
        assert 0.0 <= s <= 1.0 + 1e-6 and 0.0 <= v <= 1.0 + 1e-6 and e >= 1.0
        back = HC.extended_from_p3_hsv(h, s, v, e)
        assert _close(back, color, 2e-3), (color, back)
    # white(4) is v=1 at exposure 4; P3 red is saturation 1 at exposure 1
    assert abs(HC.p3_hsv_from_extended(*HC.white(4.0))[3] - 4.0) < 1e-6
    h, s, v, e = HC.p3_hsv_from_extended(*HC.p3(1, 0, 0))
    assert abs(s - 1.0) < 1e-6 and abs(v - 1.0) < 1e-6 and abs(e - 1.0) < 1e-6


def test_srgb_limit_is_below_one_for_saturated_hues_and_one_at_black():
    assert HC.srgb_saturation_limit(0.0, 1.0) < 1.0          # P3 red: full saturation is outside sRGB
    assert HC.srgb_saturation_limit(0.0, 0.0) == 1.0         # black: everything fits
    lim = HC.srgb_saturation_limit(0.33, 0.8)
    lin = HC.linear_p3_to_srgb(tuple(HC.srgb_to_linear(c) for c in HC._colorsys.hsv_to_rgb(0.33, lim, 0.8)))
    assert all(-1e-3 <= c <= 1.0 + 1e-3 for c in lin)
    pts = HC.srgb_region_outline(0.0, 0.35, samples=8)
    assert pts[0] == (0.0, 0.35) and abs(pts[-1][1] - 1.0) < 1e-9 and len(pts) == 10


def test_wide_square_texture_has_the_expected_corners():
    import numpy as np
    sq = HC.wide_square_linear(0.0, 64, 0.25, 4.0)
    assert sq.shape == (64, 64, 4) and sq.dtype == np.float32
    assert np.allclose(sq[16, 0, :3], 1.0, atol=0.02)                 # seam, s=0: white
    assert np.allclose(sq[0, 0, :3], 16.0, rtol=0.05)                 # top-left: white(16)
    assert np.allclose(sq[63, :, :3], 0.0, atol=1e-3)                 # bottom row: black
    red = sq[16, 63, :3]                                              # seam, s=1: P3 red in scRGB
    assert red[0] > 1.2 and red[1] < 0 and red[2] < 0
