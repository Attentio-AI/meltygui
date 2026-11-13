"""Frame cost of the native editor's occasional actions, against file size.

    python tests/benchmark_editor_actions.py [--lines 1000,10000,100000] [--presses 30] [--roster] [--json out.json]

Run on a reserved desktop, as benchmark_editor.py: a hidden GL window, no
swap, no `Analysis` or spelling worker. The lookups of Ctrl+B run where they
do in an app, on the pool thread; their time is the `lookup` row, not a
frame's. Each cell is the median over `--presses` rounds and the worst. The
garbage collector runs between the rounds (see `bench`).

Rows: `press` the frame Ctrl+B is pressed in (it queues the lookup), `lookup`
the lookup itself (off the render thread; `--roster` asks a roster joined to
the source instead of the source alone), `jump` the frame a jump lands in
(unfold, scroll, flash), `open` the frame the usage picker opens in (150
rows), `arrow` an arrow key in the picker, `outside` the Ctrl+Shift+B frame
(the launcher is stubbed), `term` the frame a search term changes in (a large
text only asks for its reading there), `hits` the frame that term's hits
land in and the first one is revealed, `next` and `previous` a step through
the hits. The generated text is benchmark_editor.py's plus one definition
with 150 uses; the term is found once in every unit.
To add a row, time it in `bench` and name it in `ROWS`.
"""
import argparse
import gc
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import conftest  # noqa: F401  (creates the ImGui context)
import benchmark_editor
import meltygui.core.windowing.window_constants as glfw
import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.editor import lookup, syntax
from meltygui.model.roster_model import Roster
from meltygui.model.source_model import Source
from meltygui.state.editor_state import EditorState

CTRL, SHIFT = glfw.MOD_CONTROL, glfw.MOD_SHIFT
ROWS = ('press', 'lookup', 'jump', 'open', 'arrow', 'outside', 'term', 'hits', 'next', 'previous')
# Lever: the search terms, one of which is set per round (each stands in every unit several times).
TERMS = ('shape', 'width')
# Lever: the definition the picker opens for, and its uses.
TARGET = '\n\ndef picked_target():\n    return 1\n\n\n' + ''.join(f'USE_{n} = picked_target()\n' for n in range(150))


def timed(call):
    start = time.perf_counter()
    call()
    return (time.perf_counter() - start) * 1000


def bench(path, presses, joined=False):
    """{row: (median ms, worst ms)} for one file, plus 'lines', 'rows' (of the picker) and 'found' (hits of the last term)."""
    text = path.read_text() + TARGET
    path.write_text(text)
    source, state, roster = Source(text, str(path)), EditorState(), None
    if joined:
        roster = Roster(str(path.parent), paths=[str(path)], roots=[str(path.parent)])
        roster.join(source)
    host = Host(view.draw_editor, input_value=source, name='editor', state=state, left=0., top=0., width=1000., height=800.,
                fold_defaults=True, path=str(path), roster=roster)
    ed, pane = state.editor, state.pane
    ed.source.settle()
    host.frame()
    if roster is not None:
        roster.settle()
    lines = text.split('\n')
    middle = len(lines) // 2
    use = next(at for at in range(middle, len(lines)) if 'if scale > LIMIT_' in lines[at])
    define = lines.index('def picked_target():')
    pane.focus()
    host.frame()
    times = {name: [] for name in ROWS}

    def answered():
        """Wait for the lookup the last press queued, off the clock."""
        start = time.perf_counter()
        ed.lookup.pending.result()
        times['lookup'].append((time.perf_counter() - start) * 1000)
        time.sleep(.005)        # (its wake is the pool thread's last act)

    def place(line, token):
        pane.jump(line + 1, token=token, flash=False)
        host.frame()
        host.frame()

    for at in range(presses):
        # The host has no collector policy (an app has `gc_manager`): a whole collection, 14 ms
        # here, is taken between the rounds, not inside whichever frame it would fall into.
        gc.collect()
        # Ctrl+B on a use: the press, the lookup, the frame the jump lands in.
        place(use, 'LIMIT')
        times['press'].append(timed(lambda: host.frame(keys=[(glfw.KEY_B, CTRL)])))
        answered()
        times['jump'].append(timed(host.frame))
        assert ed.doc.line(ed.doc.line_of(ed.doc.caret)).startswith('LIMIT_'), 'the jump did not land'
        # Ctrl+B on a definition with 150 uses: the picker opens; an arrow key in it.
        place(define, 'picked_target')
        times['press'].append(timed(lambda: host.frame(keys=[(glfw.KEY_B, CTRL)])))
        answered()
        times['open'].append(timed(host.frame))
        assert ed.lookup.open, 'the picker did not open'
        found = len(ed.lookup.rows)
        times['arrow'].append(timed(lambda: host.frame(keys=[(glfw.KEY_DOWN, 0)])))
        host.frame(keys=[(glfw.KEY_ESCAPE, 0)])
        times['outside'].append(timed(lambda: host.frame(keys=[(glfw.KEY_B, CTRL | SHIFT)])))
        # A search term changes; its hits land; stepping on and back.
        place(use, 'LIMIT')
        term = TERMS[at % len(TERMS)]
        times['term'].append(timed(lambda: host.frame(search=term)))
        ed.source.settle()
        times['hits'].append(timed(host.frame))
        host.frame()
        hits = pane.search
        assert hits.term == term and hits.index and hits.count, 'the search did not land'
        for name, delta in (('next', 1), ('next', 1), ('previous', -1), ('previous', -1)):
            pane.find(delta)
            times[name].append(timed(host.frame))
            host.frame()
        assert pane.search.index == hits.index, 'stepping did not come back'
    host.close()
    if roster is not None:
        roster.close()
    return {'lines': len(lines), 'rows': found, 'found': hits.count,
            **{name: (statistics.median(found), max(found)) for name, found in times.items()}}


def report(results):
    names = list(results)
    print(f'{"":9s}' + ''.join(f'{results[name]["lines"]:>17,d} lines' for name in names))
    print(f'{"hits":9s}' + ''.join(f'{results[name]["found"]:>23,d}' for name in names) + '   (of the last term)')
    for row in ROWS:
        print(f'{row:9s}' + ''.join(f'{results[name][row][0]:>10.2f} (max {results[name][row][1]:>6.2f})' for name in names))
    print('median ms (worst ms); `lookup` is off the render thread; the frame budget at 120 fps is 8.33 ms')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--lines', default='1000,10000,100000', help='generated sizes')
    parser.add_argument('--presses', type=int, default=30)
    parser.add_argument('--roster', action='store_true', help='ask a roster the source joined, not the source alone')
    parser.add_argument('--json', default='', help='also write the results to this file')
    options = parser.parse_args()
    syntax.tools()
    # Ctrl+Shift+B launches nothing here.
    lookup.external = lambda path, line: None
    # The first host pays for the GL context and fonts.
    Host(view.draw_editor, input_value='warm = 1\n', name='warm', state=EditorState(), left=0., top=0., width=1000., height=800.).close()
    folder = tempfile.mkdtemp(prefix='melty-editor-actions-')
    results = {}
    for count in options.lines.split(','):
        results[count] = bench(benchmark_editor.generate(int(count), folder), options.presses, options.roster)
    report(results)
    if options.json:
        Path(options.json).write_text(json.dumps(results, indent=1))
