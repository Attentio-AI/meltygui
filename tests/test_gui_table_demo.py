"""Exercise the actual table demo, including native boundary pushes and rebasing."""
import runpy
from pathlib import Path

import pytest
import meltygui
import meltygui_imgui as imgui
from conftest import _ensure_gl_context, begin_frame, end_frame
from meltygui.core.rendering.gui_prototype import gui, _window
from meltygui.core.rendering.gui_window_prototype import _GuiWindow
from meltygui.core.rendering.gui_native_collision import NativeCollision


@pytest.mark.parametrize('moving,point', [(False,(150,200)), (False,(850,500)),
                                         (False,(1200,850)), (True,(500,250))])
@pytest.mark.parametrize('native_y', [100,600])
@pytest.mark.parametrize('fill_cell', [False,True])
def test_table_move_resize_and_native_origin_changes(monkeypatch,moving,point,native_y,fill_cell):
    _ensure_gl_context()
    # Load the real example without opening its OS window or entering its loop.
    monkeypatch.setattr(meltygui,'os_window',lambda **kw:gui)
    demo=runpy.run_path(str(Path(__file__).parents[1]/'examples/example_ui.py'))
    host=_GuiWindow();cache=host.cache;geometry=cache.geometry
    token=_window.set(host)
    native=NativeCollision(geometry);geometry.native=native
    screen=((0,2560),(0,1800))

    @gui
    def ordinary_parent(draw_state=None):
        # An ordinary, unbound intermediate view must also survive a native
        # rebase. This was the original demo shape that grew 60px on every pass.
        demo['table'](label='OS table',width=draw_state.width,height=draw_state.height)
        demo['table'](key='nested',label='Nested',melty_window=True,
                      initial={'window_pos':(420,240),'width':700,'height':460})

    root=demo['example_app'] if fill_cell else ordinary_parent
    begin_frame()
    cache._begin_window(1600,1600)
    try:
        native.observe((((100,1352),(native_y,native_y+1081)),screen,'feed',1,((),())))
        root(width=1252,height=1081)
        geometry.pointer(point,drag_position=point,pressed=moving,down=moving,
                         right_pressed=not moving,right_down=not moving)
        assert geometry.gesture is not None
        for dx,dy in ((30,40),(200,300),(600,900),(-100,-200),(0,0)):
            pointer=(point[0]+dx,point[1]+dy)
            geometry.pointer(pointer,drag_position=pointer,down=moving,right_down=not moving)
            cache.flush()
            if fill_cell and point==(1200,850) and (dx,dy)==(30,40):
                # Outermost cell edges belong to the OS frame, not an unrelated
                # intermediate view with the same initial width and height.
                assert native.last==((100,1382),(native_y,native_y+1121))
            native.observe((native.last,screen,'feed',1,((),())))
            width=native.last[0][1]-native.last[0][0]
            height=native.last[1][1]-native.last[1][0]
            imgui.set_cursor_screen_pos((0,0))
            root(width=width,height=height)
            cache.flush()
            settled=tuple(graph.values() for graph in geometry.axes)
            # Holding the pointer still cannot add another rebase or grow cells.
            for _ in range(2):
                geometry.pointer(pointer,drag_position=pointer,down=moving,right_down=not moving)
                cache.flush()
                imgui.set_cursor_screen_pos((0,0))
                root(width=width,height=height)
                cache.flush()
                if fill_cell:
                    for graph,expected in zip(geometry.axes,settled):
                        assert graph.values()==pytest.approx(expected)
        geometry.pointer(point,drag_position=point,released=moving,right_released=not moving)
        cache.flush()
    finally:
        imgui.get_window_draw_list().pop_clip_rect()
        imgui.end();end_frame()
        _window.reset(token);host.close()
