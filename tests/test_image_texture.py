"""Decoded image values render without a pending view or shared path cache."""
import io
import pickle
import inspect
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import OpenGL.GL as gl
import pytest
from PIL import Image

from meltygui.code.new_codecs import ImageCodec
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.conversion.load_save_v2 import LSDUnpickler
from meltygui.core.graphics.gl_state import GLState
from meltygui.model.texture_model import ImageTexture


def decoded(color=(255, 0, 0), key='image'):
    stream = io.BytesIO()
    Image.new('RGB', (3, 2), color).save(stream, format='PNG')
    return ImageCodec.decode_bytes(stream.getvalue(), key)


def test_decode_worker_does_not_upload_and_same_path_can_reload():
    with ThreadPoolExecutor(max_workers=1) as worker:
        first = worker.submit(decoded).result()
        second = worker.submit(decoded, (0, 255, 0)).result()
    assert isinstance(first, ImageTexture)
    assert first._states == second._states == {}
    assert first is not second and first.data != second.data


def test_upload_is_lazy_tightly_packed_and_owned(gl_context):
    first, second = decoded(), decoded((0, 255, 0))
    previous = int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D))
    unpack = int(gl.glGetIntegerv(gl.GL_UNPACK_ALIGNMENT))
    row_length = int(gl.glGetIntegerv(gl.GL_UNPACK_ROW_LENGTH))
    pack = int(gl.glGetIntegerv(gl.GL_PACK_ALIGNMENT))
    try:
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 8)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 7)
        first_id = int(first)
        assert first_id == int(first)
        assert int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)) == previous
        assert int(gl.glGetIntegerv(gl.GL_UNPACK_ALIGNMENT)) == 8
        assert int(gl.glGetIntegerv(gl.GL_UNPACK_ROW_LENGTH)) == 7
        second_id = int(second)
        assert second_id != first_id
        gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
        for texture, color in ((first, (255, 0, 0)), (second, (0, 255, 0))):
            gl.glBindTexture(gl.GL_TEXTURE_2D, int(texture))
            assert gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_INTERNAL_FORMAT) == gl.GL_SRGB8
            pixels = gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGB, gl.GL_UNSIGNED_BYTE)
            assert np.frombuffer(pixels, dtype=np.uint8).reshape(2, 3, 3).tolist() == [[list(color)] * 3] * 2
        restored = pickle.loads(pickle.dumps(first))
        assert restored._states == {} and restored.data == first.data
        first.release()
        GLState.flush_deletes()
        assert not gl.glIsTexture(first_id)
        assert gl.glIsTexture(second_id)
    finally:
        first.release()
        second.release()
        GLState.flush_deletes()
        gl.glBindTexture(gl.GL_TEXTURE_2D, previous)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, unpack)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, row_length)
        gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, pack)


def test_old_image_symbols_resolve_to_direct_renderer_and_model():
    from meltygui.view.texture_view import draw_texture
    from meltygui.core.rendering.render_funcs import RenderFuncs
    unpickler = LSDUnpickler(io.BytesIO())
    assert unpickler.find_class('meltygui.graphics.texture_manager', 'PendingTexture') is ImageTexture
    assert unpickler.find_class('meltygui.view.texture_view', 'draw_pending_texture') is draw_texture
    for module, expected in (
        ('meltygui.view.texture_view', draw_texture),
        (DictConversion.RENDER_FUNC_MODULE, RenderFuncs.draw_texture),
    ):
        ref = ('draw_pending_texture', module, DictConversion.FUNCTION_TAG)
        assert DictConversion.resolve_callable(ref) is expected


@pytest.mark.parametrize('jet', [False, True])
def test_direct_view_uses_numeric_gl_id_and_preserves_input(gl_context, monkeypatch, jet):
    import meltygui_imgui as imgui
    from meltygui.core.rendering.core_decoration import Core
    from meltygui.state.new_core_model import ZoomState
    from meltygui.state.texture_state import TextureViewState
    from meltygui.view.texture_view import draw_texture
    texture = decoded()
    filters = SimpleNamespace(**{name: Mock(side_effect=lambda value, **kw: value)
                                for name in ('brightness_contrast', 'hue_saturation', 'jet')})
    monkeypatch.setattr(Core, 'melty', SimpleNamespace(
        filter=filters, text_focused_ds=None, silence_invalidate=True,
        push_clip=Mock(), pop_clip=Mock()))
    ds = SimpleNamespace(width=400, height=250, abs_left=0, abs_top=0,
                         on_action=lambda name: None)
    prepared, resources = TextureViewState(), GLState()
    imgui.new_frame()
    imgui.begin('Direct texture')
    try:
        changed, result = inspect.unwrap(draw_texture)(
            texture, hovered=False, scroll_y_changed=None, middle_mouse_drag=None,
            double_right_mouse_drag=None, zoom_state=ZoomState(), zoom_speed=.3,
            draw_state=ds, jet=jet, _texture_state=prepared, gl_state=resources)
        assert not changed and result is texture
        assert filters.brightness_contrast.call_args.args == (int(texture),)
        assert type(filters.brightness_contrast.call_args.args[0]) is int
        assert prepared.texture_id != int(texture)
        assert filters.brightness_contrast.call_args.kwargs['output_texture'] != prepared.texture_id
    finally:
        imgui.end()
        imgui.end_frame()
        texture.release()
        resources.release()
        GLState.flush_deletes()
