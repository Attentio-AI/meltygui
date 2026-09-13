"""Inline images for the chat transcript: decode off the render thread, upload
on it, draw fitted, HDR intact.

An `ImageReference` in a message names its picture one of three ways —
``path`` (a file), ``data`` + ``media_type`` (a base64 payload, the Claude API
shape, also accepted nested under ``source``), or ``url`` (a data URL) — and
`image_key` reduces any of them to one cache key. `ImageCache.entry(ref)`
returns the picture's state: it queues a decode the first time (a worker
thread running melty's `image_load`, hdr-viewer's decoder: PQ PNGs and PQ
ICC profiles come out as linear scRGB above 1.0, everything else as sRGB8),
uploads the decoded pixels the first time the render thread asks (GL is
current inside a window body only), and is drawn with `draw_image` through
imgui's draw list — an RGB16F texture in melty's fp16 scene, so on an HDR
desktop the highlights present as HDR with nothing more to do, and on an
SDR desktop they clip at white like everything else melty draws.

Textures are few and small compared to the payloads: the cache keeps
``keep`` of them and drops the least recently drawn beyond that.
"""
import base64
import hashlib
import os
import queue
import threading
import time
import weakref

from src.lsd.gl_gui.chat.messages import ImageReference

# The SDR reference white a PQ file is authored against (BT.2408: 203 nits);
# dividing by it means 1.0 = the file's SDR white, what melty maps to the
# desktop's SDR white (the same mapping Chrome applies).
PQ_SDR_WHITE = 203.0
MEDIA_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _source(ref):
    """(path, bytes-or-None) of an ImageReference, or (None, None) when it
    carries nothing decodable. Data URLs and base64 payloads are decoded here."""
    source = ref.get("source") if isinstance(ref.get("source"), dict) else ref
    path = source.get("path") or ref.get("path") or source.get("savedPath")
    if path and not str(source.get("data") or "").strip():
        return str(path), None
    data = source.get("data")
    url = source.get("url") or ref.get("url") or ref.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if not data and isinstance(url, str) and url.startswith("data:"):
        data = url.split(",", 1)[1] if "," in url else ""
    if isinstance(data, str) and data.strip():
        try:
            return None, base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            return None, None
    if isinstance(url, str) and url and not url.startswith("data:") and os.path.exists(url):
        return url, None
    return None, None


def image_key(ref):
    """One stable key per picture, kept on the reference after the first call
    (a payload is hashed once, never per frame). None: nothing to show."""
    key = getattr(ref, "_image_key", False)
    if key is not False:
        return key
    path, data = _source(ref)
    if data is not None:
        key = "data:" + hashlib.sha1(data).hexdigest()
    elif path is not None:
        try:
            stat = os.stat(path)
            key = f"path:{path}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            key = "path:" + path
    else:
        key = None
    try:
        ref._image_key = key
    except AttributeError:
        pass
    return key


class Entry:
    """One picture. ``status``: loading → ready | failed. ``size`` (w, h)
    arrives with the decode (the layout can fit the row before the upload),
    ``texture`` with the first draw. ``hdr``: the source was PQ."""

    def __init__(self, key):
        self.key = key
        self.status = "loading"
        self.size = None
        self.texture = None
        self.hdr = False
        self.peak_nits = 0.0
        self.error = None
        self.decoded = None       # image_load.Loaded, until uploaded
        self.last_drawn = time.monotonic()


