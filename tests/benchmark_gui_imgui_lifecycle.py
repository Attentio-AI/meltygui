"""Compare full binding and direct ImGui lifecycle on dedicated live contexts.

Only texture-free no-op/rectangle/empty-label-button bodies are used. This is
a lifecycle microbenchmark, not a replacement retained renderer.
"""
import argparse
import ctypes
import json
from pathlib import Path
import random
import subprocess
from time import perf_counter_ns


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--imgui-source',type=Path,default=Path('/home/lukas/Desktop/meltygui-imgui'))
    parser.add_argument('--contexts',type=int,default=341)
    parser.add_argument('--frames',type=int,default=100)
    args=parser.parse_args()
    from benchmark_gui_overhead import Graphics,summary
    import meltygui_imgui as imgui
    graphics=Graphics()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    library=args.output.with_suffix('.so')
    source=Path(__file__).with_name('gui_imgui_lifecycle_probe.cpp')
    subprocess.run(['/usr/bin/g++','-std=c++17','-O2','-shared','-fPIC',str(source),
        '-I'+str(args.imgui_source/'imgui-cpp'),imgui.core.__file__,'-o',str(library)],
        check=True,close_fds=False)
    native=ctypes.PyDLL(str(library))
    native.probe_context.restype=ctypes.c_size_t
    native.probe_header_version.restype=ctypes.c_char_p
    assert native.probe_header_version().decode()==imgui.get_version()
    native.probe_cycles.argtypes=[ctypes.POINTER(ctypes.c_size_t),ctypes.c_int,ctypes.c_int,
                                 ctypes.c_int,ctypes.POINTER(ctypes.c_double)]
    native.probe_cycles.restype=ctypes.c_double
    host=imgui.get_current_context();atlas=imgui.get_io().fonts
    contexts=[];pointers=[]
    for _ in range(args.contexts):
        context=imgui.create_context(shared_font_atlas=atlas)
        contexts.append(context);imgui.set_current_context(context)
        io=imgui.get_io();io.ini_file_name=None;io.display_size=(128,64);io.delta_time=1/60
        pointers.append(native.probe_context())
    pointers=(ctypes.c_size_t*len(pointers))(*pointers)
    phases=(ctypes.c_double*7)()
    flags=(imgui.WINDOW_NO_TITLE_BAR|imgui.WINDOW_NO_RESIZE|imgui.WINDOW_NO_MOVE|
           imgui.WINDOW_NO_SCROLLBAR|imgui.WINDOW_NO_SAVED_SETTINGS|imgui.WINDOW_NO_SCROLL_WITH_MOUSE|
           imgui.WINDOW_NO_BACKGROUND)

    def python_cycle(context,content,commands):
        imgui.set_current_context(context);imgui.new_frame()
        imgui.set_next_window_position(0,0);imgui.set_next_window_size(128,64)
        imgui.begin('lifecycle',flags=flags);imgui.set_cursor_screen_pos((0,0))
        imgui.get_window_draw_list().push_clip_rect(0,0,128,64,False)
        imgui.push_id('gui-lifecycle');imgui.begin_group()
        if content==1:
            for _ in range(commands):imgui.get_window_draw_list().add_rect_filled(0,0,8,2,0xff0000ff)
        if content==2:imgui.button('##cell',width=8,height=2)
        imgui.end_group();imgui.pop_id();imgui.get_item_rect_size()
        imgui.get_window_draw_list().pop_clip_rect();imgui.end();imgui.render()

    def snapshot():
        result=[]
        for context in contexts:
            imgui.set_current_context(context)
            assert not context._keepalive_cache
            result.append([(ctypes.string_at(dl.vtx_buffer_data,dl.vtx_buffer_size*20),
                            ctypes.string_at(dl.idx_buffer_data,dl.idx_buffer_size*4),
                            [(int(c.texture_id),c.elem_count,tuple(c.clip_rect)) for c in dl.commands])
                           for dl in imgui.get_draw_data().commands_lists])
        return result

    report={}
    rng=random.Random(734)
    try:
        for name,content,commands in [('none',0,0),('rect',1,1),('rect16',1,16),('widget',2,1)]:
            samples={'python':[],'direct':[]};native_phases=[]
            for frame in range(24+args.frames):
                order=['python','direct'];rng.shuffle(order)
                for mode in order:
                    start=perf_counter_ns()
                    if mode=='python':
                        for context in contexts:python_cycle(context,content,commands)
                    else:native.probe_cycles(pointers,len(contexts),content,commands,phases)
                    elapsed=(perf_counter_ns()-start)/1000
                    if frame>=24:
                        samples[mode].append(elapsed)
                        if mode=='direct':native_phases.append(list(phases))
            for context in contexts:python_cycle(context,content,commands)
            reference=snapshot()
            native.probe_cycles(pointers,len(contexts),content,commands,phases)
            assert snapshot()==reference,('draw buffers',name)
            report[name]=dict(cpu={mode:summary(values) for mode,values in samples.items()},
                samples_us=samples,native_phase_mean_us={label:sum(row[i] for row in native_phases)/len(native_phases)/1000
                    for i,label in enumerate(['context_switch','new_frame','begin_window','group_enter','body','group_end_window_end','render'])},
                draw_buffers_match=True,contexts=len(contexts))
            print(name,report[name]['cpu'],flush=True)
        args.output.write_text(json.dumps(report,indent=2))
    finally:
        for context in contexts:imgui.destroy_context(context)
        imgui.set_current_context(host);graphics.close()


if __name__=='__main__':main()
