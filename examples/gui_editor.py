"""The native `@gui` code editor on a real Python file.

    .venv/bin/python examples/gui_editor.py [file.py]

Without a file it opens a scratch copy of gui_editor_sample.py. The toolbar
drives the parts that need a caller: Run executes the sample's `demo` with
live value capture, Step moves the debugger line. Ctrl+F in the editor opens
its own find bar (Enter and Shift+Enter step, Escape closes); the toolbar's
Search is the other way, a caller's own field: its term goes in as `search=`,
the arrows step with `state.pane.find`, the count is `state.pane.search`.
The text is a `Source`: the editor edits it in place and its undefined names
are read with it. Split shows the same source in a second pane: each has its
own caret, folds and scroll, and follows what is typed in the other. Syntax errors, call checks, import fixes and tints come
from the background `Analysis` model. Ctrl+B goes from a use to its definition and from a
definition's name to its uses in this text (the picker lists several); it needs no caller part
for one file (examples/gui_editor_roster.py gives the editors a project). Nothing is written back to the file.
"""
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

from meltygui import os_window, imgui
from meltygui.model.editor_model import Analysis
from meltygui.model.source_model import Source
from meltygui.state.editor_state import EditorState
from meltygui.view.editor_view import draw_editor
from meltygui.editor.inline import inline
from meltygui.hdr_color import pack_color

if len(sys.argv) > 1:
    PATH = Path(sys.argv[1])
else:
    PATH = Path(tempfile.mkdtemp(prefix='melty-editor-')) / 'sample.py'
    shutil.copy(Path(__file__).with_name('gui_editor_sample.py'), PATH)


class Model:
    """What the app owns: the text and the caller side of each feature."""
    def __init__(self):
        self.source = Source(PATH.read_text(), str(PATH))     # the text: the editor edits it in place
        self.analysis = Analysis(self.source)
        self.state = EditorState()      # the caller's handle on the editor: `state.pane`
        self.second = EditorState()     # the second pane on the same source (Split)
        self.split = False
        self.breakpoints = {}      # {source site key: {'enabled': True}}, toggled by gutter clicks
        self.paused = None
        self.live = None
        self.search = ''
        self.read_only = False
        self.ghost = False
        self.status = ''

    def run(self):
        """Run `demo` from the current text and keep its live values."""
        from meltygui.code import live_view
        from meltygui.code.live_instrument import run_instrumented
        PATH.write_text(self.source.text)
        try:
            spec = importlib.util.spec_from_file_location('editor_sample', PATH)
            module = importlib.util.module_from_spec(spec)
            sys.modules['editor_sample'] = module
            spec.loader.exec_module(module)
            live_view.ACCUMULATE_UNWATCHED = True
            self.status = f'demo() returned {run_instrumented(module.demo)!r}'
            self.live = module.demo
        except Exception as error:
            self.status = f'{type(error).__name__}: {error}'

    def step(self):
        """Stand in for a debugger: pause on the next line of `demo`."""
        lines = self.source.text.split('\n')
        head = next((at for at, line in enumerate(lines) if line.startswith('def demo')), None)
        if head is None:
            return
        last = next((at for at in range(head + 1, len(lines)) if lines[at][:1] not in (' ', '')), len(lines)) - 1
        at = head + 2 if self.paused is None else self.paused
        at += 1
        self.paused = at if at <= last else None


model = Model()
BAR = 34


@inline(bool)
def lamp(input_value, width=0., height=0.):
    """Live booleans show as a lamp: one small function adds an inline widget."""
    dl = imgui.get_window_draw_list()
    color = pack_color(.35, .85, .45) if input_value else pack_color(.85, .35, .35)
    dl.add_circle_filled(height * .5, height * .5, height * .3, color)
    dl.add_text(height, 2, color, 'on' if input_value else 'off')


@os_window(name='Native code editor', app_id='meltygui-gui-editor', width=1100, height=820, use_cache=False)
def app(width=None, height=None):
    # Lever: the window's title strip covers the first rows of the content.
    imgui.set_cursor_pos((8, BAR + 6))
    if imgui.button('Run demo()'):
        model.run()
    imgui.same_line()
    if imgui.button('Step'):
        model.step()
    imgui.same_line()
    _, model.read_only = imgui.checkbox('Read only', model.read_only)
    imgui.same_line()
    _, model.ghost = imgui.checkbox('Ghost', model.ghost)
    imgui.same_line()
    _, model.split = imgui.checkbox('Split', model.split)
    imgui.same_line()
    imgui.push_item_width(160)
    _, model.search = imgui.input_text('Search', model.search, 64)
    imgui.pop_item_width()
    imgui.same_line()
    pane = model.state.pane
    if imgui.button('<') and pane is not None:
        pane.find(-1)
    imgui.same_line()
    if imgui.button('>') and pane is not None:
        pane.find(1)
    imgui.same_line()
    found = pane.search if pane is not None and model.search else None
    imgui.text(f'{found.index} of {found.count}  ' if found is not None else '')
    imgui.same_line()
    analysis = model.analysis
    imgui.text(f'{len(analysis.errors)} findings   {model.status}')
    # Lever: the first pane's share of the width when split.
    first = width * .5 if model.split else width
    draw_editor(
        model.source, name='source', state=model.state, left=0., top=BAR + 36., width=first, height=height - BAR - 40, path=str(PATH),
        editable=not model.read_only, search=model.search, errors=analysis.errors, imports=analysis.imports,
        tints=analysis.tints, live=model.live, breakpoints=model.breakpoints,
        paused=model.paused, ghost='  # Tab accepts this ghost text' if model.ghost else '')
    if model.split:
        # The same source in another session: nothing is copied, each edit reaches both.
        draw_editor(
            model.source, name='second', state=model.second, left=first + 4., top=BAR + 36., width=width - first - 4.,
            height=height - BAR - 40, path=str(PATH), editable=not model.read_only, errors=analysis.errors,
            imports=analysis.imports, tints=analysis.tints, breakpoints=model.breakpoints)
    analysis.feed()
