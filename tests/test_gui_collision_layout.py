"""Retained geometry ownership and independent layout recapture."""
from collections import Counter

import pytest

from meltygui.core.rendering.retained_gui_prototype import RetainedGui


def scene(graphics=False,freeze=False,width=400):
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

    @cache.gui(width=width,height=200)
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


def right_drag(cache,press,by=(20,20),reverse=False):
    """Right press at `press` (surface pixels), drag by `by` and back: per axis, the edge that moved."""
    g=cache.geometry
    end=(press[0]+by[0],press[1]+by[1])
    # drag_position as the host gives it: travel that a moving native origin does not feed back.
    g.pointer(press,drag_position=press,right_down=True,right_pressed=True,reverse=reverse)
    targets=dict(g.gesture['targets']); base=[graph.values() for graph in g.axes]
    g.pointer(end,drag_position=end,right_down=True)
    for axis,(edge,opposite,_) in targets.items():
        assert g.axes[axis].position(edge)==base[axis][edge]+by[axis]
        assert g.axes[axis].position(opposite)==base[axis][opposite]
    g.pointer(end,drag_position=press,right_down=True)
    assert [graph.values() for graph in g.axes]==base
    g.pointer(end,drag_position=press,right_released=True)
    assert g.gesture is None
    return tuple(targets[axis][0] for axis in range(2))


# Cell b is 200..400 of the 400 x 200 surface: x takes the cell, y the native frame.
# Both spans are 200, so the band is 30% of them, 60 px.
@pytest.mark.parametrize('press,near',[((230,30),(True,True)),((370,30),(False,True)),
                                       ((230,170),(True,False)),((370,170),(False,False)),
                                       ((259,59),(True,True)),((260,60),(False,False))])
def test_right_drag_takes_the_corner_of_the_cell_nearest_the_press(press,near):
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        cell=layouts['main'].edges[1:]
        assert right_drag(cache,press)==(cell[not near[0]],native.pairs[1][not near[1]])
    finally:cache.close()


def test_right_drag_near_band_is_at_most_400_px():
    from meltygui.core.rendering.gui_native_collision import NativeCollision
    cache,root,calls,ids,layouts,_=scene(width=3000)
    try:
        native=cache.geometry.native=NativeCollision(cache.geometry)
        native.observe((((100.,3100.),(100.,300.)),((0.,4000.),(0.,900.)),'feed',1,((),())))
        left,middle,_=layouts['main'].edges                 # cell a is 1500 wide: 30% would be 450
        assert right_drag(cache,(399,100))[0]==left
        assert right_drag(cache,(400,100))[0]==middle
    finally:cache.close()


# The window is 250 x 208 with its title, at (80, 50): the bands are 75 and 62.4 px.
# Every press is clear of the nested window at (110, 103), 100 x 88.
@pytest.mark.parametrize('press,near',[((85,55),(True,True)),((325,55),(False,True)),
                                       ((85,253),(True,False)),((325,253),(False,False)),
                                       ((154,60),(True,True)),((155,60),(False,True)),
                                       ((85,112),(True,True)),((85,113),(True,False))])
def test_right_drag_takes_the_corner_of_the_window_nearest_the_press(press,near):
    cache,ids,_=window_scene()
    try:
        attach_native(cache,600,450)
        pairs=cache.geometry.windows[ids['window']]
        assert right_drag(cache,press,by=(10,10))==tuple(pairs[axis][not near[axis]] for axis in range(2))
    finally:cache.close()


def test_double_right_drag_takes_both_near_edges_from_the_far_corner():
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        assert right_drag(cache,(370,170),reverse=True)==(layouts['main'].edges[1],native.pairs[1][0])
    finally:cache.close()
    cache,ids,_=window_scene()
    try:
        attach_native(cache,600,450)
        pairs=cache.geometry.windows[ids['window']]
        assert right_drag(cache,(320,240),by=(10,10),reverse=True)==(pairs[0][0],pairs[1][0])
    finally:cache.close()


