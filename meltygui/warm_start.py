"""Start-up shortcuts for melty apps (app.py), each worth tens of
milliseconds on the first frame. All measured 2026-09-10 on Hyprland with an
NVIDIA GPU, originally in hdr-viewer's warm_start.py. MELTY_COLD=1 skips them.

    prepare(cache)             call BEFORE ``import glfw``
    remember_glfw_library()    call after it
    cache_hinted_atlas(fm)     wrap a melty FontManager before the renderer builds
"""
import hashlib
import os
import pathlib

# --- glfw shared library -------------------------------------------------------
# PyGLFW walks ~50 candidate paths and dlopens several before settling on the
# library (48 ms). PYGLFW_LIBRARY short-circuits that, so cache what it found.

def _glfw_cache_file(cache):
    return cache / 'glfw-library'


def _hint_glfw_library(cache):
    if os.environ.get('PYGLFW_LIBRARY'):
        return
    try:
        path = _glfw_cache_file(cache).read_text().strip()
    except OSError:
        return
    if path and os.path.isfile(path):
        os.environ['PYGLFW_LIBRARY'] = path


def remember_glfw_library(cache):
    import glfw
    path = getattr(getattr(glfw, '_glfw', None), '_name', None)
    if not path or not os.path.isabs(path):
        return
    f = _glfw_cache_file(cache)
    try:
        if f.read_text().strip() == path:
            return
    except OSError:
        pass
    _write_atomic(f, path.encode())


# --- cursor theme --------------------------------------------------------------
# libwayland-cursor parses EVERY file of the theme when glfw creates the window.
# Bibata's two animated cursors (wait, progress) are 33 KB each: 130 ms for
# something a viewer never shows. Point XCURSOR at a shadow theme that symlinks
# only the theme's small cursors (< 1 MB) and inherits nothing.

CURSOR_MAX_BYTES = 1_000_000
_DEFAULT_CURSOR_PATH = ('~/.icons', '~/.local/share/icons', '/usr/share/icons', '/usr/share/pixmaps')


def _cursor_theme_dir(name):
    dirs = os.environ.get('XCURSOR_PATH', '').split(':') if os.environ.get('XCURSOR_PATH') else _DEFAULT_CURSOR_PATH
    for d in dirs:
        if not d:
            continue
        p = pathlib.Path(d).expanduser() / name / 'cursors'
        if p.is_dir():
            return p
    return None


def _shadow_cursor_theme(cache):
    name = os.environ.get('XCURSOR_THEME') or 'default'
    src = _cursor_theme_dir(name)
    if src is None:
        return
    shadow_name = f'melty-{name}'
    root = cache / 'cursor-themes'
    theme = root / shadow_name
    cursors = theme / 'cursors'
    try:
        stale = not cursors.is_dir() or cursors.stat().st_mtime < src.stat().st_mtime
        if stale:
            tmp = root / f'{shadow_name}.{os.getpid()}.tmp'
            (tmp / 'cursors').mkdir(parents=True)
            for entry in src.iterdir():
                try:
                    if entry.is_file() and entry.stat().st_size < CURSOR_MAX_BYTES:
                        (tmp / 'cursors' / entry.name).symlink_to(entry)
                except OSError:
                    pass
            (tmp / 'index.theme').write_text(f'[Icon Theme]\nName={shadow_name}\n')
            if theme.exists():
                import shutil
                shutil.rmtree(theme, ignore_errors=True)
            os.rename(tmp, theme)
    except OSError:
        return
    old_path = os.environ.get('XCURSOR_PATH')
    os.environ['XCURSOR_PATH'] = str(root) + (':' + old_path if old_path else ':' + ':'.join(_DEFAULT_CURSOR_PATH))
    os.environ['XCURSOR_THEME'] = shadow_name


# --- export --------------------------------------------------------------------

def prepare(cache):
    """Environment tweaks; must run before ``import glfw``. MELTY_COLD=1 skips them."""
    if os.environ.get('MELTY_COLD'):
        return
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _hint_glfw_library(cache)
    _shadow_cursor_theme(cache)


def cache_hinted_atlas(font_mgr, cache):
    """FontManager.hint_atlas (FreeType re-rasterising every UI glyph, ~30 ms)
    is a pure function of the stb atlas it is handed, so cache its output on
    disk keyed by those pixels (and melty's fonts.py, which holds the algorithm)."""
    if os.environ.get('MELTY_COLD'):
        return
    real = font_mgr.hint_atlas
    try:
        algo = str(os.stat(pathlib.Path(__file__).with_name('fonts.py')).st_mtime_ns).encode()
    except OSError:
        algo = b''

    def hint_atlas(width, height, pixels):
        key = hashlib.sha1(algo + b'|' + pixels).hexdigest()[:16]
        f = cache / f'atlas-{width}x{height}-{key}.rgba'
        try:
            data = f.read_bytes()
            if len(data) == width * height * 4:
                return data
        except OSError:
            pass
        data = real(width, height, pixels)
        if data is not None:
            _write_atomic(f, data)
        return data

    font_mgr.hint_atlas = hint_atlas


def _write_atomic(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        pass
