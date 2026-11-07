"""Run existing state/input tests under the isolated profiling probes."""
import argparse
import ctypes
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core',type=Path)
    args,tests=parser.parse_known_args()
    trace=None
    if args.core:
        import meltygui_imgui as imgui
        library=ctypes.PyDLL(str(args.core))
        for name in ('new_frame','render','end'):
            direct=getattr(library,'probe_'+name);direct.argtypes=[];direct.restype=ctypes.c_double
            def operation(_name=name,_direct=direct):
                if _name=='new_frame':imgui.get_current_context()._keepalive_cache.clear()
                _direct()
            setattr(imgui,name,operation)
    else:
        import benchmark_gui_overhead as bench
        from profile_gui_phase_taxonomy import Trace
        trace=Trace();trace.install(bench);trace.enabled=True
    import pytest
    try:result=pytest.main(tests)
    finally:
        if trace:trace.close()
    raise SystemExit(result)


if __name__=='__main__':main()
