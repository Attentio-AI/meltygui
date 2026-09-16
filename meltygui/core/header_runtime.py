import colorsys
import os
import sys
import types
from types import NoneType
from typing import MutableMapping

import meltygui.core.window_api as glfw
import meltygui_imgui as imgui
from meltygui.hdr_color import pack_color
from meltygui.core.style import Style
from meltygui_imgui.core import _DrawList

import meltygui.core.mouse_cursor as mouse_cursor
from meltygui.core.global_style import GlobalStyle
from meltygui.core.melty import Melty
from meltygui.core.melty import add_to_collection
from meltygui.state.core_enums import ProfileMode
from meltygui.state.new_core_model import TileMode
from meltygui.core.render_funcs import RenderFuncs
from meltygui.core.toggles import Toggles
from meltygui.core.toggles import Tint
from meltygui.utils.render_utils import push_style_var
from meltygui.utils.render_utils import push_style_color
from meltygui.utils.render_utils import pop_style_color
from meltygui.utils.render_utils import pop_style_var
from meltygui.core.glfw_utils import request_render
from meltygui.core.glfw_utils import print_stack_trace
from meltygui.core.bubbling import _BubblingDict
from meltygui.core.cursor_core import same_line
from meltygui.core.tile_cache import add_shadow
from meltygui.core.window_decoration import window


def open_file(path, app=None):
    def default_file_manager():
        # Detect platform
        if sys.platform.startswith('darwin'):
            return "open"
        elif os.name == 'nt':
            return "explorer"
        elif os.name == 'posix':
            return "nemo"

    if app is None:
        app = default_file_manager()

    import subprocess
    if os.path.exists(path):
        subprocess.Popen([app, path])
    else:
        print(f"Path does not exist: {path}")



from meltygui.model.collection_model import annotation_item_type


from meltygui.view.header_view import render_search


def _brightness_clamp_fn():
    """Compatibility accessor for the shared color model."""
    from meltygui.model.color_model import _brightness_clamp
    return _brightness_clamp


# (r, g, b, hovered) → label rgb for flat_button's explicit text_color path
_TEXT_COLOR_MEMO = globals().get("_TEXT_COLOR_MEMO", {})


from meltygui.view.header_view import flat_button
flat_button = window(tint=(0.0, 0.335, 0.772, 1.0))(flat_button)


from meltygui.core.header_core import _jump_to_view_source


from meltygui.view.header_view import draw_header_arrow


from meltygui.view.header_view import draw_header


from meltygui.view.header_view import draw_footer



from meltygui.view.header_view import draw_header_end
