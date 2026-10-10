"""Reproducible CPU comparison: python tests/benchmark_gui_prototype.py.

Headless ImGui, real styling, identical bodies, no texture cache or GPU timing.
The existing wrapper profiler supplies the isolated Melty frame harness.
"""
import argparse
import json
from pathlib import Path
import runpy
from statistics import median
from time import perf_counter_ns


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', default='25,100,300')
    parser.add_argument('--frames', type=int, default=40)
    parser.add_argument('--warm', type=int, default=12)
    parser.add_argument('--json-out', type=Path)
    args = parser.parse_args()
    harness = runpy.run_path(str(Path(__file__).with_name('profile_render_wrapper.py')))
    harness['_init_melty']()
    import meltygui_imgui as imgui
    from meltygui.examples.rust_gui_demo import make_views, python_gui, sample_data
    from functools import partial
    from native_gui_support import native_frame
    from meltygui.core.rendering.gui_prototype import _new_native, _bind_native

    imgui.get_io().ini_file_name = None
    runtime = _new_native()
    native_counts, python_counts = [0], [0]
    native = make_views(partial(_bind_native, runtime), native_counts)
    python = make_views(python_gui, python_counts)
    print('Headless identical-body benchmark (CPU wall time, cache off)')
    print('rows calls Python_ms Rust_ms ratio Rust_wrapper_us_per_call')
    results = []
    for rows in map(int, args.rows.split(',')):
        data = sample_data(rows)
        times = {'python': [], 'rust': []}
        wrapper = []
        for frame in range(args.warm + args.frames):
            for variant in (('python', 'rust') if frame % 2 == 0 else ('rust', 'python')):
                harness['_tick']()
                imgui.new_frame()
                imgui.set_next_window_size(1000, 800)
                imgui.begin('comparison')
                native_counts[0] = python_counts[0] = 0
                if variant == 'rust':
                    with native_frame(runtime,harness['HOST_DS'], width=420, mouse_pos=(-100, -100)):
                        start = perf_counter_ns()
                        changed, result = native['draw_collection'](data, name='Values')
                        elapsed = (perf_counter_ns() - start) / 1e6
                        stats = runtime.stats()
                        if frame >= args.warm:
                            wrapper.append(stats['wrapper_us'] / stats['calls'])
                    calls = native_counts[0]
                else:
                    start = perf_counter_ns()
                    changed, result = python['draw_collection'](
                        data, name='Values', width=420, mouse_pos=(-100, -100))
                    elapsed = (perf_counter_ns() - start) / 1e6
                    calls = python_counts[0]
                assert calls == 4 * rows + 2, (variant, rows, calls)
                assert not changed and result is data
                if harness['Melty'].channels_split:
                    imgui.get_window_draw_list().channels_merge()
                imgui.end()
                imgui.end_frame()
                if frame >= args.warm:
                    times[variant].append(elapsed)
        old, new = median(times['python']), median(times['rust'])
        print(f'{rows:4} {calls:5} {old:9.3f} {new:7.3f} {old/new:5.1f} {median(wrapper):10.3f}', flush=True)
        results.append({'rows': rows, 'calls': calls, 'python_median_ms': old,
                        'rust_median_ms': new, 'ratio': old/new,
                        'python_p95_ms': sorted(times['python'])[int(.95*(args.frames-1))],
                        'rust_p95_ms': sorted(times['rust'])[int(.95*(args.frames-1))],
                        'rust_wrapper_us_per_call': median(wrapper), 'samples_ms': times})
    runtime.clear()
    if args.json_out:
        args.json_out.write_text(json.dumps({'frames': args.frames, 'warm': args.warm,
                                            'benchmark': 'identical bodies, cache off, headless CPU',
                                            'results': results}, indent=2)+'\n')


if __name__ == '__main__':
    main()
