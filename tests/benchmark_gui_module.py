"""What gui_module adds over a plain @gui view: matched trees of the same body.

    .venv/bin/python tests/benchmark_gui_module.py [--json-out /tmp/gui-module.json]

One body (count a run, then call the children) is built three ways:
- raw:    a plain function calling itself.
- gui:    the same function under @gui.
- module: a standalone app file hosted by gui_module, which hosts itself for its
          children. Two shapes: `melty` (an @os_window function is the frame)
          and `imgui` (its own GLFW loop around a draw function).

Layouts: `chain` is one view per level, `size` deep (the self reference);
`flat` is `size` views side by side. Modes:
- uncached: every view is immediate (use_cache=False) and runs every frame.
- redraw:   retained views whose input (`tick`) changes every frame, so all run.
- declare:  retained views under a parent that runs while they stay clean
            (flat only: a clean chain is declared once at its top).
- load:     the first frame, which for a module runs the file per view.
CPU only (no GL): this is dispatch cost, not drawing. Levers: --sizes, --frames.
"""
import argparse
import gc
import json
import sys
import tempfile
import types
from pathlib import Path
from statistics import median
from time import perf_counter_ns

from meltygui import gui, gui_module
from meltygui.core.rendering.gui_prototype import _window
from meltygui.core.rendering.gui_window_prototype import _GuiWindow

# What every body reports to, whichever namespace it runs in.
sink = sys.modules['gui_module_bench_sink'] = types.ModuleType('gui_module_bench_sink')
sink.count, sink.cached = 0, True
BOX = dict(left=0., top=0., width=64., height=64.)

APPS = {'melty': '''
import gui_module_bench_sink as sink
from meltygui import gui_module, os_window
levels = 0
fan = 1
tick = 0
child = gui_module(__file__, background=False, use_cache=sink.cached)


@os_window(name='bench', use_cache=sink.cached)
def app(width=None, height=None):
    sink.count += 1
    for index in range(fan if levels else 0):
        child(levels=levels - 1, fan=fan, tick=tick, key=index, left=0., top=0., width=64., height=64.)
''', 'imgui': '''
import glfw
import imgui
import gui_module_bench_sink as sink
from meltygui import gui_module
levels = 0
fan = 1
tick = 0
child = gui_module(__file__, background=False, use_cache=sink.cached)


def draw():
    sink.count += 1
    for index in range(fan if levels else 0):
        child(levels=levels - 1, fan=fan, tick=tick, key=index, left=0., top=0., width=64., height=64.)


def main():
    glfw.init()
    window = glfw.create_window(64, 64, 'bench', None, None)
    imgui.create_context()
    while not glfw.window_should_close(window):
        glfw.poll_events()
        imgui.new_frame()
        draw()
        imgui.render()
        glfw.swap_buffers(window)
    glfw.terminate()


main()
'''}


def body(input_value=None, levels=0, fan=1, tick=0, key=None, left=0., top=0., width=64., height=64.):
    sink.count += 1
    for index in range(fan if levels else 0):
        body.view(None, levels=levels - 1, fan=fan, tick=tick, key=index, **BOX)


class Scene:
    """One variant of one layout, in a host of its own."""

    def __init__(self, variant, layout, size, mode, folder):
        self.variant, self.layout, self.size, self.mode = variant, layout, size, mode
        cached = sink.cached = mode != 'uncached'
        self.host = None if variant == 'raw' else _GuiWindow(graphics=False)
        if self.host:
            self.host.cache.auto_layout = False     # explicit geometry, as in benchmark_gui_overhead.py
        if variant == 'raw':
            view = body
        elif variant == 'gui':
            view = gui(body, use_cache=cached)
        else:
            view = gui_module(folder / f'{variant}.py', background=False, use_cache=cached)
        self.view = view

        def stage(input_value=None, tick=0, key=None, left=0., top=0., width=64., height=64.):
            body.view = view if variant in ('raw', 'gui') else None
            moving = tick if mode == 'redraw' else 0
            if layout == 'chain':
                view(None, levels=size - 1, fan=1, tick=moving, key=0, **BOX)
            else:
                for index in range(size):
                    view(None, levels=0, fan=1, tick=moving, key=index, **BOX)

        self.stage = stage if variant == 'raw' else gui(stage, use_cache=cached)
        self.tick = 0

    def frame(self):
        self.tick += 1
        sink.count = 0
        start = perf_counter_ns()
        if self.host is None:
            self.stage(None, tick=self.tick)
        else:
            token = _window.set(self.host)
            self.host.native.begin_frame(width=128., cache=self.host.cache)
            try:
                with self.host.cache.frame():
                    self.stage(None, tick=self.tick, key=0, **BOX)
                self.host.cache.flush()
            finally:
                self.host.native.end_frame()
                _window.reset(token)
        return (perf_counter_ns() - start) / 1000, sink.count

    def close(self):
        if self.host:
            self.host.close()


def measure(layout, size, mode, variants, frames, warm, folder):
    """{variant: {'load_us': first frame per view, 'us': median frame per view}}; variants take turns."""
    scenes, rows = {}, {}
    ran = {variant: size if mode != 'declare' or variant == 'raw' else 0 for variant in variants}
    try:
        for variant in variants:
            scene = scenes[variant] = Scene(variant, layout, size, mode, folder)
            elapsed, count = scene.frame()
            assert count == size, (variant, layout, size, mode, count)
            rows[variant] = dict(load_us=elapsed / size, samples=[])
        gc.collect()
        gc.disable()
        for frame in range(frames + warm):
            order = variants if frame % 2 else variants[::-1]
            for variant in order:
                elapsed, count = scenes[variant].frame()
                assert count == ran[variant], (variant, layout, size, mode, count)
                if frame >= warm:
                    rows[variant]['samples'].append(elapsed)
    finally:
        gc.enable()
        for scene in scenes.values():
            scene.close()
    for row in rows.values():
        samples = row.pop('samples')
        row['us'] = median(samples) / size
        row['p95_us'] = sorted(samples)[int(.95 * (len(samples) - 1))] / size
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--sizes', default='1,8,64,512', help='views per layout')
    parser.add_argument('--frames', type=int, default=200)
    parser.add_argument('--warm', type=int, default=20)
    parser.add_argument('--json-out')
    args = parser.parse_args()
    variants = ['raw', 'gui', 'melty', 'imgui']
    results = []
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        for name, source in APPS.items():
            (folder / f'{name}.py').write_text(source)
        print(f'{"layout":6} {"size":>5} {"mode":9}' + ''.join(f'{variant:>10}' for variant in variants)
              + f'{"melty-gui":>11}{"imgui-gui":>11}   microseconds per view')
        for layout in ('chain', 'flat'):
            for size in map(int, args.sizes.split(',')):
                if layout == 'chain' and size > 128:
                    continue    # a deeper chain is a recursion limit, not a measurement
                for mode in ('uncached', 'redraw', 'declare'):
                    if mode == 'declare' and layout == 'chain':
                        continue
                    rows = measure(layout, size, mode, variants, args.frames, args.warm, folder)
                    results.append(dict(layout=layout, size=size, mode=mode, rows=rows))
                    for label, field in ((mode, 'us'), ('load', 'load_us')) if mode == 'redraw' else ((mode, 'us'),):
                        print(f'{layout:6} {size:5} {label:9}' + ''.join(f'{rows[variant][field]:10.2f}' for variant in variants)
                              + ''.join(f'{rows[variant][field] - rows["gui"][field]:+11.2f}' for variant in ('melty', 'imgui')))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=1))


if __name__ == '__main__':
    main()
