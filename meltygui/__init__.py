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
from meltygui.core.styling.style import Style
from meltygui.core.styling.style import default_tint_accumulation
from meltygui.core.styling.style import default_scalar_accumulation

from meltygui.core.runtime.app import boot
from meltygui.core.runtime.app import glfw_window
from meltygui.core.runtime.app import run
from meltygui.core.runtime.app import pressed
from meltygui.core.runtime.app import content_size
from meltygui.core.runtime.app import mark
from meltygui.core.runtime.app import persisted

if TYPE_CHECKING:   # IDE / type checkers only; never executed
    from meltygui.view.text_view import draw_text
    from meltygui.view.texture_view import draw_texture
    from meltygui.core.rendering.render_dispatch import draw_any
    from meltygui.view.control_view import draw_button
    from meltygui.view.control_view import draw_str
    from meltygui.view.control_view import draw_float
    from meltygui.view.control_view import draw_int
    from meltygui.view.control_view import draw_int_slider
    from meltygui.view.control_view import draw_enum
    from meltygui.view.dropdown_view import draw_dropdown
    from meltygui.view.inspection_view import draw_view_func_selector
    from meltygui.view.color_view import draw_color_picker
    from meltygui.view.collection_view import draw_collection_as_tabs
    from meltygui.view.layout_view import draw_columns
    from meltygui.view.layout_view import draw_rows
    from meltygui.view.menu_view import draw_menu_bar
    from meltygui.view.file_view import draw_file_selector
    from meltygui.view.file_view import draw_fast_file_explorer
    from meltygui.view.file_view import draw_shortcuts
    from meltygui.view.file_view import draw_breadcrumbs
    from meltygui.core.files.file_core import draw_folder_files
    from meltygui.view.terminal_view import draw_terminal

_NCV = 'meltygui.core.rendering.render_dispatch'
_VIEWS = {
    'draw_voxels': ('meltygui.view.voxel_view', 'draw_voxels'),
    'draw_voxels_opengl': ('meltygui.view.voxel_view', 'draw_voxels_opengl'),
    'draw_voxels_cuda': ('meltygui.view.voxel_view', 'draw_voxels_cuda'),
    'draw_tensor_slices': ('meltygui.view.tensor_view', 'draw_tensor_slices'),
    'draw_tensor_error': ('meltygui.view.tensor_view', 'draw_tensor_error'),
    'draw_line_graph': ('meltygui.view.graph_view', 'draw_line_graph'),
    'render_func': ('meltygui.core.core_render', 'render_func'),
    'draw_text': ('meltygui.view.text_view', 'draw_text'),
    'draw_texture': ('meltygui.view.texture_view', 'draw_texture'),
    'draw_any': (_NCV, 'draw_any'),
    'draw_button': ('meltygui.view.control_view', 'draw_button'),
    'draw_str': ('meltygui.view.control_view', 'draw_str'),
    'draw_float': ('meltygui.view.control_view', 'draw_float'),
    'draw_int': ('meltygui.view.control_view', 'draw_int'),
    'draw_int_slider': ('meltygui.view.control_view', 'draw_int_slider'),
    'draw_enum': ('meltygui.view.control_view', 'draw_enum'),
    'draw_dropdown': ('meltygui.view.dropdown_view', 'draw_dropdown'),
    'draw_view_func_selector': ('meltygui.view.inspection_view', 'draw_view_func_selector'),
    'draw_color_picker': ('meltygui.view.color_view', 'draw_color_picker'),
    'draw_collection_as_tabs': ('meltygui.view.collection_view', 'draw_collection_as_tabs'),
    'draw_columns': ('meltygui.view.layout_view', 'draw_columns'),
    'draw_rows': ('meltygui.view.layout_view', 'draw_rows'),
    'draw_menu_bar': ('meltygui.view.menu_view', 'draw_menu_bar'),
    'draw_file_selector': ('meltygui.view.file_view', 'draw_file_selector'),
    'draw_fast_file_explorer': ('meltygui.view.file_view', 'draw_fast_file_explorer'),
    'draw_shortcuts': ('meltygui.view.file_view', 'draw_shortcuts'),
    'draw_breadcrumbs': ('meltygui.view.file_view', 'draw_breadcrumbs'),
    'draw_folder_files': ('meltygui.core.files.file_core', 'draw_folder_files'),
    'draw_terminal': ('meltygui.view.terminal_view', 'draw_terminal'),
}


def __getattr__(name):
    if name == 'imgui':
        import meltygui_imgui
        return meltygui_imgui
    if name == 'toggles':
        from meltygui.core.runtime.toggles import Toggles
        return Toggles
    if name == 'window_api':
        import meltygui.core.windowing.window_api as window_api
        return window_api
    spec = _VIEWS.get(name)
    if spec is None:
        raise AttributeError(name)
    import importlib
    from meltygui.core.runtime.app import _wait_imports
    _wait_imports()
    value = getattr(importlib.import_module(spec[0]), spec[1])
    globals()[name] = value
    return value


__all__ = ['Style', 'boot', 'glfw_window', 'run', 'pressed', 'content_size', 'mark', 'persisted', 'toggles', 'window_api', 'imgui',
           *_VIEWS]
