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


def test_hdr_content_light_metadata_is_recognizable_without_changing_color(tmp_path):
    path = tmp_path / 'icon.png'
    nits = np.full((8, 8, 3), [1000, 200, 40], dtype=np.float32)
    alpha = np.full((8, 8), 32768, dtype=np.uint16)
    app_icon.write_hdr_icon(path, nits, 8, bytes([9, 16, 0, 1]), alpha)
    data = path.read_bytes()
    chunks = {}
    offset = 8
    while offset < len(data):
        length, = struct.unpack_from('>I', data, offset)
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        crc, = struct.unpack_from('>I', data, offset + 8 + length)
        assert crc == zlib.crc32(kind + body)
        chunks[kind] = body
        offset += length + 12
    # PNG 3 specifies cLLI (uppercase I). The old draft spelling cLLi is
    # ignored by decoders, leaving bright colors without tone-mapping metadata.
    assert b'cLLi' not in chunks
    assert struct.unpack('>II', chunks[b'cLLI']) == (5000076, 5000076)
    assert list(chunks).index(b'cLLI') < list(chunks).index(b'IDAT')
    # Match the project-list icon path for ICC-based Apple image consumers,
    # not just decoders that understand PNG's newer cICP chunk.
    profile_name, compressed = chunks[b'iCCP'].split(b'\0', 1)
    assert profile_name and compressed[0] == 0
    profile = zlib.decompress(compressed[1:])
    assert profile[16:20] == b'RGB ' and profile[36:40] == b'acsp'
    count, = struct.unpack_from('>I', profile, 128)
    tags = {profile[p:p + 4]: struct.unpack_from('>II', profile, p + 4)
            for p in range(132, 132 + count * 12, 12)}
    offset, length = tags[b'cicp']
    assert profile[offset + 8:offset + length] == chunks[b'cICP']
    assert list(chunks).index(b'iCCP') < list(chunks).index(b'IDAT')
    with Image.open(path) as image:
        assert image.info['icc_profile'] == profile
    rgb, output_alpha, cicp = read_png16(path)
    assert cicp == bytes([9, 16, 0, 1])
    np.testing.assert_allclose(pq_decode(rgb / 65535), nits, rtol=.001)
    np.testing.assert_array_equal(output_alpha, alpha)


@pytest.mark.parametrize('peak', [500, 1000, 10000])
def test_hdr_icon_preserves_absolute_brightness_without_normalizing_or_capping(tmp_path, peak):
    # A decoder's default headroom must not change the artwork's exposure or
    # clip highlights. Preserve channels and shading above and below 1000 nits.
    nits = np.full((8, 8, 3), [peak, peak / 5, peak / 25], dtype=np.float32)
    nits[:4] *= .25  # Preserve the relationship between shading and highlights.
    alpha = np.full((8, 8), 32768, dtype=np.uint16)
    path = tmp_path / 'icon.png'
    app_icon.write_hdr_icon(path, nits, 8, bytes([9, 16, 0, 1]), alpha)
    rgb, output_alpha, cicp = read_png16(path)
    output = pq_decode(rgb / 65535)
    np.testing.assert_allclose(output, nits, rtol=.001)
    assert output.max() > 203  # Retain HDR, not an SDR fallback.
    np.testing.assert_array_equal(output_alpha, alpha)
    assert cicp == bytes([9, 16, 0, 1])


def test_home_screen_restores_saturation_with_orange_and_purple_hues():
    # Bright colored bodies, darker shading and a far brighter neutral glint.
    p3 = np.array([[[1800, 280, 32], [72, 75, 5200], [13000, 13000, 13000]],
                   [[450, 70, 8], [18, 18.75, 1300], [3250, 3250, 3250]]], dtype=float)
    source = p3 @ app_icon.P3_TO_REC2020.T
    result = (app_icon.home_screen_nits(source) @ app_icon.REC2020_TO_P3.T) / 203
    assert result.max() <= 1.00001 and result.min() >= -1e-6
    orange, purple, white = result[0]
    assert orange[0] > .9 and orange[1] < .4 and orange[2] < .05
    assert .1 < purple[0] < .3 and purple[1] < .05 and purple[2] > .85
    # Recorded pre-LUT palette: saturation survives the requested hue shifts.
    reference = np.array([[.8675621999313645, .45266313086364374, .020811785456523745],
                          [.04539973006407065, .05398987393086033, .964446098474158]])
    before = app_icon.p3_to_oklab(reference)
    after = app_icon.p3_to_oklab(result[0, :2])
    before_chroma = np.linalg.norm(before[:, 1:], axis=-1, keepdims=True)
    after_chroma = np.linalg.norm(after[:, 1:], axis=-1, keepdims=True)
    hue = lambda lab: np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360
    shift = (hue(after) - hue(before) + 180) % 360 - 180
    assert -19 < shift[0] < -17  # Gold moves toward orange.
    assert 20 < shift[1] < 25  # Blue moves toward purple.
    assert (after_chroma[:, 0] / after[:, 0] > before_chroma[:, 0] / before[:, 0]).all()
    assert (after[:, 0] <= before[:, 0] + 1e-6).all()
    assert (after[:, 0] > before[:, 0] * .9).all()
    np.testing.assert_allclose(white, [1, 1, 1], atol=1e-6)
    assert (app_icon.p3_to_oklab(result[1])[:, 0] < app_icon.p3_to_oklab(result[0])[:, 0]).all()