def test_near_edge_right_drag_pushes_at_the_minimum_and_reverses_exactly():
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        g=cache.geometry;graph=g.axes[0]
        left,divider,right=layouts['main'].edges
        start=graph.values()
        g.pointer((230,100),right_down=True,right_pressed=True)
        g.pointer((330,100),right_down=True)
        assert [graph.position(e) for e in (left,divider,right)]==[100,400,500]
        g.pointer((530,100),right_down=True)                # cell b at its minimum of 70 pushes the frame
        assert [graph.position(e) for e in (left,divider,right)]==[100,600,670]
        assert graph.position(native.pairs[0][1])==670
        g.pointer((230,100),right_down=True)
        assert graph.values()==start
        g.pointer((230,100),right_released=True)
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
        if model['nested']:leaf(melty_window=True,initial={'left': 30, 'top': 25})
    @cache.gui(width=250,height=180,min_width=100,min_height=60)
    def window():
        ids['window']=cache.current
        middle()
    @cache.gui(width=600,height=450)
    def root():
        ids['root']=cache.current
        window(melty_window=True,initial={'left': 80, 'top': 50})
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


def test_native_move_observation_carries_nested_geometry_without_driving_the_os():
    cache,ids,_=window_scene()
    try:
        native=attach_native(cache,600,450)
        g=cache.geometry
        before={node:g.frame_rect(node) for node in g.windows}
        local={node:cache.window_rect(node) for node in g.windows}
        g.pointer((500,350),drag_position=(500,350),pressed=True,down=True)
        assert g.gesture is None and not g.claim[0]
        # Native movement is authoritative. Repeated observations and reversal
        # must preserve all local child positions, without a second move request.
        for dx,dy in ((80,40),(80,40),(-200,-90),(700,600),(0,0)):
            pairs=((100+dx,700+dx),(100+dy,550+dy))
            native.observe((pairs,((0,1200),(0,900)),'feed',1,((),())))
            g.pointer((500,350),drag_position=(900,800),down=True)
            cache.flush()
            assert native.last==pairs and g.gesture is None
            for node,rect in before.items():
                assert g.frame_rect(node)==pytest.approx(
                    (rect[0]+dx,rect[1]+dy,rect[2],rect[3]))
                assert cache.window_rect(node)==pytest.approx(local[node])
    finally:cache.close()


@pytest.mark.parametrize('mode',['feed','walls'])
def test_uncached_native_background_is_not_claimed_by_geometry(mode):
    cache=RetainedGui(graphics=False)
    try:
        native=attach_native(cache,mode=mode)
        before=native.last
        for point in ((50,10),(50,60),(70,90)):
            cache.geometry.pointer(point,drag_position=point,pressed=True,down=True)
            assert cache.geometry.gesture is None
            assert not cache.geometry.claim[0]
            assert native.last==before
    finally:cache.close()


def test_native_background_move_does_not_steal_a_divider():
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        g=cache.geometry
        g.pointer((200,100),drag_position=(200,100),pressed=True,down=True)
        assert not g.gesture['move']
        g.pointer((220,100),drag_position=(220,100),down=True)
        assert native.last==((100,500),(100,300))
        assert g.layout_rect(layouts['main'])==(100,100,400,200)
    finally:cache.close()


def test_native_containment_keeps_interleaved_window_edges():
    cache=RetainedGui(graphics=False);ids={}
    @cache.gui(width=180,height=160,min_width=80,min_height=60)
    def window(input_value):ids[input_value]=cache.current
    @cache.gui(width=600,height=450)
    def root():
        window('a',key='a',melty_window=True,initial={'left': 20, 'top': 40})
        window('b',key='b',melty_window=True,initial={'left': 90, 'top': 120})
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


