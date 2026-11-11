"""CPU cost of the native editor per typing, caret, Enter and scroll frame.

    python tests/benchmark_editor.py [--block 16] [--frames 200] file.py ...

Run on a reserved desktop (hidden GL window). A frame is input processing,
every dirty body and the composition submit; buffer swap is not included.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import conftest  # noqa: F401  (creates the ImGui context)
import meltygui.core.windowing.window_constants as glfw
import meltygui.editor.editor as session
import meltygui.view.editor_view as view
from editor_host import Host
from meltygui.editor import syntax
from meltygui.state.editor_state import EditorState


def measure(host, frames, step):
    """Median and p95 milliseconds and mean body runs of `step(i)` frames."""
    times, runs = [], host.cache.executor.runs
    for index in range(frames):
        start = time.perf_counter()
        step(index)
        times.append((time.perf_counter() - start) * 1000)
    times.sort()
    return statistics.median(times), times[int(len(times) * .95)], (host.cache.executor.runs - runs) / frames


def bench(path, frames):
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
    rows = {
        'idle': lambda i: host.frame(),
        'type': lambda i: host.frame(keys=[(glfw.KEY_A, 0)], text='a'),
        'caret': lambda i: host.frame(keys=[(glfw.KEY_LEFT if i % 2 else glfw.KEY_RIGHT, 0)]),
        'enter': lambda i: host.frame(keys=[(glfw.KEY_ENTER if i % 2 == 0 else glfw.KEY_BACKSPACE, 0)]),
        'scroll': lambda i: host.frame((300, 400), wheel=-1 if i % 40 < 20 else 1),
    }
    print(f'{Path(path).name}: {ed.doc.lines} lines, {len(ed.stamps)} blocks of {session.BLOCK}, '
          f'first frame {mount:.1f} ms')
    for name, step in rows.items():
        if name == 'type':
            ed.suggest.close()
        if name == 'scroll':
            # Visit the range once so the timed frames replay captured blocks.
            measure(host, 80, step)
        if name == PROFILE:
            import cProfile, pstats
            profile = cProfile.Profile()
            profile.enable()
            measure(host, frames, step)
            profile.disable()
            pstats.Stats(profile).sort_stats('tottime').print_stats(18)
        median, high, runs = measure(host, frames, step)
        print(f'  {name:7s} median {median:6.3f} ms   p95 {high:6.3f} ms   bodies {runs:5.2f}')
    host.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='+')
    parser.add_argument('--block', type=int, default=session.BLOCK)
    parser.add_argument('--frames', type=int, default=200)
    parser.add_argument('--profile', default='')
    options = parser.parse_args()
    session.BLOCK = view.BLOCK = options.block
    PROFILE = options.profile
    syntax.tools()
    for file in options.files:
        bench(file, options.frames)