def test_home_screen_hue_lut_preserves_chroma_lightness_neutrals_and_other_hues():
    hues = np.array([0, 20, 80, 160, 200, 270, 350, 360.])
    radians = np.radians(hues)
    lab = np.column_stack([np.full(len(hues), .6), .2 * np.cos(radians), .2 * np.sin(radians)])
    result = app_icon.home_screen_hue(lab)
    np.testing.assert_array_equal(result[:, 0], lab[:, 0])
    np.testing.assert_allclose(np.linalg.norm(result[:, 1:], axis=-1), .2)
    np.testing.assert_allclose(result[[0, 1, 3, 4, 6, 7]], lab[[0, 1, 3, 4, 6, 7]], atol=1e-12)
    neutral = np.array([[0., 0, 0], [.5, 0, 0], [1., 0, 0]])
    np.testing.assert_array_equal(app_icon.home_screen_hue(neutral), neutral)
    # The circular seam has no discontinuity.
    np.testing.assert_allclose(result[0], result[-1], atol=1e-12)


def test_oklab_basis_matches_reference_srgb_red_and_round_trips_p3():
    red_p3 = np.array([1., 0, 0]) @ np.linalg.inv(app_icon.PRIMARIES_TO_SRGB[12]).T
    lab = app_icon.p3_to_oklab(red_p3)
    np.testing.assert_allclose(lab, [.6279553606, .2248630611, .1258462985], atol=1e-9)
    np.testing.assert_allclose(app_icon.oklab_to_p3(lab), red_p3, atol=1e-9)


def test_home_screen_gamut_mapping_keeps_hue_and_only_limits_required_chroma():
    source = np.array([[[10000., 0, 0], [0, 10000, 0], [0, 0, 10000],
                        [10000, 10000, 0], [0, 10000, 10000]]])
    p3 = source @ app_icon.REC2020_TO_P3.T / 203
    p3 /= p3.max(axis=-1, keepdims=True)
    before = app_icon.p3_to_oklab(p3)
    output = app_icon.fit_home_screen_gamut(p3)
    after = app_icon.p3_to_oklab(output)
    assert output.min() >= -1e-7 and output.max() <= 1.000001
    before_chroma = np.linalg.norm(before[..., 1:], axis=-1, keepdims=True)
    after_chroma = np.linalg.norm(after[..., 1:], axis=-1, keepdims=True)
    np.testing.assert_allclose(after[..., 1:] / after_chroma,
                               before[..., 1:] / before_chroma, atol=1e-6)
    # Gamut fitting may reduce relative chroma, but never boosts or rotates it.
    assert (after_chroma[..., 0] / after[..., 0]
            <= before_chroma[..., 0] / before[..., 0] + 1e-6).all()


def test_home_screen_ignores_invisible_rgb_and_keeps_in_range_artwork():
    p3 = np.array([[[100, 40, 10], [10000, 10000, 10000]]], dtype=float)
    source = p3 @ app_icon.P3_TO_REC2020.T
    alpha = np.array([[65535, 0]], dtype=np.uint16)
    result = app_icon.home_screen_nits(source, alpha) @ app_icon.REC2020_TO_P3.T
    np.testing.assert_allclose(result[0, 0], p3[0, 0], atol=1e-6)


def test_hdr_is_preferred_and_fitted_to_home_screen_in_primary_icon(tmp_path, icon_project):
    (tmp_path / 'pyproject.toml').write_text('[tool.hub.ios]\nicon = "icon.png"\n')
    Image.new('RGB', (4, 4), 'blue').save(tmp_path / 'icon.png')
    hdr_png(tmp_path / 'hdr.png')
    launcher(tmp_path, icon='./hdr.png')
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
    p3 = nits @ app_icon.REC2020_TO_P3.T
    assert p3[90, 90, 0] == pytest.approx(203, abs=.2)
    assert p3[90, 90, 0] > 20 * max(p3[90, 90, 1:])
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


