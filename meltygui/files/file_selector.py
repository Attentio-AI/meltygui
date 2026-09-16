"""File selection using the same explorer as the standalone file browser."""
from pathlib import Path

import meltygui_imgui as imgui

from meltygui.core.app import pressed

from meltygui.core.dict_conversion import DictConversion
from meltygui.core.core_render import render_func


from meltygui.state.file_state import FileSelectorState


from meltygui.view.file_view import draw_file_selector
