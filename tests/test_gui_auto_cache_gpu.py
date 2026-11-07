"""Shared replay, raster transitions and input continuity on a reserved desktop."""
import numpy as np
import pytest
import meltygui_imgui as imgui
from conftest import _ensure_gl_context
from test_retained_gui_gpu import pixels
from test_gui_imgui_widgets import click, frame
from meltygui.core.runtime.toggles import Toggles
from meltygui.core.rendering.retained_gui_prototype import RetainedGui


@pytest.fixture
def cache(monkeypatch):
    _ensure_gl_context()
    monkeypatch.setattr(Toggles.Rust, 'auto_cache', True)
    cache=RetainedGui(calibration=False)
    yield cache
    cache.close()


@pytest.mark.parametrize('alpha',[False,True])
@pytest.mark.parametrize('position',[(2.2,3.2),(2.7,3.7),(8.5,5.5)])
def test_shared_order_clipping_fractional_positions_and_transitions(cache,monkeypatch,alpha,position):
    ids={};calls=[]
    @cache.gui(width=31.4,height=22.6,tint=(1.,0.,0.,.5 if alpha else 1.))
    def child():
        ids['child']=cache.current;calls.append('child')
        dl=imgui.get_window_draw_list()
        dl.add_rect_filled(2,2,40,8,0x8000FF00 if alpha else 0xFF00FF00)
        for x in range(30): dl.add_rect_filled(x,12,x+1,20,0xFFFFFFFF if x%2 else 0xFF000000)
    @cache.gui(width=60,height=50,tint=(0.,0.,1.,.5 if alpha else 1.))
    def root():
        ids['root']=cache.current;calls.append('root')
        dl=imgui.get_window_draw_list();dl.add_rect_filled(0,0,60,40,0x80FFFFFF if alpha else 0xFF663322)
        child(left=position[0],top=position[1])
        dl.add_rect_filled(12,15,15,30,0xFF00FFFF)
    root()
    texture=cache.gpu.texture(ids['child'])
    context=cache.captures[ids['child']];state=cache.states[ids['child']]
    before=pixels(cache.gpu.texture(ids['root']),60,50)
    assert cache.gpu.representation_stats()==(2,1)
    for enabled in [False,True,False,True]:
        monkeypatch.setattr(Toggles.Rust,'auto_cache',enabled)
        cache.flush()
        after=pixels(cache.gpu.texture(ids['root']),60,50)
        # The legacy nested RGBA8 resolve rounds an intermediate alpha/color.
        # Opaque content must remain byte exact; translucent error is bounded.
        if alpha: assert np.max(np.abs(after.astype(int)-before.astype(int))) <= 2
        else: np.testing.assert_array_equal(after,before)
        assert cache.gpu.texture(ids['child'])==texture
        assert cache.captures[ids['child']] is context and cache.states[ids['child']] is state
        assert calls==['root','child']
    cache.invalidate_id(ids['child']);cache.flush()
    assert calls==['root','child','child']
    np.testing.assert_array_equal(pixels(cache.gpu.texture(ids['root']),60,50),before)


def test_focus_and_text_survive_runtime_representation_changes(cache,monkeypatch):
    text=[''];ids={}
    @cache.gui(width=180,height=35)
    def editor():
        ids['editor']=cache.current
        _,text[0]=imgui.input_text('Edit',text[0],128)
    @cache.gui(width=220,height=80)
    def root(): editor()
    root();node=ids['editor'];context=cache.captures[node];state=cache.states[node]
    x,y=cache._position(node);click(cache,(x+10,y+10));frame(cache,text='one')
    assert text[0]=='one'
    for enabled,char in [(False,'X'),(True,'Y')]:
        monkeypatch.setattr(Toggles.Rust,'auto_cache',enabled);cache.flush()
        assert cache.imgui_input.focus==node
        frame(cache,text=char)
        assert cache.captures[node] is context and cache.states[node] is state
    assert len(text[0])==5


