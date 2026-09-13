"""Load an image (a file, or encoded bytes) as linear scRGB float32 (h, w, 3), 1.0 = SDR white (``sdr_white`` nits).

melty's copy of hdr-viewer's image_load.py (~/Desktop/hdr-viewer), the reference decoder; `load_bytes` is the
addition for the chat transcript's inline images (base64 payloads in a session).

That is melty's working space: sRGB primaries, linear light, no ceiling, negatives
allowed (BT.2020 colours outside sRGB come out negative and survive the fp16 path).

Three kinds of file:
  * 16-bit PNG with a PQ cICP chunk (what screenshot-hdr writes). Decoded here
    by hand (Pillow flattens 16-bit RGB to 8-bit), PQ -> nits, BT.2020 -> sRGB
    primaries, divided by ``sdr_white`` — the reference the FILE was authored
    against (203 nits, BT.2408), not the desktop's SDR white.
  * 8-bit PQ: a PNG with a PQ cICP chunk (Blender's HDR output) or a JPEG (or
    anything Pillow opens) with a PQ ICC profile ("Rec2020 Gamut with PQ
    Transfer"). Pillow decodes, then the same PQ path through a 256-entry LUT.
  * anything else Pillow opens: treated as sRGB, 1.0 = SDR white. (A wide-gamut
    SDR ICC profile such as Display P3 is not honoured yet.)
"""
import ctypes
import hashlib
import os
import pathlib
import struct
import subprocess
import zlib

import numpy as np

# chromaticities -> RGB->XYZ, and the BT.2020 -> sRGB matrix (both D65, no adaptation)
def _rgb_to_xyz(xy):
    xy = np.asarray(xy, dtype=float)
    xyz = np.stack([xy[:, 0] / xy[:, 1], np.ones(4), (1 - xy.sum(axis=1)) / xy[:, 1]], axis=0)
    m = xyz[:, :3]
    return m @ np.diag(np.linalg.solve(m, xyz[:, 3]))

BT2020_XY = [[.708, .292], [.170, .797], [.131, .046], [.3127, .3290]]
SRGB_XY = [[.64, .33], [.30, .60], [.15, .06], [.3127, .3290]]
P3_XY = [[.680, .320], [.265, .690], [.150, .060], [.3127, .3290]]
BT2020_TO_SRGB = np.linalg.solve(_rgb_to_xyz(SRGB_XY), _rgb_to_xyz(BT2020_XY))
# CICP (H.273) colour primaries code -> matrix to sRGB primaries
PRIMARIES_TO_SRGB = {1: np.eye(3), 9: BT2020_TO_SRGB,
                     12: np.linalg.solve(_rgb_to_xyz(SRGB_XY), _rgb_to_xyz(P3_XY))}
CICP_PQ = 16


def pq_decode(v):
    """PQ code values in [0, 1] -> nits."""
    p = np.maximum(v, 0) ** (1 / (2523 / 32))
    return (np.maximum(p - 3424 / 4096, 0) / (2413 / 128 - (2392 / 128) * p)) ** (1 / (2610 / 16384)) * 10000


_PQ16_LUT = None


def pq16_to_nits():
    """nits for every 16-bit PQ code, float32 (65536,). Indexing a 1080p frame
    through it is ~10 ms; evaluating pq_decode over the frame was ~100 ms."""
    global _PQ16_LUT
    if _PQ16_LUT is None:
        _PQ16_LUT = pq_decode(np.arange(65536, dtype=np.float64) / 65535.0).astype(np.float32)
    return _PQ16_LUT


def pq8_to_nits():
    return pq_decode(np.arange(256, dtype=np.float64) / 255.0).astype(np.float32)


def pq_to_linear(codes, primaries, sdr_white, alpha=None):
    """PQ code values (uint8 or uint16 (h, w, 3)) in CICP ``primaries`` ->
    linear scRGB float32, 1.0 = ``sdr_white`` nits; alpha (same dtype) composites
    over black, like the compositor would. Returns (linear, peak_nits)."""
    lut = pq16_to_nits() if codes.dtype == np.uint16 else pq8_to_nits()
    nits = lut[codes]
    if alpha is not None and alpha.min() != np.iinfo(alpha.dtype).max:
        nits *= alpha.astype(np.float32)[..., None] / np.float32(np.iinfo(alpha.dtype).max)
    m = (PRIMARIES_TO_SRGB.get(primaries, BT2020_TO_SRGB) / float(sdr_white)).astype(np.float32)
    return nits @ m.T, float(nits.max())   # fp32 throughout: ~2 ms for 1080p


def srgb_to_linear(v):
    v = np.asarray(v, dtype=np.float32)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


# --- 16-bit PNG ----------------------------------------------------------------

