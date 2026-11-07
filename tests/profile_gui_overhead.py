"""Attribute the matched @gui workload without editing production modules.

See docs/GUI_OVERHEAD_PROFILING.md. GPU runs require a reserved agent desktop.
"""
import argparse
import ast
from contextlib import contextmanager
import cProfile
from functools import wraps
import importlib.util
import inspect
import json
from pathlib import Path
import pstats
import sys
import textwrap
from time import perf_counter_ns


def load_native(path):
    if path is None:
        return
    name = 'meltygui.core.rendering._gui_native'
    assert name not in sys.modules, 'Load the profiling extension before constructing a GUI runtime'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


class Phases:
    """Nested inclusive/exclusive wall timers; instrumentation stays process local."""
    def __init__(self):
        self.stack = []
        self.values = {}
        self.patches = []

    def wrap(self, label, function):
        @wraps(function)
        def measured(*args, **kwargs):
            frame = [perf_counter_ns(), 0]
            self.stack.append(frame)
            try:
                return function(*args, **kwargs)
            finally:
                elapsed = perf_counter_ns() - frame[0]
                self.stack.pop()
                if self.stack:
                    self.stack[-1][1] += elapsed
                value = self.values.setdefault(label, [0, 0, 0])
                value[0] += 1
                value[1] += elapsed
                value[2] += elapsed - frame[1]
        return measured

    def patch(self, cls, method, label):
        previous = getattr(cls, method)
        self.patches.append((cls, method, previous))
        setattr(cls, method, self.wrap(label, previous))

    @contextmanager
    def scope(self, label):
        frame = [perf_counter_ns(), 0]
        self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = perf_counter_ns() - frame[0]
            self.stack.pop()
            if self.stack:
                self.stack[-1][1] += elapsed
            value = self.values.setdefault(label, [0, 0, 0])
            value[0] += 1
            value[1] += elapsed
            value[2] += elapsed - frame[1]

    def install(self):
        from meltygui.core.rendering.retained_gui_prototype import RetainedGui
        from meltygui.core.rendering.gui_layout_prototype import GuiGeometry
        from meltygui.core.rendering import retained_gui_prototype, gui_window_prototype
        from benchmark_gui_overhead import Graphics
        original = RetainedGui._execute
        parsed = ast.parse(textwrap.dedent(inspect.getsource(original)))
        blocks = sorted((node for node in ast.walk(parsed) if isinstance(node, ast.If)
                         and ast.unparse(node.test) == 'self.graphics'), key=lambda node: node.lineno)
        assert len(blocks) == 4
        for block, label in zip(blocks, ('imgui.setup', 'imgui.finalize', 'capture.packet', 'capture.bookkeeping')):
            context = ast.Call(func=ast.Attribute(value=ast.Name(id='_profile_phases', ctx=ast.Load()),
                                                  attr='scope', ctx=ast.Load()),
                               args=[ast.Constant(label)], keywords=[])
            block.body = [ast.With(items=[ast.withitem(context_expr=context)], body=block.body)]
        namespace = {**original.__globals__, '_profile_phases': self}
        exec(compile(ast.fix_missing_locations(parsed), '<isolated-profile-execute>', 'exec'), namespace)
        self.patches.append((RetainedGui, '_execute', original))
        RetainedGui._execute = namespace['_execute']
        for method, label in [('_execute', 'retained.execute'), ('_compose', 'retained.compose'),
                              ('_evict_scroll_tiles', 'retained.eviction'), ('flush', 'retained.flush'),
                              ('present', 'retained.present'), ('invalidate', 'retained.invalidate'),
                              ('_retire', 'retained.retire'), ('_select_rasters', 'retained.policy'),
                              ('_track_capture', 'retained.capture_accounting')]:
            self.patch(RetainedGui, method, label)
        for method in ('begin', 'commit', 'captured'):
            self.patch(GuiGeometry, method, 'layout.' + method)
        self.patch(Graphics, 'submit', 'host.submit')
        previous = RetainedGui.gui
        self.patches.append((RetainedGui, 'gui', previous))

        def factory(cache, func=None, **kwargs):
            result = previous(cache, func, **kwargs)
            return self.wrap('retained.declaration', result) if func is not None else result
        RetainedGui.gui = factory
        bind = retained_gui_prototype._bind_native
        def measured_bind(native, func=None, **kwargs):
            result = bind(native, func, **kwargs)
            return self.wrap('native.dispatch_and_body', result) if func is not None else result
        for module in (retained_gui_prototype, gui_window_prototype):
            self.patches.append((module, '_bind_native', module._bind_native))
            module._bind_native = measured_bind

    def close(self):
        for cls, name, previous in reversed(self.patches):
            setattr(cls, name, previous)


