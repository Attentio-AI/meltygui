"""melty — the public face of the GUI framework in src/lsd/gl_gui.

    from melty import glfw_window, draw_text, pressed

    @glfw_window
    def editor():
        changed, new = draw_text(text)

Light on import: app.py boots melty on the first @glfw_window (see its
docstring); views are resolved lazily so the heavy modules load on the
import thread, not at ``import melty``.
"""
import pathlib as _pathlib
import sys as _sys

_ROOT = str(_pathlib.Path(__file__).resolve().parent.parent)
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

from src.lsd.gl_gui.app import glfw_window, run, pressed, content_size, mark  # noqa: E402

_VIEWS = {
    'draw_text': ('src.lsd.gl_gui.view.core_views.text_editor', 'draw_text'),
    'draw_texture': ('src.lsd.gl_gui.view.core_views.texture_view', 'draw_texture'),
}


def __getattr__(name):
    spec = _VIEWS.get(name)
    if spec is None:
        raise AttributeError(name)
    import importlib
    from src.lsd.gl_gui.app import _wait_imports
    _wait_imports()
    value = getattr(importlib.import_module(spec[0]), spec[1])
    globals()[name] = value
    return value


__all__ = ['glfw_window', 'run', 'pressed', 'content_size', 'mark', 'draw_text', 'draw_texture']
