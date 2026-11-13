"""Per view state of the native code editor (`view/editor_view.py`)."""
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save_exclude


class Loading:
    """The input of an editor whose text is still on its way."""

    def __repr__(self):
        return 'LOADING'


# Pass this as `input_value` while the real text loads: the editor paints
# the rows it showed last session from its state's viewport snapshot.
LOADING = Loading()


@no_save_exclude('editor')
class EditorState(DictConversion):
    """What survives a restart: the caret, which folds are collapsed in each
    fold layer, and a snapshot of the viewport. `editor` is the running
    session (`editor/editor.py`), a plain object the view reads and writes on
    every keystroke; it is rebuilt from the text."""

    def __init__(self):
        super().__init__()
        self.caret = 0
        self.anchor = 0
        # Collapsed folds, per layer: the scope layer by name (it survives
        # edits and restarts), the caller's layer as (head, last) lines.
        self.collapsed = []
        self.gaps = []
        # The viewport snapshot: the text of rows `view_first ..` as they
        # were shown (folded lines left out), of `view_rows` rows in all,
        # and the file line of each (None: a row without a number). With
        # `LOADING` as input the editor paints exactly these rows where they
        # were, so the first frame of a session shows code before any file
        # is read.
        self.view_first = 0
        self.view_rows = 0
        self.view_text = ''
        self.view_numbers = []
        self.editor = None

    @property
    def pane(self):
        """The running editor's handle (`editor.Pane`): commands in, geometry
        out. None before the view first ran."""
        return self.editor.handle if self.editor is not None else None
