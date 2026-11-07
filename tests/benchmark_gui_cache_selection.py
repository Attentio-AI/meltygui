"""Matched retained single-command GPU work with frozen default/calibrated costs."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import random
from time import perf_counter_ns
from benchmark_gui_overhead import Graphics, summary
from gui_cost_benchmark_support import calibrate_costs
from meltygui import imgui
from meltygui.core.rendering.retained_gui_prototype import RetainedGui
from meltygui.core.rendering.gui_calibration import DEFAULT_COSTS, Estimate
from meltygui.core.runtime.toggles import Toggles


class Scene:
    def __init__(self,costs,auto,dirty,layers):
        self.auto,self.dirty=auto,dirty
        Toggles.Rust.auto_cache=auto
        self.cache=RetainedGui(calibration=False)
        self.cache.calibration.snapshot=costs
        self.ids={};self.calls={'root':0,'heavy':0,'sibling':0}
        @self.cache.gui(width=128,height=256)
        def heavy():
            self.ids['heavy']=self.cache.current;self.calls['heavy']+=1
            dl=imgui.get_window_draw_list()
            for _ in range(layers):dl.add_rect_filled(0,0,128,256,0x10FFFFFF)
        @self.cache.gui(width=128,height=32)
        def sibling():
            self.ids['sibling']=self.cache.current;self.calls['sibling']+=1
            imgui.get_window_draw_list().add_rect_filled(0,0,128,32,0xFF00FF00)
        @self.cache.gui(width=128,height=512)
        def root():
            self.ids['root']=self.cache.current;self.calls['root']+=1
            heavy(left=0,top=0);sibling(left=0,top=256)
        self.root=root
    def frame(self):
        Toggles.Rust.auto_cache=self.auto
        if self.ids:self.cache.invalidate_id(self.ids['heavy' if self.dirty else 'sibling'])
        self.root();self.cache.flush();self.cache.present((0,0))
    def close(self):self.cache.close()


def draw(graphics,scene):
    graphics.begin();start=perf_counter_ns();scene.frame();graphics.submit()
    cpu=(perf_counter_ns()-start)/1000;gpu=graphics.finish()
    return cpu,gpu


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frames',type=int,default=60)
    parser.add_argument('--layers',type=int,default=1024)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();graphics=Graphics();scenes={};results={}
    try:
        calibrated,calibration_report=calibrate_costs()
        # Actual workload observation is gathered from a required first shared
        # render. Test-only GPU completion happens in Graphics.finish().
        source=Scene(DEFAULT_COSTS,True,False,args.layers)
        from meltygui.core.rendering._gui_native import TextureCache
        source.cache.gpu=TextureCache();source.cache.gpu.calibration_arm()
        cold=draw(graphics,source);reference=graphics.pixels()
        rows=source.cache.gpu.calibration_poll()
        sample=next(row for row in rows if row[1]==source.ids['heavy'])
        actual=Estimate(sample[3],sample[4],True,sample[4] is not None)
        source.close()
        for label,auto,costs in [('legacy',False,DEFAULT_COSTS),('defaults',True,DEFAULT_COSTS),('calibrated',True,calibrated)]:
            for dirty in (False,True):
                key=label+('-dirty' if dirty else '-stable')
                scene=scenes[key]=Scene(costs,auto,dirty,args.layers)
                mount=draw(graphics,scene)
                assert graphics.pixels()==reference,key
                scene.cache.calibration.views[scene.ids['heavy']]=actual
                results[key]=dict(mode=label,dirty=dirty,cold_cpu_us=mount[0],cold_gpu_us=mount[1],
                    cold_representations=scene.cache.gpu.representation_stats(),cpu_samples_us=[],gpu_samples_us=[])
        rng=random.Random(42)
        for frame in range(24+args.frames):
            order=list(scenes);rng.shuffle(order)
            for key in order:
                cpu,gpu=draw(graphics,scenes[key])
                if frame>=24:
                    results[key]['cpu_samples_us'].append(cpu);results[key]['gpu_samples_us'].append(gpu)
        for key,scene in scenes.items():
            draw(graphics,scene);assert graphics.pixels()==reference,key
            result=results[key];cache=scene.cache
            result.update(cpu=summary(result['cpu_samples_us']),gpu=summary(result['gpu_samples_us']),
                representations=cache.gpu.representation_stats(),cache_stats=cache.gpu.stats(),
                scratch_stats=cache.gpu.scratch_stats(),calls=scene.calls,pixels_equal=True,
                promotions=cache.raster_policy.promotions,demotions=cache.raster_policy.demotions,
                identities=len(set(cache.uniques.values())))
            assert scene.calls['root']==1
            assert scene.calls['heavy']==(26+args.frames if scene.dirty else 1)
        report=dict(renderer=graphics.renderer_name,layers=args.layers,frames=args.frames,
            calibration=calibration_report,actual_workload=asdict(actual),default_cold_first_render_us=cold,
            results=list(results.values()))
        args.output.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps([{k:v for k,v in result.items() if not k.endswith('_samples_us')} for result in results.values()]))
    finally:
        for scene in scenes.values():scene.close()
        graphics.close()

if __name__=='__main__':main()
