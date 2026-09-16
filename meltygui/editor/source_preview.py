"""A single source preview for inspection and symbol navigation."""
from pathlib import Path
from meltygui.rendering.core_render import render_func
from meltygui.state.dict_conversion import DictConversion

_pending = globals().get('_pending')


def open_source_preview(path, line=None, token=None):
    global _pending
    _pending = (str(path), line, token)
    from meltygui.utils.glfw_utils import request_render
    request_render()


from meltygui.state.code_state import SourcePreviewState


from meltygui.view.code_view import draw_source_preview


from meltygui.view.code_view import draw_pending_preview

