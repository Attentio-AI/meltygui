"""Existing populated-layout benchmark with real native observation/commit hooks."""
from types import SimpleNamespace
import benchmark_gui_layout_transactions as benchmark
from meltygui.core.rendering.gui_native_collision import NativeCollision


class HostedRetainedGui(benchmark.RetainedGui):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.geometry.native=NativeCollision(self.geometry)
        self.adapter=SimpleNamespace(prototype_snapshot=lambda:(((0.,400.),(0.,200.)),((0.,1200.),(0.,900.)),'feed',1,((),())),
                                     prototype_commit=lambda pairs:None,applied_origin=lambda axis:0.)
        self.geometry.native.begin(self.adapter)

    def invalidate_id(self, unique):
        self.geometry.native.begin(self.adapter)
        return super().invalidate_id(unique)


if __name__=='__main__':
    benchmark.RetainedGui=HostedRetainedGui
    benchmark.main()
