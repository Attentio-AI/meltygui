"""Capture-only work is skipped on replay frames; mask paint order is preserved."""
from types import SimpleNamespace
from unittest.mock import Mock
import random
import pytest
from meltygui.core.cache import tile_cache
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles


def test_full_mask_dedup_preserves_overlapping_and_holey_stamps():
    rng = random.Random(42)
    for _ in range(100):
        # Different marks intentionally share a key; identity is what matters.
        marks = [SimpleNamespace(key='same', pixels={x: rng.randrange(100)
                 for x in range(32) if rng.random() < .5}) for _ in range(12)]
        groups = {i: rng.sample(marks, rng.randrange(1, 12)) for i in range(5)}
        original = [r for group in reversed(list(groups.values())) for r in group]
        optimized = list(tile_cache._full_mask_rects(groups))
        def paint(rects):
            pixels = {}
            for r in rects:
                pixels.update(r.pixels)
            return pixels
        assert paint(original) == paint(optimized)
        assert len(optimized) == len({id(r) for r in original})


@pytest.mark.parametrize('pending,rebuild', [(False, False), (True, False), (False, True)])
def test_capture_passes_only_run_with_pending_pixels(monkeypatch, pending, rebuild):
    cache = tile_cache.TileCacheMasked()
    cache._ensure_glow_state()
    cache._snapshot_fbo = 7
    cache._full_mask_tex = 8
    cache._fb_size = cache._fb_alloc_size = (100, 100)
    cache._get_draw_xform = lambda: (0, 0, 1, 1, 100, 100)
    cache._ensure_programs = Mock()
    cache._detect_occluder_changes = Mock()
    class Unchanged:
        def __ne__(self, other):
            return rebuild
    cache._mask_sig_prev = Unchanged()
    ds = SimpleNamespace(closable=True, tile_mode=None)
    rect = tile_cache._Rect(ds, 1, 1, 0, 0, 20, 20, 'test', 0)
    cache._mask_rects.append(rect)
    if pending:
        cache._pending.append(tile_cache._Pending(ds, SimpleNamespace(fbo=-1, mask_tex=9),
                                                 (0, 0), (20, 20), 1, 1, 'test'))
    fake_gl = Mock()
    for name in dir(tile_cache.gl):
        if name.startswith('GL_'):
            setattr(fake_gl, name, getattr(tile_cache.gl, name))
    monkeypatch.setattr(tile_cache, 'gl', fake_gl)
    saved_state = Mock()
    monkeypatch.setattr(tile_cache, '_GLState', lambda: saved_state)
    monkeypatch.setattr(tile_cache.imgui, 'is_mouse_down', lambda _: False)
    monkeypatch.setattr(Melty, 'paint_ordered_ds', [])
    monkeypatch.setattr(Melty, 'default_framebuffer', lambda: 0)
    monkeypatch.setattr(Toggles.Melty, 'mask_rebuild_on_change', True)
    if rebuild:
        class DisplayMaskReached(Exception):
            pass
        def full_mask(rects):
            raise DisplayMaskReached
        monkeypatch.setattr(tile_cache, '_full_mask_rects', full_mask)
        with pytest.raises(DisplayMaskReached):
            cache.finalize_captures((100, 100))
    else:
        cache.finalize_captures((100, 100))
    assert fake_gl.glBlitFramebuffer.call_count == int(pending)
    assert fake_gl.glDrawArrays.call_count == (2 if pending else 0)
    saved_state.restore.assert_called_once()
    cache._detect_occluder_changes.assert_called_once()
    assert not cache._pending and not cache._mask_rects


def test_cached_mask_batch_reuses_only_unchanged_uniforms(monkeypatch):
    cache = tile_cache.TileCacheMasked()
    names = ('uTex', 'uOffset', 'uRectSize', 'uCornerRadius', 'uMargin', 'uUVRect')
    for i, name in enumerate(names):
        setattr(cache, '_loc_texoffr_' + name, i)
    gl = Mock()
    monkeypatch.setattr(tile_cache, 'gl', gl)
    state = {}
    cache._draw_mask_rect_cached(10, 0, 0, 40, 30, .1, 5, uv_rect=(1, 1, 0, 0), batch=state)
    gl.reset_mock()
    cache._draw_mask_rect_cached(11, 5, 10, 40, 30, .1, 5, uv_rect=(1, 1, 0, 0), batch=state)
    gl.glUseProgram.assert_not_called()
    gl.glUniform1i.assert_not_called()
    gl.glUniform1f.assert_not_called()
    gl.glUniform2f.assert_not_called()
    gl.glUniform4f.assert_not_called()
    gl.glBindTexture.assert_called_once_with(gl.GL_TEXTURE_2D, 11)
    gl.glViewport.assert_called_once_with(5, 10, 40, 30)
    gl.glDrawArrays.assert_called_once()
    gl.reset_mock()
    cache._draw_mask_rect_cached(11, 5, 10, 50, 35, .2, 6, 2, (.8, .9, 0, .1), batch=state)
    assert gl.glUniform1f.call_count == 3
    gl.glUniform2f.assert_called_once_with(2, 50., 35.)
    gl.glUniform4f.assert_called_once_with(5, .8, .9, 0, .1)
    # An intervening fresh-mask shader must invalidate the batch.
    state.clear()
    gl.reset_mock()
    cache._draw_mask_rect_cached(11, 5, 10, 50, 35, .2, 6, 2, (.8, .9, 0, .1), batch=state)
    gl.glUseProgram.assert_called_once()
    gl.glUniform1i.assert_called_once_with(0, 0)
    assert gl.glUniform1f.call_count == 3
