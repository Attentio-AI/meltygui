"""Worst-case @gui redraw: every view invalidated every frame, against raw drawing.

Hosted frames include native layout observation and idle host input, as a real
window does. Variants are frame-interleaved; each frame asserts body counts and
checksums, and the first and last frames assert exact pixels against raw drawing.
Uses the installed extension. Run on a reserved agent desktop with isolated XDG
config/state/cache directories:

    .venv/bin/python tests/benchmark_gui_redraw.py --trees 5:4,4:8,2:512 --output /tmp/redraw.json

A tree is depth:fanout; 5:4 is 341 views, 4:8 is 585, 2:512 is one parent with
512 children. --mounts N times fresh mounts; --hot adds unchanged frames and a
root-only redraw (gui_parent). Heights must stay whole pixels (512 divided by fanout per level),
otherwise raw and retained snapping differ. CPU submission time only; GPU
completion is outside the timer. See docs/GUI_REDRAW_FAST_PATHS.md.
"""
import argparse
import cProfile
import json
import pstats
import random
from pathlib import Path
from statistics import mean, median
from time import perf_counter_ns


def measure(tree_spec, args, graphics, surface):
    from benchmark_gui_overhead import Scene, tree
    from profile_gui_overhead import mount
    depth, fanout = map(int, tree_spec.split(':'))
    # gui_parent: only the root re-executes; its descendants are declared again
    # but stay cached (for 2:N, one parent over N unchanged children).
    variants = ['raw', 'gui_redraw'] + (['gui_parent', 'gui_hot'] if args.hot else [])
    scenes, samples, mounts = {}, {}, []
    reference = expected = None
    try:
        # First mount of a fresh tree, including its first frame and GPU completion.
        for index in range(args.mounts):
            scene = Scene(tree(depth, fanout, False), 'gui_redraw', 0, True)
            scene.host.cache.calibration.directory = args.output.parent / (args.output.stem + '-calibration') / f'mount{index}'
            scene.host.solve_native(surface)
            start = perf_counter_ns()
            mount(scene, graphics, False)
            mounts.append((perf_counter_ns() - start) / 1e6)
            scene.close()
        for label in variants:
            scene = Scene(tree(depth, fanout, False), 'gui_hot' if label == 'gui_parent' else label, 0, True)
            scenes[label] = scene
            if scene.host:
                scene.host.cache.calibration.directory = args.output.parent / (args.output.stem + '-calibration') / label
                scene.host.solve_native(surface)
            mount(scene, graphics, False)
            pixels = graphics.pixels()
            if reference is None:
                reference, expected = pixels, (scene.count, scene.checksum)
            assert pixels == reference, ('mount pixels', label)
            samples[label] = []
        views = expected[0]
        rng, order = random.Random(args.seed), list(variants)
        profiler = cProfile.Profile() if args.profile else None
        for frame in range(args.warm + args.frames):
            rng.shuffle(order)
            for label in order:
                scene = scenes[label]
                graphics.begin()
                profiled = profiler is not None and label == 'gui_redraw' and frame >= args.warm
                if profiled:
                    profiler.enable()
                start = perf_counter_ns()
                if scene.host:
                    scene.host.solve_native(surface)
                    scene.host.cache.process_host_input()
                    if label == 'gui_parent':
                        scene.host.cache.invalidate_id(next(iter(scene.host.cache.root_ids)))
                scene.frame()
                graphics.submit()
                elapsed = (perf_counter_ns() - start) / 1000
                if profiled:
                    profiler.disable()
                graphics.finish()
                if label == 'gui_hot':
                    assert scene.count == 0
                elif label == 'gui_parent':
                    assert scene.count == 1
                else:
                    assert (scene.count, scene.checksum) == expected, label
                if frame >= args.warm:
                    samples[label].append(elapsed)
                if frame == args.warm + args.frames - 1:
                    assert graphics.pixels() == reference, ('final pixels', label)
        row = dict(tree=tree_spec, views=views)
        if mounts:
            row['mount_ms'] = mounts
            print(f'{tree_spec:7} {"mount":11} views={views:5} median={median(mounts):7.3f} ms '
                  f'per view={median(mounts) * 1000 / views:6.2f} us', flush=True)
        for label, values in samples.items():
            row[label] = dict(mean_ms=mean(values) / 1000, median_ms=median(values) / 1000,
                              p95_ms=sorted(values)[int(.95 * (len(values) - 1))] / 1000,
                              us_per_view=mean(values) / views, samples_us=values)
            print(f'{tree_spec:7} {label:11} views={views:5} mean={row[label]["mean_ms"]:7.3f} ms '
                  f'median={row[label]["median_ms"]:7.3f} p95={row[label]["p95_ms"]:7.3f} '
                  f'per view={row[label]["us_per_view"]:6.2f} us', flush=True)
        if profiler is not None:
            rows = [(own * 1e6 / args.frames, cumulative * 1e6 / args.frames, calls / args.frames,
                     f'{Path(filename).name}:{line}:{name}')
                    for (filename, line, name), (_, calls, own, cumulative, _) in pstats.Stats(profiler).stats.items()]
            row['profile'] = [dict(self_us=own, cumulative_us=cumulative, calls=calls, function=where)
                              for own, cumulative, calls, where in sorted(rows, reverse=True)[:40]]
            for entry in row['profile'][:25]:
                print(f'  {entry["self_us"]:9.1f} self us {entry["cumulative_us"]:10.1f} cumulative '
                      f'{entry["calls"]:8.1f} calls  {entry["function"]}')
        return row
    finally:
        for scene in scenes.values():
            scene.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--trees', default='5:4', help='comma-separated depth:fanout trees')
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--warm', type=int, default=30)
    parser.add_argument('--seed', type=int, default=1027)
    parser.add_argument('--hot', action='store_true', help='also time unchanged frames and a parent-only redraw')
    parser.add_argument('--mounts', type=int, default=0, help='fresh trees to mount and time before the frame loop')
    parser.add_argument('--profile', action='store_true', help='cProfile the redraw variant; timings become inflated')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert args.frames > 0 and args.warm >= 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    from benchmark_gui_overhead import Graphics
    from benchmark_bare_gui import FixedSurface
    from meltygui.core.runtime.toggles import Toggles
    previous = Toggles.Rust.auto_cache
    Toggles.Rust.auto_cache = True
    graphics, surface = Graphics(), FixedSurface()
    try:
        rows = [measure(spec, args, graphics, surface) for spec in args.trees.split(',')]
        args.output.write_text(json.dumps(dict(renderer=graphics.renderer_name, frames=args.frames, warm=args.warm,
                                               seed=args.seed, results=rows), indent=2) + '\n')
    finally:
        Toggles.Rust.auto_cache = previous
        graphics.close()


if __name__ == '__main__':
    main()
