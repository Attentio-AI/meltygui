"""Real GL calibration checks. Run on a reserved agent desktop only."""
import time
import numpy as np
import pytest
from OpenGL import GL as gl
from conftest import _ensure_gl_context
from meltygui.core.rendering._gui_native import TextureCache
from meltygui.core.rendering.gui_calibration import OPERATIONS


@pytest.fixture
def cache():
    _ensure_gl_context()
    owner = TextureCache()
    yield owner
    owner.close()


def collect(cache):
    # Test-only completion loop. Production only polls on existing frame ticks.
    gl.glFlush()
    result = []
    deadline = time.monotonic()+2
    while cache.calibration_pending() and time.monotonic() < deadline:
        result.extend(cache.calibration_poll())
        time.sleep(.001)
    assert cache.calibration_pending() == 0
    return result


def test_active_context_identity_and_bounded_queue_cancel(cache):
    environment = cache.calibration_environment()
    assert environment[1] == gl.glGetString(gl.GL_RENDERER).decode()
    assert environment[-2].startswith('uuid:') or environment[-2] == 'unidentified-device'
    for _ in range(4): assert cache.calibration_probe('clear', 32)
    assert not cache.calibration_probe('clear', 32)
    assert cache.calibration_pending() == 4
    cache.calibration_cancel()
    assert cache.calibration_pending() == 0
    assert not cache.calibration_probe('clear', 8192)
    cache.calibration_probe('upload', 32)
    cache.close()
    assert cache.calibration_pending() == 0
    assert gl.glGetError() == gl.GL_NO_ERROR


@pytest.mark.parametrize('size', [32,128,512])
def test_baselines_return_separate_clocks_restore_state_and_preserve_pixels(cache, size):
    texture = cache.target(1, 16, 16)
    cache.begin_packet(1)
    cache.render(1, (.2,.4,.6,1.))
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
    before = gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
    gl.glViewport(3,4,50,60)
    gl.glEnable(gl.GL_SCISSOR_TEST)
    gl.glScissor(5,6,30,40)
    gl.glEnable(gl.GL_DEPTH_TEST)
    gl.glEnable(gl.GL_STENCIL_TEST)
    gl.glEnable(gl.GL_FRAMEBUFFER_SRGB)
    gl.glColorMask(False, True, False, True)
    enums = (gl.GL_VIEWPORT,gl.GL_SCISSOR_BOX,gl.GL_CURRENT_PROGRAM,
             gl.GL_DRAW_FRAMEBUFFER_BINDING,gl.GL_VERTEX_ARRAY_BINDING,
             gl.GL_TEXTURE_BINDING_2D,gl.GL_ARRAY_BUFFER_BINDING,gl.GL_COLOR_WRITEMASK,
             gl.GL_DEPTH_TEST,gl.GL_STENCIL_TEST,gl.GL_FRAMEBUFFER_SRGB)
    state = {enum: tuple(np.atleast_1d(gl.glGetIntegerv(enum))) for enum in enums}
    try:
        for op in OPERATIONS:
            assert cache.calibration_probe(op,size)
            assert all(tuple(np.atleast_1d(gl.glGetIntegerv(enum))) == value for enum,value in state.items())
            observation, = collect(cache)
            name, view, units, cpu, gpu = observation
            assert name == op and view == 0
            assert units == size*size*(4 if op=='upload' else 1)
            assert cpu >= 0 and (gpu is None or gpu > 0)
            assert gl.glGetError() == gl.GL_NO_ERROR
        assert gl.glGetTexImage(gl.GL_TEXTURE_2D,0,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE) == before
        cache.render(1,(.2,.4,.6,1.))
        assert gl.glGetTexImage(gl.GL_TEXTURE_2D,0,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE) == before
    finally:
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glDisable(gl.GL_FRAMEBUFFER_SRGB)
        gl.glColorMask(True,True,True,True)


def test_sampled_replays_rotate_views_without_extra_rendering(cache):
    for view in (1,2,3):
        cache.target(view,16,16)
        cache.begin_packet(view)
    sampled = []
    before = cache.stats()[0]
    for _ in range(3):
        cache.calibration_arm()
        cache.render_many([(view,(.1,.2,.3,1.)) for view in (1,2,3)])
        observation, = collect(cache)
        assert observation[0] == 'replay'
        sampled.append(observation[1])
    assert set(sampled) == {1,2,3}
    assert cache.stats()[0]-before == 9
    assert gl.glGetError() == gl.GL_NO_ERROR


def test_probes_respect_live_texture_budget():
    _ensure_gl_context()
    cache = TextureCache(budget=16384)
    try:
        assert not cache.calibration_probe('allocate',512)
        assert cache.calibration_pending() == 0
    finally:
        cache.close()


def test_real_allocations_take_priority_over_pending_probe_resources(cache):
    assert cache.calibration_probe('allocate',512)
    assert cache.calibration_pending() == 1
    cache.target(42,16,16)
    assert cache.calibration_pending() == 0
    cache.begin_packet(42)
    cache.calibration_arm()
    cache.render(42)
    assert cache.calibration_pending() == 1  # Passive span survives scratch allocation.
    observation, = collect(cache)
    assert observation[0] == 'replay'


def test_lost_context_forgets_queries_without_deleting_other_context_objects():
    import glfw
    _ensure_gl_context()
    previous = glfw.get_current_context()
    glfw.window_hint(glfw.VISIBLE,False)
    window = glfw.create_window(32,32,'calibration lost context',None,None)
    assert window
    glfw.make_context_current(window)
    cache = TextureCache()
    assert cache.calibration_probe('clear',32)
    assert cache.calibration_pending()==1
    glfw.make_context_current(previous)
    glfw.destroy_window(window)  # The lost context reclaims all of its GL objects.
    marker = int(gl.glGenQueries(1)[0])
    gl.glBeginQuery(gl.GL_TIME_ELAPSED,marker)
    gl.glEndQuery(gl.GL_TIME_ELAPSED)
    try:
        cache.calibration_cancel(context_lost=True)
        assert cache.calibration_pending()==0
        assert gl.glIsQuery(marker)
        assert gl.glGetError()==gl.GL_NO_ERROR
    finally:
        gl.glDeleteQueries(1,[marker])
    # Ordinary cache.close() requires the original context. It must not run here.
