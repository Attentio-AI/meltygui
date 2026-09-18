"""Palette values own lazy texture IDs without a render-host service."""
import gc
import inspect
import operator
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from meltygui.core.graphics.gl_state import GLState, current_context
from meltygui.core.graphics.lut_core import get_luts, inject_luts
from meltygui.core.melty import Melty
from meltygui.model.lut_model import Lut, LutPalette, LutTexture, make_luts
from meltygui.view.lut_view import draw_lut


class Consumer:
    def __init__(self):
        self.changes = []

    def invalidate_up(self, **kwargs):
        self.changes.append(kwargs)


def test_default_palettes_are_independent_python_values():
    first, second = make_luts(), make_luts()
    first['jet'][0] = 99
    assert second['jet'][0] != 99
    assert max(second['hot']) <= 1 and max(second['hot_hdr']) > 1
    assert all(len(colors) % 3 == 0 for colors in second.values())


def test_picker_uses_supplied_names_and_returns_lut_type(monkeypatch):
    choose = Mock(return_value=(True, 'edited'))
    monkeypatch.setattr('meltygui.view.dropdown_view.draw_dropdown', choose)
    changed, value = inspect.unwrap(draw_lut)(Lut('current'), luts={'current': [], 'edited': []})
    assert choose.call_args.kwargs['collection'] == {'current': 'current', 'edited': 'edited'}
    assert changed and value == 'edited' and type(value) is Lut


def test_palette_and_proxies_are_lazy_and_create_no_host(monkeypatch):
    monkeypatch.setattr(Melty, 'luts', None)
    before = dict(Melty.render_hosts)
    palette = get_luts()
    texture = palette.texture('jet')
    assert isinstance(palette, dict) and isinstance(palette['jet'], list)
    assert palette.texture('jet') is texture
    assert not texture._states
    assert repr(texture).startswith('LutTexture(')
    assert texture != None  # Unsupported comparisons must not require GL.
    assert Melty.render_hosts == before


def test_injected_palette_edits_notify_consumers_and_honor_overrides(monkeypatch):
    palette = LutPalette({'jet': [1., 0., 0., 1., 0., 0.]})
    supplied = LutPalette({'custom': [0., 1., 0., 0., 1., 0.]})
    monkeypatch.setattr(Melty, 'luts', palette)
    consumer = Consumer()
    kwargs = {}
    inject_luts(consumer, kwargs)
    inject_luts(consumer, kwargs)  # Repeated cached calls do not duplicate listeners.
    assert kwargs['luts'] is palette
    palette['jet'][:] = [0., 0., 1., 0., 0., 1.]
    assert consumer.changes == [{'max_depth': 8, 'force': True}]
    explicit = {'luts': supplied}
    inject_luts(consumer, explicit)
    supplied.update(custom=[1., 1., 0., 1., 1., 0.])
    assert explicit['luts'] is supplied and len(consumer.changes) == 2
    del consumer
    gc.collect()
    palette['jet'][0] = .5
    assert not palette._callbacks


def test_proxy_binds_as_an_integer_and_refreshes_after_edits(gl_context):
    import OpenGL.GL as gl
    palette = LutPalette({'jet': [1., 0., 0., 1., 0., 0.]})
    texture = palette.texture('jet')
    try:
        first = int(texture)
        assert operator.index(texture) == first and texture == first and texture > 0
        gl.glBindTexture(gl.GL_TEXTURE_1D, texture)
        assert int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_1D)) == first
        palette['jet'][:] = [0., 0., 1., 0., 0., 1.]
        second = int(texture)
        assert second != first and palette.texture('jet') is texture
        gl.glBindTexture(gl.GL_TEXTURE_1D, texture)
        actual = gl.glGetTexImage(gl.GL_TEXTURE_1D, 0, gl.GL_RGB, gl.GL_FLOAT)
        np.testing.assert_allclose(np.asarray(actual).reshape(-1), palette['jet'])
        palette['jet'] = [0., 1., 0., 0., 1., 0.]
        gl.glBindTexture(gl.GL_TEXTURE_1D, texture)
        actual = gl.glGetTexImage(gl.GL_TEXTURE_1D, 0, gl.GL_RGB, gl.GL_FLOAT)
        np.testing.assert_allclose(np.asarray(actual).reshape(-1), palette['jet'])
    finally:
        texture.release()
        GLState.flush_deletes()


