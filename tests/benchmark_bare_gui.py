"""Compare raw drawing, forwarding-only decorators, and full retained @gui.

This removes more than dependency injection: bare paths have no retained graph,
per-view contexts, caching, layout, input routing or state. Their gap to @gui
must not be attributed solely to injection. Run on a reserved agent desktop.
"""
import argparse
import gc
import json
from pathlib import Path
import random
from statistics import mean, median
from time import perf_counter_ns
import weakref


def bare_gui(function):
    """Forward explicit arguments; Python itself handles function defaults."""
    def wrapper(input_value, **kwargs):
        return function(input_value, **kwargs)
    return wrapper


def check_wrapper(decorator):
    """Check forwarding, error propagation, recursion and closure cycle cleanup."""
    value, keyword = [], object()
    def body(input_value, *, option=None):
        assert input_value is value and option is keyword
        input_value.append(option)
        return False, input_value
    wrapped = decorator(body)
    changed, result = wrapped(value, option=keyword)
    assert not changed and result is value and value == [keyword]
    try:
        wrapped(value, unexpected=True)
    except TypeError:
        pass
    else:
        raise AssertionError('Unknown arguments must reach the body and fail')
    error = ValueError('body error')
    def failing(input_value):
        raise error
    try:
        decorator(failing)(value)
    except ValueError as caught:
        assert caught is error
    else:
        raise AssertionError('Body exception was lost')
    def cycle():
        def recursive(input_value):
            return recursive(input_value - 1) if input_value else (False, 0)
        reference = weakref.ref(recursive)
        recursive = decorator(recursive)
        assert recursive(5) == (False, 0)
        return reference
    reference = cycle()
    gc.collect()
    assert reference() is None, 'Wrapper retained a function closure cycle'


class FixedSurface:
    """Same native layout seam as the existing retained comparisons."""
    def prototype_snapshot(self):
        return (((0., 128.), (0., 512.)), ((0., 128.), (0., 512.)), 'walls', 1, ((), ()))

    def prototype_commit(self, pairs):
        assert pairs == ((0., 128.), (0., 512.))

    def applied_origin(self, axis):
        return 0.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-path', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--frames', default=300, type=int)
    parser.add_argument('--warm', default=30, type=int)
    parser.add_argument('--work', default=0, type=int)
    parser.add_argument('--seed', default=1027, type=int)
    args = parser.parse_args()
    assert args.frames > 0 and args.warm >= 0
    from profile_gui_overhead import load_native, mount
    load_native(args.native_path)
    from meltygui.core.rendering._gui_native import BareGui
    from benchmark_gui_overhead import Scene, Graphics, tree, digest, summary
    from meltygui.core.runtime.toggles import Toggles
    check_wrapper(bare_gui)
    check_wrapper(BareGui)
    graphics = Graphics()
    surface = FixedSurface()
    scenes, rows = {}, {}
    previous_auto = Toggles.Rust.auto_cache
    Toggles.Rust.auto_cache = True
    expected = sum(digest(i, args.work) for i in range(341))
    def identities(scene):
        cache = scene.host.cache
        return ({n: id(v) for n, v in cache.states.items()},
                {n: id(v) for n, v in cache.captures.items()}, dict(cache.uniques))
    try:
        reference = None
        for label, wrapper in [('raw', None), ('bare_python', bare_gui),
                               ('bare_rust', BareGui), ('gui', None)]:
            scene = Scene(tree(5, 4, False), 'gui_redraw' if label == 'gui' else 'raw',
                          args.work, True, wrapper=wrapper)
            scenes[label] = scene
            if scene.host:
                scene.host.cache.calibration.enabled = False
                scene.host.solve_native(surface)
            mount(scene, graphics, False)
            pixels = graphics.pixels()
            if reference is None:
                reference = pixels
            assert pixels == reference, ('mount pixels', label)
            assert (scene.count, scene.checksum) == (341, expected)
            rows[label] = dict(cpu_samples_us=[], gpu_samples_us=[],
                               bodies=341, checksum=expected, pixels_match_raw=True)
        stable = identities(scenes['gui'])
        rng = random.Random(args.seed)
        order = list(scenes)
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
                assert (scene.count, scene.checksum) == (341, expected)
                if frame >= args.warm:
                    rows[label]['cpu_samples_us'].append(cpu)
                    rows[label]['gpu_samples_us'].append(gpu)
                if frame == args.warm + args.frames - 1:
                    assert graphics.pixels() == reference, ('final pixels', label)
        assert identities(scenes['gui']) == stable
        for label, row in rows.items():
            for device in ('cpu', 'gpu'):
                samples = row[device + '_samples_us']
                row[device] = dict(summary(samples), mean_us=mean(samples))
            differences = [a - b for a, b in zip(row['cpu_samples_us'], rows['raw']['cpu_samples_us'])]
            row['paired_over_raw_us'] = dict(mean=mean(differences), median=median(differences))
            print(label, row['cpu'], flush=True)
        report = dict(args={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                      renderer=graphics.renderer_name, results=rows,
                      wrapper_checks_passed=True, gui_identity_stable=True,
                      scope='CPU submission with native layout and idle input for full gui; '
                            'bare decorators forward explicit kwargs only. Not an injection-only ablation.')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
    finally:
        for scene in scenes.values():
            scene.close()
        graphics.close()
        Toggles.Rust.auto_cache = previous_auto


if __name__ == '__main__':
    main()