_UNFILTER_SRC = pathlib.Path(__file__).with_name('png_unfilter.c')
_native = None  # C function, or False once we know it is unavailable


def _native_unfilter():
    """png_unfilter from png_unfilter.c, compiled once with cc into the cache dir
    (keyed by source hash). None if there is no compiler."""
    global _native
    if _native is not None:
        return _native or None
    _native = False
    try:
        src = _UNFILTER_SRC.read_bytes()
        cache = pathlib.Path(os.environ.get('XDG_CACHE_HOME') or pathlib.Path.home() / '.cache') / 'melty'
        so = cache / f'png_unfilter-{hashlib.sha1(src).hexdigest()[:12]}.so'
        if not so.is_file():
            cache.mkdir(parents=True, exist_ok=True)
            tmp = so.with_suffix(f'.{os.getpid()}.tmp')
            subprocess.run(['cc', '-O2', '-shared', '-fPIC', '-o', str(tmp), str(_UNFILTER_SRC)],
                           check=True, capture_output=True)
            os.replace(tmp, so)
        fn = ctypes.CDLL(str(so)).png_unfilter
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        fn.restype = ctypes.c_int
        _native = fn
    except (OSError, subprocess.CalledProcessError):
        pass
    return _native or None


def _unfilter(raw, bpp):
    """raw: (h, 1 + stride) uint8 with the filter byte first -> (h, stride) unfiltered.
    Sub/Average/Paeth are serial in both axes, so this is a C loop when a compiler
    is around and a (very slow: seconds per Paeth-heavy 1080p frame) Python one
    otherwise."""
    h, stride = raw.shape[0], raw.shape[1] - 1
    fn = _native_unfilter()
    if fn is not None:
        raw = np.ascontiguousarray(raw)
        out = np.empty((h, stride), dtype=np.uint8)
        if fn(raw.ctypes.data, out.ctypes.data, h, stride, bpp) == 0:
            return out
        raise ValueError('bad PNG filter type')
    return _unfilter_py(raw[:, 1:], raw[:, 0], bpp)


def _unfilter_py(rows, filters, bpp):
    out = np.zeros_like(rows)
    prior = np.zeros(rows.shape[1], dtype=np.int32)
    for y in range(rows.shape[0]):
        kind = int(filters[y])
        cur = rows[y].astype(np.int32)
        if kind == 0:
            raw = cur
        elif kind == 2:
            raw = (cur + prior) & 255
        elif kind == 1:
            raw = (np.cumsum(cur.reshape(-1, bpp), axis=0) & 255).reshape(-1)
        else:
            raw = np.zeros_like(cur)
            for i in range(cur.shape[0]):
                a = raw[i - bpp] if i >= bpp else 0
                b = prior[i]
                if kind == 3:
                    pred = (a + b) >> 1
                else:
                    c = prior[i - bpp] if i >= bpp else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                raw[i] = (cur[i] + pred) & 255
        out[y] = raw
        prior = raw
    return out


