"""Full production caching with automatic selection, with/without body injection.

The fixed-body variant keeps declaration-time state and all cache services.
Only execution-time Renderer dispatch is bypassed, for Scene.body's fixed ABI.
"""
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
    parser.add_argument('--scenario', choices=('full', 'leaf', 'hot', 'recompose'), default='full')
    parser.add_argument('--calibration', choices=('live', 'frozen', 'calibrated'), default='live')
    parser.add_argument('--frames', type=int, default=200)
    parser.add_argument('--warm', type=int, default=30)
    parser.add_argument('--seed', type=int, default=1027)
    parser.add_argument('--commands', type=int, default=1)
    args = parser.parse_args()
    from profile_gui_overhead import load_native, mount
    load_native(args.native_path)
    import benchmark_gui_overhead as bench
    from profile_gui_phase_taxonomy import set_content
    set_content(bench, 'rect', args.commands)
    from benchmark_gui_overhead import Scene, Graphics, tree, digest, summary
    from benchmark_bare_gui import FixedSurface
    from gui_fixed_body_experiment import execution_methods
    from meltygui.core.rendering import retained_gui_prototype as retained
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.core.rendering.gui_prototype import _window
    original, fixed = execution_methods()
    graphics, surface = Graphics(), FixedSurface()
    calibrated, calibration_report, view_estimates = None, None, {}
    if args.calibration == 'calibrated':
        from gui_cost_benchmark_support import calibrate_costs
        calibrated, calibration_report = calibrate_costs()
    scenes, rows, leaves, stable = {}, {}, {}, {}
    old_auto = Toggles.Rust.auto_cache
    variants = {'full_auto': (original, True), 'fixed_auto': (fixed, True),
                'full_legacy': (original, False), 'fixed_legacy': (fixed, False)}
    expected_count = {'full': 341, 'leaf': 1, 'hot': 0, 'recompose': 0}[args.scenario]
    expected_checksum = {'full': 58311, 'leaf': digest(340, 0), 'hot': 0, 'recompose': 0}[args.scenario]

    def select(label):
        retained.RetainedGui._execute, Toggles.Rust.auto_cache = variants[label]

    def identity(scene):
        cache = scene.host.cache
        return ({node: id(state) for node, state in cache.states.items()},
                {node: id(ctx) for node, ctx in cache.captures.items()}, dict(cache.uniques))

    def diagnostics(scene):
        cache = scene.host.cache
        return dict(captures=len(cache.captures), gpu=cache.gpu.stats(),
                    representations=cache.gpu.representation_stats(), scratch=cache.gpu.scratch_stats(),
                    promotions=cache.raster_policy.promotions, demotions=cache.raster_policy.demotions,
                    evaluations=cache.raster_policy.evaluations,
                    cost_revision=cache.costs.revision, observed_views=len(cache.calibration.views),
                    calibration_enabled=cache.calibration.enabled,
                    calibration_diagnostics=cache.calibration.diagnostics)

    try:
        if calibrated is not None:
            # Sample every view on a separate scene so calibration replays do not
            # bias one comparison variant's workload counters or raster policy.
            from OpenGL import GL
            from meltygui.core.rendering._gui_native import TextureCache
            from meltygui.core.rendering.gui_calibration import Estimate
            Toggles.Rust.auto_cache = True
            source = Scene(tree(5, 4, False), 'gui_hot', 0, True)
            try:
                source.host.cache.calibration.enabled = False
                source.host.solve_native(surface)
                mount(source, graphics, False)
                cache = source.host.cache
                for _ in range(2 * len(cache.records)):
                    cache.gpu.calibration_arm()
                    cache.gpu.render_many([(node, (0., 0., 0., 0.)) for node in cache.root_ids])
                    GL.glFinish()  # Benchmark setup only, outside measured frames.
                    for operation, node, units, cpu, gpu in cache.gpu.calibration_poll():
                        if operation == 'replay' and node in cache.records:
                            number = cache.graph.invocation(node)[0].number
                            view_estimates[number] = Estimate(cpu, gpu, True, gpu is not None)
                    if len(view_estimates) == len(cache.records):
                        break
                assert len(view_estimates) == 341, 'Incomplete workload calibration'
            finally:
                source.close()
        reference = None
        for label in variants:
            select(label)
            scene = Scene(tree(5, 4, False), 'gui_redraw' if args.scenario == 'full' else 'gui_hot', 0, True)
            scenes[label] = scene
            cache = scene.host.cache
            cache.calibration.enabled = args.calibration == 'live'
            cache.calibration.directory = args.output.parent / (args.output.stem + '-profiles') / label
            if calibrated is not None:
                cache.calibration.snapshot = calibrated
            scene.host.solve_native(surface)
            start = perf_counter_ns()
            mount(scene, graphics, False)
            cold = (perf_counter_ns() - start) / 1000
            pixels = graphics.pixels()
            if reference is None:
                reference = pixels
            assert pixels == reference, ('mount pixels', label)
            assert (scene.count, scene.checksum) == (341, 58311)
            if calibrated is not None:
                for node in cache.records:
                    number = cache.graph.invocation(node)[0].number
                    if number in view_estimates:
                        cache.calibration.views[node] = view_estimates[number]
            leaves[label] = next(node for node in cache.records
                                 if cache.graph.invocation(node)[0].number == 340)
            stable[label] = identity(scene)
            rows[label] = dict(cpu_samples_us=[], gpu_samples_us=[], cold_mount_wall_us=cold,
                               initial=diagnostics(scene))
        rng = random.Random(args.seed)
        order = list(variants)
        for index in range(args.warm + args.frames):
            rng.shuffle(order)
            for label in order:
                select(label)
                scene, row = scenes[label], rows[label]
                cache = scene.host.cache
                before_renders = cache.gpu.stats()[0]
                graphics.begin()
                start = perf_counter_ns()
                scene.host.solve_native(surface)
                scene.host.cache.process_host_input()
                if args.scenario == 'leaf':
                    cache.invalidate_id(leaves[label])
                elif args.scenario == 'recompose':
                    for node in cache.root_ids:
                        cache.graph.request_composition(node)
                scene.frame()
                graphics.submit()
                cpu = (perf_counter_ns() - start) / 1000
                gpu = graphics.finish()
                assert (scene.count, scene.checksum) == (expected_count, expected_checksum), (label, scene.count)
                if index >= args.warm:
                    row['cpu_samples_us'].append(cpu)
                    row['gpu_samples_us'].append(gpu)
                    row['last_frame_rasters'] = cache.gpu.stats()[0] - before_renders
                if index == args.warm + args.frames - 1:
                    assert graphics.pixels() == reference, ('final pixels', label)
        for label, scene in scenes.items():
            select(label)
            assert identity(scene) == stable[label]
            rows[label]['final'] = diagnostics(scene)
            # A real independent leaf edit must update pixels without parent bodies.
            cache = scene.host.cache
            leaf = cache.graph.invocation(leaves[label])[0]
            leaf.number += 1000
            scene.count = scene.checksum = 0
            token = _window.set(scene.host)
            graphics.begin()
            try:
                cache.invalidate_id(leaves[label])
                cache.flush()
                cache.present((0., 0.))
            finally:
                _window.reset(token)
            graphics.submit()
            graphics.finish()
            assert (scene.count, scene.checksum) == (1, 1341)
            pixels = graphics.pixels()
            assert pixels != reference
            if label == 'full_auto':
                edited = pixels
            else:
                assert pixels == edited, ('independent edit', label)
        for label, row in rows.items():
            for device in ('cpu', 'gpu'):
                values = row[device + '_samples_us']
                row[device] = dict(summary(values), mean_us=mean(values))
            print(label, row['cpu'], row['final']['representations'], flush=True)
        paired = {}
        for name, left, right in [('body_auto', 'full_auto', 'fixed_auto'),
                                  ('auto_full', 'full_legacy', 'full_auto'),
                                  ('auto_fixed', 'fixed_legacy', 'fixed_auto')]:
            deltas = [a-b for a, b in zip(rows[left]['cpu_samples_us'], rows[right]['cpu_samples_us'])]
            paired[name] = dict(mean_us=mean(deltas), median_us=median(deltas),
                                fraction_faster=sum(value > 0 for value in deltas)/len(deltas))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(args={key: str(value) if isinstance(value, Path) else value
                                                   for key, value in vars(args).items()},
                                              renderer=graphics.renderer_name, results=rows, paired_savings=paired,
                                              completed_calibration=calibration_report,
                                              shared_view_observations=len(view_estimates),
                                              checks=dict(exact_pixels=True, independent_leaf_edit=True,
                                                          body_counts=True, stable_identity=True),
                                              scope='Full retained declaration/state, input, layout, capture, cache policy, '
                                                    'calibration and composition are preserved. Fixed variant bypasses '
                                                    'execution-time Renderer dispatch only.'), indent=2))
    finally:
        retained.RetainedGui._execute = original
        for scene in scenes.values():
            scene.close()
        graphics.close()
        Toggles.Rust.auto_cache = old_auto


if __name__ == '__main__':
    main()
