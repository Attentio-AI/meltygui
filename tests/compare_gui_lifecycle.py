"""Frame-interleaved ordinary versus fused retained ImGui lifecycle."""
import argparse
import json
from pathlib import Path
import random
from time import perf_counter_ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-path', type=Path)
    parser.add_argument('--python-baseline', type=Path, required=True, help='Retained module saved before lifecycle fusion')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=200)
    parser.add_argument('--warm', type=int, default=24)
    parser.add_argument('--work', type=int, default=0)
    parser.add_argument('--commands', type=int, default=1)
    parser.add_argument('--culled', action='store_true')
    parser.add_argument('--host-input', action='store_true')
    parser.add_argument('--native-layout', action='store_true',
                        help='Attach the actual solve_native bridge to a fixed surface adapter')
    args = parser.parse_args()
    from profile_gui_overhead import load_native, mount
    load_native(args.native_path)
    from meltygui.core.rendering import _gui_native
    assert not hasattr(_gui_native.RetainedGraph,'result_read_counts'), 'Performance runs require the uninstrumented build'
    import benchmark_gui_overhead as bench
    from profile_gui_phase_taxonomy import set_content
    set_content(bench, "rect", args.commands)
    from benchmark_gui_overhead import Scene, Graphics, tree, digest, selected_nodes, summary
    from meltygui.core.rendering import retained_gui_prototype as retained
    from meltygui.core.rendering.gui_layout_prototype import GuiGeometry
    from meltygui.core.runtime.toggles import Toggles
    baseline_source=args.python_baseline
    namespace={'__name__':'lifecycle_control'}
    exec(compile(baseline_source.read_text(),str(baseline_source),'exec'),namespace)
    old=namespace['RetainedGui']
    names=('_execute','_context')
    baseline=tuple(getattr(retained.RetainedGui,name) for name in names)
    methods={'control':tuple(getattr(old,name) for name in names),'fused':baseline}
    def restore(values):
        for name,value in zip(names,values):setattr(retained.RetainedGui,name,value)
    graphics = Graphics()
    scenes, rows = {}, {}
    class FixedSurface:
        """Deterministic platform seam; the real NativeCollision path stays active."""
        def prototype_snapshot(self):
            return (((0., 128.), (0., 512.)), ((0., 128.), (0., 512.)), 'walls', 1, ((), ()))

        def prototype_commit(self, pairs):
            assert pairs == ((0., 128.), (0., 512.))

        def applied_origin(self, axis):
            return 0.

    surface_adapter = FixedSurface()
    order=[('raw','control','raw',True),('control','control','gui_redraw',True),
           ('fused','fused','gui_redraw',True),('hot-control','control','gui_hot',True),
           ('hot-fused','fused','gui_hot',True)]
    reference = None
    try:
        for label, method, variant, auto in order:
            restore(methods[method])
            Toggles.Rust.auto_cache = auto
            scene = Scene(tree(5, 4, args.culled), variant, args.work, True, args.culled)
            scenes[label] = scene
            if scene.host:
                scene.host.cache.calibration.enabled = False
            start = perf_counter_ns()
            if args.native_layout and scene.host:
                scene.host.solve_native(surface_adapter)
            mount(scene, graphics, args.culled)
            cold_us = (perf_counter_ns() - start) / 1000
            pixels = graphics.pixels()
            if reference is None:
                reference = pixels
            assert pixels == reference, ('mount pixels', label)
            executed = [] if variant == 'gui_hot' else list(selected_nodes(scene.root, variant == 'gui_redraw'))
            rows[label] = dict(cold_us=cold_us, expected_bodies=len(executed),
                               expected_checksum=sum(digest(node.number, args.work) for node in executed),
                               cpu_samples_us=[], gpu_samples_us=[], pixels_match_raw=True)
        rng = random.Random(1027)
        identities={label:({n:id(v) for n,v in scene.host.cache.states.items()},
                            {n:id(v) for n,v in scene.host.cache.captures.items()},dict(scene.host.cache.uniques))
                    for label,scene in scenes.items() if scene.host}
        for frame in range(args.warm + args.frames):
            shuffled = list(order)
            rng.shuffle(shuffled)
            for label, method, variant, auto in shuffled:
                restore(methods[method])
                Toggles.Rust.auto_cache = auto
                scene, row = scenes[label], rows[label]
                graphics.begin()
                start = perf_counter_ns()
                if args.native_layout and scene.host:
                    scene.host.solve_native(surface_adapter)
                if args.host_input and scene.host:
                    scene.host.cache.process_host_input()
                scene.frame()
                graphics.submit()
                elapsed = (perf_counter_ns() - start) / 1000
                gpu = graphics.finish()
                assert (scene.count, scene.checksum) == (row['expected_bodies'], row['expected_checksum'])
                if frame >= args.warm:
                    row['cpu_samples_us'].append(elapsed)
                    row['gpu_samples_us'].append(gpu)
                if frame == args.warm + args.frames - 1:
                    assert graphics.pixels() == reference, ('final pixels', label)
        for label, scene in scenes.items():
            row = rows[label]
            row['cpu'], row['gpu'] = summary(row['cpu_samples_us']), summary(row['gpu_samples_us'])
            if scene.host:
                cache = scene.host.cache
                assert identities[label]==({n:id(v) for n,v in cache.states.items()},
                                           {n:id(v) for n,v in cache.captures.items()},dict(cache.uniques))
                row['stable_state_capture_identity']=True
                row['resources'] = dict(captures=len(cache.captures), ids=len(set(cache.uniques.values())),
                    gpu=cache.gpu.stats(), representations=cache.gpu.representation_stats(),
                    scratch=cache.gpu.scratch_stats(), native_layout=cache.geometry.native is not None,
                    native_axes=[axis.stats() for axis in cache.geometry.axes])
            print(label, row['cpu'], flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(args={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},
                                             results=rows), indent=2))
    finally:
        restore(baseline)
        for scene in scenes.values():
            scene.close()
        graphics.close()


if __name__ == '__main__':
    main()
