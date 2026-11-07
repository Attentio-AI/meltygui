"""Matched plain Python / public @gui tree benchmark.

CPU: .venv/bin/python tests/benchmark_gui_overhead.py --json-out /tmp/gui-cpu.json
GL: run with --graphics on a reserved agent desktop. No runtime changes or waits
outside this benchmark. See docs/GUI_OVERHEAD_BENCHMARK.md for interpretation.
"""
import argparse
import ctypes
from dataclasses import dataclass
import gc
import json
import platform
import random
from pathlib import Path
from statistics import median
from time import perf_counter_ns

from meltygui import gui
from meltygui.core.rendering.gui_prototype import _window
from meltygui.core.rendering.gui_window_prototype import _GuiWindow


@dataclass
class Node:
    number: int
    height: float
    top: float
    absolute_top: float
    children: tuple


def tree(depth, fanout, culled):
    numbers = iter(range(sum(fanout ** level for level in range(depth))))

    def make(level, height, top, absolute_top):
        number = next(numbers)
        child_height = height if culled else height / fanout
        children = tuple(make(level + 1, child_height, i * child_height, absolute_top + i * child_height)
                         for i in range(fanout)) if level + 1 < depth else ()
        return Node(number, height, top, absolute_top, children)

    return make(0, 512., 0., 0.)


def selected_nodes(root, manual=False):
    yield root
    for child in root.children:
        if not manual or child.top < root.height:
            yield from selected_nodes(child, manual)


def digest(number, work):
    value = number + 1
    for _ in range(work):
        value = (value * 1664525 + 1013904223) & 0xffffffff
    return value


class Scene:
    def __init__(self, root, variant, work, graphics, predeclare=False, wrapper=None,
                 local_coordinates=False, host_factory=None, gui_decorator=None):
        self.root, self.variant, self.graphics = root, variant, graphics
        self.count = self.checksum = 0
        self.host = None if variant.startswith('raw') else (host_factory or _GuiWindow)(graphics=graphics)
        self.manual = variant == 'raw_manual_cull'
        self.predeclare = predeclare and variant in ('gui_redraw', 'gui_hot')
        if self.host:
            # Explicit geometry makes automatic host cursor flow unnecessary.
            self.host.cache.auto_layout = False
        if graphics:
            from meltygui import imgui
        retained = variant in ('gui_redraw', 'gui_hot') or local_coordinates

        def body(input_value: Node, left=0., top=0., width=128., height=512., key=None):
            self.count += 1
            value = digest(input_value.number, work)
            self.checksum += value
            if graphics:
                # Cached draw lists are local; immediate lists share the host.
                x, y = 0., 0. if retained else input_value.absolute_top
                color = 0xff000000 | (value & 0xffffff)
                imgui.get_window_draw_list().add_rect_filled(x, y, x + 8, y + min(2., height), color)
            for child in input_value.children:
                view(child, key=child.number, left=0., top=0. if self.predeclare else child.top,
                     width=128., height=child.height)
            return False, input_value

        if self.host:
            view = (gui_decorator or gui)(body, use_cache=variant != 'gui_uncached', scroll=True)
        elif not self.manual:
            view = body if wrapper is None else wrapper(body)
        else:
            def view(input_value, *, key=None, left=0., top=0., width=128., height=512.):
                if self.manual and self.parents and top >= self.parents[-1][1]:
                    return False, input_value
                position = (0., 0.)
                self.parents.append((position, height))
                try:
                    return body(input_value, left=left, top=top, width=width, height=height)
                finally:
                    self.parents.pop()
        self.parents = []
        self.view = view

    def frame(self):
        self.count = self.checksum = 0
        invalidation_ns = 0
        if self.host:
            if self.variant == 'gui_redraw':
                start = perf_counter_ns()
                invalidated = self.host.cache.invalidate(self.view)
                invalidation_ns = perf_counter_ns() - start
            else:
                invalidated = 0
            token = _window.set(self.host)
            self.host.native.begin_frame(width=128., cache=self.host.cache)
            try:
                with self.host.cache.frame():
                    result = self.view(self.root, key=0, left=0., top=0., width=128., height=512.)
                self.host.cache.flush()
                if self.graphics:
                    self.host.cache.present((0., 0.))
            finally:
                self.host.native.end_frame()
                _window.reset(token)
        else:
            invalidated = 0
            result = self.view(self.root, key=0, left=0., top=0., width=128., height=512.)
        assert result[0] is False and result[1] is self.root
        return invalidation_ns, invalidated

    def close(self):
        if self.host:
            self.host.close()