def test_palette_edit_forces_the_real_consumer_cache_dirty(monkeypatch):
    from meltygui.state.new_core_model import DrawState
    cache = Mock()
    monkeypatch.setattr(Melty, 'cache', cache)
    palette = LutPalette({'jet': [1., 0., 0., 1., 0., 0.]})
    consumer = DrawState()
    consumer._tile_id = 'graph'
    inject_luts(consumer, {'luts': palette})
    palette['jet'][0] = .5
    cache.invalidate_up.assert_called_once_with(
        'graph', frame_delta=0, max_depth=8, note=None, force=True)


def test_existing_allocation_moves_to_proxy_without_upload_or_double_delete(gl_context):
    import OpenGL.GL as gl
    colors = [1., 0., 0., 1., 0., 0.]
    previous = GLState()
    allocation = previous.texture1d('lut_jet', colors, version=hash(tuple(colors)))
    texture = LutTexture(colors)
    try:
        texture.adopt(previous, 'lut_jet')
        assert int(texture) == allocation.texture_id
        assert previous.peek('lut_jet') is None
        previous.release()
        GLState.flush_deletes()
        assert gl.glIsTexture(int(texture))
        replacement = previous.texture1d('replacement', colors, version=hash(tuple(colors)))
        texture.adopt(previous, 'replacement')
        GLState.flush_deletes()
        assert not gl.glIsTexture(allocation.texture_id)
        assert int(texture) == replacement.texture_id
        GLState.release_context(current_context())
        GLState.flush_deletes()
        assert not gl.glIsTexture(replacement.texture_id)
        assert gl.glIsTexture(int(texture))
        assert texture._state().peek('texture') is not replacement
    finally:
        texture.release()
        GLState.flush_deletes()


def test_legacy_palette_host_retires_without_losing_edits_or_allocations(monkeypatch, gl_context):
    from meltygui.core.conversion.render_host import RenderHost
    monkeypatch.setattr(Melty, 'render_hosts', {})
    monkeypatch.setattr(Melty, 'luts', None)
    host = RenderHost(input_value={'jet': [.12345, 0., 0., 1., 0., 0.]}, name='LUTs')
    colors = host.input_value['jet']
    state = GLState()
    allocation = state.texture1d('lut_jet', colors, version=hash(tuple(colors)))
    cache = {'jet': allocation}
    runtime = SimpleNamespace(host=host, resources=SimpleNamespace(
        states={(current_context(), id(host.input_value)): state}, textures=cache))
    monkeypatch.setattr(Melty, 'lut_runtime', runtime, raising=False)
    palette = get_luts()
    try:
        assert palette['jet'] is colors
        assert palette['jet'][0] == .12345
        assert int(palette.texture('jet')) == allocation.texture_id
        assert palette._textures is cache
        assert id(host) not in Melty.render_hosts and not host._registered
        assert Melty.lut_runtime is None
    finally:
        palette.texture('jet').release()
        GLState.flush_deletes()


def test_cuda_palette_uses_same_edited_values(gl_context):
    import torch
    if not torch.cuda.is_available():
        pytest.skip('CUDA unavailable')
    palette = LutPalette({'jet': [1., 0., 0., 1., 0., 0.]})
    texture = palette.texture('jet')
    try:
        first = texture.cuda('cuda:0')
        assert texture.cuda('cuda:0') is first
        palette['jet'][:] = [0., 0., 1., 0., 0., 1.]
        second = texture.cuda('cuda:0')
        assert second is not first
        np.testing.assert_allclose(second.cpu().numpy().reshape(-1), palette['jet'])
        assert texture._state().peek('texture') is None
    finally:
        texture.release()


def _oklab_rows(colors):
    import math
    from meltygui.hdr_color import linear_to_oklab, srgb_to_linear
    rows = np.array(colors).reshape(-1, 3)
    lab = [linear_to_oklab(tuple(srgb_to_linear(float(c)) for c in r)) for r in rows]
    L = np.array([l[0] for l in lab])
    C = np.array([math.hypot(l[1], l[2]) for l in lab])
    H = np.degrees(np.array([math.atan2(l[2], l[1]) for l in lab]))
    return rows, L, C, H


