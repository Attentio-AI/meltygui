"""The CUDA raymarcher's finished image reaches a display-GPU texture GPU to GPU
(image_to_texture): exact pixels from the GL device and from every other CUDA
device, and the pinned-host route when interop is off. Skips cleanly without
CUDA or a GL-enabled pycuda."""

import numpy as np
import OpenGL.GL as gl
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip('meltygui_pycuda.gl')

from meltygui.core.graphics.cuda_interop_core import ensure_context
from meltygui.core.graphics.gl_state import GLState
from meltygui.core.runtime.toggles import Toggles
from meltygui.model.cuda_texture_model import image_to_texture
from meltygui.model.texture_model import _upload_cuda_image

needs_cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="no CUDA device")
DEVICES = range(torch.cuda.device_count()) if torch.cuda.is_available() else ()


@pytest.fixture
def cuda_state(gl_context):
    if not ensure_context():
        pytest.skip("no CUDA context available alongside the GL context")
    state = GLState()
    yield state
    state.release()
    GLState.flush_deletes()


def image_on(device, width=37, height=23, seed=0):
    generator = torch.Generator().manual_seed(seed)
    return torch.rand(height, width, 4, generator=generator).to(torch.float16).to(f'cuda:{device}')


def readback(texture):
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture.texture_id)
    raw = gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_HALF_FLOAT)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    pixels = np.frombuffer(raw, np.float16) if isinstance(raw, bytes) else np.asarray(raw, np.float16)
    return pixels.reshape(texture.shape[0], texture.shape[1], 4)


@needs_cuda
@pytest.mark.parametrize('device', DEVICES)
def test_image_from_every_device_arrives_exact(cuda_state, device):
    image = image_on(device)
    texture = image_to_texture(cuda_state, 'image', image.data_ptr(), device, image.shape[1], image.shape[0])
    assert texture is not None and texture.internal_format == gl.GL_RGBA16F
    assert np.array_equal(readback(texture), image.cpu().numpy())


@needs_cuda
def test_every_call_uploads_the_new_frame_into_the_same_texture(cuda_state):
    first, second = image_on(0, seed=1), image_on(0, seed=2)
    one = image_to_texture(cuda_state, 'image', first.data_ptr(), 0, first.shape[1], first.shape[0])
    two = image_to_texture(cuda_state, 'image', second.data_ptr(), 0, second.shape[1], second.shape[0])
    assert two.texture_id == one.texture_id
    assert np.array_equal(readback(two), second.cpu().numpy())


@needs_cuda
def test_resize_makes_a_texture_of_the_new_size(cuda_state):
    small, large = image_on(0, 16, 8), image_on(0, 40, 30)
    image_to_texture(cuda_state, 'image', small.data_ptr(), 0, 16, 8)
    texture = image_to_texture(cuda_state, 'image', large.data_ptr(), 0, 40, 30)
    assert texture.shape == (30, 40)
    assert np.array_equal(readback(texture), large.cpu().numpy())


@needs_cuda
@pytest.mark.parametrize('interop', [True, False])
def test_upload_routes_agree(cuda_state, monkeypatch, interop):
    monkeypatch.setattr(Toggles.Voxels, 'cuda_image_interop', interop)
    image = image_on(torch.cuda.device_count() - 1, seed=3)
    texture = _upload_cuda_image(cuda_state, image)
    assert (cuda_state._resources.get('cuda_image_interop') is not None) == interop
    assert np.array_equal(readback(texture), image.cpu().numpy())
