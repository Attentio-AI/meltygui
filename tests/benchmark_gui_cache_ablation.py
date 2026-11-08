"""One-at-a-time and cumulative removals from the full cache annotation."""
import argparse
import json
from pathlib import Path
import random
from statistics import mean, median
from time import perf_counter_ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-path', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=200)
    parser.add_argument('--warm', type=int, default=24)
    parser.add_argument('--seed', type=int, default=1027)
    parser.add_argument('--mode', choices=('single', 'cumulative', 'detail', 'declaration'), default='single')
    args = parser.parse_args()
    assert args.frames > 0 and args.warm >= 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    from profile_gui_overhead import load_native, mount
    load_native(args.native_path)
    from benchmark_gui_overhead import Scene, Graphics, tree, summary
    from benchmark_bare_gui import FixedSurface
    from cache_only_gui import CacheOnlyWindow, cache_gui
    from cached_bare_gui import CaptureBridge
    from gui_cache_ablation import apply
    from meltygui.core.rendering._gui_native import CachedBareGui
    from meltygui.core.runtime.toggles import Toggles
    steps = ('input', 'layout', 'policy', 'scopes', 'declaration', 'lifetime', 'graph', 'execution_glue')
    variants = {'baseline': ()}
    if args.mode == 'declaration':
        variants.update((f'without_{step}', (step,)) for step in
                        ('identity_lookup', 'caller_copy', 'declaration_validation', 'state_configure',
                         'state_reads', 'argument_build', 'change_detection', 'invocation_snapshots'))
    elif args.mode == 'detail':
        variants.update((f'without_{step}', (step,)) for step in
                        ('input_finish', 'host_input', 'layout_snapshot', 'layout_commit', 'state_configure',
                         'validation', 'residency', 'retirement'))
    elif args.mode == 'single':
        variants.update((f'without_{step}', (step,)) for step in steps if step not in ('graph', 'execution_glue'))
    else:
        variants.update((f'step_{index+1}_{step}', steps[:index+1]) for index, step in enumerate(steps))
    variants.update(raw=(), minimal_cache=())
    scenes, rows, stable = {}, {}, {}
    graphics, surface = Graphics(), FixedSurface()
    previous_auto = Toggles.Rust.auto_cache
    Toggles.Rust.auto_cache = True
    bridge, controller = None, None
    def resources(cache):
        return dict(captures=len(cache.captures), packets_and_raster=list(cache.gpu.stats()[2:]),
                    representations=list(cache.gpu.representation_stats()), scratch=list(cache.gpu.scratch_stats()))
    try:
        reference = None
        for label, cuts in variants.items():
            if label == 'raw':
                scene = Scene(tree(5, 4, False), 'raw', 0, True)
            elif label == 'minimal_cache':
                bridge = CaptureBridge()
                def decorator(function):
                    nonlocal controller
                    controller = CachedBareGui(function, bridge)
                    return controller
                scene = Scene(tree(5, 4, False), 'raw', 0, True,
                              wrapper=decorator, local_coordinates=True)
            else:
                scene = Scene(tree(5, 4, False), 'gui_redraw', 0, True,
                              host_factory=CacheOnlyWindow, gui_decorator=cache_gui)
                scene.host.cache.calibration.directory = args.output.parent / (args.output.stem+'-profiles') / label
                scene.host.solve_native(surface)
            scenes[label] = scene
            mount(scene, graphics, False)
            pixels = graphics.pixels()
            if reference is None:
                reference = pixels
            assert pixels == reference, ('mount pixels', label)
            assert (scene.count, scene.checksum) == (341, 58311)
            rows[label] = dict(cpu_samples_us=[], gpu_samples_us=[], cuts=list(cuts))
            if scene.host:
                cache = scene.host.cache
                stable[label] = ({node: id(ctx) for node,ctx in cache.captures.items()}, resources(cache))
                if cuts:
                    rows[label]['ablation'] = apply(scene, cuts)
        rng, order = random.Random(args.seed), list(variants)
        for index in range(args.warm + args.frames):
            rng.shuffle(order)
            for label in order:
                scene = scenes[label]
                cache = scene.host.cache if scene.host else bridge if label == 'minimal_cache' else None
                before = cache.gpu.stats()[:2] if cache else None
                graphics.begin()
                start = perf_counter_ns()
                if scene.host:
                    scene.host.solve_native(surface)
                    scene.host.cache.process_host_input()
                elif label == 'minimal_cache':
                    controller.invalidate()
                scene.frame()
                graphics.submit()
                cpu = (perf_counter_ns() - start) / 1000
                gpu = graphics.finish()
                assert (scene.count, scene.checksum) == (341, 58311), (label, scene.count)
                if cache:
                    after = cache.gpu.stats()[:2]
                    assert (after[0]-before[0], after[1]-before[1]) == (1, 341), (label, before, after)
                if index >= args.warm:
                    rows[label]['cpu_samples_us'].append(cpu)
                    rows[label]['gpu_samples_us'].append(gpu)
                if index == args.warm + args.frames - 1:
                    assert graphics.pixels() == reference, ('final pixels', label)
        for label, row in rows.items():
            if scenes[label].host:
                cache = scenes[label].host.cache
                contexts, initial = stable[label]
                assert contexts == {node: id(ctx) for node,ctx in cache.captures.items()}
                assert resources(cache) == initial, (label, resources(cache), initial)
                row['resources'] = resources(cache)
            for device in ('cpu', 'gpu'):
                values = row[device+'_samples_us']
                row[device] = dict(summary(values), mean_us=mean(values))
            deltas = [a-b for a,b in zip(rows['baseline']['cpu_samples_us'],row['cpu_samples_us'])]
            row['saving_vs_baseline_us'] = dict(mean=mean(deltas), median=median(deltas))
            print(label, row['cpu'], flush=True)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(dict(args={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
                                              results=rows, renderer=graphics.renderer_name,
                                              checks=dict(pixels_equal=True,bodies=341,checksum=58311,
                                                          per_frame_rasters=1,per_frame_packets=341,
                                                          stable_captures_and_resources=True)),indent=2))
    finally:
        if controller is not None:
            controller.clear()
        if bridge is not None:
            bridge.close()
        for scene in scenes.values():
            # Restore production retirement before closing the mounted runtime.
            if scene.host:
                scene.host.cache.__dict__.pop('_retire', None)
            scene.close()
        graphics.close()
        Toggles.Rust.auto_cache = previous_auto


if __name__ == '__main__':
    main()