@pytest.mark.parametrize('section', ['tool.hub.ios', 'tool.melty.app'])
def test_manifest_artwork_preserves_hdr_and_tracks_replacement(tmp_path, section):
    (tmp_path / 'pyproject.toml').write_text(f'[{section}]\nicon = "hdr.png"\n')
    hdr_png(tmp_path / 'hdr.png')
    build = tmp_path / 'build'
    app_icon.prepare_icon(tmp_path, build)
    output = build / 'app-icon/AppIcon60x60@3x.png'
    rgb, alpha, cicp = read_png16(output)
    assert cicp == bytes([9, 16, 0, 1])
    p3 = pq_decode(rgb / 65535) @ app_icon.REC2020_TO_P3.T
    assert p3[90, 90, 0] == pytest.approx(203, abs=.2)
    assert alpha[90, 90] == pytest.approx(32768, abs=1)
    before = output.stat().st_mtime_ns
    app_icon.prepare_icon(tmp_path, build)
    assert output.stat().st_mtime_ns == before
    Image.new('RGBA', (4, 4), 'green').save(tmp_path / 'sdr.png')
    (tmp_path / 'pyproject.toml').write_text(f'[{section}]\nicon = "sdr.png"\n')
    app_icon.prepare_icon(tmp_path, build)
    assert Image.open(output).getpixel((90, 90)) == (0, 128, 0, 255)
    (tmp_path / 'sdr.png').unlink()
    with pytest.raises(ValueError, match='could not be resolved'):
        app_icon.prepare_icon(tmp_path, build)


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
    assert icon.exists()
    assert 'CFBundleIcons' in plistlib.loads((build / 'app-icon/Info.plist').read_bytes())


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


def test_icon_requires_a_resolvable_source_and_detects_sdr(tmp_path):
    Image.new('RGB', (8, 8), 'red').save(tmp_path / 'icon.png')
    launcher(tmp_path, icon='./missing.png')
    with pytest.raises(ValueError, match='could not be resolved'):
        app_icon.prepare_icon(tmp_path, tmp_path / 'build')
    launcher(tmp_path, icon='./icon.png')
    app_icon.prepare_icon(tmp_path, tmp_path / 'build')
    receipt = json.loads((tmp_path / 'build/app-icon.json').read_text())
    assert not receipt['hdr']


def test_renamed_checkout_uses_launcher_exec_to_rebase_artwork(tmp_path):
    (tmp_path / 'launch-app').write_text('#!/bin/sh\n')
    (tmp_path / 'art').mkdir()
    hdr_png(tmp_path / 'art/hdr.png')
    (tmp_path / 'app.desktop').write_text('[Desktop Entry]\nType=Application\n'
        'Exec=/old/project/launch-app %F\nIcon=/old/project/art/hdr.png\n')
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
            p3 = pq_decode(rgb[90, 90] / 65535) @ app_icon.REC2020_TO_P3.T
            assert p3[0] == pytest.approx(203, abs=.2)
        else:
            with Image.open(path) as image:
                alpha = np.asarray(image.getchannel('A'))
                assert image.getpixel((90, 90)) == (255, 0, 0, 255)
        ys, xs = np.nonzero(alpha > alpha.max() / 2)
        assert (xs.min(), ys.min()) == pytest.approx((inset, inset), abs=1)
        assert (xs.max(), ys.max()) == pytest.approx((179 - inset, 179 - inset), abs=1)
        assert alpha[0, 0] == 0


def test_fallback_name_tint_and_cache(tmp_path, monkeypatch):
    import melty_meta
    monkeypatch.setenv('MELTY_GLOBAL', str(tmp_path / 'global.json'))
    (tmp_path / 'pyproject.toml').write_text('[tool.melty.app]\nname = "My Great App"\n')
    melty_meta.set_tint(tmp_path, (.8, .2, .1, 1))
    build = tmp_path / 'build'
    destination, icons = app_icon.prepare_icon(tmp_path, build)
    assert len(icons) == 14
    icon = destination / 'AppIcon60x60@3x.png'
    assert Image.open(icon).getpixel((0, 0)) == (204, 51, 26, 255)
    before = icon.stat().st_mtime_ns
    app_icon.prepare_icon(tmp_path, build)
    assert icon.stat().st_mtime_ns == before
    melty_meta.set_tint(tmp_path, (.1, .2, .8, 1))
    app_icon.prepare_icon(tmp_path, build)
    assert Image.open(icon).getpixel((0, 0)) == (26, 51, 204, 255)


def test_fallback_wrap_and_repository_initial():
    from PIL import ImageDraw
    draw = ImageDraw.Draw(Image.new('RGB', (1024, 1024)))
    label, font = app_icon.fallback_label('My Great App', 'repository', draw)
    assert label.replace('\n', ' ') == 'My Great App'
    assert '\n' in label
    label, font = app_icon.fallback_label('UnreasonablyLongApplicationName' * 10, 'repository', draw)
    assert label == 'R'


def test_generated_host_uses_original_repository(tmp_path, monkeypatch):
    import melty_meta
    monkeypatch.setenv('MELTY_GLOBAL', str(tmp_path / 'global.json'))
    repository = tmp_path / 'original-repo'
    repository.mkdir()
    host = tmp_path / 'host'
    host.mkdir()
    (host / 'pyproject.toml').write_text('[tool.melty.app]\nname = "Original App"\nicon_root = ' + json.dumps(str(repository)))
    melty_meta.set_tint(repository, (.2, .3, 1.4, 1))           # HDR clamps to 1
    assert app_icon.fallback_identity(host) == ('Original App', 'original-repo', [.2, .3, 1])
    melty_meta.set_tint(repository, None)
    assert app_icon.fallback_identity(host)[2] is None