class Graphics:
    def __init__(self):
        from conftest import _ensure_gl_context
        from meltygui import imgui
        from OpenGL import GL
        from OpenGL.raw.GL.VERSION.GL_3_3 import glGetQueryObjectui64v
        self.window, self.renderer = _ensure_gl_context()
        self.imgui, self.gl, self.query_result = imgui, GL, glGetQueryObjectui64v
        self.context = imgui.get_current_context()
        imgui.get_io().ini_file_name = None
        self.query = int(GL.glGenQueries(1)[0])
        self.renderer_name = GL.glGetString(GL.GL_RENDERER).decode()
        for _ in range(2):
            self.begin()
            self.submit()
            self.finish()

    def begin(self):
        imgui, gl = self.imgui, self.gl
        gl.glFinish()
        gl.glBeginQuery(gl.GL_TIME_ELAPSED, self.query)
        imgui.get_io().display_size = (800, 600)
        imgui.get_io().delta_time = 1 / 60
        imgui.new_frame()
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(128, 512)
        imgui.begin('benchmark', flags=imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE |
                    imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_SAVED_SETTINGS)
        imgui.set_cursor_screen_pos((0., 0.))
        imgui.get_window_draw_list().push_clip_rect(0, 0, 128, 512, False)

    def submit(self):
        self.imgui.get_window_draw_list().pop_clip_rect()
        self.imgui.end()
        self.imgui.render()
        self.gl.glViewport(0, 0, 800, 600)
        self.gl.glClear(self.gl.GL_COLOR_BUFFER_BIT)
        self.renderer.render(self.imgui.get_draw_data())

    def finish(self):
        self.gl.glEndQuery(self.gl.GL_TIME_ELAPSED)
        elapsed = ctypes.c_uint64()
        self.query_result(self.query, self.gl.GL_QUERY_RESULT, ctypes.byref(elapsed))
        return elapsed.value / 1000

    def pixels(self):
        return bytes(self.gl.glReadPixels(0, 88, 128, 512, self.gl.GL_RGBA, self.gl.GL_UNSIGNED_BYTE))

    def close(self):
        import glfw
        self.gl.glDeleteQueries(1, [self.query])
        self.imgui.set_current_context(self.context)
        self.renderer.shutdown()
        self.imgui.destroy_context(self.context)
        glfw.destroy_window(self.window)
        glfw.terminate()


def summary(values):
    ordered = sorted(values)
    center = median(values)
    return dict(median_us=center, p95_us=ordered[int(.95 * (len(ordered) - 1))],
                min_us=ordered[0], max_us=ordered[-1],
                mad_us=median(abs(value - center) for value in values))


def dispatch_lower_bound(frames, warm, calls=1000):
    """No draw list, counter, traversal or useful body work; loop is included."""
    def body(input_value: int):
        return False, input_value

    view = gui(body, use_cache=False)
    host = _GuiWindow(graphics=False)
    samples = {'raw': [], 'gui_uncached': []}
    try:
        for frame in range(frames + warm):
            for variant in (('raw', 'gui_uncached') if frame % 2 else ('gui_uncached', 'raw')):
                function = body if variant == 'raw' else view
                token = _window.set(host)
                host.native.begin_frame(width=128.)
                start = perf_counter_ns()
                for value in range(calls):
                    result = function(value)
                elapsed = (perf_counter_ns() - start) / 1000
                assert result == (False, calls - 1)
                if variant == 'gui_uncached':
                    assert host.native.stats()['calls'] == calls
                host.native.end_frame()
                _window.reset(token)
                if frame >= warm:
                    samples[variant].append(elapsed)
        return dict(calls=calls, samples_us=samples,
                    **{variant: dict(**summary(values), us_per_call=median(values) / calls)
                       for variant, values in samples.items()})
    finally:
        host.close()


