"""MeltyGUI: immediate-mode apps, live code editing, HDR and tensor views.

Install with ``uv pip install meltygui``; define windows with ``@glfw_window``.
Renderers are imported lazily, so importing this package does not start a GUI.
"""
import os
import sys

# Wayland windows use EGL with either backend. Select PyOpenGL's dispatch before
# importing renderer code, including extensions that use Melty eagerly.
if sys.platform.startswith('linux') and (os.environ.get('WAYLAND_DISPLAY') or os.environ.get('WAYLAND_SOCKET')):
    os.environ.setdefault('PYOPENGL_PLATFORM', 'egl')

from typing import TYPE_CHECKING
from meltygui.style import Style
from meltygui.style import default_tint_accumulation
from meltygui.style import default_scalar_accumulation

from meltygui.app import boot
from meltygui.app import glfw_window
from meltygui.app import run
from meltygui.app import pressed
from meltygui.app import content_size
from meltygui.app import mark
from meltygui.app import persisted

if TYPE_CHECKING:   # IDE / type checkers only; never executed
    from meltygui.editor.text_editor import draw_text
    from meltygui.views.texture_view import draw_texture
    from meltygui.views.new_core_view import draw_any
    from meltygui.views.new_core_view import draw_button
    from meltygui.views.new_core_view import draw_str
    from meltygui.views.new_core_view import draw_float
    from meltygui.views.new_core_view import draw_int
    from meltygui.views.new_core_view import draw_enum
    from meltygui.views.new_core_view import draw_dropdown
    from meltygui.views.new_core_view import draw_view_func_selector
    from meltygui.views.new_core_view import draw_color_picker
    from meltygui.views.new_core_view import draw_collection_as_tabs
    from meltygui.views.columns import draw_columns
    from meltygui.views.columns import draw_rows
    from meltygui.views.menu_bar import draw_menu_bar
    from meltygui.files.file_selector import draw_file_selector
    from meltygui.files.fast_file_explorer import draw_fast_file_explorer
    from meltygui.files.fast_file_explorer import draw_shortcuts
    from meltygui.files.folder_files import draw_folder_files
    from meltygui.widgets.terminal_playground import draw_terminal

_NCV = 'meltygui.views.new_core_view'
_VIEWS = {
    'draw_voxels': ('meltygui.tensor.voxel_playground', 'draw_voxels'),
    'draw_line_graph': ('meltygui.views.line_graph_playground', 'draw_line_graph'),
    'render_func': ('meltygui.rendering.core_render', 'render_func'),
    'draw_text': ('meltygui.editor.text_editor', 'draw_text'),
    'draw_texture': ('meltygui.views.texture_view', 'draw_texture'),
    'draw_any': (_NCV, 'draw_any'),
    'draw_button': (_NCV, 'draw_button'),
    'draw_str': (_NCV, 'draw_str'),
    'draw_float': (_NCV, 'draw_float'),
    'draw_int': (_NCV, 'draw_int'),
    'draw_enum': (_NCV, 'draw_enum'),
    'draw_dropdown': (_NCV, 'draw_dropdown'),
    'draw_view_func_selector': (_NCV, 'draw_view_func_selector'),
    'draw_color_picker': (_NCV, 'draw_color_picker'),
    'draw_collection_as_tabs': (_NCV, 'draw_collection_as_tabs'),
    'draw_columns': ('meltygui.views.columns', 'draw_columns'),
    'draw_rows': ('meltygui.views.columns', 'draw_rows'),
    'draw_menu_bar': ('meltygui.views.menu_bar', 'draw_menu_bar'),
    'draw_file_selector': ('meltygui.files.file_selector', 'draw_file_selector'),
    'draw_fast_file_explorer': ('meltygui.files.fast_file_explorer', 'draw_fast_file_explorer'),
    'draw_shortcuts': ('meltygui.files.fast_file_explorer', 'draw_shortcuts'),
    'draw_folder_files': ('meltygui.files.folder_files', 'draw_folder_files'),
    'draw_terminal': ('meltygui.widgets.terminal_playground', 'draw_terminal'),
}


def __getattr__(name):
    if name == 'imgui':
        import meltygui_imgui
        return meltygui_imgui
    if name == 'toggles':
        from meltygui.toggles import Toggles
        return Toggles
    if name == 'window_api':
        import meltygui.window_api as window_api
        return window_api
    spec = _VIEWS.get(name)
    if spec is None:
        raise AttributeError(name)
    import importlib
    from meltygui.app import _wait_imports
    _wait_imports()
    value = getattr(importlib.import_module(spec[0]), spec[1])
    globals()[name] = value
    return value


__all__ = ['Style', 'boot', 'glfw_window', 'run', 'pressed', 'content_size', 'mark', 'persisted', 'toggles', 'window_api', 'imgui',
           *_VIEWS]
