"""Per view state of the native code editor (`view/editor_view.py`)."""
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save_exclude


@no_save_exclude('editor')
class EditorState(DictConversion):
    """What survives a restart: the caret and which folds are collapsed.
    `editor` is the running session (`editor/editor.py`), a plain object the
    view reads and writes on every keystroke; it is rebuilt from the text."""

    def __init__(self):
        super().__init__()
        self.caret = 0
        self.anchor = 0
        self.collapsed = []
        self.editor = None


class Breakpoints(set):
    """Breakpoint lines (1 based) that follow edits. A model with another
    key, such as source sites, offers the same `toggle` and `moved`."""

    def toggle(self, line):
        self.symmetric_difference_update((line,))

    def moved(self, line, delta):
        """`delta` lines were added (or removed) below `line`."""
        shifted = {(at + delta if at > line else at) for at in self if not (delta < 0 and line < at <= line - delta)}
        self.clear()
        self.update(shifted)
