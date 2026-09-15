"""A single source preview for inspection and symbol navigation."""
from pathlib import Path
from meltygui.rendering.core import render_func
from meltygui.state.object import DictConversion

_pending = globals().get('_pending')


def open_source_preview(path, line=None, token=None):
    global _pending
    _pending = (str(path), line, token)
    from meltygui.utils.glfw_utils import request_render
    request_render()


class SourcePreviewState(DictConversion):
    def __init__(self):
        super().__init__()
        self.path = None
        self.line = None
        self.token = None


@render_func
def draw_source_preview(input_value=None, draw_state=None, preview: SourcePreviewState = None):
    from meltygui.editor.text import draw_text
    from meltygui.runtime import Melty
    from meltygui.code.new_converters import code_hosts_for
    if input_value is not None:
        preview.path, preview.line, preview.token = input_value
    if preview.path is None:
        return False, input_value
    path = Path(preview.path)
    text = Melty.read_code(path)
    if text is None:
        text = ''
    code_dict, dict_host = None, None
    if path.suffix == '.py':
        _, dict_host = code_hosts_for(path)
        code_dict = dict_host._held()
    from meltygui.editor.source_ui import _RowSpan
    _, _, pane = draw_text(text, name='source', editable=False, code_dict=code_dict, return_extras=True,
              jump_to=_RowSpan(0, path), show_header=False, width=draw_state.width,
              height=draw_state.height, show_widgets=True)
    if preview.line is not None and pane is not None and text:
        from meltygui.editor.text import fold_project_jump
        lines = text.split('\n')
        row = max(0, min(int(preview.line) - 1, len(lines) - 1))
        offset = sum(len(line) + 1 for line in lines[:row])
        if preview.token and preview.token in lines[row]:
            offset += lines[row].index(preview.token)
        offset, row = fold_project_jump(pane, text, offset, row)
        pane.text_cursor_pos = offset
        pane.text_selection_start = pane.text_selection_end = offset
        pane.invalidate()
        preview.line = None
        from meltygui.utils.glfw_utils import request_render
        request_render()
    return False, input_value


def draw_pending_preview():
    global _pending
    request, _pending = _pending, None
    draw_source_preview(request, name='Source preview', closable=True,
                        open_requested=request is not None, width=900, height=650)