@pytest.mark.parametrize('native_y',[0.,100.,300.])
@pytest.mark.parametrize('framed_root',[False,True])
@pytest.mark.parametrize('nested',[False,True])
def test_child_opposite_edge_does_not_carry_native_resize(native_y,framed_root,nested):
    from meltygui.core.rendering.gui_native_collision import NativeCollision
    cache=RetainedGui(graphics=False);ids={}
    @cache.gui(width=250,height=180)
    def child():ids['child']=cache.current
    @cache.gui(width=350,height=280)
    def parent():child(melty_window=True,initial={'left': 20, 'top': 40})
    @cache.gui(width=600,height=450)
    def root():
        if framed_root:
            with cache.geometry.declare(('body',),axis=0,key='body',padding=0):pass
        (parent if nested else child)(melty_window=True,initial={'left': 80, 'top': 50})
    try:
        root()
        g=cache.geometry
        native=g.native=NativeCollision(g)
        native.observe((((100,700),(native_y,native_y+450)),((0,1200),(0,900)),'feed',1,((),())))
        before=g.frame_rect(ids['child'])
        # Past the near bands (30% of 250 x 180), so the press takes the far edges.
        start=(before[0]-100+100,before[1]-native_y+100)
        # Bottom meets the display, then the active child's top moves upward.
        # Only after it reaches the native top may it push that top upward too.
        opposite_top=native_y/2
        delta=900-(before[1]+before[3])+(before[1]-28-opposite_top)
        end=(start[0],start[1]+delta)
        g.pointer(start,drag_position=start,right_pressed=True,right_down=True)
        # Opposite-edge expansion consumes the gap above the child first;
        # the native top stays put until the child actually contacts it.
        stage_delta=900-(before[1]+before[3])+(before[1]-28-native_y-25)
        stage=(start[0],start[1]+stage_delta)
        g.pointer(stage,drag_position=stage,right_down=True)
        assert g.frame_rect(ids['child'])[1]==pytest.approx(native_y+25+28)
        assert native.last[1]==pytest.approx((native_y,900))
        for _ in range(4):
            g.pointer(end,drag_position=end,right_down=True)
            cache.flush()
            assert g.frame_rect(ids['child'])==pytest.approx(
                (before[0],opposite_top+28,before[2],900-opposite_top-28))
            assert native.last[1]==pytest.approx((opposite_top,900))
            native.observe((native.last,((0,1200),(0,900)),'feed',1,((),())))
        g.pointer(start,drag_position=start,right_down=True)
        cache.flush()
        assert g.frame_rect(ids['child'])==pytest.approx(before)
        assert native.last[1]==pytest.approx((native_y,native_y+450))
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


def test_host_stabilized_drag_does_not_add_native_translation():
    from types import SimpleNamespace
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        # The real host has already removed surface movement from its pointer.
        # Each commit below immediately acknowledges the requested native move.
        native.os_frame=SimpleNamespace(
            applied_origin=lambda axis:native.last[axis=='y'][0],
            prototype_commit=lambda pairs:None)
        g=cache.geometry
        g.pointer((60,70),drag_position=(60,70),right_pressed=True,
                  right_down=True,reverse=True)
        for _ in range(5):
            g.pointer((30,40),drag_position=(30,40),right_down=True)
            cache.flush()
            assert native.last==((70,500),(70,300))
        # Return to the original hand position within the same held gesture.
        g.pointer((60,70),drag_position=(60,70),right_down=True)
        cache.flush()
        assert native.last==((100,500),(100,300))
        g.pointer((30,40),drag_position=(30,40),right_down=True)
        # Hover IO returns to native-local coordinates on release, but the
        # separate drag sample includes the final 5px without rebasing the hand.
        g.pointer((60,70),drag_position=(25,35),right_released=True)
        assert native.last==((65,500),(65,300))
        assert g.gesture is None
    finally:cache.close()


def test_native_reply_during_drag_preserves_reversal_and_updated_walls():
    cache,root,calls,ids,layouts,_=scene()
    try:
        native=attach_native(cache)
        g=cache.geometry
        g.pointer((300,100),drag_position=(300,100),right_pressed=True,right_down=True)
        g.pointer((1100,750),drag_position=(1100,750),right_down=True)
        token=g.gesture['token']
        # A delayed native size/move reply also reports a stricter display wall.
        native.observe((((95,1195),(45,895)),((0,1195),(0,895)),'feed',1,((),())))
        assert g.gesture['token']==token
        g.pointer((1100,750),drag_position=(1100,750),right_down=True)
        assert native.last[0][1]<=1195 and native.last[1][1]<=895
        g.pointer((300,100),drag_position=(300,100),right_down=True)
        assert native.last==((100,500),(100,300))
        g.pointer((300,100),drag_position=(300,100),right_released=True)
        # With no owned drag, a genuine native move is adopted normally.
        native.observe((((120,520),(120,320)),((0,1200),(0,900)),'feed',1,((),())))
        assert native.last==((120,520),(120,320))
    finally:cache.close()


