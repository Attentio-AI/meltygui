"""CPU cost of the native editor per frame, against file size.

    python tests/benchmark_editor.py [--lines 1000,10000,100000] [--frames 200] [--json out.json] [file.py ...]

Run on a reserved desktop (hidden GL window). A frame is input processing,
every dirty body and the composition submit; buffer swap is not included.
`--lines` writes generated sources to a temp folder. The table ends with each
row's cost at the largest size over the smallest: the gate is about 1.3.

Rows: `mount` the first frame, `scan` one fold scan (it runs on the editor's
worker after the text rests, never in a frame), `idle` no input, `type` one character, `caret` an arrow key, `enter`
Enter and Backspace alternating, `scroll` the wheel over rows already
painted, `fresh` the wheel into rows never painted. To add a row, add it to
`ROWS`; to change the generated code, edit `UNIT`.
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import conftest  # noqa: F401  (creates the ImGui context)
import meltygui.core.windowing.window_constants as glfw
import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.editor import syntax
from meltygui.state.editor_state import EditorState

# One generated unit, about 40 lines: every token kind the editor treats
# specially, a few scopes to fold, and characters outside ASCII as most of
# the code base has (the whole text then stores two bytes a character).
UNIT = '''
# Unit {n}: a comment run that folds, with an arrow → outside ASCII.
# The second line of the run.
LIMIT_{n} = {n}
SCALE_{n} = {n}.5
READY_{n} = True
TINT_{n} = (0.2, 0.4, 0.8)
WARM_{n} = '#e0703a'


class Shape{n}:
    """A class with a docstring
    over two lines."""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.label = f'shape {{width}} by {{height}} '

    def area(self, scale=SCALE_{n}):
        if scale > LIMIT_{n}:
            return self.width * self.height
        return self.width * self.height * scale


def total_{n}(shapes, ready=READY_{n}):
    total = 0
    for shape in shapes:
        if not ready:
            continue
        total += shape.area()
    return total


def describe_{n}(shape):
    parts = [str(shape.width), str(shape.height), 'unit {n}']
    return ' x '.join(parts)  # joined for the label

'''
NAMES = 24
ROWS = ('idle', 'type', 'caret', 'enter', 'scroll', 'fresh')


def generate(lines, folder):
    """Write a source of about `lines` lines; returns its path."""
    parts, count, n = ['"""Generated benchmark source."""\nimport math\nimport os\n'], 3, 0
    while count < lines:
        # Names repeat every `NAMES` units (fewer than the smallest size has),
        # so every size has the same vocabulary and only the length differs.
        parts.append(UNIT.format(n=n % NAMES))
        count += UNIT.count('\n')
        n += 1
    path = Path(folder) / f'generated_{lines}.py'
    path.write_text(''.join(parts))
    return path


def measure(host, frames, step):
    """Median and p95 milliseconds and mean body runs of `step(i)` frames."""
    times, runs = [], host.cache.executor.runs
    for index in range(frames):
        start = time.perf_counter()
        step(index)
        times.append((time.perf_counter() - start) * 1000)
    times.sort()
    return statistics.median(times), times[int(len(times) * .95)], (host.cache.executor.runs - runs) / frames


def bench(path, frames, profile=''):
    """{row: (median ms, p95 ms, bodies)} for one file, plus 'lines' and 'mount'."""
    text = Path(path).read_text()
    state = EditorState()
    start = time.perf_counter()
    host = Host(view.draw_editor, input_value=text, name='editor', state=state, left=0., top=0.,
                width=1000., height=800., fold_defaults=False)
    mount = (time.perf_counter() - start) * 1000
    ed = state.editor
    line = ed.doc.lines // 2
    y = (ed.rows.row(line) + .5) * ed.lh
    host.frame((300, 400), wheel=-(y - 400) / 40)
    host.frame((300, 400))
    host.click(ed.gutter + 200, 400)
    for _ in range(3):
        host.frame()
    start = time.perf_counter()
    ed.outline()
    ed.worker.settle()
    scan = (time.perf_counter() - start) * 1000
    steps = {
        'idle': lambda i: host.frame(),
        'type': lambda i: host.frame(keys=[(glfw.KEY_A, 0)], text='a'),
        'caret': lambda i: host.frame(keys=[(glfw.KEY_LEFT if i % 2 else glfw.KEY_RIGHT, 0)]),
        'enter': lambda i: host.frame(keys=[(glfw.KEY_ENTER if i % 2 == 0 else glfw.KEY_BACKSPACE, 0)]),
        'scroll': lambda i: host.frame((300, 400), wheel=-1 if i % 40 < 20 else 1),
        # Half a viewport a frame, downwards, over rows that were never shown.
        'fresh': lambda i: host.frame((300, 400), wheel=-10),
    }
    found = {'lines': ed.doc.lines, 'mount': mount, 'scan': scan}
    for name in ROWS:
        step, count = steps[name], frames
        # Background scans of the row before are not this row's cost.
        ed.worker.settle()
        host.frame()
        if name == 'type':
            ed.suggest.close()
        if name == 'scroll':
            # Visit the range once so the timed frames replay captured blocks.
            measure(host, 80, step)
        if name == 'fresh':
            # As many frames as the smallest file has rows left below its middle.
            count = min(frames, 20)
        if name == profile:
            import cProfile, pstats
            profiler = cProfile.Profile()
            profiler.enable()
            measure(host, count, step)
            profiler.disable()
            pstats.Stats(profiler).sort_stats('tottime').print_stats(18)
        found[name] = measure(host, count, step)
    host.close()
    return found


def report(results):
    """Print the table: one column per file, the last one largest over smallest."""
    names = list(results)
    print(f'{"":8s}' + ''.join(f'{results[name]["lines"]:>22,d} lines' for name in names) + '   largest / smallest')
    print(f'{"mount":8s}' + ''.join(f'{results[name]["mount"]:>21.1f} ms    ' for name in names))
    print(f'{"scan":8s}' + ''.join(f'{results[name]["scan"]:>21.1f} ms    ' for name in names) + '   (worker thread)')
    for row in ROWS:
        cells = [results[name][row] for name in names]
        ratio = cells[-1][0] / cells[0][0] if cells[0][0] else 0.
        print(f'{row:8s}' + ''.join(f'{median:>9.3f} p95 {high:>7.3f} ({runs:4.1f})' for median, high, runs in cells)
              + f'   {ratio:5.2f}')
    print('median ms, p95 ms, (bodies run per frame); the frame budget at 120 fps is 8.33 ms')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='*')
    parser.add_argument('--lines', default='', help='generated sizes, e.g. 1000,10000,100000')
    parser.add_argument('--frames', type=int, default=200)
    parser.add_argument('--profile', default='', help='a row name to run under cProfile')
    parser.add_argument('--json', default='', help='also write the results to this file')
    options = parser.parse_args()
    syntax.tools()
    # The first host pays for the GL context and fonts: keep that out of `mount`.
    Host(view.draw_editor, input_value='warm = 1\n', name='warm', state=EditorState(), left=0., top=0.,
         width=1000., height=800.).close()
    files = [Path(file) for file in options.files]
    if options.lines:
        folder = tempfile.mkdtemp(prefix='melty-editor-bench-')
        files += [generate(int(count), folder) for count in options.lines.split(',')]
    results = {}
    for file in files:
        results[file.name] = bench(file, options.frames, options.profile)
    report(results)
    if options.json:
        Path(options.json).write_text(json.dumps(results, indent=1))
