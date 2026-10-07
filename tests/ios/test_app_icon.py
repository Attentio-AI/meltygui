"""Launcher discovery, HDR preservation and native-build invalidation."""
import json
import plistlib
import struct
import zlib

import numpy as np
from PIL import Image
import pytest

from meltygui.image_load import pq_decode, read_png16
from meltygui.platforms.ios import app_icon, devices


def launcher(root, icon='icon.png', extra=''):
    path = root / f'{root.name}.desktop'
    path.write_text(f'[Desktop Entry]\nType=Application\nIcon=./{icon}\n{extra}')
    return path


def hdr_png(path, pixels=None):
    # Independent fixture: 1000-nit PQ red with half alpha, without a background.
    if pixels is None:
        pixels = np.full((4, 8, 4), [49271, 0, 0, 32768], dtype='>u2')
    pixels = pixels.astype('>u2')
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    path.write_bytes(b'\x89PNG\r\n\x1a\n'
                     + chunk(b'IHDR', struct.pack('>IIBBBBB', pixels.shape[1], pixels.shape[0], 16, 6, 0, 0, 0))
                     + chunk(b'cICP', bytes([9, 16, 0, 1]))
                     + chunk(b'IDAT', zlib.compress(b''.join(b'\0' + row.tobytes() for row in pixels)))
                     + chunk(b'IEND', b''))


def test_hdr_is_preferred_and_preserved_in_primary_icon(tmp_path, icon_project):
    Image.new('RGB', (4, 4), 'blue').save(tmp_path / 'icon.png')
    hdr_png(tmp_path / 'hdr.png')
    launcher(tmp_path, extra='X-HDR-Icon=./hdr.png\n')
    build = tmp_path / 'build'
    project = icon_project(build)
    app_icon.sync_project_icon(tmp_path, build)
    info = plistlib.loads((build / 'app-icon/Info.plist').read_bytes())
    assert 'AppIcon60x60' in info['CFBundleIcons']['CFBundlePrimaryIcon']['CFBundleIconFiles']
    assert 'AppIcon83.5x83.5' in info['CFBundleIcons~ipad']['CFBundlePrimaryIcon']['CFBundleIconFiles']
    rgb, alpha, cicp = read_png16(build / 'app-icon/AppIcon60x60@3x.png')
    assert rgb.shape == (180, 180, 3) and alpha.shape == (180, 180)
    assert cicp == bytes([9, 16, 0, 1])
    nits = pq_decode(rgb / 65535)
    assert nits[90, 90, 0] == pytest.approx(1000, abs=1)
    assert nits[90, 90, 1] == pytest.approx(0, abs=1)
    assert alpha[90, 90] == pytest.approx(32768, abs=1)
    assert alpha[0, 0] == 0
    objects = plistlib.loads(project.read_bytes())['objects']
    for key in ('debug', 'release'):
        settings = objects[key]['buildSettings']
        assert settings['COMPRESS_PNG_FILES'] == 'NO'
        assert settings['DEVELOPMENT_TEAM'] == 'KEEP'
        assert settings['INFOPLIST_FILE'] == str(build / 'app-icon/Info.plist')
    phase, = [obj for obj in objects.values() if obj['isa'] == 'PBXResourcesBuildPhase']
    assert len(phase['files']) == 14


def test_noop_source_edits_and_icon_replacement(tmp_path, monkeypatch, icon_project):
    Image.new('RGB', (120, 60), 'red').save(tmp_path / 'icon.png')
    desktop = launcher(tmp_path)
    build = tmp_path / 'build'
    icon_project(build)
    monkeypatch.setattr(devices, 'xcode_environment', lambda: {})
    app_icon.sync_project_icon(tmp_path, build)
    before = devices.native_inputs(build, {})
    (tmp_path / 'new_source.py').write_text('pass\n')
    app_icon.sync_project_icon(tmp_path, build)
    assert devices.native_inputs(build, {}) == before
    icon = build / 'app-icon/AppIcon60x60@3x.png'
    with Image.open(icon) as image:
        assert image.mode == 'RGBA' and image.getpixel((90, 90)) == (255, 0, 0, 255)
        assert image.getpixel((0, 0)) == (0, 0, 0, 0)
    Image.new('RGB', (120, 60), 'blue').save(tmp_path / 'icon.png')
    app_icon.sync_project_icon(tmp_path, build)
    assert devices.native_inputs(build, {}) != before
    with Image.open(icon) as image:
        assert image.getpixel((90, 90)) == (0, 0, 255, 255)
    desktop.unlink()
    app_icon.sync_project_icon(tmp_path, build)
    assert not icon.exists()
    assert 'CFBundleIcons' not in plistlib.loads((build / 'app-icon/Info.plist').read_bytes())


def test_new_launcher_and_deleted_output_are_detected(tmp_path, icon_project):
    build = tmp_path / 'build'
    icon_project(build)
    app_icon.sync_project_icon(tmp_path, build)
    Image.new('RGB', (30, 30), 'green').save(tmp_path / 'icon.png')
    launcher(tmp_path)
    app_icon.sync_project_icon(tmp_path, build)
    output = build / 'app-icon/AppIcon60x60@3x.png'
    output.unlink()
    app_icon.sync_project_icon(tmp_path, build)
    assert output.is_file()


