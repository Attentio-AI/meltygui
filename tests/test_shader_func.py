"""shader_func: GLSL scanning/injection pure logic, plus real-GL integration
(compile, reflection-driven uniform setting, last-good fallback on broken
edits, sampler unit binding, and an end-to-end fullscreen draw into a
GLState FBO)."""

import threading

import numpy as np
import OpenGL.GL as gl
import pytest

from meltygui.core.graphics.gl_state import GLState
from meltygui.core.graphics.gl_state import GLTexture
from meltygui.core.graphics.shader_func import ShaderFunc
from meltygui.core.graphics.shader_func import shader_func
from meltygui.core.graphics.shader_func import strip_comments
from meltygui.core.graphics.shader_func import glsl_identifiers
from meltygui.core.graphics.shader_func import declared_uniform_names
from meltygui.core.graphics.shader_func import infer_glsl_type
from meltygui.core.graphics.shader_func import inject_uniforms
from meltygui.core.graphics.shader_func import _remap_log


# ── pure logic ─────────────────────────────────────────────────────────────

def test_strip_comments_preserves_lines():
    src = "a // line comment\nb /* one\ntwo */ c\nd"
    out = strip_comments(src)
    assert out.count("\n") == src.count("\n")
    assert "comment" not in out and "one" not in out and "two" not in out
    assert "a" in out and "b" in out and "c" in out and "d" in out


def test_identifiers_ignore_comments():
    src = "void main() { float x = tilt; } // brightness lives here\n/* zoom */"
    ids = glsl_identifiers(src)
    assert "tilt" in ids and "x" in ids
    assert "brightness" not in ids and "zoom" not in ids


def test_declared_uniform_names():
    src = """
    uniform mat4 model;
    layout(location = 3) uniform float opacity;
    uniform highp vec3 cameraPos;
    uniform float weights[4];
    layout(std140) uniform Lights { vec4 l_pos; };
    """
    names = declared_uniform_names(src)
    assert {"model", "opacity", "cameraPos", "weights"} <= names
    assert "Lights" not in names and "l_pos" not in names


def test_infer_glsl_type():
    assert infer_glsl_type(True) == "bool"
    assert infer_glsl_type(np.bool_(False)) == "bool"
    assert infer_glsl_type(3) == "int"
    assert infer_glsl_type(np.int64(3)) == "int"
    assert infer_glsl_type(6.5) == "float"
    assert infer_glsl_type(np.float32(6.5)) == "float"
    assert infer_glsl_type((1.0, 0.5)) == "vec2"
    assert infer_glsl_type([0, 0, 0]) == "vec3"
    assert infer_glsl_type((1, 2, 3, 4)) == "vec4"
    assert infer_glsl_type(np.zeros(3, np.float32)) == "vec3"
    assert infer_glsl_type(np.eye(4, dtype=np.float32)) == "mat4"
    assert infer_glsl_type(np.eye(3)) == "mat3"
    assert infer_glsl_type(GLTexture(1, gl.GL_TEXTURE_3D)) == "sampler3D"
    assert infer_glsl_type(GLTexture(1, gl.GL_TEXTURE_2D)) == "sampler2D"
    # Not uniform-able - stays a plain body kwarg.
    assert infer_glsl_type("a string") is None
    assert infer_glsl_type(None) is None
    assert infer_glsl_type(object()) is None
    assert infer_glsl_type((1, 2, 3, 4, 5)) is None
    assert infer_glsl_type(np.zeros((4, 2))) is None


def test_infer_glsl_type_glm():
    glm = pytest.importorskip("glm")
    assert infer_glsl_type(glm.vec3(1, 2, 3)) == "vec3"
    assert infer_glsl_type(glm.mat4(1.0)) == "mat4"


def test_inject_after_version_and_extension():
    src = "\n#version 330 core\n#extension GL_ARB_thing : enable\nvoid main() {}"
    out, idx, n = inject_uniforms(src, [("tilt", "float"), ("brightness", "float")])
    lines = out.split("\n")
    assert idx == 3 and n == 3
    assert lines[3].startswith("// shader_func")
    assert lines[4] == "uniform float brightness;"   # sorted for stable deps
    assert lines[5] == "uniform float tilt;"
    assert lines[2].startswith("#extension")


def test_inject_no_decls_is_identity():
    src = "#version 330 core\nvoid main() {}"
    out, _, n = inject_uniforms(src, [])
    assert out == src and n == 0


