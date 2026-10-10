"""Current retained pipeline timings, including completed offscreen GL work.

Uses the two-column regression scene. This is not a core_render comparison:
it measures cache reuse, independent invalidation, and divider recapture.
Window presentation and OS event dispatch are excluded.
"""
import argparse
import json
from statistics import median
from time import perf_counter_ns
from pathlib import Path

from conftest import _ensure_gl_context
from test_gui_collision_layout import scene
from OpenGL import GL as gl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frames', type=int, default=100)
    parser.add_argument('--warm', type=int, default=20)
    parser.add_argument('--json-out', type=Path)
    args = parser.parse_args()
    _ensure_gl_context()
    renderer = gl.glGetString(gl.GL_RENDERER).decode()
    print(renderer, flush=True)
    results = []
    for scenario in ('hot', 'leaf', 'divider', 'full'):
        cache, root, calls, ids, layouts, _ = scene(True)
        samples = []
        try:
            cache.flush()
            for frame in range(args.warm + args.frames):
                before = calls.copy()
                gpu_before = cache.gpu.stats()[0]
                gl.glFinish()
                start = perf_counter_ns()
                if scenario == 'leaf':
                    cache.invalidate_id(ids['a'])
                elif scenario == 'divider':
                    cache.geometry.drag(layouts['main'], 1, 30 if frame % 2 else -30)
                elif scenario == 'full':
                    for node in ids.values():
                        cache.invalidate_id(node)
                root(None)
                cache.flush()
                gl.glFinish()
                elapsed = (perf_counter_ns() - start) / 1e6
                executed = {key: calls[key] - before[key] for key in ('root', 'a', 'b')}
                expected = {'hot': (0, 0, 0), 'leaf': (0, 1, 0),
                            'divider': (0, 1, 1), 'full': (1, 1, 1)}[scenario]
                assert tuple(executed.values()) == expected, (scenario, executed)
                draws = cache.gpu.stats()[0] - gpu_before
                assert draws == {'hot': 0, 'leaf': 2, 'divider': 3, 'full': 3}[scenario]
                if frame >= args.warm:
                    samples.append(elapsed)
            result = {'scenario': scenario, 'median_ms': median(samples),
                      'p95_ms': sorted(samples)[int(.95 * (len(samples) - 1))],
                      'executions': executed, 'raster_passes': draws, 'samples_ms': samples}
            results.append(result)
            print(f'{scenario:8} {result["median_ms"]:.3f} ms median '
                  f'{result["p95_ms"]:.3f} ms p95  {executed}  raster={draws}', flush=True)
        finally:
            cache.close()
    if args.json_out:
        args.json_out.write_text(json.dumps({'renderer': renderer, 'frames': args.frames,
                                            'warm': args.warm, 'results': results}, indent=2) + '\n')


if __name__ == '__main__':
    main()
