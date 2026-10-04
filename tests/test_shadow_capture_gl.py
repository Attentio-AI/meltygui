"""Nested shadow masks must match whether children draw or their parent blits."""
from types import SimpleNamespace

import numpy as np
import pytest
from OpenGL import GL as gl

from meltygui.core.cache import tile_cache
from meltygui.core.melty import Melty
from meltygui.core.runtime.toggles import Toggles, shadow_depth_at


@pytest.mark.parametrize('rebuild_on_change', [False, True])
@pytest.mark.parametrize('scale', [1, 2])
def test_shadow_changes_survive_parent_cache_replay(gl_context, monkeypatch, rebuild_on_change, scale):
    cache = tile_cache.TileCacheMasked()
    monkeypatch.setattr(Melty, 'cache', cache)
    monkeypatch.setattr(Melty, 'paint_ordered_ds', [])
    monkeypatch.setattr(Melty, 'default_framebuffer', lambda: 0)
    monkeypatch.setattr(Toggles.Melty, 'mask_rebuild_on_change', rebuild_on_change)
    monkeypatch.setattr(Toggles, 'glow', False)
    pixels = 128 * scale
    cache._get_draw_xform = lambda: (0, 0, scale, scale, pixels, pixels)
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
    cache.mask_begin_frame((pixels, pixels), (scale, scale))
    cache._ensure_programs()
    try:
        for key, ds in [('parent', parent), ('child', child)]:
            cache._tiles[key] = tile_cache._ensure_tile(None, ds.width, ds.height, draw_state=ds,
                                                        pixel_scale=(scale, scale))

        def frame(fresh, shadow):
            cache.mask_begin_frame((pixels, pixels), (scale, scale))
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
            cache.finalize_captures((pixels, pixels))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, cache._full_mask_fbo)
            return np.array(gl.glReadPixels(0, 0, pixels, pixels, gl.GL_RED, gl.GL_FLOAT)).reshape(pixels, pixels)

        for shadow in [True, False, True]:
            fresh = frame(True, shadow)
            cached = frame(False, shadow)
            # Probe the surface as well as the caster: stale receiving depth
            # also changes shadow intensity even when the caster survives.
            assert fresh[90 * scale, 35 * scale] == pytest.approx(shadow_depth_at(2, 1) / 65535.5, abs=1 / 65535)
            expected = shadow_depth_at(4 if shadow else 2, 1) / 65535.5
            assert fresh[80 * scale, 50 * scale] == pytest.approx(expected, abs=1 / 65535)
            # The child's rounded corner reveals its parent's depth at both
            # backing scales; an unscaled radius would cover it on Retina.
            corner = 30 * scale + int(.75 * scale)
            assert fresh[pixels - 1 - corner, corner] == pytest.approx(
                shadow_depth_at(1, 1) / 65535.5, abs=1 / 65535)
            np.testing.assert_allclose(cached, fresh, atol=1 / 65535, rtol=0)
    finally:
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        cache.cleanup()


