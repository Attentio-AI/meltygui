"""The native `@gui` code editor on a real Python file.

    .venv/bin/python examples/gui_editor.py [file.py]

Without a file it opens a scratch copy of gui_editor_sample.py. The toolbar
drives the parts that need a caller: Run executes the sample's `demo` with
live value capture, Step moves the debugger line, Search highlights a term.
Errors, import fixes, tints and the suggestion tree come from the background
`Analysis` model. Nothing is written back to the file.
"""
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

from meltygui import os_window, imgui
from meltygui.model.editor_model import Analysis
from meltygui.state.editor_state import Breakpoints
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
        self.text = PATH.read_text()
        self.analysis = Analysis(PATH, self.text)
        self.breakpoints = Breakpoints()
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
        PATH.write_text(self.text)
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
        lines = self.text.split('\n')
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
    imgui.push_item_width(160)
    _, model.search = imgui.input_text('Search', model.search, 64)
    imgui.pop_item_width()
    imgui.same_line()
    analysis = model.analysis
    imgui.text(f'{len(analysis.errors)} findings   {model.status}')
    changed, model.text = draw_editor(
        model.text, name='source', left=0., top=BAR + 36., width=width, height=height - BAR - 36 - 30, path=str(PATH),
        editable=not model.read_only, search=model.search, errors=analysis.errors, imports=analysis.imports,
        tints=analysis.tints, code_tree=analysis.tree, live=model.live, breakpoints=model.breakpoints,
        paused=model.paused, ghost='  # Tab accepts this ghost text' if model.ghost else '')
    analysis.text = model.text
    changed, model.search = draw_editor(model.search, name='find', left=0., top=height - 26., width=width,
                                        height=24., single_line=True)