@pytest.mark.parametrize('graphics',[False,True])
def test_fractional_nested_cells_settle_without_growing_native_frame(graphics):
    if graphics:
        from conftest import _ensure_gl_context
        _ensure_gl_context()
    cache=RetainedGui(graphics=graphics)
    ids={}
    @cache.gui
    def leaf(input_value,draw_state=None):
        ids[input_value]=cache.current
    @cache.gui
    def middle(input_value):
        with cache.geometry.declare(('a','b'),axis=1,key='rows',padding=6) as layout:
            for name in ('a','b'):
                with layout.cell(name):leaf(name,key=name)
    @cache.gui(width=1040,height=705)
    def root(input_value):
        with cache.geometry.declare(('left','middle','right'),axis=0,key='cols',padding=6) as layout:
            with layout.cell('middle'):middle(None)
    try:
        root(None)
        native=attach_native(cache,1040,705)
        for _ in range(3):
            root(None)
            cache.flush()
            assert native.last==((100,1140),(100,805))
            assert cache.graph.info(ids['a'])['rect'][2:]==pytest.approx((1040/3-24,334.5))
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
        window(melty_window=True,initial={'left': 30, 'top': 40})
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


@pytest.mark.parametrize('graphics', [False, True])
def test_nested_cells_converge_across_native_size_changes(graphics):
    """Fractional captures must not feed rounded sizes back into shared edges."""
    if graphics:
        from conftest import _ensure_gl_context
        _ensure_gl_context()
    cache = RetainedGui(graphics=graphics)
    ids = {}

    @cache.gui
    def panel(input_value):
        pass

    @cache.gui
    def split(input_value):
        with cache.geometry.declare(('top', 'bottom'), axis=1, key='rows',
                                    mins=90, padding=6) as layout:
            for key in layout.keys:
                with layout.cell(key):
                    panel(None, key=key)

    @cache.gui
    def root(input_value):
        ids['root'] = cache.current
        with cache.geometry.declare(('controls', 'rows', 'panel'), axis=0,
                                    key='columns', mins=(220, 140, 140), padding=6) as layout:
            with layout.cell('controls'):
                panel(None, key='controls')
            with layout.cell('rows'):
                split(None)
            with layout.cell('panel'):
                panel(None, key='right')

    try:
        # Shrinking after the first resize used to exhaust all 64 flush passes.
        for width, height in [(1936, 1602), (1395, 1239), (1040, 705), (1395, 1239)] * 2:
            root(None, width=width, height=height)
            cache.flush()
            assert not cache.graph.dirty_nodes()
            assert cache.graph.info(ids['root'])['rect'][2:] == (width, height)
            geometry = [axis.values() for axis in cache.geometry.axes]
            executions = {node: cache.graph.info(node)['executions'] for node in cache.records}
            root(None, width=width, height=height)
            cache.flush()
            assert [axis.values() for axis in cache.geometry.axes] == geometry
            assert {node: cache.graph.info(node)['executions'] for node in cache.records} == executions
    finally:
        cache.close()


def test_rebuilt_layout_retires_the_cross_layouts_in_its_cells():
    """New keys rebuild the columns. The rows declared in a later cell spanned between the old
    dividers: a child running in an earlier cell must not find them still bound to those."""
    cache=RetainedGui(graphics=False); model={'keys':('a','b','c')}; ids={}

    @cache.gui()
    def child(input_value):
        return False,input_value

    @cache.gui(width=600,height=300)
    def root(input_value):
        keys=model['keys']; ids['root']=cache.current
        with cache.geometry.declare(keys,axis=0,key='outer') as outer:
            for key in keys[:-1]:
                with outer.cell(key):child(key,key=key)
            with outer.cell(keys[-1]):
                with cache.geometry.declare(('top','bottom'),axis=1,key='inner') as inner:
                    for key in ('top','bottom'):
                        with inner.cell(key):child(key,key=key)
        return False,input_value
    try:
        root(None);cache.flush()
        model['keys']=('a','new','b','c')
        cache.invalidate_id(ids['root']);root(None);cache.flush()
        inner=cache.geometry.layouts[(ids['root'],1,'inner')]
        outer=cache.geometry.layouts[(ids['root'],0,'outer')]
        assert inner.cross==(outer.edges[3],outer.edges[4])
    finally:cache.close()