def test_sequential_hdr_scales_stretch_their_sdr_hue_journey():
    """viridis/inferno_hdr climb uniformly in Oklab lightness to the
    16x neutral white like hot_hdr, keep P3 chroma (negative sRGB) through
    the middle, and visit their SDR scale's hues in the same order — the
    hue axis keeps its meaning, the lightness axis gains the stops."""
    from meltygui.model.lut_model import _INFERNO, _VIRIDIS, _poly, sdr_hue_path
    luts = make_luts()
    for name, coeffs in [('viridis', _VIRIDIS), ('inferno', _INFERNO)]:
        rows, L, C, H = _oklab_rows(luts[f'{name}_hdr'])
        assert np.all(np.diff(L) > 0) and abs(L[-1] - 16 ** (1 / 3)) < 1e-3
        assert np.allclose(np.diff(L), np.diff(L)[0], atol=1e-6)
        assert rows.max() > 2.0 and rows.min() < 0.0
        assert C[-1] < 1e-3 and np.allclose(rows[-1], rows[-1][0])
        # the SDR scale's first and last chromatic hues bracket the HDR one
        _, _, sdr_c, sdr_h = _oklab_rows(_bake(_poly(coeffs)))
        chromatic = sdr_h[sdr_c >= 0.03]
        hue_at = sdr_hue_path(_poly(coeffs))       # unwrapped: compare mod 360
        assert abs((np.degrees(hue_at(0.0)) - chromatic[0] + 180) % 360 - 180) < 1
        assert abs((np.degrees(hue_at(1.0)) - chromatic[-1] + 180) % 360 - 180) < 1
        assert abs(H[C > 0.05][0] - chromatic[0]) < 10      # hue moves fast off black
        assert abs(H[C > 0.05][-1] - chromatic[-1]) < 20       # ... and whitens near its last hue
        # no hue seam where the SDR path crosses ±180° (viridis does)
        steps = np.abs(np.diff(H[(C > 0.05)]))
        assert np.all(np.minimum(steps, 360 - steps) < 5)
    hue_at = sdr_hue_path(_poly(_VIRIDIS))
    assert np.degrees(hue_at(0.0)) < -30 and abs(np.degrees(hue_at(1.0)) % 360 - 103) < 3


def _bake(fn, n=256):
    out = []
    for i in range(n):
        out += [min(1.0, max(0.0, float(c))) for c in fn(i / (n - 1))]
    return out


def test_grey_hdr_is_neutral_and_four_stops_longer():
    rows, L, C, _ = _oklab_rows(make_luts()['grey_hdr'])
    assert np.all(C < 1e-6) and np.allclose(rows[:, 0], rows[:, 1])
    assert np.all(np.diff(L) > 0) and abs(L[-1] - 16 ** (1 / 3)) < 1e-3
    assert abs(rows[-1].mean() ** 2.4 / 16.0 - 1.0) < 0.05 or rows[-1].mean() > 3.0


def test_coolwarm_hdr_keeps_sign_by_hue_and_magnitude_by_brightness():
    """Zero is coolwarm's pale neutral; each arm brightens uniformly into
    the headroom, blue below, red above, and the ends stay coloured (the
    arms stop short of the box's white pinch)."""
    from meltygui.model.lut_model import _coolwarm, _sdr_hue
    rows, L, C, H = _oklab_rows(make_luts()['coolwarm_hdr'])
    n = len(rows)
    lo, hi = n // 2 - 1, n // 2
    assert C[lo] < 0.01 and C[hi] < 0.01 and abs(L[hi] - 0.896) < 0.01
    neg, pos = L[:hi][::-1], L[hi:]
    assert np.all(np.diff(neg) > 0) and np.all(np.diff(pos) > 0)
    assert np.allclose(neg, pos, atol=2e-3)
    assert L[0] > 1.9 and L[-1] > 1.9                       # about 8x white
    assert C[0] > 0.25 and C[-1] > 0.25                     # ends still say which sign
    blue, red = np.degrees(_sdr_hue(_coolwarm(0.0))), np.degrees(_sdr_hue(_coolwarm(1.0)))
    assert np.allclose(H[C > 0.05][: n // 4], blue, atol=0.5)
    assert np.allclose(H[C > 0.05][-n // 4:], red, atol=0.5)
    assert rows.max() > 2.0 and rows.min() < 0.0
