"""The specific 341-view comparison: raw, bare Rust, full cache annotation, @gui."""
import argparse
import json
from pathlib import Path
import random
from statistics import mean, median
from time import perf_counter_ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-path', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--warm', type=int, default=30)
    parser.add_argument('--seed', type=int, default=1027)
    args = parser.parse_args()
    from profile_gui_overhead import load_native, mount
    load_native(args.native_path)
    from benchmark_gui_overhead import Scene, Graphics, tree, summary
    from benchmark_bare_gui import FixedSurface
    from cache_only_gui import CacheOnlyWindow, CacheOnlyRetained, cache_gui
    from meltygui.core.rendering._gui_native import BareGui
    from meltygui.core.rendering.retained_gui_prototype import RetainedGui
    from meltygui.core.runtime.toggles import Toggles
    # This is the production cache, not a replacement selector or capture loop.
    methods = ('_execute', '_capture', '_select_rasters', '_compose', 'flush', '_retire',
               '_evict_scroll_tiles', 'process_host_input', 'present', 'invalidate_id')
    assert all(getattr(CacheOnlyRetained, name) is getattr(RetainedGui, name) for name in methods)
    graphics, surface = Graphics(), FixedSurface()
    scenes, rows, stable = {}, {}, {}
    old_auto = Toggles.Rust.auto_cache
    Toggles.Rust.auto_cache = True
    def identities(cache):
        return ({k: id(v) for k,v in cache.states.items()},
                {k: id(v) for k,v in cache.captures.items()}, dict(cache.uniques))
    def resources(cache):
        return dict(imgui_contexts=1, captures=len(cache.captures), records=len(cache.records),
                    gpu=cache.gpu.stats(), representations=cache.gpu.representation_stats(),
                    scratch=cache.gpu.scratch_stats(), policy_evaluations=cache.raster_policy.evaluations,
                    promotions=cache.raster_policy.promotions, demotions=cache.raster_policy.demotions,
                    calibration_enabled=cache.calibration.enabled,
                    observed_views=len(cache.calibration.views), cost_revision=cache.costs.revision)
    try:
        reference = None
        for label in ('raw', 'bare_rust', 'cache_annotation', 'gui'):
            hosted = label in ('cache_annotation', 'gui')
            scene = Scene(tree(5, 4, False), 'gui_redraw' if hosted else 'raw', 0, True,
                          wrapper=BareGui if label == 'bare_rust' else None,
                          host_factory=CacheOnlyWindow if label == 'cache_annotation' else None,
                          gui_decorator=cache_gui if label == 'cache_annotation' else None)
            scenes[label] = scene
            if scene.host:
                scene.host.cache.calibration.directory = args.output.parent / (args.output.stem+'-profiles') / label
                scene.host.solve_native(surface)
            mount(scene, graphics, False)
            pixels = graphics.pixels()
            if reference is None:
                reference = pixels
            assert pixels == reference, ('mount pixels', label)
            assert (scene.count, scene.checksum) == (341, 58311)
            rows[label] = dict(cpu_samples_us=[], gpu_samples_us=[])
            if scene.host:
                rows[label]['initial'] = resources(scene.host.cache)
                stable[label] = identities(scene.host.cache)
        rng, order = random.Random(args.seed), list(scenes)
        for frame in range(args.warm + args.frames):
            rng.shuffle(order)
            for label in order:
                scene = scenes[label]
                graphics.begin()
                start = perf_counter_ns()
                if scene.host:
                    scene.host.solve_native(surface)
                    scene.host.cache.process_host_input()
                scene.frame()
                assert graphics.imgui.get_current_context() is graphics.context
                graphics.submit()
                cpu = (perf_counter_ns() - start) / 1000
                gpu = graphics.finish()
                assert (scene.count, scene.checksum) == (341, 58311)
                if frame >= args.warm:
                    rows[label]['cpu_samples_us'].append(cpu)
                    rows[label]['gpu_samples_us'].append(gpu)
                if frame == args.warm + args.frames - 1:
                    assert graphics.pixels() == reference, ('final pixels', label)
        for label, row in rows.items():
            for device in ('cpu', 'gpu'):
                values = row[device + '_samples_us']
                row[device] = dict(summary(values), mean_us=mean(values))
            if scenes[label].host:
                cache = scenes[label].host.cache
                assert identities(cache) == stable[label]
                row['final'] = resources(cache)
                assert row['final']['policy_evaluations'] > 0
                assert row['final']['calibration_enabled']
                assert row['final']['gpu'][2:] == row['initial']['gpu'][2:]
            print(label, row['cpu'], flush=True)
        paired = [a-b for a,b in zip(rows['gui']['cpu_samples_us'], rows['cache_annotation']['cpu_samples_us'])]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(args={k: str(v) if isinstance(v, Path) else v
                                                   for k,v in vars(args).items()},
                                              renderer=graphics.renderer_name, results=rows,
                                              paired_saving_us=dict(mean=mean(paired), median=median(paired)),
                                              checks=dict(exact_pixels=True, bodies=341, checksum=58311,
                                                          stable_identities=True, production_cache_methods=True, single_imgui_context=True),
                                              scope='Same 341-view full redraw, one rectangle per body, no artificial work. '
                                                    'Full production auto cache and live calibration for both hosted paths. '
                                                    'Cache annotation forwards explicit arguments without injection.'), indent=2))
    finally:
        for scene in scenes.values():
            scene.close()
        graphics.close()
        Toggles.Rust.auto_cache = old_auto


if __name__ == '__main__':
    main()
