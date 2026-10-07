"""Launcher artwork for iOS, preserving PQ HDR instead of passing it through actool."""
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import struct
import zlib

import numpy as np
from PIL import Image, ImageOps

from meltygui.image_load import png_pq_cicp, pq_decode, read_png16
from meltygui.model.folder_icon_model import Probe, decode_icon, folder_icon_candidates, stamp


# Point sizes and scales used for notifications, Settings, Spotlight and Home.
ICON_SIZES = {'20x20': (1, 2, 3), '29x29': (1, 2, 3), '40x40': (1, 2, 3),
              '60x60': (2, 3), '76x76': (1, 2), '83.5x83.5': (2,)}
INFO_TEMPLATE = Path(__file__).parent / 'Host/Info.plist'


def write_hdr_icon(path, nits, size, cicp):
    """Resize in linear light, then encode opaque 16-bit PQ with HDR metadata."""
    height, width = nits.shape[:2]
    factor = size / max(width, height)
    dimensions = max(1, round(width * factor)), max(1, round(height * factor))
    resized = np.stack([np.asarray(Image.fromarray(nits[..., channel].astype(np.float32)).resize(
        dimensions, Image.Resampling.LANCZOS)) for channel in range(3)], axis=-1)
    # Ringing must not invent brighter highlights than the source.
    resized = np.clip(resized, 0, max(203, float(nits.max())))
    square = np.full((size, size, 3), 203.0, dtype=np.float32)
    x, y = (size - dimensions[0]) // 2, (size - dimensions[1]) // 2
    square[y:y + dimensions[1], x:x + dimensions[0]] = resized
    linear = np.clip(square / 10000, 0, 1) ** (2610 / 16384)
    encoded = ((3424 / 4096 + (2413 / 128) * linear) / (1 + (2392 / 128) * linear)) ** (2523 / 32)
    codes = np.round(encoded * 65535).astype('>u2')

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    rows = b''.join(b'\0' + row.tobytes() for row in codes)
    # cLLi is expressed in ten-thousandths of a nit. Use the actual output's
    # peak and conservative frame average, keeping values above SDR white.
    light = struct.pack('>II', round(float(square.max()) * 10000),
                        round(float(square.max(axis=-1).mean()) * 10000))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 16, 2, 0, 0, 0))
                     + chunk(b'cICP', cicp) + chunk(b'cLLi', light)
                     + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def prepare_icon(root, build):
    """Refresh generated icons only when launcher/artwork inputs change."""
    root, build = Path(root).resolve(), Path(build).resolve()
    destination = build / 'app-icon'
    receipt = build / 'app-icon.json'
    launchers = sorted(path.name for path in root.glob('*.desktop'))
    try:
        previous = json.loads(receipt.read_text())
    except (OSError, ValueError):
        previous = {}
    if (previous.get('root') == str(root) and previous.get('version') == 2
            and previous.get('launchers') == launchers
            and all(stamp(Path(path)) == (tuple(value) if value else None)
                    for path, value in previous['dependencies'].items())
            and all(stamp(Path(path)) == tuple(value)
                    for path, value in previous['outputs'].items())):
        return destination, [Path(path) for path in previous['outputs'] if path.endswith('.png')]

    probe = Probe()
    candidates = folder_icon_candidates(root, probe, 1024, prefer_hdr=True)
    chosen = next(candidates, None)
    source = None
    if chosen is not None:
        desktop, value, source = chosen
        if source is None:
            raise ValueError(f'{desktop}: launcher icon {value!r} could not be resolved')
    probe.exists(INFO_TEMPLATE)
    probe.exists(Path(__file__))
    info = plistlib.loads(INFO_TEMPLATE.read_bytes())
    nits = None
    image = None
    if source is not None:
        hdr = png_pq_cicp(source)
        if hdr:
            if hdr != (16, 9):
                raise ValueError(f'{source}: HDR app icons require a 16-bit Rec.2020/PQ PNG')
            rgb, alpha, cicp = read_png16(source)
            if cicp != bytes([9, 16, 0, 1]):
                raise ValueError(f'{source}: HDR app icons require full-range RGB Rec.2020/PQ')
            nits = pq_decode(rgb.astype(np.float64) / 65535)
            if alpha is not None:
                opacity = alpha[..., None].astype(np.float64) / 65535
                nits = nits * opacity + 203 * (1 - opacity)
        else:
            # A declared HDR icon must never silently fall through an SDR decoder.
            config = probe.config(desktop)
            if config.get('Desktop Entry', 'X-HDR-Icon', fallback='').strip():
                raise ValueError(f'{source}: X-HDR-Icon must be a tagged 16-bit Rec.2020/PQ PNG')
            pixels = decode_icon(source, size=1024)
            image = Image.frombytes('RGBA', (pixels.width, pixels.height), pixels.data)

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    outputs = []
    if source is not None:
        for points, scales in ICON_SIZES.items():
            for scale in scales:
                size = round(float(points.split('x')[0]) * scale)
                suffix = f'@{scale}x' if scale != 1 else ''
                path = destination / f'AppIcon{points}{suffix}.png'
                if nits is not None:
                    write_hdr_icon(path, nits, size, cicp)
                else:
                    resized = ImageOps.contain(image, (size, size), Image.Resampling.LANCZOS)
                    square = Image.new('RGB', (size, size), 'white')
                    square.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2), resized)
                    square.save(path)
                outputs.append(path)
        phone = ['AppIcon20x20', 'AppIcon29x29', 'AppIcon40x40', 'AppIcon60x60']
        tablet = phone + ['AppIcon76x76', 'AppIcon83.5x83.5']
        info['CFBundleIcons'] = {'CFBundlePrimaryIcon': {'CFBundleIconFiles': phone}}
        info['CFBundleIcons~ipad'] = {'CFBundlePrimaryIcon': {'CFBundleIconFiles': tablet}}
    (destination / 'Info.plist').write_bytes(plistlib.dumps(info))
    outputs.append(destination / 'Info.plist')
    receipt.touch(exist_ok=True)
    receipt.write_text(json.dumps(dict(version=2, root=str(root), launchers=launchers, hdr=nits is not None,
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
