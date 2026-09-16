"""Current imports are explicit; saved-name migration is not an import hook."""
import subprocess
import sys


def test_legacy_names_do_not_import_in_a_fresh_process():
    result = subprocess.run([sys.executable, '-c', '''
import importlib
import meltygui

for name in (
    'meltygui.core.module_compatibility',
    'meltygui.core.input_handler',
    'meltygui.events.input_handler',
    'meltygui.rendering.core_render',
    'meltygui.views.headers',
    'meltygui.widgets.file_tree',
    'meltygui.windows.backends.native_wayland',
    'meltygui.files.file_selector',
    'meltygui.files.folder_files',
    'meltygui.debug.jump_to',
):
    try:
        importlib.import_module(name)
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError(f"Obsolete import is still available: {name}")

from meltygui.core.input.input_handler import InputHandler
from meltygui.core.core_render import render_func
from meltygui.view.header_view import draw_header
from meltygui.core.windowing.backends.native_wayland import Backend
assert callable(render_func) and callable(draw_header)
'''], close_fds=False, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
