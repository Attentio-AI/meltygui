"""Desktop-entry artwork for filesystem paths: cached I/O, background decode, owned textures."""
import configparser
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile

from PIL import Image
from meltygui.core.graphics import desktop_gl as GL
from meltygui.model.texture_model import TextureId, upload_native_rgba
from meltygui.core.graphics.gl_state import GLTexture, tight_unpack


ICON_SIZE = 64  # Decode small previews; never upload full application artwork for an 18px row.


@dataclass(frozen=True)
class PathIcon:
    """A path's icon presentation, independent of menu labels and row values."""
    path: str
    is_dir: bool = True
    custom_icon: str | None = None


def stamp(path):
    try:
        value = path.stat()
        return value.st_mtime_ns, value.st_size, value.st_mode
    except OSError:
        return None


class Probe:
    def __init__(self):
        self.dependencies = {}

    def exists(self, path):
        value = stamp(path)
        if value is not None:
            self.dependencies[path] = value
            return True
        # Missing candidates share their nearest existing ancestor's generation.
        # This also notices newly created icon/theme subdirectories without
        # retaining thousands of negative filename probes in each row's cache.
        parent = path.parent
        while parent != parent.parent:
            value = stamp(parent)
            if value is not None:
                self.dependencies[parent] = value
                break
            parent = parent.parent
        return False

    def config(self, path):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        if self.exists(path):
            try:
                with path.open(encoding='utf-8-sig') as source:
                    parser.read_string(source.read(262144))
            except (OSError, UnicodeError, configparser.Error):
                return configparser.ConfigParser(interpolation=None)
        return parser


def icon_roots():
    data_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    data_dirs = [Path(p) for p in os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':') if p]
    return [Path.home() / '.icons', data_home / 'icons', *(p / 'icons' for p in data_dirs)], data_dirs


