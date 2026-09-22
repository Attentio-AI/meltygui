"""A single source preview for inspection and symbol navigation."""
from pathlib import Path
from meltygui.core.core_render import render_func
from meltygui.core.conversion.dict_conversion import DictConversion

_pending = globals().get('_pending')
_requested_once = globals().get('_requested_once', False)


def open_source_preview(path, line=None, token=None):
    global _pending, _requested_once
    _pending = (str(path), line, token)
    _requested_once = True
    from meltygui.core.windowing.glfw_utils import request_render
    request_render()


def draw_pending_preview():
    """The preview window's lifecycle call, made every frame by an app's root
    body (core/runtime/app.py). Until the first open_source_preview there is
    no window to keep alive, and the code-editing views it draws with
    (view/code_view.py, the libcst stack) stay unimported."""
    global _pending
    if not _requested_once:
        return
    from meltygui.view.code_view import draw_source_preview
    request, _pending = _pending, None
    draw_source_preview(request, name='Source preview', closable=True,
                        open_requested=request is not None, width=900, height=650)


from meltygui.state.code_state import SourcePreviewState
