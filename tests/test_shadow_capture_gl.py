"""Nested shadow masks must match whether children draw or their parent blits."""
from types import SimpleNamespace

import numpy as np
import pytest
from OpenGL import GL as gl

from meltygui.core.cache import tile_cache
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles, shadow_depth_at


@pytest.mark.parametrize('rebuild_on_change', [False, True])
def test_shadow_changes_survive_parent_cache_replay(gl_context, monkeypatch, rebuild_on_change):
    cache = tile_cache.TileCacheMasked()
    monkeypatch.setattr(Melty, 'cache', cache)
    monkeypatch.setattr(Melty, 'paint_ordered_ds', [])
    monkeypatch.setattr(Melty, 'default_framebuffer', lambda: 0)
    monkeypatch.setattr(Toggles.Melty, 'mask_rebuild_on_change', rebuild_on_change)
    monkeypatch.setattr(Toggles, 'glow', False)
    cache._get_draw_xform = lambda: (0, 0, 1, 1, 128, 128)
    parent = SimpleNamespace(closable=True, tile_mode=None, abs_left=10, abs_top=10,
                             width=100, height=100, size_change=False, shadow_margin=0,
                             clipped_by_rect=None, parent_window=None, closed=False,
                             _parent=None, freeze_resize=False, _is_nested=False)
    child = SimpleNamespace(**vars(parent))
    child.closable = False
    child.parent_window = parent
    child.abs_left = child.abs_top = 30
    child.width = child.height = 50
    cache.key_to_parent_key['child'] = 'parent'
    cache.key_to_draw_state.update(parent=parent, child=child)
    cache._key_to_ctx.update(parent=True, child=True)
    gl.glDisable(gl.GL_DEPTH_TEST)
    gl.glDisable(gl.GL_STENCIL_TEST)
    gl.glDisable(gl.GL_CULL_FACE)
    cache.mask_begin_frame((128, 128))
    cache._ensure_programs()
    try:
        for key, ds in [('parent', parent), ('child', child)]:
            cache._tiles[key] = tile_cache._ensure_tile(None, ds.width, ds.height, draw_state=ds)

        def frame(fresh, shadow):
            cache.mask_begin_frame((128, 128))
            # Real end-of-view capture order is child then parent. Cached parent
            # replay emits only the parent's rect; its mask carries the subtree.
            views = [('child', child, 2), ('parent', parent, 1)] if fresh else [('parent', parent, 1)]
            for key, ds, depth in views:
                rank = shadow_depth_at(depth, 1)
                cache.mask_mark_rect(ds, 1, rank, ds.abs_left, ds.abs_top,
                                     ds.width, ds.height, key, 5)
                if fresh:
                    cache._pending.append(tile_cache._Pending(ds, cache._tiles[key],
                                          (ds.abs_left, ds.abs_top), (ds.width, ds.height), 1, rank, key))
            if fresh and shadow:
                # flat_button uses add_shadow without emitter-keyed retention.
                cache._stack.append(SimpleNamespace(key='child'))
                cache.add_shadow((40, 40, 20, 20), offset=2, layer=1, depth=2, clip=False)
                cache._stack.pop()
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            cache.finalize_captures((128, 128))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, cache._full_mask_fbo)
            return np.array(gl.glReadPixels(0, 0, 128, 128, gl.GL_RED, gl.GL_FLOAT)).reshape(128, 128)

        for shadow in [True, False, True]:
            fresh = frame(True, shadow)
            cached = frame(False, shadow)
            # Probe the surface as well as the caster: stale receiving depth
            # also changes shadow intensity even when the caster survives.
            assert fresh[90, 35] == pytest.approx(shadow_depth_at(2, 1) / 65535.5, abs=1 / 65535)
            expected = shadow_depth_at(4 if shadow else 2, 1) / 65535.5
            assert fresh[80, 50] == pytest.approx(expected, abs=1 / 65535)
            np.testing.assert_allclose(cached, fresh, atol=1 / 65535, rtol=0)
    finally:
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        cache.cleanup()