def run_case(depth, fanout, work, culled, args, graphics, cost_snapshot=None):
    root = tree(depth, fanout, culled)
    all_nodes = list(selected_nodes(root))
    visible = list(selected_nodes(root, True))
    variants = ['raw', 'gui_uncached', 'gui_redraw', 'gui_hot']
    if culled:
        variants.insert(1, 'raw_manual_cull')
    scenes, results, pixels = {}, {}, {}
    try:
        for variant in variants:
            start = perf_counter_ns()
            scene = scenes[variant] = Scene(root, variant, work, bool(graphics), args.predeclare and culled)
            if scene.host and cost_snapshot is not None:
                scene.host.cache.calibration.enabled = False
                scene.host.cache.calibration.snapshot = cost_snapshot
            if graphics:
                graphics.begin()
            scene.frame()
            if graphics:
                graphics.submit()
                graphics.finish()
            if scene.predeclare:
                assert scene.count == len(all_nodes)
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
            mount_ms = (perf_counter_ns() - start) / 1e6
            nodes = visible if variant in ('raw_manual_cull', 'gui_redraw', 'gui_hot') else all_nodes
            expected_checksum = sum(digest(node.number, work) for node in nodes)
            assert (scene.count, scene.checksum) == (len(nodes), expected_checksum), (variant, scene.count, len(nodes))
            if graphics:
                pixels[variant] = graphics.pixels()
                assert pixels[variant] == pixels['raw'], ('rendered pixels differ', variant, depth, fanout)
            results[variant] = dict(variant=variant, mount_ms=mount_ms,
                                    expected_bodies=0 if variant == 'gui_hot' else len(nodes),
                                    expected_checksum=0 if variant == 'gui_hot' else expected_checksum,
                                    cpu_samples_us=[], gpu_samples_us=[], completed_samples_us=[], invalidation_samples_us=[])
        rng = random.Random(42)
        for frame in range(args.warm + args.frames):
            order = variants.copy()
            rng.shuffle(order)
            for variant in order:
                scene, result = scenes[variant], results[variant]
                gpu_cache = scene.host.cache.gpu if scene.host else None
                before_passes = gpu_cache.stats()[0] if gpu_cache else 0
                if graphics:
                    graphics.begin()
                start = perf_counter_ns()
                invalidate_ns, invalidated = scene.frame()
                if graphics:
                    graphics.submit()
                elapsed = (perf_counter_ns() - start) / 1000
                gpu_us = graphics.finish() if graphics else None
                completed_us = (perf_counter_ns() - start) / 1000
                assert (scene.count, scene.checksum) == (result['expected_bodies'], result['expected_checksum']), (variant, scene.count, result)
                if variant == 'gui_uncached':
                    assert scene.host.native.stats()['calls'] == len(all_nodes)
                if frame >= args.warm:
                    result['cpu_samples_us'].append(elapsed)
                    result['invalidation_samples_us'].append(invalidate_ns / 1000)
                    if gpu_us is not None:
                        result['gpu_samples_us'].append(gpu_us)
                        result['completed_samples_us'].append(completed_us)
                result['invalidated_nodes'] = invalidated
                result['raster_passes_per_frame'] = gpu_cache.stats()[0] - before_passes if gpu_cache else None
        for variant, result in results.items():
            scene = scenes[variant]
            result['cpu'] = summary(result['cpu_samples_us'])
            result['invalidation'] = summary(result['invalidation_samples_us'])
            if result['expected_bodies']:
                result['us_per_executed_body'] = result['cpu']['median_us'] / result['expected_bodies']
            result['body_mutations'] = 0
            result['call_attempts'] = (1 if variant == 'gui_hot' else
                                       1 + sum(len(node.children) for node in
                                               (visible if variant in ('gui_redraw', 'raw_manual_cull') else all_nodes)))
            if graphics:
                result['gpu'] = summary(result['gpu_samples_us'])
                result['completed'] = summary(result['completed_samples_us'])
            result['declared_nodes'] = len(scene.host.cache.records) if scene.host and variant != 'gui_uncached' else result['call_attempts']
            if scene.host and variant != 'gui_uncached':
                cache = scene.host.cache
                assert len(set(cache.uniques.values())) == len(cache.records)
                assert len(cache.records) == (len(all_nodes) if args.predeclare or not culled else
                                             1 + sum(len(node.children) for node in visible))
                result['retained_visible_nodes'] = len(cache.graph.visible_nodes())
                assert result['retained_visible_nodes'] == len(visible)
                if cache.gpu:
                    result['gpu_cache_stats'] = cache.gpu.stats()
                    result['representations'] = cache.gpu.representation_stats()
                    result['scratch_stats'] = cache.gpu.scratch_stats()
                    result['policy'] = dict(promotions=cache.raster_policy.promotions,
                        demotions=cache.raster_policy.demotions,evaluations=cache.raster_policy.evaluations)
                    result['imgui_contexts'] = len(cache.captures)
                    result['unique_ids'] = len(set(cache.uniques.values()))
            result['ratio_vs_raw'] = result['cpu']['median_us'] / results['raw']['cpu']['median_us']
            print(json.dumps(dict(depth=depth, fanout=fanout, work=work, culled=culled,
                                  logical_nodes=len(all_nodes), visible_nodes=len(visible),
                                  **{key: value for key, value in result.items() if not key.endswith('_samples_us')})), flush=True)
        return dict(depth=depth, fanout=fanout, work=work, culled=culled, predeclared=args.predeclare and culled,
                    logical_nodes=len(all_nodes), visible_nodes=len(visible), results=list(results.values()))
    finally:
        for scene in scenes.values():
            scene.close()
        gc.collect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trees', default='1:1,32:1,5:4,4:8', help='depth:fanout pairs')
    parser.add_argument('--work', default='0,64,512', help='deterministic integer operations per body')
    parser.add_argument('--visibility', choices=['both', 'visible', 'culled'], default='both')
    parser.add_argument('--frames', type=int, default=60)
    parser.add_argument('--warm', type=int, default=12)
    parser.add_argument('--graphics', action='store_true')
    parser.add_argument('--predeclare', action='store_true', help='mount the full retained tree before applying culling')
    parser.add_argument('--auto-cache',choices=['on','off'],default='on')
    parser.add_argument('--policy-matrix',action='store_true',help='Randomize matched legacy/default/calibrated groups, freeze costs before timing')
    parser.add_argument('--json-out', type=Path, required=True)
    args = parser.parse_args()
    graphics = Graphics() if args.graphics else None
    from meltygui.core.runtime.toggles import Toggles
    from meltygui.core.rendering.gui_calibration import DEFAULT_COSTS
    Toggles.Rust.auto_cache = args.auto_cache == 'on'
    calibrated = None
    calibration_report = None
    if args.policy_matrix:
        assert graphics, '--policy-matrix requires --graphics'
        from gui_cost_benchmark_support import calibrate_costs
        calibrated, calibration_report = calibrate_costs()
    report = dict(python=platform.python_version(), platform=platform.platform(),
                  renderer=graphics.renderer_name if graphics else None,
                  args={key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}, cases=[])
    report['calibration'] = calibration_report
    if not graphics:
        report['dispatch_lower_bound'] = dispatch_lower_bound(args.frames, args.warm)
    try:
        for specification in args.trees.split(','):
            depth, fanout = map(int, specification.split(':'))
            for work in map(int, args.work.split(',')):
                for culled in ([False, True] if args.visibility == 'both' else [args.visibility == 'culled']):
                    modes = [('legacy',False,DEFAULT_COSTS),('defaults',True,DEFAULT_COSTS),
                             ('calibrated',True,calibrated)] if args.policy_matrix else [(args.auto_cache,args.auto_cache=='on',DEFAULT_COSTS)]
                    random.Random(42+depth+work+int(culled)).shuffle(modes)
                    for label,enabled,costs in modes:
                        Toggles.Rust.auto_cache=enabled
                        case=run_case(depth,fanout,work,culled,args,graphics,costs)
                        case['policy_mode']=label
                        report['cases'].append(case)
                        args.json_out.write_text(json.dumps(report, indent=2) + '\n')
    finally:
        if graphics:
            graphics.close()


if __name__ == '__main__':
    main()
