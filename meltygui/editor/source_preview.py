"""A single source preview for inspection and symbol navigation."""
from pathlib import Path
from meltygui.core.core_render import render_func
from meltygui.core.conversion.dict_conversion import DictConversion

_pending = globals().get('_pending')


def open_source_preview(path, line=None, token=None):
    global _pending
    _pending = (str(path), line, token)
    from meltygui.core.windowing.glfw_utils import request_render
    request_render()


from meltygui.state.code_state import SourcePreviewState
