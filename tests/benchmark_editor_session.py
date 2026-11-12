"""Frame stutters of the native editor in a whole scripted session.

    python tests/benchmark_editor_session.py [--lines 1000,10000,100000] [--off spelling,roster] [--json out.json] [file.py ...]

Run on a reserved desktop (`WAYLAND_DISPLAY` of the agent seat): each size is
its own process with a real window in the real frame loop (`app.run`), and
everything an editor has on: a `Source` with lint, `Analysis`, the project's
roster, spell check, breakpoints and two panes on the source. The script
(`session`) types in bursts with rests, presses Enter, jumps, searches,
scrolls, folds, searches in the find bar and looks names up (Ctrl+B); input arrives as the window system's events do (the native
Wayland window's callbacks, so it needs native Wayland windows), one step per
frame. Each file gets its own empty XDG folders and file-metadata store, and
is launched once unmeasured first: a launch then finds the font atlas and the
start-up hints, as every launch but an app's very first does.

What is measured: the time from when a frame is due until its buffer is
handed over (`Surface.frame`, with the swap call). The window's swap interval
is 0, so the swap does not wait for the display, and the frames are paced at
120 a second here: a frame is due when that wait ends. Waiting for the
interpreter lock is then in the frame, wherever it happens. Frames run all
the time, also while the script rests: an idle frame costs little, so one
over the budget then is a worker holding the interpreter lock.
tests/benchmark_editor.py has no workers and no swap wait, so it shows what a
frame's own work costs and none of this.

Per phase: frames, median, p95, p99 and worst in ms, and how many were over
the 8.33 ms of 120 fps; `--runs` repeats each size and reports every run.
To attribute a stutter turn one part off (`--off`, see `PARTS`), or run a
child under a sampler: `py-spy record --gil -- python tests/benchmark_editor_session.py --child file.py`.
To add an interaction, add a phase to `session` and its name to `PHASES`.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BUDGET = 1000 / 120
# Levers: frames between two key presses of a burst (4: 30 a second, a held key), and seconds of rest:
# after launch, after a phase (the workers' quiet times pass in it), and after typing (`LONG` of editor_model passes).
GAP, LAUNCH, REST, LONG = 4, 4., 1.2, 2.8
PHASES = ('launch', 'type', 'header', 'enter', 'jump', 'search', 'scroll', 'break', 'fold', 'unfold', 'find', 'lookup', 'idle')
# What `--off` takes: each part of a real editor that the session has on.
PARTS = ('analysis', 'roster', 'spelling', 'breaks', 'split', 'lint', 'complete')
# The line of a generated unit the caret types on, and the header above it (benchmark_editor.UNIT).
HOME, HEADER = '    total = 0', 'def total_'
BAR = 34


def session(model, pad, warm=False):
    """The script: every `yield phase` is one frame of that phase; what was
    injected before it arrives in that frame. `model.seen` notes what each
    interaction did to the editor, so a run that drove nothing shows."""
    import meltygui.core.windowing.window_constants as glfw
    CTRL, SHIFT = glfw.MOD_CONTROL, glfw.MOD_SHIFT
    state, seen = model.states[0], model.seen
    line = lambda at: model.source.native.texts(at, 1)[0]

    def rest(phase, seconds=REST):
        for _ in range(int(seconds * 120)):
            yield phase

    def burst(phase, presses):
        for press in presses:
            pad.key(*press)
            for _ in range(GAP):
                yield phase

    yield from rest('launch', .5 if warm else LAUNCH)
    if warm:
        return
    lines = model.source.text.split('\n')
    middle = len(lines) // 2
    home = next((line for line in range(middle, len(lines)) if lines[line] == HOME), middle)
    header = next((line for line in range(home, -1, -1) if lines[line].startswith(HEADER)), home)
    state.pane.focus()
    # Typing: three bursts of a word at the end of a body line, each followed by a rest, then taken out again.
    state.pane.jump(home + 1, column=len(lines[home]), flash=False)
    yield from rest('type', .2)
    for word in (' + value', ' + shape', ' + total'):
        yield from burst('type', [(ord(char.upper()) if char.isalpha() else glfw.KEY_SPACE if char == ' ' else glfw.KEY_EQUAL, 0, char)
                                  for char in word])
        yield from rest('type')
    seen['typed'] = line(home)
    yield from burst('type', [(glfw.KEY_BACKSPACE,)] * 24)
    yield from rest('type', LONG)
    seen['and taken out'] = model.source.text.split('\n') == lines
    # A parameter typed into a `def` header and taken out: every binding below may change.
    state.pane.jump(header + 1, token='shapes', flash=False)
    yield from rest('header', .2)
    yield from burst('header', [(ord(char.upper()), 0, char) for char in 'extra'] + [(glfw.KEY_COMMA, 0, ','), (glfw.KEY_SPACE, 0, ' ')])
    yield from rest('header')
    seen['header'] = line(header)
    yield from burst('header', [(glfw.KEY_BACKSPACE,)] * 7)
    yield from rest('header')
    seen['header taken out'] = model.source.text.split('\n') == lines
    state.pane.jump(home + 1, column=len(lines[home]), flash=False)
    yield from rest('enter', .2)
    yield from burst('enter', [(glfw.KEY_ENTER,), (glfw.KEY_BACKSPACE,)] * 8)
    yield from rest('enter')
    seen['lines after enter'] = model.source.native.lines
    # The end of the text, its start, a line in the middle.
    yield from burst('jump', [(glfw.KEY_END, CTRL)])
    yield from rest('jump', .6)
    seen['line at the end'] = state.pane.line(int(state.pane.scroll[1] // state.pane.line_height))
    yield from burst('jump', [(glfw.KEY_HOME, CTRL)])
    yield from rest('jump', .6)
    state.pane.jump(home + 1)
    yield from rest('jump')
    model.search = 'shape'
    yield from rest('search')
    seen['hits'] = len(state.editor.marks['hits'] or ())
    model.search = ''
    yield from rest('search', .6)
    # The wheel over the first pane: two seconds down into rows never shown, two back up.
    width, height = model.size
    pad.point(width * .25, height * .5)
    for down in (-3., 3.):
        for _ in range(240):
            pad.wheel(down)
            yield 'scroll'
        seen[f'scrolled to {down:+.0f}'] = state.pane.scroll[1]
    yield from rest('scroll', .6)
    state.pane.jump(home + 1, flash=False)
    yield from rest('break', .3)
    if model.breakpoints is not None:
        # Gutter clicks on three statements: a breakpoint each.
        for at in (home, home + 1, home + 2):
            pad.button(8., BAR + state.pane.y(at + 1) + state.pane.line_height * .5, True)
            yield 'break'
            pad.button(8., BAR + state.pane.y(at + 1) + state.pane.line_height * .5, False)
            yield from rest('break', .3)
        yield from rest('break', .6)
        seen['breakpoints'] = len(model.breakpoints)
    seen['rows'] = state.pane.rows
    yield from burst('fold', [(glfw.KEY_MINUS, CTRL | SHIFT)])
    yield from rest('fold')
    seen['rows folded'] = state.pane.rows
    yield from burst('unfold', [(glfw.KEY_EQUAL, CTRL | SHIFT)])
    yield from rest('unfold')
    seen['rows unfolded'] = state.pane.rows
    # The editor's own find bar: Ctrl+F, a term typed letter by letter, steps on and back, Escape.
    state.pane.jump(home + 1, flash=False)
    yield from rest('find', .3)
    yield from burst('find', [(glfw.KEY_F, CTRL)] + [(ord(char.upper()), 0, char) for char in 'shape'])
    yield from rest('find')
    yield from burst('find', [(glfw.KEY_ENTER,)] * 6 + [(glfw.KEY_ENTER, SHIFT)] * 3)
    seen['found'] = tuple(state.pane.search)
    yield from burst('find', [(glfw.KEY_ESCAPE,)])
    yield from rest('find', .6)
    # Ctrl+B on a use (a jump to its definition), then on that definition (its uses: the picker, arrows, Escape).
    state.pane.jump(header + 1, token='READY', flash=False)
    yield from rest('lookup', .3)
    yield from burst('lookup', [(glfw.KEY_B, CTRL)])
    yield from rest('lookup', .6)
    seen['went to'] = line(state.editor.doc.line_of(state.editor.doc.caret))
    yield from burst('lookup', [(glfw.KEY_B, CTRL)])
    yield from rest('lookup', .6)
    seen['picker rows'] = len(state.editor.lookup.rows)
    yield from burst('lookup', [(glfw.KEY_DOWN,)] * 3 + [(glfw.KEY_ESCAPE,)])
    yield from rest('lookup', .6)
    yield from rest('idle')


class Pad:
    """Input as the native Wayland window delivers it: its state, then its
    callback. Points are the content's (the window frame's margin is added)."""
    def __init__(self, window):
        import meltygui.core.windowing.window_constants as glfw
        self.window, self.glfw = window, glfw
        self.held = {glfw.MOD_CONTROL: glfw.KEY_LEFT_CONTROL, glfw.MOD_SHIFT: glfw.KEY_LEFT_SHIFT, glfw.MOD_ALT: glfw.KEY_LEFT_ALT}
        window.focused = True
        window.emit('window_focus', True)

    def send(self, key, action, mods):
        self.window.keys[key] = action
        self.window.emit('key', key, 0, action, mods)

    def key(self, key, mods=0, text=''):
        glfw, held = self.glfw, [found for mod, found in self.held.items() if mods & mod]
        for found in held:
            self.send(found, glfw.PRESS, mods)
        self.send(key, glfw.PRESS, mods)
        for char in text:
            self.window.emit('char', ord(char))
        self.send(key, glfw.RELEASE, mods)
        for found in held:
            self.send(found, glfw.RELEASE, 0)

    def point(self, x, y):
        from meltygui.core.melty import Melty
        x, y = x + Melty.frame_origin[0], y + Melty.frame_origin[1]
        self.window.cursor_pos = (x, y)
        self.window.emit('cursor_pos', x, y)

    def wheel(self, down):
        self.window.emit('scroll', 0., down)

    def button(self, x, y, down):
        """The left button goes down or up at a point (a frame apart: a frame reads the button's state)."""
        action = self.glfw.PRESS if down else self.glfw.RELEASE
        self.point(x, y)
        self.window.buttons[0] = action
        self.window.emit('mouse_button', 0, action, 0)


def child(path, off, out):
    """One session on `path` in this process; writes {phase: [frame ms]} to `out`.
    Without `out` it only launches: the caches a second launch finds are made."""
    from meltygui import os_window
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.model.source_model import Source
    from meltygui.state.editor_state import EditorState
    from meltygui.view.editor_view import draw_editor

    class Model:
        def __init__(self):
            self.source = Source(Path(path).read_text(), str(path))
            self.states = [EditorState() for _ in range(1 if 'split' in off else 2)]
            self.breakpoints = None if 'breaks' in off else {}
            self.search, self.seen, self.size = '', {}, (0., 0.)
            self.analysis = self.spelling = self.roster = None
            self.marks = {}
            if 'roster' not in off:
                from meltygui.model.roster_model import project
                self.roster = project(path)
                self.roster.join(self.source)
            if 'analysis' not in off:
                from meltygui.model.editor_model import Analysis
                self.analysis = Analysis(self.source, self.roster)
            self.source.lint = 'lint' not in off
            if 'spelling' not in off:
                from meltygui.model.spell_model import Spelling
                Toggles.TextEditor.enable_spell_check = True
                self.spelling = Spelling(self.source)

    model = Model()
    found = {'frames': {}, 'slow': [], 'first': None, 'lines': model.source.native.lines}

    @os_window(name='Editor session benchmark', app_id='meltygui-editor-session-bench', width=1400, height=900, use_cache=False)
    def app(width=None, height=None):
        analysis, spelling, model.size = model.analysis, model.spelling, (width, height)
        if spelling is not None and model.marks.get('spelling') is not spelling.marks:
            model.marks = {'spelling': spelling.marks}
        wide = width / len(model.states)
        for at, state in enumerate(model.states):
            draw_editor(model.source, name=f'pane {at}', state=state, left=at * wide, top=float(BAR), width=wide - 4, height=height - BAR,
                        path=str(path), search=model.search if at == 0 else '', breakpoints=model.breakpoints, marks=model.marks,
                        complete='complete' not in off, roster=model.roster,
                        **({'errors': analysis.errors, 'imports': analysis.imports, 'tints': analysis.tints} if analysis is not None else {}))
        for worker in (analysis, spelling):
            if worker is not None:
                worker.feed()

    import meltygui.core.windowing.window_api as window_api
    from meltygui.core.windowing import glfw_utils
    from meltygui.core.windowing.surface import Surface
    frame, clock = Surface.frame, time.perf_counter
    going = {'script': None, 'due': 0.}

    def timed(surface):
        if going['script'] is None:
            if window_api._state['backend'] is None:
                raise SystemExit('The session needs native Wayland windows (Toggles.windows.native_os_windows on Linux).')
            # The swap must not wait for the display: its call is part of the frame that is timed.
            surface.activate()
            window_api.swap_interval(0)
            going['script'] = session(model, Pad(surface.window), warm=not out)
            found['first'] = (time.time() - float(os.environ.get('MELTY_T0') or 0)) * 1000
        phase = next(going['script'], None)
        if phase is None:
            if out:
                Path(out).write_text(json.dumps(dict(found, seen=model.seen)))
            window_api.set_window_should_close(surface.window, True)
            return frame(surface)
        # The display's rate. A frame is due when the wait ends: getting the interpreter back after it is the frame's time.
        due = going['due'] = max(going['due'] + 1 / 120, clock())
        time.sleep(max(due - clock(), 0.))
        frame(surface)
        spent = (clock() - due) * 1000
        times = found['frames'].setdefault(phase, [])
        times.append(spent)
        if spent > BUDGET:
            # (When it was due, on `time.perf_counter`: for laying worker timings beside it.)
            found['slow'].append((phase, len(times), spent, due))
        glfw_utils.request_render()

    Surface.frame = timed
    from meltygui.core.runtime.app import run
    run()


def row(times):
    times = sorted(times)
    at = lambda share: times[min(int(len(times) * share), len(times) - 1)]
    return len(times), statistics.median(times), at(.95), at(.99), times[-1], sum(spent > BUDGET for spent in times)


def report(results):
    """One table per file and run: a row per phase."""
    for name, runs in results.items():
        for at, found in enumerate(runs):
            print(f'\n{name}: {found["lines"]:,d} lines, run {at + 1} of {len(runs)}; first frame {found["first"]:.0f} ms after the process started; '
                  f'the first frames: {" ".join(f"{spent:.1f}" for spent in found["frames"]["launch"][:8])} ms')
            print(f'{"":8s}{"frames":>7s}{"median":>8s}{"p95":>8s}{"p99":>8s}{"worst":>8s}{"over":>6s}')
            for phase in PHASES:
                if phase in found['frames']:
                    count, median, p95, p99, worst, over = row(found['frames'][phase])
                    print(f'{phase:8s}{count:7d}{median:8.2f}{p95:8.2f}{p99:8.2f}{worst:8.2f}{over:6d}')
            print(f'{"all":8s}{"":39s}{sum(row(times)[5] for times in found["frames"].values()):6d}')
            print('over, as phase#frame ms: ' + ' '.join(f'{phase}#{at} {spent:.1f}' for phase, at, spent, _ in found['slow'][:40]))
            print('what the script did: ' + ', '.join(f'{name}: {value!r}' for name, value in found['seen'].items()))
    print(f'\nms from when a frame was due until its buffer was handed over (swap interval 0); over: frames above {BUDGET:.2f} ms (120 fps)')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='*')
    parser.add_argument('--lines', default='', help='generated sizes, e.g. 1000,10000,100000')
    parser.add_argument('--off', default='', help=f'parts to leave out, of {",".join(PARTS)}')
    parser.add_argument('--runs', type=int, default=1, help='sessions per file')
    parser.add_argument('--json', default='', help='also write the results to this file')
    parser.add_argument('--child', default='', help=argparse.SUPPRESS)
    parser.add_argument('--out', default='', help=argparse.SUPPRESS)
    options = parser.parse_args()
    off = set(filter(None, options.off.split(',')))
    if off - set(PARTS):
        parser.error(f'--off takes {", ".join(PARTS)}')
    if options.child:
        return child(Path(options.child), off, options.out)
    files = [Path(file).resolve() for file in options.files]
    folder = Path(tempfile.mkdtemp(prefix='melty-editor-session-'))
    if options.lines:
        sys.path.insert(0, str(Path(__file__).parent))
        from benchmark_editor import generate
        import shutil
        # A project for the roster: the generated files beside the roster example's.
        project = folder / 'project'
        shutil.copytree(Path(__file__).parent.parent / 'examples' / 'gui_editor_roster_sample', project)
        files += [generate(int(count), project) for count in options.lines.split(',')]
    results = {}
    for file in files:
        # Every file starts from nothing saved and writes nothing into the user's own state. Its first
        # launch is not measured: it makes what every later launch finds (the font atlas, the start-up hints).
        home = folder / file.stem
        for kind in ('config', 'data', 'cache', 'state'):
            (home / kind).mkdir(parents=True)
        for at in range(-1, options.runs):
            out = home / f'frames-{at}.json'
            environment = dict(os.environ, MELTY_T0=str(time.time()), MELTY_FILE_META=str(home / 'file_meta.pkl'),
                               **{f'XDG_{kind}_HOME': str(home / kind.lower()) for kind in ('CONFIG', 'DATA', 'CACHE', 'STATE')})
            subprocess.run([sys.executable, __file__, '--child', str(file), '--off', options.off] + ['--out', str(out)] * (at >= 0),
                           env=environment, check=True)
            if at >= 0:
                results.setdefault(file.name, []).append(json.loads(out.read_text()))
    report(results)
    if options.json:
        Path(options.json).write_text(json.dumps(results, indent=1))


if __name__ == '__main__':
    main()
