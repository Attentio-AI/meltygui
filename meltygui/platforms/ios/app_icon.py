"""Launcher artwork for iOS, preserving PQ HDR instead of passing it through actool."""
import hashlib
import json
import os
import pickle
import tomllib
from pathlib import Path
import plistlib
import shutil
import struct
import zlib

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from meltygui.core.runtime.toggles import Toggles
from meltygui.image_load import PRIMARIES_TO_SRGB, png_pq_cicp, pq_decode, read_png16
from meltygui.model.folder_icon_model import Probe, decode_icon, folder_icon_candidates, stamp


# Point sizes and scales used for notifications, Settings, Spotlight and Home.
ICON_SIZES = {'20x20': (1, 2, 3), '29x29': (1, 2, 3), '40x40': (1, 2, 3),
              '60x60': (2, 3), '76x76': (1, 2), '83.5x83.5': (2,)}
INFO_TEMPLATE = Path(__file__).parent / 'Host/Info.plist'
PQ_ICC_FILE = Path(__file__).with_name('rec2020-pq.icc')
REC2020_TO_P3 = np.linalg.solve(PRIMARIES_TO_SRGB[12], PRIMARIES_TO_SRGB[9])
P3_TO_REC2020 = np.linalg.inv(REC2020_TO_P3)


def home_screen_nits(nits, alpha=None):
    """Fit the artwork to SpringBoard's standard-range Display P3 icon surface.

    The system icon service clips bright PQ channels independently, turning
    blue into lavender and gold into pale yellow. A filmic shoulder retains
    bright midtones without clipping the highlights; chroma compensation keeps
    that compression from lifting saturated blue/gold toward white. This
    affects launcher copies, never the HDR master.
    """
    p3 = np.maximum(nits @ REC2020_TO_P3.T, 0)
    peak = float(p3.max()) if alpha is None else float(
        np.max(p3, where=alpha[..., None] > 0, initial=0))
    if peak > 203:
        # Remove part of the neutral component only in colorful pixels before
        # compression. Neutrals keep equal channels and are never color-tinted.
        maximum = p3.max(axis=-1, keepdims=True)
        minimum = p3.min(axis=-1, keepdims=True)
        saturation = np.divide(maximum - minimum, maximum,
                               out=np.zeros_like(maximum), where=maximum > 0)
        colorful = p3 - .8 * saturation * minimum

        def shoulder(value):
            # Hable's filmic curve, with a 100-nit exposure reference. This
            # lifts the icon's body while rolling its much brighter glints off.
            x = value / 100
            return ((x * (.15 * x + .10 * .50) + .20 * .02)
                    / (x * (.15 * x + .50) + .20 * .30)) - .02 / .30

        p3 = np.clip(shoulder(colorful) / shoulder(peak), 0, 1) * 203
    return np.maximum(p3 @ P3_TO_REC2020.T, 0)


def foreground_scale(alpha):
    """Top up the visible artwork's inset to 10%, retaining its composition."""
    minimum_margin = .10
    # Estimate bounds from meaningful opacity, ignoring faint shadow tails and
    # at most 0.1% of opacity at either edge (e.g. an isolated stray pixel).
    # This only measures the artwork; all original pixels remain in the output.
    peak = float(alpha.max())
    if peak == 0:
        return 1.0
    weights = np.where(alpha >= peak * .05, alpha, 0).astype(np.float64)
    height, width = alpha.shape
    extent = 0.0
    for projection, length in ((weights.sum(axis=0), width), (weights.sum(axis=1), height)):
        cumulative = projection.cumsum()
        start, end = np.searchsorted(cumulative, cumulative[-1] * np.array([.001, .999]))
        extent = max(extent, length / 2 - start, end + 1 - length / 2)
    # Center-fitting a rectangular source already contributes margin on its
    # shorter axis. Never enlarge artwork or crop away its existing padding.
    return min(1.0, (0.5 - minimum_margin) * max(width, height) / extent)


def icon_dimensions(width, height, size, artwork_scale):
    factor = size * artwork_scale / max(width, height)
    return max(1, round(width * factor)), max(1, round(height * factor))