def test_anchor_ties_a_filling_edge_to_the_window_and_keeps_a_fixed_size():
    """A view that fills its window down to the bottom, 330 px wide with 250 px to its right: the
    bottom edge follows the window's, the width is the caller's whatever the window does."""
    cache=RetainedGui(graphics=False); ids={}

    @cache.gui()
    def tiles(input_value):
        ids['tiles']=cache.current
        cache.geometry.anchor(cache.current)
        with cache.geometry.declare(('a','b'),axis=0,key='main'):pass
        return False,input_value

    @cache.gui(width=600,height=300)
    def root(input_value):
        ids['root']=cache.current
        tiles(None,name='tiles',left=20.,top=0.,width=330.,height=300.)
        return False,input_value
    try:
        root(None);cache.flush()
        geometry=cache.geometry
        assert ids['tiles'] in geometry.anchored
        (left,right),(top,bottom)=geometry.frames[ids['root']][1]
        geometry.axes[1].solve(bottom,geometry.axes[1].position(bottom)+50,[top])
        geometry.axes[0].solve(right,geometry.axes[0].position(right)+50,[left])
        geometry.apply();cache.flush()
        assert cache.graph.info(ids['tiles'])['rect']==(20,0,330,350)
    finally:cache.close()


def fixed_tiles(mins=60):
    """Columns a | b in a view of a fixed 400 x 400 at (100, 100) of a 700 x 600 Melty window."""
    cache=RetainedGui(graphics=False); ids={}; layouts={}

    @cache.gui()
    def tiles(input_value):
        ids['tiles']=cache.current
        cache.geometry.anchor(cache.current)
        layouts['main']=cache.geometry.declare(('a','b'),axis=0,key='main',mins=mins)
        with layouts['main']:pass
        return False,input_value

    @cache.gui(width=700,height=600)
    def window(input_value):
        ids['window']=cache.current
        tiles(None,name='tiles',left=100.,top=100.,width=400.,height=400.)
        return False,input_value

    @cache.gui(width=1400,height=1000)
    def root(input_value):
        window(None,name='window',melty_window=True,initial={'left':50,'top':50})
        return False,input_value
    root(None);cache.flush()
    return cache,root,ids,layouts


def test_a_divider_moves_a_view_of_a_fixed_size_by_moving_its_window_edge():
    """The view's size and place in its window are its caller's. To move its right edge the
    solver moves its left one, and with that the window's left edge: the window is resized, the
    view is not. Nothing springs back when the views run again."""
    cache,root,ids,layouts=fixed_tiles()
    try:
        geometry=cache.geometry
        assert geometry.frame_rect(ids['window'])[::2]==(50,700)
        geometry.drag(layouts['main'],1,200);cache.flush()           # 140 to b's minimum, 60 more
        geometry.end_gesture();cache.flush()
        for node in (ids['window'],ids['tiles']):
            cache.invalidate_id(node);root(None);cache.flush()
        assert layouts['main'].sizes()==[340,60]
        assert cache.graph.info(ids['tiles'])['rect']==(100,100,400,400)
        assert geometry.frame_rect(ids['tiles'])[::2]==(210,400)
        assert geometry.frame_rect(ids['window'])[::2]==(110,640)    # its left edge went along
        geometry.drag(layouts['main'],1,-500,token=2);cache.flush()  # 280 to a's minimum, 220 more
        assert layouts['main'].sizes()==[60,340]
        assert geometry.frame_rect(ids['tiles'])[::2]==(-10,400)
        assert geometry.frame_rect(ids['window'])[::2]==(-110,860)
    finally:cache.close()


