"""Line-profile the full-cache explicit-argument declaration, with source mapping.

Requires the optional line-profiler tool. Run on a reserved agent desktop.
Call-site lines include called functions; execute/flush are reported separately
and excluded from the declaration subtotal because they include nested drawing.
"""
import argparse
import inspect
import json
from pathlib import Path
from time import perf_counter_ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-path', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--frames', type=int, default=50)
    parser.add_argument('--warm', type=int, default=24)
    args = parser.parse_args()
    assert args.frames > 0 and args.warm >= 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    from profile_gui_overhead import load_native, mount
    load_native(args.native_path)
    from benchmark_gui_overhead import Scene, Graphics, tree
    from benchmark_bare_gui import FixedSurface
    from cache_only_gui import CacheOnlyWindow, cache_gui
    from meltygui.core.rendering.retained_gui_prototype import RetainedGui
    from meltygui.core.runtime.toggles import Toggles
    from line_profiler import LineProfiler

    previous_auto = Toggles.Rust.auto_cache
    Toggles.Rust.auto_cache = True
    graphics, surface = Graphics(), FixedSurface()
    scene = Scene(tree(5, 4, False), 'gui_redraw', 0, True,
                  host_factory=CacheOnlyWindow, gui_decorator=cache_gui)
    cache = scene.host.cache
    cache.calibration.directory = args.output.parent / (args.output.stem + '-calibration')
    profiler = LineProfiler()
    try:
        scene.host.solve_native(surface)
        mount(scene, graphics, False)
        reference = graphics.pixels()
        stable = {node: id(capture) for node, capture in cache.captures.items()}
        resources = cache.gpu.stats()[2:]
        wrapper = scene.host.bindings[scene.view]
        profiler.add_function(wrapper)
        samples = []
        for index in range(args.warm + args.frames):
            before = cache.gpu.stats()[:2]
            graphics.begin()
            measured = index >= args.warm
            if measured:
                profiler.enable_by_count()
            start = perf_counter_ns()
            try:
                scene.host.solve_native(surface)
                cache.process_host_input()
                scene.frame()
                graphics.submit()
            finally:
                elapsed = (perf_counter_ns() - start) / 1000
                if measured:
                    profiler.disable_by_count()
            graphics.finish()
            after = cache.gpu.stats()[:2]
            assert (after[0] - before[0], after[1] - before[1]) == (1, 341)
            assert (scene.count, scene.checksum) == (341, 58311)
            if measured:
                samples.append(elapsed)
        assert graphics.pixels() == reference
        assert stable == {node: id(capture) for node, capture in cache.captures.items()}
        assert resources == cache.gpu.stats()[2:]

        source, first = inspect.getsourcelines(RetainedGui.gui)
        stats = profiler.get_stats()
        assert len(stats.timings) == 1
        timings = next(iter(stats.timings.values()))
        rows = []
        for line, hits, ticks in timings:
            text = source[line - 1].rstrip()
            rows.append(dict(line=first + line - 1, source=text,
                             hits_per_frame=hits / args.frames,
                             us_per_frame=ticks * stats.unit * 1e6 / args.frames,
                             ns_per_hit=ticks * stats.unit * 1e9 / hits,
                             nested_work=('self._execute(' in text or 'self.flush(' in text)))
        # Preserve every source line, including branches not hit in this workload.
        hit_rows = {row['line']: row for row in rows}
        start = wrapper.__code__.co_firstlineno
        stop = next(i for i, text in enumerate(source) if 'wrapper.__gui__ = True' in text)
        all_rows = [hit_rows.get(first + i, dict(line=first + i, source=source[i].rstrip(),
                    hits_per_frame=0, us_per_frame=0, ns_per_hit=0, nested_work=False))
                    for i in range(start - 1, stop)]
        report = dict(args={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                      renderer=graphics.renderer_name, cpu_samples_us=samples,
                      source_file=inspect.getsourcefile(RetainedGui), lines=all_rows,
                      declaration_profile_us=sum(row['us_per_frame'] for row in rows if not row['nested_work']),
                      checks=dict(pixels=True, bodies=341, packets=341, raster_refreshes=1,
                                  stable_captures=True, stable_resources=True),
                      warning='Instrumented line costs include callees and profiler perturbation. '
                              'Execute/flush lines include recursive drawing and must not be added to declaration.')
        args.output.write_text(json.dumps(report, indent=2))
        profiler.dump_stats(str(args.output.with_suffix('.lprof')))
        print('Declaration profile ms/frame:', report['declaration_profile_us'] / 1000)
        for row in sorted(rows, key=lambda row: row['us_per_frame'], reverse=True)[:20]:
            print(row['line'], round(row['us_per_frame'], 2), row['source'].strip())
    finally:
        scene.close()
        graphics.close()
        Toggles.Rust.auto_cache = previous_auto


if __name__ == '__main__':
    main()