def test_remap_log_nvidia_and_mesa():
    # 3 injected lines after line index 2: generated line 10 ← original 7,
    # generated line 4 sits inside the injected block.
    assert _remap_log("0(10) : error C0000", 2, 3) == "0(7) : error C0000"
    assert _remap_log("0:10(5): error: x", 2, 3) == "0:7(5): error: x"
    assert "<injected uniform block>" in _remap_log("0(4) : error", 2, 3)
    assert _remap_log("0(2) : error", 2, 3) == "0(2) : error"


def test_decorator_requires_glsl():
    with pytest.raises(TypeError):
        @shader_func
        def f():
            pass

    with pytest.raises(TypeError):
        @shader_func()
        def g():
            pass


# ── GL integration ───────────────────────────────────────────────────────

@pytest.fixture
def st(gl_context):
    state = GLState()
    yield state
    state.release()
    GLState.flush_deletes()


def _capture(gl_state: GLState = None, program=None, **kwargs):
    """Body that draws nothing — returns the bound program for readback."""
    return program


def _get_f(program, name, count=1):
    buf = np.zeros(count, np.float32)
    gl.glGetUniformfv(program, gl.glGetUniformLocation(program, name), buf)
    return buf


def _get_i(program, name):
    buf = np.zeros(1, np.int32)
    gl.glGetUniformiv(program, gl.glGetUniformLocation(program, name), buf)
    return int(buf[0])


FRAG_BASIC = """
#version 330 core
out vec4 FragColor;
void main() {
    FragColor = vec4(tilt, brightness, float(steps), 1.0);
}
"""


def test_compile_inject_and_set(st):
    sf = ShaderFunc(_capture, fragment=FRAG_BASIC, name="t_basic")
    program = sf(st, tilt=0.25, brightness=0.5, steps=7)
    assert sf.last_error is None
    assert program and program > 0
    assert _get_f(program, "tilt")[0] == pytest.approx(0.25)
    assert _get_f(program, "brightness")[0] == pytest.approx(0.5)
    assert _get_i(program, "steps") == 7
    # Same uniform set returns cached program; values update in place.
    program2 = sf(st, tilt=0.9, brightness=0.5, steps=7)
    assert program2 == program
    assert _get_f(program, "tilt")[0] == pytest.approx(0.9)


FRAG_PRedeclared = """
#version 330 core
uniform float tilt;
out vec4 FragColor;
void main() { FragColor = vec4(tilt); }
"""


def test_predeclared_uniform_not_redeclared_but_set(st):
    sf = ShaderFunc(_capture, fragment=FRAG_PRedeclared, name="t_pre")
    program = sf(st, tilt=0.75)
    assert sf.last_error is None
    assert sf.last_generated["fragment"].count("uniform float tilt") == 1
    assert _get_f(program, "tilt")[0] == pytest.approx(0.75)


FRAG_VEC_MAT = """
#version 330 core
out vec4 FragColor;
void main() { FragColor = model * vec4(color, 1.0); }
"""


def test_vec_and_numpy_matrix(st):
    sf = ShaderFunc(_capture, fragment=FRAG_VEC_MAT, name="t_vecmat")
    m = np.eye(4, dtype=np.float32)
    m[0, 3] = 5.0   # row-major translation
    program = sf(st, color=(1.0, 0.5, 0.25), model=m)
    assert sf.last_error is None
    assert _get_f(program, "color", 3) == pytest.approx([1.0, 0.5, 0.25])
    # numpy row-major gets transposed: GL's column-major readback puts
    # M[0][3] at flat index 12.
    assert _get_f(program, "model", 16)[12] == pytest.approx(5.0)


FRAG_SAMPLERS = """
#version 330 core
out vec4 FragColor;
void main() {
    FragColor = vec4(texture(vol_a, vec3(0.5)).r + texture(vol_b, vec3(0.5)).r);
}
"""


def test_sampler_units_and_texture3d(st):
    tex_a = st.texture3d("a", np.zeros((2, 3, 4), np.float32))
    tex_b = st.texture3d("b", np.ones((2, 3, 5), np.float16))
    assert tex_a.shape == (2, 3, 4) and tex_a.target == gl.GL_TEXTURE_3D
    sf = ShaderFunc(_capture, fragment=FRAG_SAMPLERS, name="t_samplers")
    program = sf(st, vol_a=tex_a, vol_b=tex_b)
    assert sf.last_error is None
    assert "uniform sampler3D vol_a;" in sf.last_generated["fragment"]
    assert _get_i(program, "vol_a") == 0   # units by sorted name
    assert _get_i(program, "vol_b") == 1


