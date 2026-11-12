"""Cost of `Source.tree`: the reading that grows it, and the frames of `draw_editor(source)` beside
`draw_any(source.tree)`, against file size.

    .venv/bin/python tests/benchmark_source_tree.py [--lines 1000,10000,100000] [--frames 200]

Hidden window on a reserved desktop, no swap (tests/editor_host.py); the sources are the generated
ones of tests/benchmark_editor.py. Off the render thread, ms:

    read      a first reading of the text, without the tree and with it
    reread    a reading after one edit, without and with

Frames, median and p95 ms (bodies run per frame). `alone` is the editor without a tree on its source,
measured in the same process: what the tree costs the editor is `type` against `alone`.

    mount     the first frame of both views
    idle      no input
    alone     a character typed in the editor, no tree
    type      a character typed in the editor, the tree beside it (it is read once typing rests)
    landing   the frame a reading is taken in, no tree (and the frame after it, for the bodies counted)
    lands     the same with the tree: its rows run again
    drag      a number dragged in the tree: a splice a frame, the editor repaints its line, a reading a frame
    scroll    the wheel over the tree's rows
    open      a branch of the tree opened, then shut

The gate: every frame row at the largest size within about 1.3 of the smallest, and `type` within
noise of `alone`.
"""
import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import conftest  # noqa: F401,E402  (creates the ImGui context)
import meltygui.core.windowing.window_constants as glfw  # noqa: E402
from benchmark_editor import HOME, generate, measure  # noqa: E402
from editor_host import Host  # noqa: E402
from meltygui.core.rendering.gui_any import draw_any  # noqa: E402
from meltygui.editor import syntax  # noqa: E402
from meltygui.model import _source_native  # noqa: E402
from meltygui.model.source_model import Source  # noqa: E402
from meltygui.state.editor_state import EditorState  # noqa: E402
from meltygui.view.dict_view import ROW, Tree  # noqa: E402
from meltygui.view.editor_view import draw_editor  # noqa: E402

SPLIT = 600.            # the editor's width; the tree is to its right
FIELD = SPLIT + 300.    # an x inside the tree's value column
ROWS = ('idle', 'alone', 'type', 'landing', 'lands', 'drag', 'scroll', 'open')


def both(source=None, state=None, rows=None, tree=True):
    draw_editor(source, name='source', state=state, left=0., top=0., width=SPLIT, height=600., fold_defaults=True)
    if tree:
        draw_any(source.tree, name='tree', tree=rows, left=SPLIT, top=0., width=500., height=600.)


