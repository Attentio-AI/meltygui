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