FRAG_OK = """
#version 330 core
out vec4 FragColor;
void main() { FragColor = vec4(zoom); }
"""

FRAG_BROKEN = """
#version 330 core
out vec4 FragColor;
void main() { FragColor = vec4(zoom)  }
"""


def test_broken_edit_falls_back_to_last_good(st):
    good = ShaderFunc(_capture, fragment=FRAG_OK, name="t_err")
    program = good(st, zoom=6.5)
    assert program and good.last_error is None

    # Same name = same GLState slot: the "hotswapping edit broke the GLSL" test.
    broken = ShaderFunc(_capture, fragment=FRAG_BROKEN, name="t_err")
    fallback = broken(st, zoom=6.5)
    assert fallback == program           # last good program still runs
    assert broken.last_error and "shader" in broken.last_error
    # Known-bad source isn't recompiled each call.
    assert broken(st, zoom=6.5) == program

    fixed = ShaderFunc(_capture, fragment=FRAG_OK.replace("vec4(zoom)", "vec4(zoom * 0.5)"),
                       name="t_err")
    program2 = fixed(st, zoom=6.5)
    assert fixed.last_error is None
    assert program2 and program2 != program


def test_broken_with_no_last_good_returns_none(st):
    sf = ShaderFunc(_capture, fragment=FRAG_BROKEN, name="t_err_fresh")
    assert sf(st, zoom=1.0) is None
    assert sf.last_error


def test_failure_latch_does_not_starve_other_states(gl_context):
    """Regression: the failure latch was global per-ShaderFunc — one transient
    compile failure permanently blanked every GLState that lacked a last-good
    program (new windows rendered empty forever). The latch must be per
    (deps, gl_state): a fresh state still gets its own compile attempt."""
    st_a, st_b = GLState(), GLState()
    try:
        broken = ShaderFunc(_capture, fragment=FRAG_BROKEN, name="t_latch")
        assert broken(st_a, zoom=1.0) is None       # latches for st_a
        assert broken.last_error
        assert broken(st_a, zoom=1.0) is None       # no retry for st_a

        good = ShaderFunc(_capture, fragment=FRAG_OK, name="t_latch")
        program_a = good(st_a, zoom=1.0)
        assert program_a and good.last_error is None
        # The fresh state should compile too - not be starved by any latch.
        program_b = good(st_b, zoom=1.0)
        assert program_b and program_b != program_a
    finally:
        st_a.release()
        st_b.release()
        GLState.flush_deletes()


def test_off_main_thread_is_noop(st):
    sf = ShaderFunc(_capture, fragment=FRAG_OK, name="t_thread")
    result = []
    t = threading.Thread(target=lambda: result.append(sf(st, zoom=1.0)))
    t.start()
    t.join()
    assert result == [None]


def test_requires_gl_state():
    sf = ShaderFunc(_capture, fragment=FRAG_OK, name="t_nostate")
    with pytest.raises(TypeError):
        sf(zoom=1.0)


FRAG_FILL = """
#version 330 core
in vec2 uv;
out vec4 FragColor;
void main() { FragColor = vec4(fill, 0.0, 0.0, 1.0); }
"""


def _fill_body(gl_state: GLState = None, **kwargs):
    gl.glBindVertexArray(gl_state.vao("fs_triangle"))
    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)


def test_end_to_end_fullscreen_draw_into_fbo(st):
    """Default fullscreen-triangle vertex stage + injected uniform + GLState
    FBO: render and read the pixels back."""
    sf = ShaderFunc(_fill_body, fragment=FRAG_FILL, name="t_draw")
    fb = st.fbo("target", 8, 8)
    with fb:
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        sf(st, fill=1.0)
        raw = gl.glReadPixels(0, 0, 8, 8, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
    assert sf.last_error is None
    pixels = np.frombuffer(raw, np.uint8).reshape(8, 8, 4)
    assert (pixels[:, :, 0] == 255).all()   # fill=1.0 → solid red
    assert (pixels[:, :, 3] == 255).all()


def test_fbo_resize_recreates(st):
    fb1 = st.fbo("f", 64, 32)
    assert st.fbo("f", 64, 32) is fb1
    fb2 = st.fbo("f", 128, 32)
    assert fb2 is not fb1 and fb2.width == 128
    assert GLState.flush_deletes() >= 1


def test_fbo_scope_restores_viewport(st):
    gl.glViewport(0, 0, 320, 200)
    fb = st.fbo("f2", 16, 16)
    with fb:
        assert tuple(gl.glGetIntegerv(gl.GL_VIEWPORT)) == (0, 0, 16, 16)
    assert tuple(gl.glGetIntegerv(gl.GL_VIEWPORT)) == (0, 0, 320, 200)


def _internal_format_of(tex):
    gl.glBindTexture(gl.GL_TEXTURE_3D, tex.texture_id)
    fmt = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_3D, 0, gl.GL_TEXTURE_INTERNAL_FORMAT)
    gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
    return int(fmt[0]) if hasattr(fmt, "__len__") else int(fmt)


