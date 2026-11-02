"""HDR project artwork reaches GPU uploads without an SDR preview conversion."""
import struct
import zlib

import numpy as np
from PIL import Image
import pytest

from meltygui.image_load import PRIMARIES_TO_SRGB, pq_decode
from meltygui.model.folder_icon_model import decode_icon, load_folder_icon


def pq_png(path, rgba, depth=16):
    rgba = np.asarray(rgba, dtype='>u2' if depth == 16 else np.uint8)
    height, width, _ = rgba.shape
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    path.write_bytes(b'\x89PNG\r\n\x1a\n'
                     + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth, 6, 0, 0, 0))
                     + chunk(b'cICP', bytes([9, 16, 0, 1]))
                     + chunk(b'IDAT', zlib.compress(b''.join(b'\0' + row.tobytes() for row in rgba)))
                     + chunk(b'IEND', b''))
    return rgba.astype(float)


@pytest.mark.parametrize('depth', [8, 16])
def test_pq_png_keeps_hdr_signed_gamut_and_straight_alpha(tmp_path, depth):
    maximum = 2**depth - 1
    codes = np.array([[[.75, 0, 0, .5], [0, .75, 0, 1]]]) * maximum
    path = tmp_path / 'hdr.png'
    encoded = pq_png(path, codes, depth)
    pixels = decode_icon(path)
    assert pixels.format == 'rgba16f'
    actual = np.frombuffer(pixels.data, np.float16).reshape(1, 2, 4).astype(float)
    expected = pq_decode(encoded[..., :3] / maximum) @ PRIMARIES_TO_SRGB[9].T / 203
    np.testing.assert_allclose(actual[..., :3], expected, rtol=.001, atol=.001)
    np.testing.assert_allclose(actual[..., 3], encoded[..., 3] / maximum, atol=.001)
    assert actual[..., :3].max() > 5 and actual[..., :3].min() < 0


def test_resize_filters_linear_light_and_excludes_invisible_hdr_rgb(tmp_path):
    path = tmp_path / 'edge.png'
    encoded = pq_png(path, [[[49271, 0, 0, 65535], [0, 65535, 0, 0]],
                            [[49271, 0, 0, 65535], [0, 65535, 0, 0]]])
    pixels = decode_icon(path, size=1)
    actual = np.frombuffer(pixels.data, np.float16).astype(float)
    expected = pq_decode(encoded[0, 0, :3] / 65535) @ PRIMARIES_TO_SRGB[9].T / 203
    np.testing.assert_allclose(actual[:3], expected, rtol=.001, atol=.001)
    assert actual[3] == .5


def test_icon_field_detects_hdr_and_sdr_from_image_metadata(tmp_path):
    sdr, hdr = tmp_path / 'sdr.png', tmp_path / 'hdr.png'
    Image.new('RGBA', (2, 2), 'red').save(sdr)
    launcher = tmp_path / 'app.desktop'
    launcher.write_text('[Desktop Entry]\nType=Application\nIcon=./hdr.png\n')
    pq_png(hdr, [[[49271, 0, 0, 65535]]])
    pixels, dependencies = load_folder_icon(tmp_path)
    assert pixels.path == hdr and pixels.format == 'rgba16f'
    assert hdr in dependencies
    hdr.write_bytes(b'broken')
    assert load_folder_icon(tmp_path)[0] is None
    hdr.unlink()
    assert load_folder_icon(tmp_path)[0] is None
    launcher.write_text('[Desktop Entry]\nType=Application\nIcon=./sdr.png\n')
    assert load_folder_icon(tmp_path)[0].path == sdr
    assert load_folder_icon(tmp_path)[0].format == 'rgba8srgb'


def test_ios_hdr_decoder_does_not_attempt_to_spawn_a_compiler(monkeypatch):
    from meltygui import image_load
    monkeypatch.setattr(image_load, '_native', None)
    monkeypatch.setattr(image_load.sys, 'platform', 'ios')
    def forbidden(*args, **kwargs):
        pytest.fail('iOS attempted to spawn a compiler')
    monkeypatch.setattr(image_load.subprocess, 'run', forbidden)
    assert image_load._native_unfilter() is None


def test_ios_packager_also_detects_pq8_through_the_standard_icon_field(tmp_path):
    import json
    from meltygui.platforms.ios.app_icon import prepare_icon
    pq_png(tmp_path / 'icon.png', [[[192, 0, 0, 255]]], depth=8)
    (tmp_path / 'app.desktop').write_text('[Desktop Entry]\nType=Application\nIcon=./icon.png\n')
    prepare_icon(tmp_path, tmp_path / 'build')
    receipt = json.loads((tmp_path / 'build/app-icon.json').read_text())
    assert receipt['hdr'] and receipt['source'] == str(tmp_path / 'icon.png')
