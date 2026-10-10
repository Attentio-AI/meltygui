"""Matched sticky-drag kernel benchmark; excludes graph construction and drawing."""
import argparse
import json
from pathlib import Path
from statistics import median
from time import perf_counter_ns

from meltygui.core.layout.edge_constraints import EdgeGraph as PythonGraph, solve_edge
from meltygui.core.rendering._gui_native import EdgeGraph


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cells',default='2,8,32,128,512')
    parser.add_argument('--frames',type=int,default=300)
    parser.add_argument('--warm',type=int,default=30)
    parser.add_argument('--json-out',type=Path)
    args=parser.parse_args()
    results=[]
    print('cells scenario Python_us Rust_us ratio',flush=True)
    for count in map(int,args.cells.split(',')):
        for scenario in ('local','pressure'):
            start=[100.*i for i in range(count+1)]
            py_edges=[{'x':p} for p in start]
            py=PythonGraph([(a,b,60.,140.) for a,b in zip(py_edges,py_edges[1:])])
            native=EdgeGraph()
            edges=[native.edge(1,str(i),p) for i,p in enumerate(start)]
            native.replace_cells(1,[(a,b,60.,140.) for a,b in zip(edges,edges[1:])])
            index=count//2
            walls={id(py_edges[0]),id(py_edges[-1])}
            native_walls=[edges[0],edges[-1]]
            # Prime the snapshot outside the measured interval, as on mouse-down.
            native.drag(1,edges[index],0.,native_walls)
            distance=30. if scenario=='local' else count*60.
            samples={'python':[],'rust':[]}
            for frame in range(args.warm+args.frames):
                delta=(distance,-distance,0.,distance,distance,0.)[frame%6]
                for variant in (('python','rust') if frame%2 else ('rust','python')):
                    at=perf_counter_ns()
                    if variant=='python':
                        for edge,value in zip(py_edges,start):edge['x']=value
                        solve_edge(py,py_edges[index],start[index]+delta,walls)
                    else:
                        native.drag(1,edges[index],delta,native_walls)
                    elapsed=(perf_counter_ns()-at)/1000.
                    if frame>=args.warm:samples[variant].append(elapsed)
                assert [p['x'] for p in py_edges]==[native.position(e) for e in edges]
            old,new=median(samples['python']),median(samples['rust'])
            print(f'{count:5} {scenario:8} {old:9.3f} {new:8.3f} {old/new:5.2f}',flush=True)
            results.append({'cells':count,'scenario':scenario,'python_median_us':old,
                            'rust_median_us':new,'ratio':old/new,'samples_us':samples})
    if args.json_out:
        args.json_out.write_text(json.dumps({'frames':args.frames,'warm':args.warm,
                                             'results':results},indent=2)+'\n')


if __name__=='__main__':main()
