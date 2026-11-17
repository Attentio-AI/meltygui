"""`Source(text, path)` and `Source(thing)` on the same code, side by side.

    .venv/bin/python examples/gui_source_side_by_side.py

Both halves show a scratch copy of gui_editor_sample.py (in a temp folder, printed at launch):
its tree above (`draw_any`), its text below (`draw_editor`).

Left, `Source(text, path)`: a text of its own. The example reads the file and writes it: an edit
stays in memory until Save, and a change on disk shows after Load.

Right, `Source(sample)`: the code of the module. Drag a number, flip a chip or type, and the file
on disk has it; change the file in another program (or press Save on the left) and both the tree
and the text here follow. There is nothing to save.
"""
import importlib.util
import shutil
import tempfile
from pathlib import Path

from meltygui import os_window, imgui
from meltygui.model.source_model import Source
from meltygui.rust import draw_any
from meltygui.state.editor_state import EditorState
from meltygui.view.editor_view import draw_editor

PATH = Path(tempfile.mkdtemp(prefix='melty-source-')) / 'sample.py'
shutil.copy(Path(__file__).with_name('gui_editor_sample.py'), PATH)
spec = importlib.util.spec_from_file_location('source_sample', PATH)
sample = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sample)
print(f'The file both halves show: {PATH}')

old = Source(PATH.read_text(), str(PATH))      # the caller reads the file, and writes it (Save)
new = Source(sample)                           # the code of the module: its file, kept by the framework
states = EditorState(), EditorState()

BAR = 40.          # height of the toolbar
GAP = 8.
TREE = .45         # the trees' share of the height


@os_window(name='Source(text, path) and Source(thing)', app_id='meltygui-gui-source-side-by-side', width=1500, height=900,
           use_cache=False)
def app(width=None, height=None):
    half = width * .5
    imgui.set_cursor_pos((GAP, imgui.get_cursor_pos_y() + 10.))
    imgui.text('Source(text, path)')
    imgui.same_line()
    if imgui.button('Save'):
        PATH.write_text(old.text)
    imgui.same_line()
    if imgui.button('Load'):
        old.text = PATH.read_text()
    imgui.same_line(half + GAP)
    imgui.text(f'Source(sample)    the file is {PATH}')
    split = BAR + (height - BAR) * TREE
    for at, (source, tree) in enumerate(((old, old.tree), (new.file, new.tree))):
        left = at * half + GAP
        draw_any(tree, name=f'tree {at}', expand=1, left=left, top=BAR, width=half - GAP * 2, height=split - BAR - GAP)
        draw_editor(source, name=f'text {at}', state=states[at], left=at * half, top=split, width=half - GAP * at,
                    height=height - split, path=str(PATH))