def readings(text):
    """(first reading, with the tree, a reading after an edit, with the tree, entries) in ms."""
    found = []
    for tree in (False, True):
        native = _source_native.Source(text, 0)
        spent = []
        for edit in range(6):
            if edit:
                native.splice(len(text) // 2, len(text) // 2, ' ')
            start = time.perf_counter()
            native.ask(now=True, tree=tree)
            spent.append((time.perf_counter() - start) * 1e3)
            native.land()
        found += [spent[0], statistics.median(spent[1:])]
        if tree:
            found.append(native.tree_shape()[1])
        native.close()
    return found[0], found[2], found[1], found[3], found[4]


def caret(host, ed):
    """Put the editor's caret on the unit line every size has, in view."""
    middle = ed.doc.lines // 2
    line = next((line for line in range(middle, min(middle + 200, ed.doc.lines)) if ed.doc.line(line) == HOME), middle)
    y = (ed.rows.row(line) + .5) * ed.lh
    host.frame((300, 300), wheel=-(y - 300) / 40)
    host.frame((300, 300))
    host.click(ed.gutter + 200, 300)
    for _ in range(3):
        host.frame()
    ed.suggest.close()


def landings(host, source, count):
    """The frames in which a reading is taken: an edit from outside (a space at the text's end, put and
    removed in turn) is read at once, the frame after it is timed."""
    times, runs = [], host.cache.executor.runs
    for index in range(count):
        size = source.native.size
        source.splice(size - index % 2, size, '' if index % 2 else ' ')
        source.ask()
        source.settle()
        start = time.perf_counter()
        host.frame()
        times.append((time.perf_counter() - start) * 1e3)
        host.frame()
    times.sort()
    return statistics.median(times), times[int(len(times) * .95)], (host.cache.executor.runs - runs) / count


def bench(path, frames):
    text = Path(path).read_text()
    found = {'lines': text.count('\n') + 1, 'read': readings(text)}
    # The editor alone, on a source nobody asked the tree of.
    source, state = Source(text), EditorState()
    host = Host(both, source=source, state=state, rows=None, tree=False)
    source.settle()
    host.frame()
    caret(host, state.editor)
    found['alone'] = measure(host, frames, lambda i: host.frame(keys=[(glfw.KEY_A, 0)], text='a'))
    state.editor.suggest.close()
    found['landing'] = landings(host, source, frames // 4)
    assert source.grown is None
    host.close()
    source.close()

    source, state, rows = Source(text), EditorState(), Tree()
    start = time.perf_counter()
    host = Host(both, source=source, state=state, rows=rows)
    found['mount'] = (time.perf_counter() - start) * 1e3
    ed = state.editor
    source.settle()
    for _ in range(3):
        host.frame()
    found['keys'] = len(source.tree)
    caret(host, ed)
    source.settle()
    host.frame()
    found['idle'] = measure(host, frames, lambda i: host.frame())
    found['type'] = measure(host, frames, lambda i: host.frame(keys=[(glfw.KEY_A, 0)], text='a'))
    ed.suggest.close()
    found['lands'] = landings(host, source, frames // 4)
    source.settle()
    host.frame()
    # Row 0 of the tree is the first constant of the first unit, a whole number.
    key = source.tree.keys()[0]
    was = source.tree[key]
    y = ROW * .5
    host.frame((FIELD, y), True)
    found['drag'] = measure(host, frames, lambda i: host.frame((FIELD + 4. * (i + 1), y), True))
    host.frame((FIELD + 4. * frames, y), False)
    source.settle()
    host.frame()
    assert source.tree[key] == was + frames and f'{key} = {was + frames}' in source.native.texts(0, 12), (source.tree[key], was)
    found['scroll'] = measure(host, frames, lambda i: host.frame((FIELD, 300.), wheel=-1 if i % 40 < 20 else 1))
    host.frame((FIELD, 300.), wheel=1e6)
    host.frame()
    # The first branch among the rows in view: a press opens it, the next shuts it.
    branch = next(at for at, name in enumerate(source.tree.keys()[:40]) if not isinstance(source.tree[name], (int, float, str, bool)))
    where, count = (SPLIT + 40., branch * ROW + ROW * .5), rows.rows.count
    found['open'] = measure(host, 40, lambda i: host.frame(where, i % 2 == 0))
    assert rows.rows.count == count
    host.close()
    source.close()
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lines', default='1000,10000,100000')
    parser.add_argument('--frames', type=int, default=200)
    args = parser.parse_args()
    syntax.tools()
    folder = tempfile.mkdtemp(prefix='melty-tree-bench-')
    sizes = [int(size) for size in args.lines.split(',')]
    bench(generate(400, folder), 20)        # the first host of a process makes the GL context and loads the modules
    results = [bench(generate(size, folder), args.frames) for size in sizes]
    print(f'{"":9s}' + ''.join(f'{found["lines"]:>22,d} lines' for found in results) + '   largest / smallest')
    for at, name in enumerate(('read', 'read+tree', 'reread', 'reread+tree')):
        print(f'{name:12s}' + ''.join(f'{found["read"][(0, 1, 2, 3)[at]]:>18.2f} ms       ' for found in results) + '   (off the render thread)')
    print(f'{"entries":12s}' + ''.join(f'{found["read"][4]:>18,d}          ' for found in results))
    print(f'{"module keys":12s}' + ''.join(f'{found["keys"]:>18,d}          ' for found in results))
    print(f'{"mount":12s}' + ''.join(f'{found["mount"]:>18.1f} ms       ' for found in results))
    for name in ROWS:
        cells = [found[name] for found in results]
        ratio = cells[-1][0] / cells[0][0] if cells[0][0] else 0.
        print(f'{name:9s}' + ''.join(f'{median:>9.3f} p95 {high:>7.3f} ({runs:4.1f})' for median, high, runs in cells) + f'    {ratio:.2f}')
    print('median ms, p95 ms, (bodies run per frame); the frame budget at 120 fps is 8.33 ms')


if __name__ == '__main__':
    main()
