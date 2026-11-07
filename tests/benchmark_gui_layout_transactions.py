"""Measure populated row/column rollback snapshots separately from view redraw."""
import argparse
import json
from pathlib import Path
from time import perf_counter_ns

from benchmark_gui_overhead import summary
from meltygui.core.rendering.retained_gui_prototype import RetainedGui


def run(count, frames):
    cache = RetainedGui(graphics=False)
    ids = {}
    executions = 0

    @cache.gui()
    def child(input_value):
        nonlocal executions
        executions += 1
        ids[input_value] = cache.current

    @cache.gui(width=400, height=200)
    def owner(input_value):
        with cache.geometry.declare((0, 1), axis=input_value % 2,
                                    key='split', mins=10, padding=4) as layout:
            for index in (0, 1):
                with layout.cell(index):
                    child((input_value, index), key=index)

    try:
        with cache.frame():
            for index in range(count):
                owner(index, key=index)
        geometry = cache.geometry
        leaf = ids[0, 0]
        stats = [axis.stats() for axis in geometry.axes]
        timings = {}
        for mode in ('native_fork_pair', 'begin_abort', 'leaf_redraw'):
            samples = []
            for frame in range(frames + 20):
                start = perf_counter_ns()
                if mode == 'native_fork_pair':
                    snapshot = tuple(axis.fork() for axis in geometry.axes)
                    del snapshot
                elif mode == 'begin_abort':
                    geometry.begin(leaf)
                    geometry.abort(leaf)
                else:
                    cache.invalidate_id(leaf)
                    cache.flush()
                elapsed = (perf_counter_ns() - start) / 1000
                if frame >= 20:
                    samples.append(elapsed)
            timings[mode] = dict(summary=summary(samples), samples_us=samples)
        assert stats == [axis.stats() for axis in geometry.axes]
        assert len(geometry.layouts) == count
        assert len(geometry.bindings) == count * 2
        assert executions == count * 2 + frames + 20
        return dict(layouts=count, bindings=len(geometry.bindings),
                    edge_stats=stats, timings=timings)
    finally:
        cache.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', default='1,16,64,256')
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results = [run(int(count), args.frames) for count in args.counts.split(',')]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + '\n')
    for row in results:
        print(row['layouts'], {key: value['summary']['median_us']
                               for key, value in row['timings'].items()})


if __name__ == '__main__':
    main()
