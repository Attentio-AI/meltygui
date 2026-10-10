"""Retained geometry ownership and independent layout recapture."""
from collections import Counter

import pytest

from meltygui.core.rendering.retained_gui_prototype import RetainedGui


def scene(graphics=False,freeze=False):
    cache=RetainedGui(graphics=graphics)
    calls=Counter(); ids={}; layouts={}; model={'show':True,'fail':False}

    @cache.gui(freeze_resize=freeze)
    def child(input_value, draw_state=None):
        key=input_value
        calls[key]+=1; ids[key]=cache.current
        if graphics:
            import meltygui_imgui as imgui
            imgui.get_window_draw_list().add_rect_filled(0,0,draw_state.width,draw_state.height,
                                                       0xFF0000FF if key=='a' else 0xFF00FF00)
        return False,input_value

    @cache.gui(width=400,height=200)
    def root(input_value):
        calls['root']+=1; ids['root']=cache.current
        if model['show']:
            layout=cache.geometry.declare(('a','b'),axis=0,key='main',mins=70,padding=5)
            layouts['main']=layout
            with layout:
                for key in ('a','b'):
                    with layout.cell(key):child(key,key=key)
        if model['fail']:raise RuntimeError('aborted owner')
        return False,input_value
    root(None)
    return cache,root,calls,ids,layouts,model


def test_retained_divider_changes_children_without_parent():
    cache,root,calls,ids,layouts,_=scene()
    try:
        layout=layouts['main']; edge=layout.edges[1]
        for dx in (60,60,500,40,0):
            cache.geometry.drag(layout,1,dx)
            cache.flush()
            position=min(330,200+dx)
            assert cache.geometry.axes[0].position(edge)==position
            assert cache.graph.info(ids['a'])['rect']==(5,5,position-10,190)
            assert cache.graph.info(ids['b'])['rect']==(position+5,5,390-position,190)
            assert calls['root']==1
        before=calls.copy()
        root(None);cache.flush()
        assert calls==before
    finally:cache.close()


def test_owner_reconciliation_and_abort_preserve_committed_geometry():
    cache,root,calls,ids,layouts,model=scene()
    try:
        old=layouts['main']; stats=[g.stats() for g in cache.geometry.axes]
        model.update(show=False,fail=True)
        cache.invalidate_id(ids['root'])
        with pytest.raises(RuntimeError,match='aborted'):root(None)
        assert cache.geometry.layouts[old.identity] is old
        assert [g.stats() for g in cache.geometry.axes]==stats
        assert set(cache.geometry.bindings)=={ids['a'],ids['b']}
        model['fail']=False
        root(None)
        assert not cache.geometry.layouts and not cache.geometry.bindings
        assert cache.geometry.axes[0].stats()[1]==0
    finally:cache.close()


def test_gpu_placement_replays_without_parent_execution():
    from conftest import _ensure_gl_context
    from test_retained_gui_gpu import pixels
    import numpy as np
    _ensure_gl_context()
    cache,root,calls,ids,layouts,_=scene(True)
    try:
        for dx in (60,-90,0):
            cache.geometry.drag(layouts['main'],1,dx);cache.flush()
            actual=pixels(cache.gpu.texture(ids['root']),400,200)
            cache.invalidate_id(ids['root']);root(None)
            expected=pixels(cache.gpu.texture(ids['root']),400,200)
            np.testing.assert_array_equal(actual,expected)
    finally:cache.close()


def attach_native(cache,width=400,height=200,mode='feed'):
    from meltygui.core.rendering.gui_native_collision import NativeCollision
    native=NativeCollision(cache.geometry)
    cache.geometry.native=native
    native.observe((((100.,100.+width),(100.,100.+height)),((0.,1200.),(0.,900.)),mode,1,((),())))
    return native


def test_divider_pushes_native_frame_and_reverses_exactly():
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        graph=cache.geometry.axes[0]
        start=graph.values()
        cache.geometry.drag(layouts['main'],1,600,token=9)
        assert graph.position(native.pairs[0][1])==970
        cache.flush()
        assert cache.graph.info(ids['root'])['rect'][2]==870
        cache.geometry.drag(layouts['main'],1,0,token=9)
        cache.flush()
        assert graph.values()==start
        assert cache.graph.info(ids['root'])['rect'][2]==400
    finally:cache.close()


def test_right_drag_selects_cell_and_native_other_axis():
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        geometry=cache.geometry
        geometry.pointer((70,60),right_down=True,right_pressed=True)
        geometry.pointer((120,85),right_down=True)
        assert geometry.axes[0].position(layouts['main'].edges[1])==350
        assert geometry.axes[1].position(native.pairs[1][1])==325
        geometry.pointer((70,60),right_down=True)
        assert geometry.axes[0].position(layouts['main'].edges[1])==300
        assert geometry.axes[1].position(native.pairs[1][1])==300
        geometry.pointer((70,60),right_released=True)
        assert geometry.gesture is None
    finally:cache.close()


