"""MeltyGUI: immediate-mode apps, live code editing, HDR and tensor views.

Install with ``uv pip install meltygui``; define windows with ``@glfw_window``.
Importing this package does not start a GUI, and it stays light: the views
below import on first use, the render libraries (imgui, numpy, OpenGL) and
the GL side of the runtime load on the boot's import thread while the first
``@glfw_window`` opens the display, and the code-editing stack (libcst) loads
with the first code edit. core/runtime/app.py describes the boot sequence;
tests/test_startup_imports.py keeps the import graph that shape.
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
from meltygui.core.runtime.app import embedded_imports
from meltygui.core.runtime.app import glfw_window
from meltygui.core.runtime.app import run
from meltygui.core.runtime.app import pressed
from meltygui.core.runtime.app import content_size
from meltygui.core.runtime.app import mark
from meltygui.core.runtime.app import persisted
from meltygui.core.runtime.app import after_first_frame
from meltygui.core.runtime.app import checkpoint

if TYPE_CHECKING:   # IDE / type checkers only; never executed
    import meltygui_imgui as imgui
    from meltygui.core.rendering.gui_prototype import gui, os_window
    from meltygui.state.new_core_model import DrawState
    from meltygui.model.tile_model import multi_instance
    from meltygui.view.chat_view import draw_claude_chat
    from meltygui.core.rendering.render_dispatch import draw_any
    from meltygui.view.menu_view import draw_menu_bar
    from meltygui.view.file_view import draw_breadcrumbs

# Non-render helpers keep explicit locations; @render_func views are discovered.
_LAZY_EXPORTS = {
    'columns': ('meltygui.core.rendering.gui_layout_prototype', 'columns'),
    'rows': ('meltygui.core.rendering.gui_layout_prototype', 'rows'),
    'gui': ('meltygui.core.rendering.gui_prototype', 'gui'),
    'os_window': ('meltygui.core.rendering.gui_prototype', 'os_window'),
    'DrawState': ('meltygui.state.new_core_model', 'DrawState'),
    'render_func': ('meltygui.core.core_render', 'render_func'),
    'multi_instance': ('meltygui.model.tile_model', 'multi_instance'),
    'draw_claude_chat': ('meltygui.view.chat_view', 'draw_claude_chat'),
    'draw_any': ('meltygui.core.rendering.render_dispatch', 'draw_any'),
    'draw_menu_bar': ('meltygui.view.menu_view', 'draw_menu_bar'),
    'draw_breadcrumbs': ('meltygui.view.file_view', 'draw_breadcrumbs'),
}


def __getattr__(name):
    # A submodule can be in sys.modules before importlib binds it on its parent.
    module = sys.modules.get(f'{__name__}.{name}')
    if module is not None:
        return module
    if name == 'imgui':
        import meltygui_imgui
        return meltygui_imgui
    if name == 'toggles':
        from meltygui.core.runtime.toggles import Toggles
        return Toggles
    if name == 'window_api':
        import meltygui.core.windowing.window_api as window_api
        return window_api
    if name == 'rust':
        # The `@gui` draw functions (`meltygui.rust.draw_any`): a table of names, loaded when asked for.
        import meltygui.rust as rust
        return rust
    if name == '__all__':
        value = list(dict.fromkeys([*_PUBLIC_NAMES, *_LAZY_EXPORTS, *_render_exports()]))
        globals()[name] = value
        return value
    spec = _LAZY_EXPORTS.get(name)
    if spec is None:
        spec = _render_exports().get(name)
    if spec is None:
        raise AttributeError(name)
    import importlib
    from meltygui.core.runtime.app import _wait_imports
    _wait_imports()
    value = getattr(importlib.import_module(spec[0]), spec[1])
    globals()[name] = value
    return value


_PUBLIC_NAMES = [
    'Style', 'boot', 'embedded_imports', 'glfw_window', 'run', 'pressed',
    'content_size', 'mark', 'persisted', 'after_first_frame', 'checkpoint',
    'toggles', 'window_api', 'imgui', 'rust',
]


def _render_exports():
    from meltygui.core.runtime.public_exports import render_exports
    return render_exports()


def __dir__():
    return sorted(set(globals()) | set(_PUBLIC_NAMES) | set(_LAZY_EXPORTS) | set(_render_exports()))
