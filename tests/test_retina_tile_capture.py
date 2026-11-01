"""Cached views retain physical pixels while their geometry stays in points."""
from types import SimpleNamespace

import numpy as np
import pytest
from OpenGL import GL as gl

from meltygui.core.cache import tile_cache
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles, shadow_depth_at


@pytest.mark.parametrize('scale', [(1., 1.), (2., 2.), (3., 3.), (1.5, 2.)])
@pytest.mark.parametrize('position', [(20., 12.), (20.125, 12.25), (20.2, 12.4)])
def test_capture_keeps_native_pixels_and_cached_mask(gl_context, monkeypatch, scale, position):
    from math import ceil, floor
    px, py = position
    cache = tile_cache.TileCacheMasked()
    monkeypatch.setattr(Melty, 'cache', cache)
    monkeypatch.setattr(Melty, 'paint_ordered_ds', [])
    monkeypatch.setattr(Toggles, 'glow', False)
    fw, fh = int(128 * scale[0]), int(96 * scale[1])
    origin = (6, 8)  # framebuffer pixels, independent of the display scale
    monkeypatch.setattr(Melty, 'framebuffer_size', (fw, fh))
    monkeypatch.setattr(Melty, 'frame_origin', origin)
    monkeypatch.setattr(tile_cache.imgui, 'get_draw_data', lambda: SimpleNamespace(
        display_pos=(0, 0), display_size=(128, 96)))
    monkeypatch.setattr(tile_cache.imgui, 'get_io', lambda: SimpleNamespace(
        display_fb_scale=scale, display_size=(128, 96)))
    source_tex = tile_cache._create_color_tex(fw, fh)
    source_fbo, _ = tile_cache._create_fbo_with_tex(source_tex, False, fw, fh)
    monkeypatch.setattr(Melty, 'default_framebuffer', lambda: source_fbo)
    # Single-physical-pixel stripes catch accidental downsampling as well as
    # wrong rectangles; unique green/blue coordinates catch incorrect origins.
    yy, xx = np.indices((fh, fw))
    source = np.stack((xx % 2, xx / fw, yy / fh, np.ones_like(xx)), axis=-1).astype('f')
    gl.glBindTexture(gl.GL_TEXTURE_2D, source_tex)
    gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, fw, fh, gl.GL_RGBA, gl.GL_FLOAT, source)
    ds = SimpleNamespace(closable=True, tile_mode=None, abs_left=px, abs_top=py,
                         width=40, height=32, size_change=False, shadow_margin=0,
                         clipped_by_rect=None, parent_window=None, closed=False,
                         _parent=None, freeze_resize=False, _is_nested=False)
    rank = shadow_depth_at(1, 1)
    cache.key_to_draw_state['view'] = ds
    cache._key_to_ctx['view'] = True
    cache.key_to_parent_key['view'] = None
    gl.glDisable(gl.GL_DEPTH_TEST)
    gl.glDisable(gl.GL_STENCIL_TEST)
    gl.glDisable(gl.GL_CULL_FACE)
    try:
        cache.mask_begin_frame((fw, fh), scale)
        tile = tile_cache._ensure_tile(None, 40, 32, draw_state=ds, pixel_scale=scale)
        cache._tiles['view'] = tile
        cache.mask_mark_rect(ds, 1, rank, px, py, 40, 32, 'view', 0)
        cache._pending.append(tile_cache._Pending(ds, tile, position, (40, 32), 1, rank, 'view'))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, source_fbo)
        cache.finalize_captures((fw, fh))
        aw, ah = tile_cache._tile_pixel_size(tile, allocated=True)
        w, h = tile_cache._tile_pixel_size(tile)
        assert (w, h) == (int(40 * scale[0]), int(32 * scale[1]))
        gl.glBindTexture(gl.GL_TEXTURE_2D, tile.tex)
        pixels = np.asarray(gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_FLOAT)).reshape(ah, aw, 4)
        x = floor(px * scale[0]) + origin[0]
        y = floor(fh - (py + 32) * scale[1] - origin[1])
        right = ceil((px + 40) * scale[0]) + origin[0]
        top = ceil(fh - py * scale[1] - origin[1])
        cw, ch = right - x, top - y
        np.testing.assert_allclose(pixels[ah - ch:ah, :cw], source[y:top, x:right], atol=.001)
        assert tile.capture_offset == pytest.approx((px * scale[0] + origin[0] - x,
                                                     top - (fh - py * scale[1] - origin[1])))
        us, vs, u0, v0 = tile_cache._tile_uv_rect(tile)
        # Replay at the capture origin addresses the exact same texel centers:
        # neither the image nor its guard texel is resampled during capture.
        for screen_x in range(x + 1, right - 1):
            local_x = screen_x + .5 - (px * scale[0] + origin[0])
            uv = u0 + local_x / (40 * scale[0]) * us
            assert uv * aw == pytest.approx(screen_x - x + .5)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, cache._full_mask_fbo)
        fresh_mask = np.array(gl.glReadPixels(0, 0, fw, fh, gl.GL_RED, gl.GL_FLOAT))
        assert fresh_mask.max() > 0

        cache.mask_begin_frame((fw, fh), scale)
        cache.mask_mark_rect(ds, 1, rank, px, py, 40, 32, 'view', 0)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, source_fbo)
        cache.finalize_captures((fw, fh))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, cache._full_mask_fbo)
        cached_mask = np.array(gl.glReadPixels(0, 0, fw, fh, gl.GL_RED, gl.GL_FLOAT))
        np.testing.assert_allclose(cached_mask, fresh_mask, atol=1 / 65535, rtol=0)
    finally:
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        cache.cleanup()
        gl.glDeleteFramebuffers(1, [source_fbo])
        gl.glDeleteTextures(1, [source_tex])
