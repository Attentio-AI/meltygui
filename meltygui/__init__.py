"""melty — the public face of the GUI framework in src/lsd/gl_gui.

    from melty import glfw_window, draw_text, pressed

    @glfw_window
    def editor():
        changed, new = draw_text(text)

Install it into the interpreter your app runs on (editable, so the checkout
is live and IDEs resolve the import):

    pip install -e /path/to/latent-descent

Light on import: app.py boots melty on the first @glfw_window (see its
docstring); views are resolved lazily so the heavy modules load on the
import thread, not at ``import melty``. The TYPE_CHECKING block below gives
IDEs and type checkers the real definitions for completion.
"""
from typing import TYPE_CHECKING

from src.lsd.gl_gui.app import glfw_window, run, pressed, content_size, mark

if TYPE_CHECKING:   # IDE / type checkers only; never executed
    from src.lsd.gl_gui.view.core_views.text_editor import draw_text
    from src.lsd.gl_gui.view.core_views.texture_view import draw_texture

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
