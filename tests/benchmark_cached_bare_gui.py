"""Compare explicit-invalidation per-view caching with raw, bare Rust and @gui.

Run on a reserved agent desktop. The minimal cache omits dependency tracking,
layout and input. It reuses production packet/raster machinery and 341 draw capture buffers in the host context.
"""
import argparse
import json
from pathlib import Path
import random
from statistics import mean
from time import perf_counter_ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-path', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--warm', type=int, default=30)
    parser.add_argument('--work', type=int, default=0)
    parser.add_argument('--seed', type=int, default=1027)
    args = parser.parse_args()
    assert args.frames > 0 and args.warm >= 0
    from profile_gui_overhead import load_native, mount
    load_native(args.native_path)
    from meltygui.core.rendering._gui_native import BareGui, CachedBareGui
    from benchmark_gui_overhead import Scene, Graphics, tree, digest, summary, selected_nodes
    from benchmark_bare_gui import FixedSurface
    from cached_bare_gui import CaptureBridge
    from meltygui.core.runtime.toggles import Toggles
    graphics, surface = Graphics(), FixedSurface()
    scenes, bridges, controllers, rows = {}, {}, {}, {}
    old_auto = Toggles.Rust.auto_cache
    Toggles.Rust.auto_cache = True
    hot = {'cached_hot', 'cached_recompose', 'gui_hot'}
    expected = sum(digest(i, args.work) for i in range(341))

    def frame(label, force=False):
        scene = scenes[label]
        graphics.begin()
        start = perf_counter_ns()
        if label == 'cached_redraw' or (force and label in controllers):
            controllers[label].invalidate()
        if scene.host:
            scene.host.solve_native(surface)
            scene.host.cache.process_host_input()
            if force:
                scene.host.cache.invalidate(scene.view)
        scene.frame()
        graphics.submit()
        cpu = (perf_counter_ns() - start) / 1000
        gpu = graphics.finish()
        return cpu, gpu

    def resources(label):
        if label in bridges:
            cache = bridges[label]
            count = controllers[label].count()
        elif scenes[label].host:
            cache = scenes[label].host.cache
            count = len(cache.uniques)
        else:
            return None
        return dict(captures=len(cache.captures), records=count, gpu=list(cache.gpu.stats()),
                    representations=list(cache.gpu.representation_stats()),
                    scratch=list(cache.gpu.scratch_stats()))

    try:
        reference = None
        for label in ('raw', 'bare_rust', 'cached_redraw', 'cached_hot',
                      'cached_recompose', 'gui_redraw', 'gui_hot'):
            wrapper = BareGui if label == 'bare_rust' else None
            cached = label.startswith('cached_')
            if cached:
                bridge = CaptureBridge()
                bridge.recompose = label == 'cached_recompose'
                bridges[label] = bridge
                def wrapper(function, bridge=bridge, label=label):
                    controllers[label] = CachedBareGui(function, bridge)
                    return controllers[label]
            variant = label if label.startswith('gui_') else 'raw'
            scene = Scene(tree(5, 4, False), variant, args.work, True,
                          wrapper=wrapper, local_coordinates=cached)
            scenes[label] = scene
            if scene.host:
                scene.host.cache.calibration.enabled = False
                scene.host.solve_native(surface)
            start = perf_counter_ns()
            mount(scene, graphics, False)
            cold = (perf_counter_ns() - start) / 1000
            pixels = graphics.pixels()
            if reference is None:
                reference = pixels
            assert pixels == reference, ('mount pixels', label)
            assert (scene.count, scene.checksum) == (341, expected)
            rows[label] = dict(cpu_samples_us=[], gpu_samples_us=[],
                               cold_mount_wall_us=cold, resources_after_mount=resources(label))
        rng, order = random.Random(args.seed), list(scenes)
        stable = {label: {key: id(ctx) for key, ctx in bridge.captures.items()}
                  for label, bridge in bridges.items()}
        for index in range(args.warm + args.frames):
            rng.shuffle(order)
            for label in order:
                before = resources(label)
                cpu, gpu = frame(label)
                scene = scenes[label]
                assert (scene.count, scene.checksum) == ((0, 0) if label in hot else (341, expected)), label
                if label in bridges:
                    after = resources(label)
                    assert after['gpu'][0] - before['gpu'][0] == (0 if label == 'cached_hot' else 1)
                    assert after['captures'] == after['records'] == 341
                    assert after['gpu'][2:] == before['gpu'][2:]
                    assert after['representations'] == before['representations']
                if index >= args.warm:
                    rows[label]['cpu_samples_us'].append(cpu)
                    rows[label]['gpu_samples_us'].append(gpu)
                if index == args.warm + args.frames - 1:
                    assert graphics.pixels() == reference, ('final pixels', label)
        for label in bridges:
            assert stable[label] == {key: id(ctx) for key, ctx in bridges[label].captures.items()}
        # Validate a real visual edit and explicit invalidation outside all timings.
        edited_reference = None
        for label, scene in scenes.items():
            nodes = list(selected_nodes(scene.root))
            nodes[-1].height = 1.
            frame(label, force=True)
            assert scene.count == 341
            assert scene.checksum == sum(digest(node.number, args.work) for node in nodes)
            pixels = graphics.pixels()
            if edited_reference is None:
                edited_reference = pixels
                assert edited_reference != reference
            assert pixels == edited_reference, ('edited pixels', label)
            if label in hot:
                frame(label)
                assert scene.count == 0
                assert graphics.pixels() == edited_reference, ('edited replay', label)
        for label in bridges:
            assert resources(label)['captures'] == resources(label)['records'] == 341
        for label, row in rows.items():
            for device in ('cpu', 'gpu'):
                samples = row[device + '_samples_us']
                row[device] = dict(summary(samples), mean_us=mean(samples))
            row['resources_final'] = resources(label)
            print(label, row['cpu'], flush=True)
        report = dict(args={key: str(value) if isinstance(value, Path) else value
                            for key, value in vars(args).items()},
                      renderer=graphics.renderer_name, results=rows,
                      checks=dict(exact_pixels=True, edited_pixels=True, hot_skips_bodies=True,
                                  stable_cached_captures=True, stable_cache_bytes=True),
                      scope='Explicit-invalidation, fixed-tree per-view caching; not full GUI policy '
                            'or dependency tracking. Capture bridge is Python; controller and GPU cache are Rust.')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
    finally:
        for controller in controllers.values():
            controller.clear()
        for bridge in bridges.values():
            bridge.close()
        for scene in scenes.values():
            scene.close()
        graphics.close()
        Toggles.Rust.auto_cache = old_auto


if __name__ == '__main__':
    main()