class ImageCache:
    def __init__(self, keep=48, wake=None):
        self.entries = {}
        self.keep = keep
        self.wake = wake             # called on the worker thread every decode: ask for a frame
        self.jobs = queue.Queue()
        self.worker = None
        self.lock = threading.Lock()
        self.views = weakref.WeakValueDictionary()
        self.generation = 0          # bumped when a decode finishes, layouts keyed on it re-fit

    def watch(self, draw_state):
        if callable(getattr(draw_state, "invalidate_up", None)):
            self.views[id(draw_state)] = draw_state

    def decoded_changed(self):
        self.generation += 1
        for view in list(self.views.values()):
            view.invalidate_up()
        if self.wake is not None:
            self.wake()

    def entry(self, ref):
        key = image_key(ref)
        if key is None:
            return None
        entry = self.entries.get(key)
        if entry is None:
            entry = self.entries[key] = Entry(key)
            self.jobs.put((entry, ref))
            self._ensure_worker()
        return entry

    def _ensure_worker(self):
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._work, daemon=True, name="chat-images")
            self.worker.start()

    def _work(self):
        while True:
            try:
                entry, ref = self.jobs.get(timeout=30)
            except queue.Empty:
                return
            try:
                from src.lsd.gl_gui import image_load
                path, data = _source(ref)
                loaded = (image_load.load_bytes(data, PQ_SDR_WHITE) if data is not None
                          else image_load.load(path, PQ_SDR_WHITE))
                with self.lock:
                    entry.decoded, entry.size = loaded, loaded.size
                    entry.hdr, entry.peak_nits = loaded.hdr, loaded.peak_nits
                    entry.status = "ready"
            except Exception as error:
                entry.error = f"{type(error).__name__}: {error}"
                entry.status = "failed"
            self.decoded_changed()

    # -- render-only -------------------------------------------------------

    def texture(self, entry):
        """The GL texture of a decoded entry, uploaded on first use (call
        inside a window body: GL is current there). None until decoded."""
        if entry.texture is None and entry.decoded is not None:
            with self.lock:
                loaded, entry.decoded = entry.decoded, None
            entry.texture = upload_texture(loaded)
            self._evict()
        entry.last_drawn = time.monotonic()
        return entry.texture

    def _evict(self):
        uploaded = [e for e in self.entries.values() if e.texture is not None]
        if len(uploaded) <= self.keep:
            return
        import OpenGL.GL as gl
        for entry in sorted(uploaded, key=lambda e: e.last_drawn)[:len(uploaded) - self.keep]:
            gl.glDeleteTextures(1, [entry.texture])
            del self.entries[entry.key]


def upload_texture(image):
    """image_load.Loaded -> GL texture id, row 0 of the array at v = 0 (draw
    with uv (0, 0) → (1, 1) for the top of the picture at the top). Linear
    float goes up as RGB16F, an opaque 8-bit sRGB source as SRGB8 (the GPU
    linearises it on sample)."""
    import numpy as np
    import OpenGL.GL as gl
    w, h = image.size
    tex = int(gl.glGenTextures(1))
    previous = gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
    if image.srgb8 is not None:
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_SRGB8, w, h, 0, gl.GL_RGB, gl.GL_UNSIGNED_BYTE,
                        np.ascontiguousarray(image.srgb8))
    else:
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGB16F, w, h, 0, gl.GL_RGB, gl.GL_FLOAT,
                        np.ascontiguousarray(image.rgb, dtype=np.float32))
    for p, v in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR), (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE), (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
        gl.glTexParameteri(gl.GL_TEXTURE_2D, p, v)
    gl.glBindTexture(gl.GL_TEXTURE_2D, int(previous))
    return tex


def fitted_size(size, max_width, max_height):
    """The picture's (w, h) inside the box, never upscaled."""
    w, h = size
    if w <= 0 or h <= 0:
        return max_width, max_height
    scale = min(1.0, max_width / w, max_height / h)
    return max(1.0, w * scale), max(1.0, h * scale)


def image_label(ref, entry=None):
    """The caption under a picture: its name, size and HDR peak when known."""
    name = ref.get("name") or ref.get("path") or ""
    if isinstance(ref.get("source"), dict):
        name = name or ref["source"].get("path") or ""
    name = os.path.basename(str(name)) if name else ""
    parts = [part for part in (name,) if part]
    if entry is not None and entry.size:
        parts.append("%d×%d" % entry.size)
        if entry.hdr:
            parts.append(f"HDR, peak {entry.peak_nits:.0f} nits")
    return " · ".join(parts)