def test_a_view_of_a_fixed_size_moves_the_os_window_edge_and_reverses():
    """draw_tiles(left=100, top=100, width=400, height=400) in an OS window: a divider dragged
    past the right tile's minimum moves the OS window's left edge, and back with the drag."""
    cache=RetainedGui(graphics=False); ids={}; layouts={}

    @cache.gui()
    def tiles(input_value):
        ids['tiles']=cache.current
        cache.geometry.anchor(cache.current)
        layouts['main']=cache.geometry.declare(('a','b'),axis=0,key='main',mins=60)
        with layouts['main']:pass
        return False,input_value

    @cache.gui(width=900,height=700)
    def root(input_value):
        tiles(None,name='tiles',left=100.,top=100.,width=400.,height=400.)
        return False,input_value
    root(None);cache.flush()
    try:
        native=attach_native(cache,900,700)
        root(None);cache.flush()
        geometry=cache.geometry; graph=geometry.axes[0]
        start=graph.values()
        left,right=native.pairs[0]
        assert (graph.position(left),graph.position(right))==(100,1000)
        geometry.drag(layouts['main'],1,200,token=9)                 # 140 to b's minimum, 60 more
        assert (graph.position(left),graph.position(right))==(160,1000)
        assert geometry.frame_rect(ids['tiles'])[::2]==(260,400)
        assert layouts['main'].sizes()==[340,60]
        geometry.drag(layouts['main'],1,0,token=9)
        assert graph.values()==start
    finally:cache.close()


def test_a_view_of_a_fixed_size_too_small_for_its_cells_is_still_laid_out():
    cache,root,ids,layouts=fixed_tiles(mins=260)
    try:
        assert sum(layouts['main'].sizes())>=520
    finally:cache.close()


def test_a_wrapping_view_is_as_small_as_its_cells_and_stays_so():
    """draw_tiles(width=None, height=FILL): the width is the least its columns can be solved
    to, the height the window's. A divider cannot make it wider, so it moves the view and the
    window's left edge; running again does not end the drag; another column widens it."""
    from meltygui.core.rendering.gui_size import FILL
    cache=RetainedGui(graphics=False); ids={}; layouts={}; model={'keys':('a','b','c')}

    @cache.gui()
    def tiles(input_value):
        ids['tiles']=cache.current
        cache.geometry.anchor(cache.current)
        layouts['main']=cache.geometry.declare(model['keys'],axis=0,key='main',mins=60)
        with layouts['main']:pass
        return False,input_value

    @cache.gui(width=700,height=600)
    def window(input_value):
        ids['window']=cache.current
        tiles(None,name='tiles',left=100.,top=0.,width=None,height=FILL)
        return False,input_value

    @cache.gui(width=1400,height=1000)
    def root(input_value):
        window(None,name='window',melty_window=True,initial={'left':50,'top':50})
        return False,input_value
    root(None);cache.flush()
    try:
        geometry=cache.geometry
        assert layouts['main'].sizes()==[60,60,60]
        assert geometry.frame_rect(ids['tiles'])[2:]==(180,600)
        geometry.pointer((210,300),down=True,pressed=True)            # the divider between a and b
        geometry.pointer((250,300),down=True);cache.flush()
        assert geometry.gesture is not None                           # the views ran again: still dragging
        geometry.pointer((310,300),down=True);cache.flush()
        geometry.pointer((310,300),released=True);cache.flush()
        assert layouts['main'].sizes()==[60,60,60]
        assert geometry.frame_rect(ids['tiles'])[::2]==(250,180)
        assert geometry.frame_rect(ids['window'])[::2]==(150,600)     # its left edge went along
        (left,right),(top,bottom)=geometry.windows[ids['window']]
        geometry.axes[1].solve(bottom,geometry.axes[1].position(bottom)+80,[top])
        geometry.apply();cache.flush()
        assert geometry.frame_rect(ids['tiles'])[2:]==(180,680)       # the height fills
        model['keys']=('a','b','c','d')
        cache.invalidate_id(ids['tiles']);root(None);cache.flush()
        assert layouts['main'].sizes()==[60,60,60,60]
        assert geometry.frame_rect(ids['tiles'])[2]==240
    finally:cache.close()


