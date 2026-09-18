"""Explicit voxel views accept the same values through view_func overrides."""
import inspect

import numpy as np
import OpenGL.GL as gl
import pytest

from meltygui import draw_voxels, draw_voxels_cuda, draw_voxels_opengl
from meltygui.core.graphics.gl_state import GLState
from meltygui.core.graphics.tensor_core import _voxels_cleanup


def test_backend_views_expose_injected_state_events_and_live_controls():
    from meltygui.core.rendering.parameter_core import view_param_names
    from types import SimpleNamespace
    signature = inspect.signature(draw_voxels)
    for view in (draw_voxels_opengl, draw_voxels_cuda):
        assert inspect.signature(view) == signature
        controls = view_param_names(SimpleNamespace(_view_func=view))
        assert {'tilt', 'spin', 'density', 'step_size'} <= set(controls)
        assert 'cuda_march' not in controls
        assert 'backend' not in controls


def _frame(runtime, source, view):
    from test_render_func_integration import _tick_frame
    from conftest import begin_frame, end_frame
    _tick_frame(runtime)
    begin_frame()
    try:
        changed, value, state = draw_voxels(
            source, view_func=view, name='backend selection', width=160, height=120,
            x_dim=2, y_dim=1, z_dim=0, step_size=0.01, max_steps=256,
            name_size=0, num_size=0, draw_plane=False, draw_shading=False,
            show_bg=False, with_header=None, show_header=False, shadow=False,
            use_cache=False, return_extras=True)
        assert not changed
        assert value is source
        return state
    finally:
        end_frame()


@pytest.mark.parametrize('kind', ['numpy', 'cpu', 'cuda:0', 'cuda:1', 'cuda:2'])
def test_opengl_view_uploads_tensor_inputs_and_reuses_texture(gl_context, kind):
    import torch
    from test_render_func_integration import _init_melty
    if kind.startswith('cuda:') and torch.cuda.device_count() <= int(kind[-1]):
        pytest.skip('CUDA device unavailable')
    data = np.linspace(0, 1, 120, dtype=np.float32).reshape(4, 5, 6)
    source = data if kind == 'numpy' else torch.tensor(data, device=kind)
    runtime = _init_melty()
    state = _frame(runtime, source, draw_voxels_opengl)
    resources = state.misc['gl_state']
    try:
        volume = resources.peek('volume_cuda') if kind.startswith('cuda:') else resources.peek('volume')
        texture = getattr(volume, 'texture', volume)
        assert texture is not None
        assert resources.peek('cuda_view') is None
        assert resources.peek('cuda_out') is None
        gl.glBindTexture(gl.GL_TEXTURE_3D, texture.texture_id)
        raw = gl.glGetTexImage(gl.GL_TEXTURE_3D, 0, gl.GL_RED, gl.GL_FLOAT)
        gl.glBindTexture(gl.GL_TEXTURE_3D, 0)
        pixels = np.frombuffer(raw, np.float32) if isinstance(raw, bytes) else np.asarray(raw)
        np.testing.assert_allclose(pixels.reshape(data.shape), data, atol=0.001)
        assert _frame(runtime, source, draw_voxels_opengl) is state
        assert (resources.peek('volume_cuda') if kind.startswith('cuda:') else resources.peek('volume')) is volume
        assert resources.peek('target') is not None
    finally:
        _voxels_cleanup(state)
        GLState.flush_deletes()


def test_override_switches_same_cuda_tensor_between_backends(gl_context):
    import torch
    from test_render_func_integration import _init_melty
    if not torch.cuda.is_available():
        pytest.skip('CUDA unavailable')
    runtime = _init_melty()
    source = torch.rand(4, 5, 6, device='cuda')
    states = []
    try:
        for view in (draw_voxels_cuda, draw_voxels_opengl, draw_voxels_cuda):
            state = _frame(runtime, source, view)
            states.append(state)
            resources = state.misc['gl_state']
            if view is draw_voxels_cuda:
                assert resources.peek('cuda_view').view.data_ptr() == source.data_ptr()
                # interop texture, or the pinned-host one where CUDA cannot reach this GL context
                assert (resources.peek('cuda_image_interop') is not None
                        or resources.peek('cuda_image') is not None)
                assert state.misc['voxel_state'].cuda_error is None
            else:
                assert resources.peek('volume_cuda') is not None
                assert resources.peek('cuda_view') is None
    finally:
        for state in states:
            _voxels_cleanup(state)
        GLState.flush_deletes()