def mount(scene, graphics, predeclare):
    if graphics:
        graphics.begin()
    scene.frame()
    if graphics:
        graphics.submit()
        graphics.finish()
    if scene.predeclare:
        scene.predeclare = False
        cache = scene.host.cache
        for node, state in cache.states.items():
            state.top = cache.graph.invocation(node)[0].top
        cache.invalidate(scene.view)
        if graphics:
            graphics.begin()
        scene.frame()
        if graphics:
            graphics.submit()
            graphics.finish()


def profile_rows(profiler, frames):
    result = []
    for (filename, line, name), (primitive, calls, own, cumulative, callers) in pstats.Stats(profiler).stats.items():
        result.append(dict(file=filename, line=line, function=name, calls=calls / frames,
                           primitive_calls=primitive / frames,
                           self_us=own * 1e6 / frames, cumulative_us=cumulative * 1e6 / frames))
    return sorted(result, key=lambda row: row['self_us'], reverse=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graphics', action='store_true')
    parser.add_argument('--culled', action='store_true')
    parser.add_argument('--predeclare', action='store_true')
    parser.add_argument('--depth', type=int, default=5)
    parser.add_argument('--fanout', type=int, default=4)
    parser.add_argument('--work', type=int, default=0)
    parser.add_argument('--frames', type=int, default=100)
    parser.add_argument('--warm', type=int, default=20)
    parser.add_argument('--variants', default='raw,gui_uncached,gui_redraw,gui_hot')
    parser.add_argument('--mode', choices=['plain', 'cprofile', 'phases', 'native'], default='plain')
    parser.add_argument('--native-path', type=Path)
    parser.add_argument('--uncached-dispatch', choices=['public', 'bound', 'native'], default='public')
    parser.add_argument('--eviction-limit', type=int)
    parser.add_argument('--eviction-rescan', action='store_true',
                        help='Benchmark control: force eligibility scanning on each over-budget flush')
    parser.add_argument('--eager-input-acknowledgement', action='store_true',
                        help='Benchmark control: read the retained result even for unchanged caller input')
    parser.add_argument('--auto-cache',choices=['on','off'],default='on')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    load_native(args.native_path)
    if args.eager_input_acknowledgement:
        from meltygui.core.rendering import retained_gui_prototype
        source = textwrap.dedent(inspect.getsource(retained_gui_prototype.RetainedGui.gui))
        old = '''            changed_input = not pending and not _same(old_input, input_value)
            if ((changed_input and not _same(self.graph.result(node, False)[1], input_value))'''
        new = '''            acknowledgement = _same(self.graph.result(node, False)[1], input_value)
            if ((not pending and not _same(old_input, input_value) and not acknowledgement)'''
        assert source.count(old) == 1
        namespace = dict(retained_gui_prototype.__dict__)
        exec(compile(source.replace(old, new), '<eager acknowledgement control>', 'exec'), namespace)
        retained_gui_prototype.RetainedGui.gui = namespace['gui']
    from benchmark_gui_overhead import Scene, Graphics, tree, selected_nodes, digest, summary
    from meltygui.core.runtime.toggles import Toggles
    Toggles.Rust.auto_cache = args.auto_cache == "on"
    graphics = Graphics() if args.graphics else None
    from meltygui.core.rendering import _gui_native
    phases = Phases() if args.mode == 'phases' else None
    if phases:
        phases.install()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(args={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}, results=[])
    reference_pixels = None
    if graphics:
        reference = Scene(tree(args.depth, args.fanout, args.culled), 'raw', args.work, True)
        try:
            mount(reference, graphics, False)
            reference_pixels = graphics.pixels()
        finally:
            reference.close()
    try:
        for variant in args.variants.split(','):
            root = tree(args.depth, args.fanout, args.culled)
            scene = Scene(root, variant, args.work, args.graphics, args.predeclare and args.culled)
            if scene.host:
                scene.host.cache.calibration.enabled = False
            profiler = cProfile.Profile() if args.mode == 'cprofile' else None
            try:
                mount(scene, graphics, args.predeclare)
                if scene.host and args.eviction_limit is not None:
                    scene.host.cache.max_scroll_tiles = args.eviction_limit
                if scene.host and args.eviction_rescan:
                    cache = scene.host.cache
                    evict = cache._evict_scroll_tiles
                    def rescan():
                        cache._eviction_inputs = None
                        evict()
                    cache._evict_scroll_tiles = rescan
                if variant == 'gui_uncached' and args.uncached_dispatch != 'public':
                    body = scene.view.__gui_definition__[0]
                    bound = scene.host.bindings[scene.view]
                    selected = bound if args.uncached_dispatch == 'bound' else bound.__gui_native__
                    for name, cell in zip(body.__code__.co_freevars, body.__closure__):
                        if name == 'view':
                            cell.cell_contents = selected
                    scene.view = selected
                visible = list(selected_nodes(root, True))
                logical = list(selected_nodes(root))
                executed = [] if variant == 'gui_hot' else (visible if variant in ('gui_redraw', 'raw_manual_cull') else logical)
                expected = len(executed), sum(digest(node.number, args.work) for node in executed)
                runtimes = ([scene.host.native] if variant == 'gui_uncached' else
                            [native for node, (native, _, _) in scene.host.cache.records.items()
                             if scene.host.cache.graph.visible(node)]) if scene.host else []
                samples, gpu_samples, wrapper_samples = [], [], []
                for frame in range(args.warm + args.frames):
                    if graphics:
                        graphics.begin()
                    if frame == args.warm:
                        if phases:
                            phases.values.clear()
                        if args.mode == 'native':
                            _gui_native.profile_reset()
                    if profiler and frame >= args.warm:
                        profiler.enable()
                    start = perf_counter_ns()
                    scene.frame()
                    if graphics:
                        graphics.submit()
                    elapsed = (perf_counter_ns() - start) / 1000
                    if profiler and frame >= args.warm:
                        profiler.disable()
                    gpu_us = graphics.finish() if graphics else None
                    assert (scene.count, scene.checksum) == expected
                    if frame >= args.warm:
                        samples.append(elapsed)
                        if graphics:
                            gpu_samples.append(gpu_us)
                        if scene.host and variant != 'gui_hot':
                            wrapper_samples.append(sum(runtime.stats()['wrapper_us'] for runtime in runtimes))
                row = dict(variant=variant, bodies=len(executed), logical_nodes=len(logical),
                           visible_nodes=len(visible), cpu=summary(samples), cpu_samples_us=samples,
                           native_wrapper=summary(wrapper_samples) if wrapper_samples else None)
                if graphics:
                    row['gpu'] = summary(gpu_samples)
                    row['pixels_match_raw'] = graphics.pixels() == reference_pixels
                    assert row['pixels_match_raw'], variant
                if scene.host:
                    cache = scene.host.cache
                    row['retained_resources'] = dict(declared=len(cache.records), captures=len(cache.captures),
                                                      scroll_tiles=len(cache.scroll_tiles), scroll_bytes=cache.scroll_tile_bytes,
                                                      layouts=len(cache.geometry.layouts), frames=len(cache.geometry.frames),
                                                      bindings=len(cache.geometry.bindings), windows=len(cache.geometry.windows))
                if phases:
                    row['phases'] = {label: dict(calls=v[0] / args.frames, inclusive_us=v[1] / args.frames / 1000,
                                                exclusive_us=v[2] / args.frames / 1000)
                                     for label, v in phases.values.items()}
                if profiler:
                    profile_path = args.output.with_name(args.output.stem + '-' + variant + '.prof')
                    profiler.dump_stats(str(profile_path))
                    row['profile'] = profile_rows(profiler, args.frames)
                if args.mode == 'native':
                    row['native_phases'] = _gui_native.profile_snapshot()
                report['results'].append(row)
                args.output.write_text(json.dumps(report, indent=2) + '\n')
                print(variant, 'median us', round(row['cpu']['median_us'], 2),
                      'native wrapper us', round(row['native_wrapper']['median_us'], 2) if row['native_wrapper'] else None,
                      'bodies', len(executed), flush=True)
            finally:
                scene.close()
    finally:
        if phases:
            phases.close()
        if graphics:
            graphics.close()


if __name__ == '__main__':
    main()