def test_nested_rows_share_constraints_and_padding():
    cache=RetainedGui(graphics=False); layouts={}; ids={}
    @cache.gui
    def nested():
        ids['nested']=cache.current
        layouts['rows']=cache.geometry.declare(2,axis=1,key='r',mins=70,padding=0)
    @cache.gui(width=400,height=200)
    def root():
        ids['root']=cache.current
        layouts['columns']=cache.geometry.declare(2,axis=0,key='c',mins=80,padding=5)
        with layouts['columns'].cell(0):nested()
    try:
        root()
        r=layouts['rows']; c=layouts['columns']
        assert cache.geometry.layout_rect(r)==(5,5,190,190)
        cache.geometry.drag(c,1,50)
        cache.flush()
        assert cache.geometry.layout_rect(r)==(5,5,240,190)
        cache.geometry.drag(r,1,100)
        assert cache.geometry.axes[1].position(r.edges[1])==125
    finally:cache.close()


def window_scene():
    cache=RetainedGui(graphics=False);ids={};model={'nested':True}
    @cache.gui(width=100,height=60,min_width=50,min_height=30)
    def leaf():ids['leaf']=cache.current
    @cache.gui(width=200,height=120)
    def middle():
        ids['middle']=cache.current
        if model['nested']:leaf(melty_window=True,initial={'window_pos':(30,25)})
    @cache.gui(width=250,height=180,min_width=100,min_height=60)
    def window():
        ids['window']=cache.current
        middle()
    @cache.gui(width=600,height=450)
    def root():
        ids['root']=cache.current
        window(melty_window=True,initial={'window_pos':(80,50)})
    root()
    return cache,ids,model


def test_nested_window_movement_and_reversal_through_ordinary_view():
    cache,ids,_=window_scene()
    try:
        attach_native(cache,600,450)
        geometry=cache.geometry
        assert cache.window_rect(ids['leaf'])[:2]==(110,103)
        before=cache.window_rect(ids['leaf'])
        geometry.pointer((90,60),down=True,pressed=True)
        geometry.pointer((140,100),down=True)
        assert cache.window_rect(ids['window'])[:2]==(130,90)
        assert cache.window_rect(ids['leaf'])[:2]==(160,143)
        assert cache.graph.windows().index(ids['leaf'])>cache.graph.windows().index(ids['window'])
        geometry.pointer((140,100),down=True)
        assert cache.window_rect(ids['leaf'])[:2]==(160,143)
        geometry.pointer((90,60),down=True)
        assert cache.window_rect(ids['leaf'])==before
        geometry.pointer((90,60),released=True)
    finally:cache.close()


def test_native_containment_keeps_interleaved_window_edges():
    cache=RetainedGui(graphics=False);ids={}
    @cache.gui(width=180,height=160,min_width=80,min_height=60)
    def window(input_value):ids[input_value]=cache.current
    @cache.gui(width=600,height=450)
    def root():
        window('a',key='a',melty_window=True,initial={'window_pos':(20,40)})
        window('b',key='b',melty_window=True,initial={'window_pos':(90,120)})
    try:
        root();native=attach_native(cache,600,450)
        native.observe((((100,700),(100,320)),((0,1200),(0,900)),'feed',1,((),())))
        cache.flush()
        a=cache.window_rect(ids['a']);b=cache.window_rect(ids['b'])
        assert 0<=a[1]<b[1]<a[1]+a[3]+28<b[1]+b[3]+28<=220
        assert a[3]>=60 and b[3]>=60
    finally:cache.close()


def test_near_resize_carries_nested_window_once_and_is_stationary():
    cache,ids,_=window_scene()
    try:
        attach_native(cache,600,450)
        g=cache.geometry
        before=cache.window_rect(ids['leaf'])
        g.pointer((90,60),right_pressed=True,right_down=True,reverse=True)
        for _ in range(3):
            g.pointer((70,40),right_down=True)
            cache.flush()
            assert cache.window_rect(ids['leaf'])[:2]==(before[0]-20,before[1]-20)
        g.pointer((90,60),right_down=True)
        assert cache.window_rect(ids['leaf'])==before
    finally:cache.close()


def test_native_two_axis_sticky_totals_and_acknowledgement():
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        for delta in (10,10,0,-20):
            pair=native.last
            native.observe((pair,((0.,1200.),(0.,900.)),'feed',1,(((1,delta),),((1,delta),))))
        assert native.last==((100,500),(100,300))
        native.release()
    finally:cache.close()


