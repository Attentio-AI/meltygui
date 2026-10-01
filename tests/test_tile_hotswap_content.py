"""Definition changes must retire resize history outside the live viewport."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from OpenGL import GL as gl

from meltygui.core.cache import tile_cache
from meltygui.core.melty import Melty


@pytest.mark.parametrize('cascade', [False, True])
def test_function_invalidation_retires_only_affected_resize_history(cascade):
    cache = tile_cache.TileCacheMasked()
    cache.invalidate = Mock()
    cache.invalidate_up = Mock()
    cache.get_child_keys = Mock(return_value={'child': (0, 'child', None)})
    cache.key_to_parent_key.update(view='parent', child='view')
    cache._tiles = {key: SimpleNamespace(content_size=(200, 200), content_stale=False)
                    for key in ('parent', 'view', 'child', 'unrelated')}
    function = lambda: None
    cache.register_func_key(function, 'view')
    invalidate = cache.invalidate_up_by_func if cascade else cache.invalidate_by_func
    invalidate(function, other_windows=False)
    assert {key for key, tile in cache._tiles.items() if tile.content_stale} == (
        {'parent', 'view', 'child'} if cascade else {'parent', 'view'})
    (cache.invalidate_up if cascade else cache.invalidate).assert_called_once()


def test_hotswap_clears_old_chrome_bands_without_losing_resize_content(gl_context, monkeypatch):
    cache = tile_cache.TileCacheMasked()
    monkeypatch.setattr(Melty, 'cache', cache)
    monkeypatch.setattr(Melty, 'default_framebuffer', lambda: 0)
    monkeypatch.setattr(tile_cache, 'request_render', Mock())
    cache.invalidate = Mock()
    draw_state = SimpleNamespace(freeze_resize=True, scroll_offset=(0, 0))
    cache.mask_begin_frame((128, 128))
    function = lambda: None
    cache.register_func_key(function, 'view')
    try:
        tile = tile_cache._ensure_tile(None, 128, 128, draw_state=draw_state)
        cache._tiles['view'] = tile
        aw, ah = tile_cache._tile_alloc(tile)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, tile.fbo)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glColorMask(True, True, True, True)
        gl.glClearColor(0.75, 0.25, 0.5, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        # The cached depth image also retains its prior larger viewport.
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, cache._scratch_fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                 gl.GL_TEXTURE_2D, tile.mask_tex, 0)
        gl.glClearColor(0.5, 0, 0, 0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)

        def pixels(texture, channels):
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            return np.array(gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, channels, gl.GL_FLOAT))

        def color():
            return pixels(tile.tex, gl.GL_RGBA).reshape(ah, aw, 4)

        # Normal shrink/grow retains source pixels, including the old footer
        # band. The definition change is what makes that history invalid.
        assert tile_cache._ensure_tile(tile, 64, 64, draw_state=draw_state) is tile
        cache._scrub_stale_content(tile, draw_state)
        assert np.all(color()[ah - 128:ah, :128] == (0.75, 0.25, 0.5, 1))
        assert tile_cache._ensure_tile(tile, 128, 128, draw_state=draw_state) is tile
        assert tile_cache._ensure_tile(tile, 64, 64, draw_state=draw_state) is tile

        before = color()
        cache.invalidate_by_func(function, other_windows=False)
        assert tile.content_stale
        # Invalidation can arrive without a GL context: it only marks work.
        np.testing.assert_array_equal(color(), before)
        cache._scrub_stale_content(tile, draw_state)
        assert not tile.content_stale
        after = color()
        np.testing.assert_array_equal(after[ah - 64:ah, :64], before[ah - 64:ah, :64])
        assert not after[ah - 128:ah - 64, :128].any()
        assert not after[ah - 64:ah, 64:128].any()
        mask = pixels(tile.mask_tex, gl.GL_RED).reshape(ah, aw)
        assert mask[ah - 32, 32] > 0.49
        assert not mask[ah - 128:ah - 64, :128].any()
        assert not mask[ah - 64:ah, 64:128].any()

        # A subsequent grow must not resurrect cleared body chrome.
        assert tile_cache._ensure_tile(tile, 128, 128, draw_state=draw_state) is tile
        assert not color()[ah - 128:ah - 64, :128].any()
    finally:
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        cache.cleanup()