def read_png16(path, data=None):
    """-> (rgb (h, w, 3) uint16, alpha uint16 or None, cicp bytes or None).
    Raises ValueError unless it is a 16-bit RGB/RGBA non-interlaced PNG.
    ``data``: the file's bytes when already in hand (no path needed)."""
    if data is None:
        data = pathlib.Path(path).read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    pos, idat, cicp, header = 8, [], None, None
    while pos + 8 <= len(data):
        length, = struct.unpack('>I', data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b'IHDR':
            header = struct.unpack('>IIBBBBB', body[:13])
        elif kind == b'cICP':
            cicp = bytes(body[:4])
        elif kind == b'IDAT':
            idat.append(body)
        elif kind == b'IEND':
            break
        pos += 12 + length
    if header is None:
        raise ValueError('PNG without IHDR')
    w, h, depth, ctype, _, _, interlace = header
    if depth != 16 or ctype not in (2, 6) or interlace:
        raise ValueError('not a 16-bit RGB/RGBA PNG')
    channels = 3 if ctype == 2 else 4
    bpp = channels * 2
    raw = np.frombuffer(zlib.decompress(b''.join(idat)), np.uint8).reshape(h, 1 + w * bpp)
    rows = _unfilter(raw, bpp) if raw[:, 0].any() else raw[:, 1:]
    samples = rows.reshape(-1).view('>u2').reshape(h, w, channels).astype(np.uint16)
    return samples[..., :3], (samples[..., 3] if channels == 4 else None), cicp


def png_pq_cicp(path, data=None):
    """(bit depth, primaries) if ``path`` (or ``data``, the file's bytes) is a
    PNG whose cICP chunk says PQ transfer (screenshot-hdr: 16-bit; Blender:
    8-bit), else None."""
    if data is not None:
        head = bytes(data[:4096])
    else:
        try:
            with open(path, 'rb') as f:
                head = f.read(4096)
        except OSError:
            return None
    if head[:8] != b'\x89PNG\r\n\x1a\n' or len(head) < 25:
        return None
    pos = 8
    while pos + 8 <= len(head):
        length, = struct.unpack('>I', head[pos:pos + 4])
        kind = head[pos + 4:pos + 8]
        if kind == b'cICP':
            primaries, transfer = head[pos + 8], head[pos + 9]
            return (head[24], primaries) if transfer == CICP_PQ else None
        if kind in (b'IDAT', b'IEND'):
            return None
        pos += 12 + length
    return None


def icc_pq_cicp(icc):
    """The primaries code if an ICC profile declares a PQ transfer: its ICC.2
    ``cicp`` tag (Google's "Rec2020 Gamut with PQ Transfer" profile carries
    one), or failing that a description naming PQ. None for an SDR profile."""
    if not icc or len(icc) < 132:
        return None
    try:
        count, = struct.unpack('>I', icc[128:132])
        for i in range(count):
            sig, off, ln = struct.unpack('>4sII', icc[132 + 12 * i:144 + 12 * i])
            if sig == b'cicp' and ln >= 12:
                primaries, transfer = icc[off + 8], icc[off + 9]
                return primaries if transfer == CICP_PQ else None
            if sig == b'desc':
                desc = icc[off:off + ln].replace(b'\x00', b'').lower()
                if b'pq' in desc or b'2100' in desc:
                    return 9
    except (struct.error, IndexError):
        pass
    return None


# --- public -------------------------------------------------------------------------------

class Loaded:
    """rgb: float32 (h, w, 3) linear scRGB, 1.0 = SDR white — or, for an opaque
    8-bit sRGB source, srgb8: uint8 (h, w, 3) as decoded, to be linearised by the
    GPU (rgb is then None). hdr: True for PQ sources; peak_nits: brightest
    channel in nits (for the title)."""
    def __init__(self, rgb, hdr, peak_nits, srgb8=None):
        self.rgb, self.hdr, self.peak_nits, self.srgb8 = rgb, hdr, peak_nits, srgb8

    @property
    def size(self):
        a = self.rgb if self.rgb is not None else self.srgb8
        return a.shape[1], a.shape[0]


def load(path, sdr_white, mark=lambda label: None):
    """Decode ``path`` to linear scRGB. ``mark(label)`` is called after each stage for timing."""
    return _decode(path, None, sdr_white, mark)


def load_bytes(data, sdr_white, mark=lambda label: None):
    """Decode an encoded image held in memory (a PNG / JPEG / WebP payload)
    exactly as `load` decodes a file: PQ PNGs and PQ ICC profiles come out HDR."""
    return _decode(None, bytes(data), sdr_white, mark)


def _decode(path, data, sdr_white, mark):
    pq = png_pq_cicp(path, data)
    if pq and pq[0] == 16:
        mark('sniffed PQ png')
        rgb, alpha, _ = read_png16(path, data)
        mark('png16 decoded')
        linear, peak = pq_to_linear(rgb, pq[1], sdr_white, alpha)
        mark('PQ -> linear scRGB')
        return Loaded(linear, True, peak)
    mark('sniffed: not 16-bit PQ png')
    from PIL import Image, ImageOps
    mark('PIL imported')
    import io
    with Image.open(path if data is None else io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        # PQ in an 8-bit container: a PNG with cICP (Blender) or a JPEG/anything
        # with a PQ ICC profile (Chromium honours both; treated as sRGB they
        # show flat and dim, 09-11).
        primaries = pq[1] if pq else icc_pq_cicp(im.info.get('icc_profile'))
        if primaries is not None:
            arr = np.asarray(im.convert('RGBA'))
            linear, peak = pq_to_linear(arr[..., :3], primaries, sdr_white, arr[..., 3])
            mark('PIL decoded, PQ8 -> linear scRGB')
            return Loaded(np.ascontiguousarray(linear, dtype=np.float32), True, peak)
        if im.mode in ('RGBA', 'LA', 'P', 'PA') or os.environ.get('MELTY_NO_SRGB8'):
            im = im.convert('RGBA')
            arr = np.asarray(im, dtype=np.float32) / 255.0
            rgb = srgb_to_linear(arr[..., :3]) * arr[..., 3:4]
            mark('PIL decoded -> linear')
            return Loaded(np.ascontiguousarray(rgb, dtype=np.float32), False, float(rgb.max()) * float(sdr_white))
        srgb8 = np.ascontiguousarray(im.convert('RGB'))
    mark('PIL decoded (sRGB8)')
    peak = srgb_to_linear(np.float32(int(srgb8.max()) / 255.0))
    return Loaded(None, False, float(peak) * float(sdr_white), srgb8=srgb8)