def write_hdr_icon(path, nits, size, cicp, alpha=None, *, artwork_scale=1.0):
    """Resample premultiplied linear light; emit straight-alpha 16-bit PQ RGBA."""
    if cicp != bytes((9, 16, 0, 1)):
        raise ValueError('HDR app icons require full-range RGB Rec.2020/PQ')
    # Preserve the artwork's absolute brightness. ImageIO's default headroom
    # is not a proven display limit and must not become an exposure adjustment
    # or a 1000-nit cap. Only suppress resampling overshoot beyond the source.
    peak = float(nits.max()) if alpha is None else float(
        np.max(nits, where=alpha[..., None] > 0, initial=0))
    height, width = nits.shape[:2]
    dimensions = icon_dimensions(width, height, size, artwork_scale)
    opacity = np.ones((height, width), dtype=np.float32) if alpha is None else alpha.astype(np.float32) / 65535

    def resize(channel):
        return np.asarray(Image.fromarray(channel.astype(np.float32)).resize(dimensions, Image.Resampling.LANCZOS))

    resized_alpha = resize(opacity)
    premultiplied = np.stack([resize(nits[..., channel] * opacity) for channel in range(3)], axis=-1)
    # Filter premultiplied values to avoid pulling invisible RGB into edges;
    # PNG stores straight alpha, so unpremultiply before PQ encoding.
    resized = np.divide(premultiplied, resized_alpha[..., None], out=np.zeros_like(premultiplied),
                        where=resized_alpha[..., None] > 1e-6)
    resized = np.clip(resized, 0, peak)
    square = np.zeros((size, size, 3), dtype=np.float32)
    square_alpha = np.zeros((size, size), dtype=np.float32)
    x, y = (size - dimensions[0]) // 2, (size - dimensions[1]) // 2
    square[y:y + dimensions[1], x:x + dimensions[0]] = resized
    square_alpha[y:y + dimensions[1], x:x + dimensions[0]] = np.clip(resized_alpha, 0, 1)
    linear = np.clip(square / 10000, 0, 1) ** (2610 / 16384)
    encoded = ((3424 / 4096 + (2413 / 128) * linear) / (1 + (2392 / 128) * linear)) ** (2523 / 32)
    codes = np.round(np.concatenate([encoded, square_alpha[..., None]], axis=-1) * 65535).astype('>u2')

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    rows = b''.join(b'\0' + row.tobytes() for row in codes)
    visible = square * square_alpha[..., None]
    light = struct.pack('>II', round(float(visible.max()) * 10000),
                        round(float(visible.max(axis=-1).mean()) * 10000))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 16, 6, 0, 0, 0))
                     # PNG chunk names are case-sensitive: cLLi is an obsolete
                     # draft spelling, not the standard content-light metadata.
                     + chunk(b'cICP', cicp) + chunk(b'cLLI', light)
                     # Match the project-list PNGs: ICC-based icon decoders also
                     # need the PQ transfer and Rec.2020 primaries, not bare codes.
                     + chunk(b'iCCP', b'HDR RGB\0\0' + zlib.compress(PQ_ICC_FILE.read_bytes()))
                     + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))



class _MetadataReader(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError('File metadata must contain plain values')


def fallback_identity(root):
    """Read the app label and painted repository color, including generated hosts."""
    manifest = root / 'pyproject.toml'
    data = tomllib.loads(manifest.read_text()) if manifest.is_file() else {}
    config = data.get('tool', {}).get('melty', {}).get('app', {})
    repository = Path(config.get('icon_root', root)).resolve()
    name = config.get('name') or data.get('tool', {}).get('hub', {}).get('ios', {}).get('name') or data.get('project', {}).get('name')
    if not name:
        for desktop in sorted(repository.glob('*.desktop')):
            name = Probe().config(desktop).get('Desktop Entry', 'Name', fallback='').strip()
            if name:
                break
    metadata = Path(os.environ.get('MELTY_FILE_META', Path.home() / '.melty/file_meta.pkl'))
    tint = None
    try:
        with metadata.open('rb') as stream:
            entries = _MetadataReader(stream).load().get('entries', {})
        value = entries.get(str(repository), {}).get('tint')
        if isinstance(value, (list, tuple)) and len(value) >= 3 and (len(value) < 4 or value[3]):
            tint = [max(0, min(1, float(channel))) for channel in value[:3]]
    except (OSError, ValueError, TypeError, AttributeError, EOFError, pickle.UnpicklingError):
        pass
    return str(name or repository.name), repository.name, tint


def fallback_label(name, repository, draw):
    """Wrap at spaces into up to three bold, readable lines; keep words intact."""
    font_path = Path(__file__).parents[2] / 'resources/dejavu/DejaVuSans.ttf'
    words = name.split()
    for size in range(220, 95, -8):
        font = ImageFont.truetype(str(font_path), size)
        lines = []
        for word in words:
            if lines and draw.textlength(lines[-1] + ' ' + word, font=font) + 2 * round(size * .025) <= 800:
                lines[-1] += ' ' + word
            else:
                lines.append(word)
        if lines and len(lines) <= 3 and all(draw.textlength(line, font=font) + 2 * round(size * .025) <= 800 for line in lines):
            return '\n'.join(lines), font
    return (repository.strip()[:1] or '?').upper(), ImageFont.truetype(str(font_path), 560)


def fallback_icon(name, repository, tint):
    """An opaque, tinted tile with centered, contrast-safe typography."""
    color = tuple(round(channel * 255) for channel in (tint or (0.22, 0.36, 0.58)))
    image = Image.new('RGBA', (1024, 1024), (*color, 255))
    draw = ImageDraw.Draw(image)
    linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in (v / 255 for v in color)]
    luminance = sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))
    ink = 'black' if luminance > .179 else 'white'
    label, font = fallback_label(name, repository, draw)
    # Embolden the bundled font consistently, including on remote Macs.
    stroke = max(2, round(font.size * .025))
    left, top, right, bottom = draw.multiline_textbbox(
        (0, 0), label, font=font, spacing=20, align='center', stroke_width=stroke)
    draw.multiline_text(((1024 - right - left) / 2, (1024 - bottom - top) / 2),
                        label, font=font, fill=ink, spacing=20, align='center',
                        stroke_width=stroke, stroke_fill=ink)
    return image


