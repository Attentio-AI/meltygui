"""Benchmark-only completed calibration. Waiting is never part of app rendering."""
from dataclasses import asdict
from time import monotonic, perf_counter_ns, sleep
from meltygui.core.rendering.gui_calibration import Calibration, DEFAULT_COSTS, OPERATIONS


def calibrate_costs():
    from OpenGL import GL as gl
    from meltygui.core.rendering._gui_native import TextureCache
    native=TextureCache();model=Calibration(enabled=False)
    started=perf_counter_ns();samples=[]
    try:
        for repeat in range(3):
            for size in (32,128,512):
                for operation in OPERATIONS:
                    assert native.calibration_probe(operation,size)
                    gl.glFlush()
                    deadline=monotonic()+2
                    while native.calibration_pending():
                        rows=native.calibration_poll()
                        samples.extend(rows)
                        for op,_,units,cpu,gpu in rows: model.observe(op,units,cpu,gpu)
                        assert monotonic()<deadline,'calibration query timed out'
                        if native.calibration_pending(): sleep(.001)
        assert all(cost.cpu.samples>=3 for cost in model.snapshot.operations)
        return model.snapshot,dict(elapsed_ms=(perf_counter_ns()-started)/1e6,
            samples=samples,costs=asdict(model.snapshot),environment=native.calibration_environment())
    finally:
        native.close()
