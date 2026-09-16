"""Compatibility entry points for the file feature.

Implementation lives in model/file_model.py, view/file_view.py and
core/files/file_core.py. Keep registered window and lifecycle identities here so
existing applications and saved sessions retain their entry points.
"""
from meltygui.core.files import file_core
from meltygui.core.melty import Melty
from meltygui.core.rendering.modes import Modes
from meltygui.core.core_render import render_func
from meltygui.core.rendering.window_decoration import window

from meltygui.core.files.file_core import ROOT
from meltygui.core.files.file_core import TEST_FOLDER
from meltygui.core.files.file_core import files_proxy
from meltygui.core.files.file_core import test_folder_proxy
from meltygui.core.files.file_core import _disk_trees
from meltygui.core.files.file_core import _window_dss
from meltygui.core.files.file_core import _proxies
from meltygui.core.files.file_core import _poller_running
from meltygui.core.files.file_core import folder_io
from meltygui.core.files.file_core import folder_proxy
from meltygui.core.files.file_core import watch_folder
from meltygui.core.files.file_core import _draw_tree
from meltygui.core.files.file_core import _poll_loop
from meltygui.model.file_model import _scan
from meltygui.model.file_model import _create
from meltygui.model.file_model import _delete
from meltygui.model.file_model import _reconcile
from meltygui.model.file_model import _collect
from meltygui.model.file_model import _file_meta
from meltygui.model.file_model import _apply_meta
from meltygui.model.file_model import _collect_meta


@Melty.on_load
def _init_file_meta(vis, root):
    return file_core._init_file_meta(vis, root)


@window(disable_scroll=False, use_cache=True, tint=(0.18, 0.11, 0.11))
def file_meta_debug(_, draw_state=None):
    return file_core.file_meta_debug(_, draw_state)


@window(input_value=files_proxy, tint=(0.36, 0.46, 0.59), disable_scroll=False, mode=Modes.WINDOW)
@render_func(show_bg=True, use_cache=True, shadow=True, selectable=False)
def draw_folder_files(input_value, draw_state, **kwargs):
    _draw_tree(input_value, draw_state, ROOT)
    return False, None


@window(input_value=test_folder_proxy, tint=(0.84, 0.933, 0.98), bg_offset=4, disable_scroll=False, mode=Modes.WINDOW)
@render_func(show_bg=False, use_cache=True, selectable=False)
def draw_test_folders(input_value, draw_state, **kwargs):
    _draw_tree(input_value, draw_state, TEST_FOLDER)
    return False, None

from meltygui.view.file_view import draw_file_tree
from meltygui.view.file_view import draw_file_metadata
