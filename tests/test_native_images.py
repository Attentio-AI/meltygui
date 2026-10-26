"""CPU bitmaps use the active native GPU owner and its deferred lifetime."""
import threading

import pytest

from meltygui.core.melty import Melty
from meltygui.core.graphics import gl_state
from meltygui.core.windowing import glfw_utils
from meltygui.model.texture_model import ImageTexture
from meltygui.model.folder_icon_model import FolderIconTexture, IconPixels


class RecordingGPU:
    def __init__(self):
        self.uploads = []
        self.deleted = []

    def create_texture(self, *args):
        self.uploads.append(args)
        return len(self.uploads)

    def delete_texture(self, handle):
        self.deleted.append(handle)


@pytest.fixture
def gpu(monkeypatch):
    gpu = RecordingGPU()
    monkeypatch.setattr(Melty, 'graphics_backend', gpu)
    monkeypatch.setattr(glfw_utils, '_render_thread_id', threading.get_ident())
    monkeypatch.setattr(gl_state, 'current_context', lambda: None)
    monkeypatch.setattr(gl_state, '_delete_queue', [])
    monkeypatch.setattr(gl_state, '_gl_thread', threading.current_thread())
    return gpu


def test_rgb_image_is_converted_once_and_release_is_deferred(gpu):
    texture = ImageTexture('test', 2, 1, 0x1907, bytes((255, 0, 0, 0, 255, 0)))
    assert not gpu.uploads
    assert int(texture) == int(texture) == 1
    assert gpu.uploads == [(2, 1, 'rgba8srgb', bytes((255, 0, 0, 255, 0, 255, 0, 255)))]
    texture.release()
    assert not gpu.deleted
    gl_state.GLState.flush_deletes()
    assert gpu.deleted == [1]


def test_folder_artwork_uses_the_same_gpu_and_ownership(gpu, tmp_path):
    pixels = bytes((12, 30, 60, 255))
    texture = FolderIconTexture(IconPixels(tmp_path / 'icon.png', 1, 1, pixels))
    assert int(texture) == int(texture) == 1
    assert gpu.uploads == [(1, 1, 'rgba8srgb', pixels)]
    texture.release()
    gl_state.GLState.flush_deletes()
    assert gpu.deleted == [1]


def test_decode_worker_cannot_upload_to_native_gpu(gpu):
    errors = []
    texture = ImageTexture('test', 1, 1, 0x1908, bytes(4))

    def upload():
        try:
            int(texture)
        except RuntimeError as error:
            errors.append(str(error))

    worker = threading.Thread(target=upload)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors and 'render thread' in errors[0]
    assert not gpu.uploads


def test_native_image_inspector_keeps_per_view_outputs_and_sampler(gpu, monkeypatch):
    import inspect
    from types import SimpleNamespace
    import numpy as np
    from meltygui.core.graphics import desktop_gl
    from meltygui.state.new_core_model import ZoomState
    from meltygui.state.texture_state import TextureViewState
    from meltygui.view import texture_view

    gpu._textures = {101: (3, 2, 'rgba16f')}
    gpu.sampling, gpu.passes = {}, []
    gpu.set_texture_sampling = lambda handle, *mode: gpu.sampling.update({handle: mode})
    gpu.draw = lambda name, target, **values: gpu.passes.append((name, target, values))
    monkeypatch.setattr(desktop_gl, '__getattr__', lambda name: (_ for _ in ()).throw(AssertionError('Desktop GL: ' + name)))
    monkeypatch.setattr(texture_view.imgui, 'dummy', lambda *args: None)
    monkeypatch.setattr(texture_view.imgui, 'get_text_line_height', lambda: 16)
    states = [TextureViewState(), TextureViewState()]
    owners = [gl_state.GLState(), gl_state.GLState()]
    view = SimpleNamespace(width=400, height=300, abs_left=0, abs_top=0, on_action=lambda name: None)
    for i in range(2):
        zoom = ZoomState(); zoom.brightness = i * .5
        inspect.unwrap(texture_view.draw_texture)(np.uint32(101), False, None, None, None, zoom, .3,
            draw_state=view, _texture_state=states[i], gl_state=owners[i], nearest=bool(i), dim_outside=(0, 0, 1, 1))
    assert states[0].texture_id != states[1].texture_id
    assert (states[0].width, states[0].height) == (3, 2)
    assert [p[0] for p in gpu.passes] == ['brightness_contrast', 'hue_saturation', 'multiply'] * 2
    assert gpu.passes[0][2]['brightness'] == 0 and gpu.passes[3][2]['brightness'] == .5
    assert gpu.sampling[states[0].texture_id] == (False, False)
    assert gpu.sampling[states[1].texture_id] == (True, True)
    for owner in owners: owner.release()
    gl_state.GLState.flush_deletes()
    assert set(gpu.deleted) == {1, 2, 3, 4, 5, 6}
