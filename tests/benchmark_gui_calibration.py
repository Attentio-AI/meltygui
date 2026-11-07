"""Uninstrumented calibration overhead / correctness, on a reserved desktop.

Only the benchmark waits for GPU completion. Runtime uses its ordinary clock;
idle probes execute on existing frames, with no synthetic maintenance ticks.
"""
from time import perf_counter_ns, monotonic, sleep
STARTED = perf_counter_ns()
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import random
from statistics import median
import subprocess
import sys

from benchmark_gui_overhead import Graphics, Scene, tree, summary
from meltygui.core.rendering.gui_calibration import Calibration


def draw(graphics, scene):
    graphics.begin()
    start = perf_counter_ns()
    scene.frame()
    graphics.submit()
    cpu = (perf_counter_ns()-start)/1000
    gpu = graphics.finish()
    return cpu, gpu


def launch_sample(mode, path):
    graphics = Graphics()
    scene = Scene(tree(3,4,False),'gui_hot',0,True)
    scene.host.cache.calibration.enabled = mode != 'disabled'
    draw(graphics,scene)
    owner = scene.host.cache.calibration
    result = dict(mode=mode, launch_ms=(perf_counter_ns()-STARTED)/1e6,
                  bodies=scene.count, probes=owner.diagnostics['probes'], environment=owner.environment)
    assert owner.environment is None and owner.job is None and result['probes']==0
    path.write_text(json.dumps(result))
    scene.close()


def runtime(output):
    graphics = Graphics()
    report = dict(renderer=graphics.renderer_name, cases=[], launch=[])
    rng = random.Random(43)
    for variant, depth, frames in [('gui_hot',5,4000),('gui_redraw',3,1000)]:
        scenes = {}
        samples = {mode: [] for mode in ('disabled','defaults','passive','idle')}
        gpu_samples = {mode: [] for mode in samples}
        maintenance_samples = {mode: [] for mode in samples}
        try:
            for mode in samples:
                scene = Scene(tree(depth,4,False),variant,0,True)
                owner = scene.host.cache.calibration
                owner.directory = output.parent / (variant + '-' + mode)
                owner.enabled = mode != 'disabled'
                # Process-local benchmark modes select ordinary policy states.
                # This does not instrument render code or advance its clock.
                if mode == 'defaults': owner.next_tick = float('inf')
                if mode == 'passive': owner.refresh_at = float('inf')
                scenes[mode] = scene
                for _ in range(3): draw(graphics,scene)
            expected_pixels = graphics.pixels()
            for mode,scene in scenes.items():
                draw(graphics,scene)
                assert graphics.pixels() == expected_pixels
                # Measure steady state separately from the launch grace period.
                scene.host.cache.calibration.started = monotonic()-3
                scene.host.cache.calibration.last_input = monotonic()-3
            expected_bodies = 0 if variant == 'gui_hot' else 21
            start_run = monotonic()
            for index in range(frames):
                modes = list(scenes)
                rng.shuffle(modes)
                for mode in modes:
                    scene = scenes[mode]
                    owner = scene.host.cache.calibration
                    previous = owner.diagnostics['probes']
                    cpu,gpu = draw(graphics,scene)
                    assert scene.count == expected_bodies
                    samples[mode].append(cpu)
                    gpu_samples[mode].append(gpu)
                    if owner.diagnostics['probes'] != previous: maintenance_samples[mode].append(cpu)
                # Simulate already scheduled frames at a moderate cadence. Sleep
                # is outside every CPU/GPU measurement and never in production.
                sleep(.001)
            for mode,scene in scenes.items():
                owner = scene.host.cache.calibration
                draw(graphics,scene)
                assert graphics.pixels() == expected_pixels
                report['cases'].append(dict(variant=variant, mode=mode, frames=frames,
                    duration_seconds=monotonic()-start_run, bodies_per_frame=expected_bodies,
                    pixels_match=True, cpu=summary(samples[mode]), gpu=summary(gpu_samples[mode]),
                    probe_frame_cpu=summary(maintenance_samples[mode]) if maintenance_samples[mode] else None,
                    diagnostics=owner.diagnostics.copy(), measured_views=len(owner.views),
                    costs=asdict(owner.snapshot), cpu_samples_us=samples[mode]))
        finally:
            for scene in scenes.values(): scene.close()
    # Fresh interpreter/GL context pairs include import and first usable scene.
    for index in range(6):
        modes = ['disabled','enabled']; rng.shuffle(modes)
        for mode in modes:
            path = output.with_name(f'launch-{index}-{mode}.json')
            subprocess.run([sys.executable,str(Path(__file__).resolve()),'--launch-mode',mode,
                            '--output',str(path)],check=True,close_fds=False)
            report['launch'].append(json.loads(path.read_text()))
    report['launch_summary_ms'] = {mode: median(row['launch_ms'] for row in report['launch'] if row['mode']==mode)
                                  for mode in ('disabled','enabled')}
    output.write_text(json.dumps(report,indent=2))
    print(json.dumps(dict(renderer=report['renderer'],launch_medians=report['launch_summary_ms'],
        cases=[{key: row[key] for key in ('variant','mode','cpu','gpu','probe_frame_cpu','diagnostics','measured_views')}
               for row in report['cases']]),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--launch-mode',choices=('disabled','enabled'))
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.launch_mode: launch_sample(args.launch_mode,args.output)
    else: runtime(args.output)