@pytest.mark.parametrize('rebuild_on_change', [False, True])
def test_tile_toolbar_shadows_survive_independent_cache_hits(gl_context, monkeypatch, rebuild_on_change):
    """A full-height toolbar body must not erase the picker/link sibling masks."""
    from meltygui.model.tile_model import Tile
    from meltygui.view import tile_view
    from meltygui.core.layout import tile_links

    cache = tile_cache.TileCacheMasked()
    monkeypatch.setattr(Melty, 'cache', cache)
    monkeypatch.setattr(Melty, 'paint_ordered_ds', [])
    monkeypatch.setattr(Melty, 'default_framebuffer', lambda: 0)
    monkeypatch.setattr(Toggles.Melty, 'mask_rebuild_on_change', rebuild_on_change)
    monkeypatch.setattr(Toggles, 'glow', False)
    monkeypatch.setattr(Melty, 'push_clip', lambda *a: None)
    monkeypatch.setattr(Melty, 'pop_clip', lambda: None)
    cursor = [(10, 10)]
    monkeypatch.setattr(tile_view.imgui, 'get_cursor_screen_pos', lambda: cursor[0])
    monkeypatch.setattr(tile_view.imgui, 'set_cursor_screen_pos', lambda pos: cursor.__setitem__(0, pos))
    cache._get_draw_xform = lambda: (0, 0, 1, 1, 256, 128)
    fresh_keys = set()
    states = {}

    def paint(key, width, height, depth, caster=False):
        x, y = cursor[0]
        ds = states.setdefault(key, SimpleNamespace(
            closable=False, tile_mode=None, abs_left=x, abs_top=y,
            width=width, height=height, size_change=False, shadow_margin=0,
            clipped_by_rect=None, parent_window=None, closed=False,
            _parent=None, freeze_resize=False, _is_nested=False))
        cache.key_to_draw_state[key] = ds
        cache._key_to_ctx[key] = True
        rank = shadow_depth_at(depth, 1)
        cache.mask_mark_rect(ds, 1, rank, x, y, width, height, key, 0)
        if key in fresh_keys:
            tile = cache._tiles.get(key)
            if tile is None:
                tile = cache._tiles[key] = tile_cache._ensure_tile(None, width, height, draw_state=ds)
            cache._pending.append(tile_cache._Pending(ds, tile, (x, y), (width, height), 1, rank, key))
            if caster:
                cache._stack.append(SimpleNamespace(key=key))
                cache.add_shadow((x + 4, y + 3, width - 8, height - 6),
                                 offset=2, layer=1, depth=depth, clip=False)
                cache._stack.pop()

    def body(value, width, height, **kwargs):
        paint('body', width, height, 1)
        return False, value

    body.__header_defaults__ = {'tile_toolbar': True}
    tile = Tile(render_func=body)
    endpoint = SimpleNamespace(parameters=('source',), draw_state=None)
    monkeypatch.setattr(tile_links, 'resolve_parameters', lambda *a: {})

    def picker(value, width, height, **kwargs):
        paint('picker', width, height, 3, caster=True)
        return False, value

    def links(endpoint, endpoints, width, height, resize_record):
        paint('links', width, height, 3, caster=True)
        return False

    monkeypatch.setattr(tile_view, 'draw_dropdown', picker)
    monkeypatch.setattr(tile_view, 'draw_tile_links', links)
    gl.glDisable(gl.GL_DEPTH_TEST)
    gl.glDisable(gl.GL_STENCIL_TEST)
    gl.glDisable(gl.GL_CULL_FACE)
    cache.mask_begin_frame((256, 128))
    cache._ensure_programs()
    try:
        def frame(fresh):
            fresh_keys.clear()
            fresh_keys.update(fresh)
            cache.mask_begin_frame((256, 128))
            cursor[0] = (10, 10)
            tile_view.draw_tile_content(tile, 220, 100, endpoints={tile.id: endpoint}, use_cache=True)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            cache.finalize_captures((256, 128))
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, cache._full_mask_fbo)
            return np.array(gl.glReadPixels(0, 0, 256, 128, gl.GL_RED, gl.GL_FLOAT)).reshape(128, 256)

        fresh = frame({'body', 'picker', 'links'})
        for rerender in [set(), {'body'}, {'picker'}, {'links'}, set()]:
            np.testing.assert_allclose(frame(rerender), fresh, atol=1 / 65535, rtol=0)
        # Both controls remain lifted above the body, not merely equally flat.
        expected = shadow_depth_at(5, 1) / 65535.5
        for key in ('picker', 'links'):
            ds = states[key]
            assert fresh[int(128 - ds.abs_top - 12), int(ds.abs_left + 12)] == pytest.approx(expected, abs=1 / 65535)
    finally:
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        cache.cleanup()