def test_reused_expensive_observation_promotes_without_body_execution(cache):
    ids={};calls=[]
    @cache.gui(width=40,height=30)
    def child():
        ids['child']=cache.current;calls.append('child')
        imgui.get_window_draw_list().add_rect_filled(0,0,40,30,0xFF00FF00)
    @cache.gui(width=80,height=60)
    def root():
        ids['root']=cache.current;child()
    root();node=ids['child'];reference=cache.gpu.texture(node)
    cache.calibration.observe('replay',1200,1000,2_000_000,view=node)
    for _ in range(12):
        cache.graph.request_composition(ids['root']);cache.flush()
    assert cache.gpu.is_raster(node) and cache.raster_policy.promotions==1
    assert calls==['child'] and cache.gpu.texture(node)==reference
    for _ in range(12): cache.invalidate_id(node);cache.flush()
    assert not cache.gpu.is_raster(node) and cache.raster_policy.demotions==1


def test_one_draw_heavy_fill_is_measured_on_its_first_shared_replay(cache):
    from test_gui_calibration_gpu import collect
    ids={}
    @cache.gui(width=256,height=256)
    def heavy():
        ids['heavy']=cache.current
        dl=imgui.get_window_draw_list()
        # ImGui merges these into one texture draw command. The real shader
        # still shades every overlapping rectangle, so command count is not cost.
        for _ in range(1024): dl.add_rect_filled(0,0,256,256,0x10FFFFFF)
    @cache.gui(width=300,height=300)
    def root():
        ids['root']=cache.current;heavy()
    # Arm before the first composition, after constructing the GL owner.
    from meltygui.core.rendering._gui_native import TextureCache
    cache.gpu=TextureCache();cache.gpu.calibration_arm()
    root()
    samples=collect(cache.gpu)
    assert len(samples)==1 and samples[0][1]==ids['heavy']
    assert samples[0][3]>0 and samples[0][4]>0
    assert not cache.gpu.is_raster(ids['heavy'])
    observation=next(row for row in cache.gpu.take_workloads(64) if row[0]==ids['heavy'])
    assert observation[-1]==1


def test_descendant_visual_changes_do_not_look_like_parent_reuse(cache):
    ids={};calls=[]
    @cache.gui(width=40,height=30)
    def leaf():
        ids['leaf']=cache.current
        imgui.get_window_draw_list().add_rect_filled(0,0,40,30,0xFF00FF00)
    @cache.gui(width=60,height=40)
    def owner():
        ids['owner']=cache.current;calls.append('owner');leaf()
    @cache.gui(width=80,height=60)
    def root(): owner()
    root();node=ids['owner']
    cache.calibration.observe('replay',2400,1_000_000,2_000_000,view=node)
    for _ in range(16): cache.invalidate_id(ids['leaf']);cache.flush()
    assert calls==['owner'] and not cache.gpu.is_raster(node)
    candidate=cache.raster_policy.candidates[node]
    assert candidate.dirty_fraction == 1.


def test_continuous_scroll_is_visual_refresh_without_body_recapture(cache):
    ids={};states={};calls=[]
    @cache.gui(width=40,height=20)
    def row(): imgui.get_window_draw_list().add_rect_filled(0,0,40,20,0xFF00FF00)
    @cache.gui(width=60,height=40,scroll=True)
    def viewport(draw_state=None):
        ids['viewport']=cache.current;states['viewport']=draw_state;calls.append('viewport')
        for i in range(8): row(key=i,left=0,top=i*20)
    @cache.gui(width=80,height=60)
    def root(): viewport()
    root();node=ids['viewport']
    cache.calibration.observe('replay',2400,1_000_000,2_000_000,view=node)
    for i in range(16):
        states['viewport'].scroll_offset=(0,20 if i%2 else 40);cache.flush()
    assert calls==['viewport'] and not cache.gpu.is_raster(node)
    assert cache.raster_policy.candidates[node].dirty_fraction == 1.


