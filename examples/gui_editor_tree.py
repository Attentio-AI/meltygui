"""A source as text and as a tree, side by side: `draw_editor(source)` and `draw_any(source.tree)`.

    .venv/bin/python examples/gui_editor_tree.py [file.py]

Without a file it opens a scratch copy of gui_editor_sample.py. On the left the
native editor, on the right the same code as rows of keys and values
(`Source.tree`, model/tree_model.py): the names a module, a class or a function
assigns, parameters with their defaults, the items of lists, dicts and calls.
Drag a number, press a True / False chip or type into a text on the right and
the text on the left changes; type on the left and the rows follow once the
typing rests. Code that is no literal is a text field holding its source.
Nothing is written back to the file.
"""
import shutil
import sys
import tempfile
from pathlib import Path

from meltygui import os_window
from meltygui.model.source_model import Source
from meltygui.rust import draw_any
from meltygui.state.editor_state import EditorState
from meltygui.view.editor_view import draw_editor

if len(sys.argv) > 1:
    PATH = Path(sys.argv[1])
else:
    PATH = Path(tempfile.mkdtemp(prefix='melty-editor-tree-')) / 'sample.py'
    shutil.copy(Path(__file__).with_name('gui_editor_sample.py'), PATH)

BAR = 34.          # the window's title strip covers the first rows of the content
SHARE = .58        # the editor's share of the width
GAP = 8.
source = Source(PATH.read_text(), str(PATH))
state = EditorState()


@os_window(name='Source and its tree', app_id='meltygui-gui-editor-tree', width=1360, height=840, use_cache=False)
def app(width=None, height=None):
    split = width * SHARE
    draw_editor(source, name='source', state=state, left=0., top=BAR, width=split, height=height - BAR, path=str(PATH))
    # The same code as keys and values: an edit here is a splice of the source, which the editor follows.
    draw_any(source.tree, name='tree', expand=1, left=split + GAP, top=BAR + GAP, width=width - split - GAP * 2, height=height - BAR - GAP * 2)