def resolve_icon(value, folder, probe, size=ICON_SIZE):
    """Absolute/project-relative artwork, then the desktop's theme and fallbacks."""
    value = re.sub(r'\\([sntr\\])', lambda m: {'s': ' ', 'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}[m[1]], value)
    path = Path(value)
    if path.is_absolute() or '/' in value:
        path = path if path.is_absolute() else folder / path
        if probe.exists(path):
            return path
        # Launchers commonly contain absolute paths from the original checkout.
        # Keep in-project artwork portable when that checkout moves to a Mac.
        if path.is_absolute() and folder.name in path.parts[:-1]:
            index = max(i for i, part in enumerate(path.parts[:-1]) if part == folder.name)
            local = folder.joinpath(*path.parts[index + 1:])
            if probe.exists(local):
                return local
        return None
    if path.suffix and probe.exists(folder / path):
        return folder / path
    roots, data_dirs = icon_roots()
    config_home = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    settings = probe.config(config_home / 'gtk-3.0/settings.ini')
    theme = settings.get('Settings', 'gtk-icon-theme-name', fallback='Adwaita')
    names = [value] if path.suffix.lower() in ('.png', '.svg', '.xpm') else [value + ext for ext in ('.png', '.svg', '.xpm')]
    visited = set()

    def in_theme(name):
        if not name or name in visited or '/' in name or name in ('.', '..'):
            return None
        visited.add(name)
        index = None
        for root in roots:
            candidate = probe.config(root / name / 'index.theme')
            if candidate.has_section('Icon Theme'):
                index = candidate
                break
        if index is None:
            return None
        directories = index.get('Icon Theme', 'Directories', fallback='').split(',')
        directories += index.get('Icon Theme', 'ScaledDirectories', fallback='').split(',')

        def distance(directory):
            try:
                nominal = index.getint(directory, 'Size', fallback=size)
                scale = index.getint(directory, 'Scale', fallback=1)
                kind = index.get(directory, 'Type', fallback='Threshold')
                low = index.getint(directory, 'MinSize', fallback=nominal) if kind == 'Scalable' else nominal
                high = index.getint(directory, 'MaxSize', fallback=nominal) if kind == 'Scalable' else nominal
                if kind == 'Threshold':
                    threshold = index.getint(directory, 'Threshold', fallback=2)
                    low, high = nominal - threshold, nominal + threshold
                return max(low * scale - size, size - high * scale, 0)
            except ValueError:
                return 100000

        directories = dict.fromkeys(d.strip() for d in directories if d.strip())
        for directory in sorted(directories, key=distance):
            if Path(directory).is_absolute() or '..' in Path(directory).parts:
                continue
            for root in roots:
                for filename in names:
                    candidate = root / name / directory / filename
                    if probe.exists(candidate):
                        return candidate
        for parent in index.get('Icon Theme', 'Inherits', fallback='').split(','):
            result = in_theme(parent.strip())
            if result is not None:
                return result
        return None

    for name in (theme, 'hicolor'):
        result = in_theme(name)
        if result is not None:
            return result
    for root in [*roots, *(p / 'pixmaps' for p in data_dirs)]:
        for filename in names:
            candidate = root / filename
            if probe.exists(candidate):
                return candidate
    return None


@dataclass
class IconPixels:
    path: Path
    width: int
    height: int
    data: bytes
    format: str = 'rgba8srgb'


def hdr_icon_pixels(path, size, rgb, alpha, primaries):
    """Resize PQ artwork in premultiplied linear light, retaining HDR and gamut."""
    import numpy as np
    from meltygui.image_load import pq_to_linear

    # PQ artwork uses the BT.2408 203-nit reference, like our image viewer.
    linear, _ = pq_to_linear(rgb, primaries, 203)
    opacity = (np.ones(rgb.shape[:2], dtype=np.float32) if alpha is None else
               alpha.astype(np.float32) / np.iinfo(alpha.dtype).max)
    rgba = np.concatenate((linear * opacity[..., None], opacity[..., None]), axis=-1)
    height, width = opacity.shape
    scale = min(1, size / max(width, height))
    target = max(1, round(width * scale)), max(1, round(height * scale))
    if target != (width, height):
        # Positive area weights preserve radiance and avoid Lanczos ringing
        # exploding when unpremultiplying a nearly transparent HDR edge.
        rgba = np.stack([np.asarray(Image.fromarray(rgba[..., channel]).resize(
            target, Image.Resampling.BOX)) for channel in range(4)], axis=-1)
    opacity = np.clip(rgba[..., 3:4], 0, 1)
    # Mesh blending consumes straight alpha. Do not clamp signed scRGB or HDR.
    rgba[..., :3] = np.divide(rgba[..., :3], opacity, out=np.zeros_like(rgba[..., :3]),
                              where=opacity > 1e-6)
    rgba[..., 3:4] = opacity
    return IconPixels(path, *target, rgba.astype(np.float16).tobytes(), 'rgba16f')


def decode_icon(path, size=ICON_SIZE):
    """CPU only. GdkPixbuf supplies SVG support without starting a GUI or shell."""
    def pixels(source):
        from meltygui.image_load import png_pq_cicp, read_png16, icc_pq_cicp
        data = source.read_bytes()
        pq = png_pq_cicp(None, data)
        if pq and pq[0] == 16:
            rgb, alpha, _ = read_png16(None, data)
            return hdr_icon_pixels(path, size, rgb, alpha, pq[1])
        with Image.open(io.BytesIO(data)) as image:
            primaries = pq[1] if pq else icc_pq_cicp(image.info.get('icc_profile'))
            if primaries is not None:
                import numpy as np
                rgba = np.asarray(image.convert('RGBA'))
                return hdr_icon_pixels(path, size, rgba[..., :3], rgba[..., 3], primaries)
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            image = image.convert('RGBA')
            return IconPixels(path, image.width, image.height, image.tobytes())
    if path.suffix.lower() != '.svg':
        return pixels(path)
    executable = shutil.which('gdk-pixbuf-thumbnailer')
    if executable is None:
        raise ValueError('SVG icon decoder is unavailable')
    with tempfile.TemporaryDirectory(prefix='melty-folder-icon-') as directory:
        output = Path(directory) / 'icon.png'
        subprocess.run([str(Path(executable).resolve()), '-s', str(size), str(path), str(output)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       close_fds=False, timeout=5)
        return pixels(output)


def folder_icon_candidates(folder, probe, size=ICON_SIZE):
    """Share launcher selection with packaging without decoding through an 8-bit preview."""
    probe.exists(folder)
    try:
        entries = sorted(folder.glob('*.desktop'), key=lambda p: (p.stem != folder.name, p.name))
    except OSError:
        entries = []
    for desktop in entries:
        config = probe.config(desktop)
        if config.get('Desktop Entry', 'Type', fallback='') != 'Application':
            continue
        if config.get('Desktop Entry', 'Hidden', fallback='false').lower() == 'true':
            continue
        value = config.get('Desktop Entry', 'Icon', fallback='').strip()
        if not value:
            continue
        path = resolve_icon(value, folder, probe, size)
        if path is None and Path(value).is_absolute():
            # The project can also be renamed when copied to the build Mac.
            # Rebase only against an explicit launcher path whose executable
            # exists in this project, never an arbitrary matching suffix.
            try:
                command = shlex.split(config.get('Desktop Entry', 'Exec', fallback=''))
                executable = Path(command[0]) if command else None
                if executable is not None and executable.is_absolute() and probe.exists(folder / executable.name):
                    relative = Path(value).relative_to(executable.parent)
                    path = resolve_icon('./' + str(relative), folder, probe, size)
            except ValueError:
                pass
        yield desktop, value, path


def load_folder_icon(folder, size=ICON_SIZE):
    """Choose the first usable Application icon; prefer a matching desktop filename."""
    probe = Probe()
    for desktop, value, path in folder_icon_candidates(folder, probe, size):
        if path is None:
            continue
        try:
            return decode_icon(path, size), probe.dependencies
        except (OSError, ValueError, subprocess.SubprocessError, Image.DecompressionBombError):
            continue
    return None, probe.dependencies


class FolderIconTexture(TextureId):
    def __init__(self, pixels):
        super().__init__(0x0DE1)  # GL_TEXTURE_2D metadata; allocation waits for the active renderer
        self.pixels = pixels

    def _upload(self, state):
        def create():
            hdr = self.pixels.format == 'rgba16f'
            internal = GL.GL_RGBA16F if hdr else GL.GL_SRGB8_ALPHA8
            pixel_type = GL.GL_HALF_FLOAT if hdr else GL.GL_UNSIGNED_BYTE
            previous = int(GL.glGetIntegerv(GL.GL_TEXTURE_BINDING_2D))
            texture = int(GL.glGenTextures(1))
            try:
                GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
                with tight_unpack():
                    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, internal,
                                    self.pixels.width, self.pixels.height, 0,
                                    GL.GL_RGBA, pixel_type, self.pixels.data)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
                return GLTexture(texture, GL.GL_TEXTURE_2D,
                                 (self.pixels.height, self.pixels.width), internal)
            except Exception:
                GL.glDeleteTextures([texture])
                raise
            finally:
                GL.glBindTexture(GL.GL_TEXTURE_2D, previous)
        return state.get('icon', create, lambda texture: GL.glDeleteTextures([texture.texture_id]))

    def _upload_native(self, state, renderer):
        from types import SimpleNamespace

        def create():
            return SimpleNamespace(texture_id=upload_native_rgba(
                renderer, self.pixels.width, self.pixels.height, self.pixels.data, self.pixels.format))

        return state.get('icon', create, lambda texture: renderer.delete_texture(texture.texture_id))


class FolderIcons:
    """A view owns its CPU results and context-local texture proxies."""
    def __init__(self):
        self.entries = {}
        self.pending = {}
        self.requested = set()
        self.executor = None
        self.closed = False
        self.dirty = set()
        self._watch_directories = None

    def _ensure_tracking(self):
        # Preserve decoded textures/futures when these methods hotswap onto
        # an already-running cache created before event-driven freshness.
        if 'dirty' not in self.__dict__:
            self.dirty = set()
            self._watch_directories = None

    def retain(self, folders):
        self._ensure_tracking()
        from meltygui.model.file_location_model import FileLocation
        requested = {path for path in folders if FileLocation.parse(path).local_path is not None}
        if requested != self.requested:
            self._watch_directories = None
        self.requested = requested
        self.dirty.intersection_update(requested)
        for folder in self.entries.keys() - self.requested:
            texture, _dependencies = self.entries.pop(folder)
            if texture is not None:
                texture.release()

    def consume(self, folders):
        self.closed = False
        self.retain(folders)
        for folder, future in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[folder]
            try:
                pixels, dependencies = future.result()
            except Exception:
                pixels, dependencies = None, {}
            if folder not in self.requested:
                continue
            if dependencies is None:  # Worker verified the existing result is current.
                continue
            self._watch_directories = None
            old = self.entries.get(folder)
            if old and old[0] is not None:
                old[0].release()
            self.entries[folder] = (FolderIconTexture(pixels) if pixels is not None else None, dependencies)

    def get(self, folder):
        entry = self.entries.get(folder)
        return entry[0] if entry else None

    def watch_directories(self):
        self._ensure_tracking()
        if self._watch_directories is not None:
            return self._watch_directories
        # A new path is watched at its parent until its worker reports its type.
        # All existence/type probes belong to the loader, never the paint loop.
        directories = {path.parent for path in self.requested - self.entries.keys()}
        for folder, (_texture, dependencies) in self.entries.items():
            if not dependencies:
                directories.add(folder.parent)
            for path, value in dependencies.items():
                directory = path if value and stat.S_ISDIR(value[2]) else path.parent
                directories.add(directory)
        self._watch_directories = {str(path) for path in directories}
        return self._watch_directories

    def mark_dirty(self):
        self._ensure_tracking()
        self.dirty.update(self.requested)

    def dispatch(self, wake, submit=None, check_stale=True):
        self._ensure_tracking()
        for folder in self.requested - self.pending.keys():
            entry = self.entries.get(folder)
            if entry is not None:
                if check_stale:
                    if all(stamp(path) == version for path, version in entry[1].items()):
                        continue
                elif folder not in self.dirty:
                    continue
            if submit is None:
                if self.executor is None:
                    self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='folder-icons')
                submit = self.executor.submit
            self.dirty.discard(folder)
            future = (submit(refresh_folder_icon, folder, entry[1]) if entry is not None
                      else submit(load_folder_icon, folder))
            self.pending[folder] = future
            future.add_done_callback(lambda _future: None if self.closed else wake())

    def close(self):
        self._ensure_tracking()
        self.closed = True
        for future in self.pending.values():
            future.cancel()
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None
        self.pending.clear()
        for texture, _dependencies in self.entries.values():
            if texture is not None:
                texture.release()
        self.entries.clear()
        self.requested.clear()
        self.dirty.clear()
        self._watch_directories = None


def refresh_folder_icon(folder, dependencies):
    """Filesystem events trigger freshness checks on the worker, before decode."""
    if all(stamp(path) == version for path, version in dependencies.items()):
        return None, None
    return load_folder_icon(folder)