def test_optional_promotion_respects_budget_and_keeps_commands(cache):
    from meltygui.core.rendering._gui_native import TextureCache
    cache.gpu=TextureCache(budget=140_000);ids={}
    @cache.gui(width=100,height=100)
    def child():
        ids['child']=cache.current
        imgui.get_window_draw_list().add_rect_filled(0,0,100,100,0xFF00FF00)
    @cache.gui(width=128,height=128)
    def root(): ids['root']=cache.current;child()
    root();node=ids['child']
    before=pixels(cache.gpu.texture(ids['root']),128,128)
    cache.calibration.observe('replay',10_000,1_000_000,2_000_000,view=node)
    for _ in range(12): cache.graph.request_composition(ids['root']);cache.flush()
    assert not cache.gpu.is_raster(node) and cache.raster_policy.promotions==0
    assert cache.gpu.stats()[2]+cache.gpu.scratch_stats()[1]<=140_000
    np.testing.assert_array_equal(pixels(cache.gpu.texture(ids['root']),128,128),before)


def test_selected_ancestor_removes_redundant_child_raster(cache):
    ids={};calls=[]
    @cache.gui(width=40,height=30)
    def heavy():
        ids['heavy']=cache.current;calls.append('heavy')
        imgui.get_window_draw_list().add_rect_filled(0,0,40,30,0xFF00FF00)
    @cache.gui(width=60,height=40)
    def owner(): ids['owner']=cache.current;calls.append('owner');heavy()
    @cache.gui(width=80,height=60)
    def root(): ids['root']=cache.current;owner()
    root();references={name:cache.gpu.texture(node) for name,node in ids.items()}
    for name in ('heavy','owner'):
        cache.calibration.observe('replay',2400,1000,2_000_000,view=ids[name])
    for _ in range(16):cache.graph.request_composition(ids['root']);cache.flush()
    assert cache.gpu.is_raster(ids['owner']) and not cache.gpu.is_raster(ids['heavy'])
    assert cache.gpu.representation_stats()==(3,2)
    assert calls==['owner','heavy']
    assert {name:cache.gpu.texture(node) for name,node in ids.items()}==references
    for _ in range(12):cache.invalidate_id(ids['heavy']);cache.flush()
    assert not cache.gpu.is_raster(ids['owner'])
    assert calls.count('owner')==1


def test_mouse_capture_survives_mode_switch_until_release(cache,monkeypatch):
    clicks=[];ids={}
    @cache.gui(width=180,height=50)
    def button():
        ids['button']=cache.current
        if imgui.button('Click',80,25):clicks.append(True)
    @cache.gui(width=220,height=80)
    def root():button(left=0,top=0)
    root();node=ids['button'];x,y=cache._position(node);position=(x+20,y+10)
    frame(cache,position);frame(cache,position,down=True)
    assert cache.imgui_input.capture==node
    for enabled in (False,True):
        monkeypatch.setattr(Toggles.Rust,'auto_cache',enabled);cache.flush()
        assert cache.imgui_input.capture==node
    frame(cache,position)
    assert clicks==[True] and cache.imgui_input.capture is None


def test_portal_descendants_are_not_covered_by_an_external_cached_ancestor(cache):
    ids={}
    @cache.gui(width=40,height=30)
    def heavy():
        ids['heavy']=cache.current
        imgui.get_window_draw_list().add_rect_filled(0,0,40,30,0xFF00FF00)
    @cache.gui(width=60,height=40)
    def portal():ids['portal']=cache.current;heavy()
    @cache.gui(width=70,height=50,tint=(.2,.2,.2,1.))
    def owner():ids['owner']=cache.current;portal(melty_window=True,left=20,top=20)
    @cache.gui(width=100,height=100)
    def root():ids['root']=cache.current;owner()
    root()
    for name in ('owner','heavy'):
        cache.calibration.observe('replay',2400,1000,2_000_000,view=ids[name])
    for _ in range(16):cache.graph.request_composition(ids['root']);cache.flush()
    assert cache.gpu.is_raster(ids['owner'])
    for _ in range(16):cache.graph.request_composition(ids['portal']);cache.flush()
    assert cache.gpu.is_raster(ids['heavy'])
