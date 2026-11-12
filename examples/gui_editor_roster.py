"""Tints and lookups across the files of a project, in the native `@gui` editor.

    .venv/bin/python examples/gui_editor_roster.py

The sample project (examples/gui_editor_roster_sample) is copied to a temp folder. Two
of its files are open, each a `Source` joined to the project's roster
(`roster_model.project`): shapes.py on the left, scene.py on the right.
palette.py is on disk only. scene.py states no tint: every wash in it is the
tint of a definition in another file, found through its imports (`import`,
`from .. import .. as`, a star import) or, for `rain`, which nothing imports,
by its one definition in the project.

Type in the left editor (change the tint after `class Circle`, rename it):
the right one follows, nothing is saved. "Next sky tint" gives palette.py
another tint as an unsaved text, the way a `PendingSave` event does in an
app: both editors follow. Ctrl+B on a name goes to its definition, on a
definition's name to its uses (several: a picker; Enter or a click jumps,
Escape closes). The editors are given the roster, so the places are the
project's; one in the other open file moves the caret and the keyboard to
that pane (`show`), one in palette.py is named in the status line.
"Look up" asks the roster the same question for the right editor's caret
and shows the answer as text. Both are read on a pool thread.
"""
import itertools
import shutil
import tempfile
from pathlib import Path

from meltygui import os_window, imgui
from meltygui.model.editor_model import Analysis
from meltygui.model.roster_model import project
from meltygui.model.source_model import Source
from meltygui.state.editor_state import EditorState
from meltygui.view.editor_view import draw_editor

# Lever: the tints "Next sky tint" goes through.
SKIES = [(0.9, 0.2, 0.3), (0.2, 0.8, 0.3), (0.95, 0.75, 0.1), (0.2, 0.5, 0.9)]
FOLDER = Path(tempfile.mkdtemp(prefix='melty-roster-')).resolve() / 'project'
shutil.copytree(Path(__file__).with_name('gui_editor_roster_sample'), FOLDER)
PALETTE = (FOLDER / 'palette.py').read_text()


class Buffer:
    """One open file: its text, the editor's state on it, its analysis."""
    def __init__(self, name, roster):
        self.path = FOLDER / name
        self.source = Source(self.path.read_text(), str(self.path))
        roster.join(self.source)
        self.analysis = Analysis(self.source, roster)
        self.state = EditorState()


class Model:
    def __init__(self):
        self.roster = project(FOLDER / 'scene.py')     # one per project, kept by Melty's runtime
        self.left, self.right = Buffer('shapes.py', self.roster), Buffer('scene.py', self.roster)
        self.skies = itertools.cycle(SKIES)
        self.pushed = itertools.count(1)
        self.status = 'Ctrl+B on a name: its definition, or the uses of a definition.'

    def repaint(self):
        """palette.py gets another sky tint as an unsaved text: only the roster is told."""
        tint = next(self.skies)
        self.roster.push(str(FOLDER / 'palette.py'), PALETTE.replace('(0.2, 0.5, 0.9)', str(tint)), next(self.pushed))
        self.status = f'pushed palette.py with sky tint {tint}'

    def look(self):
        """Ask what the name at the right editor's caret leads to (off this thread: it may read files)."""
        from meltygui.core.runtime.background import Background
        from meltygui.core.windowing.glfw_utils import request_render
        buffer = self.right
        line, column = buffer.source.native.place(buffer.state.caret)

        def ask():
            found = self.roster.lookup(buffer.source, line, column)
            if found is None:
                self.status = f'line {line + 1}, column {column}: no symbol of the project'
            else:
                places = ', '.join(f'{Path(path).name}:{at + 1}{" (guess)" if guess else ""}' for path, at, _, _, guess in found[1][:6])
                self.status = f'{"used at" if found[0] else "defined at"} {places or "nowhere"}' + (f' and {len(found[1]) - 6} more' if len(found[1]) > 6 else '')
            request_render()
        Background.submit_io(ask)


model = Model()
BAR = 34


def show(path, line, column):
    """Ctrl+B led out of the editor it was pressed in (`on_open`): jump in the pane that shows the
    file, or say where the place is. One function for both editors: a view runs again when the
    callable it is given is another object, as a bound method taken anew every frame is."""
    buffer = next((buffer for buffer in (model.left, model.right) if str(buffer.path) == path), None)
    if buffer is None:
        model.status = f'{Path(path).name}:{line} is not open here'
        return
    buffer.state.pane.jump(line, column=column)
    buffer.state.pane.focus()
    model.status = f'{Path(path).name}:{line}'


@os_window(name='Roster: tints and lookups across files', app_id='meltygui-gui-editor-roster', width=1500, height=640, use_cache=False)
def app(width=None, height=None):
    # Lever: the window's title strip covers the first rows of the content.
    imgui.set_cursor_pos((8, BAR + 6))
    if imgui.button('Next sky tint'):
        model.repaint()
    imgui.same_line()
    if imgui.button('Look up'):
        model.look()
    imgui.same_line()
    imgui.text(model.status)
    half = width / 2
    for at, buffer in enumerate((model.left, model.right)):
        analysis = buffer.analysis
        draw_editor(buffer.source, name=buffer.path.name, state=buffer.state, left=at * half, top=BAR + 36., width=half - 4, height=height - BAR - 40,
                    path=str(buffer.path), errors=analysis.errors, imports=analysis.imports, tints=analysis.tints,
                    roster=model.roster, on_open=show)