def test_freeze_resize_clips_native_scale_and_settles_once():
    from conftest import _ensure_gl_context
    from test_retained_gui_gpu import pixels
    _ensure_gl_context()
    cache,root,calls,ids,layouts,_=scene(True,freeze=True)
    try:
        g=cache.geometry
        g.pointer((200,100),down=True,pressed=True)
        g.pointer((270,100),down=True)
        cache.flush()
        assert calls==Counter(root=1,a=1,b=1)
        actual=pixels(cache.gpu.texture(ids['root']),400,200)
        # Expanded left tile leaves blank space; pixels are not stretched.
        assert actual[100,150,0]>200
        assert actual[100,240,3]==0
        assert actual[100,285,1]>200
        g.pointer((270,100),released=True)
        cache.flush()
        assert calls==Counter(root=1,a=2,b=2)
        actual=pixels(cache.gpu.texture(ids['root']),400,200)
        assert actual[100,240,0]>200
    finally:cache.close()


@pytest.mark.parametrize('axis',[0,1])
def test_initial_fixed_and_maximum_spans(axis):
    cache=RetainedGui(graphics=False);layouts=[]
    @cache.gui(width=400,height=400)
    def root():
        layouts.append(cache.geometry.declare(3,axis=axis,key='limits',mins=50,
                                             maxes=(None,100,None),fixed=(80,None,None)))
    try:
        root()
        graph=cache.geometry.axes[axis];edges=layouts[0].edges
        assert graph.position(edges[1])-graph.position(edges[0])==80
        assert graph.position(edges[2])-graph.position(edges[1])<=100
        cache.geometry.drag(layouts[0],2,90)
        assert graph.position(edges[1])-graph.position(edges[0])==80
    finally:cache.close()


def test_capability_loss_cancels_gesture_and_becomes_fixed_boundary():
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        g=cache.geometry
        g.pointer((200,100),down=True,pressed=True)
        g.pointer((220,100),down=True)
        native.observe((((0,400),(0,200)),((0,1200),(0,900)),'walls',None,((),())))
        assert g.gesture is None
        g.drag(layouts['main'],1,900,token=20)
        assert g.axes[0].position(native.pairs[0][1])==400
        assert g.axes[0].position(layouts['main'].edges[1])==330
        with pytest.raises(ValueError,match='fixed native allocation'):
            native.observe((((0,100),(0,200)),((0,1200),(0,900)),'walls',None,((),())))
    finally:cache.close()


def test_replacing_outer_edges_rebuilds_borrowed_nested_layout():
    cache=RetainedGui(graphics=False);model={'keys':('a','b')};layouts={};ids={}
    @cache.gui
    def child(input_value):
        ids[input_value]=cache.current
        layouts[input_value]=cache.geometry.declare(2,axis=1,key='inner',mins=30,padding=0)
    @cache.gui(width=600,height=200)
    def root():
        ids['root']=cache.current
        layout=cache.geometry.declare(model['keys'],axis=0,key='outer',mins=80,padding=0)
        layouts['outer']=layout
        for key in model['keys']:
            with layout.cell(key):child(key,key=key)
    try:
        root();old=ids.copy()
        model['keys']=('b','a','c')
        cache.invalidate_id(ids['root']);root()
        assert ids['a']==old['a'] and ids['b']==old['b']
        for key,x in [('b',0),('a',200),('c',400)]:
            assert cache.geometry.layout_rect(layouts[key])==(x,0,200,200)
        cache.geometry.drag(layouts['outer'],1,40)
        cache.flush()
        assert cache.geometry.layout_rect(layouts['b'])==(0,0,240,200)
    finally:cache.close()


def test_late_native_move_does_not_add_stationary_cursor_motion():
    from types import SimpleNamespace
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        native.os_frame=SimpleNamespace(applied_origin=lambda axis:100.,prototype_commit=lambda pairs:None)
        g=cache.geometry
        # The first cell's left edge is the native left; its move is pending.
        g.pointer((60,70),right_pressed=True,right_down=True,reverse=True)
        for _ in range(3):
            g.pointer((30,70),right_down=True)
            assert g.axes[0].position(native.pairs[0][0])==70
        # After acknowledgement the same physical cursor has a new local x.
        native.os_frame.applied_origin=lambda axis:70. if axis=='x' else 100.
        g.pointer((60,70),right_down=True)
        assert g.axes[0].position(native.pairs[0][0])==70
    finally:cache.close()


def test_ordinary_view_reflow_carries_cached_portal_without_reexecution():
    from conftest import _ensure_gl_context
    import meltygui_imgui as imgui
    _ensure_gl_context()
    cache=RetainedGui();model={'x':15};ids={};calls=Counter()
    @cache.gui(width=100,height=60)
    def window():calls['window']+=1;ids['window']=cache.current
    @cache.gui(width=200,height=160)
    def ordinary():
        calls['ordinary']+=1
        window(melty_window=True,initial={'window_pos':(30,40)})
    @cache.gui(width=600,height=300)
    def root():
        ids['root']=cache.current
        imgui.set_cursor_screen_pos((model['x'],10))
        ordinary()
    try:
        root();before=cache.window_rect(ids['window'])
        model['x']=65
        cache.invalidate_id(ids['root']);root()
        assert cache.window_rect(ids['window'])[:2]==(before[0]+50,before[1])
        assert calls==Counter(window=1,ordinary=1)
    finally:cache.close()
