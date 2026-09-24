"""Desktop-entry artwork for filesystem paths: cached I/O, background decode, owned textures."""
import configparser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from PIL import Image
from OpenGL import GL
from meltygui.model.texture_model import TextureId
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


def resolve_icon(value, folder, probe):
    """Absolute/project-relative artwork, then the desktop's theme and fallbacks."""
    value = re.sub(r'\\([sntr\\])', lambda m: {'s': ' ', 'n': '\n', 't': '\t', 'r': '\r', '\\': '\\'}[m[1]], value)
    path = Path(value)
    if path.is_absolute() or '/' in value:
        path = path if path.is_absolute() else folder / path
        return path if probe.exists(path) else None
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
                size = index.getint(directory, 'Size', fallback=ICON_SIZE)
                scale = index.getint(directory, 'Scale', fallback=1)
                kind = index.get(directory, 'Type', fallback='Threshold')
                low = index.getint(directory, 'MinSize', fallback=size) if kind == 'Scalable' else size
                high = index.getint(directory, 'MaxSize', fallback=size) if kind == 'Scalable' else size
                if kind == 'Threshold':
                    threshold = index.getint(directory, 'Threshold', fallback=2)
                    low, high = size - threshold, size + threshold
                return max(low * scale - ICON_SIZE, ICON_SIZE - high * scale, 0)
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


def decode_icon(path):
    """CPU only. GdkPixbuf supplies SVG support without starting a GUI or shell."""
    def pixels(source):
        with Image.open(source) as image:
            image.thumbnail((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
            image = image.convert('RGBA')
            return IconPixels(path, image.width, image.height, image.tobytes())
    if path.suffix.lower() != '.svg':
        return pixels(path)
    executable = shutil.which('gdk-pixbuf-thumbnailer')
    if executable is None:
        raise ValueError('SVG icon decoder is unavailable')
    with tempfile.TemporaryDirectory(prefix='melty-folder-icon-') as directory:
        output = Path(directory) / 'icon.png'
        subprocess.run([str(Path(executable).resolve()), '-s', str(ICON_SIZE), str(path), str(output)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       close_fds=False, timeout=5)
        return pixels(output)


def load_folder_icon(folder):
    """Choose the first usable Application icon; prefer a matching desktop filename."""
    probe = Probe()
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
        path = resolve_icon(value, folder, probe)
        if path is None:
            continue
        try:
            return decode_icon(path), probe.dependencies
        except (OSError, ValueError, subprocess.SubprocessError, Image.DecompressionBombError):
            continue
    return None, probe.dependencies


class FolderIconTexture(TextureId):
    def __init__(self, pixels):
        super().__init__(GL.GL_TEXTURE_2D)
        self.pixels = pixels

    def _upload(self, state):
        def create():
            previous = int(GL.glGetIntegerv(GL.GL_TEXTURE_BINDING_2D))
            texture = int(GL.glGenTextures(1))
            try:
                GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
                with tight_unpack():
                    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_SRGB8_ALPHA8,
                                    self.pixels.width, self.pixels.height, 0,
                                    GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, self.pixels.data)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
                return GLTexture(texture, GL.GL_TEXTURE_2D,
                                 (self.pixels.height, self.pixels.width), GL.GL_SRGB8_ALPHA8)
            except Exception:
                GL.glDeleteTextures([texture])
                raise
            finally:
                GL.glBindTexture(GL.GL_TEXTURE_2D, previous)
        return state.get('icon', create, lambda texture: GL.glDeleteTextures([texture.texture_id]))


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
        requested = set(folders)
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
