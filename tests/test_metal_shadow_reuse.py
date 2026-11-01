"""Shadow reuse follows depth writes and lighting, independently of scene color."""
from types import SimpleNamespace

import pytest

from meltygui.core.graphics.metal_renderer import MetalRenderer
from meltygui.core.graphics.metal_tiles import MetalTileBackend
from meltygui.core.cache.tile_cache import TileCacheMasked
from meltygui.core.runtime.toggles import Toggles


class RecordingRenderer(MetalRenderer):
    def __init__(self):
        self._textures = {1: (64, 48, 'rgba16f')}
        self._targets = {}
        self._shadow_key = None
        self.scene_framebuffer = 1
        self.calls = []
        self.next_texture = 1

    def create_texture(self, width, height, format='rgba16f', pixels=None):
        self.next_texture += 1
        self._textures[self.next_texture] = (width, height, format)
        return self.next_texture

    def delete_texture(self, handle):
        self._textures.pop(handle, None)

    def draw(self, name, target, **kwargs):
        self.calls.append((name, target, kwargs))

    def copy(self, source, target, **kwargs):
        self.calls.append(('copy', target, {'source': source}))

    def quad(self, target, shader, params=b'', **kwargs):
        self.calls.append((shader, target, kwargs))

    @property
    def casts(self):
        return sum(name == 'shadow_cast' for name, _, _ in self.calls)


@pytest.fixture
def scene(monkeypatch):
    monkeypatch.setattr(Toggles, 'filters', True)
    monkeypatch.setattr(Toggles, 'filter_brightness', False)
    monkeypatch.setattr(Toggles, 'draw_legacy', False)
    renderer = RecordingRenderer()
    cache = SimpleNamespace(full_mask_tex=1000, gpu=MetalTileBackend(renderer),
                            glow_active=False, glow_tex=None)
    return renderer, cache


def test_unchanged_depth_reuses_cast_but_composites_each_new_scene(scene):
    renderer, cache = scene
    renderer.compose_scene(cache)
    renderer.compose_scene(cache)
    assert renderer.casts == 1
    assert sum(name == 'shadow_composite' for name, _, _ in renderer.calls) == 2
    cache.gpu.full_mask_generation += 1
    renderer.compose_scene(cache)
    assert renderer.casts == 2
    cache.full_mask_tex += 1
    renderer.compose_scene(cache)
    assert renderer.casts == 3
    renderer._textures[renderer.scene_framebuffer] = (128, 96, 'rgba16f')
    renderer.compose_scene(cache)
    assert renderer.casts == 4


@pytest.mark.parametrize('setting', [
    'shadow_height_scale', 'shadow_blur_scale', 'shadow_blur_exponent',
    'shadow_blur_samples', 'shadow_hit_strength', 'shadow_hit_falloff',
    'shadow_strength', 'shadow_downscale', 'shadow_light_dir',
])
def test_cast_settings_invalidate_reuse(scene, monkeypatch, setting):
    renderer, cache = scene
    renderer.compose_scene(cache)
    value = getattr(Toggles, setting)
    value = tuple(v + .25 for v in value) if setting == 'shadow_light_dir' else value + 1
    monkeypatch.setattr(Toggles, setting, value)
    renderer.compose_scene(cache)
    renderer.compose_scene(cache)
    assert renderer.casts == 2


def test_mask_margin_clear_and_cleanup_invalidate_shadow(scene):
    renderer, _ = scene
    cache = TileCacheMasked(gpu=MetalTileBackend(renderer))
    cache.gpu.begin_masks(cache, 64, 48)
    renderer.compose_scene(cache)
    assert cache.clear_mask_outside(2, 2, 62, 46)
    renderer.compose_scene(cache)
    assert renderer.casts == 2
    cache.cleanup()
    cache.gpu.begin_masks(cache, 64, 48)
    renderer.compose_scene(cache)
    assert renderer.casts == 3
