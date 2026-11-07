"""CPU submission, GPU timer queries and residency for warm nested scrolling.

Run on an agent desktop: .venv/bin/python tests/benchmark_gui_scrolling.py
GPU waits occur only in this benchmark, never in the rendering path.
"""
import argparse
import ctypes
import json
from statistics import median
from time import perf_counter_ns

from meltygui.core.rendering.retained_gui_prototype import RetainedGui


def scene(count, depth, graphics, width, height):
    cache = RetainedGui(graphics=graphics)
    calls = [0, 0]
    states = []

    @cache.gui(width=width, height=20)
    def row(input_value):
        calls[0] += 1
        if graphics:
            from meltygui import imgui
            imgui.text(f'Row {input_value}')

    @cache.gui(width=width, height=height, scroll=True)
    def leaf(input_value, draw_state=None):
        calls[1] += 1
        states.append(draw_state)
        for i in range(count):
            row(i, key=i, left=0, top=i*20)

    def wrap(child):
        @cache.gui(width=width, height=height, scroll=True)
        def layer(input_value):
            calls[1] += 1
            child(None)
        return layer

    root = leaf
    for _ in range(depth-1): root = wrap(root)
    with cache.frame(): root(None)
    return cache, states[0], calls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cpu-only', action='store_true')
    parser.add_argument('--frames', type=int, default=100)
    parser.add_argument('--counts', default='100,1000,10000')
    parser.add_argument('--depths', default='1,8,32')
    parser.add_argument('--width', type=int, default=600)
    parser.add_argument('--height', type=int, default=400)
    args = parser.parse_args()
    graphics = not args.cpu_only
    if graphics:
        from conftest import _ensure_gl_context
        _ensure_gl_context()
        from OpenGL import GL
        from OpenGL.raw.GL.VERSION.GL_3_3 import glGetQueryObjectui64v
        print('GPU:', GL.glGetString(GL.GL_RENDERER).decode())
        query = int(GL.glGenQueries(1)[0])
    for count in map(int, args.counts.split(',')):
        for depth in map(int, args.depths.split(',')):
            started = perf_counter_ns()
            cache, state, calls = scene(count, depth, graphics, args.width, args.height)
            mount_ms = (perf_counter_ns()-started)/1e6
            try:
                for offset in (200, 0):
                    state.scroll_offset = (0, offset)
                    cache.flush()
                before = calls.copy()
                before_candidates = cache.graph.geometry_stats()[0]
                cpu, gpu = [], []
                for frame in range(args.frames):
                    if graphics: GL.glBeginQuery(GL.GL_TIME_ELAPSED, query)
                    started = perf_counter_ns()
                    state.scroll_offset = (0, 200 if frame % 2 == 0 else 0)
                    cache.flush()
                    cpu.append((perf_counter_ns()-started)/1000)
                    if graphics:
                        GL.glEndQuery(GL.GL_TIME_ELAPSED)
                        elapsed = ctypes.c_uint64()
                        glGetQueryObjectui64v(query, GL.GL_QUERY_RESULT, ctypes.byref(elapsed))
                        gpu.append(elapsed.value/1000)
                assert calls == before, (calls, before)
                stats = cache.gpu.stats() if graphics else (0, 0, 0, 0)
                print(json.dumps(dict(rows=count, depth=depth, viewport=[args.width, args.height], mount_ms=round(mount_ms, 2),
                    cpu_us=round(median(cpu), 2), gpu_us=round(median(gpu), 2) if gpu else None,
                    body_calls_during_scroll=0, texture_bytes=stats[2], packet_bytes=stats[3],
                    candidates_per_frame=(cache.graph.geometry_stats()[0]-before_candidates)/args.frames)), flush=True)
            finally:
                cache.close()
    if graphics: GL.glDeleteQueries(1, [query])


if __name__ == '__main__':
    main()