def test_declared_hdr_never_silently_degrades_to_sdr(tmp_path):
    Image.new('RGB', (8, 8), 'red').save(tmp_path / 'icon.png')
    launcher(tmp_path, extra='X-HDR-Icon=./missing.png\n')
    with pytest.raises(ValueError, match='could not be resolved'):
        app_icon.prepare_icon(tmp_path, tmp_path / 'build')
    launcher(tmp_path, extra='X-HDR-Icon=./icon.png\n')
    with pytest.raises(ValueError, match='tagged 16-bit'):
        app_icon.prepare_icon(tmp_path, tmp_path / 'build')


def test_renamed_checkout_uses_launcher_exec_to_rebase_artwork(tmp_path):
    (tmp_path / 'launch-app').write_text('#!/bin/sh\n')
    (tmp_path / 'art').mkdir()
    hdr_png(tmp_path / 'art/hdr.png')
    (tmp_path / 'app.desktop').write_text('[Desktop Entry]\nType=Application\n'
        'Exec=/old/project/launch-app %F\nIcon=/old/project/art/sdr.png\n'
        'X-HDR-Icon=/old/project/art/hdr.png\n')
    app_icon.prepare_icon(tmp_path, tmp_path / 'build')
    receipt = json.loads((tmp_path / 'build/app-icon.json').read_text())
    assert receipt['source'] == str(tmp_path / 'art/hdr.png') and receipt['hdr']


def test_transparent_rgb_does_not_bleed_into_hdr_edges(tmp_path):
    nits = np.zeros((4, 4, 3), dtype=np.float32)
    nits[:, :2, 0] = 1000
    nits[:, 2:, 1] = 10000  # Invisible green must not pollute the red edge.
    alpha = np.zeros((4, 4), dtype=np.uint16)
    alpha[:, :2] = 65535
    path = tmp_path / 'edge.png'
    app_icon.write_hdr_icon(path, nits, 16, bytes([9, 16, 0, 1]), alpha)
    rgb, output_alpha, _ = read_png16(path)
    decoded = pq_decode(rgb / 65535)
    visible = output_alpha > 0
    assert decoded[..., 1][visible].max() < .01
    assert decoded[..., 0][visible].max() == pytest.approx(1000, abs=2)
    assert output_alpha.min() == 0 and output_alpha.max() == 65535


@pytest.mark.parametrize('inset, expected', [(0, .8), (5, .8 / .9), (10, 1), (25, 1)])
def test_auto_margin_counts_existing_padding(inset, expected):
    alpha = np.zeros((100, 100), dtype=np.uint16)
    alpha[inset:100 - inset, inset:100 - inset] = 32768
    assert app_icon.foreground_scale(alpha) == pytest.approx(expected)


def test_margin_estimate_ignores_faint_tails_and_specks():
    alpha = np.full((100, 100), 200, dtype=np.uint16)
    alpha[15:85, 15:85] = 32768
    alpha[0, 0] = 65535
    assert app_icon.foreground_scale(alpha) == 1
    assert app_icon.foreground_scale(np.zeros((10, 20), dtype=np.uint8)) == 1


def test_margin_respects_rectangular_and_off_center_compositions():
    alpha = np.zeros((50, 100), dtype=np.uint8)
    alpha[:, 10:90] = 255
    assert app_icon.foreground_scale(alpha) == 1  # Center-fit supplies vertical padding.
    alpha[:, 2:70] = 255
    assert app_icon.foreground_scale(alpha) == pytest.approx(.4 / .48)


@pytest.mark.parametrize('hdr', [False, True])
def test_margin_toggle_changes_geometry_and_invalidates_cache(tmp_path, icon_project, monkeypatch, hdr):
    # Five percent supplied by the source; generation must top this up, not
    # blindly add another ten percent. Compare actual decoded alpha bounds.
    if hdr:
        pixels = np.full((100, 100, 4), [49271, 0, 0, 0], dtype=np.uint16)
        pixels[5:95, 5:95, 3] = 65535
        hdr_png(tmp_path / 'icon.png', pixels)
    else:
        pixels = np.full((100, 100, 4), [255, 0, 0, 0], dtype=np.uint8)
        pixels[5:95, 5:95, 3] = 255
        Image.fromarray(pixels).save(tmp_path / 'icon.png')
    launcher(tmp_path)
    build = tmp_path / 'build'
    icon_project(build)
    monkeypatch.setattr(devices, 'xcode_environment', lambda: {})
    before = None
    for enabled, inset in ((True, 18), (False, 9), (True, 18)):
        monkeypatch.setattr(app_icon.Toggles.Mobile, 'icon_auto_margin', enabled)
        app_icon.sync_project_icon(tmp_path, build)
        current = devices.native_inputs(build, {})
        assert current != before
        app_icon.sync_project_icon(tmp_path, build)
        assert devices.native_inputs(build, {}) == current
        before = current
        path = build / 'app-icon/AppIcon60x60@3x.png'
        if hdr:
            rgb, alpha, cicp = read_png16(path)
            assert cicp == bytes([9, 16, 0, 1])
            assert pq_decode(rgb[90, 90, 0] / 65535) == pytest.approx(1000, abs=1)
        else:
            with Image.open(path) as image:
                alpha = np.asarray(image.getchannel('A'))
                assert image.getpixel((90, 90)) == (255, 0, 0, 255)
        ys, xs = np.nonzero(alpha > alpha.max() / 2)
        assert (xs.min(), ys.min()) == pytest.approx((inset, inset), abs=1)
        assert (xs.max(), ys.max()) == pytest.approx((179 - inset, 179 - inset), abs=1)
        assert alpha[0, 0] == 0