def test_the_solver_does_not_author_the_place_of_a_view_it_constrains():
    """A view `anchor` constrains is placed by its caller: what the solver gives it is not
    saved as authored, and an authored rectangle a session kept from before is dropped. So a
    `left` the call passes no longer is gone on the next run, not held against the flow."""
    cache=RetainedGui(graphics=False); ids={}; model={'placed':{'left':100.,'top':40.}}

    @cache.gui()
    def tiles(input_value):
        ids['tiles']=cache.current
        cache.geometry.anchor(cache.current)
        with cache.geometry.declare(('a','b'),axis=0,key='main'):pass
        return False,input_value

    @cache.gui(width=600,height=300)
    def root(input_value):
        ids['root']=cache.current
        tiles(None,name='tiles',**model['placed'])
        return False,input_value
    try:
        root(None);cache.flush()
        geometry=cache.geometry
        state=cache.graph.state(ids['tiles'])
        (left,right),(top,bottom)=geometry.frames[ids['root']][1]
        geometry.axes[0].solve(right,geometry.axes[0].position(right)+50,[left])
        geometry.apply();cache.flush()
        assert cache.graph.info(ids['tiles'])['rect']==(100,40,550,260)      # it fills: the solver widened it
        assert not set(state.save_state()[2])&{'left','top','width','height'}
        # What a session saved before: the solver's rectangle, authored. The call passes no place now.
        for name,value in (('left',100.),('top',40.),('width',550.),('height',260.)):setattr(state,name,value)
        geometry.release(ids['tiles'])
        model['placed']={}
        cache.invalidate_id(ids['root']);root(None);cache.flush()
        assert cache.graph.info(ids['tiles'])['rect'][:2]==(0,0)
        assert not set(state.save_state()[2])&{'left','top','width','height'}
    finally:cache.close()


def test_apply_places_only_the_cells_that_changed():
    """`GuiGeometry.apply` works out a cell's rectangle when one of its edges moved, and no
    other's: a committed view's own cell and its layout's cells are placed from the kept one."""
    cache=RetainedGui(graphics=False)
    ids={}; layouts={}

    @cache.gui()
    def child(input_value):
        ids[input_value]=cache.current
        return False,input_value

    @cache.gui(width=600,height=200)
    def root(input_value):
        ids['root']=cache.current
        layout=layouts['main']=cache.geometry.declare(('a','b','c'),axis=0,key='main',mins=70,padding=5)
        with layout:
            for key in ('a','b','c'):
                with layout.cell(key):child(key,key=key)
        return False,input_value
    root(None)
    try:
        geometry=cache.geometry
        visited=[]
        plain=geometry.cell_rect
        def cell_rect(layout,index):
            visited.append(layout.keys[index])
            return plain(layout,index)
        geometry.cell_rect=cell_rect
        geometry.apply()
        assert visited==[]                                  # nothing moved since the last commit
        geometry.apply(ids['b'])
        assert visited==[]                                  # its own cell: placed from the kept rectangle
        geometry.drag(layouts['main'],1,40)                 # the divider between a and b
        assert sorted(set(visited))==['a','b']
        assert cache.graph.info(ids['a'])['rect']==(5,5,230,190)
        assert cache.graph.info(ids['c'])['rect']==(405,5,190,190)
        del visited[:]
        geometry.apply(ids['root'])
        assert sorted(set(visited))==['c']                  # the owner's commit places all its cells:
        assert cache.graph.info(ids['c'])['rect']==(405,5,190,190)   # only c's is worked out anew
    finally:cache.close()


def nested(graphics, keep):
    """A root with columns a | (rows b / c) declared by the one owner, as draw_tiles nests splits."""
    cache=RetainedGui(graphics=graphics)
    calls=Counter(); layouts={}; sizes={}

    @cache.gui()
    def child(input_value):
        calls[input_value]+=1
        return False,input_value

    @cache.gui(width=400,height=200)
    def root(input_value):
        calls['root']+=1
        outer=layouts['outer']=cache.geometry.declare(('a','right'),axis=0,key='outer',mins=70,padding=5)
        with outer:
            with outer.cell('a'):child('a',key='a')
            with outer.cell('right'):
                inner=layouts['inner']=cache.geometry.declare(('b','c'),axis=1,key='inner',mins=40,padding=5)
                with inner:
                    for key in ('b','c'):
                        with inner.cell(key):child(key,key=key)
        if keep:
            sizes['outer']=outer.sizes()
        return False,input_value
    root(None)
    return cache,calls,layouts,sizes