def prepare_icon(root, build, *, name=None):
    """Refresh generated icons only when launcher/artwork inputs change."""
    root, build = Path(root).resolve(), Path(build).resolve()
    manifest = root / 'pyproject.toml'
    data = tomllib.loads(manifest.read_text()) if manifest.is_file() else {}
    config = data.get('tool', {}).get('melty', {}).get('app', {})
    artwork_root = Path(config.get('icon_root', root)).resolve()
    destination = build / 'app-icon'
    receipt = build / 'app-icon.json'
    host_config = build / 'host-build.json'
    host_runtime = json.loads(host_config.read_text()).get('host_runtime') if host_config.is_file() else None
    identity = fallback_identity(root)
    if name:
        identity = (str(name), *identity[1:])
    launchers = sorted(path.name for path in artwork_root.glob('*.desktop'))
    try:
        previous = json.loads(receipt.read_text())
    except (OSError, ValueError):
        previous = {}
    if (previous.get('root') == str(root) and previous.get('version') == 11
            and previous.get('host_runtime') == host_runtime
            and previous.get('identity') == list(identity)
            and previous.get('auto_margin') == Toggles.Mobile.icon_auto_margin
            and previous.get('launchers') == launchers
            and all(stamp(Path(path)) == (tuple(value) if value else None)
                    for path, value in previous['dependencies'].items())
            and all(stamp(Path(path)) == tuple(value)
                    for path, value in previous['outputs'].items())):
        return destination, [Path(path) for path in previous['outputs'] if path.endswith('.png')]

    probe = Probe()
    probe.exists(manifest)
    candidates = folder_icon_candidates(artwork_root, probe, 1024, prefer_hdr=True)
    chosen = next(candidates, None)
    source = None
    if chosen is not None:
        desktop, value, source = chosen
        if source is None:
            raise ValueError(f'{desktop}: launcher icon {value!r} could not be resolved')
    else:
        # A launcher can supply an HDR counterpart to a manifest's SDR icon.
        # Without a launcher, still process configured artwork through this
        # pipeline instead of flattening it via xtool/actool or replacing it.
        value = config.get('icon') or data.get('tool', {}).get('hub', {}).get('ios', {}).get('icon')
        if value:
            source = (artwork_root / value).resolve()
            if not probe.exists(source):
                raise ValueError(f'{manifest}: app icon {value!r} could not be resolved')
    probe.exists(INFO_TEMPLATE)
    probe.exists(Path(__file__))
    probe.exists(PQ_ICC_FILE)
    info = plistlib.loads(INFO_TEMPLATE.read_bytes())
    if host_runtime:
        runtime_manifest = Path(host_runtime) / 'runtime.json'
        probe.exists(runtime_manifest)
        info.update(json.loads(runtime_manifest.read_text()).get('info', {}))
    nits = None
    alpha = None
    image = None
    if source is not None:
        hdr = png_pq_cicp(source)
        if hdr:
            if hdr != (16, 9):
                raise ValueError(f'{source}: HDR app icons require a 16-bit Rec.2020/PQ PNG')
            rgb, alpha, cicp = read_png16(source)
            if cicp != bytes([9, 16, 0, 1]):
                raise ValueError(f'{source}: HDR app icons require full-range RGB Rec.2020/PQ')
            nits = home_screen_nits(pq_decode(rgb.astype(np.float64) / 65535), alpha)
        else:
            # A declared HDR icon must never silently fall through an SDR decoder.
            if chosen is not None and probe.config(desktop).get('Desktop Entry', 'X-HDR-Icon', fallback='').strip():
                raise ValueError(f'{source}: X-HDR-Icon must be a tagged 16-bit Rec.2020/PQ PNG')
            pixels = decode_icon(source, size=1024)
            image = Image.frombytes('RGBA', (pixels.width, pixels.height), pixels.data)

    if source is None:
        image = fallback_icon(*identity)

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    outputs = []
    if image is not None or nits is not None:
        artwork_scale = 1.0
        if source is not None and Toggles.Mobile.icon_auto_margin:
            if nits is not None:
                opacity = alpha if alpha is not None else np.ones(nits.shape[:2], dtype=np.uint8)
            else:
                opacity = np.asarray(image.getchannel('A'))
            artwork_scale = foreground_scale(opacity)
        for points, scales in ICON_SIZES.items():
            for scale in scales:
                size = round(float(points.split('x')[0]) * scale)
                suffix = f'@{scale}x' if scale != 1 else ''
                path = destination / f'AppIcon{points}{suffix}.png'
                if nits is not None:
                    write_hdr_icon(path, nits, size, cicp, alpha, artwork_scale=artwork_scale)
                else:
                    dimensions = icon_dimensions(image.width, image.height, size, artwork_scale)
                    resized = image.resize(dimensions, Image.Resampling.LANCZOS)
                    square = Image.new('RGBA', (size, size), (0, 0, 0, 0))
                    square.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2))
                    square.save(path)
                outputs.append(path)
        phone = ['AppIcon20x20', 'AppIcon29x29', 'AppIcon40x40', 'AppIcon60x60']
        tablet = phone + ['AppIcon76x76', 'AppIcon83.5x83.5']
        info['CFBundleIcons'] = {'CFBundlePrimaryIcon': {'CFBundleIconFiles': phone}}
        info['CFBundleIcons~ipad'] = {'CFBundlePrimaryIcon': {'CFBundleIconFiles': tablet}}
    (destination / 'Info.plist').write_bytes(plistlib.dumps(info))
    outputs.append(destination / 'Info.plist')
    receipt.touch(exist_ok=True)
    receipt.write_text(json.dumps(dict(version=11, identity=identity, root=str(root), launchers=launchers, hdr=nits is not None,
        auto_margin=Toggles.Mobile.icon_auto_margin, host_runtime=host_runtime,
        source=str(source) if source else None,
        dependencies={str(path): stamp(path) for path in probe.dependencies if path != root},
        outputs={str(path): stamp(path) for path in outputs}), indent=2) + '\n')
    return destination, [path for path in outputs if path.suffix == '.png']


