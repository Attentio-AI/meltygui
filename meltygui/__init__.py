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
from src.lsd.gl_gui.style import Style

from src.lsd.gl_gui.app import glfw_window, run, pressed, content_size, mark, persisted

if TYPE_CHECKING:   # IDE / type checkers only; never executed
    from src.lsd.gl_gui.view.core_views.text_editor import draw_text
    from src.lsd.gl_gui.view.core_views.texture_view import draw_texture
    from src.lsd.gl_gui.view.core_views.new_core_view import (
        draw_any, draw_button, draw_str, draw_float, draw_int, draw_enum, draw_dropdown,
        draw_color_picker, draw_collection_as_tabs)
    from src.lsd.gl_gui.view.core_views.columns import draw_columns, draw_rows
    from src.lsd.gl_gui.view.core_views.menu_bar import draw_menu_bar
    from src.lsd.gl_gui.view.playground.file_selector import draw_file_selector
    from src.lsd.gl_gui.view.playground.fast_file_explorer import draw_fast_file_explorer
    from src.lsd.gl_gui.view.playground.folder_files import draw_folder_files
    from src.lsd.gl_gui.view.playground.terminal_playground import draw_terminal

_NCV = 'src.lsd.gl_gui.view.core_views.new_core_view'
_VIEWS = {
    'draw_text': ('src.lsd.gl_gui.view.core_views.text_editor', 'draw_text'),
    'draw_texture': ('src.lsd.gl_gui.view.core_views.texture_view', 'draw_texture'),
    'draw_any': (_NCV, 'draw_any'),
    'draw_button': (_NCV, 'draw_button'),
    'draw_str': (_NCV, 'draw_str'),
    'draw_float': (_NCV, 'draw_float'),
    'draw_int': (_NCV, 'draw_int'),
    'draw_enum': (_NCV, 'draw_enum'),
    'draw_dropdown': (_NCV, 'draw_dropdown'),
    'draw_color_picker': (_NCV, 'draw_color_picker'),
    'draw_collection_as_tabs': (_NCV, 'draw_collection_as_tabs'),
    'draw_columns': ('src.lsd.gl_gui.view.core_views.columns', 'draw_columns'),
    'draw_rows': ('src.lsd.gl_gui.view.core_views.columns', 'draw_rows'),
    'draw_menu_bar': ('src.lsd.gl_gui.view.core_views.menu_bar', 'draw_menu_bar'),
    'draw_file_selector': ('src.lsd.gl_gui.view.playground.file_selector', 'draw_file_selector'),
    'draw_fast_file_explorer': ('src.lsd.gl_gui.view.playground.fast_file_explorer', 'draw_fast_file_explorer'),
    'draw_folder_files': ('src.lsd.gl_gui.view.playground.folder_files', 'draw_folder_files'),
    'draw_terminal': ('src.lsd.gl_gui.view.playground.terminal_playground', 'draw_terminal'),
    'draw_code_editor': ('src.lsd.gl_gui.view.playground.open_files', 'draw_code_editor'),
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


__all__ = ['Style', 'glfw_window', 'run', 'pressed', 'content_size', 'mark', 'persisted', *_VIEWS]
