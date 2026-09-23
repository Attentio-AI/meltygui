from array import array
from unittest.mock import Mock

from meltygui.core.cache import mask_batch


def stamp(texture, rank=0, rect=(0, 0, 20, 30), clip=(0, 0, 20, 30)):
    return texture, rect, clip, (.5, .75, 0, .25), rank, 5, 0


def unpack(data):
    values = array('f')
    values.frombytes(data)
    return [list(values[i:i+16]) for i in range(0, len(values), 16)]


def test_sampler_overflow_preserves_overlapping_stamp_order_and_reuses_slots():
    textures = [10, 11, 10, None, 12, 13, 14, 15, 16, 17, 18, None, 10]
    batches = list(mask_batch.batches(stamp(texture, i) for i, texture in enumerate(textures)))
    assert [batch[2] for batch in batches] == [5, 3, 4, 1]
    assert batches[0][0] == [10, 11, 12]
    restored = []
    for bank, data, count in batches:
        records = unpack(data)
        assert len(records) == count
        for record in records:
            slot = int(record[15])
            restored.append((bank[slot] if slot >= 0 else None, record[12]))
    assert restored == list(zip(textures, range(len(textures))))


def test_clipping_keeps_mask_uvs_relative_to_original_rect_and_culls_empty():
    result = list(mask_batch.batches([
        stamp(1, rect=(10, 20, 30, 40), clip=(20, 25, 40, 10)),
        stamp(2, rect=(100, 100, 5, 5))]))
    bank, data, count = result[0]
    assert bank == [1] and count == 1
    record = unpack(data)[0]
    assert record[:8] == [10, 20, 30, 40, 20, 25, 40, 35]
    assert record[8:12] == [.5, .75, 0, .25]


def test_draw_restores_mask_pass_routing_even_on_failure(monkeypatch):
    import pytest
    fake = Mock()
    for name in dir(mask_batch.gl):
        if name.startswith('GL_'):
            setattr(fake, name, getattr(mask_batch.gl, name))
    fake.glGetIntegerv.return_value = 123
    fake.glDrawArraysInstanced.side_effect = RuntimeError('driver failure')
    monkeypatch.setattr(mask_batch, 'gl', fake)
    renderer = mask_batch.MaskBatch()
    renderer.ensure = Mock()
    renderer.program, renderer.vao, renderer.vbo = 1, 2, 3
    renderer.fb_location, renderer.textures_location = 4, 5
    with pytest.raises(RuntimeError, match='driver failure'):
        renderer.draw([stamp(20), stamp(21)], 100, 100, restore_vao=123)
    assert fake.glActiveTexture.call_args.args == (fake.GL_TEXTURE0,)
    fake.glGetIntegerv.assert_not_called()
    assert fake.glBindVertexArray.call_args.args == (123,)
    # Even unused slots receive a source, avoiding framebuffer feedback.
    assert fake.glBindTexture.call_args_list[2].args == (fake.GL_TEXTURE_2D, 20)


def test_bulk_path_uploads_once_and_advances_instance_offsets(monkeypatch):
    fake = Mock()
    for name in dir(mask_batch.gl):
        if name.startswith('GL_'):
            setattr(fake, name, getattr(mask_batch.gl, name))
    monkeypatch.setattr(mask_batch, 'gl', fake)
    renderer = mask_batch.MaskBatch()
    renderer.ensure = Mock()
    renderer.multi_bind = True
    renderer.program, renderer.vao, renderer.vbo = 1, 2, 3
    renderer.fb_location = 4
    renderer.draw([stamp(i) for i in range(7)], 100, 100, restore_vao=9)
    assert fake.glBufferData.call_count == 1
    assert [call.args[3:] for call in fake.glDrawArraysInstancedBaseInstance.call_args_list] == [(3, 0), (3, 3), (1, 6)]
    assert fake.glBindTextures.call_args_list[-1].args == (0, 3, [6, 6, 6])
    fake.glGetIntegerv.assert_not_called()


def test_bulk_binding_extension_is_used_on_a_43_context(monkeypatch):
    from OpenGL.GL.ARB import multi_bind
    from meltygui.core.cache import tile_cache
    fake = Mock()
    for name in dir(mask_batch.gl):
        if name.startswith('GL_'):
            setattr(fake, name, getattr(mask_batch.gl, name))
    fake.glGetIntegerv.side_effect = [4, 3]
    fake.glGenVertexArrays.return_value = 10
    fake.glGenBuffers.return_value = 11
    monkeypatch.setattr(mask_batch, 'gl', fake)
    monkeypatch.setattr(tile_cache, '_compile', Mock(return_value=1))
    monkeypatch.setattr(tile_cache, '_link', Mock(return_value=2))
    supported = Mock(return_value=True)
    monkeypatch.setattr(multi_bind, 'glInitMultiBindARB', supported)
    renderer = mask_batch.MaskBatch()
    renderer.ensure()
    assert renderer.multi_bind
    supported.assert_called_once()
    fake.glGetIntegerv.reset_mock()
    renderer.ensure()
    fake.glGetIntegerv.assert_not_called()
    renderer.close()
    fake.glDeleteTextures.assert_not_called()  # Source images remain cache-owned.