def test_texture3d_internal_format_pairs_with_dtype(st):
    """The internal format must strictly pair with the data dtype —
    float16 → R16F, float32 (and converted dtypes) → R32F."""
    t16 = st.texture3d("fmt16", np.zeros((2, 2, 2), np.float16))
    assert _internal_format_of(t16) == int(gl.GL_R16F)
    t32 = st.texture3d("fmt32", np.zeros((2, 2, 2), np.float32))
    assert _internal_format_of(t32) == int(gl.GL_R32F)
    tint = st.texture3d("fmti", np.zeros((2, 2, 2), np.int32))   # converts → f32
    assert _internal_format_of(tint) == int(gl.GL_R32F)


def test_texture3d_pbo_staging_roundtrip_f16(st):
    """The CPU path stages through a PIXEL_UNPACK PBO (NULL-alloc + SubImage),
    the proven upload recipe — verify content survives exactly for half."""
    data = np.linspace(0, 1, 3 * 4 * 5, dtype=np.float16).reshape(3, 4, 5)
    tex = st.texture3d("pbo16", data)
    gl.glBindTexture(gl.GL_TEXTURE_3D, tex.texture_id)
    raw = gl.glGetTexImage(gl.GL_TEXTURE_3D, 0, gl.GL_RED, gl.GL_FLOAT)
    gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
    arr = np.frombuffer(raw, np.float32) if isinstance(raw, bytes) else np.asarray(raw, np.float32)
    assert np.allclose(arr.reshape(3, 4, 5), data.astype(np.float32), atol=1e-3)


def test_texture3d_upload_survives_dirty_unpack_state(st):
    """Regression for "every other row missing": leftover GL_UNPACK_ROW_LENGTH
    / SKIP_* from other GL code sheared upload rows. Uploads must run under
    canonical tight-row state — and restore whatever was there."""
    gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 7)
    gl.glPixelStorei(gl.GL_UNPACK_SKIP_PIXELS, 3)
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 8)
    try:
        data = np.arange(2 * 3 * 5, dtype=np.float32).reshape(2, 3, 5) / 30.0
        tex = st.texture3d("dirty", data)
        gl.glBindTexture(gl.GL_TEXTURE_3D, tex.texture_id)
        raw = gl.glGetTexImage(gl.GL_TEXTURE_3D, 0, gl.GL_RED, gl.GL_FLOAT)
        gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
        arr = np.frombuffer(raw, np.float32) if isinstance(raw, bytes) else np.asarray(raw, np.float32)
        assert np.array_equal(arr.reshape(2, 3, 5), data)
        # neighbourliness: the dirty state we left is put back
        from meltygui.core.graphics.gl_state import _scalar
        assert _scalar(gl.glGetIntegerv(gl.GL_UNPACK_ROW_LENGTH)) == 7
        assert _scalar(gl.glGetIntegerv(gl.GL_UNPACK_SKIP_PIXELS)) == 3
    finally:
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        gl.glPixelStorei(gl.GL_UNPACK_SKIP_PIXELS, 0)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)


def test_vao_build_owns_buffers(st):
    created = {}

    def build():
        vbo = int(gl.glGenBuffers(1))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        verts = np.array([0, 0, 0, 1, 1, 1], np.float32)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_STATIC_DRAW)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        created["vbo"] = vbo
        return (vbo,)

    vao = st.vao("cube", build)
    assert vao > 0 and gl.glIsBuffer(created["vbo"])
    assert st.vao("cube", build) == vao     # cached, build not re-run
    st.drop("cube")
    GLState.flush_deletes()
    assert not gl.glIsBuffer(created["vbo"])