def sync_project_icon(root, build):
    """Upgrade old generated projects too; direct PNG resources bypass actool's SDR conversion."""
    destination, icons = prepare_icon(root, build)
    project = Path(build) / 'MeltyIOS.xcodeproj/project.pbxproj'
    document = plistlib.loads(project.read_bytes())
    objects = document['objects']
    changed = False

    def add(key, **fields):
        nonlocal changed
        identifier = hashlib.sha256(f'melty-app-icon:{key}'.encode()).hexdigest()[:24].upper()
        if objects.get(identifier) != fields:
            objects[identifier] = fields
            changed = True
        return identifier

    reference = add('directory', isa='PBXFileReference', lastKnownFileType='folder',
                    name=destination.name, path=str(destination), sourceTree='<absolute>')
    resources = []
    for icon in icons:
        ref = add(icon.name, isa='PBXFileReference', lastKnownFileType='image.png',
                  name=icon.name, path=str(icon), sourceTree='<absolute>')
        resources.append(add('resource:' + icon.name, isa='PBXBuildFile', fileRef=ref))
    phase = add('phase', isa='PBXResourcesBuildPhase', buildActionMask=2147483647,
                files=resources, runOnlyForDeploymentPostprocessing=0)
    for obj in list(objects.values()):
        if obj['isa'] != 'PBXNativeTarget' or obj.get('name') != 'Melty':
            continue
        if phase not in obj['buildPhases']:
            obj['buildPhases'].append(phase)
            changed = True
        configurations = objects[obj['buildConfigurationList']]['buildConfigurations']
        for identifier in configurations:
            settings = objects[identifier]['buildSettings']
            for key, value in {'INFOPLIST_FILE': str(destination / 'Info.plist'),
                               'COMPRESS_PNG_FILES': 'NO'}.items():
                if settings.get(key) != value:
                    settings[key] = value
                    changed = True
    group = objects[objects[document['rootObject']]['mainGroup']]
    if reference not in group['children']:
        group['children'].append(reference)
        changed = True
    if changed:
        temporary = project.with_suffix('.tmp')
        temporary.write_bytes(plistlib.dumps(document, sort_keys=True))
        temporary.replace(project)
