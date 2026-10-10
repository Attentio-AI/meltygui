"""Whole application comparison. Run each variant in a separate desktop process.

Uses public decorators and the unchanged app loop. The timer runs between
successive event-poll entries, enclosing dispatch, Surface.frame, GPU completion,
buffer swap and loop housekeeping. No rendering stages are disabled.
"""
import argparse
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter_ns, process_time_ns

parser = argparse.ArgumentParser()
parser.add_argument('--variant', choices=('python', 'rust'), required=True)
parser.add_argument('--cache', choices=('on', 'off'), default='on')
parser.add_argument('--scenarios', nargs='+', choices=('hot','row','redraw'), default=['hot','redraw'])
parser.add_argument('--rows', type=int, default=25)
parser.add_argument('--frames', type=int, default=40)
parser.add_argument('--warm', type=int, default=20)
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--inspect-seconds', type=float, default=0)
parser.add_argument('--profile', type=Path, help='Diagnostic cProfile of the selected measured phase (changes timing)')
parser.add_argument('--profile-phase', choices=('hot','row','redraw'), default='redraw')
parser.add_argument('--viewport', action='store_true', help='Give both root collections the window height')
args = parser.parse_args()
if args.profile and args.profile_phase not in args.scenarios:
    parser.error('--profile-phase must be included in --scenarios')
if args.profile:
    import cProfile
    profiler = cProfile.Profile()

import meltygui
from meltygui import gui, os_window
from meltygui.core.runtime import app
from meltygui.core.windowing.surface import Surface
from meltygui.core.windowing import window_api
from meltygui.core.melty import Melty
from meltygui.examples.rust_gui_demo import make_views, python_gui, sample_data
from OpenGL import GL as gl

counts = [0]
data = sample_data(args.rows)


def decorate(func=None, **options):
    if func is None:
        return lambda fn: decorate(fn, **options)
    options['use_cache'] = args.cache == 'on'
    # The retained decorator needs explicit content-driven height for collections.
    # Apply the same declaration to the production wrapper.
    options.setdefault('height', None)
    return (gui if args.variant == 'rust' else python_gui)(func, **options)


views = make_views(decorate, counts)


def root(input_value: object = None, draw_state=None):
    size = {'height': draw_state.height} if args.viewport else {}
    changed, value = views['draw_collection'](
        data, name='Values', key='values', width=max(80, draw_state.content_width - 12), **size)
    assert value is data
    return changed, input_value


host_options = dict(name=f'Full renderer benchmark {args.variant}', width=1000, height=800,
                    app_id=f'meltygui-full-benchmark-{args.variant}-{os.getpid()}')
if args.variant == 'rust':
    root = os_window(**host_options, use_cache=args.cache == 'on')(root)
else:
    from meltygui.core.core_render import render_func
    root = meltygui.glfw_window(**host_options)(render_func(use_cache=args.cache == 'on')(root))

original_process_events = app._process_events
original_frame = Surface.frame
state = {'start': None, 'pending': None, 'index': 0, 'phase': 0, 'results': [], 'samples': []}
phases = tuple(args.scenarios)


def event_poll(*positional, **kwargs):
    now = perf_counter_ns()
    pending = state['pending']
    if pending is not None:
        pending['wall_ms'] = (now - state['start']) / 1e6
        state['pending'] = None
        if state['index'] >= args.warm:
            state['samples'].append(pending)
        state['index'] += 1
        if state['index'] == args.warm + args.frames:
            samples = state['samples']
            times = sorted(sample['wall_ms'] for sample in samples)
            result = {'scenario': phases[state['phase']], 'median_ms': median(times),
                      'p95_ms': times[int(.95 * (len(times) - 1))], 'samples': samples}
            state['results'].append(result)
            print(json.dumps({k: v for k, v in result.items() if k != 'samples'}), flush=True)
            state.update(index=0, phase=state['phase'] + 1, samples=[])
            if state['phase'] == len(phases):
                args.out.write_text(json.dumps({
                    'variant': args.variant, 'rows': args.rows, 'warm': args.warm,
                    'frames': args.frames, 'backend': window_api.backend_name(),
                    'viewport': args.viewport, 'profiled': bool(args.profile), 'cache': args.cache,
                    'profile_phase': args.profile_phase if args.profile else None,
                    'renderer': gl.glGetString(gl.GL_RENDERER).decode(),
                    'framebuffer_size': window_api.get_framebuffer_size(Surface.all[0].window),
                    'results': state['results'],
                }, indent=2) + '\n')
                for surface in Surface.all:
                    window_api.set_window_should_close(surface.window, True)
                state['phase'] = -1
    state['start'] = perf_counter_ns()
    return original_process_events(*positional, **kwargs)


def frame(surface):
    profiling = (args.profile and state['phase'] >= 0
                 and phases[state['phase']] == args.profile_phase and state['index'] >= args.warm)
    if profiling:
        profiler.enable()
    before = counts[0]
    frame_start = perf_counter_ns()
    cpu_start = process_time_ns()
    prototype = getattr(surface, '_gui_prototype', None)
    raster_before = prototype.cache.gpu.stats()[0] if prototype and prototype.cache.gpu else 0
    if surface.frames and state['phase'] >= 0 and phases[state['phase']] in ('row','redraw'):
        # Mutate real data; cache-on runs explicitly invalidate the associated
        # row or whole scene. All configured rendering stages remain enabled.
        data['Row 0000']['count'] += 1
        if args.cache == 'off':
            pass  # Bodies execute normally without explicit invalidation.
        elif phases[state['phase']] == 'row':
            if args.variant == 'rust':
                surface._gui_prototype.cache.invalidate(data['Row 0000'])
            else:
                surface.activate()
                Melty.cache.invalidate_by_obj(data['Row 0000'])
        elif args.variant == 'rust':
            cache = surface._gui_prototype.cache
            for node in tuple(cache.records):
                cache.invalidate_id(node)
        else:
            surface.activate()
            Melty.cache.invalidate_all()
    original_frame(surface)
    submit_end = perf_counter_ns()
    if not surface.closed and state['phase'] >= 0:
        gl.glFinish()  # include completion, not just CPU command submission
        finish_end = perf_counter_ns()
        prototype = getattr(surface, '_gui_prototype', None)
        state['pending'] = {
            'bodies': counts[0] - before,
            'global_cache_enabled': Melty.cache.enabled,
            'configured_cache': args.cache,
            'retained_nodes': len(prototype.cache.records) if prototype else None,
            'surface_ms': (finish_end - frame_start) / 1e6,
            'surface_cpu_ms': (process_time_ns() - cpu_start) / 1e6,
            'gpu_finish_ms': (finish_end - submit_end) / 1e6,
            'raster_passes': prototype.cache.gpu.stats()[0] - raster_before
                             if prototype and prototype.cache.gpu else 0 if prototype else None,
        }
        if surface.frames == 1 and args.inspect_seconds:
            from time import sleep
            sleep(args.inspect_seconds)  # first warmup frame only, for visual inspection
        surface.request_frame()
    if profiling:
        profiler.disable()


app._process_events = event_poll
Surface.frame = frame
meltygui.run()
if args.profile:
    profiler.dump_stats(str(args.profile))
