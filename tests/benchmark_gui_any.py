"""Frame cost of `draw_any` on a large dict, against the dict's size.

    .venv/bin/python tests/benchmark_gui_any.py --sizes 100,10000,1000000

Hidden window on a reserved desktop, no swap: a frame is the host's input pass, the flushes and the
root call (tests/editor_host.py). Median and worst ms of each interaction. `views` is how many views
the host holds at the end, `states` how many it ever made (a row keeps its state once shown).
The gate: every row but `mount` at the largest size within about 1.3 of the smallest; `mount` lists
the dict's keys once.
"""
import argparse
import resource
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import meltygui.core.windowing.window_constants as glfw  # noqa: E402
from editor_host import Host  # noqa: E402
from meltygui.core.rendering.gui_any import draw_any  # noqa: E402
from meltygui.view.dict_view import ROW, Tree  # noqa: E402

WIDE, TALL = 600., 560.
FIELD = 300.            # an x inside the value column


def data(size):
    """A dict of `size` entries of mixed types, with a branch of the same kinds every hundred entries."""
    kinds = (lambda at: at * .5, lambda at: at, lambda at: f'text {at}', lambda at: at % 2 == 0)
    found = {}
    for at in range(size):
        if at % 100 == 50:
            found[f'group {at}'] = {f'inner {inner}': kinds[inner % 4](inner) for inner in range(12)}
        else:
            found[f'item {at}'] = kinds[at % 4](at)
    return found


def timed(host, frames):
    """Run `frames` (callables that each run one host frame); returns their times in ms."""
    spent = []
    for frame in frames:
        start = time.perf_counter()
        frame()
        spent.append((time.perf_counter() - start) * 1e3)
    return spent


def measure(size):
    found, tree = data(size), Tree()
    host = Host(draw_any, input_value={}, name='data', left=0., top=0., width=WIDE, height=TALL, tree=tree)
    # The frame the dict is first shown in: its keys are listed, the rows in view are mounted.
    rows = {'mount': timed(host, [lambda: host.frame(input_value=found)])}
    state = host.cache.states[next(iter(host.cache.root_ids))]
    y = lambda row: row * ROW + ROW * .5 - state.scroll_offset[1]

    def wheel(pixels):
        """Scroll by the wheel, as a user does: the view runs for it and shows the rows there."""
        host.frame((FIELD, 200.), wheel=-pixels / 40.)
        host.frame()

    rows['idle'] = timed(host, [host.frame] * 60)
    rows['scroll'] = timed(host, [lambda: host.frame((FIELD, 200.), wheel=-1.)] * 120)
    rows['fling'] = timed(host, [lambda: host.frame((FIELD, 200.), wheel=-30.)] * 60)
    wheel(-1e9)
    # Row 0 is a float, row 1 an int, row 2 a text. Every step must change its value, or it measured nothing.
    host.frame((FIELD, y(0)), True)
    rows['drag'] = timed(host, [lambda at=at: host.frame((FIELD + 4. * at, y(0)), True) for at in range(1, 61)])
    host.frame((FIELD + 240., y(0)), False)
    assert found['item 0'] == 0.6, found['item 0']
    host.click(FIELD, y(1))
    rows['type'] = timed(host, [lambda at=at: host.frame(text=str(at % 10)) for at in range(40)])
    host.frame(keys=[(glfw.KEY_ENTER, 0)])
    assert found['item 1'] == int('0123456789' * 4), found['item 1']
    host.click(FIELD + 200., y(2))
    rows['text'] = timed(host, [lambda at=at: host.frame(text='abcdefghij'[at % 10]) for at in range(40)])
    assert found['item 2'] == 'text 2' + 'abcdefghij' * 4, found['item 2']
    # A branch, when the dict has one: open, then shut.
    branch = next((at for at, key in enumerate(found) if key.startswith('group')), None)
    if branch is not None:
        wheel(max(branch - 3, 0) * ROW)
        where, count = (60., y(branch)), tree.rows.count
        rows['open'] = timed(host, [lambda: host.frame(where, True)])
        host.frame(where, False)
        assert tree.rows.count == count + 12, tree.rows.count
        rows['shut'] = timed(host, [lambda: host.frame(where, True)])
        host.frame(where, False)
        assert tree.rows.count == count, tree.rows.count
    views = len(host.cache.records)
    held = len(host.cache.bindings)
    host.close()
    return rows, views, held


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sizes', default='100,10000,1000000')
    sizes = [int(size) for size in parser.parse_args().sizes.split(',')]
    results = {}
    measure(50)             # the first host of a process makes the GL context and loads the modules
    for size in sizes:
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        results[size] = (*measure(size), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before)
    names = list(results[sizes[-1]][0])
    print('row      ' + ''.join(f'{size:>22,}' for size in sizes) + '   largest / smallest')
    for name in names:
        cells, medians = [], []
        for size in sizes:
            spent = results[size][0].get(name)
            if spent is None:
                cells.append(f'{"":>22}')
                continue
            medians.append(statistics.median(spent))
            cells.append(f'{medians[-1]:>10.2f} ({max(spent):>6.2f})  ')
        ratio = f'{medians[-1] / medians[0]:.2f}' if len(medians) == len(sizes) and medians[0] else ''
        print(f'{name:<9}' + ''.join(cells) + f'   {ratio}')
    print('views    ' + ''.join(f'{results[size][1]:>22,}' for size in sizes))
    print('states   ' + ''.join(f'{results[size][2]:>22,}' for size in sizes))
    print('peak MB  ' + ''.join(f'{results[size][3] / 1024:>22,.0f}' for size in sizes))


if __name__ == '__main__':
    main()
