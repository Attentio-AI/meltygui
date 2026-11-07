"""Behavioral controls for isolated follow-up candidates."""
from types import SimpleNamespace
import pytest
from meltygui.core.rendering._gui_native import EdgeGraph
from meltygui.core.rendering.gui_native_collision import NativeCollision
from meltygui.core.rendering.gui_window_prototype import _GuiWindow
from meltygui.core.rendering.retained_gui_prototype import RetainedGui


@pytest.mark.skipif(not hasattr(EdgeGraph,'revision'),reason='isolated native revision experiment')
def test_position_revision_tracks_all_successful_mutations():
    graph=EdgeGraph();a=graph.edge(1,'a',0);b=graph.edge(1,'b',100)
    graph.replace_cells(1,[(a,b,10,None)])
    before=graph.revision();graph.set_positions({a:0,b:100});assert graph.revision()==before
    graph.set_positions({a:5});assert graph.revision()!=before
    before=graph.revision();graph.solve(b,120,[a]);assert graph.revision()!=before
    before=graph.revision();graph.drag(1,b,10,[a]);assert graph.revision()!=before
    clone=graph.fork();assert clone.revision()==graph.revision()
    before=graph.revision()
    with pytest.raises(ValueError):graph.set_positions({a:float('nan')})
    assert graph.revision()==before


@pytest.mark.skipif(not getattr(NativeCollision.commit,'__gui_commit_candidate__',False),reason='isolated host commit experiment')
def test_host_coalesces_only_within_observation_and_tracks_adapter_and_rollback():
    host=_GuiWindow(graphics=False);calls=[]
    adapter=SimpleNamespace(prototype_snapshot=lambda:(((0.,300.),(0.,200.)),((0.,300.),(0.,200.)),'walls',1,((),())),
                            prototype_commit=lambda pairs:calls.append(pairs),applied_origin=lambda axis:0.)
    try:
        host.solve_native(adapter);assert len(calls)==1
        for _ in range(8):host.cache.geometry.apply()
        assert len(calls)==1
        host.solve_native(adapter);assert len(calls)==2
        native=host.cache.geometry.native;axis=host.cache.geometry.axes[0]
        axis.set_positions({native.pairs[0][1]:290});host.cache.geometry.apply();assert len(calls)==3
        other=SimpleNamespace(**adapter.__dict__);native.os_frame=other
        host.cache.geometry.apply();assert len(calls)==4
        # Abort replaces graph objects even when generations match.
        host.cache.geometry.axes=tuple(graph.fork() for graph in host.cache.geometry.axes)
        host.cache.geometry.apply();assert len(calls)==5
    finally:host.close()


def test_returned_state_alias_survives_first_repeat_and_replacement_input():
    cache=RetainedGui(graphics=False);calls=[]
    @cache.gui
    def view(input_value,draw_state=None):
        calls.append(input_value);return False,input_value
    first={};second={}
    try:
        _,_,state=view(first,return_extras=True)
        assert cache.invalidate(state)==1;cache.flush();assert len(calls)==2
        view(first);assert len(calls)==2
        assert cache.invalidate(state)==1;cache.flush();assert len(calls)==3
        view(second);assert len(calls)==4
        assert cache.invalidate(first)==0
        assert cache.invalidate(second)==1
    finally:cache.close()


def test_first_execution_of_previously_culled_view_registers_state_alias():
    cache=RetainedGui(graphics=False);calls=[];states=[]
    @cache.gui(width=50,height=20)
    def child(input_value,draw_state=None):
        calls.append(input_value);return False,input_value
    @cache.gui(width=50,height=20,scroll=True)
    def parent(input_value):
        _,_,state=child(input_value,top=100,return_extras=True)
        states[:]=[state]
        return False,input_value
    try:
        parent({})
        assert not calls
        # The pre-existing contract registers the state alias on first execution.
        assert cache.invalidate(states[0])==0
        states[0].top=0
        cache.flush()
        assert len(calls)==1
        assert cache.invalidate(states[0])==1
        cache.flush();assert len(calls)==2
    finally:cache.close()


def test_path_like_alias_is_recomputed_even_when_input_identity_is_unchanged():
    class MutableName(str):
        def __str__(self):return self.current
    value=MutableName('original');value.current='first'
    cache=RetainedGui(graphics=False)
    @cache.gui
    def view(input_value):return False,input_value
    try:
        view(value);value.current='second';view(value)
        assert cache.invalidate('second')==1
        assert cache.invalidate('first')==1
    finally:cache.close()