def test_a_layout_nested_in_a_cell_does_not_make_the_outer_one_raw():
    from conftest import _ensure_gl_context
    _ensure_gl_context()
    cache,calls,layouts,_=nested(True,False)
    try:
        assert not layouts['outer'].raw and not layouts['inner'].raw
        g=cache.geometry
        g.pointer((200,50),down=True,pressed=True)
        g.pointer((250,50),down=True)
        cache.flush()
        g.pointer((250,50),released=True)
        cache.flush()
        assert calls['root']==1                             # the drag ran the resized cells only
        assert calls['a']==2 and calls['b']==2 and calls['c']==2
    finally:cache.close()


def test_an_owner_that_asked_for_sizes_runs_once_when_the_drag_ends():
    cache,calls,layouts,sizes=nested(False,True)
    try:
        g=cache.geometry
        assert sizes['outer']==[200,200]
        g.pointer((200,50),down=True,pressed=True)
        for x in (220,250):
            g.pointer((x,50),down=True)
            cache.flush()
        assert calls['root']==1 and sizes['outer']==[200,200]
        g.pointer((250,50),released=True)
        cache.flush()
        assert calls['root']==2 and sizes['outer']==[250,150]
        g.pointer((250,50),down=True,pressed=True)         # a press and release that moves nothing
        g.pointer((250,50),released=True)
        cache.flush()
        assert calls['root']==2
    finally:cache.close()


@pytest.mark.parametrize('height,frame', [(400, 400), (330, 330), (200, 320)])
def test_a_layout_declared_with_too_little_room_is_solved_like_a_resize(height, frame):
    """Saved sizes scaled into a smaller window leave a nested layout short: its edges push the
    cells around it, and what the window cannot give grows the frame, as a runtime shrink does."""
    cache = RetainedGui(graphics=False); layouts = {}; ids = {}
    @cache.gui(width=300, height=height)
    def root():
        ids['root'] = cache.current
        with cache.geometry.declare(3, axis=1, key='outer', sizes=(300, 100, 400), mins=40, padding=0) as outer:
            layouts['outer'] = outer
            with outer.cell(1):
                layouts['inner'] = cache.geometry.declare(6, axis=1, key='inner', mins=40, padding=0)
    try:
        root()
        assert cache.geometry.frame_rect(ids['root'])[3] == frame
        for layout in layouts.values():
            assert all(size >= 40 - 1e-6 for size in layout.sizes())
        assert layouts['outer'].sizes()[1] == sum(layouts['inner'].sizes()) == 240
    finally:
        cache.close()


def test_a_window_resizes_only_along_the_axes_its_call_does_not_size():
    """Nothing passed: the user's to resize (a size on the definition is where it starts). A number
    on the call pins that axis; None makes it as large as the content. A pinned axis is a span at
    its minimum and maximum: a drag there moves the opposite edge with it, so the window travels."""
    cache=RetainedGui(graphics=False);ids={}
    @cache.gui(width=180,height=120,min_width=80,min_height=50)
    def window(input_value):ids[input_value]=cache.current
    @cache.gui(width=900,height=600)
    def root():
        window('free',key='free',melty_window=True,initial={'left': 20, 'top': 40})
        window('high',key='high',melty_window=True,height=100,initial={'left': 320, 'top': 40})
        window('wide',key='wide',melty_window=True,width=150,initial={'left': 620, 'top': 40})
        window('wrap',key='wrap',melty_window=True,width=None,height=None,initial={'left': 20, 'top': 320})
    def drag(name):
        x,y,w,h=cache.window_rect(ids[name])
        g=cache.geometry
        g.pointer((x+w-10,y+h-10),right_pressed=True,right_down=True)
        g.pointer((x+w+30,y+h+20),right_down=True)
        cache.flush()
        g.pointer((x+w+30,y+h+20),right_released=True)
        cache.flush()
        after=cache.window_rect(ids[name])
        return after[2]-w,after[3]-h,after[0]-x,after[1]-y
    try:
        root();attach_native(cache,900,600);cache.flush()
        assert cache.window_rect(ids['high'])[3]==100 and cache.window_rect(ids['wide'])[2]==150
        assert drag('free')==(40,30,0,0)
        assert drag('high')==(40,0,0,30)
        assert drag('wide')==(0,30,40,0)
        assert drag('wrap')==(0,0,40,30)
        assert cache.window_rect(ids['high'])[3]==100 and cache.window_rect(ids['wide'])[2]==150
    finally:cache.close()
